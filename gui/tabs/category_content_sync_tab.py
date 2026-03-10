# -*- coding: utf-8 -*-
"""
Вкладка синхронизации содержимого категорий между языками.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal, QUrl, QEvent, QTimer, QRect
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QShortcut,
    QKeySequence,
    QGuiApplication,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QCheckBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QHeaderView,
)

from ...core.localization import translate_key
from ...core.api_client import WikimediaAPIClient
from ...core.pywikibot_config import apply_pwb_config
from ...utils import debug
from ...workers.category_content_sync_worker import (
    CategoryContentSyncPreviewWorker,
    CategoryContentSyncWorker,
    _prepare_target_categories,
    _build_page_url,
)
from ..widgets.shared_panels import CategorySourcePanel
from ..widgets.ui_helpers import (
    add_info_button,
    create_log_wrap,
    inc_progress,
    init_progress,
    log_message,
    set_start_stop_ratio,
)


class CategoryContentSyncTab(QWidget):
    """Вкладка для переноса категорийного наполнения между языками."""

    language_changed = Signal(str)
    family_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent
        self.sync_worker: Optional[CategoryContentSyncWorker] = None
        self.preview_worker: Optional[CategoryContentSyncPreviewWorker] = None
        self.current_user: Optional[str] = None
        self.current_lang: Optional[str] = None
        self.current_family: Optional[str] = None

        self._preview_ready = False
        self._preview_mode = False
        self._preview_signature = None
        self._running_preview_signature = None
        self._preview_row_index: dict[str, int] = {}
        self._preview_cancel_requested = False
        self._sync_cancel_requested = False
        self._suspend_preview_invalidation = False
        self._auto_depth_origin_map: dict[str, str] = {}
        self._depth_expanded_categories: list[str] = []
        self._depth_expanded_base_signature = None
        self._last_ui_lang = self._ui_lang()
        self._last_theme_mode = ""
        self._current_progress_row_key = ""
        self._current_progress_title = ""
        self._current_progress_done = 0
        self._current_progress_total: Optional[int] = None
        self._preview_cols_user_resized = False
        self._preview_cols_auto_applying = False
        # target, wikidata, source, total, transferred, status
        self._preview_col_ratios = (0.30, 0.09, 0.23, 0.12, 0.08, 0.18)
        self._preview_header_mouse_down = False

        self.setup_ui()

    def _ui_lang(self) -> str:
        try:
            raw = str(getattr(self.parent_window, "_ui_lang", "ru")).lower()
        except Exception:
            raw = "ru"
        return "en" if raw.startswith("en") else "ru"

    def _t(self, key: str, fallback: str) -> str:
        return translate_key(key, self._ui_lang(), fallback)

    def _processed_label(self) -> str:
        return self._t("ui.processed_short", "Processed")

    def _default_summary_template(self) -> str:
        return self._t(
            "ui.sync.summary_template_default",
            (
                "Adding {target_action_en} [[{target_category}]] (based on [[{source_lang}:{source_category}]])"
            ),
        )

    @staticmethod
    def _row_key_role() -> int:
        return int(Qt.UserRole + 1)

    @staticmethod
    def _status_code_role() -> int:
        return int(Qt.UserRole + 2)

    def setup_ui(self):
        help_text = self._t(
            "help.sync.main",
            (
                "The left panel is used for target categories (where members are added).\nYou can provide one category, paste a list manually, or load a .txt file.\nTarget language is taken from the active login.\nSource language is set in the right settings block.\n\"Articles\" mode transfers linked articles via Wikidata; \"Subcategories\" mode transfers linked subcategories.\nTransfer depth: 0 = only categories from the list, 1 = + direct subcategories for each category, 2+ = deeper.\nBefore preview, the target category list is expanded by this depth.\nBefore running, click \"Preview\": mapping table will appear and then \"Start sync\" becomes enabled.\n\nEdit summary template variables:\n{target_action_en} - \"category\" or \"parent category\" depending on mode.\n{target_category} - target category (with prefix).\n{source_category} - source category (with prefix).\n{target_page} - page being edited.\n{source_page} - source page/subcategory from source wiki.\n{source_lang} - source language, {target_lang} - target language.\n{family} - project family (for example, wikipedia).\n{mode} - transfer mode: articles or subcategories."
            ),
        )

        layout = QHBoxLayout(self)
        try:
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)
        except Exception:
            pass

        self.source_panel = CategorySourcePanel(
            self,
            parent_window=self.parent_window,
            help_text=help_text,
            group_title=self._t("ui.sync.target_group", "Target categories"),
            category_section_label=self._t(
                "ui.sync.fetch_category_block", "<b>Fetch category contents</b>"
            ),
            category_placeholder=self._t(
                "ui.sync.category_placeholder", "Category name to fetch"
            ),
            manual_label=self._t(
                "ui.sync.manual_list_label", "<b>Categories to sync</b>"
            ),
            manual_placeholder=self._t(
                "ui.sync.manual_list_placeholder",
                "Target categories (one per line)",
            ),
            file_section_label=self._t(
                "ui.sync.file_section", "<b>Or load list from file</b>"
            ),
            file_caption=self._t("ui.sync.file_caption", "File (.txt):"),
        )
        self.sync_ns_combo = self.source_panel.ns_combo
        self.cat_edit = self.source_panel.cat_edit
        self.left_stack = QStackedWidget(self)
        self.left_stack.setObjectName("syncLeftStack")
        self.left_stack.addWidget(self.source_panel)

        preview_container = QWidget(self.left_stack)
        preview_layout = QVBoxLayout(preview_container)
        try:
            preview_layout.setContentsMargins(0, 0, 0, 0)
            preview_layout.setSpacing(8)
        except Exception:
            pass

        self.preview_group = QGroupBox(
            self._t("ui.sync.preview_group", "Mapping preview")
        )
        preview_group_layout = QVBoxLayout(self.preview_group)
        try:
            preview_group_layout.setContentsMargins(6, 10, 6, 6)
            preview_group_layout.setSpacing(4)
        except Exception:
            pass

        self.preview_table = QTableWidget(0, 6, self.preview_group)
        self.preview_table.setObjectName("syncPreviewTable")
        self._update_preview_headers()
        try:
            self.preview_table.setSelectionBehavior(QAbstractItemView.SelectRows)
            self.preview_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
            self.preview_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self.preview_table.setAlternatingRowColors(True)
            self.preview_table.verticalHeader().setVisible(False)
            header = self.preview_table.horizontalHeader()
            header.setStretchLastSection(False)
            header.setSortIndicatorShown(True)
            try:
                header.setMinimumSectionSize(36)
            except Exception:
                pass
            for col in range(6):
                header.setSectionResizeMode(col, QHeaderView.Interactive)
            self.preview_table.setWordWrap(False)
            self.preview_table.setSortingEnabled(True)
        except Exception:
            pass
        try:
            self.preview_table.installEventFilter(self)
        except Exception:
            pass
        try:
            self.preview_table.viewport().installEventFilter(self)
        except Exception:
            pass
        try:
            self.preview_table.horizontalHeader().viewport().installEventFilter(self)
        except Exception:
            pass
        try:
            self._delete_selected_shortcut = QShortcut(
                QKeySequence(Qt.Key_Delete), self.preview_table
            )
            self._delete_selected_shortcut.activated.connect(
                self.delete_selected_preview_rows
            )
            self._backspace_selected_shortcut = QShortcut(
                QKeySequence(Qt.Key_Backspace), self.preview_table
            )
            self._backspace_selected_shortcut.activated.connect(
                self.delete_selected_preview_rows
            )
        except Exception:
            pass
        try:
            self.preview_table.horizontalHeader().sortIndicatorChanged.connect(
                lambda *_args: self._rebuild_preview_row_index()
            )
        except Exception:
            pass
        try:
            self.preview_table.horizontalHeader().sectionResized.connect(
                self._on_preview_header_section_resized
            )
        except Exception:
            pass
        self._schedule_preview_autofit()
        self._apply_preview_table_theme()
        preview_group_layout.addWidget(self.preview_table, 1)
        preview_layout.addWidget(self.preview_group, 1)

        preview_actions_row = QHBoxLayout()
        try:
            preview_actions_row.setContentsMargins(0, 0, 0, 0)
            preview_actions_row.setSpacing(6)
        except Exception:
            pass
        self.current_progress_bar = QProgressBar()
        self.current_progress_bar.setObjectName("currentCategoryProgressBar")
        try:
            self.current_progress_bar.setMinimum(0)
            self.current_progress_bar.setMaximum(1)
            self.current_progress_bar.setValue(0)
            self.current_progress_bar.setTextVisible(True)
            self.current_progress_bar.setAlignment(Qt.AlignCenter)
            self.current_progress_bar.setFormat(
                self._t("ui.sync.current_progress_idle", "Current: 0/0 · 0%")
            )
            self.current_progress_bar.setMinimumWidth(240)
        except Exception:
            pass
        self._apply_current_progress_style()
        preview_actions_row.addWidget(self.current_progress_bar, 1)
        self.clear_skipped_btn = QPushButton(
            self._t("ui.sync.clear_skipped", "Clear skipped")
        )
        self.clear_skipped_btn.setEnabled(False)
        self.clear_skipped_btn.clicked.connect(self.clear_skipped_rows)
        preview_actions_row.addWidget(self.clear_skipped_btn)
        preview_layout.addLayout(preview_actions_row)

        self.left_stack.addWidget(preview_container)
        self.left_stack.setCurrentIndex(0)
        layout.addWidget(self.left_stack, 6)

        right_wrap = QWidget(self)
        right_layout = QVBoxLayout(right_wrap)
        try:
            right_layout.setContentsMargins(6, 0, 0, 0)
            right_layout.setSpacing(8)
        except Exception:
            pass

        self.settings_group = QGroupBox(
            self._t("ui.sync.settings_group", "Sync settings")
        )
        try:
            self.settings_group.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        except Exception:
            pass
        settings_layout = QVBoxLayout(self.settings_group)
        try:
            settings_layout.setContentsMargins(8, 8, 8, 8)
            settings_layout.setSpacing(6)
        except Exception:
            pass

        row_lang_top = QHBoxLayout()
        try:
            row_lang_top.setContentsMargins(0, 0, 0, 0)
            row_lang_top.setSpacing(6)
        except Exception:
            pass
        self.source_lang_label = QLabel(self._t("ui.sync.source_lang", "Source language:"))
        try:
            self.source_lang_label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        except Exception:
            pass
        row_lang_top.addWidget(self.source_lang_label)
        self.source_lang_edit = QLineEdit("en")
        try:
            self.source_lang_edit.setObjectName("syncSourceLangEdit")
            self.source_lang_edit.setMinimumWidth(62)
            self.source_lang_edit.setMaximumWidth(74)
            self.source_lang_edit.setToolTip(
                self._t(
                    "ui.sync.source_lang_tip",
                    "Editable source language code (for example: en, de, fr).",
                )
            )
        except Exception:
            pass
        row_lang_top.addWidget(self.source_lang_edit)
        row_lang_top.addSpacing(4)

        self.target_lang_label = QLabel(
            self._t("ui.sync.target_lang", "Target language:")
        )
        try:
            self.target_lang_label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        except Exception:
            pass
        row_lang_top.addWidget(self.target_lang_label)
        self.target_lang_value = QLabel("")
        try:
            self.target_lang_value.setObjectName("targetLangCodeBadge")
            self.target_lang_value.setAlignment(Qt.AlignCenter)
            self.target_lang_value.setFrameShape(QFrame.NoFrame)
            self.target_lang_value.setMargin(3)
            self.target_lang_value.setMinimumWidth(42)
            self.target_lang_value.setMaximumWidth(50)
            self.target_lang_value.setToolTip(
                self._t(
                    "ui.sync.target_lang_tip",
                    "Target language comes from active authorization and is read-only here.",
                )
            )
        except Exception:
            pass
        row_lang_top.addWidget(self.target_lang_value)

        self.depth_label = QLabel(self._t("ui.sync.depth", "Transfer depth:"))
        try:
            self.depth_label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        except Exception:
            pass
        row_lang_top.addSpacing(8)
        row_lang_top.addWidget(self.depth_label)

        self.article_depth_spin = QSpinBox()
        self.article_depth_spin.setObjectName("depthSpin")
        self.article_depth_spin.setMinimum(0)
        self.article_depth_spin.setMaximum(99)
        self.article_depth_spin.setValue(0)
        self.article_depth_spin.setToolTip(
            self._t(
                "ui.sync.depth_tip",
                "0 = only categories from the list; 1 = + direct subcategories for each category; 2+ = deeper.",
            )
        )
        try:
            self.article_depth_spin.setFixedWidth(34)
            self.article_depth_spin.setAlignment(Qt.AlignCenter)
            self.article_depth_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        except Exception:
            pass

        self.depth_plus_btn = QToolButton()
        self.depth_plus_btn.setObjectName("depthStepButton")
        self.depth_plus_btn.setText("+")
        self.depth_plus_btn.setToolTip(self._t("ui.sync.depth_plus", "Increase depth"))
        self.depth_plus_btn.clicked.connect(self.article_depth_spin.stepUp)

        self.depth_minus_btn = QToolButton()
        self.depth_minus_btn.setObjectName("depthStepButton")
        self.depth_minus_btn.setText("-")
        self.depth_minus_btn.setToolTip(self._t("ui.sync.depth_minus", "Decrease depth"))
        self.depth_minus_btn.clicked.connect(self.article_depth_spin.stepDown)
        try:
            self.depth_plus_btn.setFixedSize(12, 12)
            self.depth_minus_btn.setFixedSize(12, 12)
        except Exception:
            pass

        depth_step_layout = QVBoxLayout()
        try:
            depth_step_layout.setContentsMargins(0, 0, 0, 0)
            depth_step_layout.setSpacing(0)
        except Exception:
            pass
        depth_step_layout.addWidget(self.depth_plus_btn)
        depth_step_layout.addWidget(self.depth_minus_btn)

        depth_wrap = QWidget()
        depth_wrap_layout = QHBoxLayout(depth_wrap)
        try:
            depth_wrap_layout.setContentsMargins(0, 0, 0, 0)
            depth_wrap_layout.setSpacing(2)
        except Exception:
            pass
        depth_wrap_layout.addWidget(self.article_depth_spin)
        depth_wrap_layout.addLayout(depth_step_layout)
        row_lang_top.addWidget(depth_wrap, 0, Qt.AlignLeft)
        row_lang_top.addStretch(1)
        add_info_button(self, row_lang_top, help_text, inline=True)
        settings_layout.addLayout(row_lang_top)
        self._refresh_target_lang_label()
        self._apply_target_lang_badge_style()

        row_modes = QHBoxLayout()
        try:
            row_modes.setContentsMargins(0, 0, 0, 0)
            row_modes.setSpacing(8)
        except Exception:
            pass
        self.articles_checkbox = QCheckBox(self._t("ui.sync.articles", "Transfer articles"))
        self.articles_checkbox.setChecked(True)
        row_modes.addWidget(self.articles_checkbox)

        self.subcategories_checkbox = QCheckBox(
            self._t("ui.sync.subcategories", "Transfer subcategories")
        )
        self.subcategories_checkbox.setChecked(False)
        row_modes.addWidget(self.subcategories_checkbox)
        row_modes.addStretch(1)
        settings_layout.addLayout(row_modes)

        row_summary = QHBoxLayout()
        try:
            row_summary.setContentsMargins(0, 0, 0, 0)
            row_summary.setSpacing(8)
        except Exception:
            pass
        self.summary_label = QLabel(self._t("ui.edit_summary", "Edit summary:"))
        row_summary.addWidget(self.summary_label)
        self.summary_edit = QLineEdit(self._default_summary_template())
        self.summary_edit.setPlaceholderText(
            self._t(
                "ui.sync.summary_placeholder",
                "Supported variables: {target_action_en}, {target_category}, {source_category}, {target_page}, {source_lang}, {target_lang}, {mode}",
            )
        )
        row_summary.addWidget(self.summary_edit, 1)
        settings_layout.addLayout(row_summary)

        right_layout.addWidget(self.settings_group)

        self.sync_log = QTextEdit()
        self.sync_log.setReadOnly(True)
        mono_font = QFont("Consolas", 9)
        if not mono_font.exactMatch():
            mono_font = QFont("Courier New", 9)
        mono_font.setFixedPitch(True)
        self.sync_log.setFont(mono_font)
        log_wrap = create_log_wrap(self, self.sync_log, with_header=True)
        right_layout.addWidget(log_wrap, 1)

        bottom_row = QHBoxLayout()
        try:
            bottom_row.setContentsMargins(0, 0, 0, 0)
            bottom_row.setSpacing(6)
        except Exception:
            pass

        self.progress_label = QLabel(
            self._t("ui.processed_counter_initial", "Processed 0/0")
        )
        try:
            self.progress_label.setVisible(False)
        except Exception:
            pass
        self.progress_bar = QProgressBar()
        try:
            self.progress_bar.setMaximum(1)
            self.progress_bar.setValue(0)
            self.progress_bar.setTextVisible(True)
            self.progress_bar.setFormat(
                self._t("ui.processed_counter_initial", "Processed 0/0")
            )
        except Exception:
            pass

        progress_wrap = QWidget()
        progress_layout = QHBoxLayout(progress_wrap)
        try:
            progress_layout.setContentsMargins(0, 0, 0, 0)
            progress_layout.setSpacing(6)
        except Exception:
            pass
        progress_layout.addWidget(self.progress_label)
        progress_layout.addWidget(self.progress_bar)
        bottom_row.addWidget(progress_wrap, 1)

        self.preview_btn = QPushButton(self._t("ui.preview", "Preview"))
        try:
            self.preview_btn.setMinimumWidth(164)
        except Exception:
            pass
        self.preview_btn.clicked.connect(self.preview_or_toggle_settings)
        bottom_row.addWidget(self.preview_btn)

        self.start_btn = QPushButton(self._t("ui.sync.start", "Start sync"))
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self.start_sync)
        bottom_row.addWidget(self.start_btn)

        self.stop_btn = QPushButton(self._t("ui.stop", "Stop"))
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_sync)
        bottom_row.addWidget(self.stop_btn)

        set_start_stop_ratio(self.start_btn, self.stop_btn, 3)
        self._sync_action_buttons_compact()

        right_layout.addLayout(bottom_row)
        layout.addWidget(right_wrap, 5)

        self.source_panel.set_log_widget(self.sync_log)
        self.source_lang_edit.textChanged.connect(self._update_preview_headers)
        self._bind_preview_invalidation()
        self._update_clear_skipped_button_state()
        self._reset_current_category_progress()
        self._sync_preview_button_width()

    def _bind_preview_invalidation(self):
        handlers = [
            (self.source_lang_edit, "textChanged"),
            (self.articles_checkbox, "toggled"),
            (self.subcategories_checkbox, "toggled"),
            (self.article_depth_spin, "valueChanged"),
            (self.source_panel.manual_list, "textChanged"),
            (self.source_panel.in_path, "textChanged"),
            (self.source_panel.cat_edit, "textChanged"),
        ]
        for obj, signal_name in handlers:
            try:
                getattr(obj, signal_name).connect(self._invalidate_preview_state)
            except Exception:
                pass

    def _refresh_target_lang_label(self):
        lang = (self.get_current_language() or "ru").strip().lower()
        self.target_lang_value.setText(lang)

    def _update_preview_headers(self):
        source_edit = getattr(self, "source_lang_edit", None)
        if source_edit is not None:
            try:
                source_lang = (source_edit.text() or "").strip().lower()
            except Exception:
                source_lang = ""
        else:
            source_lang = ""
        source_col = self._t("ui.sync.preview.col.source", "Linked category")
        if source_lang:
            source_col = f"{source_col} ({source_lang})"
        self.preview_table.setHorizontalHeaderLabels(
            [
                self._t("ui.sync.preview.col.target", "Target category"),
                self._t("ui.sync.preview.col.wikidata", "Wikidata"),
                source_col,
                self._t("ui.sync.preview.col.count", "Total in category"),
                self._t("ui.sync.preview.col.transferred", "Transferred"),
                self._t("ui.sync.preview.col.status", "Status"),
            ]
        )

    def _invalidate_preview_state(self, *_args):
        if self._suspend_preview_invalidation:
            return
        if self.sync_worker and self.sync_worker.isRunning():
            return
        if self.preview_worker and self.preview_worker.isRunning():
            return
        self._preview_ready = False
        self._preview_cancel_requested = False
        self._preview_signature = None
        self._running_preview_signature = None
        self._preview_row_index = {}
        self._depth_expanded_categories = []
        self._depth_expanded_base_signature = None
        self._auto_depth_origin_map = {}
        self.start_btn.setEnabled(False)
        self.preview_table.setRowCount(0)
        self._reset_current_category_progress()
        self._update_clear_skipped_button_state()
        self._set_preview_mode(False)

    def _set_preview_mode(self, active: bool):
        self._preview_mode = bool(active)
        self.left_stack.setCurrentIndex(1 if self._preview_mode else 0)
        if self._preview_mode:
            self.preview_btn.setText(
                self._t("ui.sync.back_to_settings", "Back to settings")
            )
            self._sync_preview_button_width()
            self._sync_action_buttons_compact()
            self._schedule_preview_autofit()
            try:
                QTimer.singleShot(40, self._fit_preview_columns_to_table)
                QTimer.singleShot(120, self._fit_preview_columns_to_table)
            except Exception:
                pass
        else:
            self.preview_btn.setText(self._t("ui.preview", "Preview"))
            self._sync_preview_button_width()
            self._sync_action_buttons_compact()
        self._refresh_start_button_state()

    def _sync_action_buttons_compact(self):
        start_btn = getattr(self, "start_btn", None)
        stop_btn = getattr(self, "stop_btn", None)
        if start_btn is None or stop_btn is None:
            return
        try:
            fm = start_btn.fontMetrics()
            start_text = self._t("ui.sync.start", "Start sync")
            stop_text = self._t("ui.stop", "Stop")
            start_w = max(102, int(fm.horizontalAdvance(str(start_text or ""))) + 14)
            stop_w = max(82, int(fm.horizontalAdvance(str(stop_text or ""))) + 12)
            start_btn.setMaximumWidth(16777215)
            stop_btn.setMaximumWidth(16777215)
            start_btn.setMinimumWidth(int(start_w))
            stop_btn.setMinimumWidth(int(stop_w))
        except Exception:
            pass

    def _sync_preview_button_width(self):
        btn = getattr(self, "preview_btn", None)
        if btn is None:
            return
        try:
            fm = btn.fontMetrics()
            text = str(btn.text() or self._t("ui.preview", "Preview"))
            text_w = int(fm.horizontalAdvance(text))
            # Ширина строго по текущему тексту (+поля), без чрезмерного запаса.
            target_w = max(136, min(198, text_w + 18))
            btn.setMinimumWidth(int(target_w))
            btn.setMaximumWidth(16777215)
        except Exception:
            try:
                btn.setMinimumWidth(160)
                btn.setMaximumWidth(16777215)
            except Exception:
                pass

    def _theme_mode(self) -> str:
        try:
            return str(getattr(self.parent_window, "_theme_mode", "teal") or "teal").strip().lower()
        except Exception:
            return "teal"

    def _is_light_theme(self) -> bool:
        mode = self._theme_mode()
        return mode == "light" or mode.startswith("light")

    def _is_dark_black_theme(self) -> bool:
        mode = self._theme_mode()
        return mode == "dark" or mode.startswith("dark")

    def _apply_preview_table_theme(self):
        if self._is_light_theme():
            self.preview_table.setStyleSheet(
                """
                QTableWidget#syncPreviewTable {
                    background: #ffffff;
                    alternate-background-color: #f4f8fc;
                    color: #1f2f3a;
                    border: 1px solid #bcd1e2;
                    border-radius: 8px;
                    gridline-color: #c4d8e8;
                }
                QTableWidget#syncPreviewTable QHeaderView::section {
                    background: #e7f0f7;
                    color: #1e4763;
                    border: 1px solid #bcd1e2;
                    padding: 4px 6px;
                }
                QTableWidget#syncPreviewTable::item:selected {
                    background: #d7eaf9;
                    color: #102a43;
                }
                """
            )
        elif self._is_dark_black_theme():
            # Midnight dark: нейтральная палитра без teal/green оттенков.
            self.preview_table.setStyleSheet(
                """
                QTableWidget#syncPreviewTable {
                    background: #171b22;
                    alternate-background-color: #1e242d;
                    color: #e5e9ef;
                    border: 1px solid #3d4756;
                    border-radius: 8px;
                    gridline-color: #445062;
                }
                QTableWidget#syncPreviewTable QHeaderView::section {
                    background: #242c37;
                    color: #dfe6ef;
                    border: 1px solid #3d4756;
                    padding: 4px 6px;
                }
                QTableWidget#syncPreviewTable::item:selected {
                    background: #314257;
                    color: #f3f7fb;
                }
                """
            )
        else:
            # Twilight teal: фирменная teal-палитра.
            self.preview_table.setStyleSheet(
                """
                QTableWidget#syncPreviewTable {
                    background: #0a2533;
                    alternate-background-color: #0e3042;
                    color: #e6f3f6;
                    border: 1px solid rgba(115, 170, 182, 0.45);
                    border-radius: 8px;
                    gridline-color: rgba(96, 179, 192, 0.35);
                }
                QTableWidget#syncPreviewTable QHeaderView::section {
                    background: #144055;
                    color: #e8f7fa;
                    border: 1px solid rgba(115, 170, 182, 0.45);
                    padding: 4px 6px;
                }
                QTableWidget#syncPreviewTable::item:selected {
                    background: rgba(82, 148, 168, 0.55);
                    color: #f2fcff;
                }
                """
            )
        self._refresh_preview_link_styles()
        self._apply_current_progress_style()
        self._apply_target_lang_badge_style()
        self._schedule_preview_autofit()

    def _schedule_preview_autofit(self):
        if getattr(self, "_preview_cols_user_resized", False):
            return
        try:
            QTimer.singleShot(0, self._fit_preview_columns_to_table)
        except Exception:
            pass

    def _fit_preview_columns_to_table(self):
        if getattr(self, "_preview_cols_user_resized", False):
            return
        table = getattr(self, "preview_table", None)
        if table is None:
            return
        if not getattr(self, "_preview_mode", False):
            return
        try:
            if not table.isVisible():
                return
        except Exception:
            pass
        try:
            available = int(table.viewport().width())
        except Exception:
            available = 0
        if available <= 0:
            return
        ratios = tuple(getattr(self, "_preview_col_ratios", ()) or ())
        if len(ratios) != 6:
            ratios = (0.30, 0.09, 0.23, 0.12, 0.08, 0.18)
        try:
            min_w = int(table.horizontalHeader().minimumSectionSize())
        except Exception:
            min_w = 24
        min_w = max(24, min_w)
        min_ws = [min_w] * 6
        # Колонка «Статус» должна по умолчанию быть заметно шире, чтобы не резать текст.
        min_ws[5] = max(min_w, 132)

        widths = [
            max(int(min_ws[i]), int(round(float(available) * float(r))))
            for i, r in enumerate(ratios)
        ]
        total = int(sum(widths))
        if total < available:
            widths[0] += int(available - total)
        elif total > available:
            excess = int(total - available)
            for idx in sorted(range(len(widths)), key=lambda i: widths[i], reverse=True):
                if excess <= 0:
                    break
                can_take = max(0, int(widths[idx] - int(min_ws[idx])))
                if can_take <= 0:
                    continue
                take = min(can_take, excess)
                widths[idx] -= take
                excess -= take

        self._preview_cols_auto_applying = True
        try:
            for i, w in enumerate(widths):
                try:
                    table.setColumnWidth(int(i), max(min_w, int(w)))
                except Exception:
                    pass
        finally:
            self._preview_cols_auto_applying = False

    def _on_preview_header_section_resized(self, _index: int, old_size: int, new_size: int):
        if getattr(self, "_preview_cols_auto_applying", False):
            return
        if not bool(getattr(self, "_preview_header_mouse_down", False)):
            return
        try:
            if int(old_size) == int(new_size):
                return
        except Exception:
            pass
        # После ручного изменения ширин перестаём автоподгонять колонки при ресайзе окна.
        self._preview_cols_user_resized = True

    def _is_click_on_link_text(self, row: int, col: int, click_pos) -> bool:
        item = self.preview_table.item(int(row), int(col))
        if item is None:
            return False
        try:
            url = str(item.data(Qt.UserRole) or "").strip()
        except Exception:
            url = ""
        if not url:
            return False
        text = str(item.text() or "")
        if not text:
            return False
        try:
            idx = self.preview_table.model().index(int(row), int(col))
            if not idx.isValid():
                return False
            cell_rect = self.preview_table.visualRect(idx)
        except Exception:
            return False
        if not cell_rect.isValid():
            return False
        try:
            fm = self.preview_table.fontMetrics()
            text_w = max(1, int(fm.horizontalAdvance(text)))
            text_h = max(1, int(fm.height()))
        except Exception:
            return False

        try:
            alignment = int(item.textAlignment() or 0)
        except Exception:
            alignment = 0

        pad_x = 6
        pad_y = 2
        if alignment & Qt.AlignHCenter:
            text_x = int(cell_rect.x() + max(0, (cell_rect.width() - text_w) // 2))
        elif alignment & Qt.AlignRight:
            text_x = int(cell_rect.right() - text_w - pad_x)
        else:
            text_x = int(cell_rect.x() + pad_x)
        if alignment & Qt.AlignTop:
            text_y = int(cell_rect.y() + pad_y)
        elif alignment & Qt.AlignBottom:
            text_y = int(cell_rect.bottom() - text_h - pad_y)
        else:
            text_y = int(cell_rect.y() + max(0, (cell_rect.height() - text_h) // 2))

        hit_rect = QRect(text_x, text_y, text_w, text_h).adjusted(-2, -1, 2, 1)
        try:
            return bool(hit_rect.contains(click_pos))
        except Exception:
            return False

    def eventFilter(self, obj, event):
        table = getattr(self, "preview_table", None)
        if obj is table:
            try:
                if (
                    event is not None
                    and event.type() == QEvent.Resize
                    and not getattr(self, "_preview_cols_user_resized", False)
                ):
                    self._fit_preview_columns_to_table()
            except Exception:
                pass
        elif table is not None and obj is table.horizontalHeader().viewport():
            try:
                if event is not None and event.type() == QEvent.MouseButtonPress:
                    try:
                        if event.button() == Qt.LeftButton:
                            self._preview_header_mouse_down = True
                    except Exception:
                        pass
                elif event is not None and event.type() == QEvent.MouseButtonRelease:
                    self._preview_header_mouse_down = False
            except Exception:
                self._preview_header_mouse_down = False
        elif table is not None and obj is table.viewport():
            try:
                if (
                    event is not None
                    and event.type() == QEvent.Resize
                    and not getattr(self, "_preview_cols_user_resized", False)
                ):
                    self._fit_preview_columns_to_table()
                if event is not None and event.type() == QEvent.MouseButtonRelease:
                    try:
                        if event.button() != Qt.LeftButton:
                            return super().eventFilter(obj, event)
                    except Exception:
                        pass
                    mods = QGuiApplication.keyboardModifiers()
                    if mods & (Qt.ControlModifier | Qt.ShiftModifier):
                        return super().eventFilter(obj, event)
                    try:
                        idx = table.indexAt(event.pos())
                    except Exception:
                        idx = None
                    if idx is not None and idx.isValid():
                        row = int(idx.row())
                        col = int(idx.column())
                        if self._is_click_on_link_text(row, col, event.pos()):
                            self._open_preview_cell_link(row, col)
            except Exception:
                pass
        elif event is not None and event.type() in (QEvent.Hide, QEvent.Leave):
            self._preview_header_mouse_down = False
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        try:
            super().resizeEvent(event)
        finally:
            if getattr(self, "_preview_mode", False) and not getattr(
                self, "_preview_cols_user_resized", False
            ):
                self._schedule_preview_autofit()

    def _apply_target_lang_badge_style(self):
        badge = getattr(self, "target_lang_value", None)
        source_edit = getattr(self, "source_lang_edit", None)
        target_label = getattr(self, "target_lang_label", None)
        if badge is None and source_edit is None:
            return
        if self._is_light_theme():
            if source_edit is not None:
                source_edit.setStyleSheet(
                    """
                    QLineEdit#syncSourceLangEdit {
                        min-height: 27px;
                        max-height: 27px;
                        color: #18364e;
                        background: #ffffff;
                        border: 2px solid #5f93be;
                        border-radius: 8px;
                        padding: 2px 8px;
                        font-weight: 700;
                        selection-background-color: #a9cdef;
                        selection-color: #0f2d43;
                    }
                    QLineEdit#syncSourceLangEdit:focus {
                        border: 2px solid #2d77b5;
                        background: #f9fcff;
                    }
                    """
                )
            if badge is not None:
                badge.setStyleSheet(
                    """
                    QLabel#targetLangCodeBadge {
                        color: #5d6b79;
                        background: #e6ebf1;
                        border: 1px solid #b3beca;
                        border-radius: 9px;
                        padding: 2px 8px;
                        font-weight: 700;
                    }
                    """
                )
            if target_label is not None:
                target_label.setStyleSheet("")
        elif self._is_dark_black_theme():
            if source_edit is not None:
                source_edit.setStyleSheet(
                    """
                    QLineEdit#syncSourceLangEdit {
                        min-height: 27px;
                        max-height: 27px;
                        color: #e5edf7;
                        background: #1d2430;
                        border: 2px solid #6e7d90;
                        border-radius: 8px;
                        padding: 2px 8px;
                        font-weight: 700;
                        selection-background-color: #6f8198;
                        selection-color: #f0f5fb;
                    }
                    QLineEdit#syncSourceLangEdit:focus {
                        border: 2px solid #8fa2b8;
                        background: #242d3a;
                    }
                    """
                )
            if badge is not None:
                badge.setStyleSheet(
                    """
                    QLabel#targetLangCodeBadge {
                        color: #c8d4e1;
                        background: #313a47;
                        border: 1px solid #6d7b8d;
                        border-radius: 9px;
                        padding: 2px 8px;
                        font-weight: 700;
                    }
                    """
                )
            if target_label is not None:
                target_label.setStyleSheet("")
        else:
            if source_edit is not None:
                source_edit.setStyleSheet(
                    """
                    QLineEdit#syncSourceLangEdit {
                        min-height: 27px;
                        max-height: 27px;
                        color: #e8f5ff;
                        background: #0f2a3b;
                        border: 2px solid #53a8c0;
                        border-radius: 8px;
                        padding: 2px 8px;
                        font-weight: 700;
                        selection-background-color: #3e8ea6;
                        selection-color: #eefbff;
                    }
                    QLineEdit#syncSourceLangEdit:focus {
                        border: 2px solid #72c6dd;
                        background: #123146;
                    }
                    """
                )
            if badge is not None:
                badge.setStyleSheet(
                    """
                    QLabel#targetLangCodeBadge {
                        color: #d3e6f4;
                        background: #2b3d4f;
                        border: 1px solid #5f7f96;
                        border-radius: 9px;
                        padding: 2px 8px;
                        font-weight: 700;
                    }
                    """
                )
            if target_label is not None:
                target_label.setStyleSheet("")

    def _apply_current_progress_style(self):
        bar = getattr(self, "current_progress_bar", None)
        if bar is None:
            return
        if self._is_light_theme():
            bar.setStyleSheet(
                """
                QProgressBar#currentCategoryProgressBar {
                    min-height: 27px;
                    max-height: 27px;
                    border-radius: 7px;
                    border: 1px solid #9ebbd4;
                    background: #eaf2f9;
                    color: #183349;
                    text-align: center;
                    font-weight: 600;
                    padding: 0 6px;
                }
                QProgressBar#currentCategoryProgressBar::chunk {
                    border-radius: 6px;
                    background: qlineargradient(
                        x1: 0, y1: 0, x2: 1, y2: 0,
                        stop: 0 #6fa9d8,
                        stop: 1 #8fc1e8
                    );
                }
                """
            )
        elif self._is_dark_black_theme():
            bar.setStyleSheet(
                """
                QProgressBar#currentCategoryProgressBar {
                    min-height: 27px;
                    max-height: 27px;
                    border-radius: 7px;
                    border: 1px solid #55657a;
                    background: #1a2330;
                    color: #e5edf7;
                    text-align: center;
                    font-weight: 600;
                    padding: 0 6px;
                }
                QProgressBar#currentCategoryProgressBar::chunk {
                    border-radius: 6px;
                    background: qlineargradient(
                        x1: 0, y1: 0, x2: 1, y2: 0,
                        stop: 0 #5f7893,
                        stop: 1 #8ea6bf
                    );
                }
                """
            )
        else:
            bar.setStyleSheet(
                """
                QProgressBar#currentCategoryProgressBar {
                    min-height: 27px;
                    max-height: 27px;
                    border-radius: 7px;
                    border: 1px solid #3d6f88;
                    background: #082c3f;
                    color: #e8f6fb;
                    text-align: center;
                    font-weight: 600;
                    padding: 0 6px;
                }
                QProgressBar#currentCategoryProgressBar::chunk {
                    border-radius: 6px;
                    background: qlineargradient(
                        x1: 0, y1: 0, x2: 1, y2: 0,
                        stop: 0 #3f9dad,
                        stop: 1 #67c7b7
                    );
                }
                """
            )

    def _refresh_preview_link_styles(self):
        """Обновляет цвет/шрифт уже отрисованных ссылок после смены темы."""
        try:
            rows = self.preview_table.rowCount()
        except Exception:
            return
        sorting_was = False
        try:
            sorting_was = self.preview_table.isSortingEnabled()
        except Exception:
            sorting_was = False
        self._set_preview_sorting(False)
        self.preview_table.setUpdatesEnabled(False)
        try:
            # Ссылки есть только в колонках 0, 1, 2.
            for row in range(rows):
                for col in (0, 1, 2):
                    item = self.preview_table.item(row, col)
                    if item is None:
                        continue
                    try:
                        url = str(item.data(Qt.UserRole) or "").strip()
                    except Exception:
                        url = ""
                    if not url:
                        continue
                    try:
                        item.setForeground(self._link_color())
                        font = item.font()
                        font.setUnderline(True)
                        if self._is_light_theme():
                            font.setWeight(QFont.DemiBold)
                        else:
                            font.setWeight(QFont.Normal)
                        item.setFont(font)
                    except Exception:
                        pass
        finally:
            self.preview_table.setUpdatesEnabled(True)
            self._set_preview_sorting(sorting_was)
            try:
                self.preview_table.viewport().update()
            except Exception:
                pass

    def _link_color(self) -> QColor:
        try:
            if self._is_light_theme():
                # Повышенный контраст для белой темы.
                return QColor("#0b3d91")
            return QColor("#8fb8ff")
        except Exception:
            return QColor("#8fb8ff")

    def _muted_color(self) -> QColor:
        try:
            if self._is_light_theme():
                return QColor("#4b5563")
            return QColor("#9aa5b5")
        except Exception:
            return QColor("#4b5563")

    def _make_table_item(
        self, text: str, *, url: str = "", align: int = Qt.AlignLeft | Qt.AlignVCenter
    ) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        try:
            item.setTextAlignment(int(align))
        except Exception:
            pass
        try:
            if text:
                item.setToolTip(str(text))
        except Exception:
            pass
        if url:
            try:
                item.setData(Qt.UserRole, url)
                item.setForeground(self._link_color())
                font = item.font()
                font.setUnderline(True)
                if self._is_light_theme():
                    font.setWeight(QFont.DemiBold)
                item.setFont(font)
            except Exception:
                pass
        return item

    def _status_text(self, code: str) -> str:
        mapping = {
            "previewing": f"⏳ {self._t('ui.sync.status.previewing', 'Preview')}",
            "preview_ready": f"✅ {self._t('ui.sync.status.ready_for_sync', 'Ready to sync')}",
            "in_progress": f"⏳ {self._t('ui.sync.status.in_progress', 'In progress')}",
            "done": f"✅ {self._t('ui.sync.status.done', 'Completed')}",
            "done_empty": f"✅ {self._t('ui.sync.status.done_empty', 'Completed (0)')}",
            "partial": f"☑️ {self._t('ui.sync.status.partial', 'Partial')}",
            "error": f"❌ {self._t('ui.sync.status.error', 'Error')}",
            "skipped": f"⚠️ {self._t('ui.sync.status.skipped', 'Skipped')}",
            "stopped": f"⏹️ {self._t('ui.sync.status.stopped', 'Stopped')}",
        }
        return mapping.get(code, f"⏳ {self._t('ui.sync.status.in_progress', 'In progress')}")

    def _set_row_status(self, row: int, code: str):
        status_item = self._make_table_item(
            self._status_text(code),
            align=Qt.AlignHCenter | Qt.AlignVCenter,
        )
        try:
            status_item.setData(self._status_code_role(), str(code or ""))
        except Exception:
            pass
        self.preview_table.setItem(row, 5, status_item)

    def _set_target_item(
        self, row: int, target_title: str, *, url: str = "", row_key: str = ""
    ):
        item = self._make_table_item(target_title, url=url)
        try:
            key = (row_key or target_title or "").strip().casefold()
            if key:
                item.setData(self._row_key_role(), key)
        except Exception:
            pass
        self.preview_table.setItem(row, 0, item)

    def _row_key_for_row(self, row: int) -> str:
        item = self.preview_table.item(row, 0)
        if item is None:
            return ""
        try:
            key = str(item.data(self._row_key_role()) or "").strip().casefold()
            if key:
                return key
        except Exception:
            pass
        try:
            return str(item.text() or "").strip().casefold()
        except Exception:
            return ""

    def _rebuild_preview_row_index(self):
        index: dict[str, int] = {}
        for row in range(self.preview_table.rowCount()):
            key = self._row_key_for_row(row)
            if key and key not in index:
                index[key] = row
        self._preview_row_index = index

    def _set_preview_sorting(self, enabled: bool):
        try:
            self.preview_table.setSortingEnabled(bool(enabled))
        except Exception:
            pass

    def _is_row_skipped(self, row: int) -> bool:
        status_item = self.preview_table.item(row, 5)
        if status_item is None:
            return False
        try:
            code = str(status_item.data(self._status_code_role()) or "").strip().lower()
            if code == "skipped":
                return True
        except Exception:
            pass
        try:
            raw = (status_item.text() or "").strip().lower()
        except Exception:
            raw = ""
        skip_txt = self._t("ui.sync.status.skipped", "Skipped").strip().lower()
        return ("⚠" in raw) or (skip_txt and skip_txt in raw)

    def _categories_from_preview_table(self) -> list[str]:
        categories: list[str] = []
        seen: set[str] = set()
        for row in range(self.preview_table.rowCount()):
            item = self.preview_table.item(row, 0)
            if item is None:
                continue
            title = str(item.text() or "").strip()
            if not title:
                continue
            key = self._row_key_for_row(row)
            dedupe_key = key or title.casefold()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            categories.append(title)
        return categories

    def _update_clear_skipped_button_state(self):
        btn = getattr(self, "clear_skipped_btn", None)
        if btn is None:
            return
        running = bool(
            (self.preview_worker and self.preview_worker.isRunning())
            or (self.sync_worker and self.sync_worker.isRunning())
        )
        has_skipped = any(
            self._is_row_skipped(row) for row in range(self.preview_table.rowCount())
        )
        btn.setEnabled((not running) and has_skipped)

    def clear_skipped_rows(self):
        if (self.preview_worker and self.preview_worker.isRunning()) or (
            self.sync_worker and self.sync_worker.isRunning()
        ):
            return
        rows_to_remove = [
            row for row in range(self.preview_table.rowCount()) if self._is_row_skipped(row)
        ]
        if not rows_to_remove:
            self._update_clear_skipped_button_state()
            return

        sorting_was = False
        try:
            sorting_was = self.preview_table.isSortingEnabled()
        except Exception:
            sorting_was = False
        self._set_preview_sorting(False)
        self.preview_table.setUpdatesEnabled(False)
        try:
            for row in sorted(rows_to_remove, reverse=True):
                self.preview_table.removeRow(row)
        finally:
            self.preview_table.setUpdatesEnabled(True)
            self._set_preview_sorting(sorting_was)
            try:
                self.preview_table.viewport().update()
            except Exception:
                pass
        self._rebuild_preview_row_index()

        remaining = self.preview_table.rowCount()
        if remaining <= 0:
            self._preview_ready = False
            self._preview_signature = None
            self.start_btn.setEnabled(False)

        self._update_clear_skipped_button_state()
        log_message(
            self.sync_log,
            self._t(
                "ui.sync.preview.skipped_removed",
                "Removed rows with \"Skipped\" status: {count}",
            ).format(count=len(rows_to_remove)),
            debug,
        )

    def delete_selected_preview_rows(self):
        if (self.preview_worker and self.preview_worker.isRunning()) or (
            self.sync_worker and self.sync_worker.isRunning()
        ):
            return
        try:
            selected_rows = {
                index.row() for index in self.preview_table.selectionModel().selectedRows()
            }
        except Exception:
            selected_rows = set()
        if not selected_rows:
            try:
                selected_rows = {idx.row() for idx in self.preview_table.selectedIndexes()}
            except Exception:
                selected_rows = set()
        if not selected_rows:
            return

        sorting_was = False
        try:
            sorting_was = self.preview_table.isSortingEnabled()
        except Exception:
            sorting_was = False
        self._set_preview_sorting(False)
        self.preview_table.setUpdatesEnabled(False)
        try:
            for row in sorted(selected_rows, reverse=True):
                if 0 <= int(row) < self.preview_table.rowCount():
                    self.preview_table.removeRow(int(row))
        finally:
            self.preview_table.setUpdatesEnabled(True)
            self._set_preview_sorting(sorting_was)
            try:
                self.preview_table.viewport().update()
            except Exception:
                pass

        self._rebuild_preview_row_index()
        if self.preview_table.rowCount() <= 0:
            self._preview_ready = False
            self._preview_signature = None
            self.start_btn.setEnabled(False)
        self._update_clear_skipped_button_state()
        log_message(
            self.sync_log,
            self._t(
                "ui.sync.preview.selected_removed",
                "Removed selected rows: {count}",
            ).format(count=len(selected_rows)),
            debug,
        )

    def _normalized_target_categories(self, categories: list[str], family: str, lang: str) -> list[str]:
        try:
            return _prepare_target_categories(categories, family, lang)
        except Exception:
            return [c for c in categories if c]

    def _prefill_preview_rows(self, *, target_categories: list[str], target_lang: str, family: str):
        sorting_was = False
        try:
            sorting_was = self.preview_table.isSortingEnabled()
        except Exception:
            sorting_was = False
        self._set_preview_sorting(False)
        self.preview_table.setUpdatesEnabled(False)
        try:
            self.preview_table.setRowCount(len(target_categories))
            self._preview_row_index = {}

            class _SiteStub:
                def __init__(self, code: str, fam: str):
                    self.code = code
                    self.family = type("Fam", (), {"name": fam})()

                def hostname(self):
                    fam = str(self.family.name or "").strip().lower()
                    if fam == "wikipedia":
                        return f"{self.code}.wikipedia.org"
                    if fam == "commons":
                        return "commons.wikimedia.org"
                    if fam == "wikidata":
                        return "www.wikidata.org"
                    return f"{self.code}.{fam}.org"

            site_stub = _SiteStub(target_lang, family)
            for row, target_title in enumerate(target_categories):
                row_key = str(target_title).casefold()
                self._preview_row_index[row_key] = row
                target_url = ""
                try:
                    target_url = _build_page_url(site_stub, target_title)
                except Exception:
                    target_url = ""

                self._set_target_item(
                    row,
                    target_title,
                    url=target_url,
                    row_key=row_key,
                )
                self.preview_table.setItem(
                    row, 1, self._make_table_item("…", align=Qt.AlignHCenter | Qt.AlignVCenter)
                )
                self.preview_table.setItem(row, 2, self._make_table_item("…"))
                self.preview_table.setItem(
                    row, 3, self._make_table_item("—", align=Qt.AlignHCenter | Qt.AlignVCenter)
                )
                self.preview_table.setItem(
                    row, 4, self._make_table_item("0", align=Qt.AlignHCenter | Qt.AlignVCenter)
                )
                self._set_row_status(row, "previewing")
        finally:
            self.preview_table.setUpdatesEnabled(True)
            self._set_preview_sorting(sorting_was)
            self._rebuild_preview_row_index()
            self._update_clear_skipped_button_state()
            try:
                self.preview_table.viewport().update()
            except Exception:
                pass

    def _ensure_preview_row(self, row_data: dict) -> Optional[int]:
        row_key = str(row_data.get("row_key") or "").strip().casefold()
        if row_key:
            mapped_row = self._preview_row_index.get(row_key)
            if mapped_row is not None and 0 <= int(mapped_row) < self.preview_table.rowCount():
                if self._row_key_for_row(int(mapped_row)) == row_key:
                    return int(mapped_row)
            self._rebuild_preview_row_index()
            mapped_row = self._preview_row_index.get(row_key)
            if mapped_row is not None and 0 <= int(mapped_row) < self.preview_table.rowCount():
                return int(mapped_row)

        target_title = str(row_data.get("target_title") or "").strip()
        if target_title:
            sorting_was = False
            try:
                sorting_was = self.preview_table.isSortingEnabled()
            except Exception:
                sorting_was = False
            self._set_preview_sorting(False)
            row = self.preview_table.rowCount()
            self.preview_table.insertRow(row)
            key = row_key or target_title.casefold()
            self._preview_row_index[key] = row
            target_url = str(row_data.get("target_url") or "")
            self._set_target_item(row, target_title, url=target_url, row_key=key)
            self.preview_table.setItem(row, 1, self._make_table_item("…", align=Qt.AlignHCenter | Qt.AlignVCenter))
            self.preview_table.setItem(row, 2, self._make_table_item("…"))
            self.preview_table.setItem(row, 3, self._make_table_item("—", align=Qt.AlignHCenter | Qt.AlignVCenter))
            self.preview_table.setItem(row, 4, self._make_table_item("0", align=Qt.AlignHCenter | Qt.AlignVCenter))
            self._set_row_status(row, "previewing")
            self._set_preview_sorting(sorting_was)
            self._rebuild_preview_row_index()
            self._update_clear_skipped_button_state()
            return self._preview_row_index.get(key, row)
        return None

    def _append_preview_row(self, row_data: dict):
        if not isinstance(row_data, dict):
            return
        if self._preview_cancel_requested:
            return
        row = self._ensure_preview_row(row_data)
        if row is None:
            return

        phase = str(row_data.get("phase") or "").strip().lower()
        if phase == "stub":
            target_title = str(row_data.get("target_title") or "")
            target_url = str(row_data.get("target_url") or "")
            self._set_target_item(
                row,
                target_title,
                url=target_url,
                row_key=str(row_data.get("row_key") or "").strip().casefold(),
            )
            return

        qid = str(row_data.get("qid") or "")
        qid_url = str(row_data.get("qid_url") or "")
        source_title = str(row_data.get("source_title") or "")
        source_url = str(row_data.get("source_url") or "")
        count_text = str(row_data.get("count_text") or "0")

        qid_text = qid or self._t("ui.sync.preview.no_wikidata", "—")
        qid_item = self._make_table_item(
            qid_text,
            url=qid_url,
            align=Qt.AlignHCenter | Qt.AlignVCenter,
        )
        if not qid_url:
            try:
                qid_item.setForeground(self._muted_color())
            except Exception:
                pass
        self.preview_table.setItem(row, 1, qid_item)

        source_text = source_title or self._t(
            "ui.sync.preview.source_not_found", "Not found")
        source_item = self._make_table_item(source_text, url=source_url)
        if not source_url:
            try:
                source_item.setForeground(self._muted_color())
            except Exception:
                pass
        self.preview_table.setItem(row, 2, source_item)

        count_item = self._make_table_item(
            count_text,
            align=Qt.AlignHCenter | Qt.AlignVCenter,
        )
        self.preview_table.setItem(row, 3, count_item)
        self._set_row_status(row, "preview_ready" if source_title else "skipped")
        self._update_clear_skipped_button_state()

    def _append_preview_rows_batch(self, rows_data: object):
        if self._preview_cancel_requested:
            return
        if isinstance(rows_data, dict):
            rows = [rows_data]
        elif isinstance(rows_data, list):
            rows = [row for row in rows_data if isinstance(row, dict)]
        else:
            return
        if not rows:
            return

        sorting_was = False
        try:
            sorting_was = self.preview_table.isSortingEnabled()
        except Exception:
            sorting_was = False
        self._set_preview_sorting(False)
        self.preview_table.setUpdatesEnabled(False)
        try:
            for row_data in rows:
                self._append_preview_row(row_data)
        finally:
            self.preview_table.setUpdatesEnabled(True)
            self._set_preview_sorting(sorting_was)
            self._rebuild_preview_row_index()
            self._update_clear_skipped_button_state()
            try:
                self.preview_table.viewport().update()
            except Exception:
                pass

    def _apply_sync_category_result(self, row_data: dict):
        if not isinstance(row_data, dict):
            return
        row_key = str(row_data.get("row_key") or "").strip().casefold()
        row = self._ensure_preview_row(row_data)
        if row is None:
            return
        transferred = int(row_data.get("transferred") or 0)
        status = str(row_data.get("status") or "in_progress")
        if self._sync_cancel_requested and status in {
            "in_progress",
            "done",
            "done_empty",
            "partial",
            "error",
        }:
            status = "stopped"
        total_text = str(row_data.get("total_text") or "").strip()
        if total_text and total_text not in {"…", "..."}:
            self.preview_table.setItem(
                row,
                3,
                self._make_table_item(total_text, align=Qt.AlignHCenter | Qt.AlignVCenter),
            )
            if row_key and row_key == self._current_progress_row_key:
                self._current_progress_total = self._parse_total_value(total_text)
        self.preview_table.setItem(
            row,
            4,
            self._make_table_item(str(transferred), align=Qt.AlignHCenter | Qt.AlignVCenter),
        )
        self._set_row_status(row, status)
        if row_key and row_key == self._current_progress_row_key and status in {
            "done",
            "done_empty",
            "partial",
        }:
            if self._current_progress_total is not None and self._current_progress_total > 0:
                self._current_progress_done = max(
                    self._current_progress_done, self._current_progress_total
                )
        self._render_current_category_progress()
        self._update_clear_skipped_button_state()

    def _mark_rows_stopped_if_in_progress(self):
        for row in range(self.preview_table.rowCount()):
            status_item = self.preview_table.item(row, 5)
            if status_item is None:
                continue
            try:
                code = str(status_item.data(self._status_code_role()) or "").strip().lower()
            except Exception:
                code = ""
            raw = (status_item.text() if status_item else "").lower()
            if code in {"previewing", "in_progress"} or "⏳" in raw:
                self._set_row_status(row, "stopped")
            elif self._sync_cancel_requested and code in {
                "done",
                "done_empty",
                "partial",
                "error",
            }:
                row_key = self._row_key_for_row(row)
                if row_key and row_key == self._current_progress_row_key:
                    self._set_row_status(row, "stopped")
        self._update_clear_skipped_button_state()

    def _open_preview_cell_link(self, row: int, col: int):
        try:
            mods = QGuiApplication.keyboardModifiers()
        except Exception:
            mods = Qt.NoModifier
        if mods & (Qt.ControlModifier | Qt.ShiftModifier):
            return
        item = self.preview_table.item(row, col)
        if item is None:
            return
        try:
            url = str(item.data(Qt.UserRole) or "").strip()
        except Exception:
            url = ""
        if not url:
            return
        QDesktopServices.openUrl(QUrl(url))

    def _init_progress(self, total: int):
        init_progress(
            self.progress_label,
            self.progress_bar,
            total,
            processed_label=self._processed_label(),
        )

    def _inc_progress(self):
        if self._sync_cancel_requested:
            return
        inc_progress(
            self.progress_label,
            self.progress_bar,
            processed_label=self._processed_label(),
        )

    def _parse_total_value(self, raw_text: str) -> Optional[int]:
        text = str(raw_text or "").strip()
        if not text or text in {"—", "-", "…", "..."}:
            return None
        if "/" in text:
            parts = [part.strip() for part in text.split("/", 1)]
            nums: list[int] = []
            for part in parts:
                try:
                    nums.append(int(part))
                except Exception:
                    return None
            if not nums:
                return None
            return max(0, sum(nums))
        try:
            return max(0, int(text))
        except Exception:
            return None

    def _resolve_row_total(self, row_key: str) -> Optional[int]:
        key = str(row_key or "").strip().casefold()
        if not key:
            return None
        row = self._preview_row_index.get(key)
        if row is None or not (0 <= int(row) < self.preview_table.rowCount()):
            self._rebuild_preview_row_index()
            row = self._preview_row_index.get(key)
        if row is None or not (0 <= int(row) < self.preview_table.rowCount()):
            return None
        item = self.preview_table.item(int(row), 3)
        if item is None:
            return None
        return self._parse_total_value(item.text())

    def _render_current_category_progress(self):
        bar = getattr(self, "current_progress_bar", None)
        if bar is None:
            return
        title = str(self._current_progress_title or "").strip()
        if not self._current_progress_row_key:
            try:
                bar.setToolTip("")
                bar.setMinimum(0)
                bar.setMaximum(1)
                bar.setValue(0)
                bar.setFormat(
                    self._t("ui.sync.current_progress_idle", "Current: 0/0 · 0%")
                )
            except Exception:
                pass
            return
        done = max(0, int(self._current_progress_done or 0))
        total = self._current_progress_total
        if total is None:
            try:
                visible_max = max(1, done if done > 0 else 1)
                bar.setToolTip(title)
                bar.setMinimum(0)
                bar.setMaximum(visible_max)
                bar.setValue(min(done, visible_max))
                bar.setFormat(
                    self._t(
                        "ui.sync.current_progress_unknown",
                        "Current: {done}/? · ?%",
                    ).format(done=done)
                )
            except Exception:
                pass
            return
        if total <= 0:
            try:
                bar.setToolTip(title)
                bar.setMinimum(0)
                bar.setMaximum(1)
                bar.setValue(0)
                bar.setFormat(self._t("ui.sync.current_progress_idle", "Current: 0/0 · 0%"))
            except Exception:
                pass
            return
        safe_done = min(done, total)
        pct = int((safe_done * 100) / total) if total > 0 else 0
        try:
            bar.setToolTip(title)
            bar.setMinimum(0)
            bar.setMaximum(total)
            bar.setValue(safe_done)
            bar.setFormat(
                self._t(
                    "ui.sync.current_progress_fmt",
                    "Current: {done}/{total} · {percent}%",
                ).format(done=safe_done, total=total, percent=pct)
            )
        except Exception:
            pass

    def _reset_current_category_progress(self):
        self._current_progress_row_key = ""
        self._current_progress_title = ""
        self._current_progress_done = 0
        self._current_progress_total = 0
        self._render_current_category_progress()

    def _on_member_progress(self, payload: dict):
        if self._sync_cancel_requested:
            return
        if not isinstance(payload, dict):
            return
        row_key = str(payload.get("row_key") or "").strip().casefold()
        if not row_key:
            return
        processed = max(0, int(payload.get("processed") or 0))
        raw_total = payload.get("total")
        payload_total: Optional[int] = None
        try:
            if raw_total is not None and str(raw_total).strip() != "":
                payload_total = max(0, int(raw_total))
        except Exception:
            payload_total = None
        target_title = str(payload.get("target_title") or "").strip()

        if row_key != self._current_progress_row_key:
            self._current_progress_row_key = row_key
            self._current_progress_title = target_title
            self._current_progress_done = 0
            self._current_progress_total = (
                payload_total if payload_total is not None else self._resolve_row_total(row_key)
            )

        self._current_progress_done = processed
        if payload_total is not None:
            self._current_progress_total = payload_total
        elif self._current_progress_total is None:
            self._current_progress_total = self._resolve_row_total(row_key)
        self._render_current_category_progress()

    def _on_preview_category_done(self):
        if self._preview_cancel_requested:
            return
        self._inc_progress()

    def get_current_language(self) -> str:
        try:
            lang = getattr(self.parent_window, "current_lang", None)
            if lang:
                return str(lang).strip()
        except Exception:
            pass
        try:
            auth = getattr(self.parent_window, "auth_tab", None)
            if auth and hasattr(auth, "lang_combo") and auth.lang_combo:
                return (auth.lang_combo.currentText() or "ru").strip()
        except Exception:
            pass
        return "ru"

    def get_current_family(self) -> str:
        try:
            fam = getattr(self.parent_window, "current_family", None)
            if fam:
                return str(fam).strip()
        except Exception:
            pass
        try:
            auth = getattr(self.parent_window, "auth_tab", None)
            if auth and hasattr(auth, "family_combo") and auth.family_combo:
                return (auth.family_combo.currentText() or "wikipedia").strip()
        except Exception:
            pass
        return "wikipedia"

    def _load_categories_from_inputs(self) -> list[str]:
        titles = []
        try:
            titles = self.source_panel.load_titles_from_inputs()
        except Exception:
            titles = []
        if not titles:
            raw = (self.cat_edit.text() or "").strip()
            if raw:
                titles = [raw]
        cleaned = [title.strip() for title in titles if title and title.strip()]
        unique: list[str] = []
        seen: set[str] = set()
        for title in cleaned:
            key = title.casefold()
            if key in seen:
                continue
            seen.add(key)
            unique.append(title)
        return unique

    def _depth_base_signature(
        self,
        categories: list[str],
        source_lang: str,
        target_lang: str,
        family: str,
        transfer_depth: int,
    ):
        items = tuple(
            str(title or "").strip().casefold()
            for title in categories
            if str(title or "").strip()
        )
        return (
            items,
            str(source_lang or "").strip().lower(),
            str(target_lang or "").strip().lower(),
            str(family or "").strip().lower(),
            max(0, int(transfer_depth or 0)),
        )

    def _get_cached_depth_expanded_categories(
        self,
        *,
        categories: list[str],
        source_lang: str,
        target_lang: str,
        family: str,
        transfer_depth: int,
    ) -> Optional[list[str]]:
        signature = self._depth_base_signature(
            categories, source_lang, target_lang, family, transfer_depth
        )
        if self._depth_expanded_base_signature != signature:
            return None
        return list(self._depth_expanded_categories or [])

    def _set_cached_depth_expanded_categories(
        self,
        *,
        base_categories: list[str],
        expanded_categories: list[str],
        source_lang: str,
        target_lang: str,
        family: str,
        transfer_depth: int,
    ) -> None:
        self._depth_expanded_base_signature = self._depth_base_signature(
            base_categories, source_lang, target_lang, family, transfer_depth
        )
        self._depth_expanded_categories = list(expanded_categories or [])

    def _resolve_effective_categories(
        self,
        *,
        base_categories: list[str],
        source_lang: str,
        target_lang: str,
        family: str,
        transfer_depth: int,
    ) -> list[str]:
        cached = self._get_cached_depth_expanded_categories(
            categories=base_categories,
            source_lang=source_lang,
            target_lang=target_lang,
            family=family,
            transfer_depth=transfer_depth,
        )
        if cached is not None:
            return list(cached)
        return list(base_categories)

    def _apply_expanded_categories_to_manual_list(self, categories: list[str]) -> None:
        try:
            in_path = (self.source_panel.in_path.text() or "").strip()
        except Exception:
            in_path = ""
        if in_path:
            return

        new_text = "\n".join(categories or [])
        try:
            current_text = self.source_panel.manual_list.toPlainText()
        except Exception:
            current_text = ""
        if str(current_text or "") == str(new_text or ""):
            return

        self._suspend_preview_invalidation = True
        try:
            self.source_panel.manual_list.setPlainText(new_text)
        finally:
            self._suspend_preview_invalidation = False

    def _expand_categories_for_preview(
        self,
        *,
        categories: list[str],
        source_lang: str,
        target_lang: str,
        family: str,
        transfer_depth: int,
    ) -> list[str]:
        depth = max(0, int(transfer_depth or 0))
        normalized_categories = self._normalized_target_categories(
            categories, family, target_lang
        )
        if not normalized_categories:
            return []

        cached = self._get_cached_depth_expanded_categories(
            categories=normalized_categories,
            source_lang=source_lang,
            target_lang=target_lang,
            family=family,
            transfer_depth=depth,
        )
        if cached is not None:
            return cached

        expanded = list(normalized_categories)
        existing_keys: set[str] = {cat.casefold() for cat in expanded}

        # Удерживаем только актуальные авто-добавленные строки для текущего списка.
        self._auto_depth_origin_map = {
            key: origin
            for key, origin in self._auto_depth_origin_map.items()
            if key in existing_keys and origin in existing_keys
        }

        if depth <= 0:
            self._auto_depth_origin_map = {}
            self._set_cached_depth_expanded_categories(
                base_categories=normalized_categories,
                expanded_categories=expanded,
                source_lang=source_lang,
                target_lang=target_lang,
                family=family,
                transfer_depth=depth,
            )
            return expanded

        roots = [
            title for title in expanded if title.casefold() not in self._auto_depth_origin_map
        ]
        if not roots and self._auto_depth_origin_map:
            key_to_title = {title.casefold(): title for title in expanded}
            seen_root_keys: set[str] = set()
            for title in expanded:
                origin_key = str(
                    self._auto_depth_origin_map.get(title.casefold()) or ""
                ).strip().casefold()
                if not origin_key or origin_key in seen_root_keys:
                    continue
                root_title = key_to_title.get(origin_key)
                if root_title:
                    roots.append(root_title)
                    seen_root_keys.add(origin_key)
        if not roots:
            roots = list(expanded)

        log_message(
            self.sync_log,
            self._t(
                "ui.sync.depth_expand.started",
                "[Depth] Reading subcategories: depth={depth}, source categories={count}",
            ).format(depth=depth, count=len(roots)),
            debug,
        )

        api_client = WikimediaAPIClient()
        added_total = 0
        for root_title in roots:
            root_key = root_title.casefold()
            found_subcats: list[str] = []
            try:
                found_subcats = self.source_panel._fetch_subcats_recursive(
                    api_client,
                    root_title,
                    target_lang,
                    family,
                    depth - 1,
                    0,
                    set(),
                )
            except Exception as exc:
                log_message(
                    self.sync_log,
                    self._t(
                        "ui.sync.depth_expand.error",
                        "[Depth] Failed to read subcategories for '{category}': {error}",
                    ).format(category=root_title, error=exc),
                    debug,
                )
                continue

            normalized_found = self._normalized_target_categories(
                found_subcats, family, target_lang
            )
            added_for_root = 0
            for subcat_title in normalized_found:
                subcat_key = subcat_title.casefold()
                if subcat_key in existing_keys:
                    continue
                existing_keys.add(subcat_key)
                expanded.append(subcat_title)
                self._auto_depth_origin_map[subcat_key] = root_key
                added_for_root += 1
            added_total += added_for_root
            log_message(
                self.sync_log,
                self._t(
                    "ui.sync.depth_expand.progress",
                    "[Depth] {category}: added {count}",
                ).format(category=root_title, count=added_for_root),
                debug,
            )

        self._set_cached_depth_expanded_categories(
            base_categories=normalized_categories,
            expanded_categories=expanded,
            source_lang=source_lang,
            target_lang=target_lang,
            family=family,
            transfer_depth=depth,
        )
        if added_total > 0:
            self._apply_expanded_categories_to_manual_list(expanded)
            log_message(
                self.sync_log,
                self._t(
                    "ui.sync.depth_expand.done",
                    "[Depth] Categories appended to the end of the list: {count}",
                ).format(count=added_total),
                debug,
            )
        else:
            log_message(
                self.sync_log,
                self._t(
                    "ui.sync.depth_expand.empty",
                    "[Depth] No new subcategories found.",
                ),
                debug,
            )
        return expanded

    def _signature_from_values(
        self,
        categories: list[str],
        source_lang: str,
        target_lang: str,
        family: str,
        process_articles: bool,
        process_subcategories: bool,
        transfer_depth: int,
    ):
        items = tuple(
            title.strip() for title in categories if title is not None and title.strip()
        )
        return (
            items,
            (source_lang or "").strip().lower(),
            (target_lang or "").strip().lower(),
            (family or "").strip().lower(),
            bool(process_articles),
            bool(process_subcategories),
            max(0, int(transfer_depth or 0)),
        )

    def _current_signature(self):
        source_lang = (self.source_lang_edit.text() or "").strip().lower()
        target_lang = (self.get_current_language() or "").strip().lower()
        family = (self.get_current_family() or "wikipedia").strip().lower()
        base_categories = self._normalized_target_categories(
            self._load_categories_from_inputs(), family, target_lang
        )
        transfer_depth = max(0, int(self.article_depth_spin.value()))
        categories = self._resolve_effective_categories(
            base_categories=base_categories,
            source_lang=source_lang,
            target_lang=target_lang,
            family=family,
            transfer_depth=transfer_depth,
        )
        process_articles = self.articles_checkbox.isChecked()
        process_subcategories = self.subcategories_checkbox.isChecked()
        return self._signature_from_values(
            categories,
            source_lang,
            target_lang,
            family,
            process_articles,
            process_subcategories,
            transfer_depth,
        )

    def _is_preview_signature_actual(self) -> bool:
        return bool(
            self._preview_ready
            and self._preview_signature is not None
            and self._preview_signature == self._current_signature()
        )

    def _refresh_start_button_state(self):
        btn = getattr(self, "start_btn", None)
        if btn is None:
            return
        allow = bool(self._preview_mode and self._is_preview_signature_actual())
        try:
            if self.preview_worker and self.preview_worker.isRunning():
                allow = False
            if self.sync_worker and self.sync_worker.isRunning():
                allow = False
        except Exception:
            pass
        try:
            btn.setEnabled(bool(allow))
        except Exception:
            pass

    def _collect_run_config(self) -> Optional[dict]:
        categories = self._load_categories_from_inputs()
        source_lang = (self.source_lang_edit.text() or "").strip().lower()
        target_lang = (self.get_current_language() or "").strip().lower()
        family = (self.get_current_family() or "wikipedia").strip().lower()
        process_articles = self.articles_checkbox.isChecked()
        process_subcategories = self.subcategories_checkbox.isChecked()
        transfer_depth = max(0, int(self.article_depth_spin.value()))

        if not categories:
            QMessageBox.warning(
                self,
                self._t("ui.error", "Error"),
                self._t("ui.sync.error.no_categories", "Target category list is empty."),
            )
            return None
        if not source_lang or not target_lang:
            QMessageBox.warning(
                self,
                self._t("ui.error", "Error"),
                self._t(
                    "ui.sync.error.no_languages",
                    "Specify both source and target language.",
                ),
            )
            return None
        if source_lang == target_lang:
            QMessageBox.warning(
                self,
                self._t("ui.error", "Error"),
                self._t(
                    "ui.sync.error.same_languages",
                    "Source and target languages are the same: {lang}.",
                ).format(lang=source_lang),
            )
            return None
        if not process_articles and not process_subcategories:
            QMessageBox.warning(
                self,
                self._t("ui.error", "Error"),
                self._t(
                    "ui.sync.error.no_mode",
                    "Select at least one mode: articles or subcategories.",
                ),
            )
            return None

        base_categories = self._normalized_target_categories(
            categories, family, target_lang
        )
        if not base_categories:
            QMessageBox.warning(
                self,
                self._t("ui.error", "Error"),
                self._t("ui.sync.error.no_categories", "Target category list is empty."),
            )
            return None

        effective_categories = self._resolve_effective_categories(
            base_categories=base_categories,
            source_lang=source_lang,
            target_lang=target_lang,
            family=family,
            transfer_depth=transfer_depth,
        )

        return {
            "categories": effective_categories,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "family": family,
            "process_articles": process_articles,
            "process_subcategories": process_subcategories,
            "transfer_depth": transfer_depth,
            "edit_summary": (self.summary_edit.text() or "").strip(),
        }

    def _confirm_depth_warning(self, depth: int) -> bool:
        if depth <= 1:
            return True
        text = self._t(
            "ui.sync.depth_confirm",
            "Depth {depth} is selected. This can affect many pages.",
        ).format(depth=depth)
        tail = self._t(
            "ui.sync.depth_confirm_continue",
            "Confirm that you want to continue.",
        )
        answer = QMessageBox.question(
            self,
            self._t("ui.warning", "Warning"),
            f"{text}\n\n{tail}",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    def preview_or_toggle_settings(self):
        if self.sync_worker and self.sync_worker.isRunning():
            return
        if self.preview_worker and self.preview_worker.isRunning():
            if self._preview_cancel_requested and self._preview_mode:
                self._set_preview_mode(False)
            return
        if self._preview_mode:
            self._set_preview_mode(False)
            return
        if self._is_preview_signature_actual():
            self._set_preview_mode(True)
            return
        self.start_preview()

    def start_preview(self):
        if self.preview_worker and self.preview_worker.isRunning():
            return
        if self.sync_worker and self.sync_worker.isRunning():
            return

        config = self._collect_run_config()
        if not config:
            return
        if not self._confirm_depth_warning(config["transfer_depth"]):
            return

        expanded_categories = self._expand_categories_for_preview(
            categories=config["categories"],
            source_lang=config["source_lang"],
            target_lang=config["target_lang"],
            family=config["family"],
            transfer_depth=config["transfer_depth"],
        )
        if not expanded_categories:
            QMessageBox.warning(
                self,
                self._t("ui.error", "Error"),
                self._t("ui.sync.error.no_categories", "Target category list is empty."),
            )
            return
        config["categories"] = expanded_categories

        self._preview_ready = False
        self._preview_cancel_requested = False
        self._preview_signature = None
        self._running_preview_signature = self._signature_from_values(
            config["categories"],
            config["source_lang"],
            config["target_lang"],
            config["family"],
            config["process_articles"],
            config["process_subcategories"],
            config["transfer_depth"],
        )

        self._prefill_preview_rows(
            target_categories=config["categories"],
            target_lang=config["target_lang"],
            family=config["family"],
        )
        self._set_preview_mode(True)
        self._init_progress(len(config["categories"]))
        self.start_btn.setEnabled(False)
        self.preview_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._update_clear_skipped_button_state()
        log_message(
            self.sync_log,
            self._t("ui.sync.preview.started", "Starting preview..."),
            debug,
        )

        self.preview_worker = CategoryContentSyncPreviewWorker(
            target_categories=config["categories"],
            source_lang=config["source_lang"],
            target_lang=config["target_lang"],
            family=config["family"],
            process_articles=config["process_articles"],
            process_subcategories=config["process_subcategories"],
            article_depth=0,
        )
        self.preview_worker.progress.connect(
            lambda message: log_message(self.sync_log, message, debug)
        )
        self.preview_worker.rows_ready.connect(self._append_preview_rows_batch)
        self.preview_worker.category_done.connect(self._on_preview_category_done)
        self.preview_worker.finished.connect(self._on_preview_finished)
        self.preview_worker.start()

    def start_sync(self):
        if self.preview_worker and self.preview_worker.isRunning():
            return
        if self.sync_worker and self.sync_worker.isRunning():
            return

        if not self._is_preview_signature_actual():
            QMessageBox.warning(
                self,
                self._t("ui.warning", "Warning"),
                self._t(
                    "ui.sync.preview.required",
                    "Run preview first with the current settings.",
                ),
            )
            self.start_btn.setEnabled(False)
            return

        config = self._collect_run_config()
        if not config:
            return

        categories = self._categories_from_preview_table()
        if not categories:
            QMessageBox.warning(
                self,
                self._t("ui.error", "Error"),
                self._t("ui.sync.error.no_categories", "Target category list is empty."),
            )
            self.start_btn.setEnabled(False)
            return

        if not self.parent_window:
            QMessageBox.warning(
                self,
                self._t("ui.error", "Error"),
                self._t("ui.no_access_auth_data", "No access to authorization data."),
            )
            return

        user = getattr(self.parent_window, "current_user", None)
        password = getattr(self.parent_window, "current_password", None)
        if not user or not password:
            QMessageBox.warning(
                self,
                self._t("ui.error", "Error"),
                self._t("ui.must_log_in", "You need to sign in."),
            )
            return

        config["categories"] = categories
        apply_pwb_config(config["target_lang"], config["family"])

        self._sync_cancel_requested = False
        self._rebuild_preview_row_index()
        self._init_progress(len(categories))
        self._reset_current_category_progress()
        self.preview_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._update_clear_skipped_button_state()
        log_message(
            self.sync_log,
            self._t("ui.sync.started", "Starting sync..."),
            debug,
        )

        self.sync_worker = CategoryContentSyncWorker(
            target_categories=config["categories"],
            username=str(user or ""),
            password=str(password or ""),
            source_lang=config["source_lang"],
            target_lang=config["target_lang"],
            family=config["family"],
            process_articles=config["process_articles"],
            process_subcategories=config["process_subcategories"],
            article_depth=0,
            edit_summary=config["edit_summary"],
        )
        self.sync_worker.progress.connect(
            lambda message: log_message(self.sync_log, message, debug)
        )
        self.sync_worker.category_done.connect(self._inc_progress)
        self.sync_worker.category_result.connect(self._apply_sync_category_result)
        self.sync_worker.member_progress.connect(self._on_member_progress)
        self.sync_worker.finished.connect(self._on_sync_finished)
        self.sync_worker.start()

    def _on_preview_finished(self):
        worker = self.preview_worker
        stopped = bool(worker and getattr(worker, "_stop", False))
        self.preview_worker = None
        was_cancel_requested = self._preview_cancel_requested
        self._preview_cancel_requested = False

        self._rebuild_preview_row_index()
        rows_count = self.preview_table.rowCount()
        self.preview_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

        if stopped or was_cancel_requested:
            self._preview_ready = False
            self._preview_signature = None
            self.start_btn.setEnabled(False)
            self._mark_rows_stopped_if_in_progress()
            log_message(
                self.sync_log,
                self._t("ui.sync.preview.stopped", "Preview stopped."),
                debug,
            )
            if rows_count <= 0:
                self._set_preview_mode(False)
            self._update_clear_skipped_button_state()
            return

        if rows_count <= 0:
            self._preview_ready = False
            self._preview_signature = None
            self.start_btn.setEnabled(False)
            self._set_preview_mode(False)
            self._update_clear_skipped_button_state()
            log_message(
                self.sync_log,
                self._t(
                    "ui.sync.preview.empty",
                    "No preview matches were found.",
                ),
                debug,
            )
            return

        self._preview_ready = True
        self._preview_signature = self._running_preview_signature
        self._set_preview_mode(True)
        for row in range(self.preview_table.rowCount()):
            status_item = self.preview_table.item(row, 5)
            if status_item is None:
                continue
            try:
                code = str(status_item.data(self._status_code_role()) or "").strip().lower()
            except Exception:
                code = ""
            raw = (status_item.text() if status_item else "").lower()
            if code in {"previewing", "in_progress"} or "⏳" in raw:
                self._set_row_status(row, "preview_ready")
        self._update_clear_skipped_button_state()
        log_message(
            self.sync_log,
            self._t("ui.sync.preview.ready", "Preview is ready. Check the table."),
            debug,
        )

    def _on_sync_finished(self):
        worker = self.sync_worker
        stopped = bool(worker and getattr(worker, "_stop", False))
        if self._sync_cancel_requested:
            stopped = True
        try:
            worker_saved_total = int(getattr(worker, "saved_edits", 0) or 0)
        except Exception:
            worker_saved_total = 0
        try:
            worker_success_total = int(
                getattr(worker, "transferred_success_total", 0) or 0
            )
        except Exception:
            worker_success_total = 0
        table_total = self._calculate_transferred_total()
        transferred_total = max(
            0,
            int(worker_saved_total or 0),
            int(worker_success_total or 0),
            int(table_total or 0),
        )
        self.sync_worker = None
        self.stop_btn.setEnabled(False)
        self.preview_btn.setEnabled(True)
        self._refresh_start_button_state()
        self._update_clear_skipped_button_state()

        try:
            if transferred_total > 0 and self.parent_window and hasattr(self.parent_window, "record_operation"):
                self.parent_window.record_operation("sync", transferred_total)
        except Exception:
            pass

        if stopped:
            self._mark_rows_stopped_if_in_progress()
            self._init_progress(0)
            self._reset_current_category_progress()
            log_message(self.sync_log, self._t("ui.stopped", "Stopped!"), debug)
        else:
            log_message(
                self.sync_log,
                self._t("ui.done_with_exclamation", "Done!"),
                debug,
            )
        self._sync_cancel_requested = False

    def _calculate_transferred_total(self) -> int:
        total = 0
        try:
            rows = int(self.preview_table.rowCount())
        except Exception:
            rows = 0
        for row in range(rows):
            try:
                item = self.preview_table.item(row, 4)
                if item is None:
                    continue
                value = int(str(item.text() or "0").strip())
                total += max(0, value)
            except Exception:
                continue
        return total

    def stop_sync(self):
        if self.preview_worker and self.preview_worker.isRunning():
            self._preview_cancel_requested = True
            self._preview_ready = False
            self._preview_signature = None
            self.preview_worker.request_stop()
            self.preview_btn.setEnabled(True)
            self.start_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)
            self._set_preview_mode(True)
            self._mark_rows_stopped_if_in_progress()
            self._update_clear_skipped_button_state()
            log_message(
                self.sync_log,
                self._t("ui.sync.preview.stopping", "Stopping preview..."),
                debug,
            )
            return

        if self.sync_worker and self.sync_worker.isRunning():
            self._sync_cancel_requested = True
            self.sync_worker.request_stop()
            self.stop_btn.setEnabled(False)
            self._init_progress(0)
            self._reset_current_category_progress()
            self._update_clear_skipped_button_state()
            log_message(self.sync_log, self._t("ui.stopping", "Stopping..."), debug)

    def set_prefix_controls_visible(self, visible: bool):
        try:
            self.source_panel.set_prefix_controls_visible(visible)
        except Exception:
            pass

    def update_language(self, lang: str):
        self.current_lang = lang
        self._refresh_target_lang_label()
        self._invalidate_preview_state()
        family = self.get_current_family()
        try:
            self.update_namespace_combo(family, lang)
        except Exception:
            pass

    def update_family(self, family: str):
        self.current_family = family
        self._invalidate_preview_state()
        lang = self.get_current_language()
        try:
            self.update_namespace_combo(family, lang)
        except Exception:
            pass

    def set_auth_data(self, username: str, lang: str, family: str):
        self.current_user = username
        self.current_lang = lang
        self.current_family = family
        self._refresh_target_lang_label()
        self._invalidate_preview_state()

    def clear_auth_data(self):
        self.current_user = None
        self.current_lang = None
        self.current_family = None
        self._refresh_target_lang_label()
        self._invalidate_preview_state()

    def showEvent(self, event):
        try:
            super().showEvent(event)
        finally:
            current_ui_lang = self._ui_lang()
            current_theme = self._theme_mode()
            if current_theme != self._last_theme_mode:
                self._last_theme_mode = current_theme
                self._apply_preview_table_theme()
            if current_ui_lang != self._last_ui_lang:
                self._last_ui_lang = current_ui_lang
                self._update_preview_headers()
                self._refresh_target_lang_label()
                self._set_preview_mode(self._preview_mode)
                self._sync_preview_button_width()
                self._sync_action_buttons_compact()
                try:
                    self.clear_skipped_btn.setText(
                        self._t("ui.sync.clear_skipped", "Clear skipped")
                    )
                except Exception:
                    pass

    def changeEvent(self, event):
        try:
            super().changeEvent(event)
        finally:
            try:
                evt_type = event.type() if event is not None else None
            except Exception:
                evt_type = None
            if evt_type in (QEvent.StyleChange, QEvent.PaletteChange, QEvent.ApplicationPaletteChange):
                current_theme = self._theme_mode()
                if current_theme != self._last_theme_mode:
                    self._last_theme_mode = current_theme
                    self._apply_preview_table_theme()
                self._sync_preview_button_width()
                self._sync_action_buttons_compact()
