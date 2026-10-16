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
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QTableWidget, QTableWidgetItem, QWidget, QTextEdit, QTabWidget, QAbstractItemView

from wiki_cat_tool.core.category_transfer import CategoryCreationRow, category_action_plan
from wiki_cat_tool.gui.tabs.category_content_sync_tab import CategoryContentSyncTab
from wiki_cat_tool.gui.tabs.category_interwiki_tab import CategoryInterwikiTab
from wiki_cat_tool.gui.tabs.category_transfer_tab import CategoryTransferTab
from wiki_cat_tool.gui.widgets.category_articles import CategoryArticlesDialog
from wiki_cat_tool.gui.widgets.table_history import TableHistory
from wiki_cat_tool.workers.category_transfer_worker import CategoryArticlePresenceWorker, CategoryTransferReadWorker


class TableActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.parent = QWidget()
        self.parent._ui_lang = 'ru'
        self.parent.current_lang = 'ru'
        self.parent.current_family = 'wikipedia'
        self.tab = CategoryTransferTab(self.parent)
        self.rows = (CategoryCreationRow('Category:A', 'Категория:А', 'one'),
                     CategoryCreationRow('Category:B', 'Категория:Б', 'two'))
        self.tab._fill_creation_table(self.rows)
        self.addCleanup(self._dispose_parent)

    def _dispose_parent(self):
        self.parent.close()
        self.parent.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_bulk_text_is_one_undo_step_and_requires_a_new_check(self):
        self.tab._creation_rows = self.rows
        self.tab._preview_context = self.tab._creation_context()
        self.tab._fill_content_rows([0, 1], 'replacement', only_empty=False)
        self.assertEqual(['replacement'] * 2, [r.content for r in self.tab._table_creation_rows()])
        self.tab.creation_history.undo()
        self.assertEqual(self.rows, self.tab._table_creation_rows())
        self.assertFalse(self.tab.create_btn.isEnabled())
        self.assertFalse(self.tab.sync_btn.isEnabled())
        self.tab.creation_history.redo()
        self.assertEqual(['replacement'] * 2, [r.content for r in self.tab._table_creation_rows()])
        self.tab.creation_history.undo()
        self.assertFalse(self.tab.creation_history.undo_action.isEnabled())

    def test_global_empty_cell_option_controls_copy_and_title_fill(self):
        from wiki_cat_tool.core.category_transfer import plan_numbered_titles
        self.assertFalse(self.tab.source_content_empty.isChecked())
        self.tab.source_content_empty.setChecked(True)
        self.tab.copy_first_content()
        self.assertEqual(self.rows, self.tab._table_creation_rows())
        self.tab.source_content_empty.setChecked(False)
        self.tab.copy_first_content()
        self.assertEqual(['one', 'one'], [r.content for r in self.tab._table_creation_rows()])
        rows = (CategoryCreationRow('Category:20th-century art', 'Old title', 'body'),)
        self.tab._fill_creation_table(rows)
        plan = plan_numbered_titles(rows, '', 'Категория:{число}', only_empty=False)
        self.tab._apply_numbered_titles(plan)
        self.assertEqual('Категория:20', self.tab.creation_table.item(0, 1).text())
        self.tab.creation_history.undo()
        self.assertEqual(rows, self.tab._table_creation_rows())

    def test_unchanged_title_fill_does_not_invalidate_a_verified_plan(self):
        from wiki_cat_tool.core.category_transfer import plan_numbered_titles
        rows = (CategoryCreationRow('Category:20th-century art', 'Категория:20', 'body'),)
        self.tab._fill_creation_table(rows)
        self.tab._creation_rows = rows
        context = self.tab._preview_context = self.tab._creation_context()
        plan = plan_numbered_titles(rows, '', 'Категория:{число}', only_empty=False)
        self.tab._apply_numbered_titles(plan)
        self.assertEqual(context, self.tab._preview_context)
        self.assertEqual(rows, self.tab._creation_rows)
        self.assertFalse(self.tab.creation_history.undo_action.isEnabled())

    def test_adding_to_a_sorted_interwiki_table_edits_the_new_blank_row(self):
        tab = CategoryInterwikiTab(self.parent)
        tab._fill_rows(self.rows)
        tab._set_busy(False)
        tab.table.sortItems(0, Qt.AscendingOrder)
        with patch.object(tab.table, 'editItem') as edit:
            tab.add_row()
        self.assertEqual(3, tab.table.rowCount())
        self.assertEqual('', tab.table.currentItem().text())
        self.assertIs(tab.table.currentItem(), edit.call_args.args[0])
        self.assertEqual({('Category:A', 'Категория:А'), ('Category:B', 'Категория:Б'), ('', '')},
                         {(r.source_title, r.target_title) for r in tab._rows()})

    def test_scan_export_is_one_file_and_metadata_never_becomes_wikitext(self):
        import csv
        import tempfile
        from wiki_cat_tool.core.category_transfer import read_creation_tsv
        for name in ('A', 'B'):
            self.tab._apply_row(dict(source_title='Category:' + name, target_title='Категория:' + name,
                                     qid='Q12', depth=1, total=5, potential=3, status='scanned'))
        self.assertIsNone(self.tab.export_scan_btn.menu())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'categories.tsv'
            with patch('wiki_cat_tool.gui.tabs.category_transfer_tab.QFileDialog.getSaveFileName', return_value=(str(path), '')):
                self.tab.export_report()
            with path.open(encoding='utf-8-sig', newline='') as handle:
                records = list(csv.reader(handle, delimiter='\t'))
            self.assertEqual([7, 7, 7], [len(r) for r in records])
            self.assertEqual({'Category:A', 'Category:B'}, {r[0] for r in records[1:]})
            self.assertEqual({'Category:A', 'Category:B'}, {r.source_title for r in read_creation_tsv(str(path))})
            self.assertTrue(all(not r.content for r in read_creation_tsv(str(path))))

    def test_sorting_all_creation_columns_keeps_data_and_write_results_together(self):
        table = self.tab.creation_table
        for column in range(table.columnCount()):
            table.sortItems(column, Qt.DescendingOrder)
            self.assertEqual(set(self.rows), set(self.tab._table_creation_rows()))
        table.sortItems(0, Qt.DescendingOrder)
        self.tab._set_busy(True)
        self.assertFalse(table.isSortingEnabled())
        self.tab._creation_result(dict(source_title='Category:A', target_title='Категория:А', status='linked',
                                       source_qid='Q17', target_qid='Q17'))
        index = next(i for i in range(table.rowCount()) if table.item(i, 0).text() == 'Category:A')
        self.assertEqual(('one', 'Q17', 'linked'),
                         (table.item(index, 2).text(), table.item(index, 3).text(), table.item(index, 5).data(Qt.UserRole)))
        self.tab._set_busy(False)
        self.assertTrue(table.isSortingEnabled())

    def test_busy_preparation_table_can_scroll_and_select_without_editing_or_filling(self):
        rows = tuple(CategoryCreationRow('Category:' + str(i), 'Категория:' + str(i), 'body') for i in range(400))
        self.tab._fill_creation_table(rows)
        tabs = self.parent.tabs = QTabWidget(self.parent)
        tabs.resize(1280, 850)
        tabs.addTab(self.tab, 'scan')
        tabs.addTab(self.tab.creation_stage, 'prepare')
        tabs.setCurrentWidget(self.tab.creation_stage)
        self.parent.show()
        table = self.tab.creation_table
        self.tab._set_busy(True)
        self.app.processEvents()
        self.assertTrue(table.isEnabled())
        self.assertTrue(table.verticalScrollBar().isEnabled())
        self.assertGreater(table.verticalScrollBar().maximum(), 0)
        self.assertEqual(QAbstractItemView.NoEditTriggers, table.editTriggers())
        self.assertFalse(table.isSortingEnabled())
        table.setCurrentCell(0, 2)
        self.assertTrue(table._handle_rect().isEmpty())
        before = table.verticalScrollBar().value()
        table.setFocus()
        QTest.keyClick(table, Qt.Key_PageDown)
        self.assertGreater(table.verticalScrollBar().value(), before)
        QTest.keyClick(table, Qt.Key_F2)
        QTest.keyClick(table, Qt.Key_Delete)
        self.assertNotEqual(QAbstractItemView.EditingState, table.state())
        self.assertEqual(rows, self.tab._table_creation_rows())
        self.assertFalse(self.tab.creation_history.undo_action.isEnabled())
        self.tab._set_busy(False)
        self.assertNotEqual(QAbstractItemView.NoEditTriggers, table.editTriggers())
        self.assertTrue(table.isSortingEnabled())

    def test_link_only_button_excludes_creation_rows_and_keeps_them_for_later(self):
        new, existing = self.rows
        self.parent.current_user, self.parent.current_password = 'User', 'unused'
        self.tab._creation_rows = self.rows
        self.tab._link_only_rows = (existing,)
        self.tab._preview_context = self.tab._creation_context()
        self.tab._set_busy(False)
        self.assertTrue(self.tab.link_only_btn.isEnabled())
        with patch.object(self.tab, '_confirm_creation', return_value=True), \
             patch('wiki_cat_tool.gui.tabs.category_transfer_tab.CategoryTransferCreateWorker') as factory:
            self.tab.start_linking_only()
            self.assertEqual((existing,), factory.call_args.kwargs['rows'])
            self.assertFalse(factory.call_args.kwargs['create_missing'])
            worker = factory.return_value
            worker.failed = worker._stop = False
            worker.stats = dict(created=0, linked=1, labels=0, exists=1, already=0, failed=0)
            worker.saved_edits = 1
            worker.linked_categories = [existing.target_title]
            self.tab._creation_result(dict(source_title=existing.source_title, target_title=existing.target_title,
                                           status='linked', qid='Q17', source_qid='Q17', target_qid='Q17'))
            with patch.object(self.tab, '_offer_sync', return_value=False):
                self.tab._creation_finished()
        self.assertEqual((new,), self.tab._creation_rows)
        self.assertEqual((), self.tab._link_only_rows)
        self.assertEqual('Q17', self.tab._linked_pairs[existing.target_title].qid)

    def test_log_broom_stays_inside_viewport_with_both_scrollbars(self):
        from wiki_cat_tool.gui.widgets.ui_helpers import create_log_wrap
        log = QTextEdit()
        log.setLineWrapMode(QTextEdit.NoWrap)
        wrap = create_log_wrap(self.parent, log)
        wrap.resize(340, 160)
        wrap.show()
        log.setPlainText('\n'.join('x' * 200 for _ in range(80)))
        self.app.processEvents()
        self.assertTrue(log.verticalScrollBar().isVisible())
        self.assertTrue(log.horizontalScrollBar().isVisible())
        self.assertIs(wrap.clear_button.parent(), log.viewport())
        self.assertLess(wrap.clear_button.geometry().right(), log.viewport().width())
        self.assertLess(wrap.clear_button.geometry().bottom(), log.viewport().height())
        wrap.clear_button.click()
        self.assertEqual('', log.toPlainText())
        wrap.close()
        wrap.deleteLater()

    def test_interwiki_sorting_all_columns_preserves_pairs_and_explicit_missing_status(self):
        tab = CategoryInterwikiTab(self.parent)
        tab._fill_rows(self.rows)
        tab._set_busy(False)
        for column in range(tab.table.columnCount()):
            tab.table.sortItems(column, Qt.DescendingOrder)
            self.assertEqual({(r.source_title, r.target_title) for r in self.rows},
                             {(r.source_title, r.target_title) for r in tab._rows()})
        tab._set_busy(True)
        self.assertFalse(tab.table.isSortingEnabled())
        index = next(i for i, r in enumerate(tab._rows()) if r.source_title == 'Category:A')
        tab._row_result(index, dict(status='target_missing', error='Category does not exist'))
        self.assertEqual('Нет категории на текущем языке', tab.table.item(index, 4).text())
        self.assertEqual('Category does not exist', tab.table.item(index, 4).toolTip())
        tab._set_busy(False)
        self.assertTrue(tab.table.isSortingEnabled())

    def test_checking_opens_log_and_shows_overall_progress_on_both_pages(self):
        from wiki_cat_tool.workers.category_transfer_worker import CategoryTransferPreviewWorker, CategoryInterwikiPreviewWorker
        from wiki_cat_tool.tests.test_category_transfer import FixtureReader, page
        subjects = [(self.tab, 'wiki_cat_tool.gui.tabs.category_transfer_tab.CategoryTransferPreviewWorker',
                     CategoryTransferPreviewWorker, 'preview_creation', 'preview_worker')]
        interwiki = CategoryInterwikiTab(self.parent)
        interwiki._fill_rows(self.rows)
        subjects.append((interwiki, 'wiki_cat_tool.gui.tabs.category_interwiki_tab.CategoryInterwikiPreviewWorker',
                         CategoryInterwikiPreviewWorker, 'preview', 'preview_worker'))
        for tab, factory_path, worker_type, start_name, worker_name in subjects:
            with self.subTest(page=tab.__class__.__name__):
                reader = FixtureReader()
                for row in self.rows:
                    reader.page_data['en'][row.source_title] = page(row.source_title, qid='Q1')
                    reader.page_data['ru'][row.target_title] = page(row.target_title, qid='Q1')
                reader.item_entities = {'Q1': {'labels': {'en': {'value': 'Category:A'}, 'ru': {'value': 'Категория:А'}}}}
                original_pages = reader.pages
                def slow_pages(*args, **kwargs):
                    import time
                    time.sleep(0.03)
                    return original_pages(*args, **kwargs)
                reader.pages = slow_pages
                worker = worker_type(rows=self.rows, source_lang='en', target_lang='ru', family='wikipedia')
                worker.reader.close()
                worker.reader = reader
                reader.on_activity = worker._preview_activity
                events = []
                worker.activity.connect(lambda payload: events.append(payload['phase']))
                if isinstance(tab, CategoryTransferTab):
                    tab._table_dirty = True
                with patch(factory_path, return_value=worker):
                    getattr(tab, start_name)()
                self.assertTrue(tab.log_panel.expanded)
                self.assertIn('Проверка связей запущена', tab.log.toPlainText())
                self.assertEqual(100, tab.bar.maximum())
                self.assertEqual(0, tab.bar.value())
                try:
                    for _ in range(200):
                        if getattr(tab, worker_name) is None:
                            break
                        QTest.qWait(5)
                finally:
                    from shiboken6 import isValid
                    if isValid(worker) and worker.isRunning():
                        worker.request_stop()
                        worker.wait(2000)
                self.assertIsNone(getattr(tab, worker_name))
                self.assertIn('check_pages', events)
                self.assertIn('check_sitelinks', events)
                self.assertEqual('check_results', events[-1])
                self.assertIn('Подводим итоги', tab.log.toPlainText())
                self.assertNotIn('Запрос ', tab.log.toPlainText())
                self.assertLessEqual(len(tab.log.toPlainText().splitlines()), 11)
                self.assertEqual(100, tab.bar.value())
                self.assertFalse(tab.stop_btn.isEnabled())

    def test_preview_percentage_is_not_overwritten_by_table_results(self):
        interwiki = CategoryInterwikiTab(self.parent)
        interwiki._fill_rows(self.rows)
        for tab in (self.tab, interwiki):
            tab._preview_activity(dict(percent=43, text='Проверка связей: 43%'))
            tab._preview_result(dict(index=0, row=self.rows[0], status='ready_link'))
            self.assertEqual(100, tab.bar.maximum())
            self.assertEqual(43, tab.bar.value())
            self.assertEqual('Проверка связей: 43%', tab.bar.text())

    def test_preview_worker_reports_eta_without_logging_each_request(self):
        from wiki_cat_tool.workers.category_transfer_worker import CategoryTransferPreviewWorker
        worker = CategoryTransferPreviewWorker(rows=self.rows, source_lang='en', target_lang='ru', family='wikipedia')
        self.addCleanup(worker.reader.close)
        activities, logs = [], []
        worker.activity.connect(activities.append)
        worker.progress.connect(logs.append)
        payload = dict(phase='check_pages', lang='en', count=538, processed=0, stage=2, stages=9, request=0)
        with patch('wiki_cat_tool.workers.category_transfer_worker.time.monotonic', side_effect=(100, 110, 111)):
            worker._preview_activity(payload)
            worker._preview_activity({**payload, 'processed': 150, 'request': 3})
            worker._preview_activity({**payload, 'processed': 200, 'request': 4})
        self.assertEqual(1, len(logs))
        self.assertNotIn('Запрос', ''.join(logs))
        self.assertIn('150/538', activities[1]['text'])
        self.assertIn('осталось ≈', activities[1]['text'])
        self.assertGreater(activities[1]['percent'], activities[0]['percent'])
        self.assertLess(activities[1]['percent'], 100)
        self.assertNotIn('осталось', activities[0]['text'])

    def test_cell_edits_and_row_deletion_can_be_undone_in_sequence(self):
        self.tab.creation_table.item(1, 1).setText('Категория:Другое')
        self.tab.creation_table.selectRow(1)
        self.tab.delete_creation_selected()
        self.assertEqual(1, self.tab.creation_table.rowCount())
        self.tab.creation_history.undo()
        self.assertEqual('Категория:Другое', self.tab.creation_table.item(1, 1).text())
        self.tab.creation_history.undo()
        self.assertEqual(self.rows, self.tab._table_creation_rows())
        self.tab.creation_history.redo()
        self.tab.creation_history.redo()
        self.assertEqual(1, self.tab.creation_table.rowCount())

    def test_scanning_clear_and_delete_restore_results_and_row_mapping(self):
        for name, count, status in [('A', 0, 'scanned'), ('B', None, 'error'), ('C', 4, 'scanned')]:
            self.tab._apply_row(dict(source_title='Category:' + name, depth=1, potential=count, status=status, qid='Q12'))
        self.tab.clear_empty()
        self.assertEqual(2, self.tab.table.rowCount())
        self.tab.scan_history.undo()
        self.assertEqual(3, self.tab.table.rowCount())
        self.assertEqual('error', next(r for r in self.tab._rows() if r['source_title'] == 'Category:B')['status'])
        self.tab._apply_row(dict(source_title='Category:C', depth=1, potential=5, status='scanned', qid='Q12'))
        self.assertEqual(3, self.tab.table.rowCount())
        self.assertEqual(5, next(r for r in self.tab._rows() if r['source_title'] == 'Category:C')['potential'])

    def test_history_is_disabled_while_a_worker_owns_the_table(self):
        self.tab._fill_content_rows([0], 'replacement', False)
        self.tab._set_busy(True)
        self.tab.creation_history.undo()
        self.assertEqual('replacement', self.tab.creation_table.item(0, 2).text())
        self.assertFalse(self.tab.creation_history.undo_action.isEnabled())
        self.tab._set_busy(False)
        self.tab.creation_history.undo()
        self.assertEqual('one', self.tab.creation_table.item(0, 2).text())

    def test_loading_source_text_is_a_single_undo_step(self):
        self.tab.source_content_empty.setChecked(False)
        with patch('wiki_cat_tool.gui.tabs.category_transfer_tab.CategoryTextReadWorker') as factory:
            worker = factory.return_value
            worker.failed = worker._stop = False
            worker.stats = dict(loaded=2, failed=0)
            self.tab.load_source_content()
            self.tab._source_content_ready(dict(index=0, content='foreign A'))
            self.tab._source_content_ready(dict(index=1, content='foreign B'))
            self.tab._source_content_finished()
        self.tab.creation_history.undo()
        self.assertEqual(self.rows, self.tab._table_creation_rows())
        self.tab.creation_history.redo()
        self.assertEqual(['foreign A', 'foreign B'], [r.content for r in self.tab._table_creation_rows()])

    def test_loading_other_input_clears_history(self):
        self.tab._fill_content_rows([0], 'replacement', False)
        self.tab._fill_creation_table((CategoryCreationRow('Category:C', '', 'new input'),))
        self.tab.creation_history.undo()
        self.assertEqual(1, self.tab.creation_table.rowCount())
        self.assertEqual('new input', self.tab.creation_table.item(0, 2).text())

    def test_file_choice_preserves_edits_until_open_and_loads_the_typed_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'список.tsv'
            path.write_text('Category:New\tКатегория:Новое\tnew text\n', encoding='utf-8')
            with patch('wiki_cat_tool.gui.widgets.ui_helpers.QFileDialog.getOpenFileName',
                       return_value=(str(path), 'TSV')):
                self.tab.browse_btn.click()
            self.assertEqual(str(path), self.tab.tsv_path.text())
            self.assertEqual(self.rows, self.tab._table_creation_rows())
            with patch('wiki_cat_tool.gui.widgets.ui_helpers.QFileDialog.getOpenFileName') as chooser:
                self.tab.preview_btn.click()
            chooser.assert_not_called()
        self.assertEqual((CategoryCreationRow('Category:New', 'Категория:Новое', 'new text'),),
                         self.tab._table_creation_rows())
        self.assertFalse(self.tab.create_btn.isEnabled())

    def test_import_on_visible_preparation_page_enables_actions_without_switching_tabs(self):
        import tempfile
        tabs = self.parent.tabs = QTabWidget(self.parent)
        tabs.resize(1280, 850)
        tabs.addTab(self.tab, 'scan')
        tabs.addTab(self.tab.creation_stage, 'prepare')
        tabs.setCurrentWidget(self.tab.creation_stage)
        self.tab._invalidate_creation()
        self.tab._fill_creation_table(())
        self.parent.show()
        self.app.processEvents()
        self.assertFalse(self.tab.check_links_btn.isEnabled())
        self.assertFalse(self.tab.autofill_btn.isEnabled())
        switches = []
        tabs.currentChanged.connect(switches.append)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'categories.tsv'
            path.write_text(''.join(f'Category:{i} establishments\tКатегория:{i}\tbody\n' for i in range(538)), encoding='utf-8')
            self.tab.tsv_path.setText(str(path))
            self.tab.preview_btn.click()
        self.assertEqual([], switches)
        self.assertIs(tabs.currentWidget(), self.tab.creation_stage)
        self.assertEqual(538, self.tab.creation_table.rowCount())
        for button in (self.tab.check_links_btn, self.tab.autofill_btn, self.tab.export_creation_btn,
                       self.tab.clear_all_creation_btn, self.tab.incomplete_btn, self.tab.number_titles_btn,
                       self.tab.fill_content_btn):
            self.assertTrue(button.isEnabled(), button.text())
        self.assertFalse(self.tab.create_btn.isEnabled())
        self.assertFalse(self.tab.link_only_btn.isEnabled())
        self.assertFalse(self.tab.sync_btn.isEnabled())

    def test_import_updates_actions_after_replacing_a_previously_checked_list(self):
        import tempfile
        self.tab._creation_rows = self.rows
        self.tab._link_only_rows = self.rows
        self.tab._preview_context = self.tab._creation_context()
        self.tab._linked_categories = [self.rows[0].target_title]
        self.tab._linked_context = self.tab._creation_context()
        self.tab._set_busy(False)
        self.assertTrue(self.tab.create_btn.isEnabled())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'categories.tsv'
            path.write_text('Category:New\tКатегория:Новое\ttext\n', encoding='utf-8')
            self.tab.tsv_path.setText(str(path))
            self.tab.preview_btn.click()
        self.assertTrue(self.tab.check_links_btn.isEnabled())
        self.assertTrue(self.tab.export_creation_btn.isEnabled())
        self.assertFalse(self.tab.create_btn.isEnabled())
        self.assertFalse(self.tab.link_only_btn.isEnabled())
        self.assertFalse(self.tab.sync_btn.isEnabled())

    def test_interwiki_file_open_loads_pairs_without_another_file_dialog(self):
        import tempfile
        tab = CategoryInterwikiTab(self.parent)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pairs.tsv'
            path.write_text('Category:A\tКатегория:А\n', encoding='utf-8')
            tab.tsv_path.setText(str(path))
            with patch('wiki_cat_tool.gui.widgets.ui_helpers.QFileDialog.getOpenFileName') as chooser:
                tab.open_btn.click()
            chooser.assert_not_called()
        self.assertEqual((CategoryCreationRow('Category:A', 'Категория:А', ''),), tab._rows())
        self.assertFalse(tab.link_btn.isEnabled())

    def test_progress_counters_and_final_status_are_visible_in_the_bar(self):
        from wiki_cat_tool.gui.widgets.ui_helpers import init_progress
        for tab, progress in ((self.tab, self.tab._set_progress),
                              (CategoryInterwikiTab(self.parent), None)):
            progress = progress or tab._progress
            init_progress(tab.progress_label, tab.bar, 3)
            tab._advance()
            self.assertEqual('Обработано 1/3', tab.bar.text())
            progress('Остановлено')
            self.assertEqual('Остановлено', tab.bar.text())
            self.assertEqual(1, tab.bar.value())
            self.assertTrue(tab.progress_label.isHidden())

    def test_loading_a_scan_report_cannot_undo_an_older_lists_deletion(self):
        import tempfile
        self.tab._apply_row(dict(source_title='Category:Old', depth=1, status='scanned', potential=0))
        self.tab.clear_empty()
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'report.tsv'
            report.write_text('a\tb\tc\td\te\tf\tg\nCategory:New\t\t\t0\t4\t2\tscanned\n', encoding='utf-8')
            with patch('wiki_cat_tool.gui.tabs.category_transfer_tab.QFileDialog.getOpenFileName',
                       return_value=(str(report), 'TSV')):
                self.tab.import_report()
        self.tab.scan_history.undo()
        self.assertEqual(['Category:New'], [r['source_title'] for r in self.tab._rows()])

    def test_scan_file_autodetection_preserves_preparation_and_rejects_invalid_files(self):
        import tempfile
        self.tab._apply_row(dict(source_title='Category:Old', depth=1, status='scanned', potential=5))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'list.txt'
            path.write_text('a\nCategory:B\na\n', encoding='utf-8-sig')
            self.tab.scan_report_path.setText(str(path))
            self.tab.open_scan_btn.click()
            self.assertCountEqual(['a', 'Category:B'], [row['source_title'] for row in self.tab._rows()])
            self.assertTrue(all(row['manual_input'] and row['status'] == 'loaded' for row in self.tab._rows()))
            invalid = Path(directory) / 'invalid.tsv'
            invalid.write_text('Category:C\tTarget\n', encoding='utf-8')
            self.tab.scan_report_path.setText(str(invalid))
            with patch.object(self.tab, '_warn') as warning:
                self.tab.open_scan_btn.click()
            warning.assert_called_once()
            self.assertEqual(2, self.tab.table.rowCount())
            report = Path(directory) / 'report.tsv'
            report.write_text('a\tb\tc\td\te\tf\tg\nCategory:C\t\t\t0\t4\t2\tscanned\n', encoding='utf-8')
            self.tab.scan_report_path.setText(str(report))
            self.tab.open_scan_btn.click()
            self.assertEqual(['Category:C'], [row['source_title'] for row in self.tab._rows()])
            self.assertEqual(2, self.tab._rows()[0]['potential'])
        self.assertEqual(self.rows, self.tab._table_creation_rows())

    def test_normalized_manual_scan_results_update_the_existing_row_without_duplicates(self):
        self.tab._append_scan_titles(('a',))
        self.tab._apply_row(dict(source_title='Category:A', input_source_title='a', depth=0,
                                 status='error', error='offline'))
        self.assertEqual(1, self.tab.table.rowCount())
        self.assertEqual(['Category:A'], [row['source_title'] for row in self.tab._rows()])

    def test_empty_table_cannot_start_writes_from_a_stale_verified_snapshot(self):
        self.tab._creation_rows = self.rows
        self.tab._preview_context = self.tab._creation_context()
        self.tab.creation_table.setRowCount(0)
        self.tab._set_busy(False)
        self.assertFalse(self.tab.create_btn.isEnabled())
        with patch('wiki_cat_tool.gui.tabs.category_transfer_tab.CategoryTransferCreateWorker') as create:
            self.tab.start_creation()
        create.assert_not_called()
        interwiki = CategoryInterwikiTab(self.parent)
        interwiki._prepared = self.rows
        interwiki._preview_context = interwiki._context()
        interwiki._set_busy(False)
        self.assertFalse(interwiki.link_btn.isEnabled())
        with patch('wiki_cat_tool.gui.tabs.category_interwiki_tab.CategoryInterwikiLinkWorker') as link:
            interwiki.start_linking()
        link.assert_not_called()

    def test_manual_list_addition_keeps_scanned_rows_and_is_one_undo_step(self):
        self.tab._apply_row(dict(source_title='Category:A', depth=1, status='scanned', potential=7))
        with patch('wiki_cat_tool.gui.tabs.category_transfer_tab.QInputDialog.getMultiLineText',
                   return_value=('Category:A\nCategory:B\nCategory:B\n', True)):
            self.tab.enter_scan_list()
        self.assertEqual(2, self.tab.table.rowCount())
        self.assertEqual(7, next(row['potential'] for row in self.tab._rows() if row['source_title'] == 'Category:A'))
        self.tab.scan_history.undo()
        self.assertEqual(['Category:A'], [row['source_title'] for row in self.tab._rows()])
        self.assertEqual(self.rows, self.tab._table_creation_rows())

    def test_scan_undo_uses_the_current_ui_language(self):
        self.tab._apply_row(dict(source_title='Category:A', depth=1, status='scanned', potential=0))
        self.tab.clear_empty()
        self.parent._ui_lang = 'en'
        self.tab.scan_history.undo()
        self.assertEqual(self.tab._t('scanned'), self.tab.table.item(0, 6).text())

    def test_async_fill_can_finish_after_its_view_has_closed(self):
        table = QTableWidget(1, 1)
        table.setItem(0, 0, QTableWidgetItem('old'))
        history = TableHistory(table)
        operation = history.transaction()
        operation.__enter__()
        table.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        operation.__exit__(None, None, None)

    def test_confirmation_shows_counts_for_actual_actions_and_skips(self):
        rows = self.rows + (CategoryCreationRow('Category:C', 'Категория:В', ''),)
        self.tab._fill_creation_table(rows)
        self.tab._set_busy(True)
        for index, (status, qid) in enumerate([('ready_create', ''), ('ready_labels', 'Q20'), ('already_linked', 'Q30')]):
            self.tab._preview_result(dict(index=index, row=rows[index], status=status,
                                          source_qid=qid, target_qid=qid))
        self.tab._set_busy(False)
        self.tab._creation_rows = rows[:2]
        counts = {}
        def inspect():
            dialog = self.app.activeModalWidget()
            for label in dialog.findChildren(QLabel):
                if label.objectName().startswith('planCount_'):
                    counts[label.objectName()] = label.text()
            dialog.reject()
        QTimer.singleShot(0, inspect)
        self.assertFalse(self.tab._confirm_creation())
        self.assertEqual({'planCount_create': '1', 'planCount_labels': '1', 'planCount_items': '1',
                          'planCount_complete': '1'}, counts)

    def test_new_links_are_separate_from_preexisting_links_in_summary_and_confirmation(self):
        rows = tuple(CategoryCreationRow('Category:' + str(i), 'Категория:' + str(i), 'body') for i in range(5))
        self.tab._fill_creation_table(rows)
        self.tab._set_busy(True)
        for index, row in enumerate(rows):
            self.tab._preview_result(dict(index=index, row=row, status='already_linked' if index < 2 else 'ready_link'))
        for row in rows[2:4]:
            self.tab._creation_result(dict(source_title=row.source_title, target_title=row.target_title,
                                           qid='Q42', status='linked'))
        self.assertIn('Связано: 2', self.tab.creation_summary.text())
        self.assertIn('Уже связано: 2', self.tab.creation_summary.text())
        self.assertNotIn('Связано: 4', self.tab.creation_summary.text())
        self.tab._set_busy(False)
        self.tab._creation_rows = (rows[4],)
        details = {}
        def inspect():
            dialog = self.app.activeModalWidget()
            details['complete'] = dialog.findChild(QLabel, 'planCount_complete').text()
            details['explanation'] = dialog.findChild(QLabel, 'planCompleteDetail').text()
            dialog.reject()
        QTimer.singleShot(0, inspect)
        self.tab._confirm_creation()
        self.assertEqual('4', details['complete'])
        self.assertEqual('Ранее связаны: 2 · Обработаны сейчас: 2', details['explanation'])

    def test_interwiki_summary_separates_existing_links_and_label_updates(self):
        tab = CategoryInterwikiTab(self.parent)
        rows = tuple(CategoryCreationRow('Category:' + str(i), 'Категория:' + str(i), '') for i in range(5))
        tab._fill_rows(rows)
        for index, status in enumerate(('already_linked', 'already_linked', 'linked', 'linked', 'labels_updated')):
            tab._row_result(index, dict(status=status, source_qid='Q' + str(index + 1), target_qid='Q' + str(index + 1)))
        self.assertIn('Связано: 2', tab.summary_label.text())
        self.assertIn('Уже связано: 2', tab.summary_label.text())
        self.assertIn('Метки: 1', tab.summary_label.text())

    def test_linked_row_counts_actual_label_changes_and_shows_languages(self):
        self.tab._set_busy(True)
        self.tab._creation_result(dict(source_title=self.rows[0].source_title, target_title=self.rows[0].target_title,
                                       status='linked', qid='Q1', label_changes={'added': ['ru']},
                                       detail='Метки добавлены: ru.'))
        self.tab._set_busy(False)
        self.assertIn('Метки: 1', self.tab.creation_summary.text())
        self.assertIn('Метки добавлены: ru.', self.tab.creation_table.item(0, 5).toolTip())

    def test_created_page_summary_uses_the_actual_wikidata_action(self):
        self.tab._set_busy(True)
        self.tab._creation_result(dict(source_title=self.rows[0].source_title, target_title=self.rows[0].target_title,
                                       status='created', qid='Q1', link_action='already_linked'))
        self.assertIn('Создано: 1', self.tab.creation_summary.text())
        self.assertIn('Уже связано: 1', self.tab.creation_summary.text())
        self.assertNotIn('Связано: 1', self.tab.creation_summary.text())
        self.tab._set_busy(False)
        self.tab.creation_table.item(0, 1).setText('Категория:Изменённое название')
        self.assertIn('Нужно проверить: 2', self.tab.creation_summary.text())
        self.assertNotIn('Уже связано:', self.tab.creation_summary.text())

    def test_interwiki_paste_edit_and_delete_have_local_undo(self):
        tab = CategoryInterwikiTab(self.parent)
        tab.load_pairs(self.rows, 'en')
        self.app.clipboard().setText('Category:C\tКатегория:В')
        tab.paste_pairs()
        tab.table.item(2, 1).setText('Категория:Другое')
        tab.table.selectRow(2)
        tab.delete_selected()
        tab.table_history.undo()
        self.assertEqual('Категория:Другое', tab._rows()[2].target_title)
        tab.table_history.undo()
        self.assertEqual('Категория:В', tab._rows()[2].target_title)
        tab.table_history.undo()
        self.assertEqual(2, tab.table.rowCount())
        self.assertFalse(tab.link_btn.isEnabled())

    def test_completed_interwiki_pairs_can_go_to_sync_without_a_noop_run(self):
        tab = CategoryInterwikiTab(self.parent)
        tab.load_pairs(self.rows[:1], 'en')
        result = dict(index=0, row=tab._rows()[0], status='already_linked', source_qid='Q1', target_qid='Q1')
        tab.preview_worker = Mock(failed=False, _stop=False, results=[result])
        tab._preview_context = tab._context()
        tab._preview_result(result)
        tab._preview_finished()
        self.assertFalse(tab.link_btn.isEnabled())
        self.assertTrue(tab.sync_btn.isEnabled())

    def test_conflicts_remain_in_report_without_entering_the_write_plan(self):
        tab = CategoryInterwikiTab(self.parent)
        tab.load_pairs(self.rows[:1], 'en')
        result = dict(index=0, row=tab._rows()[0], status='different_items', source_qid='Q1', target_qid='Q2')
        tab.preview_worker = Mock(failed=False, _stop=False, results=[result])
        tab._preview_context = tab._context()
        tab._preview_result(result)
        tab._preview_finished()
        self.assertFalse(tab.link_btn.isEnabled())
        self.assertTrue(tab.report_btn.isEnabled())

    def test_sync_deletion_can_restore_rows_and_the_valid_preview(self):
        tab = CategoryContentSyncTab(self.parent)
        tab.preview_table.setRowCount(1)
        for column, value in enumerate(('Category:A', 'Категория:А', 'Q1', '5', '0', 'ready')):
            tab.preview_table.setItem(0, column, QTableWidgetItem(value))
        tab._preview_ready = True
        tab._preview_signature = ('test signature',)
        tab.preview_table.selectRow(0)
        tab.delete_selected_preview_rows()
        self.assertFalse(tab._preview_ready)
        tab.preview_history.undo()
        self.assertEqual(1, tab.preview_table.rowCount())
        self.assertEqual('Категория:А', tab.preview_table.item(0, 1).text())
        self.assertEqual({'категория:а': 0}, tab._preview_row_index)
        self.assertTrue(tab._preview_ready)
        self.assertEqual(('test signature',), tab._preview_signature)

    def test_shared_side_log_follows_stage_without_replacing_either_list(self):
        self.tab.log.setPlainText('kept log')
        self.tab._apply_row(dict(source_title='Category:A', depth=1, potential=4, status='scanned', qid='Q1'))
        self.tab._show_stage(self.tab)
        self.tab.log_toggle.setChecked(True)
        self.assertIs(self.tab.log_panel.parentWidget(), self.tab.scan_workspace)
        self.assertIs(self.tab.log_toggle.parentWidget(), self.tab.scan_table_toolbar)
        self.tab._show_stage(self.tab.creation_stage)
        self.assertIs(self.tab.log_panel.parentWidget(), self.tab.creation_workspace)
        self.assertIs(self.tab.log_toggle.parentWidget(), self.tab.creation_table_toolbar)
        self.assertIs(self.tab.footer.parentWidget(), self.tab.creation_stage)
        self.assertTrue(self.tab.log_panel.expanded)
        self.assertEqual('kept log', self.tab.log.toPlainText())
        self.assertEqual(self.rows, self.tab._table_creation_rows())
        self.tab._show_stage(self.tab)
        self.assertEqual(1, self.tab.table.rowCount())
        self.assertIs(self.tab.log_panel.parentWidget(), self.tab.scan_workspace)

    def test_collapsing_side_log_keeps_its_contents_and_table_edits(self):
        self.tab.log.setPlainText('kept log')
        self.tab.log_toggle.setChecked(True)
        self.assertFalse(self.tab.log_panel.isHidden())
        self.tab.log_toggle.setChecked(False)
        self.assertTrue(self.tab.log_panel.isHidden())
        self.assertEqual('kept log', self.tab.log.toPlainText())
        self.assertEqual(self.rows, self.tab._table_creation_rows())

    def test_sync_preview_and_edit_list_are_separate_without_losing_preview(self):
        tab = CategoryContentSyncTab(self.parent)
        tab.preview_table.setRowCount(1)
        for column, value in enumerate(('Category:A', 'Категория:А', 'Q1', '5', '0', 'ready')):
            tab.preview_table.setItem(0, column, QTableWidgetItem(value))
        tab._preview_ready = True
        with patch.object(tab, '_is_preview_signature_actual', return_value=True):
            tab._set_preview_mode(True)
            tab.preview_or_toggle_settings()
            self.assertTrue(tab._preview_mode)
            tab._show_category_inputs()
            self.assertFalse(tab._preview_mode)
            self.assertTrue(tab._preview_ready)
            tab.preview_or_toggle_settings()
            self.assertTrue(tab._preview_mode)
            self.assertEqual(1, tab.preview_table.rowCount())

    def test_sync_view_tabs_switch_without_discarding_data_or_rescanning(self):
        tab = CategoryContentSyncTab(self.parent)
        self.assertFalse(tab.view_tabs.isTabEnabled(1))
        tab.source_panel.manual_list.setPlainText('Категория:А')
        tab.preview_table.setRowCount(1)
        tab.preview_table.setItem(0, 0, QTableWidgetItem('Category:A'))
        tab._preview_ready = True
        tab._set_preview_mode(True)
        with patch.object(tab, 'start_preview') as scan:
            tab.view_tabs.setCurrentIndex(0)
            self.assertFalse(tab._preview_mode)
            self.assertEqual(0, tab.left_stack.currentIndex())
            tab.view_tabs.setCurrentIndex(1)
            self.assertTrue(tab._preview_mode)
            self.assertEqual(1, tab.left_stack.currentIndex())
            self.assertEqual('Категория:А', tab.source_panel.manual_list.toPlainText())
            self.assertEqual('Category:A', tab.preview_table.item(0, 0).text())
            scan.assert_not_called()

    def test_running_sync_cannot_switch_into_an_editable_list(self):
        tab = CategoryContentSyncTab(self.parent)
        tab._set_preview_mode(True)
        tab.sync_worker = Mock()
        tab.sync_worker.isRunning.return_value = True
        tab.view_tabs.setCurrentIndex(0)
        self.assertTrue(tab._preview_mode)
        self.assertEqual(1, tab.view_tabs.currentIndex())
        self.assertEqual(1, tab.left_stack.currentIndex())
        tab.sync_worker = None

    def test_append_read_preserves_scanned_results_and_can_be_undone(self):
        original = dict(source_title='Category:A', depth=1, status='scanned', total=12, potential=5)
        self.tab._apply_row(original)
        self.tab.root_edit.setText('Category:Root')
        worker = CategoryTransferReadWorker(source_lang='en', target_lang='ru', family='wikipedia')
        self.addCleanup(worker.reader.close)
        with patch('wiki_cat_tool.gui.tabs.category_transfer_tab.CategoryTransferReadWorker', return_value=worker), \
             patch.object(worker, 'start'):
            self.tab.load_categories(append=True)
        self.tab._apply_row(dict(source_title='Category:A', depth=1, status='loaded'))
        self.tab._apply_row(dict(source_title='Category:B', depth=1, status='loaded'))
        self.assertEqual(original, self.tab._rows()[0])
        self.assertEqual(2, self.tab.table.rowCount())
        self.tab._read_finished()
        self.assertEqual(self.rows, self.tab._table_creation_rows())
        self.tab.scan_history.undo()
        self.assertEqual([original], self.tab._rows())
        self.tab.scan_history.redo()
        self.assertCountEqual(['Category:A', 'Category:B'], [row['source_title'] for row in self.tab._rows()])

    def test_scan_starts_at_depth_zero_and_read_uses_the_source_language(self):
        self.assertEqual(0, self.tab.depth_spin.value())
        self.tab.source_lang.setText('de')
        self.tab.root_edit.setText('Kategorie:A')
        with patch('wiki_cat_tool.gui.tabs.category_transfer_tab.CategoryTransferReadWorker') as worker_type, \
             patch.object(self.tab, '_start_read'):
            self.tab.load_btn.click()
        self.assertEqual('de', worker_type.call_args.kwargs['source_lang'])
        self.assertEqual('ru', worker_type.call_args.kwargs['target_lang'])
        self.assertEqual(0, worker_type.call_args.kwargs['depth'])
        self.assertEqual('Kategorie:A', worker_type.call_args.kwargs['category'])

    def test_settings_help_explains_modes_and_updates_with_the_ui_language(self):
        sync = CategoryContentSyncTab(self.parent)
        with patch('wiki_cat_tool.gui.widgets.ui_helpers.show_help_dialog') as show_help:
            self.tab.scan_settings_help_btn.click()
            self.assertIn('Пропускать связанные', show_help.call_args.args[1])
            self.tab.scan_read_help_btn.click()
            self.assertIn('исходному языку', show_help.call_args.args[1])
            sync.settings_help_btn.click()
            self.assertIn('Глубина переноса', show_help.call_args.args[1])
            self.parent._ui_lang = 'en'
            self.tab.scan_settings_help_btn.click()
            self.assertIn('Skip linked at level', show_help.call_args.args[1])
            sync.settings_help_btn.click()
            self.assertIn('Transfer depth', show_help.call_args.args[1])

    def test_scan_continues_only_unfinished_rows_and_reset_is_undoable(self):
        self.tab._apply_row(dict(source_title='Category:Done', depth=1, status='scanned',
                                 potential=1, total=1, target_articles=['Article']))
        self.tab._apply_row(dict(source_title='Category:Pending', depth=1, status='loaded'))
        self.tab._apply_row(dict(source_title='Category:Error', depth=1, status='error', error='offline'))
        self.tab._set_busy(False)
        self.assertEqual('Продолжить сканирование', self.tab.scan_btn.text())
        with patch('wiki_cat_tool.gui.tabs.category_transfer_tab.CategoryTransferReadWorker') as worker_type, \
             patch.object(self.tab, '_start_read'):
            self.tab.scan_categories()
        self.assertCountEqual(['Category:Pending', 'Category:Error'],
                              [row['source_title'] for row in worker_type.call_args.kwargs['categories']])
        before = self.tab._rows()
        self.tab.reset_scan_btn.click()
        self.assertEqual(3, self.tab.table.rowCount())
        self.assertTrue(all(row['status'] == 'loaded' and row.get('potential') is None
                            and 'target_articles' not in row and 'error' not in row for row in self.tab._rows()))
        self.assertEqual(self.rows, self.tab._table_creation_rows())
        self.tab.scan_history.undo()
        self.assertEqual(before, self.tab._rows())

    def test_available_click_opens_cached_articles_while_main_processing_is_busy(self):
        self.tab._apply_row(dict(source_title='Category:Done', depth=1, status='scanned',
                                 potential=1, total=1, target_articles=['Article']))
        self.tab._set_busy(True)
        dialog = Mock()
        dialog.articles = ('Article',)
        with patch('wiki_cat_tool.gui.tabs.category_transfer_tab.CategoryArticlesDialog', return_value=dialog) as create_dialog, \
             patch('wiki_cat_tool.gui.tabs.category_transfer_tab.CategoryArticlePresenceWorker') as create_worker:
            self.tab.table.cellClicked.emit(0, 5)
        create_dialog.assert_called_once()
        self.assertEqual(['Article'], create_dialog.call_args.args[0]['target_articles'])
        dialog.exec.assert_called_once()
        create_worker.assert_not_called()
        self.tab._set_busy(False)

    def test_finishing_an_older_article_check_does_not_clear_the_newer_one(self):
        older, newer = Mock(), Mock()
        self.tab._article_workers.update((older, newer))
        self.tab.article_worker = newer
        self.tab._article_check_finished(older)
        self.assertIs(newer, self.tab.article_worker)
        self.assertEqual({newer}, self.tab._article_workers)
        self.tab._stop_article_check()
        newer.request_stop.assert_called_once()
        self.tab._article_check_finished(newer)
        self.assertIsNone(self.tab.article_worker)

    def test_inline_parameters_stay_visible_when_log_is_toggled(self):
        self.assertFalse(self.tab.settings.isHidden())
        self.assertFalse(self.tab.log_panel.expanded)
        self.tab.log.setPlainText('kept')
        self.tab.log_toggle.setChecked(True)
        self.tab.log_toggle.setChecked(False)
        self.assertTrue(self.tab.log_panel.isHidden())
        self.assertFalse(self.tab.settings.isHidden())
        self.assertEqual('kept', self.tab.log.toPlainText())
        self.assertEqual(self.rows, self.tab._table_creation_rows())

    def test_clear_all_is_undoable_and_does_not_clear_the_other_stage(self):
        self.tab._apply_row(dict(source_title='Category:Scan', depth=1, status='loaded'))
        self.tab.clear_scan()
        self.assertEqual(0, self.tab.table.rowCount())
        self.assertEqual(self.rows, self.tab._table_creation_rows())
        self.tab.scan_history.undo()
        self.assertEqual(1, self.tab.table.rowCount())
        self.tab._creation_rows = self.rows
        self.tab._linked_categories = ['Target']
        self.tab._preview_context = self.tab._linked_context = self.tab._creation_context()
        self.tab.clear_creation()
        self.assertEqual(0, self.tab.creation_table.rowCount())
        self.assertEqual(1, self.tab.table.rowCount())
        self.assertFalse(self.tab.create_btn.isEnabled())
        self.assertFalse(self.tab.sync_btn.isEnabled())
        self.tab.creation_history.undo()
        self.assertEqual(self.rows, self.tab._table_creation_rows())
        self.assertFalse(self.tab.create_btn.isEnabled())

    def test_reading_progress_has_text_and_advances_with_packets(self):
        self.tab._read_mode = 'load'
        self.tab._read_count = self.tab._read_total = 0
        self.tab._read_progress({'phase': 'reading', 'count': 0, 'category': 'Category:Root'})
        self.assertEqual(0, self.tab.bar.maximum())
        self.assertTrue(self.tab.bar.format())
        self.tab._read_progress({'phase': 'members', 'count': 25, 'total': 50, 'category': 'Category:Root'})
        self.assertEqual(50, self.tab.bar.maximum())
        self.assertEqual(25, self.tab.bar.value())
        self.assertIn('25/50', self.tab.bar.format())
        self.tab._read_progress({'phase': 'links', 'count': 25, 'checked': 10, 'check_total': 25})
        self.assertEqual(25, self.tab.bar.value())
        self.assertIn('10/25', self.tab.bar.format())

    def test_sync_file_open_loads_first_column_and_keeps_curated_input_until_open(self):
        import tempfile
        tab = CategoryContentSyncTab(self.parent)
        tab.source_panel.manual_list.setPlainText('Category:Manual')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'categories.tsv'
            path.write_text('Category:A\nCategory:B\n', encoding='utf-8-sig')
            tab.source_panel.in_path.setText(str(path))
            self.assertEqual(['Category:Manual'], tab._load_categories_from_inputs())
            tab.source_panel.file_row.open_btn.click()
            self.assertEqual(['Category:A', 'Category:B'], tab._load_categories_from_inputs())
        tab.source_panel.manual_list.setPlainText('  \n')
        tab.cat_edit.setText('Category:Fallback')
        self.assertEqual(['Category:Fallback'], tab._load_categories_from_inputs())

    def test_sync_working_file_restores_targets_and_source_pairs(self):
        import tempfile
        from wiki_cat_tool.core.category_transfer import CategoryPair
        tab = CategoryContentSyncTab(self.parent)
        tab.source_lang_edit.setText('en')
        tab.cat_edit.setText('Категория:Старое')
        tab.source_panel.manual_list.setPlainText('Категория:Вручную')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'categories.tsv'
            path.write_text('Category:A\tКатегория:А\tbody\nCategory:B\tКатегория:Б\n', encoding='utf-8-sig')
            tab.source_panel.in_path.setText(str(path))
            tab._open_sync_category_file()
        self.assertEqual(['Категория:А', 'Категория:Б'], tab._load_categories_from_inputs())
        self.assertEqual('', tab.cat_edit.text())
        self.assertEqual((CategoryPair('Category:A', 'Категория:А', ''), CategoryPair('Category:B', 'Категория:Б', '')),
                         tab._active_transfer_pairs(tab._load_categories_from_inputs(), 'en', 'ru', 'wikipedia'))

    def test_empty_sync_file_does_not_replace_input_with_a_stale_root(self):
        import tempfile
        tab = CategoryContentSyncTab(self.parent)
        tab.source_panel.manual_list.setPlainText('Категория:Вручную')
        tab.cat_edit.setText('Категория:Старое')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'categories.tsv'
            path.write_text('Category:A\t\n', encoding='utf-8')
            tab.source_panel.in_path.setText(str(path))
            with patch('wiki_cat_tool.gui.tabs.category_content_sync_tab.QMessageBox.warning') as warning:
                tab._open_sync_category_file()
            warning.assert_called_once()
        self.assertEqual(['Категория:Вручную'], tab._load_categories_from_inputs())

    def test_hidden_log_frees_the_whole_working_width_and_processing_opens_it(self):
        self.parent.resize(1280, 900)
        self.tab.resize(1280, 900)
        self.parent.show()
        self.tab.show()
        self.app.processEvents()
        workspace = self.tab.scan_workspace
        self.assertEqual(workspace.width(), workspace.widget(0).width())
        self.tab._set_busy(True)
        self.app.processEvents()
        self.assertTrue(self.tab.log_toggle.isChecked())
        self.assertTrue(self.tab.log_panel.isVisible())
        self.assertLess(workspace.widget(0).width(), workspace.width() - 200)
        self.tab.log_toggle.setChecked(False)
        self.app.processEvents()
        self.assertEqual(workspace.width(), workspace.widget(0).width())
        self.assertTrue(self.tab.log_toggle.isVisible())
        self.tab._set_busy(True)
        self.assertFalse(self.tab.log_panel.expanded)
        self.tab._set_busy(False)

    def test_interwiki_processing_opens_the_same_external_log_control(self):
        tab = CategoryInterwikiTab(self.parent)
        self.assertFalse(tab.log_panel.expanded)
        self.assertIs(tab.log_btn.parentWidget(), tab.table_toolbar)
        tab._set_busy(True)
        self.assertTrue(tab.log_panel.expanded)
        self.assertTrue(tab.log_btn.isChecked())
        tab._set_busy(False)

    def test_sync_category_reading_opens_log_without_changing_parameters(self):
        tab = CategoryContentSyncTab(self.parent)
        self.assertFalse(tab.log_panel.expanded)
        tab.source_panel.fetch_started.emit()
        self.assertTrue(tab.log_panel.expanded)
        self.assertTrue(tab.log_panel.toggle.isChecked())
        self.assertFalse(tab.settings.isHidden())

    def test_table_shortcut_preserves_cell_editors_own_undo(self):
        table = QTableWidget(1, 1)
        table.setItem(0, 0, QTableWidgetItem('before'))
        history = TableHistory(table, editable_columns=(0,))
        self.addCleanup(table.close)
        table.show()
        table.item(0, 0).setText('committed')
        table.setCurrentCell(0, 0)
        table.setFocus()
        self.app.processEvents()
        QTest.keyClick(table, Qt.Key_Z, Qt.ControlModifier)
        self.assertEqual('before', table.item(0, 0).text())
        history.redo()
        table.editItem(table.item(0, 0))
        self.app.processEvents()
        editor = table.findChild(QLineEdit)
        editor.selectAll()
        QTest.keyClicks(editor, 'buffer change')
        QTest.keyClick(editor, Qt.Key_Z, Qt.ControlModifier)
        self.assertEqual('committed', editor.text())
        self.assertEqual('committed', table.item(0, 0).text())


