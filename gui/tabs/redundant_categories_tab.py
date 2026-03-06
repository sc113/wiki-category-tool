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
from ...utils import (
    debug,
    default_redundant_category_summary,
    default_redundant_category_multi_summary,
    default_redundant_category_pair_format,
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
        self._action_handler = None
        self.setup_ui()

    def setup_ui(self):
        main_layout = QHBoxLayout(self)

        source_help = (
            'Левая панель работает так же, как во вкладке «Чтение».\n'
            'Можно получить страницы из категории, вручную вставить список или загрузить .txt.\n'
            'Префиксы/NS нормализуют заголовки страниц для обработки; «Авто» оставляет список без изменений.'
        )
        pairs_help = (
            'Формат файла пар: точная_категория<TAB>широкая_категория.\n'
            'Левая колонка — точная категория, правая — широкая, которую нужно удалить.\n'
            'Префикс категории необязателен: можно писать и с ним, и без него.\n'
            'Допустим локальный префикс проекта или Category:, но обычно удобнее хранить названия без префикса.\n'
            'Не используйте [[Категория:...]] / [[Category:...]] и не добавляйте ключ сортировки после "|".\n'
            'Для одной точной категории можно указать несколько широких категорий отдельными строками.\n'
            'Предпросмотр в центре показывает сам TSV, а не результат по страницам.'
        )
        summary_help = (
            'Есть два шаблона комментария к правке.\n'
            'Если на странице удаляется одна широкая категория, используется поле «Коммент при 1 удалении».\n'
            'Если удаляется несколько категорий сразу, используется поле «Коммент при нескольких», '
            'а {pair} заменяется списком пар из поля «Пара». Пары всегда разделяются запятой.\n'
            'Переменные:\n'
            '{title_broad} — название широкой категории без префикса.\n'
            '{title_precise} — название точной категории без префикса.\n'
            '{link_broad} — викиссылка на широкую категорию с локальным префиксом проекта.\n'
            '{link_precise} — викиссылка на точную категорию с локальным префиксом проекта.\n'
            'Для множественного шаблона доступны {pair} и {count}.\n'
            '{pair} — список пар, собранный из поля «Пара».\n'
            '{count} — число удаляемых широких категорий в этой правке.\n'
            'Поле «Пара» — это шаблон одной пары; он повторяется для каждого удаления.\n'
            'Пример для «Коммент при 1 удалении»: Удалена {link_broad}, так как существует более точная {link_precise}\n'
            'Пример для «Коммент при нескольких»: Удалены категории, так как существуют более точные: {pair}.\n'
            'Пример для «Пара»: {link_broad} → {link_precise}'
        )
        settings_help = pairs_help + '\n\n' + summary_help

        self.source_panel = CategorySourcePanel(
            self,
            parent_window=self.parent_window,
            help_text=source_help,
            group_title='Источник страниц',
            category_section_label='<b>Получить содержимое категории:</b>',
            category_placeholder='Название корневой категории',
            manual_label='<b>Список страниц для обработки:</b>',
            manual_placeholder='Список страниц (по одной на строку)',
            file_section_label='<b>Или загрузить список из файла:</b>',
            file_caption='Файл (.txt):',
        )
        self.redundant_ns_combo = self.source_panel.ns_combo
        self.manual_list = self.source_panel.manual_list
        self.list_edit = self.source_panel.list_edit
        self.in_path = self.source_panel.in_path
        self.file_edit = self.source_panel.file_edit
        main_layout.addWidget(self.source_panel, 3)

        right_side = QWidget()
        right_layout = QVBoxLayout(right_side)
        try:
            right_layout.setContentsMargins(6, 0, 0, 0)
            right_layout.setSpacing(8)
        except Exception:
            pass

        settings_group = QGroupBox('Пары категорий и комментарии')
        settings_group.setStyleSheet(
            "QGroupBox { border: 1px solid lightgray; border-radius: 5px; margin-top: 10px; } "
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px 0 5px; }"
        )
        settings_layout = QVBoxLayout(settings_group)
        try:
            settings_layout.setContentsMargins(8, 12, 8, 8)
            settings_layout.setSpacing(6)
        except Exception:
            pass

        pairs_row = QHBoxLayout()
        pairs_row.addWidget(QLabel('Пары категорий (.tsv):'))

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
            btn_browse_pairs.setFixedSize(28, 28)
            btn_browse_pairs.setToolTip('Выбрать файл')
        except Exception:
            pass
        btn_browse_pairs.clicked.connect(
            lambda: pick_file(self, self.categories_path_edit, '*.tsv'))
        pairs_row.addWidget(btn_browse_pairs)

        btn_open_pairs = QPushButton('Открыть')
        btn_open_pairs.clicked.connect(
            lambda: open_from_edit(self, self.categories_path_edit))
        pairs_row.addWidget(btn_open_pairs)
        add_info_button(self, pairs_row, settings_help)
        settings_layout.addLayout(pairs_row)

        single_row = QHBoxLayout()
        single_row.addWidget(QLabel('Коммент при 1 удалении:'))
        self.redundant_summary_single_edit = QLineEdit()
        self.redundant_summary_single_edit.setText(
            default_redundant_category_summary('ru'))
        self._configure_static_left_text(self.redundant_summary_single_edit)
        single_row.addWidget(self.redundant_summary_single_edit, 1)
        settings_layout.addLayout(single_row)

        multi_row = QHBoxLayout()
        multi_row.addWidget(QLabel('Коммент при нескольких:'))
        self.redundant_summary_multi_edit = QLineEdit()
        self.redundant_summary_multi_edit.setText(
            default_redundant_category_multi_summary('ru'))
        self._configure_static_left_text(self.redundant_summary_multi_edit)
        multi_row.addWidget(self.redundant_summary_multi_edit, 2)

        pair_label = QLabel('Пара:')
        multi_row.addWidget(pair_label)
        self.redundant_pair_format_edit = QLineEdit()
        self.redundant_pair_format_edit.setText(
            default_redundant_category_pair_format('ru'))
        self._configure_static_left_text(self.redundant_pair_format_edit)
        try:
            self.redundant_pair_format_edit.setMaximumWidth(320)
        except Exception:
            pass
        multi_row.addWidget(self.redundant_pair_format_edit, 1)
        settings_layout.addLayout(multi_row)

        right_layout.addWidget(settings_group)

        content_row = QHBoxLayout()

        preview_wrap = QWidget()
        preview_layout = QVBoxLayout(preview_wrap)
        try:
            preview_layout.setContentsMargins(0, 0, 6, 0)
        except Exception:
            pass
        self.preview_panel = TsvPreviewPanel(
            self,
            header_text='<b>Предпросмотр пар категорий:</b>',
            left_header='Точная категория',
            right_header='Широкая категория',
            left_stretch=1,
            right_stretch=1,
        )
        self.preview_titles = self.preview_panel.titles_edit
        self.preview_rest = self.preview_panel.content_edit
        preview_layout.addWidget(self.preview_panel, 1)
        content_row.addWidget(preview_wrap, 1)

        log_wrap = QWidget()
        log_layout = QVBoxLayout(log_wrap)
        try:
            log_layout.setContentsMargins(6, 0, 0, 0)
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
        content_row.addWidget(log_wrap, 1)

        right_layout.addLayout(content_row, 1)

        controls_row = QHBoxLayout()
        self.progress_label = QLabel('Обработано 0/0')
        self.progress_bar = QProgressBar()
        try:
            self.progress_bar.setMaximum(1)
            self.progress_bar.setValue(0)
            self.progress_bar.setTextVisible(False)
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

        self.action_btn = QPushButton()
        controls_row.addWidget(self.action_btn)

        self.stop_btn = QPushButton('Остановить')
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_run)
        controls_row.addWidget(self.stop_btn)
        set_start_stop_ratio(self.action_btn, self.stop_btn, 3)

        right_layout.addLayout(controls_row)

        main_layout.addWidget(right_side, 7)
        self.source_panel.set_log_widget(self.run_log)
        self._set_action_mode_preview()

    def _set_action_mode_preview(self):
        self._preview_ready = False
        self._set_action_handler('Предпросмотр', self.preview_pairs)

    def _set_action_mode_run(self):
        self._preview_ready = True
        self._set_action_handler('Удалять категории', self.start_run)

    def _set_action_handler(self, text: str, handler):
        try:
            if self._action_handler is not None:
                self.action_btn.clicked.disconnect(self._action_handler)
        except Exception:
            pass
        self._action_handler = handler
        self.action_btn.setText(text)
        self.action_btn.setEnabled(True)
        self.action_btn.clicked.connect(handler)

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
        path = (self.categories_path_edit.text() or '').strip()
        if not path:
            QMessageBox.warning(self, 'Ошибка', 'Укажите TSV с парами категорий.')
            return

        ok, message = check_tsv_format(path)
        if not ok:
            QMessageBox.warning(self, 'Ошибка', message)
            return

        try:
            left, right, count = tsv_preview_from_path(path)
        except Exception as exc:
            QMessageBox.critical(
                self, 'Ошибка', f'Не удалось прочитать TSV: {exc}')
            return

        self.preview_panel.set_preview(left, right)
        if count > 0:
            self._set_action_mode_run()

    def start_run(self):
        path = (self.categories_path_edit.text() or '').strip()
        if not path:
            QMessageBox.warning(self, 'Ошибка', 'Укажите TSV с парами категорий.')
            return

        ok, message = check_tsv_format(path)
        if not ok:
            QMessageBox.warning(self, 'Ошибка', message)
            return

        try:
            titles = self.source_panel.load_titles_from_inputs()
        except Exception as exc:
            QMessageBox.critical(self, 'Ошибка', str(exc))
            return

        if not titles:
            QMessageBox.warning(
                self, 'Ошибка', 'Не указан ни файл со списком страниц, ни текст списка.')
            return

        user = getattr(self.parent_window, 'current_user', None)
        pwd = getattr(self.parent_window, 'current_password', None)
        lang = getattr(self.parent_window, 'current_lang', 'ru')
        fam = getattr(self.parent_window, 'current_family', 'wikipedia')

        if not user or not pwd:
            QMessageBox.warning(self, 'Ошибка', 'Необходимо войти в систему.')
            return

        apply_pwb_config(lang, fam)

        self.action_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.run_log.clear()
        init_progress(self.progress_label, self.progress_bar, len(titles))

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
            lambda message: log_message(self.run_log, message, debug))
        self.rcworker.page_done.connect(
            lambda: inc_progress(self.progress_label, self.progress_bar))
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
        if getattr(self, 'rcworker', None) and getattr(self.rcworker, '_stop', False):
            log_message(self.run_log, 'Остановлено!', debug)
        else:
            log_message(self.run_log, 'Готово!', debug)

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
