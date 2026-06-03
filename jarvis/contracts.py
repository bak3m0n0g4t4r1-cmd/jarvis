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
# Фаза 2: 1.5B работает диспетчером-классификатором. Всего два намерения —
# либо это команда ОС (тег из commands.yaml), либо беседа (её ведёт casual-бэкенд,
# по умолчанию Gemini). Бывший web_search убран: за свежими фактами в casual
# ходит grounding самой облачной модели, отдельный интент не нужен.
Intent = Literal["os_command", "casual_talk"]


class JarvisResponse(BaseModel):
    """Структурированный ответ «Мозга»-диспетчера (нативный structured output Ollama).

    intent — тип намерения (os_command | casual_talk);
    payload — РОВНО один тег команды из commands.yaml ("telegram", ...) или "none";
    speech_response — короткое подтверждение в характере Джарвиса для os_command;
        для casual_talk остаётся пустым: реплику беседы генерит casual-бэкенд.
    """

    intent: Intent
    payload: str = Field(default="none")
    speech_response: str = Field(default="")
