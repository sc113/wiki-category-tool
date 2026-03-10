# -*- coding: utf-8 -*-
"""
Вкладка переименования для Wiki Category Tool.

Этот модуль содержит компонент RenameTab, который обеспечивает:
- Загрузку TSV файла с данными для переименования страниц
- Переименование страниц с созданием перенаправлений
- Перенос содержимого категорий (прямые ссылки и шаблоны)
- Template review диалоги при find_in_templates=True
- Кэширование template rules в configs/template_rules.json
- Настройку всех параметров переименования
"""

import os
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QComboBox, QPushButton, QToolButton, QSizePolicy, QProgressBar,
    QMessageBox, QCheckBox, QGroupBox
)
from PySide6.QtCore import Qt, Signal, QUrl, QEvent
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHeaderView

from ...constants import PREFIX_TOOLTIP
from ...core.localization import translate_key
from ...utils import debug
from ...workers.rename_worker import RenameWorker
from ...core.template_manager import TemplateManager
from ...core.pywikibot_config import apply_pwb_config, _dist_configs_dir
from ..widgets.ui_helpers import (
    add_info_button, pick_file, 
    open_from_edit, set_start_stop_ratio,
    init_log_tree, log_tree_parse_and_add, log_tree_add, log_tree_add_event
)
from ..dialogs.template_review_dialog import TemplateReviewDialog


