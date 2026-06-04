"""Глубокая диагностика «Джарвиса»: проверяем РАБОТОСПОСОБНОСТЬ, а не наличие.

Принцип: «файл существует» и «синтаксис валиден» запрещены как единственная
проверка — они зелёные, когда всё сломано. Каждая проверка убеждается, что
компонент реально работает на ЭТОМ железе сейчас, и при провале даёт человеческое
решение: что произошло → почему (по типу/коду ошибки) → точная команда/правка.

Режим по умолчанию — ПОЛНАЯ глубокая проверка (живые тесты Ollama/Gemini/Piper,
маршрут прокси, стабильность MQTT, здоровье юнитов, сквозная цепочка). Флаг
`--quick` пропускает долгие/сетевые/платные тесты (живой Gemini тратит квоту).
Флаг `--deep` оставлен как no-op алиас (дефолт и так полный).

Сам doctor не падает: каждая проверка обёрнута в try-except И прогоняется через
_safe в оркестрации — сбой одной проверки помечается ✗ с причиной, остальные идут.
"""
import importlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from jarvis import config, contracts
from jarvis.resilience import classify_mqtt_error, classify_ollama_error
from jarvis.services_map import SERVICES
from jarvis.ui import FAIL, OK, WARN, CheckResult, Reporter


# --------------------------------------------------------------------------- #
# Утилиты надёжности доктора
# --------------------------------------------------------------------------- #
# Классификаторы сбоёв (classify_ollama_error / classify_mqtt_error) переехали в
# jarvis.resilience — единый источник человеческих диагнозов и для сервисов, и для
# доктора (без дублирования). Импортируются выше.
def _safe(reporter: Reporter, func, *args) -> None:
    """Выполнить проверку, изолировав её падение: исключение ВНУТРИ проверки не
    должно ронять весь доктор. Проверка возвращает CheckResult или list[CheckResult];
    на исключении — честный FAIL с типом ошибки, остальные проверки продолжаются.
    """
    try:
        result = func(*args)
    except Exception as exc:
        import traceback

        name = getattr(func, "__name__", "проверка")
        reporter.report(CheckResult(
            FAIL, f"{name}: внутренний сбой проверки",
            reason=f"{type(exc).__name__}: {exc}",
            fix="это баг самой проверки доктора; трейс — в logs/doctor.log",
        ))
        reporter.log.error("Сбой проверки %s:\n%s", name, traceback.format_exc())
        return
    if isinstance(result, list):
        for r in result:
            reporter.report(r)
    elif result is not None:
        reporter.report(result)


# --------------------------------------------------------------------------- #
# Слой 1. Окружение и пакет
# --------------------------------------------------------------------------- #
def check_venv() -> CheckResult:
    """venv активен и это venv именно проекта (не системный Python)."""
    prefix = Path(sys.prefix).resolve()
    expected = (config.BASE_DIR / ".venv").resolve()
    in_venv = sys.prefix != sys.base_prefix
    if not in_venv:
        return CheckResult(
            FAIL, "venv проекта активен",
            reason="запущен системный Python, а не виртуальное окружение проекта.",
            fix="source .venv/bin/activate  (или установите через ./bootstrap.sh)",
        )
    if prefix != expected:
        return CheckResult(
            WARN, "venv проекта активен",
            reason=f"активен venv {prefix}, ожидался {expected}.",
            fix="запускайте jarvis из .venv проекта: .venv/bin/jarvis doctor",
        )
    return CheckResult(OK, f"venv проекта активен ({prefix})")


def check_imports() -> list[CheckResult]:
    """Все зависимости реально импортируются (import, а не «пакет установлен»)."""
    modules = [
        "paho.mqtt.client", "pydantic", "yaml", "numpy",
        "sounddevice", "ollama", "sherpa_onnx", "piper", "dotenv",
    ]
    results = []
    for mod in modules:
        try:
            importlib.import_module(mod)
            results.append(CheckResult(OK, f"import {mod}"))
        except Exception as exc:
            results.append(CheckResult(
                FAIL, f"import {mod}",
                reason=f"модуль не импортируется: {exc}",
                fix="pip install -e .  (а sherpa-onnx/piper-tts — сверьте версии для вашей платформы)",
            ))
    return results


def check_library_versions() -> list[CheckResult]:
    """Версии ключевых библиотек удовлетворяют спецификаторам pyproject (источник истины).

    Расхождение (установленная версия вне диапазона из pyproject) → WARN: API мог
    измениться, а код «сверен по памяти». Не блокер, но частая причина неуловимых сбоев.
    """
    try:
        import tomllib
        from importlib.metadata import PackageNotFoundError, version

        from packaging.requirements import Requirement
    except Exception as exc:
        return [CheckResult(WARN, "Версии библиотек",
                            reason=f"нет инструментов сверки версий: {exc}.",
                            fix="pip install -e .  (нужен packaging)")]
    try:
        with open(config.BASE_DIR / "pyproject.toml", "rb") as f:
            deps = tomllib.load(f).get("project", {}).get("dependencies", [])
    except Exception as exc:
        return [CheckResult(WARN, "Версии библиотек",
                            reason=f"не удалось прочитать pyproject.toml: {exc}.",
                            fix="проверьте pyproject.toml")]
    # Либы, чьи версии реально влияют на поведение (имя пакета как в pyproject).
    watched = {"google-genai", "paho-mqtt", "sherpa-onnx", "piper-tts",
               "pydantic", "ollama", "numpy", "httpx"}
    results = []
    for raw in deps:
        try:
            req = Requirement(raw)
        except Exception:
            continue
        if req.name.lower() not in watched:
            continue
        try:
            inst = version(req.name)
        except PackageNotFoundError:
            results.append(CheckResult(FAIL, f"версия {req.name}",
                                       reason="пакет не установлен.", fix="pip install -e ."))
            continue
        if req.specifier and inst not in req.specifier:
            results.append(CheckResult(
                WARN, f"версия {req.name}",
                reason=f"установлена {inst}, pyproject ожидает {req.specifier} "
                       "(API мог измениться).",
                fix=f"pip install -e .  (или сверьте код под {req.name} {inst})",
            ))
        else:
            results.append(CheckResult(OK, f"версия {req.name}: {inst}"))
    return results


def check_env_file() -> list[CheckResult]:
    """Разбор .env: дубли переменных и подозрительные значения ключей — с номерами строк.

    Подозрительное значение ключа (пробел/перенос внутри, имя GEMINI_API_KEY/KEY= внутри
    значения) приводило к Illegal header value. Дубль переменной незаметно «перетирает»
    значение. Значения секретов НЕ печатаем — только имя и номер строки.
    """
    env_path = config.BASE_DIR / ".env"
    if not env_path.exists():
        return [CheckResult(OK, ".env: файл отсутствует (работаем на окружении/дефолтах)")]
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        return [CheckResult(WARN, ".env читается",
                            reason=f"не удалось прочитать .env: {exc}.", fix="проверьте .env")]
    results = []
    seen: dict[str, int] = {}
    secret_markers = ("KEY", "TOKEN", "SECRET", "PASS")
    for i, line in enumerate(lines, start=1):
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        name, _, value = s.partition("=")
        name, value = name.strip(), value.strip()
        if name in seen:
            results.append(CheckResult(
                WARN, f".env: дубль переменной {name}",
                reason=f"{name} задан повторно (строки {seen[name]} и {i}); возьмётся последний.",
                fix=f"оставьте одну строку {name} в .env",
            ))
        else:
            seen[name] = i
        if any(m in name.upper() for m in secret_markers) and value:
            if any(ch.isspace() for ch in value) or "GEMINI_API_KEY" in value or "KEY=" in value:
                results.append(CheckResult(
                    WARN, f".env: подозрительное значение {name}",
                    reason=f"строка {i}: пробел/перенос или имя переменной внутри значения "
                           "(признак слипшихся строк; был Illegal header value).",
                    fix=f"проверьте строку {i} в .env: значение без пробелов/переносов, один ключ",
                ))
    if not results:
        return [CheckResult(OK, f".env: {len(seen)} переменных, дублей и битых значений нет")]
    return results


