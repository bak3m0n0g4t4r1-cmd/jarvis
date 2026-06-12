"""CLI «Джарвиса»: установка-обёртка, глубокая диагностика и управление.

Подкоманды (сгруппированы в фирменной справке — `jarvis` или `jarvis help`):
  Запуск:      start · stop · restart · live
  Диагностика: doctor [--quick] · status · test
  Устройства:  lamp (on|off|status|test|rtt|sync) · models (--download | --quantize)

Без аргументов (или `jarvis help`) показывает опрятную справку; `jarvis <команда> -h`
раскрывает детали конкретной команды.

Сам CLI становится доступен только ПОСЛЕ `pip install -e .`, поэтому первичную
установку делает bootstrap.sh, а CLI берёт на себя всё остальное.
"""
import argparse
import subprocess
import sys
from pathlib import Path

from jarvis import config, ui
from jarvis.services_map import SERVICES


# --- Фирменная справка ------------------------------------------------------ #
# Единый источник описаний команд: и для сгруппированной справки (jarvis / jarvis
# help), и для argparse-help= (см. build_parser). Так справка и -h не разъезжаются.
COMMAND_GROUPS = [
    ("Запуск", [
        ("start", "поднять/перезапустить сервисы"),
        ("stop", "остановить и отключить сервисы"),
        ("restart", "перезапустить все сервисы + объявить статус"),
        ("live", "живая панель состояния (до Ctrl+C)"),
    ]),
    ("Диагностика", [
        ("doctor", "полная проверка здоровья системы"),
        ("status", "статус сервисов"),
        ("test", "сквозной тест живой шины"),
    ]),
    ("Устройства и модели", [
        ("lamp", "проверка/управление лампами (on|off|status|test|rtt|sync)"),
        ("models", "загрузка моделей"),
        ("tts", "кэш голоса: сборка/статистика (build|stats)"),
        ("say", "произнести фразу голосом Джарвиса (проверка)"),
    ]),
]
_HELP = {name: desc for _group, _cmds in COMMAND_GROUPS for name, desc in _cmds}


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
        ui.fail("systemctl не найден. Управление сервисами доступно только под "
                "systemd-сессией пользователя.")
        return 1


# --- Подкоманды ------------------------------------------------------------- #
def cmd_doctor(args) -> int:
    from jarvis import doctor

    ok = doctor.run(quick=args.quick)
    return 0 if ok else 1


def cmd_models(args) -> int:
    # Квантование эмбеддера команд в INT8 (×4 меньше RAM на N100). Разовая операция.
    # tools/ не входит в установленный пакет — грузим по пути (надёжно при любом CWD).
    if getattr(args, "quantize", False):
        import importlib.util

        from jarvis import config
        path = config.BASE_DIR / "tools" / "quantize_embedder.py"
        spec = importlib.util.spec_from_file_location("quantize_embedder", str(path))
        if spec is None or spec.loader is None:
            print(f"Не найден инструмент квантования: {path}")
            return 1
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.main()

    from jarvis import downloader

    if not args.download:
        print("Укажите действие, например: jarvis models --download "
              "(или --download <имя> для опционального кандидата ASR; --quantize — INT8-эмбеддер)")
        return 2
    if args.download is True:   # без имени — все базовые (опциональные пропускаются)
        return 0 if downloader.download_all() else 1
    return 0 if downloader.download_named(str(args.download)) else 1


def cmd_tts(args) -> int:
    """Кэш голоса: build — пред-рендер фраз (Silero + JARVIS-DSP), stats — покрытие/чистка."""
    from jarvis import tts_prerender

    if getattr(args, "action", "stats") == "build":
        res = tts_prerender.build(only=getattr(args, "only", None),
                                  force=getattr(args, "force", False),
                                  limit=getattr(args, "limit", None))
        return 0 if res.get("errors", 0) == 0 else 1
    tts_prerender.stats(prune=getattr(args, "prune", False))
    return 0