class CategoryPlanTests(unittest.TestCase):
    def test_action_plan_separates_label_changes_new_items_and_unprocessed_rows(self):
        results = [dict(source_title='new', status='ready_create'),
                   dict(source_title='link', status='ready_link', source_qid='Q1'),
                   dict(source_title='labels', status='ready_labels', source_qid='Q2', target_qid='Q2'),
                   dict(source_title='done', status='already_linked'),
                   dict(source_title='draft', status='needs_content'),
                   dict(source_title='error', status='error'),
                   dict(source_title='conflict', status='different_items')]
        plan = category_action_plan(results, ['new', 'link', 'labels'])
        self.assertEqual(dict(create=1, link=1, labels=1, items=1, complete=1, drafts=1, errors=1, conflicts=1, retry=0), plan)

    def test_retry_plan_does_not_pretend_the_unknown_action_is_a_new_item(self):
        plan = category_action_plan([dict(source_title='failed', status='unlinked')], ['failed'])
        self.assertEqual(1, plan['retry'])
        self.assertEqual(0, plan['items'])


class ArticlePresenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def worker(self):
        worker = CategoryArticlePresenceWorker(articles=['А', 'Б', 'А'], category='Категория:Тема',
                                              source_lang='en', target_lang='ru', family='wikipedia')
        worker.reader.close()
        worker.reader = Mock()
        worker.reader.category_title.return_value = 'Категория:Тема'
        worker.reader.pages.return_value = {'Категория:Тема': dict(title='Категория:Тема', ns=14)}
        worker.reader.valid_page.return_value = True
        worker.reader.members.return_value = ['А', 'Other']
        return worker

    def test_counts_membership_on_the_current_wiki_and_deduplicates_candidates(self):
        worker = self.worker()
        result = []
        worker.result_ready.connect(result.append)
        worker.run()
        self.assertEqual([[('А', True), ('Б', False)]], result)
        worker.reader.members.assert_called_once_with('Категория:Тема', 'page', lang='ru')
        worker.reader.close.assert_called_once()

    def test_failed_membership_read_does_not_report_zero_existing_articles(self):
        worker = self.worker()
        worker.reader.members.side_effect = RuntimeError('API failed')
        result = []
        worker.result_ready.connect(result.append)
        worker.run()
        self.assertEqual([], result)
        self.assertTrue(worker.failed)
        self.assertEqual('API failed', worker.failure_message)

    def test_old_scan_report_fetches_its_article_list_on_demand(self):
        worker = self.worker()
        worker.articles = None
        worker.category = ''
        worker.source_category = 'Category:Topic'
        worker.reader.category_title.return_value = 'Category:Topic'
        worker.reader.pages.return_value = {'Category:Topic': dict(title='Category:Topic', ns=14)}
        worker.reader.article_matches.return_value = {'Source A': 'А', 'Source B': 'Б', 'Source C': 'А'}
        result = []
        worker.result_ready.connect(result.append)
        worker.run()
        self.assertEqual([[('А', None), ('Б', None)]], result)
        worker.reader.members.assert_called_once_with('Category:Topic', 'page')
        worker.reader.pages.assert_called_once_with('en', ['Category:Topic'], props='info')

    def test_article_dialog_keeps_main_table_compact_and_shows_additional_count(self):
        parent = QWidget()
        parent._ui_lang = 'ru'
        tab = CategoryTransferTab(parent)
        dialog = CategoryArticlesDialog(dict(source_title='Category:A', target_title='Категория:А',
                                             target_articles=['А', 'Б']), language='ru', family='wikipedia',
                                        translate=tab._t, parent=parent)
        dialog.show_membership([('А', True), ('Б', False)])
        self.assertIn('Найдено: 2 · Уже в категории: 1 · Можно добавить: 1', dialog.summary.text())
        self.assertEqual('Да', dialog.table.item(0, 1).text())
        self.assertEqual('Нет', dialog.table.item(1, 1).text())
        parent.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)


if __name__ == '__main__':
    unittest.main()
