from __future__ import annotations

# ruff: noqa: E402
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTabWidget, QWidget
from wiki_cat_tool.core.api_client import APIRequestError
from wiki_cat_tool.gui.tabs.category_content_sync_tab import CategoryContentSyncTab
from wiki_cat_tool.gui.tabs.category_transfer_tab import CategoryTransferTab
from wiki_cat_tool.gui.widgets.category_articles import CategoryArticlesDialog
from wiki_cat_tool.tests.test_category_transfer import FixtureReader, page
from wiki_cat_tool.workers.category_content_sync_worker import CategoryContentSyncPreviewWorker, CategoryContentSyncWorker
from wiki_cat_tool.workers.category_fetch_worker import CategoryFetchWorker


def scan_row(name='A', target='А'):
    return dict(source_title='Category:' + name, target_title='Категория:' + target, qid='Q1', status='scanned',
                depth=0, total=4, total_categories=2, potential=2, potential_categories=1,
                target_articles=['Статья А', 'Статья Б'], target_categories=['Категория:Дочерняя'])


class SyncMemberReadingTests(unittest.TestCase):
    def setUp(self):
        self.reader = FixtureReader()
        self.addCleanup(self.reader.close)
        self.reader.graph[('Category:A', 'page')] = ['One', 'Duplicate', 'No link']
        self.reader.graph[('Category:A', 'subcat')] = ['Category:Child']
        for title in ('One', 'Duplicate', 'No link'):
            self.reader.page_data['en'][title] = page(title, ns=0, qid=title)
        self.reader.page_data['en']['Category:Child'] = page('Category:Child', qid='child')
        self.reader.page_data['ru']['Одна'] = page('Одна', ns=0)
        self.reader.page_data['ru']['Категория:Дочерняя'] = page('Категория:Дочерняя')
        self.reader.links.update({'One': 'Одна', 'Duplicate': 'Одна', 'child': 'Категория:Дочерняя'})

    def test_shared_matching_keeps_direct_subcategories_and_deduplicates_target_pages(self):
        result = self.reader.transfer_members('Category:A', subcategories=True)
        self.assertEqual((3, 1, ['Одна']), (result['total'], result['potential'], result['target_articles']))
        self.assertEqual((1, 1, ['Категория:Дочерняя']),
                         (result['total_categories'], result['potential_categories'], result['target_categories']))

    def test_subcategory_only_mode_does_not_request_articles(self):
        with patch.object(self.reader, 'article_matches') as articles:
            result = self.reader.transfer_members('Category:A', articles=False, subcategories=True)
        articles.assert_not_called()
        self.assertEqual([], result['target_articles'])
        self.assertEqual(['Категория:Дочерняя'], result['target_categories'])

    def test_matched_and_addable_counts_are_separate_for_both_member_types(self):
        self.reader.graph[('ru', 'Категория:А', 'page')] = ['Одна']
        result = self.reader.transfer_members('Category:A', subcategories=True, target_category='Категория:А')
        self.assertEqual((1, 0), (result['potential'], result['additional']))
        self.assertEqual((1, 1), (result['potential_categories'], result['additional_categories']))
        self.assertEqual({'Одна': True, 'Категория:Дочерняя': False}, result['membership'])

    def test_source_language_category_read_returns_only_existing_target_analogs(self):
        self.reader.source_lang = 'pt'
        self.reader.page_data['pt'] = {'Categoria:A': page('Categoria:A', qid='Q1'),
                                       'Categoria:B': page('Categoria:B', qid='Q2')}
        self.reader.page_data['ru']['Категория:А'] = page('Категория:А')
        self.reader.links['Q1'] = 'Категория:А'
        base_info = self.reader.site_info
        def info(lang):
            data = base_info(lang)
            if lang == 'pt':
                data['namespaces']['14']['name'] = 'Categoria'
            return data
        self.reader.site_info = info
        worker = CategoryFetchWorker(category='Categoria:Meta', lang='pt', family='wikipedia', depth=0,
                                     mode='categories_only', map_target_lang='ru')
        self.addCleanup(worker.api_client.session.close)
        worker._fetch_titles_for_mode = Mock(return_value=(['Categoria:A', 'Categoria:B'], {}))
        results, pairs, partial = [], [], []
        worker.result_ready.connect(lambda titles, stats: results.append(titles))
        worker.mapped_pairs_ready.connect(pairs.append)
        worker.partial_ready.connect(lambda titles, stats: partial.append(titles))
        with patch('wiki_cat_tool.workers.category_fetch_worker.CategoryTransferReader', return_value=self.reader):
            worker.run()
        worker._emit_partial_snapshot(categories=['Categoria:A'], pages=[])
        self.assertEqual('Categoria:Meta', worker._fetch_titles_for_mode.call_args.kwargs['category'])
        self.assertEqual([['Категория:А']], results)
        self.assertEqual(('pt', 'ru'), (pairs[0]['source'], pairs[0]['target']))
        self.assertEqual('Categoria:A', pairs[0]['pairs'][0].source_title)
        self.assertEqual([], partial)

    def test_preview_attaches_member_lists_and_does_not_turn_read_errors_into_zero_counts(self):
        worker = CategoryContentSyncPreviewWorker(target_categories=['Категория:А'], source_lang='en', target_lang='ru',
                                                  family='wikipedia', process_articles=True, process_subcategories=True, article_depth=0)
        self.addCleanup(worker.api_client.session.close)
        worker._member_reader = self.reader
        row = dict(source_title='Category:A', target_title='Категория:А', status='preview_ready')
        result = worker._read_available_members(row)
        self.assertEqual('3+1', result['count_text'])
        self.assertEqual(['Одна'], result['members']['target_articles'])
        self.reader.transfer_members = Mock(side_effect=APIRequestError('offline'))
        result = worker._read_available_members(row)
        self.assertEqual('error', result['status'])
        self.assertNotIn('members', result)
        self.assertIn('offline', result['error'])

    def test_preview_closes_both_readers_and_its_http_session_on_failure(self):
        worker = CategoryContentSyncPreviewWorker(target_categories=['Категория:А'], source_lang='en', target_lang='ru',
                                                  family='wikipedia', process_articles=True, process_subcategories=True, article_depth=0)
        self.addCleanup(worker.api_client.session.close)
        worker._preview_reader, worker._member_reader = Mock(), Mock()
        worker._run_preview = Mock(side_effect=APIRequestError('offline'))
        with patch.object(worker.api_client.session, 'close') as close:
            worker.run()
        worker._preview_reader.close.assert_called_once()
        worker._member_reader.close.assert_called_once()
        close.assert_called_once()


class SyncMemberUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.parent = QWidget()
        self.parent._ui_lang = 'ru'
        self.parent.current_lang = 'ru'
        self.parent.current_family = 'wikipedia'
        self.sync = self.parent.category_content_sync_tab = CategoryContentSyncTab(self.parent)
        self.addCleanup(self.dispose)

    def dispose(self):
        self.parent.close()
        self.parent.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)

    def load_scan(self, rows=None):
        rows = rows or [scan_row()]
        with patch('wiki_cat_tool.gui.tabs.category_content_sync_tab.CategoryContentSyncPreviewWorker') as preview:
            self.sync.load_scanned_categories(rows, 'en', include_subcategories=True)
        preview.assert_not_called()

    def test_scan_handoff_reuses_all_lists_and_is_ready_without_another_scan(self):
        self.load_scan()
        self.assertEqual(8, self.sync.preview_table.columnCount())
        self.assertEqual(4, self.sync.preview_table.horizontalHeader().visualIndex(6))
        self.assertEqual('2+1', self.sync.preview_table.item(0, 6).text())
        self.assertEqual('4+2', self.sync.preview_table.item(0, 3).text())
        self.assertTrue(self.sync._is_preview_signature_actual())
        self.assertTrue(self.sync.start_btn.isEnabled())
        self.assertEqual({'категория:а': dict(articles=('Статья А', 'Статья Б'), subcategories=('Категория:Дочерняя',))},
                         self.sync._member_plans())

    def test_dialog_delete_button_and_del_exclude_pages_and_undo_restores_them(self):
        dialog = CategoryArticlesDialog(scan_row(), language='ru', family='wikipedia', translate=self.sync._stage_t,
                                        parent=self.sync, editable=True)
        dialog.show()
        self.app.processEvents()
        dialog.table.selectRow(0)
        dialog.table.setFocus()
        QTest.keyClick(dialog.table, Qt.Key_Delete)
        self.assertEqual(('Статья Б', 'Категория:Дочерняя'), dialog.articles)
        # A late membership response must not reinsert an excluded page.
        dialog.show_membership([('Статья А', True), ('Статья Б', False), ('Категория:Дочерняя', False)])
        self.assertEqual(2, dialog.table.rowCount())
        dialog.history.undo()
        self.assertEqual(('Статья А', 'Статья Б', 'Категория:Дочерняя'), dialog.articles)
        self.assertEqual('Да', dialog.table.item(0, 1).text())
        dialog.table.selectAll()
        dialog.remove_btn.click()
        self.assertEqual((), dialog.articles)
        self.assertEqual(0, dialog.table.rowCount())
        dialog.close()

    def test_imported_report_additional_view_filters_a_late_membership_result(self):
        data = {**scan_row(), 'target_articles': [], 'target_categories': []}
        dialog = CategoryArticlesDialog(data, language='ru', family='wikipedia', translate=self.sync._stage_t,
                                        parent=self.sync, only_additional=True)
        dialog.show_membership([('Статья А', True), ('Статья Б', False), ('Категория:Дочерняя', False)])
        self.assertEqual(('Статья Б', 'Категория:Дочерняя'), dialog.articles)
        self.assertEqual(2, dialog.table.rowCount())
        dialog.close()

    def test_review_exclusions_apply_to_only_one_category_and_parent_undo_restores_plan(self):
        self.load_scan([scan_row(), scan_row('B', 'Б')])
        def remove_one():
            dialog = self.app.activeModalWidget()
            dialog.table.selectRow(0)
            dialog.remove_btn.click()
            dialog.reject()
        with patch('wiki_cat_tool.gui.tabs.category_content_sync_tab.CategoryArticlePresenceWorker') as worker:
            QTimer.singleShot(0, remove_one)
            self.sync._review_members(0)
        self.assertEqual(('Статья Б',), self.sync._member_plans()['категория:а']['articles'])
        self.assertEqual(('Статья А', 'Статья Б'), self.sync._member_plans()['категория:б']['articles'])
        self.assertEqual('1+1', self.sync.preview_table.item(0, 6).text())
        self.sync.preview_history.undo()
        self.assertEqual(('Статья А', 'Статья Б'), self.sync._member_plans()['категория:а']['articles'])
        worker.return_value.request_stop.assert_called()
        self.sync._member_workers.clear()

    def test_run_receives_an_independent_exact_member_plan(self):
        self.load_scan()
        self.parent.current_user, self.parent.current_password = 'User', 'unused'
        with patch('wiki_cat_tool.gui.tabs.category_content_sync_tab.CategoryContentSyncWorker') as factory:
            self.sync.start_sync()
        self.assertEqual(('Статья А', 'Статья Б'), factory.call_args.kwargs['member_plans']['категория:а']['articles'])
        self.sync.sync_worker = None

    def test_additional_view_keeps_already_present_matches_when_excluding_an_addable_page(self):
        data = {**scan_row(), 'membership': {'Статья А': True, 'Статья Б': False, 'Категория:Дочерняя': False}}
        self.load_scan([data])
        self.assertEqual('1+1', self.sync.preview_table.item(0, 7).text())
        seen = []
        def remove_one():
            dialog = self.app.activeModalWidget()
            seen.extend(dialog.articles)
            dialog.table.selectRow(0)
            dialog.remove_btn.click()
            dialog.reject()
        QTimer.singleShot(0, remove_one)
        self.sync._review_members(0, only_additional=True)
        self.assertEqual(['Статья Б', 'Категория:Дочерняя'], seen)
        self.assertEqual(('Статья А',), self.sync._member_plans()['категория:а']['articles'])
        self.assertEqual('1+1', self.sync.preview_table.item(0, 6).text())
        self.assertEqual('0+1', self.sync.preview_table.item(0, 7).text())

    def test_read_language_toggle_does_not_reverse_transfer_direction(self):
        self.sync.source_lang_edit.setText('pt')
        self.sync.source_panel.manual_list.setPlainText('Категория:А')
        for source_mode, language in ((False, 'ru'), (True, 'pt'), (False, 'ru')):
            self.sync.read_source_toggle.setChecked(source_mode)
            self.assertEqual(language, self.sync.source_panel.get_read_language())
            config = self.sync._collect_run_config()
            self.assertEqual(('pt', 'ru'), (config['source_lang'], config['target_lang']))

    def test_read_source_button_uses_source_language_without_double_namespace_prefix(self):
        self.sync.source_lang_edit.setText('pt')
        self.sync.read_source_toggle.setChecked(True)
        self.sync.cat_edit.setText('Categoria:Meta')
        with patch('wiki_cat_tool.gui.widgets.shared_panels.CategoryFetchWorker') as factory:
            self.sync.source_panel.fetch_selected_mode_replace()
        self.assertEqual(('pt', 'ru', 'Categoria:Meta'),
                         tuple(factory.call_args.kwargs[key] for key in ('lang', 'map_target_lang', 'category')))
        self.sync.source_panel._on_fetch_titles_finished()

    def test_available_click_opens_review_instead_of_a_browser(self):
        self.load_scan()
        with patch.object(self.sync, '_review_members') as review, \
             patch('wiki_cat_tool.gui.tabs.category_content_sync_tab.QDesktopServices.openUrl') as browser:
            self.sync._open_preview_cell_link(0, 6)
        review.assert_called_once_with(0)
        browser.assert_not_called()

    def test_mode_change_invalidates_member_review_and_start(self):
        self.load_scan()
        self.sync.subcategories_checkbox.setChecked(False)
        self.assertFalse(self.sync._preview_ready)
        self.assertEqual({}, self.sync._member_plans())
        self.assertFalse(self.sync.start_btn.isEnabled())

    def test_category_reading_makes_the_current_language_explicit_and_explains_empty_results(self):
        self.sync.source_lang_edit.setText('pt')
        self.assertIn('(ru)', self.sync.source_panel.read_controls.section_label.text())
        self.assertIn('разделе ru', self.sync.source_panel.replace_list_btn.toolTip())
        self.assertIn('pt', self.sync.source_panel.replace_list_btn.toolTip())
        self.sync.source_panel.manual_list.setPlainText('Категория:Сохранённая')
        with patch('wiki_cat_tool.gui.tabs.category_content_sync_tab.QMessageBox.warning') as warning:
            self.sync.source_panel._on_fetch_titles_ready([], {}, append=False, mode='categories_only', depth=0)
        self.assertIn('разделе ru', warning.call_args.args[2])
        self.assertIn('pt', warning.call_args.args[2])
        self.assertEqual('Категория:Сохранённая', self.sync.source_panel.manual_list.toPlainText())

    def test_busy_sync_cannot_be_overwritten_by_scanning_handoff(self):
        self.load_scan()
        self.sync.sync_worker = Mock(isRunning=Mock(return_value=True))
        self.assertFalse(self.sync.load_scanned_categories([scan_row('Other', 'Другая')], 'en'))
        self.assertIn('категория:а', self.sync._member_plans())
        self.sync.sync_worker = None

    def test_shortcut_warns_before_skipping_rows_without_a_target_counterpart(self):
        scan = CategoryTransferTab(self.parent)
        tabs = self.parent.tabs = QTabWidget(self.parent)
        tabs.addTab(scan, 'scan')
        tabs.addTab(self.sync, 'sync')
        scan._apply_row(scan_row())
        scan._apply_row({**scan_row('Missing', ''), 'target_title': ''})
        scan._set_busy(False)
        with patch('wiki_cat_tool.gui.tabs.category_transfer_tab.QMessageBox.question', return_value=65536) as warning:
            scan.open_scan_sync()
        warning.assert_called_once()
        self.assertIs(scan, tabs.currentWidget())
        self.assertFalse(self.sync._preview_ready)

    def test_scanning_shortcut_skips_preparation_without_changing_its_input(self):
        scan = CategoryTransferTab(self.parent)
        tabs = self.parent.tabs = QTabWidget(self.parent)
        tabs.addTab(scan, 'scan')
        tabs.addTab(scan.creation_stage, 'prepare')
        tabs.addTab(self.sync, 'sync')
        scan.include_subcategories.setChecked(True)
        scan._apply_row(scan_row())
        scan._set_busy(False)
        with patch('wiki_cat_tool.gui.tabs.category_content_sync_tab.CategoryContentSyncPreviewWorker') as preview:
            scan.direct_sync_btn.click()
        preview.assert_not_called()
        self.assertIs(self.sync, tabs.currentWidget())
        self.assertEqual(0, scan.creation_table.rowCount())
        self.assertEqual('2+1', self.sync.preview_table.item(0, 6).text())


