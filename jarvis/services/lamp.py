"""«Свет» Джарвиса: умная Wi-Fi лампа Tuya ЛОКАЛЬНО (tinytuya, без облака) + реакции на события.

Изолированный узел (наследник JarvisModule). Держит персистентный сокет к лампе; команды/реакции
идут через ОДИН поток-воркер с очередью (MQTT-колбэки не блокируются Wi-Fi-задержкой). Лампа
недоступна (выкл/не в сети/неверный ключ/версия) → лог (+опц. уведомление), сервис ЖИВ, команды
тихо пропускаются до переподключения (backoff).

Реакции (всё настраиваемо в settings.yaml): старт (готовность), озвучка (мягкое свечение пока
говорит → возврат в фон), срабатывание будильника/таймера/напоминания (по `min_volume` в jarvis/say
— заметная реакция). Опц.: тишина/перерыв/ошибка. После реакции — ВОЗВРАТ в фоновое состояние.
"""
import json
import logging
import os
import queue
import socket
import threading
import time
from datetime import datetime

from jarvis import config, contracts, phrases
from jarvis import lamp as helpers
from jarvis.bus import JarvisModule

_RESTORE_DELAY = 0.8   # debounce: пауза перед возвратом в фон после конца речи (частые реплики не дёргают)
_DEFAULT_RGB = (255, 170, 87)
_STATE_FILE = "lamp_state.json"   # снимок для doctor/live: Tuya держит 1 локальный сокет,
#                                 # отдельная проба при живом сервисе рвала бы соединение
_TUYA_PORT = 6668                 # локальный TCP-порт Tuya-устройств
# Паузы между попытками подключения: первая — сразу, дальше быстрый рост до потолка
# lamp.reconnect_seconds. После ребута системы сеть поднимается ПОЗЖЕ сервиса — быстрый
# старт цикла подхватывает лампу через секунды после появления Wi-Fi (корень бага Этапа 21).
_BACKOFF_STEPS = (2.0, 4.0, 8.0, 15.0)


