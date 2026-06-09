"""«Руки» Джарвиса: исполняет команды по тегам из commands.yaml.

Безопасность: запуск только через subprocess.Popen(shell=False) со списком
аргументов. Пользовательский текст в команду НЕ подставляется — только
lookup тега в карте. Неизвестный тег → лог + предупреждение в jarvis/say.
"""
import logging
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import yaml

from jarvis import config, contracts, notify, services_map
from jarvis.bus import JarvisModule

# Парсер сигнала ActionInvoked из `gdbus monitor`: «… ActionInvoked (uint32 50, 'logs:jarvis-tts')».
_ACTION_RE = re.compile(r"ActionInvoked \(uint32 \d+, '([^']*)'\)")
# Whitelist юнитов (без .service) — кнопка открывает лог ТОЛЬКО наших сервисов, не произвольное.
_ALLOWED_UNITS = frozenset(s.unit.removesuffix(".service") for s in services_map.SERVICES)


class OsAgentModule(JarvisModule):
    """«Руки»: запускает системные команды по тегу из jarvis/execute.

    Плюс (ТЗ-6): слушает сигнал ActionInvoked уведомлений — по кнопке «Открыть логи» открывает
    kitty с логом проблемного модуля. Запуск процессов — профиль «Рук», поэтому слушатель здесь."""

    def __init__(self):
        super().__init__("jarvis-os-agent")
        self._commands: dict = {}
        self._notif_proc: subprocess.Popen | None = None
        self._notif_thread = None

    def on_start(self):
        self._load_commands()
        self.subscribe(contracts.TOPIC_EXECUTE, self.on_execute)
        # Слушатель кнопки уведомлений (ActionInvoked → открыть лог модуля в kitty).
        if shutil.which("gdbus") and shutil.which("kitty"):
            self._notif_thread = threading.Thread(
                target=self._notif_action_loop, daemon=True, name="notif-actions")
            self._notif_thread.start()
        else:
            self.log.info("gdbus/kitty не найдены — кнопка «Открыть логи» в уведомлениях недоступна")

    def _notif_action_loop(self):
        """Читает `gdbus monitor` и на ActionInvoked с ключом logs:<unit> открывает лог в kitty.

        Один gdbus-monitor на сервис; ловит клики по ЛЮБЫМ нашим уведомлениям (ключ кодирует модуль).
        Сбой/выход монитора — на WARNING, сервис продолжает исполнять команды."""
        try:
            self._notif_proc = subprocess.Popen(
                ["gdbus", "monitor", "--session", "--dest", "org.freedesktop.Notifications"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            self.log.info("Слушатель кнопок уведомлений запущен (ActionInvoked → kitty)")
            for line in self._notif_proc.stdout:
                if self._stop_event.is_set():
                    break
                m = _ACTION_RE.search(line)
                if not m:
                    continue
                key = m.group(1)
                if key.startswith(notify.ACTION_PREFIX):
                    self._open_logs(key[len(notify.ACTION_PREFIX):])
        except Exception:
            self.log_exc(logging.WARNING, "Слушатель кнопок уведомлений остановлен")

    def _open_logs(self, unit: str):
        """Открыть лог юнита в kitty (journalctl --user). Только наши юниты (whitelist)."""
        name = (unit or "").removesuffix(".service")
        if name not in _ALLOWED_UNITS:
            self.log.warning("Кнопка уведомления: неизвестный юнит %r — игнор", unit)
            return
        cmd = ["kitty", "-e", "bash", "-lc",
               f"journalctl --user -u {name}.service -n 300 -f"]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # kitty — долгоживущее GUI; дожинаем в фоне, чтобы не плодить зомби (как fire-and-forget).
            threading.Thread(target=self._reap, args=(f"kitty-logs:{name}", proc), daemon=True).start()
            self.log.info("Открываю лог %s в kitty по кнопке уведомления", name)
        except Exception:
            self.log_exc(logging.WARNING, "Не удалось открыть лог в kitty")

    def _load_commands(self):
        try:
            with open(config.COMMANDS_FILE, encoding="utf-8") as f:
                self._commands = yaml.safe_load(f) or {}
            self.log.info("Загружено команд из карты: %d", len(self._commands))
        except Exception:
            self.log.exception("Не удалось прочитать %s", config.COMMANDS_FILE)
            self._commands = {}

    def on_execute(self, payload: dict):
        tag = (payload.get("command_tag") or "").strip()
        spec = self._commands.get(tag)
        if not spec:
            self.log.warning("Неизвестный тег команды: %r", tag)
            self.say(f"Сэр, мне неизвестна команда «{tag}».")
            return
        args = spec.get("команда")
        if not isinstance(args, list) or not args:
            self.log.error("Некорректная команда для тега %s: %r", tag, args)
            self.say(f"Сэр, команда «{tag}» настроена неверно.")
            return
        # Команда может требовать примонтированный путь (носитель: флешка/диск). Если пути
        # нет — носитель не подключён: отвечаем в характере и НЕ запускаем (без падения).
        required = spec.get("требует_путь")
        if required and not os.path.exists(str(required)):
            self.log.info("Путь для «%s» отсутствует (%s) — носитель не подключён", tag, required)
            self.say(spec.get("ответ_нет_пути") or "Сэр, похоже, этот носитель не подключён.")
            return
        wait_output = bool(spec.get("ждать_вывод", False))
        self._run(tag, [str(a) for a in args], wait_output)

    def _run(self, tag: str, args: list, wait_output: bool):
        try:
            self.log.info("Запуск %s: %s", tag, args)
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE if wait_output else subprocess.DEVNULL,
                stderr=subprocess.STDOUT if wait_output else subprocess.DEVNULL,
                shell=False,  # никогда не используем shell
            )
        except FileNotFoundError:
            self.log.error("Исполняемый файл не найден: %s", args[0])
            self.say(f"Сэр, не нашёл программу для команды «{tag}».")
            return
        except Exception:
            self.log.exception("Не удалось запустить %s", tag)
            self.say(f"Сэр, не удалось выполнить «{tag}».")
            return

        if wait_output:
            # Ждём завершения в фоне, чтобы не блокировать MQTT-loop
            threading.Thread(
                target=self._wait_and_report, args=(tag, proc), daemon=True
            ).start()
        else:
            # Fire-and-forget тоже дожинаем в фоне: иначе короткие команды
            # (wpctl/brightnessctl/loginctl) копятся зомби-процессами <defunct> —
            # родитель ОБЯЗАН сделать wait(). Поток daemon: короткая команда реапится
            # мгновенно, GUI-приложение держит поток до закрытия и выходу не мешает.
            threading.Thread(
                target=self._reap, args=(tag, proc), daemon=True
            ).start()

    def _reap(self, tag: str, proc: subprocess.Popen) -> None:
        """Дожать fire-and-forget процесс, чтобы не плодить зомби <defunct>.

        Для команд с ждать_вывод:false статус не озвучиваем — важно лишь снять
        процесс из таблицы (wait). Короткая команда завершается сразу, GUI-приложение
        держит поток до своего закрытия. Ненулевой код — на DEBUG (тут не наша забота).
        """
        try:
            code = proc.wait()
            if code != 0:
                self.log.debug("Команда %s завершилась с кодом %s (fire-and-forget)", tag, code)
        except Exception:
            self.log.debug("Не удалось дождать процесс команды %s", tag, exc_info=True)

    def _wait_and_report(self, tag: str, proc: subprocess.Popen):
        try:
            stdout, _ = proc.communicate()
            output = (stdout or b"").decode("utf-8", errors="replace")
            log_path = (
                Path(config.LOGS_DIR)
                / f"cmd_{tag}_{datetime.now():%Y%m%d_%H%M%S}.log"
            )
            log_path.write_text(output, encoding="utf-8")
            if proc.returncode == 0:
                self.say(f"Сэр, команда «{tag}» выполнена. Лог сохранён.")
            else:
                self.say(
                    f"Сэр, команда «{tag}» завершилась с ошибкой "
                    f"(код {proc.returncode}). Лог сохранён."
                )
        except Exception:
            self.log.exception("Ошибка ожидания команды %s", tag)
            self.say(f"Сэр, при выполнении «{tag}» произошёл сбой.")

    def on_stop(self):
        """Погасить слушатель уведомлений (gdbus monitor) при остановке сервиса."""
        proc = self._notif_proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                self.log.debug("Не удалось прервать gdbus monitor", exc_info=True)


def main():
    OsAgentModule().run()


if __name__ == "__main__":
    main()
