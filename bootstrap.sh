#!/usr/bin/env bash
# bootstrap.sh — первичная установка «Джарвиса» (решает проблему «курица-яйцо»:
# команда `jarvis` появляется только ПОСЛЕ pip install, поэтому ставит её этот
# крошечный bash-скрипт на том, что в системе уже есть (python3 + venv).
#
# Делает: venv -> установка пакета. Работает из любой папки, идемпотентен,
# переживает типовые сбои с внятными подсказками. apt сам НЕ вызывает.

set -euo pipefail

# Работаем из директории скрипта, чтобы запуск из любой точки был корректным.
cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"

echo "== Установка «Джарвиса» из ${PROJECT_DIR} =="

# 1. Python 3
if ! command -v python3 >/dev/null 2>&1; then
    echo "✗ python3 не найден. Установите его: sudo apt install -y python3"
    exit 1
fi

# 2. venv (переиспользуем существующий, не падаем)
if [ -d "${VENV_DIR}" ] && [ -x "${VENV_DIR}/bin/python" ]; then
    echo "✓ venv уже существует, переиспользую: ${VENV_DIR}"
else
    echo "… создаю venv: ${VENV_DIR}"
    if ! python3 -m venv "${VENV_DIR}" 2>/dev/null; then
        echo "✗ Не удалось создать venv. Скорее всего не установлен модуль venv."
        echo "  Решение: sudo apt install -y python3-venv"
        echo "  Затем запустите ./bootstrap.sh снова."
        exit 1
    fi
fi

# 3. Обновляем pip и ставим пакет в editable-режиме
echo "… обновляю pip"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip >/dev/null

echo "… устанавливаю пакет (pip install -e .)"
if ! "${VENV_DIR}/bin/pip" install -e .; then
    echo "✗ Установка зависимостей не удалась."
    echo "  Частая причина — engine-пакеты (sherpa-onnx / piper-tts) под вашу платформу."
    echo "  Сверьте доступные версии и при необходимости поправьте pyproject.toml."
    exit 1
fi

echo
echo "✓ Установка завершена."
echo "  Следующий шаг — диагностика здоровья:"
echo
echo "      ${VENV_DIR}/bin/jarvis doctor"
echo
echo "  (или активируйте venv: source ${VENV_DIR}/bin/activate && jarvis doctor)"
