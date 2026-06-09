"""Контракты шины данных «Джарвиса» — единый источник правды.

Здесь живут имена MQTT-топиков, уровни QoS и допустимые состояния. Любой модуль
импортирует контракты отсюда, чтобы не было рассинхрона имён топиков и формата
сообщений. Чистый typing — без сторонних зависимостей.
"""
from typing import Literal

# --- Имена топиков шины ---
TOPIC_INPUT = "jarvis/input"      # {"text","user_level"?:float,"wake"?:bool}
#                                 # wake — было ли обращение «Джарвис»/PTT (true) или фраза без него
#                                 # (false → core примет ТОЛЬКО как продолжение активной ветки). Нет поля = true.
TOPIC_EXECUTE = "jarvis/execute"  # {"command_tag": "..."}
TOPIC_SAY = "jarvis/say"          # {"text","source","user_level"?:float,"min_volume"?:float,"chime"?:bool}
#                                 # min_volume — нижний предел громкости (будильник/таймер: обход «тихо→тихо»)
#                                 # chime — короткий сигнал перед фразой (срабатывание таймера)
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
