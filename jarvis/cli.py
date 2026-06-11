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
    # network.target в USER-менеджере systemd не существует (not-found) — директива была
    # пустышкой; сетевую готовность сервисы обеспечивают сами ретраями (лампа — Этап 21).
    if svc.needs_audio:
        deps = "Wants=pipewire.service\nAfter=pipewire.service\n"
    else:
        deps = ""
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
        # Рабочая директория = корень проекта. Страховка: относительные пути (если попадут
        # в конфиг) резолвятся верно даже помимо абсолютизации в config.py. Без неё systemd
        # стартует из $HOME → FileNotFoundError на моделях/commands.yaml (регрессия Этапа 7).
        f"WorkingDirectory={config.BASE_DIR}\n"
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
              "systemd-сессией пользователя.")
        return 1


# --- Подкоманды ------------------------------------------------------------- #
def cmd_doctor(args) -> int:
    from jarvis import doctor

    ok = doctor.run(quick=args.quick)
    return 0 if ok else 1


def cmd_models(args) -> int:
    from jarvis import downloader

    if not args.download:
        print("Укажите действие, например: jarvis models --download "
              "(или --download <имя> для опционального кандидата ASR)")
        return 2
    if args.download is True:   # без имени — все базовые (опциональные пропускаются)
        return 0 if downloader.download_all() else 1
    return 0 if downloader.download_named(str(args.download)) else 1


def cmd_test(args) -> int:
    from jarvis import doctor

    return 0 if doctor.live_chain_test() else 1


def _unit_running(unit: str) -> bool:
    """True, если юнит реально работает (ActiveState=active И SubState=running).

    Парсим key=value (без --value) — устойчиво к порядку полей. Любой сбой → False."""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", unit, "-p", "ActiveState,SubState"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return kv.get("ActiveState") == "active" and kv.get("SubState") == "running"
    except Exception:
        return False


def _settle_and_status() -> bool:
    """Дать сервисам осесть и определить статус запуска по РЕАЛЬНОМУ состоянию юнитов.

    Type=simple → 'active' появляется при СТАРТЕ процесса, не при готовности; даём время на
    загрузку моделей STT и прогрев Piper. Крэш-цикл на инициализации (Restart=always) ловим
    двумя замерами с паузой: упавший сервис не держится 'running' на обоих. Все active/running
    на обоих замерах → успех; иначе → проблема."""
    import time

    time.sleep(5.0)  # старт процессов + загрузка моделей STT и прогрев Piper
    ok = True
    for _ in range(2):
        ok = ok and all(_unit_running(svc.unit) for svc in SERVICES)
        time.sleep(1.2)
    return ok


def _announce_startup(ok: bool) -> None:
    """Озвучить старт по статусу (успех/проблема) фирменной фразой без повторов.

    Публикуем в jarvis/say разовым MQTT-клиентом (как сквозной тест доктора): сервисы уже
    осели и подписаны, TTS прогрет. Всё в try-except: сбой объявления НЕ влияет на `jarvis start`."""
    import json
    import time

    from jarvis import contracts, phrases

    pack = config.STARTUP_SUCCESS_PHRASES if ok else config.STARTUP_PROBLEM_PHRASES
    text = phrases.pick("startup.ok" if ok else "startup.warn", pack)
    if not text:
        return
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="jarvis-start-announce")
    client.connect(config.MQTT_HOST, config.MQTT_PORT, 5)
    client.loop_start()
    try:
        client.publish(contracts.TOPIC_SAY,
                       json.dumps({"text": text, "source": "startup"}),
                       qos=contracts.QOS_SAY)
        time.sleep(1.0)  # дать сообщению долететь до брокера/TTS до disconnect
    finally:
        client.loop_stop()
        client.disconnect()
    status = "успех" if ok else "проблема"
    print(f"✓ Старт объявлен ({status}): {text}")


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
    # Объявить старт голосом по реальному статусу сервисов (успех/проблема). В try-except:
    # сбой объявления НЕ должен менять код возврата `jarvis start` (печать выше остаётся).
    try:
        if config.STARTUP_ANNOUNCE:
            _announce_startup(_settle_and_status())
    except Exception as exc:
        print(f"(объявление старта пропущено: {exc})")
    return 0 if rc == 0 else 1


