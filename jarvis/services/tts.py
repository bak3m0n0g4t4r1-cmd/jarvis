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
import sys
import threading
import time

import numpy as np

from jarvis import config, contracts, phrases, silence
from jarvis.audio_env import AudioEnv
from jarvis.bus import JarvisModule
from jarvis.sysinfo import read_volume

# Сколько фраз подряд не озвучилось, чтобы крикнуть CRITICAL (синтез/вывод недоступен).
_TTS_FAILS_CRITICAL = 3


class _EnvelopeStream:
    """Огибающая РЕАЛЬНОГО звука фразы → батчи в jarvis/tts/envelope (пульсация ламп в такт).

    RMS по окнам (lamp.animation.окно_мс) НЕПРЕРЫВНО через границы чанков-предложений Piper,
    нормализация на бегущий пик фразы. Таймлайн привязан к ФАКТУ первого байта в pw-cat:
    t0 = время первого write + старт_задержка_мс — момент, когда звук реально слышен (сам
    write опережает воспроизведение: pipe ядра держит до ~1.5с аудио). Позиция любого окна
    дальше вычислима из числа уже отданных окон — лампы проигрывают уровни по wall-clock.
    Публикация best-effort: сбой шины НЕ мешает озвучке (всё в try-except)."""

    _seq = 0   # id фразы: класс-счётчик процесса TTS (фразы строго по одной — гонок нет)

    def __init__(self, module: "TtsModule", rate: int, volume: float):
        self._m = module
        self._rate = max(1, int(rate))
        self._vol = round(max(0.0, float(volume)), 3)
        self._win = max(0.01, float(config.LAMP_ANIM_WINDOW_MS) / 1000.0)
        self._win_samples = max(1, int(self._rate * self._win))
        self._rest = b""        # хвост, не кратный окну (переносится в следующий чанк)
        self._bytes_sent = 0    # всего байт звука, ушедших в pw-cat (точная длительность)
        self._win_count = 0     # окон уже опубликовано (offset следующего батча)
        self._peak = 0.02       # бегущий пик фразы (пол — чтобы тишина не делилась на ноль)
        self._t0 = None         # epoch старта ЗВУКА (ставится на первом feed)
        type(self)._seq += 1    # новый стрим = новая фраза
        self._id = type(self)._seq

    def mark_start(self, write_ts: float) -> None:
        """Якорь старта ЗВУКА по метке, снятой ПЕРЕД первым write в pw-cat. write первого
        (длинного) предложения блокируется на наполнение пайпа ядра (~64КБ ≈ 1.5с) — раньше
        t0 ставился в feed() ПОСЛЕ write и съезжал на величину блокировки, ломая упреждение
        ламп. write_ts — time.time() (epoch, НЕ perf_counter: лампы сэмплируют по time.time())
        прямо перед proc.stdin.write первого реального звука (чайм/чанк). Идемпотентен."""
        if self._t0 is not None:
            return
        self._t0 = write_ts + float(config.LAMP_ANIM_START_OFFSET_MS) / 1000.0
        if config.PERF_DEBUG:
            self._m.log.info("PERF tts: якорь анимации t0=%.4f (старт_задержка %.0fмс от write)",
                             self._t0, float(config.LAMP_ANIM_START_OFFSET_MS))

    def feed(self, pcm: bytes) -> None:
        """Учесть чанк, только что УСПЕШНО записанный в pw-cat, и опубликовать батч уровней."""
        try:
            if not pcm:
                return
            if self._t0 is None:
                self._t0 = time.time() + float(config.LAMP_ANIM_START_OFFSET_MS) / 1000.0
            self._bytes_sent += len(pcm)
            buf = self._rest + pcm
            n_win = len(buf) // (2 * self._win_samples)
            if n_win <= 0:
                self._rest = buf
                return
            usable = n_win * self._win_samples * 2
            self._rest = buf[usable:]
            arr = np.frombuffer(buf[:usable], dtype=np.int16).astype(np.float32) / 32768.0
            rms = np.sqrt(np.mean(np.square(arr.reshape(n_win, self._win_samples),
                                            dtype=np.float64), axis=1) + 1e-12)
            self._peak = max(self._peak, float(rms.max()))
            levels = [round(float(x), 3) for x in np.clip(rms / self._peak, 0.0, 1.0)]
            payload = {"seq": self._id, "t0": round(self._t0, 4),
                       "offset": round(self._win_count * self._win, 4),
                       "win": self._win, "vol": self._vol, "levels": levels}
            self._win_count += n_win
            self._m.publish_json(contracts.TOPIC_TTS_ENVELOPE, payload,
                                 qos=contracts.QOS_TTS_ENVELOPE)
        except Exception:
            self._m.log.debug("Огибающая: сбой батча — пропускаю", exc_info=True)

    def finish(self, cancelled: bool) -> None:
        """Финал из finally синтеза: точная длительность звука или cancel (авария pw-cat —
        лампы гасят анимацию немедленно). Если звука не было вовсе — молчим."""
        try:
            if self._t0 is None:
                return
            payload = {"seq": self._id, "final": True}
            if cancelled:
                payload["cancel"] = True
            else:
                payload["duration"] = round(self._bytes_sent / (2.0 * self._rate), 4)
            self._m.publish_json(contracts.TOPIC_TTS_ENVELOPE, payload,
                                 qos=contracts.QOS_TTS_ENVELOPE)
        except Exception:
            self._m.log.debug("Огибающая: сбой финала", exc_info=True)


