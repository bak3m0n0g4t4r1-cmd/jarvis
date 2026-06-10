"""Общие настройки «Джарвиса» — ЕДИНАЯ точка загрузки из settings.yaml.

ВСЕ настраиваемые параметры живут в одном человекочитаемом файле `settings.yaml`
(в корне проекта) с русскими комментариями. Здесь — загрузка этого файла, выбор
источника значения и приведение типов. Имена переменных (VAD_THRESHOLD и т.п.)
сохранены — их читает остальной код.

Приоритет источников: переменная окружения JARVIS_* (опц. оверрайд, в т.ч. для
systemd/отладки) → settings.yaml → разумный дефолт. Отсутствие файла/параметра НЕ
роняет сервис (берётся дефолт); битый YAML — предупреждение в stderr + дефолты.
"""
import os
import sys
from pathlib import Path

import yaml

# Корень проекта (jarvis/config.py -> на уровень выше пакета)
BASE_DIR = Path(__file__).resolve().parent.parent


def _resolve(value) -> str:
    """Привести путь к АБСОЛЮТНОМУ: абсолютный — как есть, относительный — от BASE_DIR.

    Зачем: под systemd рабочая директория ≠ корень проекта (юниты стартуют из $HOME),
    поэтому относительный путь из settings.yaml/env иначе не находится — FileNotFoundError
    на моделях и commands.yaml. Любой сбой приведения → строка как есть (сервис не падает).
    """
    try:
        p = Path(str(value)).expanduser()
        return str(p if p.is_absolute() else (BASE_DIR / p))
    except Exception:
        return str(value)


# Единый файл настроек (можно переопределить путь через JARVIS_SETTINGS). Резолвим в
# абсолютный — на случай относительного JARVIS_SETTINGS при запуске не из корня проекта.
SETTINGS_FILE = _resolve(os.getenv("JARVIS_SETTINGS", str(BASE_DIR / "settings.yaml")))

_SETTINGS: dict = {}
try:
    with open(SETTINGS_FILE, encoding="utf-8") as _f:
        _SETTINGS = yaml.safe_load(_f) or {}
except FileNotFoundError:
    pass  # файла нет — работаем на дефолтах (+ возможные env-оверрайды)
except Exception as _exc:  # битый YAML — НЕ падаем, предупреждаем и берём дефолты
    print(f"[config] ВНИМАНИЕ: не удалось разобрать {SETTINGS_FILE}: {_exc} — беру дефолты",
          file=sys.stderr)
    _SETTINGS = {}


def _cast(value, default):
    """Привести значение к типу дефолта (bool/int/float/list/str)."""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in ("0", "false", "no", "")
    if isinstance(default, int) and not isinstance(default, bool):
        return int(value)
    if isinstance(default, float):
        return float(value)
    if isinstance(default, list):
        if isinstance(value, list):
            return [str(v) for v in value]
        return [s.strip() for s in str(value).split(",") if s.strip()]
    if isinstance(default, dict):
        # Словарь (напр. именованные среды) — возвращаем как есть; не из dict → дефолт.
        return value if isinstance(value, dict) else default
    return str(value)


def _get(section: str, key: str, default, env: str | None = None):
    """Значение параметра: env-переменная → settings.yaml[section][key] → дефолт.

    Всё в try-except: любой сбой приведения/чтения → дефолт (сервис не падает).
    """
    if env:
        ev = os.getenv(env)
        if ev is not None and ev != "":
            try:
                return _cast(ev, default)
            except Exception:
                pass
    try:
        sec = _SETTINGS.get(section) or {}
        if key in sec and sec[key] is not None:
            return _cast(sec[key], default)
    except Exception:
        pass
    return default


def _get_path(section: str, key: str, default, env: str | None = None) -> str:
    """Как _get, но результат — ГАРАНТИРОВАННО абсолютный путь (резолв относительных от
    BASE_DIR). Для всех путей-ресурсов (модели, commands.yaml, логи): сервис под systemd
    обязан находить их независимо от рабочей директории."""
    return _resolve(_get(section, key, default, env))


# === Пути и модели (секция models) ===
# ВСЕ пути читаются через _get_path → абсолютны: относительные значения из settings.yaml/env
# разрешаются от BASE_DIR. (Секция "models" в config — исключительно пути, прочее в др. секциях.)
MODELS_DIR = Path(_get_path("models", "models_dir", str(BASE_DIR / "models"), "JARVIS_MODELS_DIR"))
LOGS_DIR = Path(_get_path("models", "logs_dir", str(BASE_DIR / "logs"), "JARVIS_LOGS_DIR"))
COMMANDS_FILE = _get_path("models", "commands_file", str(BASE_DIR / "commands.yaml"), "JARVIS_COMMANDS_FILE")

# === Система: MQTT, логи, heartbeat (секция system) ===
MQTT_HOST = _get("system", "mqtt_host", "localhost", "JARVIS_MQTT_HOST")
MQTT_PORT = _get("system", "mqtt_port", 1883, "JARVIS_MQTT_PORT")
MQTT_KEEPALIVE = _get("system", "mqtt_keepalive", 60, "JARVIS_MQTT_KEEPALIVE")
# Автопереподключение (экспоненциальный backoff, секунды). Потолок мал (15с): локальный
# брокер поднимается быстро, после suspend Джарвис восстанавливается за ~15с, а не ~2 мин.
MQTT_RECONNECT_MIN = _get("system", "mqtt_reconnect_min", 1.0, "JARVIS_MQTT_RECONNECT_MIN")
MQTT_RECONNECT_MAX = _get("system", "mqtt_reconnect_max", 15.0, "JARVIS_MQTT_RECONNECT_MAX")
LOG_MAX_BYTES = _get("system", "log_max_bytes", 2 * 1024 * 1024, "JARVIS_LOG_MAX_BYTES")
LOG_BACKUP_COUNT = _get("system", "log_backup_count", 3, "JARVIS_LOG_BACKUP_COUNT")
LOG_LEVEL = str(_get("system", "log_level", "INFO", "JARVIS_LOG_LEVEL")).strip().upper()
# Раз в столько секунд каждый сервис пишет в лог «жив» (INFO). 0 — выключить.
HEARTBEAT_INTERVAL = _get("system", "heartbeat_interval", 300.0, "JARVIS_HEARTBEAT_INTERVAL")

# === Слух (STT): захват, wake-word, VAD, анти-эхо (секция hearing) ===
SAMPLE_RATE = _get("hearing", "sample_rate", 16000, "JARVIS_SAMPLE_RATE")
CHANNELS = 1  # моно (не настраивается — модели и весь конвейер рассчитаны на 1 канал)
# Варианты wake-word: zipformer-ru искажает «джарвис» → добавляй встреченные искажения.
WAKE_WORDS = _get("hearing", "wake_words",
                  ["джарвис", "джарвиз", "жарвис", "жарвиз", "сервис", "джарвес",
                   "чарвис", "чарвиз", "джарви", "джарвас"],
                  "JARVIS_WAKE_WORDS")
WAKE_WORD_FUZZY_THRESHOLD = _get("hearing", "wake_word_fuzzy_threshold", 0.7,
                                 "JARVIS_WAKE_WORD_FUZZY_THRESHOLD")
# Анти-эхо: «хвост» паузы после речи Джарвиса (с) — НАСТРОЕНО ОПЫТНО, менять осторожно.
SPEAKING_TAIL = _get("hearing", "speaking_tail", 1.0, "JARVIS_SPEAKING_TAIL")
ECHO_CONTENT_WINDOW = _get("hearing", "echo_content_window", 2.0, "JARVIS_ECHO_CONTENT_WINDOW")
ECHO_SIMILARITY_THRESHOLD = _get("hearing", "echo_similarity_threshold", 0.6,
                                 "JARVIS_ECHO_SIMILARITY_THRESHOLD")
# Мин. длина сегмента (семплы) для zipformer: короче → RuntimeError Reshape. 8000 = 0.5с@16к.
MIN_SEGMENT_SAMPLES = _get("hearing", "min_segment_samples", 8000, "JARVIS_MIN_SEGMENT_SAMPLES")
# VAD-пороги — НАСТРОЕНЫ ОПЫТНЫМ ПУТЁМ. Менять осторожно: влияет на распознавание речи.
VAD_THRESHOLD = _get("hearing", "vad_threshold", 0.3, "JARVIS_VAD_THRESHOLD")
VAD_MIN_SILENCE = _get("hearing", "vad_min_silence", 0.8, "JARVIS_VAD_MIN_SILENCE")
VAD_MIN_SPEECH = _get("hearing", "vad_min_speech", 0.25, "JARVIS_VAD_MIN_SPEECH")
VAD_BUFFER_SECONDS = _get("hearing", "vad_buffer_seconds", 10, "JARVIS_VAD_BUFFER_SECONDS")
STT_NUM_THREADS = _get("hearing", "stt_num_threads", 1, "JARVIS_STT_NUM_THREADS")
# Адаптивный порог VAD: в ТИШИНЕ чуть снижаем порог (негромкая команда ловится сразу), при росте
# шума — возврат к закреплённой базе VAD_THRESHOLD. Базовое поведение в норме/шуме НЕ меняется.
VAD_ADAPTIVE = _get("hearing", "vad_adaptive", True, "JARVIS_VAD_ADAPTIVE")
# На сколько опустить порог в тишине (смещение ОТ базы вниз): 0.3 − 0.08 = 0.22.
VAD_QUIET_OFFSET = _get("hearing", "vad_quiet_offset", 0.08, "JARVIS_VAD_QUIET_OFFSET")
# Нижняя граница порога в тишине (ниже не опускаем — страховка от ловли шорохов).
VAD_QUIET_FLOOR = _get("hearing", "vad_quiet_floor", 0.20, "JARVIS_VAD_QUIET_FLOOR")
# Источник захвата: пусто = системный default-микрофон; имя echo-cancel source → шумоподавление.
# ВНИМАНИЕ: на этой машине PortAudio/sounddevice видит только «default» (железный микрофон) — задать
# denoised-источник напрямую НЕ выйдет; echo-cancel настраивается вручную через ALSA (см. CLAUDE.md).
STT_SOURCE = str(_get("hearing", "stt_source", "", "JARVIS_STT_SOURCE")).strip()

