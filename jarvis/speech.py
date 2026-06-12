"""Преобразование чисел в произносимый русский для TTS (Silero).

TTS читает «14:30», «07.06.2026», «45%» криво или неоднозначно. Здесь — время, дата
и проценты словами: «четырнадцать часов тридцать минут», «седьмое июня две тысячи
двадцать шестого года», «сорок пять процентов». Диапазоны конечные (часы 0–23,
минуты/проценты 0–100, дни 1–31, годы ~1900–2100) — полноценный num2words не нужен,
без внешних зависимостей. Все функции чистые и НЕ бросают (на странном входе — str(n)).

Слова с ударениями размечены `+` (Silero чтит ударение перед гласной): «час+ов»,
«дв+адцать», «сент+ября». На односложных (час, два) и «ё» знак не ставится.
"""
import re
from datetime import date as _date


def apply_pronunciation(text, table):
    """Заменить проблемные слова на корректное произношение/ударение ПЕРЕД синтезом Piper (ТЗ-10).

    Целым словом, регистронезависимо. table — {слово: замена} (латиница→кириллица или акут ́ на
    ударной гласной). Чистая функция, не бросает: сбой/пусто → исходный текст."""
    try:
        if not text or not table:
            return text
        out = text
        for word, repl in table.items():
            if not word:
                continue
            out = re.sub(rf"(?<!\w){re.escape(str(word))}(?!\w)", str(repl), out,
                         flags=re.IGNORECASE | re.UNICODE)
        return out
    except Exception:
        return text


def apply_stress(text, table):
    """Проставить `+`-ударения ПЕРЕД синтезом Silero (eugene учитывает `+`, в отличие от Piper).

    table — ручной словарь {слово: слово_с_+} (омографы, редкие слова). Замена целым словом,
    регистронезависимо. Авто-ударение для свободного текста (silero-stress) подключается
    отдельно в TTS-сервисе (лениво, как torch) — здесь только детерминированная ручная правка,
    чтобы ключ кэша был стабилен. Чистая функция, не бросает."""
    return apply_pronunciation(text, table)


_AUTO_ACCENT = None
_AUTO_ACCENT_TRIED = False


def auto_stress(text):
    """Авто-простановка `+`-ударений для СВОБОДНОГО текста (лениво, тяжёлая модель). Опционально.

    Применяется ТОЛЬКО к тексту, идущему в синтез на ПРОМАХЕ кэша, и НЕ к ключу кэша — поэтому
    не ломает детерминизм ключей (статика уже размечена вручную при сборке). Включается флагом
    voice.auto_stress; нужен пакет (pip install -e '.[stress]'). Любой сбой/отсутствие → текст как
    есть (Silero сам ставит ударения через put_accent)."""
    global _AUTO_ACCENT, _AUTO_ACCENT_TRIED
    if not text:
        return text
    if _AUTO_ACCENT is None and not _AUTO_ACCENT_TRIED:
        _AUTO_ACCENT_TRIED = True
        try:                                   # ruaccent: ставит `+` перед ударной гласной
            from ruaccent import RUAccent
            acc = RUAccent()
            acc.load(omograph_model_size="tiny", use_dictionary=True)
            _AUTO_ACCENT = acc.process_all
        except Exception:
            _AUTO_ACCENT = None
    if _AUTO_ACCENT is None:
        return text
    try:
        return _AUTO_ACCENT(text)
    except Exception:
        return text


# Количественные единицы. Мужской род по умолчанию; женский — для «одна минута»,
# «две тысячи» (род важен только для 1 и 2).
# Знак `+` ставится перед ударной гласной (Silero чтит ударения). На односложных (ноль, два,
# три, пять…) `+` не нужен, на «ё» — тоже (она уже ударная).
_UNITS_M = ("ноль", "од+ин", "два", "три", "чет+ыре", "пять", "шесть", "семь", "в+осемь",
            "д+евять", "д+есять", "од+иннадцать", "двен+адцать", "трин+адцать", "чет+ырнадцать",
            "пятн+адцать", "шестн+адцать", "семн+адцать", "восемн+адцать", "девятн+адцать")
