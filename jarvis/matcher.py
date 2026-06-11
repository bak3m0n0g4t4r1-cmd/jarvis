"""Лёгкий распознаватель команд: правила + автономные ONNX-эмбеддинги.

Заменяет прежний LLM-диспетчер (Ollama/qwen). Никакой генеративной модели и
облака — только два дешёвых слоя поверх `commands.yaml` (он же источник истины):

  1. СЛОЙ ПРАВИЛ (мгновенный, ноль моделей): нормализуем фразу и сверяем с
     `синонимы` команды — точное совпадение, вхождение фразы, совпадение по слову
     и нечёткое сравнение (difflib, ловит искажения STT). Точные и частые
     формулировки ловятся тут, без всякой модели.
  2. СЛОЙ ЭМБЕДДИНГОВ (если правила не дали уверенного ответа): фраза кодируется
     лёгкой моделью rubert-tiny2 (ONNX через onnxruntime, без torch) и сравнивается
     по косинусной близости с `примеры`-фразами команд. Выше порога И с заметным
     отрывом от второй команды → её тег.

ВАЖНО (сверено на машине): эмбеддинги rubert-tiny2 НЕ различают антонимы
(«включи»/«выключи», «громче»/«тише» почти неотличимы). Поэтому направление и
полярность ОБЯЗАН задавать слой правил (синонимы с конкретными словами), а
эмбеддинги лишь подсказывают «семейство» команды, когда правила промахнулись.
Отсюда же — защита по отрыву (margin): при почти равных кандидатах возвращаем
None (переспрос), чтобы не выполнить противоположное действие.

Модель эмбеддера грузится ЛЕНИВО (при первом промахе правил), эмбеддинги команд
считаются ОДИН раз и кешируются. Всё в try-except: сбой эмбеддера → деградация в
режим «только правила», без падения.
"""
import logging
import re
from collections import namedtuple
from difflib import SequenceMatcher
from typing import Optional

from jarvis import config, phrases

_log = logging.getLogger("jarvis-matcher")

# Результат сопоставления: тег команды, уверенность (0–1) и каким слоем найдено.
Match = namedtuple("Match", ["tag", "score", "layer"])

# Фолбэк-пул подтверждений в характере (если у команды нет своего «подтверждение»).
# Выбираются без повторов в цикле (общий механизм jarvis.phrases) — мгновенно даёт
# вариативность всем командам, у которых нет собственного пака.
_GENERIC_CONFIRMATIONS = (
    "Сделано, сэр.", "Готово, сэр.", "Выполняю, сэр.", "Выполнено, сэр.",
    "Будет сделано, сэр.", "Как пожелаете, сэр.", "Сию минуту, сэр.", "Разумеется, сэр.",
)
# Реплика на нераспознанное — в характере, без падения.
NOT_RECOGNIZED = "Боюсь, не разобрал, сэр. Повторите?"

# Служебные top-level ключи commands.yaml (НЕ команды): карты продолжений и обратимости.
_RESERVED_KEYS = {"ветки", "обратимость"}

