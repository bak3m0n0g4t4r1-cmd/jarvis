"""«Свет» Джарвиса: умная Wi-Fi лампа Tuya ЛОКАЛЬНО (tinytuya, без облака) + реакции на события.

Изолированный узел (наследник JarvisModule). Держит персистентный сокет к лампе; команды/реакции
идут через ОДИН поток-воркер с очередью (MQTT-колбэки не блокируются Wi-Fi-задержкой). Лампа
недоступна (выкл/не в сети/неверный ключ/версия) → лог (+опц. уведомление), сервис ЖИВ, команды
тихо пропускаются до переподключения (backoff).

Реакции (всё настраиваемо в settings.yaml): старт (готовность), озвучка (мягкое свечение пока
говорит → возврат в фон), срабатывание будильника/таймера/напоминания (по `min_volume` в jarvis/say
— заметная реакция). Опц.: тишина/перерыв/ошибка. После реакции — ВОЗВРАТ в фоновое состояние.
"""
import logging
import queue
import threading
import time

from jarvis import config, contracts, phrases
from jarvis import lamp as helpers
from jarvis.bus import JarvisModule

_RESTORE_DELAY = 0.8   # debounce: пауза перед возвратом в фон после конца речи (частые реплики не дёргают)
_DEFAULT_RGB = (255, 170, 87)


class LampModule(JarvisModule):
    """«Свет»: управление лампой Tuya и реакции на события Джарвиса."""

    def __init__(self):
        super().__init__("jarvis-lamp")
        self._bulb = None
        self._connected = False
        self._color_ok = True                       # сбрасывается, если set_colour не поддержан
        self._lamp_lock = threading.Lock()          # сериализация I/O с лампой
        self._queue: "queue.Queue" = queue.Queue()
        self._worker = None
        self._reconnect_thread = None
        self._restore_timer = None
        self._speaking = False
        # Фоновое (желаемое устойчивое) состояние — из конфига; меняется голосом, в него возвращаемся.
        bg = config.LAMP_BACKGROUND or {}
        self._bg_on = bool(bg.get("вкл", True))
        self._bg_rgb = helpers.resolve_color(bg.get("цвет"), config.LAMP_COLORS) or _DEFAULT_RGB
        self._bg_bright = helpers.clamp_pct(bg.get("яркость"), 60)

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
        # Подключение в фоне — не задерживаем старт сервиса.
        threading.Thread(target=self._connect_and_init, daemon=True, name="lamp-connect").start()

    def on_stop(self):
        self._cancel_restore()

    # ------------------------------------------------------------------ #
    # Соединение / переподключение
    # ------------------------------------------------------------------ #
    def _connect_and_init(self):
        if self._connect():
            spec = helpers.reaction("startup", config.LAMP_REACTIONS, config.LAMP_COLORS)
            self._enqueue(lambda: self._do_reaction(spec) if spec else self._apply_background())
        else:
            self._schedule_reconnect()

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
            self.log.warning("IP лампы не задан и не найден автопоиском — лампа не подключена")
            return False
        try:
            with self._lamp_lock:
                bulb = tinytuya.BulbDevice(
                    config.LAMP_DEVICE_ID, address=ip, local_key=config.LAMP_LOCAL_KEY,
                    version=float(config.LAMP_VERSION), persist=True)
                bulb.set_socketTimeout(float(config.LAMP_SOCKET_TIMEOUT))
                bulb.set_socketPersistent(True)
                st = bulb.status()
                if not isinstance(st, dict) or "Error" in st or "Err" in st:
                    raise RuntimeError(f"status вернул {st}")
                self._bulb = bulb
                self._connected = True
            self.log.info("Лампа подключена: %s (протокол %s)", ip, config.LAMP_VERSION)
            return True
        except Exception as exc:
            self.log.warning("Лампа не отвечает (%s, v%s): %s — Джарвис продолжает без неё",
                             ip, config.LAMP_VERSION, exc)
            self._connected = False
            if config.LAMP_NOTIFY_UNAVAILABLE:
                try:
                    self.notify("Джарвис", "Лампа сейчас не в сети.", urgency="low")
                except Exception:
                    pass
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
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True, name="lamp-reconnect")
        self._reconnect_thread.start()

    def _reconnect_loop(self):
        while not self._stop_event.is_set() and not self._connected:
            if self._stop_event.wait(max(5, float(config.LAMP_RECONNECT_SECONDS))):
                return
            if self._connect():
                self._enqueue(self._apply_background)
                return

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
        self._schedule_reconnect()

    # ------------------------------------------------------------------ #
    # Низкоуровневые операции (выполняются в воркере, под lamp_lock)
    # ------------------------------------------------------------------ #
    def _set_color(self, rgb, brightness):
        if not self._connected or self._bulb is None:
            return
        with self._lamp_lock:
            b = self._bulb
            b.turn_on(nowait=True)
            if rgb and self._color_ok:
                try:
                    b.set_colour(int(rgb[0]), int(rgb[1]), int(rgb[2]), nowait=True)
                except Exception:
                    self._color_ok = False
                    self.log.info("Лампа без RGB? — перехожу на белый/яркость")
            b.set_brightness_percentage(helpers.clamp_pct(brightness, 60), nowait=True)

    def _set_brightness(self, brightness):
        if not self._connected or self._bulb is None:
            return
        with self._lamp_lock:
            self._bulb.set_brightness_percentage(helpers.clamp_pct(brightness, 60), nowait=True)

    def _turn_off(self):
        if not self._connected or self._bulb is None:
            return
        with self._lamp_lock:
            self._bulb.turn_off(nowait=True)

    def _apply_background(self):
        """Вернуть лампу в фоновое (желаемое) состояние."""
        if self._bg_on:
            self._set_color(self._bg_rgb, self._bg_bright)
        else:
            self._turn_off()

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
                    self._bg_on = True
                    self._enqueue(self._apply_background)
            elif action == "ярче":
                self._bg_bright = min(100, self._bg_bright + int(config.LAMP_BRIGHTNESS_STEP))
                self._bg_on = True
                self._enqueue(self._apply_background)
            elif action == "темнее":
                self._bg_bright = max(5, self._bg_bright - int(config.LAMP_BRIGHTNESS_STEP))
                self._enqueue(self._apply_background)
            elif action == "авто":
                self._reset_background()
                self._enqueue(self._apply_background)
            else:
                return
            self._ack(action)
        except Exception:
            self.log.debug("Сбой обработки команды лампы", exc_info=True)

    def _reset_background(self):
        bg = config.LAMP_BACKGROUND or {}
        self._bg_on = bool(bg.get("вкл", True))
        self._bg_rgb = helpers.resolve_color(bg.get("цвет"), config.LAMP_COLORS) or _DEFAULT_RGB
        self._bg_bright = helpers.clamp_pct(bg.get("яркость"), 60)

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
