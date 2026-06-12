#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  perf_mode.sh — режим производительности CPU для «Джарвиса» (Этап 25).
#
#  На Intel N100 (intel_pstate) под powersave частота поднимается лениво на коротких
#  всплесках — а именно такие всплески формируют распознавание (ASR) и синтез речи
#  (Piper). Перевод governor в performance и EPP (energy_performance_preference) в
#  performance заметно срезает задержку ОТКЛИКА (ценой нагрева/потребления).
#
#  Использование:
#     sudo tools/perf_mode.sh        # включить производительность
#     sudo tools/perf_mode.sh --off  # вернуть энергосбережение (powersave / balance)
#
#  Идемпотентно и БЕЗОПАСНО: отсутствующие файлы пропускаются (не на всех ядрах/ОС
#  есть EPP), скрипт не падает. Требует root (пишет в /sys). Ставится один раз как
#  systemd-юнит jarvis-perf.service (см. systemd/jarvis-perf.service).
# ─────────────────────────────────────────────────────────────────────────────
set -u

MODE="on"
[ "${1:-}" = "--off" ] && MODE="off"

if [ "$MODE" = "on" ]; then
    GOV="performance"
    EPP="performance"
else
    GOV="powersave"
    EPP="balance_performance"
fi

changed=0
for cpu in /sys/devices/system/cpu/cpu[0-9]*; do
    gov_f="$cpu/cpufreq/scaling_governor"
    epp_f="$cpu/cpufreq/energy_performance_preference"
    if [ -w "$gov_f" ]; then
        # Ставим governor, только если он есть среди доступных (иначе тихо пропускаем).
        avail="$cpu/cpufreq/scaling_available_governors"
        if [ ! -r "$avail" ] || grep -qw "$GOV" "$avail" 2>/dev/null; then
            echo "$GOV" > "$gov_f" 2>/dev/null && changed=1
        fi
    fi
    [ -w "$epp_f" ] && { echo "$EPP" > "$epp_f" 2>/dev/null && changed=1; }
done

# Платформенный профиль (если поддерживается прошивкой) — ещё один рычаг частоты/лимитов.
pp_f="/sys/firmware/acpi/platform_profile"
if [ -w "$pp_f" ]; then
    if [ "$MODE" = "on" ]; then
        echo "performance" > "$pp_f" 2>/dev/null || echo "balanced" > "$pp_f" 2>/dev/null
    else
        echo "balanced" > "$pp_f" 2>/dev/null || true
    fi
fi

if [ "$changed" = "1" ]; then
    echo "Джарвис: режим CPU → $GOV (EPP $EPP)"
else
    echo "Джарвис: не удалось изменить режим CPU (нет прав или /sys недоступен)" >&2
    exit 1
fi
