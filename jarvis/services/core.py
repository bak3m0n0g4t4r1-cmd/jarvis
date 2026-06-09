"""«Мозг» Джарвиса: лёгкий распознаватель команд (без облака и без LLM).

Реплика из jarvis/input проходит так:
  1. встроенные info-ответы (время/заряд) — офлайн, мгновенно, в характере;
  2. матчер (правила + ONNX-эмбеддинги, см. jarvis/matcher.py) → тег команды →
     подтверждение в jarvis/say + тег в jarvis/execute (исполняет OS-агент);
  3. ничего не распознано → переспрос в характере (без падения).

Никакой генеративной модели: ум заменён на дешёвый гибридный матчер, чтобы
N100/8 ГБ не тормозил. Команды звучат мгновенно и работают офлайн.
"""
import logging
import random
import threading
from datetime import datetime

import yaml

from jarvis import config, contracts, phrases, worldtime
from jarvis.breaks import is_stop_phrase
from jarvis.bus import JarvisModule
from jarvis.matcher import NOT_RECOGNIZED, Matcher
from jarvis.reminders import is_dialog_pending, is_scheduler_command
from jarvis.sysinfo import read_battery, read_system_load, read_volume
from jarvis.speech import say_date, say_percent, say_time

# Ключевые слова встроенных info-ответов (срабатывают на явный вопрос).
_TIME_KEYS = (
    "который час", "которой час", "которое время", "сколько времени",
    "сколько время", "точное время", "время сейчас", "сколько на часах",
)
_BATTERY_KEYS = ("батаре", "аккумулят", "заряд", "сколько процент")
_DATE_KEYS = ("какое сегодня число", "какое число", "какая дата", "какой сегодня день",
              "число сегодня", "сегодня какое", "какой день недели", "какое число сегодня")
_VOLUME_KEYS = ("какая громкость", "уровень громкости", "текущая громкость",
                "насколько громко", "громкость сейчас", "какая сейчас громкость")
_LOAD_KEYS = ("загрузка системы", "загрузка процессора", "загрузка цпу", "нагрузка системы",
              "сколько памяти", "сколько оперативной", "сколько озу", "использование памяти",
              "загружен процессор", "загружена память", "свободной памяти", "оперативной памяти")


