"""«Мозг» Джарвиса: лёгкий распознаватель команд (без облака и без LLM).

Реплика из jarvis/input проходит так:
  1. встроенные info-ответы (время/заряд) — офлайн, мгновенно, в характере;
  2. матчер (правила + ONNX-эмбеддинги, см. jarvis/matcher.py) → тег команды →
     подтверждение в jarvis/say + тег в jarvis/execute (исполняет OS-агент);
  3. ничего не распознано → переспрос в характере (без падения).

Никакой генеративной модели: ум заменён на дешёвый гибридный матчер, чтобы
N100/8 ГБ не тормозил. Команды звучат мгновенно и работают офлайн.
"""
import logging
import random
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import yaml

from jarvis import chains, config, contracts, phrases, silence, system, voice_volume, worldtime
from jarvis import lamp as lamp_helpers
from jarvis.breaks import is_stop_phrase
from jarvis.bus import JarvisModule
from jarvis.matcher import NOT_RECOGNIZED, Matcher
from jarvis.reminders import is_dialog_pending, is_scheduler_command
from jarvis.sysinfo import read_battery, read_system_load, read_volume
from jarvis.speech import say_date, say_percent, say_time

# Ключевые слова встроенных info-ответов (срабатывают на явный вопрос).
_TIME_KEYS = (
    "который час", "которой час", "которое время", "сколько времени",
    "сколько время", "точное время", "время сейчас", "сколько на часах",
)
_BATTERY_KEYS = ("батаре", "аккумулят", "заряд", "сколько процент")
_DATE_KEYS = ("какое сегодня число", "какое число", "какая дата", "какой сегодня день",
              "число сегодня", "сегодня какое", "какой день недели", "какое число сегодня")
_VOLUME_KEYS = ("какая громкость", "уровень громкости", "текущая громкость",
                "насколько громко", "громкость сейчас", "какая сейчас громкость")
_LOAD_KEYS = ("загрузка системы", "загрузка процессора", "загрузка цпу", "нагрузка системы",
              "сколько памяти", "сколько оперативной", "сколько озу", "использование памяти",
              "загружен процессор", "загружена память", "свободной памяти", "оперативной памяти")


