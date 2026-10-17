"""Standalone linking of existing categories, with reviewable item conflicts."""

import csv
import io
import re

from PySide6.QtCore import QEvent, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QGroupBox,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from ...core.category_transfer import CategoryCreationRow, CategoryPair, category_action_plan, category_outcome_counts, merge_commands, read_creation_tsv
from ...core.localization import translate_key
from ...core.pywikibot_config import apply_pwb_config
from ...utils import debug, resolve_project_language
from ...workers.category_transfer_worker import CategoryInterwikiLinkWorker, CategoryInterwikiPreviewWorker
from ..widgets.category_autofill import title_fill_dialog
from ..widgets.table_history import TableHistory, table_action, pause_sorting
from ..widgets.category_plan import category_plan_widget
from ..widgets.category_stage import CategoryLogPanel, CategoryProgressFooter, CategoryRunRow, CategoryTableToolbar, CategoryWorkspace, CategoryWorkspaceFrame, highlight_next_action, refresh_category_toolbar, refresh_category_button_tooltips
from ..widgets.category_creation_table import TransferActionsLayout
from ..widgets.ui_helpers import category_table_style, inc_progress, init_progress, log_message, style_transfer_languages
from ..widgets.shared_panels import FileInputRow
from .category_transfer_tab import CategoryTransferTab


class InterwikiTable(QTableWidget):
    delete_requested = Signal()
    paste_requested = Signal()

    def keyPressEvent(self, event):
        if self.state() != QAbstractItemView.EditingState:
            if event.key() == Qt.Key_Delete:
                self.delete_requested.emit()
                return
            if event.key() == Qt.Key_V and event.modifiers() & Qt.ControlModifier:
                self.paste_requested.emit()
                return
        super().keyPressEvent(event)


