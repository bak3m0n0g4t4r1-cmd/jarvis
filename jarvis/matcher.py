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

from jarvis import config

_log = logging.getLogger("jarvis-matcher")

# Результат сопоставления: тег команды, уверенность (0–1) и каким слоем найдено.
Match = namedtuple("Match", ["tag", "score", "layer"])

# Фолбэк-пул подтверждений в характере (если у команды нет своего «подтверждение»).
_GENERIC_CONFIRMATIONS = ("Сделано, сэр.", "Готово, сэр.", "Выполняю, сэр.")
# Реплика на нераспознанное — в характере, без падения.
NOT_RECOGNIZED = "Боюсь, не разобрал, сэр. Повторите?"

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
        # Слой правил: тег → список нормализованных синонимов (с разбивкой на слова).
        self._rules: dict[str, list[str]] = {}
        # Слой эмбеддингов: плоский список (тег, фраза) для кодирования одним батчем.
        self._emb_tags: list[str] = []
        self._emb_phrases: list[str] = []
        self._emb_matrix = None          # np.ndarray (N, H) — считается лениво
        self._emb_ready = False
        self._build_index()

    def _build_index(self) -> None:
        """Собрать индексы правил и фраз для эмбеддингов из commands.yaml."""
        for tag, spec in self._commands.items():
            spec = spec or {}
            synonyms = [normalize(s) for s in (spec.get("синонимы") or []) if s]
            self._rules[tag] = [s for s in synonyms if s]
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
    def match(self, text: str) -> Optional[Match]:
        """Распознать команду: сперва правила, затем эмбеддинги. None — не разобрал."""
        query = normalize(text)
        if not query:
            return None
        rule = self._match_rules(query)
        if rule is not None:
            return rule
        return self._match_embeddings(query)

    def confirmation(self, tag: str) -> str:
        """Подтверждение в характере для тега: своё из yaml или из общего пула."""
        spec = self._commands.get(tag) or {}
        own = (spec.get("подтверждение") or "").strip()
        if own:
            return own
        # Детерминированный выбор из пула по тегу (без рандома — стабильно и тестируемо).
        return _GENERIC_CONFIRMATIONS[hash(tag) % len(_GENERIC_CONFIRMATIONS)]

    # ------------------------------------------------------------------ #
    # Слой 1: правила
    # ------------------------------------------------------------------ #
    def _match_rules(self, query: str) -> Optional[Match]:
        """Лучшее совпадение по синонимам: точное/вхождение/слово/нечёткое."""
        q_tokens = query.split()
        best_tag, best_score = None, 0.0
        for tag, synonyms in self._rules.items():
            score = self._rule_score(query, q_tokens, synonyms)
            if score > best_score:
                best_tag, best_score = tag, score
        if best_tag is not None and best_score >= config.MATCHER_FUZZY_THRESHOLD:
            return Match(best_tag, round(best_score, 3), "rules")
        return None

    @staticmethod
    def _rule_score(query: str, q_tokens: list[str], synonyms: list[str]) -> float:
        """Оценка совпадения фразы с синонимами одной команды (0–1)."""
        best = 0.0
        for s in synonyms:
            if not s:
                continue
            if query == s:
                return 1.0
            s_words = s.split()
            if len(s_words) >= 2:
                # Многословный синоним. Вхождение фразы целиком — сильнейший сигнал;
                # иначе достаточно, чтобы ВСЕ слова синонима встретились во фразе
                # (порядок неважен) — так направление/полярность задаёт ключевой глагол
                # («включи»/«выключи»), а не нечёткое сходство строк целиком.
                if s in query:
                    best = max(best, 0.96)
                elif all(w in q_tokens for w in s_words):
                    best = max(best, 0.94)
                best = max(best, _ratio(query, s))
            else:
                # Однословный синоним (ключевое слово): целое слово во фразе.
                if s in q_tokens:
                    return 0.92
                # Нечёткое сравнение по словам — ловит искажения STT отдельного слова.
                for tok in q_tokens:
                    best = max(best, _ratio(tok, s))
                best = max(best, _ratio(query, s))
        return best

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

    def _match_embeddings(self, query: str) -> Optional[Match]:
        """Семантический поиск с порогом и защитой по отрыву (margin)."""
        if not self._ensure_embeddings():
            return None
        qv = self._embedder.encode([query])
        if qv is None:
            return None
        try:
            import numpy as np

            sims = self._emb_matrix @ qv[0]  # косинус (векторы уже нормированы)
            # Лучшая близость на КОМАНДУ (max по её фразам).
            best: dict[str, float] = {}
            for tag, sim in zip(self._emb_tags, sims):
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
