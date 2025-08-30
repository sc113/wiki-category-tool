# -*- coding: utf-8 -*-
"""
Вкладка создания для Wiki Category Tool.

Этот модуль содержит компонент CreateTab, который обеспечивает:
- Загрузку TSV файла с данными для создания новых страниц
- Предпросмотр первых 5 страниц для создания
- Массовое создание новых страниц
- Настройку пространств имен и комментариев
- Проверку существования страниц перед созданием
"""

import os
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit, 
    QComboBox, QPushButton, QToolButton, QTextEdit, QSizePolicy, QProgressBar,
    QMessageBox
)
from PySide6.QtCore import Qt, Signal

from ...constants import PREFIX_TOOLTIP
from ...utils import debug, default_create_summary, format_russian_pages_nominative
from ...workers.create_worker import CreateWorker
from ...core.pywikibot_config import apply_pwb_config
from ..widgets.ui_helpers import (
    embed_button_in_lineedit, add_info_button, pick_file, 
    open_from_edit, log_message, set_start_stop_ratio,
    tsv_preview_from_path, init_progress, inc_progress,
    is_default_summary, count_non_empty_titles
)


class CreateTab(QWidget):
    """Вкладка для создания новых страниц"""
    
    # Сигналы для взаимодействия с главным окном
    language_changed = Signal(str)
    family_changed = Signal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent
        
        # Инициализация worker'а
        self.cworker = None
        
        # Данные авторизации
        self.current_user = None
        self.current_lang = None
        self.current_family = None
        
        # Создание UI
        self.setup_ui()
        
    def setup_ui(self):
        """Создает пользовательский интерфейс вкладки"""
        # Основной layout
        v = QVBoxLayout(self)
        
        # Текст справки
        create_help = (
            'TSV: Title<TAB>line1<TAB>line2…\n'
            '— Создаёт только отсутствующие страницы; существующие будут пропущены (в логе).\n\n'
            'Одна строка — одна новая страница.\n\n'
            'Пустая колонка в файле добавляет новую строку.\n\n'
            'Как формируется текст: всё после первого столбца склеивается переводами строк.\n\n'
            'Префиксы/NS: список «Префиксы» нормализует заголовок к выбранному пространству имён; «Авто» — без изменения.\n'
            'Комментарий: поле ниже применяется ко всем правкам. Малая правка при создании не используется.\n'
            'Предпросмотр: слева заголовки, справа итоговое содержимое.\n'
            'Очистка лога (метла) также очищает предпросмотр и возвращает кнопку «Предпросмотр».'
        )
        
        # Строка выбора файла и настроек
        h = QHBoxLayout()
        
        # Поле файла с кнопкой
        self.tsv_path_create = QLineEdit('categories.tsv')
        self.tsv_path_create.setMinimumWidth(0)
        self.tsv_path_create.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        
        h.addWidget(QLabel('Список для создания (.tsv):'))
        h.addWidget(self.tsv_path_create, 1)
        # Кнопка «…» справа от поля
        btn_browse_tsv_create = QToolButton()
        btn_browse_tsv_create.setText('…')
        btn_browse_tsv_create.setAutoRaise(False)
        try:
            btn_browse_tsv_create.setFixedSize(28, 28)
            btn_browse_tsv_create.setCursor(Qt.PointingHandCursor)
            btn_browse_tsv_create.setToolTip('Выбрать файл')
        except Exception:
            pass
        btn_browse_tsv_create.clicked.connect(lambda: pick_file(self, self.tsv_path_create, '*.tsv'))
        h.addWidget(btn_browse_tsv_create)
        
        # Кнопка "Открыть"
        btn_open_tsv_create = QPushButton('Открыть')
        btn_open_tsv_create.clicked.connect(lambda: open_from_edit(self, self.tsv_path_create))
        btn_open_tsv_create.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        h.addWidget(btn_open_tsv_create)
        
        # Компактный выбор префикса (выпадающий список)
        prefix_label_create = QLabel('Префиксы:')
        prefix_label_create.setToolTip(PREFIX_TOOLTIP)
        h.addWidget(prefix_label_create)
        
        self.ns_combo_create = QComboBox()
        self.ns_combo_create.setEditable(False)
        # Заполнение будет происходить при установке языка/семейства
        h.addWidget(self.ns_combo_create)
        
        # Кнопка ℹ в строке выбора файла
        add_info_button(self, h, create_help)
        
        # Строка комментария
        sum_layout = QHBoxLayout()
        sum_layout.addWidget(QLabel('Комментарий к правкам:'))
        
        self.summary_edit_create = QLineEdit()
        self.summary_edit_create.setText(default_create_summary('ru'))
        sum_layout.addWidget(self.summary_edit_create)
        
        # Предпросмотр области
        self.create_preview_titles = QTextEdit()
        self.create_preview_titles.setReadOnly(True)
        try:
            self.create_preview_titles.setLineWrapMode(QTextEdit.NoWrap)
            self.create_preview_titles.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        except Exception:
            pass
            
        self.create_preview_rest = QTextEdit()
        self.create_preview_rest.setReadOnly(True)
        try:
            self.create_preview_rest.setLineWrapMode(QTextEdit.NoWrap)
            self.create_preview_rest.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        except Exception:
            pass
        
        # Создаем алиас для совместимости с тестами
        self.create_preview_content = self.create_preview_rest
            
        # Синхронизация вертикального скролла
        try:
            a = self.create_preview_titles.verticalScrollBar()
            b = self.create_preview_rest.verticalScrollBar()
            a.valueChanged.connect(lambda v: (b.setValue(v) if b.value() != v else None))
            b.valueChanged.connect(lambda v: (a.setValue(v) if a.value() != v else None))
        except Exception:
            pass
        
        # Кнопки управления
        self.preview_create_btn = QPushButton('Предпросмотр')
        self.preview_create_btn.clicked.connect(self.preview_create)
        
        self.create_btn = QPushButton('Создать')
        self.create_btn.setEnabled(False)  # Активируется после предпросмотра
        self.create_btn.clicked.connect(self.start_create)
        
        self.create_stop_btn = QPushButton('Остановить')
        self.create_stop_btn.setEnabled(False)
        self.create_stop_btn.clicked.connect(self.stop_create)
        
        # Лог выполнения и кнопка очистки (заголовок внутри контейнера)
        self.create_log = QTextEdit()
        self.create_log.setReadOnly(True)
        
        from ..widgets.ui_helpers import create_log_wrap, make_clear_button
        create_wrap = create_log_wrap(self, self.create_log, with_header=True)
        
        def _clear_create_all():
            try:
                self.create_log.clear()
            except Exception:
                pass
            try:
                self.create_preview_titles.clear()
                self.create_preview_rest.clear()
            except Exception:
                pass
            try:
                self.create_btn.clicked.disconnect()
            except Exception:
                pass
            try:
                self.create_btn.setText('Предпросмотр')
                self.create_btn.clicked.connect(self.preview_create)
            except Exception:
                pass
                
        try:
            btn_extra = make_clear_button(self, _clear_create_all)
            from PySide6.QtWidgets import QGridLayout
            grid = create_wrap.layout() if isinstance(create_wrap.layout(), QGridLayout) else None
            if grid:
                grid.addWidget(btn_extra, grid.rowCount()-1, 0, Qt.AlignBottom | Qt.AlignRight)
        except Exception:
            pass
        
        # Левая половина: предпросмотр (две синхронные области); правая половина: лог
        content_row = QHBoxLayout()
        
        preview_wrap = QWidget()
        preview_layout = QVBoxLayout(preview_wrap)
        try:
            preview_layout.setContentsMargins(0, 0, 6, 0)
            preview_layout.setSpacing(0)
        except Exception:
            pass
        preview_layout.addWidget(QLabel('<b>Предпросмотр:</b>'))
        
        pv_split = QHBoxLayout()
        pv_split.addWidget(self.create_preview_titles, 1)
        pv_split.addWidget(self.create_preview_rest, 2)
        preview_layout.addLayout(pv_split)
        content_row.addWidget(preview_wrap, 3)
        
        # Лог справа
        log_wrap = QWidget()
        log_layout = QVBoxLayout(log_wrap)
        try:
            log_layout.setContentsMargins(6, 0, 0, 0)
        except Exception:
            pass
        log_layout.addWidget(create_wrap, 1)
        content_row.addWidget(log_wrap, 2)
        
        # Перемещаем кнопки вправо вниз под лог
        row_run = QHBoxLayout()
        # Группа прогресса: растягивается от левого края до кнопки «Создать»
        progress_wrap = QWidget()
        progress_layout = QHBoxLayout(progress_wrap)
        try:
            progress_layout.setContentsMargins(0, 0, 0, 0)
            progress_layout.setSpacing(6)
        except Exception:
            pass
        # Метка и полоса
        self.create_label = QLabel('Обработано 0/0')
        self.create_bar = QProgressBar()
        try:
            self.create_bar.setMaximum(1)
            self.create_bar.setValue(0)
            self.create_bar.setTextVisible(False)
        except Exception:
            pass
        progress_layout.addWidget(self.create_label)
        progress_layout.addWidget(self.create_bar)
        try:
            progress_layout.setStretchFactor(self.create_label, 0)
            progress_layout.setStretchFactor(self.create_bar, 1)
        except Exception:
            pass
        row_run.addWidget(progress_wrap, 1)
        row_run.addWidget(self.preview_create_btn)
        row_run.addWidget(self.create_btn)
        row_run.addWidget(self.create_stop_btn)
        
        # Добавляем все в основной layout
        v.addLayout(h)
        v.addLayout(sum_layout)
        v.addLayout(content_row, 1)
        v.addLayout(row_run)
        
        # Устанавливаем соотношение размеров кнопок
        set_start_stop_ratio(self.create_btn, self.create_stop_btn, 3)
        
    def preview_create(self):
        """Загружает TSV, показывает две колонки: первая — заголовок, остальные — склеенные через \\t"""
        path = (self.tsv_path_create.text() or '').strip()
        if not path:
            QMessageBox.warning(self, 'Ошибка', 'Укажите TSV.')
            return
            
        try:
            left, right, count = tsv_preview_from_path(path)
        except Exception as e:
            QMessageBox.critical(self, 'Ошибка', f'Не удалось прочитать TSV: {e}')
            return
            
        self.create_preview_titles.setPlainText('\n'.join(left))
        self.create_preview_rest.setPlainText('\n'.join(right))
        
        # Активируем кнопку "Создать" после предпросмотра
        self.create_btn.setEnabled(True)
        # Обновляем счетчик и шкалу прогресса по итогам предпросмотра
        init_progress(self.create_label, self.create_bar, count)
        
    def start_create(self):
        """Запускает процесс создания новых страниц"""
        debug(f'Start create: file={self.tsv_path_create.text()}')
        
        if not self.tsv_path_create.text():
            QMessageBox.warning(self, 'Ошибка', 'Укажите TSV.')
            return
        
        # Подсчитываем количество страниц для создания
        try:
            page_count = count_non_empty_titles(self.tsv_path_create.text())
        except Exception as e:
            QMessageBox.critical(self, 'Ошибка', f'Не удалось прочитать TSV: {e}')
            return
        
        if page_count == 0:
            QMessageBox.warning(self, 'Ошибка', 'В файле нет страниц для создания.')
            return
        
        # Показываем диалог подтверждения
        from ..dialogs.confirmation_dialog import ConfirmationDialog
        if not ConfirmationDialog.confirm_operation("создать", page_count, self):
            return
            
        # Получаем данные авторизации от родительского окна
        if not self.parent_window:
            QMessageBox.warning(self, 'Ошибка', 'Нет доступа к данным авторизации.')
            return
            
        # Получаем данные из родительского окна (будет реализовано в main_window)
        user = getattr(self.parent_window, 'current_user', None)
        pwd = getattr(self.parent_window, 'current_password', None)
        lang = getattr(self.parent_window, 'current_lang', 'ru')
        fam = getattr(self.parent_window, 'current_family', 'wikipedia')
        
        if not user or not pwd:
            QMessageBox.warning(self, 'Ошибка', 'Необходимо войти в систему.')
            return
            
        apply_pwb_config(lang, fam)
        
             
        summary = self.summary_edit_create.text().strip()
        if not summary:
            summary = default_create_summary(lang)
            
        # Малая правка НЕ применяется при создании (minor=False)
        minor = False
        
        # Блокируем кнопки и очищаем лог
        self.preview_create_btn.setEnabled(False)
        self.create_btn.setEnabled(False)
        self.create_stop_btn.setEnabled(True)
        # Настраиваем прогресс-бар
        init_progress(self.create_label, self.create_bar, page_count)
        
        ns_sel = self.ns_combo_create.currentData()
        
        # Создаем и запускаем worker
        self.cworker = CreateWorker(
            self.tsv_path_create.text(), user, pwd, lang, fam, ns_sel, summary, minor
        )
        self.cworker.progress.connect(lambda m: [inc_progress(self.create_label, self.create_bar), log_message(self.create_log, m)])
        self.cworker.finished.connect(self._on_create_finished)
        self.cworker.start()
        
    def stop_create(self):
        """Останавливает процесс создания"""
        w = getattr(self, 'cworker', None)
        if w and w.isRunning():
            w.request_stop()
            
    def _on_create_finished(self):
        """Обработчик завершения процесса создания"""
        self.preview_create_btn.setEnabled(True)
        self.create_btn.setEnabled(True)
        self.create_stop_btn.setEnabled(False)
        msg = 'Остановлено!' if getattr(self, 'cworker', None) and getattr(self.cworker, '_stop', False) else 'Создание завершено!'
        log_message(self.create_log, msg)
        init_progress(self.create_label, self.create_bar, 0)

    def _inc_create_prog(self):
        """Увеличение значения прогресс-бара создания"""
        inc_progress(self.create_label, self.create_bar)
        
    def update_language(self, lang: str):
        """Обновляет язык интерфейса и настройки"""
        # Обновляем комментарий по умолчанию
        if is_default_summary(self.summary_edit_create.text(), default_create_summary):
            self.summary_edit_create.setText(default_create_summary(lang))
            
        # Обновляем комбобокс пространств имен
        if self.parent_window:
            family = getattr(self.parent_window, 'current_family', None) or (
                getattr(getattr(self.parent_window, 'auth_tab', None), 'family_combo', None).currentText()
                if getattr(getattr(self.parent_window, 'auth_tab', None), 'family_combo', None) else 'wikipedia'
            )
            try:
                nm = getattr(self.parent_window, 'namespace_manager', None)
                if nm:
                    nm.populate_ns_combo(self.ns_combo_create, family, lang)
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
                    nm.populate_ns_combo(self.ns_combo_create, family, lang)
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
    
    def update_namespace_combo(self, family: str, lang: str):
        """Обновление комбобокса пространств имен для текущего языка/проекта"""
        try:
            nm = getattr(self.parent_window, 'namespace_manager', None)
            if nm:
                nm.populate_ns_combo(self.ns_combo_create, family, lang)
        except Exception:
            pass
    
    def update_summary(self, lang: str):
        """Автообновление summary при смене языка"""
        self.update_language(lang)
    
    # Локальные validate_tsv/check_tsv_format удалены — используйте хелперы из ui_helpers
    
    def check_page_exists(self, title: str, lang: str, family: str) -> bool:
        """Проверка существования страницы"""
        # Эта функция будет использоваться в CreateWorker
        # Здесь просто заглушка для интерфейса
        return False
    
    def warn_existing_pages(self, existing_pages: list) -> bool:
        """Предупреждение о существующих страницах"""
        if not existing_pages:
            return True
        
        msg = f"Следующие страницы уже существуют:\n\n"
        msg += "\n".join(existing_pages[:10])  # Показываем первые 10
        if len(existing_pages) > 10:
            rest = len(existing_pages) - 10
            msg += f"\n... и еще {format_russian_pages_nominative(rest)}"
        msg += "\n\nПродолжить создание остальных страниц?"
        
        reply = QMessageBox.question(
            self, 'Страницы уже существуют', msg,
            QMessageBox.Yes | QMessageBox.No
        )
        return reply == QMessageBox.Yes
    
    # Константа для лимита предпросмотра (убрана - показываем все строки)
    # PREVIEW_LIMIT = 5
    
    # Константа для отключения малой правки при создании
    MINOR_EDIT_DISABLED = True