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
import time
from datetime import datetime
from pathlib import Path

import yaml

from jarvis import config, contracts, notify, phrases, services_map
from jarvis.bus import JarvisModule

# KWin (Wayland) виртуальные столы: создание/переключение через qdbus6 (сверено на KDE 6.5).
_KWIN = ["qdbus6", "org.kde.KWin"]
_VDM = "/VirtualDesktopManager"
_VDM_IFACE = "org.kde.KWin.VirtualDesktopManager"

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
        self.subscribe(contracts.TOPIC_ENVIRONMENT, self.on_environment)  # рабочие среды (ТЗ-7)
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
        Умер сам gdbus (перезапуск сессии D-Bus и т.п.) → ПЕРЕЗАПУСКАЕМ монитор с паузой — иначе
        кнопка «Открыть логи» была бы мертва до рестарта сервиса (Этап 21)."""
        while not self._stop_event.is_set():
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
                self.log_exc(logging.WARNING, "Слушатель кнопок уведомлений сбоил")
            finally:
                self._reap_notif_proc()
            if self._stop_event.is_set():
                return
            self.log.warning("gdbus monitor завершился — перезапущу слушатель кнопок через 5с")
            if self._stop_event.wait(5):
                return

    def _reap_notif_proc(self):
        """Дожать текущий gdbus monitor (terminate + wait) — без wait() копились бы зомби."""
        proc = self._notif_proc
        self._notif_proc = None
        if proc is None:
            return
        try:
            proc.terminate()
        except Exception:
            self.log.debug("Не удалось прервать gdbus monitor", exc_info=True)
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except Exception:
                self.log.debug("gdbus monitor не дожат", exc_info=True)

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
        # Тема оформления (ТЗ-10): поле «тема» (dark|light|toggle) вместо shell-команды.
        theme = spec.get("тема")
        if theme in ("dark", "light", "toggle"):
            self._apply_theme(theme)
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

    def _apply_theme(self, which: str):
        """Сменить цветовую схему KDE (plasma-apply-colorscheme). toggle — читаем текущую, ставим иную.
        Схемы — из config (THEME_DARK_SCHEME/LIGHT_SCHEME). Сбой → внятный голос, не падаем."""
        dark, light = config.THEME_DARK_SCHEME, config.THEME_LIGHT_SCHEME
        try:
            if which == "toggle":
                cur = self._current_scheme()
                target = light if (cur and cur.lower() == str(dark).lower()) else dark
            else:
                target = dark if which == "dark" else light
            r = subprocess.run(["plasma-apply-colorscheme", target],
                               capture_output=True, text=True, timeout=8)
            if r.returncode == 0:
                self.log.info("Тема применена: %s", target)
            else:
                self.log.warning("Тема: код %s (%s)", r.returncode, (r.stderr or "").strip()[:120])
                self.say(phrases.pick("theme.fail", config.THEME_FAIL))
        except FileNotFoundError:
            self.log.error("plasma-apply-colorscheme не найден — смена темы недоступна")
            self.say(phrases.pick("theme.fail", config.THEME_FAIL))
        except Exception:
            self.log_exc(logging.WARNING, "Сбой смены темы")
            self.say(phrases.pick("theme.fail", config.THEME_FAIL))

    @staticmethod
    def _current_scheme():
        """Имя текущей цветовой схемы (маркер «(текущая…)» / «(current…)» в --list-schemes) или None."""
        try:
            out = subprocess.run(["plasma-apply-colorscheme", "--list-schemes"],
                                 capture_output=True, text=True, timeout=4).stdout
            for line in out.splitlines():
                low = line.lower()
                if "текущ" in low or "current" in low:
                    m = re.search(r"\*?\s*(\S+)\s*\(", line)
                    if m:
                        return m.group(1)
        except Exception:
            pass
        return None

    def on_environment(self, payload: dict):
        """Рабочая среда (ТЗ-7): обработка в потоке — внутри паузы ENV_LAUNCH_DELAY на каждое
        приложение, а MQTT-колбэк блокировать нельзя (следующие команды ждали бы секунды)."""
        threading.Thread(target=self._open_environment, args=(payload,),
                         daemon=True, name="os-environment").start()

    def _open_environment(self, payload: dict):
        """Создать новый вирт. стол KDE, переключиться, запустить приложения.

        Wayland: окна открываются на ТЕКУЩЕМ столе → создаём стол, переключаемся, ЗАТЕМ запускаем
        приложения (с паузами). Приложения — по тегам из своей карты команд (не произвольный shell).
        Частичный сбой (нет приложения / стол не создался) → запускаем что можно, не падаем."""
        try:
            desktop = (payload.get("desktop") or config.ENV_DESKTOP_PREFIX).strip()
            apps = [a for a in (payload.get("apps") or []) if a in self._commands]
            if not apps:
                self.log.info("Среда «%s»: нет валидных приложений в %r", desktop, payload.get("apps"))
                self.say("Сэр, в этой среде нечего открывать.")
                return
            created = self._kwin_new_desktop(desktop)
            self.log.info("Среда «%s»: стол %s, приложения %s", desktop, created or "(текущий)", apps)
            failed = []
            for tag in apps:
                spec = self._commands.get(tag) or {}
                args = spec.get("команда")
                if not isinstance(args, list) or not args:
                    failed.append(tag)
                    continue
                self._run(tag, [str(a) for a in args], bool(spec.get("ждать_вывод", False)))
                time.sleep(max(0.0, float(config.ENV_LAUNCH_DELAY)))  # дать окну открыться на новом столе
            if failed:
                self.log.warning("Среда «%s»: не запущены %s", desktop, failed)
        except Exception:
            self.log_exc(logging.WARNING, "Сбой открытия рабочей среды")

    def _kwin_new_desktop(self, name: str):
        """Создать виртуальный стол KDE (qdbus6) и переключиться на него. Возвращает индекс или None.

        Сбой (нет qdbus6/KWin) → None: приложения откроются на ТЕКУЩЕМ столе (мягкая деградация)."""
        try:
            cnt = subprocess.run(_KWIN + [_VDM, f"{_VDM_IFACE}.count"],
                                 capture_output=True, text=True, timeout=3)
            n = int((cnt.stdout or "").strip())
            subprocess.run(_KWIN + [_VDM, f"{_VDM_IFACE}.createDesktop", str(n), str(name)],
                           capture_output=True, timeout=3)
            time.sleep(0.3)
            subprocess.run(_KWIN + ["/KWin", "org.kde.KWin.setCurrentDesktop", str(n + 1)],
                           capture_output=True, timeout=3)
            return n + 1
        except Exception:
            self.log_exc(logging.WARNING,
                         "Не удалось создать виртуальный стол — открою на текущем")
            return None

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
        self._reap_notif_proc()


def main():
    OsAgentModule().run()


if __name__ == "__main__":
    main()