def cmd_stop(args) -> int:
    rc = 0
    for svc in SERVICES:
        rc |= _systemctl("disable", "--now", svc.unit)
    return 0 if rc == 0 else 1


def _announce_restart(ok: bool) -> None:
    """Озвучить результат перезагрузки (успех/проблема) паком + системное уведомление.

    Голос идёт через jarvis/say (TTS сам решит: в тишине — в уведомление). Плюс ЯВНОЕ уведомление
    (видно всегда). Всё в try-except: сбой объявления не влияет на код возврата."""
    import json
    import time

    from jarvis import contracts, notify, phrases

    pack = config.RESTART_SUCCESS if ok else config.RESTART_PROBLEM
    text = phrases.pick("restart.success" if ok else "restart.problem", pack)
    try:
        notify.notify("Джарвис",
                      "Перезагрузка завершена." if ok else "Перезагрузка прошла с проблемами.",
                      urgency="normal" if ok else "critical")
    except Exception:
        pass
    if not text:
        return
    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="jarvis-restart-announce")
    client.connect(config.MQTT_HOST, config.MQTT_PORT, 5)
    client.loop_start()
    try:
        client.publish(contracts.TOPIC_SAY,
                       json.dumps({"text": text, "source": "restart"}),
                       qos=contracts.QOS_SAY)
        time.sleep(1.0)
    finally:
        client.loop_stop()
        client.disconnect()
    print(f"✓ Перезагрузка объявлена ({'успех' if ok else 'проблема'}): {text}")


def cmd_restart(args) -> int:
    """Перезапуск ВСЕХ сервисов Джарвиса (НЕ ребут ноута) + статус-объявление.

    Голосовой путь запускает эту команду ОТКРЕПЛЁННО (systemd-run --user, своя cgroup), чтобы она
    пережила рестарт jarvis-core. Начальная пауза — дать анонсу core доиграть, пока TTS ещё жив."""
    import time

    try:
        time.sleep(max(0.0, float(config.RESTART_INITIAL_DELAY)))
    except Exception:
        pass
    print("Перезагрузка сервисов Джарвиса…")
    rc = 0
    for svc in SERVICES:
        rc |= _systemctl("restart", svc.unit)
    ok = False
    try:
        ok = _settle_and_status()
    except Exception:
        ok = False
    try:
        _announce_restart(ok and rc == 0)
    except Exception as exc:
        print(f"(объявление перезагрузки пропущено: {exc})")
    return 0 if (rc == 0 and ok) else 1


def cmd_live(args) -> int:
    """Живая панель состояния (jarvis live) — перерисовывается до Ctrl+C."""
    from jarvis import live

    return live.run()


