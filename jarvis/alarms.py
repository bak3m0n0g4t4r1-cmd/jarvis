"""Будильники — чистая логика: распознавание команд, парсинг русского времени, файл расписания.

Без MQTT/потоков (только stdlib + config + yaml) — импортируют И `core` (хук `is_alarm_command`,
чтобы молчать на команды будильника, как с `breaks.is_stop_phrase`), И сервис `scheduler` (полная
обработка). Распознавание команд — слой ПРАВИЛ (ключевые слова + difflib), а НЕ эмбеддинги:
действия поставь/отмени/удали — антонимы, эмбеддинги их путают; здесь нужны точные слова.

Парсер времени покрывает речь: цифры/«ЧЧ:ММ», числа словами (0..59, «двадцать один»), период
суток (утра/дня/вечера/ночи → 24ч), «полвосьмого»/«половина восьмого», «без четверти восемь»,
«четверть восьмого», «час дня», «полдень/полночь». Всё в try-except: сбой → None (не падаем).
"""
import logging
import os
import re
import tempfile
from difflib import SequenceMatcher

import yaml

from jarvis import config

_log = logging.getLogger("jarvis-alarms")

# --- Нормализация (как matcher/breaks): нижний регистр, ё→е, без пунктуации кроме «:» ---
_PUNCT = re.compile(r"[^\w\s:]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def _norm(text: str) -> str:
    s = (text or "").lower().replace("ё", "е")
    s = _PUNCT.sub(" ", s)
    return _SPACES.sub(" ", s).strip()


# --- Числа словами ---------------------------------------------------------- #
_ONES = {
    "ноль": 0, "один": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11,
    "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
    "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
}
_TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50}
# Порядковые в родительном (для «половина ВОСЬМОГО», «полвосьмого», «четверть восьмого»).
_ORD_GEN = {
    "первого": 1, "второго": 2, "третьего": 3, "четвертого": 4, "пятого": 5, "шестого": 6,
    "седьмого": 7, "восьмого": 8, "девятого": 9, "десятого": 10, "одиннадцатого": 11,
    "двенадцатого": 12,
}
# «без СКОЛЬКИХ-то» (родительный количественного).
_BEZ = {"пяти": 5, "десяти": 10, "пятнадцати": 15, "четверти": 15, "двадцати": 20}


def _read_number(tokens, i):
    """Прочитать ОДНО число словами с позиции i. Возвращает (значение, сколько токенов съедено).

    Десятки (20..50) поглощают следующую единицу как составное («двадцать один» = 21). Единицы/
    подростки (0..19) — одно слово. «семь тридцать» НЕ составное (единица перед десятком) — читатель
    вернёт 7 за один токен, а «тридцать» прочтётся отдельно как минуты."""
    if i >= len(tokens):
        return None, 0
    t = tokens[i]
    if t in _TENS:
        base = _TENS[t]
        if i + 1 < len(tokens) and tokens[i + 1] in _ONES and _ONES[tokens[i + 1]] < 10:
            return base + _ONES[tokens[i + 1]], 2
        return base, 1
    if t in _ONES:
        return _ONES[t], 1
    return None, 0


def _apply_period(h, period):
    """12-часовой → 24-часовой по периоду суток."""
    if period in ("утра", "ночи"):
        return 0 if h == 12 else h
    if period in ("дня", "вечера"):
        return h + 12 if 1 <= h <= 11 else h
    return h


