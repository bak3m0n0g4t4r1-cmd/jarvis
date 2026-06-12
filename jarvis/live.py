"""Живая панель состояния Джарвиса (`jarvis live`) — событийный TUI на rich.live до Ctrl+C.

Архитектура (без задержек):
  • MQTT-поток пишет эфир (фразы/состояние/реплику) и БУДИТ рендер мгновенно — фраза
    появляется ровно тогда, когда пришла, а не на следующем тике таймера.
  • Фоновый поток-поллер делает ВЕСЬ дорогой I/O (systemctl ×8, wpctl-громкость, чтение
    JSON расписания/перерыва/телефона) и складывает результат в кэш — рендер сам не
    блокируется ни на одном системном вызове.
  • Рендер форматирует только кэш + ЖИВОЕ текущее время: часы (ЧЧ:ММ:СС) и счётчики
    (обратный отсчёт таймеров, «работает N») считаются от сырых данных на каждом кадре,
    поэтому секунды тикают чётко.
  • Отрисовка по требованию (auto_refresh=False): кадр рисуется только когда что-то
    изменилось — по событию MQTT или на границе очередной секунды (для часов). Без
    лишних перерисовок и мерцания.

Палитра спокойная: серые рамки, тусклые cyan-заголовки, цвет — только на данных.

Чистый выход: rich.Live(screen=True) держит альтернативный буфер терминала и восстанавливает
его при Ctrl+C; MQTT-клиент и поллер закрываются в finally.
"""
import json
import logging
import os
import subprocess
import threading
import time
from collections import deque
from datetime import datetime

from jarvis import config, contracts
from jarvis.services_map import SERVICES

_log = logging.getLogger("jarvis-live")
_ACTIVITY_STATE = "activity_state.json"
_PHONE_STATE = "phone_state.json"

# Спокойная палитра: серые рамки, тусклые cyan-заголовки, цвет — только на данных.
_BORDER = "grey37"
_TITLE = "cyan"
_STATE_COLOR = {"listening": "green", "thinking": "yellow", "speaking": "cyan"}

# Короткие имена юнитов для сетки «Сервисы» (без префикса jarvis-, компактно).
_SHORT_NAME = {
    "jarvis-stt": "stt", "jarvis-core": "core", "jarvis-tts": "tts",
    "jarvis-os-agent": "os-agent", "jarvis-activity-monitor": "activity",
    "jarvis-scheduler": "sched", "jarvis-lamp": "lamp", "jarvis-phone": "phone",
}


