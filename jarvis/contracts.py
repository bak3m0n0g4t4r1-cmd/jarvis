"""Контракты шины данных «Джарвиса» — единый источник правды.

Здесь живут имена MQTT-топиков, уровни QoS, допустимые состояния и
pydantic-схема ответа «Мозга». Любой модуль импортирует контракты отсюда,
чтобы не было рассинхрона имён топиков и формата сообщений.
"""
from typing import Literal

from pydantic import BaseModel, Field

# --- Имена топиков шины ---
TOPIC_INPUT = "jarvis/input"      # {"text": "..."}
TOPIC_EXECUTE = "jarvis/execute"  # {"command_tag": "..."}
TOPIC_SAY = "jarvis/say"          # {"text": "...", "source": "..."}
TOPIC_STATE = "jarvis/state"      # {"state": "...", "source": "..."}

# --- Уровни QoS (см. таблицу контрактов в CLAUDE.md) ---
QOS_INPUT = 0
QOS_EXECUTE = 1
QOS_SAY = 0
QOS_STATE = 0

# --- Допустимые состояния ассистента ---
State = Literal["idle", "listening", "thinking", "speaking"]
STATE_IDLE = "idle"
STATE_LISTENING = "listening"
STATE_THINKING = "thinking"
STATE_SPEAKING = "speaking"

# --- Намерения «Мозга» ---
Intent = Literal["os_command", "web_search", "casual_talk"]


class JarvisResponse(BaseModel):
    """Структурированный ответ «Мозга» (нативный structured output Ollama).

    intent — тип намерения;
    payload — тег команды/поиска ("telegram", "wifi_off", "network_scan", "none");
    speech_response — реплика голосом в стиле Тони Старка, на русском.
    """

    intent: Intent
    payload: str = Field(default="none")
    speech_response: str