# === Push-to-talk: зажатие клавиши → слушать команду БЕЗ wake-word (надёжный путь в шуме) ===
# Читаем /dev/input напрямую (как детектор активности) — нужна группа input (как для ydotool).
PTT_ENABLED = _get("hearing", "push_to_talk_enabled", True, "JARVIS_PTT_ENABLED")
# Клавиша (человекочитаемо): «правый ctrl»/«левый ctrl»/«правый shift»/«правый alt»… или код evdev.
PTT_KEY = str(_get("hearing", "push_to_talk_key", "правый ctrl", "JARVIS_PTT_KEY")).strip().lower()
# Приглушать музыку на время прослушивания по кнопке (переиспользует ducking адаптивной громкости).
DUCK_WHILE_LISTENING = _get("hearing", "duck_while_listening", True, "JARVIS_DUCK_WHILE_LISTENING")
# Имя клавиши → evdev-код (linux/input-event-codes.h). Правые по умолчанию для «ctrl/shift/alt».
_PTT_KEYCODES = {
    "правый ctrl": 97, "правый контрол": 97, "right ctrl": 97, "rightctrl": 97,
    "левый ctrl": 29, "left ctrl": 29, "ctrl": 97,
    "правый shift": 54, "левый shift": 42, "shift": 54,
    "правый alt": 100, "левый alt": 56, "alt": 100,
    "пробел": 57, "space": 57, "scroll lock": 70, "scrolllock": 70,
}
try:
    PTT_KEYCODE = int(PTT_KEY) if PTT_KEY.isdigit() else _PTT_KEYCODES.get(PTT_KEY, 97)
except Exception:
    PTT_KEYCODE = 97  # дефолт — правый Ctrl

# Пути моделей STT (выводятся из models_dir; обычно не трогают — можно задать явно).
VAD_MODEL = _get_path("models", "vad_model", str(MODELS_DIR / "silero_vad.onnx"), "JARVIS_VAD_MODEL")
_ZIPFORMER_DIR = MODELS_DIR / "sherpa-onnx-small-zipformer-ru-2024-09-18"
ZIPFORMER_ENCODER = _get_path("models", "zipformer_encoder", str(_ZIPFORMER_DIR / "encoder.int8.onnx"),
                         "JARVIS_ZIPFORMER_ENCODER")
ZIPFORMER_DECODER = _get_path("models", "zipformer_decoder", str(_ZIPFORMER_DIR / "decoder.int8.onnx"),
                         "JARVIS_ZIPFORMER_DECODER")
ZIPFORMER_JOINER = _get_path("models", "zipformer_joiner", str(_ZIPFORMER_DIR / "joiner.int8.onnx"),
                        "JARVIS_ZIPFORMER_JOINER")
ZIPFORMER_TOKENS = _get_path("models", "zipformer_tokens", str(_ZIPFORMER_DIR / "tokens.txt"),
                        "JARVIS_ZIPFORMER_TOKENS")
ZIPFORMER_BPE = _get_path("models", "zipformer_bpe", str(_ZIPFORMER_DIR / "bpe.model"),
                     "JARVIS_ZIPFORMER_BPE")

# === Распознавание команд: матчер (секция recognition) ===
EMBEDDER_DIR = Path(_get_path("models", "embedder_dir", str(MODELS_DIR / "rubert-tiny2-onnx"),
                         "JARVIS_EMBEDDER_DIR"))
EMBEDDER_MODEL = _get_path("models", "embedder_model", str(EMBEDDER_DIR / "model_optimized.onnx"),
                      "JARVIS_EMBEDDER_MODEL")
EMBEDDER_TOKENIZER = _get_path("models", "embedder_tokenizer", str(EMBEDDER_DIR / "tokenizer.json"),
                          "JARVIS_EMBEDDER_TOKENIZER")
MATCHER_CACHE = _get_path("models", "matcher_cache", str(EMBEDDER_DIR / "cmd_emb_cache.npz"),
                     "JARVIS_MATCHER_CACHE")
# Порог слоя ПРАВИЛ (difflib 0–1): ниже — фраза не считается совпавшей с синонимом.
MATCHER_FUZZY_THRESHOLD = _get("recognition", "fuzzy_threshold", 0.7, "JARVIS_MATCHER_FUZZY_THRESHOLD")
# Порог слоя ЭМБЕДДИНГОВ (косинус 0–1): ниже — поиск не уверен → переспрос.
MATCHER_EMB_THRESHOLD = _get("recognition", "emb_threshold", 0.6, "JARVIS_MATCHER_EMB_THRESHOLD")
# Мин. отрыв лучшего кандидата от второго (косинус): меньше — кандидаты почти равны → переспрос.
MATCHER_EMB_MARGIN = _get("recognition", "emb_margin", 0.04, "JARVIS_MATCHER_EMB_MARGIN")

# === Голос (TTS): Piper + громкость (секция voice) ===
PIPER_MODEL = _get_path("models", "piper_model", str(MODELS_DIR / "ru_RU-dmitri-medium.onnx"),
                   "JARVIS_PIPER_MODEL")
PIPER_CONFIG = _get_path("models", "piper_config", str(MODELS_DIR / "ru_RU-dmitri-medium.onnx.json"),
                    "JARVIS_PIPER_CONFIG")
# Sink PipeWire для воспроизведения (pw-cat --target). Пусто = системный default sink.
TTS_SINK = str(_get("voice", "tts_sink", "", "JARVIS_TTS_SINK")).strip()
# Греть Piper при старте (true) — первая фраза без ~3с (ценой ~106 МБ RAM); false — лениво.
TTS_PRELOAD = _get("voice", "preload", True, "JARVIS_TTS_PRELOAD")
# Задержка узла pw-cat (мс): меньше — быстрее старт звука. 30 мс комфортно на N100.
TTS_LATENCY_MS = _get("voice", "latency_ms", 30, "JARVIS_TTS_LATENCY_MS")
# Базовая громкость голоса (0..1) — ОТПРАВНАЯ ТОЧКА адаптации (поверх: шум/шёпот/ducking).
VOICE_VOLUME_BASE = _get("voice", "base_volume", 0.85, "JARVIS_VOICE_VOLUME_BASE")
VOICE_VOLUME_MIN = _get("voice", "volume_min", 0.35, "JARVIS_VOICE_VOLUME_MIN")
VOICE_VOLUME_MAX = _get("voice", "volume_max", 1.0, "JARVIS_VOICE_VOLUME_MAX")
# Макс программное усиление PCM при сильном шуме (>1.0 = громче нормы; на пиках возможен клиппинг).
VOICE_GAIN_MAX = _get("voice", "gain_max", 1.5, "JARVIS_VOICE_GAIN_MAX")
# Скорость речи (length_scale Piper): >1 медленнее, <1 быстрее. Чуть медленнее = разборчивее.
VOICE_LENGTH_SCALE = _get("voice", "length_scale", 1.12, "JARVIS_VOICE_LENGTH_SCALE")
# Высота тона (1.0 норма, <1 ниже): подмена частоты pw-cat + компенсация темпа (независимо от
# length_scale). Не формант-сохраняющий — разумный диапазон 0.88–1.0 (лёгкое понижение тона).
VOICE_PITCH = _get("voice", "pitch", 0.95, "JARVIS_VOICE_PITCH")

