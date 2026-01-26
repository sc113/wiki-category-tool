# -*- coding: utf-8 -*-
"""
Вкладка перезаписи для Wiki Category Tool.

Этот модуль содержит компонент ReplaceTab, который обеспечивает:
- Загрузку TSV файла с данными для замены
- Предпросмотр первых 5 страниц
- Массовую перезапись содержимого страниц
- Настройку пространств имен и комментариев
- Отметку правок как малые (minor edit)
"""

import os
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QGridLayout, QLabel, QLineEdit,
    QComboBox, QPushButton, QToolButton, QTextEdit, QCheckBox, QProgressBar,
    QMessageBox, QSizePolicy
)
from PySide6.QtCore import Qt, Signal

from ...constants import PREFIX_TOOLTIP
from ...utils import debug, default_summary
from ...workers.replace_worker import ReplaceWorker
from ...core.pywikibot_config import apply_pwb_config
from ..widgets.ui_helpers import (
    embed_button_in_lineedit, add_info_button, pick_file,
    open_from_edit, log_message, set_start_stop_ratio,
    tsv_preview_from_path, init_progress, inc_progress,
    count_non_empty_titles, is_default_summary
)


class ReplaceTab(QWidget):
    """Вкладка для перезаписи содержимого страниц"""

    # Сигналы для взаимодействия с главным окном
    language_changed = Signal(str)
    family_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent

        # Инициализация worker'а
        self.rworker = None

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
        replace_help = (
            'TSV: Title<TAB>line1<TAB>line2…\n'
            '— Перезаписывает существующие страницы; если страницы нет — в логе «страница отсутствует».\n\n'
            'Одна строка — одна новая страница.\n\n'
            'Пустая колонка в файле добавляет новую строку.\n\n'
            'Как формируется текст: всё после первого столбца склеивается переводами строк.\n\n'
            'Префиксы/NS: список «Префиксы» нормализует заголовок к выбранному пространству имён; «Авто» — без изменения.\n'
            'Комментарий: поле ниже применяется ко всем правкам. «Малая правка» — отметит правки как minor.\n'
            'Предпросмотр: слева заголовки, справа итоговое содержимое.\n'
            'Очистка лога (метла) также очищает предпросмотр и возвращает кнопку «Предпросмотр».'
        )

        # Строка выбора файла и настроек
        h = QHBoxLayout()

        # Поле файла с кнопкой
        self.rep_file_edit = QLineEdit('categories.tsv')
        self.rep_file_edit.setMinimumWidth(0)
        self.rep_file_edit.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Preferred)

        h.addWidget(QLabel('Список для замен (.tsv):'))
        h.addWidget(self.rep_file_edit, 1)
        # Кнопка «…» справа
        btn_browse_rep = QToolButton()
        btn_browse_rep.setText('…')
        btn_browse_rep.setAutoRaise(False)
        try:
            btn_browse_rep.setFixedSize(28, 28)
            btn_browse_rep.setCursor(Qt.PointingHandCursor)
            btn_browse_rep.setToolTip('Выбрать файл')
        except Exception:
            pass
        btn_browse_rep.clicked.connect(
            lambda: pick_file(self, self.rep_file_edit, '*.tsv'))
        h.addWidget(btn_browse_rep)

        # Кнопка "Открыть"
        btn_open_tsv = QPushButton('Открыть')
        btn_open_tsv.clicked.connect(
            lambda: open_from_edit(self, self.rep_file_edit))
        btn_open_tsv.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        h.addWidget(btn_open_tsv)

        # Компактный выбор префикса (выпадающий список)
        prefix_label_replace = QLabel('Префиксы:')
        prefix_label_replace.setToolTip(PREFIX_TOOLTIP)
        h.addWidget(prefix_label_replace)

        self.rep_ns_combo = QComboBox()
        self.rep_ns_combo.setEditable(False)
        # Заполнение будет происходить при установке языка/семейства
        h.addWidget(self.rep_ns_combo)

        # Кнопка ℹ в строке выбора файла
        add_info_button(self, h, replace_help)

        # Строка комментария
        sum_layout = QHBoxLayout()
        sum_layout.addWidget(QLabel('Комментарий к правкам:'))

        self.summary_edit = QLineEdit()
        self.summary_edit.setText(default_summary('ru'))
        sum_layout.addWidget(self.summary_edit)

        # Малая правка
        self.minor_checkbox = QCheckBox('Малая правка')
        sum_layout.addWidget(self.minor_checkbox)

        # Предпросмотр области
        self.rep_preview_titles = QTextEdit()
        self.rep_preview_titles.setReadOnly(True)
        try:
            self.rep_preview_titles.setLineWrapMode(QTextEdit.NoWrap)
            self.rep_preview_titles.setHorizontalScrollBarPolicy(
                Qt.ScrollBarAsNeeded)
        except Exception:
            pass

        self.rep_preview_rest = QTextEdit()
        self.rep_preview_rest.setReadOnly(True)
        try:
            self.rep_preview_rest.setLineWrapMode(QTextEdit.NoWrap)
            self.rep_preview_rest.setHorizontalScrollBarPolicy(
                Qt.ScrollBarAsNeeded)
        except Exception:
            pass

        # Синхронизация вертикального скролла
        try:
            a = self.rep_preview_titles.verticalScrollBar()
            b = self.rep_preview_rest.verticalScrollBar()
            a.valueChanged.connect(lambda v: (
                b.setValue(v) if b.value() != v else None))
            b.valueChanged.connect(lambda v: (
                a.setValue(v) if a.value() != v else None))
        except Exception:
            pass

        # Кнопки управления
        self.preview_btn = QPushButton('Предпросмотр')
        self.preview_btn.clicked.connect(self.preview_replace)

        self.replace_btn = QPushButton('Заменить')
        self.replace_btn.setEnabled(False)  # Активируется после предпросмотра
        self.replace_btn.clicked.connect(self.start_replace)

        self.replace_stop_btn = QPushButton('Остановить')
        self.replace_stop_btn.setEnabled(False)
        self.replace_stop_btn.clicked.connect(self.stop_replace)

        # Лог выполнения и кнопка очистки (заголовок внутри контейнера)
        self.rep_log = QTextEdit()
        self.rep_log.setReadOnly(True)

        from ..widgets.ui_helpers import create_log_wrap, make_clear_button
        rep_wrap = create_log_wrap(self, self.rep_log, with_header=True)

        def _clear_rep_all():
            try:
                self.rep_log.clear()
            except Exception:
                pass
            try:
                self.rep_preview_titles.clear()
                self.rep_preview_rest.clear()
            except Exception:
                pass
            try:
                self.preview_btn.clicked.disconnect()
            except Exception:
                pass
            try:
                self.preview_btn.setText('Предпросмотр')
                self.preview_btn.clicked.connect(self.preview_replace)
                self.replace_btn.setEnabled(False)
            except Exception:
                pass

        # Дополнительно подвяжем кнопку очистки предпросмотра к швабре в лог-контейнере
        # (лог уже чистится внутри create_log_wrap)
        try:
            btn_extra = make_clear_button(self, _clear_rep_all)
            # Добавим вторую кнопку поверх основной в том же углу
            from PySide6.QtWidgets import QGridLayout
            grid = rep_wrap.layout() if isinstance(rep_wrap.layout(), QGridLayout) else None
            if grid:
                grid.addWidget(btn_extra, grid.rowCount()-1, 0,
                               Qt.AlignBottom | Qt.AlignRight)
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

        pv_split = QSplitter(Qt.Horizontal)
        pv_split.addWidget(self.rep_preview_titles)
        pv_split.addWidget(self.rep_preview_rest)
        pv_split.setStretchFactor(0, 1)
        pv_split.setStretchFactor(1, 2)
        preview_layout.addWidget(pv_split, 1)
        content_row.addWidget(preview_wrap, 3)

        # Лог справа
        log_wrap = QWidget()
        log_layout = QVBoxLayout(log_wrap)
        try:
            log_layout.setContentsMargins(6, 0, 0, 0)
        except Exception:
            pass
        log_layout.addWidget(rep_wrap, 1)
        content_row.addWidget(log_wrap, 2)

        # Перемещаем кнопки вправо вниз под лог
        row_run = QHBoxLayout()
        # Группа прогресса: растягивается от левого края до кнопки «Заменить»
        progress_wrap = QWidget()
        progress_layout = QHBoxLayout(progress_wrap)
        try:
            progress_layout.setContentsMargins(0, 0, 0, 0)
            progress_layout.setSpacing(6)
        except Exception:
            pass
        # Метка и полоса
        self.replace_label = QLabel('Обработано 0/0')
        self.replace_bar = QProgressBar()
        try:
            self.replace_bar.setMaximum(1)
            self.replace_bar.setValue(0)
            self.replace_bar.setTextVisible(False)
        except Exception:
            pass
        progress_layout.addWidget(self.replace_label)
        progress_layout.addWidget(self.replace_bar)
        try:
            progress_layout.setStretchFactor(self.replace_label, 0)
            progress_layout.setStretchFactor(self.replace_bar, 1)
        except Exception:
            pass
        row_run.addWidget(progress_wrap, 1)
        row_run.addWidget(self.preview_btn)
        row_run.addWidget(self.replace_btn)
        row_run.addWidget(self.replace_stop_btn)

        # Добавляем все в основной layout
        v.addLayout(h)
        v.addLayout(sum_layout)
        v.addLayout(content_row, 1)
        v.addLayout(row_run)

        # Устанавливаем соотношение размеров кнопок
        set_start_stop_ratio(self.replace_btn, self.replace_stop_btn, 3)

    def preview_replace(self):
        """Загружает TSV, показывает две колонки: первая — заголовок, остальные — склеенные через \\t"""
        path = (self.rep_file_edit.text() or '').strip()
        if not path:
            QMessageBox.warning(self, 'Ошибка', 'Укажите TSV.')
            return

        try:
            left, right, count = tsv_preview_from_path(path)
        except Exception as e:
            QMessageBox.critical(
                self, 'Ошибка', f'Не удалось прочитать TSV: {e}')
            return

        self.rep_preview_titles.setPlainText('\n'.join(left))
        self.rep_preview_rest.setPlainText('\n'.join(right))

        # Активируем кнопку "Заменить" после предпросмотра
        self.replace_btn.setEnabled(True)
        # Обновляем счетчик и шкалу прогресса по итогам предпросмотра
        init_progress(self.replace_label, self.replace_bar, count)

    def start_replace(self):
        """Запускает процесс замены содержимого страниц"""
        debug(f'Start replace: file={self.rep_file_edit.text()}')

        if not self.rep_file_edit.text():
            QMessageBox.warning(self, 'Ошибка', 'Укажите TSV.')
            return

        # Подсчитываем количество страниц для замены
        try:
            page_count = count_non_empty_titles(self.rep_file_edit.text())
        except Exception as e:
            QMessageBox.critical(
                self, 'Ошибка', f'Не удалось прочитать TSV: {e}')
            return

        if page_count == 0:
            QMessageBox.warning(
                self, 'Ошибка', 'В файле нет страниц для замены.')
            return

        # Получаем данные авторизации от родительского окна
        if not self.parent_window:
            QMessageBox.warning(
                self, 'Ошибка', 'Нет доступа к данным авторизации.')
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

        summary = self.summary_edit.text().strip()

        minor = self.minor_checkbox.isChecked()

        # Блокируем кнопки и очищаем лог
        self.replace_btn.setEnabled(False)
        self.replace_stop_btn.setEnabled(True)
        self.rep_log.clear()
        # Настраиваем прогресс-бар
        init_progress(self.replace_label, self.replace_bar, page_count)

        ns_sel = self.rep_ns_combo.currentData()

        # Создаем и запускаем worker
        self.rworker = ReplaceWorker(
            self.rep_file_edit.text(), user, pwd, lang, fam, ns_sel, summary, minor
        )
        self.rworker.progress.connect(lambda m: [inc_progress(
            self.replace_label, self.replace_bar), log_message(self.rep_log, m)])
        self.rworker.finished.connect(self._on_replace_finished)
        self.rworker.start()

    def stop_replace(self):
        """Останавливает процесс замены"""
        w = getattr(self, 'rworker', None)
        if w and w.isRunning():
            w.request_stop()

    def _on_replace_finished(self):
        """Обработчик завершения процесса замены"""
        self.preview_btn.setEnabled(True)
        self.replace_btn.setEnabled(True)
        self.replace_stop_btn.setEnabled(False)
        init_progress(self.replace_label, self.replace_bar, 0)

    def _inc_replace_prog(self):
        """Увеличение значения прогресс-бара перезаписи"""
        try:
            val = self.replace_bar.value() + 1
            self.replace_bar.setValue(val)
            try:
                self.replace_label.setText(
                    f'Обработано {val}/{self.replace_bar.maximum()}')
            except Exception:
                pass
        except Exception:
            pass

    def update_language(self, lang: str):
        """Обновляет язык интерфейса и настройки"""
        # Обновляем комментарий по умолчанию
        if is_default_summary(self.summary_edit.text(), default_summary):
            self.summary_edit.setText(default_summary(lang))

        # Обновляем комбобокс пространств имен
        if self.parent_window:
            family = getattr(self.parent_window, 'current_family', None) or (
                getattr(getattr(self.parent_window, 'auth_tab', None),
                        'family_combo', None).currentText()
                if getattr(getattr(self.parent_window, 'auth_tab', None), 'family_combo', None) else 'wikipedia'
            )
            try:
                nm = getattr(self.parent_window, 'namespace_manager', None)
                if nm:
                    nm.populate_ns_combo(self.rep_ns_combo, family, lang)
            except Exception:
                pass

    def update_family(self, family: str):
        """Обновляет семейство проектов"""
        if self.parent_window:
            lang = getattr(self.parent_window, 'current_lang', None) or (
                getattr(getattr(self.parent_window, 'auth_tab', None),
                        'lang_combo', None).currentText()
                if getattr(getattr(self.parent_window, 'auth_tab', None), 'lang_combo', None) else 'ru'
            )
            try:
                nm = getattr(self.parent_window, 'namespace_manager', None)
                if nm:
                    nm.populate_ns_combo(self.rep_ns_combo, family, lang)
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
                nm.populate_ns_combo(self.rep_ns_combo, family, lang)
        except Exception:
            pass

    def update_summary(self, lang: str):
        """Автообновление summary при смене языка"""
        self.update_language(lang)
