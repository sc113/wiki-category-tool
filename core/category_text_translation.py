"""Substitute linked template/category names without translating wikitext values."""

import re

import mwparserfromhell


class CategoryTextTranslator:
    """Resolve names in batches; cache both matches and missing sitelinks per run."""

    def __init__(self, reader):
        self.reader = reader
        self._names = {}
        self._magic = None

    @staticmethod
    def _namespaces(info):
        aliases = {}
        for key, namespace in info['namespaces'].items():
            for name in (namespace.get('name', namespace.get('*', '')), namespace.get('canonical', '')):
                if name:
                    aliases[name.casefold()] = int(key)
        for alias in info.get('namespacealiases', []):
            aliases[alias.get('alias', alias.get('*', '')).casefold()] = alias['id']
        return aliases

    def _magic_words(self):
        if self._magic is None:
            data = self.reader.request(self.reader.source_lang, {
                'action': 'query', 'meta': 'siteinfo', 'siprop': 'magicwords'})
            self._magic = {}
            for word in data.get('query', {}).get('magicwords', []):
                for alias in word.get('aliases', []):
                    self._magic[alias.rstrip(':').casefold()] = word['name']
        return self._magic

    def _reference(self, node, attribute, default_namespace, info, aliases, magic):
        raw = str(getattr(node, attribute))
        match = re.fullmatch(r'(\s*)(.*?)(\s*)', raw, re.S)
        leading, name, trailing = match.groups()
        modifier = ''
        if attribute == 'name':
            head, colon, rest = name.partition(':')
            if magic.get(head.casefold()) in {'subst', 'safesubst'} and colon:
                modifier, name = head + colon, rest
            elif head.startswith('#') or head.casefold() in magic:
                return None
        escaped = name.startswith(':')
        if escaped:
            name = name[1:]
            default_namespace = 0
        head, colon, rest = name.partition(':')
        explicit_namespace = bool(colon and head.replace('_', ' ').casefold() in aliases)
        namespace = aliases[head.replace('_', ' ').casefold()] if explicit_namespace else default_namespace
        if namespace not in {10, 14}:
            return None
        title = (rest if explicit_namespace else name).strip().replace('_', ' ')
        if not title or any(char in title for char in '[]{}|<>#\t\r\n'):
            return None
        ns_info = info['namespaces'][str(namespace)]
        if ns_info.get('case', 'first-letter') == 'first-letter':
            title = title[0].upper() + title[1:]
        prefix = ns_info.get('name', ns_info.get('*')) or ns_info['canonical']
        source = f'{prefix}:{title}'
        bare = attribute == 'name' and not explicit_namespace and not escaped
        return node, attribute, source, namespace, leading + modifier + (':' if escaped else ''), trailing, bare

    def _resolve(self, references):
        needed = {ref[2]: ref[3] for ref in references if ref[2] not in self._names}
        if not needed:
            return
        reader = self.reader
        sources = reader.pages(reader.source_lang, list(needed), props='info', resolve_redirects=True)
        canonical = {page['title']: needed[title] for title, page in sources.items()
                     if title in needed and reader.valid_page(page, needed[title])}
        source_wiki = reader.site_info(reader.source_lang)['general']['wikiid']
        target_wiki = reader.site_info(reader.target_lang)['general']['wikiid']
        params = {'action': 'wbgetentities', 'sites': source_wiki, 'props': 'sitelinks',
                  'sitefilter': f'{source_wiki}|{target_wiki}'}
        matches = {}
        for batch in reader._title_batches(reader.source_lang, list(canonical), params):
            data = reader.request('', {**params, 'titles': '|'.join(batch)}, wikidata=True)
            for entity in data.get('entities', {}).values():
                links = entity.get('sitelinks', {})
                source = links.get(source_wiki, {}).get('title', '')
                target = links.get(target_wiki, {}).get('title', '')
                if source in canonical and target:
                    matches[source] = target
        targets = reader.pages(reader.target_lang, list(dict.fromkeys(matches.values())),
                               props='info', resolve_redirects=True) if matches else {}
        for title, namespace in needed.items():
            source = sources.get(title, {}).get('title', '')
            target = targets.get(matches.get(source, ''))
            self._names[title] = target['title'] if reader.valid_page(target, namespace) else None

    def translate(self, texts):
        if self.reader.source_lang == self.reader.target_lang:
            return list(texts)
        codes = [mwparserfromhell.parse(text) for text in texts]
        if not any(code.filter_templates() or code.filter_wikilinks() for code in codes):
            return list(texts)
        info = self.reader.site_info(self.reader.source_lang)
        aliases = self._namespaces(info)
        magic = self._magic_words() if any(code.filter_templates() for code in codes) else {}
        references = []
        for code in codes:
            self.reader.check_stop()
            for nodes, attribute, namespace in ((code.filter_templates(), 'name', 10),
                                                (code.filter_wikilinks(), 'title', 0)):
                for node in nodes:
                    reference = self._reference(node, attribute, namespace, info, aliases, magic)
                    if reference:
                        references.append(reference)
        self._resolve(references)
        for node, attribute, source, _namespace, leading, trailing, bare in references:
            target = self._names[source]
            if target:
                value = target.partition(':')[2] if bare else target
                setattr(node, attribute, leading + value + trailing)
        return [str(code) for code in codes]
