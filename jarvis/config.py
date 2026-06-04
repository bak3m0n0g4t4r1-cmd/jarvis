"""Общие настройки «Джарвиса».

Все параметры читаются из переменных окружения с разумными дефолтами,
чтобы ничего не хардкодить и легко переопределять под конкретную машину.
"""
import os
from pathlib import Path

# Корень проекта (jarvis/config.py -> на уровень выше пакета)
BASE_DIR = Path(__file__).resolve().parent.parent

# .env в корне проекта подхватываем ДО чтения переменных — чтобы GEMINI_API_KEY и
# прочие настройки держать в одном файле (шаблон — .env.example). Если python-dotenv
# не установлен или файла нет — молча работаем на голом окружении/системных env.
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass

MODELS_DIR = Path(os.getenv("JARVIS_MODELS_DIR", str(BASE_DIR / "models")))
LOGS_DIR = Path(os.getenv("JARVIS_LOGS_DIR", str(BASE_DIR / "logs")))

# --- MQTT ---
MQTT_HOST = os.getenv("JARVIS_MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("JARVIS_MQTT_PORT", "1883"))
MQTT_KEEPALIVE = int(os.getenv("JARVIS_MQTT_KEEPALIVE", "60"))

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

# --- LLM: Ollama ---
OLLAMA_HOST = os.getenv("JARVIS_OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("JARVIS_OLLAMA_MODEL", "qwen2.5:1.5b-instruct")
OLLAMA_KEEP_ALIVE = os.getenv("JARVIS_OLLAMA_KEEP_ALIVE", "10m")
HISTORY_SIZE = int(os.getenv("JARVIS_HISTORY_SIZE", "5"))  # пар user/assistant

# --- Casual-бэкенд: беседу ведёт облачный Gemini, команды — локальная модель ---
# Бэкенд бесед: gemini | local. local — только офлайн-фоллбэк, без обращения к облаку.
CASUAL_BACKEND = os.getenv("JARVIS_CASUAL_BACKEND", "gemini").strip().lower()
# Ключ Gemini — из .env (в git не попадает). Пустой → casual уходит в офлайн-фоллбэк.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# Имя модели вынесено в env: версии Gemini меняются ежемесячно — сверять при установке.
GEMINI_MODEL = os.getenv("JARVIS_GEMINI_MODEL", "gemini-3.5-flash")
# Grounding (модель сама ищет свежие факты): 1/true — вкл, 0/false/no/off — выкл.
GEMINI_GROUNDING = os.getenv("JARVIS_GEMINI_GROUNDING", "1").strip().lower() not in (
    "0", "false", "no", "off", "",
)
# Таймаут запроса к Gemini, секунды. Для grounding 5с маловато (поиск + генерация),
# поэтому дефолт 8с; в casual.py переводится в миллисекунды для HttpOptions.
GEMINI_TIMEOUT = float(os.getenv("JARVIS_GEMINI_TIMEOUT", "8"))
# Сколько реплик беседы (сообщений user/assistant) помнить и слать в облако.
GEMINI_HISTORY = int(os.getenv("JARVIS_GEMINI_HISTORY", "8"))
# Прокси ТОЛЬКО для google-genai клиента (обход геоблока РФ). Пусто = напрямую.
# Поддержка http://… и socks5://… (для socks нужен httpx[socks]).
# КРИТИЧНО: затрагивает лишь Gemini; Ollama/MQTT/STT всегда идут напрямую.
GEMINI_PROXY = os.getenv("JARVIS_GEMINI_PROXY", "").strip()

# --- TTS: Piper ---
PIPER_MODEL = os.getenv("JARVIS_PIPER_MODEL", str(MODELS_DIR / "ru_RU-dmitri-medium.onnx"))
PIPER_CONFIG = os.getenv(
    "JARVIS_PIPER_CONFIG", str(MODELS_DIR / "ru_RU-dmitri-medium.onnx.json")
)

# --- OS-агент ---
COMMANDS_FILE = os.getenv("JARVIS_COMMANDS_FILE", str(BASE_DIR / "commands.yaml"))

# --- Логирование ---
LOG_MAX_BYTES = int(os.getenv("JARVIS_LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("JARVIS_LOG_BACKUP_COUNT", "3"))
