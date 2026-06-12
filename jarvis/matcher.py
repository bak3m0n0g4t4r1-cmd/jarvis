"""Лёгкий распознаватель команд: правила + автономные ONNX-эмбеддинги.

Заменяет прежний LLM-диспетчер (Ollama/qwen). Никакой генеративной модели и
облака — только два дешёвых слоя поверх `commands.yaml` (он же источник истины):

  1. СЛОЙ ПРАВИЛ (мгновенный, ноль моделей): нормализуем фразу и сверяем с
     `синонимы` команды — точное совпадение, вхождение фразы, совпадение по слову
     и нечёткое сравнение (difflib, ловит искажения STT). Точные и частые
     формулировки ловятся тут, без всякой модели.
  2. СЛОЙ СЕМАНТИКИ (если правила не дали уверенного ответа): фраза кодируется
     лёгкой моделью rubert-tiny2 (ONNX через onnxruntime, без torch) в 312-вектор, а
     дальше — лёгкий ЛИНЕЙНЫЙ КЛАССИФИКАТОР (LogReg, обучен на синонимах+примерах).
     Предсказание — softmax(W·x+b) в ЧИСТОМ numpy (sklearn в рантайме не грузится):
     выше порога И с заметным отрывом от второго класса → его тег. Если классификатор
     недоступен/выключен — fallback на прежний косинус-kNN по фразам команд.

ВАЖНО (сверено на машине): эмбеддинги rubert-tiny2 НЕ различают антонимы
(«включи»/«выключи», «громче»/«тише» почти неотличимы). Классификатор поверх тех же
векторов эту слепоту НАСЛЕДУЕТ — поэтому направление и полярность ОБЯЗАН задавать
слой правил (синонимы с конкретными словами), а Слой 2 лишь калибрует уверенность и
подсказывает «семейство» команды, когда правила промахнулись. Отсюда же — защита по
отрыву (margin): при почти равных кандидатах возвращаем None (переспрос), чтобы не
выполнить противоположное действие.

Модель эмбеддера грузится ЛЕНИВО (при первом промахе правил); эмбеддинги команд И
веса классификатора считаются ОДИН раз и кешируются на диск (sklearn нужен только при
переобучении после правки commands.yaml). Всё в try-except: сбой эмбеддера → только
правила; сбой/отсутствие классификатора → косинус-kNN; и то и другое — без падения.
"""
import logging
import re
import time
from collections import namedtuple
from difflib import SequenceMatcher
from typing import Optional

from jarvis import config, phrases

_log = logging.getLogger("jarvis-matcher")

