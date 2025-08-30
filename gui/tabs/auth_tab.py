# -*- coding: utf-8 -*-
"""
Вкладка авторизации для Wiki Category Tool.

Этот модуль содержит компонент AuthTab, который обеспечивает:
- Поля ввода логина и пароля
- Выбор языка и проекта Wikimedia
- Авторизацию через pywikibot
- Проверку обновлений
- Управление учетными данными
"""

import os
import re
import ast
import sys
import ctypes
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QPushButton, QToolButton, QMessageBox, QApplication
)
from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices

from ...constants import (
    APP_VERSION, RELEASES_URL, GITHUB_API_RELEASES, REQUEST_HEADERS
)
from ...core.api_client import REQUEST_SESSION, _rate_wait
from ...core.pywikibot_config import (
    _dist_configs_dir, config_base_dir, apply_pwb_config, cookies_exist,
    _delete_all_cookies, reset_pywikibot_session
)
# AWB функции удалены
from ...utils import debug, default_summary, default_create_summary
from ...workers.login_worker import LoginWorker


# AWB функция удалена


class AuthTab(QWidget):
    """Вкладка авторизации"""

    # Сигналы для связи с главным окном
    login_success = Signal(str, str, str, str)  # username, password, lang, family
    logout_success = Signal()
    lang_changed = Signal(str)  # new_lang

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent

        # Состояние авторизации
        self.current_user: Optional[str] = None
        self.current_lang: Optional[str] = None
        self.prev_lang = 'ru'
        self._stay_on_top_active = False
        self._login_worker = None
        self._last_loaded_lang = None  # Для отслеживания последнего загруженного языка
        
        # ОТКЛЮЧАЕМ таймер - он вызывает зависания при вводе текста
        # self._namespace_update_timer = QTimer()
        # self._namespace_update_timer.setSingleShot(True)
        # self._namespace_update_timer.timeout.connect(self._delayed_namespace_update)
        self._pending_lang = None

        # UI элементы
        self.user_edit: Optional[QLineEdit] = None
        self.pass_edit: Optional[QLineEdit] = None
        self.lang_combo: Optional[QComboBox] = None
        self.family_combo: Optional[QComboBox] = None
        self.login_btn: Optional[QPushButton] = None
        self.status_label: Optional[QLabel] = None
        self.switch_btn: Optional[QPushButton] = None
        self.version_label: Optional[QLabel] = None

        self.init_ui()
        self.load_creds()

    def init_ui(self):
        """Инициализация пользовательского интерфейса"""
        layout = QVBoxLayout(self)

        # Применить стили
        try:
            self.setStyleSheet(
                "QWidget { font-size: 13px; } QLineEdit, QComboBox, QPushButton { min-height: 30px; }")
        except Exception:
            pass

        # Верхняя строка: версия справа
        try:
            top_row = QHBoxLayout()
            top_row.setContentsMargins(6, 6, 6, 0)
            top_row.setSpacing(0)
            top_row.addStretch()
            self.version_label = QLabel(f"v{APP_VERSION}")
            self.version_label.setStyleSheet('color: gray; font-size: 11px;')
            top_row.addWidget(self.version_label, alignment=Qt.AlignRight | Qt.AlignTop)
            layout.addLayout(top_row)
        except Exception:
            pass

        # Поля ввода
        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText('Имя пользователя')

        self.pass_edit = QLineEdit()
        self.pass_edit.setPlaceholderText('Пароль')
        self.pass_edit.setEchoMode(QLineEdit.Password)

        # Основной лейаут формы
        layout_form = QVBoxLayout()
        layout_form.setAlignment(Qt.AlignHCenter)
        layout_form.setSpacing(15)  # Увеличили отступы между элементами

        layout.addStretch(1)
        layout.addLayout(layout_form)
        layout.addStretch(2)
        layout.setContentsMargins(0, 14, 0, 14)

        # Выбор языка
        self._create_language_selector(layout_form)

        # Выбор проекта
        self._create_project_selector(layout_form)

        # Поля ввода логина и пароля
        self.user_edit.setMinimumWidth(280)
        self.pass_edit.setMinimumWidth(280)
        try:
            self.user_edit.setMinimumHeight(30)
            self.pass_edit.setMinimumHeight(30)
        except Exception:
            pass

        layout_form.addWidget(self.user_edit, alignment=Qt.AlignHCenter)
        layout_form.addWidget(self.pass_edit, alignment=Qt.AlignHCenter)

        # Кнопки авторизации
        self._create_auth_buttons(layout_form)

        # Нижние кнопки (Debug, Проверить обновления)
        self._create_bottom_buttons(layout)

        # Подключить сигналы
        self._connect_signals()
        

    def _create_language_selector(self, layout_form):
        """Создать селектор языка"""
        lang_help = (
            'Можно вручную ввести любой код языка.\n'
            'Для большинства языков локальные префиксы определяются автоматически через кэш/API.'
        )

        row_lang = QHBoxLayout()
        row_lang.setAlignment(Qt.AlignHCenter)
        try:
            row_lang.setSpacing(12)  # Увеличили отступы в строке языка
        except Exception:
            pass

        lang_label = QLabel('Язык вики:')
        row_lang.addWidget(lang_label)

        self.lang_combo = QComboBox()
        self.lang_combo.setEditable(True)
        self.lang_combo.addItems(['ru', 'uk', 'be', 'en', 'fr', 'es', 'de'])
        self.lang_combo.setCurrentText('ru')
        self.lang_combo.setMinimumWidth(100)
        self.lang_combo.setMaximumWidth(107)  # 1/3 от 320px для языковых кодов

        # Применяем аналогичные стили для языкового комбобокса
        try:
            self.lang_combo.setStyleSheet("""
                QComboBox {
                    padding: 4px 8px;
                    min-height: 28px;
                }
                QComboBox QAbstractItemView::item {
                    padding: 6px 8px;
                    margin: 0px;
                }
            """)
        except Exception:
            pass

        self.prev_lang = 'ru'
        row_lang.addWidget(self.lang_combo)

        # Кнопка ручного обновления префиксов перенесена в нижнюю панель

        info_btn = QToolButton()
        info_btn.setText('❔')
        info_btn.setAutoRaise(True)
        info_btn.setToolTip(lang_help)
        info_btn.clicked.connect(
            lambda _=None: QMessageBox.information(self, 'Справка', lang_help))
        row_lang.addWidget(info_btn)

        layout_form.addLayout(row_lang)
        layout_form.setAlignment(row_lang, Qt.AlignHCenter)

    def _create_project_selector(self, layout_form):
        """Создать селектор проекта"""
        fam_help = (
            'Выберите проект: Wikipedia, Commons или иной (Wikibooks, Wiktionary, Wikiquote, Wikisource, '
            'Wikiversity, Wikidata, Wikifunctions, Wikivoyage, Wikinews, Meta, MediaWiki).\n\n'
            'Для Commons укажите язык "commons".\n\n'
            'Важно: работа вне Wikipedia не полностью протестирована и может быть частично ограничена.'
        )

        row_fam = QHBoxLayout()
        row_fam.setAlignment(Qt.AlignHCenter)
        try:
            row_fam.setSpacing(12)  # Увеличили отступы в строке проекта
        except Exception:
            pass

        fam_label = QLabel('Проект:')
        row_fam.addWidget(fam_label)

        self.family_combo = QComboBox()
        self.family_combo.setEditable(False)

        # Улучшенное форматирование списка проектов с отступами
        primary = ['wikipedia', 'commons']
        others = sorted([
            'wikibooks', 'wiktionary', 'wikiquote',
            'wikisource', 'wikiversity', 'species',
            'wikidata', 'wikifunctions',
            'wikivoyage', 'wikinews',
            'meta', 'mediawiki'
        ])

        # Добавляем основные проекты
        for item in primary:
            self.family_combo.addItem(item)

        # Добавляем разделитель с отступами
        separator_index = self.family_combo.count()
        self.family_combo.insertSeparator(separator_index)

        # Добавляем остальные проекты
        for item in others:
            self.family_combo.addItem(item)

        # Настройка стилей для более разряженного вида
        try:
            self.family_combo.setStyleSheet("""
                QComboBox {
                    padding: 4px 8px;
                    min-height: 28px;
                }
                QComboBox QAbstractItemView::item {
                    padding: 6px 8px;
                    margin: 0px;
                }
                QComboBox QAbstractItemView::separator {
                    height: 1px;
                    margin: 4px 6px;
                }
            """)
        except Exception:
            pass

        self.family_combo.setCurrentText('wikipedia')
        self.family_combo.setMinimumWidth(160)
        self.family_combo.setMaximumWidth(280)  # Чуть короче для проектов
        row_fam.addWidget(self.family_combo)

        fam_btn = QToolButton()
        fam_btn.setText('❔')
        fam_btn.setAutoRaise(True)
        fam_btn.setToolTip(fam_help)
        fam_btn.clicked.connect(
            lambda _=None: QMessageBox.information(self, 'Справка', fam_help))
        row_fam.addWidget(fam_btn)

        layout_form.addLayout(row_fam)
        layout_form.setAlignment(row_fam, Qt.AlignHCenter)

    def _create_auth_buttons(self, layout_form):
        """Создать кнопки авторизации"""
        self.login_btn = QPushButton('Авторизоваться')

        self.status_label = QLabel('Авторизация (pywikibot)')
        try:
            self.status_label.setTextFormat(Qt.RichText)
            self.status_label.setTextInteractionFlags(
                Qt.TextBrowserInteraction)
            self.status_label.setOpenExternalLinks(True)
        except Exception:
            pass

        self.switch_btn = QPushButton('Сменить аккаунт')
        self.switch_btn.setVisible(False)

        layout_form.addWidget(self.login_btn, alignment=Qt.AlignHCenter)
        layout_form.addWidget(self.status_label, alignment=Qt.AlignHCenter)
        layout_form.addWidget(self.switch_btn, alignment=Qt.AlignHCenter)

        layout_form.setStretchFactor(self.login_btn, 0)

    def _create_bottom_buttons(self, layout):
        """Создать нижние кнопки"""
        row_debug = QHBoxLayout()
        row_debug.addStretch()

        dbg_btn = QPushButton('Debug')
        dbg_btn.setFixedWidth(60)
        dbg_btn.clicked.connect(self._show_debug)
        row_debug.addWidget(dbg_btn)

        # Кнопка принудительного обновления префиксов пространств имён (force)
        self.refresh_ns_btn = QPushButton('Обновить префиксы')
        self.refresh_ns_btn.setToolTip('Принудительно обновить префиксы пространств имён для текущего языка и проекта')
        self.refresh_ns_btn.clicked.connect(self.force_namespace_update)
        row_debug.addWidget(self.refresh_ns_btn)

        upd_btn = QPushButton('Проверить обновления')
        upd_btn.clicked.connect(self.check_updates)
        row_debug.addWidget(upd_btn)

        layout.addLayout(row_debug)

    def _connect_signals(self):
        """Подключить сигналы"""
        # Авторизация
        self.login_btn.clicked.connect(self.save_creds)
        self.switch_btn.clicked.connect(self.switch_account)

        # Enter в полях ввода
        try:
            self.user_edit.returnPressed.connect(self.save_creds)
            self.pass_edit.returnPressed.connect(self.save_creds)
        except Exception:
            pass

        # ОТКЛЮЧАЕМ автоматическое обновление при вводе текста - это вызывает зависания
        # self.lang_combo.currentTextChanged.connect(self._on_lang_change_immediate)
        
        # Обновление только при потере фокуса или нажатии Enter
        self.lang_combo.lineEdit().editingFinished.connect(self._on_lang_editing_finished)
        self.lang_combo.lineEdit().returnPressed.connect(self._on_lang_editing_finished)

        # Изменение проекта будет обрабатываться в главном окне

    def _on_family_change(self, fam):
        """Обработчик изменения проекта"""
        # Уведомляем главное окно об изменении семейства проектов
        if hasattr(self.parent_window, 'update_family'):
            self.parent_window.update_family(fam)

    def _show_debug(self):
        """Показать окно отладки"""
        if hasattr(self.parent_window, 'show_debug'):
            self.parent_window.show_debug()

    def check_updates(self):
        """Проверить обновления"""
        try:
            debug('Проверка обновлений...')
            debug(f'Запрос к: {GITHUB_API_RELEASES}')
            debug(f'Заголовки: {REQUEST_HEADERS}')
            
            _rate_wait()
            debug('Отправляем HTTP запрос...')
            
            r = REQUEST_SESSION.get(
                GITHUB_API_RELEASES, headers=REQUEST_HEADERS, timeout=10)
            
            debug(f'Получен ответ: статус {r.status_code}')
            debug(f'Заголовки ответа: {dict(r.headers)}')
            
            if r.status_code != 200:
                debug(f'GitHub API status {r.status_code}')
                debug(f'Текст ответа: {r.text[:500]}...')
                QMessageBox.information(
                    self, 'Проверка обновлений', f'Не удалось проверить обновления. Текущая версия: {APP_VERSION}. Откроем страницу релизов.')
                QDesktopServices.openUrl(QUrl(RELEASES_URL))
                return
                
            debug('Парсим JSON ответ...')
            data = r.json() or []
            debug(f'Получено релизов: {len(data) if isinstance(data, list) else "не список"}')
            if not isinstance(data, list) or not data:
                QMessageBox.information(
                    self, 'Проверка обновлений', f'Пока нет опубликованных релизов. Текущая версия: {APP_VERSION}. Откроем страницу.')
                QDesktopServices.openUrl(QUrl(RELEASES_URL))
                return
            latest = None
            for rel in data:
                # пропускаем черновики
                if rel.get('draft'):
                    continue
                latest = rel
                break
            if not latest:
                QMessageBox.information(
                    self, 'Проверка обновлений', f'Подходящих релизов не найдено. Текущая версия: {APP_VERSION}.')
                return
            tag = (latest.get('tag_name') or '').strip()
            name = (latest.get('name') or tag or 'Новый релиз')
            html_url = (latest.get('html_url') or RELEASES_URL)
            # Форматируем дату публикации
            published = (latest.get('published_at')
                         or latest.get('created_at') or '').strip()
            date_str = ''
            if published:
                try:
                    # ISO 8601 → YYYY-MM-DD
                    date_str = (published.split('T', 1)[0])
                except Exception:
                    date_str = published
            # Сравнение версий: только числовые части

            def _num(ver: str):
                m = re.search(r"(\d+(?:\.\d+)+|\d+)", ver or '')
                return [int(x) for x in m.group(1).split('.')] if m else None

            local = (APP_VERSION or '').strip()
            remote = (tag or '').strip()

            ln = _num(local)
            rn = _num(remote)

            def _cmp_lists(a, b):
                la, lb_ = len(a), len(b)
                n = max(la, lb_)
                a = a + [0] * (n - la)
                b = b + [0] * (n - lb_)
                if a == b:
                    return 0
                return 1 if a > b else -1

            cmp_res = None
            # Сравниваем только числовые части версий
            if ln and rn:
                cmp_res = _cmp_lists(ln, rn)
            elif local and remote:
                # Фолбэк: строковое сравнение по равенству
                cmp_res = 0 if local == remote else None

            if cmp_res is None:
                # Не смогли корректно сравнить — просто предложим открыть страницу
                extra = f' ({date_str})' if date_str else ''
                msg = (
                    f'Найден релиз: {name}{extra}.\n'
                    f'Текущая версия: {APP_VERSION}\n'
                    f'Актуальная версия: {remote or name}\n'
                    f'Открыть страницу релизов?'
                )
                res = QMessageBox.question(
                    self, 'Проверить обновления', msg, QMessageBox.Yes | QMessageBox.No)
                if res == QMessageBox.Yes:
                    QDesktopServices.openUrl(QUrl(html_url))
            elif cmp_res < 0:
                # remote > local
                extra = f' ({date_str})' if date_str else ''
                msg = (
                    f'Доступна новая версия: {name}{extra}.\n\n'
                    f'Текущая версия: {APP_VERSION}\n'
                    f'Актуальная версия: {remote or name}\n'
                    f'Открыть страницу релизов?'
                )
                res = QMessageBox.question(
                    self, 'Проверить обновления', msg, QMessageBox.Yes | QMessageBox.No)
                if res == QMessageBox.Yes:
                    QDesktopServices.openUrl(QUrl(html_url))
            elif cmp_res > 0:
                # local > remote
                msg = (
                    f'У вас версия новее последнего релиза на GitHub.\n\n'
                    f'Текущая версия: {APP_VERSION}\n'
                    f'Актуальная версия: {remote or name}\n'
                )
                QMessageBox.information(self, 'Проверить обновления', msg)
            else:
                msg = (
                    f'У вас установлена актуальная версия.\n\n'
                    f'Текущая версия: {APP_VERSION}\n'
                    f'Актуальная версия: {remote or name}'
                )
                QMessageBox.information(self, 'Проверить обновления', msg)
        except Exception as e:
            debug(f'Ошибка проверки обновлений: {e}')
            QMessageBox.information(
                self, 'Проверка обновлений', f'Произошла ошибка. Текущая версия: {APP_VERSION}. Откроем страницу релизов.')
            QDesktopServices.openUrl(QUrl(RELEASES_URL))

    def _after_login_success(self, u: str, pwd: str, l: str, fam: str):
        """Обработчик успешной авторизации"""
        debug(f'Авторизация успешна: {u}@{l}.{fam}')
        self._apply_cred_style(True)
        try:
            QApplication.beep()
        except Exception:
            pass

        try:
            self.parent_window.raise_()
            self.parent_window.activateWindow()
        except Exception:
            pass

        self._force_on_top(False, delay_ms=600)
        self._bring_to_front_sequence()
        self.login_btn.setEnabled(True)

        # Сохранить текущие данные авторизации
        self.current_user = u
        self.current_lang = l

        # Уведомить главное окно об успешной авторизации
        self.login_success.emit(u, pwd, l, fam)

    def save_creds(self):
        """Сохранить учетные данные и запустить авторизацию"""
        debug('Нажата кнопка авторизации')
        user = self.user_edit.text().strip()
        pwd = self.pass_edit.text().strip()
        lang = (self.lang_combo.currentText() or 'ru').strip()
        fam = (self.family_combo.currentText() or 'wikipedia')

        debug(
            f'Попытка авторизации: пользователь={user}, язык={lang}, проект={fam}')

        if not user or not pwd:
            debug('Ошибка: не введены логин или пароль')
            QMessageBox.warning(
                self, 'Ошибка', 'Введите имя пользователя и пароль.')
            self._apply_cred_style(False)
            return
        # Запускаем логин в рабочем потоке, чтобы UI не завис при сетевых/блокирующих ошибках (в т.ч. IP-запрет)
        debug('Блокируем кнопку авторизации и запускаем LoginWorker')
        self.login_btn.setEnabled(False)
        # На время авторизации удерживаем окно поверх других (Windows может красть фокус)
        self._force_on_top(True)
        try:
            self.parent_window.raise_()
            self.parent_window.activateWindow()
        except Exception:
            pass

        debug(f'Создаем LoginWorker для {user}@{lang}.{fam}')
        worker = LoginWorker(user, pwd, lang, fam)
        # на успех
        worker.success.connect(self._after_login_success)
        # на провал
        worker.failure.connect(lambda msg: [
            debug(f'Ошибка авторизации: {msg}'),
            self.status_label.setText('Ошибка авторизации'),
            QMessageBox.critical(self, 'Ошибка авторизации',
                                 f'Не удалось авторизоваться: {msg}'),
            self._apply_cred_style(False),
            self.login_btn.setEnabled(True),
            # вернуть окно на передний план после модального окна
            (self.parent_window.raise_(), self.parent_window.activateWindow()),
            # снять режим поверх других окон
            self._force_on_top(False, delay_ms=600),
            self._bring_to_front_sequence()
        ])

        debug('Запускаем LoginWorker...')
        self._login_worker = worker
        worker.start()

    def _force_on_top(self, enable: bool, delay_ms: int = 0) -> None:
        """Принудительно удерживать окно поверх других"""
        if delay_ms and delay_ms > 0:
            try:
                QTimer.singleShot(
                    delay_ms, lambda: self._force_on_top(enable, 0))
                return
            except Exception:
                pass
        try:
            if enable == self._stay_on_top_active:
                if enable:
                    self.parent_window.raise_()
                    self.parent_window.activateWindow()
                return
            self._stay_on_top_active = bool(enable)
            was_visible = self.parent_window.isVisible()
            self.parent_window.setWindowFlag(
                Qt.WindowStaysOnTopHint, self._stay_on_top_active)
            if was_visible:
                # пере-применить флаг и удержать окно активным
                self.parent_window.show()
                self.parent_window.raise_()
                self.parent_window.activateWindow()
        except Exception:
            pass

    def _bring_to_front_sequence(self) -> None:
        """Многократное восстановление окна на передний план с задержками,
        чтобы перекрыть возможные асинхронные кражи фокуса."""
        try:
            def bring():
                try:
                    if self.parent_window.isMinimized():
                        self.parent_window.showNormal()
                    self.parent_window.raise_()
                    self.parent_window.activateWindow()
                    # Дополнительно — WinAPI на Windows
                    if sys.platform.startswith('win'):
                        try:
                            hwnd = int(self.parent_window.winId())
                            user32 = ctypes.windll.user32
                            SW_SHOWNORMAL = 1
                            SWP_NOSIZE = 0x0001
                            SWP_NOMOVE = 0x0002
                            HWND_TOPMOST = -1
                            HWND_NOTOPMOST = -2
                            # показать и вывести на передний план
                            user32.ShowWindow(hwnd, SW_SHOWNORMAL)
                            # быстрый цикл topmost -> notopmost для всплытия над другими окнами
                            user32.SetWindowPos(
                                hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
                            user32.SetWindowPos(
                                hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
                            user32.SetForegroundWindow(hwnd)
                        except Exception:
                            pass
                except Exception:
                    pass
            # несколько попыток
            for delay in (0, 80, 200, 400, 800, 1500):
                QTimer.singleShot(delay, bring)
        except Exception:
            pass

    def load_creds(self):
        """Загрузить сохраненные учетные данные"""
        # При старте читаем общий конфиг и заполняем поля. Если есть активные куки — считаем, что уже авторизованы.
        cfg_dir = _dist_configs_dir()
        uc = os.path.join(cfg_dir, 'user-config.py')
        up = os.path.join(cfg_dir, 'user-password.py')
        cur_lang = None
        cur_family = None
        username_map = {}
        password = ''
        try:
            if os.path.isfile(uc):
                with open(uc, encoding='utf-8') as f:
                    txt = f.read()
                mfam = re.search(r"^\s*family\s*=\s*'([^']+)'\s*$", txt, re.M)
                if mfam:
                    cur_family = mfam.group(1)
                    try:
                        self.family_combo.setCurrentText(cur_family)
                    except Exception:
                        pass
                mlang = re.search(r"^\s*mylang\s*=\s*'([^']+)'\s*$", txt, re.M)
                if mlang:
                    cur_lang = mlang.group(1)
                    self.lang_combo.setCurrentText(cur_lang)
                fam = cur_family or (
                    self.family_combo.currentText() or 'wikipedia')
                fam_re = re.escape(fam)
                for m in re.finditer(rf"usernames\['{fam_re}'\]\['([^']+)'\]\s*=\s*'([^']+)'", txt):
                    username_map[m.group(1)] = m.group(2)
            if os.path.isfile(up):
                with open(up, encoding='utf-8') as f:
                    try:
                        u, p = ast.literal_eval(f.read())
                        password = p
                        if cur_lang and cur_lang not in username_map and u:
                            username_map[cur_lang] = u
                    except Exception:
                        pass
        except Exception:
            pass
        # Заполнить поля
        if cur_lang and cur_lang in username_map:
            self.user_edit.setText(username_map[cur_lang])
        if password:
            self.pass_edit.setText(password)

        # Устанавливаем начальный язык как загруженный
        if cur_lang:
            self._last_loaded_lang = cur_lang
        # Если есть и логин, и пароль, и обнаружены куки — считаем, что авторизация активна
        fam = cur_family or (self.family_combo.currentText() or 'wikipedia')
        if cur_lang and (cur_lang in username_map) and password:
            try:
                if cookies_exist(cfg_dir, username_map[cur_lang]):
                    self._apply_cred_style(True)
                    # Уведомляем главное окно об успешной авторизации
                    self.login_success.emit(
                        username_map[cur_lang], password, cur_lang, fam)
                else:
                    self._apply_cred_style(False)
            except Exception:
                self._apply_cred_style(False)
        else:
            self._apply_cred_style(False)

    def _apply_cred_style(self, ok: bool):
        """Применить стили для состояния авторизации"""
        css_ok = 'background-color:#d4edda'
        css_def = ''
        for w in (self.user_edit, self.pass_edit):
            w.setStyleSheet(css_ok if ok else css_def)
        self.user_edit.setReadOnly(ok)
        self.pass_edit.setReadOnly(ok)
        self.lang_combo.setEnabled(not ok)
        self.login_btn.setVisible(not ok)
        self.switch_btn.setVisible(ok)
        self.status_label.setText(
            'Авторизовано' if ok else 'Авторизация (pywikibot)')
        if ok:
            self.current_user = self.user_edit.text().strip()
            self.current_lang = (self.lang_combo.currentText() or 'ru').strip()

    def _on_lang_change_immediate(self, new_lang):
        """Немедленная обработка изменения языка (только для UI)"""
        try:
            debug(f'_on_lang_change_immediate: {self.prev_lang} -> {new_lang}')
            
            # Обновляем только summary, без загрузки namespace'ов
            if hasattr(self.parent_window, 'summary_edit'):
                edits = [
                    (getattr(self.parent_window, 'summary_edit', None), default_summary),
                    (getattr(self.parent_window, 'summary_edit_create', None),
                     default_create_summary)
                ]
                for widget, func in edits:
                    if widget is None:
                        continue
                    cur = widget.text().strip()
                    if cur == '' or cur == func(self.prev_lang):
                        widget.setText(func(new_lang))

            self.prev_lang = new_lang
            
            # ОТКЛЮЧАЕМ автоматическое обновление namespace'ов при изменении языка
            # чтобы избежать зависания из-за синхронных HTTP-запросов к API
            debug(f'Язык изменен на {new_lang}, но автоматическое обновление namespace отключено')
            
            # Сразу уведомляем об изменении языка без задержки
            debug(f'Отправляем сигнал lang_changed: {new_lang}')
            self.lang_changed.emit(new_lang)
            
        except Exception as e:
            debug(f'Ошибка в _on_lang_change_immediate: {e}')

    def _on_lang_editing_finished(self):
        """Обработка завершения редактирования языка (потеря фокуса)"""
        try:
            new_lang = (self.lang_combo.currentText() or 'ru').strip()
            debug(f'_on_lang_editing_finished: новый язык = {new_lang}')

            # Проверяем, изменился ли язык
            last_lang = getattr(self, '_last_loaded_lang', None)
            debug(f'Сравниваем языки: новый={new_lang}, последний={last_lang}')
            if new_lang == last_lang:
                debug(f'Язык не изменился ({new_lang}), пропускаем обновление')
                return

            debug(f'Загружаем namespace префиксы для языка: {new_lang}')
            self._last_loaded_lang = new_lang

            # Уведомить главное окно об изменении языка
            debug(f'Отправляем сигнал lang_changed: {new_lang}')
            self.lang_changed.emit(new_lang)
            
        except Exception as e:
            debug(f'Критическая ошибка в _on_lang_editing_finished: {e}')
            import traceback
            debug(f'Traceback: {traceback.format_exc()}')
    
    def _delayed_namespace_update(self):
        """Отложенное обновление namespace'ов (вызывается через таймер)"""
        try:
            if not self._pending_lang:
                return
                
            new_lang = self._pending_lang
            debug(f'_delayed_namespace_update: язык изменен на {new_lang}')
            
            # Проверяем, изменился ли язык
            if new_lang == getattr(self, '_last_loaded_lang', None):
                debug(f'Язык не изменился ({new_lang}), пропускаем обновление')
                return

            self._last_loaded_lang = new_lang

            # ОТКЛЮЧАЕМ автоматическое обновление namespace'ов при вводе языка
            # чтобы избежать зависания из-за синхронных HTTP-запросов к API
            debug(f'Пропускаем автоматическое обновление namespace для {new_lang} (отключено для предотвращения зависания)')
            
            # Только уведомляем об изменении языка без обновления комбобоксов
            debug(f'Отправляем сигнал lang_changed: {new_lang}')
            self.lang_changed.emit(new_lang)
            
        except Exception as e:
            debug(f'Критическая ошибка в _delayed_namespace_update: {e}')
            import traceback
            debug(f'Traceback: {traceback.format_exc()}')

    def force_namespace_update(self):
        """Принудительное обновление namespace'ов (для Debug и переключения вкладок)"""
        try:
            current_lang = (self.lang_combo.currentText() or 'ru').strip()
            current_family = (self.family_combo.currentText() or 'wikipedia')
            
            debug(f'Принудительное обновление namespace для {current_family}:{current_lang}')
            
            # Обновляем namespace'ы через главное окно с принудительной загрузкой
            if hasattr(self.parent_window, 'force_update_namespace_combos'):
                try:
                    # Временная блокировка кнопки от двойных кликов
                    try:
                        if hasattr(self, 'refresh_ns_btn') and self.refresh_ns_btn:
                            self.refresh_ns_btn.setEnabled(False)
                    except Exception:
                        pass

                    self.parent_window.force_update_namespace_combos(current_family, current_lang)

                    # Сообщение об успехе
                    try:
                        QMessageBox.information(
                            self,
                            'Готово',
                            'Префиксы пространств имён обновлены для текущего языка и проекта.'
                        )
                    except Exception:
                        pass
                    finally:
                        try:
                            if hasattr(self, 'refresh_ns_btn') and self.refresh_ns_btn:
                                self.refresh_ns_btn.setEnabled(True)
                        except Exception:
                            pass
                except Exception as e:
                    debug(f'Ошибка при принудительном обновлении namespace: {e}')
                    try:
                        QMessageBox.warning(
                            self,
                            'Ошибка',
                            f'Не удалось обновить префиксы пространств имён: {e}'
                        )
                    except Exception:
                        pass
            else:
                debug('Метод force_update_namespace_combos не найден в parent_window')
                
        except Exception as e:
            debug(f'Критическая ошибка при принудительном обновлении namespace: {e}')

    def _on_lang_change(self, new_lang):
        """Обработчик изменения языка (для совместимости)"""
        self._on_lang_change_immediate(new_lang)

    def switch_account(self):
        """Сменить аккаунт"""
        lang = (self.lang_combo.currentText() or 'ru').strip()
        cfg_dir = config_base_dir()

        # Остановить возможный активный LoginWorker и отсоединить сигналы
        try:
            w = getattr(self, '_login_worker', None)
            if w is not None:
                try:
                    try:
                        w.success.disconnect()
                    except Exception:
                        pass
                    try:
                        w.failure.disconnect()
                    except Exception:
                        pass
                except Exception:
                    pass
                try:
                    if hasattr(w, 'isRunning') and w.isRunning():
                        if hasattr(w, 'request_stop'):
                            w.request_stop()
                        try:
                            w.wait(800)
                        except Exception:
                            pass
                        if hasattr(w, 'isRunning') and w.isRunning() and hasattr(w, 'terminate'):
                            w.terminate()
                            try:
                                w.wait(400)
                            except Exception:
                                pass
                except Exception:
                    pass
                try:
                    self._login_worker = None
                except Exception:
                    pass
        except Exception:
            pass

        # Считываем введённое имя пользователя (значение не требуется далее)
        self.user_edit.text().strip()
        _delete_all_cookies(cfg_dir)
        reset_pywikibot_session(None)

        self.current_user = None
        self.current_lang = None
        self._apply_cred_style(False)
        # Форсируем снятие зелёной подсветки и обновление состояния
        try:
            for w in (self.user_edit, self.pass_edit):
                # Сброс sheet и явный возврат к базовому цвету темы
                w.setStyleSheet('')
                w.setStyleSheet('background-color: palette(base);')
                try:
                    st = w.style()
                    if st:
                        st.unpolish(w)
                        st.polish(w)
                except Exception:
                    pass
                w.setReadOnly(False)
                w.update()
            if self.status_label:
                self.status_label.setText('Авторизация (pywikibot)')
        except Exception:
            pass

        fam = (self.family_combo.currentText() or 'wikipedia')
        apply_pwb_config(lang, fam)

        # Уведомить главное окно о выходе из системы
        self.logout_success.emit()

    def clear_auth_highlight(self):
        """Снять стили подсветки и вернуть поля в обычное состояние (для внешних вызовов)."""
        try:
            self._apply_cred_style(False)
            for w in (self.user_edit, self.pass_edit):
                w.setStyleSheet('')
                w.setStyleSheet('background-color: palette(base);')
                try:
                    st = w.style()
                    if st:
                        st.unpolish(w)
                        st.polish(w)
                except Exception:
                    pass
                w.setReadOnly(False)
                w.update()
            if self.status_label:
                self.status_label.setText('Авторизация (pywikibot)')
        except Exception:
            pass
