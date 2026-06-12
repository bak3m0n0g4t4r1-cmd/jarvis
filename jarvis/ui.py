"""Вывод CLI «Джарвиса»: сдержанно и читаемо, без визуального шума.

Палитра — три цвета: зелёный ✓, красный ✗, жёлтый ⚠. Если вывод идёт не в
терминал (пайп, файл) или задан NO_COLOR — деградируем в чистый текст без
ANSI-кодов. Каждый результат дублируется в logs/doctor.log, чтобы вывод
можно было приложить при разборе.
"""
import logging
import os
import shutil
import sys
import textwrap
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler

from jarvis import config

# --- Статусы проверки ---
OK = "ok"
FAIL = "fail"
WARN = "warn"

_SYMBOL = {OK: "✓", FAIL: "✗", WARN: "⚠"}
_COLOR = {OK: "\033[32m", FAIL: "\033[31m", WARN: "\033[33m"}
_RESET = "\033[0m"
_BOLD = "\033[1m"
# Дополнительные тона для CLI-вывода (та же сдержанная гамма): мягкий cyan для
# заголовков и приглушённый dim для второстепенных подсказок.
CYAN = "\033[36m"
DIM = "\033[2m"
BOLD = _BOLD
GREEN = _COLOR[OK]
RED = _COLOR[FAIL]
YELLOW = _COLOR[WARN]


def _use_color() -> bool:
    """Цвет только в настоящий терминал и без NO_COLOR."""
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Переиспользуемый слой цвета и единые строки-статусы для всего CLI
# --------------------------------------------------------------------------- #
def paint(text: str, code: str) -> str:
    """Покрасить текст ANSI-кодом, уважая терминал/NO_COLOR (иначе чистый текст)."""
    return f"{code}{text}{_RESET}" if _use_color() else text


def ok(msg: str) -> None:
    """Печать успешной строки: зелёная галочка."""
    print(paint(f"✓ {msg}", _COLOR[OK]))


def fail(msg: str) -> None:
    """Печать ошибки: красный крест."""
    print(paint(f"✗ {msg}", _COLOR[FAIL]))


def warn(msg: str) -> None:
    """Печать предупреждения: жёлтый знак."""
    print(paint(f"⚠ {msg}", _COLOR[WARN]))


def info(msg: str) -> None:
    """Нейтральная строка-примечание (без цвета, приглушённый маркер)."""
    print(f"… {msg}")


@dataclass
class CheckResult:
    """Результат одной проверки в формате «что → почему → как починить».

    status — OK/FAIL/WARN;
    title  — короткая суть проверки;
    reason — вероятная причина (для FAIL/WARN);
    fix    — точная рабочая команда или правка для починки.
    """

    status: str
    title: str
    reason: str = ""
    fix: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK


def _setup_logger() -> logging.Logger:
    """Логгер CLI: пишет в logs/doctor.log (ротация)."""
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("jarvis-cli")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            config.LOGS_DIR / "doctor.log",
            maxBytes=config.LOG_MAX_BYTES,
            backupCount=config.LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logger.addHandler(handler)
    return logger


class Reporter:
    """Печатает результаты проверок ровными секциями и итоговую сводку."""

    def __init__(self):
        self.color = _use_color()
        self.results: list[CheckResult] = []
        self.log = _setup_logger()
        # Ширина терминала для аккуратного переноса длинных строк
        self.width = shutil.get_terminal_size((80, 24)).columns

    def _c(self, text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if self.color else text

    def section(self, title: str) -> None:
        print()
        print(self._c(f"── {title} ──", _BOLD))

    def report(self, result: CheckResult) -> None:
        """Вывести результат проверки и записать его в лог."""
        self.results.append(result)
        symbol = self._c(_SYMBOL[result.status], _COLOR[result.status])
        print(f"  {symbol} {result.title}")
        self.log.info(
            "[%s] %s | %s | %s",
            result.status.upper(),
            result.title,
            result.reason,
            result.fix,
        )
        if result.status != OK:
            if result.reason:
                self._wrapped("Причина:", result.reason)
            if result.fix:
                self._wrapped("Решение:", result.fix)

    def _wrapped(self, label: str, text: str) -> None:
        """Перенос длинной строки по ширине терминала (колонки не разъезжаются)."""
        prefix = f"      {label} "
        avail = max(20, self.width - len(prefix))
        lines = textwrap.wrap(text, width=avail) or [""]
        print(prefix + lines[0])
        for line in lines[1:]:
            print(" " * len(prefix) + line)

    def note(self, text: str) -> None:
        """Нейтральная строка-примечание (например, для долгой операции)."""
        print(f"  … {text}", flush=True)

    def summary(self) -> bool:
        """Итоговая сводка. Возвращает True, если блокеров (✗) нет."""
        n_ok = sum(1 for r in self.results if r.status == OK)
        n_fail = sum(1 for r in self.results if r.status == FAIL)
        n_warn = sum(1 for r in self.results if r.status == WARN)
        print()
        print(self._c("── Итог ──", _BOLD))
        print(
            "  "
            + self._c(f"✓ {n_ok}", _COLOR[OK])
            + "   "
            + self._c(f"⚠ {n_warn}", _COLOR[WARN])
            + "   "
            + self._c(f"✗ {n_fail}", _COLOR[FAIL])
        )
        if n_fail == 0:
            verdict = "Готов к запуску"
            if n_warn:
                verdict += " (есть предупреждения)"
            print("  " + self._c(verdict, _COLOR[OK]))
            return True
        print("  " + self._c(f"Есть блокеры — проверок с ошибкой: {n_fail}", _COLOR[FAIL]))
        return False
