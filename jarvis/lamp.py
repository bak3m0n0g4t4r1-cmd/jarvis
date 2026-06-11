"""Хелперы умных ламп (ТЗ-8, заход «лампы»): цвет имя→RGB, разбор реакций, нормализация яркости,
гейт голосовой яркости, адресация лампы по имени, сэмплирование огибающей голоса.

Чистые функции (stdlib + voice_volume для парсинга уровней) — БЕЗ tinytuya/MQTT, легко
тестируются. Использует сервис lamp (services/lamp.py), core (гейт) и CLI/doctor. Всё в
try-except: кривое значение в settings.yaml → None/дефолт.
"""
import colorsys
import logging
import re

from jarvis import voice_volume

_log = logging.getLogger("jarvis-lamp")
_DIGITS = re.compile(r"\d+")


def rgb_to_v2hex(r, g, b, bright_pct=100) -> str:
    """RGB(0..255) + яркость(%) → colour_data_v2 hex «HHHHSSSSVVVV» (DP24, сверено на лампе v3.5).

    В режиме colour яркость = компонента V (НЕ DP22): берём тон/насыщенность из RGB, V = яркость%."""
    try:
        h, s, _ = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        v = max(10, min(1000, round(float(bright_pct) * 10)))
        return "%04x%04x%04x" % (round(h * 360), round(s * 1000), v)
    except Exception:
        return "000003e803e8"  # запасной — красный полной яркости


def pct_to_dp(pct, lo=0) -> int:
    """Процент (0..100) → шкала Tuya 0..1000 (яркость lo=10, температура lo=0). Кривое → середина."""
    try:
        return max(lo, min(1000, round(float(pct) * 10)))
    except Exception:
        return 500


def _norm(s) -> str:
    return str(s or "").strip().lower().replace("ё", "е")


def resolve_color(value, colors):
    """Значение цвета → (r, g, b) 0..255 или None. value: имя из карты / [r,g,b] / '#RRGGBB'."""
    try:
        if isinstance(value, (list, tuple)) and len(value) == 3:
            return tuple(int(max(0, min(255, c))) for c in value)
        s = _norm(value)
        if not s:
            return None
        if s.startswith("#") and len(s) == 7:
            return tuple(int(s[i:i + 2], 16) for i in (1, 3, 5))
        for name, rgb in (colors or {}).items():
            if _norm(name) == s and isinstance(rgb, (list, tuple)) and len(rgb) == 3:
                return tuple(int(max(0, min(255, c))) for c in rgb)
        return None
    except Exception:
        _log.debug("resolve_color сбой на %r", value, exc_info=True)
        return None


def clamp_pct(value, default=60) -> int:
    """Привести яркость к 1..100 (%). Кривое значение → дефолт."""
    try:
        return int(max(1, min(100, round(float(value)))))
    except Exception:
        return default


def is_lamp_level_command(text) -> bool:
    """Команда установить ЯРКОСТЬ ламп уровнем («яркость ламп 50», «громкость лампы 30»,
    «лампы наполовину», «лампы на максимум»)? Гейт стоит в core ДО гейта громкости голоса —
    иначе «громкость лампы 50» меняла бы громкость ГОЛОСА (поймано в разведке захода).

    Узкий: требует «ламп» + уровень, причём голые цифры — только при слове «яркост»/«громкост»
    («включи лампу на 5 минут» сюда не попадает). Шаговые «лампы ярче/тусклее» — за матчером."""
    try:
        s = _norm(text)
        if "ламп" not in s or voice_volume.parse_level(text) is None:
            return False
        if "яркост" in s or "громкост" in s:
            return True
        # Без ключевого слова уровень принимаем только СЛОВОМ (доля/«максимум»/«пятьдесят»),
        # и не как часть длительности («выключи лампу через четверть часа»).
        if any(u in s for u in ("час", "минут", "секунд")):
            return False
        return voice_volume.parse_level(_DIGITS.sub(" ", str(text or ""))) is not None
    except Exception:
        return False