_UNITS_F = ("ноль", "одн+а", "две", "три", "чет+ыре", "пять", "шесть", "семь", "в+осемь",
            "д+евять", "д+есять", "од+иннадцать", "двен+адцать", "трин+адцать", "чет+ырнадцать",
            "пятн+адцать", "шестн+адцать", "семн+адцать", "восемн+адцать", "девятн+адцать")
_TENS = ("", "", "дв+адцать", "тр+идцать", "с+орок", "пятьдес+ят", "шестьдес+ят", "с+емьдесят",
         "в+осемьдесят", "девян+осто")
_HUNDREDS = ("", "сто", "дв+ести", "тр+иста", "чет+ыреста", "пятьс+от", "шестьс+от", "семьс+от",
             "восемьс+от", "девятьс+от")

# Месяцы в родительном падеже («седьмое ИЮНЯ»).
_MONTHS_GEN = ("январ+я", "феврал+я", "м+арта", "апр+еля", "м+ая", "и+юня", "и+юля", "+августа",
               "сентябр+я", "октябр+я", "ноябр+я", "декабр+я")

# Порядковые средний род для дня («первое… тридцать первое»).
_ORD_NEUTER = {
    1: "п+ервое", 2: "втор+ое", 3: "тр+етье", 4: "четвёртое", 5: "п+ятое", 6: "шест+ое",
    7: "седьм+ое", 8: "восьм+ое", 9: "дев+ятое", 10: "дес+ятое", 11: "од+иннадцатое",
    12: "двен+адцатое", 13: "трин+адцатое", 14: "чет+ырнадцатое", 15: "пятн+адцатое",
    16: "шестн+адцатое", 17: "семн+адцатое", 18: "восемн+адцатое", 19: "девятн+адцатое",
    20: "двадц+атое", 30: "тридц+атое",
}
# Порядковые родительный (для года: «…двадцать ШЕСТОГО года»).
_ORD_GEN_ONES = {
    1: "п+ервого", 2: "втор+ого", 3: "тр+етьего", 4: "четвёртого", 5: "п+ятого", 6: "шест+ого",
    7: "седьм+ого", 8: "восьм+ого", 9: "дев+ятого", 10: "дес+ятого", 11: "од+иннадцатого",
    12: "двен+адцатого", 13: "трин+адцатого", 14: "чет+ырнадцатого", 15: "пятн+адцатого",
    16: "шестн+адцатого", 17: "семн+адцатого", 18: "восемн+адцатого", 19: "девятн+адцатого",
}
_ORD_GEN_TENS = {20: "двадц+атого", 30: "тридц+атого", 40: "сорок+ового", 50: "пятидес+ятого",
                 60: "шестидес+ятого", 70: "семидес+ятого", 80: "восьмидес+ятого",
                 90: "девян+остого"}


def cardinal(n: int, gender: str = "m") -> str:
    """Количественное число словами (0..999999). gender='f' для женского рода (1, 2)."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n < 0:
        return "м+инус " + cardinal(-n, gender)
    units = _UNITS_F if gender == "f" else _UNITS_M
    if n < 20:
        return units[n]
    if n < 100:
        t = _TENS[n // 10]
        return t if n % 10 == 0 else f"{t} {units[n % 10]}"
    if n < 1000:
        h = _HUNDREDS[n // 100]
        return h if n % 100 == 0 else f"{h} {cardinal(n % 100, gender)}"
    # Тысячи (нужны для года: 2026 → «две тысячи двадцать шесть»).
    th, rem = n // 1000, n % 1000
    th_word = f"{cardinal(th, 'f')} {plural_ru(th, 'т+ысяча', 'т+ысячи', 'т+ысяч')}"
    return th_word if rem == 0 else f"{th_word} {cardinal(rem, gender)}"


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Выбор формы по числу: 1 час / 2 часа / 5 часов."""
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return many
    d = n % 10
    if d == 1:
        return one
    if 2 <= d <= 4:
        return few
    return many


