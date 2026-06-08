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
                  ["джарвис", "джарвиз", "жарвис", "жарвиз", "сервис", "джарвес"],
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
# Источник захвата: пусто = системный default-микрофон; имя echo-cancel source → шумоподавление.
STT_SOURCE = str(_get("hearing", "stt_source", "", "JARVIS_STT_SOURCE")).strip()

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
