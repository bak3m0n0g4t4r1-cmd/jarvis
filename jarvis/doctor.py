"""Глубокая диагностика «Джарвиса»: проверяем РАБОТОСПОСОБНОСТЬ, а не наличие.

Принцип: «файл существует» и «синтаксис валиден» запрещены как единственная
проверка — они зелёные, когда всё сломано. Каждая проверка убеждается, что
компонент реально работает на ЭТОМ железе сейчас, и при провале даёт человеческое
решение: что произошло → почему (по типу/коду ошибки) → точная команда/правка.

Режим по умолчанию — ПОЛНАЯ глубокая проверка (загрузка моделей движком, эмбеддер
команд, синтез Piper, стабильность MQTT, здоровье юнитов, сквозная цепочка). Флаг
`--quick` пропускает долгие тесты (синтез Piper, сквозная цепочка).
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
from jarvis.resilience import classify_mqtt_error
from jarvis.services_map import SERVICES
from jarvis.ui import FAIL, OK, WARN, CheckResult, Reporter


# --------------------------------------------------------------------------- #
# Утилиты надёжности доктора
# --------------------------------------------------------------------------- #
# Классификатор сбоя MQTT (classify_mqtt_error) живёт в jarvis.resilience — единый
# источник человеческих диагнозов и для сервисов, и для доктора. Импортируется выше.
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
        "paho.mqtt.client", "yaml", "numpy", "sounddevice",
        "sherpa_onnx", "piper", "onnxruntime", "tokenizers",
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
    watched = {"onnxruntime", "tokenizers", "paho-mqtt", "sherpa-onnx",
               "piper-tts", "numpy"}
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
    """Разбор .env: дубли переменных и подозрительные значения — с номерами строк.

    Секретов в .env больше нет (облако удалено), но базовая гигиена полезна: дубль
    переменной незаметно «перетирает» значение, а пробел/перенос внутри значения с
    маркером KEY/TOKEN/SECRET/PASS — признак слипшихся строк. Значения НЕ печатаем —
    только имя и номер строки.
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
            if any(ch.isspace() for ch in value) or "KEY=" in value:
                results.append(CheckResult(
                    WARN, f".env: подозрительное значение {name}",
                    reason=f"строка {i}: пробел/перенос или имя переменной внутри значения "
                           "(признак слипшихся строк).",
                    fix=f"проверьте строку {i} в .env: значение без пробелов/переносов, одно на строку",
                ))
    if not results:
        return [CheckResult(OK, f".env: {len(seen)} переменных, дублей и битых значений нет")]
    return results


def check_settings() -> CheckResult:
    """Единый файл настроек settings.yaml — валидный YAML. Битый файл config молча игнорирует
    (берёт дефолты), поэтому проверяем явно: иначе правки пользователя «не применяются» без причины."""
    import yaml

    path = Path(config.SETTINGS_FILE)
    if not path.exists():
        return CheckResult(
            WARN, "settings.yaml",
            reason=f"файла нет ({path}) — все параметры на дефолтах.",
            fix="создайте settings.yaml в корне проекта (или это намеренно — работаем на дефолтах)",
        )
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return CheckResult(
                FAIL, "settings.yaml",
                reason="файл не словарь (пустой/битый) — правки игнорируются, идут дефолты.",
                fix="проверьте структуру: секции voice/adaptive_audio/hearing/recognition/system/models",
            )
        return CheckResult(OK, f"settings.yaml валиден ({len(data)} секций)")
    except Exception as exc:
        return CheckResult(
            FAIL, "settings.yaml",
            reason=f"невалидный YAML: {exc} — параметры МОЛЧА берутся из дефолтов!",
            fix=f"исправьте синтаксис {path}",
        )


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
        "эмбеддер (модель)": Path(config.EMBEDDER_MODEL),
        "эмбеддер (токенизатор)": Path(config.EMBEDDER_TOKENIZER),
    }
    results = []
    for label, path in paths.items():
        if not path.is_absolute():
            # Относительный путь под systemd (CWD=$HOME) не найдётся. Ловим ЯВНО: иначе из
            # корня проекта exists() ложно зеленеет — так и пропустили регрессию Этапа 7.
            results.append(CheckResult(
                FAIL, f"путь: {label}",
                reason=f"путь относительный ({path}) — под systemd не найдётся.",
                fix="config.py обязан резолвить пути от BASE_DIR (_get_path); проверьте settings.yaml",
            ))
        elif path.exists():
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
    "playerctl": ["--version"],
    "systemctl": ["--version"],
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
    from jarvis.matcher import _RESERVED_KEYS

    results = []
    for tag, spec in commands.items():
        # Служебные top-level ключи (ветки/обратимость, ТЗ-5) — НЕ команды, пропускаем.
        if tag in _RESERVED_KEYS:
            continue
        # Команды лампы (ТЗ-8) / телефона (ТЗ-9) / темы (ТЗ-10) — НЕ shell: спец-поле вместо «команда».
        if (isinstance((spec or {}).get("лампа"), dict) or isinstance((spec or {}).get("телефон"), dict)
                or (spec or {}).get("тема")):
            continue
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
                fix="закройте лишнее; модели STT/TTS и эмбеддер требуют памяти — возможны своп и тормоза",
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
    # --- Загрузка CPU (переиспользуем read-only пробу из sysinfo) ---
    try:
        from jarvis.sysinfo import read_system_load

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


class _DisabledEmbedder:
    """Заглушка эмбеддера для проверки СЛОЯ ПРАВИЛ без загрузки ONNX-модели."""

    def encode(self, texts):
        return None


