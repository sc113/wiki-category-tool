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

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QComboBox, QPushButton, QToolButton, QTextEdit, QSizePolicy, QProgressBar,
    QMessageBox
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont

from ...constants import PREFIX_TOOLTIP
from ...core.localization import translate_key
from ...utils import debug, default_create_summary
from ...workers.create_worker import CreateWorker
from ...core.pywikibot_config import apply_pwb_config
from ..widgets.shared_panels import TsvPreviewPanel
from ..widgets.ui_helpers import (
    add_info_button, pick_file,
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
        """Создает пользовательский интерфейс вкладки"""
        # Основной layout
        v = QVBoxLayout(self)
        try:
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(6)
        except Exception:
            pass

        # Текст справки
        create_help = self._t('help.create.main')

        # Строка выбора файла и настроек
        h = QHBoxLayout()
        try:
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(6)
        except Exception:
            pass

        # Поле файла с кнопкой
        self.tsv_path_create = QLineEdit('categories.tsv')
        self.tsv_path_create.setMinimumWidth(0)
        self.tsv_path_create.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Preferred)

        h.addWidget(QLabel(self._t('ui.create_list_tsv')))
        h.addWidget(self.tsv_path_create, 1)
        # Кнопка «…» справа от поля
        btn_browse_tsv_create = QToolButton()
        btn_browse_tsv_create.setText('…')
        btn_browse_tsv_create.setAutoRaise(False)
        try:
            btn_browse_tsv_create.setFixedSize(27, 27)
            btn_browse_tsv_create.setCursor(Qt.PointingHandCursor)
            btn_browse_tsv_create.setToolTip(self._t('ui.choose_file'))
        except Exception:
            pass
        btn_browse_tsv_create.clicked.connect(
            lambda: pick_file(self, self.tsv_path_create, '*.tsv'))
        h.addWidget(btn_browse_tsv_create)

        # Кнопка "Открыть"
        btn_open_tsv_create = QPushButton(self._t('ui.open'))
        btn_open_tsv_create.clicked.connect(
            lambda: open_from_edit(self, self.tsv_path_create))
        btn_open_tsv_create.setSizePolicy(
            QSizePolicy.Fixed, QSizePolicy.Preferred)
        h.addWidget(btn_open_tsv_create)

        # Компактный выбор префикса (выпадающий список)
        self.prefix_label_create = QLabel(self._t('ui.prefixes'))
        self.prefix_label_create.setToolTip(PREFIX_TOOLTIP)
        h.addWidget(self.prefix_label_create)

        self.ns_combo_create = QComboBox()
        self.ns_combo_create.setEditable(False)
        # Заполнение будет происходить при установке языка/семейства
        h.addWidget(self.ns_combo_create)

        # Кнопка ℹ в строке выбора файла
        self.prefix_help_btn_create = add_info_button(self, h, create_help)

        # Строка комментария
        sum_layout = QHBoxLayout()
        sum_layout.addWidget(QLabel(self._t('ui.edit_summary')))

        self.summary_edit_create = QLineEdit()
        self.summary_edit_create.setText(default_create_summary('ru'))
        sum_layout.addWidget(self.summary_edit_create)

        self.preview_panel = TsvPreviewPanel(
            self,
            left_header=self._t('ui.page_title'),
            right_header=self._t('ui.content_to_create'),
        )
        self.create_preview_titles = self.preview_panel.titles_edit
        self.create_preview_rest = self.preview_panel.content_edit
        self.create_preview_content = self.create_preview_rest

        # Кнопки управления
        self.preview_create_btn = QPushButton(self._t('ui.preview'))
        self.preview_create_btn.clicked.connect(self.preview_create)

        self.create_btn = QPushButton(self._t('ui.create_button'))
        self.create_btn.setEnabled(False)  # Активируется после предпросмотра
        self.create_btn.clicked.connect(self.start_create)

        self.create_stop_btn = QPushButton(self._t('ui.stop'))
        self.create_stop_btn.setEnabled(False)
        self.create_stop_btn.clicked.connect(self.stop_create)

        # Лог выполнения и кнопка очистки (заголовок внутри контейнера)
        self.create_log = QTextEdit()
        self.create_log.setReadOnly(True)
        mono_font = QFont('Consolas', 9)
        if not mono_font.exactMatch():
            mono_font = QFont('Courier New', 9)
        mono_font.setFixedPitch(True)
        self.create_log.setFont(mono_font)

        from ..widgets.ui_helpers import create_log_wrap, make_clear_button
        create_wrap = create_log_wrap(self, self.create_log, with_header=True)

        def _clear_create_all():
            try:
                self.create_log.clear()
            except Exception:
                pass
            try:
                self.preview_panel.clear()
            except Exception:
                pass
            try:
                # Возвращаем кнопку "Создать" в исходное состояние
                self.create_btn.setEnabled(False)
                self.create_btn.setText(self._t('ui.create_button'))
            except Exception:
                pass

        try:
            btn_extra = make_clear_button(self, _clear_create_all)
            from PySide6.QtWidgets import QGridLayout
            grid = create_wrap.layout() if isinstance(
                create_wrap.layout(), QGridLayout) else None
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
        preview_layout.addWidget(self.preview_panel, 1)
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
        self.create_label = QLabel(
            self._t('ui.processed_counter_initial')
        )
        try:
            self.create_label.setVisible(False)
        except Exception:
            pass
        self.create_bar = QProgressBar()
        try:
            self.create_bar.setMaximum(1)
            self.create_bar.setValue(0)
            self.create_bar.setTextVisible(True)
            self.create_bar.setFormat(
                self._t('ui.processed_counter_initial')
            )
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
            QMessageBox.warning(self, self._t('ui.error'), self._t('ui.specify_tsv'))
            return

        try:
            left, right, count = tsv_preview_from_path(path)
        except Exception as e:
            QMessageBox.critical(
                self, self._t('ui.error'), self._fmt('ui.failed_read_tsv', error=e))
            return

        self.preview_panel.set_preview(left, right)

        # Активируем кнопку "Создать" после предпросмотра
        self.create_btn.setEnabled(True)
        # Обновляем счетчик и шкалу прогресса по итогам предпросмотра
        init_progress(self.create_label, self.create_bar, count)

    def start_create(self):
        """Запускает процесс создания новых страниц"""
        debug(f'Start create: file={self.tsv_path_create.text()}')

        if not self.tsv_path_create.text():
            QMessageBox.warning(self, self._t('ui.error'), self._t('ui.specify_tsv'))
            return

        # Подсчитываем количество страниц для создания
        try:
            page_count = count_non_empty_titles(self.tsv_path_create.text())
        except Exception as e:
            QMessageBox.critical(
                self, self._t('ui.error'), self._fmt('ui.failed_read_tsv', error=e))
            return

        if page_count == 0:
            QMessageBox.warning(
                self, self._t('ui.error'), self._t('ui.create.no_pages_in_file'))
            return

        # Получаем данные авторизации от родительского окна
        if not self.parent_window:
            QMessageBox.warning(
                self, self._t('ui.error'), self._t('ui.no_access_auth_data'))
            return

        # Получаем данные из родительского окна (будет реализовано в main_window)
        user = getattr(self.parent_window, 'current_user', None)
        pwd = getattr(self.parent_window, 'current_password', None)
        lang = getattr(self.parent_window, 'current_lang', 'ru')
        fam = getattr(self.parent_window, 'current_family', 'wikipedia')

        if not user or not pwd:
            QMessageBox.warning(self, self._t('ui.error'), self._t('ui.you_need_to_sign_in'))
            return

        apply_pwb_config(lang, fam)

        summary = self.summary_edit_create.text().strip()

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
        log_message(
            self.create_log,
            self._fmt('log.create.run_started', pages=page_count, lang=lang, family=fam, ns=ns_sel),
        )
        self.cworker.progress.connect(lambda m: [inc_progress(
            self.create_label, self.create_bar), log_message(self.create_log, m)])
        self.cworker.finished.connect(self._on_create_finished)
        self.cworker.start()

    def stop_create(self):
        """Останавливает процесс создания"""
        w = getattr(self, 'cworker', None)
        if w and w.isRunning():
            w.request_stop()

    def _on_create_finished(self):
        """Обработчик завершения процесса создания"""
        worker = getattr(self, 'cworker', None)
        stopped = bool(worker and getattr(worker, '_stop', False))
        stats = {}
        try:
            stats = dict(getattr(worker, 'stats', {}) or {})
            if stats:
                log_message(
                    self.create_log,
                    self._fmt(
                        'log.create.summary',
                        total=stats.get('total', 0),
                        created=stats.get('created', 0),
                        exists=stats.get('exists', 0),
                        failed=stats.get('failed', 0),
                        invalid=stats.get('invalid', 0),
                    ),
                )
        except Exception:
            pass
        try:
            edits = int(getattr(worker, 'saved_edits', 0) or 0)
        except Exception:
            edits = 0
        try:
            created_stats = int((stats or {}).get('created', 0) or 0)
        except Exception:
            created_stats = 0
        edits = max(0, edits, created_stats)
        try:
            if edits > 0 and self.parent_window and hasattr(self.parent_window, 'record_operation'):
                self.parent_window.record_operation('create', edits)
        except Exception:
            pass
        self.preview_create_btn.setEnabled(True)
        self.create_btn.setEnabled(True)
        self.create_stop_btn.setEnabled(False)
        msg = self._t('ui.stopped') if stopped else self._t('log.create.finished')
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
                getattr(getattr(self.parent_window, 'auth_tab', None),
                        'family_combo', None).currentText()
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
                getattr(getattr(self.parent_window, 'auth_tab', None),
                        'lang_combo', None).currentText()
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

    def set_prefix_controls_visible(self, visible: bool):
        """Показать/скрыть локальные контролы префиксов."""
        state = bool(visible)
        try:
            if getattr(self, 'prefix_label_create', None) is not None:
                self.prefix_label_create.setVisible(state)
        except Exception:
            pass
        try:
            if getattr(self, 'ns_combo_create', None) is not None:
                self.ns_combo_create.setVisible(state)
        except Exception:
            pass
        try:
            if getattr(self, 'prefix_help_btn_create', None) is not None:
                self.prefix_help_btn_create.setVisible(state)
        except Exception:
            pass

    def update_summary(self, lang: str):
        """Автообновление summary при смене языка"""
        self.update_language(lang)

    def check_page_exists(self, title: str, lang: str, family: str) -> bool:
        """Проверка существования страницы"""
        # Эта функция будет использоваться в CreateWorker
        # Здесь просто заглушка для интерфейса
        return False

    def warn_existing_pages(self, existing_pages: list) -> bool:
        """Предупреждение о существующих страницах"""
        if not existing_pages:
            return True

        msg = self._t('ui.create.existing_pages_intro')
        msg += "\n".join(existing_pages[:10])  # Показываем первые 10
        if len(existing_pages) > 10:
            rest = len(existing_pages) - 10
            msg += self._fmt('ui.create.existing_pages_more', count=rest)
        msg += f"\n\n{self._t('ui.create.existing_pages_continue')}"

        reply = QMessageBox.question(
            self, self._t('ui.pages_already_exist'), msg,
            QMessageBox.Yes | QMessageBox.No
        )
        return reply == QMessageBox.Yes

    MINOR_EDIT_DISABLED = True
