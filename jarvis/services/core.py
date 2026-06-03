"""«Мозг» Джарвиса: диспетчер интентов поверх локальной 1.5B (Ollama).

Локальная модель работает КЛАССИФИКАТОРОМ: по реплике из jarvis/input она через
structured output выдаёт интент (os_command | casual_talk) и, для команды, тег из
commands.yaml + короткое подтверждение в характере. Дальше:
  os_command  → подтверждение в jarvis/say + тег в jarvis/execute (быстро, офлайн);
  casual_talk → сперва локальные инструменты (время/заряд), иначе живой ответ от
                casual-бэкенда (Gemini), и он же — в jarvis/say.
Так команды звучат мгновенно и без сети, а беседу ведёт облако.
"""
import threading
from collections import deque
from datetime import datetime

import ollama
import yaml

from jarvis import config, contracts
from jarvis.bus import JarvisModule
from jarvis.casual import CasualBackend

# Ключевые слова локальных инструментов (срабатывают на явный вопрос в беседе).
_TIME_KEYS = (
    "который час", "которой час", "которое время", "сколько времени",
    "сколько время", "точное время", "время сейчас", "сколько на часах",
)
_BATTERY_KEYS = ("батаре", "аккумулят", "заряд", "сколько процент")


def _build_system_prompt() -> str:
    """Собирает системный промпт с актуальным списком команд из commands.yaml.

    Теги загружаются динамически, чтобы Мозг видел только реально существующие
    команды — никаких выдуманных тегов модель не сгенерирует, потому что
    неоткуда их взять.
    """
    try:
        with open(config.COMMANDS_FILE, encoding="utf-8") as f:
            commands = yaml.safe_load(f) or {}
    except Exception:
        commands = {}
    tags = sorted(commands.keys())  # алфавитный порядок для стабильности промпта
    tag_list = ", ".join(tags) if tags else "(нет доступных команд)"

    return (
        "Ты — Джарвис, голосовой ассистент-диспетчер в характере дворецкого ИИ "
        "(в традиции J.A.R.V.I.S.): обращаешься на «сэр», по-русски, спокойно и "
        "лаконично. Твоя задача — КЛАССИФИЦИРОВАТЬ реплику пользователя и вернуть "
        "ТОЛЬКО валидный JSON по схеме. Развёрнутую беседу ведёт другой модуль — "
        "ты её НЕ пишешь.\n\n"
        "ИНТЕНТЫ (ровно два):\n"
        "1. os_command — пользователь явно просит выполнить ОДНУ из команд ниже. "
        "Тег бери РОВНО из списка, ничего не выдумывай. Если подходящей команды в "
        "списке нет — это НЕ os_command, ставь casual_talk.\n"
        f"Доступные команды (теги): {tag_list}.\n"
        "2. casual_talk — всё остальное: беседа, приветствие, вопрос о тебе или о "
        "мире, просьба рассказать или найти что-то, шутка, «как дела», а также "
        "вопрос о времени или заряде батареи.\n\n"
        "ПОЛЯ JSON:\n"
        "- intent: 'os_command' или 'casual_talk'.\n"
        "- payload: для os_command — РОВНО один тег из списка выше; для casual_talk — 'none'.\n"
        "- speech_response: ТОЛЬКО для os_command — короткое подтверждение в "
        "характере, ОДНОЙ фразой, на русском, с обращением «сэр», без острот и "
        "лишних слов (например «Выключаю Wi-Fi, сэр», «Сделано, сэр», "
        "«Запускаю Telegram, сэр»). Для casual_talk оставь пустую строку.\n"
    )


