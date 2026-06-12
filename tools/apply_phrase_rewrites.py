#!/usr/bin/env python3
"""Применить переписанные реплики (original→rewritten) к settings.yaml и commands.yaml.

Разовый инструмент Этапа «голос Джарвиса». Заменяет ТОЛЬКО значения-фразы построчно, сохраняя
комментарии, отступы, структуру YAML (yaml.dump бы их потерял). Перед записью — резервные копии.

Вход: JSON {"pairs": [{"original": "...", "rewritten": "..."}, ...]} (вывод workflow переписки).
Каждую original ищем как ЗНАЧЕНИЕ YAML (элемент списка `- "..."`/`- ...` или `ключ: "..."`),
сверяем по точному совпадению извлечённого значения и подставляем rewritten.

Защита: меняем строку только если её распарсенное значение РОВНО равно original (не подстрока) —
ложные срабатывания в комментариях/конфиге исключены. Плейсхолдеры/структуру проверяет валидатор.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

_VOWELS = "аеёиоуыэюяАЕЁИОУЫЭЮЯ"


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    return v


def _yaml_value_of_line(line: str):
    """Извлечь (значение, как_было_оформлено) из строки YAML, если это скаляр-строка.

    Возвращает (value, kind) где kind: 'item' (- ...), 'kv' (key: ...) или None."""
    body = line.rstrip("\n")
    m = re.match(r"^(\s*)-\s+(.*)$", body)
    if m and m.group(2).strip() and not m.group(2).lstrip().startswith("#"):
        return _unquote(m.group(2)), "item"
    m = re.match(r"^(\s*)([^\s:#][^:]*):\s+(.+)$", body)
    if m and m.group(3).strip() and not m.group(3).lstrip().startswith("#"):
        return _unquote(m.group(3)), "kv"
    return None, None


def _emit_value(rewritten: str) -> str:
    """Оформить значение для YAML: в двойных кавычках с экранированием (фразы содержат « » — ок)."""
    esc = rewritten.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{esc}"'


def _strip_stress(s: str) -> str:
    return s.replace("+", "")


def _norm(s: str) -> str:
    """Канонизация для сверки «только ударения»: убрать `+`, унифицировать ё→е."""
    return s.replace("+", "").replace("ё", "е").replace("Ё", "Е")


def _stress_problems(text: str) -> list[str]:
    bad = []
    for i, ch in enumerate(text):
        if ch == "+":
            nxt = text[i + 1] if i + 1 < len(text) else ""
            if nxt not in _VOWELS:
                bad.append(text[max(0, i - 6):i + 2])
    return bad


def _placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{[^}]+\}", text))


def _accept(value: str, rewritten: str, report: dict) -> bool:
    """ЖЕСТКОЕ ПРАВИЛО: правку принимаем ТОЛЬКО если она отличается от оригинала исключительно
    вставкой `+` (и е/ё) — смысл/формулировки не тронуты, меняются лишь ударения. Любое
    изменение букв (полировка/опечатка агента: «переnосить», «неосталось») → отказ."""
    if _norm(rewritten) != _norm(value):
        report["changed_skips"].append((value, rewritten))
        return False
    if _placeholders(rewritten) != _placeholders(value):
        report["placeholder_skips"].append(value)
        return False
    sp = _stress_problems(rewritten)
    if sp:
        report["stress_skips"].append((value, sp))
        return False
    return True


_QUOTED = re.compile(r'"([^"]*)"' + r"|'([^']*)'")


def _replace_in_line(line: str, mapping: dict, report: dict, used: set) -> tuple[str, int]:
    """Заменить в строке все строковые литералы в кавычках, чьё содержимое есть в mapping.

    Покрывает flow-списки (key: ["a","b"]), block-элементы в кавычках и kv. Содержимое сверяется
    по точному совпадению; принимается только «чистая» разметка ударений (_accept)."""
    n = 0

    def sub(m):
        nonlocal n
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        if inner in mapping and _accept(inner, mapping[inner], report):
            n += 1
            used.add(inner)
            return _emit_value(mapping[inner])
        return m.group(0)

    new = _QUOTED.sub(sub, line)
    if n:
        return new, n
    # Фоллбэк: block-скаляр БЕЗ кавычек (- value / key: value).
    value, kind = _yaml_value_of_line(line)
    if value is not None and value in mapping and _accept(value, mapping[value], report):
        indent = line[: len(line) - len(line.lstrip())]
        used.add(value)
        if kind == "item":
            return f"{indent}- {_emit_value(mapping[value])}\n", 1
        key = line.split(":", 1)[0]
        return f"{key}: {_emit_value(mapping[value])}\n", 1
    return line, 0


def apply_to_file(path: Path, mapping: dict, report: dict) -> int:
    text = path.read_text(encoding="utf-8")
    path.with_suffix(path.suffix + ".bak").write_text(text, encoding="utf-8")
    lines = text.splitlines(keepends=True)
    changed = 0
    used = report.setdefault("used", set())
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):   # чистый комментарий — не трогаем
            continue
        lines[i], n = _replace_in_line(line, mapping, report, used)
        changed += n
    path.write_text("".join(lines), encoding="utf-8")
    report["applied"][str(path)] = changed
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description="Применить переписанные реплики к YAML")
    ap.add_argument("pairs_json", help="JSON с {pairs:[{original,rewritten}]}")
    ap.add_argument("--dry-run", action="store_true", help="только отчёт, без записи")
    args = ap.parse_args()
    data = json.load(open(args.pairs_json, encoding="utf-8"))
    pairs = data.get("pairs", data) if isinstance(data, dict) else data
    mapping = {}
    dup = 0
    for p in pairs:
        o, r = p.get("original"), p.get("rewritten")
        if not o or not r:
            continue
        if o in mapping and mapping[o] != r:
            dup += 1
        mapping[o] = r
    print(f"Пар на вход: {len(pairs)} | уникальных original: {len(mapping)} | конфликтов: {dup}")
    report = {"applied": {}, "placeholder_skips": [], "stress_skips": [], "changed_skips": [],
              "used": set()}
    if args.dry_run:
        # Подсчёт совпадений без записи.
        import yaml
        for fp in (BASE / "settings.yaml", BASE / "commands.yaml"):
            txt = fp.read_text(encoding="utf-8")
            hit = sum(1 for ln in txt.splitlines()
                      if (_yaml_value_of_line(ln + "\n")[0] in mapping))
            print(f"  {fp.name}: совпадёт ~{hit} строк")
        return 0
    total = 0
    for fp in (BASE / "settings.yaml", BASE / "commands.yaml"):
        total += apply_to_file(fp, mapping, report)
    print(f"Заменено строк: {total}")
    print(f"  settings.yaml: {report['applied'].get(str(BASE/'settings.yaml'),0)}")
    print(f"  commands.yaml: {report['applied'].get(str(BASE/'commands.yaml'),0)}")
    unused = set(mapping) - report["used"]
    if report["placeholder_skips"]:
        print(f"  ⚠ пропущено по плейсхолдерам: {len(report['placeholder_skips'])}")
    if report["stress_skips"]:
        print(f"  ⚠ пропущено по битым `+`: {len(report['stress_skips'])}")
        for v, sp in report["stress_skips"][:5]:
            print(f"      {v!r}: {sp}")
    if report["changed_skips"]:
        print(f"  ⚠ пропущено (изменены слова, не только ударения): {len(report['changed_skips'])}")
        for v, r in report["changed_skips"][:8]:
            print(f"      O: {v!r}\n      R: {r!r}")
    if unused:
        print(f"  ⚠ не найдено в YAML (возможно хардкод/синоним): {len(unused)}")
        for u in list(unused)[:8]:
            print(f"      {u!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
