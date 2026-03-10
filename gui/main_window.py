# -*- coding: utf-8 -*-
"""
Основное окно Wiki Category Tool.

Этот модуль содержит класс MainWindow, который объединяет все вкладки
и обеспечивает общую функциональность приложения.
"""

import os
import sys
import json
from datetime import datetime
from typing import Optional

from PySide6.QtCore import QEvent, QTimer, QSignalBlocker, QThread, Signal, Qt, QSettings, QSize, QByteArray
from PySide6.QtWidgets import (
    QMainWindow, QTabWidget, QMessageBox, QToolButton, QWidget, QVBoxLayout, QLabel,
    QHBoxLayout, QFrame, QPushButton, QApplication, QStyle, QCheckBox, QGroupBox,
    QLineEdit, QTextEdit, QPlainTextEdit, QTreeWidget, QSizePolicy, QSpinBox, QAbstractSpinBox, QProgressBar,
    QComboBox, QGraphicsDropShadowEffect
)
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor
from PySide6.QtSvg import QSvgRenderer

from ..constants import APP_VERSION
from ..core.localization import load_translation_pairs, translate_key, set_runtime_ui_language
from ..utils import debug, default_summary, default_create_summary, resource_path
from .widgets.ui_helpers import add_info_button as ui_add_info_button
from .widgets.ui_helpers import force_on_top as ui_force_on_top
from .widgets.ui_helpers import bring_to_front_sequence as ui_bring_to_front_sequence
from .widgets.ui_helpers import install_localized_context_menu
from .widgets.ui_helpers import show_help_dialog as ui_show_help_dialog

_DEFAULT_SESSION_PROJECT = 'wikipedia / ru'


class NSLoadThread(QThread):
    finished_ok = Signal(bool)

    def __init__(self, namespace_manager, family: str, lang: str):
        super().__init__()
        self._nm = namespace_manager
        self._family = (family or 'wikipedia')
        self._lang = (lang or 'ru')

    def run(self):
        ok = False
        try:
            info = self._nm._load_ns_info(self._family, self._lang)
            ok = bool(info)
        except Exception:
            ok = False
        try:
            self.finished_ok.emit(ok)
        except Exception:
            pass