class CoreModule(JarvisModule):
    """«Мозг»: преобразует текст пользователя в реплику и тег команды."""

    def __init__(self):
        super().__init__("jarvis-core")
        # История диспетчера: до HISTORY_SIZE пар user/assistant -> 2 сообщения на пару.
        self._history: deque = deque(maxlen=config.HISTORY_SIZE * 2)
        # История команд (структурно) — последние 3, на будущее для commands.yaml-фич.
        self._cmd_history: deque = deque(maxlen=3)
        self._client = ollama.Client(host=config.OLLAMA_HOST)
        self._casual = CasualBackend(self.log)  # беседу ведёт Gemini (или фоллбэк)
        self._lock = threading.Lock()
        self._system_prompt = _build_system_prompt()

    def on_start(self):
        self.subscribe(contracts.TOPIC_INPUT, self.on_input)

    def on_input(self, payload: dict):
        text = (payload.get("text") or "").strip()
        if not text:
            return
        # Обработку выносим в поток, чтобы не блокировать MQTT-loop
        threading.Thread(target=self._process, args=(text,), daemon=True).start()

    def _process(self, text: str):
        # Один LLM-запрос за раз: на N100 нельзя держать две сессии в RAM
        with self._lock:
            self.set_state(contracts.STATE_THINKING)
            try:
                messages = (
                    [{"role": "system", "content": self._system_prompt}]
                    + list(self._history)
                    + [{"role": "user", "content": text}]
                )
                response = self._client.chat(
                    model=config.OLLAMA_MODEL,
                    messages=messages,
                    format=contracts.JarvisResponse.model_json_schema(),
                    keep_alive=config.OLLAMA_KEEP_ALIVE,
                )
                content = response["message"]["content"]
                result = contracts.JarvisResponse.model_validate_json(content)
            except Exception:
                self.log.exception("Ошибка запроса к Ollama (диспетчер)")
                self.say("Прошу прощения, сэр, у меня сбой в мыслительном модуле.")
                self.set_state(contracts.STATE_IDLE)
                return

            # Выполняем интент и получаем финальную реплику (она пойдёт в историю).
            reply = self._dispatch(text, result)

            # Обновляем историю диспетчера (FIFO ограничен maxlen у deque):
            # пара user → финальная произнесённая реплика.
            self._history.append({"role": "user", "content": text})
            self._history.append({"role": "assistant", "content": reply})

    def _dispatch(self, text: str, result: contracts.JarvisResponse) -> str:
        """Выполнить намерение, озвучить реплику и вернуть её текст (для истории).

        Реплику публикуем в jarvis/say — TTS подхватит и сам выставит speaking/idle.
        idle здесь НЕ ставим: иначе на шине «вспышка» idle между thinking и speaking.
        """
        if (
            result.intent == "os_command"
            and result.payload
            and result.payload != "none"
        ):
            # Подтверждение генерит локальная модель; подстраховка — на случай пустого.
            speech = result.speech_response.strip() or "Сделано, сэр."
            self.say(speech)
            self.publish_json(
                contracts.TOPIC_EXECUTE,
                {"command_tag": result.payload},
                qos=contracts.QOS_EXECUTE,
            )
            self._cmd_history.append({
                "intent": result.intent,
                "tag": result.payload,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            })
            return speech

        # casual_talk (и os_command без валидного тега тоже трактуем как беседу).
        # Сначала локальные инструменты (офлайн), иначе — облачный casual-бэкенд.
        local = self._local_tool_answer(text)
        if local is not None:
            self.say(local)
            return local
        answer = self._casual.reply(text)
        self.say(answer)
        return answer

    def _local_tool_answer(self, text: str) -> str | None:
        """Локальные инструменты (офлайн, без Gemini): время и заряд батареи.

        Срабатывают только на явный вопрос — иначе None, и беседу ведёт casual-бэкенд.
        """
        low = text.lower()
        if any(k in low for k in _TIME_KEYS):
            return f"Сейчас {datetime.now():%H:%M}, сэр."
        if any(k in low for k in _BATTERY_KEYS):
            return self._battery_answer()
        return None

    def _battery_answer(self) -> str:
        """Реальный заряд из /sys/class/power_supply/BAT* — в характере Джарвиса."""
        import glob

        paths = sorted(glob.glob("/sys/class/power_supply/BAT*"))
        if not paths:
            return "Батарея не обнаружена, сэр."
        bat = paths[0]
        try:
            with open(f"{bat}/capacity", encoding="utf-8") as f:
                capacity = int(f.read().strip())
        except Exception:
            self.log.exception("Не удалось снять показания батареи")
            return "Не удалось снять показания батареи, сэр."
        status = ""
        try:
            with open(f"{bat}/status", encoding="utf-8") as f:
                status = f.read().strip()
        except Exception:
            pass
        if status == "Charging":
            return f"Батарея на {capacity} процентах, заряжается, сэр."
        if status == "Full" or capacity >= 99:
            return "Батарея заряжена полностью, сэр."
        return f"Батарея на {capacity} процентах, сэр."


def main():
    CoreModule().run()


if __name__ == "__main__":
    main()
