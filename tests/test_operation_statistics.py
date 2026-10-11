from __future__ import annotations

# ruff: noqa: E402
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from wiki_cat_tool.gui.main_window import MainWindow
from wiki_cat_tool.gui.dialogs.update_dialog import UpdateDialog
from wiki_cat_tool.gui.tabs.category_interwiki_tab import CategoryInterwikiTab
from wiki_cat_tool.gui.tabs.category_transfer_tab import CategoryTransferTab
from wiki_cat_tool.gui.tabs.overview_tab import OverviewTab


class OperationStatisticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.parent = QWidget()
        self.parent._ui_lang = 'ru'
        self.parent.current_lang = 'ru'
        self.parent.current_family = 'wikipedia'
        self.parent.record_operation = Mock()
        self.addCleanup(self._dispose)

    def _dispose(self):
        self.parent.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_new_operations_persist_and_writes_are_counted_once(self):
        window = SimpleNamespace(_operation_stats={'edit_pages_total': 7}, _operation_history=[],
                                 _save_ui_settings=Mock(), overview_tab=Mock())
        MainWindow.record_operation(window, 'transfer_scan', 12)
        MainWindow.record_operation(window, 'transfer_prepare', 3)
        MainWindow.record_operation(window, 'interwiki', 1)
        self.assertEqual(12, window._operation_stats['transfer_scan_categories_total'])
        self.assertEqual(3, window._operation_stats['transfer_prepare_edits_total'])
        self.assertEqual(1, window._operation_stats['interwiki_edits_total'])
        self.assertEqual(11, window._operation_stats['edit_pages_total'])
        self.assertEqual(3, window._save_ui_settings.call_count)
        self.assertEqual(['transfer_scan', 'transfer_prepare', 'interwiki'],
                         [row['op'] for row in window._operation_history])
        MainWindow.reset_operation_stats(window)
        self.assertFalse(any(window._operation_stats.values()))
        self.assertEqual([], window._operation_history)

    def test_update_dialog_displays_short_version(self):
        dialog = UpdateDialog('2.10', '2.10', 'https://example.org/2.10', self.parent)
        text = ' '.join(label.text() for label in dialog.findChildren(QLabel))
        self.assertIn('2.1', text)
        self.assertNotIn('2.10', text)

    def test_overview_supports_old_stats_and_localizes_new_operations(self):
        tab = OverviewTab(self.parent)
        tab.update_stats({'create_edits_total': 4}, [])
        self.assertEqual('4', tab._operation_values['create'].text())
        self.assertEqual('0', tab._operation_values['transfer_prepare'].text())
        stats = {'transfer_scan_categories_total': 9, 'transfer_prepare_edits_total': 3,
                 'interwiki_edits_total': 2, 'edit_pages_total': 5}
        history = [dict(op=op, count=count, ts='2026-10-11') for op, count in
                   [('transfer_scan', 9), ('transfer_prepare', 3), ('interwiki', 2)]]
        tab.update_stats(stats, history)
        self.assertEqual('9', tab._operation_values['transfer_scan'].text())
        self.assertEqual('3', tab._operation_values['transfer_prepare'].text())
        self.assertEqual('5', tab.stat_edit_value.text())
        self.assertIn('Сканирование: +9', tab.history_list.item(0).text())
        tab.set_ui_language('en')
        tab.update_stats(stats, history)
        self.assertEqual('Preparation', tab._bar_title_labels['transfer_prepare'].text())
        self.assertIn('Preparation: +3', tab.history_list.item(1).text())
        self.assertEqual(8, len(tab._operation_values))

    def test_stopped_scan_counts_only_successes_from_this_worker(self):
        tab = CategoryTransferTab(self.parent)
        # An imported result already in the table must not be counted again.
        tab._apply_row(dict(source_title='Category:Old', depth=0, status='scanned', potential=1))
        tab.read_worker = SimpleNamespace(failed=False, _stop=True, error_count=1,
                                         results=[{'status': 'scanned'}, {'status': 'error'}],
                                         deleteLater=Mock())
        tab._read_finished()
        self.parent.record_operation.assert_called_once_with('transfer_scan', 1)

    def test_preparation_counts_writes_even_if_linking_failed_after_creation(self):
        tab = CategoryTransferTab(self.parent)
        tab.create_worker = SimpleNamespace(failed=False, _stop=False, saved_edits=1,
                                           stats=dict(created=1, linked=0, labels=0, exists=0, failed=1),
                                           linked_categories=[], deleteLater=Mock())
        tab._creation_finished()
        self.parent.record_operation.assert_called_once_with('transfer_prepare', 1)

    def test_interwiki_label_only_write_counts_but_noop_does_not(self):
        tab = CategoryInterwikiTab(self.parent)
        for writes in (1, 0):
            tab.link_worker = SimpleNamespace(failed=False, _stop=False, saved_edits=writes,
                                             stats=dict(linked=0, labels=writes, already=1,
                                                        conflicts=0, items=0, failed=0), deleteLater=Mock())
            tab._link_finished()
        self.parent.record_operation.assert_called_once_with('interwiki', 1)

    def test_interwiki_preview_does_not_record_writes(self):
        tab = CategoryInterwikiTab(self.parent)
        tab.preview_worker = SimpleNamespace(failed=False, _stop=False, results=[], deleteLater=Mock())
        tab._preview_finished()
        self.parent.record_operation.assert_not_called()


if __name__ == '__main__':
    unittest.main()