def _load_commands() -> dict:
    """Карта команд из commands.yaml (теги → спецификации). При сбое — пустой dict.

    Источник истины и для матчера (синонимы/примеры/подтверждения), и для OS-агента.
    """
    try:
        with open(config.COMMANDS_FILE, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


class CoreModule(JarvisModule):
    """«Мозг»: преобразует текст пользователя в тег команды и реплику-подтверждение."""

    def __init__(self):
        super().__init__("jarvis-core")
        commands = _load_commands()
        # Матчер: правила (мгновенно) + эмбеддинги (лениво, при промахе правил).
        self._matcher = Matcher(commands)
        # Один разбор за раз: на N100 нет смысла молотить эмбеддинги в несколько потоков.
        self._lock = threading.Lock()

    def on_start(self):
        self.subscribe(contracts.TOPIC_INPUT, self.on_input)

    def on_input(self, payload: dict):
        text = (payload.get("text") or "").strip()
        if not text:
            return
        # Стоп-фразу перерыва («не сейчас», «я работаю» и т.п.) обрабатывает сервис
        # activity_monitor (он же ответит и сбросит цикл). Здесь молчим, чтобы не было
        # двойного ответа — иначе поверх ответа на стоп прозвучало бы «не разобрал».
        if is_stop_phrase(text):
            self.log.info("Стоп-фраза перерыва — обработает монитор активности: %s", text)
            return
        # Команда планировщика (будильник/таймер/секундомер/напоминание/задача) — обработает сервис
        # scheduler. ИЛИ идёт диалог дозапроса напоминания (scheduler ждёт ответ «о чём/когда») —
        # тогда молчим на ЛЮБОЙ ввод, чтобы ответ-продолжение ушёл планировщику, а не «не разобрал».
        # Мировое время/монетка — НЕ команды планировщика, идут ниже.
        if is_scheduler_command(text) or is_dialog_pending():
            self.log.info("Команда планировщика/диалог — обработает scheduler: %s", text)
            return
        # Громкость речи пользователя (от STT) пробрасываем в ответ — для адаптивной громкости TTS.
        level = payload.get("user_level")
        user_level = level if isinstance(level, (int, float)) else None
        # Обработку выносим в поток, чтобы не блокировать MQTT-loop.
        threading.Thread(target=self._process, args=(text, user_level), daemon=True).start()

    def _process(self, text: str, user_level: float | None = None):
        # Весь обработчик под защитой: непредвиденный сбой не должен убить рабочий поток
        # и не должен оставить шину висеть в состоянии thinking.
        with self._lock:
            try:
                self.set_state(contracts.STATE_THINKING)

                # 1) Встроенные info-ответы (офлайн, без матчера).
                info = self._local_info_answer(text)
                if info is not None:
                    self._say(info, user_level)
                    return

                # 2) Распознавание команды матчером.
                match = self._matcher.match(text)
                if match is not None:
                    speech = self._matcher.confirmation(match.tag)
                    self.log.info("Команда распознана: %s (%s, %.3f) — %s",
                                  match.tag, match.layer, match.score, text)
                    self._say(speech, user_level)
                    self._execute_command(match.tag)
                    return

                # 3) Не распознано — переспрос в характере (не падаем).
                self.log.info("Не распознано: %s", text)
                self._say(NOT_RECOGNIZED, user_level)
            except Exception:
                # Подстраховка: любой непредвиденный сбой — в лог по-человечески,
                # состояние сбрасываем в idle, поток продолжает жить.
                self.log_exc(logging.ERROR,
                             "Непредвиденный сбой обработки реплики — сбрасываю состояние")
                self.set_state(contracts.STATE_IDLE)

    def _say(self, text: str, user_level: float | None = None) -> None:
        """Реплика в jarvis/say с опц. громкостью речи пользователя (для адаптивной громкости TTS)."""
        payload = {"text": text, "source": self.name}
        if user_level is not None:
            payload["user_level"] = user_level
        self.publish_json(contracts.TOPIC_SAY, payload, qos=contracts.QOS_SAY)

    def _execute_command(self, tag: str) -> None:
        """Опубликовать команду на выполнение «Руками» (OS-агент) через jarvis/execute.

        Само исполнение — у OS-агента (allow-list, shell=False). Здесь только тег.
        """
        self.publish_json(
            contracts.TOPIC_EXECUTE,
            {"command_tag": tag},
            qos=contracts.QOS_EXECUTE,
        )

    def _local_info_answer(self, text: str) -> str | None:
        """Встроенные ответы: монетка, мировое время, локальное время, дата, громкость, загрузка, заряд."""
        low = text.lower()
        # Монетка («подкинь монетку», «орёл или решка»).
        coin = self._coin_answer(low)
        if coin is not None:
            return coin
        # Мировое время («сколько времени в <город>») — ПЕРЕД локальным (оно тоже ловит «сколько
        # времени», но без города это локальное время). detect_city → None, если города нет.
        city = worldtime.detect_city(text)
        if city:
            ans = worldtime.answer(city)
            if ans:
                return ans
        if any(k in low for k in _TIME_KEYS):
            now = datetime.now()
            return f"Сейчас {say_time(now.hour, now.minute)}, сэр."
        if any(k in low for k in _DATE_KEYS):
            return self._date_answer()
        if any(k in low for k in _VOLUME_KEYS):
            return self._volume_answer()
        if any(k in low for k in _LOAD_KEYS):
            return self._load_answer()
        if any(k in low for k in _BATTERY_KEYS):
            return self._battery_answer()
        return None

    def _coin_answer(self, low: str) -> str | None:
        """Монетка: случайно орёл/решка, фраза паком без повторов. Иначе None."""
        l = low.replace("ё", "е")
        if "монет" in l or ("орел" in l and "решк" in l):
            if random.random() < 0.5:
                return phrases.pick("coin.heads", config.COIN_HEADS)
            return phrases.pick("coin.tails", config.COIN_TAILS)
        return None

    def _date_answer(self) -> str:
        """Текущая дата словами (день/месяц/год прописью для естественной озвучки)."""
        now = datetime.now()
        return f"Сегодня {say_date(now.day, now.month, now.year)}, сэр."

    def _volume_answer(self) -> str:
        """Текущая громкость основного вывода (read-only проба wpctl)."""
        data = read_volume()
        if "ошибка" in data:
            self.log.warning("Не удалось снять громкость: %s", data["ошибка"])
            return "Не удалось узнать громкость, сэр."
        if data.get("выключен"):
            return "Звук сейчас выключен, сэр."
        return f"Громкость сейчас {say_percent(data.get('громкость_процент'))}, сэр."

    def _load_answer(self) -> str:
        """Загрузка процессора и памяти в характере (read-only из /proc)."""
        data = read_system_load()
        cpu = data.get("загрузка_cpu_процент")
        ram = data.get("память_занято_процент")
        parts = []
        if cpu is not None:
            parts.append(f"процессор на {say_percent(cpu)}")
        if ram is not None:
            parts.append(f"память на {say_percent(ram)}")
        if not parts:
            return "Не удалось снять загрузку системы, сэр."
        return "Загрузка: " + ", ".join(parts) + ", сэр."

    def _battery_answer(self) -> str:
        """Реальный заряд в характере Джарвиса (read-only-проба из sysinfo)."""
        data = read_battery()
        if data.get("батарея") == "не обнаружена":
            return "Батарея не обнаружена, сэр."
        if "ошибка" in data:
            self.log.warning("Не удалось снять показания батареи: %s", data["ошибка"])
            return "Не удалось снять показания батареи, сэр."
        capacity = data.get("процент")
        status = data.get("статус", "")
        if status == "заряжается":
            return f"Батарея на {say_percent(capacity)}, заряжается, сэр."
        if status == "заряжена полностью" or (isinstance(capacity, int) and capacity >= 99):
            return "Батарея заряжена полностью, сэр."
        return f"Батарея на {say_percent(capacity)}, сэр."


def main():
    CoreModule().run()


if __name__ == "__main__":
    main()
