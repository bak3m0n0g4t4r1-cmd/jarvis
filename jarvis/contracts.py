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
# Джарвис — локальный голосовой пульт: единственное намерение — команда ОС (тег из
# commands.yaml). Беседы/облака больше нет. Намерение оставлено явным значением, чтобы
# контракт читался однозначно и был задел на будущие типы команд.
Intent = Literal["os_command"]


class JarvisResponse(BaseModel):
    """Структурированный результат распознавания команды.

    intent — тип намерения (сейчас только os_command);
    payload — РОВНО один тег команды из commands.yaml ("telegram", ...) или "none";
    speech_response — короткое подтверждение в характере Джарвиса.
    """

    intent: Intent = "os_command"
    payload: str = Field(default="none")
    speech_response: str = Field(default="")
