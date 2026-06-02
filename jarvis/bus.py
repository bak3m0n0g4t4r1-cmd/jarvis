"""Фреймворк шины данных «Джарвиса»: базовый класс JarvisModule.

Берёт на себя ВСЮ инфраструктурную рутину — подключение к MQTT,
автопереподключение, логирование, graceful shutdown и хелперы публикации.
Наследники описывают ТОЛЬКО свою логику: на что подписаться и что делать.

Добавить новый модуль = создать файл в jarvis/services/, унаследовать
JarvisModule, переопределить on_start() и вызвать run() в main().
"""
import json
import logging
import signal
import threading
from logging.handlers import RotatingFileHandler
from typing import Callable

import paho.mqtt.client as mqtt

from jarvis import config, contracts


class JarvisModule:
    """Базовый класс микросервиса «Джарвиса».

    Инкапсулирует MQTT, переподключение, логирование и корректное завершение.
    Наследник переопределяет on_start() (подписки/инициализация) и, при
    необходимости, on_stop() (освобождение ресурсов).
    """

    def __init__(self, name: str):
        self.name = name
        self.log = self._setup_logging(name)
        self._stop_event = threading.Event()
        # Реестр хендлеров: топик -> функция(dict)
        self._handlers: dict[str, Callable[[dict], None]] = {}

        # MQTT-клиент paho-mqtt 2.x (новый Callback API VERSION2)
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=name,
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        # Автопереподключение с экспоненциальным backoff (1с..60с)
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)

    # ------------------------------------------------------------------ #
    # Логирование
    # ------------------------------------------------------------------ #
    def _setup_logging(self, name: str) -> logging.Logger:
        config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            fmt = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
            )
            file_handler = RotatingFileHandler(
                config.LOGS_DIR / f"{name}.log",
                maxBytes=config.LOG_MAX_BYTES,
                backupCount=config.LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setFormatter(fmt)
            logger.addHandler(file_handler)

            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(fmt)
            logger.addHandler(stream_handler)
        return logger

    # ------------------------------------------------------------------ #
    # MQTT-колбэки (сигнатуры paho-mqtt 2.x / CallbackAPIVersion.VERSION2)
    # ------------------------------------------------------------------ #
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            self.log.error("Не удалось подключиться к MQTT: %s", reason_code)
            return
        self.log.info("Подключён к MQTT %s:%s", config.MQTT_HOST, config.MQTT_PORT)
        # Восстанавливаем подписки (важно после переподключения)
        for topic in self._handlers:
            client.subscribe(topic)
            self.log.info("Подписка на топик: %s", topic)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        # Переподключение выполняет сам paho (reconnect_delay_set + loop_forever)
        self.log.warning("Отключён от MQTT (код %s). Переподключение...", reason_code)

    def _on_message(self, client, userdata, message):
        handler = self._handlers.get(message.topic)
        if handler is None:
            return
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.log.error("Некорректный JSON в %s: %s", message.topic, exc)
            return
        try:
            handler(payload)
        except Exception:  # демон не падает ни при каких условиях
            self.log.exception("Ошибка в хендлере топика %s", message.topic)

    # ------------------------------------------------------------------ #
    # Хелперы для наследников
    # ------------------------------------------------------------------ #
    def subscribe(self, topic: str, handler: Callable[[dict], None]) -> None:
        """Подписаться на топик. handler получает уже распарсенный dict."""
        self._handlers[topic] = handler
        # Если уже подключены — подписываемся сразу (иначе — в _on_connect)
        if self.client.is_connected():
            self.client.subscribe(topic)
            self.log.info("Подписка на топик: %s", topic)

    def publish_json(self, topic: str, payload: dict, qos: int = 0) -> None:
        try:
            self.client.publish(
                topic, json.dumps(payload, ensure_ascii=False), qos=qos
            )
        except Exception:
            self.log.exception("Не удалось опубликовать в %s", topic)

    def say(self, text: str) -> None:
        """Публикация реплики в jarvis/say с проставлением source."""
        self.publish_json(
            contracts.TOPIC_SAY,
            {"text": text, "source": self.name},
            qos=contracts.QOS_SAY,
        )

    def set_state(self, state: str) -> None:
        """Публикация состояния в jarvis/state с проставлением source."""
        self.publish_json(
            contracts.TOPIC_STATE,
            {"state": state, "source": self.name},
            qos=contracts.QOS_STATE,
        )

    # ------------------------------------------------------------------ #
    # Жизненный цикл
    # ------------------------------------------------------------------ #
    def on_start(self) -> None:
        """Переопределяется наследником: здесь подписки и инициализация."""

    def on_stop(self) -> None:
        """Переопределяется наследником: освобождение ресурсов."""

    def _handle_signal(self, signum, frame):
        self.log.info("Получен сигнал %s, завершаюсь...", signum)
        self._stop_event.set()
        self.client.disconnect()

    def run(self) -> None:
        """Основной цикл: подключение, подписки, обработка сообщений."""
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        try:
            self.on_start()
        except Exception:
            self.log.exception("Ошибка в on_start()")

        # Подключаемся с ретраями — брокер мог ещё не подняться
        while not self._stop_event.is_set():
            try:
                self.client.connect(
                    config.MQTT_HOST, config.MQTT_PORT, config.MQTT_KEEPALIVE
                )
                break
            except Exception as exc:
                self.log.error("MQTT недоступен (%s), повтор через 5с", exc)
                if self._stop_event.wait(5):
                    return

        self.log.info("Модуль %s запущен", self.name)
        try:
            # loop_forever сам переподключается, пока не вызван disconnect()
            self.client.loop_forever()
        except Exception:
            self.log.exception("Сбой основного цикла")
        finally:
            try:
                self.on_stop()
            except Exception:
                self.log.exception("Ошибка в on_stop()")
            self.log.info("Модуль %s остановлен", self.name)
