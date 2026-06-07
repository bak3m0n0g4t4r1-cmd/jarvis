"""Оценка звуковой обстановки и адаптивная громкость для TTS — по РЕАЛЬНЫМ уровням сигнала.

Меряем RMS реального сигнала (НЕ позиции регуляторов громкости):
- внешний шум — микрофон (постоянный фоновый замерщик);
- уровень воспроизведения ноута — monitor-source PipeWire (`pw-cat --record` по ID);
- внешний шум = mic_rms − k·monitor_rms — вычитаем свой звук из колонок (в т.ч. голос самого
  Джарвиса), поэтому НЕТ самоподхвата (рост громкости от собственного эха).

Плюс ducking музыки (`pactl`) и расчёт громкости голоса от внешнего шума И громкости речи
пользователя: в тишине следуем за пользователем (вплоть до шёпота), в шуме — за уровнем шума
(разборчивость важнее). Всё в try-except: любой сбой → деградация в фиксированную громкость,
сервис не падает.
"""
import logging
import math
import re
import subprocess
import threading
import time

import numpy as np

from jarvis import config

_log = logging.getLogger("jarvis-audio-env")
_SR = 16000  # частота замера (моно)


def to_db(rms: float) -> float:
    """RMS-амплитуду (0..1) в дБ (для человекочитаемых логов)."""
    return 20.0 * math.log10(max(rms, 1e-6))


