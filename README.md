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

## Установка за две команды

```bash
./bootstrap.sh          # создаёт venv и ставит пакет (работает из любой папки)
.venv/bin/jarvis doctor # глубокая диагностика здоровья + подсказки по починке
```

`bootstrap.sh` решает проблему «курица-яйцо»: команда `jarvis` появляется только
после `pip install`, поэтому первичную установку делает крошечный bash-скрипт.
Он переиспользует существующий venv и внятно подсказывает (`sudo apt install -y
python3-venv`), если не хватает системного модуля. Сам `apt` он не вызывает.

Дальше всё делает CLI `jarvis` (см. раздел «CLI» ниже): диагностика, загрузка
моделей, тесты, управление сервисами.

### Системные предпосылки (ставятся вне venv)

```bash
# Брокер MQTT
sudo apt install -y mosquitto mosquitto-clients
sudo systemctl enable --now mosquitto

# Ollama + модель «Мозга» (https://ollama.com)
ollama pull qwen2.5:0.5b-instruct
```

## CLI: установка, диагностика, управление

| Команда                    | Что делает                                                            |
|----------------------------|----------------------------------------------------------------------|
| `jarvis doctor`            | быстрый чекап (доли секунды): окружение, импорты, пути, `commands.yaml`, живой round-trip с Mosquitto, доступность Ollama и нужной модели, аудиоустройства, загружаемость моделей движком, инстанцирование сервисов, проверка юнитов. |
| `jarvis doctor --deep`     | всё из быстрого **плюс** долгие живые тесты: пробная генерация Ollama с валидацией схемы, синтез сэмпла Piper, сквозная цепочка по шине. |
| `jarvis models --download` | загрузка моделей по `models.yaml` в `models/` (с прогрессом и валидацией). |
| `jarvis test`              | сквозной тест живой шины `say → execute → input`: кто ожил, кто молчит. |
| `jarvis start` / `stop`    | генерирует `--user`-юниты с верными путями и поднимает/останавливает сервисы. |
| `jarvis status`            | статус сервисов через `systemctl --user`.                            |

`doctor` проверяет **работоспособность, а не наличие**: не «файл на месте», а
«движок реально загрузил модель», не `pgrep mosquitto`, а реальный round-trip.
Каждая проблема выводится как «что → почему → точная команда починки» и
дублируется в `logs/doctor.log`. Сам `doctor` не падает: упавшая проверка —
честный `✗`, а не краш. Вывод сдержанный (`✓`/`✗`/`⚠`, 3 цвета), без цвета при
выводе не в терминал.

> Console-команды (`jarvis`, `jarvis-stt`, `jarvis-core`, `jarvis-os-agent`,
> `jarvis-tts`) появляются в `.venv/bin/` после `pip install -e .`.

## Откуда брать модели (кладём в `models/`)

Каталог `models/` в git не хранится (см. `.gitignore`). Проще всего —
`jarvis models --download` (ссылки берутся из `models.yaml`, правятся одной
строкой без Python). Либо вручную:

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
в пользовательском сеансе.

Проще всего — через CLI: он **сам генерирует юниты с верными путями** (от
текущего venv, без хардкода и симлинков) и ставит их в `~/.config/systemd/user/`:

```bash
jarvis start    # генерирует юниты + enable --now всех сервисов
jarvis status
jarvis stop     # disable --now
```

Файлы в `systemd/` — лишь документированные примеры; реальные юниты пишет
`jarvis start`. `Restart=always` поднимает упавший сервис автоматически.

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
