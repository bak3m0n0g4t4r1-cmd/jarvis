"""Режим тишины: гейт команд «без звука»/«включи звук» + ПЕРСИСТЕНТНОЕ состояние.

Команды ловятся из эфира ВСЕГДА (даже без wake-word, как стоп-фраза перерыва) → гейт лёгкий
(stdlib+config, без MQTT/потоков/циклов импорта). Состояние пишется в файл и ПЕРЕЖИВАЕТ
перезапуск: читают и core (отвечает на команду), и TTS (решает — озвучить или в уведомление).
В тишине обычные фразы уходят в уведомления; критическое (будильники/таймеры/напоминания —
помечены min_volume) звучит голосом всегда. Всё в try-except.
"""
import json
import logging
import os
import re

from jarvis import config

_log = logging.getLogger("jarvis-silence")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Нижний регистр, ё→е, без пунктуации, схлопнуть пробелы (как в breaks)."""
    s = (text or "").lower().replace("ё", "е")
    s = _PUNCT.sub(" ", s)
    return _SPACES.sub(" ", s).strip()


def _match_any(text: str, phrases) -> bool:
    """ВЕСЬ нормализованный текст ТОЧНО равен одной из фраз. Точное совпадение — НАМЕРЕННО, не
    difflib: «включи звук» (выход из тишины) и «выключи звук» (mute) отличаются на 1 букву → difflib
    их путает. Списки фраз достаточно полны; STT обычно даёт команду чисто."""
    norm = _normalize(text)
    if not norm:
        return False
    return any(_normalize(p) == norm for p in (phrases or ()))


def is_silence_on(text: str) -> bool:
    """Команда включить режим тишины («без звука», «режим тишины», «помолчи»…)?"""
    try:
        if not config.SILENCE_ENABLED:
            return False
        return _match_any(text, config.SILENCE_ON_PHRASES)
    except Exception:
        return False


def is_silence_off(text: str) -> bool:
    """Команда выключить режим тишины («включи звук», «можешь говорить»…)?"""
    try:
        if not config.SILENCE_ENABLED:
            return False
        return _match_any(text, config.SILENCE_OFF_PHRASES)
    except Exception:
        return False


def _state_file() -> str:
    return str(config.LOGS_DIR / "silence_state.json")


def is_silent() -> bool:
    """Текущее состояние режима тишины (из файла; нет файла/сбой → False)."""
    try:
        f = _state_file()
        if not os.path.exists(f):
            return False
        with open(f, encoding="utf-8") as fh:
            return bool(json.load(fh).get("silent", False))
    except Exception:
        return False


def set_silent(value: bool) -> bool:
    """Записать состояние режима тишины (атомарно, переживает перезапуск). True — успех."""
    try:
        f = _state_file()
        tmp = f + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"silent": bool(value)}, fh)
        os.replace(tmp, f)
        return True
    except Exception:
        _log.debug("Не удалось записать состояние тишины", exc_info=True)
        return False
