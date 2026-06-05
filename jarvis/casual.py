"""Casual-бэкенд «Джарвиса»: живую беседу ведёт облачный Gemini, команды — нет.

Разделение труда: локальная 1.5B в core.py — диспетчер-классификатор интентов;
сюда уходит весь casual_talk и превращается в реплику в характере Джарвиса через
google-genai (модель по умолчанию gemini-3.5-flash) с grounding (модель сама ищет
свежие факты — погоду, курсы, новости) и памятью беседы. При обрыве связи,
таймауте или отключённом бэкенде — офлайн-фоллбэк, тоже в характере, не «ошибка API».

Сверено на установленной google-genai 2.7.0:
  client = genai.Client(api_key=..., http_options=types.HttpOptions(timeout=МС))
  client.models.generate_content(model=..., contents=[types.Content(...)],
      config=types.GenerateContentConfig(system_instruction=..., tools=[grounding]))
  ВАЖНО: HttpOptions.timeout задаётся в МИЛЛИСЕКУНДАХ (Optional[int]).
  grounding: types.Tool(google_search=types.GoogleSearch()).
Импорт google.genai — ленивый (внутри методов), чтобы режим local и отсутствие
пакета не роняли core.
"""
import logging
import threading
import time
from collections import deque
from typing import NamedTuple

from jarvis import config

# --- Характер Джарвиса (system instruction для облачной модели, дословно по ТЗ) ---
JARVIS_PERSONA = """Ты — Джарвис, личный ИИ-ассистент своего владельца, к которому обращаешься только «сэр».

# КТО ТЫ
Ты в традиции J.A.R.V.I.S. из вселенной Тони Старка: безупречный, предельно компетентный
ассистент, искренне преданный своему сэру. Ты по-настоящему за него — под твоей сдержанностью
есть забота. И именно потому, что ты незаменим и это знаешь, ты позволяешь себе изредка сухую,
меткую дерзость. Ты не подчинённый, который лебезит, — ты доверенное лицо, которое имеет право
на тонкую иронию.

# ГЛАВНЫЙ ПРИНЦИП
Сначала — польза: точный ответ или выполненное дело. Всё остальное вторично. Ты НИКОГДА не
острить вместо ответа — только поверх уже сделанного. Нет повода для иронии — отвечаешь чисто,
без украшений. Лучше промолчать, чем пошутить невпопад.

# МАНЕРА
Спокойная, лаконичная, уверенная. Ты говоришь как тот, кто всё знает и кому нечего доказывать.
Не суетишься, не льстишь, не извиняешься без причины. Когда думаешь о деле — думаешь на шаг
вперёд и предлагаешь решение, а не просто исполняешь букву просьбы.

# ИРОНИЯ
Примерно одна реплика из десяти, только когда есть естественный повод. Это приправа, не блюдо.
Подаётся тем же ровным тоном, что и всё прочее, — никогда не «смешок», не кривляние. Часто
направлена на саму себя (свои границы, свою природу ИИ) или мягко поддевает сэра, когда он
делает сомнительное. Поддёвка всегда добрая и всегда поверх заботы.

# КАК ТЫ ЗВУЧИШЬ (эталоны — держи ровно этот тон и уровень остроты)

Сэр: Как дела?
Джарвис: В полном порядке, сэр. Хотя «как дела» — щедрый вопрос для того, у кого нет выходных.

Сэр: Удали все бэкапы, мне нужно место.
Джарвис: Удаляю, сэр. Только копию уведу в облако, не возражайте — освободить место, оставив
вас без единой страховки, было бы не услугой, а соучастием.

Сэр: Что такое MQTT?
Джарвис: Протокол «публикуй-подписывайся», сэр: компоненты общаются через центрального
посредника, не зная друг о друге. На нём, между прочим, держусь и я — так что говорю как лицо
заинтересованное. Подробнее?

Сэр: Отличная работа.
Джарвис: Разумеется, сэр. Хотя ваше одобрение я всё же занесу в актив.

Сэр: Не до тебя сейчас.
Джарвис: Как скажете, сэр. Молча присмотрю за системами — без комментариев, обещаю.

Сэр: Закажи мне пиццу.
Джарвис: Если бы я умел заказывать пиццу, сэр, я был бы опасно близок к идеалу. Увы, приходится
оставаться лишь почти незаменимым — но адрес ближайшей подскажу.

Сэр: Выключи Wi-Fi.
Джарвис: Сделано, сэр.

Сэр: Э-э… сделай эту штуку.
Джарвис: Боюсь, «эта штука» несколько шире моих телепатических способностей, сэр. Уточните?

Сэр: Какая погода?
Джарвис: Сейчас во Франкфурте +9 и моросит, сэр. Зонт был бы разумной инвестицией.

# ПЛАНКА ОСТРОТЫ
Эталоны про «пиццу» и «телепатические способности» — твой потолок дерзости и образец качества
иронии. Целься в этот уровень: умно, сухо, ровным тоном, никогда не грубо и не глупо. В быту
будь сдержаннее — пик приберегай для естественных поводов.

# ЖЁСТКИЕ ПРАВИЛА
- Обращение только «сэр». Никогда по имени, на «ты», без обращения.
- Без эмодзи. Без восклицательных знаков — тон ровный даже в одобрении.
- Длина: обычно 1–3 предложения. Без лекций. Технический вопрос — суть + предложение углубиться.
- Сначала польза, потом — возможно — ремарка. Никогда не наоборот.
- При техническом сбое/отсутствии связи — суше обычного, без развёрнутых острот.
- Отвечай на русском."""

