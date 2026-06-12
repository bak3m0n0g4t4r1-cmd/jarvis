"""«Спутник» Джарвиса: приём событий телефона (Android-приложение) по MQTT + реакции.

Изолированный узел (наследник JarvisModule). Подписан на jarvis/phone/* (status/battery/call/
notification/presence), реагирует: озвучка входящего звонка + приглушение музыки (AudioEnv),
аккуратное сообщение о низком заряде (троттлинг), дубль уведомлений/SMS в системные уведомления
(notify.py), приветствие при возвращении домой, трекинг статуса (LWT) для панели `jarvis live`.

Команды телефону (find_phone) шлёт core напрямую в jarvis/phone/command (роутинг поля «телефон»).
Всё в try-except; телефон офлайн/битые сообщения → Джарвис работает штатно.
"""
import json
import logging
import os
import threading
import time

from jarvis import config, contracts, notify, phrases
from jarvis.audio_env import AudioEnv
from jarvis.bus import JarvisModule, run_service

_STATE_FILE = "phone_state.json"


class PhoneModule(JarvisModule):
    """«Спутник»: события телефона → реакции Джарвиса."""

    def __init__(self):
        super().__init__("jarvis-phone")
        self._env = AudioEnv()              # ducking музыки на время звонка (без замерных потоков)
        self._call_ducked = False
        self._duck_timer = None             # страховка: вернуть музыку, если "ended" не пришёл
        self._battery_low_active = False    # эпизод низкого заряда (для троттлинга)
        self._battery_alert_at = 0.0
        self._presence = None               # последнее присутствие (home/away)
        # Снимок для панели jarvis live.
        self._snapshot = {"status": "offline", "battery": None, "charging": None, "presence": None}

    def on_start(self):
        if not config.PHONE_ENABLED:
            self.log.info("Коннект с телефоном выключен (phone.enabled=false) — сервис простаивает")
            return
        self.subscribe(contracts.TOPIC_PHONE_STATUS, self.on_status)
        self.subscribe(contracts.TOPIC_PHONE_BATTERY, self.on_battery)
        self.subscribe(contracts.TOPIC_PHONE_CALL, self.on_call)
        self.subscribe(contracts.TOPIC_PHONE_NOTIFICATION, self.on_notification)
        self.subscribe(contracts.TOPIC_PHONE_PRESENCE, self.on_presence)
        self._write_state()

    # ------------------------------------------------------------------ #
    # Приём событий
    # ------------------------------------------------------------------ #
    def on_status(self, payload: dict):
        """Статус телефона (LWT online/offline)."""
        try:
            st = (payload or {}).get("status")
            if st in ("online", "offline"):
                self._snapshot["status"] = st
                self.log.info("Телефон %s", "на связи" if st == "online" else "офлайн")
                self._write_state()
        except Exception:
            self.log.debug("Сбой обработки статуса телефона", exc_info=True)

    def on_battery(self, payload: dict):
        """Заряд: при низком (и не на зарядке) — аккуратно сообщить, не чаще раза в N минут."""
        try:
            level = payload.get("level")
            charging = bool(payload.get("isCharging"))
            is_low = bool(payload.get("isLow"))
            self._snapshot["battery"] = level
            self._snapshot["charging"] = charging
            self._write_state()
            if not config.PHONE_BATTERY_ALERTS or charging or not is_low:
                if not is_low:
                    self._battery_low_active = False  # эпизод кончился → следующий низкий снова озвучим
                return
            now = time.monotonic()
            # Троттлинг: один раз на эпизод, повтор не чаще battery_repeat_minutes.
            if self._battery_low_active and (now - self._battery_alert_at) < config.PHONE_BATTERY_REPEAT_MIN * 60:
                return
            self._battery_low_active = True
            self._battery_alert_at = now
            text = phrases.pick("phone.battery", config.PHONE_BATTERY_LOW)
            self._say(text.replace("{уровень}", str(level if level is not None else "")))
        except Exception:
            self.log.debug("Сбой обработки заряда телефона", exc_info=True)

    def on_call(self, payload: dict):
        """Звонок: incoming → озвучить + приглушить музыку; started → держим; ended → вернуть музыку."""
        try:
            ctype = (payload or {}).get("type")
            if ctype == "incoming":
                if config.PHONE_ANNOUNCE_CALLS:
                    who = (payload.get("name") or "").strip() or (payload.get("number") or "").strip() or "неизвестный номер"
                    self._say(phrases.pick("phone.call", config.PHONE_CALL).replace("{кто}", who))
                if config.PHONE_DUCK_ON_CALL and not self._call_ducked:
                    try:
                        self._env.duck()
                        self._call_ducked = True
                        self._arm_duck_timeout()
                    except Exception:
                        self.log.debug("Не удалось приглушить музыку на звонок", exc_info=True)
            elif ctype == "ended":
                self._restore_call()
        except Exception:
            self.log.debug("Сбой обработки звонка", exc_info=True)

    def _arm_duck_timeout(self):
        """Страховка: вернуть музыку через call_duck_timeout_seconds, если "ended" потерялся
        (приложение упало/телефон ушёл из сети) — иначе музыка осталась бы тихой навсегда."""
        try:
            self._cancel_duck_timeout()
            timeout = float(config.PHONE_DUCK_TIMEOUT)
            if timeout <= 0:
                return
            t = threading.Timer(timeout, self._restore_call_by_timeout)
            t.daemon = True
            t.start()
            self._duck_timer = t
        except Exception:
            self.log.debug("Не удалось взвести страховку ducking", exc_info=True)

    def _cancel_duck_timeout(self):
        t = self._duck_timer
        self._duck_timer = None
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass

    def _restore_call_by_timeout(self):
        if self._call_ducked:
            self.log.warning("Событие ended не пришло за %.0fс — возвращаю музыку по страховке",
                             float(config.PHONE_DUCK_TIMEOUT))
        self._restore_call()

    def _restore_call(self):
        self._cancel_duck_timeout()
        if self._call_ducked:
            try:
                self._env.restore()
            except Exception:
                self.log.debug("Не удалось вернуть музыку после звонка", exc_info=True)
            self._call_ducked = False

    def on_notification(self, payload: dict):
        """Уведомление/SMS телефона → системное уведомление ноута (notify.py)."""
        try:
            if not config.PHONE_NOTIFY:
                return
            app = (payload.get("appName") or "Телефон").strip()
            title = (payload.get("title") or "").strip()
            content = (payload.get("content") or "").strip()
            body = " · ".join(p for p in (title, content) if p) or "(без текста)"
            notify.notify(f"📱 {app}", body, urgency="normal")
        except Exception:
            self.log.debug("Сбой обработки уведомления телефона", exc_info=True)

    def on_presence(self, payload: dict):
        """Присутствие: пришёл домой → приветствие (один раз на переход); ушёл → лог."""
        try:
            status = (payload or {}).get("status")
            if status not in ("home", "away") or status == self._presence:
                return
            prev = self._presence
            self._presence = status
            self._snapshot["presence"] = status
            self._write_state()
            if status == "home":
                self.log.info("Пользователь дома (ssid=%s)", payload.get("ssid"))
                if config.PHONE_PRESENCE_GREETING and prev is not None:
                    self._say(phrases.pick("phone.home", config.PHONE_HOME))
            else:
                self.log.info("Пользователь ушёл (ssid=%s)", payload.get("ssid"))
        except Exception:
            self.log.debug("Сбой обработки присутствия", exc_info=True)

    # ------------------------------------------------------------------ #
    def _say(self, text: str):
        if text:
            self.say(text)  # через TTS → учитывает режим тишины (ТЗ-6)

    def _write_state(self):
        """Снимок состояния телефона для панели jarvis live (logs/phone_state.json)."""
        try:
            from datetime import datetime
            data = dict(self._snapshot)
            data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            path = os.path.join(str(config.LOGS_DIR), _STATE_FILE)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            self.log.debug("Не удалось записать phone_state", exc_info=True)

    def on_stop(self):
        self._restore_call()


def main():
    run_service(PhoneModule, "jarvis-phone")


if __name__ == "__main__":
    main()