# Версия формата/алгоритма классификатора: бамп инвалидирует кеш весов (matcher_clf.npz).
_CLF_VERSION = 1

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
NOT_RECOGNIZED = "Бо+юсь, не разобр+ал, сэр. Повтор+ите?"

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
        # Слой 2 «классификатор»: экспортированные веса (W, b, классы) — считаются/грузятся лениво.
        self._clf_w = None               # np.ndarray (C, H)
        self._clf_b = None               # np.ndarray (C,)
        self._clf_classes = None         # np.ndarray (C,) тегов
        self._clf_ready = False          # попытка подготовки уже была
        self._clf_ok = False             # классификатор готов к предсказанию
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
    def match(self, text: str, allowed_tags=None, use_embeddings: bool = True,
              branch_tags=None) -> Optional[Match]:
        """Распознать команду: сперва правила, затем семантика (Слой 2). None — не разобрал.

        allowed_tags (множество тегов) — ОГРАНИЧИТЬ распознавание подмножеством команд: нужно для
        продолжений активной ветки без wake-word (матчим только её команды). None — все команды.
        use_embeddings=False — ТОЛЬКО правила (для продолжений: в малом подмножестве семантика
        «перематчивает» неродственные фразы, поэтому продолжение требует точного синонима).
        branch_tags (множество тегов активной ветки) — МЯГКИЙ контекстный приоритет для Слоя 2: при
        почти равных кандидатах предпочесть команду из текущей ветки (не жёсткий фильтр, см. ТЗ-5)."""
        perf = config.PERF_DEBUG
        t0 = time.perf_counter() if perf else 0.0
        query = normalize(text)
        if not query:
            return None
        t_norm = time.perf_counter() if perf else 0.0
        rule = self._match_rules(query, allowed_tags)
        if perf:
            t_rules = time.perf_counter()
            _log.debug("PERF matcher: норм %.2fмс, правила %.2fмс → %s",
                       (t_norm - t0) * 1000, (t_rules - t_norm) * 1000,
                       rule.tag if rule else ("—" if use_embeddings else "переспрос (только правила)"))
        if rule is not None:
            return rule
        if not use_embeddings:
            return None
        return self._match_semantic(query, allowed_tags, branch_tags)

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
    # Слой 2: семантика (классификатор; косинус — fallback)
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

    def _match_semantic(self, query: str, allowed_tags=None, branch_tags=None) -> Optional[Match]:
        """Слой 2: классификатор (если готов), иначе косинус-fallback. None — переспрос.

        allowed_tags — ограничить подмножеством (продолжения активной ветки).
        branch_tags — мягкий контекстный приоритет: при ничьей предпочесть команду активной ветки.
        """
        if not self._ensure_embeddings():
            return None
        perf = config.PERF_DEBUG
        tv0 = time.perf_counter() if perf else 0.0
        qv = self._embedder.encode([query])
        if qv is None:
            return None
        tv1 = time.perf_counter() if perf else 0.0
        if self._ensure_classifier():
            res = self._rank_classifier(qv[0], allowed_tags, branch_tags)
            layer = "classifier"
        else:
            res = self._rank_cosine(qv[0], allowed_tags)
            layer = "cosine"
        if perf:
            _log.debug("PERF Слой2: vectorize %.2fмс, predict(%s) %.2fмс → %s",
                       (tv1 - tv0) * 1000, layer, (time.perf_counter() - tv1) * 1000,
                       res.tag if res else "переспрос")
        return res

    # ---- Классификатор: ленивая подготовка (обучение sklearn / загрузка весов) ----
    def _ensure_classifier(self) -> bool:
        """Лениво подготовить веса линейного классификатора. True — готов к предсказанию.

        Порядок: выключен конфигом → False (косинус). Иначе пробуем диск-кеш весов (numpy,
        без sklearn). Промах → обучаем sklearn-LogReg на кешированной матрице эмбеддингов,
        экспортируем веса в numpy и сохраняем. Любой сбой → False (деградация на косинус).
        """
        if self._clf_ready:
            return self._clf_ok
        if not config.MATCHER_CLF_ENABLED:
            self._clf_ready = True
            return False
        # Самодостаточность: классификатору нужна матрица эмбеддингов (идемпотентно, по кешу).
        if not self._ensure_embeddings() or not self._emb_tags:
            self._clf_ready = True
            return False
        self._clf_ready = True
        if self._load_clf_cache():           # 1) кеш весов — быстро, без sklearn
            self._clf_ok = True
            return True
        try:                                 # 2) обучение — sklearn лениво, только здесь
            self._train_classifier()
            self._save_clf_cache()
            self._clf_ok = True
            return True
        except Exception as exc:
            _log.warning("Классификатор не обучен (%s) — Слой 2 работает на косинусе", exc)
            _log.debug("Трасса обучения классификатора", exc_info=True)
            self._clf_ok = False
            return False

    def _train_classifier(self) -> None:
        """Обучить LogReg на (эмбеддинги фраз → теги) и экспортировать веса в numpy.

        softmax(W·x+b) воспроизводит predict_proba мультиномиальной LogReg точно, поэтому
        в рантайме sklearn не нужен — предсказание идёт на чистом numpy (см. _rank_classifier).
        """
        import numpy as np
        from sklearn.linear_model import LogisticRegression

        x = np.asarray(self._emb_matrix, dtype=np.float64)
        y = np.asarray(self._emb_tags)
        clf = LogisticRegression(
            C=float(config.MATCHER_CLF_C),
            class_weight="balanced",
            max_iter=2000,
        )
        clf.fit(x, y)
        coef = np.asarray(clf.coef_, dtype=np.float32)            # (C, H) или (1, H) при 2 классах
        intercept = np.asarray(clf.intercept_, dtype=np.float32)  # (C,) или (1,)
        classes = np.asarray(clf.classes_)
        # Бинарный частный случай sklearn (один ряд весов) → разворачиваем в 2 класса для softmax.
        if coef.shape[0] == 1 and classes.shape[0] == 2:
            coef = np.vstack([-coef[0], coef[0]])
            intercept = np.array([-intercept[0], intercept[0]], dtype=np.float32)
        self._clf_w = coef
        self._clf_b = intercept
        self._clf_classes = classes
        _log.info("Классификатор Слоя 2 обучен: %d классов, %d обучающих фраз",
                  classes.shape[0], x.shape[0])

    def _rank_classifier(self, qv, allowed_tags=None, branch_tags=None) -> Optional[Match]:
        """Предсказание классификатора: softmax(W·x+b) в numpy + порог/отрыв/контекст-бонус."""
        try:
            import numpy as np

            logits = self._clf_w @ np.asarray(qv, dtype=np.float32) + self._clf_b  # (C,)
            classes = self._clf_classes
            if allowed_tags is not None:
                keep = [i for i, t in enumerate(classes) if t in allowed_tags]
                if not keep:
                    return None
                logits = logits[keep]
                classes = classes[keep]
            ex = np.exp(logits - float(logits.max()))
            proba = ex / float(ex.sum())
            order = np.argsort(proba)[::-1]
        except Exception as exc:
            _log.warning("Сбой предсказания классификатора: %s — пробую косинус", exc)
            return self._rank_cosine(qv, allowed_tags)
        top_tag = str(classes[order[0]])
        top_p = float(proba[order[0]])
        second_tag = str(classes[order[1]]) if order.size > 1 else None
        second_p = float(proba[order[1]]) if order.size > 1 else 0.0
        if top_p < config.MATCHER_CLF_THRESHOLD:
            _log.debug("Классификатор не уверен: %s=%.3f < порога %.2f — переспрос",
                       top_tag, top_p, config.MATCHER_CLF_THRESHOLD)
            return None
        if (top_p - second_p) < config.MATCHER_CLF_MARGIN:
            # Мягкий контекст ветки (ТЗ-5): РОВНО один из двух кандидатов в активной ветке →
            # предпочесть его (разводит «семейства», но НЕ антонимы: оба-в-ветке → всё равно переспрос).
            if branch_tags and second_tag is not None:
                in1, in2 = (top_tag in branch_tags), (second_tag in branch_tags)
                if in1 != in2:
                    chosen, p = (top_tag, top_p) if in1 else (second_tag, second_p)
                    _log.debug("Контекст ветки развёл ничью %s/%s → %s", top_tag, second_tag, chosen)
                    return Match(chosen, round(p, 3), "classifier")
            _log.debug("Классификатор неоднозначен: %s=%.3f vs %s=%.3f — переспрос",
                       top_tag, top_p, second_tag, second_p)
            return None
        return Match(top_tag, round(top_p, 3), "classifier")

    def _rank_cosine(self, qv, allowed_tags=None) -> Optional[Match]:
        """Косинус-kNN fallback (прежний Слой 2): max-близость на команду + порог/отрыв."""
        try:
            import numpy as np

            sims = self._emb_matrix @ np.asarray(qv, dtype=np.float32)  # косинус (векторы нормированы)
            best: dict[str, float] = {}
            for tag, sim in zip(self._emb_tags, sims):
                if allowed_tags is not None and tag not in allowed_tags:
                    continue
                if sim > best.get(tag, -1.0):
                    best[tag] = float(sim)
            ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
        except Exception as exc:
            _log.warning("Сбой ранжирования косинуса: %s", exc)
            return None
        if not ranked:
            return None
        top_tag, top_sim = ranked[0]
        second_sim = ranked[1][1] if len(ranked) > 1 else -1.0
        if top_sim < config.MATCHER_EMB_THRESHOLD:
            _log.debug("Косинус не уверен: лучший %s=%.3f < порога %.2f — переспрос",
                       top_tag, top_sim, config.MATCHER_EMB_THRESHOLD)
            return None
        if (top_sim - second_sim) < config.MATCHER_EMB_MARGIN:
            _log.debug("Косинус неоднозначен: %s=%.3f vs %s=%.3f — переспрос",
                       top_tag, top_sim, ranked[1][0], second_sim)
            return None
        return Match(top_tag, round(top_sim, 3), "embeddings")

    # ---- Кеш весов классификатора (numpy, без sklearn в рантайме) ----
    def _clf_cache_key(self) -> str:
        """Ключ кеша весов: хеш фраз+тегов + stat модели + гиперпараметры + версия."""
        import hashlib
        import os

        h = hashlib.sha256(("\n".join(self._emb_phrases)).encode("utf-8"))
        h.update(b"\x00")
        h.update(("\n".join(self._emb_tags)).encode("utf-8"))
        try:
            st = os.stat(config.EMBEDDER_MODEL)
            h.update(f"{st.st_size}:{int(st.st_mtime)}".encode())
        except OSError:
            pass
        h.update(f"|C={float(config.MATCHER_CLF_C)}|v={_CLF_VERSION}".encode())
        return h.hexdigest()

    def _load_clf_cache(self) -> bool:
        """Загрузить веса классификатора из npz, если ключ совпал. True — загружено."""
        try:
            import os

            if not os.path.exists(config.MATCHER_CLF_CACHE):
                return False
            import numpy as np

            data = np.load(config.MATCHER_CLF_CACHE, allow_pickle=False)
            if str(data.get("key")) != self._clf_cache_key():
                return False
            w = data["w"].astype("float32")
            b = data["b"].astype("float32")
            classes = data["classes"]
            if w.shape[0] != classes.shape[0]:
                return False
            self._clf_w, self._clf_b, self._clf_classes = w, b, classes
            _log.info("Веса классификатора Слоя 2 взяты из кеша (%d классов)", classes.shape[0])
            return True
        except Exception as exc:
            _log.debug("Кеш классификатора не прочитан (%s) — переобучу", exc)
            return False

    def _save_clf_cache(self) -> None:
        """Сохранить веса классификатора в npz рядом с моделью (best-effort)."""
        try:
            import os

            import numpy as np

            os.makedirs(os.path.dirname(config.MATCHER_CLF_CACHE) or ".", exist_ok=True)
            np.savez(config.MATCHER_CLF_CACHE, w=self._clf_w, b=self._clf_b,
                     classes=self._clf_classes, key=self._clf_cache_key())
            _log.debug("Веса классификатора сохранены в кеш: %s", config.MATCHER_CLF_CACHE)
        except Exception as exc:
            _log.debug("Не удалось сохранить кеш классификатора (%s) — не критично", exc)


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
