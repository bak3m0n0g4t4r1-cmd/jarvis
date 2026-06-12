"""JARVIS-DSP: офлайн-обработка голоса Silero под «дворецкого» через ffmpeg.

Голос eugene сам по себе далёк от «голоса Джарвиса» из дубляжа Iron Man. Чтобы
приблизить — лёгкое понижение тона, тёплый низ, присутствие в середине, мягкие
шипящие, ровная компрессия и едва слышный «эфир» (reverb). ВСЯ обработка делается
ОДИН раз — при пред-рендере в кэш (jarvis tts build) или на редкой миссе, поэтому
в горячем пути воспроизведения стоит ноль (играем готовый WAV).

Движок — ffmpeg (есть в системе; sox отсутствует). Никаких Python-зависимостей:
PCM s16 mono гоним через `ffmpeg -f s16le … -af "<цепочка>" …` в subprocess.

Параметры (config.DSP_PARAMS / секция voice.dsp в settings.yaml) подбираются в
интерактивной тулзе tools/voice_studio.py: synth-фразу синтезируем Silero ОДИН раз,
а DSP пере-применяем мгновенно при движении ползунков.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess


# Дефолтный «сбалансированный» пресет (баритон-дворецкий, разборчивый, лёгкая «эфирность»).
# Любой ключ можно переопределить в settings.yaml → voice.dsp. Стадия выключается, когда
# её усиление 0 / коэффициент ≤1 / флаг false — поэтому «натурально» = обнулить лишнее.
DEFAULT_PARAMS: dict = {
    "pitch_cents": -120,      # тон ниже на ~1.2 полутона (ratio 2^(cents/1200)); 0 — без сдвига
    "tempo": 1.0,             # доп. темп поверх (1.0 — без изменения; <1 медленнее, степеннее)
    "bass_gain": 3.0,         # тёплый низ, дБ
    "bass_freq": 110,         # частота полки низа, Гц
    "presence_gain": 2.0,     # присутствие/чёткость речи, дБ
    "presence_freq": 2800,    # центр presence-колокола, Гц
    "treble_gain": -2.0,      # мягче шипящие, дБ (отрицательное — приглушить верх)
    "treble_freq": 9000,      # частота полки верха, Гц
    "comp_ratio": 2.2,        # компрессия (ровный спокойный уровень); ≤1 — выключить
    "comp_threshold": -18,    # порог компрессора, дБ
    "comp_makeup": 2.0,       # компенсация громкости после компрессии, дБ
    "reverb": True,           # лёгкий «эфир» (aecho), не эхо
    "reverb_in": 0.85,        # вход aecho
    "reverb_out": 0.9,        # выход aecho
    "reverb_delays": "40|55",   # задержки отражений, мс
    "reverb_decays": "0.22|0.18",  # затухания отражений (малые → не «зал»)
    "limit": 0.97,            # финальный лимитер против клиппинга после усилений (0..1); 0 — выкл
    "trim_silence": True,     # срезать хвостовую тишину (иначе лампы «висят» в конце фразы)
}

def _f(params: dict, key: str) -> float:
    """Число из params с дефолтом (битое значение → дефолт, DSP не падает на мусоре)."""
    try:
        return float(params.get(key, DEFAULT_PARAMS.get(key)))
    except (TypeError, ValueError):
        return float(DEFAULT_PARAMS.get(key, 0.0))


def build_filter_chain(params: dict, in_rate: int, out_rate: int) -> str:
    """Собрать ffmpeg-цепочку `-af` из параметров. Стадии с нулевым эффектом опускаются."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    chain: list[str] = []

    # 1) Сдвиг тона без изменения длительности: asetrate (тон+темп) → aresample → atempo (вернуть темп).
    cents = _f(p, "pitch_cents")
    if abs(cents) >= 1.0:
        ratio = 2.0 ** (cents / 1200.0)         # <1 — ниже тон
        chain.append(f"asetrate={int(round(in_rate * ratio))}")
        chain.append(f"aresample={in_rate}")
        chain.append(f"atempo={1.0 / ratio:.6f}")  # восстановить длительность (тон не меняет)

    # 2) Доп. темп (степенность речи): atempo в допустимом диапазоне 0.5..2.0.
    tempo = _f(p, "tempo")
    if abs(tempo - 1.0) >= 0.01:
        chain.append(f"atempo={min(2.0, max(0.5, tempo)):.4f}")

    # 3) Тёплый низ.
    if abs(_f(p, "bass_gain")) >= 0.1:
        chain.append(f"bass=g={_f(p,'bass_gain'):.2f}:f={int(_f(p,'bass_freq'))}")

    # 4) Присутствие/чёткость (колокол в середине).
    if abs(_f(p, "presence_gain")) >= 0.1:
        chain.append(
            f"equalizer=f={int(_f(p,'presence_freq'))}:t=q:w=1.0:g={_f(p,'presence_gain'):.2f}")

    # 5) Мягче шипящие (полка верха).
    if abs(_f(p, "treble_gain")) >= 0.1:
        chain.append(f"treble=g={_f(p,'treble_gain'):.2f}:f={int(_f(p,'treble_freq'))}")

    # 6) Компрессия — ровный спокойный уровень.
    if _f(p, "comp_ratio") > 1.0:
        chain.append(
            f"acompressor=threshold={_f(p,'comp_threshold'):.1f}dB:ratio={_f(p,'comp_ratio'):.2f}"
            f":attack=20:release=250:makeup={_f(p,'comp_makeup'):.2f}")

    # 7) Лёгкий «эфир» (aecho приближает короткий reverb; не «зал»).
    if p.get("reverb"):
        chain.append(
            f"aecho={_f(p,'reverb_in'):.2f}:{_f(p,'reverb_out'):.2f}"
            f":{p.get('reverb_delays', DEFAULT_PARAMS['reverb_delays'])}"
            f":{p.get('reverb_decays', DEFAULT_PARAMS['reverb_decays'])}")

    # 8) Срез хвостовой тишины (лампы не зависают на молчании в конце).
    if p.get("trim_silence"):
        chain.append("silenceremove=stop_periods=-1:stop_duration=0.12:stop_threshold=-50dB")

    # 9) Финальный лимитер — усиления низа/presence не дают клиппинга.
    if _f(p, "limit") > 0.0:
        chain.append(f"alimiter=limit={min(1.0, _f(p,'limit')):.3f}")

    # 10) Привести к выходной частоте (= частоте WAV в кэше = --rate pw-cat).
    if out_rate != in_rate:
        chain.append(f"aresample={out_rate}")

    return ",".join(chain) if chain else "anull"


