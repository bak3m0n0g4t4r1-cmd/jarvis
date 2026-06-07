"""Общие настройки «Джарвиса».

Все параметры читаются из переменных окружения с разумными дефолтами,
чтобы ничего не хардкодить и легко переопределять под конкретную машину.
"""
import os
from pathlib import Path

# Корень проекта (jarvis/config.py -> на уровень выше пакета)
BASE_DIR = Path(__file__).resolve().parent.parent

# Настройки берутся из переменных окружения с дефолтами. Секретов больше нет
# (облако удалено), .env не обязателен; необязательные JARVIS_*-оверрайды для
# сервисов systemd подхватывает сам через EnvironmentFile (см. cli.py/юниты).

MODELS_DIR = Path(os.getenv("JARVIS_MODELS_DIR", str(BASE_DIR / "models")))
LOGS_DIR = Path(os.getenv("JARVIS_LOGS_DIR", str(BASE_DIR / "logs")))

# --- MQTT ---
MQTT_HOST = os.getenv("JARVIS_MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("JARVIS_MQTT_PORT", "1883"))
MQTT_KEEPALIVE = int(os.getenv("JARVIS_MQTT_KEEPALIVE", "60"))
# Автопереподключение (экспоненциальный backoff между попытками, секунды).
# Брокер локальный и поднимается быстро; на этой машине разрывы — это перезапуск
# mosquitto и спящий режим (диагностировано по journalctl). Поэтому потолок мал (15с),
# иначе после suspend Джарвис «глохнет» до ~2 мин, пока backoff дорастёт до старого max=60.
MQTT_RECONNECT_MIN = float(os.getenv("JARVIS_MQTT_RECONNECT_MIN", "1"))
MQTT_RECONNECT_MAX = float(os.getenv("JARVIS_MQTT_RECONNECT_MAX", "15"))

# --- Аудио (захват/воспроизведение) ---
SAMPLE_RATE = int(os.getenv("JARVIS_SAMPLE_RATE", "16000"))
CHANNELS = 1

# --- Wake-word ---
# Список вариантов: zipformer-ru стабильно искажает «джарвис» → добавляй
# реально встреченные искажения сюда, без правки кода.
WAKE_WORDS = os.getenv("JARVIS_WAKE_WORDS", "джарвис,джарвиз,жарвис,жарвиз,сервис,джарвес").split(",")
# Порог нечёткого совпадения (difflib ratio, 0–1): ниже — больше ложных срабатываний.
WAKE_WORD_FUZZY_THRESHOLD = float(os.getenv("JARVIS_WAKE_WORD_FUZZY_THRESHOLD", "0.7"))

# Защита от эхо-петли: пока Джарвис говорит (state=speaking), STT глушит вход.
# «Хвост» — пауза после окончания речи перед возобновлением, чтобы не словить
# конец фразы из колонок (с). 1.0с покрывает «звон» колонок и реверберацию,
# которые остаются уже после того, как PortAudio считает буфер опустошённым.
SPEAKING_TAIL = float(os.getenv("JARVIS_SPEAKING_TAIL", "1.0"))
# Контент-фильтр эха (страховка): сколько секунд после возобновления слушания
# сверять распознанное с последней фразой Джарвиса (с).
ECHO_CONTENT_WINDOW = float(os.getenv("JARVIS_ECHO_CONTENT_WINDOW", "2.0"))
# Порог похожести (difflib ratio 0–1): выше — фраза считается эхом и отбрасывается.
ECHO_SIMILARITY_THRESHOLD = float(os.getenv("JARVIS_ECHO_SIMILARITY_THRESHOLD", "0.6"))

# Минимальная длина аудиосегмента в семплах для подачи в zipformer-распознаватель.
# Короче этого — гарантированный RuntimeError Reshape, модель не может обработать.
# 8000 семплов = 0.5 с при 16 кГц, минимально стабильная длина для transducer.
MIN_SEGMENT_SAMPLES = int(os.getenv("JARVIS_MIN_SEGMENT_SAMPLES", "8000"))

# --- STT: sherpa-onnx (silero-VAD + zipformer-ru offline transducer) ---
VAD_MODEL = os.getenv("JARVIS_VAD_MODEL", str(MODELS_DIR / "silero_vad.onnx"))
VAD_THRESHOLD = float(os.getenv("JARVIS_VAD_THRESHOLD", "0.3"))
VAD_MIN_SILENCE = float(os.getenv("JARVIS_VAD_MIN_SILENCE", "0.8"))
VAD_MIN_SPEECH = float(os.getenv("JARVIS_VAD_MIN_SPEECH", "0.25"))
# Размер кольцевого буфера VAD (с). Команды короткие — 10с с запасом; меньше памяти,
# чем прежние 30с. НЕ путать с порогами VAD (их НЕ трогаем).
VAD_BUFFER_SECONDS = int(os.getenv("JARVIS_VAD_BUFFER_SECONDS", "10"))
# Потоки sherpa-onnx (VAD + распознаватель). На N100 один поток экономнее и без
# потоковой возни на крошечной модели — меньше CPU при постоянном прослушивании.
STT_NUM_THREADS = int(os.getenv("JARVIS_STT_NUM_THREADS", "1"))
# Архив распаковывается во вложенную папку — учитываем в пути.
_ZIPFORMER_DIR = MODELS_DIR / "sherpa-onnx-small-zipformer-ru-2024-09-18"
ZIPFORMER_ENCODER = os.getenv(
    "JARVIS_ZIPFORMER_ENCODER", str(_ZIPFORMER_DIR / "encoder.int8.onnx")
)
ZIPFORMER_DECODER = os.getenv(
    "JARVIS_ZIPFORMER_DECODER", str(_ZIPFORMER_DIR / "decoder.int8.onnx")
)
ZIPFORMER_JOINER = os.getenv(
    "JARVIS_ZIPFORMER_JOINER", str(_ZIPFORMER_DIR / "joiner.int8.onnx")
)
ZIPFORMER_TOKENS = os.getenv(
    "JARVIS_ZIPFORMER_TOKENS", str(_ZIPFORMER_DIR / "tokens.txt")
)
ZIPFORMER_BPE = os.getenv(
    "JARVIS_ZIPFORMER_BPE", str(_ZIPFORMER_DIR / "bpe.model")
)

# --- Распознавание команд: матчер (правила + ONNX-эмбеддинги, см. matcher.py) ---
# Автономный лёгкий эмбеддер rubert-tiny2 (ONNX, через onnxruntime, без torch).
# Считает семантическую близость, когда слой правил не дал уверенного ответа.
EMBEDDER_DIR = Path(os.getenv("JARVIS_EMBEDDER_DIR", str(MODELS_DIR / "rubert-tiny2-onnx")))
EMBEDDER_MODEL = os.getenv("JARVIS_EMBEDDER_MODEL", str(EMBEDDER_DIR / "model_optimized.onnx"))
EMBEDDER_TOKENIZER = os.getenv("JARVIS_EMBEDDER_TOKENIZER", str(EMBEDDER_DIR / "tokenizer.json"))
# Кеш эмбеддингов команд на диске (npz): считаются один раз, при старте берутся готовыми.
MATCHER_CACHE = os.getenv("JARVIS_MATCHER_CACHE", str(EMBEDDER_DIR / "cmd_emb_cache.npz"))
# Порог слоя ПРАВИЛ (difflib ratio 0–1): ниже — фраза не считается совпавшей с синонимом.
MATCHER_FUZZY_THRESHOLD = float(os.getenv("JARVIS_MATCHER_FUZZY_THRESHOLD", "0.7"))
# Порог слоя ЭМБЕДДИНГОВ (косинус 0–1): ниже — семантический поиск не уверен → переспрос.
MATCHER_EMB_THRESHOLD = float(os.getenv("JARVIS_MATCHER_EMB_THRESHOLD", "0.6"))
# Минимальный отрыв лучшего кандидата от второго (косинус). Меньше — кандидаты почти
# равны (часто антонимы вроде вкл/выкл) → не угадываем, лучше переспросить.
MATCHER_EMB_MARGIN = float(os.getenv("JARVIS_MATCHER_EMB_MARGIN", "0.04"))

# --- TTS: Piper ---
PIPER_MODEL = os.getenv("JARVIS_PIPER_MODEL", str(MODELS_DIR / "ru_RU-dmitri-medium.onnx"))
PIPER_CONFIG = os.getenv(
    "JARVIS_PIPER_CONFIG", str(MODELS_DIR / "ru_RU-dmitri-medium.onnx.json")
)
# Sink PipeWire для воспроизведения (pw-cat --target). Пусто = системный default sink.
# На TUXEDO sounddevice/PortAudio не видит аналоговый вывод (только HDMI) — играем через
# PipeWire (pw-cat), иначе Джарвис нем (paInvalidSampleRate на 22050 Гц). См. tts.py.
TTS_SINK = os.getenv("JARVIS_TTS_SINK", "").strip()

# --- OS-агент ---
COMMANDS_FILE = os.getenv("JARVIS_COMMANDS_FILE", str(BASE_DIR / "commands.yaml"))

# --- Логирование ---
LOG_MAX_BYTES = int(os.getenv("JARVIS_LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("JARVIS_LOG_BACKUP_COUNT", "3"))
# Порог логирования. По умолчанию INFO: человеческие строки видны, лог чистый. Поставь
# DEBUG, чтобы увидеть стек-трассы ожидаемых сбоёв (их пишем на DEBUG, чтобы не засорять).
LOG_LEVEL = os.getenv("JARVIS_LOG_LEVEL", "INFO").strip().upper()

# --- Heartbeat (видимость «сервис жив») ---
# Раз в столько секунд каждый сервис пишет в лог, что он жив (INFO). 0 — выключить.
# По логам видно, что сервис работает, а не висит молча. На шину ничего не публикуем.
HEARTBEAT_INTERVAL = float(os.getenv("JARVIS_HEARTBEAT_INTERVAL", "300"))