def cmd_say(args) -> int:
    """Произнести фразу голосом Джарвиса офлайн (без сервисов): хит кэша или ленивый синтез.

    Удобно для проверки голоса/DSP/ударений и для прогрева конкретной фразы в кэш."""
    text = " ".join(args.text).strip()
    if not text:
        ui.fail('Укажите фразу: jarvis say "Системы в норме, сэр."')
        return 2
    from jarvis import speech, tts_cache, tts_dsp, tts_engine

    final = speech.apply_stress(
        speech.apply_pronunciation(text, config.PRONUNCIATION), config.STRESS_TABLE)
    engine = tts_engine.SileroEngine()
    dsp_sig = tts_dsp.dsp_signature(config.DSP_PARAMS, engine.voice_id)
    cache = tts_cache.TtsCache(config.TTS_CACHE_DIR, engine.voice_id, dsp_sig)
    clip = cache.get(final)
    if clip is not None:
        ui.ok("Из кэша (мгновенно).")
        pcm, rate = clip.pcm, clip.rate
    else:
        ui.info("Нет в кэше — синтезирую (Silero + JARVIS-DSP)…")
        engine.warmup()
        pcm = engine.synth(final)
        if not pcm:
            ui.fail("Синтез не дал звука.")
            return 1
        rate = engine.sample_rate
        try:
            pcm = tts_dsp.apply_dsp(pcm, rate, config.DSP_PARAMS, rate)
        except Exception as exc:
            ui.warn(f"DSP пропущен: {exc}")
        cache.put(final, pcm, rate)
        engine.unload()
    cmd = ["pw-cat", "-p", "--raw", "--rate", str(rate), "--channels", "1", "--format", "s16",
           "--volume", "0.9", "-"]
    if config.TTS_SINK:
        cmd += ["--target", config.TTS_SINK]
    try:
        subprocess.run(cmd, input=pcm)
    except FileNotFoundError:
        ui.fail("pw-cat не найден (PipeWire).")
        return 1
    return 0


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
    ui.ok(f"Старт объявлен ({status}): {text}")


def cmd_start(args) -> int:
    units_dir = _install_units()
    ui.ok(f"Юниты обновлены: {units_dir}")
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
    ui.ok(f"Перезагрузка объявлена ({'успех' if ok else 'проблема'}): {text}")


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


def _lamp_run_action(name: str, creds: dict, action: str, tinytuya, args=None) -> bool:
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
        ui.fail(f"«{name}»: IP не задан и не найден автопоиском (укажите ip в lamp.lamps).")
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
            ui.fail(f"«{name}»: лампа не отвечает: {st}")
            print("  Частая причина — неверная ВЕРСИЯ протокола (lamp.lamps → version).")
            return False
        ui.ok(f"«{name}» на связи: {ip} (протокол {creds.get('version', 3.5)}). dps={st.get('dps')}")
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
        elif action == "sync":
            _lamp_calibrate_sync(name, bulb, helpers, args)
        return True
    except Exception as exc:
        ui.fail(f"«{name}»: ошибка связи: {exc}")
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


