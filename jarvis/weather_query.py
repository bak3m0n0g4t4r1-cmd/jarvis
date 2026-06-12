"""Погодный запрос: «Джарвис, какая погода» + вариации с датой и городом (Этап 24).

Отвечает на «погода» (сейчас) / «погода сегодня|завтра|на субботу|13 числа|вчера|5 июня» (день) и
по другому городу («погода в Париже»). Источник — Open-Meteo (тот же, что у утреннего будильника),
БЕЗ ключа. Окно дат: forecast-эндпоинт покрывает [−92 … +16] дней, глубокое прошлое — archive (с
1940). Вне окна / нет данных / нет сети / город не найден → ЭЛЕГАНТНЫЙ ответ в характере, не падение.

РАСПОЗНАВАНИЕ — слой ПРАВИЛ: строгий якорь «погод» (точная основа, БЕЗ difflib). Нечёткая фраза без
основы «погод» сюда не попадает (не сработает вместо погоды что-то ещё). Внутри — один гейт и один
обработчик с ветвлением по дате/городу: подкомандам путаться не между чем.

Парсер дат — СВОЙ (reminders.parse_date кидает прошедшую дату в будущее — для погоды нельзя: «погода
на 11», когда сегодня 12-е, это вчера). Словари дат переиспользуются из reminders, геокодинг города с
падежами — из worldtime. Всё в try-except.
"""
import json
import logging
import re
import time as _time
from datetime import date, timedelta

from jarvis import config, phrases, weather, worldtime
from jarvis.alarms import _norm
from jarvis.reminders import _WD, _month_in, _num_word, _parse_day_word
from jarvis.speech import say_date_natural, say_temperature

_log = logging.getLogger("jarvis-weather-query")

# Якорь запроса погоды (основа слова — ловит «погода/погоду/погоде/погодой»).
_ANCHOR = "погод"
# Слова после «в/во», которые НЕ являются городом (время/быт) — чтобы «в субботу», «в обед»,
# «на улице» не принялись за город. Дни недели/месяцы добавляются ниже из словарей reminders.
_CITY_STOP = {
    "сейчас", "сегодня", "завтра", "послезавтра", "вчера", "позавчера", "обед", "полдень",
    "ночь", "ночи", "утро", "утром", "вечер", "вечером", "днём", "днем", "улице", "дворе",
    "окном", "окне", "городе", "целом", "общем", "градусах", "этот", "этом", "нашем", "нашем",
    "ближайшие", "выходные", "начале", "середине", "конце", "течение",
} | set(_WD)


def is_weather_query(text: str) -> bool:
    """True, если фраза — запрос погоды (содержит основу «погод»). Без неё → не погода."""
    try:
        return _ANCHOR in (text or "").lower().replace("ё", "е")
    except Exception:
        return False