def parse_time(text: str):
    """Распарсить время из русской фразы → (час 0..23, минута 0..59) или None."""
    try:
        s = _norm(text)
        if not s:
            return None
        toks = s.split()
        # Период суток определяем СРАЗУ — он применяется и к особым формам (полвосьмого вечера и т.п.).
        period = next((t for t in toks if t in ("утра", "дня", "вечера", "ночи")), None)

        # Особые слова.
        if "полдень" in s:
            return (12, 0)
        if "полночь" in s:
            return (0, 0)

        # «без X N» → (N−1):(60−X). N — следующий час (количественное «восемь» или порядковое).
        m = re.search(r"без (пяти|десяти|пятнадцати|четверти|двадцати) (\w+)", s)
        if m:
            sub = _BEZ[m.group(1)]
            # «час» здесь = следующий 1-й час («без пяти час» = 00:55), иначе число/порядковое.
            # Без этого «час» не распознавался как 1 → «без пяти час» давало 1:00.
            nxt = (1 if m.group(2) in ("час", "часа")
                   else _ONES.get(m.group(2)) or _ORD_GEN.get(m.group(2)))
            if nxt:
                # Период суток применяем к ЧАСУ, затем −1 («без пяти час дня» = 12:55, а не
                # 0:55: дня двигает 1→13, тогда −1=12). На границе час=1 это важно.
                return ((_apply_period(nxt, period) - 1) % 24, 60 - sub)

        # «полвосьмого» (слитно) / «пол восьмого» / «половина восьмого» → (N−1):30.
        for w, v in _ORD_GEN.items():
            if ("пол" + w) in s.replace(" ", ""):
                return (_apply_period((v - 1) % 24, period), 30)
        # «половина/половину/половине N-ого» или «пол N-ого» → (N−1):30 (учёт падежей).
        m = re.search(r"(?:половин\w*|пол)\s+(\w+)", s)
        if m and m.group(1) in _ORD_GEN:
            return (_apply_period((_ORD_GEN[m.group(1)] - 1) % 24, period), 30)

        # «четверть N-ого» → (N−1):15.
        m = re.search(r"четверть (\w+)", s)
        if m and m.group(1) in _ORD_GEN:
            return (_apply_period((_ORD_GEN[m.group(1)] - 1) % 24, period), 15)

        # «ЧЧ:ММ» / «ЧЧ.ММ» цифрами.
        m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", s)
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
        else:
            nums = re.findall(r"\d{1,2}", s)
            if nums:
                h = int(nums[0])
                mi = int(nums[1]) if len(nums) > 1 and int(nums[1]) < 60 else 0
            else:
                # «час дня/ночи» без числа = 1 час.
                idx = next((i for i, t in enumerate(toks) if t in _ONES or t in _TENS), None)
                if idx is None:
                    if "час" in toks:
                        h, mi = 1, 0
                    else:
                        return None
                else:
                    h, used = _read_number(toks, idx)
                    if h is None:
                        return None
                    mi = 0
                    rest = toks[idx + used:]
                    # минуты: первое число после часа (пропускаем «часов/час/ровно»).
                    j = 0
                    while j < len(rest) and rest[j] in ("часов", "час", "часа", "ровно"):
                        j += 1
                    if j < len(rest):
                        mm, _u = _read_number(rest, j)
                        if mm is not None and mm < 60:
                            mi = mm

        if period:
            h = _apply_period(h, period)
        h, mi = h % 24, mi % 60
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return (h, mi)
        return None
    except Exception:
        _log.debug("parse_time сбой на %r", text, exc_info=True)
        return None


# --- Распознавание команды будильника --------------------------------------- #
# Гейт: текст вообще про будильник? (core молчит, scheduler обрабатывает.)
_GATE_WORDS = ("будильник", "разбуди", "подъем", "утренн")

# Глаголы действий (по основам — ловим формы). Порядок проверки задаёт приоритет.
_V_MOVE = ("перенес", "перенос", "измен", "поменя", "сдвин", "переставь", "перестав")
_V_CANCEL = ("отмен", "удал", "убер", "сним", "выключи", "погаси", "сотри")
_V_SET = ("постав", "завед", "установ", "создай", "создать", "добав", "разбуд", "заведи")
_TYPE_MORNING = ("утренн", "утром", "разбуд", "подъем")

