"""Общие хелперы фичи «Напоминания о перерыве» — без тяжёлых зависимостей.

Здесь живёт распознавание стоп-фразы (по списку из settings.yaml) и выбор случайной
фразы без немедленного повтора. Модуль импортируют И core (чтобы не отвечать «не
разобрал» на стоп-фразу), И сервис activity_monitor (чтобы на неё ответить) — поэтому
он намеренно лёгкий: только stdlib + config (без MQTT/потоков/циклов импорта).
"""
import random
import re
from difflib import SequenceMatcher

from jarvis import config

# Нормализация: нижний регистр, ё→е, убрать пунктуацию, схлопнуть пробелы. Чтобы сверка
# стоп-фразы не зависела от запятых/регистра/«ё» в распознанном тексте.
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def _normalize(text: str) -> str:
    s = (text or "").lower().replace("ё", "е")
    s = _PUNCT.sub(" ", s)
    return _SPACES.sub(" ", s).strip()


def is_stop_phrase(text: str) -> bool:
    """Это стоп-фраза перерыва? Сверяем ВЕСЬ нормализованный текст с каждой стоп-фразой
    по difflib (полное совпадение, не вхождение) с ВЫСОКИМ порогом BREAK_STOP_THRESHOLD.

    Высокий порог + полное совпадение — намеренно: иначе стоп-фраза тихо проглотила бы
    похожую обычную команду (например «потом» ≈ «погромче»). Любой сбой → False
    (не мешаем обычной обработке)."""
    try:
        if not config.BREAKS_ENABLED:
            return False  # фича выключена → стоп-фразы не особенные (core обрабатывает обычно)
        norm = _normalize(text)
        if not norm:
            return False
        threshold = config.BREAK_STOP_THRESHOLD
        for phrase in config.BREAK_STOP_PHRASES:
            p = _normalize(phrase)
            if not p:
                continue
            # Точное совпадение нормализованных строк — самый частый случай, без difflib.
            if norm == p:
                return True
            if SequenceMatcher(None, norm, p).ratio() >= threshold:
                return True
    except Exception:
        return False
    return False


def pick_phrase(phrases: list, last_index: int = -1) -> tuple[int, str]:
    """Случайная фраза из списка, по возможности НЕ повторяя предыдущую. Возвращает
    (индекс, фраза). Пустой список → (-1, "")."""
    try:
        n = len(phrases)
        if n == 0:
            return -1, ""
        if n == 1:
            return 0, str(phrases[0])
        idx = random.randrange(n)
        if idx == last_index:
            idx = (idx + 1) % n
        return idx, str(phrases[idx])
    except Exception:
        return (-1, "") if not phrases else (0, str(phrases[0]))
