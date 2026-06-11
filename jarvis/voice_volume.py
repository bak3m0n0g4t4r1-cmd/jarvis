"""Базовая громкость голоса Джарвиса: голосовая установка (проценты/доли) + персистентный override.

«громкость 30» / «половина громкости» → базовая громкость 0.30 / 0.50. Адаптив (audio_env) работает
ПОВЕРХ этой отправной точки (шум/шёпот/ducking сдвигают её). Override хранится в logs/volume_state.json
(переживает рестарт); audio_env читает через get_base(). Лёгкий модуль (stdlib+config), всё в try-except.
"""
import json
import logging
import os
import re

from jarvis import config

_log = logging.getLogger("jarvis-voice-volume")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def _norm(text: str) -> str:
    s = (text or "").lower().replace("ё", "е")
    s = _PUNCT.sub(" ", s)
    return _SPACES.sub(" ", s).strip()


# Доли → проценты. ПОРЯДОК ВАЖЕН: «три/две четверти» проверяем ДО «четверть» (иначе «четверть» как
# подстрока перехватит). Без «пол» (двусмысленно: «полная»/«половина»).
_FRACTIONS = (
    ("три четверти", 75),
    ("две четверти", 50),
    ("полная", 100), ("полную", 100), ("на полную", 100), ("целая", 100), ("целую", 100),
    ("максимальная", 100), ("на максимум", 100), ("максимум", 100),
    ("половина", 50), ("половину", 50), ("наполовину", 50),
    ("четверть", 25), ("одна четверть", 25), ("четвертинка", 25),
)
# Числа словами (десятки 10..100) — на случай распознавания прописью.
_NUMWORDS = {
    "десять": 10, "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90, "сто": 100,
}


def parse_level(text: str):
    """Уровень громкости из фразы → доля 0.05..1.0 или None. Проценты (цифры/словами) или доли."""
    try:
        s = _norm(text)
        if not s:
            return None
        for frac, pct in _FRACTIONS:
            if frac in s:
                return pct / 100.0
        m = re.search(r"\b(\d{1,3})\b", s)        # «громкость 30», «до 50», «70 процентов»
        if m:
            v = int(m.group(1))
            return max(0.05, min(1.0, v / 100.0)) if v > 0 else None
        for word, v in _NUMWORDS.items():
            if word in s.split():
                return v / 100.0
        return None
    except Exception:
        return None


def is_volume_command(text: str) -> bool:
    """Команда установить громкость голоса? Требуем слово «громкост…» + распознанный уровень
    (чтобы не путать с системными «громче/тише» = volume_up/down). Фразы про ЛАМПЫ
    («громкость лампы 50») — НЕ сюда: это яркость ламп, её ловит гейт is_lamp_level_command
    (jarvis/lamp.py), который в core стоит раньше; здесь — второй ремень от коллизии."""
    try:
        s = _norm(text)
        return "громкост" in s and "ламп" not in s and parse_level(text) is not None
    except Exception:
        return False


def _state_file() -> str:
    return os.path.join(str(config.LOGS_DIR), "volume_state.json")


def get_base() -> float:
    """Базовая громкость голоса: override из файла (0..1), иначе config.VOICE_VOLUME_BASE."""
    try:
        f = _state_file()
        if os.path.exists(f):
            with open(f, encoding="utf-8") as fh:
                v = json.load(fh).get("base")
                if isinstance(v, (int, float)) and 0 < v <= 1:
                    return float(v)
    except Exception:
        pass
    return config.VOICE_VOLUME_BASE


def set_base(level: float) -> bool:
    """Записать базовую громкость (override, переживает рестарт). True — успех."""
    try:
        f = _state_file()
        tmp = f + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"base": round(max(0.05, min(1.0, float(level))), 3)}, fh)
        os.replace(tmp, f)
        return True
    except Exception:
        _log.debug("set_base сбой", exc_info=True)
        return False
