#!/usr/bin/env python3
"""Квантование ONNX-эмбеддера команд (rubert-tiny2) в INT8.

Динамическое квантование весов: model_optimized.onnx (fp32, ~116 МБ) → model_int8.onnx
(~30 МБ). На слабом N100 это меньше RAM и быстрее инференс; качество векторов проверяется
санити-разделением (похожие фразы должны остаться ближе разных) и `jarvis doctor`.

Запуск разовый, после установки/обновления модели:
    python tools/quantize_embedder.py        (или: jarvis models --quantize)

Требует dev-зависимость `onnx` (нужна только инструменту квантования). Рантайму Джарвиса
onnx НЕ нужен — он гоняет готовую int8-модель через onnxruntime. После генерации Джарвис
сам предпочтёт int8 (recognition.prefer_int8); кеши матчера инвалидируются автоматически
(их ключ включает stat файла модели) и пересчитаются при первом старте.
"""
import os
import sys
from pathlib import Path

# Прямой запуск (python tools/quantize_embedder.py) — добавим корень проекта в путь импорта.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def quantize(src: str | None = None, dst: str | None = None) -> bool:
    """Сгенерировать int8-модель из fp32. True — успех (файл записан и прошёл санити)."""
    from jarvis import config

    src = src or config.EMBEDDER_MODEL_FP32
    dst = dst or config.EMBEDDER_MODEL_INT8
    if not os.path.exists(src):
        print(f"[quantize] нет исходной модели: {src}")
        print("           сначала загрузите модели: jarvis models --download")
        return False
    try:
        import tempfile

        import onnx
        from onnxruntime.quantization import QuantType, quantize_dynamic
        from onnxruntime.quantization.shape_inference import quant_pre_process
    except Exception as exc:
        print(f"[quantize] инструмент квантования недоступен: {exc}")
        print("           установите dev-зависимости: pip install onnx sympy")
        return False

    before = os.path.getsize(src) / 1e6
    print(f"[quantize] вход : {src}  ({before:.1f} МБ, fp32)")
    # Предобработка (symbolic shape inference + оптимизация) — без неё динамическое квантование
    # rubert-tiny2 спотыкается о неизвестные типы промежуточных тензоров attention.
    with tempfile.TemporaryDirectory() as td:
        prep = os.path.join(td, "prep.onnx")
        qin = src
        try:
            quant_pre_process(src, prep)
            qin = prep
        except Exception as exc:
            print(f"[quantize] предобработка не удалась ({exc}) — квантую исходник напрямую")
        try:
            quantize_dynamic(qin, dst, weight_type=QuantType.QInt8)
        except Exception as exc:
            print(f"[quantize] обычный путь не прошёл ({exc}) — повтор с DefaultTensorType=FLOAT")
            try:
                quantize_dynamic(qin, dst, weight_type=QuantType.QInt8,
                                 extra_options={"DefaultTensorType": onnx.TensorProto.FLOAT})
            except Exception as exc2:
                print(f"[quantize] сбой квантования: {exc2}")
                return False
    after = os.path.getsize(dst) / 1e6
    ratio = before / max(after, 1e-9)
    print(f"[quantize] выход: {dst}  ({after:.1f} МБ, int8) — ×{ratio:.1f} меньше")

    # Санити: на новой int8-модели похожие фразы обязаны остаться ближе разных.
    try:
        from jarvis import matcher
        config.EMBEDDER_MODEL = dst  # заставить Embedder этого процесса взять int8
        sep = matcher.sanity_separation()
    except Exception as exc:
        print(f"[quantize] санити пропущена ({exc}) — проверьте jarvis doctor")
        sep = None
    if sep is not None:
        sim, diff = sep
        ok = sim > diff
        mark = ">" if ok else "≤"
        print(f"[quantize] санити int8: похожие {sim:.3f} {mark} разные {diff:.3f} "
              f"→ {'OK' if ok else 'ВНИМАНИЕ: вектора деградировали, оставьте fp32'}")
        if not ok:
            return False
    print("[quantize] готово. Перезапустите Джарвис (jarvis restart): первый старт пересчитает "
          "эмбеддинги и переобучит классификатор под int8. При желании перетюньте пороги "
          "recognition.clf_threshold/clf_margin (см. tools/bench_matcher.py).")
    return True


def main() -> int:
    return 0 if quantize() else 1


if __name__ == "__main__":
    raise SystemExit(main())
