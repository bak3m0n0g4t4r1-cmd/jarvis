"""Чтение состояния системы (read-only, без shell-инъекций).

Маленький набор офлайн-функций для встроенных info-ответов «Джарвиса» (заряд
батареи) и для диагностики (загрузка CPU/RAM в `jarvis doctor`). Раньше эти
функции жили в облачном brain.py (как инструменты Gemini); после перехода на
локальный пульт облако удалено, а полезные read-only пробы остались здесь.

Каждая функция возвращает компактный dict на русском и НИКОГДА не бросает: при
сбое — {"ошибка": ...}, чтобы вызывающий мог деградировать в характере.
"""
import os


def read_system_load() -> dict:
    """Загрузка CPU (среднее за 1 мин) и использование памяти — из /proc, без psutil."""
    result: dict = {}
    try:
        # loadavg за 1 минуту — без мгновенного замера и без сна (дёшево на N100).
        with open("/proc/loadavg", encoding="utf-8") as f:
            load1 = float(f.read().split()[0])
        cores = os.cpu_count() or 1
        result["загрузка_cpu_1мин"] = round(load1, 2)
        result["ядер"] = cores
        result["загрузка_cpu_процент"] = round(load1 / cores * 100)
    except Exception as exc:
        result["cpu_ошибка"] = str(exc)
    try:
        mem: dict = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                key, _, value = line.partition(":")
                mem[key.strip()] = value.strip()
        total_kb = int(mem.get("MemTotal", "0 kB").split()[0])
        avail_kb = int(mem.get("MemAvailable", "0 kB").split()[0])
        if total_kb:
            used_kb = total_kb - avail_kb
            result["память_всего_гб"] = round(total_kb / 1024 / 1024, 1)
            result["память_занято_гб"] = round(used_kb / 1024 / 1024, 1)
            result["память_занято_процент"] = round(used_kb / total_kb * 100)
    except Exception as exc:
        result["память_ошибка"] = str(exc)
    return result or {"ошибка": "нет данных о нагрузке"}


def read_battery() -> dict:
    """Заряд и статус батареи из /sys/class/power_supply/BAT* (или её отсутствие)."""
    import glob

    try:
        paths = sorted(glob.glob("/sys/class/power_supply/BAT*"))
        if not paths:
            return {"батарея": "не обнаружена"}
        bat = paths[0]
        with open(f"{bat}/capacity", encoding="utf-8") as f:
            capacity = int(f.read().strip())
        status_raw = ""
        try:
            with open(f"{bat}/status", encoding="utf-8") as f:
                status_raw = f.read().strip()
        except Exception:
            pass
        status_ru = {
            "Charging": "заряжается",
            "Discharging": "разряжается",
            "Full": "заряжена полностью",
            "Not charging": "не заряжается",
        }.get(status_raw, status_raw or "неизвестно")
        return {"процент": capacity, "статус": status_ru}
    except Exception as exc:
        return {"ошибка": f"не удалось снять показания батареи: {exc}"}


def read_volume() -> dict:
    """Громкость основного аудиовыхода в процентах (wpctl, read-only).

    Парсит вывод `wpctl get-volume @DEFAULT_AUDIO_SINK@` («Volume: 0.45» или
    «Volume: 0.45 [MUTED]»). НИКОГДА не бросает: при сбое — {"ошибка": ...}.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        parts = out.stdout.split()
        # Ожидаем формат «Volume: <float> [MUTED]»
        volume = float(parts[1])
        return {
            "громкость_процент": round(volume * 100),
            "выключен": "MUTED" in out.stdout,
        }
    except Exception as exc:
        return {"ошибка": f"не удалось снять громкость: {exc}"}
