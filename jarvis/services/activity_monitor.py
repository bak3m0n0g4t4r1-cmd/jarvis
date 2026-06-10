"""«Внимание» — детектор активности и напоминания о перерыве.

Следит, что пользователь ФИЗИЧЕСКИ работает за ПК (мышь/клавиатура/тачпад), и по
истечении случайного цикла (20–30 мин) вежливо предлагает перерыв «в характере»; при
игноре — мягко затемняет экран и повторяет напоминание; за полноценный перерыв — хвалит;
по стоп-фразе — отвечает и сбрасывает цикл.

Почему /dev/input, а не D-Bus idle: на KDE6/Wayland опрос idle недоступен
(`org.freedesktop.ScreenSaver.GetSessionIdleTime` → NotSupported), а событийный idle
(swayidle/ext-idle) УВАЖАЕТ idle-инхибиторы → полноэкранное видео считалось бы
активностью. Чтение событий ввода даёт ЧИСТО-ВХОДНОЙ простой: фильм/музыка/чтение без
касания = простой растёт (это НЕ активность), как и требует ТЗ. Читаем ТОЛЬКО тайминг
событий (факт нажатия), НЕ их содержимое.

Лёгкость: поток-читатель блокируется в select() на 2–3 fd (≈0% CPU, просыпается только на
ввод); поток-логика тикает раз в несколько секунд. Всё в try-except — сервис не падает.

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
from pathlib import Path

from jarvis import config, contracts, phrases
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

_PROC_DEVICES = "/proc/bus/input/devices"


class ActivityMonitorModule(JarvisModule):
    """Детектор активности + напоминания о перерыве (наследник JarvisModule)."""

    def __init__(self):
        super().__init__("jarvis-activity-monitor")

        # --- idle-механизм ---
        # _last_input пишет ТОЛЬКО поток-читатель, читает ТОЛЬКО поток-логика. Присваивание
        # float атомарно под GIL → отдельный lock не нужен (потеря одного события при
        # переэнумерации устройств несущественна при тике в секунды).
        self._last_input = time.monotonic()
        self._have_devices = False
        self._perm_error = False
        self._no_dev_warned = 0.0

        # --- автомат ---
        self._state = _ACCUMULATING
        self._cycle_target = self._rand_cycle()      # 20–30 мин (с)
        self._accumulated = 0.0                      # активное время в текущем цикле (с)
        self._offer_active_accum = 0.0               # активное время после предложения (с)
        self._remind_target = self._rand_remind()    # 5–6 мин (с)
        self._remind_active_accum = 0.0              # для повтора напоминания в DIMMED
        self._next_remind_target = self._rand_remind()
        self._pending_stop_reply = False
        self._last_tick = time.monotonic()

        # --- эпизод длинного простоя (перерыв/смена деятельности) ---
        self._idle_episode_active = False
        self._episode_start = 0.0
        self._episode_break_required = self._rand_break()  # 4–5 мин (с)
        self._episode_credited = False
        self._work_before_break = 0.0

        # --- яркость ---
        self._dim_saved_raw = None                   # исходная яркость (raw) до затемнения; None = не затемняли
        self._dim_state_file = config.LOGS_DIR / "break_dim_state.json"

        # Выбор фраз (без повторов в цикле) — общий механизм jarvis.phrases; состояние
        # циклов хранится в нём по ключам пака ("break.offer"/"break.praise"/"break.reply"),
        # поэтому локальные индексы здесь больше не нужны.

        # Состояние автомата мутируется из ДВУХ потоков: логики (_tick) и MQTT-колбэка
        # (_on_input/_on_execute). Один lock сериализует мутации. _last_input под lock НЕ
        # держим — это «горячее» поле, атомарно пишется читателем/_say (атомарность float — GIL).
        self._lock = threading.Lock()

        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ #
    # Случайные длительности (перевыбор на каждый цикл/перерыв/напоминание)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _rand_between(a: float, b: float) -> float:
        return random.uniform(min(a, b), max(a, b))

    def _rand_cycle(self) -> float:
        return self._rand_between(config.BREAK_CYCLE_MIN, config.BREAK_CYCLE_MAX)

    def _rand_break(self) -> float:
        return self._rand_between(config.BREAK_MIN_SECONDS, config.BREAK_MAX_SECONDS)

    def _rand_remind(self) -> float:
        return self._rand_between(config.BREAK_REMIND_MIN, config.BREAK_REMIND_MAX)

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
                             (self._logic_loop, "break-logic")):
            t = threading.Thread(target=target, daemon=True, name=name)
            t.start()
            self._threads.append(t)
        self.log.info("Монитор активности запущен (idle через /dev/input)")

    def on_stop(self):
        # Вернуть яркость НЕМЕДЛЕННО (без рампы — _stop_event уже взведён, рампа прервётся),
        # чтобы не оставить тёмный экран после остановки сервиса. Потоки-демоны выйдут сами.
        try:
            with self._lock:
                self._restore_brightness(immediate=True)
        except Exception:
            self.log_exc(logging.WARNING, "Не удалось вернуть яркость при остановке")

    # ------------------------------------------------------------------ #
    # Поток-читатель: события ввода → _last_input
    # ------------------------------------------------------------------ #
    def _reader_loop(self):
        while not self._stop_event.is_set():
            fds = self._open_devices()
            if not fds:
                self._have_devices = False
                if self._stop_event.wait(config.BREAK_DEVICE_RESCAN):
                    return
                continue
            # Появились устройства — считаем «сейчас активен», чтобы не было ложного
            # простоя из-за устаревшего _last_input (например, до выдачи прав на /dev/input).
            self._last_input = time.monotonic()
            self._have_devices = True
            deadline = time.monotonic() + config.BREAK_DEVICE_RESCAN
            try:
                while not self._stop_event.is_set() and time.monotonic() < deadline:
                    readable, _, _ = select.select(fds, [], [], 1.0)
                    if not readable:
                        continue
                    self._last_input = time.monotonic()
                    for fd in readable:
                        data = os.read(fd, 4096)   # содержимое отбрасываем — нужен лишь факт ввода
                        if not data:
                            raise OSError("устройство ввода закрылось")
            except OSError:
                # Устройство выдернули/закрылось — переэнумерация на следующей итерации.
                self.log.debug("Сбой чтения устройства ввода — переоткрываю", exc_info=True)
            finally:
                self._close_fds(fds)

    def _open_devices(self) -> list:
        """Открыть подходящие устройства ввода (клавиатура/мышь/тачпад). Возвращает список
        файловых дескрипторов (int). При отсутствии прав ставит _perm_error."""
        fds: list = []
        perm_error = False
        for path in self._candidate_event_paths():
            try:
                fds.append(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
            except PermissionError:
                perm_error = True
            except OSError:
                pass
        self._perm_error = perm_error and not fds
        return fds

    @staticmethod
    def _close_fds(fds: list) -> None:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass

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
        """Снимок состояния для панели `jarvis live` (logs/activity_state.json). Best-effort, не чаще
        раза в 10с. Поля: работает (с), до напоминания о перерыве (с), идёт ли перерыв."""
        try:
            now = time.monotonic()
            if now - getattr(self, "_state_written_at", 0.0) < 10.0:
                return
            self._state_written_at = now
            import json
            from datetime import datetime
            data = {
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "working_seconds": round(self._accumulated),
                "until_offer_seconds": round(max(0.0, self._cycle_target - self._accumulated)),
                "on_break": bool(self._idle_episode_active),
                "state": str(self._state),
            }
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
            dt = max(0.0, now - self._last_tick)
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
                return

            # ─── Возврат к активности после длинного простоя ───
            if self._idle_episode_active:
                self._return_from_idle_episode(now)
                return

            # ─── Обычная активная логика (idle < RESET) ───
            if self._state == _ACCUMULATING:
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
                if self._remind_active_accum >= self._next_remind_target:
                    self._repeat_reminder()

    # ------------------------------------------------------------------ #
    # Переходы автомата
    # ------------------------------------------------------------------ #
    def _begin_idle_episode(self, now: float, idle: float):
        """Начало длинного простоя (≥ RESET): цикл активности ПОЛНОСТЬЮ сбрасывается (ТЗ)."""
        self._idle_episode_active = True
        self._episode_start = now - idle           # реальный момент ухода (idle уже накопился)
        self._episode_break_required = self._rand_break()
        self._episode_credited = False
        self._work_before_break = self._accumulated
        self._accumulated = 0.0
        self._cycle_target = self._rand_cycle()
        self._offer_active_accum = 0.0
        self._pending_stop_reply = False
        self._state = _ACCUMULATING
        self.log.info("Простой ≥ %.0fс — цикл активности сброшен (смена деятельности)",
                      config.BREAK_RESET_IDLE)

    def _return_from_idle_episode(self, now: float):
        """Возврат к активности: снять затемнение и, если перерыв засчитан, похвалить."""
        self._idle_episode_active = False
        credited = self._episode_credited
        worked_enough = self._work_before_break >= config.BREAK_PRAISE_MIN_WORK
        # Затемнение снимаем при ЛЮБОМ возврате из длинного простоя (нудж отработал — не
        # держим тёмный экран после того, как человек уже отошёл и вернулся).
        self._restore_brightness()
        if credited and worked_enough:
            phrase = phrases.pick("break.praise", config.BREAK_PRAISE_PHRASES)
            if phrase:
                self._say(phrase)
            self.log.info("Пользователь вернулся с перерыва — похвала")
        # Свежий цикл.
        self._state = _ACCUMULATING
        self._accumulated = 0.0
        self._cycle_target = self._rand_cycle()
        self._offer_active_accum = 0.0
        self._pending_stop_reply = False
        self._last_input = now  # возврат = активность (если не было _say выше)

    def _offer_break(self):
        phrase = phrases.pick("break.offer", config.BREAK_OFFER_PHRASES)
        if phrase:
            self._say(phrase)
        self._state = _OFFERED
        self._offer_active_accum = 0.0
        self._remind_target = self._rand_remind()
        self._pending_stop_reply = True
        self.log.info("Цикл активности истёк (~%.0fс) — предложен перерыв", self._cycle_target)

    def _enter_dimmed(self):
        self._dim_screen()
        phrase = phrases.pick("break.offer", config.BREAK_OFFER_PHRASES)
        if phrase:
            self._say(phrase)
        self._state = _DIMMED
        self._remind_active_accum = 0.0
        self._next_remind_target = self._rand_remind()
        self.log.info("Предложение проигнорировано — экран затемнён, напоминание повторено")

    def _repeat_reminder(self):
        phrase = phrases.pick("break.offer", config.BREAK_OFFER_PHRASES)
        if phrase:
            self._say(phrase)
        self._remind_active_accum = 0.0
        self._next_remind_target = self._rand_remind()
        self.log.info("Повторное напоминание о перерыве")

    # ------------------------------------------------------------------ #
    # События шины
    # ------------------------------------------------------------------ #
    def _on_input(self, payload: dict):
        text = (payload.get("text") or "").strip()
        if not text:
            return
        # НАМЕРЕННАЯ команда (обращение «Джарвис»/PTT, wake=true) = активность: диктовка команд без
        # касания мыши не должна уводить в ложный простой. А вот ФОНОВУЮ речь (фильм/ТВ — wake=false,
        # ТЗ-5) активностью НЕ считаем: иначе фильм со звуком не давал бы расти простою (нарушение ТЗ-9
        # «фильм=простой»). Нет поля wake = true (обратная совместимость).
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
            self._restore_brightness()
            self._state = _ACCUMULATING
            self._accumulated = 0.0
            self._cycle_target = self._rand_cycle()
            self._offer_active_accum = 0.0
            self._pending_stop_reply = False
            self._idle_episode_active = False
            self.log.info("Стоп-фраза принята — цикл сброшен, яркость возвращена")

    def _on_execute(self, payload: dict):
        # Пользователь сам поменял яркость голосом (commands.yaml brightness_*) — отменяем
        # «долг» восстановления, чтобы не воевать с ним (рассинхрон состояния яркости).
        tag = str(payload.get("command_tag") or "")
        if not tag.startswith("brightness"):
            return
        with self._lock:
            if self._dim_saved_raw is not None:
                self.log.info("Голосовая команда яркости (%s) — отменяю восстановление затемнения", tag)
                self._dim_saved_raw = None
                self._clear_dim_state()

    def _say(self, text: str):
        # Собственная речь Джарвиса = взаимодействие: обновляем _last_input, чтобы пауза «пока
        # он говорит» не засчиталась как начало простоя/перерыва (и не было само-сброса цикла).
        self._last_input = time.monotonic()
        self.say(text)

    # ------------------------------------------------------------------ #
    # Яркость (brightnessctl, плавная рампа, без OSD)
    # ------------------------------------------------------------------ #
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
        try:
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
        except Exception:
            self.log_exc(logging.WARNING, "Не удалось затемнить экран")

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
