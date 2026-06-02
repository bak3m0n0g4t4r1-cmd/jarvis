# Джарвис — голосовое ядро (Фаза 1)

Локальный, оффлайн, асинхронный голосовой ассистент на Kali Linux.
Архитектура — **Event-Driven Microservices** поверх брокера **Mosquitto MQTT**
(`localhost:1883`). Каждый сервис — независимый демон, общается только
JSON-сообщениями в топиках шины. Целевое железо: Intel N100, 8 ГБ RAM, без GPU.

## Состав Фазы 1

| Файл                          | Роль    | Подписан на        | Публикует в                         |
|-------------------------------|---------|--------------------|-------------------------------------|
| `jarvis/services/stt.py`      | «Уши»   | —                  | `jarvis/input`, `jarvis/state`      |
| `jarvis/services/core.py`     | «Мозг»  | `jarvis/input`     | `jarvis/say`, `jarvis/execute`, `jarvis/state` |
| `jarvis/services/os_agent.py` | «Руки»  | `jarvis/execute`   | `jarvis/say`                        |
| `jarvis/services/tts.py`      | «Голос» | `jarvis/say`       | `jarvis/state`                      |

Вся MQTT/logging/shutdown-обвязка — в базовом классе `JarvisModule`
(`jarvis/bus.py`). Контракты топиков и схема ответа — в `jarvis/contracts.py`.

## Контракты шины (MQTT-топики)

| Топик            | Payload                                                    | QoS |
|------------------|-----------------------------------------------------------|-----|
| `jarvis/input`   | `{"text": "..."}`                                          | 0   |
| `jarvis/execute` | `{"command_tag": "..."}`                                   | 1   |
| `jarvis/say`     | `{"text": "...", "source": "module"}`                      | 0   |
| `jarvis/state`   | `{"state": "idle\|listening\|thinking\|speaking", "source"}` | 0 |

## Установка

```bash
# 1. Брокер MQTT
sudo apt install mosquitto mosquitto-clients
sudo systemctl enable --now mosquitto

# 2. Пакет в виртуальном окружении (рекомендуется ~/jarvis/.venv)
cd ~/jarvis
python3 -m venv .venv
source .venv/bin/activate
pip install -e .          # ставит зависимости и команды jarvis-*

# 3. Ollama + модель «Мозга»
#    https://ollama.com — установить, затем:
ollama pull qwen2.5:0.5b-instruct
```

> Команды `jarvis-stt`, `jarvis-core`, `jarvis-os-agent`, `jarvis-tts`
> появляются в `~/jarvis/.venv/bin/` после `pip install -e .`.

## Откуда брать модели (кладём в `models/`)

Каталог `models/` в git не хранится (см. `.gitignore`). Скачать вручную:

- **STT — silero-VAD + SenseVoice-Small** (sherpa-onnx):
  релизы моделей sherpa-onnx — `github.com/k2-fsa/sherpa-onnx/releases`
  (файлы `silero_vad.onnx` и пакет `sherpa-onnx-sense-voice-...`).
  Пути задаются в `jarvis/config.py` (`VAD_MODEL`, `SENSEVOICE_MODEL`, `SENSEVOICE_TOKENS`).
- **TTS — голос Piper `ru_RU dmitri`**:
  `github.com/rhasspy/piper/` (или huggingface `rhasspy/piper-voices`).
  Нужны `.onnx` и `.onnx.json`; пути — `PIPER_MODEL`, `PIPER_CONFIG` в конфиге.

Все пути переопределяются переменными окружения `JARVIS_*` (см. `jarvis/config.py`).

## Запуск и проверка по топикам

В одном терминале — слушаем всю шину:

```bash
mosquitto_sub -t 'jarvis/#' -v
```

В другом — запускаем сервисы (каждый отдельно) и шлём тестовые сообщения:

```bash
# «Мозг»: текст -> ответ + (опц.) тег команды
jarvis-core &
mosquitto_pub -t jarvis/input -m '{"text":"джарвис, какой план на вечер?"}'
#   ожидаем в jarvis/say реплику и state=thinking->idle

# «Руки»: выполнение по тегу
jarvis-os-agent &
mosquitto_pub -t jarvis/execute -m '{"command_tag":"network_scan"}'
#   ожидаем запуск nmap и статус в jarvis/say; неизвестный тег -> предупреждение

# «Голос»: озвучка
jarvis-tts &
mosquitto_pub -t jarvis/say -m '{"text":"Проверка связи, сэр.","source":"test"}'
#   ожидаем звук в колонках и state=speaking->idle

# «Уши»: скажи в микрофон «Джарвис, ...»
jarvis-stt &
#   распознанная фраза после wake-word уходит в jarvis/input
```

## Автозапуск (systemd `--user`)

Используются именно **user-юниты** — аудио (PipeWire) и Wayland-сессия живут
в пользовательском сеансе. Аудио-сервисы (`stt`, `tts`) ждут `pipewire.service`.

```bash
mkdir -p ~/.config/systemd/user
cp systemd/jarvis-*.service ~/.config/systemd/user/
# Проверь ExecStart: путь до бинаря из своего venv (по умолчанию ~/jarvis/.venv/bin/)
systemctl --user daemon-reload
systemctl --user enable --now jarvis-stt jarvis-core jarvis-os-agent jarvis-tts
systemctl --user status 'jarvis-*'
```

`Restart=always` поднимает упавший сервис автоматически.

## Как добавить новый модуль за 3 шага

1. Создай файл в `jarvis/services/`, унаследуй `JarvisModule`:
   ```python
   from jarvis.bus import JarvisModule
   from jarvis import contracts

   class LightModule(JarvisModule):
       def on_start(self):
           self.subscribe(contracts.TOPIC_STATE, self.on_state)

       def on_state(self, payload: dict):
           if payload.get("state") == "thinking":
               pulse_lamp()   # вся специфика — здесь

   def main():
       LightModule("jarvis-light").run()
   ```
2. Добавь entry point в `pyproject.toml` (`[project.scripts]`) и `pip install -e .`.
3. (Опц.) положи `--user`-юнит в `systemd/` и `systemctl --user enable --now`.

Существующие модули при этом не трогаются.

## Что сверить на целевой машине (API библиотек)

Код написан по актуальным документированным API, но версии библиотек на
Kali могут отличаться. Перед запуском сверь (`pip show <пакет>`, `--help`,
docstrings) и при необходимости поправь помеченные `ВНИМАНИЕ`-комментарии:

- **sherpa-onnx** (`jarvis/services/stt.py`): сигнатуры `VadModelConfig`,
  `VoiceActivityDetector`, `OfflineRecognizer.from_sense_voice`, доступ к
  `vad.front.samples` и `stream.result.text`.
- **piper-tts** (`jarvis/services/tts.py`): `PiperVoice.load`,
  `synthesize_stream_raw`, `voice.config.sample_rate`.
- **ollama** (`jarvis/services/core.py`): сигнатура `Client.chat(...)`,
  параметр `format` (JSON-схема), формат ответа `response["message"]["content"]`.
- **paho-mqtt 2.x**: используется `CallbackAPIVersion.VERSION2` — сигнатуры
  колбэков уже новые.
