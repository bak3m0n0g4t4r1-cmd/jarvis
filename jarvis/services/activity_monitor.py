"""«Внимание» — детектор активности и напоминания о перерыве.

Следит, что пользователь ФИЗИЧЕСКИ работает за ПК (мышь/клавиатура/тачпад), и по
истечении случайного цикла (23–27 мин) вежливо предлагает перерыв «в характере»; при
игноре — мягко затемняет экран и повторяет напоминание; за полноценный перерыв (4–6 мин
простоя) — хвалит; по стоп-фразе — отвечает и сбрасывает цикл.

Почему /dev/input, а не D-Bus idle: на KDE6/Wayland опрос idle недоступен
(`org.freedesktop.ScreenSaver.GetSessionIdleTime` → NotSupported), а событийный idle
(swayidle/ext-idle) УВАЖАЕТ idle-инхибиторы → полноэкранное видео считалось бы
активностью. Чтение событий ввода даёт ЧИСТО-ВХОДНОЙ простой: фильм/музыка/чтение без
касания = простой растёт (это НЕ активность), как и требует ТЗ. Читаем ТОЛЬКО тайминг
событий (факт нажатия), НЕ их содержимое.

⚠ КОРЕНЬ бывшего бага «постоянно напоминает, даже когда не за ноутом»: раньше
`_reader_loop` каждые `device_rescan_seconds` (60с) ПЕРЕОТКРЫВАЛ устройства и БЕЗУСЛОВНО
ставил `_last_input = now`. Так как 60с < микропауза(80) < сброс(180), idle никогда не
рос → детектор считал пользователя вечно активным и слал напоминания бесконечно. Теперь
`_last_input` сбрасывается ТОЛЬКО при переходе «нет устройств → есть» (первое появление
после выдачи прав), а периодическая переэнумерация лишь добавляет/убирает устройства
(hotplug), НЕ трогая idle.

Лёгкость: поток-читатель блокируется в select() (≈0% CPU, просыпается только на ввод);
поток-логика тикает раз в несколько секунд; рампа яркости — в ОТДЕЛЬНОМ потоке (не держит
общий lock). Всё в try-except — сервис не падает.

⚠ Требует прав на чтение /dev/input: `sudo usermod -aG input $USER` + перелогин (та же
группа нужна ydotool). Без них сервис «спит» (молчит), а `jarvis doctor` подсказывает фикс.
"""
import json
import logging
import os
import random
import select
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from jarvis import config, contracts, phrases, speech
from jarvis.breaks import is_stop_phrase
from jarvis.bus import JarvisModule

# Биты битовой маски `B: EV=` в /proc/bus/input/devices: бит N взведён, если устройство
# поддерживает тип события N. EV_KEY=тип 1 (бит 0x02), EV_REL=тип 2 (бит 0x04),
# EV_ABS=тип 3 (бит 0x08), EV_REP=тип 20 (бит 0x100000 — автоповтор, есть у настоящих
# клавиатур, но НЕ у кнопок Power/Sleep). (Бит 0x01 = EV_SYN — есть у всех, не показателен.)
_EV_KEY = 0x02
_EV_REL = 0x04
_EV_ABS = 0x08
_EV_REP = 0x100000

# Состояния автомата.
_ACCUMULATING = "accumulating"  # копим активное время цикла
_OFFERED = "offered"            # перерыв предложен, ждём реакции
_DIMMED = "dimmed"              # проигнорировано → экран затемнён, повторяем напоминание

# Желаемые состояния яркости (поток-контроллер реконсилит к ним вне общего lock).
_BRIGHT_NORMAL = "normal"
_BRIGHT_DIMMED = "dimmed"

_PROC_DEVICES = "/proc/bus/input/devices"