# === Адаптивная громкость: реальные уровни сигнала RMS (секция adaptive_audio) ===
ADAPTIVE_VOLUME = _get("adaptive_audio", "enabled", True, "JARVIS_ADAPTIVE_VOLUME")
# Порог реального уровня воспроизведения ноута (RMS monitor): выше — приглушаем музыку (ducking).
DUCK_THRESHOLD = _get("adaptive_audio", "duck_threshold", 0.02, "JARVIS_DUCK_THRESHOLD")
# До какой доли громкости приглушать музыку (0.35 = 35%, НЕ в ноль — слышна фоном).
DUCK_LEVEL = _get("adaptive_audio", "duck_level", 0.35, "JARVIS_DUCK_LEVEL")
# Связь «колонки→микрофон»: внешний шум = mic_rms − k·monitor_rms (вычесть свой звук из ноута).
NOISE_SUBTRACT_K = _get("adaptive_audio", "noise_subtract_k", 0.7, "JARVIS_NOISE_SUBTRACT_K")
# Порог «тихо вокруг» (RMS внешнего шума): ниже — громкость следует за речью пользователя.
QUIET_THRESHOLD = _get("adaptive_audio", "quiet_threshold", 0.015, "JARVIS_QUIET_THRESHOLD")
# Чувствительность громкости к шуму / к громкости речи пользователя.
NOISE_TO_VOLUME = _get("adaptive_audio", "noise_to_volume", 3.0, "JARVIS_NOISE_TO_VOLUME")
USER_TO_VOLUME = _get("adaptive_audio", "user_to_volume", 4.0, "JARVIS_USER_TO_VOLUME")
# Скорость плавных переходов громкости/ducking (доля приближения к цели за шаг рампы, 0..1).
VOLUME_RAMP = _get("adaptive_audio", "volume_ramp", 0.25, "JARVIS_VOLUME_RAMP")
# Окно/период замера RMS фоновыми замерщиками (с) — короткое, лёгкое по CPU.
NOISE_WINDOW = _get("adaptive_audio", "noise_window", 0.1, "JARVIS_NOISE_WINDOW")
# Шум меряем как ПОЛ (асимметричная EMA): быстро ВНИЗ (следуем за тишиной), очень медленно ВВЕРХ
# (всплеск голоса пользователя/команды НЕ задирает фон → первая фраза не «орёт»). ТЗ-10, фикс скачка.
NOISE_FLOOR_DOWN = _get("adaptive_audio", "noise_floor_down", 0.25, None)  # доля приближения вниз/шаг
NOISE_FLOOR_UP = _get("adaptive_audio", "noise_floor_up", 0.02, None)      # доля приближения вверх/шаг
# Сглаживание ИТОГОВОЙ громкости голоса между фразами (0..1; доля движения к цели). Меньше — плавнее,
# нет рывка громкой первой фразы. 1.0 — без сглаживания (мгновенно).
VOLUME_SMOOTHING = _get("adaptive_audio", "volume_smoothing", 0.5, "JARVIS_VOLUME_SMOOTHING")
# Пак подтверждения голосовой установки громкости (ТЗ-10). {процент} — установленный уровень.
VOLUME_ACK = _get("voice_volume", "ack", [
    "Громкость {процент} процентов, сэр.",
    "Ставлю {процент} процентов, сэр.",
    "Готово, сэр — {процент} процентов.",
], None)

# Словарь произношения (ТЗ-10): слово → замена ПЕРЕД синтезом Piper (целым словом, регистронезависимо).
# СВЕРЕНО: этот голос Piper НЕ учитывает комбинирующий акут ́ — ударение правится только ФОНЕТИЧЕСКИМ
# РЕСПЕЛЛИНГОМ (переписать слово так, как надо читать). Транслитерация латиницы → кириллицей работает
# отлично. Это ПЛОСКАЯ секция (pronunciation: {слово: замена}) — читаем словарь целиком.
_PRON_DEFAULT = {
    "Wi-Fi": "вай-фай", "WiFi": "вай-фай", "wifi": "вай-фай",
    "Bluetooth": "блютус", "Gemini": "джемини", "GitHub": "гитхаб", "Gmail": "джимейл",
    "Thorium": "ториум", "Telegram": "телеграм", "Yandex": "яндекс", "speedtest": "спидтест",
    "MQTT": "эм-ку-ти-ти", "USB": "ю-эс-би", "URL": "ю-эр-эль", "VPN": "вэ-пэ-эн",
}
try:
    _pron = _SETTINGS.get("pronunciation")
    PRONUNCIATION = _pron if isinstance(_pron, dict) and _pron else _PRON_DEFAULT
except Exception:
    PRONUNCIATION = _PRON_DEFAULT

# Тема оформления KDE (ТЗ-10): цветовые схемы для тёмной/светлой (plasma-apply-colorscheme).
THEME_DARK_SCHEME = _get("theme", "dark_scheme", "TUXEDODark", None)
THEME_LIGHT_SCHEME = _get("theme", "light_scheme", "TUXEDOLight", None)
THEME_FAIL = _get("theme", "fail",
                  ["Сэр, не удалось сменить тему.", "Тема не переключилась, сэр."], None)

# === Стартовое объявление: фирменная фраза при `jarvis start` (секция startup) ===
# Дефолтные паки (полные — фича работает и без settings.yaml). Авторитетная копия — в settings.yaml.
STARTUP_ANNOUNCE = _get("startup", "announce", True, "JARVIS_STARTUP_ANNOUNCE")
_DEFAULT_STARTUP_SUCCESS = [
    "Системы в норме, сэр. Рад снова быть в вашем распоряжении.",
    "Запуск завершён успешно, сэр. Готов к работе.",
    "Доброго дня, сэр. Все системы активны, жду ваших команд.",
    "Я в сети, сэр. Всё работает как часы.",
    "Запуск прошёл гладко, сэр. К вашим услугам.",
]
_DEFAULT_STARTUP_PROBLEM = [
    "Сэр, я запустился, но не всё гладко — часть систем требует внимания.",
    "Я в сети, сэр, однако замечу: некоторые компоненты работают не в полную силу.",
    "Запуск завершён с оговорками, сэр. Рекомендую заглянуть в состояние систем.",
]
STARTUP_SUCCESS_PHRASES = _get("startup", "success_phrases", _DEFAULT_STARTUP_SUCCESS, None)
STARTUP_PROBLEM_PHRASES = _get("startup", "problem_phrases", _DEFAULT_STARTUP_PROBLEM, None)

# === Напоминания о перерыве: детектор активности (секция break_reminders) ===
# Дефолтные фразы (полные списки — чтобы фича работала даже без settings.yaml).
# Авторитетная редактируемая копия — в settings.yaml.
_DEFAULT_OFFER_PHRASES = [
    "Сэр, вы за работой уже больше двадцати минут без передышки. Рекомендую короткий "
    "перерыв — глаза и спина будут признательны.",
    "Позволю себе заметить, сэр: вы трудитесь без остановки довольно долго. Пять минут "
    "паузы сейчас сэкономят вам час усталости позже.",
    "Сэр, даже самые надёжные системы нуждаются в охлаждении. Краткий перерыв пошёл бы "
    "вам на пользу.",
    "Прошу прощения, что вмешиваюсь, сэр, но вы давно не отрывались от экрана. Самое "
    "время размяться.",
    "Сэр, ваша продуктивность впечатляет, но даже она выигрывает от короткой паузы. "
    "Предлагаю передохнуть.",
    "Замечу без нотаций о здоровье, сэр: небольшой перерыв сейчас — разумная инвестиция "
    "в следующий час работы.",
    "Сэр, вы работаете уже изрядное время. Если позволите совет — встаньте, пройдитесь, "
    "дайте глазам отдых.",
    "Не сочтите за назойливость, сэр, но человеческий организм не рассчитан на "
    "непрерывную работу за экраном. Перерыв?",
    "Сэр, я бы рекомендовал паузу. Дела никуда не денутся, а свежая голова после отдыха "
    "работает заметно лучше.",
    "Время сделать перерыв, сэр. Обещаю присмотреть за всем, пока вы переведёте дух.",
]
_DEFAULT_PRAISE_PHRASES = [
    "С возвращением, сэр. Разумное решение — отдых ещё никому не вредил.",
    "Превосходно, сэр. Перерыв пойдёт вам только на пользу.",
    "Рад, что вы прислушались, сэр. Теперь, полагаю, дело пойдёт бодрее.",
    "Отдохнули — и правильно сделали, сэр. Я ценю благоразумие.",
    "С возвращением, сэр. Свежая голова — лучший инструмент, и вы только что его наточили.",
    "Хорошее решение, сэр. Даже короткая пауза творит чудеса с концентрацией.",
    "Вот это по-взрослому, сэр. Отдых — не роскошь, а необходимость.",
    "Признаться, я доволен, сэр. Перерыв был кстати.",
    "С возвращением, сэр. Надеюсь, отдых был приятным — продолжим?",
    "Отлично, сэр. Вы позаботились о себе — это всегда верный ход.",
]
_DEFAULT_STOP_REPLIES = [
    "Как скажете, сэр. Напоминать не буду — но за вами всё равно присмотрю.",
    "Понял, сэр. Снимаю вопрос о перерыве.",
    "Воля ваша, сэр. Отсчёт обнулю.",
]
_DEFAULT_STOP_PHRASES = [
    "не сейчас", "не время", "позже", "потом", "я работаю",
    "не мешай", "продолжаю работать", "отложим перерыв", "перерыв не нужен",
]