class SyncMemberWriteTests(unittest.TestCase):
    def setUp(self):
        self.worker = CategoryContentSyncWorker(target_categories=['Категория:А'], username='User', password='unused',
                                               source_lang='en', target_lang='ru', family='wikipedia',
                                               process_articles=True, process_subcategories=True, article_depth=0,
                                               member_plans={'Категория:А': dict(articles=('Одна',), subcategories=('Категория:Дочерняя',))})
        self.worker._save_with_retry = Mock(return_value=True)
        self.worker._category_already_exists = Mock(return_value=False)
        self.worker._build_default_summary = Mock(return_value='summary')

    def test_articles_removed_or_added_after_review_are_never_saved(self):
        reader = Mock()
        reader.article_matches.return_value = {'One': 'Одна', 'Removed': 'Убрана', 'New': 'Новая'}
        target = Mock()
        target.exists.return_value, target.isRedirectPage.return_value = True, False
        target.get.return_value, target.title.return_value = 'text', 'Одна'
        with patch('wiki_cat_tool.workers.category_content_sync_worker.CategoryTransferReader', return_value=reader), \
             patch('wiki_cat_tool.workers.category_content_sync_worker.pywikibot.Page', return_value=target) as page_type:
            stats = self.worker._transfer_articles('Category:A', 'Категория:А', Mock(), Mock(),
                                                   source_members=[Mock(title=Mock(return_value=title)) for title in ('One', 'Removed', 'New')],
                                                   row_key='категория:а')
        self.assertEqual((1, 2), (stats['success'], stats['skipped']))
        self.worker._save_with_retry.assert_called_once()
        page_type.assert_called_once()

    def test_subcategory_removed_from_review_is_never_read_or_saved(self):
        self.worker._get_linked_page_title_retry = Mock(return_value='Категория:Убрана')
        target = Mock(title=Mock(return_value='Категория:Убрана'))
        with patch('wiki_cat_tool.workers.category_content_sync_worker.pywikibot.Category', return_value=target):
            stats = self.worker._transfer_subcategories('Category:A', 'Категория:А', Mock(), Mock(),
                                                        source_members=[Mock(title=Mock(return_value='Category:Removed'))], row_key='категория:а')
        self.assertEqual(1, stats['skipped'])
        target.get.assert_not_called()
        self.worker._save_with_retry.assert_not_called()

    def test_selection_is_scoped_to_category_preserves_case_and_copies_mutable_inputs(self):
        self.assertTrue(self.worker._member_is_selected('Категория:А', 'articles', 'Одна'))
        self.assertFalse(self.worker._member_is_selected('Категория:Б', 'articles', 'Одна'))
        self.assertFalse(self.worker._member_is_selected('Категория:А', 'articles', 'ОДНА'))
        self.assertFalse(self.worker._member_is_selected('Категория:А', 'subcategories', 'Одна'))


if __name__ == '__main__':
    unittest.main()
