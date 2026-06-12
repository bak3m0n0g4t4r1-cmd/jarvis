"""Напоминания и задачи — чистая логика: парсинг дат/команд, гейты, состояние диалога.

Без MQTT/потоков (stdlib + config + reuse alarms/timers/speech). Импортируют core (гейт молчания +
флаг диалога) и scheduler (полная обработка). Парсинг — слой ПРАВИЛ (ключевые слова + difflib, как
в alarms/timers): даты относительные/абсолютные/дни недели + время через `alarms.parse_time`.

Две сущности: НАПОМИНАНИЕ (срабатывает в момент) и ЗАДАЧА (пункт списка дел, опц. дедлайн). Раздельные
команды/секции/фразы, «удалить все» раздельно. Всё в try-except — сбой не роняет сервис.
"""
import json
import logging
import re
import time as _time
from datetime import date, datetime, timedelta

from jarvis import config, timers
from jarvis.alarms import _LABEL_MARKERS, _ONES, _TENS, _norm, _take_label_words, parse_time
from jarvis.speech import _ORD_GEN_ONES, _ORD_GEN_TENS

_log = logging.getLogger("jarvis-reminders")

# Дни недели (все падежные формы, что выдаёт STT) → индекс (пн=0).
_WD = {
    "понедельник": 0, "понедельника": 0,
    "вторник": 1, "вторника": 1,
    "среда": 2, "среду": 2, "среды": 2,
    "четверг": 3, "четверга": 3,
    "пятница": 4, "пятницу": 4, "пятницы": 4,
    "суббота": 5, "субботу": 5, "субботы": 5,
    "воскресенье": 6, "воскресенья": 6,
}
# Месяцы (родительный + именительный + предложный) → номер.
_MON = {
    "января": 1, "январь": 1, "феврал": 2, "марта": 3, "март": 3, "апрел": 4, "мая": 5, "май": 5,
    "июня": 6, "июнь": 6, "июне": 6, "июля": 7, "июль": 7, "июле": 7, "августа": 8, "август": 8,
    "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12, "декабря": 12,
}
# Реверс родительных порядковых: «пятнадцатого»→15, «двадцатого»→20, «тридцатого»→30.
# ВАЖНО: формы из speech несут ударения `+` и «ё», а текст приходит НОРМАЛИЗОВАННЫМ (+ убран,
# ё→е). Без очистки ключи (а) ломали бы regex (`+` = квантификатор), (б) не совпадали бы с
# текстом по «ё». Поэтому строим карты по «чистым» формам (см. _plain_ord).
def _plain_ord(w: str) -> str:
    return str(w).replace("+", "").replace("ё", "е")


_DAY_WORD = {_plain_ord(w): n for n, w in _ORD_GEN_ONES.items()}
for _n, _w in _ORD_GEN_TENS.items():
    if _n in (20, 30):
        _DAY_WORD[_plain_ord(_w)] = _n
# Чистые формы единиц для разбора составных «двадцать/тридцать N-ого» (сравнение с текстом).
_ONES_GEN_PLAIN = {_plain_ord(w): n for n, w in _ORD_GEN_ONES.items()}


def _num_word(tok):
    """Слово-число (для «через N»): 1..50 или None."""
    return _ONES.get(tok) or _TENS.get(tok)


def _relative_delta(s):
    """«через N минут/часов» → timedelta или None («через минуту/час» = 1).

    Только минуты/часы: «через N дней/недель/месяцев» — дело parse_date (уровень даты).
    Нужна, чтобы «напомни через 30 минут» не прочлось как 30:00→06:00 (parse_time берёт
    первое число за ЧАС). «полтора/полторы» здесь НЕ ловим (дробное), вернём None."""
    m = re.search(r"через\s+(\w+)\s+(минут\w*|час\w*)", s)
    if m:
        n = _num_word(m.group(1)) or (int(m.group(1)) if m.group(1).isdigit() else None)
        unit = m.group(2)
    else:
        m1 = re.search(r"через\s+(минуту|час)\b", s)
        if not m1:
            return None
        n, unit = 1, m1.group(1)
    if not n:
        return None
    return timedelta(minutes=n) if unit.startswith("минут") else timedelta(hours=n)


def _month_in(s):
    """Найти номер месяца по основе слова в строке, иначе None."""
    for stem, num in _MON.items():
        if stem in s:
            return num
    return None


def _parse_day_word(s):
    """День месяца, заданный СЛОВОМ в родительном: «пятнадцатого»→15, «двадцать первого»→21."""
    m = re.search(r"\b(двадцать|тридцать)\s+(\w+)", s)
    if m and m.group(2) in _ONES_GEN_PLAIN:
        tens = 20 if m.group(1) == "двадцать" else 30
        return tens + _ONES_GEN_PLAIN[m.group(2)]
    for w, n in _DAY_WORD.items():
        if re.search(r"\b" + w + r"\b", s):
            return n
    return None


