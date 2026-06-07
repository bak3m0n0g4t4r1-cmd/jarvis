# Джарвис — локальный голосовой пульт

Локальный, **полностью оффлайн** голосовой пульт на базовые команды. Без облака и без
LLM: распознавание — два дешёвых слоя (правила + лёгкие ONNX-эмбеддинги). Архитектура —
**Event-Driven Microservices** поверх брокера **Mosquitto MQTT** (`localhost:1883`).
Каждый сервис — независимый демон, общается только JSON-сообщениями в топиках шины.
Целевое железо: Intel N100 (4 ядра), 8 ГБ RAM, без GPU (разработан под **TUXEDO OS 24.04**).

> Характер сохранён: подтверждения «в характере» («Сделано, сэр»), но ум заменён на
> правила + эмбеддинги — чтобы N100/8 ГБ не тормозил. Историю перехода см. в `CLAUDE.md`.

## Состав (4 микросервиса)

| Файл                          | Роль    | Подписан на        | Публикует в                         |
|-------------------------------|---------|--------------------|-------------------------------------|
| `jarvis/services/stt.py`      | «Уши»   | `jarvis/state`, `jarvis/say` | `jarvis/input`, `jarvis/state`      |
| `jarvis/services/core.py`     | «Мозг»  | `jarvis/input`     | `jarvis/say`, `jarvis/execute`, `jarvis/state` |
| `jarvis/services/os_agent.py` | «Руки»  | `jarvis/execute`   | `jarvis/say`                        |
| `jarvis/services/tts.py`      | «Голос» | `jarvis/say`       | `jarvis/state`                      |

Вся MQTT/logging/shutdown-обвязка — в базовом классе `JarvisModule` (`jarvis/bus.py`).
Контракты топиков — в `jarvis/contracts.py`. Распознавание — `jarvis/matcher.py`,
карта команд (источник истины) — `commands.yaml`.

## Контракты шины (MQTT-топики)

| Топик            | Payload                                                    | QoS |
|------------------|-----------------------------------------------------------|-----|
| `jarvis/input`   | `{"text": "..."}`                                          | 0   |
| `jarvis/execute` | `{"command_tag": "..."}`                                   | 1   |
| `jarvis/say`     | `{"text": "...", "source": "module"}`                      | 0   |
| `jarvis/state`   | `{"state": "idle\|listening\|thinking\|speaking", "source"}` | 0 |

## Как распознаётся команда (без LLM)

«Мозг» (`core.py`) прогоняет фразу так:

1. **Встроенные info-ответы** (время/заряд) — офлайн, мгновенно, в характере.
2. **Слой правил** (`matcher.py`, мгновенно, ноль моделей): нормализация фразы + сверка с
   `синонимы` команды (точное совпадение / вхождение / все слова / difflib-fuzzy). Именно он
   задаёт направление и полярность (вкл/выкл, громче/тише) конкретным глаголом.
3. **Слой эмбеддингов** (если правила промахнулись): `rubert-tiny2` (ONNX через onnxruntime,
   без torch) кодирует фразу и ищет ближайшую `примеры`-фразу по косинусу. Порог + защита по
   отрыву (margin): при почти равных кандидатах (часто антонимы) — переспрос, а не угадывание.

Эмбеддер грузится **лениво** (при первом промахе правил), эмбеддинги команд кешируются на диск
(`cmd_emb_cache.npz`). Сбой эмбеддера → деградация в режим «только правила», без падения.

## Установка

```bash
./bootstrap.sh           # создаёт .venv и ставит пакет (работает из любой папки)
.venv/bin/jarvis doctor  # глубокая диагностика здоровья + подсказки по починке
```

`bootstrap.sh` решает проблему «курица-яйцо»: команда `jarvis` появляется только после
`pip install`, поэтому первичную установку делает крошечный bash-скрипт. Он переиспользует
существующий `.venv` и подсказывает (`sudo apt install -y python3-venv`), если не хватает
системного модуля. Сам `apt` он не вызывает.

### Системные предпосылки (ставятся вне venv, через apt)

```bash
# Брокер MQTT (обязательно — без него шина мертва)
sudo apt install -y mosquitto mosquitto-clients
sudo systemctl enable --now mosquitto

# Управление яркостью экрана (для команд brightness_up/down)
sudo apt install -y brightnessctl
```

Облака/LLM/Ollama больше нет — никаких внешних сервисов, кроме локального брокера.

## CLI: установка, диагностика, управление

| Команда                    | Что делает                                                            |
|----------------------------|----------------------------------------------------------------------|
| `jarvis doctor`            | полная глубокая проверка: окружение, импорты, версии, пути, `commands.yaml` (бинари в `$PATH`), железо (RAM/swap/CPU/диск), живой round-trip с Mosquitto, аудиоустройства, загружаемость моделей движком, эмбеддер (грузится + осмыслен), матчер, инстанцирование сервисов, здоровье юнитов, сквозная цепочка. |
| `jarvis doctor --quick`    | то же, но без долгих живых тестов (синтез Piper, сквозная цепочка).   |
| `jarvis models --download` | загрузка моделей по `models.yaml` в `models/` (с прогрессом и валидацией). |
| `jarvis test`              | сквозной тест живой шины `say → execute → input`: кто ожил, кто молчит. |
| `jarvis start` / `stop`    | генерирует `--user`-юниты с верными путями и поднимает/останавливает сервисы. |
| `jarvis status`            | статус сервисов через `systemctl --user`.                            |