def _lamp_calibrate_sync(name: str, bulb, helpers, args) -> None:
    """Калибровка синхронности «свет↔звук»: метроном из щелчков (pw-cat) + вспышка лампы с
    регулируемым УПРЕЖДЕНИЕМ. Пользователь глазом/ухом ловит совпадение вспышки со щелчком —
    найденное упреждение и есть `lamp.animation.опережение_мс`.

    Привязка звука — как в сервисе: щелчок аудио-позиции p слышен в (метка перед write) +
    старт_задержка_мс + p (буфер pw-cat). Вспышку шлём на `опережение` раньше звука. Если
    оценка старт_задержка_мс верна, совпадение наступает ровно при опережение = задержка
    конвейера лампы → найденный offset переносится в опережение_мс БЕЗ пересчёта."""
    import threading
    import time

    import numpy as np

    rate = 22050
    warmup = 0.5                         # прогрев pw-cat тишиной — стабильная латентность вывода
    period = max(0.6, float(getattr(args, "period", None) or 1.2))
    sweep = bool(getattr(args, "sweep", False))
    l_audio = float(config.LAMP_ANIM_START_OFFSET_MS) / 1000.0   # оценка латентности pw-cat (= t0)
    # Перебор или фикс упреждения (мс). Дефолт фикс — текущее опережение из настроек.
    if sweep:
        seq_ms = list(range(0, 221, 20))                         # 0…220мс шагом 20
    else:
        base = getattr(args, "offset", None)
        base = float(config.LAMP_ANIM_LOOKAHEAD_MS) if base is None else float(base)
        seq_ms = [base] * int(getattr(args, "beats", None) or 8)
    beats = len(seq_ms)

    def _click() -> np.ndarray:
        n = int(rate * 0.03)             # резкий щелчок 30мс, 1500Гц, с микро-fade от трещин
        t = np.arange(n) / rate
        w = np.sin(2 * np.pi * 1500.0 * t)
        fade = max(1, int(rate * 0.004))
        env = np.ones(n)
        env[:fade] = np.linspace(0, 1, fade)
        env[-fade:] = np.linspace(1, 0, fade)
        return w * env * 0.8

    click = _click()
    tail = np.zeros(max(1, int(rate * period) - len(click)))     # тишина до конца такта
    beat = np.concatenate([click, tail])
    buf = np.concatenate([np.zeros(int(rate * warmup))] + [beat] * beats)
    pcm = np.clip(buf * 32767, -32768, 32767).astype(np.int16).tobytes()

    flash = helpers.rgb_to_v2hex(40, 90, 255, 100)               # яркая вспышка (голубая, V=100%)
    dark = helpers.rgb_to_v2hex(40, 90, 255, 2)                  # почти тёмный фон между тактами
    print(f"→ «{name}» калибровка синхронности: {beats} тактов по {period:.1f}с"
          + (" (СВИП упреждения 0…220мс)" if sweep else f", упреждение {seq_ms[0]:.0f}мс"))
    print("  Смотри/слушай: вспышка лампы должна совпасть со щелчком. Свет опаздывает →")
    print("  увеличь опережение_мс; свет убегает вперёд → уменьши. Ctrl+C — стоп.")
    bulb.set_multiple_values({20: True, 21: "colour", 24: dark}, nowait=False)

    cmd = ["pw-cat", "-p", "--raw", "--rate", str(rate), "--channels", "1", "--format", "s16",
           "--volume", "0.8", "--latency", f"{config.TTS_LATENCY_MS}ms", "-"]
    if config.TTS_SINK:
        cmd += ["--target", config.TTS_SINK]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    holder: dict = {}

    def _writer():
        holder["t0"] = time.time()       # метка ПЕРЕД первым write — якорь аудиопотока
        try:
            proc.stdin.write(pcm)
            proc.stdin.close()
        except Exception:
            pass

    th = threading.Thread(target=_writer, daemon=True, name="sync-writer")
    th.start()
    while "t0" not in holder:
        time.sleep(0.001)
    t0 = holder["t0"]
    try:
        for k in range(beats):
            off = seq_ms[k] / 1000.0
            t_sound = t0 + warmup + k * period + l_audio          # когда щелчок слышен
            t_flash = t_sound - off                               # вспышку — на упреждение раньше
            dt = t_flash - time.time()
            if dt > 0:
                time.sleep(dt)
            if sweep:
                print(f"  такт {k + 1}/{beats}: упреждение {seq_ms[k]:.0f}мс")
            bulb.set_multiple_values({24: flash}, nowait=False)   # вспышка
            time.sleep(0.12)
            bulb.set_multiple_values({24: dark}, nowait=False)    # гашение
    except KeyboardInterrupt:
        print("\n  прервано.")
    finally:
        try:
            th.join(timeout=max(2.0, period))
        except Exception:
            pass
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        # Вернуть лампу в фон (тёплый из настроек) — не оставлять тёмной/синей.
        bg = config.LAMP_BACKGROUND or {}
        bg_rgb = helpers.resolve_color(bg.get("цвет"), config.LAMP_COLORS) or (255, 170, 87)
        bulb.set_multiple_values(
            {20: True, 21: "colour",
             24: helpers.rgb_to_v2hex(*bg_rgb, helpers.clamp_pct(bg.get("яркость"), 100))},
            nowait=False)
    if sweep:
        print("→ запомни такт, где вспышка совпала со щелчком, и поставь его упреждение в "
              "lamp.animation.опережение_мс.")
    else:
        print(f"→ если совпало — оставь lamp.animation.опережение_мс = {seq_ms[0]:.0f}; "
              "иначе подбери (--offset МС) или прогони --sweep.")


