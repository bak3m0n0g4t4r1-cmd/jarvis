"""Пред-рендер фраз в WAV-кэш: голос Silero (eugene) + JARVIS-DSP, офлайн и заранее.

На N100 синтез Silero идёт со скоростью ~реального времени, поэтому всё, что можно, рендерим
ЗАРАНЕЕ: в рантайме играется готовый WAV из кэша мгновенно и без torch. Здесь — перечислитель
фраз (статика из settings.yaml/commands.yaml + конечная динамика через speech.py) и сборка
кэша (`jarvis tts build`) / отчёт покрытия (`jarvis tts stats`).

ВАЖНО: перечислитель даёт РОВНО те итоговые строки, что доходят до TTS в рантайме (подстановка
плейсхолдеров — тем же `.replace("{slot}", str(value))`, что в сервисах), иначе ключи кэша не
совпадут. Чего перечислитель не покрывает (свободный текст: надиктовка, города, погода) —
доберётся лениво на промахе и тоже осядет в кэш.
"""
from __future__ import annotations

import re
import time

import yaml

from jarvis import config, speech, tts_cache, tts_dsp, tts_engine

_CYR = re.compile(r"[а-яёА-ЯЁ]")


def _is_phrase(s) -> bool:
    """Похоже на произносимую фразу: есть кириллица, нет неподставленных плейсхолдеров."""
    return isinstance(s, str) and bool(_CYR.search(s)) and "{" not in s and len(s.strip()) >= 2


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


# Хардкод-строки сервисов без плейсхолдеров (остальные — с «{tag}» и т.п. — доберутся лениво).
# Должны ДОСЛОВНО (с теми же `+`-ударениями) совпадать с тем, что шлют сервисы, иначе ключ кэша
# не сойдётся (см. matcher.NOT_RECOGNIZED, tts.FILLER_TEXT, core/os_agent).
HARDCODED = [
    "Сек+унду, сэр.",                              # филлер на время холодного синтеза (tts.FILLER_TEXT)
    "Бо+юсь, не разобр+ал, сэр. Повтор+ите?",      # matcher.NOT_RECOGNIZED
    "Сэр, перезапуст+иться не удал+ось.",          # core
    "Сэр, в этой сред+е н+ечего открыв+ать.",      # os_agent
]


def iter_static_phrases() -> list[str]:
    """Все произносимые литералы без плейсхолдеров: settings.yaml + commands.yaml + хардкод."""
    out: set[str] = set()
    # settings.yaml — все паки фраз (кириллица-строки без `{`).
    for s in _walk_strings(config._SETTINGS):
        if _is_phrase(s):
            out.add(s.strip())
    # commands.yaml — только произносимые поля (подтверждение/ответ_*), НЕ описания/команды.
    try:
        with open(config.COMMANDS_FILE, encoding="utf-8") as f:
            cmds = yaml.safe_load(f) or {}
        for spec in (cmds.get("команды") or cmds).values() if isinstance(cmds, dict) else []:
            if not isinstance(spec, dict):
                continue
            for key, val in spec.items():
                if "подтвержд" in key or key.startswith("ответ"):
                    for s in _walk_strings(val):
                        if _is_phrase(s):
                            out.add(s.strip())
    except Exception:
        pass
    for s in HARDCODED:
        if _is_phrase(s):
            out.add(s)
    return sorted(out)


# --- Генераторы значений динамики (РОВНО как в сервисах) ---
def _percent_digits() -> list[str]:
    return [str(n) for n in range(0, 101)]          # core/lamp: .replace("{процент}", str(pct))


def _clock_values() -> list[str]:
    # scheduler/worldtime: время = say_clock(h, m). Минуты с шагом 5 — ограничить взрыв.
    return [speech.say_clock(h, m) for h in range(24) for m in range(0, 60, 5)]


def _duration_values() -> list[str]:
    # scheduler: длительность/прошло = say_duration(сек). Реально произносимые: минуты, часы, ключевые.
    vals: set[str] = set()
    for sec in (5, 10, 15, 20, 30, 45, 90):
        vals.add(speech.say_duration(sec))
    for m in range(1, 121):
        vals.add(speech.say_duration(m * 60))
    for h in range(1, 7):
        vals.add(speech.say_duration(h * 3600))
        vals.add(speech.say_duration(h * 3600 + 1800))
    return sorted(vals)


# Паки с ОДНИМ плейсхолдером, которые можно точно перечислить. Многослотовые (погода, напоминания)
# опускаем — доберутся лениво. Имя config-атрибута → (slot, генератор значений).
DYNAMIC_SPEC = [
    ("VOLUME_ACK", "процент", _percent_digits),
    ("LAMP_BRIGHT_SET_ACK", "процент", _percent_digits),
    ("ALARM_MORNING_SET", "время", _clock_values),
    ("ALARM_MORNING_MOVE", "время", _clock_values),
    ("ALARM_MORNING_ALREADY", "время", _clock_values),
    ("ALARM_REGULAR_SET", "время", _clock_values),
    ("ALARM_REGULAR_MOVE", "время", _clock_values),
    ("ALARM_REGULAR_ALREADY", "время", _clock_values),
    ("ALARM_REGULAR_FIRE", "время", _clock_values),
    ("TIMER_SET", "длительность", _duration_values),
    ("TIMER_MOVE", "длительность", _duration_values),
    ("SW_ELAPSED", "прошло", _duration_values),
    ("SW_STOP", "прошло", _duration_values),
]