# Включить фичу напоминаний о перерыве (false — сервис работает «вхолостую», молчит).
BREAKS_ENABLED = _get("break_reminders", "enabled", True, "JARVIS_BREAKS_ENABLED")
# Период тика логики (с). Реже — легче по CPU, грубее реакция. 5с — комфортный баланс.
BREAK_TICK = _get("break_reminders", "tick_seconds", 5.0, "JARVIS_BREAK_TICK")
# Длительность цикла активности (мин) — случайная в диапазоне при каждом цикле. В секунды ниже.
BREAK_CYCLE_MIN = _get("break_reminders", "cycle_min_minutes", 20, "JARVIS_BREAK_CYCLE_MIN") * 60
BREAK_CYCLE_MAX = _get("break_reminders", "cycle_max_minutes", 30, "JARVIS_BREAK_CYCLE_MAX") * 60
# Микропауза (с): простой короче — активность ПРОДОЛЖАЕТ копиться (задумался, потянулся).
BREAK_MICRO_PAUSE = _get("break_reminders", "micro_pause_seconds", 80, "JARVIS_BREAK_MICRO")
# Смена деятельности (с): простой ≥ этого → цикл ПОЛНОСТЬЮ сбрасывается (отошёл). Перекрывает микропаузу.
BREAK_RESET_IDLE = _get("break_reminders", "reset_idle_seconds", 180, "JARVIS_BREAK_RESET")
# Длительность ЗАСЧИТАННОГО перерыва (с) — случайная в диапазоне. После него — похвала + цикл заново.
BREAK_MIN_SECONDS = _get("break_reminders", "break_min_seconds", 240, "JARVIS_BREAK_MIN")
BREAK_MAX_SECONDS = _get("break_reminders", "break_max_seconds", 300, "JARVIS_BREAK_MAX")
# Задержка повторного напоминания при игноре (с) — случайная. Считается по АКТИВНОМУ времени.
BREAK_REMIND_MIN = _get("break_reminders", "remind_after_min_seconds", 300, "JARVIS_BREAK_REMIND_MIN")
BREAK_REMIND_MAX = _get("break_reminders", "remind_after_max_seconds", 360, "JARVIS_BREAK_REMIND_MAX")
# На сколько процентов затемнять экран при ре-напоминании (40–50 по ТЗ; 45 — середина).
BREAK_DIM_PERCENT = _get("break_reminders", "dim_percent", 45, "JARVIS_BREAK_DIM")
# Нижний предел яркости при затемнении (% от максимума) — экран не в ноль.
BREAK_DIM_FLOOR_PERCENT = _get("break_reminders", "dim_floor_percent", 10, "JARVIS_BREAK_DIM_FLOOR")
# Плавность затемнения: число шагов рампы и её длительность (с).
BREAK_DIM_STEPS = _get("break_reminders", "dim_ramp_steps", 8, "JARVIS_BREAK_DIM_STEPS")
BREAK_DIM_RAMP_SECONDS = _get("break_reminders", "dim_ramp_seconds", 2.0, "JARVIS_BREAK_DIM_DUR")
# Период переэнумерации устройств ввода /dev/input (с) — для hotplug.
BREAK_DEVICE_RESCAN = _get("break_reminders", "device_rescan_seconds", 60, "JARVIS_BREAK_RESCAN")
# Порог распознавания стоп-фразы (difflib 0..1, полное совпадение). ВЫСОКИЙ — чтобы не
# проглатывать похожие обычные команды.
BREAK_STOP_THRESHOLD = _get("break_reminders", "stop_phrase_threshold", 0.85, "JARVIS_STOP_THRESHOLD")
# Минимум отработанного активного времени (мин) для похвалы — защита от похвалы за «перерыв»
# сразу после логина. 0 = хвалить за любой засчитанный перерыв (поведение ТЗ).
BREAK_PRAISE_MIN_WORK = _get("break_reminders", "praise_min_work_minutes", 0, "JARVIS_BREAK_PRAISE_MIN") * 60
# Списки фраз (env-оверрайд бессмыслен из-за запятых внутри — только settings.yaml; env=None).
BREAK_OFFER_PHRASES = _get("break_reminders", "offer_phrases", _DEFAULT_OFFER_PHRASES, None)
BREAK_PRAISE_PHRASES = _get("break_reminders", "praise_phrases", _DEFAULT_PRAISE_PHRASES, None)
BREAK_STOP_REPLIES = _get("break_reminders", "stop_replies", _DEFAULT_STOP_REPLIES, None)
BREAK_STOP_PHRASES = _get("break_reminders", "stop_phrases", _DEFAULT_STOP_PHRASES, None)

# === Геолокация (секция «местоположение») — для погоды и будущего мирового времени ===
# Регион кириллицей (город[, страна]); геокодинг Open-Meteo берёт часть до запятой.
REGION = _get("местоположение", "регион", "Москва, Россия", "JARVIS_REGION")

# === Будильники (секции alarms + путь в models) ===
# Файл расписания — через _get_path (абсолютный для systemd, см. грабли Этапа 7). Человекочитаемый
# YAML в корне проекта; правится и голосом, и вручную.
SCHEDULE_FILE = _get_path("models", "schedule_file", str(BASE_DIR / "schedule.yaml"),
                          "JARVIS_SCHEDULE_FILE")
# Включить будильники (false — сервис scheduler работает «вхолостую», молчит).
ALARMS_ENABLED = _get("alarms", "enabled", True, "JARVIS_ALARMS_ENABLED")
# Период тика планировщика (с). 15с — минутной точности достаточно, между тиками ~0% CPU.
ALARM_TICK = _get("alarms", "tick_seconds", 15.0, "JARVIS_ALARM_TICK")
# Окно «опоздавшего» срабатывания (мин): пропущенное время (сон ноутбука) не дольше — сработать
# с опозданием; дольше — пропустить до следующего раза (не будить среди дня за вчерашнее).
ALARM_GRACE_MINUTES = _get("alarms", "grace_minutes", 10, "JARVIS_ALARM_GRACE")
# Утренний по умолчанию ежедневный (true) — срабатывает каждый день, пока не отменю. false — разовый.
ALARM_MORNING_DAILY = _get("alarms", "morning_daily", True, "JARVIS_ALARM_MORNING_DAILY")
# Громкость пробуждения (0..1): будильник звучит НЕ тише этого — обходит «тихо вокруг → тихо».
ALARM_WAKE_VOLUME = _get("alarms", "wake_volume", 0.95, "JARVIS_ALARM_WAKE_VOLUME")
# Нарастающая громкость утреннего пробуждения: короткие реплики тихо→громко перед богатой фразой.
ALARM_WAKE_RISING = _get("alarms", "wake_rising", True, "JARVIS_ALARM_WAKE_RISING")
# Стартовая громкость нарастания (0..1) и число ступеней до wake_volume.
ALARM_WAKE_RISING_START = _get("alarms", "wake_rising_start", 0.4, "JARVIS_ALARM_WAKE_START")
ALARM_WAKE_RISING_STEPS = _get("alarms", "wake_rising_steps", 3, "JARVIS_ALARM_WAKE_STEPS")
# Погода в утренней фразе из Open-Meteo (true) или фраза без погоды (false). Без сети — без погоды.
ALARM_WEATHER_ENABLED = _get("alarms", "weather_enabled", True, "JARVIS_ALARM_WEATHER")
# Таймаут сетевых запросов погоды/геокодинга (с) — без сети/при сбое не виснем, будим без погоды.
ALARM_WEATHER_TIMEOUT = _get("alarms", "weather_timeout", 5.0, "JARVIS_ALARM_WEATHER_TIMEOUT")

