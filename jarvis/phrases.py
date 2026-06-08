"""Общий механизм выбора фраз из «пака» без повторов в пределах цикла.

ФУНДАМЕНТ для всех вариативных реплик Джарвиса. Любой набор фраз в проекте
(стартовые, подтверждения команд, ответы перерыва, будущие будильники/таймеры)
выбирается через ОДИН механизм единообразно:

  • первый вызов — случайная фраза из пака;
  • далее — случайная из ЕЩЁ НЕ использованных в текущем цикле;
  • когда израсходована последняя неиспользованная — цикл ПОЛНОСТЬЮ обнуляется
    (все фразы снова доступны), и по возможности новый цикл НЕ начинается с той
    же фразы, что прозвучала только что (нет повтора «впритык» через границу).

Состояние циклов — по каждому паку отдельно (ключ-строка). Лёгкий (только память
процесса, без диска), потокобезопасный (Lock — фразы запрашиваются из разных мест).
Всё в try-except: на выборе фразы НИКОГДА не падаем (деградация → первая фраза/"").

Использование:
    from jarvis import phrases
    text = phrases.pick("startup.ok", config.STARTUP_SUCCESS_PHRASES)
    text = phrases.pick(f"confirm:{tag}", варианты)   # пак на каждую команду
"""
import logging
import random
import threading

_log = logging.getLogger("jarvis-phrases")

# Один лок на весь реестр: операции короткие (выбор индекса), конкуренции почти нет.
_lock = threading.Lock()


class _PackState:
    """Состояние одного пака: что уже сказано в текущем цикле и чем пак «подписан»."""

    __slots__ = ("used", "last", "signature")

    def __init__(self, signature):
        self.used = set()          # индексы фраз, использованные в ТЕКУЩЕМ цикле
        self.last = -1             # последний выбранный индекс (анти-повтор через границу)
        self.signature = signature  # отпечаток пака (len + hash) — правка фраз сбросит цикл


# Реестр состояний по ключу пака. Ключ — стабильное имя ("startup.ok", "confirm:browser").
_packs: dict[str, _PackState] = {}


def pick(key: str, phrases) -> str:
    """Вернуть фразу из пака `key`, не повторяя использованные в текущем цикле.

    `key`     — стабильное имя пака (напр. "startup.ok", "confirm:browser", "break.offer").
                Состояние цикла ведётся по этому ключу отдельно от других паков.
    `phrases` — список/кортеж строк. Пустой → "", из одной фразы → она же.

    Если содержимое пака изменилось (правка settings.yaml/commands.yaml) — цикл для
    этого ключа начинается заново (нет «застрявших» индексов на старом наборе).
    """
    try:
        items = list(phrases) if phrases else []
        n = len(items)
        if n == 0:
            return ""
        if n == 1:
            return str(items[0])
        # Отпечаток пака: длина + хеш кортежа фраз. Меняются фразы → меняется отпечаток
        # → цикл пересоздаётся (иначе used-индексы указывали бы на чужие фразы).
        signature = (n, hash(tuple(str(p) for p in items)))
        with _lock:
            st = _packs.get(key)
            if st is None or st.signature != signature:
                st = _PackState(signature)
                _packs[key] = st
            # Доступные = ещё не использованные в текущем цикле.
            avail = [i for i in range(n) if i not in st.used]
            if not avail:
                # Цикл исчерпан — обнуляем. Новый цикл по возможности не начинаем с
                # только что прозвучавшей фразы (без повтора впритык на стыке циклов).
                st.used.clear()
                avail = [i for i in range(n) if i != st.last] or list(range(n))
            idx = random.choice(avail)
            st.used.add(idx)
            st.last = idx
            return str(items[idx])
    except Exception:
        _log.debug("Сбой выбора фразы для пака %r — отдаю первую", key, exc_info=True)
        try:
            return str(list(phrases)[0]) if phrases else ""
        except Exception:
            return ""


def reset(key: str | None = None) -> None:
    """Сбросить цикл одного пака (`key`) или ВСЕХ паков (`None`). Для тестов/перезапуска."""
    try:
        with _lock:
            if key is None:
                _packs.clear()
            else:
                _packs.pop(key, None)
    except Exception:
        _log.debug("Сбой сброса пака %r", key, exc_info=True)