class CategoryInterwikiTab(QWidget):
    _toolbar = CategoryTransferTab._toolbar
    _action_group = CategoryTransferTab._action_group
    _button = CategoryTransferTab._button
    _table = CategoryTransferTab._table

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent
        self.current_lang = self.current_family = self.current_user = None
        self.preview_worker = self.link_worker = None
        self._busy = False
        self._preview_context = self._linked_context = None
        self._prepared = ()
        self._conflicts = {}
        self._linked_pairs = {}
        self._reset_pending = False
        self._last_ui_lang = getattr(parent, '_ui_lang', 'ru')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.source_lang = QLineEdit('en')
        self.target_label = QLabel()
        toolbar, _files = self._toolbar(self.source_lang, self.target_label, None)
        self.toolbar = toolbar
        layout.addWidget(toolbar)
        self.tsv_path = QLineEdit('categories.tsv')
        self.file_row = FileInputRow(self, self._t('interwiki_file'), self.tsv_path, editor_menu=True)
        self.file_row.setToolTip(self._t('import_file_tip'))
        layout.addWidget(self.file_row)
        self.table_tools, row = self._action_group('table_tools')
        self.read_file_btn = self._button('read_file', self.open_tsv, row)
        self.add_btn = self._button('add_row', self.add_row, row)
        self.paste_btn = self._button('paste_pairs', self.paste_pairs, row)
        self.paste_btn.setToolTip(self._t('paste_pairs_tip'))
        self.fill_btn = self._button('number_titles', self.fill_titles, row)
        self.delete_btn = self._button('delete', self.delete_selected, row)
        self.export_btn = self._button('export_plan', self.export_tsv, None)
        self.report_btn = self._button('conflict_report', self.show_conflicts, row)
        self.summary_label = QLabel()
        self.summary_label.setObjectName('categoryListSummary')
        self.summary_label.setWordWrap(True)
        self.table_toolbar = CategoryTableToolbar(self.summary_label)
        self.table = self._table(['source_category', 'existing_category', 'source_wd', 'current_wd', 'status'], InterwikiTable)
        self.table.setObjectName('interwikiPairsTable')
        self.table.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)
        self.table.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 90)
        self.table.setColumnWidth(4, 160)
        self.table.delete_requested.connect(self.delete_selected)
        self.table.paste_requested.connect(self.paste_pairs)
        self.table.itemChanged.connect(self._edited)
        self.table.cellClicked.connect(self._open_wd)
        self.table.setMouseTracking(True)
        self.table.cellEntered.connect(self._link_cursor)
        table_content = QWidget()
        table_layout = QVBoxLayout(table_content)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.addWidget(self.table)
        self.workspace = CategoryWorkspace(table_content)
        self.table_group = CategoryWorkspaceFrame(self._t('interwiki_list'), self.table_toolbar, self.workspace)
        self.table_group.setProperty('transfer_text_key', 'interwiki_list')
        self.table_group.setToolTip(self._t('interwiki_help'))
        layout.addWidget(self.table_group, 1)
        layout.addWidget(self.table_tools)
        actions, row = self._action_group('create_tools')
        self.check_btn = self._button('check_links', self.preview, row)
        self.link_btn = self._button('link_pairs', self.start_linking, row)
        self.sync_btn = self._button('next_sync', self.open_sync, None)
        layout.addWidget(CategoryRunRow(actions, self.sync_btn, export_action=self.export_btn))
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setProperty('_wct_skip_log_translation', True)
        self.log_panel = CategoryLogPanel(self.log, self._t)
        self.workspace.set_log_panel(self.log_panel)
        self.table_toolbar.set_log_panel(self.log_panel)
        self.footer = CategoryProgressFooter(self.stop, self._history_language, self)
        self.progress_label = self.footer.label
        self.bar = self.footer.bar
        self.stop_btn = self.footer.stop_btn
        self.log_btn = self.log_panel.toggle
        layout.addWidget(self.footer)
        self.source_lang.textChanged.connect(self._context_changed)
        self.tsv_path.textChanged.connect(lambda: self._set_busy(self._busy))
        self.table.itemSelectionChanged.connect(lambda: self.delete_btn.setEnabled(not self._busy and bool(self.table.selectedIndexes())))
        self._set_busy(False)
        self._apply_theme()
        self.table_history = TableHistory(
            self.table, editable_columns=(0, 1), allowed=self._history_allowed,
            restored=self._restore_history, language=self._history_language,
        )

    def _history_allowed(self):
        return not self._busy

    def _history_language(self):
        return getattr(self.parent_window, '_ui_lang', 'ru')

    def _restore_history(self, _extra):
        self._invalidate()

    def _t(self, key, **values):
        return translate_key('ui.transfer.' + key, getattr(self.parent_window, '_ui_lang', 'ru'), key).format(**values)

    def _context(self):
        return (self.source_lang.text().strip().lower(), self.current_lang or resolve_project_language(self.parent_window, 'ru'),
                self.current_family or getattr(self.parent_window, 'current_family', None) or 'wikipedia')

    def _write_log(self, text):
        log_message(self.log, text, debug_func=lambda value: debug('[Interwiki linking] ' + value))

    def _progress(self, text):
        compact = re.sub(r'(https?://[^\s?]+)\?\S+', r'\1', str(text))
        self.progress_label.setText(compact[:180])
        self.progress_label.setToolTip(str(text)[:1500])
        self.bar.setFormat(self.progress_label.text())
        self.bar.setToolTip(self.progress_label.toolTip())

    def _advance(self):
        inc_progress(self.progress_label, self.bar)

    def _rows(self):
        return tuple(CategoryCreationRow(self.table.item(row, 0).text().strip(), self.table.item(row, 1).text().strip(), '')
                     for row in range(self.table.rowCount()))

    @pause_sorting('table')
    def _fill_rows(self, rows):
        blocked = self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for row, pair in enumerate(rows):
            for col, text in enumerate((pair.source_title, pair.target_title, '', '', self._t('editing'))):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if col >= 2:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(row, col, item)
        self.table.blockSignals(blocked)
        if hasattr(self, 'table_history'):
            self.table_history.reset()

    @pause_sorting('table')
    def _invalidate(self):
        self._prepared = ()
        self._preview_context = self._linked_context = None
        self._conflicts = {}
        self._linked_pairs = {}
        blocked = self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            for col in (2, 3, 4):
                self.table.item(row, col).setText(self._t('editing') if col == 4 else '')
            self.table.item(row, 4).setData(Qt.UserRole, None)
        self.table.blockSignals(blocked)
        self._set_busy(self._busy)

    def _edited(self, item):
        if not self._busy and item.column() < 2:
            blocked = self.table.blockSignals(True)
            item.setToolTip(item.text())
            self.table.blockSignals(blocked)
            self._invalidate()

    def _set_busy(self, busy):
        was_busy = self._busy
        self._busy = busy
        self.table.setSortingEnabled(not busy)
        if busy and not was_busy:
            self.log_panel.set_expanded(True)
        for widget in (self.table, self.source_lang, self.file_row, self.export_btn,
                       self.add_btn, self.delete_btn, self.paste_btn, self.fill_btn, self.check_btn):
            widget.setEnabled(not busy)
        self.link_btn.setEnabled(not busy and self.table.rowCount() > 0 and bool(self._prepared) and self._preview_context == self._context())
        self.report_btn.setEnabled(not busy and bool(self._conflicts))
        self.sync_btn.setEnabled(not busy and bool(self._linked_pairs) and self._linked_context == self._context())
        self.stop_btn.setEnabled(busy)
        self.read_file_btn.setEnabled(not busy and bool(self.tsv_path.text().strip()))
        has_rows = self.table.rowCount() > 0
        self.check_btn.setEnabled(not busy and has_rows)
        self.fill_btn.setEnabled(not busy and has_rows)
        self.export_btn.setEnabled(not busy and has_rows)
        self.delete_btn.setEnabled(not busy and bool(self.table.selectedIndexes()))
        theme = getattr(self.parent_window, '_theme_mode', 'teal')
        for button, active in ((self.read_file_btn, not has_rows), (self.add_btn, not has_rows),
                               (self.check_btn, has_rows and not self.link_btn.isEnabled() and not self.sync_btn.isEnabled()),
                               (self.link_btn, self.link_btn.isEnabled()), (self.sync_btn, self.sync_btn.isEnabled())):
            highlight_next_action(button, not busy and active, theme)
        self.target_label.setText(self._context()[1])
        self._summary()
        if hasattr(self, 'table_history'):
            self.table_history.refresh()

    def _summary(self):
        results = [(self.table.item(row, 4).data(Qt.UserRole) or {}) for row in range(self.table.rowCount())]
        counts = dict(ready=sum(result.get('status') in {'ready_link', 'ready_item'} for result in results),
                      conflicts=len(self._conflicts), **category_outcome_counts(results))
        parts = [self._t('list_count', count=self.table.rowCount())]
        parts.extend(self._t('count_' + key, count=value) for key, value in counts.items() if value)
        self.summary_label.setText('   ·   '.join(parts))
        if not self.table.rowCount():
            self.summary_label.setText(self._t('interwiki_start_hint'))
        self.summary_label.setToolTip(self._t('interwiki_summary', total=self.table.rowCount(), **counts))

    def load_pairs(self, rows, source_lang):
        if self._busy:
            return False
        self._fill_rows(tuple(CategoryCreationRow(row.source_title, row.target_title, '') for row in rows))
        self.source_lang.setText(source_lang)
        self._invalidate()
        return True

    @table_action('table_history')
    def add_row(self):
        if not self._busy:
            rows = self._rows() + (CategoryCreationRow('', '', ''),)
            self._fill_rows(rows)
            new_item = self.table.item(len(rows) - 1, 0)
            self._invalidate()
            self.table.setCurrentItem(new_item)
            self.table.editItem(new_item)

    @table_action('table_history')
    def delete_selected(self):
        if not self._busy:
            for row in sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True):
                self.table.removeRow(row)
            self._invalidate()

    @table_action('table_history')
    def paste_pairs(self):
        if self._busy:
            return
        fields = list(csv.reader(io.StringIO(QGuiApplication.clipboard().text()), delimiter='\t'))
        rows = tuple(CategoryCreationRow(row[0].strip(), row[1].strip() if len(row) > 1 else '', '') for row in fields if row)
        self._fill_rows(self._rows() + rows)
        self._invalidate()

    def open_tsv(self):
        if self._busy:
            return
        path = self.tsv_path.text().strip()
        if path:
            try:
                self.load_pairs(read_creation_tsv(path, allow_untranslated=True), self.source_lang.text())
            except Exception as error:
                QMessageBox.warning(self, self._t('interwiki_title'), str(error))

    def export_tsv(self):
        path, _ = QFileDialog.getSaveFileName(self, self._t('export_plan'), 'categories.tsv', 'TSV (*.tsv)')
        if path:
            try:
                with open(path, 'w', encoding='utf-8-sig', newline='') as handle:
                    csv.writer(handle, delimiter='\t').writerows((row.source_title, row.target_title) for row in self._rows())
            except Exception as error:
                QMessageBox.warning(self, self._t('interwiki_title'), str(error))

    @table_action('table_history')
    def fill_titles(self):
        if self._busy or not self.table.rowCount():
            return
        result = title_fill_dialog(self, self._rows(), {index.row() for index in self.table.selectedIndexes()},
                                   self._t, self.table.styleSheet())
        if result is not None:
            blocked = self.table.blockSignals(True)
            for proposal in result[0]:
                if proposal.status == 'number_fill':
                    self.table.item(proposal.index, 1).setText(proposal.target_title)
                    self.table.item(proposal.index, 1).setToolTip(proposal.target_title)
            self.table.blockSignals(blocked)
            self._invalidate()

    def preview(self):
        if self._busy or not self.table.rowCount():
            return
        source, target, family = self._context()
        if not re.fullmatch(r'[a-z][a-z0-9-]*', source) or source == target:
            QMessageBox.warning(self, self._t('interwiki_title'), self._t('different_languages'))
            return
        rows = self._rows()
        self._set_busy(True)
        self._invalidate()
        self.table_history.reset()
        self._preview_context = self._context()
        worker = CategoryInterwikiPreviewWorker(rows=rows, source_lang=source, target_lang=target, family=family)
        self.preview_worker = worker
        worker.row_ready.connect(self._preview_result)
        worker.progress.connect(self._write_log)
        worker.activity.connect(self._preview_activity)
        worker.retry_wait.connect(self._progress)
        worker.finished.connect(self._preview_finished)
        self._start(worker, len(rows), 'checking')

    def _start(self, worker, count, status):
        self._set_busy(True)
        init_progress(self.progress_label, self.bar, count)
        if status == 'checking':
            self.bar.setRange(0, 100)
            self.bar.setValue(0)
        self._progress(self._t(status))
        self._write_log(self._t('check_started', count=count) if status == 'checking' else self._t(status))
        debug(f'[Interwiki linking] {status}: {self._context()}, rows={count}')
        worker.start()

    def _preview_activity(self, payload):
        self.bar.setRange(0, 100)
        self.bar.setValue(payload['percent'])
        self._progress(payload['text'])

    def _row_result(self, index, result):
        blocked = self.table.blockSignals(True)
        if 'row' in result:
            self.table.item(index, 0).setText(result['row'].source_title)
            self.table.item(index, 1).setText(result['row'].target_title)
        for col, value in ((2, result.get('source_qid', '')), (3, result.get('target_qid', '')),
                           (4, self._t(result['status']))):
            self.table.item(index, col).setText(value)
            self.table.item(index, col).setToolTip(result.get('error') or result.get('detail') or value)
        self.table.item(index, 4).setData(Qt.UserRole, dict(result))
        self.table.blockSignals(blocked)
        pair = CategoryCreationRow(self.table.item(index, 0).text().strip(), self.table.item(index, 1).text().strip(), '')
        if result['status'] == 'different_items':
            self._conflicts[pair.source_title] = {**result, 'source_title': pair.source_title, 'target_title': pair.target_title}
        else:
            self._conflicts.pop(pair.source_title, None)
        if result['status'] in {'already_linked', 'linked', 'labels_updated'}:
            self._linked_pairs[pair.target_title] = CategoryPair(pair.source_title, pair.target_title, result['source_qid'])
        else:
            self._linked_pairs.pop(pair.target_title, None)
        self._summary()
        self._apply_links((index,))

    def _preview_result(self, result):
        self._row_result(result['index'], result)

    def _preview_finished(self):
        worker = self.preview_worker
        self.preview_worker = None
        if worker.failed or worker._stop:
            self._prepared = ()
            self._preview_context = None
            status = self._t('failed', error=worker.failure_message) if worker.failed else self._t('stopped')
        else:
            self._prepared = tuple(result['row'] for result in worker.results
                                   if result['status'] in {'ready_link', 'ready_item'})
            self._linked_context = self._preview_context
            status = self._t('preview_ready')
            self.bar.setValue(self.bar.maximum())
        self._finish(worker, status)
        self.table_history.reset()

    def _confirm_linking(self):
        dialog = QDialog(self)
        dialog.setObjectName('confirmInterwikiLinkingDialog')
        dialog.setMinimumWidth(480)
        dialog.setWindowTitle(self._t('interwiki_title'))
        layout = QVBoxLayout(dialog)
        source, target, _family = self._context()
        label = QLabel(self._t('plan_link_heading', count=len(self._prepared), source=source, target=target))
        label.setWordWrap(True)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        layout.addWidget(label)
        results = [dict(self.table.item(row, 4).data(Qt.UserRole) or {},
                        source_title=self.table.item(row, 0).text()) for row in range(self.table.rowCount())]
        plan = category_action_plan(results, (row.source_title for row in self._prepared))
        layout.addWidget(category_plan_widget(plan, self._t, dialog, results=results))
        if plan['items']:
            notice = QLabel(self._t('confirm_items'))
            notice.setWordWrap(True)
            layout.addWidget(notice)
        buttons = QDialogButtonBox()
        buttons.addButton(self._t('link_pairs'), QDialogButtonBox.AcceptRole)
        buttons.addButton(self._t('cancel'), QDialogButtonBox.RejectRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog.exec() == QDialog.Accepted

    def start_linking(self):
        if not self.table.rowCount():
            return
        if self._busy or not self._prepared or self._preview_context != self._context():
            return
        user, password = getattr(self.parent_window, 'current_user', None), getattr(self.parent_window, 'current_password', None)
        if not user or not password:
            QMessageBox.warning(self, self._t('interwiki_title'), self._t('login_required'))
            return
        if not self._confirm_linking():
            return
        self.table_history.reset()
        source, target, family = self._context()
        apply_pwb_config(target, family)
        worker = CategoryInterwikiLinkWorker(rows=self._prepared, username=user, password=password,
                                            source_lang=source, target_lang=target, family=family,
                                            summary=self._t('interwiki_edit_summary'))
        self.link_worker = worker
        worker.category_result.connect(self._link_result)
        worker.progress.connect(self._write_log)
        worker.item_processed.connect(self._advance)
        worker.interactive_input_requested.connect(self._interactive_input)
        worker.finished.connect(self._link_finished)
        self._start(worker, len(self._prepared), 'linking')

    def _interactive_input(self, question, password):
        answer, accepted = QInputDialog.getText(self, self._t('interwiki_title'), question,
                                                QLineEdit.Password if password else QLineEdit.Normal)
        if self.link_worker:
            self.link_worker.provide_interactive_input(answer if accepted else None)

    def _link_result(self, result):
        for index, row in enumerate(self._rows()):
            if row.source_title == result['source_title']:
                self._row_result(index, result)
                break

    def _link_finished(self):
        worker = self.link_worker
        self.link_worker = None
        self._prepared = ()
        self._linked_context = self._preview_context
        status = (self._t('failed', error=worker.failure_message) if worker.failed else self._t('stopped')
                  if worker._stop else self._t('interwiki_done', **{'labels': 0, **worker.stats}))
        recorder = getattr(self.parent_window, 'record_operation', None)
        if recorder and worker.saved_edits:
            recorder('interwiki', worker.saved_edits)
        self._finish(worker, status)
        self.table_history.reset()
        if self._conflicts:
            self.show_conflicts()

    def _finish(self, worker, status):
        if self.bar.maximum() == 0:
            self.bar.setRange(0, self.table.rowCount())
            self.bar.setValue(len(getattr(worker, 'results', ())))
        self._progress(status)
        self._write_log(status)
        self._set_busy(False)
        if self._reset_pending:
            self._reset_pending = False
            self._invalidate()
        worker.deleteLater()

    def show_conflicts(self):
        if self._busy or not self._conflicts:
            return
        conflicts = list(self._conflicts.values())
        code = merge_commands(conflicts)
        dialog = QDialog(self)
        dialog.setObjectName('interwikiConflictsDialog')
        dialog.setWindowTitle(self._t('conflict_report'))
        dialog.resize(850, 550)
        layout = QVBoxLayout(dialog)
        hint = QLabel(self._t('merge_help', count=len(conflicts)))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        table = QTableWidget(len(conflicts), 4)
        table.setStyleSheet(self.table.styleSheet())
        table.setHorizontalHeaderLabels([self._t(key) for key in ('source_category', 'existing_category', 'source_wd', 'current_wd')])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        for row, conflict in enumerate(conflicts):
            for col, key in enumerate(('source_title', 'target_title', 'source_qid', 'target_qid')):
                item = QTableWidgetItem(conflict[key])
                item.setToolTip(conflict[key])
                table.setItem(row, col, item)
        layout.addWidget(table, 1)
        editor = QPlainTextEdit(code)
        editor.setReadOnly(True)
        layout.addWidget(editor, 1)
        row = TransferActionsLayout()
        self._button('copy_merge', lambda: QGuiApplication.clipboard().setText(code), row)
        self._button('save_conflicts', lambda: self._save_conflicts(conflicts), row)
        self._button('save_merge', lambda: self._save_merge(code), row)
        self._button('open_quickstatements', lambda: QDesktopServices.openUrl(QUrl('https://quickstatements.toolforge.org/')), row)
        layout.addLayout(row)
        buttons = QDialogButtonBox()
        buttons.addButton(self._t('close'), QDialogButtonBox.RejectRole)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _save_conflicts(self, conflicts):
        path, _ = QFileDialog.getSaveFileName(self, self._t('save_conflicts'), 'interwiki-conflicts.tsv', 'TSV (*.tsv)')
        if path:
            try:
                with open(path, 'w', encoding='utf-8-sig', newline='') as handle:
                    writer = csv.writer(handle, delimiter='\t')
                    keys = ('source_title', 'target_title', 'source_qid', 'target_qid')
                    writer.writerow(keys)
                    writer.writerows([conflict[key] for key in keys] for conflict in conflicts)
            except Exception as error:
                QMessageBox.warning(self, self._t('conflict_report'), str(error))

    def _save_merge(self, code):
        path, _ = QFileDialog.getSaveFileName(self, self._t('save_merge'), 'interwiki-merge.tsv', 'TSV (*.tsv)')
        if path:
            try:
                with open(path, 'w', encoding='utf-8') as handle:
                    handle.write(code + '\n')
            except Exception as error:
                QMessageBox.warning(self, self._t('conflict_report'), str(error))

    def open_sync(self):
        sync = getattr(self.parent_window, 'category_content_sync_tab', None)
        if self._busy or sync is None or not self._linked_pairs or self._linked_context != self._context():
            return
        if any(worker and worker.isRunning() for worker in (sync.preview_worker, sync.sync_worker)):
            QMessageBox.warning(self, self._t('interwiki_title'), self._t('sync_busy'))
            return
        sync.load_transfer_categories(list(self._linked_pairs), self._context()[0], pairs=tuple(self._linked_pairs.values()))
        self.parent_window.tabs.setCurrentWidget(sync)

    def stop(self):
        for worker in (self.preview_worker, self.link_worker):
            if worker and worker.isRunning():
                worker.request_stop()
        self.stop_btn.setEnabled(False)

    def _context_changed(self, *_args):
        if self._busy:
            self._reset_pending = True
            self.stop()
        else:
            self._invalidate()
            if hasattr(self, 'table_history'):
                self.table_history.reset()

    def update_language(self, lang):
        if lang != self.current_lang:
            self.current_lang = lang
            self._context_changed()

    def update_family(self, family):
        if family != self.current_family:
            self.current_family = family
            self._context_changed()

    def set_auth_data(self, username, lang, family):
        self.current_user = username
        self.update_language(lang)
        self.update_family(family)

    def clear_auth_data(self):
        self.current_user = None
        self._context_changed()

    def _open_wd(self, row, col):
        value = self.table.item(row, col).text()
        if col in (2, 3) and re.fullmatch(r'Q[1-9][0-9]*', value):
            QDesktopServices.openUrl(QUrl('https://www.wikidata.org/wiki/' + value))

    def _link_cursor(self, row, col):
        linked = col in (2, 3) and re.fullmatch(r'Q[1-9][0-9]*', self.table.item(row, col).text())
        self.table.viewport().setCursor(Qt.PointingHandCursor if linked else Qt.ArrowCursor)

    def _apply_links(self, rows=None):
        blocked = self.table.blockSignals(True)
        for row in range(self.table.rowCount()) if rows is None else rows:
            for col in (2, 3):
                item = self.table.item(row, col)
                font = item.font()
                linked = bool(re.fullmatch(r'Q[1-9][0-9]*', item.text()))
                font.setUnderline(linked)
                item.setFont(font)
                if linked:
                    item.setForeground(QColor('#1469a3' if getattr(self.parent_window, '_theme_mode', 'teal') == 'light' else '#72c9f0'))
        self.table.blockSignals(blocked)

    def _apply_theme(self):
        theme = getattr(self.parent_window, '_theme_mode', 'teal')
        self.table.setStyleSheet(category_table_style(theme))
        style_transfer_languages(self.source_lang, self.target_label, theme)
        muted = {'light': '#526a7a', 'dark': '#a3b3c5', 'teal': '#91b9c5'}.get(theme, '#91b9c5')
        self.summary_label.setStyleSheet(f'color: {muted}; padding: 2px 0;')
        self._apply_links()

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_theme()
        self.refresh_localized_texts()
        self._set_busy(self._busy)

    @pause_sorting('table')
    def refresh_localized_texts(self):
        refresh_category_button_tooltips(self, self._t)
        self.log_panel.refresh_localized_texts()
        self.footer.refresh_localized_texts()
        refresh_category_toolbar(self.toolbar, self._t)
        lang = getattr(self.parent_window, '_ui_lang', 'ru')
        if lang != self._last_ui_lang:
            self._last_ui_lang = lang
            self.file_row.refresh_localized_texts()
            self.file_row.caption.setText(self._t('interwiki_file'))
            self.file_row.setToolTip(self._t('import_file_tip'))
            self.paste_btn.setToolTip(self._t('paste_pairs_tip'))
            self.table_group.setToolTip(self._t('interwiki_help'))
            self.log_btn.setText(self._t('log'))
            for button in self.findChildren(QPushButton):
                key = button.property('transfer_text_key')
                if key:
                    button.setText(self._t(key))
            for group in self.findChildren(QGroupBox):
                group.setTitle(self._t(group.property('transfer_text_key')))
            for col, key in enumerate(self.table.property('transfer_header_keys')):
                self.table.horizontalHeaderItem(col).setText(self._t(key))
            blocked = self.table.blockSignals(True)
            for row in range(self.table.rowCount()):
                data = self.table.item(row, 4).data(Qt.UserRole) or {'status': 'editing'}
                self.table.item(row, 4).setText(self._t(data['status']))
            self.table.blockSignals(blocked)
            self._summary()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.StyleChange, QEvent.PaletteChange) and hasattr(self, 'table') and not getattr(self, '_applying_theme', False):
            self._applying_theme = True
            try:
                self._apply_theme()
            finally:
                self._applying_theme = False
