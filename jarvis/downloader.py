"""Загрузчик моделей «Джарвиса» по описанию из models.yaml.

URL не зашиты в код: правка протухшей ссылки = одна строка в models.yaml,
без Python. Загрузчик валидирует результат (HTTP 200, размер, опциональная
sha256, не HTML-заглушка вместо модели) и при необходимости распаковывает
архивы. Любой сбой — человеческое сообщение, без голого трейсбека.
"""
import hashlib
import tarfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import yaml

from jarvis import config

MODELS_YAML = config.BASE_DIR / "models.yaml"


def _looks_like_html(path: Path) -> bool:
    """Грубая защита от «страница ошибки вместо модели»."""
    try:
        with open(path, "rb") as f:
            head = f.read(512).lstrip().lower()
        return head.startswith(b"<!doctype html") or head.startswith(b"<html")
    except Exception:
        return False


def _download_one(name: str, spec: dict) -> bool:
    """Скачать и проверить одну модель. Возвращает True при успехе."""
    url = spec.get("url")
    dest = config.MODELS_DIR / spec.get("назначение", name)
    expected_size = spec.get("размер")          # опционально
    expected_sha = spec.get("sha256")           # опционально (может отсутствовать)
    unpack = spec.get("распаковать")            # tar.bz2/tar.gz/zip -> в каталог

    if not url:
        print(f"✗ {name}: в models.yaml не указан url. Проверьте файл.")
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"… качаю {name} → {dest}")
    # ВНИМАНИЕ: валидность URL из контейнера не проверить — сверьте models.yaml вручную.
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310
            if getattr(resp, "status", 200) != 200:
                print(f"✗ {name}: сервер вернул код {resp.status}. Проверьте ссылку в models.yaml.")
                return False
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            block = 1024 * 256
            with open(tmp, "wb") as out:
                while True:
                    chunk = resp.read(block)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded * 100 // total
                        print(f"\r  {pct:3d}%  ({downloaded // 1024} КБ)", end="", flush=True)
            print()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"✗ не удалось скачать {name}: {exc}. Проверьте ссылку в models.yaml.")
        return False
    except Exception as exc:
        print(f"✗ непредвиденная ошибка при загрузке {name}: {exc}")
        return False

    # --- Валидация скачанного ---
    if _looks_like_html(tmp):
        tmp.unlink(missing_ok=True)
        print(f"✗ {name}: вместо модели пришла HTML-страница. Ссылка в models.yaml протухла.")
        return False
    actual_size = tmp.stat().st_size
    if expected_size and abs(actual_size - int(expected_size)) > max(1024, int(expected_size) * 0.02):
        tmp.unlink(missing_ok=True)
        print(f"✗ {name}: размер {actual_size} Б не совпал с ожидаемым {expected_size} Б.")
        return False
    if expected_sha:
        digest = _sha256(tmp)
        if digest.lower() != str(expected_sha).lower():
            tmp.unlink(missing_ok=True)
            print(f"✗ {name}: sha256 не совпала (получено {digest}).")
            return False
    else:
        # Отсутствие суммы НЕ блокирует первую загрузку — только предупреждаем.
        print(f"  ⚠ {name}: sha256 не задана в models.yaml — пропускаю проверку контрольной суммы.")

    tmp.replace(dest)

    if unpack:
        # Каталог распаковки задаётся явно (распаковать_в), иначе — в models/.
        target = config.MODELS_DIR / spec.get("распаковать_в", "")
        if not _unpack(dest, target):
            return False
    print(f"✓ {name} готово.")
    return True


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _unpack(archive: Path, target_dir: Path) -> bool:
    """Распаковать архив модели рядом и удалить архив."""
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        if archive.name.endswith((".tar.bz2", ".tar.gz", ".tgz", ".tbz2")):
            with tarfile.open(archive) as tar:
                tar.extractall(target_dir)  # noqa: S202 — источник доверенный (models.yaml)
        elif archive.name.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(target_dir)
        else:
            print(f"✗ не знаю, как распаковать {archive.name}")
            return False
        archive.unlink(missing_ok=True)
        print(f"  распаковано в {target_dir}")
        return True
    except Exception as exc:
        print(f"✗ ошибка распаковки {archive.name}: {exc}")
        return False


def download_all() -> bool:
    """Скачать все модели из models.yaml. Возвращает True, если всё успешно."""
    if not MODELS_YAML.exists():
        print(f"✗ не найден {MODELS_YAML}. Он должен лежать в корне проекта.")
        return False
    try:
        with open(MODELS_YAML, encoding="utf-8") as f:
            spec = yaml.safe_load(f) or {}
    except Exception as exc:
        print(f"✗ не удалось разобрать models.yaml: {exc}")
        return False

    models = spec.get("модели") or {}
    if not models:
        print("✗ в models.yaml нет секции «модели».")
        return False

    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    ok = True
    for name, item in models.items():
        ok = _download_one(name, item or {}) and ok
    if ok:
        print("\n✓ Все модели загружены. Проверьте: jarvis doctor")
    else:
        print("\n✗ Часть моделей не загрузилась — см. сообщения выше и правьте models.yaml.")
    return ok