def check_config_paths() -> list[CheckResult]:
    """Все пути из config.py резолвятся в существующие места."""
    paths = {
        "models/": config.MODELS_DIR,
        "logs/": config.LOGS_DIR,
        "commands.yaml": Path(config.COMMANDS_FILE),
        "VAD-модель": Path(config.VAD_MODEL),
        "zipformer encoder": Path(config.ZIPFORMER_ENCODER),
        "zipformer decoder": Path(config.ZIPFORMER_DECODER),
        "zipformer joiner": Path(config.ZIPFORMER_JOINER),
        "zipformer tokens": Path(config.ZIPFORMER_TOKENS),
        "zipformer bpe": Path(config.ZIPFORMER_BPE),
        "Piper-голос": Path(config.PIPER_MODEL),
        "Piper-config": Path(config.PIPER_CONFIG),
    }
    results = []
    for label, path in paths.items():
        if path.exists():
            results.append(CheckResult(OK, f"путь: {label}"))
        else:
            is_model = config.MODELS_DIR in path.parents
            results.append(CheckResult(
                FAIL, f"путь: {label}",
                reason=f"не найдено: {path}",
                fix="jarvis models --download" if is_model else f"создайте {path}",
            ))
    return results


# Безопасные read-only version-пробы: бинарь в $PATH ≠ рабочая утилита. Для этих
# утилит лёгкий `--version` подтверждает работоспособность, ничего не меняя в системе.
# GUI и разрушительные (spectacle/konsole/firefox/dolphin/telegram/nmap, переключатели
# Wi-Fi/BT) НЕ запускаем — только наличие бинаря.
_SAFE_VERSION_PROBE = {
    "wpctl": ["--help"],          # wpctl не знает --version; --help безвреден и даёт код 0
    "brightnessctl": ["--version"],
    "nmcli": ["--version"],
    "bluetoothctl": ["--version"],
    "loginctl": ["--version"],
}


def check_commands_yaml() -> list[CheckResult]:
    """commands.yaml: структура (команда — список), бинарь в $PATH, для read-only
    утилит — живая version-проба (бинарь есть ≠ команда реально работает)."""
    import yaml

    try:
        with open(config.COMMANDS_FILE, encoding="utf-8") as f:
            commands = yaml.safe_load(f) or {}
    except Exception as exc:
        return [CheckResult(
            FAIL, "commands.yaml читается",
            reason=f"не удалось разобрать YAML: {exc}",
            fix=f"проверьте синтаксис {config.COMMANDS_FILE}",
        )]
    if not commands:
        return [CheckResult(WARN, "commands.yaml", reason="карта команд пуста.",
                            fix="добавьте хотя бы одну команду в commands.yaml")]
    results = []
    for tag, spec in commands.items():
        args = (spec or {}).get("команда")
        if not isinstance(args, list) or not args:
            results.append(CheckResult(
                FAIL, f"команда «{tag}»",
                reason="поле «команда» должно быть непустым списком аргументов.",
                fix=f"исправьте блок {tag} в commands.yaml (см. шаблон в начале файла)",
            ))
            continue
        binary = str(args[0])
        if not shutil.which(binary):
            results.append(CheckResult(
                WARN, f"команда «{tag}» → {binary}",
                reason=f"бинарь {binary!r} не найден в $PATH.",
                fix=f"установите {binary} или поправьте путь в commands.yaml",
            ))
            continue
        probe = _SAFE_VERSION_PROBE.get(binary)
        if not probe:
            # GUI/разрушительные — не запускаем, ограничиваемся наличием бинаря.
            results.append(CheckResult(OK, f"команда «{tag}» → {binary}"))
            continue
        try:
            proc = subprocess.run([binary, *probe], capture_output=True, text=True,
                                  timeout=3, check=False)
            if proc.returncode == 0:
                results.append(CheckResult(OK, f"команда «{tag}» → {binary} (отвечает)"))
            else:
                results.append(CheckResult(
                    WARN, f"команда «{tag}» → {binary}",
                    reason=f"{binary} есть, но вернул код {proc.returncode} на "
                           f"{' '.join(probe)}.",
                    fix=f"проверьте вручную: {binary} {' '.join(probe)}",
                ))
        except subprocess.TimeoutExpired:
            results.append(CheckResult(
                WARN, f"команда «{tag}» → {binary}",
                reason=f"{binary} не ответил на {' '.join(probe)} за 3с.",
                fix=f"проверьте работоспособность {binary} вручную",
            ))
        except Exception as exc:
            results.append(CheckResult(
                WARN, f"команда «{tag}» → {binary}",
                reason=f"не удалось проверить {binary}: {exc}.",
                fix=f"проверьте {binary} вручную",
            ))
    return results


