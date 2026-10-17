from __future__ import annotations

# ruff: noqa: E402
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from wiki_cat_tool.core.category_text_translation import CategoryTextTranslator
from wiki_cat_tool.core.category_transfer import CategoryTransferReader, TransferStopped, parse_scan_count, scan_count_value
from wiki_cat_tool.tests.test_category_transfer import FixtureReader, page
from wiki_cat_tool.workers.category_transfer_worker import CategoryTextReadWorker


class TranslationReader(FixtureReader):
    def __init__(self):
        super().__init__()
        self.entities = {}
        self.aliases = {}
        self.requests = []

    def site_info(self, lang):
        info = super().site_info(lang)
        info['namespaces']['10']['name'] = 'Шаблон' if lang == 'ru' else 'Template'
        info['namespacealiases'] = [{'id': 14, 'alias': 'Cat'}]
        return info

    def pages(self, lang, titles, **kwargs):
        self.page_calls.append((lang, list(titles)))
        return {title: self.page_data[lang].get(self.aliases.get((lang, title), title), page(title, missing=True))
                for title in titles}

    def request(self, lang, params, **kwargs):
        self.check_stop()
        self.requests.append((lang, dict(params)))
        if params.get('siprop') == 'magicwords':
            return {'query': {'magicwords': [{'name': name, 'aliases': aliases} for name, aliases in (
                ('pagename', ['PAGENAME']), ('defaultsort', ['DEFAULTSORT:']), ('subst', ['subst:']))]}}
        if params.get('action') == 'wbgetentities':
            return {'entities': {title: self.entities[title] for title in params['titles'].split('|')
                                 if title in self.entities}}
        raise AssertionError(params)

    def add_pair(self, source, target, namespace):
        self.page_data['en'][source] = page(source, ns=namespace)
        self.page_data['ru'][target] = page(target, ns=namespace)
        self.entities[source] = {'sitelinks': {'enwiki': {'title': source}, 'ruwiki': {'title': target}}}