# --- Парсер даты погоды (направление прошлое/будущее, в отличие от reminders) ---------- #
def parse_weather_date(text, today=None):
    """ДАТА из фразы погоды → datetime.date или None (None = «сейчас»). Поддержка: сегодня/завтра/
    послезавтра/вчера/позавчера, через N дней-недель-месяцев, N дней-лет назад, дни недели (ближайший
    или «прошлый»), N числа / N-го (этот месяц, прошлое не прыгает в будущее), N месяца (этот год),
    явный год. Для погоды прошедшая дата ОСТАЁТСЯ в прошлом (не как в reminders)."""
    try:
        if today is None:
            today = date.today()
        s = _norm(text)
        if not s:
            return None

        if "позавчера" in s:
            return today - timedelta(days=2)
        if "вчера" in s:
            return today - timedelta(days=1)
        if "послезавтра" in s:
            return today + timedelta(days=2)
        if "завтра" in s:
            return today + timedelta(days=1)
        if "сегодня" in s:
            return today

        # «N лет/дней/недель/месяцев назад».
        m = re.search(r"(\w+)\s+(год\w*|лет|дн\w+|день|недел\w+|месяц\w*)\s+назад", s)
        if not m:
            m1 = re.search(r"(год|день|недел\w+|месяц\w*)\s+назад", s)
            if m1:
                return _shift(today, 1, m1.group(1), sign=-1)
        if m:
            n = _num_word(m.group(1)) or (int(m.group(1)) if m.group(1).isdigit() else 1)
            return _shift(today, n, m.group(2), sign=-1)

        # «через N дней/недель/месяцев» (будущее).
        m = re.search(r"через\s+(\w+)\s+(дн\w+|день|недел\w+|месяц\w*)", s)
        if not m:
            m2 = re.search(r"через\s+(день|недел\w+|месяц\w*)", s)
            if m2:
                return _shift(today, 1, m2.group(1), sign=1)
        if m:
            n = _num_word(m.group(1)) or (int(m.group(1)) if m.group(1).isdigit() else 1)
            return _shift(today, n, m.group(2), sign=1)

        # День недели: «в субботу» (ближайшая вперёд), «в прошлую субботу» (назад).
        prev = "прошл" in s
        for w, idx in _WD.items():
            if re.search(r"\b" + w + r"\b", s):
                if prev:
                    delta = -((today.weekday() - idx) % 7 or 7)
                else:
                    delta = (idx - today.weekday()) % 7  # 0 = сегодня этот день недели
                return today + timedelta(days=delta)

        year = None
        ym = re.search(r"\b(19\d\d|20\d\d)\b", s)
        if ym:
            year = int(ym.group(1))

        # «N месяца» / «пятнадцатое июня» (этот год; прошлое НЕ прыгает в следующий год).
        mo = _month_in(s)
        if mo:
            md = re.search(r"\b(\d{1,2})\b", s)
            day = int(md.group(1)) if md else _parse_day_word(s)
            if day:
                return _safe_date(year or today.year, mo, day)

        # «N числа» / «N-го» (день ЭТОГО месяца; в прошлом — этот месяц, не следующий).
        m = re.search(r"\b(\d{1,2})\s*(?:го|числа)\b", s)
        if m:
            return _safe_date(today.year, today.month, int(m.group(1)))
        dw = _parse_day_word(s)
        if dw and ("числ" in s or "го " in s + " "):
            return _safe_date(today.year, today.month, dw)
        return None
    except Exception:
        _log.debug("parse_weather_date сбой на %r", text, exc_info=True)
        return None


def _shift(today, n, unit, sign):
    """Сдвиг даты на n единиц вперёд (sign=1) или назад (sign=-1)."""
    import calendar
    n = n * sign
    if unit.startswith("дн") or unit == "день":
        return today + timedelta(days=n)
    if unit.startswith("недел"):
        return today + timedelta(weeks=n)
    if unit.startswith("месяц"):
        mo = today.month - 1 + n
        y = today.year + mo // 12
        mo = mo % 12 + 1
        return date(y, mo, min(today.day, calendar.monthrange(y, mo)[1]))
    if unit.startswith("год") or unit == "лет":
        try:
            return date(today.year + n, today.month, today.day)
        except ValueError:  # 29 февраля
            return date(today.year + n, today.month, 28)
    return today


def _safe_date(y, mo, day):
    """Собрать дату с зажимом дня в границах месяца (None при явной ерунде)."""
    import calendar
    try:
        if not (1 <= mo <= 12):
            return None
        day = max(1, min(int(day), calendar.monthrange(y, mo)[1]))
        return date(y, mo, day)
    except Exception:
        return None


# --- Город из фразы («погода в Париже») + геокодинг с координатами --------------------- #
def _detect_city(text: str):
    """Город после «в/во <город>» (1–2 слова, без стоп-слов) или None (тогда регион по умолчанию)."""
    try:
        s = worldtime._norm_city(text)
        if not s:
            return None
        m = None
        for m in re.finditer(r"\bв[оо]?\s+(.+)$", s):
            pass
        if not m:
            return None
        tail = [t for t in m.group(1).split() if t not in _CITY_STOP and not t.isdigit()]
        # отбросить хвостовые погодные/служебные слова
        while tail and tail[0] in {"погода", "погоде", "погоду", "погодой"}:
            tail = tail[1:]
        if not tail:
            return None
        return " ".join(tail[:2]).strip() or None
    except Exception:
        _log.debug("_detect_city сбой на %r", text, exc_info=True)
        return None


def _cache_path():
    return config.LOGS_DIR / "weather_query_cache.json"


def _load_cache() -> dict:
    try:
        p = _cache_path()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _save_cache(cache: dict):
    try:
        _cache_path().write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        _log.debug("Кэш погодного запроса не сохранён", exc_info=True)


