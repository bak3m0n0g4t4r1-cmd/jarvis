"""«Уши» Джарвиса: захват микрофона, VAD, распознавание и wake-word.

Конвейер: sounddevice (16 кГц моно) -> silero-VAD (sherpa-onnx) -> сегмент
речи -> zipformer-ru (offline transducer) -> если фраза начинается с
wake-word «джарвис», остаток публикуется в jarvis/input.

Детекция wake-word вынесена в _match_wake_word() — заменяема на openWakeWord.
"""
import difflib
import re
import threading
import time

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
        self._stream = None
        # Анти-эхо: пока Джарвис говорит — вход заглушён, плюс «хвост» после.
        self._speaking = False
        self._resume_at = 0.0  # время (monotonic), до которого вход ещё заглушён
        # При возобновлении слушания нужен один чистый сброс VAD (см. _listen_loop).
        self._need_vad_reset = True
        # Контент-фильтр эха: последняя фраза Джарвиса и срок действия фильтра.
        self._last_say_text = ""
        self._echo_until = 0.0  # время (monotonic), до которого сверяем с эхом

    def on_start(self):
        self._init_engines()
        # Слушаем состояние шины, чтобы не распознавать собственный голос из колонок.
        self.subscribe(contracts.TOPIC_STATE, self.on_state)
        # Слушаем реплики Джарвиса — для отсева эха по содержанию (страховка).
        self.subscribe(contracts.TOPIC_SAY, self.on_say)
        self._audio_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._audio_thread.start()

    def _init_engines(self):
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

            # zipformer-ru — offline transducer с BPE-словарём.
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=config.ZIPFORMER_ENCODER,
                decoder=config.ZIPFORMER_DECODER,
                joiner=config.ZIPFORMER_JOINER,
                tokens=config.ZIPFORMER_TOKENS,
                modeling_unit="bpe",
                bpe_vocab=config.ZIPFORMER_BPE,
            )
            self.log.info("Модели STT инициализированы (zipformer-ru)")
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
            # Поток держим как атрибут, чтобы on_stop() закрыл его штатно
            # (контекст-менеджер не использует, чтобы избежать гонки при shutdown).
            self._stream = sd.InputStream(
                channels=config.CHANNELS,
                samplerate=config.SAMPLE_RATE,
                dtype="float32",
            )
            self._stream.start()
            self.set_state(contracts.STATE_LISTENING)
            self.log.info("Слушаю микрофон...")
            while not self._stop_event.is_set():
                # Вход читаем всегда (чтобы не переполнить буфер устройства),
                # но пока Джарвис говорит/идёт хвост — VAD не кормим, иначе
                # словим эхо из колонок и зациклимся.
                data, _ = self._stream.read(window)
                if self._is_muted():
                    # Помечаем, что при возобновлении нужен чистый сброс — чтобы
                    # частичный сегмент, накопленный на границе, не склеился
                    # со следующей фразой пользователя.
                    self._need_vad_reset = True
                    continue
                # Возобновились — сбрасываем VAD ОДИН раз на границе muted→активен.
                if self._need_vad_reset:
                    self._vad.reset()
                    self._need_vad_reset = False
                    self.log.debug("VAD сброшен при возобновлении слушания")
                samples = np.asarray(data, dtype=np.float32).reshape(-1)
                self._vad.accept_waveform(samples)
                while not self._vad.empty():
                    segment = np.asarray(self._vad.front.samples, dtype=np.float32)
                    self._vad.pop()
                    self._transcribe(segment)
        except Exception:
            self.log.exception("Сбой аудио-потока")

    def _is_muted(self) -> bool:
        """True, пока Джарвис говорит или ещё не истёк «хвост» после речи."""
        if self._speaking:
            return True
        return self._resume_at and time.monotonic() < self._resume_at

    def on_state(self, payload: dict):
        """Реакция на jarvis/state: глушим вход на время речи Джарвиса."""
        state = payload.get("state")
        if state == contracts.STATE_SPEAKING:
            if not self._speaking:
                self._speaking = True
                self.log.debug("STT пауза: state=speaking")
        elif self._speaking:
            # Речь закончилась — возобновляем не сразу, а после «хвоста».
            self._speaking = False
            self._resume_at = time.monotonic() + config.SPEAKING_TAIL
            # Контент-фильтр действует ещё ECHO_CONTENT_WINDOW секунд после хвоста:
            # якорим окно от конца речи, т.к. эхо распознаётся только после мьюта.
            self._echo_until = self._resume_at + config.ECHO_CONTENT_WINDOW
            self.log.debug("STT возобновление через %.2f с (хвост)", config.SPEAKING_TAIL)

    def on_say(self, payload: dict):
        """Запоминаем последнюю фразу Джарвиса — для отсева эха по содержанию."""
        text = (payload.get("text") or "").strip()
        if text:
            self._last_say_text = text  # source не важен — подойдёт любая say

    def _transcribe(self, samples: np.ndarray):
        # Слишком короткий сегмент — zipformer падает с RuntimeError на Reshape.
        # Фильтруем здесь, а не в VAD-порогах, потому что после паузы speaking
        # хвост может дать обрывок меньше минимальной длины.
        if len(samples) < config.MIN_SEGMENT_SAMPLES:
            self.log.debug("Пропуск короткого сегмента (%d семплов)", len(samples))
            return
        try:
            stream = self._recognizer.create_stream()
            stream.accept_waveform(config.SAMPLE_RATE, samples)
            # decode_stream — самая хрупкая часть: падает на некорректной форме
            # входа. Ловим отдельно, чтобы единичный сбой не сломал конвейер.
            try:
                self._recognizer.decode_stream(stream)
            except RuntimeError:
                self.log.exception("Сбой декодирования (некорректный сегмент)")
                return
            text = stream.result.text.strip()
        except Exception:
            self.log.exception("Ошибка распознавания")
            return
        if not text:
            return
        # Страховка от эхо-петли: если в окне после речи распознали почти то же,
        # что Джарвис только что произнёс — это эхо из колонок, не команда.
        if self._is_echo(text):
            return
        self.log.info("Распознано: %s", text)
        command = self._match_wake_word(text)
        if command:
            self.publish_json(
                contracts.TOPIC_INPUT, {"text": command}, qos=contracts.QOS_INPUT
            )
            self.log.info("Команда в шину: %s", command)

    @staticmethod
    def _normalize(text: str) -> str:
        """Нормализация для сравнения: убрать пунктуацию, регистр, края."""
        return re.sub(r"[^\w\s]", "", text, flags=re.UNICODE).strip().lower()

    def _is_echo(self, text: str) -> bool:
        """True, если распознанное похоже на недавнюю реплику Джарвиса (эхо).

        Действует только в окне ECHO_CONTENT_WINDOW после конца речи. Сравнение
        нормализованных строк через difflib; порог — ECHO_SIMILARITY_THRESHOLD.
        """
        if not self._last_say_text or time.monotonic() >= self._echo_until:
            return False
        a, b = self._normalize(text), self._normalize(self._last_say_text)
        if not a or not b:
            return False
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        if ratio >= config.ECHO_SIMILARITY_THRESHOLD:
            self.log.debug("Отброшено как эхо (ratio=%.2f): %r ~ %r", ratio, a, b)
            return True
        return False

    def _match_wake_word(self, text: str):
        """Возвращает текст команды после wake-word, иначе None.

        Заменяемо на openWakeWord: достаточно переопределить этот метод.
        Сначала точное совпадение по config.WAKE_WORDS, затем нечёткое
        (difflib) по первому слову — покрывает искажения маленькой модели.
        """
        normalized = self._normalize(text)
        if not normalized:
            return None

        # Точное совпадение: фраза начинается с одного из вариантов.
        for wake in config.WAKE_WORDS:
            wake = wake.strip().lower()
            if normalized.startswith(wake):
                self.log.debug("Wake-word '%s' (точное совпадение)", wake)
                return normalized[len(wake):].strip()

        # Нечёткое совпадение по первому слову.
        first = normalized.split()[0]
        best_score, best_wake = 0.0, ""
        for wake in config.WAKE_WORDS:
            wake = wake.strip().lower()
            score = difflib.SequenceMatcher(None, first, wake).ratio()
            if score > best_score:
                best_score, best_wake = score, wake
        self.log.debug("Wake-word нечётко: '%s' ~ '%s' score=%.2f", first, best_wake, best_score)
        if best_score >= config.WAKE_WORD_FUZZY_THRESHOLD:
            return " ".join(normalized.split()[1:])

        return None

    def on_stop(self):
        """Корректно гасим аудио-поток ввода ДО выхода интерпретатора.

        Без этого PipeWire/Pulse роняет assertion и даёт core dump при
        резком убийстве потока (Ctrl+C / systemctl stop). Сначала ждём
        выхода потока прослушивания из read(), затем закрываем устройство.
        """
        try:
            if self._audio_thread is not None:
                self._audio_thread.join(timeout=2.0)
        except Exception:
            self.log.exception("Ошибка ожидания аудио-потока ввода")
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
                self.log.info("Аудио-поток ввода закрыт")
        except Exception:
            self.log.exception("Ошибка закрытия аудио-потока ввода")


def main():
    SttModule().run()


if __name__ == "__main__":
    main()
