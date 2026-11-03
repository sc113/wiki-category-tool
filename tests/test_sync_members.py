from __future__ import annotations

# ruff: noqa: E402
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTabWidget, QWidget
from wiki_cat_tool.core.api_client import APIRequestError
from wiki_cat_tool.gui.tabs.category_content_sync_tab import CategoryContentSyncTab
from wiki_cat_tool.gui.tabs.category_transfer_tab import CategoryTransferTab
from wiki_cat_tool.gui.widgets.category_articles import CategoryArticlesDialog
from wiki_cat_tool.tests.test_category_transfer import FixtureReader, page
from wiki_cat_tool.workers.category_content_sync_worker import CategoryContentSyncPreviewWorker, CategoryContentSyncWorker
from wiki_cat_tool.workers.category_fetch_worker import CategoryFetchWorker, CategoryLanguageProbeWorker


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

    def test_probe_checks_both_editions_and_recognizes_either_localized_prefix(self):
        base_info = self.reader.site_info
        def info(lang):
            data = base_info(lang)
            if lang == 'en':
                data['namespaces']['14']['name'] = 'Categoria'
                data['namespacealiases'] = [dict(id=14, alias='Cat')]
            return data
        self.reader.site_info = info
        self.reader.page_data['en']['Categoria:Meta'] = page('Categoria:Meta')
        for raw in ('Meta', 'Categoria:Meta', 'Категория:Meta', 'Cat:Meta', '[[Categoria:Meta|sort]]'):
            with self.subTest(raw=raw):
                self.assertEqual([dict(lang='en', title='Categoria:Meta')], self.reader.category_read_candidates(raw))
        self.reader.page_data['ru']['Категория:Meta'] = page('Категория:Meta')
        self.assertEqual([dict(lang='en', title='Categoria:Meta'), dict(lang='ru', title='Категория:Meta')],
                         self.reader.category_read_candidates('Categoria:Meta'))

    def test_probe_accepts_populated_red_categories_but_not_empty_missing_titles(self):
        self.reader.page_data['en']['Category:Meta'] = page('Category:Meta', missing=True, categoryinfo=dict(subcats=3))
        self.reader.page_data['ru']['Категория:Meta'] = page('Категория:Meta', missing=True)
        self.assertEqual([dict(lang='en', title='Category:Meta')], self.reader.category_read_candidates('Meta'))
        self.reader.page_data['en'].clear()
        self.assertEqual([], self.reader.category_read_candidates('Meta'))

    def test_probe_resolves_category_redirects_before_selecting_the_edition(self):
        redirected = dict(title='Category:Actual', ns=14)
        self.reader.pages = Mock(side_effect=lambda lang, titles, **kwargs: {titles[0]: redirected} if lang == 'en' else {})
        self.assertEqual([dict(lang='en', title='Category:Actual')], self.reader.category_read_candidates('Category:Meta'))
        self.assertTrue(all(call.kwargs['resolve_redirects'] for call in self.reader.pages.call_args_list))

    def test_probe_failure_is_not_reported_as_missing_and_reader_always_closes(self):
        worker = CategoryLanguageProbeWorker(category='Meta', source_lang='en', target_lang='ru', family='wikipedia')
        errors = []
        worker.failed.connect(errors.append)
        self.reader.category_read_candidates = Mock(side_effect=APIRequestError('offline'))
        with patch('wiki_cat_tool.workers.category_fetch_worker.CategoryTransferReader', return_value=self.reader), \
             patch.object(self.reader, 'close') as close:
            worker.run()
        self.assertTrue(worker.failed_check)
        self.assertEqual(['offline'], errors)
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

    def test_detected_read_language_does_not_reverse_transfer_direction(self):
        self.sync.source_lang_edit.setText('pt')
        self.sync.source_panel.manual_list.setPlainText('Категория:А')
        for language in ('ru', 'pt', 'ru'):
            self.sync._read_detected_lang = language
            self.sync._refresh_read_language_hint()
            self.assertEqual(language, self.sync.source_panel.get_read_language())
            config = self.sync._collect_run_config()
            self.assertEqual(('pt', 'ru'), (config['source_lang'], config['target_lang']))

    def finish_probe(self, worker, candidates, *, stopped=False, failed=False):
        worker.candidates, worker._stop, worker.failed_check = candidates, stopped, failed
        worker.isRunning.return_value = False
        self.sync._read_detection_finished(worker, self.sync._read_detection_context(),
                                            dict(mode='categories_only', append=False, trigger_button=None, petscan=False))

    def test_automatic_source_read_uses_source_language_without_double_namespace_prefix(self):
        self.sync.source_lang_edit.setText('pt')
        self.sync.cat_edit.setText('Categoria:Meta')
        with patch('wiki_cat_tool.gui.tabs.category_content_sync_tab.CategoryLanguageProbeWorker') as probe:
            self.sync.source_panel.fetch_selected_mode_replace()
        self.assertEqual(('pt', 'ru', 'Categoria:Meta'),
                         tuple(probe.call_args.kwargs[key] for key in ('source_lang', 'target_lang', 'category')))
        self.assertFalse(self.sync.source_lang_edit.isEnabled())
        with patch('wiki_cat_tool.gui.widgets.shared_panels.CategoryFetchWorker') as factory:
            self.finish_probe(probe.return_value, [dict(lang='pt', title='Categoria:Meta')])
        self.assertEqual(('pt', 'ru', 'Categoria:Meta'),
                         tuple(factory.call_args.kwargs[key] for key in ('lang', 'map_target_lang', 'category')))
        self.sync.source_panel._on_fetch_titles_finished()

    def test_detected_target_category_reads_current_language_without_mapping(self):
        self.sync.cat_edit.setText('Категория:Мета')
        with patch('wiki_cat_tool.gui.tabs.category_content_sync_tab.CategoryLanguageProbeWorker') as probe:
            self.sync.source_panel.fetch_selected_mode_replace()
        with patch('wiki_cat_tool.gui.widgets.shared_panels.CategoryFetchWorker') as factory:
            self.finish_probe(probe.return_value, [dict(lang='ru', title='Категория:Мета')])
        self.assertEqual('ru', factory.call_args.kwargs['lang'])
        self.assertNotIn('map_target_lang', factory.call_args.kwargs)
        self.sync.source_panel._on_fetch_titles_finished()

    def test_ambiguous_category_prompts_and_cancellation_keeps_existing_input(self):
        self.sync.cat_edit.setText('Category:Meta')
        self.sync.source_panel.manual_list.setPlainText('Категория:Сохранённая')
        candidates = [dict(lang='en', title='Category:Meta'), dict(lang='ru', title='Категория:Meta')]
        for selection in (None, candidates[0], candidates[1]):
            with patch('wiki_cat_tool.gui.tabs.category_content_sync_tab.CategoryLanguageProbeWorker') as probe:
                self.sync.source_panel.fetch_selected_mode_replace()
            with patch.object(self.sync, '_choose_read_category', return_value=selection) as choose, \
                 patch('wiki_cat_tool.gui.widgets.shared_panels.CategoryFetchWorker') as fetch:
                self.finish_probe(probe.return_value, candidates)
            choose.assert_called_once_with(candidates)
            if selection is None:
                fetch.assert_not_called()
                self.assertEqual('Категория:Сохранённая', self.sync.source_panel.manual_list.toPlainText())
            else:
                self.assertEqual(selection['lang'], fetch.call_args.kwargs['lang'])
                self.sync.source_panel._on_fetch_titles_finished()

    def test_stop_failure_and_missing_category_do_not_start_a_fetch_or_clear_the_list(self):
        self.sync.cat_edit.setText('Category:Meta')
        self.sync.source_panel.manual_list.setPlainText('Категория:Сохранённая')
        for stopped, failed, warn in ((True, False, False), (False, True, False), (False, False, True)):
            with patch('wiki_cat_tool.gui.tabs.category_content_sync_tab.CategoryLanguageProbeWorker') as probe:
                self.sync.source_panel.fetch_selected_mode_replace()
            with patch('wiki_cat_tool.gui.widgets.shared_panels.CategoryFetchWorker') as fetch, \
                 patch('wiki_cat_tool.gui.tabs.category_content_sync_tab.QMessageBox.warning') as warning:
                self.finish_probe(probe.return_value, [], stopped=stopped, failed=failed)
            fetch.assert_not_called()
            self.assertEqual(warn, warning.called)
            self.assertEqual('Категория:Сохранённая', self.sync.source_panel.manual_list.toPlainText())
            self.assertTrue(self.sync.source_lang_edit.isEnabled())

    def test_context_change_discards_a_late_detection_result(self):
        self.sync.cat_edit.setText('Category:Meta')
        with patch('wiki_cat_tool.gui.tabs.category_content_sync_tab.CategoryLanguageProbeWorker') as probe:
            self.sync.source_panel.fetch_selected_mode_replace()
        worker = probe.return_value
        worker.candidates, worker._stop, worker.failed_check = [dict(lang='en', title='Category:Meta')], False, False
        worker.isRunning.return_value = False
        context = self.sync._read_detection_context()
        self.sync.cat_edit.setText('Category:Other')
        with patch('wiki_cat_tool.gui.widgets.shared_panels.CategoryFetchWorker') as fetch:
            self.sync._read_detection_finished(worker, context, dict(mode='categories_only', append=False, trigger_button=None, petscan=False))
        fetch.assert_not_called()
        self.assertEqual('', self.sync._read_detected_lang)

    def test_stop_reaches_the_detection_worker_and_no_preview_can_start_while_it_runs(self):
        self.sync.cat_edit.setText('Category:Meta')
        with patch('wiki_cat_tool.gui.tabs.category_content_sync_tab.CategoryLanguageProbeWorker') as probe:
            self.sync.source_panel.fetch_selected_mode_replace()
        self.assertFalse(self.sync.preview_btn.isEnabled())
        with patch.object(self.sync, '_collect_run_config') as config:
            self.sync.start_preview()
            self.sync.start_sync()
        config.assert_not_called()
        self.sync.source_panel.fetch_selected_mode_replace()
        probe.return_value.request_stop.assert_called_once()
        with patch('wiki_cat_tool.gui.widgets.shared_panels.CategoryFetchWorker') as fetch:
            self.finish_probe(probe.return_value, [dict(lang='en', title='Category:Meta')])
        fetch.assert_not_called()
        self.assertTrue(self.sync.preview_btn.isEnabled())

    def test_petscan_uses_the_automatically_detected_edition(self):
        self.sync.source_lang_edit.setText('pt')
        self.sync.cat_edit.setText('Categoria:Meta')
        with patch('wiki_cat_tool.gui.tabs.category_content_sync_tab.CategoryLanguageProbeWorker') as probe:
            self.sync.source_panel.open_petscan_in_browser()
        worker = probe.return_value
        worker.candidates, worker._stop, worker.failed_check = [dict(lang='pt', title='Categoria:Meta')], False, False
        worker.isRunning.return_value = False
        with patch('wiki_cat_tool.gui.tabs.category_content_sync_tab.QDesktopServices.openUrl') as browser:
            self.sync._read_detection_finished(worker, self.sync._read_detection_context(),
                                              dict(mode='categories_only', append=False, trigger_button=None, petscan=True))
        self.assertIn('language=pt', browser.call_args.args[0].toString())
        self.assertIsNone(self.sync.source_panel._fetch_worker)

    def test_choose_read_dialog_names_both_editions_and_returns_the_clicked_one(self):
        candidates = [dict(lang='en', title='Category:Meta'), dict(lang='ru', title='Категория:Meta')]
        def choose_target():
            box = self.app.activeModalWidget()
            button = next(button for button in box.buttons() if '(ru)' in button.text())
            button.click()
        QTimer.singleShot(0, choose_target)
        self.assertEqual(candidates[1], self.sync._choose_read_category(candidates))

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

    def test_zero_additions_cleanup_keeps_unknown_errors_and_positive_subcategories_and_supports_undo(self):
        empty = {**scan_row('Empty', 'Пустая'), 'membership': {'Статья А': True, 'Статья Б': True, 'Категория:Дочерняя': True}}
        positive = {**scan_row('Positive', 'Добавить'), 'membership': {'Статья А': True, 'Статья Б': True, 'Категория:Дочерняя': False}}
        unknown = scan_row('Unknown', 'Неизвестно')
        failed = {**empty, 'source_title': 'Category:Error', 'target_title': 'Категория:Ошибка'}
        self.load_scan([empty, positive, unknown, failed])
        self.sync._set_row_status(3, 'error')
        self.sync._update_clear_skipped_button_state()
        self.assertTrue(self.sync.clear_zero_additions_btn.isEnabled())
        self.sync.clear_zero_additions_btn.click()
        self.assertEqual(['Категория:Добавить', 'Категория:Неизвестно', 'Категория:Ошибка'], self.sync._categories_from_preview_table())
        self.sync.preview_history.undo()
        self.assertEqual(4, self.sync.preview_table.rowCount())
        self.assertTrue(self.sync.start_btn.isEnabled())
        self.assertTrue(self.sync.clear_zero_additions_btn.isEnabled())
        self.sync.preview_worker = Mock(isRunning=Mock(return_value=True))
        self.sync._update_clear_skipped_button_state()
        self.assertFalse(self.sync.clear_zero_additions_btn.isEnabled())
        self.sync.clear_zero_addition_rows()
        self.assertEqual(4, self.sync.preview_table.rowCount())
        self.sync.preview_worker = None

    def test_clear_zero_additions_disables_start_when_every_row_is_removed(self):
        self.load_scan([{**scan_row(), 'membership': {'Статья А': True, 'Статья Б': True, 'Категория:Дочерняя': True}}])
        self.sync.clear_zero_addition_rows()
        self.assertEqual(0, self.sync.preview_table.rowCount())
        self.assertFalse(self.sync.start_btn.isEnabled())
        self.sync.preview_history.undo()
        self.assertTrue(self.sync.start_btn.isEnabled())

    def test_saved_members_update_remaining_counts_in_sorted_rows_and_enable_cleanup(self):
        data = {**scan_row(), 'membership': {'Статья А': False, 'Статья Б': False, 'Категория:Дочерняя': False}}
        self.load_scan([data, {**data, 'source_title': 'Category:B', 'target_title': 'Категория:Б'}])
        self.sync.preview_table.sortItems(7, Qt.AscendingOrder)
        for title in ('Статья А', 'Статья Б', 'Категория:Дочерняя'):
            self.sync._apply_member_presence('категория:а', title)
        self.sync._apply_sync_category_result(dict(row_key='категория:а', target_title='Категория:А', transferred=3, status='done'))
        row = self.sync._preview_row_index['категория:а']
        self.assertEqual('0+0', self.sync.preview_table.item(row, 7).text())
        self.assertEqual('2+1', self.sync.preview_table.item(row, 6).text())
        self.assertTrue(self.sync.clear_zero_additions_btn.isEnabled())
        self.sync.clear_zero_additions_btn.click()
        self.assertEqual(['Категория:Б'], self.sync._categories_from_preview_table())

    def test_unreviewed_or_removed_member_results_cannot_change_a_transfer_plan(self):
        data = {**scan_row(), 'membership': {'Статья А': False, 'Статья Б': False, 'Категория:Дочерняя': False}}
        self.load_scan([data])
        self.sync._apply_member_presence('категория:а', 'Новая страница')
        self.sync._apply_member_presence('категория:другая', 'Статья А')
        self.assertEqual('2+1', self.sync.preview_table.item(0, 7).text())
        self.assertEqual(('Статья А', 'Статья Б'), self.sync._member_plans()['категория:а']['articles'])

    def test_excluding_last_addable_page_immediately_enables_zero_cleanup(self):
        data = {**scan_row(), 'membership': {'Статья А': True, 'Статья Б': False, 'Категория:Дочерняя': True}}
        self.load_scan([data])
        self.assertFalse(self.sync.clear_zero_additions_btn.isEnabled())
        def exclude():
            dialog = self.app.activeModalWidget()
            dialog.table.selectAll()
            dialog.remove_btn.click()
            dialog.reject()
        QTimer.singleShot(0, exclude)
        self.sync._review_members(0, only_additional=True)
        self.assertEqual('0+0', self.sync.preview_table.item(0, 7).text())
        self.assertTrue(self.sync.clear_zero_additions_btn.isEnabled())

    def test_preview_all_headers_are_sortable_and_counts_sort_numerically(self):
        rows = []
        for name, count in (('Ten', 10), ('Two', 2), ('One', 1)):
            rows.append({**scan_row(name, name), 'total': count, 'total_categories': 0,
                         'target_articles': ['Article ' + str(i) for i in range(count)], 'target_categories': [],
                         'membership': {'Article ' + str(i): False for i in range(count)}})
        self.load_scan(rows)
        table = self.sync.preview_table
        self.assertTrue(table.isSortingEnabled())
        for column in range(table.columnCount()):
            with self.subTest(column=column):
                table.sortItems(column, Qt.AscendingOrder)
                self.assertEqual(column, table.horizontalHeader().sortIndicatorSection())
        for column in (3, 6, 7):
            table.sortItems(column, Qt.AscendingOrder)
            self.assertEqual(['Категория:One', 'Категория:Two', 'Категория:Ten'], self.sync._categories_from_preview_table())
        table.sortItems(4, Qt.AscendingOrder)
        self.sync._apply_sync_category_result(dict(row_key='категория:two', target_title='Категория:Two', transferred=12,
                                                   total_text='14+2', status='done'))
        self.sync._apply_sync_category_result(dict(row_key='категория:ten', target_title='Категория:Ten', transferred=2,
                                                   total_text='10+0', status='partial'))
        table.sortItems(4, Qt.AscendingOrder)
        self.assertEqual(['Категория:One', 'Категория:Ten', 'Категория:Two'], self.sync._categories_from_preview_table())
        self.assertEqual(16, self.sync._resolve_row_total('категория:two'))
        self.assertTrue(self.sync.start_btn.isEnabled())

    def test_clicking_header_changes_visible_order_and_keeps_rows_together(self):
        self.load_scan([scan_row('B', 'Б'), scan_row('A', 'А')])
        self.sync.show()
        self.app.processEvents()
        header = self.sync.preview_table.horizontalHeader()
        point = QPoint(header.sectionViewportPosition(1) + header.sectionSize(1) // 2, header.height() // 2)
        QTest.mouseClick(header.viewport(), Qt.LeftButton, pos=point)
        first_order = self.sync._categories_from_preview_table()
        QTest.mouseClick(header.viewport(), Qt.LeftButton, pos=point)
        self.assertEqual(list(reversed(first_order)), self.sync._categories_from_preview_table())
        for row in range(2):
            self.assertEqual({'Категория:А': 'Category:A', 'Категория:Б': 'Category:B'}[self.sync.preview_table.item(row, 1).text()],
                             self.sync.preview_table.item(row, 0).text())

    def test_preview_updates_and_transfer_follow_the_sorted_visible_rows(self):
        self.load_scan([scan_row('B', 'Б'), scan_row('A', 'А')])
        table = self.sync.preview_table
        table.sortItems(7, Qt.AscendingOrder)
        self.sync.preview_worker = Mock(isRunning=Mock(return_value=True))
        self.sync._append_preview_rows_batch([dict(row_key='категория:а', target_title='Категория:А', source_title='Category:A',
                                                   count_text='3+0', status='preview_ready',
                                                   members={**scan_row(), 'target_articles': ['А1', 'А2', 'А3'],
                                                            'target_categories': [], 'membership': {'А1': False, 'А2': False, 'А3': False}})])
        self.assertTrue(table.isSortingEnabled())
        row = self.sync._preview_row_index['категория:а']
        self.assertEqual('Category:A', table.item(row, 0).text())
        self.assertEqual('3+0', table.item(row, 7).text())
        self.sync.preview_worker = None
        table.sortItems(1, Qt.AscendingOrder)
        self.parent.current_user, self.parent.current_password = 'User', 'unused'
        with patch('wiki_cat_tool.gui.tabs.category_content_sync_tab.CategoryContentSyncWorker') as factory:
            self.sync.start_sync()
        self.assertEqual(['Категория:А', 'Категория:Б'], factory.call_args.kwargs['target_categories'])
        self.assertEqual(('А1', 'А2', 'А3'), factory.call_args.kwargs['member_plans']['категория:а']['articles'])
        self.sync.sync_worker = None

    def test_reading_explains_automatic_language_detection_and_empty_results(self):
        self.sync.source_lang_edit.setText('pt')
        self.assertIn('автоматически', self.sync.read_language_label.text())
        self.assertIn('ru', self.sync.source_panel.replace_list_btn.toolTip())
        self.assertIn('pt', self.sync.source_panel.replace_list_btn.toolTip())
        self.sync.source_panel.manual_list.setPlainText('Категория:Сохранённая')
        with patch('wiki_cat_tool.gui.tabs.category_content_sync_tab.QMessageBox.warning') as warning:
            self.sync.source_panel._on_fetch_titles_ready([], {}, append=False, mode='categories_only', depth=0)
        self.assertIn('разделе ru', warning.call_args.args[2])
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

    def test_article_presence_is_reported_only_for_saved_or_already_present_pages(self):
        present = []
        self.worker.member_present.connect(lambda category, title: present.append((category, title)))
        reader = Mock()
        reader.article_matches.return_value = {'One': 'Одна'}
        target = Mock()
        target.exists.return_value, target.isRedirectPage.return_value = True, False
        target.get.return_value, target.title.return_value = 'text', 'Одна'
        for already, saved, expected in ((False, True, 1), (True, False, 1), (False, False, 0)):
            present.clear()
            self.worker._category_already_exists.return_value = already
            self.worker._save_with_retry.return_value = saved
            with patch('wiki_cat_tool.workers.category_content_sync_worker.CategoryTransferReader', return_value=reader), \
                 patch('wiki_cat_tool.workers.category_content_sync_worker.pywikibot.Page', return_value=target):
                self.worker._transfer_articles('Category:A', 'Категория:А', Mock(), Mock(),
                                               source_members=[Mock(title=Mock(return_value='One'))], row_key='категория:а')
            self.assertEqual([('категория:а', 'Одна')] * expected, present)

    def test_subcategory_presence_is_reported_only_for_saved_or_already_present_pages(self):
        present = []
        self.worker.member_present.connect(lambda category, title: present.append((category, title)))
        self.worker._get_linked_page_title_retry = Mock(return_value='Категория:Дочерняя')
        target = Mock()
        target.exists.return_value, target.isRedirectPage.return_value = True, False
        target.namespace.return_value.id = 14
        target.get.return_value, target.title.return_value = 'text', 'Категория:Дочерняя'
        for already, saved, expected in ((False, True, 1), (True, False, 1), (False, False, 0)):
            present.clear()
            self.worker._category_already_exists.return_value = already
            self.worker._save_with_retry.return_value = saved
            with patch('wiki_cat_tool.workers.category_content_sync_worker.pywikibot.Category', return_value=target):
                self.worker._transfer_subcategories('Category:A', 'Категория:А', Mock(), Mock(),
                                                    source_members=[Mock(title=Mock(return_value='Category:Child'))], row_key='категория:а')
            self.assertEqual([('категория:а', 'Категория:Дочерняя')] * expected, present)

    def test_source_member_read_failure_counts_in_the_final_error_total(self):
        self.worker.username, self.worker.password = '', ''
        self.worker.process_subcategories = False
        self.worker._resolve_source_category_with_retry = Mock(return_value=('Category:A', 'Wikidata'))
        self.worker._collect_article_members = Mock(side_effect=APIRequestError('read failed'))
        self.worker._transfer_articles = Mock(return_value=dict(success=0, error=0, skipped=0, already=0))
        results = []
        self.worker.category_result.connect(results.append)
        with patch('wiki_cat_tool.workers.category_content_sync_worker.pywikibot.Site', return_value=Mock()), \
             patch('wiki_cat_tool.workers.category_content_sync_worker.pywikibot.Category', return_value=Mock()):
            self.worker.run()
        self.assertEqual('error', results[-1]['status'])
        self.assertEqual(1, self.worker.error_count)

    def test_site_initialization_failure_is_a_failed_operation(self):
        with patch('wiki_cat_tool.workers.category_content_sync_worker.pywikibot.Site', side_effect=RuntimeError('offline')):
            self.worker.run()
        self.assertTrue(self.worker.failed)
        self.assertIn('offline', self.worker.failure_message)

    def test_stop_interrupts_a_sync_lookup_rate_limit_pause(self):
        paused = threading.Event()
        original_wait = self.worker._interruptible_wait
        def wait(seconds):
            paused.set()
            return original_wait(seconds)
        self.worker._lookup_last_ts = time.time() + 60
        self.worker._interruptible_wait = wait
        result = []
        thread = threading.Thread(target=lambda: result.append(self.worker._get_linked_page_title_retry(Mock(), Mock())), daemon=True)
        with patch('wiki_cat_tool.workers.category_content_sync_worker.pywikibot.ItemPage.fromPage') as lookup:
            thread.start()
            try:
                self.assertTrue(paused.wait(1))
                self.worker.request_stop()
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertEqual([None], result)
                lookup.assert_not_called()
            finally:
                self.worker.request_stop()
                thread.join(1)

    def test_stop_interrupts_a_retry_after_from_wikidata_lookup(self):
        paused = threading.Event()
        original_wait = self.worker._interruptible_wait
        def wait(seconds):
            if seconds >= 30:
                paused.set()
            return original_wait(seconds)
        self.worker._interruptible_wait = wait
        result = []
        thread = threading.Thread(target=lambda: result.append(self.worker._get_linked_page_title_retry(Mock(), Mock())), daemon=True)
        with patch('wiki_cat_tool.workers.category_content_sync_worker.pywikibot.ItemPage.fromPage',
                   side_effect=RuntimeError('maxlag; Retry-After: 60')) as lookup:
            thread.start()
            try:
                self.assertTrue(paused.wait(1))
                self.worker.request_stop()
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertEqual([None], result)
                lookup.assert_called_once()
            finally:
                self.worker.request_stop()
                thread.join(1)


if __name__ == '__main__':
    unittest.main()
