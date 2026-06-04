"""«Голос» Джарвиса: синтез речи через Piper и воспроизведение.

Слушает jarvis/say, складывает фразы в очередь и проигрывает их по одной
(worker-поток), чтобы реплики не накладывались. На время воспроизведения
публикует state=speaking.
"""
import logging
import queue
import threading

import numpy as np
import sounddevice as sd

from jarvis import config, contracts
from jarvis.bus import JarvisModule

# Сколько фраз подряд должны не озвучиться, чтобы признать сбой стойким и крикнуть в
# лог CRITICAL (синтез/вывод звука недоступен). Воркер при этом продолжает пытаться —
# когда устройство вернётся, озвучка восстановится сама (без рестарта сервиса).
_TTS_FAILS_CRITICAL = 3


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
            self.log_exc(logging.ERROR,
                         "Не удалось загрузить голос Piper — озвучка будет недоступна")
            self._voice = None

    def on_say(self, payload: dict):
        text = (payload.get("text") or "").strip()
        if text:
            self._queue.put(text)

    def _run_worker(self):
        """Очередь озвучки: фразы по одной (не наложатся). Воркер НЕ должен умереть от
        единичного сбоя — иначе Джарвис онемеет молча. Любое исключение тела цикла
        ловим и продолжаем. Стойкий сбой (3+ фразы подряд) — CRITICAL в лог, но
        продолжаем пытаться: вернётся устройство — озвучка восстановится сама.
        """
        fails = 0
        announced_critical = False
        while not self._stop_event.is_set():
            try:
                try:
                    text = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if self._speak(text):
                    if announced_critical:
                        self.log.info("Озвучка восстановлена — звук снова работает")
                    fails = 0
                    announced_critical = False
                else:
                    fails += 1
                    if fails >= _TTS_FAILS_CRITICAL and not announced_critical:
                        self.log.critical(
                            "Озвучка не работает уже %d фраз подряд (синтез или вывод "
                            "звука) — продолжаю пытаться, но голос сейчас недоступен", fails,
                        )
                        announced_critical = True
            except Exception:
                self.log_exc(logging.WARNING, "Сбой в цикле озвучки — продолжаю работу")

    def _speak(self, text: str) -> bool:
        """Озвучить фразу. True — успех; False — пропустили (нет голоса/сбой синтеза/вывода).

        Один плохой кусок не валит сервис: ошибку ловим, логируем по-человечески и
        возвращаем False (воркер сам решит, стойкий ли это сбой). «Один speaking на
        ответ» сохранён: speaking на входе, idle в finally.
        """
        if self._voice is None:
            self.log.warning("Голос Piper не загружен — не могу озвучить фразу: %r", text[:60])
            return False
        try:
            self.set_state(contracts.STATE_SPEAKING)
            samples, sample_rate = self._synthesize(text)
            sd.play(samples, samplerate=sample_rate)
            sd.wait()
            return True
        except Exception:
            self.log_exc(logging.WARNING,
                         "Не удалось синтезировать/воспроизвести фразу — пропускаю: %r",
                         text[:60])
            return False
        finally:
            self.set_state(contracts.STATE_IDLE)

    def on_stop(self):
        """Прерываем воспроизведение и ждём воркер ДО выхода интерпретатора.

        Иначе резкое убийство потока с активным sd.play даёт assertion
        PulseAudio и core dump при Ctrl+C / systemctl stop.
        """
        try:
            sd.stop()  # прерываем текущее воспроизведение (разблокирует sd.wait)
        except Exception:
            self.log_exc(logging.ERROR, "Ошибка остановки воспроизведения")
        try:
            if self._worker is not None:
                self._worker.join(timeout=2.0)
        except Exception:
            self.log_exc(logging.ERROR, "Ошибка ожидания воспроизводящего потока")

    def _synthesize(self, text: str):
        """Синтез фразы -> (numpy int16, sample_rate).

        piper-tts 1.4.x: synthesize() возвращает итератор AudioChunk
        (по одному на предложение). Собираем int16-PCM из всех чанков,
        частоту берём из самого чанка — она достовернее, чем из конфига.
        """
        pcm = bytearray()
        sample_rate = self._voice.config.sample_rate  # запасное значение
        for chunk in self._voice.synthesize(text):
            pcm += chunk.audio_int16_bytes
            sample_rate = chunk.sample_rate
        samples = np.frombuffer(bytes(pcm), dtype=np.int16)
        return samples, sample_rate


def main():
    TtsModule().run()


if __name__ == "__main__":
    main()