# --- Паки фраз будильника (плейсхолдеры: {время} {темп_макс} {темп_мин} {погода} {метка}) ---
# Дефолты полные (фича работает без settings.yaml). Авторитетная редактируемая копия — в settings.yaml.
_DEF_MORNING_FIRE_WEATHER = [
    "Доброе утро, сэр. Сейчас {время}. За окном {погода}, днём до {темп_макс}, "
    "ночью около {темп_мин}. Прекрасное начало дня.",
    "С добрым утром, сэр. Время — {время}. Погода сегодня: {погода}, "
    "максимум {темп_макс}, минимум {темп_мин}.",
    "Доброе утро, сэр. {время}. По прогнозу {погода}, от {темп_мин} до {темп_макс}. "
    "Пора начинать день.",
    "Подъём, сэр. Сейчас {время}. На улице {погода}, ожидается до {темп_макс}. "
    "Желаю продуктивного дня.",
]
_DEF_MORNING_FIRE_PLAIN = [
    "Доброе утро, сэр. Сейчас {время}. Пора просыпаться.",
    "С добрым утром, сэр. Время — {время}. Новый день ждёт вас.",
    "Подъём, сэр. Сейчас {время}. Желаю бодрого утра.",
]
_DEF_WAKE_PRELUDE = ["Сэр.", "Сэр, пора вставать.", "Доброе утро, сэр."]
_DEF_MORNING_SET = [
    "Утренний будильник установлен на {время}, сэр.",
    "Поставил утренний будильник на {время}, сэр. Разбужу вовремя.",
    "Готово, сэр. Утренний будильник на {время}.",
]
_DEF_MORNING_MOVE = [
    "Перенёс утренний будильник на {время}, сэр.",
    "Утренний будильник теперь на {время}, сэр.",
    "Готово, сэр. Утренний переставлен на {время}.",
]
_DEF_MORNING_ALREADY = [
    "Утренний будильник уже стоит на {время}, сэр.",
    "Он уже заведён на {время}, сэр — ничего не меняю.",
    "Сэр, утренний уже на {время}. Всё в силе.",
]
_DEF_MORNING_ALREADY_MOVE = [
    "Утренний и так на {время}, сэр — переносить некуда.",
    "Сэр, он уже на нужном времени, {время}.",
    "Время и так {время}, сэр. Оставляю как есть.",
]
_DEF_MORNING_CANCEL = [
    "Утренний будильник отменён, сэр.",
    "Снял утренний будильник, сэр.",
    "Готово, сэр. Утреннего больше нет.",
]
_DEF_MORNING_NONE = [
    "Сэр, утренний будильник не задан.",
    "У вас нет утреннего будильника, сэр.",
    "Отменять нечего, сэр — утренний не установлен.",
]
_DEF_REGULAR_SET = [
    "Будильник на {время} установлен, сэр.",
    "Поставил будильник на {время}, сэр.",
    "Готово, сэр. Будильник на {время}.",
]
_DEF_REGULAR_SET_LABEL = [
    "Будильник на {время} с пометкой «{метка}» установлен, сэр.",
    "Поставил будильник «{метка}» на {время}, сэр.",
    "Готово, сэр. «{метка}» — на {время}.",
]
_DEF_REGULAR_ALREADY = [
    "Такой будильник уже стоит на {время}, сэр.",
    "Сэр, будильник на {время} уже заведён.",
    "Он уже на {время}, сэр — дубль не создаю.",
]
_DEF_REGULAR_MOVE = [
    "Перенёс будильник на {время}, сэр.",
    "Будильник теперь на {время}, сэр.",
    "Готово, сэр. Время изменено на {время}.",
]
_DEF_REGULAR_ALREADY_MOVE = [
    "Будильник и так на {время}, сэр.",
    "Сэр, он уже на нужном времени, {время}.",
    "Менять нечего — уже {время}, сэр.",
]
_DEF_REGULAR_CANCEL_LABEL = [
    "Будильник «{метка}» удалён, сэр.",
    "Снял будильник «{метка}», сэр.",
    "Готово, сэр. «{метка}» больше не сработает.",
]
_DEF_REGULAR_CANCEL = [
    "Будильник удалён, сэр.",
    "Снял будильник, сэр.",
    "Готово, сэр. Будильник убран.",
]
_DEF_REGULAR_DELETE_ALL = [
    "Все обычные будильники удалены, сэр. Утренний не тронут.",
    "Снял все будильники, сэр. Утренний оставил.",
    "Готово, сэр. Обычных будильников больше нет.",
]
_DEF_REGULAR_NONE_FOUND = [
    "Сэр, не нашёл такого будильника.",
    "Будильника с такой пометкой нет, сэр.",
    "Сэр, у вас нет подходящего будильника для этого.",
]
_DEF_SUGGEST_LABEL = [
    "Сэр, у вас теперь несколько будильников. Советую дать им пометки — так я смогу "
    "различать их по названию.",
    "Будильник поставлен, сэр. Рекомендую добавить пометку, например «с пометкой ужин» — "
    "иначе их легко перепутать.",
    "Готово, сэр. Чтобы потом менять нужный, дайте будильникам пометки.",
]
_DEF_REGULAR_FIRE = [
    "Сэр, будильник. Сейчас {время}.",
    "Внимание, сэр — сработал будильник. Время {время}.",
    "Сэр, напоминаю о будильнике. {время}.",
]
_DEF_REGULAR_FIRE_LABEL = [
    "Сэр, напоминаю — {метка}. Сейчас {время}.",
    "Будильник «{метка}», сэр. Время {время}.",
    "Сэр, пора — {метка}. {время}.",
]
_DEF_NEED_TIME = [
    "Сэр, на какое время поставить будильник?",
    "Уточните время, сэр — я не разобрал.",
    "Простите, сэр, не расслышал время. Повторите?",
]

def _alarm_pack(key, default):
    return _get("alarms", key, default, None)

ALARM_MORNING_FIRE_WEATHER = _alarm_pack("morning_fire_weather", _DEF_MORNING_FIRE_WEATHER)
ALARM_MORNING_FIRE_PLAIN = _alarm_pack("morning_fire_plain", _DEF_MORNING_FIRE_PLAIN)
ALARM_WAKE_PRELUDE = _alarm_pack("wake_prelude_phrases", _DEF_WAKE_PRELUDE)
ALARM_MORNING_SET = _alarm_pack("morning_set", _DEF_MORNING_SET)
ALARM_MORNING_MOVE = _alarm_pack("morning_move", _DEF_MORNING_MOVE)
ALARM_MORNING_ALREADY = _alarm_pack("morning_already", _DEF_MORNING_ALREADY)
ALARM_MORNING_ALREADY_MOVE = _alarm_pack("morning_already_move", _DEF_MORNING_ALREADY_MOVE)
ALARM_MORNING_CANCEL = _alarm_pack("morning_cancel", _DEF_MORNING_CANCEL)
ALARM_MORNING_NONE = _alarm_pack("morning_none", _DEF_MORNING_NONE)
ALARM_REGULAR_SET = _alarm_pack("regular_set", _DEF_REGULAR_SET)
ALARM_REGULAR_SET_LABEL = _alarm_pack("regular_set_label", _DEF_REGULAR_SET_LABEL)
ALARM_REGULAR_ALREADY = _alarm_pack("regular_already", _DEF_REGULAR_ALREADY)
ALARM_REGULAR_MOVE = _alarm_pack("regular_move", _DEF_REGULAR_MOVE)
ALARM_REGULAR_ALREADY_MOVE = _alarm_pack("regular_already_move", _DEF_REGULAR_ALREADY_MOVE)
ALARM_REGULAR_CANCEL_LABEL = _alarm_pack("regular_cancel_label", _DEF_REGULAR_CANCEL_LABEL)
ALARM_REGULAR_CANCEL = _alarm_pack("regular_cancel", _DEF_REGULAR_CANCEL)
ALARM_REGULAR_DELETE_ALL = _alarm_pack("regular_delete_all", _DEF_REGULAR_DELETE_ALL)
ALARM_REGULAR_NONE_FOUND = _alarm_pack("regular_none_found", _DEF_REGULAR_NONE_FOUND)
ALARM_SUGGEST_LABEL = _alarm_pack("suggest_label", _DEF_SUGGEST_LABEL)
ALARM_REGULAR_FIRE = _alarm_pack("regular_fire", _DEF_REGULAR_FIRE)
ALARM_REGULAR_FIRE_LABEL = _alarm_pack("regular_fire_label", _DEF_REGULAR_FIRE_LABEL)
ALARM_NEED_TIME = _alarm_pack("need_time", _DEF_NEED_TIME)

# === Таймеры и секундомеры (секция timers) ===
# Громкость срабатывания таймера (0..1) — заметная (обходит «тихо→тихо», как будильник).
TIMER_VOLUME = _get("timers", "timer_volume", 0.9, "JARVIS_TIMER_VOLUME")
# Чайм (короткий сигнал) перед фразой срабатывания таймера. false — только голос.
TIMER_CHIME = _get("timers", "timer_chime", True, "JARVIS_TIMER_CHIME")

def _timers_pack(key, default):
    return _get("timers", key, default, None)

