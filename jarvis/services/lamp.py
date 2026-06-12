"""«Свет» Джарвиса: умные Wi-Fi лампы Tuya ЛОКАЛЬНО (tinytuya, без облака) + реакции + анимация.

Заход «лампы»: ламп НЕСКОЛЬКО (settings.yaml → lamp.lamps), работают синхронно как группа.
Каждая лампа — самостоятельный узел LampUnit: свой персистентный сокет, свой поток-воркер с
очередью, свой цикл реконнекта и keepalive (логика Этапов 21а/21в перенесена дословно). Одна
лампа выключена из розетки → вторая работает, Джарвис жив.

АНИМАЦИЯ В ТАКТ ГОЛОСУ: TTS публикует огибающую РЕАЛЬНОГО звука (jarvis/tts/envelope, батчи
RMS-уровней с epoch-якорем t0 = фактический старт звука). AnimationEngine раскладывает уровни
по таймлайну; каждая лампа сэмплирует «уровень на сейчас» по wall-clock в СВОЁМ темпе (ACK
Wi-Fi сам задаёт частоту кадров, кадры естественно скипаются) — старт пульсации совпадает со
звуком, а не с state=speaking (тот опережает звук на 0.4–1.2с — прежний рассинхрон).

РЕАКЦИИ (settings.yaml → lamp.reactions): старт/срабатывание/звонок/тишина/перерыв/ошибка.
Если в момент события идёт речь — реакция НЕ прерывает анимацию, а ПЕРЕКРАШИВАЕТ её палитру
на свою длительность. После всего — возврат в фоновое состояние.
"""
import json
import logging
import math
import os
import queue
import socket
import threading
import time
from datetime import datetime

from jarvis import config, contracts, phrases
from jarvis import lamp as helpers
from jarvis.bus import JarvisModule, run_service

_RESTORE_DELAY = 0.8   # debounce: пауза перед возвратом в фон после конца речи (фолбэк-режим)
_DEFAULT_RGB = (255, 170, 87)
_STATE_FILE = "lamp_state.json"   # снимок для doctor: Tuya держит 1 локальный сокет на лампу,
#                                 # отдельная проба при живом сервисе рвала бы соединение
_BRIGHT_FILE = "lamp_brightness.json"  # голосовой override яркости фона (переживает рестарт)
_TUYA_PORT = 6668                 # локальный TCP-порт Tuya-устройств
# Паузы между попытками подключения: первая — сразу, дальше быстрый рост до потолка
# lamp.reconnect_seconds. После ребута системы сеть поднимается ПОЗЖЕ сервиса — быстрый
# старт цикла подхватывает лампу через секунды после появления Wi-Fi (корень бага Этапа 21).
_BACKOFF_STEPS = (2.0, 4.0, 8.0, 15.0)
_FRAME_MIN_WAIT = 0.02            # нижний предел ожидания кадра анимации (с)
_ENVELOPE_TAIL = 5.0              # страховка: нет final → конец через N с после известных уровней