def say_percent(n: int) -> str:
    """«45» → «сорок пять процентов»."""
    return f"{cardinal(n)} {plural_ru(n, 'проц+ент', 'проц+ента', 'проц+ентов')}"


def say_time(h: int, m: int) -> str:
    """«14, 30» → «четырнадцать часов тридцать минут». m=0 → «… ровно»."""
    hh = f"{cardinal(h)} {plural_ru(h, 'час', 'час+а', 'час+ов')}"
    if m == 0:
        return f"{hh} р+овно"
    mm = f"{cardinal(m, 'f')} {plural_ru(m, 'мин+ута', 'мин+уты', 'мин+ут')}"
    return f"{hh} {mm}"


def say_time_of_day(h: int) -> str:
    """Период суток по часу (0..23): «ночи»/«утра»/«дня»/«вечера» — для естественной речи."""
    try:
        h = int(h) % 24
    except (TypeError, ValueError):
        return ""
    if 0 <= h <= 4:
        return "н+очи"
    if 5 <= h <= 11:
        return "утр+а"
    if 12 <= h <= 16:
        return "дня"
    return "в+ечера"


def say_clock(h: int, m: int) -> str:
    """Время будильника естественно (12-часовой стиль + период суток), НЕ «07:00».

    «7, 0» → «семь часов утра»; «19, 30» → «семь часов тридцать минут вечера»;
    «0, 0» → «двенадцать часов ночи»; «13, 5» → «час дня пять минут».
    """
    try:
        h, m = int(h) % 24, int(m) % 60
    except (TypeError, ValueError):
        return say_time(h, m)
    period = say_time_of_day(h)
    h12 = h % 12 or 12  # 0/12 → 12, 13 → 1
    hh = f"{cardinal(h12)} {plural_ru(h12, 'час', 'час+а', 'час+ов')}"
    if m == 0:
        return f"{hh} {period}"
    mm = f"{cardinal(m, 'f')} {plural_ru(m, 'мин+ута', 'мин+уты', 'мин+ут')}"
    return f"{hh} {mm} {period}"


def say_temperature(t) -> str:
    """Температура естественно: -3 → «минус три градуса», 25 → «двадцать пять градусов»,
    0 → «ноль градусов». Дробное округляется до целого. На странном входе — str(t)."""
    try:
        t = int(round(float(t)))
    except (TypeError, ValueError):
        return str(t)
    return f"{cardinal(t)} {plural_ru(abs(t), 'гр+адус', 'гр+адуса', 'гр+адусов')}"


def say_duration(seconds) -> str:
    """Длительность словами с ВЕРНЫМИ склонениями, нулевые компоненты ОПУСКАЮТСЯ.

    Час — мужской («один час/два часа/пять часов»), минута/секунда — женский («одна минута/
    две минуты/двадцать одна секунда»). Примеры: 300→«пять минут», 90→«одна минута тридцать
    секунд», 3725→«один час две минуты пять секунд», 0→«ноль секунд»."""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return str(seconds)
    if total < 0:
        return "м+инус " + say_duration(-total)
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{cardinal(h)} {plural_ru(h, 'час', 'час+а', 'час+ов')}")
    if m:
        parts.append(f"{cardinal(m, 'f')} {plural_ru(m, 'мин+ута', 'мин+уты', 'мин+ут')}")
    if sec:
        parts.append(f"{cardinal(sec, 'f')} {plural_ru(sec, 'сек+унда', 'сек+унды', 'сек+унд')}")
    if not parts:
        return "ноль сек+унд"
    return " ".join(parts)


