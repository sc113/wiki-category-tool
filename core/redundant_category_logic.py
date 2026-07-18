# -*- coding: utf-8 -*-
"""
Общая логика удаления избыточных категорий.
"""

import csv
import re

import mwparserfromhell

from ..constants import DEFAULT_EN_NS, EN_PREFIX_ALIASES
from .localization import translate_runtime
from .namespace_manager import _load_ns_info, get_policy_prefix
from ..utils import default_redundant_category_dedupe_summary


REDUNDANT_MODE_PAIRS = 'pairs'
REDUNDANT_MODE_DEDUP = 'dedupe'


class _SafeFormatDict(dict):
    def __missing__(self, key):
        return '{' + key + '}'


def category_prefix_aliases(family: str, lang: str) -> tuple[str, ...]:
    """Возвращает все известные префиксы категории без двоеточия."""
    prefixes: set[str] = set()
    try:
        info = _load_ns_info(family, lang) or {}
        meta = info.get(14, {}) or {}
        for prefix in meta.get('all', set()) or set():
            cleaned = str(prefix or '').strip().rstrip(':')
            if cleaned:
                prefixes.add(cleaned)
    except Exception:
        pass

    base_en = (DEFAULT_EN_NS.get(14) or 'Category:').strip().rstrip(':')
    if base_en:
        prefixes.add(base_en)

    for alias in EN_PREFIX_ALIASES.get(14, set()):
        cleaned = str(alias or '').strip().rstrip(':')
        if cleaned:
            prefixes.add(cleaned)

    return tuple(sorted(prefixes, key=lambda value: (-len(value), value.casefold())))


def normalize_category_name(name: str, prefixes: tuple[str, ...]) -> str:
    """Нормализует название категории для сравнения."""
    # MediaWiki treats underscores in page titles as spaces.
    value = re.sub(r'[_\s]+', ' ', (name or '').strip())
    lower = value.casefold()
    for prefix in prefixes:
        prefix_text = (prefix or '').strip().rstrip(':')
        if not prefix_text:
            continue
        normalized_prefix = prefix_text.casefold() + ':'
        if lower.startswith(normalized_prefix):
            value = value[len(prefix_text) + 1:].strip()
            break
    return re.sub(r'[_\s]+', ' ', value).strip()


def load_redundant_category_rules(
    file_path: str,
    family: str,
    lang: str,
) -> tuple[str, dict[str, set[str]], set[str]]:
    """Загружает TSV и определяет режим:

    - `pairs`: строки формата `точная<TAB>широкая`
    - `dedupe`: строки с одной колонкой `категория`
    """
    prefixes = category_prefix_aliases(family, lang)
    precise_to_broad_map: dict[str, set[str]] = {}
    dedupe_categories: set[str] = set()
    one_col_rows = 0
    two_col_rows = 0
    with open(file_path, newline='', encoding='utf-8-sig') as file_obj:
        reader = csv.reader(file_obj, delimiter='\t')
        for row in reader:
            if not row:
                continue
            precise_cat = normalize_category_name(row[0], prefixes)
            if not precise_cat:
                continue
            broad_raw = row[1] if len(row) >= 2 else ''
            broad_cat = normalize_category_name(broad_raw, prefixes)
            if broad_cat:
                two_col_rows += 1
                precise_to_broad_map.setdefault(precise_cat, set()).add(broad_cat)
            else:
                one_col_rows += 1
                dedupe_categories.add(precise_cat)

    if one_col_rows and two_col_rows:
        raise ValueError(
            translate_runtime('error.redundant_category.mixed_format', '')
        )

    if two_col_rows:
        return REDUNDANT_MODE_PAIRS, precise_to_broad_map, set()
    return REDUNDANT_MODE_DEDUP, {}, dedupe_categories


def detect_redundant_category_mode(file_path: str, family: str, lang: str) -> str:
    """Возвращает режим файла для вкладки избыточных категорий."""
    mode, _pairs, _dedupe = load_redundant_category_rules(file_path, family, lang)
    return mode


def load_precise_to_broad_list_map(file_path: str, family: str, lang: str) -> dict[str, set[str]]:
    """Совместимость: загружает только пары `точная<TAB>широкая`."""
    mode, precise_to_broad_map, _ = load_redundant_category_rules(file_path, family, lang)
    if mode != REDUNDANT_MODE_PAIRS:
        return {}
    return precise_to_broad_map


def get_page_categories_from_api(page, family: str, lang: str) -> set[str]:
    """Возвращает все категории страницы через API, включая шаблонные."""
    prefixes = category_prefix_aliases(family, lang)
    categories: set[str] = set()
    for category in page.categories():
        cat_name = category.title(with_ns=False)
        normalized = normalize_category_name(cat_name, prefixes)
        if normalized:
            categories.add(normalized)
    return categories


