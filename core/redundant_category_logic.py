# -*- coding: utf-8 -*-
"""
Общая логика удаления избыточных категорий.
"""

import csv
import re

from ..constants import DEFAULT_EN_NS, EN_PREFIX_ALIASES
from .localization import translate_key, translate_runtime
from .namespace_manager import _load_ns_info, get_policy_prefix


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
    value = re.sub(r'\s+', ' ', (name or '').strip())
    lower = value.casefold()
    for prefix in prefixes:
        prefix_text = (prefix or '').strip().rstrip(':')
        if not prefix_text:
            continue
        normalized_prefix = prefix_text.casefold() + ':'
        if lower.startswith(normalized_prefix):
            value = value[len(prefix_text) + 1:].strip()
            break
    return re.sub(r'\s+', ' ', value).strip()


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


def extract_and_filter_categories(
    text: str,
    precise_to_broad_list_map: dict[str, set[str]],
    api_categories: set[str],
    family: str,
    lang: str,
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Удаляет широкие категории, если у страницы уже есть более точные."""
    prefixes = category_prefix_aliases(family, lang)
    category_pattern = _category_pattern(family, lang)
    original_categories_full = category_pattern.findall(text or '')

    original_name_parts: dict[str, str] = {}
    category_names_in_text: set[str] = set()

    for full_category in original_categories_full:
        parts = full_category.split('|', 1)
        cat_name_part = parts[0].strip()
        normalized_name = normalize_category_name(cat_name_part, prefixes)
        if not normalized_name:
            continue
        category_names_in_text.add(normalized_name)
        if normalized_name not in original_name_parts:
            original_name_parts[normalized_name] = cat_name_part

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

    categories_to_keep_full: list[str] = []
    processed_normalized_names: set[str] = set()
    for full_cat in original_categories_full:
        cat_name_part = full_cat.split('|', 1)[0].strip()
        normalized_name = normalize_category_name(cat_name_part, prefixes)
        if normalized_name not in broad_categories_to_remove and normalized_name not in processed_normalized_names:
            categories_to_keep_full.append(full_cat)
            processed_normalized_names.add(normalized_name)
        elif normalized_name in broad_categories_to_remove:
            processed_normalized_names.add(normalized_name)

    new_text = category_pattern.sub('', text or '').strip()
    new_text = re.sub(r'\n{3,}', '\n\n', new_text)

    if categories_to_keep_full:
        category_prefix = get_policy_prefix(family, lang, 14, 'Category:').rstrip(':')
        categories_block = '\n'.join(
            f'[[{category_prefix}:{category}]]'
            for category in categories_to_keep_full
        )
        new_text = f'{new_text}\n\n{categories_block}'.strip() if new_text else categories_block

    return new_text, precise_to_broad_found, original_name_parts


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

    category_pattern = _category_pattern_with_optional_newline(family, lang)
    seen: set[str] = set()
    removed_counts: dict[str, int] = {}
    original_name_parts: dict[str, str] = {}

    def _replace_match(match):
        full_match = match.group(0)
        full_category = match.group(1)
        cat_name_part = full_category.split('|', 1)[0].strip()
        normalized_name = normalize_category_name(cat_name_part, prefixes)
        if not normalized_name:
            return full_match

        if normalized_name not in original_name_parts:
            original_name_parts[normalized_name] = cat_name_part

        if normalized_name not in targets:
            return full_match

        if normalized_name in seen:
            removed_counts[normalized_name] = removed_counts.get(normalized_name, 0) + 1
            return ''

        seen.add(normalized_name)
        return full_match

    new_text = category_pattern.sub(_replace_match, text or '')
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
    return translate_key(
        'ui.redundant_category_dedupe_summary',
        lang or 'ru',
        'Removing duplicate categories',
    )


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