class MainWindow(QMainWindow):
    """Основное окно приложения"""
    ns_update_finished = Signal(str, str, bool)  # family, lang, ok
    _msgbox_i18n_installed = False
    _msgbox_i18n_owner = None
    _TAB_META = {
        0: ('ui.main.tab.auth.tag', 'ui.authentication', 'ui.main.tab.auth.subtitle'),
        1: ('ui.main.tab.read.tag', 'ui.main.tab.read.title', 'ui.main.tab.read.subtitle'),
        2: ('ui.main.tab.replace.tag', 'ui.bulk_replace', 'ui.main.tab.replace.subtitle'),
        3: ('ui.main.tab.create.tag', 'ui.bulk_create', 'ui.main.tab.create.subtitle'),
        4: ('ui.main.tab.rename.tag', 'ui.main.tab.rename.title', 'ui.main.tab.rename.subtitle'),
        5: ('ui.main.tab.cleanup.tag', 'ui.main.tab.cleanup.title', 'ui.main.tab.cleanup.subtitle'),
        6: ('ui.main.tab.sync.tag', 'ui.category_sync_label', 'ui.main.tab.sync.subtitle'),
        7: ('ui.main.tab.overview.tag', 'ui.overview', 'ui.main.tab.overview.subtitle'),
    }
    _NAV_ITEMS = [
        (1, 'ui.read', 'read'),
        (2, 'ui.replace', 'replace'),
        (3, 'ui.create', 'create'),
        (4, 'ui.rename', 'rename'),
        (5, 'ui.redundant_categories', 'cleanup'),
        (6, 'ui.category_sync_label', 'sync'),
    ]

    def __init__(self):
        super().__init__()
        self.setWindowTitle('Wiki Category Tool')
        try:
            import sys
            icon_sources = [
                resource_path('icon.ico'),
                os.path.join(os.path.dirname(sys.executable), 'icon.ico'),
            ]
            if getattr(sys, 'frozen', False):
                icon_sources.append(sys.executable)

            applied_icon_path = None
            for icon_path in icon_sources:
                try:
                    if not icon_path or not os.path.exists(icon_path):
                        continue
                    icon = QIcon(icon_path)
                    if icon.isNull():
                        continue
                    self.setWindowIcon(icon)
                    applied_icon_path = icon_path
                    debug(f"MainWindow icon set: {icon_path}")
                    break
                except Exception:
                    continue

            # Дополнительно: на Windows принудительно установим иконку через WinAPI (WM_SETICON)
            # только когда нашли .ico-файл.
            if applied_icon_path and sys.platform.startswith('win') and applied_icon_path.lower().endswith('.ico'):
                try:
                    import ctypes
                    hwnd = int(self.winId())
                    WM_SETICON = 0x0080
                    ICON_SMALL = 0
                    ICON_BIG = 1
                    IMAGE_ICON = 1
                    LR_LOADFROMFILE = 0x0010
                    LR_DEFAULTSIZE = 0x0040
                    hicon = ctypes.windll.user32.LoadImageW(
                        None, applied_icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
                    if hicon:
                        ctypes.windll.user32.SendMessageW(
                            hwnd, WM_SETICON, ICON_SMALL, hicon)
                        ctypes.windll.user32.SendMessageW(
                            hwnd, WM_SETICON, ICON_BIG, hicon)
                        debug('WM_SETICON applied for small and big icons')
                except Exception as _e:
                    try:
                        debug(f'WM_SETICON failed: {_e}')
                    except Exception:
                        pass
            elif not applied_icon_path:
                debug("MainWindow icon not found in known locations")
        except Exception:
            pass

        # Стартовый размер как раньше, но можно сжимать до меньшего минимума
        self.resize(1200, 700)
        self.setMinimumSize(900, 540)
        self._startup_placeholder = None
        self._startup_label = None
        self._startup_complete = False
        self._nav_buttons: dict[int, QPushButton] = {}
        # Theme IDs: light / teal / dark
        self._theme_mode = 'teal'
        self._ui_lang = 'ru'
        set_runtime_ui_language(self._ui_lang)
        self._sidebar_collapsed = False
        self._session_click_targets = set()
        self._brand_click_targets = set()
        self._settings = QSettings('sc113', 'WikiCategoryTool')
        self._operation_stats = {
            'read_pages_total': 0,
            'edit_pages_total': 0,
            'replace_runs': 0,
            'create_runs': 0,
            'rename_runs': 0,
            'cleanup_runs': 0,
            'sync_runs': 0,
            'sync_transferred_total': 0,
            'replace_edits_total': 0,
            'create_edits_total': 0,
            'rename_edits_total': 0,
            'cleanup_edits_total': 0,
            'sync_edits_total': 0,
        }
        self._operation_history: list[dict] = []

        # Состояние приложения
        self.current_user: Optional[str] = None
        self.current_password: Optional[str] = None
        self.current_lang: Optional[str] = None
        self.current_family: Optional[str] = None
        self.prev_lang = 'ru'
        self._secret_buffer = ''
        self._stay_on_top_active = False

        # Запоминание флага «автоподтверждать прямые совпадения» между диалогами
        self._auto_confirm_direct_all_ui: bool = False

        # Значения по умолчанию и отложенная инициализация тяжёлых компонентов.
        self.current_lang = 'ru'
        self.current_family = 'wikipedia'
        self.api_client = None
        self.namespace_manager = None
        self.config_manager = None
        self.template_manager = None
        self.auth_tab = None
        self.parse_tab = None
        self.replace_tab = None
        self.create_tab = None
        self.rename_tab = None
        self.redundant_categories_tab = None
        self.category_content_sync_tab = None
        self.overview_tab = None
        self._ns_thread = None
        self._rules_file_path = None

        self._load_ui_settings()
        self._install_messagebox_i18n()
        self._build_shell()
        self._apply_modern_theme()
        self._init_startup_placeholder()
        self._update_header_for_tab(-1)
        self._refresh_session_card()

    def _t(self, key: str) -> str:
        return translate_key(key, self._ui_lang, '')

    def _fmt(self, key: str, **kwargs) -> str:
        text = self._t(key)
        try:
            return text.format(**kwargs)
        except Exception:
            return text

    def _build_shell(self):
        root = QWidget(self)
        root.setObjectName('windowRoot')
        shell = QHBoxLayout(root)
        try:
            shell.setContentsMargins(0, 0, 0, 0)
            shell.setSpacing(0)
        except Exception:
            pass
        self.setCentralWidget(root)

        # Левый сайдбар
        self.sidebar = QFrame(root)
        self.sidebar.setObjectName('sidebar')
        try:
            self.sidebar.setMinimumWidth(248)
            self.sidebar.setMaximumWidth(248)
        except Exception:
            pass
        sidebar_layout = QVBoxLayout(self.sidebar)
        try:
            sidebar_layout.setContentsMargins(10, 12, 10, 10)
            sidebar_layout.setSpacing(10)
        except Exception:
            pass

        self.brand_box = QFrame(self.sidebar)
        self.brand_box.setObjectName('brandBox')
        brand_layout = QVBoxLayout(self.brand_box)
        try:
            brand_layout.setContentsMargins(14, 12, 14, 12)
            brand_layout.setSpacing(2)
        except Exception:
            pass
        self.brand_title = QLabel('Wiki Category Tool')
        self.brand_title.setObjectName('brandTitle')
        self.brand_sub = QLabel('')
        self.brand_sub.setObjectName('brandSubtitle')
        self.brand_compact_icon = QLabel('')
        self.brand_compact_icon.setObjectName('brandCompactIcon')
        try:
            self.brand_compact_icon.setAlignment(Qt.AlignCenter)
            self.brand_compact_icon.setFixedSize(34, 34)
        except Exception:
            pass
        brand_layout.addWidget(self.brand_title)
        self.brand_sub.setVisible(False)
        brand_layout.addWidget(self.brand_compact_icon)
        for w in (self.brand_box, self.brand_title, self.brand_compact_icon):
            try:
                w.installEventFilter(self)
                w.setCursor(Qt.PointingHandCursor)
                self._brand_click_targets.add(w)
            except Exception:
                pass
        sidebar_layout.addWidget(self.brand_box)

        session = QFrame(self.sidebar)
        self.session_card = session
        session.setObjectName('sessionCard')
        try:
            session.setCursor(Qt.PointingHandCursor)
        except Exception:
            pass
        session_layout = QVBoxLayout(session)
        try:
            session_layout.setContentsMargins(14, 12, 14, 12)
            session_layout.setSpacing(2)
        except Exception:
            pass
        self.session_status = QLabel(self._t('ui.session_inactive'))
        self.session_status.setObjectName('sessionStatus')
        self.session_user = QLabel(self._t('ui.sign_in_to_account'))
        self.session_user.setObjectName('sessionUser')
        self.session_project = QLabel(_DEFAULT_SESSION_PROJECT)
        self.session_project.setObjectName('sessionProject')
        self.session_compact_icon = QLabel('')
        self.session_compact_icon.setObjectName('sessionCompactIcon')
        try:
            self.session_compact_icon.setAlignment(Qt.AlignCenter)
            self.session_compact_icon.setFixedSize(34, 34)
        except Exception:
            pass
        for w in (session, self.session_status, self.session_user, self.session_project, self.session_compact_icon):
            try:
                w.installEventFilter(self)
                self._session_click_targets.add(w)
            except Exception:
                pass
        session_layout.addWidget(self.session_status)
        session_layout.addWidget(self.session_user)
        session_layout.addWidget(self.session_project)
        session_layout.addWidget(self.session_compact_icon)
        sidebar_layout.addWidget(session)

        self.nav_host = QWidget(self.sidebar)
        self.nav_host.setObjectName('navHost')
        self.nav_layout = QVBoxLayout(self.nav_host)
        try:
            self.nav_layout.setContentsMargins(0, 8, 0, 0)
            self.nav_layout.setSpacing(8)
        except Exception:
            pass
        sidebar_layout.addWidget(self.nav_host, 1)
        self._sidebar_layout = sidebar_layout

        footer_row = QHBoxLayout()
        self.footer_version = QLabel(f'v{APP_VERSION}')
        self.footer_version.setObjectName('footerText')
        self.footer_hint = None
        self.theme_toggle_btn = QToolButton(self.sidebar)
        self.theme_toggle_btn.setObjectName('themeToggle')
        self.theme_toggle_btn.setCheckable(False)
        self.theme_toggle_btn.setText('☀')
        self.theme_toggle_btn.setToolTip(self._t('ui.main.toggle_theme'))
        self.theme_toggle_btn.clicked.connect(self._cycle_theme)
        self.sidebar_toggle_btn = QToolButton(self.sidebar)
        self.sidebar_toggle_btn.setObjectName('sidebarToggle')
        self.sidebar_toggle_btn.setArrowType(Qt.LeftArrow)
        self.sidebar_toggle_btn.setToolTip(self._t('ui.main.collapse_menu'))
        self.sidebar_toggle_btn.clicked.connect(self._toggle_sidebar_collapse)
        footer_row.addWidget(self.footer_version)
        footer_row.addStretch(1)
        footer_row.addWidget(self.sidebar_toggle_btn)
        footer_row.addSpacing(6)
        footer_row.addWidget(self.theme_toggle_btn)
        sidebar_layout.addLayout(footer_row)

        # Правая часть контента
        self.content_frame = QFrame(root)
        self.content_frame.setObjectName('contentFrame')
        content_layout = QVBoxLayout(self.content_frame)
        try:
            content_layout.setContentsMargins(14, 6, 14, 10)
            content_layout.setSpacing(4)
        except Exception:
            pass

        self.page_tag = QLabel('BOOT')
        self.page_tag.setObjectName('headerTag')
        self.page_title = QLabel(self._t('ui.main.starting_app'))
        self.page_title.setObjectName('headerTitle')
        self.page_title.setWordWrap(True)
        self.page_subtitle = QLabel(self._t('ui.main.loading_modules_tabs'))
        self.page_subtitle.setObjectName('headerSubtitle')
        self.page_subtitle.setWordWrap(True)

        self.header_prefix_host = QWidget(self.content_frame)
        self.header_prefix_host.setObjectName('headerPrefixHost')
        header_prefix_layout = QHBoxLayout(self.header_prefix_host)
        try:
            header_prefix_layout.setContentsMargins(0, 0, 0, 0)
            header_prefix_layout.setSpacing(6)
        except Exception:
            pass
        self.header_prefix_label = QLabel(
            translate_key('ui.prefixes', self._ui_lang, 'Prefixes:')
        )
        self.header_prefix_label.setObjectName('headerPrefixLabel')
        header_prefix_layout.addWidget(self.header_prefix_label)
        self.header_ns_combo = QComboBox(self.header_prefix_host)
        self.header_ns_combo.setObjectName('headerPrefixCombo')
        self.header_ns_combo.setEditable(False)
        try:
            self.header_ns_combo.setMinimumWidth(170)
            self.header_ns_combo.setMaximumWidth(240)
            self.header_ns_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        except Exception:
            pass
        header_prefix_layout.addWidget(self.header_ns_combo)
        self.header_prefix_help_btn = QToolButton(self.header_prefix_host)
        self.header_prefix_help_btn.setObjectName('infoButton')
        self.header_prefix_help_btn.setText('?')
        self.header_prefix_help_btn.setToolTip(
            translate_key('ui.help', self._ui_lang, 'Help')
        )
        try:
            self.header_prefix_help_btn.setFixedSize(23, 23)
            self.header_prefix_help_btn.setCursor(Qt.PointingHandCursor)
        except Exception:
            pass
        self.header_prefix_help_btn.clicked.connect(self._show_header_prefix_help)
        header_prefix_layout.addWidget(self.header_prefix_help_btn)
        self._header_prefix_help_text = ''
        self.header_prefix_host.setVisible(False)

        self.header_update_btn = QPushButton(self._t('ui.check_updates'))
        self.header_update_btn.setObjectName('overviewUpdateButton')
        try:
            self.header_update_btn.setFixedSize(176, 27)
        except Exception:
            pass
        self.header_update_btn.clicked.connect(self._check_updates_from_header)
        self.header_update_btn.setVisible(False)

        self.header_actions_host = QWidget(self.content_frame)
        header_actions_layout = QHBoxLayout(self.header_actions_host)
        try:
            header_actions_layout.setContentsMargins(0, 0, 0, 0)
            header_actions_layout.setSpacing(8)
        except Exception:
            pass
        header_actions_layout.addWidget(self.header_prefix_host, 0, Qt.AlignTop | Qt.AlignRight)
        header_actions_layout.addWidget(self.header_update_btn, 0, Qt.AlignTop | Qt.AlignRight)

        content_layout.addWidget(self.page_tag)
        content_layout.addWidget(self.page_title)
        subtitle_row = QHBoxLayout()
        try:
            subtitle_row.setContentsMargins(0, 0, 0, 0)
            subtitle_row.setSpacing(8)
        except Exception:
            pass
        subtitle_row.addWidget(self.page_subtitle, 1)
        subtitle_row.addWidget(self.header_actions_host, 0, Qt.AlignTop | Qt.AlignRight)
        content_layout.addLayout(subtitle_row)

        self.tabs = QTabWidget(self.content_frame)
        self.tabs.setObjectName('contentTabs')
        try:
            self.tabs.tabBar().hide()
        except Exception:
            pass
        content_layout.addWidget(self.tabs, 1)

        shell.addWidget(self.sidebar)
        shell.addWidget(self.content_frame, 1)
        self._refresh_compact_icons()
        self._apply_sidebar_collapse_mode()

    def _rebuild_sidebar_nav(self):
        try:
            while self.nav_layout.count():
                item = self.nav_layout.takeAt(0)
                wid = item.widget()
                if wid is not None:
                    wid.deleteLater()
        except Exception:
            pass
        self._nav_buttons = {}
        for index, text_key, icon_key in self._NAV_ITEMS:
            label = self._nav_title(text_key)
            btn = QPushButton(label, self.nav_host)
            btn.setObjectName('navButton')
            btn.setCheckable(True)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.setProperty('nav_label', label)
            btn.setProperty('nav_icon_key', icon_key)
            try:
                btn.setIconSize(QSize(18, 18))
            except Exception:
                pass
            btn.clicked.connect(
                lambda _checked=False, i=index: self._set_tab_from_nav(i))
            self.nav_layout.addWidget(btn)
            self._nav_buttons[index] = btn
        self.nav_layout.addStretch(1)
        self._refresh_nav_icons()
        self._apply_sidebar_collapse_mode()
        self._sync_sidebar_tab_state(self.tabs.currentIndex())
        QTimer.singleShot(0, self._apply_depth_effects)

    def _set_tab_from_nav(self, index: int):
        try:
            if 0 <= index < self.tabs.count():
                self.tabs.setCurrentIndex(index)
                self._sync_sidebar_tab_state(index)
        except Exception:
            pass

    def _icon_color(self) -> str:
        if self._theme_mode == 'light':
            return '#1d2a35'
        if self._theme_mode == 'dark':
            return '#e8eef4'
        return '#f4f8fb'

    def _tinted_standard_icon(self, sp_icon, color_hex: str, size: int = 16) -> QIcon:
        try:
            base = self.style().standardIcon(sp_icon).pixmap(size, size)
            if base.isNull():
                return QIcon()
            tint = QPixmap(base.size())
            tint.fill(Qt.transparent)
            p = QPainter(tint)
            p.setRenderHint(QPainter.Antialiasing)
            p.drawPixmap(0, 0, base)
            p.setCompositionMode(QPainter.CompositionMode_SourceIn)
            p.fillRect(tint.rect(), QColor(color_hex))
            p.end()
            return QIcon(tint)
        except Exception:
            return QIcon()

    def _draw_nav_icon(self, icon_key: str, color_hex: str, size: int = 16) -> QIcon:
        """Рисует 1:1 Lucide-иконки (как в v2 React sidebar)."""
        try:
            nodes = {
                # v2/src/components/Sidebar.jsx
                'dashboard': [
                    ('rect', {'width': '7', 'height': '9', 'x': '3', 'y': '3', 'rx': '1'}),
                    ('rect', {'width': '7', 'height': '5', 'x': '14', 'y': '3', 'rx': '1'}),
                    ('rect', {'width': '7', 'height': '9', 'x': '14', 'y': '12', 'rx': '1'}),
                    ('rect', {'width': '7', 'height': '5', 'x': '3', 'y': '16', 'rx': '1'}),
                ],
                'auth': [
                    ('path', {'d': 'M2 18v3c0 .6.4 1 1 1h4v-3h3v-3h2l1.4-1.4a6.5 6.5 0 1 0-4-4Z'}),
                    ('circle', {'cx': '16.5', 'cy': '7.5', 'r': '.5'}),
                ],
                'read': [
                    ('path', {'d': 'M4 22h14a2 2 0 0 0 2-2V7.5L14.5 2H6a2 2 0 0 0-2 2v3'}),
                    ('polyline', {'points': '14 2 14 8 20 8'}),
                    ('path', {'d': 'M5 17a3 3 0 1 0 0-6 3 3 0 0 0 0 6z'}),
                    ('path', {'d': 'm9 18-1.5-1.5'}),
                ],
                'replace': [
                    ('path', {'d': 'M14 4c0-1.1.9-2 2-2'}),
                    ('path', {'d': 'M20 2c1.1 0 2 .9 2 2'}),
                    ('path', {'d': 'M22 8c0 1.1-.9 2-2 2'}),
                    ('path', {'d': 'M16 10c-1.1 0-2-.9-2-2'}),
                    ('path', {'d': 'm3 7 3 3 3-3'}),
                    ('path', {'d': 'M6 10V5c0-1.7 1.3-3 3-3h1'}),
                    ('rect', {'width': '8', 'height': '8', 'x': '2', 'y': '14', 'rx': '2'}),
                    ('path', {'d': 'M14 14c1.1 0 2 .9 2 2v4c0 1.1-.9 2-2 2'}),
                    ('path', {'d': 'M20 14c1.1 0 2 .9 2 2v4c0 1.1-.9 2-2 2'}),
                ],
                'create': [
                    ('path', {'d': 'm12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z'}),
                    ('path', {'d': 'm6.08 9.5-3.5 1.6a1 1 0 0 0 0 1.81l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9a1 1 0 0 0 0-1.83l-3.5-1.59'}),
                    ('path', {'d': 'm6.08 14.5-3.5 1.6a1 1 0 0 0 0 1.81l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9a1 1 0 0 0 0-1.83l-3.5-1.59'}),
                ],
                'rename': [
                    ('path', {'d': 'm16 3 4 4-4 4'}),
                    ('path', {'d': 'M20 7H4'}),
                    ('path', {'d': 'm8 21-4-4 4-4'}),
                    ('path', {'d': 'M4 17h16'}),
                ],
                'cleanup': [
                    ('path', {'d': 'M20 10a1 1 0 0 0 1-1V6a1 1 0 0 0-1-1h-2.5a1 1 0 0 1-.8-.4l-.9-1.2A1 1 0 0 0 15 3h-2a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1Z'}),
                    ('path', {'d': 'M20 21a1 1 0 0 0 1-1v-3a1 1 0 0 0-1-1h-2.9a1 1 0 0 1-.88-.55l-.42-.85a1 1 0 0 0-.92-.6H13a1 1 0 0 0-1 1v5a1 1 0 0 0 1 1Z'}),
                    ('path', {'d': 'M3 5a2 2 0 0 0 2 2h3'}),
                    ('path', {'d': 'M3 3v13a2 2 0 0 0 2 2h3'}),
                ],
                'sync': [
                    ('path', {'d': 'M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71'}),
                    ('path', {'d': 'M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71'}),
                ],
                'about': [
                    ('path', {'d': 'M3.85 8.62a4 4 0 0 1 4.78-4.77 4 4 0 0 1 6.74 0 4 4 0 0 1 4.78 4.78 4 4 0 0 1 0 6.74 4 4 0 0 1-4.77 4.78 4 4 0 0 1-6.75 0 4 4 0 0 1-4.78-4.77 4 4 0 0 1 0-6.76Z'}),
                    ('line', {'x1': '12', 'x2': '12', 'y1': '16', 'y2': '12'}),
                    ('line', {'x1': '12', 'x2': '12.01', 'y1': '8', 'y2': '8'}),
                ],
            }.get(icon_key)
            if not nodes:
                return QIcon()

            elements = []
            for tag, attrs in nodes:
                attr_text = ' '.join(f'{k}="{v}"' for k, v in attrs.items())
                elements.append(f'<{tag} {attr_text}/>')

            svg = (
                f'<svg xmlns="http://www.w3.org/2000/svg" '
                f'viewBox="0 0 24 24" fill="none" stroke="{color_hex}" '
                f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
                f'{"".join(elements)}'
                f'</svg>'
            )
            renderer = QSvgRenderer(QByteArray(svg.encode('utf-8')))
            if not renderer.isValid():
                return QIcon()

            pix = QPixmap(size, size)
            pix.fill(Qt.transparent)
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.Antialiasing)
            renderer.render(painter)
            painter.end()
            return QIcon(pix)
        except Exception:
            return QIcon()

    def _refresh_nav_icons(self):
        style_map = {
            'auth': QStyle.SP_DialogApplyButton,
            'read': QStyle.SP_FileDialogContentsView,
            'replace': QStyle.SP_BrowserReload,
            'create': QStyle.SP_FileDialogNewFolder,
            'rename': QStyle.SP_ArrowRight,
            'cleanup': QStyle.SP_TrashIcon,
            'sync': QStyle.SP_BrowserReload,
        }
        color = self._icon_color()
        for btn in self._nav_buttons.values():
            try:
                key = str(btn.property('nav_icon_key') or '')
                icon = self._draw_nav_icon(key, color, 20 if self._sidebar_collapsed else 18)
                if icon.isNull():
                    icon = self._tinted_standard_icon(
                        style_map.get(key, QStyle.SP_FileIcon),
                        color,
                        20 if self._sidebar_collapsed else 18,
                    )
                btn.setIcon(icon)
            except Exception:
                pass

    def _refresh_compact_icons(self):
        color = self._icon_color()
        try:
            ico_brand = self._tinted_standard_icon(QStyle.SP_DesktopIcon, color, 18)
            self.brand_compact_icon.setPixmap(ico_brand.pixmap(18, 18))
            self.brand_compact_icon.setText('')
        except Exception:
            pass
        try:
            ico_session = self._tinted_standard_icon(QStyle.SP_DialogYesButton, color, 16)
            self.session_compact_icon.setPixmap(ico_session.pixmap(16, 16))
            self.session_compact_icon.setText('')
        except Exception:
            pass

    def _sync_sidebar_tab_state(self, index: int):
        for idx, btn in self._nav_buttons.items():
            try:
                btn.setChecked(idx == index)
            except Exception:
                pass
        try:
            active = bool(index == 0)
            self.session_card.setProperty('active', active)
            st = self.session_card.style()
            if st:
                st.unpolish(self.session_card)
                st.polish(self.session_card)
            self.session_card.update()
        except Exception:
            pass
        try:
            brand_active = bool(index == 7)
            self.brand_box.setProperty('active', brand_active)
            st = self.brand_box.style()
            if st:
                st.unpolish(self.brand_box)
                st.polish(self.brand_box)
            self.brand_box.update()
        except Exception:
            pass

    def _update_header_for_tab(self, index: int):
        tag, title, subtitle = self._tab_meta_map().get(
            index,
            (
                self._t('ui.main.tab.boot.tag'),
                self._t('ui.main.starting_app'),
                self._t('ui.main.loading_modules_tabs'),
            )
        )
        try:
            self.page_tag.setText(tag)
            self.page_title.setText(title)
            self.page_subtitle.setText(subtitle)
        except Exception:
            pass
        try:
            show_updates = bool(index == 7)
            self.header_update_btn.setVisible(show_updates)
            show_prefix = bool(index in (1, 2, 3, 4, 5, 6))
            if hasattr(self, 'header_prefix_host') and self.header_prefix_host is not None:
                self.header_prefix_host.setVisible(show_prefix)
            if show_prefix:
                if hasattr(self, 'header_prefix_label') and self.header_prefix_label is not None:
                    self.header_prefix_label.setText(
                        translate_key('ui.prefixes', self._ui_lang, 'Prefixes:')
                    )
                if hasattr(self, 'header_prefix_help_btn') and self.header_prefix_help_btn is not None:
                    self.header_prefix_help_btn.setToolTip(
                        translate_key('ui.help', self._ui_lang, 'Help')
                    )
                self._header_prefix_help_text = self._current_prefix_help_text_for_tab(index)
            else:
                self._header_prefix_help_text = ''
        except Exception:
            pass

    def _current_prefix_help_text_for_tab(self, index: int) -> str:
        """Возвращает текст общей справки для header-блока префиксов."""
        key_map = {
            1: 'help.parse.source',
            2: 'help.replace.main',
            3: 'help.create.main',
            4: 'help.rename.main',
            5: 'help.cleanup.source',
            6: 'help.sync.main',
        }
        key = key_map.get(int(index), '')
        if not key:
            return ''
        return translate_key(key, self._ui_lang, '')

    def _show_header_prefix_help(self):
        """Показывает общую справку для префиксов на текущей странице."""
        try:
            text = str(self._header_prefix_help_text or '').strip()
        except Exception:
            text = ''
        if not text:
            try:
                idx = self.tabs.currentIndex() if hasattr(self, 'tabs') else -1
                text = self._current_prefix_help_text_for_tab(idx)
            except Exception:
                text = ''
        if not text:
            return
        try:
            title = translate_key('ui.help', self._ui_lang, 'Help')
            ui_show_help_dialog(self, text, title)
        except Exception:
            pass

    def _check_updates_from_header(self):
        try:
            auth = getattr(self, 'auth_tab', None)
            if auth and hasattr(auth, 'check_updates'):
                auth.check_updates()
        except Exception:
            pass

    def _refresh_session_card(self):
        try:
            if self.current_user:
                self.session_status.setText(self._t('ui.session_active'))
                self.session_user.setText(self.current_user)
                fam = self.current_family or 'wikipedia'
                lang = self.current_lang or 'ru'
                self.session_project.setText(f'{fam} / {lang}')
            else:
                self.session_status.setText(self._t('ui.session_inactive'))
                self.session_user.setText(self._t('ui.sign_in_to_account'))
                self.session_project.setText(_DEFAULT_SESSION_PROJECT)
            if getattr(self, 'overview_tab', None) is not None and hasattr(self.overview_tab, 'update_session'):
                self.overview_tab.update_session(self.current_user, self.current_family, self.current_lang)
        except Exception:
            pass

    def _load_ui_settings(self):
        try:
            mode = (self._settings.value('ui/theme_mode', 'teal') or 'teal').strip().lower()
            if mode in ('light', 'teal', 'dark'):
                self._theme_mode = mode
            else:
                self._theme_mode = 'teal'
        except Exception:
            pass
        try:
            raw = self._settings.value('ui/sidebar_collapsed', False)
            if isinstance(raw, str):
                self._sidebar_collapsed = raw.strip().lower() in ('1', 'true', 'yes', 'on')
            else:
                self._sidebar_collapsed = bool(raw)
        except Exception:
            pass
        try:
            ui_lang = (self._settings.value('ui/lang', 'ru') or 'ru').strip().lower()
            self._ui_lang = 'en' if ui_lang.startswith('en') else 'ru'
        except Exception:
            pass
        try:
            for key in self._operation_stats.keys():
                raw = self._settings.value(f'stats/{key}', 0)
                try:
                    self._operation_stats[key] = int(raw or 0)
                except Exception:
                    self._operation_stats[key] = 0
        except Exception:
            pass
        try:
            raw_hist = self._settings.value('stats/history', '[]')
            if isinstance(raw_hist, (list, tuple)):
                hist = list(raw_hist)
            else:
                hist = json.loads(str(raw_hist or '[]'))
            if isinstance(hist, list):
                self._operation_history = [x for x in hist if isinstance(x, dict)][-500:]
            else:
                self._operation_history = []
        except Exception:
            self._operation_history = []

    def _save_ui_settings(self):
        try:
            self._settings.setValue('ui/theme_mode', self._theme_mode)
            self._settings.setValue('ui/sidebar_collapsed', self._sidebar_collapsed)
            self._settings.setValue('ui/lang', self._ui_lang)
            for key, value in self._operation_stats.items():
                self._settings.setValue(f'stats/{key}', int(value))
            self._settings.setValue('stats/history', json.dumps(self._operation_history, ensure_ascii=False))
            self._settings.sync()
        except Exception:
            pass

    def _toggle_sidebar_collapse(self):
        self._sidebar_collapsed = not self._sidebar_collapsed
        self._apply_sidebar_collapse_mode()
        self._save_ui_settings()

    def _apply_sidebar_collapse_mode(self):
        collapsed = bool(self._sidebar_collapsed)
        try:
            if collapsed:
                self.sidebar.setMinimumWidth(58)
                self.sidebar.setMaximumWidth(58)
            else:
                self.sidebar.setMinimumWidth(248)
                self.sidebar.setMaximumWidth(248)
            if hasattr(self, '_sidebar_layout') and self._sidebar_layout is not None:
                if collapsed:
                    self._sidebar_layout.setContentsMargins(2, 10, 6, 8)
                    self._sidebar_layout.setSpacing(6)
                else:
                    self._sidebar_layout.setContentsMargins(10, 12, 10, 10)
                    self._sidebar_layout.setSpacing(10)
            self.sidebar.setProperty('compact', collapsed)
            st = self.sidebar.style()
            if st:
                st.unpolish(self.sidebar)
                st.polish(self.sidebar)
        except Exception:
            pass
        for w, visible in (
            (getattr(self, 'brand_box', None), not collapsed),
            (getattr(self, 'session_card', None), not collapsed),
            (getattr(self, 'brand_title', None), not collapsed),
            (getattr(self, 'brand_sub', None), False),
            (getattr(self, 'brand_compact_icon', None), collapsed),
            (getattr(self, 'session_status', None), not collapsed),
            (getattr(self, 'session_user', None), not collapsed),
            (getattr(self, 'session_project', None), not collapsed),
            (getattr(self, 'session_compact_icon', None), collapsed),
            (getattr(self, 'footer_version', None), not collapsed),
            (getattr(self, 'theme_toggle_btn', None), not collapsed),
        ):
            try:
                if w is not None:
                    w.setVisible(visible)
            except Exception:
                pass
        try:
            if hasattr(self, 'sidebar_toggle_btn') and self.sidebar_toggle_btn:
                self.sidebar_toggle_btn.setArrowType(Qt.RightArrow if collapsed else Qt.LeftArrow)
                self.sidebar_toggle_btn.setToolTip(
                    self._t('ui.main.expand_menu') if collapsed else self._t('ui.main.collapse_menu')
                )
        except Exception:
            pass
        for idx, btn in self._nav_buttons.items():
            try:
                label = btn.property('nav_label') or ''
                if collapsed:
                    btn.setText('')
                    btn.setToolTip(str(label))
                    btn.setMinimumWidth(40)
                    btn.setMaximumWidth(40)
                    btn.setMinimumHeight(40)
                    btn.setMaximumHeight(40)
                    btn.setIconSize(QSize(20, 20))
                    self.nav_layout.setAlignment(btn, Qt.AlignHCenter)
                else:
                    btn.setText(str(label))
                    btn.setToolTip('')
                    btn.setMinimumWidth(0)
                    btn.setMaximumWidth(16777215)
                    btn.setMinimumHeight(44)
                    btn.setMaximumHeight(44)
                    btn.setIconSize(QSize(18, 18))
                    self.nav_layout.setAlignment(btn, Qt.Alignment())
                try:
                    st = btn.style()
                    if st:
                        st.unpolish(btn)
                        st.polish(btn)
                except Exception:
                    pass
            except Exception:
                pass
        try:
            if hasattr(self, 'nav_host') and self.nav_host is not None:
                st = self.nav_host.style()
                if st:
                    st.unpolish(self.nav_host)
                    st.polish(self.nav_host)
            self._refresh_nav_icons()
            self._refresh_compact_icons()
            self.sidebar.updateGeometry()
            self.sidebar.update()
        except Exception:
            pass

    def _apply_windows_titlebar_theme(self):
        """На Windows включает тёмный title bar в dark-теме."""
        try:
            if not sys.platform.startswith('win'):
                return
            import ctypes
            hwnd = int(self.winId())
            dark = self._theme_mode in ('teal', 'dark')
            value = ctypes.c_int(1 if dark else 0)
            # Для разных версий Windows используются атрибуты 20 и 19.
            for attr in (20, 19):
                try:
                    ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        ctypes.c_void_p(hwnd),
                        ctypes.c_uint(attr),
                        ctypes.byref(value),
                        ctypes.sizeof(value),
                    )
                except Exception:
                    pass
            # Для новых сборок Windows дополнительно задаем цвет caption.
            try:
                def _rgb(r: int, g: int, b: int) -> int:
                    return (b << 16) | (g << 8) | r

                caption = ctypes.c_uint(_rgb(6, 31, 49) if dark else _rgb(245, 248, 252))
                text = ctypes.c_uint(_rgb(236, 247, 252) if dark else _rgb(34, 49, 61))
                border = ctypes.c_uint(_rgb(6, 31, 49) if dark else _rgb(210, 225, 237))
                for attr, val in ((35, caption), (36, text), (34, border)):
                    ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        ctypes.c_void_p(hwnd),
                        ctypes.c_uint(attr),
                        ctypes.byref(val),
                        ctypes.sizeof(val),
                    )
            except Exception:
                pass
        except Exception:
            pass

    def _theme_cycle_order(self) -> tuple[str, ...]:
        return ('teal', 'dark', 'light')

    def _theme_button_icon(self, mode: str) -> str:
        return {
            'teal': '🌊',
            'dark': '🌙',
            'light': '☀',
        }.get(mode, '☀')

    def _theme_title(self, mode: str) -> str:
        return {
            'light': 'winter light',
            'teal': 'twilight teal',
            'dark': 'midnight dark',
        }.get(mode, 'light')

    def _tab_meta_map(self) -> dict[int, tuple[str, str, str]]:
        meta = {}
        for index, (tag_key, title_key, subtitle_key) in self._TAB_META.items():
            meta[index] = (
                self._t(tag_key),
                self._t(title_key),
                self._t(subtitle_key),
            )
        return meta

    def _nav_title(self, text_key: str) -> str:
        return self._t(text_key)

    def _i18n_pairs(self) -> list[tuple[str, str]]:
        pairs = load_translation_pairs()
        return list(pairs) if pairs else []

    def _translate_value(self, value: str, to_lang: str) -> str:
        try:
            text = value or ''
            pairs = self._i18n_pairs()
            if to_lang == 'en':
                mapping = {}
                for ru, en in pairs:
                    # Keep the first match so generic labels are not overridden
                    # by later context-specific variants with the same source text.
                    mapping.setdefault(ru, en)
            else:
                mapping = {}
                for ru, en in pairs:
                    mapping.setdefault(en, ru)
            if text in mapping:
                return mapping[text]
            # Учитываем мнемоники Qt (&) и экранированные &&.
            if '&' in text:
                alt = text.replace('&', '&&')
                if alt in mapping:
                    return mapping[alt]
            if '&&' in text:
                alt = text.replace('&&', '&')
                if alt in mapping:
                    return mapping[alt]
            # Частый случай для простых жирных заголовков вида <b>...</b>.
            if text.startswith('<b>') and text.endswith('</b>'):
                inner = text[3:-4]
                if inner in mapping:
                    return f'<b>{mapping[inner]}</b>'
            # Частичный перевод для составных строк (help/log/popup).
            out = text
            for src, dst in sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True):
                if src and src in out:
                    out = out.replace(src, dst)
            return out
        except Exception:
            return value

    def translate_ui_text(self, value: str) -> str:
        target = 'en' if self._ui_lang == 'en' else 'ru'
        return self._translate_value(value, target)

    def _apply_widget_text_translation(self):
        target = 'en' if self._ui_lang == 'en' else 'ru'
        try:
            for cls in (QLabel, QPushButton, QCheckBox, QToolButton):
                for w in self.findChildren(cls):
                    try:
                        txt = w.text()
                        new_txt = self._translate_value(txt, target)
                        if new_txt != txt:
                            w.setText(new_txt)
                        tip = w.toolTip()
                        new_tip = self._translate_value(tip, target)
                        if new_tip != tip:
                            w.setToolTip(new_tip)
                    except Exception:
                        pass
            for box in self.findChildren(QGroupBox):
                try:
                    title = box.title()
                    new_title = self._translate_value(title, target)
                    if new_title != title:
                        box.setTitle(new_title)
                except Exception:
                    pass
            for cls in (QLineEdit, QTextEdit, QPlainTextEdit):
                for w in self.findChildren(cls):
                    try:
                        if isinstance(w, QLineEdit):
                            txt = w.text()
                            new_txt = self._translate_value(txt, target)
                            if new_txt != txt and (
                                txt in {ru for ru, _ in self._i18n_pairs()} or
                                txt in {en for _, en in self._i18n_pairs()}
                            ):
                                w.setText(new_txt)
                        ph = w.placeholderText()
                        new_ph = self._translate_value(ph, target)
                        if new_ph != ph:
                            w.setPlaceholderText(new_ph)
                        tip = w.toolTip()
                        new_tip = self._translate_value(tip, target)
                        if new_tip != tip:
                            w.setToolTip(new_tip)
                    except Exception:
                        pass
            for bar in self.findChildren(QProgressBar):
                try:
                    fmt = bar.format()
                    new_fmt = self._translate_value(fmt, target)
                    if new_fmt != fmt:
                        bar.setFormat(new_fmt)
                    tip = bar.toolTip()
                    new_tip = self._translate_value(tip, target)
                    if new_tip != tip:
                        bar.setToolTip(new_tip)
                except Exception:
                    pass
            for tree in self.findChildren(QTreeWidget):
                try:
                    hdr = tree.headerItem()
                    if hdr is not None:
                        for col in range(tree.columnCount()):
                            txt = hdr.text(col)
                            new_txt = self._translate_value(txt, target)
                            if new_txt != txt:
                                hdr.setText(col, new_txt)
                except Exception:
                    pass
                try:
                    for item in tree.findItems('', Qt.MatchContains | Qt.MatchRecursive, 0):
                        for col in range(tree.columnCount()):
                            txt = item.text(col)
                            new_txt = self._translate_value(txt, target)
                            if new_txt != txt:
                                item.setText(col, new_txt)
                            tip = item.toolTip(col)
                            new_tip = self._translate_value(tip, target)
                            if new_tip != tip:
                                item.setToolTip(col, new_tip)
                except Exception:
                    pass
            for w in self.findChildren(QWidget):
                try:
                    tip = w.toolTip()
                    new_tip = self._translate_value(tip, target)
                    if new_tip != tip:
                        w.setToolTip(new_tip)
                except Exception:
                    pass
        except Exception:
            pass

    def _install_localized_context_menus(self):
        """Локализует контекстные меню для текстовых полей."""
        try:
            for cls in (QLineEdit, QTextEdit, QPlainTextEdit):
                for w in self.findChildren(cls):
                    try:
                        install_localized_context_menu(w)
                    except Exception:
                        pass
        except Exception:
            pass

    def _normalize_line_edit_alignment(self):
        """Держит все QLineEdit с левым выравниванием и левым началом текста."""
        try:
            for edit in self.findChildren(QLineEdit):
                try:
                    edit.setAlignment(Qt.AlignLeft)
                except Exception:
                    pass
                try:
                    if not bool(edit.property('_wct_left_anchor')):
                        def _pin_start(le=edit):
                            try:
                                if not le.hasFocus():
                                    le.setCursorPosition(0)
                                    le.deselect()
                            except Exception:
                                pass
                        edit.editingFinished.connect(_pin_start)
                        edit.setProperty('_wct_left_anchor', True)
                    if not edit.hasFocus():
                        edit.setCursorPosition(0)
                        edit.deselect()
                except Exception:
                    pass
        except Exception:
            pass

    def _normalize_depth_spins(self):
        """Стабилизирует компактный размер и стрелки у поля «Глубина» после смены темы."""
        try:
            for spin in self.findChildren(QSpinBox):
                try:
                    if spin.objectName() != 'depthSpin':
                        continue
                    spin.setMaximum(max(99, int(spin.maximum())))
                    spin.setAlignment(Qt.AlignCenter)
                    spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
                    spin.setFixedWidth(34)
                except Exception:
                    pass
        except Exception:
            pass

    def set_ui_language(self, lang: str):
        self._ui_lang = 'en' if str(lang).lower().startswith('en') else 'ru'
        set_runtime_ui_language(self._ui_lang)
        self._save_ui_settings()
        try:
            self._rebuild_sidebar_nav()
        except Exception:
            pass
        self._update_header_for_tab(self.tabs.currentIndex() if hasattr(self, 'tabs') else -1)
        self._apply_widget_text_translation()
        try:
            from .widgets.shared_panels import CategorySourcePanel
            for panel in self.findChildren(CategorySourcePanel):
                try:
                    panel.refresh_localized_texts()
                except Exception:
                    pass
        except Exception:
            pass
        self._install_localized_context_menus()
        self._normalize_line_edit_alignment()
        try:
            if hasattr(self, 'header_update_btn') and self.header_update_btn is not None:
                update_text = translate_key('ui.check_updates', self._ui_lang, 'Check updates')
                self.header_update_btn.setText(update_text)
                self.header_update_btn.setToolTip(update_text)
        except Exception:
            pass
        try:
            if getattr(self, 'overview_tab', None) is not None and hasattr(self.overview_tab, 'set_ui_language'):
                self.overview_tab.set_ui_language(self._ui_lang)
                if hasattr(self.overview_tab, 'update_stats'):
                    self.overview_tab.update_stats(self._operation_stats, self._operation_history)
                if hasattr(self.overview_tab, 'update_session'):
                    self.overview_tab.update_session(
                        self.current_user,
                        self.current_family,
                        self.current_lang,
                    )
        except Exception:
            pass
        # Обновляем карточку сессии после синхронизации языка overview_tab,
        # чтобы не было рассинхрона «Активно/Active».
        self._refresh_session_card()
        try:
            self._set_theme(self._theme_mode)
        except Exception:
            pass

    def _install_messagebox_i18n(self):
        try:
            MainWindow._msgbox_i18n_owner = self
        except Exception:
            pass
        if MainWindow._msgbox_i18n_installed:
            return
        MainWindow._msgbox_i18n_installed = True
        try:
            QMessageBox._wct_orig_information = QMessageBox.information
            QMessageBox._wct_orig_warning = QMessageBox.warning
            QMessageBox._wct_orig_critical = QMessageBox.critical
            QMessageBox._wct_orig_question = QMessageBox.question
            QMessageBox._wct_orig_exec = QMessageBox.exec
        except Exception:
            return

        def _owner():
            wnd = MainWindow._msgbox_i18n_owner
            return wnd if wnd is not None else None

        def _tx(value):
            wnd = _owner()
            try:
                if wnd is not None and hasattr(wnd, 'translate_ui_text'):
                    return wnd.translate_ui_text(value or '')
            except Exception:
                pass
            return value

        def _wrap_info(parent, title, text, *args, **kwargs):
            return QMessageBox._wct_orig_information(parent, _tx(title), _tx(text), *args, **kwargs)

        def _wrap_warn(parent, title, text, *args, **kwargs):
            return QMessageBox._wct_orig_warning(parent, _tx(title), _tx(text), *args, **kwargs)

        def _wrap_crit(parent, title, text, *args, **kwargs):
            return QMessageBox._wct_orig_critical(parent, _tx(title), _tx(text), *args, **kwargs)

        def _wrap_question(parent, title, text, *args, **kwargs):
            return QMessageBox._wct_orig_question(parent, _tx(title), _tx(text), *args, **kwargs)

        def _wrap_exec(msgbox):
            try:
                msgbox.setWindowTitle(_tx(msgbox.windowTitle()))
                msgbox.setText(_tx(msgbox.text()))
                msgbox.setInformativeText(_tx(msgbox.informativeText()))
                msgbox.setDetailedText(_tx(msgbox.detailedText()))
                for btn in msgbox.buttons():
                    try:
                        btn.setText(_tx(btn.text()))
                    except Exception:
                        pass
            except Exception:
                pass
            return QMessageBox._wct_orig_exec(msgbox)

        QMessageBox.information = staticmethod(_wrap_info)
        QMessageBox.warning = staticmethod(_wrap_warn)
        QMessageBox.critical = staticmethod(_wrap_crit)
        QMessageBox.question = staticmethod(_wrap_question)
        QMessageBox.exec = _wrap_exec

    def _cycle_theme(self, *_args):
        order = self._theme_cycle_order()
        cur = self._theme_mode if self._theme_mode in order else 'teal'
        next_mode = order[(order.index(cur) + 1) % len(order)]
        self._set_theme(next_mode)

    def _apply_modern_theme(self):
        self._set_theme(self._theme_mode)

    def _set_theme(self, mode: str):
        self._theme_mode = mode if mode in ('dark', 'light', 'teal') else 'teal'
        try:
            if hasattr(self, 'theme_toggle_btn') and self.theme_toggle_btn is not None:
                self.theme_toggle_btn.blockSignals(True)
                self.theme_toggle_btn.setText(self._theme_button_icon(self._theme_mode))
                order = self._theme_cycle_order()
                next_mode = order[(order.index(self._theme_mode) + 1) % len(order)]
                self.theme_toggle_btn.setToolTip(
                    self._fmt(
                        'ui.main.theme_cycle_tooltip',
                        current=self._theme_title(self._theme_mode),
                        next=self._theme_title(next_mode),
                    )
                )
                self.theme_toggle_btn.blockSignals(False)
        except Exception:
            pass

        if self._theme_mode == 'light':
            qss = self._light_theme_qss()
        elif self._theme_mode == 'dark':
            qss = self._midnight_theme_qss()
        else:
            qss = self._dark_theme_qss()
        try:
            app = QApplication.instance()
            if app is not None:
                app.setStyleSheet(qss)
            else:
                self.setStyleSheet(qss)
        except Exception:
            try:
                self.setStyleSheet(qss)
            except Exception:
                pass

        self._save_ui_settings()
        QTimer.singleShot(0, self._apply_windows_titlebar_theme)
        QTimer.singleShot(0, self._refresh_nav_icons)
        QTimer.singleShot(0, self._refresh_compact_icons)
        QTimer.singleShot(0, self._ensure_button_widths)
        QTimer.singleShot(0, self._normalize_depth_spins)
        QTimer.singleShot(0, self._apply_depth_effects)
        try:
            auth = getattr(self, 'auth_tab', None)
            if auth is not None and hasattr(auth, 'refresh_theme_styles'):
                QTimer.singleShot(0, auth.refresh_theme_styles)
        except Exception:
            pass
        try:
            if getattr(self, 'overview_tab', None) is not None and hasattr(self.overview_tab, 'update_ui_context'):
                self.overview_tab.update_ui_context(self._theme_mode, self._ui_lang)
        except Exception:
            pass

    def _ensure_button_widths(self):
        """Не даём кнопкам стать уже их текста."""
        try:
            for btn in self.findChildren(QPushButton):
                try:
                    if btn.objectName() == 'navButton':
                        continue
                    if btn.maximumWidth() < 10000:
                        continue
                    need = btn.sizeHint().width() + 6
                    if need > btn.minimumWidth():
                        btn.setMinimumWidth(need)
                except Exception:
                    pass
            for btn in self.findChildren(QToolButton):
                try:
                    txt = (btn.text() or '').strip()
                    if not txt or txt == '…':
                        continue
                    if btn.maximumWidth() < 10000:
                        continue
                    need = btn.sizeHint().width() + 4
                    if need > btn.minimumWidth():
                        btn.setMinimumWidth(need)
                except Exception:
                    pass
        except Exception:
            pass

    def _shadow_color(self, key: str = 'panel') -> QColor:
        try:
            mode = str(self._theme_mode or 'teal').strip().lower()
        except Exception:
            mode = 'teal'
        if mode == 'light':
            palette = {
                'card': QColor(24, 47, 66, 62),
                'nav': QColor(20, 44, 63, 58),
                'panel': QColor(28, 56, 78, 52),
            }
        elif mode == 'dark':
            palette = {
                'card': QColor(0, 0, 0, 118),
                'nav': QColor(0, 0, 0, 96),
                'panel': QColor(0, 0, 0, 86),
            }
        else:
            palette = {
                'card': QColor(0, 0, 0, 122),
                'nav': QColor(0, 0, 0, 96),
                'panel': QColor(0, 0, 0, 86),
            }
        return palette.get(key, palette['panel'])

    def _set_shadow(
        self,
        widget: QWidget | None,
        *,
        key: str = 'panel',
        blur: float = 20.0,
        dx: float = 0.0,
        dy: float = 2.0,
    ) -> None:
        if widget is None:
            return
        try:
            effect = widget.graphicsEffect()
            if not isinstance(effect, QGraphicsDropShadowEffect):
                effect = QGraphicsDropShadowEffect(widget)
                widget.setGraphicsEffect(effect)
            effect.setColor(self._shadow_color(key))
            effect.setBlurRadius(float(max(8.0, blur)))
            effect.setOffset(float(dx), float(dy))
        except Exception:
            pass

    def _apply_depth_effects(self):
        """Добавляет мягкие тени для объёмного вида ключевых блоков."""
        try:
            self._set_shadow(getattr(self, 'brand_box', None), key='card', blur=28, dy=3)
            self._set_shadow(getattr(self, 'session_card', None), key='card', blur=28, dy=3)
        except Exception:
            pass
        # Сбрасываем эффект с кнопок меню, чтобы не было артефакта по левому краю.
        try:
            for btn in getattr(self, '_nav_buttons', {}).values():
                try:
                    btn.setGraphicsEffect(None)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            key_names = (
                'overviewSection',
                'settingsSection',
                'statsSection',
                'overviewMetaCard',
                'statsBarsPanel',
                'statsHistoryPanel',
            )
            for name in key_names:
                for w in self.findChildren(QWidget, name):
                    self._set_shadow(w, key='panel', blur=18, dy=2)
        except Exception:
            pass
        # На мелких статистических карточках тени не нужны: из-за blur они визуально "налезают".
        try:
            for name in ('statsTotalCard', 'statsRunChip'):
                for w in self.findChildren(QWidget, name):
                    try:
                        w.setGraphicsEffect(None)
                    except Exception:
                        pass
        except Exception:
            pass

    def _dark_theme_qss(self) -> str:
        qss = """
            QMainWindow, QWidget, QDialog, QMessageBox {
                color: #ddf1f3;
                font-family: "Trebuchet MS", "Segoe UI";
                font-size: 12px;
            }
            #windowRoot {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 1,
                    stop: 0 #08263a,
                    stop: 0.55 #11495f,
                    stop: 1 #1d6f76
                );
            }
            QDialog, QMessageBox, QMenu {
                background: #0a2a40;
                border: 1px solid rgba(113, 214, 219, 0.3);
            }
            QMenu {
                padding: 4px 2px;
            }
            QMenu::item {
                padding: 5px 12px;
                margin: 1px 2px;
                border-radius: 4px;
                background: transparent;
                color: #e6f3f6;
            }
            QMenu::item:selected {
                background: rgba(79, 132, 153, 0.62);
                color: #f4fcff;
            }
            QMenu::item:disabled {
                color: rgba(206, 229, 234, 0.45);
            }
            QMenu::separator {
                height: 1px;
                margin: 4px 8px;
                background: rgba(121, 209, 214, 0.25);
            }
            #sidebar {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #02111c,
                    stop: 1 #041a2b
                );
                border-right: 1px solid rgba(121, 209, 214, 0.2);
            }
            #brandBox, #sessionCard {
                border-radius: 16px;
                border: 1px solid rgba(121, 209, 214, 0.26);
                background: rgba(5, 49, 70, 0.58);
            }
            #sessionCard:hover {
                background: rgba(16, 66, 90, 0.72);
                border-color: rgba(121, 209, 214, 0.45);
            }
            #sessionCard[active="true"] {
                background: rgba(56, 144, 157, 0.32);
                border: 1px solid rgba(130, 222, 226, 0.78);
            }
            #brandBox[active="true"] {
                background: rgba(56, 144, 157, 0.36);
                border: 1px solid rgba(130, 222, 226, 0.82);
            }
            #brandBox:hover {
                background: rgba(20, 74, 98, 0.76);
                border-color: rgba(142, 222, 227, 0.55);
            }
            #brandTitle {
                font-size: 18px;
                font-weight: 700;
                color: #f0fbff;
            }
            #brandSubtitle {
                color: #98c9d0;
                font-size: 12px;
            }
            #brandCompactIcon {
                color: #b9ebef;
                font-size: 20px;
                border-radius: 10px;
                border: 1px solid rgba(121, 209, 214, 0.32);
                background: rgba(8, 56, 79, 0.55);
            }
            #sessionStatus {
                color: #9cd7dc;
                font-size: 12px;
            }
            #sessionUser {
                color: #ffffff;
                font-size: 18px;
                font-weight: 700;
            }
            #sessionProject {
                color: #5ec8c4;
                font-size: 13px;
            }
            #sessionCompactIcon {
                color: #f0fbff;
                font-size: 18px;
                border-radius: 10px;
                border: 1px solid rgba(121, 209, 214, 0.32);
                background: rgba(8, 56, 79, 0.55);
            }
            #footerText {
                color: #88b8bf;
                font-size: 11px;
            }
            QToolButton#themeToggle {
                min-width: 38px;
                max-width: 38px;
                min-height: 30px;
                max-height: 30px;
                font-size: 16px;
                border-radius: 15px;
                padding: 0;
                border: 1px solid rgba(112, 201, 208, 0.36);
                background: rgba(7, 51, 73, 0.68);
                color: #d9e6ef;
            }
            QToolButton#themeToggle:hover {
                background: rgba(17, 72, 96, 0.78);
                border-color: rgba(135, 218, 223, 0.48);
            }
            QToolButton#themeToggle:checked {
                background: rgba(98, 144, 172, 0.35);
                border-color: rgba(151, 188, 210, 0.62);
            }
            QToolButton#sidebarToggle {
                min-width: 27px;
                min-height: 27px;
                max-height: 27px;
                border-radius: 6px;
                border: 1px solid rgba(111, 201, 208, 0.32);
                background: rgba(7, 52, 76, 0.68);
                color: #d8f5f8;
                font-size: 12px;
                padding: 0;
            }
            QToolButton#sidebarToggle:hover {
                background: rgba(22, 92, 118, 0.78);
            }
            QPushButton#navButton {
                text-align: left;
                min-width: 0;
                min-height: 27px;
                padding: 7px 12px;
                border-radius: 9px;
                border: 1px solid rgba(111, 201, 208, 0.18);
                background: rgba(10, 63, 88, 0.62);
                color: #d2edf0;
                font-weight: 700;
                font-size: 13px;
                font-family: "Segoe UI Variable Text", "Segoe UI", "Noto Sans";
            }
            QPushButton#navButton:hover {
                background: rgba(17, 85, 112, 0.82);
                border-color: rgba(126, 214, 220, 0.48);
            }
            QPushButton#navButton:checked {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 rgba(36, 114, 129, 0.52),
                    stop: 1 rgba(24, 86, 108, 0.58)
                );
                border: 1px solid rgba(139, 227, 225, 0.68);
                color: #f3fcff;
            }
            QPushButton#navButton:focus {
                outline: none;
            }
            #sidebar[compact="true"] QPushButton#navButton {
                text-align: center;
                padding: 0;
                font-size: 14px;
                min-width: 40px;
                max-width: 40px;
                min-height: 40px;
                max-height: 40px;
                border-radius: 8px;
            }
            #sidebar[compact="true"] #brandBox,
            #sidebar[compact="true"] #sessionCard {
                border-radius: 12px;
            }
            #contentFrame {
                background: transparent;
            }
            #headerTag {
                font-size: 12px;
                color: #a3d9de;
                font-weight: 700;
                letter-spacing: 2px;
            }
            #headerTitle {
                font-size: 30px;
                color: #f2fcff;
                font-weight: 800;
                padding: 0;
                margin: 0;
            }
            #headerSubtitle {
                color: #8dadb2;
                font-size: 12px;
                padding: 0;
                margin: 0;
            }
            QTabWidget::pane {
                border: 0;
                background: transparent;
            }
            QTabBar::tab {
                width: 0;
                height: 0;
                margin: 0;
                padding: 0;
                border: none;
            }
            QGroupBox {
                margin-top: 8px;
                border-radius: 12px;
                border: 1px solid rgba(113, 214, 219, 0.28);
                background: rgba(5, 42, 62, 0.52);
                padding-top: 8px;
                font-weight: 700;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
                color: #f1fdff;
            }
            #overviewSection, #settingsSection, #statsSection {
                border-radius: 14px;
                border: 1px solid rgba(113, 214, 219, 0.36);
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 rgba(8, 48, 70, 0.74),
                    stop: 1 rgba(5, 34, 52, 0.78)
                );
            }
            #overviewMetaCard, #statsBarsPanel, #statsHistoryPanel, #developerInline, #developerMini {
                border-radius: 10px;
                border: 1px solid rgba(111, 201, 208, 0.36);
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 rgba(9, 55, 80, 0.78),
                    stop: 1 rgba(4, 30, 45, 0.86)
                );
            }
            #developerMini {
                border-radius: 8px;
                background: rgba(2, 28, 43, 0.52);
            }
            #developerMiniText {
                color: #9fd8dc;
                font-size: 10px;
            }
            QToolButton#developerLinkButton {
                min-height: 27px;
                max-height: 27px;
                min-width: 60px;
                max-width: 88px;
                padding: 0 8px;
                border-radius: 6px;
                border: 1px solid rgba(111, 201, 208, 0.42);
                background: rgba(9, 61, 86, 0.55);
                color: #e6f8fb;
                font-size: 11px;
                font-weight: 700;
            }
            QToolButton#developerLinkButton:hover {
                background: rgba(20, 88, 114, 0.76);
            }
            #statsTotalCard {
                border-radius: 10px;
                border: 1px solid rgba(111, 201, 208, 0.36);
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 rgba(12, 64, 91, 0.84),
                    stop: 1 rgba(5, 34, 52, 0.9)
                );
            }
            #statsKpiTitle {
                color: #93c9cf;
                font-size: 10px;
            }
            #statsKpiValue {
                color: #f4fbff;
                font-size: 22px;
                font-weight: 800;
            }
            #statsRunsPanel {
                border: 0;
                background: transparent;
            }
            #statsRunChip {
                border-radius: 8px;
                border: 1px solid rgba(111, 201, 208, 0.32);
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 rgba(9, 55, 80, 0.78),
                    stop: 1 rgba(4, 30, 45, 0.88)
                );
            }
            #statsRunTitle {
                color: #93c9cf;
                font-size: 10px;
            }
            #statsRunValue {
                color: #e7f8fb;
                font-size: 18px;
                font-weight: 800;
            }
            #statsBarLabel {
                color: #dff5f8;
                font-weight: 600;
            }
            #overviewCardTitle {
                color: #9fd8dc;
                font-size: 11px;
            }
            #overviewCardValue, #statsValue {
                color: #f4fbff;
                font-size: 17px;
                font-weight: 800;
            }
            #statsTitle {
                color: #dff5f8;
                font-weight: 700;
            }
            #statsTrend {
                color: #9bd2d8;
                font-family: "Consolas", "Courier New";
            }
            QToolButton#miniResetButton {
                min-width: 27px;
                min-height: 27px;
                max-width: 27px;
                max-height: 27px;
                border-radius: 6px;
                border: 1px solid rgba(111, 201, 208, 0.42);
                background: rgba(7, 52, 76, 0.82);
                color: #d8f5f8;
                font-size: 12px;
                padding: 0;
            }
            QToolButton#miniResetButton:hover {
                background: rgba(22, 92, 118, 0.9);
            }
            QLabel {
                color: #d6edf0;
            }
            QLabel#mutedParenText {
                color: #8fb5ba;
                font-size: 11px;
            }
            QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QTreeWidget, QTableWidget, QListWidget {
                border-radius: 8px;
                border: 1px solid rgba(111, 201, 208, 0.36);
                background: rgba(2, 28, 43, 0.8);
                color: #eaf7f9;
                padding: 3px 8px;
                selection-background-color: rgba(92, 171, 186, 0.65);
            }
            QLineEdit, QComboBox, QSpinBox {
                min-height: 27px;
                max-height: 27px;
            }
            QPushButton, QToolButton {
                min-height: 27px;
                max-height: 27px;
            }
            QTextEdit, QTreeWidget, QTableWidget {
                padding: 8px;
            }
            QTableWidget {
                gridline-color: rgba(96, 179, 192, 0.35);
            }
            QTableWidget::item:selected {
                background: rgba(82, 148, 168, 0.45);
                color: #eaf7f9;
            }
            QListWidget {
                padding: 6px;
            }
            QListWidget::item {
                color: #d8edf1;
                padding: 2px 4px;
            }
            QListWidget::item:selected {
                background: rgba(31, 103, 131, 0.72);
                color: #f2fcff;
            }
            QTextEdit#tsvPreviewLeft {
                border-top-right-radius: 0;
                border-bottom-right-radius: 0;
                border-right: 1px solid rgba(129, 141, 154, 0.45);
            }
            QTextEdit#tsvPreviewRight {
                border-top-left-radius: 0;
                border-bottom-left-radius: 0;
                border-left: 0;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 18px;
                border: none;
                background: transparent;
            }
            QComboBox QAbstractItemView {
                border: 1px solid rgba(111, 201, 208, 0.46);
                background: #08263a;
                color: #e5f4f7;
                selection-background-color: rgba(61, 129, 149, 0.85);
            }
            QPushButton {
                min-width: 0;
                min-height: 27px;
                max-height: 27px;
                border-radius: 6px;
                border: 1px solid rgba(102, 188, 199, 0.45);
                background: rgba(17, 82, 111, 0.74);
                color: #f0fbff;
                padding: 3px 10px;
                font-weight: 600;
                font-size: 13px;
                font-family: "Segoe UI Variable Text", "Segoe UI", "Noto Sans";
            }
            QPushButton#sourceActionButton {
                padding: 2px 6px;
                font-size: 12px;
            }
            QComboBox#sourceFetchModeCombo {
                padding: 2px 8px;
            }
            QPushButton:hover {
                background: rgba(31, 103, 131, 0.92);
            }
            QPushButton:pressed {
                background: rgba(13, 67, 93, 0.96);
            }
            QPushButton:disabled {
                color: rgba(215, 232, 236, 0.45);
                border-color: rgba(92, 146, 154, 0.25);
                background: rgba(20, 51, 67, 0.52);
            }
            QPushButton#debugMiniButton {
                min-height: 27px;
                max-height: 27px;
                min-width: 78px;
                max-width: 78px;
                padding: 0 8px;
                font-size: 12px;
            }
            QPushButton#overviewUpdateButton {
                min-height: 27px;
                max-height: 27px;
                min-width: 132px;
                border-radius: 7px;
                font-size: 13px;
                font-weight: 700;
                padding: 0 10px;
            }
            QToolButton {
                min-height: 27px;
                max-height: 27px;
                border-radius: 6px;
                border: 1px solid rgba(111, 201, 208, 0.38);
                background: rgba(9, 61, 86, 0.65);
                color: #e3f6f9;
                padding: 2px 6px;
                font-size: 13px;
                font-family: "Segoe UI Variable Text", "Segoe UI", "Noto Sans";
            }
            QToolButton#infoButton {
                min-width: 23px;
                max-width: 23px;
                min-height: 23px;
                max-height: 23px;
                padding: 0;
                border-radius: 5px;
                font-size: 11px;
            }
            QToolButton:hover {
                background: rgba(22, 92, 118, 0.85);
            }
            QProgressBar {
                min-height: 27px;
                max-height: 27px;
                border-radius: 7px;
                border: 1px solid rgba(98, 183, 196, 0.3);
                background: rgba(4, 25, 37, 0.9);
                color: #d4e9ec;
                text-align: center;
                padding: 0 6px;
            }
            QProgressBar::chunk {
                border-radius: 6px;
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #2b8996,
                    stop: 1 #5bc8b4
                );
            }
            QProgressBar#statsProgressBar {
                min-height: 22px;
                max-height: 22px;
                border-radius: 6px;
                padding: 0 4px;
            }
            QProgressBar#statsProgressBar::chunk {
                border-radius: 5px;
            }
            QCheckBox {
                spacing: 8px;
                color: #d4e9ec;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border-radius: 4px;
                border: 1px solid rgba(109, 196, 205, 0.48);
                background: rgba(9, 44, 62, 0.9);
            }
            QCheckBox::indicator:checked {
                background: #4da5a2;
                border-color: #95ddd4;
            }
            QSpinBox {
                padding: 0 2px;
            }
            QSpinBox#depthSpin {
                padding: 0 1px;
                min-width: 34px;
                max-width: 34px;
                font-weight: 700;
            }
            QToolButton#depthStepButton {
                min-width: 12px;
                max-width: 12px;
                min-height: 12px;
                max-height: 12px;
                border-radius: 2px;
                padding: 0;
                border: 1px solid rgba(109, 196, 205, 0.46);
                background: rgba(8, 47, 67, 0.95);
                color: #ecfcff;
                font-size: 10px;
                font-weight: 700;
            }
            QToolButton#depthStepButton:hover {
                background: rgba(18, 74, 98, 0.98);
            }
            QSpinBox#depthSpin::up-button,
            QSpinBox#depthSpin::down-button {
                width: 12px;
                border-left: 1px solid rgba(109, 196, 205, 0.42);
                background: rgba(8, 47, 67, 0.95);
                color: #ecfcff;
                font-size: 11px;
            }
            QSpinBox#depthSpin::up-button {
                subcontrol-origin: border;
                subcontrol-position: top right;
            }
            QSpinBox#depthSpin::down-button {
                subcontrol-origin: border;
                subcontrol-position: bottom right;
            }
            QSpinBox#depthSpin::up-button:hover,
            QSpinBox#depthSpin::down-button:hover {
                background: rgba(18, 74, 98, 0.98);
            }
            QHeaderView::section {
                background: rgba(9, 52, 75, 0.92);
                color: #d8eff2;
                border: 1px solid rgba(96, 179, 192, 0.45);
                min-height: 24px;
                padding: 0 6px;
            }
            QTableCornerButton::section {
                background: rgba(9, 52, 75, 0.92);
                border: 1px solid rgba(96, 179, 192, 0.45);
            }
            QTreeWidget::item:selected {
                background: rgba(82, 148, 168, 0.45);
                color: #eaf7f9;
            }
            QTreeWidget::item {
                selection-background-color: rgba(82, 148, 168, 0.45);
            }
            QSplitter#tsvPreviewSplitter::handle:vertical {
                width: 6px;
                margin: 0;
                border: 0;
                background: transparent;
            }
            QSplitter#tsvPreviewSplitter::handle:horizontal {
                height: 6px;
                margin: 2px 8px;
                border-top: 1px solid rgba(134, 208, 214, 0.55);
                border-bottom: 1px solid rgba(88, 163, 177, 0.35);
                background: transparent;
                border-radius: 3px;
            }
            QScrollBar:vertical {
                background: rgba(6, 34, 49, 0.95);
                width: 12px;
                margin: 2px;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical {
                background: rgba(99, 174, 186, 0.65);
                min-height: 18px;
                border-radius: 6px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
            QScrollBar:horizontal {
                background: rgba(6, 34, 49, 0.95);
                height: 12px;
                margin: 2px;
                border-radius: 6px;
            }
            QScrollBar::handle:horizontal {
                background: rgba(99, 174, 186, 0.65);
                min-width: 18px;
                border-radius: 6px;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0;
            }
        """
        arrow_path = resource_path('assets/caret-down-light.svg').replace('\\', '/')
        qss += f"""
            QComboBox::down-arrow {{
                image: url("{arrow_path}");
                width: 10px;
                height: 10px;
            }}
            QComboBox#authLangCombo::drop-down {{
                width: 0px;
                border: none;
                background: transparent;
            }}
            QComboBox#authLangCombo::down-arrow {{
                image: none;
                width: 0px;
                height: 0px;
            }}
            QComboBox#authLangCombo {{
                padding-right: 8px;
            }}
        """
        return qss

    def _midnight_theme_qss(self) -> str:
        """Ночной вариант (midnight dark)."""
        qss = self._dark_theme_qss()
        replacements = {
            '#02111c': '#121820',
            '#041a2b': '#1a222c',
            '#07273a': '#1b2028',
            '#083247': '#232b35',
            '#08263a': '#1b2129',
            '#11495f': '#2a323c',
            '#1d6f76': '#3a454f',
            '#061f31': '#171d25',
            '#051a27': '#11171e',
            '#0a2a40': '#202833',
            '#2b8996': '#6f8598',
            '#5bc8b4': '#9aabb9',
            '#4da5a2': '#7d8f9f',
            '#95ddd4': '#a5b3bf',
            '#5ec8c4': '#a3b3c2',
            '#98c9d0': '#9baab7',
            '#a3d9de': '#a4b0bc',
            '#b6d9dd': '#b0bcc7',
            '#d2edf0': '#d0d9e1',
            '#d6edf0': '#d4dce4',
            '#d4e9ec': '#cdd6de',
            '#d8eff2': '#d2dbe3',
            'rgba(5, 49, 70, 0.58)': 'rgba(36, 44, 54, 0.70)',
            'rgba(10, 63, 88, 0.62)': 'rgba(45, 55, 66, 0.66)',
            'rgba(17, 85, 112, 0.82)': 'rgba(60, 72, 86, 0.88)',
            'rgba(17, 82, 111, 0.74)': 'rgba(56, 67, 79, 0.76)',
            'rgba(31, 103, 131, 0.92)': 'rgba(74, 87, 102, 0.92)',
            'rgba(13, 67, 93, 0.96)': 'rgba(50, 62, 75, 0.96)',
            'rgba(98, 144, 172, 0.35)': 'rgba(120, 132, 146, 0.38)',
            'rgba(151, 188, 210, 0.62)': 'rgba(154, 166, 180, 0.64)',
            'rgba(56, 144, 157, 0.32)': 'rgba(76, 88, 103, 0.34)',
            'rgba(56, 144, 157, 0.36)': 'rgba(80, 92, 107, 0.38)',
            'rgba(42, 126, 141, 0.36)': 'rgba(73, 86, 100, 0.40)',
            'rgba(36, 114, 129, 0.52)': 'rgba(72, 85, 98, 0.54)',
            'rgba(24, 86, 108, 0.58)': 'rgba(60, 74, 88, 0.60)',
            'rgba(20, 51, 67, 0.52)': 'rgba(36, 43, 52, 0.58)',
            'rgba(5, 42, 62, 0.52)': 'rgba(34, 41, 50, 0.62)',
            'rgba(8, 48, 70, 0.74)': 'rgba(47, 56, 66, 0.76)',
            'rgba(5, 34, 52, 0.78)': 'rgba(41, 49, 59, 0.80)',
            'rgba(2, 28, 43, 0.8)': 'rgba(25, 31, 39, 0.84)',
            'rgba(9, 55, 80, 0.78)': 'rgba(50, 59, 70, 0.80)',
            'rgba(4, 30, 45, 0.86)': 'rgba(31, 39, 49, 0.88)',
            'rgba(12, 64, 91, 0.84)': 'rgba(56, 67, 79, 0.86)',
            'rgba(5, 34, 52, 0.9)': 'rgba(39, 48, 58, 0.92)',
            'rgba(4, 30, 45, 0.88)': 'rgba(31, 39, 49, 0.90)',
            'rgba(9, 61, 86, 0.65)': 'rgba(50, 60, 72, 0.72)',
            'rgba(22, 92, 118, 0.85)': 'rgba(68, 80, 94, 0.88)',
            'rgba(7, 51, 73, 0.84)': 'rgba(45, 55, 67, 0.86)',
            'rgba(7, 52, 76, 0.82)': 'rgba(46, 56, 69, 0.84)',
            'rgba(16, 66, 90, 0.72)': 'rgba(57, 68, 80, 0.78)',
            'rgba(8, 56, 79, 0.55)': 'rgba(48, 59, 72, 0.62)',
            'rgba(9, 44, 62, 0.9)': 'rgba(37, 46, 57, 0.92)',
            'rgba(9, 52, 75, 0.92)': 'rgba(47, 57, 69, 0.92)',
            'rgba(6, 34, 49, 0.95)': 'rgba(31, 39, 49, 0.95)',
            'rgba(4, 25, 37, 0.9)': 'rgba(26, 33, 41, 0.92)',
            'rgba(64, 142, 153, 0.34)': 'rgba(92, 108, 126, 0.38)',
            'rgba(64, 142, 153, 0.26)': 'rgba(88, 103, 121, 0.32)',
            'rgba(113, 214, 219, 0.3)': 'rgba(147, 159, 171, 0.30)',
            'rgba(113, 214, 219, 0.28)': 'rgba(142, 154, 166, 0.28)',
            'rgba(121, 209, 214, 0.2)': 'rgba(132, 144, 156, 0.22)',
            'rgba(121, 209, 214, 0.26)': 'rgba(132, 144, 156, 0.28)',
            'rgba(121, 209, 214, 0.32)': 'rgba(132, 144, 156, 0.34)',
            'rgba(121, 209, 214, 0.45)': 'rgba(132, 144, 156, 0.45)',
            'rgba(112, 201, 208, 0.5)': 'rgba(129, 141, 154, 0.50)',
            'rgba(111, 201, 208, 0.42)': 'rgba(129, 141, 154, 0.44)',
            'rgba(111, 201, 208, 0.38)': 'rgba(129, 141, 154, 0.40)',
            'rgba(111, 201, 208, 0.36)': 'rgba(129, 141, 154, 0.38)',
            'rgba(111, 201, 208, 0.46)': 'rgba(129, 141, 154, 0.48)',
            'rgba(111, 201, 208, 0.32)': 'rgba(128, 140, 153, 0.34)',
            'rgba(102, 188, 199, 0.45)': 'rgba(124, 136, 149, 0.48)',
            'rgba(98, 183, 196, 0.3)': 'rgba(122, 134, 146, 0.34)',
            'rgba(109, 196, 205, 0.48)': 'rgba(126, 138, 150, 0.50)',
            'rgba(109, 196, 205, 0.46)': 'rgba(126, 138, 150, 0.48)',
            'rgba(112, 201, 208, 0.36)': 'rgba(130, 142, 154, 0.38)',
            'rgba(7, 51, 73, 0.68)': 'rgba(44, 54, 65, 0.72)',
            'rgba(17, 72, 96, 0.78)': 'rgba(61, 73, 86, 0.82)',
            'rgba(135, 218, 223, 0.48)': 'rgba(151, 163, 176, 0.52)',
            'rgba(7, 52, 76, 0.68)': 'rgba(45, 55, 67, 0.72)',
            'rgba(22, 92, 118, 0.78)': 'rgba(66, 78, 92, 0.82)',
            'rgba(92, 171, 186, 0.65)': 'rgba(118, 130, 142, 0.66)',
            'rgba(61, 129, 149, 0.85)': 'rgba(94, 106, 120, 0.86)',
            'rgba(82, 148, 168, 0.45)': 'rgba(108, 120, 133, 0.48)',
            'rgba(130, 222, 226, 0.78)': 'rgba(161, 173, 186, 0.76)',
            'rgba(130, 222, 226, 0.82)': 'rgba(161, 173, 186, 0.80)',
            'rgba(139, 227, 225, 0.82)': 'rgba(162, 174, 187, 0.80)',
            'rgba(139, 227, 225, 0.68)': 'rgba(160, 172, 185, 0.68)',
            'rgba(142, 222, 227, 0.55)': 'rgba(163, 175, 188, 0.56)',
            'rgba(134, 208, 214, 0.65)': 'rgba(140, 152, 164, 0.65)',
            'rgba(134, 208, 214, 0.55)': 'rgba(140, 152, 164, 0.55)',
            'rgba(88, 163, 177, 0.35)': 'rgba(112, 124, 137, 0.35)',
            'rgba(99, 174, 186, 0.65)': 'rgba(120, 132, 145, 0.66)',
            'rgba(8, 47, 67, 0.95)': 'rgba(48, 57, 69, 0.95)',
            'rgba(18, 74, 98, 0.98)': 'rgba(64, 74, 87, 0.98)',
            '#ffdb76': '#c7d2dd',
            'rgba(255, 214, 121, 0.25)': 'rgba(184, 198, 212, 0.28)',
            'rgba(255, 214, 121, 0.78)': 'rgba(188, 202, 216, 0.72)',
        }
        for old, new in replacements.items():
            qss = qss.replace(old, new)
        # Дополнительные жёсткие переопределения для графитовой темы,
        # чтобы обзорные карточки не уходили в зеленоватый оттенок.
        qss += """
            #windowRoot {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 1,
                    stop: 0 #121820,
                    stop: 0.58 #1a212b,
                    stop: 1 #202833
                );
            }
            #sidebar {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #0f141b,
                    stop: 1 #161d26
                );
            }
            #overviewSection, #settingsSection, #statsSection {
                border: 1px solid rgba(139, 151, 163, 0.36);
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 rgba(40, 48, 58, 0.88),
                    stop: 1 rgba(30, 37, 46, 0.92)
                );
            }
            #overviewMetaCard, #statsBarsPanel, #statsHistoryPanel, #developerMini, #statsTotalCard, #statsRunChip {
                border: 1px solid rgba(131, 143, 156, 0.34);
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 rgba(34, 42, 53, 0.92),
                    stop: 1 rgba(24, 30, 38, 0.96)
                );
            }
            QPushButton#navButton:checked {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 rgba(88, 106, 125, 0.55),
                    stop: 1 rgba(70, 86, 102, 0.58)
                );
                border: 1px solid rgba(159, 172, 185, 0.70);
                color: #f2f6fb;
            }
            QToolButton#themeToggle {
                border: 1px solid rgba(129, 141, 154, 0.40);
                background: rgba(45, 55, 67, 0.82);
                color: #dbe4ee;
            }
            QToolButton#themeToggle:hover {
                background: rgba(61, 73, 86, 0.88);
                border-color: rgba(151, 163, 176, 0.56);
            }
            QToolButton#themeToggle:checked {
                background: rgba(120, 132, 146, 0.42);
                border-color: rgba(154, 166, 180, 0.66);
            }
            QToolButton#sidebarToggle {
                border: 1px solid rgba(128, 140, 153, 0.34);
                background: rgba(45, 55, 67, 0.82);
                color: #dbe4ee;
            }
            QToolButton#sidebarToggle:hover {
                background: rgba(66, 78, 92, 0.88);
            }
            QToolButton#developerLinkButton {
                border: 1px solid rgba(131, 143, 156, 0.44);
                background: rgba(45, 54, 66, 0.92);
                color: #edf3fa;
            }
            QToolButton#developerLinkButton:hover {
                background: rgba(62, 72, 84, 0.96);
            }
            QPushButton#overviewUpdateButton {
                border: 1px solid rgba(131, 143, 156, 0.46);
                background: rgba(45, 54, 66, 0.92);
                color: #edf3fa;
            }
            QPushButton#overviewUpdateButton:hover {
                background: rgba(62, 72, 84, 0.96);
            }
            QToolButton#miniResetButton {
                border: 1px solid rgba(131, 143, 156, 0.46);
                background: rgba(45, 54, 66, 0.92);
                color: #edf3fa;
            }
            QToolButton#miniResetButton:hover {
                background: rgba(62, 72, 84, 0.96);
            }
            QLabel#mutedParenText {
                color: #9faab7;
            }
            QSpinBox#depthSpin::up-button,
            QSpinBox#depthSpin::down-button {
                border-left: 1px solid rgba(131, 143, 156, 0.5);
                background: rgba(48, 57, 69, 0.95);
                color: #edf2f8;
            }
            QSpinBox#depthSpin::up-button:hover,
            QSpinBox#depthSpin::down-button:hover {
                background: rgba(64, 74, 87, 0.98);
            }
            QToolButton#depthStepButton {
                border: 1px solid rgba(131, 143, 156, 0.52);
                background: rgba(48, 57, 69, 0.95);
                color: #edf2f8;
            }
            QToolButton#depthStepButton:hover {
                background: rgba(64, 74, 87, 0.98);
            }
            QHeaderView::section {
                background: rgba(47, 57, 69, 0.92);
                border: 1px solid rgba(131, 143, 156, 0.38);
                color: #e8eef5;
            }
        """
        return qss

    def _light_theme_qss(self) -> str:
        qss = """
            QMainWindow, QWidget, QDialog, QMessageBox {
                color: #22313d;
                font-family: "Segoe UI";
                font-size: 9pt;
            }
            #windowRoot {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 1,
                    stop: 0 #edf7fb,
                    stop: 0.6 #f5fbff,
                    stop: 1 #ffffff
                );
            }
            QDialog, QMessageBox, QMenu {
                background: #ffffff;
                border: 1px solid #cbdce7;
            }
            QMenu {
                padding: 4px 2px;
            }
            QMenu::item {
                padding: 5px 12px;
                margin: 1px 2px;
                border-radius: 4px;
                background: transparent;
                color: #22313d;
            }
            QMenu::item:selected {
                background: #dcefff;
                color: #153a55;
            }
            QMenu::item:disabled {
                color: #8da3b5;
            }
            QMenu::separator {
                height: 1px;
                margin: 4px 8px;
                background: #d6e4ef;
            }
            #sidebar {
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #f3f8fd,
                    stop: 1 #eaf2f8
                );
                border-right: 1px solid #d3e2ed;
            }
            #brandBox, #sessionCard {
                border-radius: 16px;
                border: 1px solid #c6d9e8;
                background: rgba(255, 255, 255, 0.96);
            }
            #sessionCard:hover {
                border-color: #8caecc;
                background: #f7fbff;
            }
            #sessionCard[active="true"] {
                background: #e8f3ff;
                border: 1px solid #9fbfe0;
            }
            #brandBox[active="true"] {
                background: #e8f3ff;
                border: 1px solid #9fbfe0;
            }
            #brandBox:hover {
                background: #f2f8ff;
                border-color: #8caecc;
            }
            #brandTitle {
                font-size: 18px;
                font-weight: 700;
                color: #224057;
            }
            #brandSubtitle {
                color: #6f8ca2;
                font-size: 12px;
            }
            #brandCompactIcon {
                color: #46718e;
                font-size: 20px;
                border-radius: 10px;
                border: 1px solid #bed2e2;
                background: #f7fbff;
            }
            #sessionStatus {
                color: #5b7c95;
                font-size: 12px;
            }
            #sessionUser {
                color: #1f3d52;
                font-size: 18px;
                font-weight: 700;
            }
            #sessionProject {
                color: #2f6c8f;
                font-size: 13px;
            }
            #sessionCompactIcon {
                color: #2d5875;
                font-size: 18px;
                border-radius: 10px;
                border: 1px solid #bed2e2;
                background: #f7fbff;
            }
            #footerText {
                color: #6f8ca2;
                font-size: 11px;
            }
            QToolButton#themeToggle {
                min-width: 38px;
                max-width: 38px;
                min-height: 30px;
                max-height: 30px;
                font-size: 16px;
                border-radius: 15px;
                padding: 0;
                border: 1px solid #bdd3e4;
                background: #f7fbff;
                color: #6f8ca2;
            }
            QToolButton#themeToggle:hover {
                background: #eef6fd;
                border-color: #a9c6dd;
            }
            QToolButton#themeToggle:checked {
                background: #e6f1fb;
                border-color: #9cbddb;
            }
            QToolButton#sidebarToggle {
                min-width: 27px;
                min-height: 27px;
                max-height: 27px;
                border-radius: 6px;
                border: 1px solid #bfd3e3;
                background: #f8fbfe;
                color: #2f5670;
                font-size: 12px;
                padding: 0;
            }
            QToolButton#sidebarToggle:hover {
                background: #edf5fc;
            }
            QPushButton#navButton {
                text-align: left;
                min-width: 0;
                min-height: 27px;
                padding: 7px 12px;
                border-radius: 9px;
                border: 1px solid #c6d9e8;
                background: #ffffff;
                color: #2a4a61;
                font-weight: 700;
                font-size: 13px;
                font-family: "Segoe UI Variable Text", "Segoe UI", "Noto Sans";
            }
            QPushButton#navButton:hover {
                background: #f2f8ff;
            }
            QPushButton#navButton:checked {
                background: #e8f3ff;
                border: 1px solid #9fbfe0;
                color: #1f4e74;
            }
            #sidebar[compact="true"] QPushButton#navButton {
                text-align: center;
                padding: 0;
                font-size: 14px;
                min-width: 40px;
                max-width: 40px;
                min-height: 40px;
                max-height: 40px;
                border-radius: 8px;
            }
            #sidebar[compact="true"] #brandBox,
            #sidebar[compact="true"] #sessionCard {
                border-radius: 12px;
            }
            #contentFrame {
                background: transparent;
            }
            #headerTag {
                font-size: 12px;
                color: #5a7f9d;
                font-weight: 700;
                letter-spacing: 2px;
            }
            #headerTitle {
                font-size: 30px;
                color: #21445f;
                font-weight: 800;
                padding: 0;
                margin: 0;
            }
            #headerSubtitle {
                color: #7a92a2;
                font-size: 12px;
                padding: 0;
                margin: 0;
            }
            QTabWidget::pane {
                border: 0;
                background: transparent;
            }
            QTabBar::tab {
                width: 0;
                height: 0;
                margin: 0;
                padding: 0;
                border: none;
            }
            QGroupBox {
                margin-top: 8px;
                border-radius: 12px;
                border: 1px solid #c5d8e7;
                background: rgba(255, 255, 255, 0.88);
                padding-top: 8px;
                font-weight: 700;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
                color: #2d4c63;
            }
            #overviewSection, #settingsSection, #statsSection {
                border-radius: 14px;
                border: 1px solid #c5d8e7;
                background: rgba(255, 255, 255, 0.90);
            }
            #overviewMetaCard, #statsBarsPanel, #statsHistoryPanel, #developerInline, #developerMini {
                border-radius: 10px;
                border: 1px solid #b9cfdf;
                background: #ffffff;
            }
            #developerMini {
                border-radius: 8px;
                background: rgba(255, 255, 255, 0.86);
            }
            #developerMiniText {
                color: #5e7d94;
                font-size: 10px;
            }
            QToolButton#developerLinkButton {
                min-height: 27px;
                max-height: 27px;
                min-width: 60px;
                max-width: 88px;
                padding: 0 8px;
                border-radius: 6px;
                border: 1px solid #b9cfdf;
                background: #ffffff;
                color: #2f5670;
                font-size: 11px;
                font-weight: 700;
            }
            QToolButton#developerLinkButton:hover {
                background: #f1f8ff;
            }
            #statsTotalCard {
                border-radius: 10px;
                border: 1px solid #bed2e2;
                background: #f7fbff;
            }
            #statsKpiTitle {
                color: #5e7d94;
                font-size: 10px;
            }
            #statsKpiValue {
                color: #244661;
                font-size: 22px;
                font-weight: 800;
            }
            #statsRunsPanel {
                border: 0;
                background: transparent;
            }
            #statsRunChip {
                border-radius: 8px;
                border: 1px solid #bed2e2;
                background: #f7fbff;
            }
            #statsRunTitle {
                color: #5e7d94;
                font-size: 10px;
            }
            #statsRunValue {
                color: #244661;
                font-size: 18px;
                font-weight: 800;
            }
            #statsBarLabel {
                color: #2f5670;
                font-weight: 600;
            }
            #overviewCardTitle {
                color: #5e7d94;
                font-size: 11px;
            }
            #overviewCardValue, #statsValue {
                color: #244661;
                font-size: 17px;
                font-weight: 800;
            }
            #statsTitle {
                color: #2f5670;
                font-weight: 700;
            }
            #statsTrend {
                color: #5d7e95;
                font-family: "Consolas", "Courier New";
            }
            QToolButton#miniResetButton {
                min-width: 27px;
                min-height: 27px;
                max-width: 27px;
                max-height: 27px;
                border-radius: 6px;
                border: 1px solid #b9cfdf;
                background: #ffffff;
                color: #2f5670;
                font-size: 12px;
                padding: 0;
            }
            QToolButton#miniResetButton:hover {
                background: #f1f8ff;
            }
            QLabel {
                color: #2a485d;
            }
            QLabel#mutedParenText {
                color: #6f8798;
                font-size: 11px;
            }
            QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QTreeWidget, QTableWidget, QListWidget {
                border-radius: 8px;
                border: 1px solid #bed2e2;
                background: #ffffff;
                color: #22313d;
                padding: 3px 8px;
                selection-background-color: #b7d8f3;
            }
            QLineEdit, QComboBox, QSpinBox {
                min-height: 27px;
                max-height: 27px;
            }
            QPushButton, QToolButton {
                min-height: 27px;
                max-height: 27px;
            }
            QTextEdit, QTreeWidget, QTableWidget {
                padding: 8px;
            }
            QTableWidget {
                gridline-color: #c4d8e8;
            }
            QTableWidget::item:selected {
                background: #d7eaf9;
                color: #22313d;
            }
            QListWidget {
                padding: 6px;
            }
            QListWidget::item {
                color: #2a485d;
                padding: 2px 4px;
            }
            QListWidget::item:selected {
                background: #d7eaf9;
                color: #22313d;
            }
            QTextEdit#tsvPreviewLeft {
                border-top-right-radius: 0;
                border-bottom-right-radius: 0;
                border-right: 1px solid #b8cede;
            }
            QTextEdit#tsvPreviewRight {
                border-top-left-radius: 0;
                border-bottom-left-radius: 0;
                border-left: 0;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 18px;
                border: none;
                background: transparent;
            }
            QComboBox QAbstractItemView {
                border: 1px solid #bed2e2;
                background: #ffffff;
                color: #22313d;
                selection-background-color: #d7eaf9;
            }
            QPushButton {
                min-width: 0;
                min-height: 27px;
                max-height: 27px;
                border-radius: 6px;
                border: 1px solid #9fbdd6;
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 0, y2: 1,
                    stop: 0 #f6fbff,
                    stop: 1 #e7f2fb
                );
                color: #1f4e74;
                padding: 3px 10px;
                font-weight: 600;
                font-size: 13px;
                font-family: "Segoe UI Variable Text", "Segoe UI", "Noto Sans";
            }
            QPushButton#sourceActionButton {
                padding: 2px 6px;
                font-size: 12px;
            }
            QComboBox#sourceFetchModeCombo {
                padding: 2px 8px;
            }
            QPushButton:hover {
                background: #dfedf9;
            }
            QPushButton:pressed {
                background: #d3e5f5;
            }
            QPushButton:disabled {
                color: #90a7b8;
                border-color: #d3e0ea;
                background: #f6f9fc;
            }
            QPushButton#debugMiniButton {
                min-height: 27px;
                max-height: 27px;
                min-width: 78px;
                max-width: 78px;
                padding: 0 8px;
                font-size: 12px;
            }
            QPushButton#overviewUpdateButton {
                min-height: 27px;
                max-height: 27px;
                min-width: 132px;
                border-radius: 7px;
                font-size: 13px;
                font-weight: 700;
                padding: 0 10px;
            }
            QToolButton {
                min-height: 27px;
                max-height: 27px;
                border-radius: 6px;
                border: 1px solid #b9cfdf;
                background: #ffffff;
                color: #2f5670;
                padding: 2px 6px;
                font-size: 13px;
                font-family: "Segoe UI Variable Text", "Segoe UI", "Noto Sans";
            }
            QToolButton#infoButton {
                min-width: 23px;
                max-width: 23px;
                min-height: 23px;
                max-height: 23px;
                padding: 0;
                border-radius: 5px;
                font-size: 11px;
            }
            QToolButton:hover {
                background: #f1f8ff;
            }
            QProgressBar {
                min-height: 27px;
                max-height: 27px;
                border-radius: 7px;
                border: 1px solid #c1d5e5;
                background: #edf4fa;
                color: #37566f;
                text-align: center;
                padding: 0 6px;
            }
            QProgressBar::chunk {
                border-radius: 6px;
                background: qlineargradient(
                    x1: 0, y1: 0, x2: 1, y2: 0,
                    stop: 0 #4b8fc3,
                    stop: 1 #7eb5df
                );
            }
            QProgressBar#statsProgressBar {
                min-height: 22px;
                max-height: 22px;
                border-radius: 6px;
                padding: 0 4px;
            }
            QProgressBar#statsProgressBar::chunk {
                border-radius: 5px;
            }
            QCheckBox {
                spacing: 8px;
                color: #37566f;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border-radius: 4px;
                border: 1px solid #a9bfd2;
                background: #ffffff;
            }
            QCheckBox::indicator:checked {
                background: #6ea5d4;
                border-color: #4f86b6;
            }
            QSpinBox {
                padding: 0 2px;
            }
            QSpinBox#depthSpin {
                padding: 0 1px;
                min-width: 34px;
                max-width: 34px;
                font-weight: 700;
            }
            QToolButton#depthStepButton {
                min-width: 12px;
                max-width: 12px;
                min-height: 12px;
                max-height: 12px;
                border-radius: 2px;
                padding: 0;
                border: 1px solid #9eb8cc;
                background: #f2f7fc;
                color: #34536b;
                font-size: 10px;
                font-weight: 700;
            }
            QToolButton#depthStepButton:hover {
                background: #e3eef8;
            }
            QSpinBox#depthSpin::up-button,
            QSpinBox#depthSpin::down-button {
                width: 12px;
                border-left: 1px solid #9eb8cc;
                background: #f2f7fc;
                color: #34536b;
                font-size: 11px;
            }
            QSpinBox#depthSpin::up-button {
                subcontrol-origin: border;
                subcontrol-position: top right;
            }
            QSpinBox#depthSpin::down-button {
                subcontrol-origin: border;
                subcontrol-position: bottom right;
            }
            QSpinBox#depthSpin::up-button:hover,
            QSpinBox#depthSpin::down-button:hover {
                background: #e3eef8;
            }
            QHeaderView::section {
                background: #e8f1f9;
                color: #2b4d65;
                border: 1px solid #b8cede;
                min-height: 24px;
                padding: 0 6px;
            }
            QTableCornerButton::section {
                background: #e8f1f9;
                border: 1px solid #b8cede;
            }
            QTreeWidget::item:selected {
                background: #d7eaf9;
                color: #22313d;
            }
            QTreeWidget::item {
                selection-background-color: #d7eaf9;
            }
            QSplitter#tsvPreviewSplitter::handle:vertical {
                width: 6px;
                margin: 0;
                border: 0;
                background: transparent;
            }
            QSplitter#tsvPreviewSplitter::handle:horizontal {
                height: 6px;
                margin: 2px 8px;
                border-top: 1px solid #a8bfd2;
                border-bottom: 1px solid #c4d7e5;
                background: transparent;
                border-radius: 3px;
            }
            QScrollBar:vertical {
                background: #eef5fb;
                width: 12px;
                margin: 2px;
                border-radius: 6px;
            }
            QScrollBar::handle:vertical {
                background: #a9c4da;
                min-height: 18px;
                border-radius: 6px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
            QScrollBar:horizontal {
                background: #eef5fb;
                height: 12px;
                margin: 2px;
                border-radius: 6px;
            }
            QScrollBar::handle:horizontal {
                background: #a9c4da;
                min-width: 18px;
                border-radius: 6px;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0;
            }
        """
        arrow_path = resource_path('assets/caret-down-dark.svg').replace('\\', '/')
        qss += f"""
            QComboBox::down-arrow {{
                image: url("{arrow_path}");
                width: 10px;
                height: 10px;
            }}
            QComboBox#authLangCombo::drop-down {{
                width: 0px;
                border: none;
                background: transparent;
            }}
            QComboBox#authLangCombo::down-arrow {{
                image: none;
                width: 0px;
                height: 0px;
            }}
            QComboBox#authLangCombo {{
                padding-right: 8px;
            }}
        """
        return qss

    def _init_startup_placeholder(self):
        """Лёгкая оболочка окна, чтобы GUI показался до тяжёлых импортов."""
        holder = QWidget(self)
        layout = QVBoxLayout(holder)
        layout.addStretch(1)
        label = QLabel(self._t('ui.main.startup_placeholder'), holder)
        try:
            label.setAlignment(Qt.AlignCenter)
            label.setWordWrap(True)
        except Exception:
            pass
        layout.addWidget(label)
        layout.addStretch(1)
        self._startup_placeholder = holder
        self._startup_label = label
        try:
            self.tabs.addTab(holder, self._t('ui.main.startup_tab'))
            self.tabs.setTabEnabled(0, False)
        except Exception:
            pass

    def set_startup_status(self, text: str):
        """Обновляет статус стартовой инициализации."""
        try:
            if self._startup_label is not None:
                self._startup_label.setText(text or '')
                self._startup_label.repaint()
            self.page_subtitle.setText(text or '')
            self.page_subtitle.repaint()
        except Exception:
            pass

    def complete_startup(self):
        """Ленивая инициализация core-компонентов и вкладок после первого показа окна."""
        if self._startup_complete:
            return
        self.set_startup_status(self._t('ui.main.init_modules'))
        self.init_core_components()
        self.set_startup_status(self._t('ui.creating_tabs'))
        self.init_tabs()
        self._set_embedded_prefix_controls_visible(False)
        try:
            self._rules_file_path = os.path.join(
                self.config_manager._dist_configs_dir(), 'template_rules.json')
        except Exception:
            self._rules_file_path = None
        try:
            self.tabs.clear()
        except Exception:
            pass
        self.tabs.addTab(self.auth_tab, self._t('ui.authentication'))
        self.tabs.addTab(self.parse_tab, self._t('ui.read'))
        self.tabs.addTab(self.replace_tab, self._t('ui.replace'))
        self.tabs.addTab(self.create_tab, self._t('ui.create'))
        self.tabs.addTab(self.rename_tab, self._t('ui.rename'))
        self.tabs.addTab(self.redundant_categories_tab, self._t('ui.main.tab.cleanup.title'))
        self.tabs.addTab(self.category_content_sync_tab, self._t('ui.category_sync_label'))
        self.tabs.addTab(self.overview_tab, self._t('ui.overview'))
        try:
            self.tabs.setCurrentIndex(0)
        except Exception:
            pass
        self._rebuild_sidebar_nav()
        self._update_header_for_tab(0)
        self._sync_sidebar_tab_state(0)
        self._refresh_session_card()
        try:
            self.overview_tab.set_ui_language(self._ui_lang)
            self.overview_tab.update_stats(self._operation_stats, self._operation_history)
            self.overview_tab.update_session(self.current_user, self.current_family, self.current_lang)
        except Exception:
            pass
        self._apply_widget_text_translation()
        self._install_localized_context_menus()
        self._normalize_line_edit_alignment()
        QTimer.singleShot(0, self._ensure_button_widths)
        QTimer.singleShot(0, self._normalize_depth_spins)
        self._startup_complete = True

    def init_core_components(self):
        """Инициализация всех core компонентов"""
        from ..core.api_client import WikimediaAPIClient
        from ..core.namespace_manager import NamespaceManager
        from ..core.pywikibot_config import PywikibotConfigManager
        from ..core.template_manager import TemplateManager
        # Создание API клиента
        self.api_client = WikimediaAPIClient()

        # Создание менеджера пространств имен
        self.namespace_manager = NamespaceManager(self.api_client)

        # Создание менеджера конфигурации Pywikibot
        self.config_manager = PywikibotConfigManager()

        # Создание менеджера шаблонов
        self.template_manager = TemplateManager()

    def init_tabs(self):
        """Инициализация всех вкладок"""
        from .tabs.auth_tab import AuthTab
        from .tabs.parse_tab import ParseTab
        from .tabs.replace_tab import ReplaceTab
        from .tabs.create_tab import CreateTab
        from .tabs.rename_tab import RenameTab
        from .tabs.redundant_categories_tab import RedundantCategoriesTab
        from .tabs.category_content_sync_tab import CategoryContentSyncTab
        from .tabs.overview_tab import OverviewTab
        # Создание вкладок с передачей core компонентов
        self.auth_tab = AuthTab(self)
        self.parse_tab = ParseTab(self)
        self.replace_tab = ReplaceTab(self)
        self.create_tab = CreateTab(self)
        self.rename_tab = RenameTab(self)
        self.redundant_categories_tab = RedundantCategoriesTab(self)
        self.category_content_sync_tab = CategoryContentSyncTab(self)
        self.overview_tab = OverviewTab(self)

        # Передача core компонентов во вкладки
        for tab in [
            self.auth_tab,
            self.parse_tab,
            self.replace_tab,
            self.create_tab,
            self.rename_tab,
            self.redundant_categories_tab,
            self.category_content_sync_tab,
            self.overview_tab,
        ]:
            if hasattr(tab, 'set_core_components'):
                tab.set_core_components(
                    api_client=self.api_client,
                    namespace_manager=self.namespace_manager,
                    config_manager=self.config_manager,
                    template_manager=self.template_manager
                )


        # Подключение сигналов для передачи данных между вкладками
        self.auth_tab.login_success.connect(self._on_login_success)
        self.auth_tab.logout_success.connect(self._on_logout_success)
        self.auth_tab.lang_changed.connect(self._on_lang_change)

        # Обновление семейства проекта по выбору в AuthTab (без тяжёлых операций)
        if hasattr(self.auth_tab, 'family_combo') and self.auth_tab.family_combo:
            try:
                self.auth_tab.family_combo.currentTextChanged.connect(
                    self.update_family)
            except Exception:
                pass

        # Обновление только при потере фокуса (если lineEdit доступен)
        if (hasattr(self.auth_tab, 'family_combo') and
            self.auth_tab.family_combo and
            hasattr(self.auth_tab.family_combo, 'lineEdit') and
                self.auth_tab.family_combo.lineEdit()):
            self.auth_tab.family_combo.lineEdit().editingFinished.connect(
                lambda: self.update_family(
                    self.auth_tab.family_combo.currentText())
            )

        # Загрузка сохраненных учетных данных
        try:
            self.auth_tab.load_creds()
        except Exception:
            pass

        # Связать выпадающие списки namespace между вкладками
        QTimer.singleShot(150, self._link_ns_combos)

        # Обновлять namespace при первом/каждом открытии содержательных вкладок
        try:
            self.tabs.currentChanged.connect(self._on_tab_changed)
        except Exception:
            pass

    def _set_embedded_prefix_controls_visible(self, visible: bool):
        """Показывает/скрывает локальные строки префиксов во вкладках."""
        state = bool(visible)
        for tab in (
            self.parse_tab,
            self.replace_tab,
            self.create_tab,
            self.rename_tab,
            self.redundant_categories_tab,
            self.category_content_sync_tab,
        ):
            try:
                handler = getattr(tab, 'set_prefix_controls_visible', None)
                if callable(handler):
                    handler(state)
            except Exception:
                pass

    def _on_login_success(self, username: str, password: str, lang: str, family: str):
        """Обработка успешной авторизации"""
        self.current_user = username
        self.current_password = password
        self.current_lang = lang
        self.current_family = family
        self._refresh_session_card()

        # Передача данных авторизации во все вкладки
        for tab in self.content_tabs():
            if hasattr(tab, 'set_auth_data'):
                tab.set_auth_data(username, lang, family)

        # При авторизации обновляем namespace только если нет кэша для текущей пары
        try:
            cache_path = self.namespace_manager._ns_cache_file(
                family or 'wikipedia', lang or 'ru')
            if not os.path.isfile(cache_path):
                debug(
                    self._fmt('log.main.auth_cache_missing', family=family, lang=lang))
                QTimer.singleShot(0, lambda: self.update_namespace_combos(
                    family, lang, force=True))
            else:
                debug(self._t('log.main.auth_cache_hit'))
        except Exception:
            pass

    def _on_logout_success(self):
        """Обработка выхода из системы"""
        self.current_user = None
        self.current_password = None
        self.current_lang = None
        self.current_family = None
        self._refresh_session_card()

        # Очистка данных авторизации во всех вкладках
        for tab in self.content_tabs():
            if hasattr(tab, 'clear_auth_data'):
                tab.clear_auth_data()

        # Снять зелёную подсветку логина/пароля на вкладке авторизации
        try:
            if hasattr(self.auth_tab, 'clear_auth_highlight'):
                self.auth_tab.clear_auth_highlight()
        except Exception:
            pass

    def _on_lang_change(self, new_lang: str):
        """Обработка изменения языка"""
        self.current_lang = new_lang
        self._refresh_session_card()

        # Обновление summary полей в зависимости от языка
        edits = []

        # Собираем все summary поля из вкладок
        if hasattr(self.replace_tab, 'summary_edit'):
            edits.append((self.replace_tab.summary_edit, default_summary))
        if hasattr(self.create_tab, 'summary_edit_create'):
            edits.append(
                (self.create_tab.summary_edit_create, default_create_summary))

        # Обновляем текст в полях summary
        for widget, func in edits:
            if widget is None:
                continue
            cur = widget.text().strip()
            if cur == '' or cur == func(self.prev_lang):
                widget.setText(func(new_lang))

        self.prev_lang = new_lang

        # Обновление namespace комбобоксов во всех вкладках
        family = getattr(self.auth_tab, 'family_combo', None)
        if family:
            family_text = family.currentText() or 'wikipedia'
            self.current_family = family_text
            debug(
                self._fmt('log.main.skip_ns_on_lang_change', lang=new_lang))

        # Уведомление вкладок об изменении языка
        for tab in self.content_tabs():
            if hasattr(tab, 'update_language'):
                tab.update_language(new_lang)

        # После обновления — вновь связать комбобоксы
        self._link_ns_combos()

    def _update_all_namespaces(self, lang: str, family: str):
        """Обновление namespace комбобоксов во всех вкладках"""
        # Обновление namespace комбобоксов для всех вкладок
        try:
            combos = self._gather_ns_combos()
            for combo in combos:
                try:
                    self.namespace_manager.populate_ns_combo(
                        combo, family, lang)
                    self.namespace_manager._adjust_combo_popup_width(combo)
                except Exception:
                    pass
        except Exception as e:
            debug(self._fmt('log.main.ns_update_error', error=e))

    def update_namespace_combos(self, family: str, lang: str, force: bool = False):
        """Публичный метод для обновления namespace комбобоксов.

        Args:
            family: семейство проектов
            lang: язык проекта
            force: зарезервировано для принудительного обновления (для совместимости)
        """
        debug(self._fmt('log.main.ns_update_called', family=family, lang=lang, force=force))
        try:
            combos = self._gather_ns_combos()
            try:
                if not force:
                    cached = self.namespace_manager._get_cached_ns_info(family, lang)
                    if cached:
                        debug(
                            self._fmt(
                                'log.main.ns_from_cache',
                                family=family,
                                lang=lang,
                                cached=len(cached),
                                combos=len(combos),
                            )
                        )
            except Exception:
                pass
            for combo in combos:
                try:
                    # Если force=True — разрешаем сетевую загрузку и обновление кэша
                    self.namespace_manager.populate_ns_combo(
                        combo, family, lang, force_load=force)
                    self.namespace_manager._adjust_combo_popup_width(combo)
                except Exception:
                    pass
            debug(self._t('log.main.ns_update_done'))
            # После заполнения — связать комбобоксы
            self._link_ns_combos()
        except Exception as e:
            debug(self._fmt('log.main.ns_update_failed', error=e))
            import traceback
            debug(f'Traceback: {traceback.format_exc()}')

    def _on_tab_changed(self, index: int):
        """Обработка переключения вкладок: при открытии содержательных вкладок обновляем NS.

        Выполняем сетевую загрузку только если отсутствует кэш для текущего языка/проекта.
        """
        self._sync_sidebar_tab_state(index)
        self._update_header_for_tab(index)
        try:
            # Вкладки: 0=Авторизация, 1=Чтение, 2=Перезапись, 3=Создание,
            # 4=Переименование, 5=Удаление избыточных категорий, 6=Синхронизация категорий
            if index in (1, 2, 3, 4, 5, 6):
                fam = self.current_family or 'wikipedia'
                lang = self.current_lang or 'ru'
                force_needed = False
                try:
                    cache_path = self.namespace_manager._ns_cache_file(
                        fam, lang)
                    force_needed = not os.path.isfile(cache_path)
                except Exception:
                    force_needed = False
                debug(
                    self._fmt('log.main.tab_ns_update', index=index, force=force_needed))
                self.update_namespace_combos(fam, lang, force=force_needed)
        except Exception as e:
            try:
                debug(self._fmt('log.main.tab_change_error', error=e))
            except Exception:
                pass

    def force_update_namespace_combos(self, family: str, lang: str):
        """Асинхронная загрузка NS в кэш и обновление комбобоксов после завершения."""
        try:
            # Не запускаем второй поток, если первый ещё работает
            running = getattr(self, '_ns_thread', None)
            if running is not None and hasattr(running, 'isRunning') and running.isRunning():
                return
        except Exception:
            pass

        t = NSLoadThread(self.namespace_manager, family, lang)
        self._ns_thread = t

        def _on_done(ok: bool):
            try:
                self.update_namespace_combos(family, lang, force=False)
            except Exception:
                pass
            try:
                self.ns_update_finished.emit(family, lang, ok)
            except Exception:
                pass
            try:
                self._ns_thread = None
            except Exception:
                pass

        try:
            t.finished_ok.connect(_on_done)
            t.start()
        except Exception:
            # Фолбэк: синхронно (нежелательно, но лучше, чем отсутствие действия)
            try:
                self.update_namespace_combos(family, lang, force=True)
                self.ns_update_finished.emit(family, lang, True)
            except Exception:
                try:
                    self.ns_update_finished.emit(family, lang, False)
                except Exception:
                    pass

    def update_family(self, new_family: str):
        """Обновление семейства проектов"""
        self.current_family = new_family
        self._refresh_session_card()

        debug(self._fmt('log.main.skip_ns_on_family_change', family=new_family))

        # Уведомление вкладок об изменении семейства
        for tab in self.content_tabs():
            if hasattr(tab, 'update_family'):
                tab.update_family(new_family)

        # После обновления — вновь связать комбобоксы
        self._link_ns_combos()

    def content_tabs(self):
        """Возвращает список вкладок с основными операциями (без авторизации)."""
        return [
            tab for tab in [
                getattr(self, 'parse_tab', None),
                getattr(self, 'replace_tab', None),
                getattr(self, 'create_tab', None),
                getattr(self, 'rename_tab', None),
                getattr(self, 'redundant_categories_tab', None),
                getattr(self, 'category_content_sync_tab', None),
            ] if tab is not None
        ]

    def record_operation(self, op_key: str, count: int = 0):
        """Обновляет обзорную статистику операций."""
        try:
            count_val = max(0, int(count or 0))
            if op_key == 'parse':
                self._operation_stats['read_pages_total'] = int(
                    self._operation_stats.get('read_pages_total', 0) + count_val
                )
            elif op_key == 'replace':
                self._operation_stats['replace_runs'] = int(
                    self._operation_stats.get('replace_runs', 0) + 1
                )
                self._operation_stats['replace_edits_total'] = int(
                    self._operation_stats.get('replace_edits_total', 0) + count_val
                )
                self._operation_stats['edit_pages_total'] = int(
                    self._operation_stats.get('edit_pages_total', 0) + count_val
                )
            elif op_key == 'create':
                self._operation_stats['create_runs'] = int(
                    self._operation_stats.get('create_runs', 0) + 1
                )
                self._operation_stats['create_edits_total'] = int(
                    self._operation_stats.get('create_edits_total', 0) + count_val
                )
                self._operation_stats['edit_pages_total'] = int(
                    self._operation_stats.get('edit_pages_total', 0) + count_val
                )
            elif op_key == 'rename':
                self._operation_stats['rename_runs'] = int(
                    self._operation_stats.get('rename_runs', 0) + 1
                )
                self._operation_stats['rename_edits_total'] = int(
                    self._operation_stats.get('rename_edits_total', 0) + count_val
                )
                self._operation_stats['edit_pages_total'] = int(
                    self._operation_stats.get('edit_pages_total', 0) + count_val
                )
            elif op_key == 'cleanup':
                self._operation_stats['cleanup_runs'] = int(
                    self._operation_stats.get('cleanup_runs', 0) + 1
                )
                self._operation_stats['cleanup_edits_total'] = int(
                    self._operation_stats.get('cleanup_edits_total', 0) + count_val
                )
                self._operation_stats['edit_pages_total'] = int(
                    self._operation_stats.get('edit_pages_total', 0) + count_val
                )
            elif op_key == 'sync':
                self._operation_stats['sync_runs'] = int(
                    self._operation_stats.get('sync_runs', 0) + 1
                )
                self._operation_stats['sync_edits_total'] = int(
                    self._operation_stats.get('sync_edits_total', 0)
                    + count_val
                )
                self._operation_stats['sync_transferred_total'] = int(
                    self._operation_stats.get('sync_transferred_total', 0)
                    + count_val
                )
                self._operation_stats['edit_pages_total'] = int(
                    self._operation_stats.get('edit_pages_total', 0) + count_val
                )
            self._operation_history.append({
                'ts': datetime.now().strftime('%Y-%m-%d %H:%M'),
                'op': str(op_key or ''),
                'count': int(count_val),
            })
            if len(self._operation_history) > 500:
                self._operation_history = self._operation_history[-500:]
            self._save_ui_settings()
            if getattr(self, 'overview_tab', None) is not None and hasattr(self.overview_tab, 'update_stats'):
                self.overview_tab.update_stats(self._operation_stats, self._operation_history)
        except Exception:
            pass

    def reset_operation_stats(self):
        """Сбрасывает накопленную статистику и историю операций."""
        try:
            for key in list(self._operation_stats.keys()):
                self._operation_stats[key] = 0
            self._operation_history = []
            self._save_ui_settings()
            if getattr(self, 'overview_tab', None) is not None and hasattr(self.overview_tab, 'update_stats'):
                self.overview_tab.update_stats(self._operation_stats, self._operation_history)
        except Exception:
            pass

    # ===== Связь выпадающих списков Namespace между вкладками =====
    def _gather_ns_combos(self):
        combos = []
        mapping = [
            (self.parse_tab, 'ns_combo_parse'),
            (self.replace_tab, 'rep_ns_combo'),
            (self.create_tab, 'ns_combo_create'),
            (self.rename_tab, 'rename_ns_combo'),
            (self.redundant_categories_tab, 'redundant_ns_combo'),
            (self.category_content_sync_tab, 'sync_ns_combo'),
        ]
        for tab, attr in mapping:
            try:
                combo = getattr(tab, attr, None)
                if combo is not None:
                    combos.append(combo)
            except Exception:
                pass
        try:
            if getattr(self, 'header_ns_combo', None) is not None:
                combos.append(self.header_ns_combo)
        except Exception:
            pass
        return combos

    def _unlink_ns_combos(self):
        if not hasattr(self, '_ns_combo_slots'):
            self._ns_combo_slots = {}
        # Отключаем предыдущие слоты, если были
        for combo, slot in list(self._ns_combo_slots.items()):
            try:
                combo.currentIndexChanged.disconnect(slot)
            except Exception:
                pass
        self._ns_combo_slots.clear()

    def _link_ns_combos(self):
        try:
            combos = self._gather_ns_combos()
            if not combos:
                return
            self._unlink_ns_combos()

            def make_slot(src_combo):
                def _on_changed(idx):
                    try:
                        data_val = src_combo.itemData(idx)
                    except Exception:
                        data_val = None
                    for other in combos:
                        if other is src_combo:
                            continue
                        try:
                            if data_val is None:
                                with QSignalBlocker(other):
                                    other.setCurrentIndex(idx)
                            else:
                                row = other.findData(data_val)
                                if row != -1 and row != other.currentIndex():
                                    with QSignalBlocker(other):
                                        other.setCurrentIndex(row)
                        except Exception:
                            pass
                return _on_changed

            # Подключаем слоты
            for c in combos:
                slot = make_slot(c)
                try:
                    c.currentIndexChanged.connect(slot)
                    self._ns_combo_slots[c] = slot
                except Exception:
                    pass

            # Начальная синхронизация: берём текущий индекс первого
            leader = combos[0]
            try:
                idx = leader.currentIndex()
                # руками вызываем слот лидера для выравнивания
                if leader in self._ns_combo_slots:
                    self._ns_combo_slots[leader](idx)
            except Exception:
                pass
        except Exception:
            pass

    def _add_info_button(self, host_layout, text: str, inline: bool = False):
        """Делегирует создание кнопки справки в ui_helpers.add_info_button"""
        try:
            return ui_add_info_button(self, host_layout, text, inline)
        except Exception:
            # Fallback на простое сообщение
            try:
                btn = QToolButton()
                btn.setObjectName('infoButton')
                btn.setText('?')
                btn.setAutoRaise(True)
                btn.setToolTip(self._t('ui.help'))
                try:
                    btn.setFixedSize(23, 23)
                except Exception:
                    pass
                btn.clicked.connect(
                    lambda _=None, t=text: QMessageBox.information(self, self._t('ui.help'), t))
                if hasattr(host_layout, 'addWidget'):
                    host_layout.addWidget(btn)
                return btn
            except Exception:
                return None

    def _force_on_top(self, enable: bool, delay_ms: int = 0) -> None:
        """Делегирует в ui_helpers.force_on_top"""
        try:
            ui_force_on_top(self, enable, delay_ms)
        except Exception:
            pass

    def _bring_to_front_sequence(self) -> None:
        """Делегирует в ui_helpers.bring_to_front_sequence"""
        try:
            ui_bring_to_front_sequence(self)
        except Exception:
            pass

    def show_debug(self):
        """Показать/скрыть окно отладки (singleton поведение)"""
        from .dialogs.debug_dialog import DebugDialog

        # Проверяем, есть ли уже открытое окно debug
        if hasattr(self, '_debug_dialog') and self._debug_dialog is not None:
            if self._debug_dialog.isVisible():
                # Если окно видимо, скрываем его
                self._debug_dialog.hide()
            else:
                # Если окно скрыто, показываем его
                self._debug_dialog.show()
                self._debug_dialog.raise_()
                self._debug_dialog.activateWindow()
        else:
            # Создаем новое окно debug
            self._debug_dialog = DebugDialog(self)
            # Подключаем сигнал закрытия для сброса ссылки
            self._debug_dialog.finished.connect(self._on_debug_dialog_closed)
            self._debug_dialog.show()

    def _on_debug_dialog_closed(self):
        """Обработка закрытия debug диалога"""
        self._debug_dialog = None

    def eventFilter(self, obj, event):
        """Фильтр событий для обработки специальных событий"""
        try:
            if event.type() == QEvent.MouseButtonRelease and obj in self._session_click_targets:
                self._set_tab_from_nav(0)
                return True
            if event.type() == QEvent.MouseButtonRelease and obj in self._brand_click_targets:
                self._set_tab_from_nav(7)
                return True
            from ..utils import get_debug_view
            debug_view = get_debug_view()
            if event.type() == QEvent.KeyPress and debug_view is not None and obj is debug_view:
                pass
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        try:
            super().showEvent(event)
        finally:
            QTimer.singleShot(0, self._apply_windows_titlebar_theme)

    def get_core_components(self):
        """Получить все core компоненты для использования в вкладках"""
        return {
            'api_client': self.api_client,
            'namespace_manager': self.namespace_manager,
            'config_manager': self.config_manager,
            'template_manager': self.template_manager
        }

    def closeEvent(self, event):
        """Обработка закрытия окна"""
        # Проверяем запущенные потоки как в оригинале
        running_threads = []
        tab_workers = [
            ('parse', self.parse_tab, 'worker'),
            ('replace', self.replace_tab, 'rworker'),
            ('create', self.create_tab, 'cworker'),
            ('rename', self.rename_tab, 'mrworker'),
            ('redundant', self.redundant_categories_tab, 'rcworker'),
            ('sync', self.category_content_sync_tab, 'sync_worker'),
            ('sync_preview', self.category_content_sync_tab, 'preview_worker'),
            ('auth', self.auth_tab, '_login_worker')
        ]

        for tab_name, tab, worker_attr in tab_workers:
            worker = getattr(tab, worker_attr, None)
            if worker and hasattr(worker, 'isRunning') and worker.isRunning():
                running_threads.append(tab_name)

        if running_threads:
            res = QMessageBox.question(
                self,
                self._t('ui.warning'),
                translate_key(
                    'ui.main.close_running_text',
                    self._ui_lang,
                    '',
                ).format(tabs=', '.join(running_threads)),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if res != QMessageBox.Yes:
                event.ignore()
                return

        # Не пытаемся сохранять/валидировать учётные данные при закрытии, чтобы не появлялись диалоги
        # (раньше вызывался save_creds(), что могло запрашивать ввод пароля при закрытии окна)

        # Точечная остановка LoginWorker, чтобы избежать предупреждения QThread при выходе
        try:
            login_worker = getattr(self.auth_tab, '_login_worker', None)
            if login_worker and hasattr(login_worker, 'isRunning') and login_worker.isRunning():
                try:
                    if hasattr(login_worker, 'request_stop'):
                        login_worker.request_stop()
                except Exception:
                    pass
                try:
                    login_worker.wait(1500)
                except Exception:
                    pass
                try:
                    if hasattr(login_worker, 'isRunning') and login_worker.isRunning() and hasattr(login_worker, 'terminate'):
                        login_worker.terminate()
                        login_worker.wait(1000)
                except Exception:
                    pass
        except Exception:
            pass

        try:
            self._save_ui_settings()
        except Exception:
            pass
        super().closeEvent(event)







