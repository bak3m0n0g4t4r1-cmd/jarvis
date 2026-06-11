"""Бенчмарк ASR-моделей для Джарвиса (Этап 21в): точность «с первого раза» vs задержка vs RAM.

Методика (честная, без записей голоса пользователя — синтетика с анти-смещением):
  корпус   = по 1 примеру на команду из commands.yaml (+ «джарвис» префикс) + фразы
             планировщика (будильники/таймеры/напоминания) + фразы-ловушки (не команды);
  аудио    = синтез Piper ТРЕМЯ голосами (dmitri/irina/ruslan) с вариацией темпа,
             ресемпл 22050→16000; шум — белый и «бормотание» (микс речи, как ТВ-фон)
             на SNR 10/5 дБ;
  метрики  = tag-accuracy (декод → wake-снятие → matcher → тег == эталону) — ГЛАВНАЯ,
             wake-detect, гейт планировщика, ложные срабатывания на ловушках,
             CER, задержка декода p50/p95, RSS процесса.

Запуск (модели грузить ПО ОДНОЙ — 8 ГБ RAM):
  python tools/bench_asr.py gen                  # корпус + WAV (однократно)
  python tools/bench_asr.py run <пресет>         # прогон одной модели → JSON-отчёт
  python tools/bench_asr.py report               # сводная таблица по готовым JSON
Пресеты: zipformer-small-ru (текущая), zipformer-ru, giga-am-v2-ctc, giga-am-v2-transducer.
"""
import difflib
import json
import os
import re
import statistics
import sys
import time
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np  # noqa: E402

from jarvis import config  # noqa: E402

BENCH_DIR = os.path.join(str(config.BASE_DIR), "models", "_bench")
CORPUS_DIR = os.path.join(BENCH_DIR, "corpus")
CORPUS_FILE = os.path.join(CORPUS_DIR, "corpus.json")
RESULTS_DIR = os.path.join(BENCH_DIR, "results")
SAMPLE_RATE = 16000

# Кандидаты — РОВНО те же пресеты и пути, что у боевого stt (config._ASR_PRESETS):
# бенчмарк меряет именно то, что загрузит сервис после смены models.asr_preset.
PRESETS = dict(config._ASR_PRESETS)

# Фразы планировщика (гейт — слой правил, эталон = гейт сработал).
SCHEDULER_PHRASES = [
    "поставь будильник на семь тридцать",
    "разбуди меня в восемь утра",
    "перенеси утренний на шесть",
    "отмени будильник на девять",
    "поставь таймер на пять минут",
    "таймер на полтора часа",
    "запусти секундомер",
    "останови секундомер",
    "напомни позвонить маме через час",
    "напомни про оплату завтра в десять",
    "добавь задачу купить продукты",
    "удали все таймеры",
]

# Ловушки: похожи на реплики Джарвиса/бытовую речь — НЕ должны давать команду.
TRAP_PHRASES = [
    "сделано сэр",
    "к вашим услугам всегда рад",
    "сегодня прекрасная погода не правда ли",
    "посмотри какой интересный фильм",
    "громкость у телевизора слишком большая",
    "надо бы не забыть про музыку",
    "свет в комнате какой-то тусклый",
    "я завтра пойду гулять в парк",
]


def rss_mb() -> int:
    with open(f"/proc/{os.getpid()}/status") as f:
        for line in f:
            if line.startswith("VmRSS"):
                return int(line.split()[1]) // 1024
    return -1


def _normalize(text: str) -> str:
    return re.sub(r"[^\w\s]", "", text, flags=re.UNICODE).strip().lower()


def strip_wake(text: str):
    """Снятие wake-word — та же логика, что stt._match_wake_word. None = wake не найден."""
    normalized = _normalize(text)
    if not normalized:
        return None
    for wake in config.WAKE_WORDS:
        wake = wake.strip().lower()
        if wake and normalized.startswith(wake):
            return normalized[len(wake):].strip()
    words = normalized.split()
    best = max((difflib.SequenceMatcher(None, words[0], w.strip().lower()).ratio()
                for w in config.WAKE_WORDS if w.strip()), default=0.0)
    if best >= config.WAKE_WORD_FUZZY_THRESHOLD:
        return " ".join(words[1:])
    return None


def cer(ref: str, hyp: str) -> float:
    """Доля ошибочных символов (расстояние Левенштейна / длину эталона)."""
    a, b = _normalize(ref), _normalize(hyp)
    if not a:
        return 0.0 if not b else 1.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] / len(a)


