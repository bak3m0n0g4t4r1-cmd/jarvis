"""Фреймворк шины данных «Джарвиса»: базовый класс JarvisModule.

Берёт на себя ВСЮ инфраструктурную рутину — подключение к MQTT,
автопереподключение, логирование, graceful shutdown и хелперы публикации.
Наследники описывают ТОЛЬКО свою логику: на что подписаться и что делать.

Добавить новый модуль = создать файл в jarvis/services/, унаследовать
JarvisModule, переопределить on_start() и вызвать run() в main().
"""
import json
import logging
import os
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from typing import Callable

import paho.mqtt.client as mqtt

from jarvis import config, contracts, resilience


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
        # Код выхода процесса: 0 — штатно; ненулевой ставит request_restart() при
        # фатальном сбое, чтобы systemd гарантированно поднял сервис заново.
        self._exit_code = 0
        # Был ли уже хоть один успешный коннект — чтобы отличать первое подключение
        # от ВОССТАНОВЛЕНИЯ после разрыва (нарратив в логах: «восстановлена»).
        self._was_connected = False
        self._start_time = time.monotonic()
        self._heartbeat_thread: threading.Thread | None = None
        # Реестр хендлеров: топик -> функция(dict)
        self._handlers: dict[str, Callable[[dict], None]] = {}

        # client_id = имя-сервиса + PID: каждый экземпляр уникален. Это уже исключает
        # конфликт id между старым (завис) и новым (systemd) процессами. Диагностика по
        # факту (journalctl mosquitto + suspend): оставшиеся «Unspecified error» — это
        # перезапуски брокера и спящий режим, общие для всех сервисов, а НЕ конфликт id.
        self._client_id = f"{name}-{os.getpid()}"
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self._client_id,
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        # Автопереподключение с экспоненциальным backoff. Потолок намеренно мал
        # (config, дефолт 15с): локальный брокер поднимается быстро, и после suspend/
        # перезапуска брокера Джарвис восстанавливается за ~15с, а не за ~2 мин.
        self.client.reconnect_delay_set(
            min_delay=config.MQTT_RECONNECT_MIN,
            max_delay=config.MQTT_RECONNECT_MAX,
        )

    # ------------------------------------------------------------------ #
    # Логирование
    # ------------------------------------------------------------------ #
    def _setup_logging(self, name: str) -> logging.Logger:
        config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(name)
        # Уровень из конфига (дефолт INFO). DEBUG раскрывает стек-трассы ожидаемых
        # сбоёв, которые мы намеренно держим на DEBUG, чтобы не засорять обычный лог.
        level = getattr(logging, config.LOG_LEVEL, logging.INFO)
        logger.setLevel(level if isinstance(level, int) else logging.INFO)
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

    def log_exc(self, level: int, msg: str, *args) -> None:
        """Человеческое объяснение проблемы + трасса для отладки (единый паттерн).

        Человеческая строка пишется на `level`. Стек-трасса: на том же уровне для
        ERROR/CRITICAL (трассу настоящей ошибки нельзя терять даже при LOG_LEVEL=INFO),
        а для WARNING и ниже — на DEBUG (частые ожидаемые сбои не заваливают лог
        трассами, но видны при JARVIS_LOG_LEVEL=DEBUG). Звать из блока except.
        """
        self.log.log(level, msg, *args, exc_info=(level >= logging.ERROR))
        if level < logging.ERROR:
            self.log.debug("  └ трасса предыдущего сообщения (детали для отладки)",
                           exc_info=True)

    # ------------------------------------------------------------------ #
    # MQTT-колбэки (сигнатуры paho-mqtt 2.x / CallbackAPIVersion.VERSION2)
    # ------------------------------------------------------------------ #
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        # Колбэк paho: любое исключение здесь не должно ломать клиент молча.
        try:
            if reason_code != 0:
                self.log.error(
                    "Брокер отклонил подключение (%s) — paho повторит автоматически",
                    reason_code,
                )
                return
            # Восстанавливаем подписки: при clean session брокер их не помнит, поэтому
            # подписываемся ЗАНОВО на каждый топик и при первом коннекте, и после разрыва.
            for topic in self._handlers:
                client.subscribe(topic)
            if self._was_connected:
                # Это ВОССТАНОВЛЕНИЕ после разрыва — внятно сообщаем, что связь и подписки живы.
                self.log.info(
                    "Связь с шиной восстановлена (%s:%s), подписки активны: %d топиков",
                    config.MQTT_HOST, config.MQTT_PORT, len(self._handlers),
                )
                for topic in self._handlers:
                    self.log.debug("Переподписка на топик: %s", topic)
            else:
                self.log.info("Подключён к шине MQTT %s:%s", config.MQTT_HOST, config.MQTT_PORT)
                for topic in self._handlers:
                    self.log.info("Подписка на топик: %s", topic)
            self._was_connected = True
        except Exception:
            self.log_exc(logging.ERROR, "Сбой в обработчике подключения MQTT")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        # Колбэк paho: исключение внутри не должно ломать клиент. Переподключение
        # выполняет сам paho (reconnect_delay_set + loop_start) — мы только сообщаем.
        try:
            clean, human = resilience.mqtt_disconnect_reason(reason_code)
            if clean or self._stop_event.is_set():
                # Наш собственный disconnect (shutdown) — это норма, не тревога.
                self.log.info("Штатное отключение от шины (%s)", human)
            else:
                # Неожиданный разрыв — фраза «Связь с шиной потеряна» ищется в doctor.
                self.log.warning(
                    "Связь с шиной потеряна: %s — переподключаюсь автоматически", human
                )
        except Exception:
            self.log_exc(logging.ERROR, "Сбой в обработчике отключения MQTT")

    def _on_message(self, client, userdata, message):
        handler = self._handlers.get(message.topic)
        if handler is None:
            return
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.log.warning(
                "Пришло некорректное сообщение в %s (%s) — пропускаю, слушаю дальше",
                message.topic, exc,
            )
            return
        try:
            handler(payload)
        except Exception:  # демон не падает ни при каких условиях
            self.log_exc(
                logging.ERROR,
                "Сбой в обработчике топика %s — сообщение пропущено, сервис работает дальше",
                message.topic,
            )

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

    def notify(self, title: str, body: str, *, module: str | None = None,
               urgency: str = "normal", replace_id: int = 0) -> int:
        """Показать системное уведомление (D-Bus через jarvis.notify). Возвращает id или 0.

        Единый вход для всех сервисов: режим тишины (дубль речи), проблемы звука, сбои.
        Сбой уведомления не роняет вызывающего (всё в try-except внутри notify)."""
        try:
            from jarvis import notify as _notify
            return _notify.notify(title, body, module=module, urgency=urgency, replace_id=replace_id)
        except Exception:
            self.log.debug("Не удалось показать уведомление", exc_info=True)
            return 0

    def notify_failure(self, human: str, reason: str | None = None) -> None:
        """ЭЛЕГАНТНОЕ уведомление о сбое: человеческий текст (БЕЗ сырых трейсов) + кнопка «Открыть
        логи» → kitty с логом ИМЕННО этого модуля. reason — короткое уточнение (тоже человеческое)."""
        try:
            title = getattr(config, "NOTIFY_FAILURE_TITLE", "Джарвис — неполадка")
            body = f"{human}\n{reason}" if reason else human
            self.notify(title, body, module=self.name, urgency="critical")
        except Exception:
            self.log.debug("Не удалось показать уведомление о сбое", exc_info=True)

    # ------------------------------------------------------------------ #
    # Жизненный цикл
    # ------------------------------------------------------------------ #
    def on_start(self) -> None:
        """Переопределяется наследником: здесь подписки и инициализация."""

    def on_stop(self) -> None:
        """Переопределяется наследником: освобождение ресурсов."""

    def request_restart(self, reason: str) -> None:
        """Сигнал о необходимости рестарта при ФАТАЛЬНОМ сбое (обычно из фонового потока).

        Вместо тихой смерти потока: логируем CRITICAL с причиной, ставим ненулевой код
        выхода и взводим флаг остановки. run() штатно освободит ресурсы (on_stop) и выйдет
        с этим кодом — systemd (Restart=always) поднимет сервис заново.
        """
        self.log.critical(
            "%s — сервис не может продолжать, сигналю systemd о перезапуске", reason
        )
        # Элегантное уведомление пользователю (человеческий текст + кнопка к логу модуля).
        self.notify_failure(
            f"Модуль «{self.name}» перезапускается из-за неполадки.", reason)
        self._exit_code = 1
        self._stop_event.set()

    def _heartbeat_loop(self) -> None:
        """Раз в HEARTBEAT_INTERVAL пишет в лог, что сервис жив (по логам видно, что он
        не висит молча). Только в лог — на шину ничего не публикуем. Гаснет по stop_event."""
        interval = config.HEARTBEAT_INTERVAL
        if interval <= 0:
            return
        # wait() вернёт True сразу при остановке — heartbeat не задерживает выход.
        while not self._stop_event.wait(interval):
            try:
                uptime = int(time.monotonic() - self._start_time)
                up = f"{uptime // 60} мин" if uptime >= 60 else f"{uptime} с"
                mqtt_ok = "подключён" if self.client.is_connected() else "нет связи"
                self.log.info("Сервис жив: uptime %s, MQTT: %s", up, mqtt_ok)
            except Exception:
                self.log.debug("Не удалось записать heartbeat", exc_info=True)

    def _handle_signal(self, signum, frame):
        self.log.info("Получен сигнал %s, завершаюсь...", signum)
        # Только взводим флаг. Закрытие MQTT и освобождение ресурсов наследника
        # делаем в run() в правильном порядке (сначала on_stop, потом disconnect),
        # чтобы аудио-потоки закрылись штатно до выхода интерпретатора.
        self._stop_event.set()

    def run(self) -> None:
        """Основной цикл: подключение, подписки, обработка сообщений.

        Сеть MQTT крутится в фоновом потоке (loop_start), главный поток ждёт
        сигнала на _stop_event. При завершении сначала вызываем on_stop()
        наследника (освобождение аудио/ресурсов), и только потом гасим MQTT.
        """
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        try:
            self.on_start()
        except Exception:
            self.log_exc(logging.ERROR, "Ошибка инициализации модуля %s (on_start)", self.name)

        # Подключаемся с ретраями — брокер мог ещё не подняться
        connected = False
        while not self._stop_event.is_set():
            try:
                self.client.connect(
                    config.MQTT_HOST, config.MQTT_PORT, config.MQTT_KEEPALIVE
                )
                connected = True
                break
            except Exception as exc:
                self.log.warning(
                    "Шина MQTT недоступна (%s) — повтор через 5с",
                    resilience.classify_mqtt_error(exc),
                )
                self.log.debug("Трасса подключения к MQTT", exc_info=True)
                if self._stop_event.wait(5):
                    break

        if connected:
            # Сетевой цикл — в фоновом потоке (сам переподключается по backoff).
            # Главный поток просто ждёт сигнала: так on_stop() гарантированно
            # отработает ДО loop_stop/disconnect.
            self.client.loop_start()
            if config.HEARTBEAT_INTERVAL > 0:
                self._heartbeat_thread = threading.Thread(
                    target=self._heartbeat_loop, daemon=True,
                    name=f"{self.name}-heartbeat",
                )
                self._heartbeat_thread.start()
            self.log.info("Модуль %s запущен", self.name)
            try:
                while not self._stop_event.is_set():
                    self._stop_event.wait(1.0)
            except Exception:
                self.log_exc(logging.ERROR, "Сбой основного цикла модуля %s", self.name)

        # Завершение: сперва ресурсы наследника (аудио), потом MQTT. Heartbeat-поток
        # daemon и сам выйдет по stop_event — дожидаемся коротко, чтобы не висел в логе.
        try:
            self.on_stop()
        except Exception:
            self.log_exc(logging.ERROR, "Ошибка в on_stop() модуля %s", self.name)
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=1.0)
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            self.log_exc(logging.ERROR, "Ошибка при закрытии MQTT")
        self.log.info("Модуль %s остановлен", self.name)
        # Фатальный сбой (request_restart) → ненулевой код, чтобы systemd поднял заново.
        if self._exit_code:
            sys.exit(self._exit_code)
