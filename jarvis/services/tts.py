"""«Голос» Джарвиса: синтез речи через Piper и воспроизведение.

Слушает jarvis/say, складывает фразы в очередь и проигрывает их по одной
(worker-поток), чтобы реплики не накладывались. На время воспроизведения
публикует state=speaking.
"""
import queue
import threading

import numpy as np
import sounddevice as sd

from jarvis import config, contracts
from jarvis.bus import JarvisModule


class TtsModule(JarvisModule):
    """«Голос»: озвучивает фразы из jarvis/say через Piper."""

    def __init__(self):
        super().__init__("jarvis-tts")
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._voice = None
        self._worker = None

    def on_start(self):
        self._load_voice()
        self.subscribe(contracts.TOPIC_SAY, self.on_say)
        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._worker.start()

    def _load_voice(self):
        # ВНИМАНИЕ: сверить API piper с установленной версией (пакет piper-tts).
        try:
            from piper import PiperVoice

            self._voice = PiperVoice.load(
                config.PIPER_MODEL, config_path=config.PIPER_CONFIG
            )
            self.log.info("Голос Piper загружен: %s", config.PIPER_MODEL)
        except Exception:
            self.log.exception("Не удалось загрузить голос Piper")
            self._voice = None

    def on_say(self, payload: dict):
        text = (payload.get("text") or "").strip()
        if text:
            self._queue.put(text)

    def _run_worker(self):
        while not self._stop_event.is_set():
            try:
                text = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._speak(text)

    def _speak(self, text: str):
        if self._voice is None:
            self.log.error("Голос не загружен, пропускаю фразу: %s", text)
            return
        try:
            self.set_state(contracts.STATE_SPEAKING)
            samples, sample_rate = self._synthesize(text)
            sd.play(samples, samplerate=sample_rate)
            sd.wait()
        except Exception:
            self.log.exception("Ошибка синтеза/воспроизведения")
        finally:
            self.set_state(contracts.STATE_IDLE)

    def _synthesize(self, text: str):
        """Синтез фразы -> (numpy int16, sample_rate).

        ВНИМАНИЕ: API Piper менялся между версиями — сверить с установленной.
        Здесь читаем потоковый сырой PCM (int16, моно) и собираем в массив.
        """
        sample_rate = self._voice.config.sample_rate
        pcm = bytearray()
        for chunk in self._voice.synthesize_stream_raw(text):
            pcm.extend(chunk)
        samples = np.frombuffer(bytes(pcm), dtype=np.int16)
        return samples, sample_rate


def main():
    TtsModule().run()


if __name__ == "__main__":
    main()