# --- Паки таймера ({длительность}/{остаток}/{метка}) ---
TIMER_SET = _timers_pack("timer_set", [
    "Таймер на {длительность} установлен, сэр.",
    "Поставил таймер на {длительность}, сэр.",
    "Готово, сэр. Таймер на {длительность}.",
])
TIMER_SET_LABEL = _timers_pack("timer_set_label", [
    "Таймер на {длительность} с пометкой «{метка}» установлен, сэр.",
    "Поставил таймер «{метка}» на {длительность}, сэр.",
    "Готово, сэр. «{метка}» — {длительность}.",
])
TIMER_MOVE = _timers_pack("timer_move", [
    "Изменил таймер на {длительность}, сэр.",
    "Таймер теперь на {длительность}, сэр.",
    "Готово, сэр. Таймер перезаведён на {длительность}.",
])
TIMER_MOVE_LABEL = _timers_pack("timer_move_label", [
    "Таймер «{метка}» теперь на {длительность}, сэр.",
    "Изменил «{метка}» на {длительность}, сэр.",
    "Готово, сэр. «{метка}» — теперь {длительность}.",
])
TIMER_CANCEL = _timers_pack("timer_cancel", [
    "Таймер отменён, сэр.", "Снял таймер, сэр.", "Готово, сэр. Таймер убран.",
])
TIMER_CANCEL_LABEL = _timers_pack("timer_cancel_label", [
    "Таймер «{метка}» отменён, сэр.",
    "Снял таймер «{метка}», сэр.",
    "Готово, сэр. «{метка}» больше не сработает.",
])
TIMER_DELETE_ALL = _timers_pack("timer_delete_all", [
    "Все таймеры отменены, сэр.", "Снял все таймеры, сэр.", "Готово, сэр. Таймеров больше нет.",
])
TIMER_REMAINING = _timers_pack("timer_remaining", [
    "На таймере осталось {остаток}, сэр.", "Осталось {остаток}, сэр.", "Ещё {остаток}, сэр.",
])
TIMER_REMAINING_LABEL = _timers_pack("timer_remaining_label", [
    "На таймере «{метка}» осталось {остаток}, сэр.",
    "До «{метка}» осталось {остаток}, сэр.",
    "«{метка}»: ещё {остаток}, сэр.",
])
TIMER_NONE_FOUND = _timers_pack("timer_none_found", [
    "Сэр, не нашёл такого таймера.",
    "Таймера с такой пометкой нет, сэр.",
    "Сэр, у вас нет подходящего таймера.",
])
TIMER_FIRE = _timers_pack("timer_fire", [
    "Сэр, таймер вышел.", "Время вышло, сэр.", "Сэр, ваш таймер сработал.",
])
TIMER_FIRE_LABEL = _timers_pack("timer_fire_label", [
    "Сэр, таймер «{метка}» вышел.",
    "«{метка}» — время вышло, сэр.",
    "Сэр, напоминаю: таймер «{метка}» сработал.",
])
TIMER_SUGGEST_LABEL = _timers_pack("timer_suggest_label", [
    "Сэр, у вас теперь несколько таймеров. Советую дать им пометки, чтобы не перепутать.",
    "Таймер поставлен, сэр. Рекомендую пометку, например «с пометкой чай».",
    "Готово, сэр. Чтобы потом менять нужный — дайте таймерам пометки.",
])
TIMER_NEED_DURATION = _timers_pack("timer_need_duration", [
    "Сэр, на какую длительность поставить таймер?",
    "Уточните длительность, сэр — я не разобрал.",
    "Простите, сэр, не расслышал, на сколько. Повторите?",
])

# --- Паки секундомера ({прошло}/{метка}) ---
SW_START = _timers_pack("stopwatch_start", [
    "Засёк время, сэр.", "Секундомер пошёл, сэр.", "Время пошло, сэр.",
])
SW_START_LABEL = _timers_pack("stopwatch_start_label", [
    "Засёк время с пометкой «{метка}», сэр.",
    "Секундомер «{метка}» пошёл, сэр.",
    "Готово, сэр. Отсчёт «{метка}» начат.",
])
SW_ELAPSED = _timers_pack("stopwatch_elapsed", [
    "Прошло {прошло}, сэр.", "С момента старта {прошло}, сэр.", "На секундомере {прошло}, сэр.",
])
SW_ELAPSED_LABEL = _timers_pack("stopwatch_elapsed_label", [
    "На секундомере «{метка}» прошло {прошло}, сэр.",
    "«{метка}»: прошло {прошло}, сэр.",
    "С момента «{метка}» прошло {прошло}, сэр.",
])
SW_STOP = _timers_pack("stopwatch_stop", [
    "Секундомер остановлен, прошло {прошло}, сэр.",
    "Остановил, сэр. Итог — {прошло}.",
    "Готово, сэр. Зафиксировано {прошло}.",
])
SW_STOP_LABEL = _timers_pack("stopwatch_stop_label", [
    "Секундомер «{метка}» остановлен, прошло {прошло}, сэр.",
    "«{метка}» остановлен, сэр. Итог — {прошло}.",
    "Готово, сэр. «{метка}»: {прошло}.",
])
SW_RESET = _timers_pack("stopwatch_reset", [
    "Секундомер сброшен, сэр.", "Обнулил секундомер, сэр.", "Готово, сэр. Секундомер сброшен.",
])
SW_RESET_LABEL = _timers_pack("stopwatch_reset_label", [
    "Секундомер «{метка}» сброшен, сэр.",
    "Обнулил «{метка}», сэр.",
    "Готово, сэр. «{метка}» сброшен.",
])
SW_DELETE_ALL = _timers_pack("stopwatch_delete_all", [
    "Все секундомеры сброшены, сэр.",
    "Снял все секундомеры, сэр.",
    "Готово, сэр. Секундомеров больше нет.",
])
SW_NONE_FOUND = _timers_pack("stopwatch_none_found", [
    "Сэр, не нашёл такого секундомера.",
    "Секундомера с такой пометкой нет, сэр.",
    "Сэр, сейчас ни один секундомер не запущен.",
])
SW_SUGGEST_LABEL = _timers_pack("stopwatch_suggest_label", [
    "Сэр, у вас теперь несколько секундомеров. Советую дать им пометки, чтобы различать.",
    "Засёк, сэр. Рекомендую пометку, например «с пометкой работа».",
    "Готово, сэр. Чтобы потом спрашивать нужный — дайте секундомерам пометки.",
])

# === Мировое время (секция worldtime) ===
def _wt_pack(key, default):
    return _get("worldtime", key, default, None)

WORLDTIME_ANSWER = _wt_pack("answer", [
    "Сэр, в городе {город} сейчас {время}. {разница}",
    "В городе {город} время {время}, сэр. {разница}",
    "{город}: сейчас {время}, сэр. {разница}",
])
WORLDTIME_NOT_FOUND = _wt_pack("not_found", [
    "Сэр, не нашёл город {город}.",
    "Затрудняюсь определить, где это, сэр.",
    "Не удалось распознать город, сэр.",
])

# === Монетка (секция coin) ===
def _coin_pack(key, default):
    return _get("coin", key, default, None)

COIN_HEADS = _coin_pack("heads", [
    "Орёл, сэр.", "Выпал орёл, сэр.", "Орёл. Удача на вашей стороне, сэр.",
    "Решка не выпала — орёл, сэр.",
])
COIN_TAILS = _coin_pack("tails", [
    "Решка, сэр.", "Выпала решка, сэр.", "Решка. Что ж, бывает, сэр.",
    "Орёл не выпал — решка, сэр.",
])

# === Напоминания и задачи (секция reminders) ===
# Дата/время без точного времени СЕГОДНЯ → напомнить через случайные REMINDER_RANDOM_MIN..MAX.
REMINDER_RANDOM_MIN = _get("reminders", "random_min_minutes", 90, "JARVIS_REMINDER_RND_MIN") * 60
REMINDER_RANDOM_MAX = _get("reminders", "random_max_minutes", 180, "JARVIS_REMINDER_RND_MAX") * 60
# Будущая дата без времени → случайное время в дневном окне [start; end] (часы).
REMINDER_DAY_START = _get("reminders", "day_window_start_hour", 10, "JARVIS_REMINDER_DAY_START")
REMINDER_DAY_END = _get("reminders", "day_window_end_hour", 18, "JARVIS_REMINDER_DAY_END")
# Громкость срабатывания напоминания (0..1) — заметная (обходит «тихо→тихо»).
REMINDER_VOLUME = _get("reminders", "reminder_volume", 0.9, "JARVIS_REMINDER_VOLUME")
# Чайм перед напоминанием (как у таймера). false — только голос.
REMINDER_CHIME = _get("reminders", "reminder_chime", True, "JARVIS_REMINDER_CHIME")
# Таймаут диалога дозапроса (с): сколько core молчит, ожидая ответа на «о чём/когда».
REMINDER_DIALOG_TIMEOUT = _get("reminders", "dialog_timeout_seconds", 45, "JARVIS_REMINDER_DIALOG")

def _rem_pack(key, default):
    return _get("reminders", key, default, None)