def _category_pattern(family: str, lang: str):
    prefixes = category_prefix_aliases(family, lang)
    escaped = [re.escape(prefix) for prefix in prefixes if prefix]
    if not escaped:
        escaped = ['Category']
    return re.compile(
        r'\[\[\s*(?:' + '|'.join(escaped) + r')\s*:\s*([^\]]+)\]\]',
        re.IGNORECASE,
    )


def _category_pattern_with_optional_newline(family: str, lang: str):
    prefixes = category_prefix_aliases(family, lang)
    escaped = [re.escape(prefix) for prefix in prefixes if prefix]
    if not escaped:
        escaped = ['Category']
    return re.compile(
        r'\n?\[\[\s*(?:' + '|'.join(escaped) + r')\s*:\s*([^\]]+)\]\]',
        re.IGNORECASE,
    )


def _inclusion_scopes(code):
    """Return inclusion-tag contents from inner to outer, then the page root."""
    scopes = []
    inclusion_tags = {'noinclude', 'includeonly', 'onlyinclude'}
    try:
        tags = list(code.filter_tags(recursive=True))
    except Exception:
        tags = []
    for tag in reversed(tags):
        try:
            tag_name = str(tag.tag or '').strip().casefold()
            if tag_name in inclusion_tags and tag.contents is not None:
                scopes.append(tag.contents)
        except Exception:
            continue
    scopes.append(code)
    return scopes


def _category_link_data(link, prefixes: tuple[str, ...]):
    """Return normalized name and original category payload for a category link."""
    try:
        title = str(link.title or '').strip()
    except Exception:
        return None
    if ':' not in title:
        return None
    raw_prefix, raw_name = title.split(':', 1)
    prefix_keys = {
        str(prefix or '').strip().rstrip(':').casefold()
        for prefix in prefixes
        if str(prefix or '').strip()
    }
    if raw_prefix.strip().casefold() not in prefix_keys:
        return None
    cat_name_part = raw_name.strip()
    normalized = normalize_category_name(cat_name_part, prefixes)
    if not normalized:
        return None
    sort_key = None
    try:
        if link.text is not None:
            sort_key = str(link.text)
    except Exception:
        sort_key = None
    full_category = (
        f'{cat_name_part}|{sort_key}' if sort_key is not None else cat_name_part
    )
    return normalized, cat_name_part, full_category


def _rewrite_scope_categories(scope, entries, broad_categories_to_remove, category_prefix):
    """Move direct categories to the bottom of this exact inclusion scope."""
    keep_payloads: list[str] = []
    seen: set[str] = set()
    for link, normalized, _original, payload in entries:
        try:
            scope.remove(link, recursive=False)
        except Exception:
            try:
                scope.nodes.remove(link)
            except Exception:
                continue
        if normalized in broad_categories_to_remove or normalized in seen:
            continue
        seen.add(normalized)
        keep_payloads.append(payload)

    if not entries:
        return

    base = re.sub(r'\n{3,}', '\n\n', str(scope)).rstrip()
    categories_block = '\n'.join(
        f'[[{category_prefix}:{payload}]]' for payload in keep_payloads
    )
    if base and categories_block:
        rebuilt = f'{base}\n\n{categories_block}'
    else:
        rebuilt = base or categories_block
    scope.nodes[:] = mwparserfromhell.parse(rebuilt).nodes


