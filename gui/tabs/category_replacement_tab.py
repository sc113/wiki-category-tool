"""Batch replacement of explicit categories on selected pages."""

import csv

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QProgressBar, QPushButton, QSizePolicy, QTextEdit, QVBoxLayout, QWidget,
)

from ...core.category_replacement import read_category_first_column, read_replacement_map
from ...core.localization import translate_key
from ...core.pywikibot_config import apply_pwb_config
from ...utils import debug
from ...workers.category_replacement_worker import CategoryReplacementWorker
from ..widgets.category_stage import CategoryLogPanel, CategoryWorkspace
from ..widgets.category_creation_table import TransferActionsLayout, TransferToolbarLayout
from ..widgets.shared_panels import CategorySourcePanel, FileInputRow, TsvPreviewPanel
from ..widgets.ui_helpers import add_info_button, inc_progress, init_progress, log_message


class CategoryReplacementTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent
        self.worker = None
        self._rules = None
        self._fetching = False
        self._build_ui()
        self.refresh_localized_texts()
        self._update_actions()

    def _t(self, key, fallback=''):
        return translate_key(key, getattr(self.parent_window, '_ui_lang', 'ru'), fallback)

    def _build_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.source_panel = CategorySourcePanel(
            self, parent_window=self.parent_window,
            group_title=self._t('ui.pages_source'),
            help_text=self._t('help.cleanup.source'),
            compact=True,
        )
        self.source_panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.source_panel.setMinimumWidth(260)
        layout.addWidget(self.source_panel, 1)

        work = QWidget()
        main = QVBoxLayout(work)
        main.setContentsMargins(6, 0, 0, 0)
        main.setSpacing(8)
        layout.addWidget(work, 2)

        self.run_log = QTextEdit()
        self.run_log.setReadOnly(True)
        self.run_log.setProperty('_wct_skip_log_translation', True)
        self.log_panel = CategoryLogPanel(self.run_log, lambda key: self._t(f'ui.transfer.{key}', key))
        toolbar = TransferToolbarLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(6)
        self.fetch_map_btn = QPushButton()
        self.fetch_map_btn.setObjectName('sourceActionButton')
        self.fetch_map_btn.clicked.connect(self.fetch_map_pages)
        toolbar.addWidget(self.fetch_map_btn)
        toolbar.addWidget(self.log_panel.toggle)
        main.addLayout(toolbar)

        self.map_path_edit = QLineEdit('category-replacements.tsv')
        self.map_path_edit.textChanged.connect(self._clear_preview)
        self.map_file_row = FileInputRow(self, '', self.map_path_edit, editor_menu=True)
        file_layout = QHBoxLayout()
        file_layout.addWidget(self.map_file_row, 1)
        self.map_help_btn = add_info_button(self, file_layout,
                                           lambda: self._t('help.cat_replace.map'), inline=True)
        self.preview_panel = TsvPreviewPanel(self, header_text=self._t('ui.cat_replace.map_title'))
        self.preview_panel.add_top_layout(file_layout)
        workspace = CategoryWorkspace(self.preview_panel, minimum_log_width=160)
        workspace.set_log_panel(self.log_panel)
        main.addWidget(workspace, 1)

        self.summary_group = QGroupBox()
        options_layout = QVBoxLayout(self.summary_group)
        self.sort_key_check = QCheckBox()
        options_layout.addWidget(self.sort_key_check)
        reason_row = QHBoxLayout()
        self.reason_label = QLabel()
        self.reason_edit = QLineEdit()
        self.reason_edit.setProperty('_wct_skip_text_translation', True)
        self.reason_edit.setAlignment(Qt.AlignLeft)
        self.reason_label.setBuddy(self.reason_edit)
        reason_row.addWidget(self.reason_label)
        reason_row.addWidget(self.reason_edit, 1)
        options_layout.addLayout(reason_row)
        main.addWidget(self.summary_group)

        controls = TransferActionsLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)
        self.progress_bar = QProgressBar()
        init_progress(None, self.progress_bar, 0)
        main.addWidget(self.progress_bar)
        self.preview_btn = QPushButton()
        self.preview_btn.setObjectName('sourceActionButton')
        self.preview_btn.clicked.connect(self.preview_map)
        controls.addWidget(self.preview_btn)
        self.start_btn = QPushButton()
        self.start_btn.setObjectName('sourceActionButton')
        self.start_btn.clicked.connect(self.start_run)
        controls.addWidget(self.start_btn)
        self.stop_btn = QPushButton()
        self.stop_btn.setObjectName('sourceActionButton')
        self.stop_btn.clicked.connect(self.stop_run)
        controls.addWidget(self.stop_btn)
        main.addLayout(controls)

        self.source_panel.set_log_widget(self.run_log)
        self.source_panel.set_fetch_progress_widgets(bar_widget=self.progress_bar)
        self.source_panel.fetch_started.connect(self._on_fetch_started)
        self.source_panel.fetch_finished.connect(self._on_fetch_finished)

    def refresh_localized_texts(self):
        self.source_panel.refresh_localized_texts()
        self.map_file_row.refresh_localized_texts()
        self.log_panel.refresh_localized_texts()
        self.preview_panel.findChild(QGroupBox, 'previewGroup').setTitle(self._t('ui.cat_replace.map_title'))
        self.map_file_row.caption.setText(self._t('ui.cat_replace.map_file'))
        for label, key in ((self.preview_panel.left_header_label, 'ui.cat_replace.old_category'),
                           (self.preview_panel.right_header_label, 'ui.cat_replace.new_categories')):
            label.setText(self._t(key))
            label.show()
        if self.source_panel._fetch_active_button is not self.fetch_map_btn:
            self.fetch_map_btn.setText(self._t('ui.source.fetch_map'))
            self.fetch_map_btn.setToolTip(self._t('ui.source.fetch_map_tip'))
        self.summary_group.setTitle(self._t('ui.cleanup.comments_group'))
        self.reason_label.setText(self._t('ui.cat_replace.reason'))
        self.reason_edit.setPlaceholderText(self._t('ui.cat_replace.reason_placeholder'))
        self.sort_key_check.setText(self._t('ui.cat_replace.inherit_sort_key'))
        self.sort_key_check.setToolTip(self._t('ui.cat_replace.inherit_sort_key_tip'))
        self.preview_btn.setText(self._t('ui.preview'))
        self.start_btn.setText(self._t('ui.cat_replace.start'))
        self.stop_btn.setText(self._t('ui.stop'))

    def set_prefix_controls_visible(self, visible):
        self.source_panel.set_prefix_controls_visible(visible)

    def _update_actions(self):
        busy = self.worker is not None
        self.source_panel.setEnabled(not busy)
        self.map_file_row.setEnabled(not busy and not self._fetching)
        self.preview_btn.setEnabled(not busy and not self._fetching)
        self.start_btn.setEnabled(not busy and not self._fetching and self._rules is not None)
        self.stop_btn.setEnabled(busy)
        self.summary_group.setEnabled(not busy)
        if self.source_panel._fetch_active_button is not self.fetch_map_btn:
            self.fetch_map_btn.setEnabled(not busy and not self._fetching)

    def _clear_preview(self):
        self._rules = None
        self.preview_panel.clear()
        self._update_actions()

    def _read_rules(self):
        try:
            path = self.map_path_edit.text().strip()
            if not path:
                raise ValueError(self._t('ui.cat_replace.choose_map'))
            return read_replacement_map(path)
        except (OSError, ValueError, csv.Error) as exc:
            self._warn(self._t('ui.cleanup.failed_read_tsv').format(error=exc))
            return None

    def preview_map(self):
        self._clear_preview()
        rules = self._read_rules()
        if rules:
            self._rules = rules
            self.preview_panel.set_preview(
                [source for source, _ in rules],
                ['\t'.join(target.display() for target in targets) for _, targets in rules],
            )
            self._update_actions()

    def fetch_map_pages(self):
        if self.source_panel._is_fetch_stop_button(self.fetch_map_btn):
            self.source_panel.stop_fetch_titles()
        elif not self._fetching and self.worker is None:
            try:
                categories = read_category_first_column(self.map_path_edit.text().strip())
                if not categories:
                    raise ValueError(self._t('ui.source.empty_first_column'))
            except (OSError, ValueError, csv.Error) as exc:
                self._warn(self._t('ui.cleanup.failed_read_tsv').format(error=exc))
                return
            self.source_panel.fetch_map_titles(categories, self.fetch_map_btn)

    def _on_fetch_started(self):
        self._fetching = True
        self.log_panel.set_expanded(True)
        self._update_actions()

    def _on_fetch_finished(self):
        self._fetching = False
        self.refresh_localized_texts()
        self._update_actions()

    def _warn(self, text):
        QMessageBox.warning(self, self._t('ui.error'), text)

    def _log(self, message):
        log_message(self.run_log, message, debug)

    def _page_done(self):
        inc_progress(None, self.progress_bar)

    def start_run(self):
        if self.worker is not None or self._fetching or self._rules is None:
            return
        try:
            titles = self.source_panel.load_titles_from_inputs()
        except (OSError, ValueError) as exc:
            self._warn(str(exc))
            return
        if not titles:
            self._warn(self._t('ui.cleanup.no_input_pages'))
            return
        username = getattr(self.parent_window, 'current_user', None)
        password = getattr(self.parent_window, 'current_password', None)
        if not username or not password:
            self._warn(self._t('ui.you_need_to_sign_in'))
            return
        reason = self.reason_edit.text().strip()
        if len(reason) > 496:
            self._warn(self._t('ui.cat_replace.reason_too_long'))
            return
        lang, family = self.source_panel.get_current_language(), self.source_panel.get_current_family()
        apply_pwb_config(lang, family)
        self.run_log.clear()
        self.log_panel.set_expanded(True)
        init_progress(None, self.progress_bar, len(titles))
        self.worker = CategoryReplacementWorker(
            titles=titles, rules=self._rules, username=username, password=password,
            lang=lang, family=family, ns_selection=self.source_panel.ns_combo.currentData(),
            reason=reason, inherit_sort_key=self.sort_key_check.isChecked(),
        )
        self.worker.progress.connect(self._log)
        self.worker.page_done.connect(self._page_done)
        self.worker.finished.connect(self._on_run_finished)
        self._update_actions()
        self.worker.start()

    def stop_run(self):
        if self.worker is not None:
            self.worker.request_stop()
            self.stop_btn.setEnabled(False)

    def _on_run_finished(self):
        worker, self.worker = self.worker, None
        if worker.saved_edits:
            self.parent_window.record_operation('category_replace', worker.saved_edits)
        if worker.failed:
            self._log(self._t('ui.operation_failed').format(error=worker.failure_message))
        elif worker._stop:
            self._log(self._t('ui.stopped'))
        elif worker.error_count:
            self._log(self._t('ui.completed_with_errors').format(count=worker.error_count))
        else:
            self._log(self._t('ui.done_with_exclamation'))
        worker.deleteLater()
        self._update_actions()