def _geocode_city(phrase: str):
    """Фраза-город (возможно в падеже) → {'lat','lon','name'} или None. Кэш в файле по фразе."""
    phrase = (phrase or "").strip()
    if not phrase:
        return None
    cache = _load_cache()
    geo = cache.get("geo") or {}
    hit = geo.get(phrase)
    if isinstance(hit, dict) and "lat" in hit:
        return hit
    for cand in worldtime._candidates(phrase):
        data = weather._get_json(weather._GEOCODE_URL,
                                 {"name": cand, "count": 1, "language": "ru", "format": "json"},
                                 config.ALARM_WEATHER_TIMEOUT)
        res = (data or {}).get("results") or []
        if res:
            r = res[0]
            info = {"lat": float(r["latitude"]), "lon": float(r["longitude"]),
                    "name": r.get("name", cand)}
            geo[phrase] = info
            cache["geo"] = geo
            _save_cache(cache)
            return info
    return None


def _pick(key, pack, **kw):
    """Выбрать фразу пака без повторов и подставить плейсхолдеры."""
    out = phrases.pick(key, pack) or ""
    for k, v in kw.items():
        if v is not None:
            out = out.replace("{" + k + "}", str(v))
    return out.strip()


def _date_phrase(d, today):
    """Дата для фразы погоды: вчера/позавчера/сегодня/завтра/в субботу/пятнадцатого июня."""
    delta = (d - today).days
    if delta == -2:
        return "позавчера"
    if delta == -1:
        return "вчера"
    return say_date_natural(d, today)


def answer(text: str):
    """Готовый ответ о погоде или None (если это не запрос). Любой сбой → фраза в характере."""
    try:
        if not is_weather_query(text):
            return None
        today = date.today()
        d = parse_weather_date(text, today)

        # Координаты: город из речи или регион по умолчанию.
        city_phrase = _detect_city(text)
        if city_phrase:
            geo = _geocode_city(city_phrase)
            if not geo:
                return _pick("weather.not_found_city", config.WEATHER_NOT_FOUND_CITY,
                             город=city_phrase)
            lat, lon, place = geo["lat"], geo["lon"], geo["name"]
        else:
            reg = weather.geocode(config.REGION)
            if not reg:
                return _pick("weather.no_network", config.WEATHER_NO_NETWORK)
            lat, lon, place = reg[0], reg[1], None

        # Куда указывает «в городе X» (только если город назвали явно).
        где = f"в городе {place}" if place else None

        if d is None:
            # Текущая погода («какая погода» без указания дня).
            w = weather.current_weather(lat, lon)
            if not w:
                return _pick("weather.no_network", config.WEATHER_NO_NETWORK)
            return _pick("weather.now", config.WEATHER_NOW,
                         характер=w["характер"],
                         температура=say_temperature(w["температура"]),
                         ощущается=say_temperature(w["ощущается"]),
                         город=где or "")

        # Дневная погода на конкретную дату.
        delta = (d - today).days
        if delta > config.WEATHER_FORECAST_MAX_DAYS:
            return _pick("weather.too_far_future", config.WEATHER_TOO_FAR_FUTURE)
        if d.year < 1940:
            return _pick("weather.too_far_past", config.WEATHER_TOO_FAR_PAST)

        w = weather.weather_for_date(lat, lon, d)
        if not w:
            # Архив отстаёт ~5 дней / нет данных за дату → внятно по направлению времени.
            if delta < 0:
                return _pick("weather.too_far_past", config.WEATHER_TOO_FAR_PAST)
            return _pick("weather.no_network", config.WEATHER_NO_NETWORK)

        дата = _date_phrase(d, today)
        осадки = _precip_phrase(w.get("осадки"))
        if delta == 0:
            pack_key, pack = "weather.day_today", config.WEATHER_DAY_TODAY
        elif delta > 0:
            pack_key, pack = "weather.day_future", config.WEATHER_DAY_FUTURE
        else:
            pack_key, pack = "weather.day_past", config.WEATHER_DAY_PAST
        return _pick(pack_key, pack, дата=дата, характер=w["характер"],
                     темп_макс=say_temperature(w["темп_макс"]),
                     темп_мин=say_temperature(w["темп_мин"]),
                     осадки=осадки, город=где or "")
    except Exception:
        _log.debug("weather_query.answer сбой на %r", text, exc_info=True)
        try:
            return _pick("weather.no_network", config.WEATHER_NO_NETWORK)
        except Exception:
            return None


def _precip_phrase(prob):
    """Вероятность осадков в характере (для будущих дней) или пустая строка."""
    try:
        if prob is None:
            return ""
        prob = int(prob)
        if prob >= 70:
            return "Дождь весьма вероятен — зонт будет разумной предосторожностью."
        if prob >= 35:
            return "Дождь не исключён, сэр."
        return ""
    except Exception:
        return ""