def _chime_pcm(rate: int) -> bytes:
    """Короткий мягкий двухнотный сигнал (s16 PCM) перед фразой таймера. Генерится тоном
    (numpy-синус) — без звуковых файлов и зависимостей; громкость задаёт pw-cat --volume."""
    import math

    import numpy as np

    def beep(freq, dur):
        n = int(rate * dur)
        t = np.arange(n) / rate
        wave = np.sin(2 * math.pi * freq * t)
        # Плавные нарастание/спад (10 мс) — чтобы не было щелчков на стыках.
        fade = max(1, int(rate * 0.01))
        env = np.ones(n)
        if n > 2 * fade:
            env[:fade] = np.linspace(0, 1, fade)
            env[-fade:] = np.linspace(1, 0, fade)
        return wave * env * 0.5

    gap = np.zeros(int(rate * 0.06))
    seq = np.concatenate([beep(880.0, 0.12), gap, beep(1175.0, 0.14)])  # «ди-дии», восходящий
    return np.clip(seq * 32767, -32768, 32767).astype(np.int16).tobytes()


class TtsModule(JarvisModule):
    """«Голос»: озвучивает фразы из jarvis/say через Piper + PipeWire с адаптивной громкостью."""

    def __init__(self):
        super().__init__("jarvis-tts")
        # Очередь хранит (текст, громкость_речи, нижний_предел_громкости, чайм, критично).
        # Уровень — от STT для адаптации; нижний предел — будильник/таймер (обход «тихо→тихо»);
        # чайм — короткий сигнал перед фразой (таймер); критично — озвучить даже в режиме тишины.
        self._queue: "queue.Queue[tuple[str, float | None, float | None, bool, bool]]" = queue.Queue()
        self._voice = None
        self._voice_tried = False
        self._voice_lock = threading.Lock()
        self._worker = None
        self._play_proc: subprocess.Popen | None = None
        self._env = AudioEnv()  # замер обстановки + ducking + расчёт громкости
        # Режим тишины / дубль речи: id «речевого» уведомления (заменяем, чтобы не копились) + кэш
        # проверки звука перед фразой (не дёргать wpctl на каждую реплику в очереди).
        self._speech_notif_id = 0
        self._audio_state_cache: tuple[str, str] | None = None
        self._audio_check_at = 0.0
        self._audio_refreshing = False  # guard: один фоновый wpctl-замер за раз
        # Перебивание речи (barge-in): взводится из on_control (STT попросил оборвать). Текущий
        # синтез прерывается, очередь чистится. Намеренный обрыв ≠ сбой озвучки (см. _synth_and_play).
        self._interrupt = threading.Event()

    def on_start(self):
        self.subscribe(contracts.TOPIC_SAY, self.on_say)
        self.subscribe(contracts.TOPIC_TTS_CONTROL, self.on_control)  # перебивание речи (barge-in)
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
        # Прогрев кэша проверки звука: первая фраза не платит синхронный wpctl (~17мс).
        self._refresh_audio_state()

    def _ensure_voice(self):
        """Загрузить голос Piper один раз (потокобезопасно). Используется и прогревом, и _speak."""
        if self._voice is not None or self._voice_tried:
            return
        with self._voice_lock:
            if self._voice is not None or self._voice_tried:
                return
            self._voice_tried = True
            try:
                # Число потоков синтеза (Этап 25): выставляем ДО первого создания сессии
                # onnxruntime (PiperVoice.load). 0 — не трогать (онрантайм-дефолт). На N100
                # первый чанк упирается в CPU — потоки + performance-governor режут задержку.
                n = int(config.VOICE_SYNTH_THREADS or 0)
                if n > 0:
                    import os
                    os.environ["OMP_NUM_THREADS"] = str(n)
                    os.environ["ORT_INTRA_OP_NUM_THREADS"] = str(n)
                    self.log.info("Piper: число потоков синтеза = %d", n)

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
            # Нижний предел громкости (будильник/таймер): озвучить НЕ тише — обходит «тихо→тихо».
            mv = payload.get("min_volume")
            chime = bool(payload.get("chime"))  # короткий сигнал перед фразой (таймер)
            # Критично = срабатывание (будильник/таймер/напоминание): звучит даже в режиме тишины.
            # Сигнал — явный флаг ИЛИ наличие min_volume (его ставят только срабатывания).
            critical = bool(payload.get("critical")) or isinstance(mv, (int, float))
            self._queue.put((
                text,
                level if isinstance(level, (int, float)) else None,
                float(mv) if isinstance(mv, (int, float)) else None,
                chime,
                critical,
            ))

    def on_control(self, payload: dict):
        """Перебивание речи (barge-in): пользователь заговорил (PTT/точное «Джарвис…») — мгновенно
        оборвать текущую озвучку и выбросить отложенные фразы. Сама новая команда придёт обычным
        путём (jarvis/input → core → jarvis/say) и озвучится свежей."""
        try:
            if (payload or {}).get("action") != "stop":
                return
            self._interrupt.set()
            proc = self._play_proc
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()   # pw-cat завершится → write даст BrokenPipe → синтез прервётся
                except Exception:
                    self.log.debug("Не удалось прервать pw-cat при barge-in", exc_info=True)
            self._drain_queue()        # отложенные фразы после перебивания не нужны
            self.log.info("Перебивание (barge-in, %s) — обрываю озвучку", payload.get("reason", "?"))
        except Exception:
            self.log.debug("Сбой обработки barge-in", exc_info=True)

    def _drain_queue(self) -> None:
        """Выбросить все отложенные фразы из очереди (после перебивания/сброса)."""
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass

    def _run_worker(self):
        """Очередь озвучки по одной фразе. Воркер не умирает от единичного сбоя; стойкий сбой
        (3+ фразы) → CRITICAL, но продолжаем пытаться (вернётся устройство — озвучка оживёт)."""
        fails = 0
        announced_critical = False
        while not self._stop_event.is_set():
            try:
                # Сбрасываем флаг перебивания ПЕРЕД новой фразой: очередь уже очищена в on_control,
                # а свежая barge-команда придёт по jarvis/say ПОСЛЕ (paho-колбэки сериализованы) —
                # её мы НЕ теряем. Чистим, не дренируем (drain — только в on_control до прихода команды).
                if self._interrupt.is_set():
                    self._interrupt.clear()
                try:
                    text, level, min_volume, chime, critical = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if self._speak(text, level, min_volume, chime, critical):
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
                        # Элегантное уведомление (без трейса) + кнопка к логу голоса.
                        self.notify_failure(
                            "Голос сейчас недоступен, сэр.",
                            "Синтез речи или вывод звука не отвечает несколько фраз подряд.")
                        announced_critical = True
            except Exception:
                self.log_exc(logging.WARNING, "Сбой в цикле озвучки — продолжаю работу")

    def _speak(self, text: str, user_level: float | None, min_volume: float | None = None,
               chime: bool = False, critical: bool = False) -> bool:
        """Озвучить фразу с адаптивной громкостью. True — успех/обработано; False — сбой озвучки.

        Перед озвучкой: (1) РЕЖИМ ТИШИНЫ — некритичное уходит в УВЕДОМЛЕНИЕ (голос молчит),
        критическое (будильник/таймер/напоминание) звучит всегда; (2) ПРОВЕРКА ЗВУКА — громкость 0
        или сбой устройства → дубль фразы в уведомление с пометкой ПРИЧИНЫ (две разные).
        speaking держится РЕАЛЬНО пока играет звук (тайминг анти-эхо); ducking музыки; min_volume
        (будильник) — гарантированный нижний предел громкости (обход «тихо→тихо»)."""
        t_start = time.perf_counter()
        # (1) Режим тишины: обычные фразы — в уведомление; критическое — озвучиваем как обычно.
        if silence.is_silent() and not critical:
            self._notify_speech(text)
            return True
        # (2) Доступность звука перед озвучкой (быстро, с кэшем): «ноль» и «сбой» — РАЗНЫЕ пометки.
        state, reason = self._audio_state()
        if state == "zero":
            self._notify_speech(text, phrases.pick("notif.audio_zero", config.AUDIO_ZERO_PREFIX))
            return True  # на нуле голос немой — доставили текстом
        if state == "fail":
            pref = phrases.pick("notif.audio_fail", config.AUDIO_FAIL_PREFIX)
            self._notify_speech(text, f"{pref} {reason}".strip())
            return True
        self._ensure_voice()
        if self._voice is None:
            self.log.warning("Голос Piper не загружен — не могу озвучить фразу: %r", text[:60])
            self._notify_speech(text, phrases.pick("notif.audio_fail", config.AUDIO_FAIL_PREFIX)
                                + " синтез речи не загрузился.")
            return False
        ducked = False
        duck_thread = None
        try:
            self.set_state(contracts.STATE_SPEAKING)
            self._env.set_speaking(True)
            # Ducking музыки ноута, если она реально звучит выше порога. Рампа (~150мс pactl+паузы)
            # идёт ПАРАЛЛЕЛЬНО синтезу Piper (~170мс): музыка приглушается, пока готовится первый
            # чанк, а не ПЕРЕД ним. restore — строго после конца рампы (join в finally).
            if self._env.should_duck():
                duck_thread = threading.Thread(target=self._env.duck, daemon=True, name="tts-duck")
                duck_thread.start()
                ducked = True
            volume, gain = self._env.target_voice(user_level)
            # Будильник: поднять громкость до нижнего предела, если адаптив дал тише (будит надёжно).
            if min_volume is not None and min_volume > 0:
                volume = max(volume, min(1.0, float(min_volume)))
            self.log.debug("Громкость голоса %.2f (gain %.2f), ducking=%s", volume, gain, ducked)
            if config.PERF_DEBUG:
                self.log.info("PERF tts: проверки до синтеза %.1fмс (ducking=%s)",
                              (time.perf_counter() - t_start) * 1000, ducked)
            self._synth_and_play(text, volume, gain, chime)
            return True
        except Exception as exc:
            self.log.warning(
                "Не удалось синтезировать/воспроизвести фразу — пропускаю: %r (%s: %s)",
                text[:60], type(exc).__name__, exc,
            )
            self.log.debug("Трасса сбоя озвучки", exc_info=True)
            # Не теряем фразу: дублируем в уведомление с технической пометкой (без сырого трейса).
            self._audio_state_cache = None  # сбросить кэш — звук явно проблемный
            pref = phrases.pick("notif.audio_fail", config.AUDIO_FAIL_PREFIX)
            self._notify_speech(text, f"{pref} поток воспроизведения не открылся.")
            return False
        finally:
            self.set_state(contracts.STATE_IDLE)
            self._env.set_speaking(False)
            if ducked:
                if duck_thread is not None:
                    # Дождаться конца рампы duck ПЕРЕД restore — иначе гонка: restore
                    # вернул громкость, хвост рампы снова приглушил → музыка застряла тихой.
                    duck_thread.join(timeout=2.0)
                self._env.restore()  # плавно вернуть музыку к исходной громкости

    def _notify_speech(self, text: str, prefix: str | None = None) -> None:
        """Показать фразу в системном уведомлении (дубль речи: режим тишины или проблемы звука).

        Без кнопки (это не сбой Джарвиса). Заменяем прежнее «речевое» уведомление (replace_id),
        чтобы они не копились пачкой при череде ответов."""
        try:
            body = f"{prefix}\n{text}" if prefix else text
            nid = self.notify(config.NOTIFY_SPEECH_TITLE, body, urgency="normal",
                              replace_id=self._speech_notif_id)
            if nid:
                self._speech_notif_id = nid
        except Exception:
            self.log.debug("Не удалось продублировать фразу в уведомление", exc_info=True)

    def _audio_state(self) -> tuple[str, str]:
        """Состояние звука перед фразой: ('ok'|'zero'|'fail', причина).

        Кэш освежается АСИНХРОННО (wpctl ~17мс не задерживает первый звук): отдаём последнее
        известное значение сразу, протухшее обновляем фоном — следующая фраза увидит свежее.
        СИНХРОННО проверяем только когда кэша ещё нет или он говорит «проблема» ('zero'/'fail'):
        снятие проблемы нельзя брать из устаревшего кэша — фраза ушла бы в уведомление с
        неверной пометкой причины (точность пометок ТЗ-16)."""
        now = time.monotonic()
        cached = self._audio_state_cache
        if cached is not None and (now - self._audio_check_at) < config.AUDIO_CHECK_TTL:
            return cached
        if cached is None or cached[0] != "ok":
            self._audio_state_cache = self._classify_audio()
            self._audio_check_at = time.monotonic()
            return self._audio_state_cache
        self._refresh_audio_state()
        return cached

    def _refresh_audio_state(self) -> None:
        """Обновить кэш проверки звука в фоне (один поток за раз; сбой → кэш не трогаем)."""
        if self._audio_refreshing:
            return
        self._audio_refreshing = True

        def _job():
            try:
                state = self._classify_audio()
                self._audio_state_cache = state
                self._audio_check_at = time.monotonic()
            except Exception:
                self.log.debug("Фоновая проверка звука не удалась", exc_info=True)
            finally:
                self._audio_refreshing = False

        threading.Thread(target=_job, daemon=True, name="tts-audio-check").start()

    @staticmethod
    def _classify_audio() -> tuple[str, str]:
        """Различить «громкость на нуле» (пользователь убавил) и ТЕХНИЧЕСКИЙ сбой (устройство/
        PipeWire недоступны). Через sysinfo.read_volume (wpctl, read-only). Сбой пробы → 'fail'."""
        try:
            data = read_volume()
            if "ошибка" in data:
                return ("fail", "аудиосистема не отвечает.")
            if data.get("выключен"):
                return ("zero", "")
            vol = data.get("громкость_процент")
            if isinstance(vol, (int, float)) and vol <= 0:
                return ("zero", "")
            return ("ok", "")
        except Exception:
            return ("fail", "не удалось проверить звук.")

    def _synth_and_play(self, text: str, volume: float, gain: float, chime: bool = False) -> None:
        """ПОТОКОВЫЙ синтез+воспроизведение: чанки Piper пишем в stdin pw-cat по мере готовности
        (время до первого звука ↓). Громкость — pw-cat --volume; усиление >1 — gain PCM.

        Скорость и тон НЕЗАВИСИМЫ: темп задаёт length_scale Piper, тон — подмена частоты pw-cat
        (--rate = частота_модели·pitch). length_scale домножаем на pitch, чтобы компенсировать
        растяжение времени от ресэмплинга → net-темп = length_scale, net-тон = pitch. Pitch не
        формант-сохраняющий: для МАЛОГО сдвига звучит чисто, без доп. зависимостей и заметного CPU
        (pw-cat и так ресэмплит к частоте устройства)."""
        from piper import SynthesisConfig

        from jarvis import speech
        # Словарь произношения/ударений (ТЗ-10): правим проблемные слова ПЕРЕД синтезом.
        text = speech.apply_pronunciation(text, config.PRONUNCIATION)
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
        t_synth = time.perf_counter()
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self._play_proc = proc
        # stderr читаем ФОНОВЫМ потоком: чтение после wait() — классический дедлок (pw-cat,
        # заливший stderr больше буфера pipe, никогда не завершится; найдено аудитом захода).
        err_buf: list[bytes] = []
        err_thread = threading.Thread(target=self._drain_stderr, args=(proc, err_buf),
                                      daemon=True, name="tts-stderr")
        err_thread.start()
        # Огибающая звука для анимации ламп (заход «лампы»): кормим тем, что РЕАЛЬНО ушло
        # в pw-cat (чайм + чанки), финал — в finally. Выключена → ноль накладных расходов.
        env_stream = _EnvelopeStream(self, rate, volume) if config.LAMP_ANIM_ENABLED else None
        first_write = None

        def _mark_first():
            nonlocal first_write
            if first_write is None:
                first_write = time.perf_counter()
                if config.PERF_DEBUG:
                    self.log.info("PERF tts: первый звук через %.0fмс от старта синтеза",
                                  (first_write - t_synth) * 1000)
        try:
            if chime:  # короткий сигнал перед фразой (таймер) — тем же потоком pw-cat
                try:
                    data = _chime_pcm(rate)
                    if gain > 1.0:
                        data = AudioEnv.apply_gain(data, gain)  # чайм не тонет в шуме (как фраза)
                    ts = time.time()  # якорь анимации ДО write (write блокируется на пайпе)
                    proc.stdin.write(data)
                    _mark_first()  # чайм — первый РЕАЛЬНЫЙ звук (раньше PERF-метка его не видела)
                    if env_stream is not None:
                        env_stream.mark_start(ts)  # якорь по метке перед write чайма
                        env_stream.feed(data)
                except Exception:
                    self.log.debug("Не удалось проиграть чайм", exc_info=True)
            for chunk in self._voice.synthesize(text, syn_config=syn):
                if self._interrupt.is_set():
                    break  # barge-in: пользователь заговорил — прекращаем синтез немедленно
                pcm = chunk.audio_int16_bytes
                if gain > 1.0:
                    pcm = AudioEnv.apply_gain(pcm, gain)
                try:
                    ts = time.time()  # якорь анимации ДО write первого чанка (если чайма не было)
                    proc.stdin.write(pcm)  # стримим по мере синтеза
                    _mark_first()
                    if env_stream is not None:
                        env_stream.mark_start(ts)  # no-op, если t0 уже поставлен чаймом
                        env_stream.feed(pcm)
                except BrokenPipeError:
                    break
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.wait()  # дождаться конца воспроизведения (НЕ communicate — stdin уже закрыт)
        finally:
            # Дожать pw-cat ПРИ ЛЮБОМ исходе: исключение синтеза посреди цикла раньше
            # оставляло процесс осиротевшим с открытым stdin (висел до сборщика мусора —
            # утечка fd/процесса, найдено аудитом Этапа 21в). Штатный путь уже дождался
            # wait() выше — здесь poll() не None и блок проходится мгновенно.
            try:
                proc.stdin.close()
            except Exception:
                pass
            if proc.poll() is None:
                try:
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.terminate()
                        proc.wait(timeout=2)
                    except Exception:
                        self.log.debug("pw-cat не дожат", exc_info=True)
            try:
                err_thread.join(timeout=1.0)
            except Exception:
                pass
            try:
                proc.stderr.close()
            except Exception:
                pass
            self._play_proc = None
            # Финал огибающей ВСЕГДА (лампы вернутся в фон): штатно — точная длительность;
            # исключение синтеза или ненулевой код pw-cat → cancel (гасить немедленно).
            if env_stream is not None:
                cancelled = (sys.exc_info()[0] is not None
                             or proc.returncode not in (0, None))
                env_stream.finish(cancelled)
        # Намеренный обрыв (barge-in убил pw-cat) — НЕ сбой: не поднимаем ошибку, не шлём
        # уведомление/не считаем фразу проваленной. Лампы уже погасли через envelope cancel
        # (returncode≠0 → cancelled=True в finally выше). Иначе ненулевой код — настоящий сбой.
        if proc.returncode not in (0, None) and not self._interrupt.is_set():
            err = b"".join(err_buf)
            detail = err.decode("utf-8", "replace").strip() if err else ""
            raise RuntimeError(f"pw-cat вернул код {proc.returncode}: {detail or 'без stderr'}")

    @staticmethod
    def _drain_stderr(proc, sink: list) -> None:
        """Выпить stderr pw-cat до EOF (фоновый поток): wait() не блокируется переполненным
        pipe, а текст ошибки доступен после завершения процесса."""
        try:
            data = proc.stderr.read()
            if data:
                sink.append(data)
        except Exception:
            pass

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
