"""Parse replacement rules and apply them to explicit category links in place."""

import csv
from dataclasses import dataclass
import re
from unicodedata import normalize

import mwparserfromhell
from mwparserfromhell.nodes import Text, Wikilink
from pywikibot.tools import first_upper

from .localization import translate_runtime
from .redundant_category_logic import _inclusion_scopes, normalize_category_name


@dataclass(frozen=True)
class CategoryTarget:
    name: str
    sort_key: str | None = None

    def display(self):
        return self.name if self.sort_key is None else f'{self.name}|{self.sort_key}'


def read_category_first_column(path):
    with open(path, encoding='utf-8-sig', newline='') as stream:
        return list(dict.fromkeys(row[0].strip() for row in csv.reader(stream, delimiter='\t')
                                  if row and row[0].strip()))


def _cell_payload(cell):
    wrapped = cell.strip()
    if wrapped.startswith('[[') and wrapped.endswith(']]'):
        return wrapped[2:-2].removeprefix(':')
    return cell


def _parse_cell(cell):
    name, separator, key = _cell_payload(cell).partition('|')
    name = name.strip()
    if (not name or name.startswith(':') or not name.replace('_', '').strip()
            or re.search(r'[\[\]{}<>#\x00-\x1f\x7f]', name)):
        raise ValueError
    if separator:
        # A key may contain wikitext, but must stay inside the category link.
        probe = f'[[Category:{name}|{key}]]'
        nodes = mwparserfromhell.parse(probe).nodes
        if (len(nodes) != 1 or not isinstance(nodes[0], Wikilink)
                or str(nodes[0].text) != key):
            raise ValueError
    return CategoryTarget(name, key if separator else None)


def read_replacement_map(path):
    rows = []
    with open(path, encoding='utf-8-sig', newline='') as stream:
        reader = csv.reader(stream, delimiter='\t', strict=True)
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            try:
                old = _parse_cell(row[0])
                targets = tuple(_parse_cell(cell) for cell in row[1:] if cell.strip())
                if old.sort_key is not None or not targets:
                    raise ValueError
            except ValueError:
                raise ValueError(translate_runtime('ui.cat_replace.invalid_row').format(
                    line=reader.line_num)) from None
            rows.append((old.name, targets))
    if not rows:
        raise ValueError(translate_runtime('ui.cat_replace.empty_map'))
    return tuple(rows)


class CategoryReplacements:
    def __init__(self, rows, *, prefix, aliases=(), first_letter=True, inherit_sort_key=False):
        self.prefix = prefix
        self.aliases = (prefix, 'Category', *aliases)
        self.alias_keys = {value.replace('_', ' ').casefold() for value in self.aliases}
        self.first_letter = first_letter
        self.inherit_sort_key = inherit_sort_key
        rules = {}
        for old, targets in rows:
            source = self._name(old, self.aliases)
            if not source:
                raise ValueError(old)
            destinations = rules.setdefault(source, {})
            for target in targets:
                name = self._name(target.name, self.aliases)
                if not name:
                    raise ValueError(target.name)
                if name not in destinations or (
                    destinations[name] is None and target.sort_key is not None
                ):
                    destinations[name] = target.sort_key
        self.rules = rules

    def _name(self, name, prefixes=()):
        value = normalize('NFC', normalize_category_name(name, prefixes))
        return first_upper(value) if self.first_letter else value

    def _link_name(self, node):
        if not isinstance(node, Wikilink):
            return ''
        prefix, sep, name = str(node.title).partition(':')
        if not sep or prefix.strip().replace('_', ' ').casefold() not in self.alias_keys:
            return ''
        return self._name(name)

    @staticmethod
    def _remove_line_end(scope, index):
        """Remove the now empty line only if the category occupied it by itself."""
        before = scope.nodes[index - 1] if index else None
        after = scope.nodes[index + 1] if index + 1 < len(scope.nodes) else None
        if index and (not isinstance(before, Text) or not re.search(r'\n[ \t]*$', str(before))):
            return
        if after is None:
            if isinstance(before, Text):
                before.value = re.sub(r'[ \t]*$', '', str(before))
            return
        if isinstance(after, Text) and re.match(r'[ \t]*\r?\n', str(after)):
            if isinstance(before, Text):
                before.value = str(before).rstrip(' \t')
            after.value = re.sub(r'^[ \t]*\r?\n', '', str(after), count=1)

    def apply(self, text):
        code = mwparserfromhell.parse(text)
        changes = {}
        eol = '\r\n' if '\r\n' in text else '\n'
        for scope in _inclusion_scopes(code):
            entries = [(i, node, self._link_name(node)) for i, node in enumerate(scope.nodes)
                       if isinstance(node, Wikilink)]
            # Rules apply together to the original text. A target that's itself
            # being removed may need to be re-added by a different rule.
            kept = {name for _, _, name in entries if name and name not in self.rules}
            explicit_keys = {}
            for _, _, old in entries:
                for name, key in self.rules.get(old, {}).items():
                    if key is not None:
                        explicit_keys.setdefault(name, key)
            moves = []
            for index, node, old in entries:
                if old not in self.rules:
                    continue
                targets = self.rules[old]
                output = []
                source_key = str(node.text) if node.text is not None else None
                for name in targets:
                    if name in kept:
                        continue
                    key = explicit_keys.get(name, source_key if self.inherit_sort_key else None)
                    kept.add(name)
                    if name == old and key == source_key:
                        output.append(str(node))
                    else:
                        output.append(f'[[{self.prefix}:{CategoryTarget(name, key).display()}]]')
                replacement = eol.join(output)
                if replacement != str(node):
                    changes[old] = tuple(targets)
                    moves.append((index, replacement))
            for index, replacement in reversed(moves):
                if not replacement:
                    self._remove_line_end(scope, index)
                scope.nodes[index:index + 1] = mwparserfromhell.parse(replacement).nodes
        return str(code), changes

    def summary(self, changes, reason='', *, limit=500):
        reason = reason.strip()
        suffix = f' — {reason}' if reason else ''
        link_prefix = 'К' if 'к' in self.alias_keys else self.prefix

        def link(name):
            return f'[[{link_prefix}:{name}]]'

        pieces = [f'{link(old)} → ' + ', '.join(map(link, targets)) for old, targets in changes.items()]
        details = ', '.join(pieces)
        if len(details) + len(suffix) > limit:
            # Stop between links so MediaWiki's summary limit does not break
            # a wikilink or cut off the reason.
            budget = limit - len(suffix) - 1
            matches = list(re.finditer(r'\]\](?=,|$)', details[:max(0, budget)]))
            details = (details[:matches[-1].end()] if matches else '') + '…'
        return details + suffix