class CategoryTextTranslationTests(unittest.TestCase):
    def setUp(self):
        self.reader = TranslationReader()
        self.addCleanup(self.reader.close)
        self.reader.add_pair('Template:Known', 'Шаблон:Известный', 10)
        self.reader.add_pair('Category:Parent', 'Категория:Родитель', 14)
        self.translator = CategoryTextTranslator(self.reader)

    def test_replaces_nested_names_and_preserves_parameters_spacing_and_sort_keys(self):
        source = 'before\n{{ Known |year=1170|x={{Known|original}}}}\n[[Cat:Parent| unchanged ]]\nafter'
        expected = 'before\n{{ Известный |year=1170|x={{Известный|original}}}}\n[[Категория:Родитель| unchanged ]]\nafter'
        self.assertEqual([expected], self.translator.translate([source]))

    def test_missing_links_and_pages_stay_exactly_as_written(self):
        self.reader.page_data['en']['Template:Unlinked'] = page('Template:Unlinked', ns=10)
        text = '{{Missing}} {{Unlinked|x=raw}} [[Category:Missing|x]]'
        self.assertEqual([text], self.translator.translate([text]))

    def test_comments_nowiki_parser_functions_magic_words_and_article_links_are_preserved(self):
        text = '<!-- {{Known}} --> <nowiki>{{Known}} [[Category:Parent]]</nowiki> ' \
               '{{#if:1|{{Known}}}} {{PAGENAME}} {{DEFAULTSORT:Known}} [[Known|Category:Parent]]'
        self.assertEqual([text.replace('{{#if:1|{{Known}}}}', '{{#if:1|{{Известный}}}}')],
                         self.translator.translate([text]))
        queried = [title for lang, titles in self.reader.page_calls if lang == 'en' for title in titles]
        self.assertEqual(['Template:Known'], queried)

    def test_explicit_namespace_escaped_category_and_substitution_prefix_are_kept(self):
        self.assertEqual(['{{Шаблон:Известный}} {{subst:Известный}} [[:Категория:Родитель|label]] [[Шаблон:Известный]]'],
                         self.translator.translate(['{{Template:Known}} {{subst:Known}} [[:Category:Parent|label]] [[Template:Known]]']))

    def test_dynamic_template_names_are_not_guessed_but_nested_templates_can_be_replaced(self):
        self.assertEqual(['{{{{{name}}}|x={{Известный}}}}'],
                         self.translator.translate(['{{{{{name}}}|x={{Known}}}}']))

    def test_redirects_resolve_to_the_existing_template_and_target_namespace_is_checked(self):
        self.reader.aliases['en', 'Template:Alias'] = 'Template:Known'
        self.assertEqual(['{{Известный}}'], self.translator.translate(['{{Alias}}']))
        self.reader.add_pair('Template:Wrong', 'Mainspace page', 10)
        self.reader.page_data['ru']['Mainspace page']['ns'] = 0
        self.assertEqual(['{{Wrong}}'], self.translator.translate(['{{Wrong}}']))

    def test_repeated_and_missing_names_are_cached_and_only_two_wikis_are_requested(self):
        text = '{{Known}} {{Missing}} [[Category:Parent]]'
        self.translator.translate([text, text])
        calls = len(self.reader.page_calls), len(self.reader.requests)
        self.translator.translate([text])
        self.assertEqual(calls, (len(self.reader.page_calls), len(self.reader.requests)))
        wikidata = [params for _, params in self.reader.requests if params.get('action') == 'wbgetentities']
        self.assertEqual(1, len(wikidata))
        self.assertEqual('enwiki|ruwiki', wikidata[0]['sitefilter'])

    def test_plain_text_and_same_language_do_not_make_translation_requests(self):
        self.assertEqual(['plain text'], self.translator.translate(['plain text']))
        self.reader.target_lang = 'en'
        self.assertEqual(['{{Known}}'], self.translator.translate(['{{Known}}']))
        self.assertEqual([], self.reader.requests)

    def test_stop_is_propagated(self):
        self.reader.stop_event.set()
        with self.assertRaises(TransferStopped):
            self.translator.translate(['{{Known}}'])

    def test_reader_follows_normalization_and_redirect_chains_only_when_requested(self):
        import threading
        reader = CategoryTransferReader('en', 'ru', 'wikipedia', threading.Event())
        self.addCleanup(reader.close)
        packet = {'query': {'normalized': [{'from': 'Template:foo_bar', 'to': 'Template:Foo bar'}],
                            'redirects': [{'from': 'Template:Foo bar', 'to': 'Template:Final'}],
                            'pages': [page('Template:Final', ns=10)]}}
        with patch.object(reader, 'request', return_value=packet) as request:
            pages = reader.pages('en', ['Template:foo_bar'], props='info', resolve_redirects=True)
        self.assertEqual('Template:Final', pages['Template:foo_bar']['title'])
        self.assertEqual(1, request.call_args.args[1]['redirects'])

    def test_worker_loads_translated_text_and_falls_back_to_source_on_lookup_error(self):
        for failed in (False, True):
            with self.subTest(lookup_failed=failed):
                worker = CategoryTextReadWorker(rows=((7, 'Source'),), source_lang='en', target_lang='ru',
                                                family='wikipedia', translate_names=True)
                worker.reader.close()
                worker.reader = self.reader
                self.reader.page_data['en']['Category:Source'] = page('Category:Source', revisions=[
                    {'slots': {'main': {'content': '{{Known|original}}'}}}])
                payloads = []
                worker.content_ready.connect(payloads.append)
                if failed:
                    with patch.object(CategoryTextTranslator, 'translate', side_effect=RuntimeError('Unavailable')):
                        worker.run()
                else:
                    worker.run()
                expected = '{{Known|original}}' if failed else '{{Известный|original}}'
                self.assertEqual([dict(index=7, content=expected)], payloads)
                self.assertEqual({'loaded': 1, 'failed': 0}, worker.stats)