def cmd_lamp(args) -> int:
    """Прямое управление лампами для проверки кредов (БЕЗ сервисов/голоса): on|off|status|test|rtt|sync.

    --lamp ИМЯ — одна лампа из settings.yaml (lamp.lamps); без флага — все по очереди.
    Помогает убедиться, что device_id/local_key/ip/версия верны; rtt — замер задержки ACK
    (по нему выбирается потолок кадров анимации fps_max); sync — калибровка синхронности
    анимации со звуком (метроном + вспышки с регулируемым упреждением → опережение_мс)."""
    action = getattr(args, "action", "status")
    devices = dict(config.LAMP_DEVICES)
    want = (getattr(args, "lamp", None) or "").strip()
    if want:
        if want not in devices:
            ui.fail(f"Лампа «{want}» не найдена в settings.yaml (есть: {', '.join(devices) or 'ни одной'}).")
            return 1
        devices = {want: devices[want]}
    if not devices:
        ui.fail("Лампы не заданы (settings.yaml → lamp.lamps).")
        return 1
    # Tuya держит ОДИН локальный сокет: параллельное подключение CLI может сбить
    # персистентный сокет сервиса (реакции «зависнут» до его реконнекта).
    try:
        if subprocess.run(["systemctl", "--user", "is-active", "--quiet", "jarvis-lamp.service"],
                          timeout=5).returncode == 0:
            ui.warn("Сервис jarvis-lamp активен: параллельное подключение может сбить его сокет "
                    "(лампа Tuya держит одно соединение). Сервис восстановится сам через реконнект.")
    except Exception:
        pass
    try:
        import tinytuya
    except Exception:
        ui.fail("tinytuya не установлен. Выполните `pip install -e .`.")
        return 1
    rc = 0
    for name, creds in devices.items():
        try:
            if not _lamp_run_action(name, creds, action, tinytuya, args):
                rc = 1
        except Exception as exc:
            ui.fail(f"«{name}»: неожиданный сбой: {exc}")
            rc = 1
    return rc


def cmd_status(args) -> int:
    return _systemctl("--no-pager", "status", *[svc.unit for svc in SERVICES])


def cmd_help(args=None) -> int:
    """Фирменная справка: команды сгруппированы и снабжены коротким описанием.

    Печатается на `jarvis` без аргументов, `jarvis help`, `jarvis -h`/`--help`.
    Палитра — из ui.py (мягкий cyan на заголовках групп), уважает NO_COLOR/не-tty."""
    width = max(len(name) for name in _HELP)
    print()
    print("  " + ui.paint("Джарвис", ui.BOLD) + " — голосовой пульт")
    for group, cmds in COMMAND_GROUPS:
        print()
        print("  " + ui.paint(group, ui.CYAN))
        for name, desc in cmds:
            print(f"    {name.ljust(width)}  {desc}")
    print()
    print("  " + ui.paint("Подробнее:", ui.DIM) + " jarvis <команда> -h")
    print()
    return 0