def resolve_target(text, names):
    """Имена ламп, упомянутые во фразе (адресация: «выключи вторую лампу» → [«вторая»]).

    Пустой список = адресата нет, команда действует на ВСЕ лампы. Совпадение по ОСНОВЕ имени
    (без окончания: «вторую/второй» → «вторая»); многословные имена — подстрокой."""
    try:
        s = _norm(text)
        if not s:
            return []
        words = s.split()
        found = []
        for name in names or []:
            n = _norm(name)
            if not n:
                continue
            if " " in n:
                if n in s:
                    found.append(name)
                continue
            stem = n[: max(3, len(n) - 2)]
            if any(w.startswith(stem) for w in words):
                found.append(name)
        return found
    except Exception:
        return []


def rgb_to_hs(r, g, b):
    """RGB 0..255 → (тон 0..360, насыщенность 0..1000) — компоненты DP24 без яркости.

    Для кадров анимации: тон/насыщенность считаются один раз на палитру, яркость (V)
    меняется покадрово через hsv_to_v2hex."""
    try:
        h, s, _ = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        return round(h * 360), round(s * 1000)
    except Exception:
        return 0, 1000


def hsv_to_v2hex(h360, s1000, bright_pct) -> str:
    """Компоненты HSV → colour_data_v2 hex «HHHHSSSSVVVV» (DP24). Тон в градусах (любое
    число — нормализуется по кругу), насыщенность 0..1000, яркость в % (V клампится 10..1000)."""
    try:
        h = int(round(float(h360))) % 360
        s = max(0, min(1000, int(round(float(s1000)))))
        v = max(10, min(1000, round(float(bright_pct) * 10)))
        return "%04x%04x%04x" % (h, s, v)
    except Exception:
        return "000003e803e8"  # запасной — красный полной яркости


def level_to_brightness(level, lo, hi, gamma=1.0) -> int:
    """Уровень огибающей 0..1 → яркость % в коридоре [lo..hi] по кривой gamma (<1 — тихие
    звуки заметнее). Кривое значение → lo."""
    try:
        lv = max(0.0, min(1.0, float(level)))
        g = float(gamma) if gamma and float(gamma) > 0 else 1.0
        return clamp_pct(lo + (hi - lo) * (lv ** g), 60)
    except Exception:
        return clamp_pct(lo, 60)


def sample_envelope(levels, t0, win, now, end=None, hold=0.25):
    """Целевой уровень огибающей (0..1) на момент now (epoch-секунды).

    levels — уровни окон шириной win сек от якоря t0 (дыры потерянных батчей = 0).
    До t0 и после end → 0. За последним ИЗВЕСТНЫМ окном (следующий батч ещё едет по шине)
    держим последний уровень hold секунд, дальше 0 — мягкая деградация при потере батча."""
    try:
        if not levels or now < t0 or (end is not None and now > end):
            return 0.0
        i = int((now - t0) / win)
        if 0 <= i < len(levels):
            v = levels[i]
            return min(1.0, max(0.0, float(v))) if isinstance(v, (int, float)) else 0.0
        tail = t0 + len(levels) * win
        if 0 <= now - tail <= hold:
            v = levels[-1]
            return min(1.0, max(0.0, float(v))) if isinstance(v, (int, float)) else 0.0
        return 0.0
    except Exception:
        return 0.0


def reaction(name, reactions, colors):
    """Спецификация реакции события → нормализованный dict или None (выключена/нет/кривая).

    Возврат: {rgb|None, pattern, brightness(%), duration(с), repeats}. rgb=None → реакция только
    яркостью/паттерном на текущем цвете (например, лампа без RGB)."""
    try:
        spec = (reactions or {}).get(name)
        if not isinstance(spec, dict) or not spec.get("вкл", True):
            return None
        return {
            "rgb": resolve_color(spec.get("цвет"), colors),
            "pattern": _norm(spec.get("паттерн")) or "свечение",
            "brightness": clamp_pct(spec.get("яркость"), 60),
            "duration": max(0.0, float(spec.get("длительность", 0) or 0)),
            "repeats": max(1, int(spec.get("повторы", 1) or 1)),
        }
    except Exception:
        _log.debug("reaction spec сбой для %s", name, exc_info=True)
        return None