class LampModule(JarvisModule):
    """«Свет»: управление лампой Tuya и реакции на события Джарвиса."""

    def __init__(self):
        super().__init__("jarvis-lamp")
        self._bulb = None
        self._connected = False
        self._ever_connected = False                # был ли хоть один успешный коннект (для фразы лога)
        self._fail_logged = False                   # WARNING о недоступности — раз на эпизод, не на попытку
        self._last_io_ok = 0.0                      # monotonic последнего успешного I/O (гейт keepalive)
        self._color_ok = True                       # сбрасывается, если set_colour не поддержан
        self._lamp_lock = threading.Lock()          # сериализация I/O с лампой
        self._reconnect_lock = threading.Lock()     # одиночность reconnect-потока
        self._queue: "queue.Queue" = queue.Queue()
        self._worker = None
        self._reconnect_thread = None
        self._keepalive_thread = None
        self._restore_timer = None
        self._speaking = False
        # Фоновое (желаемое устойчивое) состояние — из конфига; меняется голосом, в него возвращаемся.
        bg = config.LAMP_BACKGROUND or {}
        self._bg_on = bool(bg.get("вкл", True))
        self._bg_rgb = helpers.resolve_color(bg.get("цвет"), config.LAMP_COLORS) or _DEFAULT_RGB
        self._bg_bright = helpers.clamp_pct(bg.get("яркость"), 60)
        self._bg_mode = "colour"               # 'colour' (rgb) | 'white' (яркость+температура)
        self._bg_temp = 40                      # температура белого (% — 0 тёплый, 100 холодный)

    # ------------------------------------------------------------------ #
    # Жизненный цикл
    # ------------------------------------------------------------------ #
    def on_start(self):
        if not config.LAMP_ENABLED:
            self.log.info("Лампа выключена (lamp.enabled=false) — сервис простаивает")
            return
        self._worker = threading.Thread(target=self._run_worker, daemon=True, name="lamp-worker")
        self._worker.start()
        self.subscribe(contracts.TOPIC_STATE, self.on_state)   # озвучка (speaking/idle)
        self.subscribe(contracts.TOPIC_SAY, self.on_say)       # срабатывания (min_volume)
        self.subscribe(contracts.TOPIC_LAMP, self.on_lamp)     # голос-команды лампой
        self.subscribe(contracts.TOPIC_PHONE_CALL, self.on_call)  # реакция на входящий звонок (ТЗ-9)
        # Подключение в фоне (не задерживаем старт сервиса): живучий цикл с быстрым backoff —
        # после ребута дожидается сети и сам поднимает связь и свечение.
        self._schedule_reconnect()
        if float(config.LAMP_KEEPALIVE_MINUTES) > 0:
            self._keepalive_thread = threading.Thread(
                target=self._keepalive_loop, daemon=True, name="lamp-keepalive")
            self._keepalive_thread.start()

    def on_stop(self):
        self._cancel_restore()
        self._write_state()

    # ------------------------------------------------------------------ #
    # Соединение / переподключение
    # ------------------------------------------------------------------ #
    def _tcp_probe(self, ip: str, timeout: float = 1.0) -> bool:
        """Дешёвая проверка достижимости лампы (TCP-порт Tuya) ПЕРЕД полным коннектом:
        сеть/лампа ещё не готовы → быстрый тихий фейл за ~1с вместо блокировки в tinytuya.
        Зовётся только когда НЕ подключены — персистентному сокету не мешает."""
        try:
            sock = socket.create_connection((ip, _TUYA_PORT), timeout=timeout)
            sock.close()
            return True
        except OSError:
            return False

    def _note_unreachable(self, detail: str):
        """Лог о недоступности: WARNING один раз на эпизод (ретраи теперь частые —
        не спамим), дальше DEBUG. Уведомление (если включено) — тоже раз на эпизод."""
        if self._fail_logged:
            self.log.debug("Лампа всё ещё недоступна: %s", detail)
            return
        self._fail_logged = True
        self.log.warning("Лампа недоступна (%s) — продолжаю попытки в фоне", detail)
        if config.LAMP_NOTIFY_UNAVAILABLE:
            try:
                self.notify("Джарвис", "Лампа сейчас не в сети.", urgency="low")
            except Exception:
                pass

    def _connect(self) -> bool:
        if not (config.LAMP_DEVICE_ID and config.LAMP_LOCAL_KEY):
            self.log.warning("Креды лампы не заданы (lamp.device_id/local_key) — лампа не подключена")
            return False
        try:
            import tinytuya
        except Exception:
            self.log.error("tinytuya не установлен — лампа недоступна (выполните `pip install -e .`)")
            return False
        ip = config.LAMP_IP or self._discover_ip(tinytuya)
        if not ip:
            self._note_unreachable("IP не задан и не найден автопоиском")
            return False
        if not self._tcp_probe(ip):
            self._note_unreachable(f"{ip}:{_TUYA_PORT} не отвечает — сеть/лампа ещё не готовы")
            return False
        try:
            with self._lamp_lock:
                bulb = tinytuya.BulbDevice(
                    config.LAMP_DEVICE_ID, address=ip, local_key=config.LAMP_LOCAL_KEY,
                    version=float(config.LAMP_VERSION), persist=True)
                bulb.set_socketTimeout(float(config.LAMP_SOCKET_TIMEOUT))
                # Внутренние ретраи tinytuya (дефолт 5×(5с+5с)) превращали ОДИН фейл в ~36-50с
                # блокировки — из-за этого лампа «умирала» после ребута. Темп задаёт НАШ цикл.
                bulb.set_socketRetryLimit(1)
                bulb.set_socketRetryDelay(1)
                bulb.set_socketPersistent(True)
                st = bulb.status()
                if not isinstance(st, dict) or "Error" in st or "Err" in st:
                    raise RuntimeError(f"status вернул {st}")
                self._bulb = bulb
                self._connected = True
            self._fail_logged = False
            self._last_io_ok = time.monotonic()
            self.log.info("Лампа подключена: %s (протокол %s)", ip, config.LAMP_VERSION)
            self._write_state()
            return True
        except Exception as exc:
            self._connected = False
            self._note_unreachable(f"{ip}, v{config.LAMP_VERSION}: {exc}")
            return False

    def _discover_ip(self, tinytuya) -> str:
        """Автопоиск IP по device_id (если ip пуст). Скан сети ~несколько секунд; best-effort."""
        if not config.LAMP_AUTODISCOVER:
            return ""
        try:
            self.log.info("Ищу лампу в сети по device_id…")
            devices = tinytuya.deviceScan(False, 5) or {}
            for ip, info in devices.items():
                if config.LAMP_DEVICE_ID in (info.get("gwId"), info.get("id")):
                    found = info.get("ip", ip)
                    self.log.info("Лампа найдена автопоиском: %s", found)
                    return found
        except Exception:
            self.log.debug("Автопоиск лампы не удался", exc_info=True)
        return ""

    def _schedule_reconnect(self):
        with self._reconnect_lock:
            if self._reconnect_thread and self._reconnect_thread.is_alive():
                return
            self._reconnect_thread = threading.Thread(
                target=self._connect_loop, daemon=True, name="lamp-reconnect")
            self._reconnect_thread.start()

    def _connect_loop(self):
        """Живучий цикл (пере)подключения: первая попытка СРАЗУ, затем быстрый backoff
        (2→4→8→15с) до потолка lamp.reconnect_seconds — бесконечно, пока не подключимся.
        Любое исключение попытки не убивает цикл. На успехе — реакция startup («я жив»)
        и возврат в фоновое свечение."""
        attempt = 0
        while not self._stop_event.is_set() and not self._connected:
            ok = False
            try:
                ok = self._connect()
            except Exception:
                self.log_exc(logging.WARNING, "Неожиданный сбой попытки подключения к лампе")
            if ok:
                if self._ever_connected:
                    self.log.info("Лампа снова на связи — реакции восстановлены")
                self._ever_connected = True
                spec = helpers.reaction("startup", config.LAMP_REACTIONS, config.LAMP_COLORS)
                self._enqueue(lambda: self._do_reaction(spec) if spec else self._apply_background())
                return
            cap = max(5.0, float(config.LAMP_RECONNECT_SECONDS))
            delay = _BACKOFF_STEPS[attempt] if attempt < len(_BACKOFF_STEPS) else cap
            attempt += 1
            if self._stop_event.wait(min(delay, cap)):
                return

    # ------------------------------------------------------------------ #
    # Keepalive: ловим молча умерший персистентный сокет (лампу выключали,
    # Wi-Fi моргнул) — иначе первая реакция после долгой паузы терялась бы.
    # ------------------------------------------------------------------ #
    def _keepalive_loop(self):
        interval = max(60.0, float(config.LAMP_KEEPALIVE_MINUTES) * 60.0)
        while not self._stop_event.wait(interval):
            if not self._connected:
                continue
            if time.monotonic() - self._last_io_ok < interval * 0.5:
                self._write_state()      # I/O было недавно — пинг не нужен, но снимок освежаем
                continue
            self._enqueue(self._ping)

    def _ping(self):
        """Лёгкий status-пинг через воркер (сериализован, nowait=False — сокет не десинхронится).
        Сбой → исключение → воркер вызовет _on_io_error → мгновенный реконнект."""
        if not self._connected or self._bulb is None:
            return
        with self._lamp_lock:
            st = self._bulb.status()
        if not isinstance(st, dict) or "Error" in st or "Err" in st:
            raise RuntimeError(f"keepalive: status вернул {st}")
        self._last_io_ok = time.monotonic()
        self._write_state()

    def _write_state(self):
        """Снимок состояния лампы (logs/lamp_state.json, атомарно). Его читает doctor:
        Tuya держит ОДИН локальный сокет — отдельная проба при живом сервисе рвала бы
        соединение сервиса (та же причина — предупреждение в `jarvis lamp`)."""
        try:
            data = {
                "connected": bool(self._connected),
                "ip": config.LAMP_IP,
                "version": config.LAMP_VERSION,
                "keepalive_minutes": float(config.LAMP_KEEPALIVE_MINUTES),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            path = os.path.join(str(config.LOGS_DIR), _STATE_FILE)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            self.log.debug("Не удалось записать lamp_state", exc_info=True)

    # ------------------------------------------------------------------ #
    # Воркер очереди команд (один поток — сериализует обращения к лампе)
    # ------------------------------------------------------------------ #
    def _run_worker(self):
        while not self._stop_event.is_set():
            try:
                fn = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if fn is None:
                continue
            try:
                fn()
            except Exception:
                self._on_io_error()

    def _enqueue(self, fn):
        try:
            self._queue.put_nowait(fn)
        except Exception:
            self.log.debug("Очередь лампы переполнена — команда отброшена", exc_info=True)

    def _on_io_error(self):
        self.log_exc(logging.WARNING, "Сбой обращения к лампе — помечаю недоступной, переподключусь")
        self._connected = False
        self._write_state()
        self._schedule_reconnect()

    # ------------------------------------------------------------------ #
    # Низкоуровневые операции — НАПРЯМУЮ ПО DPS (сверено на лампе v3.5; см. CLAUDE.md/ГРАБЛИ)
    # DP20 switch · DP21 work_mode(colour/white) · DP22 bright(10–1000) · DP23 temp(0–1000) ·
    # DP24 colour_data_v2 (HSV-hex). В режиме colour яркость = V в DP24 (DP22 НЕ слать — собьёт в white).
    # ------------------------------------------------------------------ #
    def _dps(self, data: dict):
        """Атомарно записать DPS (set_multiple_values). nowait=False: на ПЕРСИСТЕНТНОМ сокете ждём
        ответ — иначе непрочитанные reply копятся и десинхронят сокет (сверено: status читал чужой
        ответ, «выкл» не отражался). Воркер сериализует, MQTT не блокируется. Ошибка → реконнект."""
        if not self._connected or self._bulb is None:
            return
        with self._lamp_lock:
            self._bulb.set_multiple_values(data, nowait=False)
        self._last_io_ok = time.monotonic()

    def _set_color(self, rgb, brightness):
        """Цвет (режим colour): тон/насыщенность из RGB, яркость — в V компоненте DP24 (без DP22)."""
        rgb = rgb or self._bg_rgb
        self._dps({20: True, 21: "colour",
                   24: helpers.rgb_to_v2hex(rgb[0], rgb[1], rgb[2], helpers.clamp_pct(brightness, 60))})

    def _set_white(self, brightness, temp_pct):
        """Белый (режим white): яркость DP22 + температура DP23 (0 тёплый ↔ 1000 холодный)."""
        self._dps({20: True, 21: "white",
                   22: helpers.pct_to_dp(helpers.clamp_pct(brightness, 60), lo=10),
                   23: helpers.pct_to_dp(temp_pct, lo=0)})

    def _set_brightness(self, brightness):
        """Изменить яркость в ТЕКУЩЕМ режиме (colour → перекодировать V; white → DP22)."""
        if self._bg_mode == "white":
            self._dps({20: True, 22: helpers.pct_to_dp(helpers.clamp_pct(brightness, 60), lo=10)})
        else:
            self._set_color(self._bg_rgb, brightness)

    def _turn_off(self):
        if not self._connected or self._bulb is None:
            return
        with self._lamp_lock:
            self._bulb.set_value(20, False, nowait=False)
        self._last_io_ok = time.monotonic()

    def _apply_background(self):
        """Вернуть лампу в фоновое (желаемое) состояние (по запомненному режиму)."""
        if not self._bg_on:
            self._turn_off()
        elif self._bg_mode == "white":
            self._set_white(self._bg_bright, self._bg_temp)
        else:
            self._set_color(self._bg_rgb, self._bg_bright)

    def _ramp(self, frm, to, steps=4, dt=0.12):
        """Плавный переход яркости (для паттерна «пульс»)."""
        frm, to = helpers.clamp_pct(frm, 30), helpers.clamp_pct(to, 80)
        for i in range(1, steps + 1):
            if self._stop_event.is_set():
                return
            self._set_brightness(round(frm + (to - frm) * i / steps))
            time.sleep(dt)

    def _do_reaction(self, spec):
        """Выполнить реакцию (паттерн) и ВЕРНУТЬ фон. Блокирует воркер на свою (краткую) длительность."""
        if spec is None or not self._connected:
            return
        rgb = spec.get("rgb") or self._bg_rgb
        br = spec.get("brightness", 70)
        pattern = spec.get("pattern", "свечение")
        dur = spec.get("duration", 0.0)
        reps = spec.get("repeats", 1)
        try:
            if pattern == "мигание":
                half = max(0.12, (dur / max(1, reps)) / 2) if dur else 0.2
                for _ in range(reps):
                    if self._stop_event.is_set():
                        break
                    self._set_color(rgb, br)
                    time.sleep(half)
                    self._set_brightness(1)
                    time.sleep(half)
            elif pattern == "пульс":
                low = max(5, br // 4)
                self._set_color(rgb, low)
                for _ in range(reps):
                    if self._stop_event.is_set():
                        break
                    self._ramp(low, br)
                    self._ramp(br, low)
            else:  # свечение — ровно
                self._set_color(rgb, br)
                if dur > 0:
                    self._stop_event.wait(dur)
        finally:
            self._apply_background()

    # ------------------------------------------------------------------ #
    # Реакции на события Джарвиса
    # ------------------------------------------------------------------ #
    def on_state(self, payload: dict):
        """Озвучка: speaking → мягкое свечение; idle → возврат в фон (с debounce)."""
        try:
            st = payload.get("state")
            if st == contracts.STATE_SPEAKING:
                self._speaking = True
                self._cancel_restore()
                spec = helpers.reaction("speaking", config.LAMP_REACTIONS, config.LAMP_COLORS)
                if spec:
                    self._enqueue(lambda: self._set_color(spec["rgb"] or self._bg_rgb, spec["brightness"]))
            elif self._speaking:
                self._speaking = False
                self._schedule_restore()
        except Exception:
            self.log.debug("Сбой реакции на состояние", exc_info=True)

    def on_say(self, payload: dict):
        """Срабатывание (будильник/таймер/напоминание помечены min_volume) → заметная реакция."""
        try:
            if payload.get("min_volume") is None and not payload.get("critical"):
                return
            spec = helpers.reaction("firing", config.LAMP_REACTIONS, config.LAMP_COLORS)
            if spec:
                self._cancel_restore()
                self._enqueue(lambda: self._do_reaction(spec))
        except Exception:
            self.log.debug("Сбой реакции на срабатывание", exc_info=True)

    def _ack(self, action):
        """Озвучить результат голосовой команды лампой (паки из settings.yaml)."""
        packs = {
            "вкл": ("lamp.on", config.LAMP_ON_ACK), "выкл": ("lamp.off", config.LAMP_OFF_ACK),
            "цвет": ("lamp.color", config.LAMP_COLOR_ACK),
            "ярче": ("lamp.bright", config.LAMP_BRIGHT_ACK), "темнее": ("lamp.bright", config.LAMP_BRIGHT_ACK),
            "тепло": ("lamp.temp", config.LAMP_TEMP_ACK), "холод": ("lamp.temp", config.LAMP_TEMP_ACK),
            "авто": ("lamp.auto", config.LAMP_AUTO_ACK),
        }
        key_pack = packs.get(action)
        if key_pack:
            try:
                self.say(phrases.pick(key_pack[0], key_pack[1]))
            except Exception:
                self.log.debug("Не удалось озвучить ответ лампы", exc_info=True)

    def on_lamp(self, payload: dict):
        """Голосовая команда лампой (core форвардит поле «лампа» команды): вкл/выкл/цвет/ярче/темнее/авто.
        Команда меняет ФОНОВОЕ состояние (в него возвращаются реакции). Лампа сама ОЗВУЧИВАЕТ результат:
        успех — пак действия; не в сети — пак «недоступна» (единый источник ответа, core молчит)."""
        try:
            action = (payload or {}).get("действие")
            if not action:
                return
            if not self._connected:
                self.say(phrases.pick("lamp.unavailable", config.LAMP_UNAVAILABLE))
                return
            if action == "вкл":
                self._bg_on = True
                self._enqueue(self._apply_background)
            elif action == "выкл":
                self._bg_on = False
                self._enqueue(self._turn_off)
            elif action == "цвет":
                rgb = helpers.resolve_color(payload.get("цвет"), config.LAMP_COLORS)
                if rgb:
                    self._bg_rgb = rgb
                    self._bg_mode = "colour"
                    self._bg_on = True
                    self._enqueue(self._apply_background)
            elif action == "ярче":
                self._bg_bright = min(100, self._bg_bright + int(config.LAMP_BRIGHTNESS_STEP))
                self._bg_on = True
                self._enqueue(self._apply_background)
            elif action == "темнее":
                self._bg_bright = max(5, self._bg_bright - int(config.LAMP_BRIGHTNESS_STEP))
                self._enqueue(self._apply_background)
            elif action in ("тепло", "холод"):
                # Белый режим: тёплый = меньше DP23, холодный = больше. Шаг temp_step (%).
                step = int(config.LAMP_TEMP_STEP)
                self._bg_temp = (max(0, self._bg_temp - step) if action == "тепло"
                                 else min(100, self._bg_temp + step))
                self._bg_mode = "white"
                self._bg_on = True
                self._enqueue(self._apply_background)
            elif action == "авто":
                self._reset_background()
                self._enqueue(self._apply_background)
            else:
                return
            self._ack(action)
        except Exception:
            self.log.debug("Сбой обработки команды лампы", exc_info=True)

    def on_call(self, payload: dict):
        """Реакция лампы на ВХОДЯЩИЙ звонок (ТЗ-9): incoming → реакция call (приоритет), затем фон."""
        try:
            if (payload or {}).get("type") != "incoming":
                return
            spec = helpers.reaction("call", config.LAMP_REACTIONS, config.LAMP_COLORS)
            if spec:
                self._cancel_restore()
                self._enqueue(lambda: self._do_reaction(spec))
        except Exception:
            self.log.debug("Сбой реакции на звонок", exc_info=True)

    def _reset_background(self):
        bg = config.LAMP_BACKGROUND or {}
        self._bg_on = bool(bg.get("вкл", True))
        self._bg_rgb = helpers.resolve_color(bg.get("цвет"), config.LAMP_COLORS) or _DEFAULT_RGB
        self._bg_bright = helpers.clamp_pct(bg.get("яркость"), 60)
        self._bg_mode = "colour"
        self._bg_temp = 40

    # --- debounce возврата в фон после речи ---
    def _schedule_restore(self):
        self._cancel_restore()
        try:
            self._restore_timer = threading.Timer(_RESTORE_DELAY,
                                                  lambda: self._enqueue(self._apply_background))
            self._restore_timer.daemon = True
            self._restore_timer.start()
        except Exception:
            self._enqueue(self._apply_background)

    def _cancel_restore(self):
        t = self._restore_timer
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass
            self._restore_timer = None


def main():
    LampModule().run()


if __name__ == "__main__":
    main()