def apply_dsp(pcm: bytes, in_rate: int, params: dict, out_rate: int | None = None) -> bytes:
    """Прогнать сырой s16 mono PCM через JARVIS-DSP ffmpeg-цепочку. Возвращает s16 mono PCM.

    Бросает RuntimeError, если ffmpeg недоступен или вернул ошибку — вызывающий решает,
    падать (build/studio) или взять «сухой» PCM (рантайм-мисс)."""
    if not pcm:
        return b""
    out_rate = int(out_rate or in_rate)
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg не найден — обработка голоса невозможна (apt install ffmpeg)")
    af = build_filter_chain(params, int(in_rate), out_rate)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-f", "s16le", "-ar", str(int(in_rate)), "-ac", "1", "-i", "pipe:0",
           "-af", af, "-f", "s16le", "-ar", str(out_rate), "-ac", "1", "pipe:1"]
    proc = subprocess.run(cmd, input=pcm, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"ffmpeg DSP вернул код {proc.returncode}: {detail or 'без stderr'}")
    return proc.stdout


def dsp_signature(params: dict, voice_id: str) -> str:
    """Стабильный короткий хэш (10 hex) от параметров DSP + идентификатора голоса.

    Кладётся в путь кэша (cache/tts/<voice_id>/<dsp_sig>/) — смена ЛЮБОГО параметра
    обработки или голоса автоматически даёт новый подкаталог, старый кэш не путается
    с новым тембром (чистится `jarvis tts stats --prune`)."""
    merged = {**DEFAULT_PARAMS, **(params or {})}
    blob = json.dumps({"voice": voice_id, "dsp": merged}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]