# ---------------------------------------------------------------------------- #
# Генерация корпуса и аудио
# ---------------------------------------------------------------------------- #
def build_corpus():
    """Корпус: текст, тип (команда/планировщик/ловушка), эталонный тег (по ЧИСТОМУ тексту)."""
    import yaml

    from jarvis.matcher import Matcher
    # Гейт ЦЕЛИКОМ как в core (reminders-обёртка: таймеры+будильники+напоминания+задачи).
    from jarvis.reminders import is_scheduler_command

    with open(str(config.COMMANDS_FILE), encoding="utf-8") as f:
        commands = yaml.safe_load(f) or {}
    matcher = Matcher(commands)
    items = []
    for tag, spec in commands.items():
        if not isinstance(spec, dict) or tag in ("ветки", "обратимость"):
            continue
        examples = spec.get("примеры") or []
        if not examples:
            continue
        text = str(examples[0]).strip().lower()
        m = matcher.match(text)   # эталон по чистому тексту (правила+эмбеддинги, как в core)
        if m is None:
            continue              # пример сам не матчится — не годится в эталоны
        items.append({"text": text, "kind": "команда", "tag": m.tag})
    for text in SCHEDULER_PHRASES:
        if is_scheduler_command(text):
            items.append({"text": text, "kind": "планировщик", "tag": None})
    for text in TRAP_PHRASES:
        items.append({"text": text, "kind": "ловушка", "tag": None})
    return items


