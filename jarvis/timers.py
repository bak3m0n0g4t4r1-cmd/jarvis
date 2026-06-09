"""Таймеры (обратный отсчёт) и секундомеры (счёт вверх) — чистая логика распознавания/парсинга.

Без MQTT/потоков (stdlib + config + переиспользование `alarms`): импортируют core (гейт — молчать)
и scheduler (полная обработка). Распознавание — слой ПРАВИЛ (ключевые слова + difflib, как в alarms):
антонимы поставь/удали/останови эмбеддинги путают. Парсер длительности — слова+цифры, дроби
пол/полтора. Хранение/срабатывание — в сервисе scheduler (тот же schedule.yaml).

Термины: СЕКУНДОМЕР считает ВВЕРХ от нуля; ТАЙМЕР — обратный отсчёт, срабатывает в конце.
"""
import logging
import re

from jarvis import config
from jarvis.alarms import (_LABEL_MARKERS, _ONES, _TENS, _norm, _take_label_words,
                           is_alarm_command)

_log = logging.getLogger("jarvis-timers")

# Числа словами для длительности: единицы/подростки/десятки (+ «одну» — винит.).
_NUM = {**_ONES, "одну": 1}
_TENS_MAP = dict(_TENS)
# Единицы длительности → секунды (все падежи/формы, что выдаёт STT).
_UNITS = {
    "час": 3600, "часа": 3600, "часов": 3600,
    "минута": 60, "минуту": 60, "минуты": 60, "минут": 60,
    "секунда": 1, "секунду": 1, "секунды": 1, "секунд": 1, "сек": 1,
}


def _read_num(tokens, i):
    """Прочитать число с позиции i (десяток+единица = составное «двадцать пять»; цифры)."""
    if i >= len(tokens):
        return None, 0
    t = tokens[i]
    if t in _TENS_MAP:
        base = _TENS_MAP[t]
        if i + 1 < len(tokens) and tokens[i + 1] in _NUM and _NUM[tokens[i + 1]] < 10:
            return base + _NUM[tokens[i + 1]], 2
        return base, 1
    if t in _NUM:
        return _NUM[t], 1
    if re.fullmatch(r"\d+", t):
        return int(t), 1
    return None, 0


def _unit_at(tokens, i):
    """Если токен i — единица длительности, вернуть её секунды, иначе None."""
    if i < len(tokens) and tokens[i] in _UNITS:
        return _UNITS[tokens[i]]
    return None


def parse_duration(text: str):
    """Длительность из русской фразы → СЕКУНДЫ (int) или None.

    Поддержано: «5 минут», «пять минут», «полторы минуты»=90, «30 секунд», «час двадцать»=4800,
    «полтора часа»=5400, «два часа тридцать минут», «на минуту»=60, «полчаса»=1800, «полминуты»=30."""
    try:
        s = _norm(text)
        if not s:
            return None
        toks = s.split()
        total = 0.0
        found = False
        pending = None  # «голое» число без единицы (напр. минуты в «час двадцать»)
        i = 0
        while i < len(toks):
            t = toks[i]
            if t == "полчаса":
                total += 1800; found = True; i += 1; continue
            if t == "полминуты":
                total += 30; found = True; i += 1; continue
            if t in ("полтора", "полторы"):
                u = _unit_at(toks, i + 1)
                if u:
                    total += 1.5 * u; found = True; i += 2; continue
                i += 1; continue
            num, used = _read_num(toks, i)
            if num is not None:
                u = _unit_at(toks, i + used)
                if u is not None:
                    total += num * u; found = True; i += used + 1; continue
                pending = num; i += used; continue  # число без единицы — запомним
            u = _unit_at(toks, i)
            if u is not None:  # голая единица без числа = 1 (час/минута/секунда)
                total += u; found = True; i += 1; continue
            i += 1
        if pending is not None:
            # Остаточное число без единицы → минуты («час двадцать», «таймер на пять»).
            total += pending * 60; found = True
        if not found:
            return None
        total = int(round(total))
        return total if total > 0 else None
    except Exception:
        _log.debug("parse_duration сбой на %r", text, exc_info=True)
        return None