class _State:
    """Потокобезопасное общее состояние панели.

    Пишут два потока: MQTT-колбэк (эфир) и поллер (кэш системных данных). Читает рендер
    через snapshot(). Любая запись будит рендер (_wake), чтобы кадр обновился без задержки.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._wake = threading.Event()
        # Эфир (пишет MQTT-поток).
        self.phrases = deque(maxlen=8)   # (время, текст, wake)
        self.state = "—"                 # последнее jarvis/state
        self.last_say = ""               # последняя реплика Джарвиса
        self.mqtt_ok = False
        # Кэш системных данных (пишет поллер) — сырьё, человекочитаемое считает рендер.
        self._services = []              # [(unit, active)]
        self._silent = False
        self._vol = "?"
        self._sched = {}                 # сырой dict расписания
        self._break = None               # сырой dict перерыва или None
        self._phone = None               # сырой dict телефона или None

    # -- сторона MQTT (эфир) -- #
    def add_phrase(self, text, wake):
        with self._lock:
            self.phrases.appendleft((datetime.now().strftime("%H:%M:%S"), text, wake))
        self._wake.set()

    def set_state(self, s):
        with self._lock:
            self.state = s
        self._wake.set()

    def set_say(self, s):
        with self._lock:
            self.last_say = s
        self._wake.set()

    def set_mqtt(self, ok):
        with self._lock:
            self.mqtt_ok = ok
        self._wake.set()

    # -- сторона поллера (кэш) -- #
    def set_poll(self, services, silent, vol, sched, brk, phone):
        with self._lock:
            self._services = services
            self._silent = silent
            self._vol = vol
            self._sched = sched
            self._break = brk
            self._phone = phone
        self._wake.set()

    # -- сторона рендера -- #
    def snapshot(self):
        with self._lock:
            return {
                "phrases": list(self.phrases),
                "state": self.state,
                "last_say": self.last_say,
                "mqtt_ok": self.mqtt_ok,
                "services": list(self._services),
                "silent": self._silent,
                "vol": self._vol,
                "sched": self._sched,
                "break": self._break,
                "phone": self._phone,
            }

    def wait(self, timeout: float) -> bool:
        """Дождаться события или таймаута; сбросить флаг. True — разбудило событие."""
        woke = self._wake.wait(timeout)
        self._wake.clear()
        return woke


# --------------------------------------------------------------------------- #
# MQTT-подписка на эфир/состояние (мгновенно будит рендер)
# --------------------------------------------------------------------------- #
def _connect_mqtt(state: _State):
    import paho.mqtt.client as mqtt

    def on_connect(client, userdata, flags, reason_code, properties=None):
        state.set_mqtt(True)
        for topic in (contracts.TOPIC_INPUT, contracts.TOPIC_STATE, contracts.TOPIC_SAY):
            client.subscribe(topic)

    def on_disconnect(client, userdata, *a):
        state.set_mqtt(False)

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

    # client_id с PID: два параллельных (или зависший + новый) `jarvis live` не выбивают
    # друг друга с брокера по совпадению id — как это делают сервисы в bus.py.
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"jarvis-live-{os.getpid()}")
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.connect(config.MQTT_HOST, config.MQTT_PORT, 5)
    client.loop_start()
    return client


# --------------------------------------------------------------------------- #
# Фоновый поллер: весь дорогой I/O — вне потока рендера
# --------------------------------------------------------------------------- #
def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def _poll_services():
    """Статус всех юнитов через `systemctl --user is-active`. Любой сбой юнита → неактивен."""
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
    return out


def _poll_volume():
    """Громкость строкой (через wpctl). Выключен → «выкл», сбой → «?»."""
    from jarvis.sysinfo import read_volume

    try:
        v = read_volume()
        if v.get("выключен"):
            return "выкл"
        if "ошибка" in v:
            return "?"
        return f"{v.get('громкость_процент')}%"
    except Exception:
        return "?"


def _read_state_json(name):
    """Прочитать logs/<name>.json (его пишет сервис). Нет файла/сбой → None."""
    path = os.path.join(str(config.LOGS_DIR), name)
    try:
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _poll_loop(state: _State, stop: threading.Event):
    """Фоновое обновление кэша. systemctl дёргаем редко (LIVE_STATUS_TTL), остальное —
    каждый цикл (локальные чтения дёшевы). Никогда не падает: всё под _safe/try."""
    from jarvis import alarms, silence

    services, services_at = [], 0.0
    interval = max(0.5, float(getattr(config, "LIVE_REFRESH_SECONDS", 1.0)))
    while not stop.is_set():
        try:
            now_m = time.monotonic()
            if not services or (now_m - services_at) >= float(config.LIVE_STATUS_TTL):
                services = _poll_services()
                services_at = now_m
            state.set_poll(
                services,
                _safe(silence.is_silent, False),
                _poll_volume(),
                _safe(alarms.read_schedule, {}),
                _read_state_json(_ACTIVITY_STATE),
                _read_state_json(_PHONE_STATE),
            )
        except Exception:
            _log.debug("Сбой поллера live", exc_info=True)
        stop.wait(interval)


# --------------------------------------------------------------------------- #
# Форматирование (чистые функции: сырьё + текущее время → строки)
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


def _schedule_lines(sched: dict, now: datetime, horizon_hours: int):
    """Строки расписания из уже прочитанного dict + живое время (счётчики тикают каждый кадр)."""
    sched = sched or {}
    lines = []
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


def _phone_lines(phone):
    """Строки телефона из уже прочитанного dict (его пишет сервис phone). None → не подключён."""
    if not phone:
        return ["  (телефон не подключён)"]
    lines = []
    try:
        status = phone.get("status", "offline")
        lines.append(f"  статус: {'🟢 на связи' if status == 'online' else '⚪ офлайн'}")
        bat = phone.get("battery")
        if isinstance(bat, (int, float)):
            chrg = " (заряжается)" if phone.get("charging") else ""
            lines.append(f"  заряд: {bat}%{chrg}")
        pres = phone.get("presence")
        if pres:
            lines.append(f"  присутствие: {'дома' if pres == 'home' else 'нет дома'}")
    except Exception:
        pass
    return lines or ["  (нет данных)"]


def _break_lines(brk, now: datetime):
    """Строки детектора перерывов из dict + живое время. None → детектор не активен."""
    if not brk:
        return ["  (детектор перерывов не активен)"]
    lines = []
    try:
        if brk.get("on_break"):
            lines.append("  🟢 идёт перерыв")
        ws = brk.get("working_seconds")
        if isinstance(ws, (int, float)):
            lines.append(f"  работает: {_fmt_duration(ws)}")
        us = brk.get("until_offer_seconds")
        if isinstance(us, (int, float)):
            lines.append(f"  напомнит о перерыве через: {_fmt_duration(us)}" if us > 0
                         else "  напоминание о перерыве — скоро")
        updated = _parse_iso(brk.get("updated_at"))
        if updated and (now - updated).total_seconds() > 120:
            lines.append("  (данные устарели — монитор не обновляет)")
    except Exception:
        pass
    return lines or ["  (нет данных)"]


def _mode_label(silent: bool, vol: str):
    """Метка режима и её цвет: тишина — жёлтым, голос — зелёным."""
    if silent:
        return "ТИШИНА", "yellow"
    return f"голос · {vol}", "green"


def _air_title(cur_state: str, silent: bool, vol: str):
    """Заголовок «Эфира»: состояние и режим — цвет на данных, рамка остаётся серой."""
    from rich.text import Text

    state_color = _STATE_COLOR.get(cur_state, "white")
    mode, mode_color = _mode_label(silent, vol)
    return Text.assemble(
        ("Эфир", _TITLE),
        ("  ·  ", "dim"), (cur_state, state_color),
        ("  ·  ", "dim"), (mode, mode_color),
    )


def _render(state: _State):
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    now = datetime.now()
    snap = state.snapshot()
    cur_state = snap["state"]

    # 1. Эфир — состояние/режим в заголовке, живые часы в углу, тело: реплика + фразы.
    air = Table.grid(padding=(0, 1))
    air.add_column()
    if snap["last_say"]:
        air.add_row(Text(f"реплика: «{snap['last_say'][:70]}»", style="cyan dim"))
    air.add_row(Text("слышу:", style="dim"))
    if snap["phrases"]:
        for ts, text, wake in snap["phrases"]:
            tag = "" if wake else "  ·без обращения"
            air.add_row(Text(f"  {ts}  {text}{tag}", style="white" if wake else "dim"))
    else:
        air.add_row(Text("  (тишина в эфире)", style="dim"))

    # 2. Сервисы — сетка по 4 в ряд + MQTT. Зелёный/красный несёт сам статус.
    svc = Table.grid(padding=(0, 2))
    for _ in range(4):
        svc.add_column()
    cells = []
    for unit, active in snap["services"]:
        name = _SHORT_NAME.get(unit, unit)
        color = "green" if active else "red"
        cells.append(Text.assemble(("● ", color), (name, "white" if active else "red")))
    for i in range(0, len(cells), 4):
        svc.add_row(*cells[i:i + 4])
    mqtt_ok = snap["mqtt_ok"]
    svc.add_row(Text.assemble(("● ", "green" if mqtt_ok else "red"),
                              ("MQTT", "white" if mqtt_ok else "red")))

    # 3. Расписание · 3 дня.
    sched = Text("\n".join(_schedule_lines(snap["sched"], now, config.LIVE_HORIZON_HOURS)))

    # 4. Окружение — перерыв и телефон в одной панели, с тусклыми мини-заголовками.
    env = Text()
    env.append("перерыв\n", style="dim")
    for line in _break_lines(snap["break"], now):
        env.append(line + "\n")
    env.append("телефон\n", style="dim")
    phone_lines = _phone_lines(snap["phone"])
    for i, line in enumerate(phone_lines):
        env.append(line + ("\n" if i < len(phone_lines) - 1 else ""))

    clock = Text(now.strftime("%H:%M:%S"), style="dim")
    return Group(
        Panel(air, title=_air_title(cur_state, snap["silent"], snap["vol"]),
              title_align="left", subtitle=clock, subtitle_align="right",
              border_style=_BORDER),
        Panel(svc, title=Text("Сервисы", style=_TITLE),
              title_align="left", border_style=_BORDER),
        Panel(sched, title=Text("Расписание · 3 дня", style=_TITLE),
              title_align="left", border_style=_BORDER),
        Panel(env, title=Text("Окружение", style=_TITLE),
              title_align="left", border_style=_BORDER),
    )


def run() -> int:
    """Запустить живую панель до Ctrl+C. Возвращает 0.

    Событийный цикл: рисуем кадр, затем ждём либо события MQTT (мгновенная фраза/состояние),
    либо границы следующей секунды (чёткие часы и счётчики). Весь I/O — в потоке-поллере."""
    from rich.live import Live

    state = _State()
    stop = threading.Event()
    client = None
    try:
        client = _connect_mqtt(state)
    except Exception as exc:
        print(f"Не удалось подключиться к MQTT: {exc}")

    poller = threading.Thread(target=_poll_loop, args=(state, stop),
                              daemon=True, name="live-poll")
    poller.start()

    try:
        with Live(_render(state), screen=True, auto_refresh=False) as live:
            while not stop.is_set():
                live.update(_render(state), refresh=True)
                # До границы следующей секунды — часы и счётчики идут чётко; событие MQTT
                # будит раньше (мгновенная фраза/состояние).
                state.wait(1.0 - (time.time() % 1.0))
    except KeyboardInterrupt:
        pass
    except Exception:
        _log.debug("Панель завершилась с ошибкой", exc_info=True)
    finally:
        stop.set()
        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
    print("Панель закрыта, сэр.")
    return 0
