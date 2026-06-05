"""Общие настройки «Джарвиса».

Все параметры читаются из переменных окружения с разумными дефолтами,
чтобы ничего не хардкодить и легко переопределять под конкретную машину.
"""
import logging
import os
from pathlib import Path

# Корень проекта (jarvis/config.py -> на уровень выше пакета)
BASE_DIR = Path(__file__).resolve().parent.parent

# Логгер модуля. На момент импорта config хендлеры ещё не настроены — Python отдаст
# WARNING в stderr (lastResort), и он будет виден в консоли/journal. Нужен для
# предупреждений о подозрительных значениях ключей в .env.
_log = logging.getLogger(__name__)

# .env в корне проекта подхватываем ДО чтения переменных — чтобы GEMINI_API_KEY и
# прочие настройки держать в одном файле (шаблон — .env.example). Если python-dotenv
# не установлен или файла нет — молча работаем на голом окружении/системных env.
try:
    from dotenv import load_dotenv

    # override=True: значения из .env перекрывают устаревшее окружение, иначе при
    # уже выставленной (старой) переменной в env взялся бы прежний ключ, а не из .env.
    load_dotenv(BASE_DIR / ".env", override=True)
except Exception:
    pass

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
# Отказоустойчивость локальной модели: сколько ВСЕГО попыток запроса (1 = без ретрая)
# и базовая пауза между ними (секунды, удваивается). При исчерпании — деградация в
# характере, без падения сервиса. Ollama на N100 может «подвиснуть» на загрузке модели.
OLLAMA_RETRIES = int(os.getenv("JARVIS_OLLAMA_RETRIES", "2"))
OLLAMA_RETRY_DELAY = float(os.getenv("JARVIS_OLLAMA_RETRY_DELAY", "1.0"))

# --- Casual-бэкенд: беседу ведёт облачный Gemini, команды — локальная модель ---
# Бэкенд бесед: gemini | local. local — только офлайн-фоллбэк, без обращения к облаку.
CASUAL_BACKEND = os.getenv("JARVIS_CASUAL_BACKEND", "gemini").strip().lower()
# Ключи Gemini — из .env (в git не попадают). Поддерживаем набор ключей с РАЗНЫХ
# аккаунтов: при исчерпании квоты (429) casual.py переключается на следующий. Слоты:
# GEMINI_API_KEY (основной) + GEMINI_API_KEY_2..5. Пустой список → casual в офлайн-режиме.
def _collect_gemini_keys() -> list[str]:
    """Собрать ключи Gemini из .env по слотам, сохранив порядок (первый = основной).

    Каждый ключ .strip(); пустые и дубли отбрасываем (порядок сохраняем). Подозрительные
    значения отбрасываем с WARN: если в ключе остался пробельный символ (после strip это
    значит внутренний пробел или перенос строки), встретилась подстрока GEMINI_API_KEY
    (имя самой переменной внутри значения — верный признак слипшихся строк в .env, в т.ч.
    с нумерованными слотами вида GEMINI_API_KEY_2=) или общая подстрока KEY= — такой ключ
    не должен уйти в HTTP-заголовок, иначе будет ошибка вроде «Illegal header value».
    Настоящий ключ Google (AIza…) пробелов и подстроки GEMINI_API_KEY не содержит.
    """
    raw_values = [os.getenv("GEMINI_API_KEY", "")]
    raw_values += [os.getenv(f"GEMINI_API_KEY_{i}", "") for i in range(2, 6)]

    keys: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        key = (raw or "").strip()
        if not key:
            continue
        if any(ch.isspace() for ch in key) or "GEMINI_API_KEY" in key or "KEY=" in key:
            _log.warning("подозрительное значение ключа Gemini в .env, пропущен")
            continue
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys


