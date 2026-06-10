"""Живая панель состояния Джарвиса (`jarvis live`) — TUI на rich.live до Ctrl+C.

Показывает в реальном времени: эфир (распознанные фразы + состояние), сервисы (юниты + MQTT),
расписание на 3 дня (будильники/таймеры/секундомеры/напоминания/задачи), перерыв (детектор
активности), режим (тишина/громкость). Лёгкость: rich.live ~1–2 fps, статус юнитов кэшируется.

Чистый выход: rich.Live(screen=True) использует альтернативный буфер терминала и ВОССТАНАВЛИВАЕТ
его при выходе (Ctrl+C → терминал цел, без «битого» вывода); MQTT-клиент закрывается в finally.
"""
import json
import logging
import subprocess
import threading
import time
from collections import deque
from datetime import datetime

from jarvis import config, contracts
from jarvis.services_map import SERVICES

_log = logging.getLogger("jarvis-live")
_ACTIVITY_STATE = "activity_state.json"


class _State:
    """Потокобезопасное общее состояние панели (пишет MQTT-поток, читает рендер)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.phrases = deque(maxlen=6)   # (время, текст, wake)
        self.state = "—"                 # последнее jarvis/state
        self.last_say = ""               # последняя реплика Джарвиса
        self.mqtt_ok = False
        self._status = []                # [(name, active)]
        self._status_at = 0.0

    def add_phrase(self, text, wake):
        with self._lock:
            self.phrases.appendleft((datetime.now().strftime("%H:%M:%S"), text, wake))

    def set_state(self, s):
        with self._lock:
            self.state = s

    def set_say(self, s):
        with self._lock:
            self.last_say = s

    def snapshot(self):
        with self._lock:
            return list(self.phrases), self.state, self.last_say, self.mqtt_ok

    def services(self):
        """Статус юнитов с кэшем LIVE_STATUS_TTL (не дёргать systemctl каждый кадр)."""
        now = time.monotonic()
        if self._status and (now - self._status_at) < config.LIVE_STATUS_TTL:
            return self._status
        out = []
        for svc in SERVICES:
            active = False
            try:
                r = subprocess.run(["systemctl", "--user", "is-active", svc.unit],
                                   capture_output=True, text=True, timeout=2)
                active = r.stdout.strip() == "active"
            except Exception:
                active = False
            out.append((svc.unit.removesuffix(".service"), active))
        self._status, self._status_at = out, now
        return out


# --------------------------------------------------------------------------- #
# MQTT-подписка на эфир/состояние
# --------------------------------------------------------------------------- #
def _connect_mqtt(state: _State):
    import paho.mqtt.client as mqtt

    def on_connect(client, userdata, flags, reason_code, properties=None):
        state.mqtt_ok = True
        for topic in (contracts.TOPIC_INPUT, contracts.TOPIC_STATE, contracts.TOPIC_SAY):
            client.subscribe(topic)

    def on_disconnect(client, userdata, *a):
        state.mqtt_ok = False

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return
        if msg.topic == contracts.TOPIC_INPUT:
            text = (payload.get("text") or "").strip()
            if text:
                state.add_phrase(text, payload.get("wake", True))
        elif msg.topic == contracts.TOPIC_STATE:
            state.set_state(payload.get("state") or "—")
        elif msg.topic == contracts.TOPIC_SAY:
            text = (payload.get("text") or "").strip()
            if text:
                state.set_say(text)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="jarvis-live")
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.connect(config.MQTT_HOST, config.MQTT_PORT, 5)
    client.loop_start()
    return client


# --------------------------------------------------------------------------- #
# Форматирование
# --------------------------------------------------------------------------- #
def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}ч {m:02d}м {s:02d}с"
    if m:
        return f"{m}м {s:02d}с"
    return f"{s}с"


def _parse_iso(value):
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _schedule_lines(now: datetime, horizon_hours: int):
    """Строки расписания на ближайшие N часов (что задано: будильники/таймеры/…). Всё в try-except."""
    from jarvis import alarms

    lines = []
    try:
        sched = alarms.read_schedule()
    except Exception:
        return ["  (не удалось прочитать расписание)"]
    try:
        for a in sched.get("будильники", []):
            if not a.get("активен", True):
                continue
            mark = f" «{a['метка']}»" if a.get("метка") else ""
            repeat = a.get("повтор") or ("ежедневный" if a.get("тип") == "morning" else "разовый")
            lines.append(f"  ⏰ {a.get('время', '??:??')}{mark} · {repeat}")
        for t in sched.get("таймеры", []):
            if not t.get("активен", True) or t.get("сработал"):
                continue
            end = _parse_iso(t.get("окончание"))
            if not end:
                continue
            mark = f" «{t['метка']}»" if t.get("метка") else ""
            lines.append(f"  ⏳ осталось {_fmt_duration((end - now).total_seconds())}{mark}")
        for w in sched.get("секундомеры", []):
            if not w.get("активен", True):
                continue
            start = _parse_iso(w.get("старт"))
            if not start:
                continue
            end = _parse_iso(w.get("стоп")) or now
            mark = f" «{w['метка']}»" if w.get("метка") else ""
            lines.append(f"  ⏱ прошло {_fmt_duration((end - start).total_seconds())}{mark}")
        for r in sched.get("напоминания", []):
            if not r.get("активен", True) or r.get("сработал"):
                continue
            when = _parse_iso(r.get("срабатывание"))
            if when and (when - now).total_seconds() > horizon_hours * 3600:
                continue
            ws = when.strftime("%d.%m %H:%M") if when else "когда-то"
            lines.append(f"  🔔 {ws} · {r.get('текст', '')}")
        for task in sched.get("задачи", []):
            if not task.get("активен", True) or task.get("выполнена"):
                continue
            dl = _parse_iso(task.get("дедлайн"))
            ds = f" (до {dl.strftime('%d.%m %H:%M')})" if dl else ""
            lines.append(f"  📋 {task.get('текст', '')}{ds}")
    except Exception:
        _log.debug("Сбой формирования расписания", exc_info=True)
    return lines or ["  (пусто на ближайшие 3 дня)"]


def _phone_lines():
    """Состояние телефона из logs/phone_state.json (его пишет сервис phone). Нет файла → офлайн."""
    import os

    path = os.path.join(str(config.LOGS_DIR), "phone_state.json")
    try:
        if not os.path.exists(path):
            return ["  (телефон не подключён)"]
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        return ["  (состояние недоступно)"]
    lines = []
    try:
        status = st.get("status", "offline")
        lines.append(f"  статус: {'🟢 на связи' if status == 'online' else '⚪ офлайн'}")
        bat = st.get("battery")
        if isinstance(bat, (int, float)):
            chrg = " (заряжается)" if st.get("charging") else ""
            lines.append(f"  заряд: {bat}%{chrg}")
        pres = st.get("presence")
        if pres:
            lines.append(f"  присутствие: {'дома' if pres == 'home' else 'нет дома'}")
    except Exception:
        pass
    return lines or ["  (нет данных)"]


def _break_lines(now: datetime):
    """Состояние детектора перерывов из logs/activity_state.json (его пишет activity_monitor)."""
    import os

    path = os.path.join(str(config.LOGS_DIR), _ACTIVITY_STATE)
    try:
        if not os.path.exists(path):
            return ["  (детектор перерывов не активен)"]
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        return ["  (состояние недоступно)"]
    lines = []
    try:
        if st.get("on_break"):
            lines.append("  🟢 идёт перерыв")
        ws = st.get("working_seconds")
        if isinstance(ws, (int, float)):
            lines.append(f"  работает: {_fmt_duration(ws)}")
        us = st.get("until_offer_seconds")
        if isinstance(us, (int, float)):
            lines.append(f"  напомнит о перерыве через: {_fmt_duration(us)}" if us > 0
                         else "  напоминание о перерыве — скоро")
        updated = _parse_iso(st.get("updated_at"))
        if updated and (now - updated).total_seconds() > 120:
            lines.append("  (данные устарели — монитор не обновляет)")
    except Exception:
        pass
    return lines or ["  (нет данных)"]


def _render(state: _State):
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    from jarvis import silence
    from jarvis.sysinfo import read_volume

    now = datetime.now()
    phrases, cur_state, last_say, mqtt_ok = state.snapshot()

    # Эфир.
    air = Table.grid(padding=(0, 1))
    air.add_column()
    _state_color = {"listening": "green", "thinking": "yellow", "speaking": "cyan"}.get(cur_state, "white")
    air.add_row(Text(f"состояние: {cur_state}", style=_state_color))
    if last_say:
        air.add_row(Text(f"последняя реплика: {last_say[:70]}", style="dim"))
    air.add_row(Text("слышу:", style="bold"))
    if phrases:
        for ts, text, wake in phrases:
            tag = "" if wake else " ·без обращения"
            air.add_row(Text(f"  {ts}  {text}{tag}", style="white" if wake else "dim"))
    else:
        air.add_row(Text("  (тишина в эфире)", style="dim"))

    # Сервисы.
    svc = Table.grid(padding=(0, 2))
    svc.add_column(); svc.add_column()
    for name, active in state.services():
        svc.add_row(Text("●", style="green" if active else "red"),
                    Text(name, style="white" if active else "red"))
    svc.add_row(Text("●", style="green" if mqtt_ok else "red"),
                Text("MQTT", style="white" if mqtt_ok else "red"))

    # Расписание / перерыв / режим.
    sched = Text("\n".join(_schedule_lines(now, config.LIVE_HORIZON_HOURS)) or "")
    brk = Text("\n".join(_break_lines(now)))

    try:
        silent = silence.is_silent()
    except Exception:
        silent = False
    try:
        vdata = read_volume()
        vol = "выкл" if vdata.get("выключен") else (f"{vdata.get('громкость_процент')}%"
                                                    if "ошибка" not in vdata else "?")
    except Exception:
        vol = "?"
    mode = Text.assemble(("режим: ", "bold"),
                         ("ТИШИНА" if silent else "голос", "yellow" if silent else "green"),
                         (f"   ·   громкость: {vol}", "white"))

    return Group(
        Panel(air, title="Эфир", border_style="cyan"),
        Panel(svc, title="Сервисы", border_style="blue"),
        Panel(sched, title="Расписание · 3 дня", border_style="magenta"),
        Panel(brk, title="Перерыв", border_style="green"),
        Panel(Text("\n".join(_phone_lines())), title="Телефон", border_style="yellow"),
        Panel(mode, title="Режим", border_style="white"),
    )


def run() -> int:
    """Запустить живую панель до Ctrl+C. Возвращает 0."""
    from rich.live import Live

    state = _State()
    client = None
    try:
        client = _connect_mqtt(state)
    except Exception as exc:
        print(f"Не удалось подключиться к MQTT: {exc}")
    refresh = max(0.25, float(config.LIVE_REFRESH_SECONDS))
    try:
        with Live(_render(state), screen=True, refresh_per_second=4) as live:
            while True:
                time.sleep(refresh)
                live.update(_render(state))
    except KeyboardInterrupt:
        pass
    except Exception:
        _log.debug("Панель завершилась с ошибкой", exc_info=True)
    finally:
        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
    print("Панель закрыта, сэр.")
    return 0
