#!/usr/bin/env python3
"""
Check that UI literal strings used in GUI code are present in locales.

The script scans string literals used in common UI constructors/setters and
asserts that each literal exists in either ru-RU.json or en-US.json values.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path


UI_CALL_TARGETS = {
    "QLabel",
    "QPushButton",
    "QCheckBox",
    "QGroupBox",
    "QToolButton",
    "QMessageBox.warning",
    "QMessageBox.critical",
    "QMessageBox.information",
    "QMessageBox.question",
    "setText",
    "setToolTip",
    "setPlaceholderText",
    "setTitle",
    "setWindowTitle",
    "addTab",
    "addItem",
}

SKIP_TOKEN_RE = re.compile(r"^[a-z]{1,3}$")


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return (base + "." if base else "") + node.attr
    return ""


def _collect_ui_literals(root: Path) -> list[tuple[str, int, str]]:
    files = [*root.joinpath("gui").rglob("*.py"), root / "main.py"]
    out: list[tuple[str, int, str]] = []
    for path in files:
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source)
        except Exception:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            if name.split(".")[-1] not in UI_CALL_TARGETS and name not in UI_CALL_TARGETS:
                continue
            for arg in node.args:
                if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                    continue
                text = arg.value
                stripped = text.strip()
                if not stripped or len(stripped) < 2:
                    continue
                if SKIP_TOKEN_RE.fullmatch(stripped):
                    continue
                # Skip likely stylesheet payloads.
                if "{" in text and "}" in text and ("Q" in text or ";" in text) and len(text) > 80:
                    continue
                out.append((str(path).replace("\\", "/"), getattr(arg, "lineno", 0), text))

    seen: set[str] = set()
    unique: list[tuple[str, int, str]] = []
    for row in out:
        if row[2] in seen:
            continue
        seen.add(row[2])
        unique.append(row)
    return unique


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    ru_map = json.loads((root / "locales" / "ru-RU.json").read_text(encoding="utf-8"))
    en_map = json.loads((root / "locales" / "en-US.json").read_text(encoding="utf-8"))
    catalog_values = set(ru_map.values()) | set(en_map.values())

    missing = [row for row in _collect_ui_literals(root) if row[2] not in catalog_values]
    if missing:
        print(f"Missing locale coverage: {len(missing)} strings")
        for file_path, line_no, text in missing:
            print(f"{file_path}:{line_no}: {text}")
        return 1

    print("Locale UI coverage: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