class _FriendlyParser(argparse.ArgumentParser):
    """argparse, но без сухого usage-краша: подсказывает `jarvis help`."""

    def error(self, message: str):
        ui.fail(message)
        ui.info("Список команд: jarvis help")
        sys.exit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = _FriendlyParser(
        prog="jarvis",
        description="Управление и диагностика голосового ассистента «Джарвис».",
        add_help=False,  # верхнеуровневый -h/--help перехватываем в main() → cmd_help
    )
    # Команды не обязательны: пустой вызов показывает фирменную справку (main).
    sub = parser.add_subparsers(dest="command", required=False)

    p_doctor = sub.add_parser("doctor", help=_HELP["doctor"])
    p_doctor.add_argument(
        "--quick", action="store_true",
        help="быстро: пропустить долгие тесты (синтез голоса, сквозная цепочка)",
    )
    p_doctor.add_argument(
        "--deep", action="store_true",
        help="устар.: дефолт уже полный (флаг ничего не меняет)",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_models = sub.add_parser("models", help=_HELP["models"])
    p_models.add_argument("--download", nargs="?", const=True, metavar="ИМЯ",
                          help="скачать базовые модели; с ИМЕНЕМ — одну из models.yaml "
                               "(вкл. опциональные кандидаты ASR)")
    p_models.add_argument("--quantize", action="store_true",
                          help="квантовать эмбеддер команд в INT8 (×4 меньше RAM на N100)")
    p_models.set_defaults(func=cmd_models)

    p_test = sub.add_parser("test", help=_HELP["test"])
    p_test.set_defaults(func=cmd_test)

    p_tts = sub.add_parser("tts", help=_HELP["tts"])
    p_tts.add_argument("action", nargs="?", default="stats", choices=["build", "stats"],
                       help="build — пред-рендер фраз в кэш; stats — покрытие/объём (по умолчанию)")
    p_tts.add_argument("--only", choices=["static", "dynamic"], default=None,
                       help="build: только статика (литералы) или только динамика (числа/время)")
    p_tts.add_argument("--force", action="store_true",
                       help="build: пересоздать даже уже закэшированные фразы")
    p_tts.add_argument("--limit", type=int, default=None, metavar="N",
                       help="build: потолок числа синтезов за прогон (частичная сборка/проверка)")
    p_tts.add_argument("--prune", action="store_true",
                       help="stats: удалить кэш других сигнатур голос/DSP (осиротевший)")
    p_tts.set_defaults(func=cmd_tts)

    p_say = sub.add_parser("say", help=_HELP["say"])
    p_say.add_argument("text", nargs="+", help="фраза для озвучивания (в кавычках)")
    p_say.set_defaults(func=cmd_say)

    sub.add_parser("start", help=_HELP["start"]).set_defaults(func=cmd_start)
    sub.add_parser("stop", help=_HELP["stop"]).set_defaults(func=cmd_stop)
    sub.add_parser("status", help=_HELP["status"]).set_defaults(func=cmd_status)
    sub.add_parser("restart", help=_HELP["restart"]).set_defaults(func=cmd_restart)
    sub.add_parser("live", help=_HELP["live"]).set_defaults(func=cmd_live)
    sub.add_parser("help", help="показать эту справку").set_defaults(func=cmd_help)
    p_lamp = sub.add_parser("lamp", help=_HELP["lamp"])
    p_lamp.add_argument("action", nargs="?", default="status",
                        choices=["on", "off", "status", "test", "rtt", "sync"],
                        help="действие (по умолчанию status; rtt — замер задержки ACK; "
                             "sync — калибровка синхронности анимации со звуком)")
    p_lamp.add_argument("--lamp", metavar="ИМЯ", default="",
                        help="одна лампа из lamp.lamps (по умолчанию — все по очереди)")
    p_lamp.add_argument("--offset", type=float, default=None, metavar="МС",
                        help="sync: упреждение света над звуком, мс (дефолт — текущее опережение_мс)")
    p_lamp.add_argument("--sweep", action="store_true",
                        help="sync: автоперебор упреждения 0…220мс по тактам (ловить совпадение)")
    p_lamp.add_argument("--beats", type=int, default=8, metavar="N",
                        help="sync: число тактов метронома (по умолчанию 8)")
    p_lamp.add_argument("--period", type=float, default=1.2, metavar="С",
                        help="sync: период метронома в секундах (по умолчанию 1.2)")
    p_lamp.set_defaults(func=cmd_lamp)

    return parser


def main() -> None:
    argv = sys.argv[1:]
    # Пустой вызов и верхнеуровневый запрос справки → фирменная сгруппированная справка
    # (а не сухой usage argparse). `jarvis <команда> -h` сюда не попадает — там argv[0]
    # это команда, и -h обрабатывает её собственный субпарсер.
    if not argv or argv[0] in ("help", "-h", "--help"):
        sys.exit(cmd_help())
    parser = build_parser()
    args = parser.parse_args(argv)
    # Подстраховка: команды нет (например, передали только флаг) — показываем справку.
    if not getattr(args, "func", None):
        sys.exit(cmd_help())
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
