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
import threading
from datetime import datetime

import yaml

from jarvis import config, contracts
from jarvis.bus import JarvisModule
from jarvis.matcher import NOT_RECOGNIZED, Matcher
from jarvis.sysinfo import read_battery, read_system_load, read_volume

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

# Родительный падеж месяцев для произнесения даты («5 июня»).
_MONTHS_RU = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря")


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
        # Обработку выносим в поток, чтобы не блокировать MQTT-loop.
        threading.Thread(target=self._process, args=(text,), daemon=True).start()

    def _process(self, text: str):
        # Весь обработчик под защитой: непредвиденный сбой не должен убить рабочий поток
        # и не должен оставить шину висеть в состоянии thinking.
        with self._lock:
            try:
                self.set_state(contracts.STATE_THINKING)

                # 1) Встроенные info-ответы (офлайн, без матчера).
                info = self._local_info_answer(text)
                if info is not None:
                    self.say(info)
                    return

                # 2) Распознавание команды матчером.
                match = self._matcher.match(text)
                if match is not None:
                    speech = self._matcher.confirmation(match.tag)
                    self.log.info("Команда распознана: %s (%s, %.3f) — %s",
                                  match.tag, match.layer, match.score, text)
                    self.say(speech)
                    self._execute_command(match.tag)
                    return

                # 3) Не распознано — переспрос в характере (не падаем).
                self.log.info("Не распознано: %s", text)
                self.say(NOT_RECOGNIZED)
            except Exception:
                # Подстраховка: любой непредвиденный сбой — в лог по-человечески,
                # состояние сбрасываем в idle, поток продолжает жить.
                self.log_exc(logging.ERROR,
                             "Непредвиденный сбой обработки реплики — сбрасываю состояние")
                self.set_state(contracts.STATE_IDLE)

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
        """Встроенные офлайн-ответы: время, дата, громкость, загрузка, заряд. Иначе None."""
        low = text.lower()
        if any(k in low for k in _TIME_KEYS):
            return f"Сейчас {datetime.now():%H:%M}, сэр."
        if any(k in low for k in _DATE_KEYS):
            return self._date_answer()
        if any(k in low for k in _VOLUME_KEYS):
            return self._volume_answer()
        if any(k in low for k in _LOAD_KEYS):
            return self._load_answer()
        if any(k in low for k in _BATTERY_KEYS):
            return self._battery_answer()
        return None

    def _date_answer(self) -> str:
        """Текущая дата в характере (число + месяц словом)."""
        now = datetime.now()
        return f"Сегодня {now.day} {_MONTHS_RU[now.month - 1]} {now.year} года, сэр."

    def _volume_answer(self) -> str:
        """Текущая громкость основного вывода (read-only проба wpctl)."""
        data = read_volume()
        if "ошибка" in data:
            self.log.warning("Не удалось снять громкость: %s", data["ошибка"])
            return "Не удалось узнать громкость, сэр."
        if data.get("выключен"):
            return "Звук сейчас выключен, сэр."
        return f"Громкость на {data.get('громкость_процент')} процентах, сэр."

    def _load_answer(self) -> str:
        """Загрузка процессора и памяти в характере (read-only из /proc)."""
        data = read_system_load()
        cpu = data.get("загрузка_cpu_процент")
        ram = data.get("память_занято_процент")
        parts = []
        if cpu is not None:
            parts.append(f"процессор на {cpu} процентах")
        if ram is not None:
            parts.append(f"память на {ram} процентах")
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
            return f"Батарея на {capacity} процентах, заряжается, сэр."
        if status == "заряжена полностью" or (isinstance(capacity, int) and capacity >= 99):
            return "Батарея заряжена полностью, сэр."
        return f"Батарея на {capacity} процентах, сэр."


def main():
    CoreModule().run()


if __name__ == "__main__":
    main()