class RenameTab(QWidget):
    """Вкладка для переименования страниц и переноса содержимого категорий"""
    
    # Сигналы для взаимодействия с главным окном
    language_changed = Signal(str)
    family_changed = Signal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent
        
        # Инициализация worker'а
        self.mrworker = None
        
        # Кэш template rules для UI
        self._template_auto_cache_ui = {}
        
        # Данные авторизации
        self.current_user = None
        self.current_lang = None
        self.current_family = None
        self._last_theme_mode = ''
        
        # Создание UI
        self.setup_ui()
        
    def setup_ui(self):
        """Создает пользовательский интерфейс вкладки"""
        ui_lang = getattr(self.parent_window, '_ui_lang', 'ru') if self.parent_window is not None else 'ru'
        # Основной layout
        v = QVBoxLayout(self)
        try:
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(6)
        except Exception:
            pass
        
        # Текст справки
        rename_help = translate_key('help.rename.main', ui_lang, '')
        
        # Строка выбора файла и настроек
        h = QHBoxLayout()
        try:
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(6)
        except Exception:
            pass
        
        # Поле файла с кнопкой
        self.rename_file_edit = QLineEdit('categories.tsv')
        self.rename_file_edit.setMinimumWidth(0)
        self.rename_file_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        
        h.addWidget(QLabel(self._t('ui.rename_list_tsv')))
        h.addWidget(self.rename_file_edit, 1)
        # Кнопка «…» справа
        btn_browse_rename = QToolButton()
        btn_browse_rename.setText('…')
        btn_browse_rename.setAutoRaise(False)
        try:
            btn_browse_rename.setFixedSize(27, 27)
            btn_browse_rename.setCursor(Qt.PointingHandCursor)
            btn_browse_rename.setToolTip(self._t('ui.choose_file', 'Choose file'))
        except Exception:
            pass
        btn_browse_rename.clicked.connect(lambda: pick_file(self, self.rename_file_edit, '*.tsv'))
        h.addWidget(btn_browse_rename)
        
        # Кнопка "Открыть"
        btn_open_tsv_rename = QPushButton(self._t('ui.open', 'Open'))
        btn_open_tsv_rename.clicked.connect(lambda: open_from_edit(self, self.rename_file_edit))
        btn_open_tsv_rename.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        h.addWidget(btn_open_tsv_rename)
        
        # Компактный выбор префикса (выпадающий список)
        self.prefix_label_rename = QLabel(self._t('ui.prefixes', 'Prefixes:'))
        self.prefix_label_rename.setToolTip(PREFIX_TOOLTIP)
        h.addWidget(self.prefix_label_rename)
        
        self.rename_ns_combo = QComboBox()
        self.rename_ns_combo.setEditable(False)
        # Заполнение будет происходить при установке языка/семейства
        h.addWidget(self.rename_ns_combo)
        
        # Кнопка ℹ в строке выбора файла
        self.prefix_help_btn_rename = add_info_button(self, h, rename_help)
        
        v.addLayout(h)
        
        # Опции в две колонки
        # Подсказки для опций переноса
        phase1_help = translate_key('help.rename.phase1', ui_lang, '')
        phase2_help = translate_key('help.rename.phase2', ui_lang, '')
        locative_help = translate_key('help.rename.locative', ui_lang, '')
        
        # Первая опция: прямые ссылки
        row_p1 = QHBoxLayout()
        phase1_main_label = translate_key(
            'ui.transfer_plain_main',
            ui_lang,
            'Direct transfer',
        )
        phase1_mode_label = translate_key(
            'ui.transfer_plain_hint',
            ui_lang,
            '(categories in plain text)',
        )
        self.phase1_enabled_cb = QCheckBox(phase1_main_label)
        self.phase1_enabled_cb.setChecked(True)
        try:
            self.phase1_enabled_cb.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        except Exception:
            pass
        row_p1.addWidget(self.phase1_enabled_cb)
        self.phase1_mode_hint = QLabel(phase1_mode_label)
        self.phase1_mode_hint.setObjectName('mutedParenText')
        try:
            if not self.phase1_mode_hint.text().startswith(' '):
                self.phase1_mode_hint.setText(' ' + self.phase1_mode_hint.text())
            self.phase1_mode_hint.setStyleSheet('color: rgba(148, 166, 183, 0.78); font-size: 11px;')
        except Exception:
            pass
        row_p1.addWidget(self.phase1_mode_hint)
        add_info_button(self, row_p1, phase1_help, inline=True)
        try:
            row_p1.addStretch(1)
        except Exception:
            pass
        
        # Опция: переименовывать саму категорию
        row_move_cat = QHBoxLayout()
        self.move_members_cb = QCheckBox(self._t('ui.rename_pages'))
        self.move_members_cb.setChecked(True)
        try:
            self.move_members_cb.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        except Exception:
            pass
        row_move_cat.addWidget(self.move_members_cb)
        try:
            row_move_cat.addStretch(1)
        except Exception:
            pass
        
        # Вторая опция: параметры шаблонов
        row_p2 = QHBoxLayout()
        phase2_main_label = translate_key(
            'ui.transfer_template_main',
            ui_lang,
            'Template-based categorization',
        )
        phase2_mode_label = translate_key(
            'ui.transfer_template_hint',
            ui_lang,
            '(category name in template params)',
        )
        self.find_in_templates_cb = QCheckBox(phase2_main_label)
        self.find_in_templates_cb.setChecked(True)
        try:
            self.find_in_templates_cb.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        except Exception:
            pass
        row_p2.addWidget(self.find_in_templates_cb)
        self.phase2_mode_hint = QLabel(phase2_mode_label)
        self.phase2_mode_hint.setObjectName('mutedParenText')
        try:
            if not self.phase2_mode_hint.text().startswith(' '):
                self.phase2_mode_hint.setText(' ' + self.phase2_mode_hint.text())
            self.phase2_mode_hint.setStyleSheet('color: rgba(148, 166, 183, 0.78); font-size: 11px;')
        except Exception:
            pass
        row_p2.addWidget(self.phase2_mode_hint)
        add_info_button(self, row_p2, phase2_help, inline=True)
        try:
            row_p2.addStretch(1)
        except Exception:
            pass

        # Опция: Локативы
        row_loc = QHBoxLayout()
        locative_main_label = translate_key(
            'ui.locative_changes_main',
            ui_lang,
            'Geocase inflection changes in parameters',
        )
        locative_mode_label = translate_key(
            'ui.locative_changes_mode',
            ui_lang,
            '(manual mode)'
        )
        self.locatives_cb = QCheckBox(locative_main_label)
        self.locatives_cb.setChecked(False)
        try:
            self.locatives_cb.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        except Exception:
            pass
        row_loc.addWidget(self.locatives_cb)
        self.locatives_mode_hint = QLabel(locative_mode_label)
        self.locatives_mode_hint.setObjectName('mutedParenText')
        try:
            if not self.locatives_mode_hint.text().startswith(' '):
                self.locatives_mode_hint.setText(' ' + self.locatives_mode_hint.text())
            self.locatives_mode_hint.setStyleSheet('color: rgba(148, 166, 183, 0.78); font-size: 11px;')
        except Exception:
            pass
        row_loc.addWidget(self.locatives_mode_hint)
        add_info_button(self, row_loc, locative_help, inline=True)
        try:
            row_loc.addStretch(1)
        except Exception:
            pass
        
        # Кнопки правил (будут прикреплены к правому заголовку)
        btn_show_rules = QPushButton(self._t('ui.show_replacement_rules'))
        btn_clear_rules = QPushButton(self._t('ui.clear_replacement_rules'))
        
        # Подключаем обработчики кнопок правил
        btn_show_rules.clicked.connect(self._open_rules_dialog)
        btn_clear_rules.clicked.connect(self._clear_rules)
        
        # Чекбоксы перенаправлений
        self.leave_cat_redirect_cb = QCheckBox(self._t('ui.leave_category_redirects'))
        try:
            self.leave_cat_redirect_cb.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        except Exception:
            pass
        self.leave_cat_redirect_cb.setChecked(False)
        
        self.leave_other_redirect_cb = QCheckBox(self._t('ui.leave_other_redirects'))
        try:
            self.leave_other_redirect_cb.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        except Exception:
            pass
        self.leave_other_redirect_cb.setChecked(True)
        
        # Управление доступностью перенаправлений по галке «Переименовывать страницы»
        try:
            self.leave_cat_redirect_cb.setEnabled(self.move_members_cb.isChecked())
            self.leave_other_redirect_cb.setEnabled(self.move_members_cb.isChecked())
            self.move_members_cb.toggled.connect(self.leave_cat_redirect_cb.setEnabled)
            self.move_members_cb.toggled.connect(self.leave_other_redirect_cb.setEnabled)
        except Exception:
            pass
        
        # Утилита-обёртка для QHBoxLayout
        def _wrap(layout_obj):
            try:
                w = QWidget()
                lay = QHBoxLayout(w)
                lay.setContentsMargins(0, 0, 0, 0)
                lay.setSpacing(0)
                lay.addLayout(layout_obj)
                return w
            except Exception:
                return QWidget()

        # Рамка «Переименование»
        rename_opts_group = QGroupBox(self._t('ui.rename'))
        rename_opts_layout = QVBoxLayout(rename_opts_group)
        try:
            rename_opts_layout.setContentsMargins(10, 8, 10, 8)
            rename_opts_layout.setSpacing(2)
        except Exception:
            pass
        rename_opts_layout.addWidget(_wrap(row_move_cat))
        row_redirect_cat = QHBoxLayout()
        row_redirect_cat.addWidget(self.leave_cat_redirect_cb)
        row_redirect_cat.addStretch(1)
        row_redirect_other = QHBoxLayout()
        row_redirect_other.addWidget(self.leave_other_redirect_cb)
        row_redirect_other.addStretch(1)
        rename_opts_layout.addWidget(_wrap(row_redirect_cat))
        rename_opts_layout.addWidget(_wrap(row_redirect_other))

        # Рамка «Перенос содержимого категорий»
        transfer_opts_group = QGroupBox(self._t('ui.transfer_category_content'))
        transfer_opts_layout = QVBoxLayout(transfer_opts_group)
        try:
            transfer_opts_layout.setContentsMargins(10, 8, 10, 8)
            transfer_opts_layout.setSpacing(2)
        except Exception:
            pass
        transfer_body = QHBoxLayout()
        try:
            transfer_body.setContentsMargins(0, 0, 0, 0)
            transfer_body.setSpacing(8)
        except Exception:
            pass
        transfer_opts_col = QVBoxLayout()
        try:
            transfer_opts_col.setContentsMargins(0, 0, 0, 0)
            transfer_opts_col.setSpacing(2)
        except Exception:
            pass
        transfer_opts_col.addWidget(_wrap(row_p1))
        transfer_opts_col.addWidget(_wrap(row_p2))
        transfer_opts_col.addWidget(_wrap(row_loc))
        transfer_opts_col.addStretch(1)
        rules_col = QVBoxLayout()
        try:
            rules_col.setContentsMargins(0, 0, 0, 0)
            rules_col.setSpacing(4)
        except Exception:
            pass
        rules_col.addWidget(btn_show_rules)
        rules_col.addWidget(btn_clear_rules)
        rules_col.addStretch(1)
        transfer_body.addLayout(transfer_opts_col, 1)
        transfer_body.addLayout(rules_col, 0)
        transfer_opts_layout.addLayout(transfer_body)

        # Две рамки в одну строку
        opts_row = QHBoxLayout()
        try:
            opts_row.setContentsMargins(0, 0, 0, 0)
            opts_row.setSpacing(12)
        except Exception:
            pass
        opts_row.addWidget(rename_opts_group, 4)
        opts_row.addWidget(transfer_opts_group, 5)
        v.addLayout(opts_row)

        # Комментарий к правке — перенесён вниз (между фильтром и прогрессом)
        self.rename_comment_edit = QLineEdit()
        try:
            self.rename_comment_edit.setPlaceholderText(self._t('ui.rename_comment_placeholder'))
        except Exception:
            pass

        # Постоянно видимые шкалы прогресса (создаём сейчас, добавим в нижний ряд)
        # Левая: общий прогресс по TSV
        self.rename_outer_label = QLabel(
            translate_key('ui.processed_counter_initial', ui_lang, 'Processed 0/0')
        )
        try:
            self.rename_outer_label.setVisible(False)
        except Exception:
            pass
        self.rename_outer_bar = QProgressBar()
        try:
            self.rename_outer_bar.setMaximum(1)
            self.rename_outer_bar.setValue(0)
            self.rename_outer_bar.setTextVisible(True)
            self.rename_outer_bar.setFormat(
                translate_key('ui.processed_counter_initial', ui_lang, 'Processed 0/0')
            )
            self.rename_outer_bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            self.rename_outer_bar.setMinimumWidth(60)
        except Exception:
            pass
        # Правая: прогресс переноса участников текущей категории
        self.rename_inner_label = QLabel(
            translate_key('ui.moved_counter_initial', ui_lang, 'Moved 0/0')
        )
        try:
            self.rename_inner_label.setVisible(False)
        except Exception:
            pass
        self.rename_inner_bar = QProgressBar()
        try:
            self.rename_inner_bar.setMaximum(1)
            self.rename_inner_bar.setValue(0)
            self.rename_inner_bar.setTextVisible(True)
            self.rename_inner_bar.setFormat(
                translate_key('ui.moved_counter_initial', ui_lang, 'Moved 0/0')
            )
            self.rename_inner_bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            self.rename_inner_bar.setMinimumWidth(60)
        except Exception:
            pass

        # Поле фильтра перенесем ниже — перед комментарием (см. ниже)
        
        # Лог выполнения: заголовок + легенда (ℹ)
        log_header_row = QHBoxLayout()
        log_header_row.addWidget(QLabel(self._t('ui.execution_log')))
        v.addLayout(log_header_row)

        # Дерево лога вместо QTextEdit
        self.rename_log_tree = init_log_tree(self)
        self.rename_log_tree.setObjectName('renameLogTree')
        self._configure_log_header()
        self._apply_rename_log_tree_theme()

        from ..widgets.ui_helpers import create_tree_log_wrap
        rename_wrap = create_tree_log_wrap(self, self.rename_log_tree, with_header=False)
        v.addWidget(rename_wrap, 1)
        
        # Поле фильтрации переносимых страниц по заголовку (регулярное выражение)
        regex_help = translate_key('help.rename.regex', ui_lang, '')

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel(self._t('ui.filter_category_content_by_titles')))
        self.title_regex_edit = QLineEdit()
        try:
            self.title_regex_edit.setPlaceholderText(self._t('ui.specify_a_regex_for_titles_to_exclude_from'))
        except Exception:
            pass
        filter_row.addWidget(self.title_regex_edit, 1)
        add_info_button(self, filter_row, regex_help, inline=True)
        v.addLayout(filter_row)
        comment_row_bottom = QHBoxLayout()
        comment_row_bottom.addWidget(QLabel(self._t('ui.edit_summary_singular', 'Edit summary:')))
        comment_row_bottom.addWidget(self.rename_comment_edit, 1)
        v.addLayout(comment_row_bottom)

        # Валидация регулярного выражения по вводу
        self._title_regex_valid = True
        try:
            self.title_regex_edit.textChanged.connect(self._on_title_regex_changed)
        except Exception:
            pass

        # Кнопки управления
        self.rename_btn = QPushButton(self._t('ui.start_rename'))
        self.rename_btn.clicked.connect(self.start_rename)
        
        self.rename_stop_btn = QPushButton(self._t('ui.stop', 'Stop'))
        self.rename_stop_btn.setEnabled(False)
        self.rename_stop_btn.clicked.connect(self.stop_rename)
        
        row_run = QHBoxLayout()
        try:
            row_run.setContentsMargins(0, 0, 0, 0)
            row_run.setSpacing(6)
        except Exception:
            pass
        # Группа прогресса, растягивается до кнопки «Начать»
        progress_wrap = QWidget()
        progress_layout = QHBoxLayout(progress_wrap)
        try:
            progress_layout.setContentsMargins(0, 0, 0, 0)
            progress_layout.setSpacing(6)
        except Exception:
            pass
        progress_layout.addWidget(self.rename_outer_label)
        progress_layout.addWidget(self.rename_outer_bar)
        try:
            progress_layout.setStretchFactor(self.rename_outer_label, 0)
            progress_layout.setStretchFactor(self.rename_outer_bar, 1)
        except Exception:
            pass
        progress_layout.addSpacing(12)
        progress_layout.addWidget(self.rename_inner_label)
        progress_layout.addWidget(self.rename_inner_bar)
        try:
            progress_layout.setStretchFactor(self.rename_inner_label, 0)
            progress_layout.setStretchFactor(self.rename_inner_bar, 1)
        except Exception:
            pass
        row_run.addWidget(progress_wrap, 1)
        row_run.addWidget(self.rename_btn)
        row_run.addWidget(self.rename_stop_btn)
        v.addLayout(row_run)
        
        # Устанавливаем соотношение размеров кнопок
        set_start_stop_ratio(self.rename_btn, self.rename_stop_btn, 3)

    def _theme_mode(self) -> str:
        try:
            return str(getattr(self.parent_window, '_theme_mode', 'teal') or 'teal').strip().lower()
        except Exception:
            return 'teal'

    def _ui_lang(self) -> str:
        try:
            raw = str(getattr(self.parent_window, '_ui_lang', 'ru')).lower()
        except Exception:
            raw = 'ru'
        return 'en' if raw.startswith('en') else 'ru'

    def _processed_label(self) -> str:
        return translate_key('ui.processed_short', self._ui_lang(), 'Processed')

    def _moved_label(self) -> str:
        return translate_key('ui.moved_short', self._ui_lang(), 'Moved')

    def _t(self, key: str, default: str = '') -> str:
        return translate_key(key, self._ui_lang(), default)

    def _fmt(self, key: str, default: str = '', **kwargs) -> str:
        text = self._t(key, default)
        try:
            return text.format(**kwargs)
        except Exception:
            return text

    def _is_light_theme(self) -> bool:
        mode = self._theme_mode()
        return mode == 'light' or mode.startswith('light')

    def _is_dark_black_theme(self) -> bool:
        mode = self._theme_mode()
        return mode == 'dark' or mode.startswith('dark')

    def _apply_rename_log_tree_theme(self):
        if not getattr(self, 'rename_log_tree', None):
            return
        if self._is_light_theme():
            self.rename_log_tree.setStyleSheet(
                """
                QTreeWidget#renameLogTree {
                    background: #ffffff;
                    alternate-background-color: #f4f8fc;
                    color: #1f2f3a;
                    border: 1px solid #bcd1e2;
                    border-radius: 8px;
                }
                QTreeWidget#renameLogTree::item:selected {
                    background: #d7eaf9;
                    color: #102a43;
                }
                QTreeWidget#renameLogTree QHeaderView::section {
                    background: #e7f0f7;
                    color: #1e4763;
                    border: 1px solid #bcd1e2;
                    padding: 4px 6px;
                }
                """
            )
        elif self._is_dark_black_theme():
            self.rename_log_tree.setStyleSheet(
                """
                QTreeWidget#renameLogTree {
                    background: #171d25;
                    alternate-background-color: #1f2630;
                    color: #e4eaf2;
                    border: 1px solid #4f5b6a;
                    border-radius: 8px;
                }
                QTreeWidget#renameLogTree::item:selected {
                    background: #3b4656;
                    color: #f2f6fb;
                }
                QTreeWidget#renameLogTree QHeaderView::section {
                    background: #2a3340;
                    color: #e6edf6;
                    border: 1px solid #4f5b6a;
                    padding: 4px 6px;
                }
                """
            )
        else:
            self.rename_log_tree.setStyleSheet(
                """
                QTreeWidget#renameLogTree {
                    background: #0a2533;
                    alternate-background-color: #0e3042;
                    color: #e6f3f6;
                    border: 1px solid rgba(115, 170, 182, 0.45);
                    border-radius: 8px;
                }
                QTreeWidget#renameLogTree::item:selected {
                    background: rgba(82, 148, 168, 0.55);
                    color: #f2fcff;
                }
                QTreeWidget#renameLogTree QHeaderView::section {
                    background: #144055;
                    color: #e8f7fa;
                    border: 1px solid rgba(115, 170, 182, 0.45);
                    padding: 4px 6px;
                }
                """
            )

    def showEvent(self, event):
        try:
            super().showEvent(event)
        finally:
            current_theme = self._theme_mode()
            if current_theme != self._last_theme_mode:
                self._last_theme_mode = current_theme
                self._apply_rename_log_tree_theme()

    def changeEvent(self, event):
        try:
            super().changeEvent(event)
        finally:
            try:
                evt_type = event.type() if event is not None else None
            except Exception:
                evt_type = None
            if evt_type in (QEvent.StyleChange, QEvent.PaletteChange, QEvent.ApplicationPaletteChange):
                self._apply_rename_log_tree_theme()

    def _configure_log_header(self):
        """Растягивает полезные колонки лога на всю ширину, с адаптацией при сжатии окна."""
        try:
            hdr = self.rename_log_tree.header()
            fm = self.rename_log_tree.fontMetrics()
            try:
                hdr.setSectionResizeMode(0, QHeaderView.Fixed)
                self.rename_log_tree.setColumnWidth(0, fm.horizontalAdvance('00:00:00') + 8)
            except Exception:
                pass
            try:
                hdr.setSectionResizeMode(1, QHeaderView.Fixed)
                self.rename_log_tree.setColumnWidth(1, max(40, fm.horizontalAdvance(self._t('ui.type', 'Type')) + 14))
            except Exception:
                pass
            try:
                hdr.setSectionResizeMode(2, QHeaderView.Fixed)
                self.rename_log_tree.setColumnWidth(2, max(84, fm.horizontalAdvance(self._t('ui.skipped', 'Skipped')) + 20))
            except Exception:
                pass
            for col in (3, 4, 5):
                try:
                    hdr.setSectionResizeMode(col, QHeaderView.Stretch)
                except Exception:
                    pass
            try:
                hdr.setStretchLastSection(True)
            except Exception:
                pass
        except Exception:
            pass

    def _on_title_regex_changed(self, _=None):
        """Проверяет валидность регулярного выражения и подсвечивает поле."""
        try:
            text = (self.title_regex_edit.text() or '').strip()
        except Exception:
            text = ''
        if not text:
            # Пусто — считается валидным (фильтр выключен)
            self._title_regex_valid = True
            try:
                self.title_regex_edit.setStyleSheet('')
                self.title_regex_edit.setToolTip('')
            except Exception:
                pass
            return
        try:
            import re as _re
            _re.compile(text)
            self._title_regex_valid = True
            try:
                self.title_regex_edit.setStyleSheet('')
                self.title_regex_edit.setToolTip('')
            except Exception:
                pass
        except Exception as e:
            self._title_regex_valid = False
            try:
                self.title_regex_edit.setStyleSheet('background-color:#fdecea')
                self.title_regex_edit.setToolTip(self._fmt('ui.regex_error', error=e))
            except Exception:
                pass
        
    def _open_rules_dialog(self):
        """Открыть файл правил в системе (папка configs/template_rules.json)"""
        try:
            # Унифицированное определение пути к правилам
            path = self._resolve_rules_path()
            
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not os.path.exists(path):
                with open(path, 'w', encoding='utf-8') as f:
                    f.write('{}')
            
            url = QUrl.fromLocalFile(os.path.abspath(path))
            ok = QDesktopServices.openUrl(url)
            if not ok:
                # Фолбэк для Windows: прямой запуск
                try:
                    os.startfile(path)
                    ok = True
                except Exception:
                    ok = False
            if not ok:
                # Фолбэк: открыть Проводник и выделить файл
                try:
                    import subprocess
                    subprocess.Popen(['explorer', '/select,', path])
                    ok = True
                except Exception:
                    ok = False
            if not ok:
                # Последняя попытка: открыть папку
                dir_url = QUrl.fromLocalFile(os.path.dirname(os.path.abspath(path)))
                ok = QDesktopServices.openUrl(dir_url)
            if not ok:
                raise RuntimeError(self._t('ui.rules_open_failed'))
        except Exception:
            QMessageBox.warning(self, self._t('ui.error', 'Error'), self._t('ui.rules_open_failed'))
    
    def _clear_rules(self):
        """Очистить правила замен"""
        try:
            cleared = False
            # Очищаем через TemplateManager, если доступен у воркера
            w = getattr(self, 'mrworker', None)
            try:
                if w and hasattr(w, 'template_manager') and w.template_manager:
                    w.template_manager.clear_template_cache()
                    cleared = True
            except Exception:
                pass
            # Очистим и локальный UI‑кэш
            if hasattr(self, '_template_auto_cache_ui'):
                try:
                    self._template_auto_cache_ui.clear()
                    cleared = True or cleared
                except Exception:
                    pass
            # Фолбэк: принудительно записать пустой JSON в файл
            if not cleared:
                path = self._resolve_rules_path()
                if path:
                    try:
                        with open(path, 'w', encoding='utf-8') as f:
                            f.write('{}')
                        cleared = True or cleared
                    except Exception:
                        pass
            if cleared:
                QMessageBox.information(self, self._t('ui.done', 'Done'), self._t('ui.rules_cleared'))
            else:
                QMessageBox.information(self, self._t('ui.info', 'Info'), self._t('ui.rules_cache_empty'))
        except Exception:
            QMessageBox.warning(self, self._t('ui.error', 'Error'), self._t('ui.rules_clear_failed'))

    def _resolve_rules_path(self) -> str:
        """Единая точка получения пути к файлу правил шаблонов."""
        try:
            # 1) Через активный TemplateManager воркера
            w = getattr(self, 'mrworker', None)
            if w and hasattr(w, 'template_manager') and w.template_manager:
                p = w.template_manager.get_rules_file_path()
                if p:
                    return p
        except Exception:
            pass
        # 2) Вычислить путь по политике TemplateManager
        try:
            return TemplateManager.resolve_rules_file_path()
        except Exception:
            # 3) Жёсткий фолбэк: рядом с GUI модулем
            try:
                base = _dist_configs_dir()
            except Exception:
                base = os.path.join(os.path.dirname(__file__), 'configs')
            return os.path.join(base, 'template_rules.json')
    
    def start_rename(self):
        """Запускает процесс переименования"""
        debug(f'Start rename: file={self.rename_file_edit.text()}')
        
        if not self.rename_file_edit.text():
            QMessageBox.warning(self, self._t('ui.error', 'Error'), self._t('ui.specify_tsv', 'Specify TSV'))
            return
        
        # Получаем данные авторизации от родительского окна
        if not self.parent_window:
            QMessageBox.warning(self, self._t('ui.error', 'Error'), self._t('ui.no_access_auth_data'))
            return
        
        # Получаем данные из родительского окна (будет реализовано в main_window)
        user = getattr(self.parent_window, 'current_user', None)
        pwd = getattr(self.parent_window, 'current_password', None)
        lang = getattr(self.parent_window, 'current_lang', 'ru')
        fam = getattr(self.parent_window, 'current_family', 'wikipedia')
        
        debug(self._fmt('log.rename_tab.auth_data_received', user=user, password='***' if pwd else None, lang=lang, family=fam))
        
        if not user or not pwd:
            debug(self._fmt(
                'log.rename_tab.auth_error',
                user=user,
                password_state=self._t('ui.password_set') if pwd else self._t('ui.password_not_set'),
            ))
            QMessageBox.warning(self, self._t('ui.error', 'Error'), self._t('ui.must_log_in'))
            return
        
        # Предупреждение, если выбран «Авто» и в TSV, похоже, названия без префиксов
        try:
            ns_sel_preview = self.rename_ns_combo.currentData()
        except Exception:
            ns_sel_preview = 'auto'
        try:
            is_auto = isinstance(ns_sel_preview, str) and (ns_sel_preview or '').strip().lower() == 'auto'
        except Exception:
            is_auto = True

        if is_auto:
            try:
                import csv as _csv
                plain_rows = 0
                checked = 0
                with open(self.rename_file_edit.text(), newline='', encoding='utf-8-sig') as _f:
                    _reader = _csv.reader(_f, delimiter='\t')
                    for _row in _reader:
                        if len(_row) < 2:
                            continue
                        _old = (_row[0] or '').strip()
                        _new = (_row[1] or '').strip()
                        if not _old and not _new:
                            continue
                        checked += 1
                        if (':' not in _old) and (':' not in _new):
                            plain_rows += 1
                        if checked >= 30:
                            break
                if checked > 0 and plain_rows == checked:
                    # Информируем в лог
                    try:
                        info_msg = self._t('ui.rename_plain_titles_info')
                        log_tree_add(self.rename_log_tree, datetime.now().strftime('%H:%M:%S'), None, info_msg, 'manual', 'info', None, None, True)
                    except Exception:
                        pass
                    msg = self._t('ui.rename_plain_titles_confirm')
                    res = QMessageBox.question(
                        self,
                        self._t('ui.confirm_launch'),
                        msg,
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No,
                    )
                    if res != QMessageBox.Yes:
                        try:
                            log_tree_add(self.rename_log_tree, datetime.now().strftime('%H:%M:%S'), None, self._t('ui.rename_plain_titles_cancelled'), 'manual', 'info', None, None, True)
                        except Exception:
                            pass
                        return
                    else:
                        try:
                            log_tree_add(self.rename_log_tree, datetime.now().strftime('%H:%M:%S'), None, self._t('ui.rename_plain_titles_confirmed'), 'manual', 'info', None, None, True)
                        except Exception:
                            pass
            except Exception:
                # Любые ошибки эвристики не должны мешать запуску
                pass

        apply_pwb_config(lang, fam)

        # Блокируем кнопки и очищаем лог
        self.rename_btn.setEnabled(False)
        self.rename_stop_btn.setEnabled(True)
        try:
            self.rename_outer_bar.setVisible(True)
            self.rename_outer_label.setVisible(False)
            self.rename_outer_bar.setMaximum(1)
            self.rename_outer_bar.setValue(0)
            self.rename_outer_bar.setFormat(f'{self._processed_label()} 0/0')
            self.rename_inner_bar.setVisible(True)
            self.rename_inner_label.setVisible(False)
            self.rename_inner_bar.setMaximum(1)
            self.rename_inner_bar.setValue(0)
            self.rename_inner_bar.setFormat(f'{self._moved_label()} 0/0')
        except Exception:
            pass
        
        ns_sel = self.rename_ns_combo.currentData()
        
        # Валидация регулярного выражения фильтра
        title_regex = (self.title_regex_edit.text() or '').strip()
        if title_regex:
            try:
                import re as _re
                _re.compile(title_regex)
            except Exception as e:
                QMessageBox.warning(self, self._t('ui.error', 'Error'), self._fmt('ui.invalid_title_filter_regex', error=e))
                return

        # Создаем и запускаем worker
        self.mrworker = RenameWorker(
            self.rename_file_edit.text(),
            user, pwd, lang, fam,
            ns_sel,
            self.leave_cat_redirect_cb.isChecked(),
            self.leave_other_redirect_cb.isChecked(),
            True,  # move_members - всегда True если хотя бы одна фаза включена
            self.find_in_templates_cb.isChecked(),
            self.phase1_enabled_cb.isChecked(),
            self.move_members_cb.isChecked(),
            (self.rename_comment_edit.text() or '').strip(),
            title_regex,
            self.locatives_cb.isChecked()
        )
        
        # Подключаем сигналы
        self.mrworker.progress.connect(lambda m: log_tree_parse_and_add(self.rename_log_tree, m))
        # Подключаем структурированные события
        try:
            self.mrworker.log_event.connect(lambda e: log_tree_add_event(self.rename_log_tree, e))
        except Exception:
            pass
        # Прогресс по файлу TSV
        try:
            self.mrworker.tsv_progress_init.connect(self._rename_outer_init)
            self.mrworker.tsv_progress_inc.connect(self._rename_outer_inc)
        except Exception:
            pass
        # Прогресс по участникам категории
        try:
            self.mrworker.inner_progress_init.connect(self._rename_inner_init)
            self.mrworker.inner_progress_inc.connect(self._rename_inner_inc)
            self.mrworker.inner_progress_reset.connect(self._rename_inner_reset)
        except Exception:
            pass
        self.mrworker.finished.connect(self._on_rename_finished)
        
        # Подключаем template review диалоги
        self.mrworker.template_review_request.connect(self._on_review_request)
        
        self.mrworker.start()
    
    def stop_rename(self):
        """Останавливает процесс переименования"""
        w = getattr(self, 'mrworker', None)
        if w and hasattr(w, 'isRunning') and w.isRunning():
            try:
                # Сначала мягкая остановка
                w.request_stop()
                # Если поток ещё активен — ждём недолго и жёстко завершаем
                if not w.wait(1200):
                    w.graceful_stop(5000)
            except Exception:
                try:
                    w.graceful_stop(5000)
                except Exception:
                    pass
    
    def _on_rename_finished(self):
        """Обработчик завершения процесса переименования"""
        stopped = bool(getattr(self, 'mrworker', None) and getattr(self.mrworker, '_stop', False))
        worker = getattr(self, 'mrworker', None)
        try:
            edits = int(getattr(worker, 'saved_edits', 0) or 0)
        except Exception:
            edits = 0
        try:
            if edits > 0 and self.parent_window and hasattr(self.parent_window, 'record_operation'):
                self.parent_window.record_operation('rename', edits)
        except Exception:
            pass
        self.rename_btn.setEnabled(True)
        self.rename_stop_btn.setEnabled(False)
        msg = self._t('ui.stopped', 'Stopped!') if stopped else self._t('ui.rename_completed')
        try:
            # Служебное системное сообщение: статус ℹ️, без иконки объекта
            log_tree_add(self.rename_log_tree, datetime.now().strftime('%H:%M:%S'), None, msg, 'manual', 'info', None, None, True)
        except Exception:
            pass
        # Прогресс-бары остаются видимыми по требованию UX
        try:
            self.rename_outer_bar.setVisible(True)
            self.rename_outer_label.setVisible(False)
            self.rename_inner_bar.setVisible(True)
            self.rename_inner_label.setVisible(False)
        except Exception:
            pass

    def _rename_outer_init(self, total: int):
        try:
            self.rename_outer_bar.setVisible(True)
            self.rename_outer_label.setVisible(False)
            self.rename_outer_bar.setMaximum(max(1, int(total)))
            self.rename_outer_bar.setValue(0)
            self.rename_outer_bar.setFormat(f"{self._processed_label()} 0/{max(1, int(total))}")
            try:
                self.rename_outer_label.setText(f"{self._processed_label()} 0/{max(1, int(total))}")
            except Exception:
                pass
        except Exception:
            pass

    def _rename_outer_inc(self):
        try:
            val = self.rename_outer_bar.value() + 1
            self.rename_outer_bar.setValue(val)
            self.rename_outer_bar.setFormat(f"{self._processed_label()} {val}/{self.rename_outer_bar.maximum()}")
            try:
                self.rename_outer_label.setText(f"{self._processed_label()} {val}/{self.rename_outer_bar.maximum()}")
            except Exception:
                pass
        except Exception:
            pass

    def _rename_inner_init(self, total: int):
        try:
            self.rename_inner_bar.setVisible(True)
            self.rename_inner_label.setVisible(False)
            self.rename_inner_bar.setMaximum(max(1, int(total)))
            self.rename_inner_bar.setValue(0)
            self.rename_inner_bar.setFormat(f"{self._moved_label()} 0/{max(1, int(total))}")
            try:
                self.rename_inner_label.setText(f"{self._moved_label()} 0/{max(1, int(total))}")
            except Exception:
                pass
        except Exception:
            pass

    def _rename_inner_inc(self):
        try:
            val = self.rename_inner_bar.value() + 1
            self.rename_inner_bar.setValue(val)
            self.rename_inner_bar.setFormat(f"{self._moved_label()} {val}/{self.rename_inner_bar.maximum()}")
            try:
                self.rename_inner_label.setText(f"{self._moved_label()} {val}/{self.rename_inner_bar.maximum()}")
            except Exception:
                pass
        except Exception:
            pass

    def _rename_inner_reset(self):
        try:
            self.rename_inner_bar.setMaximum(1)
            self.rename_inner_bar.setValue(0)
            self.rename_inner_bar.setFormat(f"{self._moved_label()} 0/0")
            try:
                self.rename_inner_label.setText('')
            except Exception:
                pass
        except Exception:
            pass
    
    def _on_review_request(self, payload):
        """Обработчик запроса на проверку изменений в шаблоне"""
        try:
            debug(self._fmt('log.rename_tab.review_request_received', payload=payload))
            
            # Добавим контекст проекта (family/lang) для корректных ссылок «открыть/история»
            try:
                fam = None
                lng = None
                try:
                    fam = getattr(self.mrworker, 'family', None)
                    lng = getattr(self.mrworker, 'lang', None)
                except Exception:
                    fam = None
                    lng = None
                if not fam:
                    fam = getattr(self, 'current_family', None) or (
                        getattr(self.parent_window, 'current_family', None)
                    ) or 'wikipedia'
                if not lng:
                    lng = getattr(self, 'current_lang', None) or (
                        getattr(self.parent_window, 'current_lang', None)
                    ) or 'ru'
                payload = dict(payload)
                payload['family'] = fam
                payload['lang'] = lng
            except Exception:
                pass

            # Показываем диалог проверки шаблона
            dialog = TemplateReviewDialog(self, payload)
            result = dialog.exec()
            
            debug(self._fmt('log.rename_tab.review_result', result=result))
            
            # Отправляем результат обратно в worker
            # Если пользователь выбрал «Подтверждать все аналогичные» — пометим правило auto=approve
            # dedupe_mode передаём только если действительно есть предупреждение о дублях
            try:
                if (payload.get('dup_warning') and payload.get('dup_idx1') and payload.get('dup_idx2')):
                    dm_resp = dialog.get_dedupe_mode() if hasattr(dialog, 'get_dedupe_mode') else None
                else:
                    dm_resp = None
            except Exception:
                dm_resp = None
            response_data = {
                'req_id': payload.get('request_id'),
                'result': dialog.get_action(),
                'auto_confirm': dialog.get_auto_confirm(),
                'auto_skip': dialog.get_auto_skip(),
                'edited_template': dialog.get_edited_template() if hasattr(dialog, 'get_edited_template') else '',
                'dedupe_mode': dm_resp
            }
            
            debug(self._fmt('log.rename_tab.review_response_sent', response=response_data))
            
            # Установим флаг авто для правила сразу при подтверждении всех аналогичных
            try:
                if response_data.get('result') == 'apply' and response_data.get('auto_confirm'):
                    # Передаём в воркер через отдельный канал шаблон исходный/новый, чтобы он мог установить auto
                    if hasattr(self.mrworker, 'template_manager'):
                        tm = self.mrworker.template_manager
                        before = payload.get('template') or ''
                        after = response_data.get('edited_template') or (payload.get('proposed_template') or '')
                        if before and after:
                            # dedupe сохраняем только если в диалоге действительно были дубли
                            dm = response_data.get('dedupe_mode', None)
                            try:
                                if not (payload.get('dup_warning') and payload.get('dup_idx1') and payload.get('dup_idx2')):
                                    dm = None
                            except Exception:
                                dm = None
                            # Нормализуем режим дедупликации к left/right при необходимости
                            if dm == 'keep_first':
                                dm = 'left'
                            elif dm == 'keep_second':
                                dm = 'right'
                            tm.update_template_cache_from_edit(self.mrworker.family, self.mrworker.lang, before, after, 'approve', dm)
            except Exception:
                pass

            if hasattr(self.mrworker, 'review_response'):
                self.mrworker.review_response.emit(response_data)
            else:
                debug(self._t('log.rename_tab.worker_missing_review_response'))
                
        except Exception as e:
            debug(self._fmt('log.rename_tab.template_review_dialog_error', error=e))
            # При ошибке безопасно пропускаем кейс, не останавливая процесс
            try:
                # Логируем как ошибку, но продолжаем
                msg = self._fmt('log.rename_tab.template_review_dialog_continue', error=e)
                log_tree_add(self.rename_log_tree, datetime.now().strftime('%H:%M:%S'), None, msg, 'manual', 'error', None, None, True)
            except Exception:
                pass
            response_data = {
                'req_id': payload.get('request_id'),
                'result': 'skip',
                'skip_reason': 'dialog_error',
                'error': str(e)
            }
            if hasattr(self.mrworker, 'review_response'):
                self.mrworker.review_response.emit(response_data)
    
    def update_language(self, lang: str):
        """Обновляет язык интерфейса и настройки"""
        # Обновляем комбобокс пространств имен
        if self.parent_window:
            family = getattr(self.parent_window, 'current_family', None) or (
                getattr(getattr(self.parent_window, 'auth_tab', None), 'family_combo', None).currentText()
                if getattr(getattr(self.parent_window, 'auth_tab', None), 'family_combo', None) else 'wikipedia'
            )
            try:
                nm = getattr(self.parent_window, 'namespace_manager', None)
                if nm:
                    nm.populate_ns_combo(self.rename_ns_combo, family, lang)
            except Exception:
                pass
    
    def update_family(self, family: str):
        """Обновляет семейство проектов"""
        if self.parent_window:
            lang = getattr(self.parent_window, 'current_lang', None) or (
                getattr(getattr(self.parent_window, 'auth_tab', None), 'lang_combo', None).currentText()
                if getattr(getattr(self.parent_window, 'auth_tab', None), 'lang_combo', None) else 'ru'
            )
            try:
                nm = getattr(self.parent_window, 'namespace_manager', None)
                if nm:
                    nm.populate_ns_combo(self.rename_ns_combo, family, lang)
            except Exception:
                pass
    
    def set_auth_data(self, username: str, lang: str, family: str):
        """Установить данные авторизации"""
        self.current_user = username
        self.current_lang = lang
        self.current_family = family
    
    def clear_auth_data(self):
        """Очистить данные авторизации"""
        self.current_user = None
        self.current_lang = None
        self.current_family = None

    def set_prefix_controls_visible(self, visible: bool):
        """Показать/скрыть локальные контролы префиксов."""
        state = bool(visible)
        try:
            if getattr(self, 'prefix_label_rename', None) is not None:
                self.prefix_label_rename.setVisible(state)
        except Exception:
            pass
        try:
            if getattr(self, 'rename_ns_combo', None) is not None:
                self.rename_ns_combo.setVisible(state)
        except Exception:
            pass
        try:
            if getattr(self, 'prefix_help_btn_rename', None) is not None:
                self.prefix_help_btn_rename.setVisible(state)
        except Exception:
            pass
