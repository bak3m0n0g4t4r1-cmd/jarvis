"""«Голос» Джарвиса: синтез Piper + воспроизведение через PipeWire с адаптивной громкостью.

Слушает jarvis/say, проигрывает фразы по одной (worker-поток). На время речи — state=speaking.

ВОСПРОИЗВЕДЕНИЕ — через PipeWire (`pw-cat`), НЕ sounddevice (PortAudio видит только HDMI →
была немота). АДАПТИВНАЯ ГРОМКОСТЬ (audio_env): меряем реальные уровни сигнала (микрофон +
monitor воспроизведения), приглушаем музыку (ducking) и подстраиваем громкость голоса под
внешний шум И громкость речи пользователя. Синтез ПОТОКОВЫЙ — играем чанки по мере готовности.
"""
import logging
import queue
import subprocess
import threading

from jarvis import config, contracts
from jarvis.audio_env import AudioEnv
from jarvis.bus import JarvisModule

# Сколько фраз подряд не озвучилось, чтобы крикнуть CRITICAL (синтез/вывод недоступен).
_TTS_FAILS_CRITICAL = 3


class TtsModule(JarvisModule):
    """«Голос»: озвучивает фразы из jarvis/say через Piper + PipeWire с адаптивной громкостью."""

    def __init__(self):
        super().__init__("jarvis-tts")
        # Очередь хранит (текст, громкость_речи_пользователя, нижний_предел_громкости).
        # Уровень — от STT для адаптации; нижний предел — для будильника (обход «тихо→тихо»).
        self._queue: "queue.Queue[tuple[str, float | None, float | None]]" = queue.Queue()
        self._voice = None
        self._voice_tried = False
        self._voice_lock = threading.Lock()
        self._worker = None
        self._play_proc: subprocess.Popen | None = None
        self._env = AudioEnv()  # замер обстановки + ducking + расчёт громкости

    def on_start(self):
        self.subscribe(contracts.TOPIC_SAY, self.on_say)
        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._worker.start()
        # Постоянный замер звуковой обстановки (микрофон + monitor воспроизведения).
        try:
            self._env.start()
        except Exception:
            self.log_exc(logging.WARNING, "Не удалось запустить замер обстановки — громкость фиксированная")
        # Прогрев Piper в фоне при старте: первая фраза не ждёт ~3с загрузки (ценой ~106 МБ RAM).
        if config.TTS_PRELOAD:
            threading.Thread(target=self._ensure_voice, daemon=True, name="piper-preload").start()

    def _ensure_voice(self):
        """Загрузить голос Piper один раз (потокобезопасно). Используется и прогревом, и _speak."""
        if self._voice is not None or self._voice_tried:
            return
        with self._voice_lock:
            if self._voice is not None or self._voice_tried:
                return
            self._voice_tried = True
            try:
                from piper import PiperVoice

                self._voice = PiperVoice.load(config.PIPER_MODEL, config_path=config.PIPER_CONFIG)
                self.log.info("Голос Piper загружен: %s", config.PIPER_MODEL)
            except Exception:
                self.log_exc(logging.ERROR,
                             "Не удалось загрузить голос Piper — озвучка будет недоступна")
                self._voice = None

    def on_say(self, payload: dict):
        text = (payload.get("text") or "").strip()
        if text:
            # Уровень громкости речи пользователя (от STT через core) — для адаптации громкости.
            level = payload.get("user_level")
            # Нижний предел громкости (будильник): озвучить НЕ тише — обходит «тихо вокруг → тихо».
            mv = payload.get("min_volume")
            self._queue.put((
                text,
                level if isinstance(level, (int, float)) else None,
                float(mv) if isinstance(mv, (int, float)) else None,
            ))

    def _run_worker(self):
        """Очередь озвучки по одной фразе. Воркер не умирает от единичного сбоя; стойкий сбой
        (3+ фразы) → CRITICAL, но продолжаем пытаться (вернётся устройство — озвучка оживёт)."""
        fails = 0
        announced_critical = False
        while not self._stop_event.is_set():
            try:
                try:
                    text, level, min_volume = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if self._speak(text, level, min_volume):
                    if announced_critical:
                        self.log.info("Озвучка восстановлена — звук снова работает")
                    fails = 0
                    announced_critical = False
                else:
                    fails += 1
                    if fails >= _TTS_FAILS_CRITICAL and not announced_critical:
                        self.log.critical(
                            "Озвучка не работает уже %d фраз подряд (синтез или вывод звука) — "
                            "продолжаю пытаться, но голос сейчас недоступен", fails,
                        )
                        announced_critical = True
            except Exception:
                self.log_exc(logging.WARNING, "Сбой в цикле озвучки — продолжаю работу")

    def _speak(self, text: str, user_level: float | None, min_volume: float | None = None) -> bool:
        """Озвучить фразу с адаптивной громкостью. True — успех; False — пропуск (нет голоса/сбой).

        speaking держится РЕАЛЬНО пока играет звук (корректный тайминг анти-эхо). Перед речью:
        приглушаем музыку (ducking) если она реально звучит, и считаем громкость голоса под
        внешний шум + громкость речи пользователя. После речи — возвращаем музыку плавно.
        min_volume (будильник) — гарантированный нижний предел громкости (обход «тихо→тихо»).
        """
        self._ensure_voice()
        if self._voice is None:
            self.log.warning("Голос Piper не загружен — не могу озвучить фразу: %r", text[:60])
            return False
        ducked = False
        try:
            self.set_state(contracts.STATE_SPEAKING)
            self._env.set_speaking(True)
            # Ducking музыки ноута, если она реально звучит выше порога.
            if self._env.should_duck():
                self._env.duck()
                ducked = True
            volume, gain = self._env.target_voice(user_level)
            # Будильник: поднять громкость до нижнего предела, если адаптив дал тише (будит надёжно).
            if min_volume is not None and min_volume > 0:
                volume = max(volume, min(1.0, float(min_volume)))
            self.log.debug("Громкость голоса %.2f (gain %.2f), ducking=%s", volume, gain, ducked)
            self._synth_and_play(text, volume, gain)
            return True
        except Exception as exc:
            self.log.warning(
                "Не удалось синтезировать/воспроизвести фразу — пропускаю: %r (%s: %s)",
                text[:60], type(exc).__name__, exc,
            )
            self.log.debug("Трасса сбоя озвучки", exc_info=True)
            return False
        finally:
            self.set_state(contracts.STATE_IDLE)
            self._env.set_speaking(False)
            if ducked:
                self._env.restore()  # плавно вернуть музыку к исходной громкости

    def _synth_and_play(self, text: str, volume: float, gain: float) -> None:
        """ПОТОКОВЫЙ синтез+воспроизведение: чанки Piper пишем в stdin pw-cat по мере готовности
        (время до первого звука ↓). Громкость — pw-cat --volume; усиление >1 — gain PCM.

        Скорость и тон НЕЗАВИСИМЫ: темп задаёт length_scale Piper, тон — подмена частоты pw-cat
        (--rate = частота_модели·pitch). length_scale домножаем на pitch, чтобы компенсировать
        растяжение времени от ресэмплинга → net-темп = length_scale, net-тон = pitch. Pitch не
        формант-сохраняющий: для МАЛОГО сдвига звучит чисто, без доп. зависимостей и заметного CPU
        (pw-cat и так ресэмплит к частоте устройства)."""
        from piper import SynthesisConfig

        # Защита от мусора в settings.yaml: вне разумных границ → клиппинг (битое значение не должно
        # давать немоту/нулевую частоту). length_scale и pitch — положительные множители.
        pitch = min(2.0, max(0.5, float(config.VOICE_PITCH)))
        length = min(2.0, max(0.5, float(config.VOICE_LENGTH_SCALE)))
        model_rate = int(self._voice.config.sample_rate)
        # Подмена частоты воспроизведения сдвигает тон (ниже при pitch<1) и растягивает время в
        # 1/pitch раз; length_scale·pitch компенсирует растяжение → темп зависит ТОЛЬКО от length.
        rate = max(8000, round(model_rate * pitch))
        syn = SynthesisConfig(length_scale=length * pitch)
        cmd = ["pw-cat", "-p", "--raw", "--rate", str(rate), "--channels", "1", "--format", "s16",
               "--volume", f"{max(0.0, volume):.3f}", "--latency", f"{config.TTS_LATENCY_MS}ms", "-"]
        if config.TTS_SINK:
            cmd += ["--target", config.TTS_SINK]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self._play_proc = proc
        err = b""
        try:
            for chunk in self._voice.synthesize(text, syn_config=syn):
                pcm = chunk.audio_int16_bytes
                if gain > 1.0:
                    pcm = AudioEnv.apply_gain(pcm, gain)
                try:
                    proc.stdin.write(pcm)  # стримим по мере синтеза
                except BrokenPipeError:
                    break
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.wait()  # дождаться конца воспроизведения (НЕ communicate — stdin уже закрыт)
            try:
                err = proc.stderr.read() or b""
            except Exception:
                err = b""
        finally:
            self._play_proc = None
        if proc.returncode not in (0, None):
            detail = err.decode("utf-8", "replace").strip() if err else ""
            raise RuntimeError(f"pw-cat вернул код {proc.returncode}: {detail or 'без stderr'}")

    def on_stop(self):
        """Останавливаем замерщики, прерываем воспроизведение, ждём воркер ДО выхода."""
        try:
            self._env.stop()
        except Exception:
            self.log.debug("Ошибка остановки замерщиков обстановки", exc_info=True)
        proc = self._play_proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                self.log.debug("Не удалось прервать pw-cat", exc_info=True)
        try:
            if self._worker is not None:
                self._worker.join(timeout=2.0)
        except Exception:
            self.log_exc(logging.ERROR, "Ошибка ожидания воспроизводящего потока")


def main():
    TtsModule().run()


if __name__ == "__main__":
    main()