def synth_corpus(items):
    """Синтез WAV: 2 варианта на фразу (голос+темп детерминированно по индексу), 16 кГц."""
    from piper import PiperVoice, SynthesisConfig

    voices_spec = [
        ("dmitri", str(config.PIPER_MODEL)),
        ("irina", os.path.join(BENCH_DIR, "ru_RU-irina-medium.onnx")),
        ("ruslan", os.path.join(BENCH_DIR, "ru_RU-ruslan-medium.onnx")),
    ]
    scales = (0.95, 1.1)
    voices = {}
    for name, path in voices_spec:
        voices[name] = PiperVoice.load(path, config_path=path + ".json")
        print(f"голос {name} загружен ({rss_mb()} МБ)", flush=True)

    os.makedirs(CORPUS_DIR, exist_ok=True)
    utts = []
    for i, item in enumerate(items):
        # Команды произносим С wake-word (сквозная проверка), ловушки — без.
        spoken = item["text"] if item["kind"] == "ловушка" else "джарвис, " + item["text"]
        for v in range(2):
            name, _ = voices_spec[(i + v) % len(voices_spec)]
            scale = scales[(i + v) % len(scales)]
            wav_name = f"u{i:03d}_{v}_{name}.wav"
            wav_path = os.path.join(CORPUS_DIR, wav_name)
            if not os.path.exists(wav_path):
                voice = voices[name]
                pcm = b"".join(ch.audio_int16_bytes for ch in voice.synthesize(
                    spoken, syn_config=SynthesisConfig(length_scale=scale)))
                audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                native = voice.config.sample_rate
                # Ресемпл native→16к: линейная интерполяция (для бенчмарка достаточно).
                n_out = int(len(audio) * SAMPLE_RATE / native)
                audio16 = np.interp(np.linspace(0, len(audio) - 1, n_out),
                                    np.arange(len(audio)), audio).astype(np.float32)
                with wave.open(wav_path, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(SAMPLE_RATE)
                    w.writeframes((audio16 * 32767).astype(np.int16).tobytes())
            utts.append({"wav": wav_name, "idx": i, "spoken": spoken, **item})
        if (i + 1) % 20 == 0:
            print(f"  синтез: {i+1}/{len(items)} фраз", flush=True)
    return utts


def make_babble(utts, seconds=30):
    """«Бормотание» (ТВ-фон): сумма трёх случайных сдвинутых фраз корпуса."""
    rng = np.random.default_rng(99)
    out = np.zeros(seconds * SAMPLE_RATE, dtype=np.float32)
    for _ in range(3):
        mix = np.zeros_like(out)
        pos = 0
        while pos < len(out):
            u = utts[int(rng.integers(0, len(utts)))]
            with wave.open(os.path.join(CORPUS_DIR, u["wav"]), "rb") as w:
                a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
            end = min(pos + len(a), len(out))
            mix[pos:end] += a[:end - pos]
            pos = end
        out += mix
    out /= max(1e-6, np.max(np.abs(out)))
    np.save(os.path.join(CORPUS_DIR, "babble.npy"), out)
    print("бормотание готово", flush=True)


def cmd_gen():
    items = build_corpus()
    print(f"корпус: {len(items)} фраз "
          f"(команд {sum(1 for x in items if x['kind']=='команда')}, "
          f"планировщик {sum(1 for x in items if x['kind']=='планировщик')}, "
          f"ловушек {sum(1 for x in items if x['kind']=='ловушка')})", flush=True)
    utts = synth_corpus(items)
    with open(CORPUS_FILE, "w", encoding="utf-8") as f:
        json.dump(utts, f, ensure_ascii=False, indent=1)
    make_babble([u for u in utts if u["kind"] == "команда"])
    print(f"готово: {len(utts)} высказываний в {CORPUS_DIR}", flush=True)


# ---------------------------------------------------------------------------- #
# Прогон модели
# ---------------------------------------------------------------------------- #
def load_recognizer(preset: str):
    """Загрузка РОВНО как stt._init_engines: те же ветки по типу и те же пути (config)."""
    import sherpa_onnx

    spec = PRESETS[preset]
    t0 = time.monotonic()
    if "дир" not in spec:   # боевой small: пути ZIPFORMER_* (уважают env-оверрайды)
        paths = {"encoder": str(config.ZIPFORMER_ENCODER), "decoder": str(config.ZIPFORMER_DECODER),
                 "joiner": str(config.ZIPFORMER_JOINER), "tokens": str(config.ZIPFORMER_TOKENS),
                 "bpe": str(config.ZIPFORMER_BPE)}
    else:
        d = config.MODELS_DIR / spec["дир"]
        paths = {k: str(d / v) for k, v in spec["файлы"].items()}
    if spec["тип"] == "nemo_ctc":
        rec = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
            model=paths["model"], tokens=paths["tokens"],
            num_threads=config.STT_NUM_THREADS, sample_rate=SAMPLE_RATE)
    elif spec["тип"] == "nemo_transducer":
        rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=paths["encoder"], decoder=paths["decoder"], joiner=paths["joiner"],
            tokens=paths["tokens"], model_type="nemo_transducer",
            num_threads=config.STT_NUM_THREADS, sample_rate=SAMPLE_RATE)
    else:  # zipformer_bpe (small и полный)
        rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=paths["encoder"], decoder=paths["decoder"], joiner=paths["joiner"],
            tokens=paths["tokens"], modeling_unit="bpe", bpe_vocab=paths["bpe"],
            num_threads=config.STT_NUM_THREADS)
    print(f"модель {preset} загружена за {time.monotonic()-t0:.1f}с, RSS {rss_mb()} МБ", flush=True)
    return rec