def _lamp_run_action(name: str, creds: dict, action: str, tinytuya) -> bool:
    """Одно действие CLI над одной лампой (по кредам из lamp.lamps). True — успех.

    Управление — БОЕВЫМ DPS-путём сервиса (DP20/21/22/23/24), НЕ set_colour tinytuya:
    высокоуровневые методы шлют DP22 в режиме colour и сбивают лампу в white (грабля v3.5)."""
    from jarvis import lamp as helpers

    ip = str(creds.get("ip", "")).strip()
    if not ip and config.LAMP_AUTODISCOVER:
        print(f"Ищу лампу «{name}» в сети по device_id…")
        try:
            for k, info in (tinytuya.deviceScan(False, 5) or {}).items():
                if creds["device_id"] in (info.get("gwId"), info.get("id")):
                    ip = info.get("ip", k)
                    break
        except Exception:
            pass
    if not ip:
        print(f"✗ «{name}»: IP не задан и не найден автопоиском (укажите ip в lamp.lamps).")
        return False
    try:
        bulb = tinytuya.BulbDevice(creds["device_id"], address=ip,
                                   local_key=creds["local_key"],
                                   version=float(creds.get("version", 3.5)), persist=True)
        bulb.set_socketTimeout(float(config.LAMP_SOCKET_TIMEOUT))
        bulb.set_socketRetryLimit(1)   # без внутренних ретраев tinytuya — быстрый честный фейл
        bulb.set_socketRetryDelay(1)
        bulb.set_socketPersistent(True)  # как у сервиса (для rtt — честный замер без реконнектов)
        st = bulb.status()
        if not isinstance(st, dict) or "Error" in st or "Err" in st:
            print(f"✗ «{name}»: лампа не отвечает: {st}")
            print("  Частая причина — неверная ВЕРСИЯ протокола (lamp.lamps → version).")
            return False
        print(f"✓ «{name}» на связи: {ip} (протокол {creds.get('version', 3.5)}). dps={st.get('dps')}")
        if action == "on":
            bulb.set_value(20, True, nowait=False)
            print("→ включена")
        elif action == "off":
            bulb.set_value(20, False, nowait=False)
            print("→ выключена")
        elif action == "test":
            import time
            print("→ тест (боевой DPS-путь): красный → зелёный → синий → тёплый белый")
            for rgb in [(255, 0, 0), (0, 255, 0), (0, 80, 255)]:
                bulb.set_multiple_values(
                    {20: True, 21: "colour", 24: helpers.rgb_to_v2hex(*rgb, 100)}, nowait=False)
                time.sleep(0.9)
            bulb.set_multiple_values(
                {20: True, 21: "white", 22: helpers.pct_to_dp(60, lo=10),
                 23: helpers.pct_to_dp(40)}, nowait=False)
        elif action == "rtt":
            _lamp_bench_rtt(name, bulb, helpers)
        return True
    except Exception as exc:
        print(f"✗ «{name}»: ошибка связи: {exc}")
        print("  Проверьте ip/local_key и ВЕРСИЮ протокола в settings.yaml (lamp.lamps).")
        return False


def _lamp_bench_rtt(name: str, bulb, helpers) -> None:
    """Микробенч задержки лампы: ACK на DP24-кадр (как кадр анимации) и на status().

    По p50 выбирается потолок кадров анимации lamp.animation.fps_max. Заодно проверяет,
    принимает ли прошивка кадр «только DP24» (без DP20/21) — оптимизация анимации."""
    import statistics
    import time

    rgb = (40, 90, 255)
    # Войти в режим colour полным пакетом (как первый кадр анимации).
    bulb.set_multiple_values({20: True, 21: "colour",
                              24: helpers.rgb_to_v2hex(*rgb, 50)}, nowait=False)
    lat, dp24_ok = [], True
    for i in range(30):
        v = 30 + (i % 2) * 40  # яркость 30↔70% — типичный кадр пульсации
        t = time.perf_counter()
        r = bulb.set_multiple_values({24: helpers.rgb_to_v2hex(*rgb, v)}, nowait=False)
        lat.append((time.perf_counter() - t) * 1000.0)
        if isinstance(r, dict) and ("Error" in r or "Err" in r):
            dp24_ok = False
    st_lat = []
    for _ in range(10):
        t = time.perf_counter()
        bulb.status()
        st_lat.append((time.perf_counter() - t) * 1000.0)
    # Кадр «только DP24» не сломал режим? (status должен остаться в colour)
    st = bulb.status()
    mode_ok = isinstance(st, dict) and (st.get("dps") or {}).get("21") == "colour"
    # Вернуть лампу в фон (тёплый, яркость из background) — не оставлять синей после бенча.
    bg = config.LAMP_BACKGROUND or {}
    bg_rgb = helpers.resolve_color(bg.get("цвет"), config.LAMP_COLORS) or (255, 170, 87)
    bulb.set_multiple_values(
        {20: True, 21: "colour",
         24: helpers.rgb_to_v2hex(*bg_rgb, helpers.clamp_pct(bg.get("яркость"), 100))}, nowait=False)
    p50 = statistics.median(lat)
    p95 = sorted(lat)[max(0, int(len(lat) * 0.95) - 1)]
    fps = max(1, min(12, int(0.8 * 1000.0 / max(p50, 1.0))))
    print(f"→ «{name}» RTT DP24-кадра: p50 {p50:.0f}мс, p95 {p95:.0f}мс, max {max(lat):.0f}мс "
          f"(n={len(lat)}); status(): p50 {statistics.median(st_lat):.0f}мс")
    print(f"→ кадр «только DP24» (без DP20/21): {'работает, режим colour цел' if dp24_ok and mode_ok else 'НЕ работает — анимации нужен полный пакет'}")
    print(f"→ рекомендация: lamp.animation.fps_max ≈ {fps} (0.8 × 1000/p50)")