def extract_and_filter_categories(
    text: str,
    precise_to_broad_list_map: dict[str, set[str]],
    api_categories: set[str],
    family: str,
    lang: str,
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Удаляет широкие категории, если у страницы уже есть более точные."""
    prefixes = category_prefix_aliases(family, lang)
    code = mwparserfromhell.parse(text or '')
    original_name_parts: dict[str, str] = {}
    category_names_in_text: set[str] = set()
    scoped_entries = []
    for scope in _inclusion_scopes(code):
        entries = []
        try:
            links = list(scope.filter_wikilinks(recursive=False))
        except Exception:
            links = []
        for link in links:
            data = _category_link_data(link, prefixes)
            if data is None:
                continue
            normalized_name, cat_name_part, full_category = data
            entries.append((link, normalized_name, cat_name_part, full_category))
            category_names_in_text.add(normalized_name)
            original_name_parts.setdefault(normalized_name, cat_name_part)
        scoped_entries.append((scope, entries))

    broad_categories_to_remove: set[str] = set()
    precise_to_broad_found: dict[str, str] = {}
    for article_precise_norm in api_categories.intersection(precise_to_broad_list_map.keys()):
        for broad_norm in precise_to_broad_list_map[article_precise_norm]:
            if broad_norm in api_categories and broad_norm in category_names_in_text:
                broad_categories_to_remove.add(broad_norm)
                precise_to_broad_found[broad_norm] = article_precise_norm
                if article_precise_norm not in original_name_parts:
                    original_name_parts[article_precise_norm] = article_precise_norm

    if not broad_categories_to_remove:
        return text, {}, original_name_parts

    category_prefix = get_policy_prefix(family, lang, 14, 'Category:').rstrip(':')
    for scope, entries in scoped_entries:
        _rewrite_scope_categories(
            scope,
            entries,
            broad_categories_to_remove,
            category_prefix,
        )

    return str(code), precise_to_broad_found, original_name_parts


def deduplicate_target_categories_in_text(
    text: str,
    dedupe_categories: set[str],
    family: str,
    lang: str,
) -> tuple[str, dict[str, int], dict[str, str]]:
    """Удаляет дубликаты только для категорий из входного списка.

    Возвращает:
    - new_text: изменённый текст страницы
    - removed_counts: количество удалённых дублей по нормализованному имени
    - original_names: мапа нормализованное имя -> первое исходное имя в тексте
    """
    prefixes = category_prefix_aliases(family, lang)
    targets: set[str] = set()
    for value in dedupe_categories or set():
        normalized = normalize_category_name(value, prefixes)
        if normalized:
            targets.add(normalized)
    if not targets:
        return text, {}, {}

    code = mwparserfromhell.parse(text or '')
    removed_counts: dict[str, int] = {}
    original_name_parts: dict[str, str] = {}
    for scope in _inclusion_scopes(code):
        seen: set[str] = set()
        try:
            links = list(scope.filter_wikilinks(recursive=False))
        except Exception:
            links = []
        for link in links:
            data = _category_link_data(link, prefixes)
            if data is None:
                continue
            normalized_name, cat_name_part, _payload = data
            original_name_parts.setdefault(normalized_name, cat_name_part)
            if normalized_name not in targets:
                continue
            if normalized_name in seen:
                removed_counts[normalized_name] = removed_counts.get(normalized_name, 0) + 1
                try:
                    scope.remove(link, recursive=False)
                except Exception:
                    try:
                        scope.nodes.remove(link)
                    except Exception:
                        pass
            else:
                seen.add(normalized_name)

    new_text = str(code)
    if removed_counts:
        new_text = re.sub(r'\n{3,}', '\n\n', new_text)
    return new_text, removed_counts, original_name_parts


def build_comment(
    precise_to_broad_found: dict[str, str],
    original_names: dict[str, str],
    family: str,
    lang: str,
    single_template: str = '',
    multi_template: str = '',
    pair_template: str = '',
) -> str | None:
    """Формирует комментарий к правке."""
    single_template = (single_template or '').strip()
    multi_template = (multi_template or '').strip()
    pair_template = (pair_template or '').strip()

    if not precise_to_broad_found:
        return None

    category_prefix = get_policy_prefix(family, lang, 14, 'Category:')
    pair_values_list: list[_SafeFormatDict] = []

    for broad_norm in sorted(precise_to_broad_found.keys()):
        precise_norm = precise_to_broad_found[broad_norm]
        original_broad = original_names.get(broad_norm, broad_norm)
        original_precise = original_names.get(precise_norm, precise_norm)
        pair_values_list.append(_SafeFormatDict({
            'title_broad': original_broad,
            'title_precise': original_precise,
            'link_broad': f'[[{category_prefix}{original_broad}]]',
            'link_precise': f'[[{category_prefix}{original_precise}]]',
            'original_broad': original_broad,
            'original_precise': original_precise,
            'broad_name': original_broad,
            'precise_name': original_precise,
            'broad_link': f'[[{category_prefix}{original_broad}]]',
            'precise_link': f'[[{category_prefix}{original_precise}]]',
        }))

    if len(pair_values_list) == 1:
        if single_template:
            return single_template.format_map(pair_values_list[0])
        if pair_template:
            return pair_template.format_map(pair_values_list[0])
        return None

    pair_parts: list[str] = []
    for values in pair_values_list:
        if pair_template:
            pair_parts.append(pair_template.format_map(values))
        elif single_template:
            pair_parts.append(single_template.format_map(values))

    pair_joined = ', '.join(part for part in pair_parts if part).strip()
    if multi_template:
        return multi_template.format_map(_SafeFormatDict({
            'pair': pair_joined,
            'repeat': pair_joined,
            'count': str(len(pair_values_list)),
        }))
    return pair_joined or None


def build_dedupe_comment(lang: str) -> str:
    """Комментарий к правке для режима удаления дублей категорий."""
    return default_redundant_category_dedupe_summary(lang or 'ru')


def analyze_page_text(
    page,
    precise_to_broad_map: dict[str, set[str]],
    family: str,
    lang: str,
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Полный анализ текста страницы для worker'а."""
    api_categories = get_page_categories_from_api(page, family, lang)
    return extract_and_filter_categories(
        page.text,
        precise_to_broad_map,
        api_categories,
        family,
        lang,
    )