def _load_commands() -> dict:
    """Карта команд из commands.yaml (теги → спецификации). При сбое — пустой dict.

    Источник истины и для матчера (синонимы/примеры/подтверждения), и для OS-агента.
    """
    try:
        with open(config.COMMANDS_FILE, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


class CoreModule(JarvisModule):
    """«Мозг»: преобразует текст пользователя в тег команды и реплику-подтверждение."""

    def __init__(self):
        super().__init__("jarvis-core")
        commands = _load_commands()
        self._commands = commands
        # Матчер: правила (мгновенно) + эмбеддинги (лениво, при промахе правил).
        self._matcher = Matcher(commands)
        # Цепочки команд (ТЗ-5): ветки продолжений + история + активная ветка.
        self._branches, self._primary = chains.build_branches(commands)
        self._active_branch = None          # ветка, чьи продолжения принимаем без wake-word
        self._history = chains.History()     # история выполненных команд (повтор/отмена)
        self._last_command = None            # последний выполненный тег
        # Один разбор за раз: на N100 нет смысла молотить эмбеддинги в несколько потоков.
        self._lock = threading.Lock()

    def on_start(self):
        self.subscribe(contracts.TOPIC_INPUT, self.on_input)

    def on_input(self, payload: dict):
        text = (payload.get("text") or "").strip()
        if not text:
            # Голое «Джарвис» без команды (wake есть, остаток пуст): откликаемся, что слушаем —
            # раньше фраза терялась МОЛЧА (точка потери, Этап 21в). Требуем ЯВНЫЙ wake=true:
            # пустой текст от старых публикаторов без поля wake — мусор, игнорируем как раньше.
            if payload.get("wake") is True:
                level = payload.get("user_level")
                ul = level if isinstance(level, (int, float)) else None
                threading.Thread(target=self._say_listening, args=(ul,), daemon=True).start()
            return
        # Громкость речи пользователя (от STT) пробрасываем в ответ — для адаптивной громкости TTS.
        level = payload.get("user_level")
        user_level = level if isinstance(level, (int, float)) else None
        # Команда РЕЖИМА ТИШИНЫ ловится из эфира ВСЕГДА (с wake и без — как стоп-фраза перерыва).
        if silence.is_silence_on(text) or silence.is_silence_off(text):
            threading.Thread(target=self._toggle_silence,
                             args=(text, user_level), daemon=True).start()
            return
        # wake — было ли обращение «Джарвис»/PTT. Нет поля = true (обратная совместимость).
        wake = payload.get("wake", True)
        if not wake:
            # Без wake-word — кандидат-ПРОДОЛЖЕНИЕ активной ветки (без гейтов scheduler/stop: «тише»/
            # «следующий» к ним не относятся). core примет, ТОЛЬКО если фраза — продолжение ветки.
            threading.Thread(target=self._process_continuation,
                             args=(text, user_level), daemon=True).start()
            return
        # Дальше — полноценная команда (с обращением). Стоп-фразу перерыва обрабатывает
        # activity_monitor (он ответит и сбросит цикл) — здесь молчим, чтобы не было двойного ответа.
        if is_stop_phrase(text):
            self.log.info("Стоп-фраза перерыва — обработает монитор активности: %s", text)
            return
        # Команда планировщика (будильник/таймер/секундомер/напоминание/задача) — обработает scheduler.
        # ИЛИ идёт диалог дозапроса напоминания → молчим на ЛЮБОЙ ввод (ответ уйдёт планировщику).
        # Мировое время/монетка — НЕ команды планировщика, идут ниже.
        if is_scheduler_command(text) or is_dialog_pending():
            self.log.info("Команда планировщика/диалог — обработает scheduler: %s", text)
            return
        # Обработку выносим в поток, чтобы не блокировать MQTT-loop.
        threading.Thread(target=self._process_wake_timed,
                         args=(text, user_level, time.perf_counter()), daemon=True).start()

    def _process_wake_timed(self, text: str, user_level, t0: float):
        """Обёртка _process_wake с замером (perf_debug): приём фразы → ответ/исполнение."""
        self._process_wake(text, user_level)
        if config.PERF_DEBUG:
            self.log.info("PERF core: фраза обработана за %.1fмс — %s",
                          (time.perf_counter() - t0) * 1000, text[:40])

    def _toggle_silence(self, text: str, user_level: float | None) -> None:
        """Включить/выключить режим тишины + ответ в характере.

        ON: сперва ставим тишину → ack уйдёт в УВЕДОМЛЕНИЕ (TTS уже молчит). OFF: сперва снимаем
        тишину → ack ОЗВУЧИТСЯ голосом («теперь уже голосом»). Состояние переживает рестарт."""
        with self._lock:
            try:
                if silence.is_silence_off(text):
                    silence.set_silent(False)
                    self.log.info("Режим тишины ВЫКЛЮЧЕН: %s", text)
                    self.publish_event("silence_off")  # лампы: мягкая индикация (заход «лампы»)
                    self._say(phrases.pick("silence.off_ack", config.SILENCE_OFF_ACK), user_level)
                else:
                    silence.set_silent(True)
                    self.log.info("Режим тишины ВКЛЮЧЁН: %s", text)
                    self.publish_event("silence_on")
                    self._say(phrases.pick("silence.on_ack", config.SILENCE_ON_ACK), user_level)
            except Exception:
                self.log_exc(logging.ERROR, "Сбой переключения режима тишины")

    def _say_listening(self, user_level: float | None) -> None:
        """Отклик на голое «Джарвис» (обращение без команды): подтверждаем, что слушаем."""
        try:
            self._say(phrases.pick("recognition.listening", config.LISTENING_ACK), user_level)
        except Exception:
            self.log_exc(logging.WARNING, "Сбой отклика на обращение")

    def _process_wake(self, text: str, user_level: float | None = None):
        """Полноценная команда (с wake-word/PTT): повтор/отмена → info → комбо → матчер.

        Под защитой: непредвиденный сбой не должен убить поток и оставить шину в thinking."""
        with self._lock:
            try:
                self.set_state(contracts.STATE_THINKING)

                # 0) История: «повтори последнее» / «отмени».
                if chains.is_repeat(text):
                    self._do_repeat(user_level)
                    return
                if chains.is_undo(text):
                    self._do_undo(user_level)
                    return

                # 0.1) Системное (ТЗ-7, только wake): перезагрузка Джарвиса / открыть рабочую среду.
                if system.is_restart_command(text):
                    self._do_restart(user_level)
                    return
                if system.is_environment_command(text):
                    self._do_environment(text, user_level)
                    return

                # 0.15) Яркость ламп уровнем (заход «лампы»): «яркость ламп 50», «лампы
                # наполовину». СТРОГО ДО гейта громкости голоса: «громкость лампы 50» —
                # это про лампы (раньше угоняла громкость ГОЛОСА — гейт-коллизия).
                if lamp_helpers.is_lamp_level_command(text):
                    self._do_lamp_level(text)
                    return

                # 0.2) Базовая громкость голоса (ТЗ-10): «громкость 30» / «половина громкости».
                if voice_volume.is_volume_command(text):
                    self._do_volume(text, user_level)
                    return

                # 1) Встроенные info-ответы (офлайн, без матчера).
                info = self._local_info_answer(text)
                if info is not None:
                    self._say(info, user_level)
                    return

                # 2) КОМБО: несколько действий через «и/потом» — если распозналось ≥2 команды.
                if self._try_combo(text, user_level):
                    return

                # 3) Одиночная команда матчером.
                match = self._matcher.match(text)
                if match is not None:
                    self.log.info("Команда распознана: %s (%s, %.3f) — %s",
                                  match.tag, match.layer, match.score, text)
                    self._execute_matched(match.tag, user_level, text=text)
                    return

                # 4) Не распознано — переспрос в характере (не падаем).
                self.log.info("Не распознано: %s", text)
                self._say(NOT_RECOGNIZED, user_level)
            except Exception:
                self.log_exc(logging.ERROR,
                             "Непредвиденный сбой обработки реплики — сбрасываю состояние")
            finally:
                # IDLE на ЛЮБОМ исходе: раньше в режиме тишины (TTS не трогает состояния)
                # thinking зависал до следующей фразы (видно в jarvis live).
                self.set_state(contracts.STATE_IDLE)

    def _process_continuation(self, text: str, user_level: float | None = None):
        """Фраза без wake-word: принять, ТОЛЬКО если это продолжение АКТИВНОЙ ветки (иначе молчим).

        Матчим строго по правилам в подмножестве ветки (в малом наборе эмбеддинги «перематчивают»).
        Ветку при этом НЕ меняем (контекст сохраняется: музыка→тише остаётся музыкой)."""
        with self._lock:
            try:
                if not self._active_branch:
                    return  # нет активной ветки — продолжения не ждём, молчим
                tags = self._branches.get(self._active_branch)
                if not tags:
                    return
                match = self._matcher.match(text, allowed_tags=tags, use_embeddings=False)
                if match is None:
                    return  # не продолжение этой ветки — тихо игнорируем (никаких «не разобрал»)
                self.log.info("Продолжение ветки «%s»: %s — %s",
                              self._active_branch, match.tag, text)
                self.set_state(contracts.STATE_THINKING)
                self._execute_matched(match.tag, user_level, set_branch=False, text=text)
                self.set_state(contracts.STATE_IDLE)  # не зависаем в thinking (режим тишины)
            except Exception:
                self.log_exc(logging.WARNING, "Сбой обработки продолжения — игнорирую")
                self.set_state(contracts.STATE_IDLE)

    # ------------------------------------------------------------------ #
    # Выполнение команд, ветки, комбо, история
    # ------------------------------------------------------------------ #
    def _forward_special(self, spec: dict, text: str | None = None) -> str | None:
        """Спец-поля команды (НЕ shell): «лампа» → jarvis/lamp, «телефон» → jarvis/phone/command.

        Возврат: «лампа» / «телефон» / None (обычная команда). Единая точка роутинга для
        _dispatch и _execute_matched (раньше дублировалась — риск рассинхрона). В payload ламп
        добавляется исходная фраза — для адресации по имени («выключи вторую лампу»)."""
        if isinstance(spec.get("лампа"), dict):
            payload = dict(spec["лампа"])
            if text:
                payload["текст"] = text
            self.publish_json(contracts.TOPIC_LAMP, payload, qos=contracts.QOS_LAMP)
            return "лампа"
        if isinstance(spec.get("телефон"), dict):
            self.publish_json(contracts.TOPIC_PHONE_COMMAND, spec["телефон"],
                              qos=contracts.QOS_EXECUTE)
            return "телефон"
        return None

    def _dispatch(self, tag: str) -> None:
        """Отправить тег на исполнение: лампа (поле «лампа») → jarvis/lamp; иначе → jarvis/execute.

        Команды лампы — не shell: у них поле `лампа: {действие,…}` вместо `команда`. Их обрабатывает
        сервис lamp по jarvis/lamp; os_agent их НЕ видит."""
        spec = self._commands.get(tag) or {}
        if self._forward_special(spec) is None:
            self._execute_command(tag)

    def _execute_matched(self, tag: str, user_level, set_branch: bool = True,
                         text: str | None = None) -> None:
        """Выполнить тег + обновить ветку (опц.) и историю. Озвучка: обычная команда/телефон —
        подтверждение здесь; КОМАНДА ЛАМП — озвучивает сервис lamp (знает успех/недоступность)."""
        spec = self._commands.get(tag) or {}
        routed = self._forward_special(spec, text)
        if routed == "телефон":
            # Команда телефону (find_phone): подтверждение озвучиваем здесь.
            self._say(self._matcher.confirmation(tag), user_level)
        elif routed is None:
            self._say(self._matcher.confirmation(tag), user_level)
            self._execute_command(tag)
        if set_branch:
            self._active_branch = self._primary.get(tag)  # None для команд без ветки
        self._history.record(tag)
        self._last_command = tag

    def _try_combo(self, text: str, user_level) -> bool:
        """Комбо «выключи блютуз и вай-фай»: разбить, распознать каждое, выполнить по порядку.

        ≥2 распознанных действия → True (выполнено). Иначе False (пусть обработает одиночный путь).
        Опущенный глагол второго действия («…и вай-фай») восстанавливаем из первой части."""
        parts = chains.split_combo(text)
        if not parts:
            return False
        first_words = parts[0].split()
        verb = first_words[0] if first_words else ""
        matched, unmatched = [], []
        for i, part in enumerate(parts):
            m = self._matcher.match(part)
            if m is None and verb and i > 0:
                m = self._matcher.match(f"{verb} {part}")  # подставить глагол первой части
            if m is not None:
                matched.append(m.tag)
            else:
                unmatched.append(part)
        if len(matched) < 2:
            return False  # не настоящее комбо — «и» было частью одной команды
        self.log.info("Комбо из %d действий: %s%s", len(matched), matched,
                      f" (не понял: {unmatched})" if unmatched else "")
        for tag in matched:
            # Каждое действие: своё подтверждение + исполнение + история. Ветку зададим по последнему.
            self._execute_matched(tag, user_level, set_branch=False)
        self._active_branch = self._primary.get(matched[-1])
        if unmatched:
            self._say(phrases.pick("chains.combo_partial", config.COMBO_PARTIAL), user_level)
        return True

    def _do_repeat(self, user_level) -> None:
        """«Повтори последнее» — выполнить последнюю команду заново (из истории)."""
        last = self._last_command or self._history.last_tag()
        if not last:
            self._say(phrases.pick("chains.repeat_nothing", config.REPEAT_NOTHING), user_level)
            return
        self.log.info("Повтор последней команды: %s", last)
        self._say(phrases.pick("chains.repeat_done", config.REPEAT_DONE), user_level)
        self._dispatch(last)
        self._history.record(last)

    def _do_undo(self, user_level) -> None:
        """«Отмени» — выполнить обратную команду (карта обратимость). Необратимо/нечего → в характере."""
        last = self._last_command or self._history.last_tag()
        if not last:
            self._say(phrases.pick("chains.undo_nothing", config.UNDO_NOTHING), user_level)
            return
        inv = chains.inverse_tag(self._commands, last)
        if not inv:
            self.log.info("Отмена невозможна: %s необратима", last)
            self._say(phrases.pick("chains.undo_irreversible", config.UNDO_IRREVERSIBLE), user_level)
            return
        self.log.info("Отмена %s → обратная %s", last, inv)
        self._say(phrases.pick("chains.undo_done", config.UNDO_DONE), user_level)
        self._dispatch(inv)
        self._history.record(inv)
        self._last_command = inv
        self._active_branch = self._primary.get(inv)

    # ------------------------------------------------------------------ #
    # Системное (ТЗ-7): перезагрузка Джарвиса + рабочие среды
    # ------------------------------------------------------------------ #
    def _match_tag(self, text: str):
        """Тег команды по тексту (для разбора содержимого среды). None — не распознано."""
        r = self._matcher.match(text)
        return r.tag if r else None

    def _do_restart(self, user_level) -> None:
        """Голосовая перезагрузка ВСЕХ сервисов Джарвиса (не ребут ноута). Анонс + уведомление +
        ОТКРЕПЛЁННЫЙ `jarvis restart` (своя cgroup через systemd-run → переживёт рестарт самого core).

        В тишине анонс уйдёт в уведомление (TTS сам решит). Статус после рестарта озвучит `jarvis
        restart` (он живёт отдельно). Сбой запуска → честно говорим/уведомляем, не висим."""
        self.log.info("Команда перезагрузки — анонс + откреплённый jarvis restart")
        self._say(phrases.pick("restart.announce", config.RESTART_ANNOUNCE), user_level)
        self.notify("Джарвис", "Перезагрузка сервисов…", urgency="normal")
        jarvis_bin = str(Path(sys.prefix) / "bin" / "jarvis")
        if not Path(jarvis_bin).exists():
            jarvis_bin = shutil.which("jarvis") or "jarvis"
        # systemd-run --user — отдельный transient-юнit (своя cgroup): переживёт restart jarvis-core.
        # Фолбэк (нет systemd-run) — setsid -f (открепление сессии). Старт в фоне, не блокируем поток.
        for launcher in (["systemd-run", "--user", "--collect", "--quiet", jarvis_bin, "restart"],
                         ["setsid", "-f", jarvis_bin, "restart"]):
            try:
                subprocess.Popen(launcher, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except FileNotFoundError:
                continue
            except Exception:
                self.log_exc(logging.ERROR, "Не удалось запустить перезагрузку (%s)", launcher[0])
                break
        self._say("Сэр, перезапуститься не удалось.", user_level)

    def _do_environment(self, text: str, user_level) -> None:
        """Открыть рабочую среду: разобрать (именованная/из речи) → новый вирт. стол + приложения.

        core только РЕЗОЛВИТ и публикует jarvis/environment; стол создаёт и приложения запускает
        os_agent («Руки»). Ничего не распознали → честно переспрашиваем."""
        name, apps = system.resolve_environment(text, self._match_tag)
        if not apps:
            self.log.info("Среда: не разобрал содержимое — %s", text)
            self._say(phrases.pick("environments.empty", config.ENV_EMPTY), user_level)
            return
        self.log.info("Открываю среду «%s» с приложениями %s", name, apps)
        self._say(phrases.pick("environments.open", config.ENV_OPEN), user_level)
        self.publish_json(contracts.TOPIC_ENVIRONMENT, {"desktop": name, "apps": apps},
                          qos=contracts.QOS_ENVIRONMENT)
        self._active_branch = None  # среда — не ветка продолжений

    def _do_volume(self, text: str, user_level) -> None:
        """Установить БАЗОВУЮ громкость голоса (ТЗ-10): проценты/доли. Адаптив работает поверх.
        Состояние переживает рестарт (voice_volume.set_base → logs/volume_state.json)."""
        level = voice_volume.parse_level(text)
        if level is None:
            return  # недостижимо: гейт is_volume_command требует распознанный уровень
        voice_volume.set_base(level)
        pct = round(level * 100)
        self.log.info("Базовая громкость голоса → %d%%", pct)
        self._say(phrases.pick("voice_volume.ack", config.VOLUME_ACK).replace("{процент}", str(pct)),
                  user_level)

    def _do_lamp_level(self, text: str) -> None:
        """Яркость ламп уровнем (заход «лампы»): «яркость ламп 50» / «лампы наполовину».

        core только парсит уровень и форвардит сервису ламп (с исходной фразой — адресация
        «яркость второй лампы 30»). Озвучивает РЕЗУЛЬТАТ сервис ламп (единый источник:
        знает успех/недоступность), как и остальные lamp-команды."""
        level = voice_volume.parse_level(text)
        if level is None:
            return  # недостижимо: гейт is_lamp_level_command требует распознанный уровень
        self.log.info("Яркость ламп голосом: %d%% — %s", round(level * 100), text)
        self.publish_json(contracts.TOPIC_LAMP,
                          {"действие": "яркость", "уровень": level, "текст": text},
                          qos=contracts.QOS_LAMP)

    def _say(self, text: str, user_level: float | None = None) -> None:
        """Реплика в jarvis/say с опц. громкостью речи пользователя (для адаптивной громкости TTS)."""
        payload = {"text": text, "source": self.name}
        if user_level is not None:
            payload["user_level"] = user_level
        self.publish_json(contracts.TOPIC_SAY, payload, qos=contracts.QOS_SAY)

    def _execute_command(self, tag: str) -> None:
        """Опубликовать команду на выполнение «Руками» (OS-агент) через jarvis/execute.

        Само исполнение — у OS-агента (allow-list, shell=False). Здесь только тег.
        """
        self.publish_json(
            contracts.TOPIC_EXECUTE,
            {"command_tag": tag},
            qos=contracts.QOS_EXECUTE,
        )

    def _local_info_answer(self, text: str) -> str | None:
        """Встроенные ответы: монетка, мировое время, локальное время, дата, громкость, загрузка, заряд."""
        low = text.lower()
        # Монетка («подкинь монетку», «орёл или решка»).
        coin = self._coin_answer(low)
        if coin is not None:
            return coin
        # Мировое время («сколько времени в <город>») — ПЕРЕД локальным (оно тоже ловит «сколько
        # времени», но без города это локальное время). detect_city → None, если города нет.
        city = worldtime.detect_city(text)
        if city:
            ans = worldtime.answer(city)
            if ans:
                return ans
        if any(k in low for k in _TIME_KEYS):
            now = datetime.now()
            return f"Сейчас {say_time(now.hour, now.minute)}, сэр."
        if any(k in low for k in _DATE_KEYS):
            return self._date_answer()
        if any(k in low for k in _VOLUME_KEYS):
            return self._volume_answer()
        if any(k in low for k in _LOAD_KEYS):
            return self._load_answer()
        if any(k in low for k in _BATTERY_KEYS):
            return self._battery_answer()
        return None

    def _coin_answer(self, low: str) -> str | None:
        """Монетка: случайно орёл/решка, фраза паком без повторов. Иначе None."""
        l = low.replace("ё", "е")
        if "монет" in l or ("орел" in l and "решк" in l):
            if random.random() < 0.5:
                return phrases.pick("coin.heads", config.COIN_HEADS)
            return phrases.pick("coin.tails", config.COIN_TAILS)
        return None

    def _date_answer(self) -> str:
        """Текущая дата словами (день/месяц/год прописью для естественной озвучки)."""
        now = datetime.now()
        return f"Сегодня {say_date(now.day, now.month, now.year)}, сэр."

    def _volume_answer(self) -> str:
        """Текущая громкость основного вывода (read-only проба wpctl)."""
        data = read_volume()
        if "ошибка" in data:
            self.log.warning("Не удалось снять громкость: %s", data["ошибка"])
            return "Не удалось узнать громкость, сэр."
        if data.get("выключен"):
            return "Звук сейчас выключен, сэр."
        return f"Громкость сейчас {say_percent(data.get('громкость_процент'))}, сэр."

    def _load_answer(self) -> str:
        """Загрузка процессора и памяти в характере (read-only из /proc)."""
        data = read_system_load()
        cpu = data.get("загрузка_cpu_процент")
        ram = data.get("память_занято_процент")
        parts = []
        if cpu is not None:
            parts.append(f"процессор на {say_percent(cpu)}")
        if ram is not None:
            parts.append(f"память на {say_percent(ram)}")
        if not parts:
            return "Не удалось снять загрузку системы, сэр."
        return "Загрузка: " + ", ".join(parts) + ", сэр."

    def _battery_answer(self) -> str:
        """Реальный заряд в характере Джарвиса (read-only-проба из sysinfo)."""
        data = read_battery()
        if data.get("батарея") == "не обнаружена":
            return "Батарея не обнаружена, сэр."
        if "ошибка" in data:
            self.log.warning("Не удалось снять показания батареи: %s", data["ошибка"])
            return "Не удалось снять показания батареи, сэр."
        capacity = data.get("процент")
        status = data.get("статус", "")
        if status == "заряжается":
            return f"Батарея на {say_percent(capacity)}, заряжается, сэр."
        if status == "заряжена полностью" or (isinstance(capacity, int) and capacity >= 99):
            return "Батарея заряжена полностью, сэр."
        return f"Батарея на {say_percent(capacity)}, сэр."


def main():
    CoreModule().run()


if __name__ == "__main__":
    main()