def check_entry_points() -> list[CheckResult]:
    """pyproject.toml валиден, entry points резолвятся в реальные функции."""
    try:
        import tomllib
        with open(config.BASE_DIR / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        scripts = data.get("project", {}).get("scripts", {})
    except Exception as exc:
        return [CheckResult(FAIL, "pyproject.toml валиден",
                            reason=f"не удалось разобрать: {exc}",
                            fix="проверьте синтаксис pyproject.toml")]
    if not scripts:
        return [CheckResult(WARN, "entry points", reason="секция [project.scripts] пуста.",
                            fix="добавьте console-скрипты в pyproject.toml")]
    results = []
    for name, target in scripts.items():
        try:
            module_path, func_name = target.split(":")
            module = importlib.import_module(module_path)
            func = getattr(module, func_name)
            if not callable(func):
                raise TypeError("цель не вызываемая")
            results.append(CheckResult(OK, f"entry point: {name}"))
        except Exception as exc:
            results.append(CheckResult(
                FAIL, f"entry point: {name}",
                reason=f"{target} не резолвится: {exc}",
                fix="переустановите пакет: pip install -e .",
            ))
    return results


# --------------------------------------------------------------------------- #
# Слой «Железо» (N100, 8 ГБ — впритык: ловим нехватку RAM/swap/диска)
# --------------------------------------------------------------------------- #
def check_hardware() -> list[CheckResult]:
    """Реальные ресурсы железа с числами: память+swap, загрузка CPU, место на диске.

    На N100/8 ГБ Джарвис с моделями и облаком идёт впритык: нехватка памяти/свопинг и
    забитый диск реально тормозят работу. Это WARN с цифрами, не блокеры.
    """
    results = []
    # --- Память и swap из /proc/meminfo ---
    try:
        mem: dict = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                key, _, value = line.partition(":")
                mem[key.strip()] = int(value.split()[0])  # значения в kB
        total_gb = mem.get("MemTotal", 0) / 1024 / 1024
        avail_gb = mem.get("MemAvailable", 0) / 1024 / 1024
        swap_total = mem.get("SwapTotal", 0)
        swap_used = swap_total - mem.get("SwapFree", 0)
        detail = f"доступно {avail_gb:.1f} ГБ из {total_gb:.1f} ГБ"
        swap_pct = round(swap_used / swap_total * 100) if swap_total else 0
        if swap_total:
            detail += f", swap занят {swap_used/1024/1024:.1f} ГБ ({swap_pct}%)"
        if avail_gb < 0.5:
            results.append(CheckResult(
                WARN, "Память",
                reason=f"мало свободной RAM: {detail}.",
                fix="закройте лишнее; модели и Gemini требуют памяти — возможны своп и тормоза",
            ))
        elif swap_total and swap_pct >= 50:
            results.append(CheckResult(
                WARN, "Память",
                reason=f"активный свопинг: {detail}.",
                fix="память под нагрузкой; закройте лишнее, иначе возможны подтормаживания",
            ))
        else:
            results.append(CheckResult(OK, f"Память: {detail}"))
    except Exception as exc:
        results.append(CheckResult(WARN, "Память",
                                   reason=f"не удалось прочитать /proc/meminfo: {exc}."))
    # --- Загрузка CPU (переиспользуем read-only функцию мозга) ---
    try:
        from jarvis.brain import read_system_load

        load = read_system_load()
        pct, l1, cores = (load.get("загрузка_cpu_процент"),
                          load.get("загрузка_cpu_1мин"), load.get("ядер"))
        if pct is None:
            results.append(CheckResult(WARN, "Загрузка CPU", reason=f"нет данных: {load}."))
        elif pct >= 90:
            results.append(CheckResult(
                WARN, "Загрузка CPU",
                reason=f"высокая загрузка: {pct}% (loadavg {l1} на {cores} ядра).",
                fix="что-то нагружает процессор — проверьте top/htop",
            ))
        else:
            results.append(CheckResult(
                OK, f"Загрузка CPU: {pct}% (loadavg {l1} на {cores} ядра)"))
    except Exception as exc:
        results.append(CheckResult(WARN, "Загрузка CPU",
                                   reason=f"не удалось снять загрузку: {exc}."))
    # --- Место на диске (корень проекта) ---
    try:
        usage = shutil.disk_usage(config.BASE_DIR)
        free_gb = usage.free / 1024**3
        total_gb = usage.total / 1024**3
        used_pct = round(usage.used / usage.total * 100)
        detail = f"свободно {free_gb:.0f} ГБ из {total_gb:.0f} ГБ ({used_pct}% занято)"
        if used_pct >= 90 or free_gb < 5:
            results.append(CheckResult(
                WARN, "Диск",
                reason=f"мало места: {detail}.",
                fix="освободите место — модели большие, логи растут",
            ))
        else:
            results.append(CheckResult(OK, f"Диск: {detail}"))
    except Exception as exc:
        results.append(CheckResult(WARN, "Диск",
                                   reason=f"не удалось определить место: {exc}."))
    return results


# --------------------------------------------------------------------------- #
# Слой 2. Системные службы (живые проверки, доли секунды)
# --------------------------------------------------------------------------- #
def check_mosquitto() -> CheckResult:
    """Реальный коннект к брокеру + round-trip: publish своё сообщение и приём назад."""
    # ВНИМАНИЕ: сверить поведение paho-mqtt 2.x на целевой машине.
    try:
        import paho.mqtt.client as mqtt
    except Exception as exc:
        return CheckResult(FAIL, "Mosquitto round-trip",
                           reason=f"нет paho-mqtt: {exc}", fix="pip install -e .")
    received: list[bytes] = []
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="jarvis-doctor")
    client.on_message = lambda cl, u, msg: received.append(msg.payload)
    topic = "jarvis/_doctor/ping"
    try:
        client.connect(config.MQTT_HOST, config.MQTT_PORT, 5)
    except Exception as exc:
        return CheckResult(
            FAIL, "Mosquitto round-trip",
            reason=f"брокер {config.MQTT_HOST}:{config.MQTT_PORT}: {classify_mqtt_error(exc)}.",
            fix="sudo systemctl enable --now mosquitto   (или: mosquitto -d)",
        )
    try:
        client.loop_start()
        client.subscribe(topic)
        time.sleep(0.2)
        client.publish(topic, b"ping", qos=0)
        deadline = time.time() + 2.0
        while time.time() < deadline and not received:
            time.sleep(0.05)
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass
    if received:
        return CheckResult(OK, f"Mosquitto round-trip ({config.MQTT_HOST}:{config.MQTT_PORT})")
    return CheckResult(
        FAIL, "Mosquitto round-trip",
        reason="подключение есть, но опубликованное сообщение не вернулось.",
        fix="проверьте конфиг брокера (acl/allow_anonymous) и логи mosquitto",
    )


def check_ollama() -> CheckResult:
    """HTTP к Ollama + наличие модели в списке (без долгой генерации)."""
    # ВНИМАНИЕ: сверить API клиента ollama (структуру list()) с установленной версией.
    try:
        import ollama
        client = ollama.Client(host=config.OLLAMA_HOST)
        data = client.list()
    except Exception as exc:
        return CheckResult(
            FAIL, "Ollama отвечает",
            reason=f"сервер {config.OLLAMA_HOST}: {classify_ollama_error(exc)}.",
            fix="ollama serve   (и проверьте JARVIS_OLLAMA_HOST)",
        )
    names = _ollama_model_names(data)
    wanted = config.OLLAMA_MODEL
    # Точное совпадение или совпадение по базовому имени до тега (model:tag)
    if wanted in names or any(n.split(":")[0] == wanted.split(":")[0] for n in names):
        return CheckResult(OK, f"Ollama: модель {wanted} доступна")
    return CheckResult(
        FAIL, f"Ollama: модель {wanted}",
        reason="сервер отвечает, но модель не скачана на этой машине.",
        fix=f"ollama pull {wanted}",
    )


def _ollama_model_names(data) -> list[str]:
    """Извлечь имена моделей из разнообразных форматов ответа list()."""
    models = []
    raw = getattr(data, "models", None)
    if raw is None and isinstance(data, dict):
        raw = data.get("models", [])
    for item in raw or []:
        name = getattr(item, "model", None) or getattr(item, "name", None)
        if name is None and isinstance(item, dict):
            name = item.get("model") or item.get("name")
        if name:
            models.append(name)
    return models


