"""Offline coverage of TSV parsing, category changes and first-column reads."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtWidgets import QApplication, QWidget
from pywikibot.site import Namespace

from wiki_cat_tool.core.category_replacement import (
    CategoryReplacements, CategoryTarget, read_category_first_column, read_replacement_map,
)
from wiki_cat_tool.core.category_transfer import TransferStopped
from wiki_cat_tool.gui.tabs.category_replacement_tab import CategoryReplacementTab
from wiki_cat_tool.gui.tabs.redundant_categories_tab import RedundantCategoriesTab
from wiki_cat_tool.workers.category_fetch_worker import CategoryMapFetchWorker
from wiki_cat_tool.workers.category_replacement_worker import CategoryReplacementWorker


def replacements(rows, **kwargs):
    return CategoryReplacements(rows, prefix='Категория', aliases=('К',), **kwargs)


class CategoryReplacementTests(unittest.TestCase):
    def test_tsv_and_sort_key_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rules.tsv'
            path.write_text('Category:some_old\tFirst\tК:Second|Custom\tThird|\tFourth|{{Sort|X}}\n',
                            encoding='utf-8-sig')
            rules = read_replacement_map(path)
            self.assertEqual(['Category:some_old'], read_category_first_column(path))
        text = 'Intro\n[[К:Some old|Inherited]]\n[[Category:Unaffected]]\n'
        for inherit in (False, True):
            with self.subTest(inherit=inherit):
                operation = replacements(rules, inherit_sort_key=inherit)
                changed, changes = operation.apply(text)
                first = 'First|Inherited' if inherit else 'First'
                self.assertEqual(
                    f'Intro\n[[Категория:{first}]]\n[[Категория:Second|Custom]]\n'
                    '[[Категория:Third|]]\n[[Категория:Fourth|{{Sort|X}}]]\n[[Category:Unaffected]]\n',
                    changed,
                )
                self.assertEqual(
                    '[[К:Some old]] → [[К:First]], [[К:Second]], [[К:Third]], [[К:Fourth]] — [[ВП:ОК|итог]]',
                    operation.summary(changes, '[[ВП:ОК|итог]]'),
                )

    def test_invalid_rules_report_the_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'rules.tsv'
            for invalid in ('Only source', 'A|key\tB', 'A\tB|x]][[Category:Surprise'):
                with self.subTest(invalid=invalid):
                    path.write_text(f'A\tB\n{invalid}\n', encoding='utf-8')
                    with self.assertRaisesRegex(ValueError, '2'):
                        read_replacement_map(path)

    def test_existing_categories_and_protected_text_stay_in_their_places(self):
        operation = replacements([('A', (CategoryTarget('B', 'Ignore'),))])
        text = ('<!-- [[Category:A]] -->\n\n\n<nowiki>[[Category:A]]</nowiki>\n'
                '[[:Category:A]]\n[[Category:A|X]]\n[[Category:B|Keep]]\n'
                '<noinclude>[[Category:A|Doc]]</noinclude>\n')
        changed, changes = operation.apply(text)
        self.assertEqual(text.replace('[[Category:A|X]]\n', '').replace(
            '[[Category:A|Doc]]', '[[Категория:B|Ignore]]'), changed)
        self.assertEqual({'A': ('B',)}, changes)
        untouched = '[[Category:Other]]\n\n\n'
        self.assertEqual((untouched, {}), operation.apply(untouched))

    def test_overlapping_rules_and_duplicate_targets(self):
        operation = replacements([
            ('A', (CategoryTarget('B'), CategoryTarget('C'), CategoryTarget('C'))),
            ('B', (CategoryTarget('C', 'Explicit'), CategoryTarget('D'))),
        ], inherit_sort_key=True)
        text = '[[Category:A|A key]]\n[[Category:B|B key]]\n'
        self.assertEqual('[[Категория:B|A key]]\n[[Категория:C|Explicit]]\n[[Категория:D|B key]]\n',
                         operation.apply(text)[0])

    def test_long_summary_keeps_complete_links_and_reason(self):
        operation = replacements([])
        summary = operation.summary({'Old': ['New'] + ['Long name' * 40] * 3}, '[[ВП:ОК|итог]]', limit=100)
        self.assertEqual('[[К:Old]] → [[К:New]]… — [[ВП:ОК|итог]]', summary)

    def test_worker_saves_changed_pages_once(self):
        worker = CategoryReplacementWorker(
            titles=['First', 'First', 'Second'], rules=[('Old', (CategoryTarget('New', 'key'),))],
            username='user', password='password', lang='ru', family='wikipedia', ns_selection='auto',
            reason='reason', inherit_sort_key=False,
        )
        module = 'wiki_cat_tool.workers.category_replacement_worker'
        site = Mock()
        site.namespaces = {14: Namespace(14, 'Category', case='first-letter')}
        with patch(f'{module}.pywikibot.Site', return_value=site), \
             patch(f'{module}.normalize_title_by_selection', side_effect=lambda title, *_: title), \
             patch(f'{module}.pywikibot.Page') as page_cls:
            first = Mock(text='[[Category:Old|previous]]', content_model='wikitext')
            first.title.return_value = 'First'
            second = Mock(text='[[Category:Other]]', content_model='wikitext')
            second.title.return_value = 'Second'
            page_cls.side_effect = lambda _, title: first if title == 'First' else second
            worker.run()
        self.assertFalse(worker.failed, worker.failure_message)
        self.assertEqual(1, worker.saved_edits)
        self.assertEqual('[[Category:New|key]]', first.text)
        first.save.assert_called_once_with(
            summary='[[Category:Old]] → [[Category:New]] — reason', minor=False, quiet=True)
        second.save.assert_not_called()


class CategoryMapReadTests(unittest.TestCase):
    def test_map_read_follows_continuation_and_keeps_first_occurrence(self):
        worker = CategoryMapFetchWorker(['First', 'Second'], lang='ru', family='wikipedia')
        seen = []
        requests = []

        def response(_lang, params):
            requests.append(dict(params))
            members = lambda *titles: {'query': {'categorymembers': [{'title': title} for title in titles]}}
            if params['cmtitle'] == 'Category:Second':
                return members('Page 2', 'Category:Sub')
            if 'cmcontinue' in params:
                return members('Page 2')
            return {**members('Page 1'), 'continue': {'cmcontinue': 'next', 'continue': '-||'}}

        with patch('wiki_cat_tool.workers.category_fetch_worker.CategoryTransferReader') as cls:
            reader = cls.return_value
            reader.category_title.side_effect = lambda category, _lang: 'Category:' + category
            reader.request.side_effect = response
            worker.result_ready.connect(seen.append)
            worker.run()
        self.assertEqual([['Page 1', 'Page 2', 'Category:Sub']], seen)
        self.assertEqual('next', requests[1]['cmcontinue'])
        self.assertNotIn('cmcontinue', requests[2])
        reader.close.assert_called_once()

    def test_cancelled_map_read_does_not_publish_partial_titles(self):
        worker = CategoryMapFetchWorker(['First', 'Second'], lang='ru', family='wikipedia')
        received = Mock()
        worker.result_ready.connect(received)
        with patch('wiki_cat_tool.workers.category_fetch_worker.CategoryTransferReader') as cls:
            reader = cls.return_value
            reader.category_title.return_value = 'Category:First'
            reader.request.side_effect = TransferStopped()
            worker.run()
        received.assert_not_called()
        reader.close.assert_called_once()


class CategoryReplacementUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_default_sort_key_setting_and_logs(self):
        parent = QWidget()
        parent._ui_lang = 'en'
        for tab in (CategoryReplacementTab(parent), RedundantCategoriesTab(parent)):
            self.assertTrue(tab.log_panel.isHidden())
            self.assertEqual('Source category members', tab.fetch_map_btn.text())
            tab.log_panel.toggle.click()
            self.assertFalse(tab.log_panel.isHidden())
            tab.log_panel.toggle.click()
            self.assertTrue(tab.log_panel.isHidden())
            if isinstance(tab, CategoryReplacementTab):
                self.assertFalse(tab.sort_key_check.isChecked())
                self.assertFalse(tab.start_btn.isEnabled())
            tab.deleteLater()
