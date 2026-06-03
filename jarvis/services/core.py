"""«Мозг» Джарвиса: слушает jarvis/input, спрашивает Ollama и отвечает.

Использует нативный structured output Ollama (format = JSON-схема pydantic),
держит короткую историю диалога и публикует реплику в jarvis/say, а теги
команд — в jarvis/execute.
"""
import threading
from collections import deque

import ollama
import yaml

from jarvis import config, contracts
from jarvis.bus import JarvisModule


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
        "Ты — Джарвис, локальный голосовой ассистент в стиле Тони Старка: "
        "краткий, остроумный, обращаешься на «сэр». Отвечай строго на русском. "
        "Определи намерение пользователя и верни ТОЛЬКО валидный JSON по схеме.\n\n"
        "ПРАВИЛА intents:\n"
        "1. os_command — пользователь явно просит выполнить ОДНУ из команд ниже. "
        "Проверь, что запрос действительно совпадает с командой из списка. "
        "ЕСЛИ В СПИСКЕ НЕТ подходящей команды — НЕ используй os_command, "
        "переключись на casual_talk и честно скажи, что такой команды нет.\n"
        f"Доступные команды: {tag_list}.\n"
        "2. web_search — пользователь просит найти информацию в интернете "
        "(например «сколько времени», «какая погода», «найди рецепт»). "
        "НО ЕСЛИ ты можешь ответить сам (общие знания, дата, факты) — используй "
        "casual_talk и ответь своими словами, без поиска.\n"
        "3. casual_talk — всё остальное: приветствие, вопрос о тебе, беседа, "
        "просьба рассказать что-то, шутка, вопрос «как дела». Здесь ты — живой "
        "собеседник, отвечаешь осмысленно и по-русски, в характере Джарвиса.\n\n"
        "ПРАВИЛА payload:\n"
        "- Для os_command: РОВНО один тег из списка выше (например 'telegram'). "
        "НЕ придумывай теги — только из списка. Если подходящего тега нет, "
        "НЕ используй os_command.\n"
        "- Для web_search: 'none'.\n"
        "- Для casual_talk: 'none'.\n\n"
        "ПРАВИЛА speech_response:\n"
        "- Всегда на русском, от первого лица (Джарвис), кратко (1–3 предложения).\n"
        "- Для os_command: подтверди выполнение (например «Запускаю Telegram, сэр»).\n"
        "- Для web_search: скажи, что поиск пока в разработке, но предложи помощь.\n"
        "- Для casual_talk: отвечай как живой собеседник — вежливо, с лёгкой иронией, "
        "в духе дворецкого-ИИ. НИКОГДА не говори «запрос не может быть обработан», "
        "«предоставьте запрос для поиска» и подобных канцелярских фраз. "
        "НИКОГДА не давай ссылки на google или другие сайты — ты голосовой ассистент, "
        "а не поисковик."
    )


class CoreModule(JarvisModule):
    """«Мозг»: преобразует текст пользователя в реплику и тег команды."""

    def __init__(self):
        super().__init__("jarvis-core")
        # История: до HISTORY_SIZE пар user/assistant -> 2 сообщения на пару
        self._history: deque = deque(maxlen=config.HISTORY_SIZE * 2)
        self._client = ollama.Client(host=config.OLLAMA_HOST)
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
                self.log.exception("Ошибка запроса к Ollama")
                self.say("Прошу прощения, сэр, у меня сбой в мыслительном модуле.")
                self.set_state(contracts.STATE_IDLE)
                return

            # Обновляем историю (FIFO ограничен maxlen у deque)
            self._history.append({"role": "user", "content": text})
            self._history.append(
                {"role": "assistant", "content": result.speech_response}
            )

            # Публикуем реплику — TTS подхватит и сам выставит speaking/idle.
            # НЕ ставим idle здесь: иначе на шине видна «вспышка» idle между
            # thinking и speaking, а на двойной ответ (реплика + ошибка агента)
            # получается два цикла speaking→idle.
            self.say(result.speech_response)
            if (
                result.intent in ("os_command", "web_search")
                and result.payload
                and result.payload != "none"
            ):
                self.publish_json(
                    contracts.TOPIC_EXECUTE,
                    {"command_tag": result.payload},
                    qos=contracts.QOS_EXECUTE,
                )


def main():
    CoreModule().run()


if __name__ == "__main__":
    main()
