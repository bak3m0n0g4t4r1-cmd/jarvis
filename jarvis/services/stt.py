"""«Уши» Джарвиса: захват микрофона, VAD, распознавание и wake-word.

Конвейер: sounddevice (16 кГц моно) -> silero-VAD (sherpa-onnx) -> сегмент
речи -> SenseVoice-Small -> если фраза начинается с wake-word «джарвис»,
остаток публикуется в jarvis/input. Иначе сегмент игнорируется.

Детекция wake-word вынесена в отдельный метод _match_wake_word(), чтобы её
можно было заменить на openWakeWord, не трогая остальной модуль.
"""
import re
import threading

import numpy as np
import sounddevice as sd

from jarvis import config, contracts
from jarvis.bus import JarvisModule


class SttModule(JarvisModule):
    """«Уши»: слушает микрофон и публикует команды после wake-word."""

    def __init__(self):
        super().__init__("jarvis-stt")
        self._vad = None
        self._recognizer = None
        self._audio_thread = None

    def on_start(self):
        self._init_engines()
        self._audio_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._audio_thread.start()

    def _init_engines(self):
        # ВНИМАНИЕ: API sherpa-onnx менялся между версиями — сверить на машине.
        try:
            import sherpa_onnx

            vad_config = sherpa_onnx.VadModelConfig()
            vad_config.silero_vad.model = config.VAD_MODEL
            vad_config.silero_vad.threshold = config.VAD_THRESHOLD
            vad_config.silero_vad.min_silence_duration = config.VAD_MIN_SILENCE
            vad_config.silero_vad.min_speech_duration = config.VAD_MIN_SPEECH
            vad_config.sample_rate = config.SAMPLE_RATE
            self._vad = sherpa_onnx.VoiceActivityDetector(
                vad_config, buffer_size_in_seconds=30
            )

            self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=config.SENSEVOICE_MODEL,
                tokens=config.SENSEVOICE_TOKENS,
                use_itn=True,
            )
            self.log.info("Модели STT инициализированы")
        except Exception:
            self.log.exception("Не удалось инициализировать модели STT")
            self._vad = None
            self._recognizer = None

    def _listen_loop(self):
        if self._vad is None or self._recognizer is None:
            self.log.error("STT не инициализирован, прослушивание невозможно")
            return
        window = int(0.1 * config.SAMPLE_RATE)  # блоки по 100 мс
        try:
            with sd.InputStream(
                channels=config.CHANNELS,
                samplerate=config.SAMPLE_RATE,
                dtype="float32",
            ) as stream:
                self.set_state(contracts.STATE_LISTENING)
                self.log.info("Слушаю микрофон...")
                while not self._stop_event.is_set():
                    data, _ = stream.read(window)
                    samples = np.asarray(data, dtype=np.float32).reshape(-1)
                    self._vad.accept_waveform(samples)
                    while not self._vad.empty():
                        segment = np.asarray(self._vad.front.samples, dtype=np.float32)
                        self._vad.pop()
                        self._transcribe(segment)
        except Exception:
            self.log.exception("Сбой аудио-потока")

    def _transcribe(self, samples: np.ndarray):
        try:
            stream = self._recognizer.create_stream()
            stream.accept_waveform(config.SAMPLE_RATE, samples)
            self._recognizer.decode_stream(stream)
            text = stream.result.text.strip()
        except Exception:
            self.log.exception("Ошибка распознавания")
            return
        if not text:
            return
        self.log.info("Распознано: %s", text)
        command = self._match_wake_word(text)
        if command:
            self.publish_json(
                contracts.TOPIC_INPUT, {"text": command}, qos=contracts.QOS_INPUT
            )
            self.log.info("Команда в шину: %s", command)

    def _match_wake_word(self, text: str):
        """Возвращает текст после wake-word, иначе None.

        Заменяемо на openWakeWord: достаточно переопределить этот метод.
        Нормализуем регистр и убираем пунктуацию для устойчивого совпадения.
        """
        normalized = re.sub(r"[^\w\s]", "", text, flags=re.UNICODE).strip().lower()
        wake = config.WAKE_WORD.lower()
        if normalized.startswith(wake):
            return normalized[len(wake):].strip()
        return None


def main():
    SttModule().run()


if __name__ == "__main__":
    main()
