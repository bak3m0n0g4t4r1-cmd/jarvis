"""Погода для утреннего будильника — Open-Meteo, БЕЗ ключа и без сторонних зависимостей.

Только stdlib `urllib` + короткий таймаут: погода ОПЦИОНАЛЬНА — есть сеть, добавляем; нет
сети/ошибка/таймаут → возвращаем None (будильник звонит без погоды, НЕ падает и НЕ виснет).

Поток: регион кириллицей (config.REGION) → геокодинг Open-Meteo в координаты (кэшируются на
диск — не дёргаем сеть каждое утро) → дневной прогноз (макс/мин температура + weather_code) →
русский «характер дня» по таблице WMO.

ВНИМАНИЕ (CLAUDE.md): пользователь — пентестер, системный трафик НЕ заворачивается в прокси/VPN.
Это обычный прямой HTTPS-запрос к публичному API, разовый и лёгкий.
"""
import json
import logging
import urllib.parse
import urllib.request

from jarvis import config

_log = logging.getLogger("jarvis-weather")

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# weather_code (WMO) → русский характер дня. Группируем по смыслу (диапазоны кодов).
_WMO = {
    0: "ясно",
    1: "преимущественно ясно", 2: "переменная облачность", 3: "облачно",
    45: "туман", 48: "туман с изморозью",
    51: "лёгкая морось", 53: "морось", 55: "сильная морось",
    56: "ледяная морось", 57: "сильная ледяная морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    66: "ледяной дождь", 67: "сильный ледяной дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа",
    80: "кратковременный дождь", 81: "ливень", 82: "сильный ливень",
    85: "снегопад", 86: "сильный снегопад",
    95: "гроза", 96: "гроза с градом", 99: "сильная гроза с градом",
}


def _wmo_text(code) -> str:
    """weather_code → русский характер дня. Незнакомый код → нейтральное «переменная облачность»."""
    try:
        return _WMO.get(int(code), "переменная облачность")
    except (TypeError, ValueError):
        return "переменная облачность"