def parse_date(text, today=None):
    """Распарсить ДАТУ из русской фразы → datetime.date или None. Относительные, дни недели,
    абсолютные (число+месяц / N-го / словом). Прошедшая дата текущего года → следующий год."""
    try:
        if today is None:
            today = date.today()
        s = _norm(text)
        if not s:
            return None
        if "послезавтра" in s:
            return today + timedelta(days=2)
        if "завтра" in s:
            return today + timedelta(days=1)
        if "сегодня" in s:
            return today

        # «через N дней/недель/месяцев» (N — цифра или слово; «через день/неделю/месяц» = 1).
        m = re.search(r"через\s+(\w+)\s+(дн\w+|день|недел\w+|месяц\w*)", s)
        if not m:
            m2 = re.search(r"через\s+(день|недел\w+|месяц\w*)", s)
            if m2:
                return _add_unit(today, 1, m2.group(1))
        if m:
            n = _num_word(m.group(1)) or (int(m.group(1)) if m.group(1).isdigit() else 1)
            return _add_unit(today, n, m.group(2))

        # День недели: «в пятницу», «в следующий вторник».
        nextw = "следующ" in s
        for w, idx in _WD.items():
            if re.search(r"\b" + w + r"\b", s):
                delta = (idx - today.weekday()) % 7
                if delta == 0:
                    delta = 7
                if nextw and delta < 7:
                    delta += 7
                return today + timedelta(days=delta)

        # «в начале/середине/конце <месяца>».
        m = re.search(r"(начал|серед|конц)\w*\s+(\w+)", s)
        if m:
            mo = _month_in(m.group(2))
            if mo:
                day = {"начал": 5, "серед": 15, "конц": 25}[m.group(1)]
                return _ymd(today, mo, day)

        # «15 июня» / «пятнадцатого июня» (цифрой или словом + месяц).
        mo = _month_in(s)
        if mo:
            md = re.search(r"\b(\d{1,2})\b", s)
            day = int(md.group(1)) if md else _parse_day_word(s)
            if day:
                return _ymd(today, mo, day)

        # «15-го» / «15 числа» (день этого/следующего месяца).
        m = re.search(r"\b(\d{1,2})\s*(?:го|числа)\b", s)
        if m:
            return _day_this_or_next(today, int(m.group(1)))
        # День словом без месяца: «пятнадцатого [числа]».
        dw = _parse_day_word(s)
        if dw and ("числ" in s or "го " in s + " "):
            return _day_this_or_next(today, dw)
        return None
    except Exception:
        _log.debug("parse_date сбой на %r", text, exc_info=True)
        return None


def _add_unit(today, n, unit):
    import calendar
    if unit.startswith("дн") or unit == "день":
        return today + timedelta(days=n)
    if unit.startswith("недел"):
        return today + timedelta(weeks=n)
    if unit.startswith("месяц"):
        mo = today.month - 1 + n
        y = today.year + mo // 12
        mo = mo % 12 + 1
        return date(y, mo, min(today.day, calendar.monthrange(y, mo)[1]))
    return today


def _ymd(today, mo, day):
    import calendar
    y = today.year
    day = min(day, calendar.monthrange(y, mo)[1])
    cand = date(y, mo, day)
    if cand < today:
        y += 1
        cand = date(y, mo, min(day, calendar.monthrange(y, mo)[1]))
    return cand


