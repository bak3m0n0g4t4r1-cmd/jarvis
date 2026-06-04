"""Общие средства отказоустойчивости «Джарвиса»: диагнозы сбоёв и ретраи.

Сводим любой сбой к КОРОТКОМУ человекочитаемому диагнозу (что/почему), чтобы и в
логах сервисов, и в `jarvis doctor` причина читалась одинаково и по-человечески, а не
«сервер недоступен». Здесь же — общий ретрай с человеческим логом.

ВАЖНО: модуль НЕ импортирует bus/сервисы — только config/stdlib. Иначе получится цикл
(bus → resilience → bus). Классификатор Gemini живёт в casual.py (там он нужен самому
бэкенду и тоже не должен тянуть лишнего); при необходимости импортируется оттуда.

Импорты сторонних либ (ollama/httpx) — ленивые и в try-except: сам классификатор не
должен падать, даже если пакета нет.
"""
import logging
import socket
import time
from typing import Callable, Optional


# --------------------------------------------------------------------------- #
# MQTT: человеческая расшифровка причины разрыва
# --------------------------------------------------------------------------- #
def mqtt_disconnect_reason(reason_code) -> tuple[bool, str]:
    """Расшифровать reason_code разрыва MQTT (paho 2.x) в (штатный?, человеческий текст).

    paho передаёт в on_disconnect объект ReasonCode (str() → «Normal disconnection»,
    «Unspecified error» и т.п.). Штатным считаем наш собственный disconnect (код 0):
    его НЕ нужно подавать как тревогу. «Unspecified error» (0x80) на этой машине —
    падение брокера/спящий режим (диагностировано по journalctl mosquitto + suspend),
    а не конфликт client_id (он уникален name-PID). Сводим к понятной фразе.
    """
    text = str(reason_code).strip() if reason_code is not None else ""
    low = text.lower()
    value = getattr(reason_code, "value", None)

    if value == 0 or "normal disconnection" in low or "success" in low:
        return True, "штатное отключение"
    if "unspecified" in low:
        return False, "брокер недоступен (перезапуск, спящий режим или нагрузка)"
    if "keep alive" in low or "keepalive" in low:
        return False, "брокер не дождался keepalive (перегрузка или подвисание процесса)"
    if not text:
        return False, "соединение разорвано неожиданно"
    return False, f"неожиданный разрыв ({text})"


# --------------------------------------------------------------------------- #
# Классификаторы сбоёв (перенесены из doctor.py — единый источник диагнозов)
# --------------------------------------------------------------------------- #
def classify_ollama_error(exc: Exception) -> str:
    """Точный диагноз сбоя Ollama (сверено на ollama 0.6.2)."""
    try:
        import ollama

        if isinstance(exc, ollama.ResponseError):
            code = getattr(exc, "status_code", 0) or 0
            if code == 404:
                return "модель не найдена на сервере (404)"
            if code == 400:
                return "неверный запрос или имя модели (400)"
            if code and code >= 500:
                return f"внутренняя ошибка сервера Ollama ({code})"
            return f"ошибка API Ollama ({code or '—'})"
    except Exception:
        pass
    try:
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            return "превышено время ожидания Ollama (модель грузится или висит)"
    except Exception:
        pass
    # ollama 0.6.x при недоступном сервере бросает builtins.ConnectionError.
    if isinstance(exc, (ConnectionRefusedError, ConnectionError)):
        return "сервер Ollama не запущен (нет соединения)"
    if isinstance(exc, socket.gaierror):
        return "не разрешается адрес Ollama (DNS) — проверьте JARVIS_OLLAMA_HOST"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "Ollama не ответил вовремя (таймаут)"
    return f"непредвиденная ошибка Ollama ({type(exc).__name__}: {exc})"


def classify_mqtt_error(exc: Exception) -> str:
    """Точный диагноз сбоя подключения к MQTT-брокеру (сверено на paho-mqtt 2.1.0)."""
    if isinstance(exc, ConnectionRefusedError):
        return "брокер не запущен (соединение отклонено)"
    if isinstance(exc, socket.gaierror):
        return "не разрешается адрес брокера (DNS) — проверьте JARVIS_MQTT_HOST"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "брокер не ответил вовремя (таймаут)"
    if isinstance(exc, OSError):
        return f"сетевая ошибка соединения с брокером ({exc})"
    return f"непредвиденная ошибка MQTT ({type(exc).__name__}: {exc})"


# --------------------------------------------------------------------------- #
# Общий ретрай с человеческим логом
# --------------------------------------------------------------------------- #
def with_retry(
    call: Callable,
    *,
    attempts: int,
    delay: float,
    log: logging.Logger,
    what: str,
    classify: Optional[Callable[[Exception], str]] = None,
):
    """Выполнить call() с ретраями и человеческим логом; вернуть результат call().

    attempts — СКОЛЬКО ВСЕГО попыток (1 = без ретрая). Между попытками: WARNING
    «<what>: попытка k из N не удалась (<диагноз>) — повтор через Dс» + трасса на DEBUG,
    затем backoff (delay удваивается). Исключение ПОСЛЕДНЕЙ попытки пробрасывается наружу —
    вызывающий сам решает, как деградировать (и логирует финальный диагноз по-человечески).
    """
    attempts = max(1, attempts)
    last_exc: Optional[Exception] = None
    for i in range(attempts):
        try:
            return call()
        except Exception as exc:
            last_exc = exc
            if i + 1 < attempts:
                diag = classify(exc) if classify else f"{type(exc).__name__}: {exc}"
                wait = delay * (2 ** i)
                log.warning("%s: попытка %d из %d не удалась (%s) — повтор через %.1fс",
                            what, i + 1, attempts, diag, wait)
                log.debug("Трасса сбоя «%s», попытка %d", what, i + 1, exc_info=True)
                time.sleep(wait)
    # Все попытки исчерпаны — пробрасываем последнюю ошибку.
    raise last_exc
