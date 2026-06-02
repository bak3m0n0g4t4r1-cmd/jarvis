"""«Мозг» Джарвиса: слушает jarvis/input, спрашивает Ollama и отвечает.

Использует нативный structured output Ollama (format = JSON-схема pydantic),
держит короткую историю диалога и публикует реплику в jarvis/say, а теги
команд — в jarvis/execute.
"""
import threading
from collections import deque

import ollama

from jarvis import config, contracts
from jarvis.bus import JarvisModule

SYSTEM_PROMPT = (
    "Ты — Джарвис, локальный голосовой ассистент в стиле Тони Старка: "
    "краткий, остроумный, обращаешься на «сэр». Отвечай на русском. "
    "Определи намерение пользователя и верни строго JSON по схеме. "
    "intent: 'os_command' — если нужно выполнить команду в системе; "
    "'web_search' — если нужен поиск в интернете; "
    "'casual_talk' — обычный разговор. "
    "payload: тег команды ('telegram', 'wifi_off', 'network_scan') или 'none'. "
    "speech_response: что сказать вслух."
)


class CoreModule(JarvisModule):
    """«Мозг»: преобразует текст пользователя в реплику и тег команды."""

    def __init__(self):
        super().__init__("jarvis-core")
        # История: до HISTORY_SIZE пар user/assistant -> 2 сообщения на пару
        self._history: deque = deque(maxlen=config.HISTORY_SIZE * 2)
        self._client = ollama.Client(host=config.OLLAMA_HOST)
        self._lock = threading.Lock()

    def on_start(self):
        self.subscribe(contracts.TOPIC_INPUT, self.on_input)

    def on_input(self, payload: dict):
        text = (payload.get("text") or "").strip()
        if not text:
            return
        # Обработку выносим в поток, чтобы не блокировать MQTT-loop
        threading.Thread(target=self._process, args=(text,), daemon=True).start()

    def _process(self, text: str):
        # Один LLM-запрос за раз: на N100 нельзя держать две сессии в RAM
        with self._lock:
            self.set_state(contracts.STATE_THINKING)
            try:
                messages = (
                    [{"role": "system", "content": SYSTEM_PROMPT}]
                    + list(self._history)
                    + [{"role": "user", "content": text}]
                )
                # ВНИМАНИЕ: сверить сигнатуру chat() с установленной версией ollama.
                # В актуальных версиях format принимает JSON-схему (dict),
                # keep_alive не даёт выгрузить модель между запросами.
                response = self._client.chat(
                    model=config.OLLAMA_MODEL,
                    messages=messages,
                    format=contracts.JarvisResponse.model_json_schema(),
                    keep_alive=config.OLLAMA_KEEP_ALIVE,
                )
                content = response["message"]["content"]
                result = contracts.JarvisResponse.model_validate_json(content)
            except Exception:
                self.log.exception("Ошибка запроса к Ollama")
                self.say("Прошу прощения, сэр, у меня сбой в мыслительном модуле.")
                self.set_state(contracts.STATE_IDLE)
                return

            # Обновляем историю (FIFO ограничен maxlen у deque)
            self._history.append({"role": "user", "content": text})
            self._history.append(
                {"role": "assistant", "content": result.speech_response}
            )

            self.say(result.speech_response)
            if (
                result.intent in ("os_command", "web_search")
                and result.payload
                and result.payload != "none"
            ):
                self.publish_json(
                    contracts.TOPIC_EXECUTE,
                    {"command_tag": result.payload},
                    qos=contracts.QOS_EXECUTE,
                )
            self.set_state(contracts.STATE_IDLE)


def main():
    CoreModule().run()


if __name__ == "__main__":
    main()
