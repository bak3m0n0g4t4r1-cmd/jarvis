"""«Мозг»-агент Джарвиса: Gemini с function calling поверх CasualBackend.

Если casual.py просто отвечает текстом, то brain даёт облачной модели РУКИ и ГЛАЗА
на состояние системы: Gemini сам решает, какое локальное действие выполнить, вызывает
функцию, Джарвис её исполняет и возвращает результат, Gemini продолжает рассуждение и
даёт финальный ответ в характере. Так классификатор интентов превращается в агента.

Что объявляется инструментами:
  - команды управления — ВСЕ теги commands.yaml (источник истины): громкость, яркость,
    Wi-Fi, Bluetooth, запуск приложений, скриншот, блокировка, диагностика. Имя функции =
    тег, описание из карты. Без параметров — пользовательский текст в команду НЕ
    подставляется (безопасность: см. CLAUDE.md);
  - чтение состояния (read-only) — загрузка CPU/RAM, заряд батареи, дата/время, сеть.

Режим function calling — РУЧНОЙ (automatic_function_calling.disable=True): мы сами
выполняем вызовы (команды — публикацией в jarvis/execute через колбэк, состояние —
локально), а не отдаём это SDK. Это даёт контроль allow-list и не открывает shell.

Ротация ключей, прокси, persona, память беседы и офлайн-фоллбэк — наследуются у
CasualBackend как есть, без дублирования.

Сверено на google-genai 2.7.0: FunctionDeclaration(name, description, parameters),
ответ несёт свойство response.function_calls (список FunctionCall(name, args)),
ответ инструмента — Part.from_function_response(name, response=dict), несколько Tool
в одном запросе (функции + google_search) допустимы структурно.
"""
import logging
import os
import subprocess
from datetime import datetime

from jarvis import config
from jarvis.casual import CasualBackend


# --------------------------------------------------------------------------- #
# Read-only функции состояния системы (чистый Python, без shell-инъекций).
# Каждая возвращает компактный dict на русском — Gemini формулирует ответ по нему.
# Всё в try-except: инструмент не должен ронять цикл, при беде вернёт {"ошибка": ...}.
# --------------------------------------------------------------------------- #
_WEEKDAYS_RU = [
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
]


def read_datetime() -> dict:
    """Текущие дата, время и день недели."""
    try:
        now = datetime.now()
        return {
            "время": now.strftime("%H:%M"),
            "дата": now.strftime("%d.%m.%Y"),
            "день_недели": _WEEKDAYS_RU[now.weekday()],
        }
    except Exception as exc:
        return {"ошибка": f"не удалось получить время: {exc}"}


def read_system_load() -> dict:
    """Загрузка CPU (среднее за 1 мин) и использование памяти — из /proc, без psutil."""
    result: dict = {}
    try:
        # loadavg за 1 минуту — без мгновенного замера и без сна (дёшево на N100).
        with open("/proc/loadavg", encoding="utf-8") as f:
            load1 = float(f.read().split()[0])
        cores = os.cpu_count() or 1
        result["загрузка_cpu_1мин"] = round(load1, 2)
        result["ядер"] = cores
        result["загрузка_cpu_процент"] = round(load1 / cores * 100)
    except Exception as exc:
        result["cpu_ошибка"] = str(exc)
    try:
        mem: dict = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                key, _, value = line.partition(":")
                mem[key.strip()] = value.strip()
        total_kb = int(mem.get("MemTotal", "0 kB").split()[0])
        avail_kb = int(mem.get("MemAvailable", "0 kB").split()[0])
        if total_kb:
            used_kb = total_kb - avail_kb
            result["память_всего_гб"] = round(total_kb / 1024 / 1024, 1)
            result["память_занято_гб"] = round(used_kb / 1024 / 1024, 1)
            result["память_занято_процент"] = round(used_kb / total_kb * 100)
    except Exception as exc:
        result["память_ошибка"] = str(exc)
    return result or {"ошибка": "нет данных о нагрузке"}