# --- Гейты (для core — молчать; для scheduler — маршрутизация) --------------- #
def is_timer_command(text: str) -> bool:
    try:
        if not config.ALARMS_ENABLED:
            return False
        s = _norm(text)
        return bool(s) and ("таймер" in s or "осталось" in s)
    except Exception:
        return False


def is_stopwatch_command(text: str) -> bool:
    try:
        if not config.ALARMS_ENABLED:
            return False
        s = _norm(text)
        if not s:
            return False
        return ("секундомер" in s or "засек" in s or "засеч" in s or "прошло" in s)
    except Exception:
        return False


def is_scheduler_command(text: str) -> bool:
    """Любая команда планировщика (будильник/таймер/секундомер) — core на ней молчит."""
    try:
        return is_alarm_command(text) or is_timer_command(text) or is_stopwatch_command(text)
    except Exception:
        return False


# --- Извлечение метки и разбор команд --------------------------------------- #
_V_MOVE = ("перенес", "перенос", "измен", "поменя", "сдвин", "переставь", "перестав")
_V_CANCEL = ("отмен", "удал", "убер", "сбрось", "сброс", "обнул", "сними", "сотри")
_V_SET = ("постав", "завед", "заведи", "установ", "запуст", "создай", "создать", "вруб")
_V_STOP = ("останов", "приостанов", "стоп", "заверши")
_V_START = ("засек", "засеч", "запуст", "начни", "вруб", "старт")


def _has(s, stems):
    return any(st in s for st in stems)


def _extract_label(s, noun):
    """Метка из фразы: явные маркеры («с пометкой X») или «<noun> X» (таймер/секундомер X)."""
    for mk in _LABEL_MARKERS:
        idx = s.find(mk)
        if idx >= 0:
            lbl = _take_label_words(s[idx + len(mk):].strip().split())
            if lbl:
                return lbl
    m = re.search(noun + r"\w*\s+(.+)", s)
    if m:
        lbl = _take_label_words(m.group(1).split())
        if lbl:
            return lbl
    return None


def _duration_part(s):
    """Текст до маркера метки (чтобы числа из метки не попали в длительность)."""
    cut = len(s)
    for mk in _LABEL_MARKERS:
        idx = s.find(mk)
        if idx >= 0:
            cut = min(cut, idx)
    return s[:cut]


def parse_timer_command(text: str):
    """Команда таймера → dict или None.
    {'действие': set|move|cancel|delete_all|query, 'длительность': int|None, 'метка': str|None}."""
    try:
        s = _norm(text)
        if not s:
            return None
        if _has(s, _V_CANCEL) and re.search(r"\bвс[еёя]\b", s) and "таймер" in s:
            return {"действие": "delete_all", "длительность": None, "метка": None}
        if "осталось" in s or ("сколько" in s and not _has(s, _V_SET + _V_MOVE)):
            action = "query"
        elif _has(s, _V_MOVE):
            action = "move"
        elif _has(s, _V_CANCEL):
            action = "cancel"
        else:
            action = "set"
        dur = parse_duration(_duration_part(s)) if action in ("set", "move") else None
        label = _extract_label(s, "таймер")
        return {"действие": action, "длительность": dur, "метка": label}
    except Exception:
        _log.debug("parse_timer_command сбой на %r", text, exc_info=True)
        return None


def parse_stopwatch_command(text: str):
    """Команда секундомера → dict или None.
    {'действие': start|stop|reset|query|delete_all, 'метка': str|None}."""
    try:
        s = _norm(text)
        if not s:
            return None
        if _has(s, _V_CANCEL) and re.search(r"\bвс[еёя]\b", s) and "секундомер" in s:
            return {"действие": "delete_all", "метка": None}
        if "прошло" in s or ("сколько" in s and "секундомер" in s):
            action = "query"
        elif _has(s, _V_START) or "засек" in s or "засеч" in s:
            action = "start"
        elif _has(s, _V_STOP):
            action = "stop"
        elif _has(s, _V_CANCEL):  # сбрось/обнули/удали
            action = "reset"
        else:
            action = "query"
        label = _extract_label(s, "секундомер")
        return {"действие": action, "метка": label}
    except Exception:
        _log.debug("parse_stopwatch_command сбой на %r", text, exc_info=True)
        return None