class LampUnit:
    """Одна лампа: креды + персистентный сокет + СВОЙ воркер/реконнект и фоновое состояние.

    Логика соединения — Этапы 21а/21в (TCP-пробник, быстрый backoff, тихий реконнект по
    keepalive, set_socketRetryLimit(1), явное закрытие старого bulb) — перенесена из
    одиночного LampModule и параметризована кредами."""

    def __init__(self, name: str, creds: dict, module: "LampModule", bright_override=None):
        self.name = name
        self.creds = creds
        self._m = module
        self.log = module.log
        self._engine = None                         # AnimationEngine, проставляет менеджер
        self._bulb = None
        self.connected = False
        self._ever_connected = False                # был ли хоть один успешный коннект
        self._fail_logged = False                   # WARNING о недоступности — раз на эпизод
        # Тихий реконнект: сокет умер В ПРОСТОЕ (лампа рвёт неактивный TCP — норма, не поломка).
        # Взводит ТОЛЬКО _ping, сбрасывает ТОЛЬКО _connect_loop/_on_io_error (GIL-атомарный bool).
        self._quiet_reconnect = False
        self._last_io_ok = 0.0                      # monotonic последнего успешного I/O
        self._lamp_lock = threading.Lock()          # сериализация I/O с лампой
        self._reconnect_lock = threading.Lock()     # одиночность reconnect-потока
        self._queue: "queue.Queue" = queue.Queue()
        self._worker = None
        self._reconnect_thread = None
        # Реакция-узор: флаг «идёт» + флаг прерывания (анимация речи вытесняет мигание/пульс).
        self._reaction_active = False
        self._abort_reaction = False
        # Анимация: момент последнего кадра (темп) и последний отправленный кадр (скип близких).
        self._last_frame_at = 0.0
        self._last_frame = (None, None)             # (яркость %, тон °)
        # Фоновое (желаемое устойчивое) состояние — per-lamp; меняется голосом.
        bg = config.LAMP_BACKGROUND or {}
        self._bg_on = bool(bg.get("вкл", True))
        self._bg_rgb = helpers.resolve_color(bg.get("цвет"), config.LAMP_COLORS) or _DEFAULT_RGB
        self._bg_bright = helpers.clamp_pct(bg.get("яркость"), 100)
        if bright_override is not None:             # голосовая яркость пережила рестарт
            self._bg_bright = helpers.clamp_pct(bright_override, self._bg_bright)
        self._bg_mode = "colour"               # 'colour' (rgb) | 'white' (яркость+температура)
        self._bg_temp = 40                      # температура белого (% — 0 тёплый, 100 холодный)

    # ------------------------------------------------------------------ #
    # Жизненный цикл юнита
    # ------------------------------------------------------------------ #
    def start(self):
        self._worker = threading.Thread(target=self._run_worker, daemon=True,
                                        name=f"lamp-{self.name}-worker")
        self._worker.start()
        self.schedule_reconnect()

    def stop(self):
        # Сокет закрываем ЯВНО: под systemd Restart=always полагаться на сборщик мусора
        # нельзя (аудит Этапа 21в; при реконнекте старый bulb тоже закрывается явно).
        bulb = self._bulb
        self._bulb = None
        self.connected = False
        if bulb is not None:
            try:
                bulb.close()
            except Exception:
                pass

    def enqueue(self, fn):
        try:
            self._queue.put_nowait(fn)
        except Exception:
            self.log.debug("Очередь лампы «%s» переполнена — команда отброшена",
                           self.name, exc_info=True)

    def abort_reaction(self):
        """Прервать ИДУЩИЙ узор реакции (анимация речи главнее). Вне узора — ничего не делает
        (иначе флаг повис бы и убил следующую реакцию на старте)."""
        if self._reaction_active:
            self._abort_reaction = True

    # ------------------------------------------------------------------ #
    # Соединение / переподключение (логика Этапов 21а/21в)
    # ------------------------------------------------------------------ #
    def _tcp_probe(self, ip: str, timeout: float = 1.0) -> bool:
        """Дешёвая проверка достижимости (TCP-порт Tuya) ПЕРЕД полным коннектом: сеть/лампа
        не готовы → тихий фейл за ~1с вместо блокировки в tinytuya. Зовётся только когда НЕ
        подключены — персистентному сокету не мешает."""
        try:
            sock = socket.create_connection((ip, _TUYA_PORT), timeout=timeout)
            sock.close()
            return True
        except OSError:
            return False

    def _note_unreachable(self, detail: str):
        """WARNING о недоступности один раз на эпизод (ретраи частые — не спамим), дальше DEBUG."""
        if self._fail_logged:
            self.log.debug("Лампа «%s» всё ещё недоступна: %s", self.name, detail)
            return
        self._fail_logged = True
        self.log.warning("Лампа «%s» недоступна (%s) — продолжаю попытки в фоне",
                         self.name, detail)
        if config.LAMP_NOTIFY_UNAVAILABLE:
            try:
                self._m.notify("Джарвис", f"Лампа «{self.name}» сейчас не в сети.", urgency="low")
            except Exception:
                pass

    def _connect(self) -> bool:
        dev_id = self.creds.get("device_id", "")
        local_key = self.creds.get("local_key", "")
        if not (dev_id and local_key):
            self.log.warning("Креды лампы «%s» не заданы (lamp.lamps) — лампа не подключена",
                             self.name)
            return False
        try:
            import tinytuya
        except Exception:
            self.log.error("tinytuya не установлен — лампы недоступны (выполните `pip install -e .`)")
            return False
        ip = self.creds.get("ip", "") or self._discover_ip(tinytuya)
        if not ip:
            self._note_unreachable("IP не задан и не найден автопоиском")
            return False
        if not self._tcp_probe(ip):
            self._note_unreachable(f"{ip}:{_TUYA_PORT} не отвечает — сеть/лампа ещё не готовы")
            return False
        version = float(self.creds.get("version", 3.5))
        try:
            with self._lamp_lock:
                bulb = tinytuya.BulbDevice(dev_id, address=ip, local_key=local_key,
                                           version=version, persist=True)
                bulb.set_socketTimeout(float(config.LAMP_SOCKET_TIMEOUT))
                # Внутренние ретраи tinytuya (дефолт 5×(5с+5с)) превращали ОДИН фейл в ~36-50с
                # блокировки — из-за этого лампа «умирала» после ребута. Темп задаёт НАШ цикл.
                bulb.set_socketRetryLimit(1)
                bulb.set_socketRetryDelay(1)
                bulb.set_socketPersistent(True)
                st = bulb.status()
                if not isinstance(st, dict) or "Error" in st or "Err" in st:
                    raise RuntimeError(f"status вернул {st}")
                # Старый объект закрываем ЯВНО: замена без close() копила бы fd до сборщика.
                old = self._bulb
                if old is not None:
                    try:
                        old.close()
                    except Exception:
                        pass
                self._bulb = bulb
                self.connected = True
            self._fail_logged = False
            self._last_io_ok = time.monotonic()
            # Тихий реконнект (умерший в простое сокет) — не событие, лог не выше DEBUG.
            level = logging.DEBUG if self._quiet_reconnect else logging.INFO
            self.log.log(level, "Лампа «%s» подключена: %s (протокол %s)", self.name, ip, version)
            self._m.write_state()
            return True
        except Exception as exc:
            self.connected = False
            self._note_unreachable(f"{ip}, v{version}: {exc}")
            return False

    def _discover_ip(self, tinytuya) -> str:
        """Автопоиск IP по device_id (если ip пуст). Скан сети ~несколько секунд; best-effort."""
        if not config.LAMP_AUTODISCOVER:
            return ""
        try:
            self.log.info("Ищу лампу «%s» в сети по device_id…", self.name)
            devices = tinytuya.deviceScan(False, 5) or {}
            for ip, info in devices.items():
                if self.creds.get("device_id") in (info.get("gwId"), info.get("id")):
                    found = info.get("ip", ip)
                    self.log.info("Лампа «%s» найдена автопоиском: %s", self.name, found)
                    return found
        except Exception:
            self.log.debug("Автопоиск лампы «%s» не удался", self.name, exc_info=True)
        return ""

    def schedule_reconnect(self):
        with self._reconnect_lock:
            if self._reconnect_thread and self._reconnect_thread.is_alive():
                return
            self._reconnect_thread = threading.Thread(
                target=self._connect_loop, daemon=True, name=f"lamp-{self.name}-reconnect")
            self._reconnect_thread.start()

    def _connect_loop(self):
        """Живучий цикл (пере)подключения: первая попытка СРАЗУ, затем быстрый backoff
        (2→4→8→15с) до потолка lamp.reconnect_seconds — бесконечно, пока не подключимся.
        Любое исключение попытки не убивает цикл. На успехе — ДВА пути: тихий (сокет умер
        в простое, поднялись с первой попытки → DEBUG, без вспышки) и громкий (настоящий
        обрыв → INFO + реакция startup «я жив» + возврат в фоновое свечение)."""
        stop = self._m.stop_event
        attempt = 0
        while not stop.is_set() and not self.connected:
            ok = False
            try:
                ok = self._connect()
            except Exception:
                self._m.log_exc(logging.WARNING,
                                "Неожиданный сбой попытки подключения к лампе «%s»", self.name)
            if ok:
                # Тихий путь: сокет умер в простое и поднялся С ПЕРВОЙ попытки — физическое
                # состояние лампы не менялось, слать DPS/вспышку незачем. Не с первой →
                # настоящий обрыв (_connect уже дал WARNING) → громкий путь со startup-реакцией.
                quiet = self._quiet_reconnect and attempt == 0
                self._quiet_reconnect = False
                if quiet:
                    self.log.debug("Сокет лампы «%s» переподнят тихо (умер в простое — норма)",
                                   self.name)
                    return
                if self._ever_connected:
                    self.log.info("Лампа «%s» снова на связи — реакции восстановлены", self.name)
                self._ever_connected = True
                spec = helpers.reaction("startup", config.LAMP_REACTIONS, config.LAMP_COLORS)
                self.enqueue(lambda: self._do_reaction(spec) if spec else self._apply_background())
                return
            cap = max(5.0, float(config.LAMP_RECONNECT_SECONDS))
            delay = _BACKOFF_STEPS[attempt] if attempt < len(_BACKOFF_STEPS) else cap
            attempt += 1
            if stop.wait(min(delay, cap)):
                return

    def _ping(self):
        """status-пинг через воркер (сериализован, nowait=False — сокет не десинхронится).
        HEART_BEAT эта прошивка ИГНОРИРУЕТ (Этап 21в) — потому пингуем status().

        Сбой пинга = сокет умер в простое (норма для этой лампы) → ТИХИЙ реконнект:
        DEBUG + флаг _quiet_reconnect, без WARNING и без startup-вспышки. Громкий путь
        (_on_io_error) остаётся за сбоями РЕАЛЬНЫХ действий — команда/реакция."""
        if not self.connected or self._bulb is None:
            return
        try:
            with self._lamp_lock:
                st = self._bulb.status()
            if not isinstance(st, dict) or "Error" in st or "Err" in st:
                raise RuntimeError(f"status вернул {st}")
            self._last_io_ok = time.monotonic()
        except Exception as exc:
            self.log.debug("keepalive «%s»: сокет умер в простое (%s) — тихо переподключаюсь",
                           self.name, exc)
            self._quiet_reconnect = True
            self.connected = False
            self._m.write_state()
            self.schedule_reconnect()

    def _on_io_error(self):
        """Громкий путь: сбой РЕАЛЬНОГО действия (команда/реакция/кадр) — WARNING + реконнект
        со startup-реакцией на восстановлении. Тихий keepalive-путь живёт в _ping."""
        self._m.log_exc(logging.WARNING,
                        "Сбой обращения к лампе «%s» — помечаю недоступной, переподключусь",
                        self.name)
        self._quiet_reconnect = False   # настоящий обрыв перекрывает тихий план восстановления
        self.connected = False
        self._m.write_state()
        self.schedule_reconnect()

    # ------------------------------------------------------------------ #
    # Воркер: команды из очереди + кадры анимации, когда очередь пуста
    # ------------------------------------------------------------------ #
    def _run_worker(self):
        stop = self._m.stop_event
        while not stop.is_set():
            engine = self._engine
            active = engine is not None and engine.active_for(self)
            timeout = engine.frame_wait(self) if active else 0.5
            try:
                fn = self._queue.get(timeout=timeout)
            except queue.Empty:
                # Очередь пуста и идёт речь → один кадр анимации. nowait=False ждёт ACK —
                # лампа сама задаёт фактический темп (кадры между ACK естественно скипаются).
                if engine is not None and engine.active_for(self):
                    try:
                        self._send_frame(engine)
                    except Exception:
                        self._on_io_error()
                continue
            if fn is None:
                continue
            try:
                fn()
            except Exception:
                self._on_io_error()

    def _send_frame(self, engine):
        """Один кадр анимации: целевой уровень «на сейчас» → DP24 (V=яркость, тон±дрейф).

        Первый кадр фразы — полный пакет {DP20, DP21, DP24} (гарантирует режим colour после
        чего угодно); дальше ТОЛЬКО {DP24} — СВЕРЕНО на лампе (`jarvis lamp rtt`): прошивка
        принимает одиночный DP24, режим не сбивается. Близкие кадры пропускаются."""
        frame = engine.frame_for(self, time.time())
        if frame is None:
            return
        bright, hue, sat, first = frame
        last_b, last_h = self._last_frame
        if not first and last_b is not None:
            if abs(bright - last_b) < 3 and abs(hue - (last_h or 0.0)) < 4.0:
                self._last_frame_at = time.time()   # изменение незаметно — не дёргаем лампу
                return
        data = {24: helpers.hsv_to_v2hex(hue, sat, bright)}
        if first:
            data = {20: True, 21: "colour", 24: helpers.hsv_to_v2hex(hue, sat, bright)}
        # Интервал кадров отсчитываем от СТАРТА отправки: ожидание ACK — часть кадра, иначе
        # фактический темп был бы (1/fps + RTT), а не fps_max (поймано тестом пейсинга).
        self._last_frame_at = time.time()
        if first and config.PERF_DEBUG:
            # Объективная калибровка: рассинхрон первого кадра от t0 (−lead = упредили,
            # +N = опоздали) и фактический ACK. По ним выставляется опережение_мс.
            skew = (self._last_frame_at - (engine._t0 or self._last_frame_at)) * 1000.0
            self._dps(data)
            rtt = (time.time() - self._last_frame_at) * 1000.0
            self.log.info("PERF lamp «%s»: первый кадр, рассинхрон от t0 %+.0fмс, ACK %.0fмс",
                          self.name, skew, rtt)
        else:
            self._dps(data)
        self._last_frame = (bright, hue)

    # ------------------------------------------------------------------ #
    # Низкоуровневые операции — НАПРЯМУЮ ПО DPS (сверено на лампе v3.5; см. CLAUDE.md/ГРАБЛИ)
    # DP20 switch · DP21 work_mode(colour/white) · DP22 bright(10–1000) · DP23 temp(0–1000) ·
    # DP24 colour_data_v2 (HSV-hex). В режиме colour яркость = V в DP24 (DP22 НЕ слать!).
    # ------------------------------------------------------------------ #
    def _dps(self, data: dict):
        """Атомарно записать DPS (set_multiple_values). nowait=False: на ПЕРСИСТЕНТНОМ сокете
        ждём ответ — иначе непрочитанные reply копятся и десинхронят сокет (Этап 19). Воркер
        сериализует, MQTT не блокируется. Ошибка → реконнект (ловит воркер)."""
        if not self.connected or self._bulb is None:
            return
        with self._lamp_lock:
            self._bulb.set_multiple_values(data, nowait=False)
        self._last_io_ok = time.monotonic()

    def _set_color(self, rgb, brightness):
        """Цвет (режим colour): тон/насыщенность из RGB, яркость — в V компоненте DP24."""
        rgb = rgb or self._bg_rgb
        self._dps({20: True, 21: "colour",
                   24: helpers.rgb_to_v2hex(rgb[0], rgb[1], rgb[2],
                                            helpers.clamp_pct(brightness, 60))})

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
        if not self.connected or self._bulb is None:
            return
        with self._lamp_lock:
            self._bulb.set_value(20, False, nowait=False)
        self._last_io_ok = time.monotonic()

    def _apply_background(self):
        """Вернуть лампу в фоновое (желаемое) состояние (по запомненному режиму)."""
        self._last_frame = (None, None)   # следующая анимация начнёт с полного пакета
        if not self._bg_on:
            self._turn_off()
        elif self._bg_mode == "white":
            self._set_white(self._bg_bright, self._bg_temp)
        else:
            self._set_color(self._bg_rgb, self._bg_bright)

    def reset_background(self):
        bg = config.LAMP_BACKGROUND or {}
        self._bg_on = bool(bg.get("вкл", True))
        self._bg_rgb = helpers.resolve_color(bg.get("цвет"), config.LAMP_COLORS) or _DEFAULT_RGB
        self._bg_bright = helpers.clamp_pct(bg.get("яркость"), 100)
        self._bg_mode = "colour"
        self._bg_temp = 40

    def _ramp(self, frm, to, steps=4, dt=0.12):
        """Плавный переход яркости (для паттерна «пульс»). Прерывается анимацией речи."""
        stop = self._m.stop_event
        frm, to = helpers.clamp_pct(frm, 30), helpers.clamp_pct(to, 80)
        for i in range(1, steps + 1):
            if stop.is_set() or self._abort_reaction:
                return
            self._set_brightness(round(frm + (to - frm) * i / steps))
            time.sleep(dt)

    def _do_reaction(self, spec):
        """Выполнить реакцию-узор и ВЕРНУТЬ фон. Блокирует воркер на свою (краткую) длительность.

        Анимация речи ВЫТЕСНЯЕТ узор (abort_reaction): прерванный узор фон не трогает —
        его вернёт анимация по концу фразы (иначе фон перебил бы первый кадр)."""
        if spec is None or not self.connected:
            return
        rgb = spec.get("rgb") or self._bg_rgb
        br = spec.get("brightness", 70)
        pattern = spec.get("pattern", "свечение")
        dur = spec.get("duration", 0.0)
        reps = spec.get("repeats", 1)
        stop = self._m.stop_event
        self._reaction_active = True
        try:
            if pattern == "мигание":
                half = max(0.12, (dur / max(1, reps)) / 2) if dur else 0.2
                for _ in range(reps):
                    if stop.is_set() or self._abort_reaction:
                        break
                    self._set_color(rgb, br)
                    time.sleep(half)
                    self._set_brightness(1)
                    time.sleep(half)
            elif pattern == "пульс":
                low = max(5, br // 4)
                self._set_color(rgb, low)
                for _ in range(reps):
                    if stop.is_set() or self._abort_reaction:
                        break
                    self._ramp(low, br)
                    self._ramp(br, low)
            else:  # свечение — ровно
                self._set_color(rgb, br)
                t_end = time.monotonic() + max(0.0, float(dur or 0))
                while time.monotonic() < t_end:
                    if stop.is_set() or self._abort_reaction:
                        break
                    time.sleep(0.1)
        finally:
            self._reaction_active = False
            aborted = self._abort_reaction
            self._abort_reaction = False
            engine = self._engine
            if not (aborted and engine is not None and engine.active_for(self)):
                self._apply_background()


class AnimationEngine:
    """Таймлайн огибающей голоса (ОДИН на сервис) + палитра-override реакций.

    Уровни приходят батчами (jarvis/tts/envelope) и раскладываются по индексам окон от якоря
    t0 (epoch старта ЗВУКА; TTS и лампы — одна машина, часы общие). Каждая лампа в своём
    воркере спрашивает «целевой уровень на сейчас» — сглаживание атака/спад per-lamp, кривая
    гамма, лёгкий дрейф тона. Конец: final (точная длительность) → дотяг спада → фон; ремни —
    отсутствие final (таймаут), state=idle от TTS, жёсткий потолок макс_сек."""

    def __init__(self, module: "LampModule"):
        self._m = module
        self._lock = threading.Lock()
        self._seq = None          # id текущей фразы (None — речи нет)
        self._t0 = 0.0
        self._win = 0.05
        self._levels: list = []
        self._end = None          # epoch конца звука (по final/idle); None — ещё неизвестен
        self._palette = None      # (тон°, насыщенность, до_epoch) — override цвета (событие)
        self._unit_state: dict = {}  # имя лампы → {level, t, first, done}
        # Базовая палитра речи — цвет реакции speaking (нет → фон конкретной лампы).
        spec = helpers.reaction("speaking", config.LAMP_REACTIONS, config.LAMP_COLORS)
        rgb = (spec or {}).get("rgb")
        self._speech_hs = helpers.rgb_to_hs(*rgb) if rgb else None

    # --- приём данных ---
    def on_envelope(self, p: dict):
        """Батч огибающей от TTS (или final). Новый seq = новая фраза."""
        try:
            if not config.LAMP_ANIM_ENABLED:
                return
            seq = p.get("seq")
            if p.get("final"):
                with self._lock:
                    if self._seq is not None and seq == self._seq:
                        if p.get("cancel"):
                            self._end = time.time() - 1.0   # авария воспроизведения — гасим сразу
                        else:
                            self._end = self._t0 + float(p.get("duration") or 0.0)
                return
            t0 = float(p.get("t0") or 0.0)
            levels = [float(x) for x in (p.get("levels") or [])]
            if not levels or t0 <= 0:
                return
            fresh = False
            with self._lock:
                if seq != self._seq:
                    fresh = True
                    self._seq = seq
                    self._t0 = t0
                    self._win = float(p.get("win") or 0.05) or 0.05
                    self._levels = []
                    self._end = None
                    for st in self._unit_state.values():
                        st["first"], st["done"] = False, False
                idx = max(0, int(round(float(p.get("offset") or 0.0) / self._win)))
                need = idx + len(levels)
                if len(self._levels) < need:   # дыра от потерянного батча останется нулями
                    self._levels.extend([0.0] * (need - len(self._levels)))
                self._levels[idx:need] = levels
            if fresh:
                # Речь началась — анимация вытесняет идущие узоры реакций (их цвет, если это
                # было событие, уже перенесён в палитру через set_palette). None-элемент БУДИТ
                # спящий воркер немедленно: иначе он досыпал бы свой тик queue.get(0.5) и
                # первый кадр опаздывал до полусекунды от старта звука (поймано тестом).
                for u in self._m.units.values():
                    u.abort_reaction()
                    u.enqueue(None)
        except Exception:
            self._m.log.debug("Сбой обработки огибающей", exc_info=True)

    def on_idle(self):
        """state=idle от TTS = точный конец воспроизведения — ремень, если final потерялся."""
        with self._lock:
            if self._seq is not None and self._end is None:
                self._end = time.time()

    def set_palette(self, rgb, ttl: float):
        """Перекрасить анимацию (событие во время речи): тон/насыщенность rgb на ttl секунд."""
        try:
            h, s = helpers.rgb_to_hs(rgb[0], rgb[1], rgb[2])
            with self._lock:
                self._palette = (h, s, time.time() + max(0.5, float(ttl)))
        except Exception:
            pass

    # --- опрос лампами ---
    def speech_active(self) -> bool:
        now = time.time()
        with self._lock:
            if self._seq is None:
                return False
            return now <= self._effective_end_locked() + 1.0   # +1с на дотяг спада

    def active_for(self, unit: LampUnit) -> bool:
        """Анимировать ли эту лампу сейчас (речь идёт, лампа на связи и не выключена голосом)."""
        try:
            if not config.LAMP_ANIM_ENABLED or not unit.connected or not unit._bg_on:
                return False
            with self._lock:
                if self._seq is None:
                    return False
                st = self._unit_state.get(unit.name)
                return not (st and st.get("done"))
        except Exception:
            return False

    def frame_wait(self, unit: LampUnit) -> float:
        """Сколько воркеру ждать до следующего кадра (потолок fps_max; до старта звука — пауза)."""
        try:
            now = time.time()
            lead = max(0.0, float(config.LAMP_ANIM_LOOKAHEAD_MS) / 1000.0)
            with self._lock:
                if self._seq is None:
                    return 0.5
                pre = (self._t0 - lead) - now   # первый кадр уходит на lead раньше t0
            if pre > 0:
                return min(max(pre, _FRAME_MIN_WAIT), 0.5)
            fps = max(1.0, float(config.LAMP_ANIM_FPS_MAX))
            return max(_FRAME_MIN_WAIT, (1.0 / fps) - (now - unit._last_frame_at))
        except Exception:
            return 0.5

    def _effective_end_locked(self) -> float:
        """Конец фразы: final → точный; нет → оценка по известным уровням + страховка;
        всегда не позже жёсткого потолка макс_сек."""
        cap = self._t0 + max(1.0, float(config.LAMP_ANIM_MAX_SECONDS))
        if self._end is not None:
            return min(self._end, cap)
        return min(self._t0 + len(self._levels) * self._win + _ENVELOPE_TAIL, cap)

    def frame_for(self, unit: LampUnit, now: float):
        """Кадр для лампы «на сейчас»: (яркость %, тон °, насыщенность, первый?) или None.

        None до старта звука (не дёргаем лампу РАНЬШЕ речи), после конца (с возвратом в фон)
        и при закрытой фразе. Сглаживание атака/спад — per-lamp, с поправкой на фактический
        интервал кадров (темп у каждой лампы свой)."""
        try:
            with self._lock:
                if self._seq is None:
                    return None
                st = self._unit_state.setdefault(
                    unit.name, {"level": 0.0, "t": 0.0, "first": False, "done": False})
                if st["done"]:
                    return None
                # Упреждение конвейера лампы: свет появляется через ~ACK (+ лаг атаки) ПОСЛЕ
                # команды, поэтому сэмплируем огибающую на lead ВПЕРЁД — тогда свет совпадёт со
                # звуком. Данные «будущего» уже в self._levels (батч на всю фразу пришёл заранее).
                # sample_t КЛАМПИМ потолком end: иначе на последних lead мс sample_envelope
                # вернул бы 0 (now+lead > end) и хвост фразы погас бы раньше голоса.
                lead = max(0.0, float(config.LAMP_ANIM_LOOKAHEAD_MS) / 1000.0)
                if now + lead < self._t0 - 0.05:
                    return None   # звук ещё не начался даже с учётом упреждения
                end = self._effective_end_locked()
                sample_t = min(now + lead, end)
                raw = helpers.sample_envelope(self._levels, self._t0, self._win, sample_t, end=end)
                prev, tprev = st["level"], st["t"]
                dt = (now - tprev) if tprev > 0 else 0.0
                tau_ms = float(config.LAMP_ANIM_ATTACK_MS if raw > prev
                               else config.LAMP_ANIM_RELEASE_MS)
                k = 1.0 if dt <= 0 or tau_ms <= 0 else 1.0 - math.exp(-dt * 1000.0 / tau_ms)
                lvl = prev + (raw - prev) * k
                st["level"], st["t"] = lvl, now
                if now > end and lvl < 0.04:
                    # Спад дотянут до пола — фраза для этой лампы закончена: вернуть фон.
                    st["done"] = True
                    if all(s.get("done") for s in self._unit_state.values()):
                        self._seq = None
                    unit.enqueue(unit._apply_background)
                    return None
                bright = helpers.level_to_brightness(
                    lvl, int(config.LAMP_ANIM_BRIGHT_MIN), int(config.LAMP_ANIM_BRIGHT_MAX),
                    float(config.LAMP_ANIM_GAMMA))
                pal = self._palette
                if pal and now < pal[2]:
                    hue, sat = pal[0], pal[1]
                else:
                    if pal:
                        self._palette = None
                    if self._speech_hs is not None:
                        hue, sat = self._speech_hs
                    else:
                        hue, sat = helpers.rgb_to_hs(*unit._bg_rgb)
                drift = float(config.LAMP_ANIM_HUE_DRIFT)
                if drift:
                    hue = hue + drift * (lvl * 2.0 - 1.0)   # «дыхание» тона вокруг базового
                first = not st["first"]
                st["first"] = True
                return (bright, hue, sat, first)
        except Exception:
            self._m.log.debug("Сбой кадра анимации", exc_info=True)
            return None


class LampModule(JarvisModule):
    """«Свет»: менеджер ламп — строит юниты, маршрутизирует команды/реакции/анимацию."""

    def __init__(self):
        super().__init__("jarvis-lamp")
        self.units: dict[str, LampUnit] = {}
        self._engine = AnimationEngine(self)
        self._restore_timer = None
        self._speaking = False        # для фолбэк-режима (анимация выключена)
        self._state_lock = threading.Lock()   # сериализация снимка lamp_state.json

    @property
    def stop_event(self):
        return self._stop_event

    # ------------------------------------------------------------------ #
    # Жизненный цикл
    # ------------------------------------------------------------------ #
    def on_start(self):
        if not config.LAMP_ENABLED:
            self.log.info("Лампы выключены (lamp.enabled=false) — сервис простаивает")
            return
        if not config.LAMP_DEVICES:
            self.log.warning("Лампы не заданы (settings.yaml → lamp.lamps) — сервис простаивает")
            return
        override = self._load_brightness()
        for name, creds in config.LAMP_DEVICES.items():
            unit = LampUnit(name, creds, self, bright_override=override)
            unit._engine = self._engine
            self.units[name] = unit
        self.subscribe(contracts.TOPIC_STATE, self.on_state)      # фолбэк-glow + ремень idle
        self.subscribe(contracts.TOPIC_SAY, self.on_say)          # срабатывания (min_volume)
        self.subscribe(contracts.TOPIC_LAMP, self.on_lamp)        # голос-команды лампам
        self.subscribe(contracts.TOPIC_PHONE_CALL, self.on_call)  # входящий звонок
        self.subscribe(contracts.TOPIC_EVENT, self.on_event)      # ошибка/тишина/перерыв
        self.subscribe(contracts.TOPIC_TTS_ENVELOPE, self._engine.on_envelope)  # огибающая
        for unit in self.units.values():
            unit.start()
        if float(config.LAMP_KEEPALIVE_MINUTES) > 0:
            threading.Thread(target=self._keepalive_loop, daemon=True,
                             name="lamp-keepalive").start()
        self.log.info("Ламп в группе: %d (%s)", len(self.units), ", ".join(self.units))

    def on_stop(self):
        self._cancel_restore()
        for unit in self.units.values():
            unit.stop()
        self.write_state()

    # ------------------------------------------------------------------ #
    # Keepalive (один поток на все лампы) + снимок состояния
    # ------------------------------------------------------------------ #
    def _keepalive_loop(self):
        """Пинг чаще таймаута лампы (рвёт простаивающий TCP за 30–60с — Этап 21в). Гейт
        _last_io_ok: лампа с активным трафиком (анимация!) пинг не получает."""
        interval = max(15.0, float(config.LAMP_KEEPALIVE_MINUTES) * 60.0)
        last_snapshot = 0.0
        while not self._stop_event.wait(interval):
            now = time.monotonic()
            if now - last_snapshot >= 60.0:
                last_snapshot = now
                self.write_state()      # снимок для doctor — раз в минуту достаточно
            for unit in self.units.values():
                if not unit.connected:
                    continue
                if now - unit._last_io_ok < interval * 0.5:
                    continue            # I/O было недавно — пинг не нужен
                unit.enqueue(unit._ping)

    def write_state(self):
        """Снимок состояния ламп (logs/lamp_state.json, атомарно). Его читает doctor: Tuya
        держит ОДИН сокет на лампу — отдельная проба при живом сервисе рвала бы соединение."""
        try:
            with self._state_lock:
                data = {
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "keepalive_minutes": float(config.LAMP_KEEPALIVE_MINUTES),
                    "лампы": {
                        name: {
                            "connected": bool(u.connected),
                            "ip": u.creds.get("ip", ""),
                            "version": u.creds.get("version"),
                        } for name, u in self.units.items()
                    },
                }
                path = os.path.join(str(config.LOGS_DIR), _STATE_FILE)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp, path)
        except Exception:
            self.log.debug("Не удалось записать lamp_state", exc_info=True)

    # --- голосовой override яркости фона (переживает рестарт; паттерн voice_volume) ---
    def _load_brightness(self):
        try:
            path = os.path.join(str(config.LOGS_DIR), _BRIGHT_FILE)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    v = json.load(f).get("процент")
                if isinstance(v, (int, float)) and 5 <= v <= 100:
                    return int(v)
        except Exception:
            self.log.debug("Не удалось прочитать override яркости ламп", exc_info=True)
        return None

    def _save_brightness(self, pct):
        """Записать/снять (pct=None) голосовой override яркости — атомарно (tmp+replace)."""
        try:
            path = os.path.join(str(config.LOGS_DIR), _BRIGHT_FILE)
            if pct is None:
                if os.path.exists(path):
                    os.remove(path)
                return
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"процент": int(pct)}, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            self.log.debug("Не удалось записать override яркости ламп", exc_info=True)

    # ------------------------------------------------------------------ #
    # Реакции на события Джарвиса
    # ------------------------------------------------------------------ #
    def _react(self, spec, palette_extra: float = 0.0):
        """Реакция с учётом речи: палитра анимации перекрашивается ВСЕГДА (если фраза уже идёт
        или начнётся в ближайшие секунды — событие обычно сопровождается репликой, которая
        синтезируется 0.5–1.2с), а узор на лампах — только когда речи СЕЙЧАС нет."""
        if spec is None:
            return
        rgb = spec.get("rgb")
        dur = float(spec.get("duration") or 0) or 2.0
        if rgb:
            self._engine.set_palette(rgb, dur + palette_extra)
        if not self._engine.speech_active():
            for unit in self.units.values():
                if unit.connected:
                    unit.enqueue(lambda u=unit: u._do_reaction(spec))

    def on_state(self, payload: dict):
        """С анимацией: speaking ИГНОРИРУЕМ (опережает звук на 0.4–1.2с — прежний рассинхрон);
        idle от TTS — ремень точного конца. Без анимации — прежнее ровное свечение."""
        try:
            st = payload.get("state")
            if config.LAMP_ANIM_ENABLED:
                if st == contracts.STATE_IDLE and payload.get("source") == "jarvis-tts":
                    self._engine.on_idle()
                return
            # Фолбэк (animation.вкл=false): поведение Этапа 18 — свечение пока говорит.
            if st == contracts.STATE_SPEAKING:
                self._speaking = True
                self._cancel_restore()
                spec = helpers.reaction("speaking", config.LAMP_REACTIONS, config.LAMP_COLORS)
                if spec:
                    for unit in self.units.values():
                        if unit.connected:
                            unit.enqueue(lambda u=unit: u._set_color(
                                spec["rgb"] or u._bg_rgb, spec["brightness"]))
            elif self._speaking:
                self._speaking = False
                self._schedule_restore()
        except Exception:
            self.log.debug("Сбой реакции на состояние", exc_info=True)

    def on_say(self, payload: dict):
        """Срабатывание (будильник/таймер/напоминание — помечены min_volume) → заметная реакция.
        Фраза срабатывания начнётся через ~0.5–1.2с — палитре даём запас, чтобы она
        перекрасила и саму анимацию этой фразы."""
        try:
            if payload.get("min_volume") is None and not payload.get("critical"):
                return
            spec = helpers.reaction("firing", config.LAMP_REACTIONS, config.LAMP_COLORS)
            if spec:
                self._cancel_restore()
                self._react(spec, palette_extra=4.0)
        except Exception:
            self.log.debug("Сбой реакции на срабатывание", exc_info=True)

    def on_call(self, payload: dict):
        """Входящий звонок → реакция call (приоритет), затем возврат в фон."""
        try:
            if (payload or {}).get("type") != "incoming":
                return
            spec = helpers.reaction("call", config.LAMP_REACTIONS, config.LAMP_COLORS)
            if spec:
                self._cancel_restore()
                self._react(spec, palette_extra=4.0)
        except Exception:
            self.log.debug("Сбой реакции на звонок", exc_info=True)

    def on_event(self, payload: dict):
        """Общие события (jarvis/event): ошибка/тишина/перерыв → мягкие реакции.
        Свои события игнорируем (ошибка лампы не должна сама себя подсвечивать по кругу)."""
        try:
            if (payload or {}).get("source") == self.name:
                return
            key = {"error": "error", "silence_on": "silence", "silence_off": "silence",
                   "break_offer": "break", "break_praise": "break"}.get(payload.get("event"))
            if not key:
                return
            spec = helpers.reaction(key, config.LAMP_REACTIONS, config.LAMP_COLORS)
            if spec:
                self._cancel_restore()
                self._react(spec, palette_extra=2.0)
        except Exception:
            self.log.debug("Сбой реакции на событие", exc_info=True)

    # ------------------------------------------------------------------ #
    # Голосовые команды лампам
    # ------------------------------------------------------------------ #
    def _resolve_units(self, payload: dict):
        """Цели команды: (юниты, адресность). Явное поле «кто» → одна лампа; иначе ищем имя
        лампы в исходной фразе («выключи вторую лампу»); не нашли → ВСЕ лампы (группа)."""
        who = str(payload.get("кто") or "").strip()
        if who and who in self.units:
            return [self.units[who]], True
        names = helpers.resolve_target(payload.get("текст") or "", list(self.units))
        if names:
            return [self.units[n] for n in names if n in self.units], True
        return list(self.units.values()), False

    def _ack(self, action):
        """Озвучить результат голосовой команды (паки из settings.yaml)."""
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
                self.log.debug("Не удалось озвучить ответ ламп", exc_info=True)

    def on_lamp(self, payload: dict):
        """Голосовая команда лампам (core форвардит поле «лампа» + исходную фразу): вкл/выкл/
        цвет/ярче/темнее/яркость/тепло/холод/авто. Команда меняет ФОНОВОЕ состояние целевых
        ламп (в него возвращаются реакции). Озвучивает результат ЭТОТ сервис (единый источник):
        успех — пак действия; цели не в сети — пак «недоступна» (адресная — с именем)."""
        try:
            action = (payload or {}).get("действие")
            if not action:
                return
            targets, explicit = self._resolve_units(payload)
            if not targets:
                return
            alive = [u for u in targets if u.connected]
            if not alive:
                if explicit and len(targets) == 1:
                    self.say(phrases.pick("lamp.unavailable_one", config.LAMP_UNAVAILABLE_ONE)
                             .replace("{имя}", targets[0].name))
                else:
                    self.say(phrases.pick("lamp.unavailable", config.LAMP_UNAVAILABLE))
                return
            if action == "вкл":
                for u in alive:
                    u._bg_on = True
                    u.enqueue(u._apply_background)
            elif action == "выкл":
                for u in alive:
                    u._bg_on = False
                    u.enqueue(u._turn_off)
            elif action == "цвет":
                rgb = helpers.resolve_color(payload.get("цвет"), config.LAMP_COLORS)
                if not rgb:
                    # Раньше нерезолвленный цвет молча игнорировался, но ack «Меняю цвет»
                    # всё равно звучал — честный ответ вместо ложного подтверждения.
                    self.say(phrases.pick("lamp.color_unknown", config.LAMP_COLOR_UNKNOWN))
                    return
                for u in alive:
                    u._bg_rgb = rgb
                    u._bg_mode = "colour"
                    u._bg_on = True
                    u.enqueue(u._apply_background)
            elif action == "яркость":
                lvl = payload.get("уровень")
                if not isinstance(lvl, (int, float)) or lvl <= 0:
                    return
                pct = helpers.clamp_pct(float(lvl) * 100.0, 100)
                for u in alive:
                    u._bg_bright = pct
                    u._bg_on = True
                    u.enqueue(u._apply_background)
                if not explicit:   # групповую яркость запоминаем (переживает рестарт)
                    self._save_brightness(pct)
                self.say(phrases.pick("lamp.bright_set", config.LAMP_BRIGHT_SET_ACK)
                         .replace("{процент}", str(pct)))
                return
            elif action == "ярче":
                for u in alive:
                    u._bg_bright = min(100, u._bg_bright + int(config.LAMP_BRIGHTNESS_STEP))
                    u._bg_on = True
                    u.enqueue(u._apply_background)
            elif action == "темнее":
                for u in alive:
                    u._bg_bright = max(5, u._bg_bright - int(config.LAMP_BRIGHTNESS_STEP))
                    u.enqueue(u._apply_background)
            elif action in ("тепло", "холод"):
                step = int(config.LAMP_TEMP_STEP)
                for u in alive:
                    u._bg_temp = (max(0, u._bg_temp - step) if action == "тепло"
                                  else min(100, u._bg_temp + step))
                    u._bg_mode = "white"
                    u._bg_on = True
                    u.enqueue(u._apply_background)
            elif action == "авто":
                self._save_brightness(None)   # «авто» снимает и запомненную яркость
                for u in alive:
                    u.reset_background()
                    u.enqueue(u._apply_background)
            else:
                return
            self._ack(action)
        except Exception:
            self.log.debug("Сбой обработки команды ламп", exc_info=True)

    # --- debounce возврата в фон после речи (только фолбэк-режим без анимации) ---
    def _schedule_restore(self):
        self._cancel_restore()
        try:
            self._restore_timer = threading.Timer(
                _RESTORE_DELAY,
                lambda: [u.enqueue(u._apply_background) for u in self.units.values()])
            self._restore_timer.daemon = True
            self._restore_timer.start()
        except Exception:
            for unit in self.units.values():
                unit.enqueue(unit._apply_background)

    def _cancel_restore(self):
        t = self._restore_timer
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass
            self._restore_timer = None


def main():
    run_service(LampModule, "jarvis-lamp")


if __name__ == "__main__":
    main()
