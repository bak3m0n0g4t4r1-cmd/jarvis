"""Цепочки команд: ветки продолжений, разбор комбо-команд, обратимость, история, гейты повтор/отмена.

Чистая логика (stdlib). Используется core:
  • после команды активируется её ВЕТКА — продолжения этой ветки принимаются БЕЗ wake-word;
  • «и/потом/затем» разбивает фразу на несколько действий (комбо);
  • «повтори/отмени» работают по истории и обратным тегам (поле `обратимая` в commands.yaml).
Всё в try-except — сбой не роняет распознавание.
"""
import logging
import re
import time
from collections import deque

_log = logging.getLogger("jarvis-chains")

# Разделители комбо-команд (стоят между действиями; «и» — отдельным словом).
_COMBO_SPLIT = re.compile(r"\s+(?:и|потом|затем|плюс|а также|а потом|дальше)\s+")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def _norm(text):
    """Лёгкая нормализация (нижний регистр, ё→е, без пунктуации) — сохраняем слова для split."""
    s = (text or "").lower().replace("ё", "е")
    s = _PUNCT.sub(" ", s)
    return _SPACES.sub(" ", s).strip()


def build_branches(commands):
    """Из top-level `ветки:` карты commands.yaml → (branch→set(tags), tag→primary_branch).

    Списки членов могут пересекаться; primary-ветка тега — первая, где он встретился. Несуществующие
    теги отбрасываются. Сбой → пустые карты (продолжения просто выключены)."""
    branches, primary = {}, {}
    try:
        raw = (commands or {}).get("ветки") or {}
        for name, members in raw.items():
            tags = {t for t in (members or []) if t in commands}
            if tags:
                branches[name] = tags
            for t in (members or []):
                if t in commands and t not in primary:
                    primary[t] = name
    except Exception:
        _log.debug("build_branches сбой", exc_info=True)
    return branches, primary


def split_combo(text):
    """Разбить фразу на под-команды по «и/потом/затем/…». ≥2 непустые части → список, иначе None
    (одиночная команда: «и» могло быть частью одной фразы)."""
    try:
        s = _norm(text)
        if not s:
            return None
        parts = [p.strip() for p in _COMBO_SPLIT.split(s) if p.strip()]
        return parts if len(parts) >= 2 else None
    except Exception:
        _log.debug("split_combo сбой на %r", text, exc_info=True)
        return None


def inverse_tag(commands, tag):
    """Обратный тег из top-level карты `обратимость:` или None (необратимо/нет такого тега)."""
    try:
        inv = ((commands or {}).get("обратимость") or {}).get(tag)
        return inv if inv and inv in (commands or {}) else None
    except Exception:
        return None


# Гейты «повтори»/«отмени» (намеренные команды; стоп/scheduler-команды отсеяны раньше в core).
_REPEAT = ("повтори", "еще раз", "снова", "повторить", "то же самое", "ещё раз", "повторно")
_UNDO = ("отмени", "отмена", "откати", "откатить", "верни как было", "верни обратно",
         "верни назад", "отмени что сделал", "отмени последнее", "сделай как было")


def _has(s, words):
    return any(w in s for w in words)


def is_repeat(text):
    try:
        return _has(_norm(text), _REPEAT)
    except Exception:
        return False


def is_undo(text):
    try:
        return _has(_norm(text), _UNDO)
    except Exception:
        return False


class History:
    """Лёгкая история выполненных команд (in-memory deque). Для «повтори»/«отмени»/контекста ветки."""

    def __init__(self, maxlen=20):
        self._items = deque(maxlen=maxlen)

    def record(self, tag):
        try:
            self._items.append((tag, time.time()))
        except Exception:
            pass

    def last_tag(self):
        return self._items[-1][0] if self._items else None

    def __len__(self):
        return len(self._items)
