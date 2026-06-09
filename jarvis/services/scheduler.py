"""«Планировщик» — будильники (утренний + обычные). Наследник JarvisModule, needs_audio=False.

Слушает jarvis/input ПАРАЛЛЕЛЬНО с core (как activity_monitor): распознаёт команды будильника
(гейт + парсер из jarvis/alarms), мутирует расписание schedule.yaml, отвечает паком фраз. core при
этом молчит на эти команды (хук is_alarm_command). Тик-цикл (раз в ~15с, wall-clock) проверяет
наступление времени и озвучивает срабатывание; состояние переживает перезапуск (schedule.yaml +
logs/scheduler_state.json — анти-двойное срабатывание). Утренний — singleton (ежедневный по
умолчанию), будит ГРОМКО (min_volume, опц. нарастание); обычных — сколько угодно (разовые, метки).

Всё в try-except: сбой одной команды/тика не роняет сервис.
"""
import json
import logging
import threading
from datetime import datetime, time as dtime, timedelta
from difflib import SequenceMatcher

from jarvis import alarms, config, contracts, phrases, timers, weather
from jarvis.bus import JarvisModule
from jarvis.speech import say_clock, say_duration, say_temperature

_PLACEHOLDERS = ("{время}", "{метка}", "{погода}", "{темп_макс}", "{темп_мин}",
                 "{длительность}", "{остаток}", "{прошло}")


def _fmt(template: str, **kw) -> str:
    """Подставить плейсхолдеры в шаблон фразы (безопасно, без .format — стрэй-скобки не ломают)."""
    out = template or ""
    for ph in _PLACEHOLDERS:
        key = ph[1:-1]
        if key in kw and kw[key] is not None:
            out = out.replace(ph, str(kw[key]))
    return out