def check_gemini() -> CheckResult:
    """Casual-бэкенд: при gemini — есть ли пакет и ключ (без сетевого запроса).

    Gemini опционален: при отсутствии пакета/ключа беседа уходит в офлайн-фоллбэк,
    поэтому это WARN (не блокер) — doctor остаётся зелёным, но честно предупреждает.
    """
    if config.CASUAL_BACKEND != "gemini":
        return CheckResult(OK, f"Casual-бэкенд: локальный режим ({config.CASUAL_BACKEND})")
    try:
        import google.genai  # noqa: F401
    except Exception as exc:
        return CheckResult(
            WARN, "Gemini casual-бэкенд",
            reason=f"пакет google-genai не импортируется ({exc}); беседа уйдёт в офлайн-фоллбэк.",
            fix="pip install -e .  (или JARVIS_CASUAL_BACKEND=local)",
        )
    if not config.GEMINI_API_KEYS:
        return CheckResult(
            WARN, "Gemini casual-бэкенд",
            reason="ключи Gemini не заданы — беседа будет уходить в офлайн-фоллбэк.",
            fix="впишите GEMINI_API_KEY в .env (см. .env.example) или JARVIS_CASUAL_BACKEND=local",
        )
    grounding = "вкл" if config.GEMINI_GROUNDING else "выкл"
    proxy = ", через прокси" if config.GEMINI_PROXY else ""
    return CheckResult(
        OK,
        f"Gemini casual-бэкенд: модель {config.GEMINI_MODEL}, grounding {grounding}{proxy}, "
        f"ключей: {len(config.GEMINI_API_KEYS)}",
    )


def check_brain_tools() -> CheckResult:
    """Инструменты Gemini-мозга собираются: команды commands.yaml + read-only (без сети).

    Только сборка function declarations и валидность имён — живого вызова нет. При
    бэкенде не-gemini инструменты не используются, но сборка всё равно валидна.
    Опционально (google-genai/ключи) → WARN, не FAIL.
    """
    if config.CASUAL_BACKEND != "gemini":
        return CheckResult(OK, f"Инструменты мозга: пропущено (бэкенд {config.CASUAL_BACKEND})")
    try:
        import yaml

        with open(config.COMMANDS_FILE, encoding="utf-8") as f:
            commands = yaml.safe_load(f) or {}
    except Exception as exc:
        return CheckResult(
            WARN, "Инструменты мозга (function calling)",
            reason=f"не удалось прочитать {config.COMMANDS_FILE}: {exc}.",
            fix="проверьте синтаксис commands.yaml",
        )
    try:
        import re

        from jarvis import brain

        tools = brain.build_function_tools(commands)
        decls = (tools[0].function_declarations if tools else []) or []
        names = [d.name for d in decls]
        if not names:
            return CheckResult(
                WARN, "Инструменты мозга (function calling)",
                reason="не собрано ни одной функции (пустая карта команд?).",
                fix="добавьте команды в commands.yaml",
            )
        bad = [n for n in names if not n or not re.fullmatch(r"[A-Za-z0-9_]+", n)]
        if bad:
            return CheckResult(
                WARN, "Инструменты мозга (function calling)",
                reason=f"имена функций невалидны для Gemini: {bad}.",
                fix="теги commands.yaml — латиница/цифры/подчёркивание (как имена функций)",
            )
        return CheckResult(
            OK,
            f"Инструменты мозга: {len(commands)} команд + {len(brain.READONLY_TOOLS)} "
            f"состояния (function calling)",
        )
    except Exception as exc:
        return CheckResult(
            WARN, "Инструменты мозга (function calling)",
            reason=f"сборка инструментов упала: {exc}.",
            fix="pip install -e .  (нужен google-genai) или JARVIS_CASUAL_BACKEND=local",
        )


def check_audio() -> list[CheckResult]:
    """PipeWire живой: есть устройство вывода И устройство ввода (микрофон)."""
    try:
        import sounddevice as sd
        devices = sd.query_devices()
    except Exception as exc:
        return [CheckResult(
            FAIL, "Аудиоустройства",
            reason=f"не удалось опросить аудио ({exc}).",
            fix="проверьте, что сессия PipeWire запущена: systemctl --user status pipewire",
        )]
    has_in = any(d.get("max_input_channels", 0) > 0 for d in devices)
    has_out = any(d.get("max_output_channels", 0) > 0 for d in devices)
    results = []
    results.append(CheckResult(OK, "Аудио: устройство ввода (микрофон)") if has_in else
                   CheckResult(FAIL, "Аудио: устройство ввода (микрофон)",
                               reason="не найдено ни одного устройства захвата.",
                               fix="подключите микрофон; проверьте wpctl status"))
    results.append(CheckResult(OK, "Аудио: устройство вывода") if has_out else
                   CheckResult(FAIL, "Аудио: устройство вывода",
                               reason="не найдено ни одного устройства воспроизведения.",
                               fix="проверьте вывод звука: wpctl status"))
    return results


def check_proxy_route() -> CheckResult:
    """Реальный маршрут прокси Gemini: внешний IP и страна ЧЕРЕЗ тот же прокси-клиент.

    «Прокси задан» ≠ «трафик идёт через него». Делаем запрос к сервису определения IP с
    тем же proxy, что у Gemini-клиента, и показываем внешний IP/страну. Российский IP или
    молчащий прокси → WARN (Gemini упрётся в геоблок). Без прокси — инфо «напрямую».
    Системный/пентест-трафик это не затрагивает — разовый запрос только этого клиента.
    """
    if not config.GEMINI_PROXY:
        return CheckResult(OK, "Прокси Gemini не задан (облако — напрямую)")
    try:
        import httpx
    except Exception as exc:
        return CheckResult(WARN, "Маршрут прокси Gemini",
                           reason=f"httpx недоступен: {exc}.", fix="pip install -e .")
    try:
        with httpx.Client(proxy=config.GEMINI_PROXY, timeout=8) as client:
            data = client.get("https://ipinfo.io/json").json()
        ip = data.get("ip") or "?"
        country = (data.get("country") or "").upper()
    except Exception as exc:
        from jarvis.casual import classify_gemini_error

        return CheckResult(
            WARN, "Маршрут прокси Gemini",
            reason=f"прокси не отвечает: {classify_gemini_error(exc)}.",
            fix="проверьте JARVIS_GEMINI_PROXY (адрес/логин/пароль) и доступность прокси",
        )
    if not country or country == "RU":
        return CheckResult(
            WARN, "Маршрут прокси Gemini",
            reason=f"внешний IP {ip}, страна {country or '?'} — не зарубежная; "
                   "Gemini упрётся в геоблок.",
            fix="нужен прокси с зарубежным IP (см. CLAUDE.md, раздел «Прокси»)",
        )
    return CheckResult(OK, f"Маршрут прокси Gemini: внешний IP {ip}, страна {country}")


