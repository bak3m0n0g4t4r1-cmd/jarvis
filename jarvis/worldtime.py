"""Мировое время: «сколько времени в <город>» → текущее время города + разница с регионом.

Город из русской речи идёт в ПРЕДЛОЖНОМ падеже («в Париже»), а Open-Meteo geocoding ищет по
ИМЕНИТЕЛЬНОМУ («Париж») — сверено: «париже» не находится. Поэтому генерируем кандидаты-формы
(париже→париж/парижа, москве→москва, твери→тверь) и пробуем их, пока геокодинг не найдёт. Часовой
пояс берём из ответа geocoding (поле timezone), время и летнее/зимнее — через stdlib `zoneinfo`
(без ручных офсетов). Разница — от РЕГИОНА из конфига (не жёстко МСК). Город не распознан → внятный
ответ в характере (не падение). Имя города в ответе — в именительном из geocoding («в городе Париж»),
чтобы не склонять (морфологию-pymorphy на N100 не тянем).
"""
import json
import logging
import re
from datetime import datetime

from jarvis import config, phrases, weather
from jarvis.speech import cardinal, plural_ru, say_clock

_log = logging.getLogger("jarvis-worldtime")

# Лёгкая нормализация, СОХРАНЯЯ дефис (Нью-Йорк, Санкт-Петербург — один токен города).
_CITY_KEEP = re.compile(r"[^\w\s\-]", re.UNICODE)
# Хвостовые/служебные слова, не относящиеся к названию города (вкл. слова запроса времени —
# на случай порядка «в лондоне сейчас сколько времени»).
_CITY_STOP = {"сейчас", "там", "сегодня", "пожалуйста", "сэр", "а", "и", "у", "нас", "это",
              "сколько", "время", "времени", "который", "которой", "час", "часа", "часов",
              "точное", "ровно", "будет", "покажи", "скажи"}


def _norm_city(text: str) -> str:
    s = (text or "").lower().replace("ё", "е")
    s = _CITY_KEEP.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def detect_city(text: str):
    """Распознать запрос мирового времени и вернуть фразу-город (предложный падеж) или None.

    Требуется контекст времени (врем*/час*) И предлог «в/во <город>». Город — 1–2 слова после
    последнего «в», без хвостовых стоп-слов. None → это не запрос мирового времени."""
    try:
        s = _norm_city(text)
        if not s or not re.search(r"врем|час", s):
            return None
        # Последнее вхождение предлога «в/во» + остаток фразы.
        m = None
        for m in re.finditer(r"\bв[оо]?\s+(.+)$", s):
            pass
        if not m:
            return None
        tail = [t for t in m.group(1).split() if t not in _CITY_STOP]
        if not tail:
            return None
        return " ".join(tail[:2]).strip() or None
    except Exception:
        _log.debug("detect_city сбой на %r", text, exc_info=True)
        return None


def _candidates(city: str):
    """Формы города для геокодинга: исходная + восстановление именительного из предложного."""
    c = [city]
    if city.endswith("е"):
        c += [city[:-1], city[:-1] + "а"]          # Париже→Париж, Москве→Москва
    elif city.endswith("и"):
        c += [city[:-1] + "ь", city[:-1] + "я", city[:-1]]  # Твери→Тверь, …
    # уникальные, порядок сохранён
    seen, out = set(), []
    for x in c:
        if x and x not in seen:
            seen.add(x); out.append(x)
    return out


def _wt_cache_path():
    return config.LOGS_DIR / "worldtime_cache.json"


def _load_cache() -> dict:
    try:
        p = _wt_cache_path()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _save_cache(cache: dict):
    try:
        _wt_cache_path().write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        _log.debug("Кэш мирового времени не сохранён", exc_info=True)


def geocode_city(city: str):
    """Фраза-город (возможно в падеже) → {'name','tz'} или None. Кэшируется по исходной фразе."""
    city = (city or "").strip()
    if not city:
        return None
    cache = _load_cache()
    hit = cache.get(city)
    if isinstance(hit, dict) and "tz" in hit:
        return hit
    for cand in _candidates(city):
        data = weather._get_json(weather._GEOCODE_URL,
                                 {"name": cand, "count": 1, "language": "ru", "format": "json"},
                                 config.ALARM_WEATHER_TIMEOUT)
        res = (data or {}).get("results") or []
        if res:
            r = res[0]
            info = {"name": r.get("name", cand), "tz": r.get("timezone", "UTC")}
            cache[city] = info
            _save_cache(cache)
            _log.info("Мировое время: «%s» → %s (%s)", city, info["name"], info["tz"])
            return info
    _log.info("Мировое время: город «%s» не найден", city)
    return None


def _diff_phrase(city_off_min: int, region_off_min: int) -> str:
    """Фраза о разнице во времени с регионом («на два часа больше/меньше» / «совпадает»)."""
    diff_min = city_off_min - region_off_min
    if diff_min == 0:
        return "Время совпадает с нашим."
    hours = abs(diff_min) // 60
    mins = abs(diff_min) % 60
    word = "больше" if diff_min > 0 else "меньше"
    if hours and mins:
        h = f"{cardinal(hours)} {plural_ru(hours, 'час', 'часа', 'часов')}"
        mm = f"{cardinal(mins, 'f')} {plural_ru(mins, 'минута', 'минуты', 'минут')}"
        return f"Это на {h} {mm} {word}, чем у нас."
    if hours:
        h = f"{cardinal(hours)} {plural_ru(hours, 'час', 'часа', 'часов')}"
        return f"Это на {h} {word}, чем у нас."
    mm = f"{cardinal(mins, 'f')} {plural_ru(mins, 'минута', 'минуты', 'минут')}"
    return f"Это на {mm} {word}, чем у нас."


def _fmt(template, **kw):
    out = template or ""
    for k, v in kw.items():
        if v is not None:
            out = out.replace("{" + k + "}", str(v))
    return out


def answer(city_phrase: str):
    """Готовый ответ о мировом времени или None (если это не запрос). Сбой/город не найден →
    фраза в характере, не падение."""
    try:
        from zoneinfo import ZoneInfo
        info = geocode_city(city_phrase)
        if not info:
            return _fmt(phrases.pick("worldtime.not_found", config.WORLDTIME_NOT_FOUND),
                        город=city_phrase)
        now_utc = datetime.now(ZoneInfo("UTC"))
        city_dt = now_utc.astimezone(ZoneInfo(info["tz"]))
        # Часовой пояс региона (из конфига) — для разницы.
        region_geo = weather.geocode(config.REGION)
        region_off = 0
        if region_geo:
            region_dt = now_utc.astimezone(ZoneInfo(region_geo[2]))
            region_off = int(region_dt.utcoffset().total_seconds() // 60)
        city_off = int(city_dt.utcoffset().total_seconds() // 60)
        время = say_clock(city_dt.hour, city_dt.minute)
        разница = _diff_phrase(city_off, region_off) if region_geo else ""
        return _fmt(phrases.pick("worldtime.answer", config.WORLDTIME_ANSWER),
                    город=info["name"], время=время, разница=разница).strip()
    except Exception:
        _log.debug("worldtime.answer сбой на %r", city_phrase, exc_info=True)
        return None