def iter_dynamic_phrases() -> list[str]:
    """Конечная динамика: подстановка перечислимых значений в одно-слотовые паки."""
    out: set[str] = set()
    for attr, slot, gen in DYNAMIC_SPEC:
        pack = getattr(config, attr, None)
        if not pack:
            continue
        token = "{" + slot + "}"
        values = gen()
        for phrase in pack:
            if token not in phrase:
                continue
            for v in values:
                s = phrase.replace(token, str(v))
                if "{" in s:          # остались другие плейсхолдеры — этот вариант не литерал
                    continue
                out.add(s.strip())
    return sorted(out)


def iter_all_phrases(only: str | None = None) -> list[str]:
    out: list[str] = []
    if only in (None, "static"):
        out += iter_static_phrases()
    if only in (None, "dynamic"):
        out += iter_dynamic_phrases()
    # дедуп с сохранением порядка
    seen: set[str] = set()
    uniq = []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def _final_text(phrase: str) -> str:
    """То же преобразование, что в TTS перед ключом кэша: произношение + `+`-ударения."""
    return speech.apply_stress(
        speech.apply_pronunciation(phrase, config.PRONUNCIATION), config.STRESS_TABLE)


def _make_cache(engine) -> tts_cache.TtsCache:
    dsp_sig = tts_dsp.dsp_signature(config.DSP_PARAMS, engine.voice_id)
    return tts_cache.TtsCache(config.TTS_CACHE_DIR, engine.voice_id, dsp_sig)


def build(only: str | None = None, force: bool = False, limit: int | None = None,
          log=print) -> dict:
    """Синтезировать и закэшировать все перечислимые фразы (инкрементально: существующие — пропуск).

    only: 'static'|'dynamic'|None(оба). force: пересоздать даже существующие. limit: потолок числа
    синтезов за прогон (для частичной сборки/проверки)."""
    engine = tts_engine.SileroEngine()
    cache = _make_cache(engine)
    phrases = iter_all_phrases(only)
    finals = [(_final_text(p), p) for p in phrases]
    todo = [(ft, p) for ft, p in finals if force or not cache.has(ft)]
    if limit:
        todo = todo[:limit]
    total = len(todo)
    log(f"Голос: {engine.voice_id}")
    log(f"Кэш:   {cache.dir}")
    log(f"Фраз перечислено: {len(finals)} | к синтезу: {total}"
        + (" (force)" if force else " (новых)") + (f" | лимит {limit}" if limit else ""))
    if total == 0:
        log("Нечего синтезировать — кэш уже полон для текущего тембра.")
        return {"rendered": 0, "skipped": len(finals), "errors": 0}
    engine.warmup()
    rendered = errors = 0
    t0 = time.time()
    for i, (ft, src) in enumerate(todo, 1):
        try:
            pcm = engine.synth(ft)
            if not pcm:
                errors += 1
                continue
            try:
                pcm = tts_dsp.apply_dsp(pcm, engine.sample_rate, config.DSP_PARAMS, engine.sample_rate)
            except Exception as exc:
                log(f"  ⚠ DSP не применился к {src[:40]!r}: {exc} — кладу «сухой»")
            cache.put(ft, pcm, engine.sample_rate)
            rendered += 1
        except Exception as exc:
            errors += 1
            log(f"  ⚠ сбой синтеза {src[:50]!r}: {type(exc).__name__}: {exc}")
        if i % 25 == 0 or i == total:
            el = time.time() - t0
            rate = i / el if el > 0 else 0
            eta = (total - i) / rate if rate > 0 else 0
            log(f"  [{i}/{total}] {rate:.1f} фраз/с | прошло {el/60:.1f}м | ост. ~{eta/60:.1f}м")
    engine.unload()
    log(f"Готово: отрендерено {rendered}, ошибок {errors}, за {(time.time()-t0)/60:.1f} мин.")
    return {"rendered": rendered, "skipped": len(finals) - total, "errors": errors}


def stats(prune: bool = False, log=print) -> dict:
    """Покрытие кэша: сколько перечислимых фраз уже готово, объём на диске, чистка чужих сигнатур."""
    engine = tts_engine.SileroEngine()
    cache = _make_cache(engine)
    statics = iter_static_phrases()
    dynamics = iter_dynamic_phrases()
    have_s = sum(1 for p in statics if cache.has(_final_text(p)))
    have_d = sum(1 for p in dynamics if cache.has(_final_text(p)))
    cs = cache.stats()
    log(f"Голос: {engine.voice_id} | DSP-сигнатура: {cache.dsp_sig}")
    log(f"Кэш:   {cache.dir}")
    log(f"Файлов в кэше: {cs['count']} | объём: {cs['bytes']/1e6:.1f} МБ")
    log(f"Статика:  {have_s}/{len(statics)} готово")
    log(f"Динамика: {have_d}/{len(dynamics)} готово")
    miss = (len(statics) - have_s) + (len(dynamics) - have_d)
    if miss:
        log(f"Не хватает {miss} — добери: jarvis tts build")
    if prune:
        removed = cache.prune_other_signatures()
        log(f"Подчищено осиротевших клипов (другие голос/DSP): {removed}")
    return {"cached": cs["count"], "bytes": cs["bytes"],
            "static": [have_s, len(statics)], "dynamic": [have_d, len(dynamics)]}