def check_mqtt_stability(quick: bool = False) -> CheckResult:
    """Стабильность MQTT: частые НЕОЖИДАННЫЕ разрывы видны в логах и/или живым наблюдением.

    Считаем по логам только неожиданные разрывы — строку «Связь с шиной потеряна» (штатные
    отключения при shutdown в счёт не идут). Типичная причина на этой машине — перезапуск
    брокера и спящий режим (диагностировано по journalctl), а не сторона Джарвиса; реконнект
    автоматический. В полном режиме дополнительно держим подписчика ~5 c и ловим разрывы.
    """
    import glob as _glob

    total_disc, worst = 0, ("", 0)
    try:
        for path in _glob.glob(str(config.LOGS_DIR / "jarvis-*.log")):
            try:
                n = Path(path).read_text(encoding="utf-8", errors="replace").count(
                    "Связь с шиной потеряна")
            except Exception:
                continue
            total_disc += n
            if n > worst[1]:
                worst = (Path(path).name, n)
    except Exception:
        pass

    live_disc = 0
    if not quick:
        try:
            import paho.mqtt.client as mqtt

            state = {"n": 0, "closing": False}
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                 client_id="jarvis-doctor-stab")
            client.on_disconnect = lambda *a, **k: (
                None if state["closing"] else state.__setitem__("n", state["n"] + 1))
            client.connect(config.MQTT_HOST, config.MQTT_PORT, 5)
            client.loop_start()
            time.sleep(5.0)
            state["closing"] = True  # наш штатный disconnect разрывом не считаем
            client.loop_stop()
            client.disconnect()
            live_disc = state["n"]
        except Exception as exc:
            return CheckResult(WARN, "MQTT-стабильность",
                               reason=f"не удалось проверить вживую: {classify_mqtt_error(exc)}.",
                               fix="убедитесь, что брокер запущен")

    if live_disc > 0:
        return CheckResult(
            WARN, "MQTT-стабильность",
            reason=f"за 5 c соединение рвалось {live_disc} раз; неожиданных разрывов в логах "
                   f"суммарно {total_disc} (худший: {worst[0]} — {worst[1]}).",
            fix="проверьте, не перезапускается ли брокер (systemctl status mosquitto) и "
                "стабильность сети; client_id уже уникален (name-PID), реконнект автоматический",
        )
    if total_disc >= 10:
        return CheckResult(
            WARN, "MQTT-стабильность",
            reason=f"в логах много неожиданных разрывов: суммарно {total_disc} "
                   f"(худший: {worst[0]} — {worst[1]}). Чаще всего это перезапуски брокера "
                   "или спящий режим — Джарвис восстанавливается сам.",
            fix="journalctl -u mosquitto (рестарты брокера?); разрывы при suspend — это норма",
        )
    detail = "вживую разрывов нет" if not quick else "по логам"
    return CheckResult(OK, f"MQTT-стабильность: {detail}, неожиданных разрывов в логах {total_disc}")


# --------------------------------------------------------------------------- #
# Слой 3. Модели (загружаемость движком — инициализация, не инференс)
# --------------------------------------------------------------------------- #
def check_sherpa_models() -> list[CheckResult]:
    """VAD и zipformer-ru реально загружаются движком, а не «файл на месте»."""
    results = []
    try:
        import sherpa_onnx
    except Exception as exc:
        return [CheckResult(FAIL, "sherpa-onnx загружается",
                            reason=f"пакет недоступен: {exc}", fix="pip install -e .")]
    try:
        vad_config = sherpa_onnx.VadModelConfig()
        vad_config.silero_vad.model = config.VAD_MODEL
        vad_config.sample_rate = config.SAMPLE_RATE
        sherpa_onnx.VoiceActivityDetector(vad_config, buffer_size_in_seconds=10)
        results.append(CheckResult(OK, "Модель VAD загружается движком"))
    except Exception as exc:
        results.append(CheckResult(
            FAIL, "Модель VAD загружается движком",
            reason=f"движок не смог загрузить VAD: {exc}",
            fix="jarvis models --download  (или сверьте путь JARVIS_VAD_MODEL)",
        ))
    try:
        # zipformer-ru — offline transducer с BPE-словарём.
        sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=config.ZIPFORMER_ENCODER,
            decoder=config.ZIPFORMER_DECODER,
            joiner=config.ZIPFORMER_JOINER,
            tokens=config.ZIPFORMER_TOKENS,
            modeling_unit="bpe",
            bpe_vocab=config.ZIPFORMER_BPE,
        )
        results.append(CheckResult(OK, "Модель zipformer-ru загружается движком"))
    except Exception as exc:
        results.append(CheckResult(
            FAIL, "Модель zipformer-ru загружается движком",
            reason=f"движок не смог загрузить zipformer-ru: {exc}",
            fix="jarvis models --download  (или сверьте JARVIS_ZIPFORMER_*)",
        ))
    return results


def check_piper_voice() -> CheckResult:
    """Голос Piper загружается движком (синтез сэмпла — в --deep)."""
    # ВНИМАНИЕ: API piper-tts менялся между версиями — сверить на целевой машине.
    try:
        from piper import PiperVoice
        PiperVoice.load(config.PIPER_MODEL, config_path=config.PIPER_CONFIG)
        return CheckResult(OK, "Голос Piper загружается движком")
    except Exception as exc:
        return CheckResult(
            FAIL, "Голос Piper загружается движком",
            reason=f"движок не смог загрузить голос: {exc}",
            fix="jarvis models --download  (или сверьте JARVIS_PIPER_MODEL/_CONFIG)",
        )


def check_sample_rate() -> CheckResult:
    """Согласованность частот: STT 16 кГц vs частота голоса Piper («бурундук»)."""
    import json

    try:
        with open(config.PIPER_CONFIG, encoding="utf-8") as f:
            voice_sr = int(json.load(f).get("audio", {}).get("sample_rate", 0))
    except Exception as exc:
        return CheckResult(
            WARN, "Согласованность частот дискретизации",
            reason=f"не удалось прочитать sample_rate из {config.PIPER_CONFIG}: {exc}.",
            fix="скачайте корректный config голоса: jarvis models --download",
        )
    stt_sr = config.SAMPLE_RATE
    # TTS воспроизводит на частоте голоса (см. tts.py), поэтому разные частоты —
    # норма; важно лишь, что воспроизведение не прибито к 16 кГц.
    if voice_sr <= 0:
        return CheckResult(WARN, "Согласованность частот дискретизации",
                           reason="в config голоса нет audio.sample_rate.",
                           fix="скачайте корректный config голоса")
    return CheckResult(
        OK, f"Частоты: STT {stt_sr} Гц, голос Piper {voice_sr} Гц "
            f"(воспроизведение — на частоте голоса)"
    )


# --------------------------------------------------------------------------- #
# Слой 4. Сервисы Джарвиса
# --------------------------------------------------------------------------- #
def check_services() -> list[CheckResult]:
    """Каждый сервис импортируется и инстанцируется без падения, цепляется к шине."""
    from jarvis.bus import JarvisModule

    results = []
    for svc in SERVICES:
        try:
            module = importlib.import_module(svc.module)
            cls = getattr(module, svc.cls)
            instance = cls()
            if not isinstance(instance, JarvisModule):
                raise TypeError(f"{svc.cls} не наследует JarvisModule")
            if not callable(getattr(module, "main", None)):
                raise TypeError("нет функции main()")
            results.append(CheckResult(OK, f"сервис {svc.key} инстанцируется"))
        except Exception as exc:
            results.append(CheckResult(
                FAIL, f"сервис {svc.key} инстанцируется",
                reason=f"{svc.module}.{svc.cls}: {exc}",
                fix="см. трейс в logs/ соответствующего модуля; проверьте импорты",
            ))
    return results


def check_subscription_restore() -> CheckResult:
    """Подписки восстанавливаются после реконнекта (subscribe в on_connect, не разово)."""
    from jarvis.bus import JarvisModule

    class _FakeClient:
        def __init__(self):
            self.subscribed: list[str] = []

        def subscribe(self, topic):
            self.subscribed.append(topic)

    try:
        module = JarvisModule("jarvis-doctor-probe")
        module._handlers = {contracts.TOPIC_INPUT: lambda p: None,
                            contracts.TOPIC_STATE: lambda p: None}
        fake = _FakeClient()
        # Имитируем успешный реконнект
        module._on_connect(fake, None, None, 0, None)
        restored = set(fake.subscribed)
        expected = set(module._handlers)
        if restored == expected:
            return CheckResult(OK, "Подписки восстанавливаются после реконнекта")
        return CheckResult(
            FAIL, "Подписки восстанавливаются после реконнекта",
            reason=f"при reconnect восстановлены {restored}, ожидались {expected}.",
            fix="в bus.py _on_connect должен переподписываться по всему self._handlers",
        )
    except Exception as exc:
        return CheckResult(FAIL, "Подписки восстанавливаются после реконнекта",
                           reason=f"сбой проверки: {exc}", fix="см. jarvis/bus.py")


