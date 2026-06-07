"""«Голос» Джарвиса: синтез речи через Piper и воспроизведение через PipeWire.

Слушает jarvis/say, складывает фразы в очередь и проигрывает их по одной
(worker-поток), чтобы реплики не накладывались. На время воспроизведения
публикует state=speaking.

ВОСПРОИЗВЕДЕНИЕ — через PipeWire (`pw-cat`), НЕ sounddevice/PortAudio. На TUXEDO
PortAudio видит только сырые ALSA-выходы (HDMI) и не отдаёт звук в аналоговый
вывод/PipeWire → Джарвис был нем (paInvalidSampleRate на 22050 Гц, исключение
глоталось). pw-cat идёт в системный default sink с авто-ресемплингом.
"""
import logging
import queue
import subprocess
import threading

from jarvis import config, contracts
from jarvis.bus import JarvisModule

# Сколько фраз подряд должны не озвучиться, чтобы признать сбой стойким и крикнуть в
# лог CRITICAL (синтез/вывод звука недоступен). Воркер при этом продолжает пытаться —
# когда устройство вернётся, озвучка восстановится сама (без рестарта сервиса).
_TTS_FAILS_CRITICAL = 3


class TtsModule(JarvisModule):
    """«Голос»: озвучивает фразы из jarvis/say через Piper + PipeWire (pw-cat)."""

    def __init__(self):
        super().__init__("jarvis-tts")
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._voice = None
        self._voice_tried = False  # ленивую загрузку пробуем один раз
        self._worker = None
        # Текущий процесс воспроизведения (pw-cat) — чтобы on_stop мог его прервать.
        self._play_proc: subprocess.Popen | None = None

    def on_start(self):
        # Голос Piper грузим ЛЕНИВО (при первой фразе), а не на старте — пока Джарвис
        # молчит, ~120 МБ модели не висят в RAM. Загрузка происходит в _speak.
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

        Один плохой кусок не валит сервис: ошибку ловим, логируем по-человечески (с СУТЬю
        ошибки — проглоченное исключение тут и было причиной немоты) и возвращаем False.
        «Один speaking на ответ» сохранён: speaking на входе, idle в finally; speaking
        держится РЕАЛЬНО пока играет звук (pw-cat дожидается) — корректный тайминг анти-эхо.
        """
        # Ленивая загрузка голоса при первой фразе (одна попытка). Если не вышло —
        # дальше просто пропускаем озвучку, не пытаясь грузить каждый раз.
        if self._voice is None and not self._voice_tried:
            self._voice_tried = True
            self._load_voice()
        if self._voice is None:
            self.log.warning("Голос Piper не загружен — не могу озвучить фразу: %r", text[:60])
            return False
        try:
            self.set_state(contracts.STATE_SPEAKING)
            pcm, sample_rate = self._synthesize(text)
            self._play_pcm(pcm, sample_rate)
            return True
        except Exception as exc:
            # СУТЬ ошибки — прямо в WARNING-строку (видно при LOG_LEVEL=INFO), трасса на DEBUG.
            # Так будущая немота читается из лога сразу, а не теряется в stderr ALSA/PipeWire.
            self.log.warning(
                "Не удалось синтезировать/воспроизвести фразу — пропускаю: %r (%s: %s)",
                text[:60], type(exc).__name__, exc,
            )
            self.log.debug("Трасса сбоя озвучки", exc_info=True)
            return False
        finally:
            self.set_state(contracts.STATE_IDLE)

    def _play_pcm(self, pcm: bytes, sample_rate: int) -> None:
        """Воспроизвести PCM (signed-16 mono) через PipeWire: pw-cat → default sink.

        Почему не sounddevice: на TUXEDO PortAudio видит лишь сырые ALSA-выходы (HDMI),
        аналоговый вывод/PipeWire ему недоступен → была немота. pw-cat отдаёт звук в
        системный default sink (или JARVIS_TTS_SINK) с авто-ресемплингом частоты.
        Процесс держим в self._play_proc, чтобы on_stop мог прервать воспроизведение.
        """
        cmd = ["pw-cat", "-p", "--raw", "--rate", str(sample_rate),
               "--channels", "1", "--format", "s16", "-"]
        if config.TTS_SINK:
            cmd += ["--target", config.TTS_SINK]
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self._play_proc = proc
        try:
            _, err = proc.communicate(pcm)
        finally:
            self._play_proc = None
        if proc.returncode != 0:
            detail = err.decode("utf-8", "replace").strip() if err else ""
            raise RuntimeError(
                f"pw-cat вернул код {proc.returncode}: {detail or 'без stderr'}"
            )

    def on_stop(self):
        """Прерываем воспроизведение и ждём воркер ДО выхода интерпретатора.

        Иначе резкое убийство потока с активным pw-cat оставляет осиротевший процесс
        воспроизведения. Сначала прерываем текущий pw-cat (разблокирует communicate),
        затем ждём выхода воркера.
        """
        proc = self._play_proc
        if proc is not None:
            try:
                proc.terminate()  # прерываем текущее воспроизведение
            except Exception:
                self.log.debug("Не удалось прервать pw-cat", exc_info=True)
        try:
            if self._worker is not None:
                self._worker.join(timeout=2.0)
        except Exception:
            self.log_exc(logging.ERROR, "Ошибка ожидания воспроизводящего потока")

    def _synthesize(self, text: str) -> tuple[bytes, int]:
        """Синтез фразы -> (PCM signed-16 mono, sample_rate).

        piper-tts 1.4.x: synthesize() возвращает итератор AudioChunk (по одному на
        предложение). Собираем int16-PCM из всех чанков; частоту берём из самого
        чанка — она достовернее, чем из конфига.
        """
        pcm = bytearray()
        sample_rate = self._voice.config.sample_rate  # запасное значение
        for chunk in self._voice.synthesize(text):
            pcm += chunk.audio_int16_bytes
            sample_rate = chunk.sample_rate
        return bytes(pcm), sample_rate


def main():
    TtsModule().run()


if __name__ == "__main__":
    main()
