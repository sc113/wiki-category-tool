"""Discover, review, create and link categories, then hand off to content sync."""

from __future__ import annotations

import csv
import re

from PySide6.QtCore import Qt, QUrl, QEvent
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices, QKeySequence, QShortcut, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGroupBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QProgressBar,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QStyledItemDelegate, QPlainTextEdit,
    QTextEdit, QVBoxLayout, QWidget, QToolButton, QSizePolicy, QMenu, QLayout,
)

from ...core.category_transfer import (
    CategoryCreationRow, CategoryPair, export_creation_tsv, read_creation_tsv,
)
from ...core.localization import translate_key
from ...core.pywikibot_config import apply_pwb_config
from ...utils import debug, default_create_summary, resolve_project_language
from ...workers.category_transfer_worker import (
    CategoryTextReadWorker, CategoryTransferCreateWorker, CategoryTransferPreviewWorker, CategoryTransferReadWorker,
)
from ..widgets.ui_helpers import create_log_wrap, create_spin_control, log_message, style_transfer_languages
from ..widgets.category_autofill import title_fill_dialog
from ..widgets.category_creation_table import CategoryCandidatesTable, CategoryCreationTable, TransferActionsLayout, TransferAutofillTabs, TransferToolbarLayout


class CategoryTextDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        return QPlainTextEdit(parent)

    def setEditorData(self, editor, index):
        editor.setPlainText(index.data(Qt.EditRole) or "")

    def setModelData(self, editor, model, index):
        model.setData(index, editor.toPlainText(), Qt.EditRole)

    def updateEditorGeometry(self, editor, option, index):
        rect = option.rect
        rect.setHeight(min(140, max(40, editor.parent().height() - rect.top())))
        editor.setGeometry(rect)

    def eventFilter(self, editor, event):
        if isinstance(editor, QPlainTextEdit) and event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() & Qt.ControlModifier:
                self.commitData.emit(editor)
                self.closeEditor.emit(editor)
                return True
            return False
        return super().eventFilter(editor, event)


class CategoryLinksStage(QWidget):
    """A separate navigation page using the transfer controller and its data."""

    def __init__(self, controller):
        super().__init__(controller.parent_window)
        self.controller = controller

    def showEvent(self, event):
        super().showEvent(event)
        self.controller._show_stage(self)


class CategoryTransferTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent
        self.current_lang = None
        self.current_family = None
        self.current_user = None
        self.read_worker = None
        self.content_worker = None
        self.create_worker = None
        self.preview_worker = None
        self._busy = False
        self._read_mode = ""
        self._creation_rows = ()
        self._preview_rows = ()
        self._existing_linked_categories = []
        self._creation_row_results = {}
        self._preview_context = None
        self._linked_categories = []
        self._linked_context = None
        self._linked_pairs = {}
        self._table_dirty = False
        self._rows_by_title = {}
        self._pending_context_reset = False
        self._last_ui_lang = getattr(parent, "_ui_lang", "ru")
        self._setup_ui()

    def _t(self, key: str, **values) -> str:
        lang = getattr(self.parent_window, "_ui_lang", "ru")
        return translate_key(f"ui.transfer.{key}", lang, key).format(**values)

    def _context(self):
        return (self.source_lang.text().strip().lower(),
                self.current_lang or resolve_project_language(self.parent_window, "ru"),
                self.current_family or getattr(self.parent_window, "current_family", None) or "wikipedia")

    def _creation_context(self):
        return (self.links_source_lang.text().strip().lower(), *self._context()[1:])

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.source_lang = QLineEdit("en")
        self.target_label = QLabel()
        self.scan_options_btn = self._options_button()
        scan_toolbar, scan_files = self._toolbar(self.source_lang, self.target_label, self.scan_options_btn)
        self.open_scan_btn = self._button("choose_tsv", self.import_report, scan_files)
        self.export_scan_btn = QToolButton()
        self.export_scan_btn.setText(self._t("export_plan"))
        self.export_scan_btn.setPopupMode(QToolButton.InstantPopup)
        export_menu = QMenu(self.export_scan_btn)
        self.report_btn = QAction(self._t("export_report"), export_menu)
        self.report_btn.setProperty("transfer_text_key", "export_report")
        self.report_btn.triggered.connect(self.export_report)
        self.tsv_btn = QAction(self._t("prepare_tsv"), export_menu)
        self.tsv_btn.setProperty("transfer_text_key", "prepare_tsv")
        self.tsv_btn.triggered.connect(self.prepare_tsv)
        export_menu.addAction(self.report_btn)
        export_menu.addAction(self.tsv_btn)
        self.export_scan_btn.setMenu(export_menu)
        scan_files.addWidget(self.export_scan_btn)
        layout.addWidget(scan_toolbar)
        root_row = QHBoxLayout()
        root_row.setSpacing(12)
        root_row.addWidget(QLabel(self._t("root")))
        self.root_edit = QLineEdit()
        self.root_edit.setMinimumWidth(0)
        self.root_edit.setPlaceholderText(self._t("root_placeholder"))
        root_row.addWidget(self.root_edit, 1)
        self.load_btn = self._button("load", self.load_categories, root_row)
        layout.addLayout(root_row)
        self.settings = QGroupBox(self._t("settings"))
        settings_layout = QFormLayout(self.settings)
        settings_layout.setHorizontalSpacing(18)
        settings_layout.setVerticalSpacing(10)
        self.depth_spin = QSpinBox()
        self.depth_spin.setRange(0, 99)
        self.depth_spin.setValue(4)
        depth_wrap, self.depth_plus_btn, self.depth_minus_btn = create_spin_control(
            self.depth_spin, plus_tip=self._t("increase"), minus_tip=self._t("decrease"))
        depth_row = QHBoxLayout()
        depth_row.setSpacing(18)
        depth_row.addWidget(depth_wrap)
        self.include_root = QCheckBox(self._t("include_root"))
        depth_row.addWidget(self.include_root)
        depth_row.addStretch(1)
        settings_layout.addRow(self._t("depth"), depth_row)
        self.skip_spin = QSpinBox()
        self.skip_spin.setRange(0, 99)
        self.skip_spin.setValue(0)
        self.skip_spin.setToolTip(self._t("skip_tip"))
        skip_wrap, _, _ = create_spin_control(self.skip_spin, plus_tip=self._t("increase"), minus_tip=self._t("decrease"))
        settings_layout.addRow(self._t("skip_level"), skip_wrap)
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setDecimals(2)
        self.delay_spin.setRange(0.35, 10.0)
        self.delay_spin.setSingleStep(0.1)
        self.delay_spin.setValue(0.5)
        delay_wrap, _, _ = create_spin_control(self.delay_spin, width=60,
                                              plus_tip=self._t("increase"), minus_tip=self._t("decrease"))
        settings_layout.addRow(self._t("delay"), delay_wrap)
        self.settings.hide()
        self.scan_options_btn.toggled.connect(lambda checked: self._toggle_panel(self.settings, self.scan_options_btn, checked))
        layout.addWidget(self.settings)

        results_page = QWidget()
        result_layout = QVBoxLayout(results_page)
        result_layout.setContentsMargins(0, 6, 0, 0)
        self.table = self._table(["source_category", "existing_category", "wikidata", "depth_column",
                                  "total", "potential_column", "status"], CategoryCandidatesTable)
        self.table.horizontalHeader().setStretchLastSection(False)
        for col in (0, 1):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.Interactive)
        self.table.setColumnWidth(3, 65)
        self.table.setColumnWidth(4, 105)
        self.table.setColumnWidth(6, 145)
        self.table.setObjectName("transferCandidatesTable")
        self.table.cellDoubleClicked.connect(self._open_category)
        self.table.cellClicked.connect(lambda row, col: self._open_category(row, col) if col == 2 else None)
        self.table.setMouseTracking(True)
        self.table.cellEntered.connect(self._link_cursor)
        result_layout.addWidget(self.table, 1)
        list_tools, actions = self._action_group("list_tools")
        self.delete_btn = self._button("delete", self.delete_selected, actions)
        self.clear_btn = self._button("clear_empty", self.clear_empty, actions)
        result_layout.addWidget(list_tools)
        scan_tools, run_actions = self._action_group("scan_tools")
        self.scan_btn = self._button("scan", self.scan_categories, run_actions)
        self.retry_btn = self._button("retry_errors", self.retry_errors, run_actions)
        self.next_btn = self._button("next_links", self.open_links, run_actions)
        result_layout.addWidget(scan_tools)
        layout.addWidget(results_page, 1)

        creation_page = self.creation_stage = CategoryLinksStage(self)
        creation_layout = QVBoxLayout(creation_page)
        creation_layout.setContentsMargins(0, 0, 0, 0)
        self.links_source_lang = QLineEdit("en")
        self.links_target_label = QLabel()
        self.options_btn = self._options_button()
        creation_toolbar, context_row = self._toolbar(self.links_source_lang, self.links_target_label, self.options_btn)
        self.browse_btn = self._button("choose_tsv", self.choose_tsv, context_row)
        self.export_creation_btn = self._button("export_plan", self.export_plan, context_row)
        creation_layout.addWidget(creation_toolbar)
        self.creation_hint = QLabel(self._t("creation_hint"))
        self.creation_hint.setWordWrap(True)
        creation_layout.addWidget(self.creation_hint)
        self.file_options = QWidget()
        options_layout = QVBoxLayout(self.file_options)
        options_layout.setContentsMargins(0, 0, 0, 0)
        instructions = QLabel(self._t("tsv_help"))
        instructions.setWordWrap(True)
        options_layout.addWidget(instructions)
        file_row = QHBoxLayout()
        self.tsv_path = QLineEdit()
        file_row.addWidget(self.tsv_path, 1)
        self.open_btn = self._button("open_tsv", self.open_tsv, file_row)
        self.preview_btn = self._button("import_tsv", self.import_tsv, file_row)
        options_layout.addLayout(file_row)
        summary_row = QHBoxLayout()
        summary_row.addWidget(QLabel(self._t("summary")))
        self.summary_edit = QLineEdit(default_create_summary(self._context()[1]))
        self._summary_lang = self._context()[1]
        self.summary_edit.setMinimumWidth(0)
        summary_row.addWidget(self.summary_edit, 1)
        options_layout.addLayout(summary_row)
        self.links_delay_spin = QDoubleSpinBox()
        self.links_delay_spin.setDecimals(2)
        self.links_delay_spin.setRange(0.35, 10.0)
        self.links_delay_spin.setSingleStep(0.1)
        self.links_delay_spin.setValue(0.5)
        links_delay_wrap, _, _ = create_spin_control(self.links_delay_spin, width=60,
                                                    plus_tip=self._t("increase"), minus_tip=self._t("decrease"))
        links_delay_row = QHBoxLayout()
        links_delay_row.addWidget(QLabel(self._t("delay")))
        links_delay_row.addWidget(links_delay_wrap)
        links_delay_row.addStretch(1)
        options_layout.addLayout(links_delay_row)
        self.file_options.hide()
        self.options_btn.toggled.connect(self._toggle_options)
        creation_layout.addWidget(self.file_options)
        edit_tools, edit_actions = self._action_group("table_tools")
        self.remove_creation_btn = self._button("delete", self.delete_creation_selected, edit_actions)
        self.incomplete_btn = self._button("next_incomplete", self.select_next_incomplete, edit_actions)
        self.autofill_btn = QToolButton()
        self.autofill_btn.setText(self._t("autofill"))
        self.autofill_btn.setCheckable(True)
        self.autofill_btn.setArrowType(Qt.RightArrow)
        self.autofill_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        edit_actions.addWidget(self.autofill_btn)
        creation_layout.addWidget(edit_tools)
        self.autofill_box = QFrame()
        self.autofill_box.setObjectName("transferAutofillPanel")
        self.autofill_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        autofill_layout = QVBoxLayout(self.autofill_box)
        autofill_layout.setContentsMargins(0, 0, 0, 0)
        autofill_layout.setSizeConstraint(QLayout.SetMinimumSize)
        self.autofill_tabs = TransferAutofillTabs()
        autofill_layout.addWidget(self.autofill_tabs)
        titles_page, content_page = QWidget(), QWidget()
        titles_layout = QVBoxLayout(titles_page)
        content_layout = QVBoxLayout(content_page)
        self.autofill_tabs.addTab(titles_page, self._t("titles_column"))
        self.autofill_tabs.addTab(content_page, self._t("content_column"))
        title_row = QHBoxLayout()
        self.title_template_edit = QLineEdit()
        self.title_template_edit.setPlaceholderText(self._t("number_target_example"))
        title_row.addWidget(self.title_template_edit, 1)
        self.number_titles_btn = self._button("preview_titles", self.fill_numbered_titles, title_row)
        titles_layout.addLayout(title_row)
        self.autofill_title_help = QLabel(self._t("autofill_title_help"))
        self.autofill_title_help.setWordWrap(True)
        titles_layout.addWidget(self.autofill_title_help)
        content_actions = TransferActionsLayout()
        content_actions.setSpacing(8)
        self.fill_content_btn = self._button("fill_content", self.fill_content, content_actions)
        self.copy_first_btn = self._button("copy_first_content", self.copy_first_content, content_actions)
        self.load_text_btn = self._button("load_source_content", self.load_source_content, content_actions)
        self.copy_first_btn.setToolTip(self._t("copy_first_tip"))
        self.load_text_btn.setToolTip(self._t("source_content_tip"))
        content_layout.addLayout(content_actions)
        self.source_content_empty = QCheckBox(self._t("source_content_empty"))
        self.source_content_empty.setChecked(True)
        self.source_content_empty.setToolTip(self._t("source_content_tip"))
        content_layout.addWidget(self.source_content_empty)
        self.autofill_box.hide()
        self.autofill_btn.toggled.connect(lambda checked: self._toggle_panel(self.autofill_box, self.autofill_btn, checked))
        creation_layout.addWidget(self.autofill_box)
        self.creation_summary = QLabel()
        self.creation_summary.setWordWrap(True)
        creation_layout.addWidget(self.creation_summary)
        self.creation_table = self._table(["source_category", "new_category", "content", "source_wd", "current_wd", "status"], CategoryCreationTable)
        self.creation_table.setObjectName("transferCreationTable")
        self.creation_table.horizontalHeader().setStretchLastSection(False)
        for col in range(3):
            self.creation_table.horizontalHeader().setSectionResizeMode(col, QHeaderView.Interactive)
        self.creation_table.setColumnWidth(3, 95)
        self.creation_table.setColumnWidth(4, 95)
        self.creation_table.setColumnWidth(5, 145)
        self.creation_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.creation_table.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.creation_table.setItemDelegateForColumn(2, CategoryTextDelegate(self.creation_table))
        self.creation_table.delete_rows_requested.connect(self.delete_creation_selected)
        self.creation_table.fill_requested.connect(self._fill_content_range)
        self.creation_table.itemChanged.connect(self._creation_edited)
        self.creation_table.itemSelectionChanged.connect(self._show_creation_detail)
        self.creation_table.cellClicked.connect(self._open_creation_wd)
        self.creation_table.setMouseTracking(True)
        self.creation_table.cellEntered.connect(self._creation_link_cursor)
        creation_layout.addWidget(self.creation_table, 1)
        self.creation_detail = QLabel(self._t("row_detail_hint"))
        self.creation_detail.setWordWrap(True)
        self.creation_detail.setTextFormat(Qt.PlainText)
        self.creation_detail.setMaximumHeight(44)
        creation_layout.addWidget(self.creation_detail)
        create_tools, create_actions = self._action_group("create_tools")
        self.check_links_btn = self._button("check_links", self.preview_creation, create_actions)
        self.create_btn = self._button("create", self.start_creation, create_actions)
        self.sync_btn = self._button("next_sync", self.open_sync, create_actions)
        creation_layout.addWidget(create_tools)

        self.footer = QWidget()
        self.footer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        footer_layout = QVBoxLayout(self.footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        self.log_toggle = QToolButton()
        self.log_toggle.setText(self._t("log"))
        self.log_toggle.setCheckable(True)
        self.log_toggle.setArrowType(Qt.RightArrow)
        self.log_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setProperty("_wct_skip_log_translation", True)
        self.log.setFixedHeight(95)
        self.log_wrap = create_log_wrap(self, self.log, with_header=False)
        self.log_wrap.hide()
        footer_layout.addWidget(self.log_wrap)
        self.log_toggle.toggled.connect(self._toggle_log)
        self.progress_label = QLabel(self._t("ready"))
        self.progress_label.setMaximumHeight(42)
        self.progress_label.setTextFormat(Qt.PlainText)
        self.progress_label.setWordWrap(True)
        self.progress_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        footer_layout.addWidget(self.progress_label)
        progress_row = QHBoxLayout()
        progress_row.addWidget(self.log_toggle)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1)
        progress_row.addWidget(self.bar, 1)
        self.stop_btn = self._button("stop", self.stop, progress_row)
        footer_layout.addLayout(progress_row)
        layout.addWidget(self.footer)
        self._delete_shortcut = QShortcut(QKeySequence(Qt.Key_Delete), self.table)
        self._delete_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self._delete_shortcut.activated.connect(self.delete_selected)
        self._copy_shortcut = QShortcut(QKeySequence.Copy, self.table)
        self._copy_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self._copy_shortcut.activated.connect(self.copy_selected)
        self.table.horizontalHeader().sortIndicatorChanged.connect(self._reindex)
        self.source_lang.textChanged.connect(self._source_changed)
        self.links_source_lang.textChanged.connect(self._links_source_changed)
        self._refresh_target()
        self._set_busy(False)
        self._apply_table_theme()

    def _toggle_options(self, checked):
        self.file_options.setVisible(checked)
        self.options_btn.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)

    def _toggle_log(self, checked):
        self.log_wrap.setVisible(checked)
        self.log_toggle.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)

    def _interactive_input(self, question, password):
        worker = self.create_worker
        if worker is None:
            return
        answer, accepted = QInputDialog.getText(
            self, self._t("title"), question,
            QLineEdit.Password if password else QLineEdit.Normal,
        )
        worker.provide_interactive_input(answer if accepted else None)

    def _table(self, keys, table_type=QTableWidget):
        table = table_type(0, len(keys))
        table.setProperty("transfer_header_keys", keys)
        table.setHorizontalHeaderLabels([self._t(key) for key in keys])
        for col, key in enumerate(keys):
            table.horizontalHeaderItem(col).setToolTip(self._t("potential" if key == "potential_column" else key))
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().hide()
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setStretchLastSection(True)
        for col in range(2, len(keys)):
            table.setColumnWidth(col, 85 if keys[col] != "content" else 280)
        table.setWordWrap(False)
        return table

    def _options_button(self):
        button = QToolButton()
        button.setText(self._t("file_options"))
        button.setCheckable(True)
        button.setArrowType(Qt.RightArrow)
        button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        return button

    def _toolbar(self, source_edit, target_label, options_button):
        frame = QFrame()
        frame.setObjectName("transferToolbar")
        row = TransferToolbarLayout(frame)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(8)
        context = QWidget()
        context_row = QHBoxLayout(context)
        context_row.setContentsMargins(0, 0, 0, 0)
        context_row.setSpacing(10)
        context_row.addWidget(QLabel(self._t("source_lang")))
        source_edit.setFixedWidth(62)
        context_row.addWidget(source_edit)
        context_row.addWidget(QLabel(self._t("current_lang_caption")))
        target_label.setObjectName("targetLangCodeBadge")
        target_label.setMinimumWidth(42)
        target_label.setMaximumWidth(50)
        target_label.setToolTip(self._t("current_lang_tip"))
        context_row.addWidget(target_label)
        context_row.addWidget(options_button)
        row.addWidget(context)
        file_host = QWidget()
        files = QHBoxLayout(file_host)
        files.setContentsMargins(0, 0, 0, 0)
        files.setSpacing(6)
        row.addWidget(file_host)
        return frame, files

    def _action_group(self, title_key):
        frame = QGroupBox(self._t(title_key))
        frame.setObjectName("transferActionGroup")
        frame.setProperty("transfer_text_key", title_key)
        frame.setToolTip(self._t(title_key))
        row = TransferActionsLayout(frame)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(8)
        return frame, row

    @staticmethod
    def _toggle_panel(panel, button, checked):
        panel.setVisible(checked)
        button.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)

    def _button(self, key, callback, layout):
        button = QPushButton(self._t(key))
        button.setProperty("transfer_text_key", key)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def _refresh_target(self):
        self.target_label.setText(self._context()[1])
        self.links_target_label.setText(self.target_label.text())

    def _set_busy(self, busy):
        self._busy = busy
        for widget in (self.settings, self.source_lang, self.root_edit, self.load_btn, self.scan_options_btn,
                       self.open_scan_btn, self.export_scan_btn, self.delete_btn, self.clear_btn, self.report_btn,
                       self.tsv_btn, self.browse_btn, self.open_btn, self.tsv_path,
                       self.preview_btn, self.summary_edit, self.links_source_lang,
                       self.links_delay_spin,
                       self.remove_creation_btn, self.export_creation_btn, self.check_links_btn, self.options_btn,
                       self.autofill_box, self.autofill_btn):
            widget.setEnabled(not busy)
        self.table.setSortingEnabled(not busy)
        self.scan_btn.setEnabled(not busy and self.table.rowCount() > 0)
        self.next_btn.setEnabled(not busy and self.table.rowCount() > 0)
        self.retry_btn.setEnabled(not busy and any(row.get("status") == "error" for row in self._rows()))
        self.incomplete_btn.setEnabled(not busy and self.creation_table.rowCount() > 0)
        self.number_titles_btn.setEnabled(not busy and self.creation_table.rowCount() > 0)
        self.fill_content_btn.setEnabled(not busy and self.creation_table.rowCount() > 0)
        self.creation_table.setEnabled(not busy)
        self.create_btn.setEnabled(not busy and bool(self._creation_rows)
                                   and self._preview_context == self._creation_context())
        self.create_btn.setText(self._t("create"))
        self.sync_btn.setEnabled(not busy and bool(self._linked_categories)
                                 and self._linked_context == self._creation_context())
        self.stop_btn.setEnabled(busy)
        self._reindex()

    def _validate_context(self, *, creation=False):
        source, target, family = self._creation_context() if creation else self._context()
        if not re.fullmatch(r"[a-z][a-z0-9-]*", source) or source == target:
            self._warn("different_languages")
            return None
        return source, target, family

    def _warn(self, key, **values):
        QMessageBox.warning(self, self._t("title"), self._t(key, **values))

    def _write_log(self, text):
        log_message(self.log, text, debug_func=lambda value: debug(f"[Category transfer] {value}"))

    def _set_progress(self, text):
        # Diagnostics can contain a many-kilobyte request URL. Keep the footer
        # compact even for unexpected errors; the full message stays in the log.
        compact = re.sub(r"(https?://[^\s?]+)\?\S+", r"\1", str(text))
        compact = " ".join(compact.split())
        self.progress_label.setText(compact if len(compact) <= 180 else compact[:177] + "…")
        self.progress_label.setToolTip(str(text)[:1500])

    def _retry_notice(self, text):
        self._set_progress(text)

    def _reindex(self, *_args):
        self._rows_by_title = {
            self.table.item(row, 0).text(): row for row in range(self.table.rowCount())
            if self.table.item(row, 0) is not None
        }

    def _rows(self):
        return [self.table.item(row, 0).data(Qt.UserRole) for row in range(self.table.rowCount())]

    def load_categories(self):
        if self._busy:
            return
        context = self._validate_context()
        if not context:
            return
        if not self.root_edit.text().strip():
            self._warn("specify_root")
            return
        self.table.setRowCount(0)
        self._start_read(CategoryTransferReadWorker(
            source_lang=context[0], target_lang=context[1], family=context[2],
            delay=self.delay_spin.value(), category=self.root_edit.text(),
            depth=self.depth_spin.value(), skip_existing_level=self.skip_spin.value(),
            include_root=self.include_root.isChecked(),
        ), "load", 0)

    def scan_categories(self):
        self._scan_rows(self._rows())

    def retry_errors(self):
        self._scan_rows([row for row in self._rows() if row.get("status") == "error"])

    def _scan_rows(self, rows):
        if self._busy:
            return
        context = self._validate_context()
        if not context or not rows:
            return
        self._set_busy(False)
        self._start_read(CategoryTransferReadWorker(
            source_lang=context[0], target_lang=context[1], family=context[2],
            delay=self.delay_spin.value(), categories=rows,
        ), "scan", len(rows))

    def _start_read(self, worker, mode, total):
        self.read_worker = worker
        self._read_mode = mode
        self._set_busy(True)
        self.bar.setRange(0, total)
        self.bar.setValue(0)
        self._set_progress(self._t("loading" if mode == "load" else "scanning"))
        debug(f"[Category transfer] Start {mode}: {self._context()}, rows={total}, depth={self.depth_spin.value()}")
        worker.row_ready.connect(self._apply_row)
        worker.read_progress.connect(self._read_progress)
        worker.item_processed.connect(self._advance)
        worker.progress.connect(self._write_log)
        worker.retry_wait.connect(self._retry_notice)
        worker.finished.connect(self._read_finished)
        worker.start()

    def _apply_row(self, data):
        row = self._rows_by_title.get(data["source_title"])
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._rows_by_title[data["source_title"]] = row
        values = [data["source_title"], data.get("target_title", ""), data.get("qid", ""),
                  data["depth"], data.get("total"), data.get("potential"), self._t(data["status"])]
        for col, value in enumerate(values):
            item = QTableWidgetItem()
            if isinstance(value, int):
                item.setData(Qt.DisplayRole, value)
            else:
                item.setText("—" if value is None else str(value))
            if col == 0:
                item.setData(Qt.UserRole, dict(data))
            elif col == 2 and re.fullmatch(r"Q[1-9][0-9]*", str(value)):
                self._style_wikidata_link(item)
            item.setToolTip(str(value or ""))
            if col == 5 and data.get("target_articles"):
                item.setToolTip("\n".join(data["target_articles"][:100]))
            elif data.get("error"):
                item.setToolTip(data["error"])
            self.table.setItem(row, col, item)

    def _read_progress(self, payload):
        self.progress_label.setToolTip(payload["title"])
        if self._read_mode == "load":
            self._set_progress(self._t("found", count=payload["count"]))

    def _advance(self):
        self.bar.setValue(self.bar.value() + 1)

    def _read_finished(self):
        worker = self.read_worker
        self.read_worker = None
        if worker.failed:
            status = self._t("failed", error=worker.failure_message)
        elif worker._stop:
            status = self._t("stopped")
        else:
            status = self._t("read_done", count=self.table.rowCount(), errors=worker.error_count)
        self._set_progress(status)
        self._write_log(status)
        if worker.failed or worker.error_count:
            self.log_toggle.setChecked(True)
        self.bar.setRange(0, max(1, self.table.rowCount()))
        if not worker.failed and not worker._stop:
            self.bar.setValue(self.bar.maximum())
        self._set_busy(False)
        self._reset_changed_context()
        worker.deleteLater()

    def delete_selected(self):
        if self._busy:
            return
        for row in sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(row)
        self._reindex()
        self.scan_btn.setEnabled(self.table.rowCount() > 0)
        self._set_busy(False)

    def clear_empty(self):
        if self._busy:
            return
        for row in range(self.table.rowCount() - 1, -1, -1):
            data = self.table.item(row, 0).data(Qt.UserRole)
            # Errors and unscanned rows remain visible for retry.
            if data.get("status") == "scanned" and data.get("potential") == 0:
                self.table.removeRow(row)
        self._reindex()
        self.scan_btn.setEnabled(self.table.rowCount() > 0)
        self._set_busy(False)

    def copy_selected(self):
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        text = "\n".join("\t".join(self.table.item(row, col).text()
                                   for col in range(self.table.columnCount())) for row in rows)
        QGuiApplication.clipboard().setText(text)

    def export_report(self):
        if self._busy:
            return
        path, _ = QFileDialog.getSaveFileName(self, self._t("export_report"), "category-transfer-results.tsv", "TSV (*.tsv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle, delimiter="\t")
                writer.writerow([self.table.horizontalHeaderItem(col).text() for col in range(7)])
                for row in self._rows():
                    writer.writerow([row["source_title"], row.get("target_title", ""), row.get("qid", ""),
                                     row["depth"], row.get("total"), row.get("potential"), row["status"]])
        except Exception as error:
            self._warn("failed", error=error)

    def import_report(self):
        if self._busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, self._t("open_report"), "", "TSV (*.tsv)")
        if not path:
            return
        rows = []
        try:
            with open(path, encoding="utf-8-sig", newline="") as handle:
                fields_list = list(csv.reader(handle, delimiter="\t"))
            if not fields_list or len(fields_list[0]) != 7:
                raise ValueError(self._t("report_invalid", line=1))
            for line, fields in enumerate(fields_list[1:], 2):
                if not fields or not any(field.strip() for field in fields):
                    continue
                if len(fields) != 7 or not fields[0].strip() or fields[6] not in {"loaded", "scanned", "error"}:
                    raise ValueError(self._t("report_invalid", line=line))
                depth, total, potential = (int(value) if value else None for value in fields[3:6])
                if depth is None or any(value is not None and value < 0 for value in (depth, total, potential)):
                    raise ValueError(self._t("report_invalid", line=line))
                if fields[2] and not re.fullmatch(r"Q[1-9][0-9]*", fields[2]):
                    raise ValueError(self._t("report_invalid", line=line))
                rows.append(dict(source_title=fields[0].strip(), target_title=fields[1].strip(), qid=fields[2],
                                 depth=depth, total=total, potential=potential, status=fields[6]))
            if not rows:
                raise ValueError(self._t("tsv_empty"))
        except Exception as error:
            self._warn("failed", error=error)
            return
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self._rows_by_title.clear()
        for row in rows:
            self._apply_row(row)
        self._set_busy(False)
        self._write_log(self._t("report_loaded", count=len(rows), path=path))

    def prepare_tsv(self):
        if self._busy:
            return
        candidates = [row for row in self._rows() if row.get("status") == "scanned"
                      and (row.get("potential") or 0) > 0]
        if not candidates:
            self._warn("no_candidates")
            return
        path, _ = QFileDialog.getSaveFileName(self, self._t("prepare_tsv"), "category-transfer-create.tsv", "TSV (*.tsv)")
        if not path:
            return
        try:
            export_creation_tsv(path, candidates)
        except Exception as error:
            self._warn("failed", error=error)
            return
        self._write_log(self._t("tsv_saved", count=len(candidates), path=path))

    def choose_tsv(self):
        if self._busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, self._t("choose_tsv"), "", "TSV (*.tsv)")
        if path:
            self.tsv_path.setText(path)
            self.import_tsv()

    def import_tsv(self):
        if self._busy:
            return
        try:
            rows = read_creation_tsv(self.tsv_path.text().strip(), allow_untranslated=True)
        except Exception as error:
            self._warn("failed", error=error)
            return
        self._invalidate_creation()
        self._fill_creation_table(rows)
        self._table_dirty = True

    def open_tsv(self):
        if self.tsv_path.text().strip():
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.tsv_path.text().strip()))

    def _invalidate_creation(self, *_args, clear_table=True):
        self._creation_rows = ()
        self._preview_rows = ()
        self._existing_linked_categories = []
        self._preview_context = None
        self._linked_categories = []
        self._linked_pairs = {}
        self._linked_context = None
        self.sync_btn.setEnabled(False)
        if clear_table:
            self.creation_table.blockSignals(True)
            self.creation_table.setRowCount(0)
            self.creation_table.blockSignals(False)
            self._table_dirty = False
        else:
            self._table_dirty = True
            self.creation_table.blockSignals(True)
            try:
                for row in range(self.creation_table.rowCount()):
                    item = self.creation_table.item(row, 5)
                    if item:
                        item.setData(Qt.UserRole + 1, item.data(Qt.UserRole))
                        item.setData(Qt.UserRole, "editing")
                        item.setText(self._t("editing"))
                        item.setToolTip("")
                        self._highlight_incomplete(row, "editing")
            finally:
                self.creation_table.blockSignals(False)
        self.create_btn.setEnabled(False)
        self._refresh_creation_summary()

    def _open_links_page(self):
        tabs = getattr(self.parent_window, "tabs", None)
        if tabs is not None:
            tabs.setCurrentWidget(self.creation_stage)

    def open_links(self):
        if self._busy or not self._rows():
            return
        # Carry precisely the curated list, including linked and unscanned rows.
        rows = tuple(CategoryCreationRow(row["source_title"], row.get("target_title", ""), "")
                     for row in self._rows())
        self.links_source_lang.setText(self.source_lang.text())
        self.tsv_path.clear()
        self._invalidate_creation()
        self._fill_creation_table(rows)
        self._table_dirty = True
        self._open_links_page()

    def _fill_creation_table(self, rows, status="editing"):
        self.creation_table.blockSignals(True)
        try:
            self.creation_table.setRowCount(len(rows))
            for row, data in enumerate(rows):
                for col, value in enumerate((data.source_title, data.target_title, data.content, "", "", self._t(status))):
                    item = QTableWidgetItem(value)
                    item.setToolTip(value)
                    if col >= 3:
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    if col == 5:
                        item.setData(Qt.UserRole, status)
                    self.creation_table.setItem(row, col, item)
        finally:
            self.creation_table.blockSignals(False)
        self._refresh_creation_summary()

    def _refresh_creation_summary(self):
        counts = {"ready": 0, "drafts": 0, "linked": 0, "errors": 0, "unchecked": 0}
        for row in range(self.creation_table.rowCount()):
            item = self.creation_table.item(row, 5)
            code = item.data(Qt.UserRole) if item else "editing"
            if code in {"ready_create", "ready_link", "ready_labels"}:
                counts["ready"] += 1
            elif code in {"needs_translation", "needs_content"}:
                counts["drafts"] += 1
            elif code in {"already_linked", "created", "linked", "labels_updated"}:
                counts["linked"] += 1
            elif code in {"error", "unlinked"}:
                counts["errors"] += 1
            else:
                counts["unchecked"] += 1
        self.creation_summary.setText(self._t("list_summary", total=self.creation_table.rowCount(), **counts))

    def _show_creation_detail(self):
        row = self.creation_table.currentRow()
        item = self.creation_table.item(row, 5) if row >= 0 else None
        self.creation_detail.setText((item.toolTip() or item.text()) if item else self._t("row_detail_hint"))

    def _set_creation_qids(self, row, source_qid, target_qid):
        for col, qid in ((3, source_qid), (4, target_qid)):
            item = QTableWidgetItem(qid)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setToolTip(qid)
            item.setTextAlignment(Qt.AlignCenter)
            if re.fullmatch(r"Q[1-9][0-9]*", qid):
                self._style_wikidata_link(item)
            self.creation_table.setItem(row, col, item)

    def _open_creation_wd(self, row, col):
        item = self.creation_table.item(row, col)
        if col in (3, 4) and item and re.fullmatch(r"Q[1-9][0-9]*", item.text()):
            QDesktopServices.openUrl(QUrl("https://www.wikidata.org/wiki/" + item.text()))

    def _creation_link_cursor(self, row, col):
        item = self.creation_table.item(row, col)
        linked = col in (3, 4) and item and re.fullmatch(r"Q[1-9][0-9]*", item.text())
        self.creation_table.viewport().setCursor(Qt.PointingHandCursor if linked else Qt.ArrowCursor)

    def select_next_incomplete(self):
        count = self.creation_table.rowCount()
        if self._busy or not count:
            return
        start = self.creation_table.currentRow() + 1
        for offset in range(count):
            row = (start + offset) % count
            status = self.creation_table.item(row, 5)
            code = status.data(Qt.UserRole)
            if code == "editing":
                code = status.data(Qt.UserRole + 1)
            missing_title = not self.creation_table.item(row, 1).text().strip()
            missing_content = code == "needs_content" and not self.creation_table.item(row, 2).text().strip()
            if missing_title or missing_content:
                col = 1 if missing_title else 2
                item = self.creation_table.item(row, col)
                self.creation_table.setCurrentCell(row, col)
                self.creation_table.scrollToItem(item)
                self.creation_table.editItem(item)
                return

    def _highlight_incomplete(self, row, code):
        color = QColor("#fff1c9" if getattr(self.parent_window, "_theme_mode", "teal") == "light" else "#443d26")
        blocked = self.creation_table.blockSignals(True)
        try:
            for col in (1, 2):
                item = self.creation_table.item(row, col)
                if item:
                    missing = (col == 1 and code == "needs_translation") or (col == 2 and code == "needs_content")
                    item.setBackground(QBrush(color) if missing else QBrush())
        finally:
            self.creation_table.blockSignals(blocked)

    def _table_creation_rows(self):
        return tuple(CategoryCreationRow(*(self.creation_table.item(row, col).text().strip() if col < 2
                                         else self.creation_table.item(row, col).text() for col in range(3)))
                     for row in range(self.creation_table.rowCount()))

    def _fill_content_rows(self, indices, value, only_empty=True):
        if self._busy:
            return
        changed = []
        blocked = self.creation_table.blockSignals(True)
        try:
            for row in indices:
                item = self.creation_table.item(row, 2)
                if item and item.text() != value and (not only_empty or not item.text().strip()):
                    item.setText(value)
                    item.setToolTip(value)
                    changed.append(item)
        finally:
            self.creation_table.blockSignals(blocked)
        if changed:
            self._creation_edited(changed[0])
            self._write_log(self._t("content_filled", count=len(changed)))

    def _fill_content_range(self, start, end, value, only_empty):
        self._fill_content_rows(range(max(0, start), min(end + 1, self.creation_table.rowCount())), value, only_empty)

    def fill_content(self):
        if self._busy or not self.creation_table.rowCount():
            return
        selected = sorted({index.row() for index in self.creation_table.selectedIndexes()})
        dialog = QDialog(self.creation_stage)
        dialog.setObjectName("fillCategoryContentDialog")
        dialog.setWindowTitle(self._t("fill_content"))
        dialog.resize(600, 380)
        layout = QVBoxLayout(dialog)
        help_label = QLabel(self._t("fill_content_help"))
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        editor = QPlainTextEdit()
        item = self.creation_table.item(self.creation_table.currentRow(), 2)
        editor.setPlainText(item.text() if item else "")
        layout.addWidget(editor, 1)
        selected_only = QCheckBox(self._t("selected_only", count=len(selected)))
        selected_only.setEnabled(bool(selected))
        selected_only.setChecked(len(selected) > 1)
        layout.addWidget(selected_only)
        only_empty = QCheckBox(self._t("content_only_empty"))
        only_empty.setChecked(True)
        layout.addWidget(only_empty)
        buttons = QDialogButtonBox()
        apply_button = buttons.addButton(self._t("fill_apply"), QDialogButtonBox.AcceptRole)
        buttons.addButton(self._t("cancel"), QDialogButtonBox.RejectRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        apply_button.setEnabled(bool(editor.toPlainText().strip()))
        editor.textChanged.connect(lambda: apply_button.setEnabled(bool(editor.toPlainText().strip())))
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted:
            indices = selected if selected_only.isChecked() else range(self.creation_table.rowCount())
            self._fill_content_rows(indices, editor.toPlainText(), only_empty.isChecked())

    def copy_first_content(self):
        if self._busy or not self.creation_table.rowCount():
            return
        value = self.creation_table.item(0, 2).text()
        if not value.strip():
            self._warn("first_content_required")
            return
        self._fill_content_range(1, self.creation_table.rowCount() - 1, value, False)

    def load_source_content(self):
        if self._busy or not self.creation_table.rowCount():
            return
        context = self._validate_context(creation=True)
        if context is None:
            return
        rows = tuple((index, row.source_title) for index, row in enumerate(self._table_creation_rows())
                     if not self.source_content_empty.isChecked() or not row.content.strip())
        if not rows:
            self._write_log(self._t("no_empty_content"))
            return
        self._invalidate_creation(clear_table=False)
        worker = CategoryTextReadWorker(rows=rows, source_lang=context[0], target_lang=context[1],
                                        family=context[2], delay=self.links_delay_spin.value())
        self.content_worker = worker
        worker.content_ready.connect(self._source_content_ready)
        worker.item_processed.connect(self._advance)
        worker.progress.connect(self._write_log)
        worker.retry_wait.connect(self._retry_notice)
        worker.finished.connect(self._source_content_finished)
        self._set_busy(True)
        self.bar.setRange(0, len(rows))
        self.bar.setValue(0)
        self._set_progress(self._t("loading_source_content"))
        debug(f"[Category transfer] Load source wikitext: {context}, rows={len(rows)}")
        worker.start()

    def _source_content_ready(self, payload):
        item = self.creation_table.item(payload["index"], 2)
        if item:
            blocked = self.creation_table.blockSignals(True)
            item.setText(payload["content"])
            item.setToolTip(payload["content"])
            self.creation_table.blockSignals(blocked)
            self._table_dirty = True

    def _source_content_finished(self):
        worker = self.content_worker
        self.content_worker = None
        status = (self._t("failed", error=worker.failure_message) if worker.failed
                  else self._t("stopped") if worker._stop else self._t("source_content_done", **worker.stats))
        self._set_progress(status)
        self._write_log(status)
        self._set_busy(False)
        self._reset_changed_context()
        worker.deleteLater()

    def fill_numbered_titles(self):
        if self._busy or not self.creation_table.rowCount():
            return
        result = title_fill_dialog(self.creation_stage, self._table_creation_rows(),
                                   {index.row() for index in self.creation_table.selectedIndexes()},
                                   self._t, self.creation_table.styleSheet(), self.title_template_edit.text())
        if result is not None:
            plan, template = result
            self.title_template_edit.setText(template)
            self._apply_numbered_titles(plan)

    def _apply_numbered_titles(self, plan):
        if self._busy:
            return
        changed = []
        blocked = self.creation_table.blockSignals(True)
        try:
            for proposal in plan:
                if proposal.status != "number_fill":
                    continue
                item = self.creation_table.item(proposal.index, 1)
                if item and not item.text().strip():
                    item.setText(proposal.target_title)
                    item.setToolTip(proposal.target_title)
                    changed.append(item)
        finally:
            self.creation_table.blockSignals(blocked)
        if changed:
            # A single edit invalidates all write and sync snapshots once.
            self._creation_edited(changed[0])
            self._set_progress(self._t("number_done", count=len(changed)))
            self._write_log(self._t("number_done", count=len(changed)))

    def _creation_edited(self, item):
        if self._busy or item.column() >= 3:
            return
        self._table_dirty = True
        # Preserve editable input, but invalidate all write and handoff snapshots.
        self._creation_rows = ()
        self._preview_context = self._linked_context = None
        self._linked_categories = []
        self._linked_pairs = {}
        self.create_btn.setEnabled(False)
        self.sync_btn.setEnabled(False)
        for row in range(self.creation_table.rowCount()):
            status = self.creation_table.item(row, 5)
            if status:
                if status.data(Qt.UserRole) != "editing":
                    status.setData(Qt.UserRole + 1, status.data(Qt.UserRole))
                status.setText(self._t("editing"))
                status.setData(Qt.UserRole, "editing")
                status.setToolTip("")
                self._highlight_incomplete(row, "editing")
        self._refresh_creation_summary()
        self._show_creation_detail()

    def delete_creation_selected(self):
        if self._busy:
            return
        rows = sorted({index.row() for index in self.creation_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.creation_table.removeRow(row)
        if rows:
            # Even an empty table must supersede a previously imported file.
            self._table_dirty = True
            self._creation_rows = ()
            self._preview_context = self._linked_context = None
            self._linked_categories = []
            self._linked_pairs = {}
            self._set_busy(False)
            self._refresh_creation_summary()

    def export_plan(self):
        if self._busy or not self.creation_table.rowCount():
            return
        path, _ = QFileDialog.getSaveFileName(self, self._t("export_plan"), "category-transfer-create.tsv", "TSV (*.tsv)")
        if path:
            try:
                with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                    csv.writer(handle, delimiter="\t").writerows(
                        (row.source_title, row.target_title, row.content) for row in self._table_creation_rows())
            except Exception as error:
                self._warn("failed", error=error)

    def preview_creation(self):
        if self._busy or not self._validate_context(creation=True):
            return
        try:
            rows = (self._table_creation_rows() if self._table_dirty
                    else read_creation_tsv(self.tsv_path.text().strip(), allow_untranslated=True))
            if not rows:
                raise ValueError(self._t("tsv_empty"))
        except Exception as error:
            self._warn("failed", error=error)
            return
        self._invalidate_creation()
        self._table_dirty = True
        self._preview_rows = rows
        self._preview_context = self._creation_context()
        self._fill_creation_table(rows, "checking")
        source, target, family = self._creation_context()
        worker = CategoryTransferPreviewWorker(rows=rows, source_lang=source, target_lang=target,
                                              family=family, delay=self.links_delay_spin.value())
        self.preview_worker = worker
        self._set_busy(True)
        self.bar.setRange(0, len(rows))
        self.bar.setValue(0)
        self._set_progress(self._t("checking"))
        debug(f"[Category transfer] Start preview: {self._creation_context()}, rows={len(rows)}")
        worker.row_ready.connect(self._preview_result)
        worker.progress.connect(self._write_log)
        worker.retry_wait.connect(self._retry_notice)
        worker.finished.connect(self._preview_finished)
        worker.start()

    def _preview_result(self, result):
        row = result["index"]
        data = result["row"]
        for col, title in enumerate((data.source_title, data.target_title)):
            item = QTableWidgetItem(title)
            item.setToolTip(title)
            self.creation_table.setItem(row, col, item)
        status = QTableWidgetItem(self._t(result["status"]))
        status.setFlags(status.flags() & ~Qt.ItemIsEditable)
        status.setData(Qt.UserRole, result["status"])
        status.setToolTip(result.get("error", result.get("detail", "")))
        self.creation_table.setItem(row, 5, status)
        self._set_creation_qids(row, result.get("source_qid", ""), result.get("target_qid", ""))
        self._highlight_incomplete(row, result["status"])
        self._refresh_creation_summary()
        self._show_creation_detail()
        self._advance()

    def _preview_finished(self):
        worker = self.preview_worker
        self.preview_worker = None
        if worker.failed or worker._stop:
            self._creation_rows = ()
            self._preview_context = None
            status = self._t("failed", error=worker.failure_message) if worker.failed else self._t("stopped")
            self.creation_table.blockSignals(True)
            try:
                for row in range(self.creation_table.rowCount()):
                    item = self.creation_table.item(row, 5)
                    if item and item.data(Qt.UserRole) == "checking":
                        item.setText(self._t("editing"))
                        item.setData(Qt.UserRole, "editing")
                        item.setToolTip("")
            finally:
                self.creation_table.blockSignals(False)
            self._refresh_creation_summary()
            self._show_creation_detail()
        else:
            self._preview_rows = tuple(result["row"] for result in worker.results)
            self._creation_rows = tuple(result["row"] for result in worker.results
                                        if result["status"] in {"ready_create", "ready_link", "ready_labels"})
            self._existing_linked_categories = [result["row"].target_title for result in worker.results
                                               if result["status"] == "already_linked"]
            self._linked_categories = list(self._existing_linked_categories)
            self._linked_pairs = {result["row"].target_title: CategoryPair(
                result["row"].source_title, result["row"].target_title, result.get("qid", ""))
                for result in worker.results if result["status"] == "already_linked"}
            self._linked_context = self._preview_context
            status = self._t("preview_done", missing=len(self._creation_rows),
                             linked=len(self._existing_linked_categories),
                             drafts=sum(result["status"] in {"needs_translation", "needs_content"} for result in worker.results),
                             errors=sum(result["status"] == "error" for result in worker.results))
        self._set_progress(self._t("preview_ready") if not worker.failed and not worker._stop else status)
        self._write_log(status)
        if worker.failed or any(result["status"] == "error" for result in worker.results):
            self.log_toggle.setChecked(True)
        self._set_busy(False)
        self._reset_changed_context()
        worker.deleteLater()

    def start_creation(self):
        if self._busy or not self._creation_rows or self._preview_context != self._creation_context():
            return
        user = getattr(self.parent_window, "current_user", None)
        password = getattr(self.parent_window, "current_password", None)
        if not user or not password:
            self._warn("login_required")
            return
        if not self._confirm_creation():
            return
        source, target, family = self._creation_context()
        apply_pwb_config(target, family)
        self._linked_categories = list(self._existing_linked_categories)
        self._linked_context = self._creation_context()
        self._creation_row_results = {}
        worker = CategoryTransferCreateWorker(
            rows=self._creation_rows, username=user, password=password,
            source_lang=source, target_lang=target, family=family, summary=self.summary_edit.text().strip(),
        )
        self.create_worker = worker
        self._set_busy(True)
        self.bar.setRange(0, len(self._creation_rows))
        self.bar.setValue(0)
        self._set_progress(self._t("creating"))
        debug(f"[Category transfer] Start creation: {self._creation_context()}, rows={len(self._creation_rows)}")
        worker.category_result.connect(self._creation_result)
        worker.interactive_input_requested.connect(self._interactive_input)
        worker.progress.connect(self._write_log)
        worker.item_processed.connect(self._advance)
        worker.finished.connect(self._creation_finished)
        worker.start()

    def _creation_result(self, result):
        self._creation_row_results[result["source_title"]] = result
        for row, data in enumerate(self._preview_rows):
            if data.source_title == result["source_title"]:
                status = QTableWidgetItem(self._t(result["status"]))
                status.setFlags(status.flags() & ~Qt.ItemIsEditable)
                status.setData(Qt.UserRole, result["status"])
                status.setToolTip(result.get("error", result.get("qid", "")))
                self.creation_table.setItem(row, 5, status)
                if "source_qid" in result or "target_qid" in result:
                    self._set_creation_qids(row, result.get("source_qid", ""), result.get("target_qid", ""))
                self.creation_table.setItem(row, 1, QTableWidgetItem(result["target_title"]))
                self._refresh_creation_summary()
                break

    def _creation_finished(self):
        worker = self.create_worker
        self.create_worker = None
        self._linked_categories = list(dict.fromkeys(self._existing_linked_categories + worker.linked_categories))
        for row in self._creation_rows:
            result = self._creation_row_results.get(row.source_title, {})
            if result.get("status") in {"created", "linked", "labels_updated", "already_linked"}:
                self._linked_pairs[row.target_title] = CategoryPair(
                    row.source_title, row.target_title, result.get("qid", ""))
        self._existing_linked_categories = list(self._linked_categories)
        self._creation_rows = tuple(row for row in self._creation_rows
                                    if self._creation_row_results.get(row.source_title, {}).get("status")
                                    not in {"created", "linked", "labels_updated", "already_linked"})
        status = (self._t("failed", error=worker.failure_message) if worker.failed
                  else self._t("stopped") if worker._stop else self._t("creation_done", **{"already": 0, "labels": 0, **worker.stats}))
        self._set_progress(status)
        self._write_log(status)
        recorder = getattr(self.parent_window, "record_operation", None)
        if recorder and worker.stats["created"]:
            recorder("create", worker.stats["created"])
        self._set_busy(False)
        self._reset_changed_context()
        worker.deleteLater()
        if self._linked_categories and self._linked_context == self._creation_context():
            if self._offer_sync(len(self._linked_categories)):
                self.open_sync()

    def _confirm_creation(self):
        dialog = QDialog(self.creation_stage)
        dialog.setObjectName("confirmCategoryCreationDialog")
        dialog.setWindowTitle(self._t("links_title"))
        dialog.setMinimumWidth(540)
        layout = QVBoxLayout(dialog)
        layout.setSpacing(14)
        heading = QLabel(self._t("confirm_heading", count=len(self._creation_rows),
                                source=self._creation_context()[0], target=self._creation_context()[1]))
        heading.setWordWrap(True)
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)
        for key in ("confirm_pages", "confirm_links", "confirm_items"):
            label = QLabel(self._t(key))
            label.setWordWrap(True)
            label.setTextFormat(Qt.PlainText)
            layout.addWidget(label)
        buttons = QDialogButtonBox()
        buttons.addButton(self._t("create"), QDialogButtonBox.AcceptRole)
        cancel = buttons.addButton(self._t("cancel"), QDialogButtonBox.RejectRole)
        cancel.setDefault(True)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog.exec() == QDialog.Accepted

    def _offer_sync(self, count):
        box = QMessageBox(self.creation_stage)
        box.setWindowTitle(self._t("title"))
        box.setIcon(QMessageBox.Question)
        box.setText(self._t("offer_sync", count=count))
        yes = box.addButton(self._t("next_sync"), QMessageBox.AcceptRole)
        box.addButton(self._t("later"), QMessageBox.RejectRole)
        box.exec()
        return box.clickedButton() == yes

    def open_sync(self):
        if self._busy or not self._linked_categories or self._linked_context != self._creation_context():
            return
        sync = getattr(self.parent_window, "category_content_sync_tab", None)
        if sync is None:
            return
        if (sync.sync_worker and sync.sync_worker.isRunning()) or (sync.preview_worker and sync.preview_worker.isRunning()):
            self._warn("sync_busy")
            return
        sync.load_transfer_categories(self._linked_categories, self._linked_context[0],
                                      pairs=tuple(self._linked_pairs.values()))
        self.parent_window.tabs.setCurrentWidget(sync)

    def open_interwiki(self):
        tab = getattr(self.parent_window, "category_interwiki_tab", None)
        if self._busy or tab is None or not self.creation_table.rowCount():
            return
        if tab.load_pairs(self._table_creation_rows(), self._creation_context()[0]):
            self.parent_window.tabs.setCurrentWidget(tab)
        else:
            self._warn("interwiki_busy")

    def stop(self):
        for worker in (self.read_worker, self.create_worker, self.preview_worker, self.content_worker):
            if worker and worker.isRunning():
                worker.request_stop()
        self.stop_btn.setEnabled(False)
        self._set_progress(self._t("stopping"))

    def _source_changed(self, *_args):
        if not self._busy:
            self.table.setRowCount(0)
            self._set_busy(False)

    def _links_source_changed(self, *_args):
        if not self._busy:
            self._invalidate_creation(clear_table=False)
            self._set_busy(False)

    def _open_category(self, row, col):
        if col not in (0, 1, 2):
            return
        value = self.table.item(row, col).text()
        if not value:
            return
        if col == 2:
            if not re.fullmatch(r"Q[1-9][0-9]*", value):
                return
            url = QUrl(f"https://www.wikidata.org/wiki/{value}")
        else:
            from urllib.parse import quote
            from ...core.api_client import WikimediaAPIClient
            lang = self._context()[0 if col == 0 else 1]
            host = WikimediaAPIClient()._build_api_url(self._context()[2], lang).split("/w/")[0]
            url = QUrl(f"{host}/wiki/{quote(value.replace(' ', '_'))}")
        QDesktopServices.openUrl(url)

    def _style_wikidata_link(self, item):
        item.setForeground(QColor("#1469a3" if getattr(self.parent_window, "_theme_mode", "teal") == "light" else "#72c9f0"))
        font = item.font()
        font.setUnderline(True)
        item.setFont(font)

    def _link_cursor(self, row, col):
        item = self.table.item(row, col)
        linked = col == 2 and item and re.fullmatch(r"Q[1-9][0-9]*", item.text())
        self.table.viewport().setCursor(Qt.PointingHandCursor if linked else Qt.ArrowCursor)

    def _apply_table_theme(self):
        theme = getattr(self.parent_window, "_theme_mode", "teal")
        light = theme == "light"
        base, alternate, text, header, selected = (
            ("#ffffff", "#eff5f9", "#1d2a35", "#e5eef5", "#c4dfef") if light
            else ("#20252c", "#272e36", "#e5e8ed", "#303945", "#3a526c") if theme == "dark"
            else ("#092c3d", "#11394b", "#dceef5", "#164357", "#22677a")
        )
        for table in (self.table, self.creation_table):
            table.setStyleSheet(
                f"QTableWidget {{ background: {base}; alternate-background-color: {alternate}; color: {text}; "
                f"padding: 0px; border: 1px solid {header}; border-radius: 0px; }}"
                f"QTableWidget::item:selected {{ background: {selected}; color: {text}; }}"
                f"QHeaderView {{ border: none; padding: 0px; }}"
                f"QHeaderView::section {{ background: {header}; color: {text}; padding: 5px; "
                f"border: none; border-right: 1px solid {base}; border-bottom: 1px solid {base}; }}"
            )
        for source, badge in ((self.source_lang, self.target_label), (self.links_source_lang, self.links_target_label)):
            style_transfer_languages(source, badge, theme)
        self.autofill_tabs.setStyleSheet(
            f"QTabWidget::pane {{ background: {base}; border: 1px solid {header}; border-radius: 4px; }}"
            f"QTabBar::tab {{ background: {header}; color: {text}; border: 1px solid {base}; "
            "padding: 6px 10px; border-top-left-radius: 4px; border-top-right-radius: 4px; }"
            f"QTabBar::tab:selected {{ background: {selected}; font-weight: bold; }}"
        )
        for spin in (self.delay_spin, self.links_delay_spin):
            spin.setStyleSheet(f"QDoubleSpinBox {{ background: {base}; color: {text}; border: 1px solid {header}; "
                              "border-radius: 4px; padding: 3px; min-height: 24px; }")
        group_style = f"QFrame#transferToolbar, QFrame#transferActionGroup {{ border: 1px solid {header}; border-radius: 4px; }}"
        for page in (self, self.creation_stage):
            for frame in page.findChildren(QFrame):
                if frame.objectName() in {"transferToolbar", "transferActionGroup"}:
                    frame.setStyleSheet(group_style)
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 2)
            if item and re.fullmatch(r"Q[1-9][0-9]*", item.text()):
                self._style_wikidata_link(item)
        for row in range(self.creation_table.rowCount()):
            for col in (3, 4):
                qid = self.creation_table.item(row, col)
                if qid and re.fullmatch(r"Q[1-9][0-9]*", qid.text()):
                    self._style_wikidata_link(qid)
            item = self.creation_table.item(row, 5)
            self._highlight_incomplete(row, item.data(Qt.UserRole) if item else "editing")

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.StyleChange, QEvent.PaletteChange) and hasattr(self, "creation_table"):
            # Local stylesheet changes also emit StyleChange; avoid a recursion.
            if not getattr(self, "_applying_theme", False):
                self._applying_theme = True
                try:
                    self._apply_table_theme()
                finally:
                    self._applying_theme = False

    def showEvent(self, event):
        super().showEvent(event)
        self._show_stage(self)

    def _show_stage(self, stage):
        # A single log and progress area follows the active stage; workers and
        # snapshots belong to the controller rather than duplicated widgets.
        stage.layout().addWidget(self.footer)
        self.footer.show()
        self._apply_table_theme()
        self.refresh_localized_texts()

    def refresh_localized_texts(self):
        lang = getattr(self.parent_window, "_ui_lang", "ru")
        if lang != self._last_ui_lang:
            self._last_ui_lang = lang
            self.log_toggle.setText(self._t("log"))
            self.options_btn.setText(self._t("file_options"))
            self.scan_options_btn.setText(self._t("file_options"))
            self.autofill_btn.setText(self._t("autofill"))
            for index, key in enumerate(("titles_column", "content_column")):
                self.autofill_tabs.setTabText(index, self._t(key))
            self.source_content_empty.setText(self._t("source_content_empty"))
            self.source_content_empty.setToolTip(self._t("source_content_tip"))
            self.autofill_title_help.setText(self._t("autofill_title_help"))
            self.title_template_edit.setPlaceholderText(self._t("number_target_example"))
            self.copy_first_btn.setToolTip(self._t("copy_first_tip"))
            self.load_text_btn.setToolTip(self._t("source_content_tip"))
            self.export_scan_btn.setText(self._t("export_plan"))
            self.creation_hint.setText(self._t("creation_hint"))
            for table in (self.table, self.creation_table):
                for col, key in enumerate(table.property("transfer_header_keys")):
                    table.horizontalHeaderItem(col).setText(self._t(key))
                    table.horizontalHeaderItem(col).setToolTip(self._t("potential" if key == "potential_column" else key))
            for page in (self, self.creation_stage):
                for group in page.findChildren(QGroupBox):
                    key = group.property("transfer_text_key")
                    if key:
                        group.setTitle(self._t(key))
                for button in page.findChildren(QPushButton) + page.findChildren(QAction):
                    key = button.property("transfer_text_key")
                    if key:
                        button.setText(self._t(key))
            sorting = self.table.isSortingEnabled()
            self.table.setSortingEnabled(False)
            for row in range(self.table.rowCount()):
                data = self.table.item(row, 0).data(Qt.UserRole)
                self.table.item(row, 6).setText(self._t(data["status"]))
            self.table.setSortingEnabled(sorting)
            for row in range(self.creation_table.rowCount()):
                item = self.creation_table.item(row, 5)
                if item and item.data(Qt.UserRole):
                    item.setText(self._t(item.data(Qt.UserRole)))
            self._refresh_target()
            self._refresh_creation_summary()
            self._show_creation_detail()
            self._set_busy(self._busy)

    def update_language(self, lang):
        if lang != self.current_lang:
            old_default = default_create_summary(self._summary_lang)
            if self.summary_edit.text() == old_default:
                self.summary_edit.setText(default_create_summary(lang))
            self._summary_lang = lang
            self.current_lang = lang
            self._context_changed()

    def update_family(self, family):
        if family != self.current_family:
            self.current_family = family
            self._context_changed()

    def _context_changed(self):
        self._refresh_target()
        if self._busy:
            # The worker keeps its original immutable context until it stops.
            self._pending_context_reset = True
            self.stop()
        else:
            self.table.setRowCount(0)
            self._invalidate_creation(clear_table=False)
            self._set_busy(False)

    def _reset_changed_context(self):
        if self._pending_context_reset:
            self._pending_context_reset = False
            self.table.setRowCount(0)
            self._invalidate_creation(clear_table=False)
            self._linked_categories = []
            self._set_busy(False)

    def set_auth_data(self, username, lang, family):
        self.current_user = username
        self.update_language(lang)
        self.update_family(family)

    def clear_auth_data(self):
        self.current_user = None
        if self._busy:
            self._pending_context_reset = True
            self.stop()
        else:
            self._invalidate_creation(clear_table=False)
