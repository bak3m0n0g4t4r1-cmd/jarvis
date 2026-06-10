"""Системные голосовые действия (ТЗ-7): перезагрузка Джарвиса и рабочие среды (вирт. столы KDE).

Лёгкие гейты/парсеры (stdlib + config + reuse chains). Без MQTT/потоков/тяжёлых зависимостей.
Импортирует core: детект «перезагрузись» (только wake) и «открой … среду» + резолв в (стол, теги).
Разбор содержимого среды использует ПЕРЕДАННУЮ функцию match (matcher core) — без цикла импорта.
"""
import logging
import re

from jarvis import config

_log = logging.getLogger("jarvis-system")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")

# Слова, не относящиеся к СОДЕРЖИМОМУ среды (глаголы/триггеры/предлоги) — режем перед разбором.
_ENV_STOPWORDS = {
    "открой", "открыть", "создай", "создать", "запусти", "запустить", "сделай", "подними",
    "новую", "новый", "новое", "новым", "среду", "среда", "среде", "средой", "сред",
    "пространство", "окружение", "рабочую", "рабочий", "рабочее", "рабочем",
    "с", "со", "и", "включи", "мне", "пожалуйста",
}


def _norm(text: str) -> str:
    s = (text or "").lower().replace("ё", "е")
    s = _PUNCT.sub(" ", s)
    return _SPACES.sub(" ", s).strip()


def is_restart_command(text: str) -> bool:
    """Команда перезагрузить ВСЕ сервисы Джарвиса (НЕ ребут ноута)? Совпадение с рефлексивными
    фразами из config (подстрока): «-сь» не ловит «перезагрузи браузер». Вызывать ТОЛЬКО на wake-пути."""
    try:
        s = _norm(text)
        if not s:
            return False
        return any(pn and pn in s for pn in (_norm(p) for p in config.RESTART_PHRASES))
    except Exception:
        return False


def is_environment_command(text: str) -> bool:
    """Команда открыть рабочую среду? Нужны И слово-триггер среды, И глагол открытия/создания."""
    try:
        s = _norm(text)
        if not s:
            return False
        has_trigger = any(_norm(t) in s for t in config.ENV_TRIGGERS)
        if not has_trigger:
            return False
        verbs = ("открой", "открыть", "создай", "создать", "запусти", "сделай", "подними")
        return any(v in s for v in verbs)
    except Exception:
        return False


def _strip_env_words(s: str) -> str:
    """Оставить только СОДЕРЖИМОЕ среды: убрать глаголы/триггеры/предлоги, сохранить «и» для split."""
    keep = []
    for w in s.split():
        if w == "и":          # разделитель комбо — оставляем
            keep.append(w)
        elif w not in _ENV_STOPWORDS:
            keep.append(w)
    return " ".join(keep).strip()


def resolve_environment(text, match):
    """Разобрать команду среды → (имя_стола, [теги_приложений]).

    1) ИМЕНОВАННАЯ среда (config.ENVIRONMENTS): имя встречается во фразе → её desktop+apps.
    2) ИЗ РЕЧИ: вырезать служебные слова → split по «и» (chains.split_combo) → match каждого
       фрагмента в тег (match — функция matcher из core: str→tag|None). Нераспознанные опускаем.
    `match` принимает строку и возвращает тег или None."""
    try:
        s = _norm(text)
        # 1) Именованные среды (стем-матч: «рабочую/рабочей» ловит имя «рабочая»).
        words = s.split()
        for name, spec in (config.ENVIRONMENTS or {}).items():
            if not isinstance(spec, dict):
                continue
            nm = _norm(name)
            stem = nm[:5]
            if nm in s or (len(stem) >= 4 and any(w.startswith(stem) for w in words)):
                apps = [t for t in (spec.get("apps") or []) if t]
                return (spec.get("desktop") or name, apps)
        # 2) Разбор из речи.
        from jarvis import chains
        content = _strip_env_words(s)
        parts = chains.split_combo(content) or ([content] if content else [])
        tags = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            tag = match(part)
            if tag and tag not in tags:
                tags.append(tag)
        return (config.ENV_DESKTOP_PREFIX, tags)
    except Exception:
        _log.debug("resolve_environment сбой на %r", text, exc_info=True)
        return (config.ENV_DESKTOP_PREFIX, [])