def _systemctl_show(unit: str) -> dict:
    """Свойства юнита через `systemctl --user show` → dict. Пустой dict при сбое."""
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show", unit,
             "-p", "ActiveState", "-p", "SubState", "-p", "NRestarts"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        props = {}
        for line in proc.stdout.splitlines():
            key, _, value = line.partition("=")
            props[key.strip()] = value.strip()
        return props
    except Exception:
        return {}


def _log_freshness(svc) -> str:
    """Справочная пометка о свежести лога сервиса (не влияет на статус: молчащий
    сервис может законно долго не писать)."""
    try:
        log_path = config.LOGS_DIR / f"{svc.command}.log"
        if not log_path.exists():
            return ", лог не найден"
        age = time.time() - log_path.stat().st_mtime
        if age < 120:
            return f", лог свежий ({int(age)} c назад)"
        if age < 3600:
            return f", лог {int(age / 60)} мин назад"
        return f", лог {int(age / 3600)} ч назад"
    except Exception:
        return ""


def check_service_health() -> list[CheckResult]:
    """Здоровье юнитов: установлен, ExecStart-бинарь есть и (если запущен) реально жив.

    Не «класс инстанцируется», а живой статус systemd: failed/флапающий процесс виден по
    ActiveState/SubState/NRestarts. Юнит не установлен → WARN (jarvis start). systemctl
    недоступен (не systemd-сессия) → ограничиваемся проверкой файла юнита и бинаря.
    """
    import os as _os

    base = _os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    units_dir = Path(base) / "systemd" / "user"
    have_systemctl = shutil.which("systemctl") is not None
    results = []
    for svc in SERVICES:
        unit_path = units_dir / svc.unit
        if not unit_path.exists():
            results.append(CheckResult(
                WARN, f"юнит {svc.unit}",
                reason="юнит ещё не установлен в ~/.config/systemd/user/.",
                fix="jarvis start  (сгенерирует юниты с верными путями)",
            ))
            continue
        # ExecStart-бинарь существует.
        try:
            exec_start = ""
            for line in unit_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("ExecStart="):
                    exec_start = line.split("=", 1)[1].strip()
                    break
            binary = exec_start.split()[0] if exec_start else ""
        except Exception as exc:
            results.append(CheckResult(FAIL, f"юнит {svc.unit}",
                                       reason=f"не удалось прочитать юнит: {exc}",
                                       fix="jarvis start"))
            continue
        if not (binary and Path(binary).exists()):
            results.append(CheckResult(
                FAIL, f"юнит {svc.unit}",
                reason=f"ExecStart указывает на несуществующий бинарь: {binary!r}.",
                fix="jarvis start  (перегенерирует юниты от текущего venv)",
            ))
            continue
        if not have_systemctl:
            results.append(CheckResult(
                OK, f"юнит {svc.unit} → {binary} (systemctl недоступен, статус не снят)"))
            continue
        # Живое состояние.
        props = _systemctl_show(svc.unit)
        active = props.get("ActiveState", "")
        sub = props.get("SubState", "")
        nrestarts = props.get("NRestarts", "0")
        if active == "active":
            fresh = _log_freshness(svc)
            if nrestarts.isdigit() and int(nrestarts) >= 5:
                results.append(CheckResult(
                    WARN, f"юнит {svc.unit}",
                    reason=f"active/{sub}, но рестартовал {nrestarts} раз — вероятно флапает{fresh}.",
                    fix=f"журнал: journalctl --user -u {svc.unit} -n 50",
                ))
            else:
                results.append(CheckResult(
                    OK, f"юнит {svc.unit}: active/{sub}, рестартов {nrestarts}{fresh}"))
        elif active == "failed" or sub == "failed":
            results.append(CheckResult(
                FAIL, f"юнит {svc.unit}",
                reason=f"юнит в состоянии failed ({active}/{sub}).",
                fix=f"journalctl --user -u {svc.unit} -n 50; затем jarvis start",
            ))
        elif active in ("inactive", "deactivating", ""):
            results.append(CheckResult(
                WARN, f"юнит {svc.unit}",
                reason=f"не запущен ({active or '?'}/{sub or '?'}).",
                fix=f"jarvis start  (или systemctl --user start {svc.unit})",
            ))
        else:
            results.append(CheckResult(
                WARN, f"юнит {svc.unit}",
                reason=f"состояние {active}/{sub} (не active).",
                fix=f"jarvis status; journalctl --user -u {svc.unit} -n 50",
            ))
    return results


def check_heartbeat() -> list[CheckResult]:
    """Heartbeat: каждый запущенный сервис периодически пишет в лог «Сервис жив».

    Проверяем свежесть последней такой отметки — чтобы поймать «активен, но висит молча».
    Никогда не FAIL (это не блокер): только OK/WARN. Для НЕзапущенных сервисов проверку
    пропускаем (их статус уже сообщает check_service_health — не дублируем предупреждение).
    """
    interval = config.HEARTBEAT_INTERVAL
    if interval <= 0:
        return [CheckResult(OK, "Heartbeat выключен (JARVIS_HEARTBEAT_INTERVAL=0)")]

    import re as _re
    from datetime import datetime as _dt

    stale_after = interval * 2 + 60  # допуск: два интервала + минута на запись
    ts_re = _re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    results: list[CheckResult] = []
    for svc in SERVICES:
        try:
            # Если знаем, что сервис не запущен — heartbeat не ждём (это норма).
            active = _systemctl_show(svc.unit).get("ActiveState", "")
            if active and active != "active":
                results.append(CheckResult(OK, f"heartbeat {svc.key}: пропущено (не запущен)"))
                continue
            log_path = config.LOGS_DIR / f"{svc.command}.log"
            if not log_path.exists():
                results.append(CheckResult(
                    WARN, f"heartbeat {svc.key}",
                    reason="лог сервиса не найден — сервис ещё ни разу не запускался?",
                    fix="jarvis start",
                ))
                continue
            last_ts = None
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if "Сервис жив" in line:
                    m = ts_re.match(line)
                    if m:
                        last_ts = m.group(1)
            if last_ts is None:
                # Нет ни одной отметки. Если лог свежий — сервис просто недавно
                # стартовал и не дошёл до первого тика heartbeat (норма, не тревога).
                mtime_age = time.time() - log_path.stat().st_mtime
                if mtime_age < stale_after:
                    results.append(CheckResult(
                        OK, f"heartbeat {svc.key}: недавно запущен, ждём первую отметку"))
                else:
                    results.append(CheckResult(
                        WARN, f"heartbeat {svc.key}",
                        reason=f"нет отметок «жив» (heartbeat раз в {int(interval)}с), а лог "
                               f"не обновлялся {int(mtime_age)}с — heartbeat не пишется?",
                        fix="обновите код (pip install -e .) и перезапустите: jarvis start",
                    ))
                continue
            age = (_dt.now() - _dt.strptime(last_ts, "%Y-%m-%d %H:%M:%S")).total_seconds()
            if age > stale_after:
                results.append(CheckResult(
                    WARN, f"heartbeat {svc.key}",
                    reason=f"последняя отметка «жив» была {int(age)}с назад (ждём каждые "
                           f"{int(interval)}с) — сервис мог зависнуть.",
                    fix=f"journalctl --user -u {svc.unit} -n 50; при необходимости jarvis start",
                ))
            else:
                results.append(CheckResult(OK, f"heartbeat {svc.key}: жив {int(age)}с назад"))
        except Exception as exc:
            results.append(CheckResult(
                WARN, f"heartbeat {svc.key}",
                reason=f"не удалось проверить heartbeat: {exc}.",
                fix=f"смотрите logs/{svc.command}.log",
            ))
    return results


