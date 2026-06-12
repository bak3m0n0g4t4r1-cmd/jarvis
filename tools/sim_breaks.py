#!/usr/bin/env python3
"""Оффлайн-симуляция автомата детектора перерывов — доказывает ТОЧНОСТЬ таймингов и
поведения БЕЗ железа (Этап 23). Гоняет `ActivityMonitorModule._tick` по синтетическим
трассам ввода с управляемыми часами; ввод/яркость/события/речь застаблены.

Запуск: `python tools/sim_breaks.py` (код возврата 0 — все сценарии прошли).

Что проверяем:
  • случайные длительности — ТОЛЬКО целые минуты (секунды 00) и в нужных диапазонах;
  • «не за ноутом» (нет ввода) → idle растёт, уход в простой, НИ ОДНОГО напоминания
    (поведенческая гарантия корневого фикса; сам reader-loop проверяется вживую);
  • активная работа → предложение по истечении цикла, в фразе названы минуты паузы;
  • микропауза ≤80с не сбрасывает цикл, простой ≥180с — сбрасывает;
  • игнор → затемнение + повтор; стоп-фраза → ответ + возврат яркости + сброс;
  • похвала ТОЛЬКО за осмысленный перерыв (была работа/предложение), НЕ за «фильм».
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis import config  # noqa: E402
from jarvis.services import activity_monitor as am  # noqa: E402


class Clock:
    """Управляемые монотонные часы: подменяют am.time → time.monotonic()."""

    def __init__(self, t=10_000.0):
        self.t = t

    def monotonic(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make_mon(clock, *, low=True):
    """Свежий монитор со застабленным вводом/выводом и ДЕТЕРМИНИРОВАННЫМИ таргетами.

    low=True → берём нижний край диапазонов (цикл 23 мин, перерыв 4 мин, повтор 5 мин)."""
    am.time = clock
    mon = am.ActivityMonitorModule()
    mon.says, mon.events, mon.bright = [], [], []
    mon.say = lambda text: mon.says.append(text)            # _say запишет + обновит _last_input
    mon.publish_event = lambda event, detail=None: mon.events.append(event)
    mon._request_brightness = lambda desired: mon.bright.append(desired)
    edge = (lambda a, b: float(min(a, b) * 60)) if low else (lambda a, b: float(max(a, b) * 60))
    mon._rand_minutes = edge
    mon._new_cycle()                                        # применить детерминированные таргеты
    mon._last_input = clock.t
    mon._last_tick = clock.t
    mon._idle_episode_active = False
    return mon


def run(mon, clock, seconds, active, dt=5.0):
    """Прогнать `seconds` тиков по dt. active — есть ли реальный ввод в тике (моделирует reader)."""
    for _ in range(int(seconds / dt)):
        clock.advance(dt)
        if active:
            mon._last_input = clock.t
        mon._tick()


# --------------------------------------------------------------------------- #
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"  [{'OK' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def t_rand_minutes():
    clock = Clock()
    mon = am.ActivityMonitorModule()  # реальный _rand_minutes
    am.time = clock
    bad = []
    for (a, b) in [(config.BREAK_CYCLE_MIN_MINUTES, config.BREAK_CYCLE_MAX_MINUTES),
                   (config.BREAK_BREAK_MIN_MINUTES, config.BREAK_BREAK_MAX_MINUTES),
                   (config.BREAK_REMIND_MIN_MINUTES, config.BREAK_REMIND_MAX_MINUTES)]:
        for _ in range(500):
            v = mon._rand_minutes(a, b)
            if v % 60 != 0 or not (min(a, b) * 60 <= v <= max(a, b) * 60):
                bad.append(v)
    check("случайные длительности — целые минуты (секунды 00), в диапазоне", not bad,
          f"нарушений: {len(bad)}" if bad else "1500 проб чисты")


def t_away_no_reminders():
    """Не за ноутом (нет ввода) → idle растёт, уход в простой, НИ ОДНОГО напоминания."""
    clock = Clock()
    mon = make_mon(clock)
    run(mon, clock, 40 * 60, active=False)     # 40 минут без касаний
    no_speech = not mon.says
    on_break = mon._idle_episode_active
    no_offer_events = mon.events.count("break_offer") == 0
    check("«не за ноутом» → нет напоминаний (корневой фикс, поведение)",
          no_speech and on_break and no_offer_events,
          f"реплик={len(mon.says)}, простой={on_break}, offer-событий={mon.events.count('break_offer')}")


def t_active_offer_with_minutes():
    """Активная работа → предложение по истечении цикла, в фразе названы минуты паузы."""
    clock = Clock()
    mon = make_mon(clock)                       # цикл 23 мин, перерыв 4 мин
    run(mon, clock, 24 * 60, active=True)       # 24 минуты непрерывной работы
    offered = mon._state in (am._OFFERED, am._DIMMED) or mon.events.count("break_offer") >= 1
    said = " ".join(mon.says)
    names_minutes = ("четыре минуты" in said)   # break=4 мин → say_duration(240)
    check("активная работа → предложение перерыва", offered and bool(mon.says),
          f"состояние={mon._state}, реплик={len(mon.says)}")
    check("в предложении названы минуты паузы (4 → «четыре минуты»)", names_minutes,
          (mon.says[0][:60] + "…") if mon.says else "нет реплик")


def t_micropause_vs_reset():
    """Микропауза ≤80с не сбрасывает цикл; простой ≥180с — сбрасывает."""
    clock = Clock()
    mon = make_mon(clock)
    run(mon, clock, 10 * 60, active=True)       # 10 мин работы
    acc_before = mon._accumulated
    run(mon, clock, 70, active=False)           # микропауза 70с (<80)
    run(mon, clock, 10, active=True)            # снова ввод
    micro_ok = mon._accumulated >= acc_before and not mon._idle_episode_active
    check("микропауза ≤80с НЕ сбрасывает цикл (активность копится)", micro_ok,
          f"было {acc_before:.0f}с → стало {mon._accumulated:.0f}с, простой={mon._idle_episode_active}")
    run(mon, clock, 200, active=False)          # простой 200с (≥180)
    reset_ok = mon._idle_episode_active and mon._accumulated == 0.0
    check("простой ≥180с → цикл ПОЛНОСТЬЮ сброшен (смена деятельности)", reset_ok,
          f"простой={mon._idle_episode_active}, накоплено={mon._accumulated:.0f}с")


def t_ignore_dim_repeat():
    """Игнор предложения (продолжаю работать) → затемнение + повтор; стоп-фраза → возврат."""
    clock = Clock()
    mon = make_mon(clock)                       # цикл 23, повтор 5 мин
    run(mon, clock, 24 * 60, active=True)       # довести до предложения
    if mon._state != am._OFFERED:
        check("игнор → затемнение", False, f"не дошли до OFFERED (state={mon._state})")
        return
    run(mon, clock, 6 * 60, active=True)        # игнор: ещё ~6 мин работы (> remind 5 мин)
    dimmed = am._BRIGHT_DIMMED in mon.bright and mon._state == am._DIMMED
    check("игнор ≥5 мин → экран затемнён + повтор напоминания", dimmed,
          f"яркость-запросы={mon.bright}, state={mon._state}")
    # стоп-фраза в контексте напоминания
    mon.bright.clear(); mon.says.clear()
    mon._handle_stop_phrase()
    stopped = (am._BRIGHT_NORMAL in mon.bright and mon._state == am._ACCUMULATING
               and mon._accumulated == 0.0 and bool(mon.says))
    check("стоп-фраза → ответ + возврат яркости + сброс цикла", stopped,
          f"яркость={mon.bright}, state={mon._state}, реплик={len(mon.says)}")


def t_praise_only_after_work():
    """Похвала ТОЛЬКО за осмысленный перерыв: была работа/предложение — да; «фильм» — нет."""
    # (а) Работал 11 мин (>10) → перерыв 5 мин → ПОХВАЛА.
    clock = Clock()
    mon = make_mon(clock)
    run(mon, clock, 11 * 60, active=True)       # 11 мин работы (>порог 10)
    mon.says.clear(); mon.events.clear()
    run(mon, clock, 5 * 60, active=False)       # перерыв 5 мин (>break 4)
    run(mon, clock, 10, active=True)            # вернулся
    praised = "break_praise" in mon.events
    check("работал >10 мин → перерыв → ПОХВАЛА", praised,
          f"события={mon.events}")

    # (б) «Фильм»: НЕТ работы (нет ввода) → 6 мин простоя → возврат → НЕ хвалить.
    clock = Clock()
    mon = make_mon(clock)
    run(mon, clock, 6 * 60, active=False)       # «фильм»: работа не копится
    mon.events.clear(); mon.says.clear()
    run(mon, clock, 10, active=True)            # «коснулся мыши» — вернулся
    not_praised = "break_praise" not in mon.events
    check("«фильм» (нет работы) → перерыв → НЕ хвалить (ключевой фикс)", not_praised,
          f"события={mon.events}")

    # (в) Работал 5 мин (<10), предложения не было → перерыв → НЕ хвалить.
    clock = Clock()
    mon = make_mon(clock)
    run(mon, clock, 5 * 60, active=True)        # 5 мин (<порог), до предложения далеко
    mon.events.clear()
    run(mon, clock, 5 * 60, active=False)
    run(mon, clock, 10, active=True)
    not_praised2 = "break_praise" not in mon.events
    check("работал <10 мин, без предложения → НЕ хвалить", not_praised2,
          f"события={mon.events}")


def main():
    print("Симуляция детектора перерывов (оффлайн, без железа):")
    t_rand_minutes()
    t_away_no_reminders()
    t_active_offer_with_minutes()
    t_micropause_vs_reset()
    t_ignore_dim_repeat()
    t_praise_only_after_work()
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\nИтог: {len(RESULTS) - len(failed)}/{len(RESULTS)} проверок прошли.")
    if failed:
        print("ПРОВАЛЫ:", ", ".join(failed))
        return 1
    print("Все сценарии прошли — тайминги и поведение детектора верны.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