def read_battery() -> dict:
    """Заряд и статус батареи из /sys/class/power_supply/BAT* (или её отсутствие)."""
    import glob

    try:
        paths = sorted(glob.glob("/sys/class/power_supply/BAT*"))
        if not paths:
            return {"батарея": "не обнаружена"}
        bat = paths[0]
        with open(f"{bat}/capacity", encoding="utf-8") as f:
            capacity = int(f.read().strip())
        status_raw = ""
        try:
            with open(f"{bat}/status", encoding="utf-8") as f:
                status_raw = f.read().strip()
        except Exception:
            pass
        status_ru = {
            "Charging": "заряжается",
            "Discharging": "разряжается",
            "Full": "заряжена полностью",
            "Not charging": "не заряжается",
        }.get(status_raw, status_raw or "неизвестно")
        return {"процент": capacity, "статус": status_ru}
    except Exception as exc:
        return {"ошибка": f"не удалось снять показания батареи: {exc}"}


def read_network() -> dict:
    """Активные сетевые подключения и Wi-Fi через nmcli (read-only, с таймаутом)."""
    try:
        proc = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        conns = []
        for line in proc.stdout.splitlines():
            # nmcli -t разделяет поля двоеточием; имя соединения может содержать его —
            # берём первое поле как имя, последнее как устройство, тип между ними.
            parts = line.split(":")
            if len(parts) >= 3 and parts[0]:
                conns.append({"имя": parts[0], "тип": parts[-2], "устройство": parts[-1]})
        if not conns:
            return {"сеть": "нет активных подключений"}
        return {"активные_подключения": conns}
    except FileNotFoundError:
        return {"ошибка": "nmcli не найден"}
    except subprocess.TimeoutExpired:
        return {"ошибка": "nmcli не ответил вовремя"}
    except Exception as exc:
        return {"ошибка": f"не удалось определить сеть: {exc}"}


# Имя инструмента → (описание для Gemini, функция). По описанию модель решает, когда звать.
_READONLY_DECLS = [
    ("get_datetime", "Текущие дата, время и день недели.", read_datetime),
    ("get_system_load",
     "Загрузка процессора и использование оперативной памяти. Зови, когда спрашивают "
     "про нагрузку или жалуются, что система тормозит/виснет.", read_system_load),
    ("get_battery", "Уровень заряда батареи и статус (заряжается/разряжается).",
     read_battery),
    ("get_network", "Активные сетевые подключения и текущий Wi-Fi.", read_network),
]
READONLY_TOOLS = {name: func for name, _, func in _READONLY_DECLS}


def build_function_tools(commands: dict) -> list:
    """Собрать инструменты Gemini: команды управления (commands.yaml) + read-only.

    Источник истины — commands.yaml: каждый тег становится функцией без параметров
    (имя=тег, описание из карты). Добавление команды в yaml автоматически даёт новый
    инструмент. Read-only функции состояния добавляются следом. Возвращает список из
    одного types.Tool с общим списком function_declarations.
    """
    from google.genai import types

    declarations = []
    for tag, spec in (commands or {}).items():
        description = ((spec or {}).get("описание") or str(tag))
        declarations.append(
            types.FunctionDeclaration(name=str(tag), description=str(description))
        )
    for name, description, _ in _READONLY_DECLS:
        declarations.append(
            types.FunctionDeclaration(name=name, description=description)
        )
    return [types.Tool(function_declarations=declarations)]


