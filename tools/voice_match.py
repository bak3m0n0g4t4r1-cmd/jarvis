#!/usr/bin/env python3
"""Свести голос Silero к эталонному русскому Джарвису по ОБЪЕКТИВНЫМ метрикам.

У пользователя есть эталонный sound pack (реальный русский Джарвис). Этот инструмент:
  1. меряет усреднённый спектр + питч (F0) + реверб эталонных файлов;
  2. синтезирует «сухой» Silero (выбранным спикером), меряет то же;
  3. вычисляет DSP, приводящий Silero к эталону — pitch-сдвиг (rubberband) к F0 эталона +
     многополосный matching-EQ (firequalizer) под спектр эталона + реверб;
  4. печатает diff и (по флагу) пишет пресет в settings.yaml → voice.dsp.

Порядок важен: EQ считается ПОСЛЕ pitch-сдвига (rubberband меняет спектральный баланс),
ровно как в боевой цепочке tts_dsp: rubberband → firequalizer → reverb → limiter.

Запуск:
  python tools/voice_match.py                      # анализ + расчёт (без записи)
  python tools/voice_match.py --speaker-scan       # сравнить спикеров Silero с эталоном
  python tools/voice_match.py --write              # записать пресет в settings.yaml
  python tools/voice_match.py --ref-dir "<путь к sound pack>"
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import wave

import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from jarvis import config  # noqa: E402

REF_DIR_DEFAULT = "/home/tux/Загрузки/Jarvis Sound Pack от Jarvis Desktop"
# Эталонные файлы с чистой речью (без музыки/звонков) — репрезентативная выборка.
REF_FILES = ["Да сэр.wav", "Да сэр(второй).wav", "К вашим услугам сэр.wav",
             "Всегда к вашим услугам сэр.wav", "Загружаю сэр.wav", "Запрос выполнен сэр.wav",
             "Доброе утро.wav", "Да, это поможет вам оставаться незамеченным.wav",
             "Мы подключены и готовы.wav", "Другой информации нет.wav"]
# Тест-фразы для синтеза Silero (близки по длине/составу к эталонным).
SYNTH_PHRASES = ["Да, сэр.", "Всегда к вашим услугам, сэр.", "Загружаю, сэр.",
                 "Доброе утро, сэр.", "Запрос выполнен, сэр.", "Мы подключены и готовы."]

# Центры 1/3-октавных полос 80 Гц … 12.5 кГц (для спектрального сведения).
def _third_octave_centers(lo=80.0, hi=12500.0):
    centers = []
    f = lo
    while f <= hi * 1.001:
        centers.append(f)
        f *= 2 ** (1 / 3)
    return np.array(centers)


BANDS = _third_octave_centers()


def load_mono(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        x = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32)
    if ch == 2:
        x = x.reshape(-1, 2).mean(1)
    return x / 32768.0, sr


def f0_windows(x: np.ndarray, sr: int) -> list[float]:
    """F0 по голосовым окнам (автокорреляция, 70–350 Гц). Список всех оценок."""
    win, hop = int(0.04 * sr), int(0.02 * sr)
    out = []
    for i in range(0, len(x) - win, hop):
        seg = x[i:i + win]
        if np.sqrt(np.mean(seg ** 2)) < 0.02:
            continue
        seg = seg - seg.mean()
        ac = np.correlate(seg, seg, "full")[len(seg) - 1:]
        lo, hi = int(sr / 350), int(sr / 70)
        if hi >= len(ac):
            continue
        peak = lo + int(np.argmax(ac[lo:hi]))
        if ac[peak] > 0.3 * ac[0]:
            out.append(sr / peak)
    return out


def band_spectrum_db(x: np.ndarray, sr: int) -> np.ndarray:
    """Средняя мощность в 1/3-октавных полосах (дБ), усреднённая по окнам сигнала."""
    win = 4096
    if len(x) < win:
        x = np.pad(x, (0, win - len(x)))
    hop = win // 2
    acc = np.zeros(len(BANDS))
    cnt = 0
    w = np.hanning(win)
    freqs = np.fft.rfftfreq(win, 1 / sr)
    edges_lo = BANDS / 2 ** (1 / 6)
    edges_hi = BANDS * 2 ** (1 / 6)
    idx = [np.where((freqs >= lo) & (freqs < hi))[0] for lo, hi in zip(edges_lo, edges_hi)]
    for i in range(0, len(x) - win, hop):
        seg = x[i:i + win]
        if np.sqrt(np.mean(seg ** 2)) < 0.01:
            continue
        p = np.abs(np.fft.rfft(seg * w)) ** 2
        for b, ix in enumerate(idx):
            if len(ix):
                acc[b] += p[ix].mean()
        cnt += 1
    if cnt:
        acc /= cnt
    return 10 * np.log10(acc + 1e-12)


def reverb_tail_ms(x: np.ndarray, sr: int) -> float:
    env = np.abs(x)
    wv = int(0.02 * sr)
    env = np.convolve(env, np.ones(wv) / wv, "same")
    peak = env.max()
    above = np.where(env > peak * 10 ** (-25 / 20))[0]
    if len(above) == 0:
        return 0.0
    tail = env[above[-1]:]
    below = np.where(tail < peak * 10 ** (-45 / 20))[0]
    return (below[0] / sr * 1000) if len(below) else (len(tail) / sr * 1000)


def analyze_signals(sigs: list[tuple[np.ndarray, int]]) -> dict:
    """Усреднить метрики по набору (массив, sr): спектр в полосах, F0, реверб."""
    specs, f0s, revs = [], [], []
    for x, sr in sigs:
        specs.append(band_spectrum_db(x, sr))
        f0s.extend(f0_windows(x, sr))
        revs.append(reverb_tail_ms(x, sr))
    spec = np.mean(specs, axis=0)
    spec -= spec.max()  # нормировка к пику (форма важна, не абсолют)
    return {"spec_db": spec, "f0": float(np.median(f0s)) if f0s else 0.0,
            "reverb_ms": float(np.median(revs))}


def rubberband_pcm(pcm: bytes, sr: int, pitch_scale: float, formant: str) -> bytes:
    """Применить только rubberband-pitch (для измерения спектра «после питча»)."""
    af = f"rubberband=pitch={pitch_scale:.5f}:formant={formant}:pitchq=quality"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(sr),
           "-ac", "1", "-i", "pipe:0", "-af", af, "-f", "s16le", "-ar", str(sr), "-ac", "1", "pipe:1"]
    r = subprocess.run(cmd, input=pcm, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.stdout


def synth_speaker(speaker: str) -> list[tuple[np.ndarray, int]]:
    """Синтезировать тест-фразы Silero спикером → список «сухих» сигналов."""
    from jarvis.tts_engine import SileroEngine
    eng = SileroEngine(speaker=speaker)
    eng.warmup()
    sr = eng.sample_rate
    out = []
    for ph in SYNTH_PHRASES:
        pcm = eng.synth(ph)
        if pcm:
            out.append((np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0, sr))
    eng.unload()
    return out


def compute_match(ref: dict, speaker: str, max_semitones: float = 8.0,
                  formant: str = "shifted") -> dict:
    """Вычислить pitch-сдвиг + matching-EQ + реверб, сводящие спикера к эталону.

    pitch ограничен max_semitones (полная октава вверх даёт артефакты — остальное добирает EQ).
    EQ считается на «сухом» спикере ПОСЛЕ rubberband-pitch (порядок как в боевой цепочке)."""
    sigs = synth_speaker(speaker)
    dry = analyze_signals(sigs)
    # Питч: к F0 эталона, но не больше max_semitones (естественность важнее точного совпадения).
    ratio = (ref["f0"] / dry["f0"]) if dry["f0"] else 1.0
    cap = 2 ** (max_semitones / 12)
    pitch_scale = min(cap, max(1 / cap, ratio))
    semitones = 12 * np.log2(pitch_scale)
    # Спектр спикера ПОСЛЕ pitch (rubberband меняет баланс) — на нём считаем EQ.
    pitched = []
    for x, sr in sigs:
        pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
        pb = rubberband_pcm(pcm, sr, pitch_scale, formant)
        if pb:
            pitched.append((np.frombuffer(pb, dtype=np.int16).astype(np.float32) / 32768.0, sr))
    pitched_spec = analyze_signals(pitched)["spec_db"]
    # matching-EQ: разница форм спектра (эталон − наш-после-питча), сглаживание, клип ±12 дБ.
    diff = ref["spec_db"] - pitched_spec
    diff -= np.median(diff)  # убрать общий уровень — firequalizer формирует ФОРМУ
    sm = np.convolve(diff, np.ones(3) / 3, "same")
    eq = np.clip(sm, -12, 12)
    eq_curve = [(round(float(f), 1), round(float(g), 1)) for f, g in zip(BANDS, eq)]
    return {"speaker": speaker, "ratio_full": ratio, "pitch_scale": round(pitch_scale, 4),
            "semitones": round(float(semitones), 2), "formant": formant,
            "eq_curve": eq_curve, "reverb_ms": ref["reverb_ms"],
            "dry_f0": dry["f0"], "ref_f0": ref["f0"], "dry_spec": dry["spec_db"]}


def load_refs(ref_dir: str) -> list[tuple[np.ndarray, int]]:
    sigs = []
    for name in REF_FILES:
        p = os.path.join(ref_dir, name)
        if os.path.exists(p):
            sigs.append(load_mono(p))
    if not sigs:  # фоллбэк: любые wav из папки
        for p in sorted(glob.glob(os.path.join(ref_dir, "*.wav")))[:10]:
            sigs.append(load_mono(p))
    return sigs


def _fmt_band(f):
    return f"{f/1000:.1f}k" if f >= 1000 else f"{int(f)}"


def main():
    ap = argparse.ArgumentParser(description="Свести голос Silero к эталонному русскому Джарвису")
    ap.add_argument("--ref-dir", default=REF_DIR_DEFAULT)
    ap.add_argument("--speaker", default=config.SILERO_SPEAKER)
    ap.add_argument("--speaker-scan", action="store_true",
                    help="сравнить мужских спикеров Silero с эталоном (F0/спектральная дистанция)")
    ap.add_argument("--max-semitones", type=float, default=8.0)
    ap.add_argument("--formant", default="shifted", choices=["shifted", "preserved"])
    ap.add_argument("--write", action="store_true", help="записать пресет в settings.yaml voice.dsp")
    args = ap.parse_args()

    refs = load_refs(args.ref_dir)
    if not refs:
        print(f"Эталоны не найдены в {args.ref_dir}")
        return 1
    ref = analyze_signals(refs)
    print(f"ЭТАЛОН ({len(refs)} файлов): F0 {ref['f0']:.0f} Гц, реверб {ref['reverb_ms']:.0f} мс")

    if args.speaker_scan:
        print("\nСравнение спикеров Silero с эталоном (меньше дистанция = ближе):")
        for sp in ["eugene", "aidar", "baya", "xenia", "kseniya"]:
            try:
                dry = analyze_signals(synth_speaker(sp))
                dist = float(np.sqrt(np.mean((ref["spec_db"] - dry["spec_db"]) ** 2)))
                print(f"  {sp:9} F0 {dry['f0']:5.0f} Гц | спектр-дистанция {dist:5.1f} дБ")
            except Exception as exc:
                print(f"  {sp:9} ошибка: {exc}")
        return 0

    m = compute_match(ref, args.speaker, args.max_semitones, args.formant)
    print(f"\nСпикер {m['speaker']}: F0 {m['dry_f0']:.0f} → цель {m['ref_f0']:.0f} Гц "
          f"(нужно ×{m['ratio_full']:.2f}, ставлю ×{m['pitch_scale']:.2f} = {m['semitones']:+.1f} полутона, "
          f"формант {m['formant']})")
    print(f"Реверб: {m['reverb_ms']:.0f} мс")
    print("\nMatching-EQ кривая (firequalizer), дБ по полосам:")
    for f, g in m["eq_curve"]:
        bar = ("+" if g >= 0 else "-") * min(12, int(abs(g)))
        print(f"  {_fmt_band(f):>6} Гц : {g:+5.1f}  {bar}")

    if args.write:
        _write_preset(m)
        print("\n✓ Пресет записан в settings.yaml → voice.dsp. Пересоберите кэш: jarvis tts build --force")
    else:
        print("\n(для записи в settings.yaml добавьте --write)")
    return 0


def _write_preset(m: dict):
    """Записать pitch/formant/eq_curve/reverb в settings.yaml voice.dsp (переиспуем writer студии)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "voice_studio", os.path.join(BASE, "tools", "voice_studio.py"))
    vs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vs)
    from jarvis import tts_dsp
    params = {**tts_dsp.DEFAULT_PARAMS}
    params.update({
        "engine_speaker": m["speaker"],
        "pitch_semitones": m["semitones"],
        "formant": m["formant"],
        "eq_curve": ";".join(f"{f}:{g}" for f, g in m["eq_curve"]),
        "reverb_ms": round(m["reverb_ms"]),
        # старые понижающие параметры обнуляем (их заменил matching-EQ + rubberband)
        "pitch_cents": 0, "bass_gain": 0, "presence_gain": 0, "treble_gain": 0,
    })
    vs._save_dsp_to_settings(params)


if __name__ == "__main__":
    sys.exit(main())