def mix_noise(audio: np.ndarray, noise: np.ndarray, snr_db: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    if len(noise) < len(audio):
        noise = np.tile(noise, int(np.ceil(len(audio) / len(noise))))
    start = int(rng.integers(0, len(noise) - len(audio) + 1))
    n = noise[start:start + len(audio)]
    p_sig = float(np.mean(audio ** 2) + 1e-12)
    p_noise = float(np.mean(n ** 2) + 1e-12)
    k = np.sqrt(p_sig / (p_noise * 10 ** (snr_db / 10)))
    out = audio + k * n
    peak = float(np.max(np.abs(out)))
    return (out / peak * 0.97).astype(np.float32) if peak > 1.0 else out.astype(np.float32)


def cmd_run(preset: str):
    import yaml

    from jarvis.matcher import Matcher
    # Гейт ЦЕЛИКОМ как в core (reminders-обёртка: таймеры+будильники+напоминания+задачи).
    from jarvis.reminders import is_scheduler_command

    with open(CORPUS_FILE, encoding="utf-8") as f:
        utts = json.load(f)
    babble = np.load(os.path.join(CORPUS_DIR, "babble.npy"))
    rng = np.random.default_rng(5)
    white = (rng.standard_normal(30 * SAMPLE_RATE)).astype(np.float32)
    with open(str(config.COMMANDS_FILE), encoding="utf-8") as f:
        matcher = Matcher(yaml.safe_load(f) or {})
    rec = load_recognizer(preset)
    rss_loaded = rss_mb()

    conditions = [("чисто", None, 0), ("шум10", "babble", 10), ("шум5", "white", 5)]
    stats = {c[0]: {"tag_ok": 0, "tag_n": 0, "wake_ok": 0, "wake_n": 0,
                    "sched_ok": 0, "sched_n": 0, "trap_fa": 0, "trap_n": 0, "cers": []}
             for c in conditions}
    lat = []
    t_start = time.monotonic()
    for k, u in enumerate(utts):
        with wave.open(os.path.join(CORPUS_DIR, u["wav"]), "rb") as w:
            audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        for cname, ntype, snr in conditions:
            sig = audio
            if ntype:
                sig = mix_noise(audio, babble if ntype == "babble" else white, snr, seed=k * 7 + snr)
            t0 = time.perf_counter()
            st = rec.create_stream()
            st.accept_waveform(SAMPLE_RATE, sig)
            rec.decode_stream(st)
            dt = time.perf_counter() - t0
            lat.append(dt)
            hyp = st.result.text.strip().lower()
            s = stats[cname]
            s["cers"].append(cer(u["spoken"], hyp))
            if u["kind"] == "ловушка":
                s["trap_n"] += 1
                m = matcher.match(_normalize(hyp))
                if m is not None and strip_wake(hyp) is not None:
                    s["trap_fa"] += 1   # ловушка БЕЗ wake прикинулась командой с wake
                continue
            s["wake_n"] += 1
            cmd_text = strip_wake(hyp)
            if cmd_text is not None:
                s["wake_ok"] += 1
            if u["kind"] == "команда":
                s["tag_n"] += 1
                m = matcher.match(cmd_text) if cmd_text else None
                if m is not None and m.tag == u["tag"]:
                    s["tag_ok"] += 1
            else:  # планировщик
                s["sched_n"] += 1
                if cmd_text and is_scheduler_command(cmd_text):
                    s["sched_ok"] += 1
        if (k + 1) % 40 == 0:
            print(f"  {k+1}/{len(utts)} высказываний, RSS {rss_mb()} МБ", flush=True)

    result = {
        "preset": preset,
        "rss_loaded_mb": rss_loaded,
        "rss_final_mb": rss_mb(),
        "lat_p50_ms": round(statistics.median(lat) * 1000),
        "lat_p95_ms": round(sorted(lat)[int(len(lat) * 0.95)] * 1000),
        "total_s": round(time.monotonic() - t_start),
        "условия": {},
    }
    for cname, *_ in conditions:
        s = stats[cname]
        result["условия"][cname] = {
            "tag_accuracy": round(s["tag_ok"] / max(1, s["tag_n"]), 3),
            "wake_rate": round(s["wake_ok"] / max(1, s["wake_n"]), 3),
            "sched_rate": round(s["sched_ok"] / max(1, s["sched_n"]), 3),
            "trap_false": f"{s['trap_fa']}/{s['trap_n']}",
            "cer": round(statistics.mean(s["cers"]), 3),
        }
    os.makedirs(RESULTS_DIR, exist_ok=True)
    out = os.path.join(RESULTS_DIR, f"{preset}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(json.dumps(result, ensure_ascii=False, indent=1), flush=True)


def cmd_report():
    rows = []
    for name in PRESETS:
        path = os.path.join(RESULTS_DIR, f"{name}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                rows.append(json.load(f))
    if not rows:
        print("нет результатов — сперва `run <пресет>`")
        return
    hdr = f"{'модель':<22} {'RSS МБ':>7} {'p50/p95 мс':>11} | " + " | ".join(
        f"{c}: tag/wake/cer" for c in ("чисто", "шум10", "шум5"))
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        cells = []
        for c in ("чисто", "шум10", "шум5"):
            u = r["условия"][c]
            cells.append(f"{u['tag_accuracy']:.2f}/{u['wake_rate']:.2f}/{u['cer']:.2f}")
        print(f"{r['preset']:<22} {r['rss_final_mb']:>7} {r['lat_p50_ms']:>5}/{r['lat_p95_ms']:<5} | "
              + " | ".join(cells))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "gen":
        cmd_gen()
    elif mode == "run" and len(sys.argv) > 2 and sys.argv[2] in PRESETS:
        cmd_run(sys.argv[2])
    elif mode == "report":
        cmd_report()
    else:
        print(__doc__)
        sys.exit(2)
