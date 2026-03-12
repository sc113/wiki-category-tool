# -*- coding: utf-8 -*-
"""
Вкладка чтения для Wiki Category Tool.
"""

import os

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QToolButton, QTextEdit, QProgressBar,
    QGroupBox, QMessageBox
)

from ...utils import debug
from ...core.localization import translate_key
from ...workers.parse_worker import ParseWorker
from ..widgets.shared_panels import CategorySourcePanel
from ..widgets.ui_helpers import (
    add_info_button, pick_save, open_from_edit, log_message,
    set_start_stop_ratio, create_log_wrap
)


class ParseTab(QWidget):
    """Вкладка для чтения содержимого страниц."""

    language_changed = Signal(str)
    family_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent
        self.worker = None
        self.current_user = None
        self.current_lang = None
        self.current_family = None
        self.setup_ui()

    def _ui_lang(self) -> str:
        return getattr(self.parent_window, '_ui_lang', 'ru') if self.parent_window is not None else 'ru'

    def _t(self, key: str) -> str:
        return translate_key(key, self._ui_lang(), '')

    def _fmt(self, key: str, **kwargs) -> str:
        text = self._t(key)
        try:
            return text.format(**kwargs)
        except Exception:
            return text

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        try:
            main_layout.setContentsMargins(0, 0, 0, 0)
            main_layout.setSpacing(6)
        except Exception:
            pass

        parse_help_left = self._t('help.parse.source')
        parse_help_right = self._t('help.parse.output')

        h_main = QHBoxLayout()
        try:
            h_main.setSpacing(8)
        except Exception:
            pass

        self.source_panel = CategorySourcePanel(
            self,
            parent_window=self.parent_window,
            help_text=parse_help_left,
            group_title=self._t('ui.source'),
            manual_label=f"<b>{self._t('ui.list_of_categories_to_read')}</b>",
        )
        self.ns_combo_parse = self.source_panel.ns_combo
        self.cat_edit = self.source_panel.cat_edit
        self.fetch_mode_combo = self.source_panel.fetch_mode_combo
        self.replace_list_btn = self.source_panel.replace_list_btn
        self.append_list_btn = self.source_panel.append_list_btn
        self.open_petscan_btn = self.source_panel.open_petscan_btn
        self.depth_spin = self.source_panel.depth_spin
        self.manual_list = self.source_panel.manual_list
        self.list_edit = self.source_panel.list_edit
        self.in_path = self.source_panel.in_path
        self.file_edit = self.source_panel.file_edit

        right_group = QGroupBox(self._t('ui.settings_output'))
        right_layout = QVBoxLayout(right_group)
        try:
            right_layout.setContentsMargins(8, 12, 8, 8)
            right_layout.setSpacing(8)
        except Exception:
            pass

        save_layout = QHBoxLayout()
        save_layout.addWidget(QLabel(self._t('ui.save_to')))

        self.out_path = QLineEdit('categories.tsv')
        self.out_path.setMinimumWidth(0)
        save_layout.addWidget(self.out_path, 1)

        btn_browse_out = QToolButton()
        btn_browse_out.setText('…')
        btn_browse_out.setAutoRaise(False)
        try:
            btn_browse_out.setFixedSize(27, 27)
            btn_browse_out.setToolTip(self._t('ui.select_save_path'))
        except Exception:
            pass
        btn_browse_out.clicked.connect(
            lambda: pick_save(self, self.out_path, '.tsv'))
        save_layout.addWidget(btn_browse_out)

        btn_open_out = QPushButton(self._t('ui.open'))
        btn_open_out.clicked.connect(lambda: open_from_edit(self, self.out_path))
        save_layout.addWidget(btn_open_out)
        save_layout.addStretch()
        add_info_button(self, save_layout, parse_help_right, inline=True)
        right_layout.addLayout(save_layout)

        right_layout.addSpacing(6)
        self.parse_log = QTextEdit()
        self.parse_log.setReadOnly(True)
        self.parse_log.setMinimumHeight(220)
        self.parse_log.setProperty('_wct_skip_log_translation', True)

        mono_font = QFont('Consolas', 9)
        if not mono_font.exactMatch():
            mono_font = QFont('Courier New', 9)
        mono_font.setFixedPitch(True)
        self.parse_log.setFont(mono_font)

        parse_log_wrap = create_log_wrap(
            self, self.parse_log, with_header=True)
        right_layout.addWidget(parse_log_wrap, 1)

        self.parse_bar = QProgressBar()
        self.parse_bar.setMaximum(1)
        self.parse_bar.setValue(0)
        try:
            self.parse_bar.setTextVisible(True)
            self.parse_bar.setFormat(
                self._t('ui.processed_counter_initial')
            )
        except Exception:
            pass
        right_layout.addWidget(self.parse_bar)

        control_layout = QHBoxLayout()
        control_layout.addStretch()
        self.parse_btn = QPushButton(self._t('ui.start_reading'))
        self.parse_btn.clicked.connect(self.start_parse)
        control_layout.addWidget(self.parse_btn)
        self.parse_stop_btn = QPushButton(self._t('ui.stop'))
        self.parse_stop_btn.setEnabled(False)
        self.parse_stop_btn.clicked.connect(self.stop_parse)
        control_layout.addWidget(self.parse_stop_btn)
        control_layout.addStretch()
        right_layout.addLayout(control_layout)
        set_start_stop_ratio(self.parse_btn, self.parse_stop_btn, 3)

        self.source_panel.set_log_widget(self.parse_log)

        h_main.addWidget(self.source_panel, 1)
        h_main.addWidget(right_group, 1)
        main_layout.addLayout(h_main)

    def update_namespace_combo(self, family: str, lang: str):
        self.source_panel.update_namespace_combo(family, lang)

    def set_prefix_controls_visible(self, visible: bool):
        try:
            self.source_panel.set_prefix_controls_visible(visible)
        except Exception:
            pass

    def get_current_language(self) -> str:
        return self.source_panel.get_current_language()

    def get_current_family(self) -> str:
        return self.source_panel.get_current_family()

    def _processed_label(self) -> str:
        return self._t('ui.processed_short')

    def open_petscan_in_browser(self):
        self.source_panel.open_petscan_in_browser()

    def open_petscan(self):
        self.source_panel.open_petscan()

    def fetch_category_pages(self):
        self.source_panel.fetch_category_pages()

    def start_parse(self):
        if not self.out_path.text():
            QMessageBox.warning(self, self._t('ui.error'), self._t('ui.specify_output_file'))
            return

        try:
            titles = self.source_panel.load_titles_from_inputs()
        except Exception as exc:
            QMessageBox.critical(self, self._t('ui.error'), str(exc))
            return

        if not titles:
            QMessageBox.warning(
                self, self._t('ui.error'), self._t('ui.neither_input_file_nor_text_list_is_specified'))
            return

        lang = self.get_current_language()
        fam = self.get_current_family()

        self.parse_bar.setMaximum(len(titles))
        self.parse_bar.setValue(0)
        try:
            self.parse_bar.setFormat(f'{self._processed_label()} 0/{len(titles)}')
        except Exception:
            pass
        self.parse_btn.setEnabled(False)

        ns_sel = self.ns_combo_parse.currentData()
        log_message(
            self.parse_log,
            self._fmt('log.parse.run_started', pages=len(titles), lang=lang, family=fam, ns=ns_sel),
            debug,
        )
        self.worker = ParseWorker(
            titles, self.out_path.text(), ns_sel, lang, fam)
        self.worker.progress.connect(
            lambda message: [self._inc_parse_prog(), log_message(self.parse_log, message, debug)])
        self.worker.finished.connect(self._on_parse_finished)

        try:
            self.parse_stop_btn.clicked.disconnect()
        except Exception:
            pass
        self.parse_stop_btn.setText(self._t('ui.stop'))
        self.parse_stop_btn.clicked.connect(self.stop_parse)
        self.parse_stop_btn.setEnabled(True)
        self.worker.start()

    def stop_parse(self):
        worker = getattr(self, 'worker', None)
        if worker and worker.isRunning():
            worker.request_stop()
            try:
                self.parse_stop_btn.setEnabled(False)
                log_message(self.parse_log, self._t('ui.stopping'), debug)
            except Exception:
                pass

    def _on_parse_finished(self):
        try:
            self.parse_btn.setEnabled(True)
            if getattr(self, 'worker', None) and getattr(self.worker, '_stop', False):
                self.parse_stop_btn.setText(self._t('ui.stop'))
                self.parse_stop_btn.setEnabled(False)
                message = self._t('ui.stopped')
            else:
                self.parse_stop_btn.setText(self._t('ui.open'))
                self.parse_stop_btn.setEnabled(True)
                try:
                    if self.parent_window and hasattr(self.parent_window, 'record_operation'):
                        self.parent_window.record_operation(
                            'parse', self.parse_bar.maximum())
                except Exception:
                    pass
                out_path = self.out_path.text().strip()

                def _open_result():
                    try:
                        if out_path and os.path.isfile(out_path):
                            os.startfile(out_path)
                        else:
                            QMessageBox.information(
                                self, self._t('ui.file_not_found'), self._t('ui.result_file_not_found'))
                    except Exception as exc:
                        QMessageBox.warning(self, self._t('ui.error'), str(exc))

                try:
                    self.parse_stop_btn.clicked.disconnect()
                except Exception:
                    pass
                self.parse_stop_btn.clicked.connect(_open_result)
                message = self._t('ui.done_with_exclamation')
            log_message(self.parse_log, message, debug)
        except Exception:
            pass

    def _inc_parse_prog(self):
        val = self.parse_bar.value() + 1
        self.parse_bar.setValue(val)
        try:
            self.parse_bar.setFormat(f'{self._processed_label()} {val}/{self.parse_bar.maximum()}')
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

    def pick_file(self, line_edit, filter_str: str):
        from ..widgets.ui_helpers import pick_file
        pick_file(self, line_edit, filter_str)

    def open_from_edit(self, line_edit):
        from ..widgets.ui_helpers import open_from_edit
        open_from_edit(self, line_edit)