_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Привести фразу к канону: нижний регистр, ё→е, без пунктуации, один пробел."""
    if not text:
        return ""
    low = text.lower().replace("ё", "е")
    low = _PUNCT_RE.sub(" ", low)
    return _SPACE_RE.sub(" ", low).strip()


def _ratio(a: str, b: str) -> float:
    """Нечёткая близость строк (difflib, 0–1) — как для wake-word в stt.py."""
    return SequenceMatcher(None, a, b).ratio()


# --------------------------------------------------------------------------- #
# Эмбеддер: автономная ONNX-модель rubert-tiny2 (без torch), ленивая загрузка
# --------------------------------------------------------------------------- #
class Embedder:
    """Лёгкий ONNX-эмбеддер. Грузится при первом обращении; при сбое — молча
    выключается (encode вернёт None), и матчер работает только на правилах.
    """

    def __init__(self):
        self._session = None
        self._tokenizer = None
        self._input_names: set = set()
        self._failed = False

    @property
    def available(self) -> bool:
        """Готов ли эмбеддер (после попытки ленивой загрузки)."""
        self._ensure()
        return self._session is not None

    def _ensure(self) -> None:
        if self._session is not None or self._failed:
            return
        try:
            import numpy as np  # noqa: F401  (нужен в encode; проверяем заранее)
            import onnxruntime as ort
            from tokenizers import Tokenizer

            self._tokenizer = Tokenizer.from_file(str(config.EMBEDDER_TOKENIZER))
            opts = ort.SessionOptions()
            # На N100 один поток экономнее и предсказуемее, чем гонка за ядра.
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1
            self._session = ort.InferenceSession(
                str(config.EMBEDDER_MODEL),
                sess_options=opts,
                providers=["CPUExecutionProvider"],
            )
            self._input_names = {i.name for i in self._session.get_inputs()}
            _log.info("Эмбеддер rubert-tiny2 загружен (вход: %s)",
                      ", ".join(sorted(self._input_names)))
        except Exception as exc:
            self._failed = True
            self._session = None
            _log.warning("Эмбеддер недоступен: %s — матчер работает только на правилах", exc)
            _log.debug("Трасса загрузки эмбеддера", exc_info=True)

    def encode(self, texts):
        """Вернуть L2-нормированные эмбеддинги фраз (mean-pooling) или None при сбое.

        Пулинг и имена входов сверены на машине: вход input_ids/attention_mask/
        token_type_ids (int64), выход last_hidden_state (B,T,312); mean-pooling по
        attention-маске разделяет фразы заметно лучше CLS.
        """
        self._ensure()
        if self._session is None:
            return None
        try:
            import numpy as np

            encs = [self._tokenizer.encode(t) for t in texts]
            maxlen = max((len(e.ids) for e in encs), default=1)
            ids = np.zeros((len(encs), maxlen), dtype=np.int64)
            att = np.zeros((len(encs), maxlen), dtype=np.int64)
            for i, e in enumerate(encs):
                ids[i, : len(e.ids)] = e.ids
                att[i, : len(e.attention_mask)] = e.attention_mask
            feeds = {"input_ids": ids, "attention_mask": att}
            if "token_type_ids" in self._input_names:
                feeds["token_type_ids"] = np.zeros_like(ids)
            out = self._session.run(None, feeds)[0]  # (B, T, H)
            mask = att[..., None].astype(np.float32)
            summed = (out * mask).sum(axis=1)
            counts = np.clip(mask.sum(axis=1), 1e-9, None)
            emb = summed / counts
            norm = np.clip(np.linalg.norm(emb, axis=1, keepdims=True), 1e-9, None)
            return (emb / norm).astype(np.float32)
        except Exception as exc:
            _log.warning("Сбой инференса эмбеддера: %s — пропускаю эмбеддинги", exc)
            _log.debug("Трасса инференса эмбеддера", exc_info=True)
            return None


# --------------------------------------------------------------------------- #
# Матчер
# --------------------------------------------------------------------------- #
class Matcher:
    """Гибридный распознаватель: правила + эмбеддинги поверх карты команд."""

    def __init__(self, commands: dict, embedder: Optional[Embedder] = None):
        self._commands = commands or {}
        self._embedder = embedder if embedder is not None else Embedder()
        # Слой правил: тег → список (синоним, множество слов, многословный?).
        self._rules: dict[str, list] = {}
        # Индекс точных синонимов: норм. фраза → тег (мгновенное распознавание частых команд).
        self._exact: dict[str, str] = {}
        # Слой эмбеддингов: плоский список (тег, фраза) для кодирования одним батчем.
        self._emb_tags: list[str] = []
        self._emb_phrases: list[str] = []
        self._emb_matrix = None          # np.ndarray (N, H) — считается лениво
        self._emb_ready = False
        self._build_index()

    def _build_index(self) -> None:
        """Собрать индексы правил и фраз для эмбеддингов из commands.yaml."""
        for tag, spec in self._commands.items():
            # Пропускаем СЛУЖЕБНЫЕ top-level ключи (карты продолжений/обратимости), а не команды.
            if tag in _RESERVED_KEYS or not isinstance(spec, dict):
                continue
            synonyms = [normalize(s) for s in (spec.get("синонимы") or []) if s]
            # Предпосчёт для быстрых проверок без difflib: (синоним, множество слов,
            # многословный?) + индекс точных синонимов для O(1)-распознавания.
            rule_list = []
            for s in synonyms:
                self._exact.setdefault(s, tag)  # первый тег с таким синонимом
                words = s.split()
                rule_list.append((s, set(words), len(words) >= 2))
            self._rules[tag] = rule_list
            # Для эмбеддингов берём примеры + синонимы + описание — чем больше живых
            # формулировок, тем устойчивее семантический поиск при промахе правил.
            phrases = list(spec.get("примеры") or [])
            phrases += (spec.get("синонимы") or [])
            if spec.get("описание"):
                phrases.append(spec["описание"])
            for ph in phrases:
                norm = normalize(ph)
                if norm:
                    self._emb_tags.append(tag)
                    self._emb_phrases.append(norm)

    # ------------------------------------------------------------------ #
    # Публичный вход
    # ------------------------------------------------------------------ #
    def match(self, text: str, allowed_tags=None, use_embeddings: bool = True) -> Optional[Match]:
        """Распознать команду: сперва правила, затем эмбеддинги. None — не разобрал.

        allowed_tags (множество тегов) — ОГРАНИЧИТЬ распознавание подмножеством команд: нужно для
        продолжений активной ветки без wake-word (матчим только её команды). None — все команды.
        use_embeddings=False — ТОЛЬКО правила (для продолжений: в малом подмножестве эмбеддинги
        «перематчивают» неродственные фразы, поэтому продолжение требует точного синонима)."""
        query = normalize(text)
        if not query:
            return None
        rule = self._match_rules(query, allowed_tags)
        if rule is not None:
            return rule
        if not use_embeddings:
            return None
        return self._match_embeddings(query, allowed_tags)

    def confirmation(self, tag: str) -> str:
        """Подтверждение в характере для тега: свой пак из commands.yaml или общий пул.

        Поле «подтверждение» может быть СТРОКОЙ (одна фраза) или СПИСКОМ (пак вариаций) — в
        обоих случаях выбор идёт через общий механизм без повторов (jarvis.phrases): для пака
        чередуем вариации (не повторяясь в пределах цикла), для одной фразы возвращаем её же.
        Нет поля — общий пул в характере (тоже без повторов)."""
        spec = self._commands.get(tag) or {}
        own = spec.get("подтверждение")
        if isinstance(own, (list, tuple)):
            variants = [str(v).strip() for v in own if str(v).strip()]
            if variants:
                return phrases.pick(f"confirm:{tag}", variants)
        elif isinstance(own, str) and own.strip():
            return own.strip()
        return phrases.pick("confirm.generic", _GENERIC_CONFIRMATIONS)

    # ------------------------------------------------------------------ #
    # Слой 1: правила
    # ------------------------------------------------------------------ #
    def _match_rules(self, query: str, allowed_tags=None) -> Optional[Match]:
        """Лучшее совпадение по синонимам. Горячий путь — БЕЗ difflib: точное совпадение
        (O(1) по индексу), вхождение фразы, все слова синонима во фразе. Нечёткое (difflib)
        — только fallback, когда быстрые проверки не дали сильного сигнала (искажения STT).
        Так частые/точные команды распознаются за <1 мс вместо ~24 мс (difflib по всем).
        Приоритеты совпадения сохранены: точное 1.0, вхождение 0.96, все слова 0.94, слово 0.92.
        allowed_tags — ограничить подмножеством (продолжения активной ветки)."""
        # 1) Точное совпадение — мгновенно, по индексу (если тег в разрешённых).
        tag = self._exact.get(query)
        if tag is not None and (allowed_tags is None or tag in allowed_tags):
            return Match(tag, 1.0, "rules")
        # 2) Быстрые проверки (вхождение / все слова / однословное ключевое) — без difflib.
        q_tokens = query.split()
        q_set = set(q_tokens)
        best_tag, best_score = None, 0.0
        for tag, rules in self._rules.items():
            if allowed_tags is not None and tag not in allowed_tags:
                continue
            score = 0.0
            for s, s_words, is_multi in rules:
                if is_multi:
                    if s in query:
                        score = max(score, 0.96)
                    elif s_words <= q_set:
                        score = max(score, 0.94)
                elif s in q_set:
                    score = max(score, 0.92)
            if score > best_score:
                best_tag, best_score = tag, score
        # Сильный сигнал (точное слово/вхождение) — difflib не нужен, выходим сразу.
        if best_tag is not None and best_score >= 0.92:
            return Match(best_tag, round(best_score, 3), "rules")
        # 3) Fallback: нечёткое сравнение difflib — редкий путь (быстрые промахнулись).
        return self._match_rules_fuzzy(query, q_tokens, best_tag, best_score, allowed_tags)

    def _match_rules_fuzzy(self, query: str, q_tokens: list[str],
                           best_tag, best_score, allowed_tags=None) -> Optional[Match]:
        """Нечёткое (difflib) сравнение — ловит искажения STT, когда точных совпадений нет.
        Стартует от лучшего быстрого результата, difflib может его только улучшить."""
        for tag, rules in self._rules.items():
            if allowed_tags is not None and tag not in allowed_tags:
                continue
            score = 0.0
            for s, _s_words, is_multi in rules:
                if is_multi:
                    score = max(score, _ratio(query, s))
                else:
                    for tok in q_tokens:
                        score = max(score, _ratio(tok, s))
                    score = max(score, _ratio(query, s))
            if score > best_score:
                best_tag, best_score = tag, score
        if best_tag is not None and best_score >= config.MATCHER_FUZZY_THRESHOLD:
            return Match(best_tag, round(best_score, 3), "rules")
        return None

    # ------------------------------------------------------------------ #
    # Слой 2: эмбеддинги
    # ------------------------------------------------------------------ #
    def _ensure_embeddings(self) -> bool:
        """Лениво подготовить эмбеддинги фраз команд один раз. True — готовы.

        Сначала пробуем диск-кеш (по хешу набора фраз + версии модели) — тогда при
        старте ничего не пересчитываем. Промах кеша → считаем и сохраняем.
        """
        if self._emb_ready:
            return self._emb_matrix is not None
        self._emb_ready = True
        if not self._emb_phrases:
            return False
        cached = self._load_cache()
        if cached is not None:
            self._emb_matrix = cached
            return True
        self._emb_matrix = self._embedder.encode(self._emb_phrases)
        if self._emb_matrix is None:
            _log.warning("Эмбеддинги команд не посчитаны — остаётся только слой правил")
            return False
        self._save_cache(self._emb_matrix)
        return True

    def _cache_key(self) -> str:
        """Ключ кеша: хеш набора фраз + размер/mtime файла модели (инвалидация)."""
        import hashlib
        import os

        h = hashlib.sha256("\n".join(self._emb_phrases).encode("utf-8"))
        try:
            st = os.stat(config.EMBEDDER_MODEL)
            h.update(f"{st.st_size}:{int(st.st_mtime)}".encode())
        except OSError:
            pass
        return h.hexdigest()

    def _load_cache(self):
        """Загрузить матрицу эмбеддингов из npz, если ключ совпал. Иначе None."""
        try:
            import os

            if not os.path.exists(config.MATCHER_CACHE):
                return None
            import numpy as np

            data = np.load(config.MATCHER_CACHE, allow_pickle=False)
            if str(data.get("key")) != self._cache_key():
                return None
            matrix = data.get("matrix")
            if matrix is None or matrix.shape[0] != len(self._emb_phrases):
                return None
            _log.info("Эмбеддинги команд взяты из кеша (%d фраз)", matrix.shape[0])
            return matrix.astype("float32")
        except Exception as exc:
            _log.debug("Кеш эмбеддингов не прочитан (%s) — пересчитаю", exc)
            return None

    def _save_cache(self, matrix) -> None:
        """Сохранить матрицу эмбеддингов в npz рядом с моделью (best-effort)."""
        try:
            import os

            import numpy as np

            os.makedirs(os.path.dirname(config.MATCHER_CACHE) or ".", exist_ok=True)
            np.savez(config.MATCHER_CACHE, matrix=matrix, key=self._cache_key())
            _log.debug("Эмбеддинги команд сохранены в кеш: %s", config.MATCHER_CACHE)
        except Exception as exc:
            _log.debug("Не удалось сохранить кеш эмбеддингов (%s) — не критично", exc)

    def _match_embeddings(self, query: str, allowed_tags=None) -> Optional[Match]:
        """Семантический поиск с порогом и защитой по отрыву (margin).
        allowed_tags — ограничить подмножеством (продолжения активной ветки)."""
        if not self._ensure_embeddings():
            return None
        qv = self._embedder.encode([query])
        if qv is None:
            return None
        try:
            import numpy as np

            sims = self._emb_matrix @ qv[0]  # косинус (векторы уже нормированы)
            # Лучшая близость на КОМАНДУ (max по её фразам), только разрешённые теги.
            best: dict[str, float] = {}
            for tag, sim in zip(self._emb_tags, sims):
                if allowed_tags is not None and tag not in allowed_tags:
                    continue
                if sim > best.get(tag, -1.0):
                    best[tag] = float(sim)
            ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
        except Exception as exc:
            _log.warning("Сбой ранжирования эмбеддингов: %s", exc)
            return None
        if not ranked:
            return None
        top_tag, top_sim = ranked[0]
        second_sim = ranked[1][1] if len(ranked) > 1 else -1.0
        if top_sim < config.MATCHER_EMB_THRESHOLD:
            _log.debug("Эмбеддинги не уверены: лучший %s=%.3f < порога %.2f — переспрос",
                       top_tag, top_sim, config.MATCHER_EMB_THRESHOLD)
            return None
        # Почти равные кандидаты (часто антонимы) → не угадываем, лучше переспросить.
        if (top_sim - second_sim) < config.MATCHER_EMB_MARGIN:
            _log.debug("Эмбеддинги неоднозначны: %s=%.3f vs %s=%.3f — переспрос",
                       top_tag, top_sim, ranked[1][0], second_sim)
            return None
        return Match(top_tag, round(top_sim, 3), "embeddings")


# --------------------------------------------------------------------------- #
# Утилита для diagnostics (jarvis doctor)
# --------------------------------------------------------------------------- #
def sanity_separation(embedder: Optional[Embedder] = None) -> Optional[tuple[float, float]]:
    """Санити эмбеддера: вернуть (близость похожих, близость разных) или None.

    Похожие фразы должны быть ближе разных. Используется doctor'ом, чтобы «модель
    грузится» подтверждалось ещё и осмысленностью векторов, а не только импортом.
    """
    emb = embedder if embedder is not None else Embedder()
    vecs = emb.encode(["сделай громче", "прибавь звук", "выключи вайфай"])
    if vecs is None:
        return None
    try:
        similar = float(vecs[0] @ vecs[1])    # громче ~ прибавь (одно семейство)
        different = float(vecs[0] @ vecs[2])   # громче ~ вайфай (разные)
        return similar, different
    except Exception:
        return None