def _get_json(url: str, params: dict, timeout: float):
    """GET → JSON или None (любой сбой/таймаут — тихо, чтобы будильник не зависел от сети)."""
    try:
        full = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(full, headers={"User-Agent": "jarvis/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        _log.debug("HTTP-запрос не удался (%s): %s", type(exc).__name__, exc)
        return None


def _geocache_path():
    return config.LOGS_DIR / "geocache.json"


def _load_geocache() -> dict:
    try:
        p = _geocache_path()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        _log.debug("Геокеш не прочитан", exc_info=True)
    return {}


def _save_geocache(cache: dict) -> None:
    try:
        _geocache_path().write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        _log.debug("Геокеш не сохранён", exc_info=True)


def geocode(region: str, timeout: float | None = None):
    """Регион кириллицей → (lat, lon, timezone) или None. Координаты КЭШируются на диск
    (по строке региона) — сеть дёргаем только при смене региона/пустом кэше."""
    region = (region or "").strip()
    if not region:
        return None
    if timeout is None:
        timeout = config.ALARM_WEATHER_TIMEOUT
    cache = _load_geocache()
    hit = cache.get(region)
    if isinstance(hit, dict) and "lat" in hit and "lon" in hit:
        return hit["lat"], hit["lon"], hit.get("tz", "auto")
    # Open-Meteo ищет по названию города; берём часть до запятой («Москва, Россия» → «Москва»).
    name = region.split(",")[0].strip() or region
    data = _get_json(_GEOCODE_URL, {"name": name, "count": 1, "language": "ru", "format": "json"},
                     timeout)
    try:
        res = (data or {}).get("results") or []
        if not res:
            _log.info("Геокодинг не нашёл регион %r — будильник без погоды", region)
            return None
        r = res[0]
        lat, lon, tz = float(r["latitude"]), float(r["longitude"]), r.get("timezone", "auto")
        cache[region] = {"lat": lat, "lon": lon, "tz": tz, "name": r.get("name", name)}
        _save_geocache(cache)
        _log.info("Координаты региона %r: %.3f, %.3f (%s)", region, lat, lon, tz)
        return lat, lon, tz
    except Exception:
        _log.debug("Разбор ответа геокодинга не удался", exc_info=True)
        return None


def forecast(lat: float, lon: float, timeout: float | None = None):
    """Дневной прогноз → {'темп_макс': int, 'темп_мин': int, 'характер': str} или None."""
    if timeout is None:
        timeout = config.ALARM_WEATHER_TIMEOUT
    data = _get_json(_FORECAST_URL, {
        "latitude": lat, "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,weather_code",
        "timezone": "auto", "forecast_days": 1,
    }, timeout)
    try:
        daily = (data or {}).get("daily") or {}
        tmax = round(float(daily["temperature_2m_max"][0]))
        tmin = round(float(daily["temperature_2m_min"][0]))
        char = _wmo_text(daily["weather_code"][0])
        return {"темп_макс": tmax, "темп_мин": tmin, "характер": char}
    except Exception:
        _log.debug("Разбор прогноза не удался", exc_info=True)
        return None


def morning_weather(region: str | None = None, timeout: float | None = None):
    """Удобный вход для будильника: регион → погода или None (без сети/при сбое — None).

    Возвращает {'темп_макс', 'темп_мин', 'характер'} либо None (тогда фраза без погоды)."""
    try:
        if region is None:
            region = config.REGION
        geo = geocode(region, timeout)
        if not geo:
            return None
        lat, lon, _tz = geo
        return forecast(lat, lon, timeout)
    except Exception:
        _log.debug("Получение погоды не удалось", exc_info=True)
        return None


# === Расширение Этапа 24: текущая погода и погода на конкретную дату ================
# Источник тот же Open-Meteo, БЕЗ ключа. Будущее/недавнее прошлое — forecast (current/daily,
# past_days≤92, forecast_days≤16). Глубокое прошлое — archive (ERA5, с 1940, лаг ~5 дней).
# Любой сбой/нет сети → None (модуль погоды отвечает в характере, не падает).

_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def current_weather(lat: float, lon: float, timeout: float | None = None):
    """Погода СЕЙЧАС → {'температура','ощущается','характер','день'} или None.

    `день` = True днём / False ночью (Open-Meteo is_day) — для фраз «солнечно/ясная ночь»."""
    if timeout is None:
        timeout = config.ALARM_WEATHER_TIMEOUT
    data = _get_json(_FORECAST_URL, {
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,apparent_temperature,weather_code,is_day",
        "timezone": "auto",
    }, timeout)
    try:
        cur = (data or {}).get("current") or {}
        return {
            "температура": round(float(cur["temperature_2m"])),
            "ощущается": round(float(cur["apparent_temperature"])),
            "характер": _wmo_text(cur["weather_code"]),
            "день": bool(cur.get("is_day", 1)),
        }
    except Exception:
        _log.debug("Разбор текущей погоды не удался", exc_info=True)
        return None


def weather_for_date(lat: float, lon: float, target, timeout: float | None = None):
    """Дневная погода на дату `target` (datetime.date) → {'темп_макс','темп_мин','характер',
    'осадки'} или None. Сама выбирает endpoint: недавние даты [−92..+16] дней — forecast;
    глубокое прошлое — archive. `осадки` — вероятность дождя в % (forecast) либо None (archive)."""
    if timeout is None:
        timeout = config.ALARM_WEATHER_TIMEOUT
    try:
        from datetime import date as _date
        today = _date.today()
        delta = (target - today).days
        iso = target.isoformat()
        if -92 <= delta <= 16:
            # Окно forecast-эндпоинта (прогноз + недавняя история).
            url = _FORECAST_URL
            params = {
                "latitude": lat, "longitude": lon,
                "daily": ("temperature_2m_max,temperature_2m_min,weather_code,"
                          "precipitation_probability_max"),
                "timezone": "auto", "start_date": iso, "end_date": iso,
            }
        else:
            # Глубокое прошлое — реанализ-архив ERA5.
            url = _ARCHIVE_URL
            params = {
                "latitude": lat, "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min,weather_code",
                "timezone": "auto", "start_date": iso, "end_date": iso,
            }
        data = _get_json(url, params, timeout)
        daily = (data or {}).get("daily") or {}
        tmax = daily.get("temperature_2m_max") or []
        tmin = daily.get("temperature_2m_min") or []
        codes = daily.get("weather_code") or []
        if not tmax or tmax[0] is None or not tmin or tmin[0] is None:
            return None
        prob = daily.get("precipitation_probability_max") or []
        осадки = None
        if prob and prob[0] is not None:
            try:
                осадки = int(round(float(prob[0])))
            except (TypeError, ValueError):
                осадки = None
        return {
            "темп_макс": round(float(tmax[0])),
            "темп_мин": round(float(tmin[0])),
            "характер": _wmo_text(codes[0] if codes else None),
            "осадки": осадки,
        }
    except Exception:
        _log.debug("Разбор погоды на дату не удался", exc_info=True)
        return None
