# -*- coding: utf-8 -*-
"""
Загрузка словарей локализации из JSON-файлов.
"""

from __future__ import annotations

import json
import os
import sys

from ..utils import resource_path

_LOCALE_FILES = {
    'ru': 'ru-RU.json',
    'en': 'en-US.json',
}
_PROJECT_TEXTS_FILE = 'project-texts.json'
_RUNTIME_UI_LANG = 'ru'
_LOCALE_CACHE: dict[str, dict[str, str]] = {}
_PROJECT_TEXT_CACHE: dict[str, dict[str, dict[str, str]]] = {}
_PAIR_CACHE: list[tuple[str, str]] | None = None


def _normalized_lang(lang: str | None) -> str:
    value = str(lang or 'ru').lower()
    return 'en' if value.startswith('en') else 'ru'


def _locale_dir_candidates() -> list[str]:
    pkg_root = os.path.dirname(os.path.dirname(__file__))
    repo_root = os.path.dirname(pkg_root)
    meipass = getattr(sys, '_MEIPASS', '') or ''
    exe_dir = os.path.dirname(getattr(sys, 'executable', '') or '')
    candidates = [
        os.path.join(meipass, 'locales') if meipass else '',
        os.path.join(meipass, 'wiki_cat_tool', 'locales') if meipass else '',
        resource_path('locales'),
        os.path.join(pkg_root, 'locales'),
        os.path.join(repo_root, 'locales'),
        os.path.join(exe_dir, 'locales') if exe_dir else '',
        os.path.join(os.getcwd(), 'locales'),
        os.path.join(pkg_root, 'design_goal', 'locales'),
    ]
    deduped: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        if not path:
            continue
        norm = os.path.normcase(os.path.normpath(path))
        if norm in seen:
            continue
        seen.add(norm)
        deduped.append(path)
    return deduped


def _load_json_file(path: str) -> dict[str, str]:
    try:
        with open(path, encoding='utf-8') as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
    except Exception:
        pass
    return {}


def _load_nested_json_file(path: str) -> dict[str, dict[str, str]]:
    try:
        with open(path, encoding='utf-8') as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            normalized: dict[str, dict[str, str]] = {}
            for key, value in raw.items():
                if not isinstance(value, dict):
                    continue
                normalized[str(key)] = {
                    str(lang): str(text)
                    for lang, text in value.items()
                    if isinstance(text, str)
                }
            return normalized
    except Exception:
        pass
    return {}


def load_locale_map(lang: str | None) -> dict[str, str]:
    norm = _normalized_lang(lang)
    cached = _LOCALE_CACHE.get(norm)
    if cached:
        return cached
    file_name = _LOCALE_FILES.get(norm, _LOCALE_FILES['ru'])
    for base in _locale_dir_candidates():
        try:
            path = os.path.join(base, file_name)
            if os.path.isfile(path):
                data = _load_json_file(path)
                if data:
                    _LOCALE_CACHE[norm] = data
                    return data
        except Exception:
            continue
    return {}


def load_translation_pairs() -> list[tuple[str, str]]:
    global _PAIR_CACHE
    if _PAIR_CACHE is not None:
        return list(_PAIR_CACHE)
    ru_map = load_locale_map('ru')
    en_map = load_locale_map('en')
    if not ru_map or not en_map:
        return []
    keys = [k for k in ru_map.keys() if k in en_map]
    _PAIR_CACHE = [(ru_map[k], en_map[k]) for k in keys]
    return list(_PAIR_CACHE)


def load_project_text_map() -> dict[str, dict[str, str]]:
    cached = _PROJECT_TEXT_CACHE.get('project')
    if cached:
        return cached
    for base in _locale_dir_candidates():
        try:
            path = os.path.join(base, _PROJECT_TEXTS_FILE)
            if os.path.isfile(path):
                data = _load_nested_json_file(path)
                if data:
                    _PROJECT_TEXT_CACHE['project'] = data
                    return data
        except Exception:
            continue
    return {}


def translate_key(key: str, lang: str | None = None, default: str = '') -> str:
    try:
        value = load_locale_map(lang).get(str(key), '')
        if value:
            return value
    except Exception:
        pass
    return default


def translate_project_key(key: str, lang: str | None = None, default: str = '') -> str:
    try:
        values = load_project_text_map().get(str(key), {})
        if not values:
            return default
        lang_value = str(lang or 'en').strip().lower()
        candidates = [
            lang_value,
            lang_value.split('-', 1)[0],
            'en',
            'default',
        ]
        for candidate in candidates:
            value = values.get(candidate, '')
            if value:
                return value
    except Exception:
        pass
    return default


def set_runtime_ui_language(lang: str | None) -> None:
    global _RUNTIME_UI_LANG
    _RUNTIME_UI_LANG = _normalized_lang(lang)


def get_runtime_ui_language() -> str:
    return _RUNTIME_UI_LANG


def translate_runtime(key: str, default: str = '') -> str:
    return translate_key(key, _RUNTIME_UI_LANG, default)
