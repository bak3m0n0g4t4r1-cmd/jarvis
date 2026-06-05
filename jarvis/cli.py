"""CLI «Джарвиса»: установка-обёртка, глубокая диагностика и управление.

Подкоманды:
  jarvis doctor [--quick]  — полная проверка здоровья (см. jarvis/doctor.py)
  jarvis models --download — загрузка моделей по models.yaml
  jarvis test              — сквозной тест живой шины (say → execute → input)
  jarvis start|stop|status — обёртка над systemctl --user по юнитам Джарвиса

Сам CLI становится доступен только ПОСЛЕ `pip install -e .`, поэтому первичную
установку делает bootstrap.sh, а CLI берёт на себя всё остальное.
"""
import argparse
import subprocess
import sys
from pathlib import Path

from jarvis import config
from jarvis.services_map import SERVICES


# --- Генерация и установка systemd --user-юнитов ---------------------------- #
def _venv_bin_dir() -> Path:
    """Каталог с бинарями текущего venv.

    Берём от sys.prefix (корень venv), а НЕ через resolve() исполняемого файла:
    .venv/bin/python обычно симлинк на системный python, и resolve() увёл бы нас
    в /usr/bin. Console-скрипты (jarvis-*) лежат именно в sys.prefix/bin.
    """
    return Path(sys.prefix) / "bin"


def _user_units_dir() -> Path:
    """Каталог пользовательских systemd-юнитов (с учётом XDG_CONFIG_HOME)."""
    import os

    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def _render_unit(svc, bin_dir: Path) -> str:
    """Текст юнита с ExecStart на реальный бинарь venv (без хардкода путей)."""
    exec_start = bin_dir / svc.command
    # «Мозг» подхватывает необязательные JARVIS_*-оверрайды из .env проекта (пороги
    # матчера, лог-уровень и т.п.) — прокидываем файл в юнит. Дефис у пути: если .env
    # нет, юнит не падает (все параметры имеют дефолты в config.py). Секретов в .env
    # больше нет — облако удалено.
    env_line = ""
    if svc.key == "core":
        env_line = f"EnvironmentFile=-{config.BASE_DIR / '.env'}\n"
    # Аудио-сервисам (STT/TTS) нужен пользовательский PipeWire: без этой зависимости
    # они стартуют до сессии звука, падают на устройстве и крутятся на рестартах.
    if svc.needs_audio:
        deps = "Wants=pipewire.service\nAfter=pipewire.service network.target\n"
    else:
        deps = "After=network.target\n"
    return (
        "[Unit]\n"
        f"Description=Джарвис — {svc.description}\n"
        f"{deps}"
        # Крэш-бурст переживаем и поднимаемся заново; но жёсткий цикл падений
        # ограничиваем окном, чтобы он был ВИДЕН (а не маскировался бесконечными
        # рестартами) — doctor отдельно WARN'ит при NRestarts≥5.
        "StartLimitIntervalSec=300\n"
        "StartLimitBurst=10\n\n"
        "[Service]\n"
        "Type=simple\n"
        # Учёт памяти (без лимита): видно потребление в `systemctl --user status`.
        # Жёсткий MemoryMax не ставим — на 8 ГБ впритык это грозит OOM-kill голосового
        # конвейера; лёгкость достигается урезанием футпринта в коде (ленивые загрузки).
        "MemoryAccounting=true\n"
        f"{env_line}"
        f"ExecStart={exec_start}\n"
        "Restart=always\n"
        "RestartSec=3\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _install_units() -> Path:
    """Сгенерировать/обновить юниты с актуальными путями. Идемпотентно."""
    units_dir = _user_units_dir()
    units_dir.mkdir(parents=True, exist_ok=True)
    bin_dir = _venv_bin_dir()
    for svc in SERVICES:
        (units_dir / svc.unit).write_text(_render_unit(svc, bin_dir), encoding="utf-8")
    return units_dir


def _systemctl(*args: str) -> int:
    """Вызов `systemctl --user ...`; дружелюбно сообщает, если его нет."""
    try:
        return subprocess.call(["systemctl", "--user", *args])
    except FileNotFoundError:
        print("✗ systemctl не найден. Управление сервисами доступно только под "
              "systemd-сессией пользователя (на целевой Kali).")
        return 1


# --- Подкоманды ------------------------------------------------------------- #
def cmd_doctor(args) -> int:
    from jarvis import doctor

    ok = doctor.run(quick=args.quick)
    return 0 if ok else 1


def cmd_models(args) -> int:
    from jarvis import downloader

    if not args.download:
        print("Укажите действие, например: jarvis models --download")
        return 2
    return 0 if downloader.download_all() else 1


def cmd_test(args) -> int:
    from jarvis import doctor

    return 0 if doctor.live_chain_test() else 1


def cmd_start(args) -> int:
    units_dir = _install_units()
    print(f"✓ Юниты обновлены: {units_dir}")
    _systemctl("daemon-reload")
    rc = 0
    for svc in SERVICES:
        # enable — автозапуск при логине (без --now, чтобы не было гонки со restart).
        # restart — идемпотентно: не запущен → стартует, запущен → перезапускает
        # с новым кодом (старый --now этого не делал — крутился старый процесс).
        rc |= _systemctl("enable", svc.unit)
        rc |= _systemctl("restart", svc.unit)
    return 0 if rc == 0 else 1


def cmd_stop(args) -> int:
    rc = 0
    for svc in SERVICES:
        rc |= _systemctl("disable", "--now", svc.unit)
    return 0 if rc == 0 else 1


def cmd_status(args) -> int:
    return _systemctl("--no-pager", "status", *[svc.unit for svc in SERVICES])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description="Управление и диагностика голосового ассистента «Джарвис».",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="полная проверка здоровья системы")
    p_doctor.add_argument(
        "--quick", action="store_true",
        help="быстро: пропустить долгие тесты (синтез Piper, сквозная цепочка)",
    )
    p_doctor.add_argument(
        "--deep", action="store_true",
        help="устар.: дефолт уже полный (флаг ничего не меняет)",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_models = sub.add_parser("models", help="управление моделями")
    p_models.add_argument("--download", action="store_true", help="скачать модели в models/")
    p_models.set_defaults(func=cmd_models)

    p_test = sub.add_parser("test", help="сквозной тест живой шины")
    p_test.set_defaults(func=cmd_test)

    sub.add_parser("start", help="установить юниты и запустить сервисы").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="остановить и отключить сервисы").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="статус сервисов").set_defaults(func=cmd_status)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
