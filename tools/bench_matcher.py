#!/usr/bin/env python3
"""Бенч матчера: точность, латентность, антонимы, контекст ветки, калибровка порогов.

Объективная оценка распознавания команд (Слой 1 правила + Слой 2 классификатор/косинус).
Запуск:
    python tools/bench_matcher.py                         # активная модель (обычно int8)
    JARVIS_EMBEDDER_PREFER_INT8=0 python tools/bench_matcher.py   # сравнить на fp32
    JARVIS_MATCHER_CLF_ENABLED=0 python tools/bench_matcher.py    # косинус-fallback

Метрики:
  • Правила: каждый синоним обязан вернуть свой тег (регрессия нормализации/пересечений).
  • Слой 2 на примерах (форс, in-sample): точность + распределение proba — для порогов.
  • Парафразы вне синонимов: реальная генерализация Слоя 2.
  • Антонимы: вкл/выкл, громче/тише → строго разные теги.
  • OOS/шум: посторонние фразы → переспрос (None).
  • Латентность: норм / Слой1 / vectorize / predict (мс).
"""
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# Парафразы ВНЕ синонимов (живые формулировки, которых нет в commands.yaml) → ожидаемый тег.
# Подобраны под заведомо существующие команды; отсутствующие теги тихо пропускаются.
PARAPHRASES = {
    "volume_up": ["сделай-ка звук побольше", "что-то очень тихо совсем"],
    "volume_down": ["слишком орёт, поубавь", "звук режет уши"],
    "brightness_up": ["экран тусклый, добавь яркости"],
    "brightness_down": ["глаза болят от яркого экрана"],
    "lamp_on": ["стало темно, дай света в комнате"],
    "lamp_off": ["погаси освещение в комнате"],
    "wifi_on": ["подними беспроводную сеть"],
    "wifi_off": ["отруби вайфай совсем"],
    "browser": ["хочу полазить в интернете"],
}

# Антонимы: пары, которые ОБЯЗАНЫ распознаваться в разные теги (работа слоя правил).
ANTONYMS = [
    ("включи свет", "выключи свет"),
    ("громче", "тише"),
    ("ярче", "темнее"),
    ("включи вайфай", "выключи вайфай"),
]

# Посторонние фразы — Джарвис должен переспросить (None), а не выполнить случайное.
OUT_OF_SCOPE = [
    "расскажи анекдот про слона",
    "сколько будет два плюс два",
    "как дела у тебя сегодня",
    "напиши стихотворение о море",
]


def _raw_proba(m, phrase):
    """Сырые top1/top2 классификатора (без порога) — для калибровки. None, если клф выкл."""
    import numpy as np

    from jarvis import matcher as M
    if not getattr(m, "_clf_ok", False):
        return None
    qv = m._embedder.encode([M.normalize(phrase)])
    if qv is None:
        return None
    logits = m._clf_w @ np.asarray(qv[0], dtype=np.float32) + m._clf_b
    ex = np.exp(logits - float(logits.max()))
    p = ex / float(ex.sum())
    order = np.argsort(p)[::-1]
    return (str(m._clf_classes[order[0]]), float(p[order[0]]),
            str(m._clf_classes[order[1]]), float(p[order[1]]))


