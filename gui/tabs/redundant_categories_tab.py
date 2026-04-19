# -*- coding: utf-8 -*-
"""
Вкладка удаления избыточных категорий.
"""

from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QToolButton, QTextEdit, QGroupBox, QProgressBar, QMessageBox,
    QSizePolicy
)

from ...core.pywikibot_config import apply_pwb_config
from ...core.localization import translate_key
from ...core.redundant_category_logic import (
    REDUNDANT_MODE_DEDUP,
    detect_redundant_category_mode,
)
from ...utils import (
    debug,
    default_redundant_category_summary,
    default_redundant_category_multi_summary,
    default_redundant_category_pair_format,
    resolve_project_language,
)
from ...workers.redundant_category_worker import RedundantCategoryWorker
from ..widgets.shared_panels import CategorySourcePanel, TsvPreviewPanel
from ..widgets.ui_helpers import (
    add_info_button, pick_file, open_from_edit, create_log_wrap,
    make_clear_button, tsv_preview_from_path, init_progress, inc_progress,
    log_message, set_start_stop_ratio, is_default_summary, check_tsv_format
)


class RedundantCategoriesTab(QWidget):
    """Вкладка для удаления широких категорий при наличии точных."""

    language_changed = Signal(str)
    family_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent
        self.rcworker = None
        self.current_user = None
        self.current_lang = None
        self.current_family = None
        self._preview_ready = False
        self.setup_ui()

    def _ui_lang(self) -> str:
        try:
            raw = str(getattr(self.parent_window, '_ui_lang', 'ru')).lower()
        except Exception:
            raw = 'ru'
        return 'en' if raw.startswith('en') else 'ru'

    def _t(self, key: str, fallback: str) -> str:
        try:
            return translate_key(key, self._ui_lang(), fallback)
        except Exception:
            return fallback

    def _project_lang(self) -> str:
        return resolve_project_language(self.parent_window, 'ru')

    def setup_ui(self):
        main_layout = QHBoxLayout(self)
        try:
            main_layout.setContentsMargins(0, 0, 0, 0)
            main_layout.setSpacing(8)
        except Exception:
            pass

        source_help = self._t('help.cleanup.source', '')
        pairs_help = self._t('help.cleanup.pairs', '')
        summary_help = self._t('help.cleanup.summary', '')

        self.source_panel = CategorySourcePanel(
            self,
            parent_window=self.parent_window,
            help_text=source_help,
            group_title=self._t('ui.pages_source', 'Pages source'),
            category_section_label=self._t(
                'ui.cleanup.fetch_category_content',
                '<b>Fetch category contents</b>',
            ),
            category_placeholder=self._t(
                'ui.root_category_name',
                'Root category name',
            ),
            manual_label=self._t(
                'ui.cleanup.list_of_pages_to_process',
                '<b>List of pages to process</b>',
            ),
            manual_placeholder=self._t(
                'ui.page_list_one_per_line',
                'Page list (one per line)',
            ),
            file_section_label=self._t(
                'ui.cleanup.or_load_list_from_file',
                '<b>Or load list from file</b>',
            ),
            file_caption=self._t('ui.file_txt', 'File (.txt):'),
        )
        self.redundant_ns_combo = self.source_panel.ns_combo
        self.manual_list = self.source_panel.manual_list
        self.list_edit = self.source_panel.list_edit
        self.in_path = self.source_panel.in_path
        self.file_edit = self.source_panel.file_edit
        try:
            # Блок 1: сжимается вместе с блоком 2 раньше лога.
            self.source_panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        except Exception:
            pass
        main_layout.addWidget(self.source_panel, 3)

        right_side = QWidget()
        right_layout = QVBoxLayout(right_side)
        try:
            right_layout.setContentsMargins(6, 0, 0, 0)
            right_layout.setSpacing(8)
        except Exception:
            pass

        content_row = QHBoxLayout()
        try:
            content_row.setContentsMargins(0, 0, 0, 0)
            content_row.setSpacing(8)
        except Exception:
            pass

        preview_wrap = QWidget()
        preview_layout = QVBoxLayout(preview_wrap)
        try:
            preview_layout.setContentsMargins(0, 0, 6, 0)
            preview_layout.setSpacing(4)
        except Exception:
            pass
        try:
            # Блок 2: сжимается вместе с блоком 1 раньше лога.
            preview_wrap.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        except Exception:
            pass

        self.preview_panel = TsvPreviewPanel(
            self,
            header_text=f"<b>{self._t('ui.cleanup.preview_for_categories', 'Category preview')}</b>",
            left_header=self._t('ui.precise_category', 'Precise category'),
            right_header=self._t('ui.broad_category', 'Broad category'),
            left_stretch=1,
            right_stretch=1,
        )
        pairs_block = QVBoxLayout()
        try:
            pairs_block.setContentsMargins(0, 0, 0, 0)
            pairs_block.setSpacing(4)
        except Exception:
            pass
        pairs_block.addWidget(QLabel(self._t('ui.category_pairs_tsv', 'Category pairs (.tsv):')))
        pairs_row = QHBoxLayout()
        try:
            pairs_row.setContentsMargins(0, 0, 0, 0)
            pairs_row.setSpacing(6)
        except Exception:
            pass

        self.categories_path_edit = QLineEdit('categories.tsv')
        self.categories_path_edit.setMinimumWidth(0)
        self.categories_path_edit.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.categories_path_edit.textChanged.connect(self._on_pairs_path_changed)
        pairs_row.addWidget(self.categories_path_edit, 1)

        btn_browse_pairs = QToolButton()
        btn_browse_pairs.setText('…')
        btn_browse_pairs.setAutoRaise(False)
        try:
            btn_browse_pairs.setFixedSize(27, 27)
            btn_browse_pairs.setToolTip(self._t('ui.choose_file', 'Choose file'))
        except Exception:
            pass
        btn_browse_pairs.clicked.connect(
            lambda: pick_file(self, self.categories_path_edit, '*.tsv'))
        pairs_row.addWidget(btn_browse_pairs)

        btn_open_pairs = QPushButton(self._t('ui.open', 'Open'))
        btn_open_pairs.clicked.connect(
            lambda: open_from_edit(self, self.categories_path_edit)
        )
        pairs_row.addWidget(btn_open_pairs)
        add_info_button(self, pairs_row, pairs_help, inline=True)
        pairs_block.addLayout(pairs_row)
        self.preview_panel.add_top_layout(pairs_block)
        self.preview_titles = self.preview_panel.titles_edit
        self.preview_rest = self.preview_panel.content_edit
        preview_layout.addWidget(self.preview_panel, 1)
        content_row.addWidget(preview_wrap, 6)

        log_wrap = QWidget()
        log_layout = QVBoxLayout(log_wrap)
        try:
            log_layout.setContentsMargins(6, 0, 0, 0)
        except Exception:
            pass
        try:
            # Блок 3 (лог) держим читаемым дольше остальных.
            log_wrap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            log_wrap.setMinimumWidth(200)
        except Exception:
            pass
        self.run_log = QTextEdit()
        self.run_log.setReadOnly(True)
        mono_font = QFont('Consolas', 9)
        if not mono_font.exactMatch():
            mono_font = QFont('Courier New', 9)
        mono_font.setFixedPitch(True)
        self.run_log.setFont(mono_font)

        wrapped_log = create_log_wrap(self, self.run_log, with_header=True)

        def _clear_all():
            self._reset_preview_state(clear_preview=True, clear_log=True)

        try:
            btn_extra = make_clear_button(self, _clear_all)
            grid = wrapped_log.layout()
            if grid is not None:
                grid.addWidget(
                    btn_extra,
                    grid.rowCount() - 1,
                    0,
                    Qt.AlignBottom | Qt.AlignRight,
                )
        except Exception:
            pass

        log_layout.addWidget(wrapped_log, 1)
        content_row.addWidget(log_wrap, 5)
        try:
            content_row.setStretch(0, 6)
            content_row.setStretch(1, 5)
        except Exception:
            pass
        try:
            content_row.setStretch(0, 1)
            content_row.setStretch(1, 1)
        except Exception:
            pass

        right_layout.addLayout(content_row, 1)

        comments_group = QGroupBox(self._t('ui.cleanup.comments_group', 'Edit summaries'))
        comments_layout = QVBoxLayout(comments_group)
        try:
            comments_layout.setContentsMargins(8, 12, 8, 8)
            comments_layout.setSpacing(6)
        except Exception:
            pass

        single_row = QHBoxLayout()
        single_row.addWidget(QLabel(self._t('ui.comment_for_single_removal', 'Comment for single removal:')))
        self.redundant_summary_single_edit = QLineEdit()
        self.redundant_summary_single_edit.setText(
            default_redundant_category_summary(self._project_lang()))
        self._configure_static_left_text(self.redundant_summary_single_edit)
        single_row.addWidget(self.redundant_summary_single_edit, 1)
        add_info_button(self, single_row, summary_help, inline=True)
        comments_layout.addLayout(single_row)

        multi_row = QHBoxLayout()
        multi_row.addWidget(
            QLabel(self._t('ui.comment_for_multiple_removals', 'Comment for multiple removals:'))
        )
        self.redundant_summary_multi_edit = QLineEdit()
        self.redundant_summary_multi_edit.setText(
            default_redundant_category_multi_summary(self._project_lang()))
        self._configure_static_left_text(self.redundant_summary_multi_edit)
        multi_row.addWidget(self.redundant_summary_multi_edit, 2)

        pair_label = QLabel(self._t('ui.pair', 'Pair:'))
        multi_row.addWidget(pair_label)
        self.redundant_pair_format_edit = QLineEdit()
        self.redundant_pair_format_edit.setText(
            default_redundant_category_pair_format(self._project_lang()))
        self._configure_static_left_text(self.redundant_pair_format_edit)
        try:
            self.redundant_pair_format_edit.setMaximumWidth(320)
        except Exception:
            pass
        multi_row.addWidget(self.redundant_pair_format_edit, 1)
        comments_layout.addLayout(multi_row)

        right_layout.addWidget(comments_group)

        controls_row = QHBoxLayout()
        self.progress_label = QLabel(self._t('ui.processed_counter_initial', 'Processed 0/0'))
        try:
            self.progress_label.setVisible(False)
        except Exception:
            pass
        self.progress_bar = QProgressBar()
        try:
            self.progress_bar.setMaximum(1)
            self.progress_bar.setValue(0)
            self.progress_bar.setTextVisible(True)
            self.progress_bar.setFormat(self._t('ui.processed_counter_initial', 'Processed 0/0'))
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
        controls_row.addWidget(progress_wrap, 1)

        self.preview_btn = QPushButton(self._t('ui.preview', 'Preview'))
        self.preview_btn.clicked.connect(self.preview_pairs)
        controls_row.addWidget(self.preview_btn)

        self.start_btn = QPushButton(self._t('ui.cleanup.start_removal', 'Start removal'))
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self.start_run)
        controls_row.addWidget(self.start_btn)

        self.stop_btn = QPushButton(self._t('ui.stop', 'Stop'))
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_run)
        controls_row.addWidget(self.stop_btn)
        set_start_stop_ratio(self.start_btn, self.stop_btn, 3)

        right_layout.addLayout(controls_row)

        main_layout.addWidget(right_side, 7)
        try:
            # На среднем размере три верхних блока близки по ширине.
            main_layout.setStretch(0, 1)
            main_layout.setStretch(1, 2)
        except Exception:
            pass
        self.source_panel.set_log_widget(self.run_log)
        self.source_panel.set_fetch_progress_widgets(self.progress_label, self.progress_bar)
        self._set_action_mode_preview()

    def _set_action_mode_preview(self):
        self._preview_ready = False
        try:
            self.start_btn.setEnabled(False)
        except Exception:
            pass

    def _set_action_mode_run(self):
        self._preview_ready = True
        try:
            worker = getattr(self, 'rcworker', None)
            running = bool(worker and worker.isRunning())
        except Exception:
            running = False
        try:
            self.start_btn.setEnabled(not running)
        except Exception:
            pass

    def _on_pairs_path_changed(self):
        worker = getattr(self, 'rcworker', None)
        if worker and worker.isRunning():
            return
        self._reset_preview_state(clear_preview=True, clear_log=False)

    def _reset_preview_state(self, *, clear_preview: bool, clear_log: bool):
        if clear_preview:
            try:
                self.preview_panel.clear()
            except Exception:
                pass
        if clear_log:
            try:
                self.run_log.clear()
            except Exception:
                pass
        init_progress(self.progress_label, self.progress_bar, 0)
        self.stop_btn.setEnabled(False)
        self._set_action_mode_preview()

    def preview_pairs(self):
        worker = getattr(self, 'rcworker', None)
        if worker and worker.isRunning():
            return
        self._set_action_mode_preview()

        path = (self.categories_path_edit.text() or '').strip()
        if not path:
            QMessageBox.warning(
                self,
                self._t('ui.error', 'Error'),
                self._t('ui.cleanup.specify_tsv_categories', 'Specify a TSV file with category pairs.'),
            )
            return

        ok, message = check_tsv_format(path, allow_single_column=True, widget=self)
        if not ok:
            QMessageBox.warning(self, self._t('ui.error', 'Error'), message)
            return

        try:
            left, right, count = tsv_preview_from_path(path)
        except Exception as exc:
            QMessageBox.critical(
                self,
                self._t('ui.error', 'Error'),
                self._t('ui.cleanup.failed_read_tsv', 'Failed to read TSV: {error}').format(error=exc),
            )
            return

        self.preview_panel.set_preview(left, right)
        if count > 0:
            self._set_action_mode_run()
        else:
            self._set_action_mode_preview()

    def start_run(self):
        if not self._preview_ready:
            QMessageBox.warning(
                self,
                self._t('ui.error', 'Error'),
                self._t('ui.cleanup.preview_required', 'Run preview first.'),
            )
            return

        path = (self.categories_path_edit.text() or '').strip()
        if not path:
            QMessageBox.warning(
                self,
                self._t('ui.error', 'Error'),
                self._t('ui.cleanup.specify_tsv_categories', 'Specify a TSV file with category pairs.'),
            )
            return

        ok, message = check_tsv_format(path, allow_single_column=True, widget=self)
        if not ok:
            QMessageBox.warning(self, self._t('ui.error', 'Error'), message)
            return

        try:
            titles = self.source_panel.load_titles_from_inputs()
        except Exception as exc:
            QMessageBox.critical(self, self._t('ui.error', 'Error'), str(exc))
            return

        if not titles:
            QMessageBox.warning(
                self,
                self._t('ui.error', 'Error'),
                self._t(
                    'ui.cleanup.no_input_pages',
                    'Neither page list file nor manual page list is provided.',
                ),
            )
            return

        user = getattr(self.parent_window, 'current_user', None)
        pwd = getattr(self.parent_window, 'current_password', None)
        lang = getattr(self.parent_window, 'current_lang', 'ru')
        fam = getattr(self.parent_window, 'current_family', 'wikipedia')

        if not user or not pwd:
            QMessageBox.warning(
                self,
                self._t('ui.error', 'Error'),
                self._t('ui.you_need_to_sign_in', 'You need to sign in.'),
            )
            return

        try:
            detected_mode = detect_redundant_category_mode(path, fam, lang)
        except Exception as exc:
            QMessageBox.warning(
                self,
                self._t('ui.error', 'Error'),
                self._t(
                    'ui.cleanup.detect_mode_error',
                    'Failed to detect TSV processing mode: {error}',
                ).format(error=exc),
            )
            return

        apply_pwb_config(lang, fam)

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.run_log.clear()
        init_progress(self.progress_label, self.progress_bar, len(titles))
        mode_name = 'dedupe' if detected_mode == REDUNDANT_MODE_DEDUP else 'pairs'
        log_message(
            self.run_log,
            self._t(
                'ui.cleanup.run_started',
                'Starting redundant category cleanup: pages={pages}, lang={lang}, family={family}, ns={ns}, mode={mode}',
            ).format(
                pages=len(titles),
                lang=lang,
                family=fam,
                ns=self.redundant_ns_combo.currentData(),
                mode=mode_name,
            ),
            debug,
        )
        if detected_mode == REDUNDANT_MODE_DEDUP:
            info_text = self._t(
                'ui.cleanup.dedupe_mode_info',
                'Category dedupe mode is enabled.\nTSV contains one column: only duplicate occurrences of listed categories will be removed, first occurrence is kept.',
            )
            QMessageBox.information(
                self,
                self._t('ui.cleanup.dedupe_mode_title', 'Dedupe mode'),
                info_text,
            )
            log_message(
                self.run_log,
                self._t(
                    'ui.cleanup.dedupe_mode_log',
                    'Dedupe mode: removing only duplicate occurrences of categories from the first TSV column.',
                ),
                debug,
            )

        self.rcworker = RedundantCategoryWorker(
            titles=titles,
            categories_path=path,
            username=user,
            password=pwd,
            lang=lang,
            family=fam,
            ns_selection=self.redundant_ns_combo.currentData(),
            single_template=self.redundant_summary_single_edit.text().strip(),
            multi_template=self.redundant_summary_multi_edit.text().strip(),
            pair_template=self.redundant_pair_format_edit.text().strip(),
        )
        self.rcworker.progress.connect(
            lambda message: log_message(self.run_log, message, debug)
        )
        self.rcworker.page_done.connect(
            lambda: inc_progress(self.progress_label, self.progress_bar)
        )
        self.rcworker.finished.connect(self._on_run_finished)
        self.rcworker.start()

    def stop_run(self):
        worker = getattr(self, 'rcworker', None)
        if worker and worker.isRunning():
            worker.request_stop()

    def _on_run_finished(self):
        self.stop_btn.setEnabled(False)
        if self._preview_ready:
            self._set_action_mode_run()
        else:
            self._set_action_mode_preview()
        worker = getattr(self, 'rcworker', None)
        stopped = bool(worker and getattr(worker, '_stop', False))
        try:
            edits = int(getattr(worker, 'saved_edits', 0) or 0)
        except Exception:
            edits = 0
        try:
            if edits > 0 and self.parent_window and hasattr(self.parent_window, 'record_operation'):
                self.parent_window.record_operation('cleanup', edits)
        except Exception:
            pass
        if stopped:
            log_message(self.run_log, self._t('ui.stopped', 'Stopped!'), debug)
        else:
            log_message(self.run_log, self._t('ui.done_with_exclamation', 'Done!'), debug)

    def update_language(self, lang: str):
        if is_default_summary(
            self.redundant_summary_single_edit.text(),
            default_redundant_category_summary,
        ):
            self.redundant_summary_single_edit.setText(
                default_redundant_category_summary(lang))
            self._pin_line_edit_left(self.redundant_summary_single_edit)
        if is_default_summary(
            self.redundant_summary_multi_edit.text(),
            default_redundant_category_multi_summary,
        ):
            self.redundant_summary_multi_edit.setText(
                default_redundant_category_multi_summary(lang))
            self._pin_line_edit_left(self.redundant_summary_multi_edit)
        if is_default_summary(
            self.redundant_pair_format_edit.text(),
            default_redundant_category_pair_format,
        ):
            self.redundant_pair_format_edit.setText(
                default_redundant_category_pair_format(lang))
            self._pin_line_edit_left(self.redundant_pair_format_edit)

        if self.parent_window:
            family = getattr(self.parent_window, 'current_family', None) or (
                getattr(getattr(self.parent_window, 'auth_tab', None),
                        'family_combo', None).currentText()
                if getattr(getattr(self.parent_window, 'auth_tab', None), 'family_combo', None) else 'wikipedia'
            )
            try:
                nm = getattr(self.parent_window, 'namespace_manager', None)
                if nm:
                    nm.populate_ns_combo(self.redundant_ns_combo, family, lang)
            except Exception:
                pass

    def _configure_static_left_text(self, line_edit: QLineEdit):
        try:
            line_edit.setAlignment(Qt.AlignLeft)
        except Exception:
            pass
        try:
            line_edit.textChanged.connect(
                lambda _txt='', edit=line_edit: self._pin_line_edit_left(edit))
        except Exception:
            pass
        try:
            line_edit.editingFinished.connect(
                lambda edit=line_edit: self._pin_line_edit_left(edit))
        except Exception:
            pass
        self._pin_line_edit_left(line_edit)

    @staticmethod
    def _pin_line_edit_left(line_edit: QLineEdit):
        try:
            if not line_edit.hasFocus():
                line_edit.setCursorPosition(0)
                line_edit.deselect()
        except Exception:
            pass

    def update_family(self, family: str):
        if self.parent_window:
            lang = getattr(self.parent_window, 'current_lang', None) or (
                getattr(getattr(self.parent_window, 'auth_tab', None),
                        'lang_combo', None).currentText()
                if getattr(getattr(self.parent_window, 'auth_tab', None), 'lang_combo', None) else 'ru'
            )
            try:
                nm = getattr(self.parent_window, 'namespace_manager', None)
                if nm:
                    nm.populate_ns_combo(self.redundant_ns_combo, family, lang)
            except Exception:
                pass

    def update_namespace_combo(self, family: str, lang: str):
        self.source_panel.update_namespace_combo(family, lang)

    def set_prefix_controls_visible(self, visible: bool):
        try:
            self.source_panel.set_prefix_controls_visible(visible)
        except Exception:
            pass

    def set_auth_data(self, username: str, lang: str, family: str):
        self.current_user = username
        self.current_lang = lang
        self.current_family = family

    def clear_auth_data(self):
        self.current_user = None
        self.current_lang = None
        self.current_family = None

    def update_summary(self, lang: str):
        self.update_language(lang)