REMINDER_SET = _rem_pack("set", [
    "Напомню {текст} {когда}, сэр.",
    "Хорошо, сэр, напомню {текст} {когда}.",
    "Принято, сэр: {текст} — {когда}.",
])
REMINDER_SET_RANDOM = _rem_pack("set_random", [
    "Напомню {текст} в ближайшие пару часов, сэр.",
    "Хорошо, сэр, скоро напомню: {текст}.",
    "Принято, сэр. Напомню {текст} в течение пары часов.",
])
REMINDER_FIRE = _rem_pack("fire", [
    "Сэр, напоминаю: {текст}.",
    "Напоминание, сэр: {текст}.",
    "Сэр, пора — {текст}.",
])
REMINDER_FIRE_RANDOM = _rem_pack("fire_random", [
    "Сэр, вы просили напомнить — {текст}.",
    "Напоминаю, как просили, сэр: {текст}.",
    "Сэр, та самая напоминалка — {текст}.",
])
REMINDER_DIALOG_WHAT = _rem_pack("dialog_what", [
    "О чём напомнить, сэр?",
    "Что напомнить, сэр?",
    "Хорошо, сэр. О чём вам напомнить?",
])
REMINDER_DIALOG_WHEN = _rem_pack("dialog_when", [
    "На когда поставить напоминание, сэр?",
    "Когда напомнить, сэр?",
    "А на какое время, сэр?",
])
REMINDER_DIALOG_CANT_WHEN = _rem_pack("dialog_cant_when", [
    "Не разобрал когда, сэр. Скажите дату или время — например «завтра в десять».",
    "Простите, сэр, когда именно? Например «сегодня в шесть» или «в пятницу».",
])
REMINDER_DIALOG_CANCEL = _rem_pack("dialog_cancel", [
    "Хорошо, сэр, отменил.",
    "Как скажете, сэр, не буду.",
    "Принято, сэр. Забыли.",
])
REMINDER_MOVE = _rem_pack("move", [
    "Перенёс напоминание {текст} на {когда}, сэр.",
    "Готово, сэр: {текст} теперь {когда}.",
    "Напоминание {текст} переставлено на {когда}, сэр.",
])
REMINDER_CANCEL = _rem_pack("cancel", [
    "Удалил напоминание {текст}, сэр.",
    "Снял напоминание про {текст}, сэр.",
    "Готово, сэр. {текст} — больше не напомню.",
])
REMINDER_DELETE_ALL = _rem_pack("delete_all", [
    "Все напоминания удалены, сэр.",
    "Снял все напоминания, сэр.",
    "Готово, сэр. Напоминаний больше нет.",
])
REMINDER_NONE_FOUND = _rem_pack("none_found", [
    "Сэр, не нашёл такого напоминания.",
    "Напоминания с таким описанием нет, сэр.",
    "Сэр, у вас нет подходящего напоминания.",
])
REMINDER_NEED_TEXT = _rem_pack("need_text", [
    "О чём напомнить, сэр?",
    "Что записать в напоминание, сэр?",
])
REMINDER_LIST = _rem_pack("list", [
    "Сэр, ваши напоминания: {список}.",
    "На повестке, сэр: {список}.",
    "Напоминания, сэр: {список}.",
])
REMINDER_LIST_EMPTY = _rem_pack("list_empty", [
    "Активных напоминаний нет, сэр.",
    "Сэр, напоминаний у вас нет.",
])
TODAY_LIST = _rem_pack("today_list", [
    "На сегодня, сэр: {список}.",
    "Сегодня у вас, сэр: {список}.",
])
TODAY_EMPTY = _rem_pack("today_empty", [
    "На сегодня ничего не запланировано, сэр.",
    "Сэр, на сегодня всё чисто.",
])

# --- Задачи ---
TASK_ADD = _rem_pack("task_add", [
    "Добавил задачу: {текст}, сэр.",
    "Записал, сэр: {текст}.",
    "Готово, сэр. Задача «{текст}» в списке.",
])
TASK_LIST = _rem_pack("task_list", [
    "Ваши задачи, сэр: {список}.",
    "В списке дел, сэр: {список}.",
    "Задачи, сэр: {список}.",
])
TASK_LIST_EMPTY = _rem_pack("task_list_empty", [
    "Список задач пуст, сэр.",
    "Сэр, задач нет.",
])
TASK_DONE = _rem_pack("task_done", [
    "Отметил «{текст}» выполненной, сэр.",
    "Готово, сэр. «{текст}» вычеркнул.",
    "Превосходно, сэр. «{текст}» сделано.",
])
TASK_DELETE = _rem_pack("task_delete", [
    "Удалил задачу «{текст}», сэр.",
    "Снял задачу «{текст}», сэр.",
    "Готово, сэр. «{текст}» убрал.",
])
TASK_DELETE_ALL = _rem_pack("task_delete_all", [
    "Все задачи удалены, сэр.",
    "Очистил список задач, сэр.",
    "Готово, сэр. Задач больше нет.",
])
TASK_NONE_FOUND = _rem_pack("task_none_found", [
    "Сэр, не нашёл такой задачи.",
    "Задачи с таким описанием нет, сэр.",
    "Сэр, у вас нет подходящей задачи.",
])
TASK_NEED_TEXT = _rem_pack("task_need_text", [
    "Какую задачу добавить, сэр?",
    "Что записать в задачи, сэр?",
])

# === Цепочки команд: продолжения без wake-word, комбо, повтор/отмена (секция chains) ===
# Принимать продолжения активной ветки БЕЗ обращения «Джарвис» (true). false — только с wake-word.
CONTINUATIONS_ENABLED = _get("chains", "continuations_enabled", True, "JARVIS_CONTINUATIONS")

def _chains_pack(key, default):
    return _get("chains", key, default, None)

# Комбо: часть фразы не распознана (понятое выполнено, об остальном — честно).
COMBO_PARTIAL = _chains_pack("combo_partial", [
    "Остальное не разобрал, сэр.",
    "Часть команды я не понял, сэр.",
    "Сэр, остальное выполнить не смог — не расслышал.",
])
# «Повтори последнее».
REPEAT_DONE = _chains_pack("repeat_done", [
    "Повторяю, сэр.", "Ещё раз, сэр.", "Снова, сэр.",
])
REPEAT_NOTHING = _chains_pack("repeat_nothing", [
    "Сэр, мне нечего повторить.", "Пока нечего повторять, сэр.",
])
# «Отмени» — обратимое / нечего / необратимое.
UNDO_DONE = _chains_pack("undo_done", [
    "Отменяю, сэр.", "Возвращаю как было, сэр.", "Откатываю, сэр.",
])
UNDO_NOTHING = _chains_pack("undo_nothing", [
    "Сэр, отменять нечего.", "Пока нечего отменять, сэр.",
])
UNDO_IRREVERSIBLE = _chains_pack("undo_irreversible", [
    "Сэр, это действие отменить нельзя.",
    "Боюсь, откатить такое не выйдет, сэр.",
    "Увы, сэр, это необратимо.",
])

# === Уведомления (D-Bus org.freedesktop.Notifications через gdbus) + режим тишины (ТЗ-6) ===
NOTIFICATIONS_ENABLED = _get("notifications", "enabled", True, "JARVIS_NOTIFICATIONS")
NOTIFY_LOGS_BUTTON = _get("notifications", "logs_button", "Открыть логи", None)
NOTIFY_FAILURE_TITLE = _get("notifications", "failure_title", "Джарвис — неполадка", None)
NOTIFY_SPEECH_TITLE = _get("notifications", "speech_title", "Джарвис", None)
# Кэш проверки звука перед фразой (с): не дёргать wpctl на каждую фразу в очереди.
AUDIO_CHECK_TTL = _get("notifications", "audio_check_ttl", 1.5, None)

def _notif_pack(key, default):
    return _get("notifications", key, default, None)

# Пометка-приставка перед фразой, когда громкость вывода НА НУЛЕ (не поломка — пользователь убавил).
AUDIO_ZERO_PREFIX = _notif_pack("audio_zero", [
    "Сэр, звук на динамиках на нуле — дублирую в уведомления.",
    "Громкость на нуле, сэр, отвечаю текстом.",
    "Динамики приглушены до нуля, сэр — вот текстом.",
])
# Пометка-приставка перед фразой при ТЕХНИЧЕСКОМ сбое звука (устройство/PipeWire/поток). К ней
# добавляется конкретная причина. Иная по смыслу, чем «ноль».
AUDIO_FAIL_PREFIX = _notif_pack("audio_fail", [
    "Сэр, со звуком неполадка — дублирую в уведомления.",
    "Не удаётся вывести голос, сэр — вот текстом.",
    "Звук сейчас недоступен, сэр, дублирую текстом.",
])

# === Режим тишины ===
SILENCE_ENABLED = _get("silence", "enabled", True, "JARVIS_SILENCE_ENABLED")
SILENCE_ON_PHRASES = _get("silence", "on_phrases",
    ["без звука", "режим тишины", "тихий режим", "беззвучный режим", "помолчи", "замолчи",
     "не говори", "молчи"], None)
SILENCE_OFF_PHRASES = _get("silence", "off_phrases",
    ["включи звук", "можешь говорить", "выйди из режима тишины", "отмени тишину", "говори",
     "верни голос", "громкий режим"], None)

def _sil_pack(key, default):
    return _get("silence", key, default, None)

# Ответ на «без звука» (уходит в УВЕДОМЛЕНИЕ — Джарвис уже в тишине).
SILENCE_ON_ACK = _sil_pack("on_ack", [
    "Перехожу в режим тишины, сэр.",
    "Молчу, сэр — отвечаю текстом.",
    "Хорошо, сэр, дальше без голоса.",
])
# Ответ на «включи звук» (теперь уже ГОЛОСОМ).
SILENCE_OFF_ACK = _sil_pack("off_ack", [
    "Снова на связи, сэр.",
    "Голос вернулся, сэр.",
    "Рад снова говорить, сэр.",
])

# === Системное (ТЗ-7): перезагрузка / рабочие среды / live-панель ===

# --- Голосовая перезагрузка ВСЕХ сервисов Джарвиса (НЕ ребут ноута), только с wake ---
# Фразы-триггеры (рефлексивные «-сь» безопасны: не ловят «перезагрузи браузер»).
RESTART_PHRASES = _get("restart", "phrases",
    ["перезагрузись", "перезагрузи себя", "перезапустись", "перезапусти себя",
     "ребутнись", "рестарт", "перезагрузка джарвиса", "перезагрузись джарвис"], None)
# Начальная пауза перед рестартом (с) — дать анонсу доиграть, пока TTS ещё жив.
RESTART_INITIAL_DELAY = _get("restart", "initial_delay_seconds", 2.5, None)