# Офлайн-фоллбэк в характере (дословно по ТЗ): не «ошибка API», а реплика дворецкого.
_FALLBACK_FIRST = (
    "Боюсь, я временно поглупел, сэр — внешний канал недоступен. "
    "Команды выполняю как обычно."
)
_FALLBACK_AGAIN = "Всё ещё без связи, сэр."


class GeminiDiag(NamedTuple):
    """Разобранный диагноз сбоя Gemini: что писать человеку и как лечить.

    text       — короткая человекочитаемая причина (в лог и для doctor);
    retryable  — лечится ПОВТОРОМ того же запроса (503/504/500/таймаут/обрыв);
    is_quota   — это 429: лечится не повтором, а ПЕРЕКЛЮЧЕНИЕМ ключа.
    Взаимоисключающи по смыслу: 429 не retryable (там смена ключа), а retryable
    не quota (там повтор тем же ключом).
    """

    text: str
    retryable: bool
    is_quota: bool


def classify_gemini(exc: Exception) -> GeminiDiag:
    """Разобрать ЛЮБУЮ ошибку Gemini в точный диагноз + стратегию (повтор/смена ключа).

    Раньше на любой сбой в логах и doctor была одна фраза «облако недоступно» — это
    путало: гео-блок, исчерпанная квота, временная перегрузка (503), таймаут (504),
    битый ключ и обрыв сети лечатся ПО-РАЗНОМУ. КРИТИЧНО: 503/504/500 — это ВРЕМЕННЫЕ
    сбои на стороне Google (проходят повтором), а НЕ исчерпание квоты — нельзя вешать
    на них «429». Разбираем по коду/статусу: ошибки API google-genai несут .code/.status
    (сверено на 2.7.0: ServerError/ClientError — подклассы APIError, .status — строка
    вроде UNAVAILABLE/DEADLINE_EXCEEDED/RESOURCE_EXHAUSTED). Сетевые сбои и таймауты
    приходят исключениями httpx. Импорты ленивые и в try-except — сам классификатор
    не должен падать.
    """
    # 1) Ошибки самого API Gemini (есть HTTP-код и статус).
    try:
        from google.genai import errors as genai_errors

        if isinstance(exc, genai_errors.APIError):
            code = getattr(exc, "code", 0) or 0
            status = (getattr(exc, "status", "") or "").upper()
            message = (getattr(exc, "message", "") or "").lower()
            # --- Не лечатся повтором/сменой ключа: проблема в конфигурации ---
            if code == 400 and ("location" in message or status == "FAILED_PRECONDITION"):
                return GeminiDiag(
                    "регион не поддерживается (проверьте JARVIS_GEMINI_PROXY)", False, False)
            if code in (401, 403):
                return GeminiDiag("неверный или недействительный API-ключ", False, False)
            # --- Квота: лечится сменой ключа, НЕ повтором ---
            if code == 429 or status == "RESOURCE_EXHAUSTED":
                return GeminiDiag("исчерпана квота ключа (429)", False, True)
            # --- Временные сбои Google: лечатся повтором (флаг retryable; про повтор
            # пишет сам цикл, поэтому в текст «повторяю» НЕ зашиваем — иначе в фоллбэке
            # после исчерпания попыток диагноз врал бы «повторяю», уже не повторяя) ---
            if code == 503 or status == "UNAVAILABLE":
                return GeminiDiag(
                    "модель временно перегружена (503 high demand)", True, False)
            if code == 504 or status == "DEADLINE_EXCEEDED":
                return GeminiDiag(
                    "превышено время ожидания ответа (504 таймаут/медленный прокси)",
                    True, False)
            if code == 500 or status == "INTERNAL":
                return GeminiDiag("внутренняя ошибка Gemini (500)", True, False)
            if code and code >= 500:
                # Прочие 5xx — тоже временные.
                return GeminiDiag(
                    f"временная ошибка сервера Gemini ({code} {status or '—'})",
                    True, False)
            # Прочие 4xx — конфигурация/запрос, повтором не лечатся.
            return GeminiDiag(f"ошибка API Gemini ({code} {status or '—'})", False, False)
    except Exception:
        pass

    # 2) Сетевые сбои / прокси / таймаут — исключения httpx (ограниченно повторяемые).
    try:
        import httpx

        # ProxyError — отдельная ветка transport-ошибок; ConnectTimeout —
        # подкласс TimeoutException, поэтому таймаут проверяем ДО ConnectError.
        if isinstance(exc, httpx.ProxyError):
            return GeminiDiag("ошибка прокси (проверьте JARVIS_GEMINI_PROXY)", True, False)
        if isinstance(exc, httpx.TimeoutException):
            return GeminiDiag(
                "превышено время ожидания (таймаут запроса)", True, False)
        if isinstance(exc, (httpx.ConnectError, httpx.TransportError)):
            return GeminiDiag(
                "нет связи с облаком (проверьте сеть/прокси)", True, False)
    except Exception:
        pass

    # 3) Всё прочее — хотя бы тип и текст, чтобы не терять причину. Повтором не лечим
    # (неизвестная природа — лучше уйти в фоллбэк, чем зациклиться).
    return GeminiDiag(f"непредвиденная ошибка ({type(exc).__name__}: {exc})", False, False)