def _day_this_or_next(today, day):
    import calendar
    if day >= today.day:
        return date(today.year, today.month, min(day, calendar.monthrange(today.year, today.month)[1]))
    mo = today.month % 12 + 1
    y = today.year + (today.month // 12)
    return date(y, mo, min(day, calendar.monthrange(y, mo)[1]))


# Маркеры явного времени (часы/минуты/период/особые формы).
_TIME_MARK = re.compile(
    r"\d{1,2}[:.]\d{2}|\bчас|минут|утр|вечер|\bдня\b|ночи|полдень|полноч|полвосьм|полминут|"
    r"без\s+(?:пят|чет|дес|двад)|четверть|половин", re.UNICODE)
_MONTH_RE = r"(?:янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт|ноя|дек)"


def parse_when(text, today=None):
    """(дата|None, (час,мин)|None, точное:bool). Время берём ТОЛЬКО при явном маркере или «в N»,
    чтобы «15 июня» (день) не принялось за время 15:00."""
    try:
        s = _norm(text)
        # «через N минут/часов» = относительный момент now+Δ. Ловим ДО parse_time, иначе
        # «через 30 минут» прочлось бы как 30:00→06:00. Кладём в модель (дата, время) — дальше
        # scheduler._compute_fire соберёт точный datetime (с переходом через полночь).
        delta = _relative_delta(s)
        if delta is not None:
            fire = datetime.now() + delta
            return fire.date(), (fire.hour, fire.minute), True
        d = parse_date(s, today)
        t = None
        if _TIME_MARK.search(s):
            t = parse_time(s)
        else:
            # «в 12» как время (но не «в 12 июня» и не «12-го»).
            m = re.search(r"\bв[оо]?\s+(\d{1,2})\b(?!\s*(?:числ|го)\b)(?!\s+" + _MONTH_RE + r")", s)
            if m:
                h = int(m.group(1))
                if 0 <= h <= 23:
                    t = (h, 0)
        return d, t, (t is not None)
    except Exception:
        _log.debug("parse_when сбой на %r", text, exc_info=True)
        return None, None, False


# --- Извлечение «о чём» (текст напоминания/задачи) -------------------------- #
_CMD_WORDS = {"напомни", "напомните", "напоминай", "напоминать", "напомнить", "мне", "поставь",
              "добавь", "добавить", "создай", "новая", "новую", "задача", "задачу", "задачи",
              "задач", "дело", "запиши", "отметь", "удали", "удалить", "отмени", "перенеси",
              "измени", "что", "какие", "мои", "напоминание", "напоминания", "напоминаний",
              # глаголы «выполнено» (чтобы текст задачи был чистым при отметке «выполнена X»)
              "выполнена", "выполнено", "выполнен", "выполнить", "выполни", "сделал", "сделала",
              "сделано", "готово", "готова", "заверши", "завершена", "завершить", "завершено",
              # слова повтора (текст без «каждый день»)
              "каждый", "каждую", "каждое", "каждые", "ежедневно", "ежедневное", "раз"}
_TOPIC_PREP = {"про", "о", "об", "обо"}                      # вводят тему — всегда убираем
_POS_PREP = {"в", "во", "на", "к", "с", "со", "до"}          # убираем ТОЛЬКО перед датой/временем
# Токены даты/времени (одиночные) — выкидываем из текста.
_DT_TOKENS = (
    {"сегодня", "завтра", "послезавтра", "сейчас", "через", "день", "дня", "дней",
     "неделю", "недели", "недель", "месяц", "месяца", "месяцев", "час", "часа", "часов",
     "минут", "минуту", "минуты", "утра", "вечера", "ночи", "ровно", "начале", "середине",
     "конце", "начала", "середины", "следующий", "следующую", "следующее", "следующего",
     "следующей", "числа", "го", "полдень", "полночь"}
    | set(_WD) | set(_MON) | set(_DAY_WORD) | set(_ONES) | set(_TENS)
)


def extract_text(text):
    """«О чём»: убрать глагол/тему-предлог/дату/время/метку → остаток (описание).

    «напомни про стирку сегодня в 12»→«стирку»; «…позвонить маме»→«позвонить маме»;
    «выйти на прогулку» сохраняется (предлог «на» не перед датой/временем)."""
    try:
        s = _norm(text)
        # Отрезать хвост метки «с пометкой …».
        for mk in _LABEL_MARKERS:
            idx = s.find(mk)
            if idx >= 0:
                s = s[:idx].strip()
                break
        toks = s.split()
        out = []
        i = 0
        while i < len(toks):
            t = toks[i]
            if t in _CMD_WORDS or t in _TOPIC_PREP:
                i += 1
                continue
            if t in _DT_TOKENS or re.fullmatch(r"\d+", t) or re.fullmatch(r"\d{1,2}[:.]\d{2}", t):
                i += 1
                continue
            if t in _POS_PREP:
                nxt = toks[i + 1] if i + 1 < len(toks) else ""
                if (nxt in _DT_TOKENS or re.fullmatch(r"\d+", nxt)
                        or re.fullmatch(r"\d{1,2}[:.]\d{2}", nxt)):
                    i += 1
                    continue  # предлог относится к дате/времени — убираем
            out.append(t)
            i += 1
        return " ".join(out).strip() or None
    except Exception:
        _log.debug("extract_text сбой на %r", text, exc_info=True)
        return None


def _extract_label(s):
    """Метка «с пометкой X» (для привязки при изменении/удалении). Иначе None."""
    for mk in _LABEL_MARKERS:
        idx = s.find(mk)
        if idx >= 0:
            lbl = _take_label_words(s[idx + len(mk):].strip().split())
            if lbl:
                return lbl
    return None


# --- Гейты ------------------------------------------------------------------ #
def _enabled():
    return config.ALARMS_ENABLED


def is_list_query(text):
    """«Что у меня на сегодня/завтра», «мои планы» — общий список напоминаний+задач."""
    try:
        s = _norm(text)
        if not s:
            return False
        if "что" in s and ("на сегодня" in s or "на завтра" in s or "у меня" in s):
            return True
        return "мои планы" in s
    except Exception:
        return False


def is_reminder_command(text):
    try:
        if not _enabled():
            return False
        s = _norm(text)
        # Две основы: «напомн-и/-ить» (сов.вид) и «напомин-ай/-ание/-ать» (несов.вид — для повтора).
        return bool(s) and ("напомн" in s or "напомин" in s or is_list_query(text))
    except Exception:
        return False


def is_task_command(text):
    try:
        if not _enabled():
            return False
        s = _norm(text)
        return bool(s) and ("задач" in s or "список дел" in s or "дела на" in s)
    except Exception:
        return False


def is_scheduler_command(text):
    """Полный гейт планировщика (будильник/таймер/секундомер/напоминание/задача/список) — core молчит."""
    try:
        return (timers.is_scheduler_command(text) or is_reminder_command(text)
                or is_task_command(text))
    except Exception:
        return False


# --- Действия (слой правил) ------------------------------------------------- #
_V_MOVE = ("перенес", "перенос", "измен", "поменя", "сдвин", "переставь", "перестав")
_V_CANCEL = ("отмен", "удал", "убер", "сними", "сотри")
_V_DONE = ("выполн", "сделал", "сделан", "готов", "заверш")


def _has(s, stems):
    return any(st in s for st in stems)


def parse_reminder_command(text):
    """{действие: set|move|cancel|delete_all|list, текст, дата, время, точное, повтор, метка}."""
    try:
        s = _norm(text)
        if not s:
            return None
        if is_list_query(text) or (("напомин" in s or "напомн" in s)
                                   and _has(s, ("какие", "что", "мои", "список", "перечисли"))):
            return {"действие": "list"}
        if _has(s, _V_CANCEL) and re.search(r"\bвс[еёя]\b", s):
            return {"действие": "delete_all"}
        if _has(s, _V_MOVE):
            action = "move"
        elif _has(s, _V_CANCEL):
            action = "cancel"
        else:
            action = "set"
        d, t, exact = parse_when(s)
        повтор = "ежедневный" if re.search(r"кажд\w+ день|ежедневн|каждый раз", s) else "нет"
        return {"действие": action, "текст": extract_text(s),
                "дата": d, "время": t, "точное": exact, "повтор": повтор,
                "метка": _extract_label(s)}
    except Exception:
        _log.debug("parse_reminder_command сбой на %r", text, exc_info=True)
        return None


def parse_task_command(text):
    """{действие: add|list|done|delete|delete_all, текст, дата, время, точное, метка}."""
    try:
        s = _norm(text)
        if not s:
            return None
        if _has(s, ("какие", "что", "мои", "список", "перечисли")) and "задач" in s:
            return {"действие": "list"}
        if _has(s, _V_CANCEL) and re.search(r"\bвс[еёя]\b", s):
            return {"действие": "delete_all"}
        if _has(s, _V_DONE):
            action = "done"
        elif _has(s, _V_CANCEL):
            action = "delete"
        else:
            action = "add"
        d, t, exact = parse_when(s)
        return {"действие": action, "текст": extract_text(s),
                "дата": d, "время": t, "точное": exact, "метка": _extract_label(s)}
    except Exception:
        _log.debug("parse_task_command сбой на %r", text, exc_info=True)
        return None


# --- Состояние диалога (флаг для core — молчать на ответ-продолжение) -------- #
def _dialog_file():
    return config.LOGS_DIR / "dialog_state.json"


def is_dialog_pending():
    """True, если scheduler ведёт диалог дозапроса (core должен молчать на любой ввод)."""
    try:
        p = _dialog_file()
        if not p.exists():
            return False
        data = json.loads(p.read_text(encoding="utf-8")) or {}
        return _time.time() < float(data.get("active_until", 0))
    except Exception:
        return False


def arm_dialog(seconds):
    """Взвести флаг диалога на `seconds` (продлевается при каждом вопросе)."""
    try:
        _dialog_file().write_text(
            json.dumps({"active_until": _time.time() + float(seconds)}), encoding="utf-8")
    except Exception:
        _log.debug("Не удалось взвести флаг диалога", exc_info=True)


def clear_dialog():
    try:
        p = _dialog_file()
        if p.exists():
            p.unlink()
    except Exception:
        _log.debug("Не удалось снять флаг диалога", exc_info=True)


def is_cancel(text):
    """Пользователь отменяет диалог («отмена», «неважно», «забудь»)."""
    try:
        s = _norm(text)
        return any(w in s for w in ("отмена", "отмени", "неважно", "не важно", "забудь", "отбой"))
    except Exception:
        return False