def _restart_pack(key, default):
    return _get("restart", key, default, None)

RESTART_ANNOUNCE = _restart_pack("announce", [
    "Перезагружаюсь, сэр. Одну минуту.",
    "Перезапускаю себя, сэр.",
    "Минутку, сэр, перезагружаю системы.",
])
RESTART_SUCCESS = _restart_pack("success", [
    "С возвращением, сэр. Все системы в строю.",
    "Готов к работе, сэр. Перезагрузка завершена.",
    "Снова на связи, сэр. Всё в порядке.",
])
RESTART_PROBLEM = _restart_pack("problem", [
    "Сэр, перезагрузился, но часть систем не поднялась.",
    "Готов, сэр, однако есть неполадки с сервисами.",
    "Сэр, перезагрузка прошла с проблемами — проверьте логи.",
])

# --- Рабочие среды (виртуальные столы KDE) ---
# Именованные среды: {имя: {desktop: "Название стола", apps: [теги_команд]}}. Вызов «открой <имя> среду».
ENVIRONMENTS = _get("environments", "named", {
    "рабочая": {"desktop": "Работа", "apps": ["site_gemini", "music_yandex"]},
}, None)
# Слова-триггеры команды среды.
ENV_TRIGGERS = _get("environments", "triggers",
    ["сред", "новое пространство", "новое окружение"], None)
ENV_DESKTOP_PREFIX = _get("environments", "desktop_name", "Среда", None)
# Пауза между запусками приложений среды (с) — чтобы окна открылись на новом столе.
ENV_LAUNCH_DELAY = _get("environments", "launch_delay_seconds", 0.8, None)

def _env_pack(key, default):
    return _get("environments", key, default, None)

ENV_OPEN = _env_pack("open", [
    "Открываю среду, сэр.",
    "Готовлю рабочее пространство, сэр.",
    "Создаю новый стол, сэр.",
])
ENV_PARTIAL = _env_pack("partial", [
    "Открыл что смог, сэр — часть не удалась.",
    "Среда готова частично, сэр.",
])
ENV_EMPTY = _env_pack("empty", [
    "Сэр, не разобрал, что открыть в среде.",
    "Уточните, сэр, что должно быть в среде.",
])

# --- Live-панель (jarvis live) ---
LIVE_REFRESH_SECONDS = _get("live", "refresh_seconds", 1.0, None)
LIVE_STATUS_TTL = _get("live", "status_ttl_seconds", 3.0, None)
LIVE_HORIZON_HOURS = _get("live", "horizon_hours", 72, None)

# === Умная Wi-Fi лампа (Tuya, ЛОКАЛЬНО через tinytuya — без облака) (ТЗ-8) ===
LAMP_ENABLED = _get("lamp", "enabled", True, "JARVIS_LAMP_ENABLED")
LAMP_DEVICE_ID = str(_get("lamp", "device_id", "", "JARVIS_LAMP_DEVICE_ID")).strip()
LAMP_LOCAL_KEY = str(_get("lamp", "local_key", "", "JARVIS_LAMP_LOCAL_KEY")).strip()
LAMP_IP = str(_get("lamp", "ip", "", "JARVIS_LAMP_IP")).strip()       # пусто → автопоиск по device_id
LAMP_VERSION = _get("lamp", "version", 3.5, "JARVIS_LAMP_VERSION")    # СВЕРЕНО на лампе: 3.5
LAMP_AUTODISCOVER = _get("lamp", "autodiscover", True, None)          # искать по device_id, если ip пуст/молчит
LAMP_RECONNECT_SECONDS = _get("lamp", "reconnect_seconds", 30, None)  # период попыток переподключения
LAMP_SOCKET_TIMEOUT = _get("lamp", "socket_timeout", 4.0, None)       # таймаут сокета (быстрый отклик/фейл)
LAMP_BRIGHTNESS_STEP = _get("lamp", "brightness_step", 20, None)      # шаг «ярче/темнее» (%)
LAMP_TEMP_STEP = _get("lamp", "temp_step", 25, None)                  # шаг «теплее/холоднее» белого (%)
LAMP_NOTIFY_UNAVAILABLE = _get("lamp", "notify_unavailable", False, None)  # уведомлять, если лампа не в сети

# Карта цветов: имя → RGB [0..255]. Пользователь правит/добавляет.
LAMP_COLORS = _get("lamp", "colors", {
    "тёплый": [255, 170, 87], "белый": [255, 255, 255], "красный": [255, 30, 30],
    "зелёный": [40, 230, 60], "синий": [40, 90, 255], "голубой": [60, 200, 255],
    "жёлтый": [255, 210, 40], "оранжевый": [255, 120, 20], "фиолетовый": [170, 40, 255],
    "розовый": [255, 80, 160],
}, None)
# Фоновое состояние лампы (куда возвращаемся после реакций; индикация «готов» при старте).
LAMP_BACKGROUND = _get("lamp", "background",
                       {"вкл": True, "цвет": "тёплый", "яркость": 60}, None)
# Реакции на события: {вкл, цвет, паттерн (свечение|пульс|мигание), яркость, длительность, повторы}.
LAMP_REACTIONS = _get("lamp", "reactions", {
    "startup":  {"вкл": True, "цвет": "тёплый", "паттерн": "пульс", "яркость": 80, "длительность": 1.5, "повторы": 1},
    "speaking": {"вкл": True, "цвет": "синий", "паттерн": "свечение", "яркость": 55, "длительность": 0, "повторы": 1},
    "firing":   {"вкл": True, "цвет": "красный", "паттерн": "мигание", "яркость": 100, "длительность": 3.0, "повторы": 4},
    "call":     {"вкл": True, "цвет": "голубой", "паттерн": "пульс", "яркость": 100, "длительность": 3.0, "повторы": 4},
    "silence":  {"вкл": False, "цвет": "фиолетовый", "паттерн": "свечение", "яркость": 40, "длительность": 0, "повторы": 1},
    "break":    {"вкл": False, "цвет": "зелёный", "паттерн": "пульс", "яркость": 60, "длительность": 2.0, "повторы": 2},
    "error":    {"вкл": False, "цвет": "красный", "паттерн": "мигание", "яркость": 90, "длительность": 1.5, "повторы": 2},
}, None)

def _lamp_pack(key, default):
    return _get("lamp", key, default, None)

LAMP_ON_ACK = _lamp_pack("on_ack", ["Включаю лампу, сэр.", "Свет, сэр.", "Зажигаю, сэр."])
LAMP_OFF_ACK = _lamp_pack("off_ack", ["Гашу лампу, сэр.", "Выключаю свет, сэр.", "Темнота, сэр."])
LAMP_COLOR_ACK = _lamp_pack("color_ack", ["Готово, сэр.", "Меняю цвет, сэр.", "Как скажете, сэр."])
LAMP_BRIGHT_ACK = _lamp_pack("bright_ack", ["Готово, сэр.", "Регулирую, сэр."])
LAMP_TEMP_ACK = _lamp_pack("temp_ack", ["Готово, сэр.", "Меняю оттенок белого, сэр."])
LAMP_AUTO_ACK = _lamp_pack("auto_ack", ["Возвращаю авто-режим, сэр.", "Лампа снова реагирует сама, сэр."])
LAMP_UNAVAILABLE = _lamp_pack("unavailable", [
    "Сэр, лампа не отвечает.", "Не вижу лампу в сети, сэр.", "Лампа сейчас недоступна, сэр."])

# === Коннект с телефоном (приём событий MQTT от «Спутника Джарвиса») (ТЗ-9) ===
PHONE_ENABLED = _get("phone", "enabled", True, "JARVIS_PHONE_ENABLED")
PHONE_ANNOUNCE_CALLS = _get("phone", "announce_calls", True, None)       # озвучивать входящие звонки
PHONE_DUCK_ON_CALL = _get("phone", "duck_on_call", True, None)           # приглушать музыку на время звонка
PHONE_NOTIFY = _get("phone", "notify_notifications", True, None)         # дублировать уведомления телефона
PHONE_BATTERY_ALERTS = _get("phone", "battery_alerts", True, None)       # сообщать о низком заряде
PHONE_BATTERY_REPEAT_MIN = _get("phone", "battery_repeat_minutes", 30, None)  # не чаще раза в N минут
PHONE_PRESENCE_GREETING = _get("phone", "presence_greeting", True, None)  # приветствие при возвращении домой

def _phone_pack(key, default):
    return _get("phone", key, default, None)

# {кто} — имя или номер; {уровень} — процент заряда.
PHONE_CALL = _phone_pack("call", [
    "Сэр, вам звонит {кто}.", "Звонок, сэр — {кто}.", "Вам звонят, сэр: {кто}."])
PHONE_BATTERY_LOW = _phone_pack("battery_low", [
    "Сэр, телефон почти разряжен — {уровень} процентов.",
    "Заряд телефона низкий, сэр: {уровень} процентов."])
PHONE_HOME = _phone_pack("home", [
    "С возвращением, сэр.", "Рад видеть вас дома, сэр.", "Дома, сэр."])