_LABEL_MARKERS = ("под названием", "с пометкой", "с меткой", "пометкой", "пометка",
                  "меткой", "метка", "названием", "назови", "название")
# Слова, которые НЕ могут быть меткой (предлоги, время, служебное).
_LABEL_STOP = set(["на", "в", "к", "во", "со", "с", "и", "а", "будильник", "будильника",
                   "будильнике", "будильники", "будильников", "час", "часа", "часов", "минут",
                   "минуту", "минуты", "утра", "дня", "вечера", "ночи", "ровно", "время",
                   "утренний", "обычный"]) | set(_ONES) | set(_TENS) | set(_ORD_GEN)


def is_alarm_command(text: str) -> bool:
    """Это команда про будильник? (гейт для core — молчать, для scheduler — обрабатывать).

    Срабатывает на наличие «будильник»/«разбуди»/«подъём»/«утренн» (точно или близко по difflib —
    ловим искажения STT). Любой сбой → False (не мешаем обычной обработке)."""
    try:
        if not config.ALARMS_ENABLED:
            return False
        s = _norm(text)
        if not s:
            return False
        for w in _GATE_WORDS:
            if w in s:
                return True
        # Нечётко (искажения STT): сверяем каждое слово с «будильник».
        for tok in s.split():
            if len(tok) >= 6 and SequenceMatcher(None, tok, "будильник").ratio() >= 0.8:
                return True
        return False
    except Exception:
        return False


def _has_any(s, stems):
    return any(st in s for st in stems)


def _extract_label(s):
    """Извлечь метку из нормализованной фразы. Сначала явные маркеры («с пометкой X»),
    затем «будильник X» (для изменения/удаления по названию). Иначе None."""
    # Явные маркеры.
    for mk in _LABEL_MARKERS:
        idx = s.find(mk)
        if idx >= 0:
            tail = s[idx + len(mk):].strip().split()
            label = _take_label_words(tail)
            if label:
                return label
    # «будильник <слово(а)>» — название после слова «будильник…».
    m = re.search(r"будильник\w*\s+(.+)", s)
    if m:
        label = _take_label_words(m.group(1).split())
        if label:
            return label
    return None


def _take_label_words(tokens):
    """Собрать метку из ведущих токенов, пока они допустимы (не предлог/время/число). До 3 слов."""
    out = []
    for t in tokens:
        if t in _LABEL_STOP or re.fullmatch(r"\d+", t) or re.fullmatch(r"\d{1,2}[:.]\d{2}", t):
            break
        out.append(t)
        if len(out) >= 3:
            break
    return " ".join(out) if out else None


def parse_command(text: str):
    """Разобрать команду будильника → dict или None.

    Возвращает {'действие': set|move|cancel|delete_all, 'тип': morning|regular,
    'час': int|None, 'минута': int|None, 'метка': str|None}. None — не команда будильника."""
    try:
        s = _norm(text)
        if not s:
            return None
        is_morning = _has_any(s, _TYPE_MORNING)
        # «убери ВСЕ будильники» — только при глаголе удаления + «все/всё».
        if _has_any(s, _V_CANCEL) and re.search(r"\bвс[еёя]\b", s):
            return {"действие": "delete_all", "тип": "regular",
                    "час": None, "минута": None, "метка": None}
        # Приоритет: перенос → отмена → установка (move/cancel содержат свои глаголы).
        if _has_any(s, _V_MOVE):
            action = "move"
        elif _has_any(s, _V_CANCEL):
            action = "cancel"
        elif _has_any(s, _V_SET) or is_morning:
            action = "set"
        else:
            # «будильник на 7» без явного глагола — трактуем как установку.
            action = "set"
        tm = parse_time(s)
        hour = tm[0] if tm else None
        minute = tm[1] if tm else None
        # «разбуди через N минут/часов» в модель будильника (только wall-clock «ЧЧ:ММ», без даты)
        # чисто не ложится, а parse_time взял бы N за ЧАС («через 30 минут»→06:00). Если есть
        # относительное «через …» И НЕТ явного абсолютного маркера времени — не выдаём мусорный
        # час, а просим уточнить (переспрос лучше неверного будильника). Для относительного
        # времени уместнее напоминание («напомни … через N минут») или таймер.
        if re.search(r"через\s+(?:\w+\s+)?(?:минут|час)", s) and not re.search(
                r"\d{1,2}[:.]\d{2}|утра|дня|вечера|ночи|полдень|полноч|половин|четверт|\bбез\b", s):
            hour = minute = None
        label = None if is_morning else _extract_label(s)
        return {"действие": action, "тип": "morning" if is_morning else "regular",
                "час": hour, "минута": minute, "метка": label}
    except Exception:
        _log.debug("parse_command сбой на %r", text, exc_info=True)
        return None


