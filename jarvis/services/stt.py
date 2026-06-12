"""«Уши» Джарвиса: захват микрофона, VAD, распознавание и wake-word.

Конвейер: sounddevice (16 кГц моно) -> silero-VAD (sherpa-onnx) -> сегмент
речи -> zipformer-ru (offline transducer) -> если фраза начинается с
wake-word «джарвис», остаток публикуется в jarvis/input.

Детекция wake-word вынесена в _match_wake_word() — заменяема на openWakeWord.
"""
import difflib
import logging
import os
import re
import select
import struct
import threading
import time

import numpy as np
import sounddevice as sd

from jarvis import config, contracts
from jarvis.audio_env import AudioEnv
from jarvis.bus import JarvisModule

# Формат события /dev/input (struct input_event): timeval(2 long) + type(H) + code(H) + value(i) = 24 байта.
_EVENT_FMT = "llHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)
_EV_KEY = 1  # тип события — клавиша
# Push-to-talk: не копим бесконечно (защита от зажатой/залипшей кнопки), потолок секунд.
_PTT_MAX_SECONDS = 30
_PTT_RESCAN_SECONDS = 60   # период переоткрытия клавиатур (подхват новых/выдернутых устройств)
_PROC_DEVICES = "/proc/bus/input/devices"  # карта устройств ввода (поиск клавиатур для PTT)

# Самовосстановление аудио-входа: сколько раз подряд пытаться переоткрыть микрофон,
# прежде чем признать сбой фатальным и сигналить systemd о рестарте. Между попытками —
# линейный backoff (база × номер попытки, но не выше потолка).
_AUDIO_MAX_RETRIES = 5
_AUDIO_RETRY_BASE = 1.0   # секунды
_AUDIO_RETRY_MAX = 10.0   # секунды