def check_embedder() -> CheckResult:
    """Эмбеддер команд (rubert-tiny2 ONNX) реально грузится и даёт осмысленные векторы.

    Проверяем не «файл на месте», а работоспособность: модель грузится onnxruntime,
    токенизатор читается, и похожие фразы оказываются ближе разных (санити-разделение).
    Матчер при сбое эмбеддера деградирует на слой правил, но зелёный doctor должен
    означать, что выбранный слой эмбеддингов реально работает → при провале FAIL.
    """
    from jarvis import matcher

    sep = matcher.sanity_separation()
    if sep is None:
        return CheckResult(
            FAIL, "Эмбеддер команд (rubert-tiny2)",
            reason="модель/токенизатор не загрузились или не дали векторов.",
            fix="jarvis models --download  (или сверьте JARVIS_EMBEDDER_MODEL/_TOKENIZER; "
                "pip install -e . для onnxruntime/tokenizers)",
        )
    similar, different = sep
    if similar <= different:
        return CheckResult(
            FAIL, "Эмбеддер команд (rubert-tiny2)",
            reason=f"векторы неосмысленны: похожие {similar:.3f} ≤ разные {different:.3f}.",
            fix="сверьте модель эмбеддера и пулинг (mean) в jarvis/matcher.py",
        )
    return CheckResult(
        OK,
        f"Эмбеддер команд грузится и осмыслен (похожие {similar:.3f} > разные {different:.3f})")


