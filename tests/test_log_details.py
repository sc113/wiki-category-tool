"""Long rename failures stay readable without translating API text or page names."""

from __future__ import annotations

# ruff: noqa: E402
import html
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pywikibot
from PySide6.QtCore import QEvent, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QPlainTextEdit, QPushButton, QWidget

from wiki_cat_tool.gui.main_window import MainWindow
from wiki_cat_tool.gui.tabs.rename_tab import RenameTab
from wiki_cat_tool.gui.widgets.ui_helpers import LOG_DETAILS_ROLE, init_log_tree, log_tree_add, log_tree_add_event
from wiki_cat_tool.workers.rename_worker import RenameWorker


class LogErrorDetailsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.parent = QWidget()
        self.parent._ui_lang = 'ru'
        self.parent.translate_ui_text = lambda text: text.replace('Page', 'Страница').replace('Save', 'Сохранить')
        self.tree = init_log_tree(self.parent)
        self.addCleanup(self.dispose)

    def dispose(self):
        self.parent.close()
        self.parent.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)

    def test_full_error_tooltip_and_api_identifiers_survive_language_refresh(self):
        message = ('OtherPageSaveError: API rejected the edit\n' + 'Detailed reason. ' * 180).rstrip()
        title = 'Category:Page Save'
        log_tree_add_event(self.tree, dict(type='operation_error', title=title, message=message,
                                           details='APIError: protectedpage <permission denied>'))
        row = self.tree.topLevelItem(0)
        self.assertIn(message, html.unescape(row.toolTip(3)).replace('<br/>', '\n'))
        self.assertIn(message, row.text(3))
        self.assertIn(title, row.text(4))
        fake_window = SimpleNamespace(_ui_lang='ru', findChildren=self.parent.findChildren,
                                      _translate_value=lambda value, target: value.replace('Page', 'Страница'))
        MainWindow._apply_widget_text_translation(fake_window)
        self.assertIn('OtherPageSaveError', row.text(3))
        self.assertIn(title, row.text(4))
        self.assertEqual(message, row.data(3, LOG_DETAILS_ROLE)['message'])

    def test_double_click_opens_wrapped_selectable_details_and_copy_keeps_every_character(self):
        message = ('Не удалось сохранить: protectedpage\n' + 'Причина <from API> & details. ' * 160).rstrip()
        details = 'OtherPageSaveError: Edit failed:\nprotectedpage\nDo not translate Page or Save.'
        log_tree_add_event(self.tree, dict(type='operation_error', title='Category:Page', message=message, details=details))
        seen = {}
        def inspect():
            dialog = self.app.activeModalWidget()
            try:
                seen['name'] = dialog.objectName()
                editor = dialog.findChild(QPlainTextEdit, 'logErrorDetailsText')
                seen.update(readonly=editor.isReadOnly(), text=editor.toPlainText(), wrap=editor.lineWrapMode())
                dialog.findChild(QPushButton, 'copyLogErrorButton').click()
                seen['copied'] = QGuiApplication.clipboard().text()
            except Exception as error:
                seen['error'] = repr(error)
            finally:
                if dialog:
                    dialog.reject()
        QTimer.singleShot(0, inspect)
        self.tree.itemDoubleClicked.emit(self.tree.topLevelItem(0), 3)
        self.assertNotIn('error', seen)
        self.assertEqual('logErrorDetailsDialog', seen['name'])
        self.assertTrue(seen['readonly'])
        self.assertIn(message, seen['text'])
        self.assertIn(details, seen['text'])
        self.assertNotEqual(QPlainTextEdit.NoWrap, seen['wrap'])
        self.assertEqual(seen['text'], seen['copied'])

    def test_non_error_double_click_does_not_open_a_dialog(self):
        log_tree_add(self.tree, '12:00:00', None, 'Completed', 'manual', 'success')
        with patch('wiki_cat_tool.gui.widgets.ui_helpers.QDialog') as dialog:
            self.tree.itemDoubleClicked.emit(self.tree.topLevelItem(0), 3)
        dialog.assert_not_called()

    def test_summary_error_rows_also_have_full_details_without_a_structured_event(self):
        message = 'Could not save: APIError [readonly] ' + 'x' * 3000
        log_tree_add(self.tree, '12:00:00', 'Category:Page', message, 'manual', 'error', system=True)
        row = self.tree.topLevelItem(0)
        self.assertEqual(message, row.data(3, LOG_DETAILS_ROLE)['message'])
        self.assertIn(message, row.toolTip(3))

    def test_save_error_shows_the_actual_reason_first_and_keeps_original_exception_details(self):
        with patch('wiki_cat_tool.workers.rename_worker.TemplateManager', return_value=Mock()):
            worker = RenameWorker('unused.tsv', '', '', 'en', 'wikipedia', 0, False, True, True, False, False)
        worker._tf = lambda key, default='', **kwargs: default.format(**kwargs)
        page = Mock()
        page.title.return_value = 'Category:Page'
        error = pywikibot.exceptions.OtherPageSaveError(page, 'readonly: database temporarily locked')
        events = []
        worker.log_event.connect(events.append)
        worker._report_save_error(page, error)
        self.assertEqual('Could not save: readonly: database temporarily locked', events[0]['message'])
        self.assertIn('OtherPageSaveError', events[0]['details'])
        self.assertEqual('Category:Page', events[0]['title'])
        self.assertEqual(1, worker.error_count)
        self.assertIn(events[0]['message'], worker.error_pages['Category:Page'])

    def test_narrow_rename_log_keeps_fixed_status_columns_without_a_layout_exception(self):
        with patch.object(RenameTab, '_schedule_rename_log_autofit'):
            tab = RenameTab(self.parent)
            tab.rename_log_tree.setFixedWidth(500)
            tab.show()
            self.app.processEvents()
            tab._rename_log_cols_user_resized = False
            tab._fit_rename_log_columns_to_tree()
            self.assertGreaterEqual(tab.rename_log_tree.columnWidth(3), 280)
            self.assertGreaterEqual(tab.rename_log_tree.columnWidth(4), 220)
            self.assertGreaterEqual(tab.rename_log_tree.columnWidth(5), 160)
            self.assertGreater(tab.rename_log_tree.columnWidth(2), 0)


if __name__ == '__main__':
    unittest.main()