# Адаптивный порог VAD: не пересобираем детектор чаще, чем раз в столько секунд (анти-дёрганье).
_VAD_SWITCH_MIN_INTERVAL = 10.0


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
        self._near_wake_log_at = 0.0  # rate-limit INFO о фразах «похоже на обращение, но ниже порога»
        # Адаптивный порог VAD: в тишине чуть снижаем (ловим тихую речь), в шуме — закреплённая
        # база. Шумовой пол оцениваем по собственному потоку захвата (без второго аудиострима).
        self._noise_floor = config.QUIET_THRESHOLD  # старт на границе → до замера тишины держим базу
        self._vad_threshold = config.VAD_THRESHOLD  # текущий эффективный порог VAD
        self._last_vad_switch = 0.0                 # rate-limit пересборки VAD (анти-дёрганье)
        # Перебивание речи голосом (barge-in): ОТДЕЛЬНЫЙ VAD, питается ТОЛЬКО пока Джарвис говорит
        # (основной конвейер в это время молчит — анти-эхо). Строится лениво при первом перебивании.
        self._barge_vad = None
        # Push-to-talk: пока кнопка зажата — копим аудио (без wake-word). Флаг пишет поток-читатель
        # клавиатуры, буфер ведёт ТОЛЬКО поток захвата (нет гонки). Ducking — через AudioEnv (pactl).
        self._ptt = False
        self._ptt_buf: list[np.ndarray] = []
        self._ptt_thread = None
        self._env = AudioEnv()  # для duck/restore музыки на время прослушивания (без замерных потоков)

    def on_start(self):
        self._init_engines()
        # Слушаем состояние шины, чтобы не распознавать собственный голос из колонок.
        self.subscribe(contracts.TOPIC_STATE, self.on_state)
        # Слушаем реплики Джарвиса — для отсева эха по содержанию (страховка).
        self.subscribe(contracts.TOPIC_SAY, self.on_say)
        self._audio_thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._audio_thread.start()
        # Push-to-talk: отдельный поток-читатель клавиатуры (/dev/input). Опционально.
        if config.PTT_ENABLED:
            self._ptt_thread = threading.Thread(target=self._ptt_loop, daemon=True, name="stt-ptt")
            self._ptt_thread.start()

    def _build_vad(self, threshold: float):
        """Построить детектор голоса (silero-VAD) с заданным порогом.

        Вынесено отдельно, чтобы адаптивно ПЕРЕСОБИРАТЬ VAD под обстановку: порог задаётся при
        конструировании (живой правки порога в sherpa-onnx нет), а silero крошечный — пересборка
        дешёвая и редкая (только при смене полосы тишина/шум). Прочие пороги (тишина/речь/буфер) —
        ЗАКРЕПЛЁННЫЕ из config, не трогаем."""
        import sherpa_onnx

        vad_config = sherpa_onnx.VadModelConfig()
        vad_config.silero_vad.model = config.VAD_MODEL
        vad_config.silero_vad.threshold = threshold
        vad_config.silero_vad.min_silence_duration = config.VAD_MIN_SILENCE
        vad_config.silero_vad.min_speech_duration = config.VAD_MIN_SPEECH
        vad_config.sample_rate = config.SAMPLE_RATE
        # Один поток на N100 (сверено: VadModelConfig.num_threads поддержан).
        vad_config.num_threads = config.STT_NUM_THREADS
        # Буфер VAD короче (команды короткие) — меньше RAM, чем прежние 30с.
        return sherpa_onnx.VoiceActivityDetector(
            vad_config, buffer_size_in_seconds=config.VAD_BUFFER_SECONDS
        )

    def _init_engines(self):
        try:
            import sherpa_onnx

            self._vad = self._build_vad(self._vad_threshold)

            # ASR по пресету (models.asr_preset): пути/тип готовит config. num_threads=1 —
            # меньше потоковой возни на маленьких моделях (сверено профилем, Этап 14).
            if not config.ASR_PRESET_KNOWN:
                self.log.warning("Неизвестный ASR-пресет «%s» — использую zipformer-small-ru",
                                 config.ASR_PRESET)
            p = config.ASR_PATHS
            if config.ASR_TYPE == "nemo_ctc":
                self._recognizer = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
                    model=p["model"], tokens=p["tokens"],
                    num_threads=config.STT_NUM_THREADS, sample_rate=config.SAMPLE_RATE)
            elif config.ASR_TYPE == "nemo_transducer":
                self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                    encoder=p["encoder"], decoder=p["decoder"], joiner=p["joiner"],
                    tokens=p["tokens"], model_type="nemo_transducer",
                    num_threads=config.STT_NUM_THREADS, sample_rate=config.SAMPLE_RATE)
            else:  # zipformer_bpe — offline transducer с BPE-словарём (small и полный)
                self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                    encoder=p["encoder"], decoder=p["decoder"], joiner=p["joiner"],
                    tokens=p["tokens"], modeling_unit="bpe", bpe_vocab=p["bpe"],
                    num_threads=config.STT_NUM_THREADS,
                )
            self.log.info("Модели STT инициализированы (ASR-пресет: %s)", config.ASR_PRESET)
        except Exception:
            self.log.exception("Не удалось инициализировать модели STT")
            self._vad = None
            self._recognizer = None

    def _listen_loop(self):
        """Внешний цикл захвата с САМОВОССТАНОВЛЕНИЕМ микрофона.

        Если устройство ввода пропало/дало ошибку (sounddevice) — не молчим и не умираем:
        закрываем поток, ждём (backoff) и переоткрываем. После _AUDIO_MAX_RETRIES неудач
        подряд признаём сбой фатальным и сигналим systemd о рестарте (request_restart),
        а не оставляем STT тихо оглохшим.
        """
        if self._vad is None or self._recognizer is None:
            self.request_restart("модели STT не инициализированы — слушать нечем")
            return
        # Блоки по 50 мс: готовый сегмент VAD замечается на ~25-50мс раньше, чем при 100 мс
        # (квантование цикла чтения). Пороги/min_silence VAD НЕ затронуты — меняется только
        # гранулярность опроса. CPU сверен на машине: рост в пределах погрешности (Этап 21).
        window = int(0.05 * config.SAMPLE_RATE)
        fails = 0
        while not self._stop_event.is_set():
            try:
                self._open_input_stream()
                fails = 0  # успешное открытие сбрасывает счётчик неудач
                self.set_state(contracts.STATE_LISTENING)
                self.log.info("Слушаю микрофон...")
                self._capture_until_stop(window)
                return  # вышли штатно по stop_event — поток закроет on_stop()
            except Exception:
                fails += 1
                self._close_input_stream()
                if fails >= _AUDIO_MAX_RETRIES:
                    self.request_restart(
                        f"аудио-вход недоступен после {fails} попыток переоткрыть микрофон"
                    )
                    return
                delay = min(_AUDIO_RETRY_BASE * fails, _AUDIO_RETRY_MAX)
                self.log_exc(
                    logging.WARNING,
                    "Аудио-вход отвалился (попытка %d из %d) — переоткрою поток через %.1fс",
                    fails, _AUDIO_MAX_RETRIES, delay,
                )
                if self._stop_event.wait(delay):
                    return

    def _open_input_stream(self):
        """Открыть поток ввода. Держим как атрибут, чтобы on_stop() закрыл его штатно
        (контекст-менеджер не используем, чтобы избежать гонки при shutdown)."""
        self._stream = sd.InputStream(
            channels=config.CHANNELS,
            samplerate=config.SAMPLE_RATE,
            dtype="float32",
            device=config.STT_SOURCE or None,  # опц. denoise-source (echo-cancel); пусто = default
        )
        self._stream.start()

    def _close_input_stream(self):
        """Тихо закрыть поток ввода перед переоткрытием (ошибки закрытия — на DEBUG)."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                self.log.debug("Ошибка закрытия аудио-потока при переоткрытии", exc_info=True)
            finally:
                self._stream = None

    def _capture_until_stop(self, window: int):
        """Чтение микрофона и подача в VAD/распознаватель — до сигнала остановки.

        ВНИМАНИЕ: логика VAD/анти-эхо ниже не меняется (пороги закреплены в CLAUDE.md).
        Исключения устройства пробрасываются наружу — их ловит _listen_loop и переоткрывает.
        """
        while not self._stop_event.is_set():
            # Вход читаем всегда (чтобы не переполнить буфер устройства),
            # но пока Джарвис говорит/идёт хвост — VAD не кормим, иначе
            # словим эхо из колонок и зациклимся.
            data, _ = self._stream.read(window)
            # PUSH-TO-TALK имеет приоритет: пока кнопка зажата — копим сырое аудио, минуя wake-word/VAD.
            if self._ptt:
                total = sum(len(b) for b in self._ptt_buf)
                if total < _PTT_MAX_SECONDS * config.SAMPLE_RATE:
                    self._ptt_buf.append(np.asarray(data, dtype=np.float32).reshape(-1))
                self._need_vad_reset = True  # после PTT — чистый VAD
                continue
            if self._ptt_buf:  # кнопку только что отпустили → обработать накопленное (без wake-word)
                self._process_ptt_buffer()
                continue
            if self._is_muted():
                # Пока Джарвис говорит — основной конвейер молчит (анти-эхо). Но СЛУШАЕМ на
                # ПЕРЕБИВАНИЕ: точное «Джарвис…» поверх речи → мгновенный обрыв озвучки (barge-in).
                if (config.BARGE_ENABLED and config.BARGE_VOICE_ENABLED and self._speaking):
                    self._barge_listen(np.asarray(data, dtype=np.float32).reshape(-1))
                # Помечаем, что при возобновлении нужен чистый сброс — чтобы
                # частичный сегмент, накопленный на границе, не склеился
                # со следующей фразой пользователя.
                self._need_vad_reset = True
                continue
            # Адаптация порога VAD под обстановку. Пересборка детектора — в ЭТОМ же потоке
            # (нет гонки с accept_waveform) и ДО reset/accept, чтобы свежий VAD начал с чистого листа.
            self._maybe_adapt_threshold(time.monotonic())
            # Возобновились — сбрасываем VAD ОДИН раз на границе muted→активен.
            if self._need_vad_reset:
                self._vad.reset()
                self._need_vad_reset = False
                self.log.debug("VAD сброшен при возобновлении слушания")
            samples = np.asarray(data, dtype=np.float32).reshape(-1)
            self._vad.accept_waveform(samples)
            # Оценка шумового пола — ТОЛЬКО когда речь не детектируется (фон, не голос пользователя):
            # иначе тихая речь не снижала бы порог. Та же RMS-формула, что у audio_env.
            if not self._vad.is_speech_detected():
                self._update_noise_floor(samples)
            while not self._vad.empty():
                segment = np.asarray(self._vad.front.samples, dtype=np.float32)
                self._vad.pop()
                self._transcribe(segment)

    def _update_noise_floor(self, samples: np.ndarray) -> None:
        """Оценка шумового пола по фоновым (не-речевым) окнам захвата.

        Асимметричная EMA: быстро вниз / очень медленно вверх → следим за фоном, всплески речи
        почти не задирают пол. RMS-формула как в audio_env (единые единицы с адаптивной громкостью)."""
        try:
            rms = float(np.sqrt(np.mean(np.square(samples)) + 1e-12))
            if rms < self._noise_floor:
                self._noise_floor = 0.9 * self._noise_floor + 0.1 * rms
            else:
                self._noise_floor = 0.995 * self._noise_floor + 0.005 * rms
        except Exception:
            self.log.debug("Не удалось обновить шумовой пол", exc_info=True)

    def _maybe_adapt_threshold(self, now: float) -> None:
        """Сместить порог VAD под обстановку: в ТИШИНЕ чуть ниже базы (ловим негромкую речь сразу),
        в шуме — строго закреплённая база VAD_THRESHOLD (базовое поведение не ломаем, см. CLAUDE.md).

        Полосу «тишина/шум» разделяем гистерезисом вокруг QUIET_THRESHOLD (того же, что у адаптивной
        громкости), пересобираем VAD не чаще _VAD_SWITCH_MIN_INTERVAL — чтобы не дёргать модель."""
        if not config.VAD_ADAPTIVE:
            return
        if now - self._last_vad_switch < _VAD_SWITCH_MIN_INTERVAL:
            return
        base = config.VAD_THRESHOLD
        quiet = max(config.VAD_QUIET_FLOOR, base - config.VAD_QUIET_OFFSET)
        qt = config.QUIET_THRESHOLD
        if self._vad_threshold > quiet:
            # Сейчас на базе → опускаем порог ТОЛЬКО в явной тишине (пол ниже порога «тихо вокруг»).
            target = quiet if self._noise_floor < qt else base
        else:
            # Сейчас опущен → возвращаем базу при заметном шуме (гистерезис ×1.5, без дребезга).
            target = base if self._noise_floor > qt * 1.5 else quiet
        if abs(target - self._vad_threshold) < 1e-3:
            return
        try:
            self._vad = self._build_vad(target)
            self._vad_threshold = target
            self._need_vad_reset = True  # свежий VAD — сбросить на границе (ниже в цикле)
            self._last_vad_switch = now
            self.log.info("Порог VAD → %.2f (шумовой пол %.4f, база %.2f)",
                          target, self._noise_floor, base)
        except Exception:
            self.log_exc(logging.WARNING,
                         "Не удалось пересобрать VAD под обстановку — остаюсь на текущем пороге")

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
                # Новая реплика — свежий barge-VAD (если уже строился): копит речь в пределах
                # одной фразы Джарвиса, не таща хвосты прошлой.
                if self._barge_vad is not None:
                    try:
                        self._barge_vad.reset()
                    except Exception:
                        pass
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

    def _asr(self, samples: np.ndarray) -> str:
        """Распознать сегмент → текст (или ""). Общий для wake-word-пути и push-to-talk.

        Слишком короткий сегмент zipformer отвергает (RuntimeError Reshape) — это ОЖИДАЕМО
        (тишина/шум/обрывок), пропускаем тихо. Любой иной сбой — WARN, конвейер живёт."""
        if len(samples) < config.MIN_SEGMENT_SAMPLES:
            self.log.debug("Пропуск короткого сегмента (%d семплов)", len(samples))
            return ""
        try:
            stream = self._recognizer.create_stream()
            stream.accept_waveform(config.SAMPLE_RATE, samples)
            try:
                self._recognizer.decode_stream(stream)
            except RuntimeError as exc:
                self.log.debug("Фрагмент пропущен: короткий/искажённый сегмент (%s)",
                               exc.__class__.__name__)
                return ""
            return stream.result.text.strip()
        except Exception:
            self.log_exc(logging.WARNING,
                         "Не удалось распознать фрагмент — пропускаю, продолжаю слушать")
            return ""

    @staticmethod
    def _rms(samples: np.ndarray):
        try:
            return round(float(np.sqrt(np.mean(np.square(samples)) + 1e-12)), 5)
        except Exception:
            return None

    def _transcribe(self, samples: np.ndarray):
        t0 = time.perf_counter()
        text = self._asr(samples)
        if config.PERF_DEBUG and text:
            self.log.info("PERF stt: ASR-декод %.0fмс (сегмент %.2fс)",
                          (time.perf_counter() - t0) * 1000, len(samples) / config.SAMPLE_RATE)
        if not text:
            return
        # Страховка от эхо-петли: если в окне после речи распознали почти то же,
        # что Джарвис только что произнёс — это эхо из колонок, не команда.
        # Фразу с ТОЧНЫМ wake-префиксом фильтр НЕ трогает: реплики Джарвиса не начинаются
        # с «джарвис» (сверено по пакам) — а команда сразу после ответа, похожая на сам
        # ответ, раньше молча съедалась фильтром как эхо (Этап 21в).
        if not self._has_exact_wake(text) and self._is_echo(text):
            return
        self.log.info("Распознано: %s", text)
        command = self._match_wake_word(text)
        user_level = self._rms(samples)
        if command is not None:
            # Wake-word есть → полноценная команда (core обработает обычным путём, задаст ветку).
            # ПУСТОЙ остаток (голое «Джарвис» и пауза) тоже публикуем: core откликнется
            # «Слушаю» — раньше такая фраза терялась МОЛЧА (точка потери, Этап 21в).
            self._publish_input(command, wake=True, user_level=user_level)
            self.log.info("Команда в шину: %s (wake, громкость речи %.4f)",
                          command or "<голое обращение>", user_level or 0.0)
        elif config.CONTINUATIONS_ENABLED:
            # Без wake-word → отправляем ВСЮ фразу как кандидат-ПРОДОЛЖЕНИЕ активной ветки. Решает
            # core (примет, только если фраза — продолжение текущей ветки; иначе молча игнор).
            full = self._normalize(text)
            if full:
                self._publish_input(full, wake=False, user_level=user_level)
                self.log.debug("Без wake — кандидат-продолжение: %s", full)

    def _publish_input(self, text: str, wake: bool, user_level):
        """Опубликовать распознанное в jarvis/input с флагом wake (есть/нет обращения «Джарвис»)."""
        payload = {"text": text, "wake": bool(wake)}
        if user_level is not None:
            payload["user_level"] = user_level
        self.publish_json(contracts.TOPIC_INPUT, payload, qos=contracts.QOS_INPUT)

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
            # INFO, не debug: отброс фразы — потеря, она должна быть ВИДНА в логах
            # (диагностика «сказал — не услышал» по логам, не гаданием).
            self.log.info("Отброшено как эхо (ratio=%.2f): %r ~ %r", ratio, a, b)
            return True
        return False

    def _has_exact_wake(self, text: str) -> bool:
        """Точный wake-префикс (без difflib). Такую фразу эхо-фильтр не трогает: реплики
        Джарвиса не начинаются с «джарвис», значит это точно команда пользователя."""
        normalized = self._normalize(text)
        if not normalized:
            return False
        return any(normalized.startswith(w.strip().lower())
                   for w in config.WAKE_WORDS if w.strip())

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

        # Near-miss: первое слово ПОХОЖЕ на обращение, но не дотянуло до порога — фраза
        # будет отброшена. INFO с rate-limit, чтобы потери были видны в логах (Этап 21в).
        if best_score >= 0.5:
            now = time.monotonic()
            if now - self._near_wake_log_at >= 10.0:
                self._near_wake_log_at = now
                self.log.info("Похоже на обращение, но ниже порога: '%s' ~ '%s' (%.2f < %.2f) — отброшено",
                              first, best_wake, best_score, config.WAKE_WORD_FUZZY_THRESHOLD)
        return None

    # ------------------------------------------------------------------ #
    # Перебивание речи голосом (barge-in)
    # ------------------------------------------------------------------ #
    def _barge_stop(self, reason: str) -> None:
        """Сигнал TTS оборвать текущую озвучку (barge-in). Если Джарвис молчит — для TTS это no-op."""
        try:
            self.publish_json(contracts.TOPIC_TTS_CONTROL,
                              {"action": "stop", "reason": reason},
                              qos=contracts.QOS_TTS_CONTROL)
        except Exception:
            self.log.debug("Не удалось отправить сигнал перебивания", exc_info=True)

    def _barge_listen(self, samples: np.ndarray) -> None:
        """Лёгкий детектор перебивания голосом, пока Джарвис говорит: точное «Джарвис…» поверх
        речи → мгновенный обрыв + публикация команды. КОНСЕРВАТИВНО (без AEC микрофон слышит сам
        голос Джарвиса): отдельный VAD + энерго-гейт (пользователь должен говорить ГРОМЧЕ эха) +
        ТОЧНЫЙ wake-префикс (реплики Джарвиса не начинаются с «джарвис») + анти-эхо по содержанию.
        Сегментирует на естественных паузах речи; для мгновенного обрыва посреди фразы — надёжнее PTT."""
        try:
            if self._barge_vad is None:
                self._barge_vad = self._build_vad(config.VAD_THRESHOLD)
            self._barge_vad.accept_waveform(samples)
            while not self._barge_vad.empty():
                seg = np.asarray(self._barge_vad.front.samples, dtype=np.float32)
                self._barge_vad.pop()
                rms = self._rms(seg) or 0.0
                if rms < config.BARGE_MIN_RMS:
                    continue  # тише порога — это эхо Джарвиса/фон, не намеренная команда
                text = self._asr(seg)
                if not text or not self._has_exact_wake(text) or self._is_echo(text):
                    continue
                self.log.info("Перебивание голосом: «%s» (RMS %.3f)", text[:40], rms)
                self._barge_stop("wake")
                # Остаток после «Джарвис» → обычная команда (core ответит/исполнит свежей фразой).
                command = self._match_wake_word(text)
                if command is not None:
                    self._publish_input(command, wake=True, user_level=rms)
                try:
                    self._barge_vad.reset()
                except Exception:
                    pass
                return
        except Exception:
            self.log.debug("Сбой детектора перебивания голосом", exc_info=True)

    # ------------------------------------------------------------------ #
    # Push-to-talk (зажатие клавиши → команда без wake-word)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _keyboard_event_paths():
        """Пути /dev/input/event* клавиатур (Handlers содержит kbd). Power/Sleep тоже kbd, но они
        не шлют нужный код — безвредно. Сбой → пусто (PTT просто не активируется)."""
        paths = []
        try:
            with open(_PROC_DEVICES, encoding="utf-8", errors="replace") as f:
                txt = f.read()
            for block in txt.split("\n\n"):
                if "kbd" in block:
                    m = re.search(r"event(\d+)", block)
                    if m:
                        paths.append(f"/dev/input/event{m.group(1)}")
        except Exception:
            pass
        return paths

    def _ptt_loop(self):
        """Поток-читатель клавиатуры: следит за зажатием/отпусканием PTT-кнопки (/dev/input).

        Читаем сырые input_event, фильтруем EV_KEY + код кнопки. value 1=нажата, 0=отпущена,
        2=автоповтор (игнорируем). Нет доступа (нет группы input) → WARN раз + повторный поиск.

        Устройства ПЕРЕОТКРЫВАЮТСЯ при ошибке fd и периодически (Этап 21): раньше выдернутая
        USB-клавиатура либо тихо убивала поток (PTT мёртв до рестарта), либо вгоняла цикл в
        busy-spin (select «readable» → read OSError → continue → снова select)."""
        warned_no_access = False
        announced = False  # «активен» объявляем ОДИН раз (плановые переоткрытия — на DEBUG)
        while not self._stop_event.is_set():
            fds = {}
            for p in self._keyboard_event_paths():
                try:
                    fds[os.open(p, os.O_RDONLY | os.O_NONBLOCK)] = p
                except OSError:
                    continue
            if not fds:
                if not warned_no_access:
                    warned_no_access = True
                    announced = False
                    self.log.warning(
                        "Push-to-talk не активен: нет доступа к /dev/input (нужна группа input: "
                        "`sudo usermod -aG input $USER` + перелогин) или клавиатура не найдена — "
                        "поищу клавиатуры снова через %dс", _PTT_RESCAN_SECONDS)
                if self._stop_event.wait(_PTT_RESCAN_SECONDS):
                    return
                continue
            warned_no_access = False
            if not announced:
                announced = True
                self.log.info("Push-to-talk активен: кнопка код %d (%s)", config.PTT_KEYCODE, config.PTT_KEY)
            else:
                self.log.debug("PTT: клавиатуры переоткрыты (%d устройств)", len(fds))
            deadline = time.monotonic() + _PTT_RESCAN_SECONDS  # периодически подхватываем новые клавиатуры
            reopen = False
            try:
                # Пока кнопка зажата (self._ptt) — плановое переоткрытие откладываем (не потерять release).
                while not self._stop_event.is_set() and not reopen \
                        and (time.monotonic() < deadline or self._ptt):
                    try:
                        r, _, _ = select.select(list(fds), [], [], 0.5)
                    except Exception:
                        self.log.debug("PTT: select сбоил — переоткрою клавиатуры", exc_info=True)
                        reopen = True
                        break
                    for fd in r:
                        try:
                            data = os.read(fd, _EVENT_SIZE * 64)
                        except OSError:
                            # Устройство выдернули — без переоткрытия был бы busy-spin на этом fd.
                            self.log.debug("PTT: устройство ввода закрылось — переоткрою")
                            reopen = True
                            break
                        for off in range(0, len(data) - _EVENT_SIZE + 1, _EVENT_SIZE):
                            _s, _us, etype, code, val = struct.unpack(
                                _EVENT_FMT, data[off:off + _EVENT_SIZE])
                            if etype == _EV_KEY and code == config.PTT_KEYCODE:
                                if val == 1:
                                    self._ptt_press()
                                elif val == 0:
                                    self._ptt_release()
            finally:
                for fd in fds:
                    try:
                        os.close(fd)
                    except Exception:
                        pass

    def _ptt_press(self):
        """Кнопка зажата → режим прослушивания команды без wake-word + приглушить музыку.

        Заодно ПЕРЕБИВАЕМ речь Джарвиса (barge-in): зажал кнопку, чтобы дать команду — значит, ему
        пора замолчать. Физический сигнал, без эха → самый надёжный путь перебивания."""
        if self._ptt:
            return
        self._ptt = True
        self._ptt_buf = []
        if config.BARGE_ENABLED:
            self._barge_stop("ptt")  # если Джарвис говорит — мгновенно оборвать (иначе no-op)
        self.log.info("PTT: слушаю команду (кнопка зажата)")
        if config.DUCK_WHILE_LISTENING:
            try:
                self._env.duck()
            except Exception:
                self.log.debug("PTT: ducking не удался", exc_info=True)

    def _ptt_release(self):
        """Кнопка отпущена → поток захвата обработает накопленный буфер; вернуть музыку."""
        if not self._ptt:
            return
        self._ptt = False  # _capture_until_stop увидит непустой буфер и распознает (без wake-word)
        self.log.info("PTT: отпущено — распознаю команду")
        if config.DUCK_WHILE_LISTENING:
            try:
                self._env.restore()
            except Exception:
                self.log.debug("PTT: restore не удался", exc_info=True)

    def _process_ptt_buffer(self):
        """Распознать накопленное по PTT аудио и опубликовать в jarvis/input БЕЗ wake-word."""
        buf, self._ptt_buf = self._ptt_buf, []
        if not buf:
            return
        try:
            audio = np.concatenate(buf)
        except Exception:
            return
        text = self._asr(audio)
        if not text:
            self.log.info("PTT: команда не распознана")
            return
        # PTT — намеренная команда: wake=true (полноценная, как с обращением «Джарвис»).
        user_level = self._rms(audio)
        self._publish_input(text, wake=True, user_level=user_level)
        self.log.info("PTT команда в шину: %s (громкость речи %.4f)", text, user_level or 0.0)

    def on_stop(self):
        """Корректно гасим аудио-поток ввода ДО выхода интерпретатора.

        Без этого PipeWire/Pulse роняет assertion и даёт core dump при
        резком убийстве потока (Ctrl+C / systemctl stop). Сначала ждём
        выхода потока прослушивания из read(), затем закрываем устройство.
        """
        try:
            if self._audio_thread is not None:
                self._audio_thread.join(timeout=2.0)
            if self._ptt_thread is not None:
                self._ptt_thread.join(timeout=1.0)
        except Exception:
            self.log_exc(logging.ERROR, "Ошибка ожидания аудио-потока ввода")
        # Если музыка была приглушена под PTT — вернуть на выходе (на всякий случай).
        try:
            self._env.restore()
        except Exception:
            self.log.debug("Возврат музыки при остановке не удался", exc_info=True)
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
                self.log.info("Аудио-поток ввода закрыт")
        except Exception:
            self.log_exc(logging.ERROR, "Ошибка закрытия аудио-потока ввода")


def main():
    SttModule().run()


if __name__ == "__main__":
    main()