# --------------------------------------------------------------------------- #
# Слой 2/3 (--deep): реально долгие живые тесты
# --------------------------------------------------------------------------- #
def check_ollama_generation() -> CheckResult:
    """Пробная генерация Ollama → валидация ответа через JarvisResponse."""
    try:
        import ollama
        client = ollama.Client(host=config.OLLAMA_HOST)
    except Exception as exc:
        return CheckResult(FAIL, "Ollama: генерация + схема",
                           reason=f"клиент недоступен: {classify_ollama_error(exc)}",
                           fix="ollama serve")
    try:
        response = client.chat(
            model=config.OLLAMA_MODEL,
            messages=[{"role": "user", "content": "джарвис, скажи что-нибудь короткое"}],
            format=contracts.JarvisResponse.model_json_schema(),
            keep_alive=config.OLLAMA_KEEP_ALIVE,
        )
        content = response["message"]["content"]
    except Exception as exc:
        return CheckResult(
            FAIL, "Ollama: генерация + схема",
            reason=f"модель {config.OLLAMA_MODEL}: {classify_ollama_error(exc)}.",
            fix=f"ollama run {config.OLLAMA_MODEL}  — проверьте, что модель рабочая",
        )
    try:
        contracts.JarvisResponse.model_validate_json(content)
        return CheckResult(OK, "Ollama: генерация даёт валидный JarvisResponse")
    except Exception as exc:
        return CheckResult(
            FAIL, "Ollama: генерация + схема",
            reason=f"модель отвечает, но JSON не проходит схему: {exc}.",
            fix="проверьте системный промпт/формат; при необходимости поднимите модель до 1.5b",
        )


def check_piper_synthesis() -> CheckResult:
    """Голос Piper реально синтезирует тестовый сэмпл (не пустой звук).

    piper-tts 1.4.x: synthesize(text) возвращает итератор AudioChunk.
    У каждого чанка — audio_int16_bytes (bytes), sample_rate, sample_channels.
    """
    try:
        from piper import PiperVoice
        voice = PiperVoice.load(config.PIPER_MODEL, config_path=config.PIPER_CONFIG)
        pcm = bytearray()
        for chunk in voice.synthesize("Проверка связи, сэр."):
            pcm += chunk.audio_int16_bytes
        if len(pcm) > 0:
            return CheckResult(OK, f"Piper синтезирует звук ({len(pcm)} байт PCM)")
        return CheckResult(FAIL, "Piper синтезирует звук",
                           reason="синтез вернул пустой буфер.",
                           fix="сверьте корректность голоса и конфига Piper")
    except Exception as exc:
        return CheckResult(FAIL, "Piper синтезирует звук",
                           reason=f"сбой синтеза: {exc}",
                           fix="jarvis models --download; сверьте API piper-tts")


def check_gemini_live() -> CheckResult:
    """Живой запрос к Gemini: реальный ответ в пределах таймаута (только --deep).

    Гоняем через настоящий CasualBackend — заодно проверяем ключ, сеть, имя модели
    и grounding-конфиг. Если вернулся офлайн-фоллбэк, значит облако не ответило.
    """
    if config.CASUAL_BACKEND != "gemini":
        return CheckResult(OK, f"Gemini: живой тест пропущен (бэкенд {config.CASUAL_BACKEND})")
    if not config.GEMINI_API_KEYS:
        return CheckResult(
            WARN, "Gemini: живой ответ",
            reason="ключи Gemini не заданы — живой тест пропущен.",
            fix="впишите GEMINI_API_KEY в .env (см. .env.example)",
        )
    try:
        import logging

        from jarvis import casual

        backend = casual.CasualBackend(logging.getLogger("jarvis-doctor-gemini"))
        answer = backend.reply("Ответь одним коротким словом для проверки связи.")
    except Exception as exc:
        # Gemini опционален (есть офлайн-фоллбэк), поэтому недоступность — WARN, не блокер.
        return CheckResult(
            WARN, "Gemini: живой ответ",
            reason=f"запрос упал: {exc}",
            fix="проверьте GEMINI_API_KEY, сеть и имя модели JARVIS_GEMINI_MODEL",
        )
    if answer in (casual._FALLBACK_FIRST, casual._FALLBACK_AGAIN):
        # backend прогнан тем же путём, что casual (включая прокси), поэтому диагноз
        # из last_error точный: гео-блок/квота/ключ/сеть, а не общая «облако недоступно».
        diag = backend.last_error or "облако недоступно, неверный ключ или имя модели"
        return CheckResult(
            WARN, "Gemini: живой ответ",
            reason=f"вернулся офлайн-фоллбэк: {diag}.",
            fix="проверьте GEMINI_API_KEY, JARVIS_GEMINI_PROXY, сеть и "
                "JARVIS_GEMINI_MODEL; см. logs/jarvis-core.log",
        )
    proxy = " (через прокси)" if config.GEMINI_PROXY else ""
    # На каком по счёту ключе прошёл запрос — видно, сработала ли ротация.
    total = len(config.GEMINI_API_KEYS)
    on_key = f", на ключе #{backend._key_index + 1} из {total}" if total > 1 else ""
    return CheckResult(OK, f"Gemini: живой ответ получен{on_key}{proxy}")


def check_brain_live() -> CheckResult:
    """Живой прогон function calling (--deep): Gemini вызывает read-only функцию.

    Вопрос про загрузку системы минует локальный перехват времени/заряда и должен
    спровоцировать вызов get_system_load. Успех — был хотя бы один вызов инструмента и
    ответ не офлайн-фоллбэк. Облако опционально → WARN при недоступности, не FAIL.
    Команды управления в тесте не исполняются: execute-колбэк лишь копит теги.
    """
    if config.CASUAL_BACKEND != "gemini":
        return CheckResult(OK, f"Мозг: тест function calling пропущен (бэкенд {config.CASUAL_BACKEND})")
    if not config.GEMINI_API_KEYS:
        return CheckResult(
            WARN, "Мозг: function calling",
            reason="ключи Gemini не заданы — живой тест пропущен.",
            fix="впишите GEMINI_API_KEY в .env (см. .env.example)",
        )
    try:
        import logging

        import yaml

        from jarvis import brain, casual

        with open(config.COMMANDS_FILE, encoding="utf-8") as f:
            commands = yaml.safe_load(f) or {}
        executed: list[str] = []
        backend = brain.Brain(
            logging.getLogger("jarvis-doctor-brain"),
            lambda tag: executed.append(tag),  # в тесте команды не исполняем
            commands,
        )
        answer = backend.think("Какая сейчас загрузка системы?")
    except Exception as exc:
        return CheckResult(
            WARN, "Мозг: function calling",
            reason=f"прогон упал: {exc}.",
            fix="проверьте google-genai, GEMINI_API_KEY и сеть",
        )
    if answer in (casual._FALLBACK_FIRST, casual._FALLBACK_AGAIN):
        diag = backend.last_error or "облако недоступно"
        return CheckResult(
            WARN, "Мозг: function calling",
            reason=f"вернулся офлайн-фоллбэк: {diag}.",
            fix="проверьте GEMINI_API_KEY, JARVIS_GEMINI_PROXY, сеть; logs/jarvis-core.log",
        )
    if not backend.last_tool_calls:
        return CheckResult(
            WARN, "Мозг: function calling",
            reason="ответ получен, но Gemini не вызвал ни одной функции.",
            fix="сверьте описания инструментов; возможно, модель ответила без вызова",
        )
    proxy = " (через прокси)" if config.GEMINI_PROXY else ""
    return CheckResult(
        OK, f"Мозг: function calling работает — вызвано {backend.last_tool_calls}{proxy}"
    )