class ActivityMonitorModule(JarvisModule):
    """Детектор активности + напоминания о перерыве (наследник JarvisModule)."""

    def __init__(self):
        super().__init__("jarvis-activity-monitor")

        # --- idle-механизм ---
        # _last_input пишет ТОЛЬКО поток-читатель (на РЕАЛЬНЫЙ ввод) и _say (речь Джарвиса =
        # взаимодействие). Читает ТОЛЬКО поток-логика. Присваивание float атомарно под GIL.
        self._last_input = time.monotonic()
        self._have_devices = False
        self._perm_error = False
        self._no_dev_warned = 0.0

        # --- автомат ---
        self._state = _ACCUMULATING
        self._cycle_target = 0.0        # 23–27 мин (с) — выбирается _new_cycle
        self._break_target = 0.0        # 4–6 мин (с) — длительность перерыва, озвучивается
        self._accumulated = 0.0         # активное время в текущем цикле (с)
        self._offered = False           # был ли предложен перерыв в этом цикле (гейт похвалы)
        self._offer_active_accum = 0.0  # активное время после предложения (с)
        self._remind_target = 0.0       # 5–6 мин (с) — задержка повтора напоминания
        self._remind_active_accum = 0.0  # для повтора напоминания в DIMMED
        self._pending_stop_reply = False
        self._last_tick = time.monotonic()
        self._new_cycle()               # первичные таргеты цикла/перерыва

        # --- эпизод длинного простоя (перерыв/смена деятельности) ---
        self._idle_episode_active = False
        self._episode_start = 0.0
        self._episode_break_required = self._break_target  # фиксируется на входе в эпизод
        self._episode_credited = False
        self._episode_offered = False
        self._work_before_break = 0.0

        # --- яркость (отдельный поток-контроллер, рампа НЕ под общим lock) ---
        self._bright_lock = threading.Lock()
        self._bright_wake = threading.Event()
        self._bright_desired = _BRIGHT_NORMAL   # чего хочет логика
        self._bright_applied = _BRIGHT_NORMAL   # что контроллер уже применил
        self._bright_cancel_debt = False        # пользователь сам сменил яркость → забыть «долг»
        self._dim_saved_raw = None              # исходная яркость (raw) до затемнения; None = не дим
        self._dim_state_file = config.LOGS_DIR / "break_dim_state.json"

        # Снимок для панели `jarvis live` — формируется под _lock в _tick, пишется отдельно.
        self._snapshot = {"working_seconds": 0, "until_offer_seconds": round(self._cycle_target),
                          "on_break": False, "state": self._state}
        self._state_written_at = 0.0

        # Состояние автомата мутируется из ДВУХ потоков: логики (_tick) и MQTT-колбэка
        # (_on_input/_on_execute). Один lock сериализует мутации. _last_input под lock НЕ держим
        # (атомарность float — GIL). Порядок захвата всегда _lock → _bright_lock (без обратного).
        self._lock = threading.Lock()

        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ #
    # Случайные длительности — ТОЛЬКО целые минуты (секунды всегда 00, ТЗ)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _rand_minutes(a_min: int, b_min: int) -> float:
        lo, hi = int(min(a_min, b_min)), int(max(a_min, b_min))
        return float(random.randint(lo, hi) * 60)

    def _new_cycle(self) -> None:
        """Свежий цикл активности: новые таргеты (цикл + длительность перерыва), сброс
        аккумуляторов и флага «перерыв предложен». Состояние → ACCUMULATING."""
        self._cycle_target = self._rand_minutes(config.BREAK_CYCLE_MIN_MINUTES,
                                                config.BREAK_CYCLE_MAX_MINUTES)
        self._break_target = self._rand_minutes(config.BREAK_BREAK_MIN_MINUTES,
                                                config.BREAK_BREAK_MAX_MINUTES)
        self._accumulated = 0.0
        self._offer_active_accum = 0.0
        self._remind_active_accum = 0.0
        self._offered = False
        self._pending_stop_reply = False
        self._state = _ACCUMULATING

    # ------------------------------------------------------------------ #
    # Жизненный цикл
    # ------------------------------------------------------------------ #
    def on_start(self):
        # Рестарт сервиса в затемнённом виде не должен оставить экран тёмным навсегда.
        self._restore_from_state_file()
        if not config.BREAKS_ENABLED:
            self.log.info("Напоминания о перерыве выключены (break_reminders.enabled=false)")
            return
        self.subscribe(contracts.TOPIC_INPUT, self._on_input)
        self.subscribe(contracts.TOPIC_EXECUTE, self._on_execute)
        for target, name in ((self._reader_loop, "input-reader"),
                             (self._logic_loop, "break-logic"),
                             (self._brightness_loop, "break-brightness")):
            t = threading.Thread(target=target, daemon=True, name=name)
            t.start()
            self._threads.append(t)
        self.log.info("Монитор активности запущен (idle через /dev/input)")

    def on_stop(self):
        # Разбудить контроллер яркости (он выйдет по stop_event) и вернуть яркость НЕМЕДЛЕННО
        # (без рампы — _stop_event уже взведён), чтобы не оставить тёмный экран после остановки.
        self._bright_wake.set()
        try:
            self._restore_brightness(immediate=True)
        except Exception:
            self.log_exc(logging.WARNING, "Не удалось вернуть яркость при остановке")
        for t in self._threads:
            try:
                t.join(timeout=2.0)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Поток-читатель: события ввода → _last_input
    # ------------------------------------------------------------------ #
    def _reader_loop(self):
        """Держит устройства ввода открытыми ПОСТОЯННО; периодически синхронизирует набор
        (hotplug), НЕ трогая _last_input (корневой баг: рескан обнулял idle). _last_input
        обновляется ТОЛЬКО на реальном вводе (непустой read) и при ПЕРВОМ появлении устройств."""
        open_fds: dict[str, int] = {}        # путь → fd
        had_devices = False
        next_rescan = 0.0
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if now >= next_rescan:
                    next_rescan = now + config.BREAK_DEVICE_RESCAN
                    self._sync_devices(open_fds)
                if not open_fds:
                    self._have_devices = False
                    had_devices = False
                    if self._stop_event.wait(min(config.BREAK_DEVICE_RESCAN, 5.0)):
                        return
                    continue
                if not had_devices:
                    # Переход «нет устройств → есть»: ТОЛЬКО здесь считаем «сейчас активен»,
                    # чтобы устаревший _last_input не дал ложный простой (до выдачи прав/
                    # перелогина). На периодическом рескане idle НЕ сбрасываем.
                    self._last_input = time.monotonic()
                    had_devices = True
                    self._have_devices = True
                try:
                    readable, _, _ = select.select(list(open_fds.values()), [], [], 1.0)
                except OSError:
                    self.log.debug("select по устройствам сбоил — пересинхронизирую", exc_info=True)
                    self._close_all(open_fds)
                    continue
                for fd in readable:
                    try:
                        data = os.read(fd, 4096)   # содержимое отбрасываем — нужен лишь факт ввода
                    except OSError:
                        # Устройство выдернули — убрать; рескан вернёт его при появлении.
                        self._drop_fd(open_fds, fd)
                        continue
                    if not data:
                        self._drop_fd(open_fds, fd)
                        continue
                    self._last_input = time.monotonic()   # РЕАЛЬНЫЙ ввод
        finally:
            self._close_all(open_fds)

    def _sync_devices(self, open_fds: dict) -> None:
        """Синхронизировать набор открытых устройств с текущими кандидатами (hotplug):
        добавить новые, закрыть исчезнувшие. Уже открытые НЕ трогаем."""
        try:
            wanted = set(self._candidate_event_paths())
        except Exception:
            self.log.debug("не удалось перечислить устройства ввода", exc_info=True)
            return
        for path in list(open_fds):
            if path not in wanted:
                self._drop_path(open_fds, path)
        perm_error = False
        for path in wanted:
            if path in open_fds:
                continue
            try:
                open_fds[path] = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except PermissionError:
                perm_error = True
            except OSError:
                pass
        self._perm_error = perm_error and not open_fds

    @staticmethod
    def _drop_path(open_fds: dict, path: str) -> None:
        fd = open_fds.pop(path, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _drop_fd(self, open_fds: dict, fd: int) -> None:
        for path, f in list(open_fds.items()):
            if f == fd:
                self._drop_path(open_fds, path)
                return

    def _close_all(self, open_fds: dict) -> None:
        for path in list(open_fds):
            self._drop_path(open_fds, path)

    def _candidate_event_paths(self) -> list:
        """Пути /dev/input/eventN устройств-источников ВВОДА по /proc/bus/input/devices.

        Берём указатели (EV_REL/EV_ABS — мышь/тачпад) и настоящие клавиатуры (EV_KEY+EV_REP).
        Так отсекаются датчики/Lid/Power/Video Bus, которые иначе дали бы ложную «активность»."""
        paths: list = []
        try:
            text = Path(_PROC_DEVICES).read_text(encoding="utf-8", errors="replace")
        except Exception:
            self.log.debug("не удалось прочитать %s", _PROC_DEVICES, exc_info=True)
            return paths
        handlers = None
        ev = 0
        for line in text.splitlines():
            if line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1]
            elif line.startswith("B: EV="):
                try:
                    ev = int(line.split("=", 1)[1].strip(), 16)
                except Exception:
                    ev = 0
            elif not line.strip():
                self._maybe_add_device(handlers, ev, paths)
                handlers, ev = None, 0
        self._maybe_add_device(handlers, ev, paths)  # последний блок (без хвостовой пустой строки)
        return paths

    @staticmethod
    def _maybe_add_device(handlers: str | None, ev: int, paths: list) -> None:
        if not handlers or not ev:
            return
        is_pointer = bool(ev & (_EV_REL | _EV_ABS))
        is_keyboard = bool(ev & _EV_KEY) and bool(ev & _EV_REP)
        if not (is_pointer or is_keyboard):
            return
        for token in handlers.split():
            if token.startswith("event"):
                paths.append("/dev/input/" + token)
                return

    # ------------------------------------------------------------------ #
    # Поток-логика: тик автомата
    # ------------------------------------------------------------------ #
    def _logic_loop(self):
        # wait() вернёт True при остановке — тик не задерживает выход.
        while not self._stop_event.wait(config.BREAK_TICK):
            try:
                if self._have_devices:
                    self._tick()
                else:
                    self._tick_no_devices()
                self._write_activity_state()  # снимок для панели jarvis live (throttled)
            except Exception:
                self.log_exc(logging.ERROR, "Сбой тика логики напоминаний — продолжаю")

    def _write_activity_state(self):
        """Снимок состояния для панели `jarvis live` (logs/activity_state.json). Best-effort, не
        чаще раза в 10с. Сериализует готовый снимок (он формируется под _lock в _tick — без
        torn-state)."""
        try:
            now = time.monotonic()
            if now - self._state_written_at < 10.0:
                return
            self._state_written_at = now
            data = dict(self._snapshot)  # _snapshot заменяется целиком под _lock — чтение атомарно
            data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            path = config.LOGS_DIR / "activity_state.json"
            tmp = str(path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            self.log.debug("Не удалось записать activity_state", exc_info=True)

    def _tick_no_devices(self):
        # Нет доступа к устройствам ввода → НЕ выполняем автомат (иначе idle «растёт вечно» →
        # ложный бесконечный перерыв + спам похвалой). Только редкий WARN, голосом молчим.
        now = time.monotonic()
        self._last_tick = now  # чтобы dt не «взорвался», когда устройства появятся
        if now - self._no_dev_warned >= 300:
            self._no_dev_warned = now
            if self._perm_error:
                self.log.warning(
                    "Нет доступа к /dev/input — детектор активности спит. "
                    "Фикс: sudo usermod -aG input $USER + перелогин")
            else:
                self.log.warning("Устройства ввода не найдены — детектор активности спит")

    def _tick(self):
        now = time.monotonic()
        idle = now - self._last_input              # _last_input читается атомарно (GIL)
        with self._lock:
            # dt ограничиваем сверху микропаузой: зависший тик/край не должен разово
            # «впрыснуть» в аккумулятор минуты несуществующей работы.
            dt = min(max(0.0, now - self._last_tick), config.BREAK_MICRO_PAUSE)
            self._last_tick = now
            micro = config.BREAK_MICRO_PAUSE
            reset = config.BREAK_RESET_IDLE

            # ─── Длинный простой (≥ RESET): смена деятельности / зреющий перерыв ───
            if idle >= reset:
                if not self._idle_episode_active:
                    self._begin_idle_episode(now, idle)
                if (not self._episode_credited
                        and (now - self._episode_start) >= self._episode_break_required):
                    self._episode_credited = True
                    self.log.info("Перерыв засчитан (простой ≥ %.0fс)", self._episode_break_required)
            # ─── Возврат к активности после длинного простоя ───
            elif self._idle_episode_active:
                self._return_from_idle_episode(now)
            # ─── Обычная активная логика (idle < RESET) ───
            elif self._state == _ACCUMULATING:
                if idle < micro:                   # микропауза <80с — активность копится
                    self._accumulated += dt
                if self._accumulated >= self._cycle_target:
                    self._offer_break()
            elif self._state == _OFFERED:
                if idle < micro:                   # «игнор» = активная работа после предложения
                    self._offer_active_accum += dt
                if self._offer_active_accum >= self._remind_target:
                    self._enter_dimmed()
            elif self._state == _DIMMED:
                if idle < micro:
                    self._remind_active_accum += dt
                if self._remind_active_accum >= self._remind_target:
                    self._repeat_reminder()

            # Снимок для live-панели (под lock — без torn-state).
            self._snapshot = {
                "working_seconds": round(self._accumulated),
                "until_offer_seconds": round(max(0.0, self._cycle_target - self._accumulated)),
                "on_break": bool(self._idle_episode_active),
                "state": str(self._state),
            }

    # ------------------------------------------------------------------ #
    # Переходы автомата
    # ------------------------------------------------------------------ #
    def _phrase_minutes(self, key: str, pack) -> str:
        """Фраза из пака с подстановкой {минуты} = длительность ПЕРЕРЫВА словами («пять минут»)."""
        text = phrases.pick(key, pack)
        return text.replace("{минуты}", speech.say_duration(self._break_target))

    def _begin_idle_episode(self, now: float, idle: float):
        """Начало длинного простоя (≥ RESET): цикл активности ПОЛНОСТЬЮ сбрасывается (ТЗ).

        Сохраняем контекст для решения о похвале на возврате: сколько отработал и был ли
        перерыв предложен. Длительность перерыва ФИКСИРУЕМ текущим _break_target (тем, что
        озвучивали), а НЕ переслучаиваем — иначе кредит расходился бы с обещанием."""
        self._idle_episode_active = True
        self._episode_start = now - idle           # реальный момент ухода (idle уже накопился)
        self._episode_break_required = self._break_target
        self._episode_credited = False
        self._episode_offered = self._offered
        self._work_before_break = self._accumulated
        self._new_cycle()                          # свежий цикл-задел на возврат
        self.log.info("Простой ≥ %.0fс — цикл активности сброшен (смена деятельности)",
                      config.BREAK_RESET_IDLE)

    def _return_from_idle_episode(self, now: float):
        """Возврат к активности: снять затемнение и, если перерыв засчитан И заслужен, похвалить."""
        self._idle_episode_active = False
        # Похвала только за ОСМЫСЛЕННЫЙ перерыв: засчитан И (был предложен ИЛИ перед ним была
        # заметная работа). Иначе «перерыв» во время фильма со случайным касанием мыши хвалился бы.
        worked_enough = self._work_before_break >= config.BREAK_PRAISE_MIN_WORK
        if self._episode_credited and (self._episode_offered or worked_enough):
            phrase = phrases.pick("break.praise", config.BREAK_PRAISE_PHRASES)
            if phrase:
                self._say(phrase)
            self.publish_event("break_praise")  # лампы: мягкий зелёный пульс
            self.log.info("Пользователь вернулся с перерыва — похвала "
                          "(предложен=%s, отработано=%.0fс)",
                          self._episode_offered, self._work_before_break)
        # Затемнение снимаем при ЛЮБОМ возврате из длинного простоя.
        self._request_brightness(_BRIGHT_NORMAL)
        # Свежий цикл (таргеты уже выбраны в _begin_idle_episode; здесь подстраховка + last_input).
        self._new_cycle()
        self._last_input = now  # возврат = активность (если выше не было _say)

    def _offer_break(self):
        phrase = self._phrase_minutes("break.offer", config.BREAK_OFFER_PHRASES)
        if phrase:
            self._say(phrase)
        self.publish_event("break_offer")  # лампы: мягкий зелёный пульс
        self._state = _OFFERED
        self._offered = True
        self._offer_active_accum = 0.0
        self._remind_target = self._rand_minutes(config.BREAK_REMIND_MIN_MINUTES,
                                                 config.BREAK_REMIND_MAX_MINUTES)
        self._pending_stop_reply = True
        self.log.info("Цикл активности истёк (~%.0fс) — предложен перерыв на %.0f мин",
                      self._cycle_target, self._break_target / 60)

    def _enter_dimmed(self):
        self._request_brightness(_BRIGHT_DIMMED)
        phrase = self._phrase_minutes("break.remind", config.BREAK_REMIND_PHRASES)
        if phrase:
            self._say(phrase)
        self.publish_event("break_offer")
        self._state = _DIMMED
        self._remind_active_accum = 0.0
        self._remind_target = self._rand_minutes(config.BREAK_REMIND_MIN_MINUTES,
                                                 config.BREAK_REMIND_MAX_MINUTES)
        self.log.info("Предложение проигнорировано — экран затемнён, напоминание повторено")

    def _repeat_reminder(self):
        phrase = self._phrase_minutes("break.remind", config.BREAK_REMIND_PHRASES)
        if phrase:
            self._say(phrase)
        self.publish_event("break_offer")
        self._remind_active_accum = 0.0
        self._remind_target = self._rand_minutes(config.BREAK_REMIND_MIN_MINUTES,
                                                 config.BREAK_REMIND_MAX_MINUTES)
        self.log.info("Повторное напоминание о перерыве")

    # ------------------------------------------------------------------ #
    # События шины
    # ------------------------------------------------------------------ #
    def _on_input(self, payload: dict):
        text = (payload.get("text") or "").strip()
        if not text:
            return
        # НАМЕРЕННАЯ команда (обращение «Джарвис»/PTT, wake=true) = активность: диктовка команд без
        # касания мыши не должна уводить в ложный простой. А вот ФОНОВУЮ речь (фильм/ТВ — wake=false)
        # активностью НЕ считаем: иначе фильм со звуком не давал бы расти простою (нарушение «фильм=
        # простой»). Нет поля wake = true (обратная совместимость).
        if payload.get("wake", True):
            self._last_input = time.monotonic()
        if is_stop_phrase(text):
            self._handle_stop_phrase()

    def _handle_stop_phrase(self):
        with self._lock:
            # Реагируем (ответ+сброс+яркость) только в контексте напоминания/затемнения; вне его —
            # тихо (core уже подавил «не разобрал»), чтобы случайная стоп-фраза не сбивала цикл.
            acting = (self._pending_stop_reply or self._state in (_OFFERED, _DIMMED)
                      or self._dim_saved_raw is not None)
            if not acting:
                self.log.debug("Стоп-фраза вне контекста напоминания — игнорирую")
                return
            reply = phrases.pick("break.reply", config.BREAK_STOP_REPLIES)
            if reply:
                self._say(reply)
            self._request_brightness(_BRIGHT_NORMAL)
            self._new_cycle()
            self._idle_episode_active = False
            self.log.info("Стоп-фраза принята — цикл сброшен, яркость возвращена")

    def _on_execute(self, payload: dict):
        # Пользователь сам поменял яркость голосом (commands.yaml brightness_*) — отменяем
        # «долг» восстановления, чтобы не воевать с ним (рассинхрон состояния яркости).
        tag = str(payload.get("command_tag") or "")
        if not tag.startswith("brightness"):
            return
        with self._lock:
            if self._dim_saved_raw is not None or self._bright_applied == _BRIGHT_DIMMED:
                self.log.info("Голосовая команда яркости (%s) — отменяю восстановление затемнения", tag)
                self._cancel_dim_debt()

    def _say(self, text: str):
        # Собственная речь Джарвиса = взаимодействие: обновляем _last_input, чтобы пауза «пока
        # он говорит» не засчиталась как начало простоя/перерыва (и не было само-сброса цикла).
        self._last_input = time.monotonic()
        self.say(text)

    # ------------------------------------------------------------------ #
    # Яркость: ОТДЕЛЬНЫЙ поток-контроллер (рампа brightnessctl вне общего lock)
    # ------------------------------------------------------------------ #
    def _request_brightness(self, desired: str) -> None:
        """Логика лишь ЗАЯВЛЯЕТ желаемое состояние экрана и будит контроллер (рампу он делает
        сам, вне _lock — раньше 2с-рампа держала lock и тормозила ответ на стоп-фразу)."""
        with self._bright_lock:
            self._bright_desired = desired
        self._bright_wake.set()

    def _cancel_dim_debt(self) -> None:
        """Забыть «долг» восстановления (пользователь сам управляет яркостью). Зовётся под _lock."""
        with self._bright_lock:
            self._bright_desired = _BRIGHT_NORMAL
            self._bright_cancel_debt = True
        self._bright_wake.set()

    def _brightness_loop(self):
        """Реконсилит фактическую яркость к желаемой. Единственный поток, трогающий
        _dim_saved_raw и brightnessctl → нет гонок. Рампа здесь не блокирует логику/MQTT."""
        while not self._stop_event.is_set():
            self._bright_wake.wait(timeout=1.0)
            if self._stop_event.is_set():
                return
            self._bright_wake.clear()
            with self._bright_lock:
                desired = self._bright_desired
                cancel = self._bright_cancel_debt
                self._bright_cancel_debt = False
            try:
                if cancel:
                    # Пользователь сам сменил яркость: забыть исходную, считать «нормой» (без рампы).
                    self._dim_saved_raw = None
                    self._clear_dim_state()
                    self._bright_applied = _BRIGHT_NORMAL
                    continue
                if desired == self._bright_applied:
                    continue
                if desired == _BRIGHT_DIMMED:
                    self._dim_screen()
                    self._bright_applied = _BRIGHT_DIMMED
                else:
                    self._restore_brightness()
                    self._bright_applied = _BRIGHT_NORMAL
            except Exception:
                self.log_exc(logging.WARNING, "Сбой контроллера яркости — продолжаю")

    @staticmethod
    def _bctl(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["brightnessctl", *args], capture_output=True, text=True,
                              timeout=3, check=False)

    def _read_brightness(self) -> tuple:
        try:
            cur = int(self._bctl("get").stdout.strip())
            mx = int(self._bctl("max").stdout.strip())
            return cur, mx
        except Exception:
            self.log.debug("не удалось снять яркость (brightnessctl)", exc_info=True)
            return None, None

    def _set_brightness_raw(self, raw: int):
        try:
            self._bctl("-q", "set", str(int(raw)))
        except Exception:
            self.log.debug("не удалось установить яркость %s", raw, exc_info=True)

    def _ramp_brightness(self, frm: int, to: int, immediate: bool = False):
        if immediate:
            self._set_brightness_raw(to)
            return
        steps = max(1, int(config.BREAK_DIM_STEPS))
        dur = max(0.0, float(config.BREAK_DIM_RAMP_SECONDS))
        for i in range(1, steps + 1):
            if self._stop_event.is_set():
                break
            v = int(round(frm + (to - frm) * (i / steps)))
            self._set_brightness_raw(v)
            if i < steps and dur > 0:
                self._stop_event.wait(dur / steps)

    def _dim_screen(self):
        cur, mx = self._read_brightness()
        if cur is None or mx is None or mx <= 0:
            return
        if self._dim_saved_raw is None:
            self._dim_saved_raw = cur
            self._write_dim_state(cur)
        floor = int(mx * config.BREAK_DIM_FLOOR_PERCENT / 100.0)
        target = int(round(self._dim_saved_raw * (1.0 - config.BREAK_DIM_PERCENT / 100.0)))
        target = max(floor, min(target, self._dim_saved_raw))
        self._ramp_brightness(cur, target)
        self.log.info("Экран затемнён: %d → %d (−%d%%)", cur, target, config.BREAK_DIM_PERCENT)

    def _restore_brightness(self, immediate: bool = False):
        if self._dim_saved_raw is None:
            return  # не затемняли — нечего возвращать (no-op)
        target = self._dim_saved_raw
        try:
            cur, _ = self._read_brightness()
            self._ramp_brightness(cur if cur is not None else target, target, immediate=immediate)
            self.log.info("Яркость восстановлена → %d", target)
        except Exception:
            self.log_exc(logging.WARNING, "Не удалось восстановить яркость")
        finally:
            self._dim_saved_raw = None
            self._clear_dim_state()

    # --- state-файл затемнения (переживает рестарт сервиса) ---
    def _write_dim_state(self, raw: int):
        try:
            self._dim_state_file.write_text(json.dumps({"saved_raw": int(raw)}), encoding="utf-8")
        except Exception:
            self.log.debug("не удалось записать %s", self._dim_state_file, exc_info=True)

    def _clear_dim_state(self):
        try:
            self._dim_state_file.unlink(missing_ok=True)
        except Exception:
            pass

    def _restore_from_state_file(self):
        try:
            if not self._dim_state_file.exists():
                return
            data = json.loads(self._dim_state_file.read_text(encoding="utf-8"))
            raw = int(data.get("saved_raw"))
            self.log.info("Сервис стартовал в затемнённом виде — восстанавливаю яркость → %d", raw)
            self._dim_saved_raw = raw
            self._restore_brightness(immediate=True)
        except Exception:
            self.log_exc(logging.WARNING, "Не удалось восстановить яркость из state-файла")
            self._clear_dim_state()


def main():
    ActivityMonitorModule().run()


if __name__ == "__main__":
    main()