class SubcategoryScanTests(unittest.TestCase):
    def setUp(self):
        self.reader = FixtureReader()
        self.addCleanup(self.reader.close)
        self.row = dict(source_title='Category:Root', depth=0, status='loaded')
        self.reader.page_data['en'].update({
            'Category:Root': page('Category:Root', qid='Q1'),
            'Article A': page('Article A', ns=0, qid='Q2'),
            'Article B': page('Article B', ns=0, qid='Q3'),
            'Category:Child A': page('Category:Child A', qid='Q4'),
            'Category:Child B': page('Category:Child B', qid='Q5'),
            'Category:Grandchild': page('Category:Grandchild', qid='Q6'),
        })
        self.reader.page_data['ru'].update({
            'Категория:Корень': page('Категория:Корень'),
            'Статья А': page('Статья А', ns=0),
            'Категория:Дочерняя': page('Категория:Дочерняя'),
            'Wrong namespace': page('Wrong namespace', ns=0),
            'Категория:Глубже': page('Категория:Глубже'),
        })
        self.reader.links.update(Q1='Категория:Корень', Q2='Статья А', Q3='Missing article',
                                 Q4='Категория:Дочерняя', Q5='Wrong namespace', Q6='Категория:Глубже')
        from unittest.mock import Mock
        def members(title):
            names = ['Article A', 'Article B']
            if self.reader.include_subcategories:
                names += ['Category:Child A', 'Category:Child B']
            return {name: self.reader.page_data['en'][name] for name in names}
        self.reader.read_article_pages = Mock(side_effect=members)

    def test_counts_articles_and_immediate_categories_separately_without_traversing_children(self):
        self.reader.include_subcategories = True
        result = self.reader.scan(self.row)
        self.assertEqual((2, 2, 1, 1), (result['total'], result['total_categories'], result['potential'], result['potential_categories']))
        self.assertEqual(['Статья А'], result['target_articles'])
        self.assertEqual(['Категория:Дочерняя'], result['target_categories'])
        self.reader.read_article_pages.assert_called_once_with('Category:Root')
        self.assertNotIn('Категория:Глубже', result['target_categories'])
        self.assertEqual('2+2', scan_count_value(result, 'total'))
        self.assertEqual('1+1', scan_count_value(result, 'potential'))

    def test_default_mode_keeps_article_only_results(self):
        result = self.reader.scan(self.row)
        self.assertEqual((2, 1), (result['total'], result['potential']))
        self.assertNotIn('total_categories', result)
        self.assertNotIn('target_categories', result)

    def test_generator_requests_only_direct_mainspace_and_category_members(self):
        import threading
        reader = CategoryTransferReader('en', 'ru', 'wikipedia', threading.Event())
        self.addCleanup(reader.close)
        reader.include_subcategories = True
        packet = {'query': {'pages': [page('Article', ns=0, qid='Q2'), page('Category:Child', qid='Q4')]}}
        with patch.object(reader, 'request', return_value=packet) as request:
            result = reader.read_article_pages('Category:Root')
        params = request.call_args.args[1]
        self.assertEqual(('page|subcat', '0|14'), (params['gcmtype'], params['gcmnamespace']))
        self.assertEqual({'Article', 'Category:Child'}, set(result))
        request.assert_called_once()

    def test_combined_counts_are_strict_and_zero_categories_are_preserved(self):
        self.assertEqual((4, 2), parse_scan_count('4+2'))
        self.assertEqual((4, 0), parse_scan_count('4+0'))
        self.assertEqual((4, None), parse_scan_count('4'))
        self.assertEqual((None, None), parse_scan_count(''))
        for text in ('-1', '1+-2', '1+2+3', '1+bad'):
            with self.assertRaises(ValueError):
                parse_scan_count(text)

    def test_detail_membership_includes_categories_on_both_wikis(self):
        from unittest.mock import Mock
        from wiki_cat_tool.workers.category_transfer_worker import CategoryArticlePresenceWorker
        worker = CategoryArticlePresenceWorker(articles=None, category='Категория:Корень', source_category='Category:Root',
                                               source_lang='en', target_lang='ru', family='wikipedia', include_subcategories=True)
        worker.reader.close()
        worker.reader = self.reader
        def members(category, kind, **kwargs):
            if kwargs.get('lang') == 'ru':
                return ['Категория:Дочерняя'] if kind == 'subcat' else []
            return ['Category:Child A'] if kind == 'subcat' else ['Article A']
        self.reader.members = Mock(side_effect=members)
        results = []
        worker.result_ready.connect(results.append)
        worker.run()
        self.assertEqual([[('Статья А', False), ('Категория:Дочерняя', True)]], results)
        self.assertFalse(worker.failed)


if __name__ == '__main__':
    unittest.main()