class AudioEnv:
    """Постоянный замер обстановки + ducking + расчёт громкости. Лёгкий по CPU."""

    def __init__(self):
        self._mic_rms = 0.0        # текущий RMS микрофона
        self._noise = 0.0          # внешний шум (EMA; обновляется, когда Джарвис молчит)
        self._monitor_rms = 0.0    # RMS реального воспроизведения ноута (monitor)
        self._speaking = False
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._ducked: dict[str, float] = {}   # sink-input id → исходная громкость (для restore)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Жизненный цикл
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Запустить фоновые замерщики (микрофон + monitor воспроизведения)."""
        if not config.ADAPTIVE_VOLUME:
            _log.info("Адаптивная громкость выключена (JARVIS_ADAPTIVE_VOLUME=0)")
            return
        for target, name in ((self._mic_loop, "audio-mic"), (self._monitor_loop, "audio-mon")):
            t = threading.Thread(target=target, daemon=True, name=name)
            t.start()
            self._threads.append(t)
        _log.info("Замерщики обстановки запущены (микрофон + monitor воспроизведения)")

    def stop(self) -> None:
        self._stop.set()

    def set_speaking(self, value: bool) -> None:
        """TTS сообщает, говорит ли сейчас Джарвис (чтобы не считать свой голос внешним шумом)."""
        self._speaking = value

    # ------------------------------------------------------------------ #
    # Фоновые замерщики
    # ------------------------------------------------------------------ #
    def _mic_loop(self) -> None:
        try:
            import sounddevice as sd
        except Exception as exc:
            _log.warning("Замер микрофона недоступен (%s) — громкость будет фиксированной", exc)
            return
        block = max(160, int(config.NOISE_WINDOW * _SR))
        device = config.STT_SOURCE or None  # тот же источник, что слушает STT (опц. denoise)
        while not self._stop.is_set():
            try:
                with sd.InputStream(channels=1, samplerate=_SR, dtype="float32",
                                    device=device) as stream:
                    while not self._stop.is_set():
                        data, _ = stream.read(block)
                        rms = float(np.sqrt(np.mean(np.square(data, dtype=np.float64)) + 1e-12))
                        self._mic_rms = rms
                        # Внешний шум = микрофон − k·воспроизведение (вычесть свой звук из колонок,
                        # включая голос Джарвиса → нет самоподхвата).
                        ext = max(0.0, rms - config.NOISE_SUBTRACT_K * self._monitor_rms)
                        if not self._speaking:
                            # EMA только пока Джарвис молчит — чистый внешний фон без артефактов речи.
                            self._noise = 0.8 * self._noise + 0.2 * ext
            except Exception as exc:
                _log.debug("Сбой замерщика микрофона (%s) — переоткрою", exc)
                if self._stop.wait(1.0):
                    return

    def _monitor_loop(self) -> None:
        # ВАЖНО: `pw-cat --record` по ИМЕНИ monitor зависает (сверено) — используем ID
        # (находим через pactl). Держим поток открытым: старт ~0.2с один раз, дальше течёт.
        block_bytes = max(320, int(config.NOISE_WINDOW * _SR)) * 2  # s16 = 2 байта/семпл
        while not self._stop.is_set():
            proc = None
            try:
                mon_id = self._monitor_id()
                if not mon_id:
                    if self._stop.wait(2.0):
                        return
                    continue
                proc = subprocess.Popen(
                    ["pw-cat", "--record", "--target", mon_id, "--rate", str(_SR),
                     "--channels", "1", "--format", "s16", "-"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
                while not self._stop.is_set():
                    buf = proc.stdout.read(block_bytes)
                    if not buf:
                        break
                    d = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
                    self._monitor_rms = float(np.sqrt(np.mean(np.square(d)) + 1e-12))
            except Exception as exc:
                _log.debug("Сбой замерщика воспроизведения (%s) — переоткрою", exc)
            finally:
                self._monitor_rms = 0.0
                if proc is not None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=0.5)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
            if self._stop.wait(1.0):
                return

    @staticmethod
    def _monitor_id() -> str | None:
        """ID monitor-source текущего default-вывода (через pactl). ID динамичен — берём каждый раз."""
        try:
            out = subprocess.run(["pactl", "list", "short", "sources"],
                                 capture_output=True, text=True, timeout=2).stdout
            for line in out.splitlines():
                if ".monitor" in line:
                    return line.split("\t")[0]
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------ #
    # Доступ к уровням (мгновенно — из rolling-замерщиков)
    # ------------------------------------------------------------------ #
    def playback_rms(self) -> float:
        return self._monitor_rms

    def external_noise(self) -> float:
        return self._noise

    # ------------------------------------------------------------------ #
    # Расчёт громкости голоса
    # ------------------------------------------------------------------ #
    def target_voice(self, user_level: float | None = None) -> tuple[float, float]:
        """Целевая (громкость pw-cat 0..1, gain≥1). В шуме — за шумом (разборчивость), в тишине —
        за громкостью речи пользователя (вплоть до шёпота). gain>1 — программное усиление при
        сильном шуме (если выше потолка громкости)."""
        try:
            noise = self._noise
            vmin, vbase, vmax = (config.VOICE_VOLUME_MIN, config.VOICE_VOLUME_BASE,
                                 config.VOICE_VOLUME_MAX)
            if noise > config.QUIET_THRESHOLD:
                # Шумно — громкость диктуется необходимостью перекрыть шум.
                vol = vbase + config.NOISE_TO_VOLUME * (noise - config.QUIET_THRESHOLD)
                gain = 1.0
                if vol > vmax:
                    gain = min(config.VOICE_GAIN_MAX, 1.0 + (vol - vmax))
                    vol = vmax
                vol = max(vbase, min(vmax, vol))
            else:
                # Тихо вокруг — следуем за громкостью речи пользователя (тихо → тихо/шёпот).
                if user_level is not None and user_level > 0:
                    vol = vmin + config.USER_TO_VOLUME * user_level
                else:
                    vol = vbase
                gain = 1.0
                vol = max(vmin, min(vbase, vol))  # в тишине не громче базы
            return round(vol, 3), round(gain, 3)
        except Exception as exc:
            _log.debug("Расчёт громкости сбоил (%s) — база", exc)
            return config.VOICE_VOLUME_BASE, 1.0

    @staticmethod
    def apply_gain(pcm: bytes, gain: float) -> bytes:
        """Программно усилить PCM (s16) на gain>1 с защитой от переполнения (clip)."""
        if gain <= 1.0:
            return pcm
        try:
            a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) * gain
            np.clip(a, -32768, 32767, out=a)
            return a.astype(np.int16).tobytes()
        except Exception:
            return pcm

    # ------------------------------------------------------------------ #
    # Ducking музыки ноута (pactl, плавно)
    # ------------------------------------------------------------------ #
    def should_duck(self) -> bool:
        """Реальный уровень воспроизведения выше порога → есть что приглушать."""
        return self._monitor_rms > config.DUCK_THRESHOLD

    def duck(self) -> None:
        """Частично приглушить музыку ноута (не в ноль), плавно. Своё pw-cat не трогаем."""
        try:
            inputs = self._music_inputs()
            if not inputs:
                return
            with self._lock:
                for sid, vol in inputs.items():
                    self._ducked.setdefault(sid, vol)
            self._ramp({sid: (vol, config.DUCK_LEVEL) for sid, vol in inputs.items()})
        except Exception as exc:
            _log.debug("ducking не удался (%s)", exc)

    def restore(self) -> None:
        """Плавно вернуть приглушённую музыку к исходной громкости."""
        try:
            with self._lock:
                saved = dict(self._ducked)
                self._ducked.clear()
            if saved:
                self._ramp({sid: (config.DUCK_LEVEL, vol) for sid, vol in saved.items()})
        except Exception as exc:
            _log.debug("restore не удался (%s)", exc)

    def _music_inputs(self) -> dict[str, float]:
        """sink-input id → текущая громкость (доля), КРОМЕ своего pw-cat (Джарвиса)."""
        res: dict[str, float] = {}
        try:
            out = subprocess.run(["pactl", "list", "sink-inputs"],
                                 capture_output=True, text=True, timeout=2).stdout
            cur, binary, vol = None, "", 1.0

            def _flush():
                if cur is not None and "pw-cat" not in binary and "jarvis" not in binary.lower():
                    res[cur] = vol

            for line in out.splitlines():
                s = line.strip()
                if s.startswith("Sink Input #"):
                    _flush()
                    cur, binary, vol = s.split("#", 1)[1].strip(), "", 1.0
                elif "application.process.binary" in s:
                    binary = s.split("=", 1)[-1].strip().strip('"')
                elif s.startswith("Volume:"):
                    m = re.search(r"(\d+)%", s)
                    if m:
                        vol = int(m.group(1)) / 100.0
            _flush()
        except Exception as exc:
            _log.debug("список sink-inputs не получен (%s)", exc)
        return res

    def _ramp(self, items: dict[str, tuple[float, float]]) -> None:
        """Плавно (несколько шагов) подвести громкость sink-inputs от a к b."""
        try:
            steps = 5
            for i in range(1, steps + 1):
                frac = i / steps
                for sid, (a, b) in items.items():
                    v = a + (b - a) * frac
                    subprocess.run(["pactl", "set-sink-input-volume", sid, f"{v:.3f}"],
                                   capture_output=True, timeout=1)
                time.sleep(config.NOISE_WINDOW * 0.25)
        except Exception as exc:
            _log.debug("рампа громкости не удалась (%s)", exc)
