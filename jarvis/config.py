"""Общие настройки «Джарвиса».

Все параметры читаются из переменных окружения с разумными дефолтами,
чтобы ничего не хардкодить и легко переопределять под конкретную машину.
"""
import os
from pathlib import Path

# Корень проекта (jarvis/config.py -> на уровень выше пакета)
BASE_DIR = Path(__file__).resolve().parent.parent
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
WAKE_WORD = os.getenv("JARVIS_WAKE_WORD", "джарвис")

# --- STT: sherpa-onnx (silero-VAD + SenseVoice-Small) ---
VAD_MODEL = os.getenv("JARVIS_VAD_MODEL", str(MODELS_DIR / "silero_vad.onnx"))
VAD_THRESHOLD = float(os.getenv("JARVIS_VAD_THRESHOLD", "0.5"))
VAD_MIN_SILENCE = float(os.getenv("JARVIS_VAD_MIN_SILENCE", "0.5"))
VAD_MIN_SPEECH = float(os.getenv("JARVIS_VAD_MIN_SPEECH", "0.25"))
SENSEVOICE_MODEL = os.getenv(
    "JARVIS_SENSEVOICE_MODEL", str(MODELS_DIR / "sense-voice" / "model.onnx")
)
SENSEVOICE_TOKENS = os.getenv(
    "JARVIS_SENSEVOICE_TOKENS", str(MODELS_DIR / "sense-voice" / "tokens.txt")
)

# --- LLM: Ollama ---
OLLAMA_HOST = os.getenv("JARVIS_OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("JARVIS_OLLAMA_MODEL", "qwen2.5:0.5b-instruct")
OLLAMA_KEEP_ALIVE = os.getenv("JARVIS_OLLAMA_KEEP_ALIVE", "10m")
HISTORY_SIZE = int(os.getenv("JARVIS_HISTORY_SIZE", "5"))  # пар user/assistant

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
