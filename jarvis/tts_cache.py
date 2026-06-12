"""Контент-адресуемый кэш синтезированной речи (WAV целых фраз).

Голос Silero на N100 синтезируется со скоростью ~реального времени (RTF≈1) — это секунды
задержки на фразу. Поэтому ВСЕ фразы заранее рендерятся в WAV-кэш (jarvis tts build), а
в рантайме играется готовый WAV — мгновенно и без torch. Свободный текст, которого нет в
кэше, рендерится лениво на промахе и тоже оседает сюда.

Ключ файла = SHA1 ИТОГОВОГО текста синтеза (после словаря произношения + `+`-ударений) —
ровно той строки, что ушла бы в Silero. Одинаковый итог делит один WAV. Тембр (голос+DSP)
версионируется ПУТЁМ: cache/tts/<voice_id>/<dsp_sig>/<aa>/<key>.wav. Смена голоса/DSP →
новый подкаталог; старый осиротевает (чистится `jarvis tts stats --prune`).

Формат — обычный WAV (s16 mono, частота = частота воспроизведения): самодокументирует
частоту, читается stdlib `wave`, виден в файловом менеджере для отладки.
"""
from __future__ import annotations

import hashlib
import wave
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CachedClip:
    """Готовый звук фразы: сырой s16 mono PCM + его частота (для pw-cat --rate)."""
    pcm: bytes
    rate: int


def _read_wav(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        pcm = w.readframes(w.getnframes())
    return pcm, int(rate)


def _write_wav(path: Path, pcm: bytes, rate: int) -> None:
    """Атомарная запись WAV (через .tmp + replace) — недописанный файл не попадёт в кэш."""
    tmp = path.with_suffix(".wav.tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm)
    tmp.replace(path)


class TtsCache:
    """WAV-кэш фраз для одной комбинации голос+DSP (своя поддиректория)."""

    def __init__(self, root: str, voice_id: str, dsp_sig: str):
        self.root = Path(root)
        self.voice_id = voice_id
        self.dsp_sig = dsp_sig
        self.dir = self.root / voice_id / dsp_sig

    def key(self, final_text: str) -> str:
        return hashlib.sha1(final_text.encode("utf-8")).hexdigest()

    def path_for(self, key: str) -> Path:
        return self.dir / key[:2] / f"{key}.wav"  # шардируем по первым 2 hex (тысячи файлов)

    def has(self, final_text: str) -> bool:
        return self.path_for(self.key(final_text)).exists()

    def get(self, final_text: str) -> CachedClip | None:
        """Достать готовый клип по тексту. Нет файла/битый WAV → None (рантайм синтезирует)."""
        p = self.path_for(self.key(final_text))
        if not p.exists():
            return None
        try:
            pcm, rate = _read_wav(p)
            return CachedClip(pcm, rate) if pcm else None
        except Exception:
            return None

    def put(self, final_text: str, pcm: bytes, rate: int) -> Path:
        p = self.path_for(self.key(final_text))
        p.parent.mkdir(parents=True, exist_ok=True)
        _write_wav(p, pcm, int(rate))
        return p

    def stats(self) -> dict:
        """Сводка по текущей поддиректории (голос+DSP): число клипов и объём на диске."""
        files = list(self.dir.rglob("*.wav")) if self.dir.exists() else []
        total = 0
        for f in files:
            try:
                total += f.stat().st_size
            except OSError:
                pass
        return {"count": len(files), "bytes": total, "dir": str(self.dir),
                "voice_id": self.voice_id, "dsp_sig": self.dsp_sig}

    def prune_other_signatures(self) -> int:
        """Удалить кэш-поддиректории ДРУГИХ сигнатур голос/DSP (осиротевшие после правок).

        Возвращает число удалённых файлов. Текущую (self.dir) НЕ трогает."""
        import shutil
        removed = 0
        if not self.root.exists():
            return 0
        for vdir in self.root.iterdir():
            if not vdir.is_dir():
                continue
            for sdir in vdir.iterdir():
                if sdir.is_dir() and sdir != self.dir:
                    removed += sum(1 for _ in sdir.rglob("*.wav"))
                    shutil.rmtree(sdir, ignore_errors=True)
            # пустой каталог голоса убрать
            try:
                if vdir != self.dir.parent and not any(vdir.iterdir()):
                    vdir.rmdir()
            except OSError:
                pass
        return removed
