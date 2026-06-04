"""Глубокая диагностика «Джарвиса»: проверяем РАБОТОСПОСОБНОСТЬ, а не наличие.

Принцип: «файл существует» и «синтаксис валиден» запрещены как единственная
проверка — они зелёные, когда всё сломано. Каждая проверка убеждается, что
компонент реально работает, и при провале даёт человеческое решение:
что произошло → почему → точная команда/правка для починки.

Граница режимов — по стоимости:
  быстрый  — всё, что отвечает за доли секунды (коннекты, импорты, загрузка);
  --deep   — только реально долгое: генерация LLM, синтез звука, сквозная цепочка.

Сам doctor не падает: каждая проверка обёрнута в try-except, любой сбой —
честный ✗ с диагнозом, а не краш CLI.
"""
import importlib
import shutil
import socket
import sys
import time
from pathlib import Path

from jarvis import config, contracts
from jarvis.services_map import SERVICES
from jarvis.ui import FAIL, OK, WARN, CheckResult, Reporter


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


def check_commands_yaml() -> list[CheckResult]:
    """commands.yaml семантически: команда — список, бинарь есть в $PATH."""
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
        if shutil.which(binary):
            results.append(CheckResult(OK, f"команда «{tag}» → {binary}"))
        else:
            results.append(CheckResult(
                WARN, f"команда «{tag}» → {binary}",
                reason=f"бинарь {binary!r} не найден в $PATH.",
                fix=f"установите {binary} или поправьте путь в commands.yaml",
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
            reason=f"брокер {config.MQTT_HOST}:{config.MQTT_PORT} не отвечает ({exc}).",
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
            reason=f"сервер {config.OLLAMA_HOST} недоступен ({exc}).",
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
    if not config.GEMINI_API_KEY:
        return CheckResult(
            WARN, "Gemini casual-бэкенд",
            reason="GEMINI_API_KEY не задан — беседа будет уходить в офлайн-фоллбэк.",
            fix="впишите ключ в .env (см. .env.example) или JARVIS_CASUAL_BACKEND=local",
        )
    grounding = "вкл" if config.GEMINI_GROUNDING else "выкл"
    proxy = ", через прокси" if config.GEMINI_PROXY else ""
    return CheckResult(
        OK,
        f"Gemini casual-бэкенд: модель {config.GEMINI_MODEL}, grounding {grounding}{proxy}",
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


def check_systemd_units() -> list[CheckResult]:
    """systemd --user-юниты найдены, ExecStart указывает на существующий бинарь."""
    import os

    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    units_dir = Path(base) / "systemd" / "user"
    results = []
    for svc in SERVICES:
        unit = units_dir / svc.unit
        if not unit.exists():
            results.append(CheckResult(
                WARN, f"юнит {svc.unit}",
                reason="юнит ещё не установлен в ~/.config/systemd/user/.",
                fix="jarvis start  (сгенерирует юниты с верными путями)",
            ))
            continue
        try:
            exec_start = ""
            for line in unit.read_text(encoding="utf-8").splitlines():
                if line.startswith("ExecStart="):
                    exec_start = line.split("=", 1)[1].strip()
                    break
            binary = exec_start.split()[0] if exec_start else ""
            if binary and Path(binary).exists():
                results.append(CheckResult(OK, f"юнит {svc.unit} → {binary}"))
            else:
                results.append(CheckResult(
                    FAIL, f"юнит {svc.unit}",
                    reason=f"ExecStart указывает на несуществующий бинарь: {binary!r}.",
                    fix="jarvis start  (перегенерирует юниты от текущего venv)",
                ))
        except Exception as exc:
            results.append(CheckResult(FAIL, f"юнит {svc.unit}",
                                       reason=f"не удалось прочитать юнит: {exc}",
                                       fix="jarvis start"))
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
                           reason=f"клиент недоступен: {exc}", fix="ollama serve")
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
            reason=f"модель {config.OLLAMA_MODEL} не ответила: {exc}.",
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
    if not config.GEMINI_API_KEY:
        return CheckResult(
            WARN, "Gemini: живой ответ",
            reason="GEMINI_API_KEY не задан — живой тест пропущен.",
            fix="впишите ключ в .env (см. .env.example)",
        )
    try:
        import logging

        from jarvis import casual

        backend = casual.CasualBackend(logging.getLogger("jarvis-doctor-gemini"))
        answer = backend.reply("Ответь одним коротким словом для проверки связи.")
    except Exception as exc:
        return CheckResult(
            FAIL, "Gemini: живой ответ",
            reason=f"запрос упал: {exc}",
            fix="проверьте GEMINI_API_KEY, сеть и имя модели JARVIS_GEMINI_MODEL",
        )
    if answer in (casual._FALLBACK_FIRST, casual._FALLBACK_AGAIN):
        # backend прогнан тем же путём, что casual (включая прокси), поэтому диагноз
        # из last_error точный: гео-блок/квота/ключ/сеть, а не общая «облако недоступно».
        diag = backend.last_error or "облако недоступно, неверный ключ или имя модели"
        return CheckResult(
            FAIL, "Gemini: живой ответ",
            reason=f"вернулся офлайн-фоллбэк: {diag}.",
            fix="проверьте GEMINI_API_KEY, JARVIS_GEMINI_PROXY, сеть и "
                "JARVIS_GEMINI_MODEL; см. logs/jarvis-core.log",
        )
    proxy = " (через прокси)" if config.GEMINI_PROXY else ""
    return CheckResult(OK, f"Gemini: живой ответ получен{proxy}")


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
def run(deep: bool = False) -> bool:
    """Запустить диагностику. deep=True добавляет долгие живые тесты."""
    reporter = Reporter()

    reporter.section("Слой 1. Окружение и пакет")
    reporter.report(check_venv())
    for r in check_imports():
        reporter.report(r)
    for r in check_config_paths():
        reporter.report(r)
    for r in check_commands_yaml():
        reporter.report(r)
    for r in check_entry_points():
        reporter.report(r)

    reporter.section("Слой 2. Системные службы")
    reporter.report(check_mosquitto())
    reporter.report(check_ollama())
    reporter.report(check_gemini())
    for r in check_audio():
        reporter.report(r)

    reporter.section("Слой 3. Модели")
    reporter.note("загружаю модели движком…")
    for r in check_sherpa_models():
        reporter.report(r)
    reporter.report(check_piper_voice())
    reporter.report(check_sample_rate())

    reporter.section("Слой 4. Сервисы Джарвиса")
    for r in check_services():
        reporter.report(r)
    reporter.report(check_subscription_restore())
    for r in check_systemd_units():
        reporter.report(r)

    if deep:
        reporter.section("Слой 5 (--deep). Живые долгие тесты")
        reporter.note("пробная генерация Ollama (может занять время)…")
        reporter.report(check_ollama_generation())
        reporter.note("синтез тестового сэмпла Piper…")
        reporter.report(check_piper_synthesis())
        reporter.note("живой запрос к Gemini (casual-бэкенд)…")
        reporter.report(check_gemini_live())

    ok = reporter.summary()

    if deep:
        # Сквозную цепочку печатаем отдельной секцией со своим итогом
        live_chain_test()
    return ok