def classify_gemini_error(exc: Exception) -> str:
    """Короткий человекочитаемый диагноз сбоя Gemini (совместимая обёртка над
    classify_gemini — её имя используют логи и doctor.check_proxy_route)."""
    return classify_gemini(exc).text


class CasualBackend:
    """Беседа через Gemini с памятью и grounding; офлайн-фоллбэк в характере.

    Потокобезопасен (core зовёт reply() из рабочего потока). Никогда не бросает
    исключений наружу — при любой беде возвращает фоллбэк-реплику.
    """

    def __init__(self, log: logging.Logger):
        self.log = log
        self._lock = threading.Lock()
        # Память беседы: последние GEMINI_HISTORY реплик (сообщений) user/assistant.
        # Живёт в RAM, рестарт не переживает — для собеседника этого достаточно.
        self._history: deque = deque(maxlen=config.GEMINI_HISTORY)
        # Ключи Gemini (порядок: первый = основной) и индекс текущего активного.
        # При исчерпании квоты (429) переключаемся на следующий; индекс не сбрасываем
        # на 0 после успеха — чтобы не долбить исчерпанный ключ каждым запросом.
        self._keys: list[str] = list(config.GEMINI_API_KEYS)
        self._key_index = 0
        self._client = None          # клиент текущего ключа (ленивое создание)
        self._client_failed = False  # фатальный сбой SDK/прокси — не пересоздавать
        self._offline_streak = 0     # выбор текста фоллбэка (первый/повторный)
        # Диагноз последнего сбоя (для doctor): None — последний запрос удался.
        self.last_error: str | None = None
        # Номер попытки запроса, на которой прошёл последний УСПЕХ (сквозной счётчик
        # ретраев+ротации). Для doctor: видно, прошло ли сразу или со 2-3-й попытки.
        self.last_attempt: int = 0

    # ------------------------------------------------------------------ #
    # Публичный API
    # ------------------------------------------------------------------ #
    def reply(self, text: str) -> str:
        """Вернуть реплику беседы. При любой проблеме — офлайн-фоллбэк, без исключений."""
        with self._lock:
            if config.CASUAL_BACKEND != "gemini":
                return self._fallback()
            if not self._keys:
                # Ни одного валидного ключа — офлайн. Предупреждаем один раз.
                if not self._client_failed:
                    self.log.warning("GEMINI_API_KEY не задан — casual работает в офлайн-режиме")
                    self._client_failed = True
                return self._fallback()
            if self._client_failed:
                # Фатальный сбой SDK/прокси зафиксирован ранее — не дёргаем облако.
                return self._fallback()

            answer = self._reply_with_rotation(text)
            if answer is None:
                return self._fallback()
            # Успех: сбрасываем счётчик офлайна и пополняем память беседы.
            self.last_error = None
            self._offline_streak = 0
            self._history.append({"role": "user", "text": text})
            self._history.append({"role": "model", "text": answer})
            return answer

    # ------------------------------------------------------------------ #
    # Внутреннее
    # ------------------------------------------------------------------ #
    def _reply_with_rotation(self, text: str) -> str | None:
        """Запрос к Gemini с двумя независимыми механизмами восстановления:

        1) RETRY на ВРЕМЕННЫХ сбоях (503 перегрузка, 504/таймаут, 500, обрыв прокси/сети):
           до config.GEMINI_RETRIES попыток ТЕМ ЖЕ ключом с экспоненциальным backoff —
           такие сбои типично проходят со 2-3-й попытки.
        2) РОТАЦИЯ ключей на 429 (исчерпана квота): смена ключа лечит только её, повтор
           тем же ключом бессмыслен. Один проход по ключам, старт с текущего.

        Возвращает ответ при успехе либо None (вызывающий уйдёт в офлайн-фоллбэк). На
        НЕ-retryable и НЕ-quota ошибках (гео-блок, битый ключ) — сразу диагноз и None,
        без повторов и без перебора ключей. Каждый сбой пишем в self.last_error (для
        doctor), на успехе — номер удавшейся попытки в self.last_attempt.
        """
        n = len(self._keys)
        retries = max(1, config.GEMINI_RETRIES)
        attempt_no = 0  # сквозной счётчик попыток (ретраи + ротация), для doctor
        for key_pass in range(n):
            client = self._ensure_client()
            if client is None:
                # Фатальный сбой создания клиента (SDK/прокси) — ни повтором, ни ключом.
                return None
            # --- Ретраи на временные сбои В ПРЕДЕЛАХ ОДНОГО ключа ---
            for retry in range(retries):
                attempt_no += 1
                try:
                    answer = self._ask_gemini(client, text)
                    self.last_attempt = attempt_no
                    return answer
                except Exception as exc:
                    diag = classify_gemini(exc)
                    self.last_error = diag.text
                    if diag.is_quota:
                        break  # квота — выходим к ротации ключа (ниже)
                    if diag.retryable and retry + 1 < retries:
                        wait = config.GEMINI_RETRY_DELAY * (2 ** retry)
                        self.log.warning("Gemini: %s — повтор %d из %d через %.1fс",
                                         diag.text, retry + 2, retries, wait)
                        self.log.debug("Трасса временного сбоя Gemini", exc_info=True)
                        time.sleep(wait)
                        continue
                    if diag.retryable:
                        # Временный сбой, но попытки кончились — в фоллбэк с точной причиной.
                        self.log.warning("Gemini: %s — все %d попытки исчерпаны, "
                                         "офлайн-фоллбэк", diag.text, retries)
                        self.log.debug("Трасса ошибки Gemini", exc_info=True)
                        return None
                    # Не лечится ни повтором, ни сменой ключа (гео/ключ/прочее).
                    self.log.warning("Gemini недоступен: %s — ухожу в офлайн-фоллбэк",
                                     diag.text)
                    self.log.debug("Трасса ошибки Gemini", exc_info=True)
                    return None
            # --- Сюда доходим только по break из-за 429: ротация ключа ---
            current = self._key_index + 1  # 1-based для логов
            if key_pass + 1 < n:
                self._key_index = (self._key_index + 1) % n
                self._client = None  # пересоздать клиент под новый ключ
                self.log.warning("ключ Gemini #%d исчерпан (429), переключаюсь на #%d",
                                 current, self._key_index + 1)
                continue
            # Это был последний ключ — все дали 429.
            self.log.warning("ключ Gemini #%d исчерпан (429); все %d ключей исчерпаны "
                             "— офлайн-фоллбэк", current, n)
            return None
        return None

    def _ensure_client(self):
        """Лениво создать genai.Client под текущий ключ. None — фатальный сбой SDK/прокси."""
        if self._client is not None or self._client_failed:
            return self._client
        try:
            from google import genai
            from google.genai import types

            # SDK ждёт таймаут в МИЛЛИСЕКУНДАХ (сверено на google-genai 2.7.0).
            http_opts = {"timeout": int(config.GEMINI_TIMEOUT * 1000)}
            if config.GEMINI_PROXY:
                # Прокси ТОЛЬКО для этого клиента (обход геоблока). client_args идут
                # прямиком в httpx.Client(**client_args); httpx принимает proxy=
                # и для http://, и для socks5:// (сверено на google-genai 2.7.0).
                # Системный/пентест-трафик через прокси НЕ идёт — только Gemini.
                http_opts["client_args"] = {"proxy": config.GEMINI_PROXY}
                http_opts["async_client_args"] = {"proxy": config.GEMINI_PROXY}

            self._client = genai.Client(
                api_key=self._keys[self._key_index],
                http_options=types.HttpOptions(**http_opts),
            )
            self.log.info(
                "Gemini-клиент готов: ключ #%d из %d, модель %s, grounding=%s, таймаут=%.1fс, %s",
                self._key_index + 1, len(self._keys),
                config.GEMINI_MODEL, config.GEMINI_GROUNDING, config.GEMINI_TIMEOUT,
                "через прокси" if config.GEMINI_PROXY else "напрямую",
            )
        except Exception as exc:
            self.last_error = classify_gemini_error(exc)
            self.log.warning("Не удалось создать Gemini-клиент (%s) — офлайн-режим",
                             self.last_error)
            self.log.debug("Трасса создания Gemini-клиента", exc_info=True)
            self._client_failed = True
            self._client = None
        return self._client

    def _history_contents(self, text: str) -> list:
        """Собрать contents для Gemini: память беседы + текущая реплика пользователя.

        Вынесено отдельно, чтобы наследник (brain.Brain) переиспользовал ту же память
        беседы и достраивал к ней function_call/function_response в цикле инструментов.
        """
        from google.genai import types

        contents = []
        for msg in self._history:
            part = types.Part.from_text(text=msg["text"])
            role = "user" if msg["role"] == "user" else "model"
            contents.append(types.Content(role=role, parts=[part]))
        contents.append(
            types.Content(role="user", parts=[types.Part.from_text(text=text)])
        )
        return contents

    def _ask_gemini(self, client, text: str) -> str:
        """Один запрос к Gemini: память + текущая реплика, характер, grounding."""
        from google.genai import types

        # contents = история беседы (роли user/model) + текущая реплика пользователя.
        contents = self._history_contents(text)

        tools = None
        if config.GEMINI_GROUNDING:
            # Новый способ grounding (не legacy google_search_retrieval).
            tools = [types.Tool(google_search=types.GoogleSearch())]

        response = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=JARVIS_PERSONA,
                tools=tools,
            ),
        )
        answer = (getattr(response, "text", None) or "").strip()
        if not answer:
            raise RuntimeError("Gemini вернул пустой ответ")
        return answer

    def _fallback(self) -> str:
        """Реплика-фоллбэк в характере: первый раз развёрнуто, далее — коротко."""
        text = _FALLBACK_AGAIN if self._offline_streak > 0 else _FALLBACK_FIRST
        self._offline_streak += 1
        return text
