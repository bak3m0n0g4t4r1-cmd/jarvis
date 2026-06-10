"""Хелперы умной лампы (ТЗ-8): цвет имя→RGB, разбор спецификации реакции, нормализация яркости.

Чистые функции (только stdlib) — БЕЗ tinytuya/MQTT, легко тестируются. Использует сервис lamp
(services/lamp.py) и CLI/doctor. Всё в try-except: кривое значение в settings.yaml → None/дефолт.
"""
import colorsys
import logging

_log = logging.getLogger("jarvis-lamp")


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