def _ordinal_neuter(n: int) -> str:
    """Порядковое ср. рода 1..31 (день месяца)."""
    if n in _ORD_NEUTER:
        return _ORD_NEUTER[n]
    if 21 <= n <= 29:
        return f"двадцать {_ORD_NEUTER[n - 20]}"
    if n == 31:
        return "тридцать первое"
    return str(n)


def _ordinal_gen(n: int) -> str:
    """Порядковое родит. 1..99 (последняя часть года)."""
    if n in _ORD_GEN_ONES:
        return _ORD_GEN_ONES[n]
    if n in _ORD_GEN_TENS:
        return _ORD_GEN_TENS[n]
    t, o = (n // 10) * 10, n % 10
    if t in _ORD_GEN_TENS and o in _ORD_GEN_ONES:
        # «двадцать шестого»: десяток количественный + единица порядковая.
        return f"{_TENS[t // 10]} {_ORD_GEN_ONES[o]}"
    return str(n)


def year_genitive(y: int) -> str:
    """Год в родительном: 2026 → «две тысячи двадцать шестого»."""
    try:
        y = int(y)
    except (TypeError, ValueError):
        return str(y)
    th, rem = y // 1000, y % 1000
    parts = []
    if th == 1:
        parts.append("т+ысяча")
    elif th:
        parts.append(f"{cardinal(th, 'f')} {plural_ru(th, 'т+ысяча', 'т+ысячи', 'т+ысяч')}")
    hund, last = rem // 100, rem % 100
    if hund:
        parts.append(_HUNDREDS[hund])
    if last:
        parts.append(_ordinal_gen(last))
    elif not hund and not th:
        return str(y)
    return " ".join(parts)


def say_date(day: int, month: int, year: int) -> str:
    """«7, 6, 2026» → «седьмое июня две тысячи двадцать шестого года»."""
    month_word = _MONTHS_GEN[month - 1] if 1 <= month <= 12 else str(month)
    return f"{_ordinal_neuter(day)} {month_word} {year_genitive(year)} г+ода"


# Дни недели в винительном с предлогом («во вторник», «в среду») — для естественных дат.
_WEEKDAY_ACC = ("в понедельник", "во вторник", "в среду", "в четверг", "в пятницу",
                "в субботу", "в воскресенье")


def say_day_genitive(day: int) -> str:
    """День месяца в родительном: 15 → «пятнадцатого», 21 → «двадцать первого» (для дат)."""
    try:
        return _ordinal_gen(int(day))
    except Exception:
        return str(day)


def say_date_natural(d, today=None) -> str:
    """Дата ЕСТЕСТВЕННО для напоминаний: сегодня/завтра/послезавтра → словом; ближайшая неделя →
    «во вторник»; иначе «пятнадцатого июня» (+ год, если не текущий). На странном входе — str(d)."""
    try:
        if today is None:
            today = _date.today()
        delta = (d - today).days
        if delta == 0:
            return "сегодня"
        if delta == 1:
            return "завтра"
        if delta == 2:
            return "послезавтра"
        if 3 <= delta <= 6:
            return _WEEKDAY_ACC[d.weekday()]
        month_word = _MONTHS_GEN[d.month - 1] if 1 <= d.month <= 12 else str(d.month)
        base = f"{say_day_genitive(d.day)} {month_word}"
        if d.year != today.year:
            base += f" {year_genitive(d.year)} г+ода"
        return base
    except Exception:
        return str(d)


def say_when(dt, точное: bool = True, today=None) -> str:
    """Момент срабатывания словами: дата (say_date_natural) + (если точное) «в <время>».
    «завтра в десять часов утра» / «во вторник» (без времени)."""
    try:
        date_part = say_date_natural(dt.date() if hasattr(dt, "date") else dt, today)
        if точное and hasattr(dt, "hour"):
            return f"{date_part} в {say_clock(dt.hour, dt.minute)}"
        return date_part
    except Exception:
        return str(dt)