class SchedulerModule(JarvisModule):
    """Планировщик будильников: распознаёт команды и озвучивает срабатывания."""

    def __init__(self):
        super().__init__("jarvis-scheduler")
        self._lock = threading.Lock()              # сериализует команды и тик (общий доступ к файлу/_fired)
        self._fired: dict[str, str] = {}           # id будильника → метка последнего срабатывания
        self._state_file = config.LOGS_DIR / "scheduler_state.json"
        self._weather_cache = None                 # (дата_iso, погода|None) — кэш погоды на день
        self._thread = None

    # ------------------------------------------------------------------ #
    # Жизненный цикл
    # ------------------------------------------------------------------ #
    def on_start(self):
        if not config.ALARMS_ENABLED:
            self.log.info("Будильники выключены (alarms.enabled=false) — планировщик простаивает")
            return
        self._load_state()
        self.subscribe(contracts.TOPIC_INPUT, self._on_input)
        self._thread = threading.Thread(target=self._tick_loop, daemon=True, name="scheduler-tick")
        self._thread.start()
        try:
            n = len(alarms.read_schedule().get("будильники", []))
        except Exception:
            n = 0
        self.log.info("Планировщик запущен (будильников в расписании: %d)", n)

    def on_stop(self):
        try:
            if self._thread is not None:
                self._thread.join(timeout=2.0)
        except Exception:
            self.log.debug("Ошибка ожидания тик-потока планировщика", exc_info=True)

    def _load_state(self):
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text(encoding="utf-8")) or {}
                fired = data.get("последнее_срабатывание")
                self._fired = fired if isinstance(fired, dict) else {}
        except Exception:
            self.log_exc(logging.WARNING, "Не удалось прочитать состояние планировщика — начинаю с чистого")
            self._fired = {}

    def _save_state(self):
        try:
            self._state_file.write_text(
                json.dumps({"последнее_срабатывание": self._fired}, ensure_ascii=False),
                encoding="utf-8")
        except Exception:
            self.log.debug("Состояние планировщика не сохранено", exc_info=True)

    # ------------------------------------------------------------------ #
    # Озвучка
    # ------------------------------------------------------------------ #
    def _say(self, text: str, user_level=None, min_volume=None, chime=False):
        """Реплика в jarvis/say с опц. адаптивной громкостью (user_level) или жёстким нижним
        пределом (min_volume — будильник/таймер, обходит «тихо→тихо»); chime — сигнал перед фразой."""
        if not text:
            return
        payload = {"text": text, "source": self.name}
        if user_level is not None:
            payload["user_level"] = user_level
        if min_volume is not None:
            payload["min_volume"] = min_volume
        if chime:
            payload["chime"] = True
        self.publish_json(contracts.TOPIC_SAY, payload, qos=contracts.QOS_SAY)

    def _reply(self, key, pack, user_level, **fmt):
        """Выбрать фразу из пака (без повторов) + подставить плейсхолдеры + озвучить (адаптивно)."""
        self._say(_fmt(phrases.pick(key, pack), **fmt), user_level=user_level)

    # ------------------------------------------------------------------ #
    # Обработка голосовых команд
    # ------------------------------------------------------------------ #
    def _on_input(self, payload: dict):
        try:
            text = (payload.get("text") or "").strip()
            if not text:
                return
            level = payload.get("user_level")
            user_level = level if isinstance(level, (int, float)) else None
            # Маршрутизация: будильник → таймер → секундомер (гейты разводят по ключевым словам).
            with self._lock:
                if alarms.is_alarm_command(text):
                    self._handle_command(text, user_level)
                elif timers.is_timer_command(text):
                    self._handle_timer(text, user_level)
                elif timers.is_stopwatch_command(text):
                    self._handle_stopwatch(text, user_level)
        except Exception:
            self.log_exc(logging.ERROR, "Сбой обработки команды планировщика — продолжаю")

    def _handle_command(self, text: str, user_level):
        cmd = alarms.parse_command(text)
        if not cmd:
            self.log.info("Будильник: команда не разобрана (%s) — переспрашиваю время", text)
            self._reply("alarm.need_time", config.ALARM_NEED_TIME, user_level)
            return
        self.log.info("Будильник: команда %s/%s время=%s метка=%s (%s)",
                      cmd["действие"], cmd["тип"], cmd.get("час"), cmd.get("метка"), text)
        sched = alarms.read_schedule()
        if cmd["тип"] == "morning":
            changed = self._cmd_morning(cmd, sched, user_level)
        else:
            changed = self._cmd_regular(cmd, sched, user_level)
        if changed:
            alarms.write_schedule(sched)
            self._save_state()

    # ---- Утренний (singleton) ---- #
    def _cmd_morning(self, cmd, sched, user_level) -> bool:
        lst = sched["будильники"]
        morning = next((a for a in lst if a.get("тип") == "утренний"), None)
        action = cmd["действие"]

        if action == "cancel":
            if not morning:
                self._reply("alarm.morning_none", config.ALARM_MORNING_NONE, user_level)
                return False
            lst.remove(morning)
            self._fired.pop(morning.get("id", ""), None)
            self._reply("alarm.morning_cancel", config.ALARM_MORNING_CANCEL, user_level)
            return True

        if cmd["час"] is None:  # set/move без времени
            if action == "move" and not morning:
                self._reply("alarm.morning_none", config.ALARM_MORNING_NONE, user_level)
            else:
                self._reply("alarm.need_time", config.ALARM_NEED_TIME, user_level)
            return False

        hhmm = f"{cmd['час']:02d}:{cmd['минута']:02d}"
        время = say_clock(cmd["час"], cmd["минута"])

        if action == "move":
            if not morning:
                self._reply("alarm.morning_none", config.ALARM_MORNING_NONE, user_level)
                return False
            if morning.get("время") == hhmm:
                self._reply("alarm.morning_already_move", config.ALARM_MORNING_ALREADY_MOVE,
                            user_level, время=время)
                return False
            morning["время"] = hhmm
            self._fired.pop(morning.get("id", ""), None)
            self._reply("alarm.morning_move", config.ALARM_MORNING_MOVE, user_level, время=время)
            return True

        # action == "set" (перезапись существующего или создание)
        if morning and morning.get("время") == hhmm:
            self._reply("alarm.morning_already", config.ALARM_MORNING_ALREADY, user_level, время=время)
            return False
        repeat = "ежедневный" if config.ALARM_MORNING_DAILY else "разовый"
        if morning:
            morning["время"] = hhmm
            morning["повтор"] = repeat
            morning["активен"] = True
            self._fired.pop(morning.get("id", ""), None)
        else:
            lst.append(self._make_alarm("утренний", hhmm, None, repeat, "morning"))
        self._reply("alarm.morning_set", config.ALARM_MORNING_SET, user_level, время=время)
        return True

    # ---- Обычные (множественные, метки) ---- #
    def _cmd_regular(self, cmd, sched, user_level) -> bool:
        lst = sched["будильники"]
        regs = [a for a in lst if a.get("тип") == "обычный"]
        action = cmd["действие"]
        label = (cmd.get("метка") or "").strip() or None

        if action == "delete_all":
            if not regs:
                self._reply("alarm.regular_none_found", config.ALARM_REGULAR_NONE_FOUND, user_level)
                return False
            sched["будильники"] = [a for a in lst if a.get("тип") != "обычный"]
            for a in regs:
                self._fired.pop(a.get("id", ""), None)
            self._reply("alarm.regular_delete_all", config.ALARM_REGULAR_DELETE_ALL, user_level)
            return True

        if action == "cancel":
            target = self._find_regular(regs, label, cmd.get("час"), cmd.get("минута"))
            if not target:
                self._reply("alarm.regular_none_found", config.ALARM_REGULAR_NONE_FOUND, user_level)
                return False
            lst.remove(target)
            self._fired.pop(target.get("id", ""), None)
            if (target.get("метка") or "").strip():
                self._reply("alarm.regular_cancel_label", config.ALARM_REGULAR_CANCEL_LABEL,
                            user_level, метка=target["метка"])
            else:
                self._reply("alarm.regular_cancel", config.ALARM_REGULAR_CANCEL, user_level)
            return True

        if action == "move":
            target = self._find_regular(regs, label, None, None)
            if not target:
                self._reply("alarm.regular_none_found", config.ALARM_REGULAR_NONE_FOUND, user_level)
                return False
            if cmd["час"] is None:
                self._reply("alarm.need_time", config.ALARM_NEED_TIME, user_level)
                return False
            hhmm = f"{cmd['час']:02d}:{cmd['минута']:02d}"
            время = say_clock(cmd["час"], cmd["минута"])
            if target.get("время") == hhmm:
                self._reply("alarm.regular_already_move", config.ALARM_REGULAR_ALREADY_MOVE,
                            user_level, время=время)
                return False
            target["время"] = hhmm
            self._fired.pop(target.get("id", ""), None)
            self._reply("alarm.regular_move", config.ALARM_REGULAR_MOVE, user_level, время=время)
            return True

        # action == "set"
        if cmd["час"] is None:
            self._reply("alarm.need_time", config.ALARM_NEED_TIME, user_level)
            return False
        hhmm = f"{cmd['час']:02d}:{cmd['минута']:02d}"
        время = say_clock(cmd["час"], cmd["минута"])
        # Дубль: то же время И та же метка (одинаковый будильник) → «уже стоит».
        dup = next((a for a in regs if a.get("время") == hhmm
                    and ((a.get("метка") or "").strip() or None) == label), None)
        if dup:
            self._reply("alarm.regular_already", config.ALARM_REGULAR_ALREADY, user_level, время=время)
            return False
        lst.append(self._make_alarm("обычный", hhmm, label, "разовый",
                                    self._gen_regular_id(regs, hhmm, label)))
        if label:
            self._reply("alarm.regular_set_label", config.ALARM_REGULAR_SET_LABEL,
                        user_level, время=время, метка=label)
        else:
            self._reply("alarm.regular_set", config.ALARM_REGULAR_SET, user_level, время=время)
        # Предложить метку: стало ≥2 обычных и есть будильник без метки (чтобы потом различать).
        regs_now = [a for a in lst if a.get("тип") == "обычный"]
        if len(regs_now) >= 2 and any(not (a.get("метка") or "").strip() for a in regs_now):
            self._reply("alarm.suggest_label", config.ALARM_SUGGEST_LABEL, user_level)
        return True

    # ------------------------------------------------------------------ #
    # Вспомогательное
    # ------------------------------------------------------------------ #
    @staticmethod
    def _make_alarm(тип, время, метка, повтор, aid):
        return {"тип": тип, "время": время, "метка": метка, "повтор": повтор,
                "активен": True, "id": aid}

    @staticmethod
    def _gen_regular_id(regs, hhmm, label):
        base = "reg:" + (alarms._norm(label) if label else hhmm.replace(":", ""))
        existing = {a.get("id") for a in regs}
        rid, n = base, 2
        while rid in existing:
            rid = f"{base}#{n}"
            n += 1
        return rid

    @staticmethod
    def _find_regular(regs, label, hour, minute):
        """Найти обычный будильник: по метке (точно/нечётко), иначе по времени, иначе —
        единственный активный. Неоднозначно (несколько без критерия) → None."""
        active = [a for a in regs if a.get("активен")]
        if label:
            ln = alarms._norm(label)
            for a in active:
                am = alarms._norm(a.get("метка") or "")
                if am and (am == ln or SequenceMatcher(None, am, ln).ratio() >= 0.8):
                    return a
            return None
        if hour is not None:
            hhmm = f"{hour:02d}:{minute:02d}"
            for a in active:
                if a.get("время") == hhmm:
                    return a
            return None
        return active[0] if len(active) == 1 else None

    # ------------------------------------------------------------------ #
    # ТАЙМЕРЫ (обратный отсчёт)
    # ------------------------------------------------------------------ #
    def _handle_timer(self, text, user_level):
        cmd = timers.parse_timer_command(text)
        if not cmd:
            self._reply("timer.need_duration", config.TIMER_NEED_DURATION, user_level)
            return
        self.log.info("Таймер: команда %s длит=%s метка=%s (%s)",
                      cmd["действие"], cmd.get("длительность"), cmd.get("метка"), text)
        sched = alarms.read_schedule()
        if self._cmd_timer(cmd, sched, user_level):
            alarms.write_schedule(sched)

    def _cmd_timer(self, cmd, sched, user_level) -> bool:
        lst = sched["таймеры"]
        active = [t for t in lst if t.get("активен")]
        action = cmd["действие"]
        label = (cmd.get("метка") or "").strip() or None
        dur = cmd.get("длительность")

        if action == "delete_all":
            if not active:
                self._reply("timer.none_found", config.TIMER_NONE_FOUND, user_level)
                return False
            sched["таймеры"] = [t for t in lst if not t.get("активен")]
            self._reply("timer.delete_all", config.TIMER_DELETE_ALL, user_level)
            return True

        if action == "query":
            target = self._find_active(active, label)
            if not target:
                self._reply("timer.none_found", config.TIMER_NONE_FOUND, user_level)
                return False
            ост = say_duration(max(0, self._timer_remaining(target)))
            if (target.get("метка") or "").strip():
                self._reply("timer.remaining_label", config.TIMER_REMAINING_LABEL,
                            user_level, остаток=ост, метка=target["метка"])
            else:
                self._reply("timer.remaining", config.TIMER_REMAINING, user_level, остаток=ост)
            return False  # запрос не меняет файл

        if action == "cancel":
            target = self._find_active(active, label)
            if not target:
                self._reply("timer.none_found", config.TIMER_NONE_FOUND, user_level)
                return False
            lst.remove(target)
            if (target.get("метка") or "").strip():
                self._reply("timer.cancel_label", config.TIMER_CANCEL_LABEL,
                            user_level, метка=target["метка"])
            else:
                self._reply("timer.cancel", config.TIMER_CANCEL, user_level)
            return True

        if action == "move":
            target = self._find_active(active, label)
            if not target:
                self._reply("timer.none_found", config.TIMER_NONE_FOUND, user_level)
                return False
            if not dur:
                self._reply("timer.need_duration", config.TIMER_NEED_DURATION, user_level)
                return False
            self._set_timer_end(target, dur)
            self._reply_timer_change(target, dur, user_level)
            return True

        # action == "set"
        if not dur:
            self._reply("timer.need_duration", config.TIMER_NEED_DURATION, user_level)
            return False
        # Метка задана и активный таймер с такой меткой уже есть → перезаписать (как изменение).
        if label:
            existing = self._find_active(active, label)
            if existing:
                self._set_timer_end(existing, dur)
                self._reply_timer_change(existing, dur, user_level)
                return True
        end = self._iso(datetime.now() + timedelta(seconds=dur))
        lst.append(self._make_timer(dur, end, label))
        дл = say_duration(dur)
        if label:
            self._reply("timer.set_label", config.TIMER_SET_LABEL, user_level, длительность=дл, метка=label)
        else:
            self._reply("timer.set", config.TIMER_SET, user_level, длительность=дл)
        act_now = [t for t in lst if t.get("активен")]
        if len(act_now) >= 2 and any(not (t.get("метка") or "").strip() for t in act_now):
            self._reply("timer.suggest_label", config.TIMER_SUGGEST_LABEL, user_level)
        return True

    def _reply_timer_change(self, target, dur, user_level):
        дл = say_duration(dur)
        if (target.get("метка") or "").strip():
            self._reply("timer.move_label", config.TIMER_MOVE_LABEL,
                        user_level, длительность=дл, метка=target["метка"])
        else:
            self._reply("timer.move", config.TIMER_MOVE, user_level, длительность=дл)

    def _set_timer_end(self, target, dur):
        target["длительность_сек"] = int(dur)
        target["окончание"] = self._iso(datetime.now() + timedelta(seconds=dur))
        target["сработал"] = False
        target["активен"] = True

    def _timer_remaining(self, t) -> int:
        end = self._parse_iso(t.get("окончание"))
        return int((end - datetime.now()).total_seconds()) if end else 0

    @staticmethod
    def _make_timer(dur, end_iso, label):
        tid = "timer:" + (alarms._norm(label) if label else end_iso[-8:])
        return {"длительность_сек": int(dur), "окончание": end_iso, "метка": label,
                "активен": True, "сработал": False, "id": tid}

    # ------------------------------------------------------------------ #
    # СЕКУНДОМЕРЫ (счёт вверх)
    # ------------------------------------------------------------------ #
    def _handle_stopwatch(self, text, user_level):
        cmd = timers.parse_stopwatch_command(text)
        if not cmd:
            self._reply("stopwatch.none_found", config.SW_NONE_FOUND, user_level)
            return
        self.log.info("Секундомер: команда %s метка=%s (%s)",
                      cmd["действие"], cmd.get("метка"), text)
        sched = alarms.read_schedule()
        if self._cmd_stopwatch(cmd, sched, user_level):
            alarms.write_schedule(sched)

    def _cmd_stopwatch(self, cmd, sched, user_level) -> bool:
        lst = sched["секундомеры"]
        action = cmd["действие"]
        label = (cmd.get("метка") or "").strip() or None
        running = [s for s in lst if s.get("активен")]

        if action == "delete_all":
            if not lst:
                self._reply("stopwatch.none_found", config.SW_NONE_FOUND, user_level)
                return False
            sched["секундомеры"] = []
            self._reply("stopwatch.delete_all", config.SW_DELETE_ALL, user_level)
            return True

        if action == "start":
            lst.append(self._make_sw(label))
            if label:
                self._reply("stopwatch.start_label", config.SW_START_LABEL, user_level, метка=label)
            else:
                self._reply("stopwatch.start", config.SW_START, user_level)
            run_now = [s for s in lst if s.get("активен")]
            if len(run_now) >= 2 and any(not (s.get("метка") or "").strip() for s in run_now):
                self._reply("stopwatch.suggest_label", config.SW_SUGGEST_LABEL, user_level)
            return True

        if action == "query":
            target = self._find_one(lst, running, label)  # включая остановленные (queryable)
            if not target:
                self._reply("stopwatch.none_found", config.SW_NONE_FOUND, user_level)
                return False
            прош = say_duration(self._sw_elapsed(target))
            if (target.get("метка") or "").strip():
                self._reply("stopwatch.elapsed_label", config.SW_ELAPSED_LABEL,
                            user_level, прошло=прош, метка=target["метка"])
            else:
                self._reply("stopwatch.elapsed", config.SW_ELAPSED, user_level, прошло=прош)
            return False

        if action == "stop":
            target = self._find_active(running, label)
            if not target:
                self._reply("stopwatch.none_found", config.SW_NONE_FOUND, user_level)
                return False
            target["стоп"] = self._iso(datetime.now())
            target["активен"] = False
            прош = say_duration(self._sw_elapsed(target))
            if (target.get("метка") or "").strip():
                self._reply("stopwatch.stop_label", config.SW_STOP_LABEL,
                            user_level, прошло=прош, метка=target["метка"])
            else:
                self._reply("stopwatch.stop", config.SW_STOP, user_level, прошло=прош)
            return True

        # action == "reset" (сброс/удаление)
        target = self._find_one(lst, running, label)
        if not target:
            self._reply("stopwatch.none_found", config.SW_NONE_FOUND, user_level)
            return False
        lbl = (target.get("метка") or "").strip()
        lst.remove(target)
        if lbl:
            self._reply("stopwatch.reset_label", config.SW_RESET_LABEL, user_level, метка=lbl)
        else:
            self._reply("stopwatch.reset", config.SW_RESET, user_level)
        return True

    def _sw_elapsed(self, sw) -> int:
        start = self._parse_iso(sw.get("старт"))
        if start is None:
            return 0
        stop = self._parse_iso(sw.get("стоп")) if sw.get("стоп") else datetime.now()
        return int((stop - start).total_seconds())

    @staticmethod
    def _make_sw(label):
        now_iso = datetime.now().isoformat(timespec="seconds")
        sid = "sw:" + (alarms._norm(label) if label else now_iso[-8:])
        return {"старт": now_iso, "стоп": None, "метка": label, "активен": True, "id": sid}

    # ------------------------------------------------------------------ #
    # Поиск/время — общее для таймеров и секундомеров
    # ------------------------------------------------------------------ #
    @staticmethod
    def _find_active(items, label):
        """По метке (точно/нечётко) среди items; без метки — единственный."""
        if label:
            ln = alarms._norm(label)
            for it in items:
                im = alarms._norm(it.get("метка") or "")
                if im and (im == ln or SequenceMatcher(None, im, ln).ratio() >= 0.8):
                    return it
            return None
        return items[0] if len(items) == 1 else None

    def _find_one(self, all_items, active_items, label):
        """Найти среди ВСЕХ (вкл. остановленные): по метке; без метки — единственный всего,
        иначе единственный активный."""
        if label:
            return self._find_active(all_items, label)
        if len(all_items) == 1:
            return all_items[0]
        return active_items[0] if len(active_items) == 1 else None

    @staticmethod
    def _iso(dt):
        return dt.isoformat(timespec="seconds")

    @staticmethod
    def _parse_iso(s):
        try:
            return datetime.fromisoformat(str(s))
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # Тик-цикл и срабатывание
    # ------------------------------------------------------------------ #
    def _tick_loop(self):
        while not self._stop_event.wait(config.ALARM_TICK):
            try:
                self._tick()
            except Exception:
                self.log_exc(logging.ERROR, "Сбой тика планировщика — продолжаю")

    def _tick(self):
        now = datetime.now()
        fire_alarms, fire_timers = [], []
        with self._lock:
            sched = alarms.read_schedule()
            before_fired = dict(self._fired)
            changed = False
            for a in sched.get("будильники", []):
                if not a.get("активен"):
                    continue
                was_active = a.get("активен")
                if self._evaluate(a, now):
                    fire_alarms.append(dict(a))  # копия — озвучиваем ВНЕ лока (сеть/синтез)
                if a.get("активен") != was_active:
                    changed = True
            for t in sched.get("таймеры", []):
                if self._timer_due(t, now):  # помечает сработал/активен=false (персист ниже)
                    fire_timers.append(dict(t))
                    changed = True
            if changed:
                alarms.write_schedule(sched)
            if self._fired != before_fired:
                self._save_state()
        # Озвучка вне лока: не держим команды/файл на время сетевого запроса погоды.
        for a in fire_alarms:
            self._fire_alarm(a, now)
        for t in fire_timers:
            self._fire_timer(t)

    def _timer_due(self, t, now) -> bool:
        """Таймер истёк? Срабатывает и при пропуске во время простоя (now ≫ окончание) — один раз.
        Помечает сработал/активен=false (для персиста и анти-повтора)."""
        if not t.get("активен") or t.get("сработал"):
            return False
        end = self._parse_iso(t.get("окончание"))
        if end is not None and now >= end:
            t["сработал"] = True
            t["активен"] = False
            return True
        return False

    def _evaluate(self, alarm, now) -> bool:
        """Пора ли сработать? Обновляет _fired (анти-двойное) и активность (разовый → выкл).

        Окно срабатывания [время; время+grace] — пропущенное (сон ноутбука) дольше grace:
        ежедневный ждёт завтра, разовый — следующего наступления. Возвращает True для озвучки."""
        hm = self._alarm_hm(alarm)
        if hm is None:
            return False
        h, m = hm
        aid = alarm.get("id") or ""
        daily = alarm.get("повтор") == "ежедневный"
        fire_dt = datetime.combine(now.date(), dtime(h, m))
        grace = timedelta(minutes=float(config.ALARM_GRACE_MINUTES))
        today = now.date().isoformat()
        if daily:
            if self._fired.get(aid) == today:
                return False
            if fire_dt <= now <= fire_dt + grace:
                self._fired[aid] = today
                return True
            if now > fire_dt + grace:
                self._fired[aid] = today  # надолго пропустили — помечаем сегодня, ждём завтра
            return False
        # разовый
        if self._fired.get(aid):
            alarm["активен"] = False
            return False
        if fire_dt <= now <= fire_dt + grace:
            self._fired[aid] = now.isoformat(timespec="seconds")
            alarm["активен"] = False
            return True
        return False  # ещё не время или сегодня пропущено → сработает в следующее наступление

    @staticmethod
    def _alarm_hm(alarm):
        try:
            h, m = str(alarm.get("время") or "").split(":")
            return int(h), int(m)
        except Exception:
            return None

    def _fire_alarm(self, alarm, now):
        try:
            if alarm.get("тип") == "утренний":
                self._fire_morning(now)
            else:
                self._fire_regular(alarm, now)
            self.log.info("Будильник сработал: %s %s (метка %s)",
                          alarm.get("тип"), alarm.get("время"), alarm.get("метка"))
        except Exception:
            self.log_exc(logging.ERROR, "Сбой озвучки сработавшего будильника")

    def _get_weather(self):
        """Погода на сегодня с кэшем (1 запрос/день). Без сети/при сбое → None (фраза без погоды)."""
        today = datetime.now().date().isoformat()
        if self._weather_cache and self._weather_cache[0] == today:
            return self._weather_cache[1]
        w = weather.morning_weather()
        self._weather_cache = (today, w)
        return w

    def _fire_morning(self, now):
        время = say_clock(now.hour, now.minute)
        w = self._get_weather() if config.ALARM_WEATHER_ENABLED else None
        if w:
            rich = _fmt(phrases.pick("alarm.morning_fire_weather", config.ALARM_MORNING_FIRE_WEATHER),
                        время=время, погода=w.get("характер"),
                        темп_макс=say_temperature(w.get("темп_макс")),
                        темп_мин=say_temperature(w.get("темп_мин")))
        else:
            rich = _fmt(phrases.pick("alarm.morning_fire_plain", config.ALARM_MORNING_FIRE_PLAIN),
                        время=время)
        wake = float(config.ALARM_WAKE_VOLUME)
        # Нарастание: короткие реплики тихо→громко, затем богатая фраза на целевой громкости.
        if config.ALARM_WAKE_RISING:
            steps = max(1, int(config.ALARM_WAKE_RISING_STEPS))
            start = max(0.0, min(wake, float(config.ALARM_WAKE_RISING_START)))
            for i in range(steps):
                vol = start + (wake - start) * (i / steps)
                self._say(phrases.pick("alarm.wake_prelude", config.ALARM_WAKE_PRELUDE),
                          min_volume=round(vol, 3))
        self._say(rich, min_volume=wake)

    def _fire_regular(self, alarm, now):
        время = say_clock(now.hour, now.minute)
        label = (alarm.get("метка") or "").strip()
        wake = float(config.ALARM_WAKE_VOLUME)
        if label:
            text = _fmt(phrases.pick("alarm.regular_fire_label", config.ALARM_REGULAR_FIRE_LABEL),
                        время=время, метка=label)
        else:
            text = _fmt(phrases.pick("alarm.regular_fire", config.ALARM_REGULAR_FIRE), время=время)
        self._say(text, min_volume=wake)

    def _fire_timer(self, t):
        """Срабатывание таймера: чайм (опц.) + фраза на заметной громкости (обходит «тихо→тихо»)."""
        try:
            label = (t.get("метка") or "").strip()
            if label:
                text = _fmt(phrases.pick("timer.fire_label", config.TIMER_FIRE_LABEL), метка=label)
            else:
                text = _fmt(phrases.pick("timer.fire", config.TIMER_FIRE))
            self._say(text, min_volume=float(config.TIMER_VOLUME), chime=bool(config.TIMER_CHIME))
            self.log.info("Таймер сработал: %sс (метка %s)", t.get("длительность_сек"), label or "—")
        except Exception:
            self.log_exc(logging.ERROR, "Сбой озвучки сработавшего таймера")


def main():
    SchedulerModule().run()


if __name__ == "__main__":
    main()