def live_chain_test() -> bool:
    """Сквозной тест живой шины (для `jarvis test` и Слоя 5 --deep).

    Слушает все топики jarvis/#, публикует say/execute/input и сообщает,
    какой модуль ожил, а какой молчит. Также проверяет публикацию состояний.
    """
    reporter = Reporter()
    reporter.section("Сквозной тест живой шины (say → execute → input)")
    try:
        import paho.mqtt.client as mqtt
    except Exception as exc:
        reporter.report(CheckResult(FAIL, "MQTT-клиент", reason=str(exc), fix="pip install -e ."))
        return reporter.summary()

    seen: dict[str, list] = {}
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="jarvis-doctor-chain")

    def on_message(cl, u, msg):
        seen.setdefault(msg.topic, []).append(msg.payload.decode("utf-8", "replace"))

    client.on_message = on_message
    try:
        client.connect(config.MQTT_HOST, config.MQTT_PORT, 5)
    except Exception as exc:
        reporter.report(CheckResult(
            FAIL, "Подключение к брокеру", reason=str(exc),
            fix="sudo systemctl enable --now mosquitto"))
        return reporter.summary()

    client.loop_start()
    client.subscribe("jarvis/#")
    time.sleep(0.3)
    try:
        # 1) say → должен ответить «Голос» (state=speaking)
        reporter.note("публикую jarvis/say и жду реакции «Голоса»…")
        client.publish(contracts.TOPIC_SAY,
                       '{"text":"Проверка связи, сэр","source":"doctor"}')
        time.sleep(3.0)
        # 2) execute → должны ответить «Руки»
        reporter.note("публикую jarvis/execute (неизвестный тег) и жду реакции «Рук»…")
        client.publish(contracts.TOPIC_EXECUTE, '{"command_tag":"__doctor_probe__"}',
                       qos=contracts.QOS_EXECUTE)
        time.sleep(2.0)
        # 3) input → должен ответить «Мозг» (state=thinking + say)
        reporter.note("публикую jarvis/input и жду реакции «Мозга» (может быть долго)…")
        client.publish(contracts.TOPIC_INPUT, '{"text":"джарвис как дела"}')
        time.sleep(8.0)
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass

    states = " ".join(seen.get(contracts.TOPIC_STATE, []))
    says = seen.get(contracts.TOPIC_SAY, [])
    # «Руки» откликаются репликой про неизвестный тег
    hands_alive = any("__doctor_probe__" in s or "неизвестна" in s for s in says)
    brain_alive = "thinking" in states or len(says) >= 2
    voice_alive = "speaking" in states

    reporter.report(CheckResult(OK, "«Голос» (tts) откликнулся (state=speaking)") if voice_alive
                    else CheckResult(WARN, "«Голос» (tts) молчит",
                                     reason="не было state=speaking после jarvis/say.",
                                     fix="jarvis status; журналы: logs/jarvis-tts.log"))
    reporter.report(CheckResult(OK, "«Руки» (os_agent) откликнулись") if hands_alive
                    else CheckResult(WARN, "«Руки» (os_agent) молчат",
                                     reason="нет реакции на jarvis/execute.",
                                     fix="jarvis status; журналы: logs/jarvis-os-agent.log"))
    reporter.report(CheckResult(OK, "«Мозг» (core) откликнулся (state=thinking)") if brain_alive
                    else CheckResult(WARN, "«Мозг» (core) молчит",
                                     reason="нет state=thinking/ответа на jarvis/input.",
                                     fix="jarvis status; журналы: logs/jarvis-core.log"))
    return reporter.summary()


# --------------------------------------------------------------------------- #
# Оркестрация
# --------------------------------------------------------------------------- #
def run(quick: bool = False) -> bool:
    """Полная глубокая диагностика (дефолт). quick=True пропускает долгие/сетевые/платные
    тесты (живой Gemini тратит квоту, синтез/цепочка — секунды). Каждая проверка идёт
    через _safe: её падение не роняет весь доктор.
    """
    reporter = Reporter()

    reporter.section("Слой 1. Окружение и пакет")
    _safe(reporter, check_venv)
    _safe(reporter, check_imports)
    _safe(reporter, check_library_versions)
    _safe(reporter, check_config_paths)
    _safe(reporter, check_env_file)
    _safe(reporter, check_commands_yaml)
    _safe(reporter, check_entry_points)

    reporter.section("Слой 2. Железо (N100, 8 ГБ)")
    _safe(reporter, check_hardware)

    reporter.section("Слой 3. Системные службы")
    _safe(reporter, check_mosquitto)
    if not quick:
        reporter.note("проверяю стабильность MQTT (≈5 c)…")
    _safe(reporter, check_mqtt_stability, quick)
    _safe(reporter, check_ollama)
    _safe(reporter, check_gemini)
    _safe(reporter, check_brain_tools)
    if not quick:
        reporter.note("проверяю маршрут прокси Gemini (внешний IP)…")
        _safe(reporter, check_proxy_route)
    _safe(reporter, check_audio)

    reporter.section("Слой 4. Модели")
    reporter.note("загружаю модели движком…")
    _safe(reporter, check_sherpa_models)
    _safe(reporter, check_piper_voice)
    _safe(reporter, check_sample_rate)
    if not quick:
        reporter.note("пробная генерация Ollama…")
        _safe(reporter, check_ollama_generation)
        reporter.note("синтез тестового сэмпла Piper…")
        _safe(reporter, check_piper_synthesis)

    reporter.section("Слой 5. Сервисы Джарвиса")
    _safe(reporter, check_services)
    _safe(reporter, check_subscription_restore)
    _safe(reporter, check_service_health)
    _safe(reporter, check_heartbeat)

    if not quick:
        reporter.section("Слой 6. Облако и функции (живые)")
        reporter.note("живой запрос к Gemini (ротация ключей/прокси)…")
        _safe(reporter, check_gemini_live)
        reporter.note("живой function-calling прогон мозга…")
        _safe(reporter, check_brain_live)

    ok = reporter.summary()

    if not quick:
        # Сквозную цепочку печатаем отдельной секцией со своим итогом.
        live_chain_test()
    return ok
