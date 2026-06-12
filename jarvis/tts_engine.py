"""Движки синтеза речи: Silero (основной) и Piper (фоллбэк).

ВАЖНО про ресурсы: движок нужен ТОЛЬКО для генерации звука — при офлайн-сборке кэша
(jarvis tts build) и на редком промахе кэша (свободный текст). В горячем пути
воспроизведения движок НЕ участвует: TTS-сервис играет готовый WAV из кэша. Поэтому
torch/Silero импортируется ЛЕНИВО внутри SileroEngine._load() — обычный systemd-старт
TTS остаётся чисто-ONNX, без torch в памяти.

Движок отдаёт СЫРОЙ s16 mono PCM на своей родной частоте. «Очеловечивание» под голос
Джарвиса (понижение тона, EQ, реверб) делает отдельный слой jarvis.tts_dsp — так
tools/voice_studio может пере-применять DSP без повторного синтеза.
"""
from __future__ import annotations

import threading

import numpy as np

from jarvis import config


def _floats_to_pcm16(audio) -> bytes:
    """float32 [-1,1] (np.ndarray или torch.Tensor) → s16 LE mono bytes с клиппингом."""
    arr = np.asarray(audio, dtype=np.float32).reshape(-1)
    np.clip(arr, -1.0, 1.0, out=arr)
    return (arr * 32767.0).astype("<i2").tobytes()


class SileroEngine:
    """Silero TTS (спикер eugene). torch грузится лениво и выгружается по требованию.

    Модель — torch.package (`v4_ru.pt`), загружается через PackageImporter. Нативно
    понимает `+`-ударения (put_accent/put_yo), что и нужно для русской речи Джарвиса."""

    voice_id_fmt = "silero-{speaker}-{stem}-sr{rate}"

    def __init__(self, model_path: str | None = None, speaker: str | None = None,
                 sample_rate: int | None = None, threads: int | None = None):
        self.model_path = model_path or config.SILERO_MODEL
        self.speaker = speaker or config.SILERO_SPEAKER
        self.sample_rate = int(sample_rate or config.SILERO_SAMPLE_RATE)
        self.threads = int(threads if threads is not None else (config.VOICE_SYNTH_THREADS or 0))
        self._model = None
        self._lock = threading.Lock()

    @property
    def voice_id(self) -> str:
        """Идентификатор тембра для версионирования кэша (часть пути)."""
        from pathlib import Path
        stem = Path(self.model_path).stem  # напр. v4_ru
        return self.voice_id_fmt.format(speaker=self.speaker, stem=stem, rate=self.sample_rate)

    def _load(self):
        """Лениво загрузить модель (потокобезопасно). torch импортируется ТОЛЬКО здесь."""
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            import torch  # ленивый импорт: вне этого пути torch в памяти нет
            if self.threads > 0:
                torch.set_num_threads(self.threads)
            package = torch.package.PackageImporter(self.model_path)
            model = package.load_pickle("tts_models", "model")
            model.to(torch.device("cpu"))
            self._model = model
            return model

    def warmup(self) -> None:
        self._load()

    def synth(self, text: str) -> bytes:
        """Синтезировать фразу → сырой s16 mono PCM на self.sample_rate. Пусто/сбой → b''."""
        text = (text or "").strip()
        if not text:
            return b""
        model = self._load()
        audio = model.apply_tts(text=text, speaker=self.speaker,
                                sample_rate=self.sample_rate, put_accent=True, put_yo=True)
        return _floats_to_pcm16(audio)

    def unload(self) -> None:
        """Выгрузить модель и вернуть RAM (swap=0 — память на счету). Идемпотентно."""
        with self._lock:
            self._model = None
        try:
            import gc
            gc.collect()
        except Exception:
            pass


class PiperEngine:
    """Фоллбэк-движок Piper (onnxruntime, без torch). Лёгкий, но тембр не «джарвисовский».

    Используется офлайн и как аварийный путь, если Silero/torch недоступны (нехватка RAM
    при swap=0). НЕ в горячем пути воспроизведения."""

    voice_id_fmt = "piper-{stem}-sr{rate}"

    def __init__(self, model_path: str | None = None, config_path: str | None = None):
        self.model_path = model_path or config.PIPER_MODEL
        self.config_path = config_path or config.PIPER_CONFIG
        self._voice = None
        self._lock = threading.Lock()
        self.sample_rate = 22050  # уточняется после загрузки из voice.config.sample_rate

    @property
    def voice_id(self) -> str:
        from pathlib import Path
        return self.voice_id_fmt.format(stem=Path(self.model_path).stem, rate=self.sample_rate)

    def _load(self):
        if self._voice is not None:
            return self._voice
        with self._lock:
            if self._voice is not None:
                return self._voice
            from piper import PiperVoice
            voice = PiperVoice.load(self.model_path, config_path=self.config_path)
            self.sample_rate = int(voice.config.sample_rate)
            self._voice = voice
            return voice

    def warmup(self) -> None:
        self._load()

    def synth(self, text: str) -> bytes:
        text = (text or "").strip()
        if not text:
            return b""
        from piper import SynthesisConfig
        voice = self._load()
        parts: list[bytes] = []
        for chunk in voice.synthesize(text, syn_config=SynthesisConfig()):
            parts.append(chunk.audio_int16_bytes)
        return b"".join(parts)

    def unload(self) -> None:
        with self._lock:
            self._voice = None


def make_engine(name: str | None = None):
    """Фабрика движка по имени ('silero'|'piper'). По умолчанию — config.TTS_ENGINE."""
    name = (name or config.TTS_ENGINE or "silero").strip().lower()
    if name == "piper":
        return PiperEngine()
    return SileroEngine()