def check_matcher() -> CheckResult:
    """Матчер реально распознаёт команды из commands.yaml (слой ПРАВИЛ, без сети).

    Для каждой команды берём её ПЕРВЫЙ синоним и убеждаемся, что матчер вернул именно
    её тег. Это ловит опечатки в синонимах, пересечения и регрессии нормализации —
    «работает», а не «существует». Команды без синонимов подсвечиваем (их не распознать).
    """
    import yaml

    from jarvis import matcher as matcher_mod

    try:
        with open(config.COMMANDS_FILE, encoding="utf-8") as f:
            commands = yaml.safe_load(f) or {}
    except Exception as exc:
        return CheckResult(WARN, "Матчер команд",
                           reason=f"не удалось прочитать {config.COMMANDS_FILE}: {exc}.",
                           fix="проверьте синтаксис commands.yaml")
    if not commands:
        return CheckResult(WARN, "Матчер команд", reason="карта команд пуста.",
                           fix="добавьте команды в commands.yaml")
    # Только правила: эмбеддер не трогаем (его проверяет check_embedder отдельно).
    m = matcher_mod.Matcher(commands, embedder=_DisabledEmbedder())
    # Служебные top-level ключи (ветки/обратимость, ТЗ-5) — НЕ команды, исключаем из проверки.
    real = {t: s for t, s in commands.items() if t not in matcher_mod._RESERVED_KEYS}
    no_synonyms = [t for t, s in real.items() if not (s or {}).get("синонимы")]
    wrong = []
    for tag, spec in real.items():
        synonyms = (spec or {}).get("синонимы") or []
        if not synonyms:
            continue
        got = m.match(synonyms[0])
        if got is None or got.tag != tag:
            wrong.append(f"{tag}→{got.tag if got else '∅'}")
    if wrong:
        return CheckResult(
            FAIL, "Матчер команд",
            reason=f"первый синоним распознан неверно: {', '.join(wrong)}.",
            fix="поправьте синонимы в commands.yaml (уникальные, без пересечений)",
        )
    if no_synonyms:
        return CheckResult(
            WARN, "Матчер команд",
            reason=f"у команд нет синонимов (их не распознать правилами): {', '.join(no_synonyms)}.",
            fix="добавьте поле «синонимы» этим командам в commands.yaml",
        )
    return CheckResult(
        OK, f"Матчер: {len(real)} команд распознаются по синонимам (правила)")


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
    # TTS воспроизводит через pw-cat (PipeWire), НЕ sounddevice — на TUXEDO PortAudio
    # видит только HDMI и Джарвис был нем. Без pw-cat голос недоступен → это блокер.
    if shutil.which("pw-cat"):
        results.append(CheckResult(OK, "Воспроизведение TTS: pw-cat (PipeWire) доступен"))
    else:
        results.append(CheckResult(
            FAIL, "Воспроизведение TTS: pw-cat",
            reason="pw-cat не найден в $PATH — TTS не сможет играть звук (Джарвис будет нем).",
            fix="sudo apt install -y pipewire-bin (обычно уже стоит вместе с PipeWire)",
        ))
    # Адаптивная громкость (audio_env): pactl — ducking музыки, pw-cat --record — замер реального
    # уровня воспроизведения. Нет → деградация в фиксированную громкость (WARN, не блокер).
    if shutil.which("pactl"):
        results.append(CheckResult(OK, "Адаптивная громкость: pactl + pw-cat доступны"))
    else:
        results.append(CheckResult(
            WARN, "Адаптивная громкость: pactl",
            reason="нет pactl — ducking музыки и замер уровня недоступны, громкость фиксированная.",
            fix="sudo apt install -y pipewire-pulse (даёт pactl)",
        ))
    return results


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
    """VAD и АКТИВНЫЙ ASR-пресет реально загружаются движком, а не «файл на месте»."""
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
    if not config.ASR_PRESET_KNOWN:
        results.append(CheckResult(
            WARN, f"ASR-пресет «{config.ASR_PRESET}» неизвестен",
            reason="models.asr_preset не из списка пресетов — STT возьмёт zipformer-small-ru.",
            fix="поправьте settings.yaml (models.asr_preset)",
        ))
    try:
        # Грузим ровно так же, как stt._init_engines (та же ветка по типу пресета).
        p = config.ASR_PATHS
        if config.ASR_TYPE == "nemo_ctc":
            sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
                model=p["model"], tokens=p["tokens"], sample_rate=config.SAMPLE_RATE)
        elif config.ASR_TYPE == "nemo_transducer":
            sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=p["encoder"], decoder=p["decoder"], joiner=p["joiner"],
                tokens=p["tokens"], model_type="nemo_transducer", sample_rate=config.SAMPLE_RATE)
        else:
            sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=p["encoder"], decoder=p["decoder"], joiner=p["joiner"],
                tokens=p["tokens"], modeling_unit="bpe", bpe_vocab=p["bpe"])
        results.append(CheckResult(OK, f"Модель ASR ({config.ASR_PRESET}) загружается движком"))
    except Exception as exc:
        results.append(CheckResult(
            FAIL, f"Модель ASR ({config.ASR_PRESET}) загружается движком",
            reason=f"движок не смог загрузить пресет: {exc}",
            fix=f"jarvis models --download  (кандидаты: jarvis models --download <имя>; "
                f"или откат models.asr_preset)",
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
        # 3) input → должен ответить «Мозг» (state=thinking + say-переспрос)
        reporter.note("публикую jarvis/input (нераспознанная фраза) и жду реакции «Мозга»…")
        client.publish(contracts.TOPIC_INPUT, '{"text":"джарвис как дела"}')
        time.sleep(3.0)
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
# Напоминания о перерыве (детектор активности): доступ к /dev/input и яркость
# --------------------------------------------------------------------------- #
# Биты EV для отбора устройств ВВОДА (бит N = тип события N; см. activity_monitor.py).
# EV_KEY=0x02, EV_REL=0x04, EV_ABS=0x08, EV_REP=0x100000 (0x01=EV_SYN — у всех, не показателен).
_EV_KEY, _EV_REL, _EV_ABS, _EV_REP = 0x02, 0x04, 0x08, 0x100000


def _input_event_paths() -> list:
    """Пути /dev/input/eventN клавиатуры/мыши/тачпада по /proc/bus/input/devices."""
    paths: list = []
    try:
        text = Path("/proc/bus/input/devices").read_text(encoding="utf-8", errors="replace")
    except Exception:
        return paths
    handlers, ev = None, 0
    for line in text.splitlines() + [""]:
        if line.startswith("H: Handlers="):
            handlers = line.split("=", 1)[1]
        elif line.startswith("B: EV="):
            try:
                ev = int(line.split("=", 1)[1].strip(), 16)
            except Exception:
                ev = 0
        elif not line.strip():
            if handlers and ev and (ev & (_EV_REL | _EV_ABS)
                                    or (ev & _EV_KEY and ev & _EV_REP)):
                for tok in handlers.split():
                    if tok.startswith("event"):
                        paths.append("/dev/input/" + tok)
                        break
            handlers, ev = None, 0
    return paths


def check_input_access() -> CheckResult:
    """Детектор активности читает /dev/input (idle на Wayland опрашивать нельзя) — нужен
    доступ к устройствам ввода (группа input). Без него сервис «спит» — проверяем явно."""
    if not config.BREAKS_ENABLED:
        return CheckResult(OK, "напоминания о перерыве выключены (break_reminders.enabled=false)")
    paths = _input_event_paths()
    if not paths:
        return CheckResult(
            WARN, "детектор активности: устройства ввода",
            reason="клавиатура/мышь не найдены в /proc/bus/input/devices.",
            fix="подключите мышь/клавиатуру; проверьте /proc/bus/input/devices",
        )
    import os

    for p in paths:
        try:
            fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
            os.close(fd)
            return CheckResult(OK, f"детектор активности: /dev/input читается ({len(paths)} устройств)")
        except OSError:
            continue
    return CheckResult(
        WARN, "детектор активности: нет доступа к /dev/input",
        reason="пользователь не в группе input → idle не отслеживается, сервис «спит».",
        fix="sudo usermod -aG input $USER, затем ПЕРЕЛОГИН (та же группа нужна ydotool)",
    )


def check_brightness():
    """Затемнение экрана при напоминании — read-only проба brightnessctl (get/max)."""
    if not config.BREAKS_ENABLED:
        return None
    if not shutil.which("brightnessctl"):
        return CheckResult(
            WARN, "затемнение экрана: brightnessctl",
            reason="бинаря brightnessctl нет → затемнение при напоминании не сработает.",
            fix="sudo apt install brightnessctl",
        )
    try:
        cur = subprocess.run(["brightnessctl", "get"], capture_output=True, text=True,
                             timeout=3, check=False)
        mx = subprocess.run(["brightnessctl", "max"], capture_output=True, text=True,
                            timeout=3, check=False)
        if cur.returncode == 0 and cur.stdout.strip().isdigit() and mx.stdout.strip().isdigit():
            return CheckResult(
                OK, f"затемнение экрана: brightnessctl читает яркость "
                    f"({cur.stdout.strip()}/{mx.stdout.strip()})")
        return CheckResult(
            WARN, "затемнение экрана: brightnessctl",
            reason=f"неожиданный вывод get/max (код {cur.returncode}).",
            fix="проверьте `brightnessctl get` вручную",
        )
    except Exception as exc:
        return CheckResult(
            WARN, "затемнение экрана: brightnessctl",
            reason=f"проба не удалась: {exc}",
            fix="проверьте `brightnessctl get` вручную",
        )


def check_push_to_talk():
    """Push-to-talk (зажатие кнопки → команда без wake-word) читает /dev/input — нужен доступ
    (группа input, как ydotool). Нет доступа → кнопка не работает, но wake-word работает."""
    if not config.PTT_ENABLED:
        return CheckResult(OK, "push-to-talk выключен (push_to_talk_enabled=false)")
    import os as _os
    import re as _re

    paths = []
    try:
        with open("/proc/bus/input/devices", encoding="utf-8", errors="replace") as f:
            for block in f.read().split("\n\n"):
                if "kbd" in block:
                    m = _re.search(r"event(\d+)", block)
                    if m:
                        paths.append(f"/dev/input/event{m.group(1)}")
    except Exception:
        pass
    if not paths:
        return CheckResult(WARN, "push-to-talk: клавиатура",
                           reason="клавиатуры не найдены в /proc/bus/input/devices.",
                           fix="проверьте подключение клавиатуры")
    for p in paths:
        try:
            fd = _os.open(p, _os.O_RDONLY | _os.O_NONBLOCK)
            _os.close(fd)
            return CheckResult(
                OK, f"push-to-talk: /dev/input доступен (кнопка «{config.PTT_KEY}» код {config.PTT_KEYCODE})")
        except OSError:
            continue
    return CheckResult(
        WARN, "push-to-talk: нет доступа к /dev/input",
        reason="пользователь не в группе input → кнопка не работает (wake-word продолжает работать).",
        fix="sudo usermod -aG input $USER, затем ПЕРЕЛОГИН (та же группа нужна ydotool)")


def check_notifications() -> list[CheckResult]:
    """Уведомления (ТЗ-6): gdbus есть, D-Bus-сервер с actions (кнопки), kitty для кнопки, состояние
    тишины читается. Нет графической сессии (headless/cron) → WARN, не FAIL (фича вспомогательная)."""
    import shutil as _shutil
    import subprocess as _sp

    if not config.NOTIFICATIONS_ENABLED:
        return [CheckResult(OK, "уведомления выключены (notifications.enabled=false)")]
    results = []
    if not _shutil.which("gdbus"):
        results.append(CheckResult(
            WARN, "уведомления: gdbus не найден",
            reason="без gdbus системные уведомления недоступны (режим тишины/дубль/сбои не покажутся).",
            fix="sudo apt install libglib2.0-bin"))
    else:
        try:
            out = _sp.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
                           "--object-path", "/org/freedesktop/Notifications",
                           "--method", "org.freedesktop.Notifications.GetCapabilities"],
                          capture_output=True, text=True, timeout=4)
            if out.returncode == 0 and "actions" in out.stdout:
                results.append(CheckResult(OK, "уведомления: D-Bus-сервер с actions (кнопки) на месте"))
            elif out.returncode == 0:
                results.append(CheckResult(
                    WARN, "уведомления: сервер без поддержки actions",
                    reason="кнопка «Открыть логи» может не отображаться этим сервером уведомлений."))
            else:
                results.append(CheckResult(
                    WARN, "уведомления: сервер не отвечает",
                    reason="org.freedesktop.Notifications недоступен (нет графической сессии?).",
                    fix="запускайте в активной сессии рабочего стола"))
        except Exception as exc:
            results.append(CheckResult(WARN, "уведомления: проба сервера", reason=str(exc)))
    if _shutil.which("kitty"):
        results.append(CheckResult(OK, "уведомления: kitty есть — кнопка откроет лог модуля"))
    else:
        results.append(CheckResult(
            WARN, "уведомления: kitty не найден",
            reason="кнопка «Открыть логи» не сможет открыть терминал с логом.",
            fix="sudo apt install kitty"))
    try:
        from jarvis import silence
        cur = "тишина" if silence.is_silent() else "голос"
        results.append(CheckResult(OK, f"режим тишины: состояние читается (сейчас — {cur})"))
    except Exception as exc:
        results.append(CheckResult(WARN, "режим тишины: состояние", reason=str(exc)))
    return results