def main() -> int:
    import yaml

    from jarvis import config
    from jarvis import matcher as M

    print("=" * 72)
    print("Модель эмбеддера:", os.path.basename(config.EMBEDDER_MODEL),
          f"| clf_enabled={config.MATCHER_CLF_ENABLED}"
          f" | пороги clf={config.MATCHER_CLF_THRESHOLD}/{config.MATCHER_CLF_MARGIN}")
    cmds = yaml.safe_load(open(config.COMMANDS_FILE, encoding="utf-8"))
    t0 = time.perf_counter()
    m = M.Matcher(cmds)
    m.match("прогрев слоя 2 чтобы обучить классификатор")  # форс ленивой подготовки Слоя 2
    print(f"Старт матчера (вкл. подготовку Слоя 2): {(time.perf_counter()-t0)*1000:.0f}мс"
          f" | классификатор: {'ON' if getattr(m,'_clf_ok',False) else 'OFF (косинус)'}")
    real = {t: s for t, s in cmds.items()
            if t not in M._RESERVED_KEYS and isinstance(s, dict)}

    # 1) Правила: каждый синоним → свой тег. Различаем НАСТОЯЩИЕ ошибки и КОЛЛИЗИИ общих
    #    синонимов (одна фраза у нескольких команд — разводится контекстом ветки, не баг).
    syn_owners: dict[str, list] = {}
    for tag, s in real.items():
        for syn in (s.get("синонимы") or []):
            syn_owners.setdefault(M.normalize(syn), []).append(tag)
    bad, collisions, nsyn = [], [], 0
    for tag, s in real.items():
        for syn in (s.get("синонимы") or []):
            nsyn += 1
            g = m.match(syn)
            if g is not None and g.tag == tag:
                continue
            got = g.tag if g else None
            # Коллизия: эта фраза — синоним и у полученной команды (общий словарь, разводится веткой).
            if got is not None and len(syn_owners.get(M.normalize(syn), [])) > 1 and got in syn_owners[M.normalize(syn)]:
                collisions.append((syn, tag, got))
            else:
                bad.append((syn, tag, got))
    print(f"\n[1] Правила: синонимов {nsyn} | настоящих ошибок {len(bad)} | "
          f"коллизий общих синонимов {len(collisions)} (норм — разводятся веткой)")
    for syn, exp, got in bad[:15]:
        print(f"    ✗ ОШИБКА '{syn}': ждали {exp}, получили {got}")
    for syn, exp, got in collisions[:8]:
        print(f"    ~ коллизия '{syn}': {exp}↔{got} (голое→{got}, в ветке {exp} разводится)")

    # 2) Слой 2 на примерах (форс минуя правила) — in-sample точность + proba.
    correct, total, probs, wrong = 0, 0, [], []
    for tag, s in real.items():
        for ex in (s.get("примеры") or []):
            total += 1
            r = m._match_semantic(M.normalize(ex))
            if r and r.tag == tag:
                correct += 1
                probs.append(r.score)
            else:
                wrong.append((ex, tag, r.tag if r else None, r.score if r else None))
    print(f"\n[2] Слой 2 на примерах (форс, in-sample): {correct}/{total} = "
          f"{correct/max(total,1)*100:.1f}%")
    if probs:
        print(f"    proba распознанных: медиана {statistics.median(probs):.3f}, "
              f"мин {min(probs):.3f}, p10 {statistics.quantiles(probs, n=10)[0]:.3f}")
    for ex, exp, got, sc in wrong[:15]:
        print(f"    ✗ '{ex}': ждали {exp}, получили {got} ({sc})")

    # 3) Парафразы вне синонимов — реальная генерализация Слоя 2.
    pcorrect, ptotal, praw = 0, 0, []
    miss = []
    for tag, phrases in PARAPHRASES.items():
        if tag not in real:
            continue
        for ph in phrases:
            ptotal += 1
            r = m.match(ph)
            ok = r is not None and r.tag == tag
            pcorrect += int(ok)
            raw = _raw_proba(m, ph)
            if raw:
                praw.append((ph, tag, ok, raw))
            if not ok:
                miss.append((ph, tag, r.tag if r else None, r.layer if r else None,
                             r.score if r else None))
    print(f"\n[3] Парафразы вне синонимов: {pcorrect}/{ptotal} = "
          f"{pcorrect/max(ptotal,1)*100:.1f}%")
    for ph, exp, got, layer, sc in miss:
        print(f"    ✗ '{ph}': ждали {exp}, получили {got} ({layer},{sc})")
    if praw:
        print("    сырые top1/top2 классификатора (для порогов):")
        for ph, exp, ok, (t1, p1, t2, p2) in praw:
            mark = "✓" if ok else "✗"
            print(f"      {mark} '{ph[:34]:34}' {t1}={p1:.3f} / {t2}={p2:.3f} (отрыв {p1-p2:.3f})")

    # 4) Антонимы → разные теги.
    print("\n[4] Антонимы (обязаны различаться):")
    anti_bad = 0
    for a, b in ANTONYMS:
        ra, rb = m.match(a), m.match(b)
        ta = ra.tag if ra else None
        tb = rb.tag if rb else None
        ok = ta is not None and tb is not None and ta != tb
        anti_bad += int(not ok)
        print(f"    {'✓' if ok else '✗'} '{a}'→{ta}  |  '{b}'→{tb}")

    # 5) Контекст ветки: мягкий приоритет не ломает обычное распознавание.
    print("\n[5] Контекст ветки (мягкий приоритет):")
    branch = (cmds.get("ветки") or {}).get("музыка")
    bt = set(branch) if branch else None
    for ph in ["хочу музыку погромче", "поставь на паузу"]:
        r0 = m.match(ph)
        r1 = m.match(ph, branch_tags=bt)
        print(f"    '{ph}': без ветки→{r0.tag if r0 else None} | в музыке→{r1.tag if r1 else None}")

    # 6) OOS / шум → переспрос (None).
    oos_bad = 0
    print("\n[6] Посторонние фразы (ожидаем переспрос/None):")
    for ph in OUT_OF_SCOPE:
        r = m.match(ph)
        ok = r is None
        oos_bad += int(not ok)
        raw = _raw_proba(m, ph)
        extra = ""
        if raw:
            t1, p1, t2, p2 = raw
            extra = f"  [сырое: {t1}={p1:.3f}/{t2}={p2:.3f}]"
        print(f"    {'✓' if ok else '✗'} '{ph}' → {r.tag if r else 'переспрос'}{extra}")

    # 7) Латентность (PERF) — горячий путь и Слой 2.
    sample_rules = ["сделай громче", "выключи вайфай", "открой браузер"]
    sample_sem = ["сделай звук побольше", "стало темно дай света"]
    n = 50
    t = time.perf_counter()
    for _ in range(n):
        for ph in sample_rules:
            m.match(ph)
    rules_ms = (time.perf_counter() - t) / (n * len(sample_rules)) * 1000
    t = time.perf_counter()
    for _ in range(n):
        for ph in sample_sem:
            m._match_semantic(M.normalize(ph))
    sem_ms = (time.perf_counter() - t) / (n * len(sample_sem)) * 1000
    print(f"\n[7] Латентность: Слой1(правила) ~{rules_ms:.2f}мс/фраза | "
          f"Слой2(vectorize+predict) ~{sem_ms:.2f}мс/фраза")

    print("\n" + "=" * 72)
    print(f"ИТОГ: правила {len(bad)} ошибок | примеры {correct}/{total} | "
          f"парафразы {pcorrect}/{ptotal} | антонимы {len(ANTONYMS)-anti_bad}/{len(ANTONYMS)} | "
          f"OOS {len(OUT_OF_SCOPE)-oos_bad}/{len(OUT_OF_SCOPE)}")
    return 0 if (not bad and not anti_bad) else 1


if __name__ == "__main__":
    raise SystemExit(main())
