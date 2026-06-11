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
TOPIC_LAMP = "jarvis/lamp"        # голос-команда лампам (ТЗ-8, заход «лампы»); core форвардит поле
#                                 # «лампа» распознанной команды: {"действие": "вкл|выкл|цвет|ярче|
#                                 # темнее|яркость|тепло|холод|авто", "цвет"?, "уровень"? (0.05..1.0,
#                                 # для действия «яркость»), "кто"?/"текст"? (адресация по имени лампы;
#                                 # нет — действуют ВСЕ лампы)}.
TOPIC_TTS_ENVELOPE = "jarvis/tts/envelope"  # огибающая РЕАЛЬНОГО звука TTS → анимация ламп в такт
#                                 # голосу. Батч на чанк-предложение Piper: {"seq" (id фразы), "t0"
#                                 # (epoch старта звука: первый write в pw-cat + старт_задержка_мс),
#                                 # "offset" (сек первого окна батча от t0 = байты/(2·rate)),
#                                 # "win" (ширина окна, с), "vol", "levels": [0..1, …]}.
#                                 # Финал (всегда, из finally): {"seq","final":true,"duration"} или
#                                 # {"seq","final":true,"cancel":true} — авария, погасить немедленно.
TOPIC_EVENT = "jarvis/event"      # общие события Джарвиса (заход «лампы»): {"event": "error"|
#                                 # "silence_on"|"silence_off"|"break_offer"|"break_praise",
#                                 # "source": "jarvis-…", "detail"?}. Публикуют bus.notify_failure/
#                                 # обработчик ERROR-логов (троттлинг), core (тишина), монитор
#                                 # (перерыв); подписаны лампы (мягкие реакции).
# --- Телефон «Спутник Джарвиса» (ТЗ-9): приложение → Джарвис (JSON, QoS1) ---
TOPIC_PHONE_STATUS = "jarvis/phone/status"            # {"status":"online|offline"} (LWT)
TOPIC_PHONE_BATTERY = "jarvis/phone/battery"          # {"level":82,"isCharging":bool,"isLow":bool}
TOPIC_PHONE_CALL = "jarvis/phone/call"                # {"type":"incoming|started|ended","number","name"}
TOPIC_PHONE_NOTIFICATION = "jarvis/phone/notification"  # {"appCode","appName","title","content"}
TOPIC_PHONE_PRESENCE = "jarvis/phone/presence"        # {"status":"home|away","ssid"}
TOPIC_PHONE_COMMAND = "jarvis/phone/command"          # Джарвис → телефон: {"command":"find_phone"}
#                                 # core резолвит «рабочую среду» → os_agent создаёт вирт. стол и
#                                 # запускает приложения по тегам (как execute, но + новый стол KDE).

# --- Уровни QoS (см. таблицу контрактов в CLAUDE.md) ---
QOS_INPUT = 0
QOS_EXECUTE = 1
QOS_SAY = 0
QOS_STATE = 0
QOS_ENVIRONMENT = 1
QOS_LAMP = 1      # голос-команда лампам не должна теряться (была 0). QoS1 = at-least-once:
#                 # дубль на ЛОКАЛЬНОМ брокере практически исключён, действия идемпотентны.
QOS_TTS_ENVELOPE = 0  # поток огибающей: потеря батча → мягкий провал уровня (страховки в лампах)
QOS_EVENT = 1

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
