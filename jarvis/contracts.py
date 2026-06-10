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
TOPIC_SAY = "jarvis/say"          # {"text","source","user_level"?,"min_volume"?,"chime"?,"critical"?}
#                                 # min_volume — нижний предел громкости (будильник/таймер: обход «тихо→тихо»)
#                                 # chime — короткий сигнал перед фразой (срабатывание таймера)
#                                 # critical — срабатывание (будильник/таймер/напоминание): ОЗВУЧИТЬ даже в
#                                 #   режиме тишины. Наличие min_volume тоже трактуется как critical (ТЗ-6).
TOPIC_STATE = "jarvis/state"      # {"state": "...", "source": "..."}
TOPIC_ENVIRONMENT = "jarvis/environment"  # {"desktop": "имя стола", "apps": ["тег", ...]} (ТЗ-7)
TOPIC_LAMP = "jarvis/lamp"        # {"действие": {...}} — голос-команда лампе (ТЗ-8); core форвардит
#                                 # поле «лампа» распознанной команды (вкл/выкл/цвет/яркость/авто).
#                                 # core резолвит «рабочую среду» → os_agent создаёт вирт. стол и
#                                 # запускает приложения по тегам (как execute, но + новый стол KDE).

# --- Уровни QoS (см. таблицу контрактов в CLAUDE.md) ---
QOS_INPUT = 0
QOS_EXECUTE = 1
QOS_SAY = 0
QOS_STATE = 0
QOS_ENVIRONMENT = 1
QOS_LAMP = 0

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