`doctor` проверяет **работоспособность, а не наличие**: не «файл на месте», а «движок реально
загрузил модель», не `pgrep mosquitto`, а реальный round-trip. Каждая проблема выводится как
«что → почему → точная команда починки» и дублируется в `logs/doctor.log`. Сам `doctor` не
падает: упавшая проверка — честный `✗`, а не краш.

> Console-команды (`jarvis`, `jarvis-stt`, `jarvis-core`, `jarvis-os-agent`, `jarvis-tts`)
> появляются в `.venv/bin/` после `pip install -e .`.

## Откуда брать модели (кладём в `models/`)

Каталог `models/` в git не хранится (см. `.gitignore`). Проще всего — `jarvis models --download`
(ссылки берутся из `models.yaml`, правятся одной строкой без Python). Состав:

- **STT — silero-VAD + zipformer-ru** (sherpa-onnx, offline transducer, int8, только русский):
  релизы `github.com/k2-fsa/sherpa-onnx/releases` (`silero_vad.onnx` и пакет
  `sherpa-onnx-small-zipformer-ru-2024-09-18`). Пути — `VAD_MODEL`, `ZIPFORMER_*` в `config.py`.
- **TTS — голос Piper `ru_RU dmitri medium`**: huggingface `rhasspy/piper-voices`.
  Нужны `.onnx` и `.onnx.json`; пути — `PIPER_MODEL`, `PIPER_CONFIG`.
- **Эмбеддер — rubert-tiny2 (ONNX)**: готовый экспорт `Vuy/rubert-tiny2-onnx` (model + tokenizer).
  Пути — `EMBEDDER_MODEL`, `EMBEDDER_TOKENIZER`.

Все пути переопределяются переменными окружения `JARVIS_*` (см. `jarvis/config.py`).

## Запуск и проверка по топикам

В одном терминале — слушаем всю шину:

```bash
mosquitto_sub -t 'jarvis/#' -v
```

В другом — запускаем сервисы и шлём тестовые сообщения:

```bash
# «Мозг»: текст -> подтверждение + тег команды
jarvis-core &
mosquitto_pub -t jarvis/input -m '{"text":"джарвис сделай погромче"}'
#   ожидаем в jarvis/say подтверждение и тег volume_up в jarvis/execute

# «Руки»: выполнение по тегу (allow-list, shell=False)
jarvis-os-agent &
mosquitto_pub -t jarvis/execute -m '{"command_tag":"volume_up"}'
#   ожидаем wpctl set-volume; неизвестный тег -> предупреждение в jarvis/say

# «Голос»: озвучка
jarvis-tts &
mosquitto_pub -t jarvis/say -m '{"text":"Проверка связи, сэр.","source":"test"}'
#   ожидаем звук в колонках и state=speaking->idle

# «Уши»: скажи в микрофон «Джарвис, ...»
jarvis-stt &
#   распознанная фраза после wake-word уходит в jarvis/input
```

## Автозапуск (systemd `--user`)

Используются именно **user-юниты** — аудио (PipeWire) и Wayland-сессия живут в пользовательском
сеансе. CLI **сам генерирует юниты с верными путями** (от текущего `.venv`, без хардкода) и
ставит их в `~/.config/systemd/user/`:

```bash
jarvis start    # генерирует юниты + enable + restart всех сервисов
jarvis status
jarvis stop     # disable --now
```

Файлы в `systemd/` — лишь документированные примеры; реальные юниты пишет `jarvis start`.
`Restart=always` поднимает упавший сервис автоматически.

## Как добавить команду или модуль

**Команду** — блок в `commands.yaml` (тег + `команда:[...]` + `синонимы` + `примеры` +
`подтверждение`), Python трогать не нужно. Направление/полярность задавай явными синонимами
с глаголом — эмбеддинги антонимы НЕ различают.

**Модуль** — создай файл в `jarvis/services/`, унаследуй `JarvisModule`, добавь entry point в
`pyproject.toml` и строку в `jarvis/services_map.py`. Существующие модули при этом не трогаются.

## Что сверить на целевой машине (API библиотек)

Код написан по актуальным документированным API, но версии библиотек меняются. Перед запуском
сверь (`pip show <пакет>`, `--help`, docstrings) помеченные `ВНИМАНИЕ`-комментарии:

- **sherpa-onnx** (`jarvis/services/stt.py`): `VadModelConfig`, `VoiceActivityDetector`,
  `OfflineRecognizer.from_transducer` (BPE), доступ к `vad.front.samples`, `stream.result.text`.
- **piper-tts** (`jarvis/services/tts.py`): `PiperVoice.load`, `synthesize()` → итератор
  `AudioChunk` (`audio_int16_bytes`, `sample_rate`). Не старый `synthesize_stream_raw`.
- **onnxruntime / tokenizers** (`jarvis/matcher.py`): имена входов (`input_ids`/`attention_mask`/
  `token_type_ids`), mean-pooling по attention-маске (сверено: разделяет лучше CLS).
- **paho-mqtt 2.x**: используется `CallbackAPIVersion.VERSION2` — сигнатуры колбэков новые.
