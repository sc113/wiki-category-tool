#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Audit hardcoded Cyrillic string literals in executable Python code.

The script scans Python files with AST, skips docstrings, and reports string
constants that still contain Cyrillic characters. This helps track UI/log text
that should be moved into locale JSON files.
"""

from __future__ import annotations

import argparse
import ast
import os
from collections import Counter


DEFAULT_TARGETS = [
    "gui",
    "workers",
    "core",
    "main.py",
    "__main__.py",
    "utils.py",
]

SKIP_DIR_NAMES = {
    "__pycache__",
    ".venv",
    "v2",
    "design_goal",
    "beta version",
}


def has_cyrillic(value: str) -> bool:
    return any(("А" <= ch <= "я") or ch in "Ёё" for ch in value)


def iter_python_files(root: str, targets: list[str]) -> list[str]:
    files: list[str] = []
    for target in targets:
        path = os.path.join(root, target)
        if os.path.isfile(path) and path.endswith(".py"):
            files.append(path)
            continue
        if not os.path.isdir(path):
            continue
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [name for name in dirnames if name not in SKIP_DIR_NAMES]
            for filename in filenames:
                if filename.endswith(".py"):
                    files.append(os.path.join(dirpath, filename))
    return files


def build_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parent_map: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[child] = parent
    return parent_map


def is_docstring_constant(node: ast.Constant, parent_map: dict[ast.AST, ast.AST]) -> bool:
    parent = parent_map.get(node)
    if not isinstance(parent, ast.Expr):
        return False
    grand_parent = parent_map.get(parent)
    body = getattr(grand_parent, "body", None)
    return isinstance(body, list) and body and body[0] is parent


def collect_cyrillic_literals(root: str, targets: list[str]) -> list[tuple[str, int, str]]:
    results: list[tuple[str, int, str]] = []
    for file_path in iter_python_files(root, targets):
        try:
            source = open(file_path, "r", encoding="utf-8").read()
            tree = ast.parse(source, filename=file_path)
        except Exception:
            continue

        parent_map = build_parent_map(tree)
        rel_path = os.path.relpath(file_path, root)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if not has_cyrillic(node.value):
                continue
            if is_docstring_constant(node, parent_map):
                continue

            text = node.value.strip().replace("\n", "\\n")
            if not text:
                continue
            results.append((rel_path, int(getattr(node, "lineno", 0) or 0), text))
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("targets", nargs="*", default=DEFAULT_TARGETS)
    parser.add_argument("--samples", type=int, default=120)
    args = parser.parse_args()

    root = os.getcwd()
    results = collect_cyrillic_literals(root, list(args.targets))
    counts = Counter(path for path, _, _ in results)

    print("Files with Cyrillic literals:")
    for path, count in counts.most_common():
        print(f"{count:4} {path}")

    print(f"\nTotal files: {len(counts)}")
    print(f"Total string literals: {len(results)}")

    if args.samples > 0:
        print("\nSample literals:")
        for path, line, text in results[: args.samples]:
            safe_text = text.encode("utf-8", errors="replace").decode("utf-8")
            print(f"{path}:{line}: {safe_text[:160]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
