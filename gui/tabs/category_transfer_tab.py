"""Discover, review, create and link categories, then hand off to content sync."""

from __future__ import annotations

import csv
import re

from PySide6.QtCore import Qt, QUrl, QEvent
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices, QKeySequence, QShortcut, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame, QGroupBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QStyledItemDelegate, QPlainTextEdit,
    QTextEdit, QVBoxLayout, QWidget, QToolButton, QSizePolicy, QLayout,
)

from ...core.category_transfer import (
    CategoryCreationRow, CategoryPair, category_action_plan, category_outcome_counts, export_creation_tsv, read_creation_tsv,
    parse_scan_list,
    parse_scan_count, scan_count_value, scan_available_count,
)
from ...core.localization import translate_key
from ...core.pywikibot_config import apply_pwb_config
from ...utils import debug, default_create_summary, resolve_project_language
from ...workers.category_transfer_worker import (
    CategoryArticlePresenceWorker, CategoryTextReadWorker, CategoryTransferCreateWorker,
    CategoryTransferPreviewWorker, CategoryTransferReadWorker,
)
from ..widgets.ui_helpers import category_table_palette, category_table_style, create_spin_control, add_info_button, inc_progress, init_progress, log_message, set_reading_progress, style_transfer_languages
from ..widgets.shared_panels import FileInputRow
from ..widgets.category_stage import CategoryLogPanel, CategoryParametersPanel, CategoryProgressFooter, CategoryRunRow, CategoryInlineParameters, CategoryTableToolbar, CategoryWorkspace, CategoryWorkspaceFrame, category_actions, category_toolbar, highlight_next_action, refresh_category_toolbar, set_category_button_tooltip, refresh_category_button_tooltips
from ..widgets.category_autofill import title_fill_dialog
from ..widgets.table_history import TableHistory, table_action, pause_sorting
from ..widgets.category_plan import category_plan_widget
from ..widgets.category_articles import CategoryArticlesDialog
from ..widgets.category_text_dialog import CategoryTextDialog
from ..widgets.category_creation_table import CategoryCandidatesTable, CategoryCreationTable, CategoryCountItem, TransferActionsLayout, TransferAutofillTabs


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
        self.article_worker = None
        self._article_workers = set()
        self._busy = False
        self._read_mode = ""
        self._append_read = False
        self._scan_load_transaction = None
        self._creation_rows = ()
        self._link_only_rows = ()
        self._write_rows = ()
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
        self.scan_history = TableHistory(self.table, allowed=self._history_allowed,
                                         restored=self._restore_scan, language=self._history_language)
        self.creation_history = TableHistory(self.creation_table, editable_columns=(0, 1, 2),
                                             allowed=self._history_allowed,
                                             restored=self._restore_creation, language=self._history_language)
        self._content_undo = None

    def _history_allowed(self):
        return not self._busy

    def _history_language(self):
        return getattr(self.parent_window, "_ui_lang", "ru")

    def _restore_scan(self, _extra):
        for row in range(self.table.rowCount()):
            data = self.table.item(row, 0).data(Qt.UserRole)
            self.table.item(row, 6).setText(self._t(data["status"]))
            if data["status"] == "scanned":
                self._style_wikidata_link(self.table.item(row, 5))
                self.table.item(row, 5).setToolTip(self._t("articles_hint"))
        self._reindex()
        self._apply_table_theme()
        self._set_busy(False)

    def _restore_creation(self, _extra):
        self._invalidate_creation(clear_table=False)
        self._apply_table_theme()
        self._set_busy(False)
        self._show_creation_detail()

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
        stage_layout = QVBoxLayout(self)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setSpacing(6)
        scan_content = QWidget()
        layout = QVBoxLayout(scan_content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.source_lang = QLineEdit("en")
        self.target_label = QLabel()
        self.scan_ns_combo = QComboBox(self)
        self.scan_ns_combo.addItem(translate_key('ui.auto', self._last_ui_lang, 'Auto'), 'auto')
        self.scan_ns_combo.hide()
        scan_toolbar, scan_controls = self._toolbar(self.source_lang, self.target_label, None)
        self.scan_report_path = QLineEdit('categories.tsv')
        self.scan_file_row = FileInputRow(self, self._t('ready_categories_file'), self.scan_report_path,
                                         self.open_scan_file, editor_menu=True)
        self.scan_file_row.setToolTip(self._t('scan_inputs_tip'))
        self.scan_file_row.open_tooltip_key = 'ui.transfer.scan_inputs_tip'
        self.scan_file_row.refresh_localized_texts()
        self.scan_file_row.pattern = '*.txt *.tsv'
        self.open_scan_btn = self.scan_file_row.open_btn
        self.export_scan_btn = QPushButton()
        self.export_scan_btn.setText(self._t("export_plan"))
        set_category_button_tooltip(self.export_scan_btn, 'export_scan', self._t)
        self.export_scan_btn.clicked.connect(self.export_report)
        stage_layout.addWidget(scan_toolbar)
        self.scan_root_controls = QWidget()
        root_row = QHBoxLayout(self.scan_root_controls)
        root_row.setContentsMargins(0, 0, 0, 0)
        root_row.setSpacing(6)
        self.read_category_caption = self._setting_caption('read_category_caption')
        root_row.addWidget(self.read_category_caption)
        self.root_edit = QLineEdit()
        self.root_edit.setProperty('_wct_skip_text_translation', True)
        self.root_edit.setMinimumWidth(0)
        self.root_edit.setPlaceholderText(self._t('root_placeholder'))
        self.root_edit.setToolTip(self._t('source_root_tip'))
        root_row.addWidget(self.root_edit, 1)
        root_row.addWidget(self._setting_caption('depth'))
        self.depth_spin = QSpinBox()
        self.depth_spin.setRange(0, 99)
        self.depth_spin.setValue(0)
        depth_wrap, self.depth_plus_btn, self.depth_minus_btn = create_spin_control(
            self.depth_spin, plus_tip=self._t('increase'), minus_tip=self._t('decrease'))
        root_row.addWidget(depth_wrap)
        self.scan_read_help_btn = add_info_button(self, root_row, lambda: self._t('scan_read_help'), inline=True)
        set_category_button_tooltip(self.scan_read_help_btn, 'scan_read_help', self._t)
        self.include_root = QCheckBox(self._t("include_root"))
        self.include_subcategories = QCheckBox(self._t('scan_subcategories'))
        self.include_subcategories.setToolTip(self._t('scan_subcategories_tip'))
        self.depth_spin.setToolTip(self._t("scan_depth_tip"))
        self.skip_spin = QSpinBox()
        self.skip_spin.setRange(0, 99)
        self.skip_spin.setValue(0)
        self.skip_spin.setToolTip(self._t("skip_tip"))
        skip_wrap, _, _ = create_spin_control(self.skip_spin, plus_tip=self._t("increase"), minus_tip=self._t("decrease"))
        self.settings = CategoryInlineParameters([
            [self.include_root],
            [self.include_subcategories],
            [self._setting_caption('skip_level'), skip_wrap],
            [self._scan_settings_help_button()],
        ])
        scan_controls.addWidget(self.settings, 1)
        stage_layout.addWidget(self.scan_root_controls)
        stage_layout.addWidget(self.scan_file_row)

        results_page = QWidget()
        result_layout = QVBoxLayout(results_page)
        result_layout.setContentsMargins(0, 0, 0, 0)
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
        self.table.cellClicked.connect(self._scan_cell_clicked)
        self.table.setMouseTracking(True)
        self.table.cellEntered.connect(self._link_cursor)
        result_layout.addWidget(self.table, 1)
        self.scan_table_tools, actions = self._action_group("table_tools")
        self.add_scan_list_btn = self._button('enter_scan_list', self.enter_scan_list, actions)
        self.delete_btn = self._button("delete", self.delete_selected, actions)
        self.clear_btn = self._button("clear_empty", self.clear_empty, actions)
        self.clear_all_scan_btn = self._button("clear_all", self.clear_scan, actions)
        self.reset_scan_btn = self._button("reset_scan", self.reset_scan, actions)
        self.reset_scan_btn.setToolTip(self._t("reset_scan_tip"))
        self.scan_guidance = QLabel()
        self.scan_guidance.setObjectName('categoryListSummary')
        self.scan_guidance.setWordWrap(True)
        self.scan_header = QWidget()
        scan_header_row = QHBoxLayout(self.scan_header)
        scan_header_row.setContentsMargins(0, 0, 0, 0)
        scan_header_row.setSpacing(10)
        self.load_btn = self._button('read', lambda: self.load_categories(), scan_header_row)
        scan_header_row.addWidget(self.scan_guidance, 1)
        self.scan_table_toolbar = CategoryTableToolbar(self.scan_header)
        layout.addWidget(results_page, 1)
        self.scan_workspace = CategoryWorkspace(scan_content)
        self.scan_frame = CategoryWorkspaceFrame(self._t('scan_list'), self.scan_table_toolbar, self.scan_workspace)
        self.scan_frame.setProperty('transfer_text_key', 'scan_list')
        stage_layout.addWidget(self.scan_frame, 1)
        stage_layout.addWidget(self.scan_table_tools)
        scan_tools, run_actions = self._action_group("create_tools")
        self.scan_btn = self._button("scan", self.scan_categories, run_actions)
        self.retry_btn = self._button("retry_errors", self.retry_errors, run_actions)
        self.next_btn = self._button("next_links", self.open_links, None)
        stage_layout.addWidget(CategoryRunRow(scan_tools, self.next_btn, export_action=self.export_scan_btn))

        creation_page = self.creation_stage = CategoryLinksStage(self)
        creation_stage_layout = QVBoxLayout(creation_page)
        creation_stage_layout.setContentsMargins(0, 0, 0, 0)
        creation_stage_layout.setSpacing(6)
        creation_content = QWidget()
        creation_layout = QVBoxLayout(creation_content)
        creation_layout.setContentsMargins(0, 0, 0, 0)
        self.links_source_lang = QLineEdit("en")
        self.links_target_label = QLabel()
        creation_toolbar, _context_row = self._toolbar(self.links_source_lang, self.links_target_label, None)
        self._toolbars = (scan_toolbar, creation_toolbar)
        creation_stage_layout.addWidget(creation_toolbar)
        self.tsv_path = QLineEdit('categories.tsv')
        self.file_controls = FileInputRow(self, self._t('preparation_file'), self.tsv_path,
                                         self.import_tsv, editor_menu=True)
        self.file_controls.setToolTip(self._t('import_file_tip'))
        self.file_controls.open_tooltip_key = 'ui.transfer.import_file_tip'
        self.file_controls.refresh_localized_texts()
        self.browse_btn = self.file_controls.browse_btn
        self.preview_btn = self.file_controls.open_btn
        creation_stage_layout.addWidget(self.file_controls)
        self.summary_edit = QLineEdit(default_create_summary(self._context()[1]))
        self.summary_edit.setCursorPosition(0)
        self._summary_lang = self._context()[1]
        self.summary_edit.setMinimumWidth(0)
        self.creation_table_tools, edit_actions = self._action_group("table_tools")
        self.autofill_btn = QToolButton()
        self.autofill_btn.setText(self._t("autofill"))
        self.autofill_btn.setCheckable(True)
        self.autofill_btn.setArrowType(Qt.RightArrow)
        self.autofill_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        set_category_button_tooltip(self.autofill_btn, 'autofill', self._t)
        edit_actions.addWidget(self.autofill_btn)
        self.incomplete_btn = self._button("next_incomplete", self.select_next_incomplete, edit_actions)
        self.remove_creation_btn = self._button("delete", self.delete_creation_selected, edit_actions)
        self.clear_all_creation_btn = self._button("clear_all", self.clear_creation, edit_actions)
        self.export_creation_btn = self._button("export_plan", self.export_plan, None)
        self.autofill_box = QFrame()
        self.autofill_box.setObjectName("transferAutofillPanel")
        self.autofill_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        autofill_layout = QVBoxLayout(self.autofill_box)
        autofill_layout.setContentsMargins(0, 0, 0, 0)
        autofill_layout.setSizeConstraint(QLayout.SetMinimumSize)
        self.autofill_tabs = TransferAutofillTabs()
        self.autofill_tabs.tabBar().setDrawBase(False)
        autofill_layout.addWidget(self.autofill_tabs)
        titles_page, content_page = QWidget(), QWidget()
        titles_layout = QVBoxLayout(titles_page)
        content_layout = QVBoxLayout(content_page)
        self.autofill_tabs.addTab(titles_page, self._t("titles_column"))
        self.autofill_tabs.addTab(content_page, self._t("content_column"))
        self.title_template_edit = QLineEdit()
        self.title_template_edit.setPlaceholderText(self._t("number_target_example"))
        titles_layout.addWidget(self.title_template_edit)
        title_row = QHBoxLayout()
        self.number_titles_btn = self._button("preview_titles", self.fill_numbered_titles, title_row)
        self.autofill_title_help = QLabel(self._t("autofill_title_help"))
        self.autofill_title_help.setWordWrap(True)
        title_row.addWidget(self.autofill_title_help, 1)
        titles_layout.addLayout(title_row)
        content_actions = TransferActionsLayout()
        content_actions.setSpacing(4)
        self.fill_content_btn = self._button("fill_content", self.fill_content, content_actions)
        self.copy_first_btn = self._button("copy_first_content", self.copy_first_content, content_actions)
        self.load_text_btn = QToolButton()
        self.load_text_btn.setObjectName('categorySourceTextButton')
        self.load_text_btn.setStyleSheet('QToolButton#categorySourceTextButton { padding-right: 24px; } '
                                        'QToolButton#categorySourceTextButton::menu-button { width: 18px; border: none; background: transparent; }')
        self.load_text_btn.setText(self._t('load_source_content'))
        self.load_text_btn.setProperty('transfer_text_key', 'load_source_content')
        self.load_text_btn.setFont(self.fill_content_btn.font())
        self.load_text_btn.setPopupMode(QToolButton.MenuButtonPopup)
        self.load_text_btn.clicked.connect(self.load_source_content)
        set_category_button_tooltip(self.load_text_btn, 'load_source_content', self._t)
        content_actions.addWidget(self.load_text_btn)
        source_menu = QMenu(self.load_text_btn)
        source_menu.setToolTipsVisible(True)
        self.translate_source_names = source_menu.addAction(self._t('translate_source_names'))
        self.translate_source_names.setCheckable(True)
        self.translate_source_names.setChecked(True)
        self.translate_source_names.setToolTip(self._t('translate_source_names_tip'))
        self.load_text_btn.setMenu(source_menu)
        self.copy_first_btn.setToolTip(self._t("copy_first_tip"))
        self.load_text_btn.setToolTip(self._t("source_content_tip"))
        content_layout.addLayout(content_actions)
        self.autofill_content_help = QLabel(self._t('autofill_content_help'))
        self.autofill_content_help.setWordWrap(True)
        self.autofill_content_help.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        content_layout.addWidget(self.autofill_content_help)
        self.source_content_empty = QCheckBox(self._t("source_content_empty"))
        self.source_content_empty.setChecked(False)
        self.source_content_empty.setToolTip(self._t("autofill_empty_tip"))
        self.autofill_tabs.setCornerWidget(self.source_content_empty, Qt.TopRightCorner)
        self.autofill_scroll = CategoryParametersPanel(self.autofill_box, self.autofill_btn,
                                                      minimum_visible_height=90)
        self.autofill_scroll.setObjectName('categoryAutofillScroll')
        self.autofill_scroll.setStyleSheet('QScrollArea#categoryAutofillScroll { background: transparent; border: none; }')
        self.autofill_scroll.viewport().setObjectName('categoryAutofillViewport')
        self.autofill_scroll.viewport().setStyleSheet('QWidget#categoryAutofillViewport { background: transparent; }')
        self.creation_summary = QLabel()
        self.creation_summary.setObjectName('categoryListSummary')
        self.creation_summary.setWordWrap(True)
        self.creation_table_toolbar = CategoryTableToolbar(self.creation_summary)
        self.creation_table = self._table(["source_category", "new_category", "content", "source_wd", "current_wd", "status"], CategoryCreationTable)
        self.creation_table.setObjectName("transferCreationTable")
        self.creation_table.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)
        self.creation_table.horizontalHeader().setStretchLastSection(False)
        for col in range(3):
            self.creation_table.horizontalHeader().setSectionResizeMode(col, QHeaderView.Interactive)
        self.creation_table.setColumnWidth(3, 95)
        self.creation_table.setColumnWidth(4, 95)
        self.creation_table.setColumnWidth(5, 145)
        self.creation_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed | QAbstractItemView.SelectedClicked)
        self.creation_table.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.creation_table.setItemDelegateForColumn(2, CategoryTextDelegate(self.creation_table))
        self.creation_table.delete_rows_requested.connect(self.delete_creation_selected)
        self.creation_table.fill_requested.connect(self._fill_content_range)
        self.creation_table.text_edit_requested.connect(self._edit_creation_text)
        self.creation_table.horizontalHeaderItem(2).setToolTip(self._t('edit_category_text_tip'))
        self.creation_table.itemChanged.connect(self._creation_edited)
        self.creation_table.itemSelectionChanged.connect(self._show_creation_detail)
        self.creation_table.cellClicked.connect(self._open_creation_wd)
        self.creation_table.setMouseTracking(True)
        self.creation_table.cellEntered.connect(self._creation_link_cursor)
        creation_group = QWidget()
        creation_group.setToolTip(self._t('creation_hint'))
        table_layout = QVBoxLayout(creation_group)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.addWidget(self.creation_table)
        creation_layout.addWidget(creation_group, 1)
        self.creation_workspace = CategoryWorkspace(creation_content)
        self.creation_frame = CategoryWorkspaceFrame(self._t('preparation_list'), self.creation_table_toolbar,
                                                    self.creation_workspace)
        self.creation_frame.setProperty('transfer_text_key', 'preparation_list')
        creation_stage_layout.addWidget(self.creation_frame, 1)
        self.creation_detail = QLabel(self._t("row_detail_hint"))
        self.creation_detail.setWordWrap(True)
        self.creation_detail.setTextFormat(Qt.PlainText)
        self.creation_detail.setMaximumHeight(44)
        self.creation_detail.hide()
        creation_stage_layout.addWidget(self.creation_detail)
        creation_stage_layout.addWidget(self.creation_table_tools)
        creation_stage_layout.addWidget(self.autofill_scroll)
        create_tools = QWidget()
        create_actions = QHBoxLayout(create_tools)
        create_actions.setContentsMargins(0, 0, 0, 0)
        create_actions.setSpacing(4)
        self.check_links_btn = self._button("check_links", self.preview_creation, create_actions)
        self.create_btn = self._button("create", self.start_creation, create_actions)
        self.link_only_btn = self._button("link_only", self.start_linking_only, create_actions)
        create_actions.addWidget(self._setting_caption('summary'))
        create_actions.addWidget(self.summary_edit, 1)
        self.sync_btn = self._button("next_sync", self.open_sync, None)
        creation_stage_layout.addWidget(CategoryRunRow(create_tools, self.sync_btn, export_action=self.export_creation_btn))

        self.footer = CategoryProgressFooter(self.stop, self._history_language, self)
        self.progress_label = self.footer.label
        self.bar = self.footer.bar
        self.stop_btn = self.footer.stop_btn
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setProperty("_wct_skip_log_translation", True)
        self.log_panel = CategoryLogPanel(self.log, self._t)
        self.log_toggle = self.log_panel.toggle
        self.log_wrap = self.log_panel.wrap
        self.scan_workspace.set_log_panel(self.log_panel)
        self.scan_table_toolbar.set_log_panel(self.log_panel)
        stage_layout.addWidget(self.footer)
        self._delete_shortcut = QShortcut(QKeySequence(Qt.Key_Delete), self.table)
        self._delete_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self._delete_shortcut.activated.connect(self.delete_selected)
        self._copy_shortcut = QShortcut(QKeySequence.Copy, self.table)
        self._copy_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self._copy_shortcut.activated.connect(self.copy_selected)
        self.table.horizontalHeader().sortIndicatorChanged.connect(self._reindex)
        self.source_lang.textChanged.connect(self._source_changed)
        self.include_subcategories.toggled.connect(self._scan_subcategories_changed)
        self.links_source_lang.textChanged.connect(self._links_source_changed)
        self.root_edit.textChanged.connect(lambda: self._set_busy(self._busy))
        self.table.itemSelectionChanged.connect(lambda: self.delete_btn.setEnabled(not self._busy and bool(self.table.selectedIndexes())))
        self.creation_table.itemSelectionChanged.connect(lambda: self.remove_creation_btn.setEnabled(not self._busy and bool(self.creation_table.selectedIndexes())))
        self._refresh_target()
        self._set_busy(False)
        self._apply_table_theme()

    def _refresh_scan_headers(self):
        included = self.include_subcategories.isChecked()
        caption = self._t('total_subpages' if included else 'total')
        self.table.horizontalHeaderItem(4).setText(caption)
        self.table.setColumnWidth(4, max(105, self.table.horizontalHeader().fontMetrics().horizontalAdvance(caption) + 28))
        for col in (4, 5):
            self.table.horizontalHeaderItem(col).setToolTip(self._t('subpages_count_help') if included else
                                                          self._t('total' if col == 4 else 'potential'))

    def _scan_subcategories_changed(self, *_args):
        if self._busy:
            return
        if any(row.get('status') != 'loaded' for row in self._rows()):
            self.reset_scan()
        self.scan_history.reset()
        self._refresh_scan_headers()

    def _toggle_log(self, checked):
        self.log_toggle.setChecked(checked)

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

    def _toolbar(self, source_edit, target_label, options_button):
        return category_toolbar(source_edit, target_label, options_button, self._t)

    def _setting_caption(self, key):
        caption = QLabel(self._t(key))
        caption.setProperty('transfer_text_key', key)
        return caption

    def _action_group(self, title_key):
        frame, row = category_actions(self._t(title_key), framed=title_key == 'table_tools')
        frame.setProperty("transfer_text_key", title_key)
        return frame, row

    def _button(self, key, callback, layout):
        button = QPushButton(self._t(key))
        button.setProperty("transfer_text_key", key)
        set_category_button_tooltip(button, key, self._t)
        button.clicked.connect(callback)
        if layout is not None:
            layout.addWidget(button)
        return button

    def _refresh_target(self):
        self.target_label.setText(self._context()[1])
        self.links_target_label.setText(self.target_label.text())

    def _set_busy(self, busy):
        was_busy = self._busy
        self._busy = busy
        if busy and not was_busy:
            self.log_panel.set_expanded(True)
        for widget in (self.settings, self.scan_root_controls, self.source_lang, self.root_edit, self.load_btn,
                       self.scan_file_row, self.export_scan_btn, self.delete_btn, self.clear_btn, self.clear_all_scan_btn, self.clear_all_creation_btn, self.reset_scan_btn,
                       self.file_controls,
                       self.preview_btn, self.summary_edit, self.links_source_lang,
                       self.remove_creation_btn, self.export_creation_btn, self.check_links_btn,
                       self.autofill_box, self.autofill_btn):
            widget.setEnabled(not busy)
        self.table.setSortingEnabled(not busy)
        self.creation_table.setSortingEnabled(not busy)
        pending = [row for row in self._rows() if row.get('status') != 'scanned']
        self.scan_btn.setEnabled(not busy and bool(pending))
        self.scan_btn.setText(self._t('continue_scan' if pending and len(pending) < self.table.rowCount() else 'scan'))
        self.reset_scan_btn.setEnabled(not busy and any(row.get('status') != 'loaded'
                                       or row.get('total') is not None or row.get('potential') is not None
                                       for row in self._rows()))
        self.next_btn.setEnabled(not busy and self.table.rowCount() > 0)
        self.retry_btn.setEnabled(not busy and any(row.get("status") == "error" for row in self._rows()))
        self.incomplete_btn.setEnabled(not busy and self.creation_table.rowCount() > 0)
        self.number_titles_btn.setEnabled(not busy and self.creation_table.rowCount() > 0)
        self.fill_content_btn.setEnabled(not busy and self.creation_table.rowCount() > 0)
        self.creation_table.setEditTriggers(QAbstractItemView.NoEditTriggers if busy else
                                           QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed | QAbstractItemView.SelectedClicked)
        self.create_btn.setEnabled(not busy and self.creation_table.rowCount() > 0 and bool(self._creation_rows)
                                   and self._preview_context == self._creation_context())
        self.create_btn.setText(self._t("create"))
        self.link_only_btn.setEnabled(not busy and bool(self._link_only_rows)
                                      and self._preview_context == self._creation_context())
        self.sync_btn.setEnabled(not busy and bool(self._linked_categories)
                                 and self._linked_context == self._creation_context())
        self.stop_btn.setEnabled(busy)
        has_scan_rows = self.table.rowCount() > 0
        has_creation_rows = self.creation_table.rowCount() > 0
        self.load_btn.setEnabled(not busy and bool(self.root_edit.text().strip()))
        self.delete_btn.setEnabled(not busy and bool(self.table.selectedIndexes()))
        self.clear_btn.setEnabled(not busy and any(row.get('status') == 'scanned' and row.get('potential') == 0 for row in self._rows()))
        self.clear_all_scan_btn.setEnabled(not busy and has_scan_rows)
        self.clear_all_creation_btn.setEnabled(not busy and has_creation_rows)
        self.export_scan_btn.setEnabled(not busy and has_scan_rows)
        self.add_scan_list_btn.setEnabled(not busy)
        self.remove_creation_btn.setEnabled(not busy and bool(self.creation_table.selectedIndexes()))
        self.autofill_btn.setEnabled(not busy and has_creation_rows)
        self.export_creation_btn.setEnabled(not busy and has_creation_rows)
        self.check_links_btn.setEnabled(not busy and has_creation_rows)
        scanned = has_scan_rows and all(row.get('status') == 'scanned' for row in self._rows())
        hint = 'scan_start_hint' if not has_scan_rows else 'scan_review_hint' if scanned else 'scan_resume_hint' if len(pending) < self.table.rowCount() else 'scan_run_hint'
        if busy and self.read_worker is not None:
            hint = 'loading' if self._read_mode == 'load' else 'scanning'
        self.scan_guidance.setText(self._t(hint, checked=self.table.rowCount() - len(pending),
                                            total=self.table.rowCount(), remaining=len(pending)))
        theme = getattr(self.parent_window, '_theme_mode', 'teal')
        needs_fill = any((self.creation_table.item(row, 5).data(Qt.UserRole) or '') in {'needs_translation', 'needs_content'}
                         for row in range(self.creation_table.rowCount()))
        for button, active in ((self.load_btn, not has_scan_rows), (self.open_scan_btn, not has_scan_rows),
                               (self.scan_btn, has_scan_rows and not scanned), (self.next_btn, scanned),
                               (self.preview_btn, not has_creation_rows),
                               (self.incomplete_btn, needs_fill), (self.autofill_btn, needs_fill),
                               (self.check_links_btn, has_creation_rows and not needs_fill and not self.create_btn.isEnabled() and not self.sync_btn.isEnabled()),
                               (self.create_btn, self.create_btn.isEnabled()), (self.sync_btn, self.sync_btn.isEnabled())):
            highlight_next_action(button, not busy and active, theme)
        self._reindex()
        for name in ("scan_history", "creation_history"):
            history = getattr(self, name, None)
            if history:
                history.refresh()

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
        self.bar.setFormat(self.progress_label.text())
        self.bar.setToolTip(self.progress_label.toolTip())

    def _retry_notice(self, text):
        self._set_progress(text)

    def _reindex(self, *_args):
        self._rows_by_title = {
            self.table.item(row, 0).text(): row for row in range(self.table.rowCount())
            if self.table.item(row, 0) is not None
        }

    def _rows(self):
        return [self.table.item(row, 0).data(Qt.UserRole) for row in range(self.table.rowCount())]

    def load_categories(self, *, append=False):
        if self._busy:
            return
        context = self._validate_context()
        if not context:
            return
        if not self.root_edit.text().strip():
            self._warn("specify_root")
            return
        self._append_read = bool(append)
        if not append:
            self.table.setRowCount(0)
            self._reindex()
        self._start_read(CategoryTransferReadWorker(
            source_lang=context[0], target_lang=context[1], family=context[2],
            category=self.root_edit.text(),
            depth=self.depth_spin.value(), skip_existing_level=self.skip_spin.value(),
            include_root=self.include_root.isChecked(),
            namespace_selection=self.scan_ns_combo.currentData() or 'auto',
        ), "load", 0)

    def _scan_settings_help_button(self):
        group = QWidget()
        row = QHBoxLayout(group)
        row.setContentsMargins(0, 0, 0, 0)
        self.scan_settings_help_btn = add_info_button(self, row, lambda: self._t('scan_settings_help'), inline=True)
        set_category_button_tooltip(self.scan_settings_help_btn, 'scan_settings_help', self._t)
        return group

    def scan_categories(self):
        self._scan_rows([row for row in self._rows() if row.get('status') != 'scanned'])

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
            categories=rows,
            include_subcategories=self.include_subcategories.isChecked(),
        ), "scan", len(rows))

    def _start_read(self, worker, mode, total):
        self._scan_load_transaction = None
        if mode == 'load' and getattr(self, '_append_read', False):
            self._scan_load_transaction = self.scan_history.transaction()
            self._scan_load_transaction.__enter__()
        else:
            self.scan_history.reset()
        self.read_worker = worker
        self._read_mode = mode
        self._set_busy(True)
        init_progress(self.progress_label, self.bar, total)
        self._set_progress(self._t("loading" if mode == "load" else "scanning"))
        self._read_count = self._read_total = 0
        if mode == 'load':
            self.bar.setRange(0, 0)
            self._write_log(translate_key('ui.source.fetch_started_background', self._history_language()).format(
                category=self.root_edit.text().strip(), depth=self.depth_spin.value(),
                mode=translate_key('ui.source.fetch_mode.categories_only', self._history_language())))
        else:
            self._write_log(self._t('scanning'))
        debug(f"[Category transfer] Start {mode}: {self._context()}, rows={total}, depth={self.depth_spin.value()}")
        worker.row_ready.connect(self._apply_row)
        worker.read_progress.connect(self._read_progress)
        worker.item_processed.connect(self._advance)
        worker.progress.connect(self._write_log)
        worker.retry_wait.connect(self._retry_notice)
        worker.finished.connect(self._read_finished)
        worker.start()

    def _apply_row(self, data):
        if (getattr(self, '_append_read', False) and getattr(self, '_read_mode', '') == 'load'
                and data['source_title'] in self._rows_by_title):
            return
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        original = data.get('input_source_title', data['source_title'])
        row = self._rows_by_title.get(original)
        canonical_row = self._rows_by_title.get(data['source_title'])
        if row is not None and canonical_row is not None and row != canonical_row:
            self.table.removeRow(row)
            self._reindex()
            row = self._rows_by_title.get(data['source_title'])
        if row is None:
            row = self._rows_by_title.get(data['source_title'])
        if original != data['source_title']:
            self._rows_by_title.pop(original, None)
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._rows_by_title[data["source_title"]] = row
        values = [data["source_title"], data.get("target_title", ""), data.get("qid", ""),
                  data["depth"], scan_count_value(data, 'total'), scan_count_value(data, 'potential'), self._t(data["status"])]
        for col, value in enumerate(values):
            item = CategoryCountItem() if col in (4, 5) else QTableWidgetItem()
            if isinstance(value, int):
                item.setData(Qt.DisplayRole, value)
            else:
                item.setText("—" if value is None else str(value))
            if col == 0:
                item.setData(Qt.UserRole, dict(data))
            elif col in (4, 5):
                key = 'total' if col == 4 else 'potential'
                count, categories = data.get(key), data.get(key + '_categories') or 0
                item.setData(Qt.UserRole, (-1, -1, -1) if count is None else (count + categories, count, categories))
            elif col == 2 and re.fullmatch(r"Q[1-9][0-9]*", str(value)):
                self._style_wikidata_link(item)
            item.setToolTip(str(value or ""))
            if col == 5 and data.get("status") == "scanned":
                self._style_wikidata_link(item)
                item.setToolTip(self._t('subpages_hint' if data.get('potential_categories') is not None else 'articles_hint'))
            elif data.get("error"):
                item.setToolTip(data["error"])
            self.table.setItem(row, col, item)
        self.table.setSortingEnabled(sorting)
        self._reindex()

    def _read_progress(self, payload):
        if self._read_mode != 'load':
            keys = {'scan_categories': 'scan_batch_categories', 'scan_members': 'scan_batch_members',
                    'scan_links': 'scan_batch_links', 'scan_articles': 'scan_article_packet',
                    'scan_subpages': 'scan_subpage_packet'}
            key = keys.get(payload.get('phase'))
            if key:
                self._set_progress(self._t(key, **payload))
            elif payload.get('title'):
                self._set_progress(self._t('scan_row_progress', title=payload['title']))
            return
        self._read_count = max(self._read_count, int(payload.get('count') or 0))
        self._read_total = max(self._read_total, int(payload.get('total') or 0))
        lang = self._history_language()
        set_reading_progress(self.progress_label, self.bar, self._read_count, self._read_total,
                             lambda key, fallback: translate_key(key, lang, fallback),
                             waiting=not self._read_count and not self._read_total)
        if payload.get('phase') == 'links':
            self._set_progress(self._t('checking_links_progress', **payload))
        self.bar.setToolTip(payload.get('category', payload.get('title', '')))

    def _advance(self):
        inc_progress(self.progress_label, self.bar)

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
        scanned = sum(row.get('status') == 'scanned' for row in worker.results)
        recorder = getattr(self.parent_window, 'record_operation', None)
        if recorder and scanned:
            recorder('transfer_scan', scanned)
        if worker.failed or worker.error_count:
            self.log_toggle.setChecked(True)
        self.bar.setRange(0, max(1, self.table.rowCount()))
        if not worker.failed and not worker._stop:
            self.bar.setValue(self.bar.maximum())
        self._set_busy(False)
        if self._scan_load_transaction is not None:
            self._scan_load_transaction.__exit__(None, None, None)
            self._scan_load_transaction = None
        else:
            self.scan_history.reset()
        self._append_read = False
        self._reset_changed_context()
        worker.deleteLater()

    @table_action("scan_history")
    def delete_selected(self):
        if self._busy:
            return
        for row in sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(row)
        self._reindex()
        self.scan_btn.setEnabled(self.table.rowCount() > 0)
        self._set_busy(False)

    @table_action("scan_history")
    def clear_empty(self):
        if self._busy:
            return
        for row in range(self.table.rowCount() - 1, -1, -1):
            data = self.table.item(row, 0).data(Qt.UserRole)
            # Errors and unscanned rows remain visible for retry.
            if data.get("status") == "scanned" and data.get('potential') is not None and scan_available_count(data) == 0:
                self.table.removeRow(row)
        self._reindex()
        self.scan_btn.setEnabled(self.table.rowCount() > 0)
        self._set_busy(False)

    @table_action("scan_history")
    def clear_scan(self):
        if self._busy:
            return
        self.table.setRowCount(0)
        self._reindex()
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self._set_progress(self._t('ready'))
        self._set_busy(False)

    @table_action('scan_history')
    def reset_scan(self):
        if self._busy:
            return
        rows = self._rows()
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        try:
            for row in rows:
                data = {key: value for key, value in row.items()
                        if key not in {'error', 'target_articles', 'total', 'potential',
                                       'total_categories', 'potential_categories', 'target_categories'}}
                self._apply_row({**data, 'status': 'loaded', 'total': None, 'potential': None})
        finally:
            self.table.setSortingEnabled(sorting)
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self._set_progress(self._t('scan_reset_done'))
        self._set_busy(False)

    @table_action("creation_history")
    def clear_creation(self):
        if self._busy:
            return
        self.creation_table.setRowCount(0)
        self._invalidate_creation(clear_table=False)
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self._set_progress(self._t('ready'))
        self._set_busy(False)
        self._show_creation_detail()

    def copy_selected(self):
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        text = "\n".join("\t".join(self.table.item(row, col).text()
                                   for col in range(self.table.columnCount())) for row in rows)
        QGuiApplication.clipboard().setText(text)

    def export_report(self):
        if self._busy:
            return
        path, _ = QFileDialog.getSaveFileName(self, self._t("export_plan"), "categories.tsv", "TSV (*.tsv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle, delimiter="\t")
                writer.writerow([self.table.horizontalHeaderItem(col).text() for col in range(7)])
                for row in self._rows():
                    writer.writerow([row["source_title"], row.get("target_title", ""), row.get("qid", ""),
                                     row["depth"], scan_count_value(row, 'total'), scan_count_value(row, 'potential'), row["status"]])
        except Exception as error:
            self._warn("failed", error=error)

    def import_report(self, path=None):
        if self._busy:
            return
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, self._t("open_report"), "", "TSV (*.tsv)")
        if not path:
            return
        self.scan_report_path.setText(path)
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
                depth = int(fields[3]) if fields[3] else None
                total, total_categories = parse_scan_count(fields[4])
                potential, potential_categories = parse_scan_count(fields[5])
                if (total_categories is None) != (potential_categories is None) and total is not None and potential is not None:
                    raise ValueError(self._t('report_invalid', line=line))
                if depth is None or any(value is not None and value < 0 for value in (depth, total, potential)):
                    raise ValueError(self._t("report_invalid", line=line))
                if fields[2] and not re.fullmatch(r"Q[1-9][0-9]*", fields[2]):
                    raise ValueError(self._t("report_invalid", line=line))
                rows.append(dict(source_title=fields[0].strip(), target_title=fields[1].strip(), qid=fields[2],
                                 depth=depth, total=total, potential=potential, status=fields[6],
                                 manual_input=fields[6] == 'loaded'))
                if total_categories is not None or potential_categories is not None:
                    rows[-1].update(total_categories=total_categories, potential_categories=potential_categories)
            if not rows:
                raise ValueError(self._t("tsv_empty"))
        except Exception as error:
            self._warn("failed", error=error)
            return
        self.table.setSortingEnabled(False)
        blocked = self.include_subcategories.blockSignals(True)
        self.include_subcategories.setChecked(any(row.get('total_categories') is not None for row in rows))
        self.include_subcategories.blockSignals(blocked)
        self._refresh_scan_headers()
        self.table.setRowCount(0)
        self._rows_by_title.clear()
        for row in rows:
            self._apply_row(row)
        self._set_busy(False)
        self.scan_history.reset()
        self._write_log(self._t("report_loaded", count=len(rows), path=path))

    def prepare_tsv(self):
        if self._busy:
            return
        candidates = [row for row in self._rows() if row.get("status") == "scanned"
                      and scan_available_count(row) > 0]
        if not candidates:
            self._warn("no_candidates")
            return
        path, _ = QFileDialog.getSaveFileName(self, self._t("prepare_tsv"), "categories.tsv", "TSV (*.tsv)")
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
        self.file_controls.choose_file()

    def open_scan_file(self):
        if self._busy:
            return
        try:
            with open(self.scan_report_path.text().strip(), encoding='utf-8-sig') as handle:
                text = handle.read()
            fields = next((row for row in csv.reader(text.splitlines(), delimiter='\t')
                           if row and any(cell.strip() for cell in row)), [])
            if len(fields) == 7:
                self.import_report(self.scan_report_path.text().strip())
                return
            titles = parse_scan_list(text)
        except Exception as error:
            self._warn('failed', error=error)
            return
        self.scan_history.reset()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self._rows_by_title.clear()
        self._append_scan_titles(titles)
        self.scan_history.reset()

    def _append_scan_titles(self, titles):
        for title in titles:
            if title not in self._rows_by_title:
                self._apply_row(dict(source_title=title, depth=0, status='loaded', manual_input=True))
        self._set_busy(False)

    @table_action('scan_history')
    def enter_scan_list(self):
        if self._busy:
            return
        text, accepted = QInputDialog.getMultiLineText(self, self._t('category_list_file'), self._t('scan_list_prompt'))
        if not accepted:
            return
        try:
            titles = parse_scan_list(text)
        except Exception as error:
            self._warn('failed', error=error)
            return
        self._append_scan_titles(titles)

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

    @pause_sorting('creation_table')
    def _invalidate_creation(self, *_args, clear_table=True):
        self._creation_rows = ()
        self._link_only_rows = ()
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
        self.link_only_btn.setEnabled(False)
        self._refresh_creation_summary()
        history = getattr(self, "creation_history", None)
        if history:
            history.reset()

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

    @pause_sorting('creation_table')
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
        if hasattr(self, "creation_history"):
            self.creation_history.reset()
        self._set_busy(self._busy)

    def _refresh_creation_summary(self):
        counts = {"ready": 0, "drafts": 0, "errors": 0, "unchecked": 0}
        outcomes = []
        for row in range(self.creation_table.rowCount()):
            item = self.creation_table.item(row, 5)
            code = item.data(Qt.UserRole) if item else "editing"
            outcomes.append(dict(status=code, label_changes=item.data(Qt.UserRole + 2) if item else None,
                                 link_action=item.data(Qt.UserRole + 3) if item else None))
            if code in {"ready_create", "ready_link", "ready_labels"}:
                counts["ready"] += 1
            elif code in {"needs_translation", "needs_content"}:
                counts["drafts"] += 1
            elif code in {"already_linked", "created", "linked", "labels_updated"}:
                pass
            elif code in {"error", "unlinked"}:
                counts["errors"] += 1
            else:
                counts["unchecked"] += 1
        counts.update(category_outcome_counts(outcomes))
        parts = [self._t('list_count', count=self.creation_table.rowCount())]
        parts.extend(self._t('count_' + key, count=value) for key, value in counts.items() if value)
        self.creation_summary.setText('   ·   '.join(parts))
        if not self.creation_table.rowCount():
            self.creation_summary.setText(self._t('prepare_start_hint'))
        self.creation_summary.setToolTip(self._t('list_summary', total=self.creation_table.rowCount(), **counts))

    def _show_creation_detail(self):
        row = self.creation_table.currentRow()
        item = self.creation_table.item(row, 5) if row >= 0 else None
        detail = (item.toolTip() or item.text()) if item else ''
        self.creation_detail.setText(detail)
        self.creation_detail.setVisible(bool(detail))

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

    @table_action("creation_history")
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
        self._fill_content_rows(range(max(0, start), min(end + 1, self.creation_table.rowCount())), value,
                                self.source_content_empty.isChecked())

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
            self._fill_content_rows(indices, editor.toPlainText(), self.source_content_empty.isChecked())

    def copy_first_content(self):
        if self._busy or not self.creation_table.rowCount():
            return
        value = self.creation_table.item(0, 2).text()
        if not value.strip():
            self._warn("first_content_required")
            return
        self._fill_content_range(1, self.creation_table.rowCount() - 1, value, False)

    @table_action('creation_history')
    def _edit_creation_text(self, row):
        if self._busy:
            return
        item = self.creation_table.item(row, 2)
        if item is None:
            return
        category = self.creation_table.item(row, 1).text() or self.creation_table.item(row, 0).text()
        dialog = CategoryTextDialog(item.text(), category, translate=self._t, parent=self.creation_stage)
        if dialog.exec() == QDialog.Accepted:
            item.setText(dialog.editor.toPlainText())
            item.setToolTip(item.text())
        dialog.deleteLater()

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
        self._content_undo = self.creation_history.transaction()
        self._content_undo.__enter__()
        self._set_busy(True)
        self._invalidate_creation(clear_table=False)
        worker = CategoryTextReadWorker(rows=rows, source_lang=context[0], target_lang=context[1],
                                        family=context[2], translate_names=self.translate_source_names.isChecked())
        self.content_worker = worker
        worker.content_ready.connect(self._source_content_ready)
        worker.item_processed.connect(self._advance)
        worker.progress.connect(self._write_log)
        worker.retry_wait.connect(self._retry_notice)
        worker.finished.connect(self._source_content_finished)
        self._set_busy(True)
        init_progress(self.progress_label, self.bar, len(rows))
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
        if self._content_undo:
            self._content_undo.__exit__(None, None, None)
            self._content_undo = None
        self._reset_changed_context()
        worker.deleteLater()

    def fill_numbered_titles(self):
        if self._busy or not self.creation_table.rowCount():
            return
        result = title_fill_dialog(self.creation_stage, self._table_creation_rows(),
                                   {index.row() for index in self.creation_table.selectedIndexes()},
                                   self._t, self.creation_table.styleSheet(), self.title_template_edit.text(),
                                   only_empty=self.source_content_empty.isChecked())
        if result is not None:
            plan, template = result
            self.title_template_edit.setText(template)
            self._apply_numbered_titles(plan)

    @table_action("creation_history")
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
                if (item and item.text() != proposal.target_title
                        and (not self.source_content_empty.isChecked() or not item.text().strip())):
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

    @pause_sorting('creation_table')
    def _creation_edited(self, item):
        if self._busy or item.column() >= 3:
            return
        self._table_dirty = True
        # Preserve editable input, but invalidate all write and handoff snapshots.
        self._creation_rows = ()
        self._link_only_rows = ()
        self._preview_context = self._linked_context = None
        self._linked_categories = []
        self._linked_pairs = {}
        self.create_btn.setEnabled(False)
        self.link_only_btn.setEnabled(False)
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

    @table_action("creation_history")
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
            self._link_only_rows = ()
            self._preview_context = self._linked_context = None
            self._linked_categories = []
            self._linked_pairs = {}
            self._set_busy(False)
            self._refresh_creation_summary()

    def export_plan(self):
        if self._busy or not self.creation_table.rowCount():
            return
        path, _ = QFileDialog.getSaveFileName(self, self._t("export_plan"), "categories.tsv", "TSV (*.tsv)")
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
        self._set_busy(True)
        self._fill_creation_table(rows, "checking")
        source, target, family = self._creation_context()
        worker = CategoryTransferPreviewWorker(rows=rows, source_lang=source, target_lang=target,
                                              family=family)
        self.preview_worker = worker
        self._set_busy(True)
        init_progress(self.progress_label, self.bar, len(rows))
        self._set_progress(self._t("checking"))
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self._write_log(self._t('check_started', count=len(rows)))
        debug(f"[Category transfer] Start preview: {self._creation_context()}, rows={len(rows)}")
        worker.row_ready.connect(self._preview_result)
        worker.progress.connect(self._write_log)
        worker.activity.connect(self._preview_activity)
        worker.retry_wait.connect(self._retry_notice)
        worker.finished.connect(self._preview_finished)
        worker.start()

    def _preview_activity(self, payload):
        self.bar.setRange(0, 100)
        self.bar.setValue(payload['percent'])
        self._set_progress(payload['text'])

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

    def _preview_finished(self):
        worker = self.preview_worker
        self.preview_worker = None
        if worker.failed or worker._stop:
            self._creation_rows = ()
            self._link_only_rows = ()
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
            self._link_only_rows = tuple(result['row'] for result in worker.results
                                         if result['status'] in {'ready_link', 'ready_labels'})
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
        if not worker.failed and not worker._stop:
            self.bar.setValue(self.bar.maximum())
        self._set_progress(self._t("preview_ready") if not worker.failed and not worker._stop else status)
        self._write_log(status)
        if worker.failed or any(result["status"] == "error" for result in worker.results):
            self.log_toggle.setChecked(True)
        self._set_busy(False)
        self._reset_changed_context()
        self.creation_history.reset()
        worker.deleteLater()

    def start_linking_only(self):
        self.start_creation(link_only=True)

    def start_creation(self, *, link_only=False):
        rows = self._link_only_rows if link_only else self._creation_rows
        if self._busy or not self.creation_table.rowCount() or not rows or self._preview_context != self._creation_context():
            return
        user = getattr(self.parent_window, "current_user", None)
        password = getattr(self.parent_window, "current_password", None)
        if not user or not password:
            self._warn("login_required")
            return
        if not self._confirm_creation(link_only=link_only):
            return
        self.creation_history.reset()
        source, target, family = self._creation_context()
        apply_pwb_config(target, family)
        self._linked_categories = list(self._existing_linked_categories)
        self._linked_context = self._creation_context()
        self._creation_row_results = {}
        self._write_rows = rows
        worker = CategoryTransferCreateWorker(
            rows=rows, username=user, password=password,
            source_lang=source, target_lang=target, family=family, summary=self.summary_edit.text().strip(),
            create_missing=not link_only,
        )
        self.create_worker = worker
        self._set_busy(True)
        init_progress(self.progress_label, self.bar, len(rows))
        self._set_progress(self._t('linking' if link_only else 'creating'))
        self._write_log(self._t('linking' if link_only else 'creating'))
        debug(f"[Category transfer] Start creation: {self._creation_context()}, rows={len(self._creation_rows)}")
        worker.category_result.connect(self._creation_result)
        worker.interactive_input_requested.connect(self._interactive_input)
        worker.progress.connect(self._write_log)
        worker.item_processed.connect(self._advance)
        worker.finished.connect(self._creation_finished)
        worker.start()

    def _creation_result(self, result):
        self._creation_row_results[result["source_title"]] = result
        for row in range(self.creation_table.rowCount()):
            if self.creation_table.item(row, 0).text() == result["source_title"]:
                status = QTableWidgetItem(self._t(result["status"]))
                status.setFlags(status.flags() & ~Qt.ItemIsEditable)
                status.setData(Qt.UserRole, result["status"])
                status.setData(Qt.UserRole + 2, result.get('label_changes', {}))
                status.setData(Qt.UserRole + 3, result.get('link_action'))
                status.setToolTip(result.get("error") or ' '.join(filter(None, (result.get('qid'), result.get('detail')))))
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
        for row in self._write_rows:
            result = self._creation_row_results.get(row.source_title, {})
            if result.get("status") in {"created", "linked", "labels_updated", "already_linked"}:
                self._linked_pairs[row.target_title] = CategoryPair(
                    row.source_title, row.target_title, result.get("qid", ""))
        self._existing_linked_categories = list(self._linked_categories)
        self._creation_rows = tuple(row for row in self._creation_rows
                                    if self._creation_row_results.get(row.source_title, {}).get("status")
                                    not in {"created", "linked", "labels_updated", "already_linked"})
        self._link_only_rows = tuple(row for row in self._link_only_rows
                                     if self._creation_row_results.get(row.source_title, {}).get('status')
                                     not in {'created', 'linked', 'labels_updated', 'already_linked'})
        status = (self._t("failed", error=worker.failure_message) if worker.failed
                  else self._t("stopped") if worker._stop else self._t("creation_done", **{"already": 0, "labels": 0, **worker.stats}))
        self._set_progress(status)
        self._write_log(status)
        recorder = getattr(self.parent_window, "record_operation", None)
        if recorder and worker.saved_edits:
            recorder("transfer_prepare", worker.saved_edits)
        self._set_busy(False)
        self._reset_changed_context()
        self.creation_history.reset()
        worker.deleteLater()
        if self._linked_categories and self._linked_context == self._creation_context():
            if self._offer_sync(len(self._linked_categories)):
                self.open_sync()

    def _confirm_creation(self, *, link_only=False):
        dialog = QDialog(self.creation_stage)
        dialog.setObjectName("confirmCategoryCreationDialog")
        dialog.setWindowTitle(self._t("links_title"))
        dialog.setMinimumWidth(540)
        layout = QVBoxLayout(dialog)
        layout.setSpacing(14)
        rows = self._link_only_rows if link_only else self._creation_rows
        heading = QLabel(self._t('plan_link_heading' if link_only else 'confirm_heading', count=len(rows),
                                source=self._creation_context()[0], target=self._creation_context()[1]))
        heading.setWordWrap(True)
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading)
        results = [dict(source_title=self.creation_table.item(row, 0).text(),
                        source_qid=self.creation_table.item(row, 3).text(),
                        target_qid=self.creation_table.item(row, 4).text(),
                        status=self.creation_table.item(row, 5).data(Qt.UserRole))
                   for row in range(self.creation_table.rowCount())]
        plan = category_action_plan(results, (row.source_title for row in rows))
        layout.addWidget(category_plan_widget(plan, self._t, dialog, results=results))
        for key in ("plan_preserve_text",) + (("confirm_items",) if plan["items"] else ()):
            label = QLabel(self._t(key))
            label.setWordWrap(True)
            label.setTextFormat(Qt.PlainText)
            layout.addWidget(label)
        buttons = QDialogButtonBox()
        buttons.addButton(self._t('link_only' if link_only else 'create'), QDialogButtonBox.AcceptRole)
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
            self.scan_history.reset()

    def _scan_cell_clicked(self, row, col):
        if col == 2:
            self._open_category(row, col)
        elif col == 5:
            self._open_article_matches(row)

    def _open_article_matches(self, row):
        data = dict(self.table.item(row, 0).data(Qt.UserRole))
        if data.get("status") != "scanned":
            return
        source, target, family = self._context()
        dialog = CategoryArticlesDialog(data, language=target, family=family, translate=self._t, parent=self)
        if 'target_articles' not in data or (data.get("target_title") and dialog.articles):
            worker = CategoryArticlePresenceWorker(articles=dialog.articles if 'target_articles' in data else None,
                                                  category=data.get("target_title", ""), source_category=data["source_title"],
                                                  source_lang=source, target_lang=target, family=family,
                                                  include_subcategories=data.get('total_categories') is not None)
            self.article_worker = worker
            self._article_workers.add(worker)
            worker.result_ready.connect(dialog.show_membership)
            worker.progress.connect(dialog.show_progress)
            worker.finished.connect(lambda worker=worker: self._article_check_finished(worker))
            dialog.finished.connect(lambda _result, worker=worker: worker.request_stop())
            worker.start()
        elif data.get("target_title"):
            dialog.show_membership([])
        dialog.exec()
        dialog.deleteLater()

    def _stop_article_check(self, *_args):
        for worker in tuple(self._article_workers):
            worker.request_stop()

    def _article_check_finished(self, worker):
        self._article_workers.discard(worker)
        if self.article_worker is worker:
            self.article_worker = None
        worker.deleteLater()

    def _links_source_changed(self, *_args):
        if not self._busy:
            self._invalidate_creation(clear_table=False)
            self._set_busy(False)
            self.creation_history.reset()

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
        if col == 5:
            data = self.table.item(row, 0).data(Qt.UserRole) or {}
            linked = data.get("status") == "scanned"
        self.table.viewport().setCursor(Qt.PointingHandCursor if linked else Qt.ArrowCursor)

    def _align_scan_input_captions(self):
        labels = (self.read_category_caption, self.scan_file_row.caption)
        width = max(label.sizeHint().width() for label in labels)
        for label in labels:
            label.setFixedWidth(width)

    def _apply_table_theme(self):
        self._align_scan_input_captions()
        theme = getattr(self.parent_window, "_theme_mode", "teal")
        base, _, text, header, _, border, _, selected, _ = category_table_palette(theme)
        for table in (self.table, self.creation_table):
            table.setStyleSheet(category_table_style(theme))
        for source, badge in ((self.source_lang, self.target_label), (self.links_source_lang, self.links_target_label)):
            style_transfer_languages(source, badge, theme)
        muted = {'light': '#526a7a', 'dark': '#a3b3c5', 'teal': '#91b9c5'}.get(theme, '#91b9c5')
        for label in (self.creation_summary, self.scan_guidance):
            label.setStyleSheet(f'color: {muted}; padding: 2px 0;')
        self.autofill_tabs.setStyleSheet(
            'QTabWidget, QTabBar { background: transparent; }'
            f"QTabWidget::pane {{ background: {base}; border: 1px solid {border}; border-radius: 8px; }}"
            f"QTabBar::tab {{ background: {header}; color: {text}; border: 1px solid {border}; "
            "padding: 6px 10px; border-top-left-radius: 8px; border-top-right-radius: 8px; }"
            f"QTabBar::tab:selected {{ background: {selected}; font-weight: bold; }}"
        )
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
        workspace = self.scan_workspace if stage is self else self.creation_workspace
        workspace.set_log_panel(self.log_panel)
        toolbar = self.scan_table_toolbar if stage is self else self.creation_table_toolbar
        toolbar.set_log_panel(self.log_panel)
        self.log_panel.refresh_localized_texts()
        self.footer.refresh_localized_texts()
        self._apply_table_theme()
        self.refresh_localized_texts()
        self._set_busy(self._busy)

    def refresh_localized_texts(self):
        lang = getattr(self.parent_window, "_ui_lang", "ru")
        if lang != self._last_ui_lang:
            self._last_ui_lang = lang
            for toolbar in self._toolbars:
                refresh_category_toolbar(toolbar, self._t)
            self.log_panel.refresh_localized_texts()
            self.root_edit.setPlaceholderText(self._t('root_placeholder'))
            self.root_edit.setToolTip(self._t('source_root_tip'))
            self.include_root.setText(self._t("include_root"))
            self.include_subcategories.setText(self._t('scan_subcategories'))
            self.include_subcategories.setToolTip(self._t('scan_subcategories_tip'))
            self.depth_spin.setToolTip(self._t("scan_depth_tip"))
            self.skip_spin.setToolTip(self._t("skip_tip"))
            self.autofill_btn.setText(self._t("autofill"))
            for index, key in enumerate(("titles_column", "content_column")):
                self.autofill_tabs.setTabText(index, self._t(key))
            self.source_content_empty.setText(self._t("source_content_empty"))
            self.source_content_empty.setToolTip(self._t("autofill_empty_tip"))
            self.autofill_title_help.setText(self._t("autofill_title_help"))
            self.autofill_content_help.setText(self._t('autofill_content_help'))
            self.translate_source_names.setText(self._t('translate_source_names'))
            self.translate_source_names.setToolTip(self._t('translate_source_names_tip'))
            self.title_template_edit.setPlaceholderText(self._t("number_target_example"))
            self.copy_first_btn.setToolTip(self._t("copy_first_tip"))
            self.load_text_btn.setToolTip(self._t("source_content_tip"))
            self.reset_scan_btn.setToolTip(self._t("reset_scan_tip"))
            self.export_scan_btn.setText(self._t("export_plan"))
            self.scan_file_row.refresh_localized_texts()
            self.scan_file_row.caption.setText(self._t('ready_categories_file'))
            self.scan_file_row.setToolTip(self._t('scan_inputs_tip'))
            self.file_controls.refresh_localized_texts()
            self.file_controls.caption.setText(self._t('preparation_file'))
            self.file_controls.setToolTip(self._t('import_file_tip'))
            for table in (self.table, self.creation_table):
                for col, key in enumerate(table.property("transfer_header_keys")):
                    table.horizontalHeaderItem(col).setText(self._t(key))
                    table.horizontalHeaderItem(col).setToolTip(self._t("potential" if key == "potential_column" else key))
            self.creation_table.horizontalHeaderItem(2).setToolTip(self._t('edit_category_text_tip'))
            self._refresh_scan_headers()
            for page in (self, self.creation_stage):
                for label in page.findChildren(QLabel):
                    key = label.property('transfer_text_key')
                    if key:
                        label.setText(self._t(key))
                for group in page.findChildren(QGroupBox):
                    key = group.property("transfer_text_key")
                    if key:
                        group.setTitle(self._t(key))
                        if key == 'preparation_list':
                            group.setToolTip(self._t('creation_hint'))
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
            sorting = self.creation_table.isSortingEnabled()
            self.creation_table.setSortingEnabled(False)
            for row in range(self.creation_table.rowCount()):
                item = self.creation_table.item(row, 5)
                if item and item.data(Qt.UserRole):
                    item.setText(self._t(item.data(Qt.UserRole)))
            self.creation_table.setSortingEnabled(sorting)
            self._refresh_target()
            self._refresh_creation_summary()
            self._show_creation_detail()
            self._set_busy(self._busy)

        self._align_scan_input_captions()
        for page in (self, self.creation_stage):
            refresh_category_button_tooltips(page, self._t)

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
        self._stop_article_check()
        if self._busy:
            # The worker keeps its original immutable context until it stops.
            self._pending_context_reset = True
            self.stop()
        else:
            self.table.setRowCount(0)
            self._invalidate_creation(clear_table=False)
            self._set_busy(False)
            self.scan_history.reset()

    def _reset_changed_context(self):
        if self._pending_context_reset:
            self._pending_context_reset = False
            self.table.setRowCount(0)
            self._invalidate_creation(clear_table=False)
            self._linked_categories = []
            self._set_busy(False)
            self.scan_history.reset()

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
