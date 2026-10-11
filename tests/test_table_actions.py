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
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QTableWidget, QTableWidgetItem, QWidget

from wiki_cat_tool.core.category_transfer import CategoryCreationRow, category_action_plan
from wiki_cat_tool.gui.tabs.category_content_sync_tab import CategoryContentSyncTab
from wiki_cat_tool.gui.tabs.category_interwiki_tab import CategoryInterwikiTab
from wiki_cat_tool.gui.tabs.category_transfer_tab import CategoryTransferTab
from wiki_cat_tool.gui.widgets.category_articles import CategoryArticlesDialog
from wiki_cat_tool.gui.widgets.table_history import TableHistory
from wiki_cat_tool.workers.category_transfer_worker import CategoryArticlePresenceWorker


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
        self.tab._show_stage(self.tab.creation_stage)
        self.assertIs(self.tab.log_panel.parentWidget(), self.tab.creation_workspace)
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
        self.assertFalse(self.tab.log_wrap.isHidden())
        self.tab.log_toggle.setChecked(False)
        self.assertTrue(self.tab.log_wrap.isHidden())
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
