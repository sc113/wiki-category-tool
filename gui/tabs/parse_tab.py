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

    def setup_ui(self):
        main_layout = QVBoxLayout(self)

        parse_help_left = (
            'Префиксы/NS: «Авто» — без изменения исходного списка. Выбирайте NS, если в списке названия без префиксов.\n'
            'Локальные и английские префиксы в исходном списке распознаются.\n'
            'Глубина применяется и к страницам, и к подкатегориям.\n'
            'Кнопка «PetScan» открывает расширенный поиск для указанной категории в браузере.'
        )
        parse_help_right = (
            'Результат: .tsv (UTF-8 с BOM). Формат: Title<TAB>строка1<TAB>строка2…\n'
            'В файл записываются только найденные страницы; отсутствующие отражаются в логе.\n'
            'Список берётся из .txt (если указан), иначе — из поля слева.\n'
            'При лимитах API выполняются автоматические паузы/повторы.'
        )

        h_main = QHBoxLayout()

        self.source_panel = CategorySourcePanel(
            self,
            parent_window=self.parent_window,
            help_text=parse_help_left,
            group_title='Источник',
            category_section_label='<b>Получить содержимое категории:</b>',
            category_placeholder='Название корневой категории',
            manual_label='<b>Список категорий для считывания:</b>',
            manual_placeholder='Список страниц (по одной на строку)',
            file_section_label='<b>Или загрузить из файла:</b>',
            file_caption='Файл (.txt):',
        )
        self.ns_combo_parse = self.source_panel.ns_combo
        self.cat_edit = self.source_panel.cat_edit
        self.get_pages_btn = self.source_panel.get_pages_btn
        self.petscan_btn = self.source_panel.petscan_btn
        self.open_petscan_btn = self.source_panel.open_petscan_btn
        self.depth_spin = self.source_panel.depth_spin
        self.manual_list = self.source_panel.manual_list
        self.list_edit = self.source_panel.list_edit
        self.in_path = self.source_panel.in_path
        self.file_edit = self.source_panel.file_edit

        right_group = QGroupBox('Настройки и результат')
        right_group.setStyleSheet(
            "QGroupBox { border: 1px solid lightgray; border-radius: 5px; margin-top: 10px; } "
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px 0 5px; }"
        )
        right_layout = QVBoxLayout(right_group)
        try:
            right_layout.setContentsMargins(8, 12, 8, 8)
            right_layout.setSpacing(8)
        except Exception:
            pass

        save_layout = QHBoxLayout()
        save_layout.addWidget(QLabel('Сохранить в:'))

        self.out_path = QLineEdit('categories.tsv')
        self.out_path.setMinimumWidth(0)
        save_layout.addWidget(self.out_path, 1)

        btn_browse_out = QToolButton()
        btn_browse_out.setText('…')
        btn_browse_out.setAutoRaise(False)
        try:
            btn_browse_out.setFixedSize(28, 28)
            btn_browse_out.setToolTip('Выбрать путь сохранения')
        except Exception:
            pass
        btn_browse_out.clicked.connect(
            lambda: pick_save(self, self.out_path, '.tsv'))
        save_layout.addWidget(btn_browse_out)

        btn_open_out = QPushButton('Открыть')
        btn_open_out.clicked.connect(lambda: open_from_edit(self, self.out_path))
        save_layout.addWidget(btn_open_out)
        save_layout.addStretch()
        add_info_button(self, save_layout, parse_help_right, inline=True)
        right_layout.addLayout(save_layout)

        right_layout.addSpacing(6)
        self.parse_log = QTextEdit()
        self.parse_log.setReadOnly(True)
        self.parse_log.setMinimumHeight(220)

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
        right_layout.addWidget(self.parse_bar)

        control_layout = QHBoxLayout()
        control_layout.addStretch()
        self.parse_btn = QPushButton('Начать считывание')
        self.parse_btn.clicked.connect(self.start_parse)
        control_layout.addWidget(self.parse_btn)
        self.parse_stop_btn = QPushButton('Остановить')
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

    def get_current_language(self) -> str:
        return self.source_panel.get_current_language()

    def get_current_family(self) -> str:
        return self.source_panel.get_current_family()

    def open_petscan_in_browser(self):
        self.source_panel.open_petscan_in_browser()

    def open_petscan(self):
        self.source_panel.open_petscan()

    def fetch_category_pages(self):
        self.source_panel.fetch_category_pages()

    def start_parse(self):
        if not self.out_path.text():
            QMessageBox.warning(self, 'Ошибка', 'Укажите файл результата.')
            return

        try:
            titles = self.source_panel.load_titles_from_inputs()
        except Exception as exc:
            QMessageBox.critical(self, 'Ошибка', str(exc))
            return

        if not titles:
            QMessageBox.warning(
                self, 'Ошибка', 'Не указан ни файл со списком, ни текст списка.')
            return

        lang = self.get_current_language()
        fam = self.get_current_family()

        self.parse_bar.setMaximum(len(titles))
        self.parse_bar.setValue(0)
        self.parse_btn.setEnabled(False)

        ns_sel = self.ns_combo_parse.currentData()
        self.worker = ParseWorker(
            titles, self.out_path.text(), ns_sel, lang, fam)
        self.worker.progress.connect(
            lambda message: [self._inc_parse_prog(), log_message(self.parse_log, message, debug)])
        self.worker.finished.connect(self._on_parse_finished)

        try:
            self.parse_stop_btn.clicked.disconnect()
        except Exception:
            pass
        self.parse_stop_btn.setText('Остановить')
        self.parse_stop_btn.clicked.connect(self.stop_parse)
        self.parse_stop_btn.setEnabled(True)
        self.worker.start()

    def stop_parse(self):
        worker = getattr(self, 'worker', None)
        if worker and worker.isRunning():
            worker.request_stop()
            try:
                self.parse_stop_btn.setEnabled(False)
                log_message(self.parse_log, 'Останавливаю...', debug)
            except Exception:
                pass

    def _on_parse_finished(self):
        try:
            self.parse_btn.setEnabled(True)
            if getattr(self, 'worker', None) and getattr(self.worker, '_stop', False):
                self.parse_stop_btn.setText('Остановить')
                self.parse_stop_btn.setEnabled(False)
                message = 'Остановлено!'
            else:
                self.parse_stop_btn.setText('Открыть')
                self.parse_stop_btn.setEnabled(True)
                out_path = self.out_path.text().strip()

                def _open_result():
                    try:
                        if out_path and os.path.isfile(out_path):
                            os.startfile(out_path)
                        else:
                            QMessageBox.information(
                                self, 'Файл не найден', 'Файл результата не найден.')
                    except Exception as exc:
                        QMessageBox.warning(self, 'Ошибка', str(exc))

                try:
                    self.parse_stop_btn.clicked.disconnect()
                except Exception:
                    pass
                self.parse_stop_btn.clicked.connect(_open_result)
                message = 'Готово!'
            log_message(self.parse_log, message, debug)
        except Exception:
            pass

    def _inc_parse_prog(self):
        self.parse_bar.setValue(self.parse_bar.value() + 1)

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