class Brain(CasualBackend):
    """Мозг-агент: Gemini с function calling. Наследует ротацию ключей/прокси/persona/
    память/офлайн-фоллбэк у CasualBackend. Никогда не бросает наружу — при любой беде
    срабатывает наследованный фоллбэк («временно поглупел, сэр»).
    """

    def __init__(self, log: logging.Logger, execute_cb, commands: dict):
        super().__init__(log)
        # Колбэк выполнения команды управления: публикует тег в jarvis/execute (даёт core).
        self._execute_cb = execute_cb
        # Какие имена считать командой управления (а какие — read-only / неизвестным).
        self._command_tags = set((commands or {}).keys())
        # Инструменты Gemini собираем один раз. Если пакета нет — пусто, уйдём в фоллбэк.
        try:
            self._function_tools = build_function_tools(commands)
        except Exception:
            self.log.exception("Не удалось собрать инструменты Gemini — мозг без функций")
            self._function_tools = []
        # Деградация grounding (квота веб-поиска 429 / таймаут / конфликт с функциями 400)
        # унаследована из CasualBackend: общий self._grounding + self._generate_content сами
        # один раз отступают к функциям без grounding и гасят его на сессию — без дублей здесь.
        # Наблюдаемость для doctor: какие инструменты вызывались в последнем запросе.
        self.last_tool_calls: list[str] = []

    # ------------------------------------------------------------------ #
    # Публичный вход
    # ------------------------------------------------------------------ #
    def think(self, text: str) -> str:
        """Подумать над репликой (возможно, с вызовом локальных функций) и ответить.

        Тонкая обёртка над наследованным reply(): тот же lock, память беседы, ротация
        ключей и офлайн-фоллбэк. Цикл инструментов живёт в переопределённом _ask_gemini.
        """
        return self.reply(text)

    # ------------------------------------------------------------------ #
    # Цикл function calling (переопределяет одиночный запрос CasualBackend)
    # ------------------------------------------------------------------ #
    def _ask_gemini(self, client, text: str) -> str:
        """Цикл: Gemini вызывает функции → мы выполняем → возвращаем результат → ответ.

        До config.BRAIN_MAX_STEPS итераций (составная задача = несколько вызовов подряд,
        но не бесконечно). Ротация ключей по 429 оборачивает этот метод снаружи
        (в наследованном _reply_with_rotation): на исчерпании квоты весь цикл повторится
        на следующем ключе.
        """
        from google.genai import types

        self.last_tool_calls = []
        contents = self._history_contents(text)

        for _ in range(max(1, config.BRAIN_MAX_STEPS)):
            # generate с инструментами; grounding и его самоисцеление — в базовом
            # _generate_content. Ручной function calling: SDK сам вызовы НЕ исполняет.
            response = self._generate_content(
                client, contents,
                tools=self._function_tools,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            )
            calls = list(getattr(response, "function_calls", None) or [])
            if not calls:
                # Финальный текстовый ответ в характере.
                answer = (getattr(response, "text", None) or "").strip()
                if not answer:
                    raise RuntimeError("Gemini вернул пустой ответ")
                return answer

            # 1) Кладём ответ модели (parts с function_call) обратно в диалог.
            try:
                contents.append(response.candidates[0].content)
            except Exception:
                contents.append(types.Content(
                    role="model",
                    parts=[types.Part.from_function_call(name=c.name, args=c.args or {})
                           for c in calls],
                ))
            # 2) Выполняем каждый вызов и собираем function_response.
            resp_parts = []
            for call in calls:
                name = call.name or ""
                self.last_tool_calls.append(name)
                result = self._execute_tool(name, dict(call.args or {}))
                resp_parts.append(
                    types.Part.from_function_response(name=name, response=result)
                )
            # Ответы инструментов отправляются ролью user (паттерн google-genai).
            contents.append(types.Content(role="user", parts=resp_parts))

        raise RuntimeError("превышен лимит шагов function calling")

    def _execute_tool(self, name: str, args: dict) -> dict:
        """Выполнить вызов инструмента и вернуть результат для function_response.

        Read-only функция → считать локально. Тег команды → опубликовать в jarvis/execute
        через колбэк (само исполнение — у OS-агента, не здесь, без shell). Неизвестное имя
        → ошибка (Gemini увидит и переиграет). Всё в try-except — цикл не должен падать.
        """
        try:
            if name in READONLY_TOOLS:
                return READONLY_TOOLS[name]()
            if name in self._command_tags:
                self._execute_cb(name)
                return {"status": "ok",
                        "действие": f"команда «{name}» отправлена на выполнение"}
            self.log.warning("Gemini вызвал неизвестный инструмент: %r", name)
            return {"status": "error", "причина": f"неизвестный инструмент «{name}»"}
        except Exception as exc:
            self.log.exception("Сбой выполнения инструмента %s", name)
            return {"status": "error", "причина": str(exc)}