def cmd_lamp(args) -> int:
    """Прямое управление лампами для проверки кредов (БЕЗ сервисов/голоса): on|off|status|test|rtt.

    --lamp ИМЯ — одна лампа из settings.yaml (lamp.lamps); без флага — все по очереди.
    Помогает убедиться, что device_id/local_key/ip/версия верны; rtt — замер задержки ACK
    (по нему выбирается потолок кадров анимации fps_max)."""
    action = getattr(args, "action", "status")
    devices = dict(config.LAMP_DEVICES)
    want = (getattr(args, "lamp", None) or "").strip()
    if want:
        if want not in devices:
            print(f"✗ Лампа «{want}» не найдена в settings.yaml (есть: {', '.join(devices) or 'ни одной'}).")
            return 1
        devices = {want: devices[want]}
    if not devices:
        print("✗ Лампы не заданы (settings.yaml → lamp.lamps).")
        return 1
    # Tuya держит ОДИН локальный сокет: параллельное подключение CLI может сбить
    # персистентный сокет сервиса (реакции «зависнут» до его реконнекта).
    try:
        if subprocess.run(["systemctl", "--user", "is-active", "--quiet", "jarvis-lamp.service"],
                          timeout=5).returncode == 0:
            print("⚠ Сервис jarvis-lamp активен: параллельное подключение может сбить его сокет "
                  "(лампа Tuya держит одно соединение). Сервис восстановится сам через реконнект.")
    except Exception:
        pass
    try:
        import tinytuya
    except Exception:
        print("✗ tinytuya не установлен. Выполните `pip install -e .`.")
        return 1
    rc = 0
    for name, creds in devices.items():
        try:
            if not _lamp_run_action(name, creds, action, tinytuya):
                rc = 1
        except Exception as exc:
            print(f"✗ «{name}»: неожиданный сбой: {exc}")
            rc = 1
    return rc


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
    p_models.add_argument("--download", nargs="?", const=True, metavar="ИМЯ",
                          help="скачать базовые модели; с ИМЕНЕМ — одну из models.yaml "
                               "(вкл. опциональные кандидаты ASR)")
    p_models.set_defaults(func=cmd_models)

    p_test = sub.add_parser("test", help="сквозной тест живой шины")
    p_test.set_defaults(func=cmd_test)

    sub.add_parser("start", help="установить юниты и запустить сервисы").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="остановить и отключить сервисы").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="статус сервисов").set_defaults(func=cmd_status)
    sub.add_parser("restart", help="перезапустить все сервисы Джарвиса + объявить статус").set_defaults(func=cmd_restart)
    sub.add_parser("live", help="живая панель состояния (до Ctrl+C)").set_defaults(func=cmd_live)
    p_lamp = sub.add_parser("lamp", help="проверка/управление умными лампами (on|off|status|test|rtt)")
    p_lamp.add_argument("action", nargs="?", default="status",
                        choices=["on", "off", "status", "test", "rtt"],
                        help="действие (по умолчанию status; rtt — замер задержки для fps анимации)")
    p_lamp.add_argument("--lamp", metavar="ИМЯ", default="",
                        help="одна лампа из lamp.lamps (по умолчанию — все по очереди)")
    p_lamp.set_defaults(func=cmd_lamp)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