def check_system() -> list[CheckResult]:
    """Системное (ТЗ-7): rich для панели, systemd-run для рестарта, KWin DBus для сред, валидность
    именованных сред. Не KDE-сессия / нет инструмента → WARN (фича недоступна), не FAIL."""
    import shutil as _sh
    import subprocess as _sp

    results = []
    try:
        import rich  # noqa: F401
        results.append(CheckResult(OK, "live-панель: rich установлен (`jarvis live`)"))
    except Exception:
        results.append(CheckResult(WARN, "live-панель: rich не установлен",
                                   reason="`jarvis live` недоступен.", fix="pip install rich"))
    if _sh.which("systemd-run"):
        results.append(CheckResult(OK, "перезагрузка: systemd-run --user есть (откреплённый рестарт)"))
    else:
        results.append(CheckResult(WARN, "перезагрузка: systemd-run не найден",
                                   reason="рестарт пойдёт через setsid-фолбэк (может не пережить рестарт core).",
                                   fix="обычно есть в systemd; проверьте окружение"))
    if _sh.which("qdbus6"):
        try:
            r = _sp.run(["qdbus6", "org.kde.KWin", "/VirtualDesktopManager",
                         "org.kde.KWin.VirtualDesktopManager.count"],
                        capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip().isdigit():
                results.append(CheckResult(
                    OK, f"рабочие среды: KWin DBus отвечает (виртуальных столов {r.stdout.strip()})"))
            else:
                results.append(CheckResult(WARN, "рабочие среды: KWin DBus не отвечает",
                                           reason="создание виртуальных столов недоступно (не KDE-сессия?)."))
        except Exception as exc:
            results.append(CheckResult(WARN, "рабочие среды: проба KWin", reason=str(exc)))
    else:
        results.append(CheckResult(WARN, "рабочие среды: qdbus6 не найден",
                                   reason="виртуальные столы KDE недоступны.", fix="sudo apt install qt6-tools"))
    try:
        import yaml as _yaml
        with open(config.COMMANDS_FILE, encoding="utf-8") as f:
            cmds = _yaml.safe_load(f) or {}
        bad = []
        for name, spec in (config.ENVIRONMENTS or {}).items():
            for tag in ((spec or {}).get("apps") or []):
                if not isinstance(cmds.get(tag), dict):
                    bad.append(f"{name}:{tag}")
        if bad:
            results.append(CheckResult(FAIL, "рабочие среды: неизвестные теги приложений",
                                       reason=", ".join(bad[:6]),
                                       fix="поправьте environments.named в settings.yaml"))
        else:
            results.append(CheckResult(
                OK, f"рабочие среды: {len(config.ENVIRONMENTS)} именованных, теги приложений валидны"))
    except Exception as exc:
        results.append(CheckResult(WARN, "рабочие среды: проверка тегов", reason=str(exc)))
    return results


def check_lamp() -> list[CheckResult]:
    """Умные лампы (ТЗ-8, заход «лампы»): tinytuya, СПИСОК ламп lamp.lamps, связь per-lamp
    (WARN, не FAIL — лампа в розетке/Wi-Fi это норма жизни), стражи гейтов голосовой яркости.

    ⚠ Tuya держит ОДИН локальный сокет на лампу: пока сервис jarvis-lamp активен, свои пробы
    НЕ открываем (рвали бы персистентные сокеты сервиса) — читаем его снимок lamp_state.json.
    Прямые пробы — только когда сервис не запущен."""
    if not config.LAMP_ENABLED:
        return [CheckResult(OK, "лампы выключены (lamp.enabled=false)")]
    results = [_lamp_gates_result()]
    try:
        import tinytuya
    except Exception:
        results.append(CheckResult(WARN, "лампы: tinytuya не установлен",
                                   reason="управление лампами недоступно.", fix="pip install -e ."))
        return results
    results.append(CheckResult(OK, f"лампы: tinytuya {getattr(tinytuya, 'version', '?')}"))
    devices = config.LAMP_DEVICES
    if not devices:
        results.append(CheckResult(
            WARN, "лампы: не заданы",
            reason="lamp.lamps пуст (и старых плоских device_id/local_key нет).",
            fix="впишите лампы в settings.yaml → lamp.lamps; затем `jarvis lamp test`"))
        return results
    service_active = _systemctl_show("jarvis-lamp.service").get("ActiveState") == "active"
    snapshot = _lamp_snapshot() if service_active else None
    for name, creds in devices.items():
        if service_active:
            results.append(_lamp_state_result(name, snapshot))
        else:
            results.append(_lamp_probe_result(name, creds, tinytuya))
    return results


def _lamp_gates_result() -> CheckResult:
    """Стражи гейтов голосовой яркости: «громкость лампы 50» обязана идти ЛАМПАМ, а не менять
    громкость голоса (гейт-коллизия поймана в заходе «лампы» — не возвращать)."""
    try:
        from jarvis import lamp as lamp_helpers
        from jarvis import voice_volume
        cases_ok = (
            lamp_helpers.is_lamp_level_command("яркость ламп 50")
            and lamp_helpers.is_lamp_level_command("лампы наполовину")
            and not lamp_helpers.is_lamp_level_command("включи лампу")
            and not lamp_helpers.is_lamp_level_command("включи лампу на 5 минут")
            and not voice_volume.is_volume_command("громкость лампы 50")
            and voice_volume.is_volume_command("громкость 30")
        )
        if cases_ok:
            return CheckResult(OK, "лампы: гейты яркости голосом (6 фраз)")
        return CheckResult(
            FAIL, "лампы: гейты яркости сломаны",
            reason="фразы о яркости ламп / громкости голоса попадают не в свой гейт.",
            fix="см. jarvis/lamp.py::is_lamp_level_command и voice_volume.is_volume_command")
    except Exception as exc:
        return CheckResult(FAIL, "лампы: гейты яркости не проверились", reason=str(exc))


def _lamp_snapshot() -> dict | None:
    """Снимок сервиса ламп (logs/lamp_state.json) или None."""
    import json as _json
    import os as _os

    path = _os.path.join(str(config.LOGS_DIR), "lamp_state.json")
    if not _os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return None


def _lamp_state_result(name: str, snapshot: dict | None) -> CheckResult:
    """Состояние ОДНОЙ лампы по снимку сервиса — без второго сокета к Tuya.

    Формат снимка: {"updated_at", "keepalive_minutes", "лампы": {имя: {connected, ip,
    version}}}; старый плоский формат одной лампы (до захода «лампы») принимается фолбэком."""
    from datetime import datetime as _dt

    if not snapshot:
        return CheckResult(WARN, f"лампа «{name}»: сервис запущен, снимка состояния ещё нет",
                           reason="lamp_state.json не появился (сервис только стартовал?).",
                           fix="подождите минуту; затем `journalctl --user -u jarvis-lamp -n 20`")
    try:
        st = (snapshot.get("лампы") or {}).get(name)
        if st is None and "connected" in snapshot and len(config.LAMP_DEVICES) == 1:
            st = snapshot   # фолбэк: старый плоский снимок — только для ОДНОЙ лампы в конфиге
        if not isinstance(st, dict):
            return CheckResult(WARN, f"лампа «{name}»: нет в снимке сервиса",
                               reason="сервис ещё работает со старым конфигом (до перезапуска).",
                               fix="перезапустите сервисы: `jarvis start`")
        age = None
        try:
            age = (_dt.now() - _dt.fromisoformat(snapshot.get("updated_at", ""))).total_seconds()
        except Exception:
            pass
        keepalive = float(snapshot.get("keepalive_minutes") or 0)
        # Снимок освежается keepalive-пингом; протух сильнее 3 интервалов (мин. 10 мин) → WARN.
        if st.get("connected") and keepalive > 0 and age is not None \
                and age > max(600.0, keepalive * 60 * 3):
            return CheckResult(WARN, f"лампа «{name}»: снимок состояния устарел",
                               reason=f"lamp_state.json обновлялся {int(age // 60)} мин назад.",
                               fix="проверьте лог: journalctl --user -u jarvis-lamp -n 30")
        if st.get("connected"):
            return CheckResult(OK, f"лампа «{name}»: на связи ({st.get('ip')}, "
                                   f"протокол {st.get('version')}) — по данным сервиса")
        return CheckResult(WARN, f"лампа «{name}»: не в сети (по данным сервиса)",
                           reason="сервис jarvis-lamp переподключается в фоне.",
                           fix="проверьте питание лампы и Wi-Fi; лог: journalctl --user -u jarvis-lamp")
    except Exception as exc:
        return CheckResult(WARN, f"лампа «{name}»: состояние", reason=str(exc))


def _lamp_probe_result(name: str, creds: dict, tinytuya) -> CheckResult:
    """Прямая проба ОДНОЙ лампы (только когда сервис НЕ активен): status() с её кредами."""
    ip = creds.get("ip", "")
    if not ip:
        return CheckResult(WARN, f"лампа «{name}»: IP не задан",
                           reason="ip пуст — нужен автопоиск (медленно) или явный адрес.",
                           fix="укажите ip в settings.yaml → lamp.lamps")
    try:
        bulb = tinytuya.BulbDevice(creds["device_id"], address=ip,
                                   local_key=creds["local_key"],
                                   version=float(creds.get("version", 3.5)))
        bulb.set_socketTimeout(3)
        bulb.set_socketRetryLimit(1)   # без внутренних ретраев tinytuya (5×10с) — doctor не виснет
        bulb.set_socketRetryDelay(1)
        st = bulb.status()
        if isinstance(st, dict) and "Error" not in st and "Err" not in st:
            return CheckResult(OK, f"лампа «{name}»: на связи ({ip}, "
                                   f"протокол {creds.get('version', 3.5)})")
        return CheckResult(WARN, f"лампа «{name}»: не отвечает",
                           reason=f"status: {st}",
                           fix="проверьте питание лампы и ВЕРСИЮ протокола в settings.yaml")
    except Exception as exc:
        return CheckResult(WARN, f"лампа «{name}»: нет связи",
                           reason=str(exc),
                           fix="проверьте ip/local_key и версию протокола; `jarvis lamp test --lamp "
                               f"{name}`")


def check_phone() -> list[CheckResult]:
    """Телефон (ТЗ-9): статус из logs/phone_state.json (его пишет сервис phone по событиям приложения).
    Нет данных/офлайн → WARN (телефон может быть выключен/приложение не запущено), не FAIL."""
    if not config.PHONE_ENABLED:
        return [CheckResult(OK, "телефон выключен (phone.enabled=false)")]
    import json as _json
    import os as _os

    path = _os.path.join(str(config.LOGS_DIR), "phone_state.json")
    if not _os.path.exists(path):
        return [CheckResult(
            WARN, "телефон: событий не было",
            reason="приложение «Спутник» ещё ничего не присылало (телефон офлайн / приложение не запущено).",
            fix="запустите приложение на телефоне; проверьте подключение к брокеру Mosquitto")]
    try:
        with open(path, encoding="utf-8") as f:
            st = _json.load(f)
        if st.get("status") == "online":
            extra = []
            if st.get("battery") is not None:
                extra.append(f"заряд {st['battery']}%")
            if st.get("presence"):
                extra.append("дома" if st["presence"] == "home" else "не дома")
            return [CheckResult(OK, "телефон: на связи" + (f" ({', '.join(extra)})" if extra else ""))]
        return [CheckResult(WARN, "телефон: офлайн", reason="последний статус — offline.",
                            fix="запустите приложение «Спутник» на телефоне")]
    except Exception as exc:
        return [CheckResult(WARN, "телефон: состояние", reason=str(exc))]


def check_chains() -> list[CheckResult]:
    """Цепочки (ТЗ-5): ветки продолжений валидны, обратимость валидна, фильтр/комбо-split/гейты живы."""
    results = []
    try:
        import yaml

        from jarvis import chains
        from jarvis.matcher import Matcher
        with open(config.COMMANDS_FILE, encoding="utf-8") as f:
            commands = yaml.safe_load(f) or {}
    except Exception as exc:
        return [CheckResult(FAIL, "цепочки: загрузка", reason=str(exc),
                            fix="см. commands.yaml / jarvis/chains.py")]

    # 1) Ветки: все перечисленные теги существуют как команды.
    try:
        branches, _primary = chains.build_branches(commands)
        missing = []
        for name, members in (commands.get("ветки") or {}).items():
            for t in (members or []):
                if not isinstance(commands.get(t), dict):
                    missing.append(f"{name}:{t}")
        if missing:
            results.append(CheckResult(
                FAIL, "цепочки: ветки ссылаются на несуществующие теги",
                reason=", ".join(missing[:8]), fix="поправьте список в commands.yaml → ветки"))
        else:
            results.append(CheckResult(
                OK, f"ветки продолжений: {len(branches)} ({', '.join(branches)}) — теги валидны"))
    except Exception as exc:
        results.append(CheckResult(FAIL, "цепочки: ветки", reason=str(exc)))

    # 2) Обратимость («отмени»): ключи и значения — реальные команды.
    try:
        inv_map = commands.get("обратимость") or {}
        bad = [f"{k}->{v}" for k, v in inv_map.items()
               if not (isinstance(commands.get(k), dict) and isinstance(commands.get(v), dict))]
        if bad:
            results.append(CheckResult(
                FAIL, "цепочки: обратимость ссылается на несуществующие теги",
                reason=", ".join(bad[:8]), fix="поправьте commands.yaml → обратимость"))
        else:
            results.append(CheckResult(OK, f"обратимость («отмени»): {len(inv_map)} пар — валидны"))
    except Exception as exc:
        results.append(CheckResult(FAIL, "цепочки: обратимость", reason=str(exc)))

    # 3) Логика: фильтр продолжений по ветке + комбо-split + гейты повтор/отмена (на эталонах).
    try:
        m = Matcher(commands)
        branches, _ = chains.build_branches(commands)
        music = branches.get("музыка", set())
        ok_cont = m.match("тише", allowed_tags=music, use_embeddings=False)        # должно совпасть
        no_cont = m.match("открой браузер", allowed_tags=music, use_embeddings=False)  # не должно
        combo = chains.split_combo("выключи блютуз и вай-фай")  # ≥2 части
        single = chains.split_combo("сделай тише")             # None
        problems = []
        if not (ok_cont and ok_cont.tag in music):
            problems.append("продолжение «тише» не сматчилось в ветке")
        if no_cont is not None:
            problems.append("«открой браузер» ложно принято как продолжение")
        if not (combo and len(combo) >= 2):
            problems.append("split_combo не разбил комбо")
        if single is not None:
            problems.append("split_combo ложно разбил одиночную команду")
        if not (chains.is_repeat("повтори последнее") and chains.is_undo("отмени")):
            problems.append("гейты повтор/отмена не сработали")
        if problems:
            results.append(CheckResult(FAIL, "цепочки: логика", reason="; ".join(problems)))
        else:
            results.append(CheckResult(
                OK, "цепочки: фильтр ветки/комбо-split/повтор-отмена — распознаются"))
    except Exception as exc:
        results.append(CheckResult(FAIL, "цепочки: логика", reason=str(exc)))
    return results


def check_alarms() -> list[CheckResult]:
    """Будильники: расписание читается, парсер времени/команд жив, погода доступна (или офлайн).

    Парсер и расписание — офлайн (поломка → FAIL). Погода сетевая → офлайн = WARN, не FAIL
    (будильник просто без погоды)."""
    import os as _os

    results = []
    if not config.ALARMS_ENABLED:
        return [CheckResult(OK, "будильники выключены (alarms.enabled=false)")]
    try:
        from jarvis import alarms
    except Exception as exc:
        return [CheckResult(FAIL, "будильники: импорт", reason=str(exc), fix="см. jarvis/alarms.py")]

    # 1) Расписание читается и валидно (нет файла — это норма, пустые списки).
    try:
        sched = alarms.read_schedule()
        nb = len(sched.get("будильники", []))
        nt = len(sched.get("таймеры", []))
        ns = len(sched.get("секундомеры", []))
        results.append(CheckResult(
            OK, f"расписание читается ({_os.path.basename(config.SCHEDULE_FILE)}): "
                f"будильников {nb}, таймеров {nt}, секундомеров {ns}"))
    except Exception as exc:
        results.append(CheckResult(FAIL, "расписание", reason=str(exc),
                                   fix="проверьте schedule.yaml (валидный YAML)"))

    # 2) Парсер времени/команд жив (эталонные фразы → ожидаемый разбор).
    try:
        expected = [
            ("поставь утренний будильник на 7 утра", "set", "morning", 7, 0, None),
            ("перенеси утренний на полвосьмого", "move", "morning", 7, 30, None),
            ("поставь будильник на 7 вечера с пометкой ужин", "set", "regular", 19, 0, "ужин"),
            ("убери все будильники", "delete_all", "regular", None, None, None),
        ]
        bad = []
        for text, act, typ, h, m, lbl in expected:
            c = alarms.parse_command(text)
            if (not c or c["действие"] != act or c["тип"] != typ or c["час"] != h
                    or c["минута"] != m or (c.get("метка") or None) != lbl):
                bad.append(text)
        if bad:
            results.append(CheckResult(
                FAIL, "парсер будильников",
                reason=f"эталонные фразы разобраны неверно: {'; '.join(bad)}.",
                fix="см. jarvis/alarms.py (parse_time/parse_command)"))
        else:
            results.append(CheckResult(OK, "парсер будильников: время/тип/метка/«все» распознаются"))
    except Exception as exc:
        results.append(CheckResult(FAIL, "парсер будильников", reason=str(exc),
                                   fix="см. jarvis/alarms.py"))

    # 3) Погода — сетевая, поэтому офлайн/ошибка = WARN (не FAIL): будильник звонит без погоды.
    if not config.ALARM_WEATHER_ENABLED:
        results.append(CheckResult(OK, "погода в будильнике выключена (alarms.weather_enabled=false)"))
    else:
        try:
            from jarvis import weather
            geo = weather.geocode(config.REGION)
            if geo:
                results.append(CheckResult(
                    OK, f"погода: регион «{config.REGION}» определён ({geo[0]:.2f}, {geo[1]:.2f})"))
            else:
                results.append(CheckResult(
                    WARN, "погода: регион/сеть",
                    reason=f"не удалось определить регион «{config.REGION}» (нет сети или опечатка).",
                    fix="проверьте интернет и поле «регион» в settings.yaml — пока будильник без погоды"))
        except Exception as exc:
            results.append(CheckResult(WARN, "погода", reason=str(exc),
                                       fix="будильник будет звонить без погоды"))

    # 4) Парсер таймеров/секундомеров и длительности (офлайн → FAIL при поломке).
    try:
        from jarvis import timers
        ok_dur = (timers.parse_duration("на пять минут") == 300
                  and timers.parse_duration("полтора часа") == 5400
                  and timers.parse_duration("на 30 секунд") == 30)
        tc = timers.parse_timer_command("поставь таймер на 10 минут с пометкой чай")
        sc = timers.parse_stopwatch_command("засеки время с пометкой работа")
        ok_cmd = (tc and tc["действие"] == "set" and tc["длительность"] == 600 and tc["метка"] == "чай"
                  and sc and sc["действие"] == "start" and sc["метка"] == "работа")
        if ok_dur and ok_cmd:
            results.append(CheckResult(
                OK, "парсер таймеров/секундомеров: длительность/команды/метки распознаются"))
        else:
            results.append(CheckResult(FAIL, "парсер таймеров/секундомеров",
                                       reason="эталонные фразы разобраны неверно.",
                                       fix="см. jarvis/timers.py"))
    except Exception as exc:
        results.append(CheckResult(FAIL, "парсер таймеров/секундомеров", reason=str(exc),
                                   fix="см. jarvis/timers.py"))

    # 5) Мировое время: zoneinfo + часовой пояс пробного города (сеть → WARN офлайн, не FAIL).
    try:
        from zoneinfo import ZoneInfo  # noqa: F401  — проверяем наличие tz-базы
        from jarvis import worldtime
        info = worldtime.geocode_city("париже")
        if info and info.get("tz"):
            results.append(CheckResult(
                OK, f"мировое время: пробный город → {info['name']} ({info['tz']})"))
        else:
            results.append(CheckResult(
                WARN, "мировое время: сеть",
                reason="не удалось определить часовой пояс пробного города (нет сети?).",
                fix="нужен интернет для геокодинга; офлайн — мировое время недоступно"))
    except Exception as exc:
        results.append(CheckResult(WARN, "мировое время", reason=str(exc),
                                   fix="проверьте zoneinfo/tzdata и сеть"))

    # 6) Монетка: паки фраз на месте.
    try:
        if config.COIN_HEADS and config.COIN_TAILS:
            results.append(CheckResult(OK, "монетка: паки фраз на месте"))
        else:
            results.append(CheckResult(WARN, "монетка", reason="пустой пак фраз.",
                                       fix="заполните coin.heads/coin.tails в settings.yaml"))
    except Exception:
        pass

    # 7) Парсер дат и команд напоминаний/задач (офлайн → FAIL при поломке).
    try:
        from datetime import date as _d, timedelta as _td

        from jarvis import reminders
        base = _d(2026, 6, 9)
        ok_date = (reminders.parse_date("завтра", base) == base + _td(days=1)
                   and reminders.parse_date("через неделю", base) == base + _td(weeks=1)
                   and reminders.parse_date("15 июня", base) == _d(2026, 6, 15)
                   and reminders.parse_date("в пятницу", base) == _d(2026, 6, 12)
                   and reminders.parse_date("пятнадцатого июня", base) == _d(2026, 6, 15))
        rc = reminders.parse_reminder_command("напомни про стирку сегодня в 12")
        tc = reminders.parse_task_command("добавь задачу починить кран")
        ok_cmd = (rc and rc.get("действие") == "set" and rc.get("текст") == "стирку"
                  and rc.get("время") == (12, 0)
                  and tc and tc.get("действие") == "add" and tc.get("текст") == "починить кран")
        _s = alarms.read_schedule()
        nr = len(_s.get("напоминания", []))
        ntk = len(_s.get("задачи", []))
        if ok_date and ok_cmd:
            results.append(CheckResult(
                OK, f"парсер напоминаний/задач: даты/команды распознаются "
                    f"(в расписании: напоминаний {nr}, задач {ntk})"))
        else:
            results.append(CheckResult(FAIL, "парсер напоминаний/задач",
                                       reason="эталонные формы разобраны неверно.",
                                       fix="см. jarvis/reminders.py (parse_date/parse_*_command)"))
    except Exception as exc:
        results.append(CheckResult(FAIL, "парсер напоминаний/задач", reason=str(exc),
                                   fix="см. jarvis/reminders.py"))
    return results


# --------------------------------------------------------------------------- #
# Оркестрация
# --------------------------------------------------------------------------- #
def run(quick: bool = False) -> bool:
    """Полная глубокая диагностика (дефолт). quick=True пропускает долгие тесты
    (синтез Piper, сквозная цепочка — секунды). Каждая проверка идёт через _safe:
    её падение не роняет весь доктор.
    """
    reporter = Reporter()

    reporter.section("Слой 1. Окружение и пакет")
    _safe(reporter, check_venv)
    _safe(reporter, check_imports)
    _safe(reporter, check_library_versions)
    _safe(reporter, check_settings)
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
    _safe(reporter, check_audio)
    _safe(reporter, check_input_access)
    _safe(reporter, check_brightness)
    _safe(reporter, check_push_to_talk)
    _safe(reporter, check_notifications)
    _safe(reporter, check_system)
    _safe(reporter, check_lamp)
    _safe(reporter, check_phone)

    reporter.section("Слой 4. Модели и распознавание")
    reporter.note("загружаю модели движком…")
    _safe(reporter, check_sherpa_models)
    _safe(reporter, check_piper_voice)
    _safe(reporter, check_sample_rate)
    reporter.note("проверяю эмбеддер команд (rubert-tiny2)…")
    _safe(reporter, check_embedder)
    _safe(reporter, check_matcher)
    _safe(reporter, check_alarms)
    _safe(reporter, check_chains)
    if not quick:
        reporter.note("синтез тестового сэмпла Piper…")
        _safe(reporter, check_piper_synthesis)

    reporter.section("Слой 5. Сервисы Джарвиса")
    _safe(reporter, check_services)
    _safe(reporter, check_subscription_restore)
    _safe(reporter, check_service_health)
    _safe(reporter, check_heartbeat)

    ok = reporter.summary()

    if not quick:
        # Сквозную цепочку печатаем отдельной секцией со своим итогом.
        live_chain_test()
    return ok
