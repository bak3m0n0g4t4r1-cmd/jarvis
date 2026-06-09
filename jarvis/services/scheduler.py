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

from jarvis import alarms, config, contracts, phrases, weather
from jarvis.bus import JarvisModule
from jarvis.speech import say_clock, say_temperature

_PLACEHOLDERS = ("{время}", "{метка}", "{погода}", "{темп_макс}", "{темп_мин}")


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
    def _say(self, text: str, user_level=None, min_volume=None):
        """Реплика в jarvis/say с опц. адаптивной громкостью (user_level) или жёстким нижним
        пределом (min_volume — для будильника, обходит «тихо→тихо»)."""
        if not text:
            return
        payload = {"text": text, "source": self.name}
        if user_level is not None:
            payload["user_level"] = user_level
        if min_volume is not None:
            payload["min_volume"] = min_volume
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
            if not text or not alarms.is_alarm_command(text):
                return
            level = payload.get("user_level")
            user_level = level if isinstance(level, (int, float)) else None
            with self._lock:
                self._handle_command(text, user_level)
        except Exception:
            self.log_exc(logging.ERROR, "Сбой обработки команды будильника — продолжаю")

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
        to_fire = []
        with self._lock:
            sched = alarms.read_schedule()
            before_fired = dict(self._fired)
            changed = False
            for a in sched.get("будильники", []):
                if not a.get("активен"):
                    continue
                was_active = a.get("активен")
                if self._evaluate(a, now):
                    to_fire.append(dict(a))  # копия — озвучиваем ВНЕ лока (сеть/синтез)
                if a.get("активен") != was_active:
                    changed = True
            if changed:
                alarms.write_schedule(sched)
            if self._fired != before_fired:
                self._save_state()
        # Озвучка вне лока: не держим команды/файл на время сетевого запроса погоды.
        for a in to_fire:
            self._fire_alarm(a, now)

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


def main():
    SchedulerModule().run()


if __name__ == "__main__":
    main()