GEMINI_API_KEYS: list[str] = _collect_gemini_keys()
# Совместимый алиас на основной ключ — для кода, который ждёт один ключ.
GEMINI_API_KEY = GEMINI_API_KEYS[0] if GEMINI_API_KEYS else ""
# Имя модели вынесено в env: версии Gemini меняются ежемесячно — сверять при установке.
GEMINI_MODEL = os.getenv("JARVIS_GEMINI_MODEL", "gemini-3.5-flash")
# Grounding (модель сама ищет свежие факты): 1/true — вкл, 0/false/no/off — выкл.
GEMINI_GROUNDING = os.getenv("JARVIS_GEMINI_GROUNDING", "1").strip().lower() not in (
    "0", "false", "no", "off", "",
)
# Бюджет «размышления» (thinking) Gemini в токенах. gemini-3.5-flash — ДУМАЮЩАЯ модель:
# по умолчанию тратит на скрытое размышление много времени, из-за чего запрос с нашей
# богатой persona РЕГУЛЯРНО не укладывался в таймаут (сверено на машине: с thinking
# ответ висит >40с → таймаут → фоллбэк; с thinking=0 та же реплика приходит за ~2с).
# Голосовому дворецкому с короткими репликами и простыми вызовами функций размышление не
# нужно — оно лишь съедает латентность и токены. 0 — ВЫКЛ (рекомендуется); >0 — ограничить
# бюджет; -1 — не задавать (оставить поведение модели, если она thinking не поддерживает).
GEMINI_THINKING_BUDGET = int(os.getenv("JARVIS_GEMINI_THINKING_BUDGET", "0"))
# Таймаут запроса к Gemini, секунды. Запрос идёт через зарубежный прокси (лишний хоп).
# С thinking=0 здоровый ответ приходит за ~2-3с, поэтому 20с с запасом покрывают и чуть
# медленные ответы, и при этом БЫСТРО ловят зависший запрос (серверный троттл free-tier).
# В casual.py переводится в миллисекунды для HttpOptions. Минимум API — 10с.
GEMINI_TIMEOUT = float(os.getenv("JARVIS_GEMINI_TIMEOUT", "20"))
# Ретраи запроса к Gemini на ВРЕМЕННЫЕ сбои (503 перегрузка, 504/таймаут, 500, обрыв
# прокси/сети): сколько ВСЕГО попыток на ОДИН ключ (1 = без ретрая) и базовая пауза между
# ними (с, удваивается). 429 (квота) ретраем НЕ лечится — это переключение ключа (см.
# casual.py). Бюджет времени ≈ GEMINI_TIMEOUT × GEMINI_RETRIES — держим коротким для голоса
# (20с × 2 = ~40с худший случай перед фоллбэком; обычно 503/504 проходят со 2-й попытки).
GEMINI_RETRIES = int(os.getenv("JARVIS_GEMINI_RETRIES", "2"))
GEMINI_RETRY_DELAY = float(os.getenv("JARVIS_GEMINI_RETRY_DELAY", "1.0"))
# Сколько реплик беседы (сообщений user/assistant) помнить и слать в облако.
GEMINI_HISTORY = int(os.getenv("JARVIS_GEMINI_HISTORY", "8"))
# Прокси ТОЛЬКО для google-genai клиента (обход геоблока РФ). Пусто = напрямую.
# Поддержка http://… и socks5://… (для socks нужен httpx[socks]).
# КРИТИЧНО: затрагивает лишь Gemini; Ollama/MQTT/STT всегда идут напрямую.
GEMINI_PROXY = os.getenv("JARVIS_GEMINI_PROXY", "").strip()

# --- Мозг с function calling (brain.py) ---
# Максимум итераций цикла «вызов функции → результат → продолжение» на один запрос.
# Защита от зацикливания: составная задача = несколько вызовов подряд, но не бесконечно.
BRAIN_MAX_STEPS = int(os.getenv("JARVIS_BRAIN_MAX_STEPS", "5"))

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
# Порог логирования. По умолчанию INFO: человеческие строки видны, лог чистый. Поставь
# DEBUG, чтобы увидеть стек-трассы ожидаемых сбоёв (их пишем на DEBUG, чтобы не засорять).
LOG_LEVEL = os.getenv("JARVIS_LOG_LEVEL", "INFO").strip().upper()

# --- Heartbeat (видимость «сервис жив») ---
# Раз в столько секунд каждый сервис пишет в лог, что он жив (INFO). 0 — выключить.
# По логам видно, что сервис работает, а не висит молча. На шину ничего не публикуем.
HEARTBEAT_INTERVAL = float(os.getenv("JARVIS_HEARTBEAT_INTERVAL", "300"))
