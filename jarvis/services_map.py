"""Единый реестр микросервисов «Джарвиса» — источник правды для CLI,
диагностики и генерации systemd-юнитов.

LEGO-принцип: добавил сервис в jarvis/services/ — допиши сюда одну строку
(и entry point в pyproject.toml). Существующие модули при этом не трогаются.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceDef:
    """Описание одного микросервиса."""

    key: str          # короткий ключ: stt / core / os_agent / tts
    unit: str         # имя systemd-юнита: jarvis-stt.service
    command: str      # console-команда из entry points: jarvis-stt
    module: str       # путь модуля для импорта: jarvis.services.stt
    cls: str          # имя класса-наследника JarvisModule: SttModule
    description: str  # человеческое описание
    needs_audio: bool = False  # нужен ли пользовательский PipeWire (STT/TTS) для зависимостей юнита


SERVICES = [
    ServiceDef(
        "stt", "jarvis-stt.service", "jarvis-stt",
        "jarvis.services.stt", "SttModule", "«Уши» — STT и wake-word",
        needs_audio=True,
    ),
    ServiceDef(
        "core", "jarvis-core.service", "jarvis-core",
        "jarvis.services.core", "CoreModule", "«Мозг» — матчер (правила + ONNX-эмбеддинги)",
    ),
    ServiceDef(
        "os_agent", "jarvis-os-agent.service", "jarvis-os-agent",
        "jarvis.services.os_agent", "OsAgentModule", "«Руки» — OS-команды",
    ),
    ServiceDef(
        "tts", "jarvis-tts.service", "jarvis-tts",
        "jarvis.services.tts", "TtsModule", "«Голос» — Piper TTS",
        needs_audio=True,
    ),
    ServiceDef(
        "activity_monitor", "jarvis-activity-monitor.service", "jarvis-activity-monitor",
        "jarvis.services.activity_monitor", "ActivityMonitorModule",
        "«Внимание» — детектор активности и напоминания о перерыве",
        needs_audio=False,
    ),
    ServiceDef(
        "scheduler", "jarvis-scheduler.service", "jarvis-scheduler",
        "jarvis.services.scheduler", "SchedulerModule",
        "«Планировщик» — будильники (утренний и обычные)",
        needs_audio=False,
    ),
]
