"""Системные уведомления Джарвиса через D-Bus (org.freedesktop.Notifications) напрямую gdbus.

`notify-send` на машине НЕТ → шлём через `gdbus call … Notify` (subprocess, без зависимостей).
Кнопка-действие («Открыть логи») — через actions + сигнал ActionInvoked, который ловит os_agent
(services/os_agent.py) и открывает kitty с логом проблемного модуля.

Единый стиль для всех частей Джарвиса: режим тишины (дубль речи), проблемы звука, сбои.
Всё в try-except — уведомление вспомогательное, его сбой не должен ронять вызывающего.
"""
import logging
import re
import subprocess

from jarvis import config

_log = logging.getLogger("jarvis-notify")

_DEST = "org.freedesktop.Notifications"
_PATH = "/org/freedesktop/Notifications"
_APP = "Джарвис"
# Конвенция ключа действия: "logs:<unit>" → os_agent открывает лог этого юнита в kitty.
ACTION_PREFIX = "logs:"
_ID_RE = re.compile(r"uint32\s+(\d+)")  # id из ответа gdbus "(uint32 50,)"
_URGENCY = {"low": 0, "normal": 1, "critical": 2}


def _gv_str(s) -> str:
    """Строка → GVariant-литерал в одинарных кавычках (экранируем обратный слеш и кавычку)."""
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


def notify(title: str, body: str, *, module: str | None = None, urgency: str = "normal",
           replace_id: int = 0, icon: str = "", timeout_ms: int = -1) -> int:
    """Показать системное уведомление. Возвращает id (для замены) или 0 при сбое/выключенных.

    module — ключ юнита (напр. 'jarvis-tts'): добавляет кнопку «Открыть логи» → kitty с этим логом.
    urgency: low|normal|critical. replace_id>0 — заменить прежнее уведомление (без накопления).
    Любой сбой gdbus → 0 (в DEBUG): уведомление вспомогательное, не роняем вызывающего."""
    if not getattr(config, "NOTIFICATIONS_ENABLED", True):
        return 0
    try:
        actions = "@as []"
        if module:
            label = getattr(config, "NOTIFY_LOGS_BUTTON", "Открыть логи") or "Открыть логи"
            actions = f"[{_gv_str(ACTION_PREFIX + module)}, {_gv_str(label)}]"
        urg = _URGENCY.get(urgency, 1)
        hints = f"@a{{sv}} {{'urgency': <byte {urg}>}}"
        cmd = [
            "gdbus", "call", "--session", "--dest", _DEST, "--object-path", _PATH,
            "--method", f"{_DEST}.Notify",
            _APP, str(int(replace_id)), icon or "", str(title or ""), str(body or ""),
            actions, hints, str(int(timeout_ms)),
        ]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=4, check=False)
        if out.returncode != 0:
            _log.debug("gdbus Notify код %s: %s", out.returncode, (out.stderr or "").strip())
            return 0
        m = _ID_RE.search(out.stdout or "")
        return int(m.group(1)) if m else 0
    except Exception:
        _log.debug("Не удалось показать уведомление", exc_info=True)
        return 0