# --- Файл расписания (schedule.yaml) ---------------------------------------- #
_SCHEDULE_HEADER = (
    "# ═══════════════════════════════════════════════════════════════════════════════\n"
    "#  РАСПИСАНИЕ «ДЖАРВИСА» — будильники, таймеры, секундомеры, напоминания, задачи\n"
    "# ═══════════════════════════════════════════════════════════════════════════════\n"
    "#  Правится и голосом, и вручную. Поля:\n"
    "#   будильники:  тип(утренний|обычный), время(\"ЧЧ:ММ\"), метка, повтор(ежедневный|разовый),\n"
    "#                активен, id\n"
    "#   таймеры:     длительность_сек, окончание(ISO-время), метка, активен, сработал, id\n"
    "#   секундомеры: старт(ISO-время), стоп(ISO|пусто=идёт), метка, активен, id\n"
    "#   напоминания: текст, срабатывание(ISO), точное(true|false), повтор(нет|ежедневный),\n"
    "#                метка, активен, сработал, id\n"
    "#   задачи:      текст, дедлайн(ISO|null), статус(активна|выполнена), метка, id\n"
    "#   (id — служебный идентификатор, НЕ меняйте; время в ISO — местное)\n"
    "#\n"
    "#  ⚠ При изменении ГОЛОСОМ файл перезаписывается — ручные комментарии внутри не\n"
    "#    сохранятся (значения — сохранятся). Структура — задел под будущее.\n"
    "# ═══════════════════════════════════════════════════════════════════════════════\n\n"
)


# Списки расписания, которыми владеет код (порядок — как пишем в файл).
_SCHEDULE_LISTS = ("будильники", "таймеры", "секундомеры", "напоминания", "задачи")


def read_schedule() -> dict:
    """Прочитать schedule.yaml → {'будильники':[...], 'таймеры':[...], 'секундомеры':[...]}.
    Нет файла/битый → пустые списки (не падаем)."""
    empty = {k: [] for k in _SCHEDULE_LISTS}
    try:
        path = config.SCHEDULE_FILE
        if not os.path.exists(path):
            return dict(empty)
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for key in _SCHEDULE_LISTS:
            v = data.get(key)
            data[key] = v if isinstance(v, list) else []
        return data
    except Exception:
        _log.warning("schedule.yaml не прочитан — считаю расписание пустым", exc_info=True)
        return dict(empty)


def write_schedule(data: dict) -> bool:
    """Атомарно записать расписание (tmp + os.replace) с русской шапкой-комментарием."""
    try:
        path = config.SCHEDULE_FILE
        payload = {key: data.get(key, []) for key in _SCHEDULE_LISTS}
        for extra in ("напоминания",):  # сохраняем будущие секции, если появятся
            if extra in data:
                payload[extra] = data[extra]
        body = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".schedule.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(_SCHEDULE_HEADER)
                f.write(body)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return True
    except Exception:
        _log.warning("schedule.yaml не записан", exc_info=True)
        return False
