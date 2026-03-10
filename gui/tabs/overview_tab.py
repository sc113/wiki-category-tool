# -*- coding: utf-8 -*-
"""
Обзорная вкладка: общий статус, настройки и статистика операций.
"""

from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGroupBox, QComboBox, QProgressBar, QFrame, QListWidget, QToolButton,
    QGridLayout, QSizePolicy
)

from ...constants import APP_VERSION
from ...core.localization import translate_key


class OverviewTab(QWidget):
    """Обзор приложения: разделы Overview/Settings/Statistics."""

    DEV_PROFILE_URL = 'https://ru.wikipedia.org/wiki/%D0%A3%D1%87%D0%B0%D1%81%D1%82%D0%BD%D0%B8%D0%BA:Solidest'
    DEV_GITHUB_URL = 'https://github.com/sc113/wiki-category-tool'

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent
        self._ui_lang = 'ru'
        self._bar_title_labels = {}

        # Overview
        self.overview_group = None
        self.ov_status_title = None
        self.ov_status_value = None
        self.ov_project_title = None
        self.ov_project_value = None
        self.ov_version_title = None
        self.ov_version_value = None
        self.ov_theme_title = None
        self.ov_theme_value = None
        self.dev_footer = None
        self.dev_caption = None
        self.dev_debug_btn = None
        self.dev_profile_btn = None
        self.dev_github_btn = None
        self.quick_update_btn = None

        # Settings
        self.settings_group = None
        self.lang_label = None
        self.lang_combo = None
        self.refresh_ns_btn = None
        self.check_updates_btn = None
        self.debug_btn = None

        # Statistics
        self.stats_group = None
        self.stats_title_label = None
        self.stats_reset_btn = None
        self._run_title_labels = {}
        self.stat_read_title = None
        self.stat_read_value = None
        self.stat_edit_title = None
        self.stat_sync_transferred_title = None
        self.stat_sync_transferred_value = None
        self.stat_edit_value = None
        self.bar_replace = None
        self.bar_create = None
        self.bar_rename = None
        self.bar_cleanup = None
        self.bar_sync = None
        self.stat_replace_runs = None
        self.stat_create_runs = None
        self.stat_rename_runs = None
        self.stat_cleanup_runs = None
        self.stat_sync_runs = None
        self.history_title = None
        self.history_list = None

        self._setup_ui()
        self.set_ui_language('ru')

    def _t(self, key: str) -> str:
        return translate_key(key, self._ui_lang, '')

    def _setup_ui(self):
        root = QVBoxLayout(self)
        try:
            root.setContentsMargins(0, 0, 0, 0)
            root.setSpacing(10)
        except Exception:
            pass

        self.quick_update_btn = None
        self.check_updates_btn = None

        self.overview_group = self._build_overview_section()
        self.settings_group = self._build_settings_section()
        self.stats_group = self._build_stats_section()
        self.dev_footer = self._build_developer_footer()
        root.addWidget(self.overview_group)
        root.addWidget(self.settings_group)
        root.addWidget(self.stats_group)
        root.addWidget(self.dev_footer)

    def _build_overview_section(self) -> QGroupBox:
        box = QGroupBox(self._t('ui.session_and_project'))
        box.setObjectName('overviewSection')
        lay = QVBoxLayout(box)
        try:
            lay.setContentsMargins(10, 10, 10, 10)
            lay.setSpacing(8)
        except Exception:
            pass

        cards_row = QHBoxLayout()
        try:
            cards_row.setContentsMargins(0, 0, 0, 0)
            cards_row.setSpacing(8)
        except Exception:
            pass
        cards_row.addWidget(self._build_meta_card(self._t('ui.session'), self._t('ui.inactive'), 'status'), 1)
        cards_row.addWidget(self._build_meta_card(self._t('ui.project_label_short'), 'wikipedia / ru', 'project'), 1)
        cards_row.addWidget(self._build_meta_card(self._t('ui.version'), f'v{APP_VERSION}', 'version'), 1)
        cards_row.addWidget(self._build_meta_card(self._t('ui.theme'), 'twilight teal', 'theme'), 1)
        lay.addLayout(cards_row)
        return box

    def _build_developer_footer(self) -> QFrame:
        footer = QFrame()
        footer.setObjectName('developerMini')
        lay = QHBoxLayout(footer)
        try:
            lay.setContentsMargins(2, 1, 6, 1)
            lay.setSpacing(6)
        except Exception:
            pass
        self.dev_debug_btn = QToolButton()
        self.dev_debug_btn.setObjectName('developerLinkButton')
        self.dev_debug_btn.setText(self._t('ui.debug'))
        self.dev_debug_btn.clicked.connect(self._open_debug)
        self.dev_caption = QLabel(self._t('ui.developer_solidest'))
        self.dev_caption.setObjectName('developerMiniText')
        self.dev_profile_btn = QToolButton()
        self.dev_profile_btn.setObjectName('developerLinkButton')
        self.dev_profile_btn.setText(self._t('ui.profile'))
        self.dev_github_btn = QToolButton()
        self.dev_github_btn.setObjectName('developerLinkButton')
        self.dev_github_btn.setText(self._t('ui.github'))
        try:
            self.dev_debug_btn.setFixedHeight(27)
            self.dev_profile_btn.setFixedHeight(27)
            self.dev_github_btn.setFixedHeight(27)
        except Exception:
            pass
        self.dev_profile_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.DEV_PROFILE_URL)))
        self.dev_github_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.DEV_GITHUB_URL)))
        lay.addWidget(self.dev_debug_btn)
        lay.addStretch(1)
        lay.addWidget(self.dev_caption)
        lay.addWidget(self.dev_profile_btn)
        lay.addWidget(self.dev_github_btn)
        return footer

    def _build_meta_card(self, title: str, value: str, key: str) -> QFrame:
        card = QFrame()
        card.setObjectName('overviewMetaCard')
        lay = QVBoxLayout(card)
        try:
            lay.setContentsMargins(10, 8, 10, 8)
            lay.setSpacing(2)
        except Exception:
            pass
        title_lbl = QLabel(title)
        title_lbl.setObjectName('overviewCardTitle')
        value_lbl = QLabel(value)
        value_lbl.setObjectName('overviewCardValue')
        lay.addWidget(title_lbl)
        lay.addWidget(value_lbl)

        if key == 'status':
            self.ov_status_title = title_lbl
            self.ov_status_value = value_lbl
        elif key == 'project':
            self.ov_project_title = title_lbl
            self.ov_project_value = value_lbl
        elif key == 'version':
            self.ov_version_title = title_lbl
            self.ov_version_value = value_lbl
        elif key == 'theme':
            self.ov_theme_title = title_lbl
            self.ov_theme_value = value_lbl
        return card

    def _build_settings_section(self) -> QGroupBox:
        box = QGroupBox(self._t('ui.settings'))
        box.setObjectName('settingsSection')
        lay = QVBoxLayout(box)
        try:
            lay.setContentsMargins(10, 10, 10, 10)
            lay.setSpacing(8)
        except Exception:
            pass

        row_lang = QHBoxLayout()
        self.lang_label = QLabel(self._t('ui.interface_language'))
        self.lang_combo = QComboBox()
        self.lang_combo.addItem('', 'ru')
        self.lang_combo.addItem('', 'en')
        self.lang_combo.currentIndexChanged.connect(self._on_lang_changed)
        try:
            self.lang_combo.setMinimumWidth(170)
            self.lang_combo.setMaximumWidth(210)
        except Exception:
            pass
        row_lang.addWidget(self.lang_label)
        self.refresh_ns_btn = QPushButton(self._t('ui.refresh_namespace_prefixes'))
        self.refresh_ns_btn.setToolTip(self._t('ui.refresh_namespace_prefixes_tooltip'))
        self.refresh_ns_btn.clicked.connect(self._refresh_prefixes)
        row_lang.addWidget(self.lang_combo)
        row_lang.addStretch(1)
        row_lang.addSpacing(8)
        row_lang.addWidget(self.refresh_ns_btn)
        lay.addLayout(row_lang)
        return box

    def _build_stats_section(self) -> QGroupBox:
        box = QGroupBox(self._t('ui.statistics'))
        box.setObjectName('statsSection')
        lay = QVBoxLayout(box)
        try:
            lay.setContentsMargins(10, 10, 10, 10)
            lay.setSpacing(8)
        except Exception:
            pass

        hdr = QHBoxLayout()
        self.stats_title_label = QLabel(self._t('ui.operations_and_history'))
        self.stats_title_label.setObjectName('statsTitle')
        self.stats_reset_btn = QToolButton()
        self.stats_reset_btn.setObjectName('miniResetButton')
        self.stats_reset_btn.setText('⟲')
        self.stats_reset_btn.setToolTip(self._t('ui.reset_statistics'))
        try:
            self.stats_reset_btn.setFixedSize(27, 27)
        except Exception:
            pass
        self.stats_reset_btn.clicked.connect(self._reset_stats)
        hdr.addWidget(self.stats_title_label)
        hdr.addStretch(1)
        hdr.addWidget(self.stats_reset_btn)
        lay.addLayout(hdr)

        body = QHBoxLayout()
        try:
            body.setContentsMargins(0, 0, 0, 0)
            body.setSpacing(10)
        except Exception:
            pass

        # Левая часть: бары + ключевые счётчики
        bars_panel = QFrame()
        bars_panel.setObjectName('statsBarsPanel')
        bars_l = QVBoxLayout(bars_panel)
        try:
            bars_l.setContentsMargins(10, 8, 10, 8)
            bars_l.setSpacing(6)
        except Exception:
            pass

        totals_grid = QGridLayout()
        try:
            totals_grid.setContentsMargins(0, 0, 0, 0)
            totals_grid.setHorizontalSpacing(8)
            totals_grid.setVerticalSpacing(8)
        except Exception:
            pass
        totals_grid.addWidget(
            self._build_total_card(self._t('ui.pages_read_label'), 'read'),
            0,
            0,
        )
        totals_grid.addWidget(
            self._build_total_card(self._t('ui.total_edits_label'), 'edit'),
            0,
            1,
        )
        totals_grid.addWidget(
            self._build_total_card(self._t('ui.transferred_sync_label'), 'sync_transferred'),
            0,
            2,
        )
        for col in range(3):
            totals_grid.setColumnStretch(col, 1)
        bars_l.addLayout(totals_grid)

        runs_row = QHBoxLayout()
        try:
            runs_row.setContentsMargins(0, 0, 0, 0)
            runs_row.setSpacing(6)
        except Exception:
            pass
        cards = (
            self._build_runs_card(self._t('ui.replace'), 'replace'),
            self._build_runs_card(self._t('ui.create'), 'create'),
            self._build_runs_card(self._t('ui.rename'), 'rename'),
            self._build_runs_card(self._t('ui.redundant_short'), 'cleanup'),
            self._build_runs_card(self._t('ui.sync_short'), 'sync'),
        )
        for card in cards:
            runs_row.addWidget(card, 1)
        bars_l.addLayout(runs_row)

        self.bar_replace = self._make_bar_row(bars_l, 'replace', self._t('ui.replace'))
        self.bar_create = self._make_bar_row(bars_l, 'create', self._t('ui.create'))
        self.bar_rename = self._make_bar_row(bars_l, 'rename', self._t('ui.rename'))
        self.bar_cleanup = self._make_bar_row(bars_l, 'cleanup', self._t('ui.redundant_categories'))
        self.bar_sync = self._make_bar_row(bars_l, 'sync', self._t('ui.category_sync_label'))

        # Правая часть: история
        history_panel = QFrame()
        history_panel.setObjectName('statsHistoryPanel')
        hist_l = QVBoxLayout(history_panel)
        try:
            hist_l.setContentsMargins(10, 8, 10, 8)
            hist_l.setSpacing(6)
        except Exception:
            pass
        self.history_title = QLabel(self._t('ui.history'))
        self.history_list = QListWidget()
        try:
            self.history_list.setUniformItemSizes(True)
        except Exception:
            pass
        hist_l.addWidget(self.history_title)
        hist_l.addWidget(self.history_list, 1)

        body.addWidget(bars_panel, 3)
        body.addWidget(history_panel, 2)
        lay.addLayout(body, 1)
        return box

    def _build_total_card(self, title: str, key: str) -> QFrame:
        card = QFrame()
        card.setObjectName('statsTotalCard')
        try:
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            card.setMinimumHeight(58)
        except Exception:
            pass
        lay = QVBoxLayout(card)
        try:
            lay.setContentsMargins(10, 7, 10, 7)
            lay.setSpacing(1)
        except Exception:
            pass
        t = QLabel(title)
        t.setObjectName('statsKpiTitle')
        v = QLabel('0')
        v.setObjectName('statsKpiValue')
        lay.addWidget(t)
        lay.addWidget(v)
        if key == 'read':
            self.stat_read_title = t
            self.stat_read_value = v
        elif key == 'edit':
            self.stat_edit_title = t
            self.stat_edit_value = v
        elif key == 'sync_transferred':
            self.stat_sync_transferred_title = t
            self.stat_sync_transferred_value = v
        return card

    def _build_runs_card(self, title: str, key: str) -> QFrame:
        card = QFrame()
        card.setObjectName('statsRunChip')
        try:
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            card.setMinimumWidth(0)
            card.setMinimumHeight(34)
            card.setMaximumHeight(34)
        except Exception:
            pass
        lay = QHBoxLayout(card)
        try:
            lay.setContentsMargins(9, 5, 9, 5)
            lay.setSpacing(6)
        except Exception:
            pass
        t = QLabel(title)
        t.setObjectName('statsRunTitle')
        v = QLabel('0')
        v.setObjectName('statsRunValue')
        try:
            v.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        except Exception:
            pass
        lay.addWidget(t, 1)
        lay.addWidget(v, 0)
        self._run_title_labels[key] = t
        if key == 'replace':
            self.stat_replace_runs = v
        elif key == 'create':
            self.stat_create_runs = v
        elif key == 'rename':
            self.stat_rename_runs = v
        elif key == 'cleanup':
            self.stat_cleanup_runs = v
        elif key == 'sync':
            self.stat_sync_runs = v
        return card

    def _make_bar_row(self, layout: QVBoxLayout, key: str, title: str) -> QProgressBar:
        row = QHBoxLayout()
        label = QLabel(title)
        label.setObjectName('statsBarLabel')
        row.addWidget(label)
        self._bar_title_labels[key] = label
        bar = QProgressBar()
        bar.setObjectName('statsProgressBar')
        try:
            bar.setTextVisible(False)
            bar.setMaximum(1)
            bar.setValue(0)
        except Exception:
            pass
        row.addWidget(bar, 1)
        layout.addLayout(row)
        return bar

    def _on_lang_changed(self):
        code = 'ru'
        try:
            code = str(self.lang_combo.currentData() or 'ru')
        except Exception:
            pass
        try:
            if self.parent_window and hasattr(self.parent_window, 'set_ui_language'):
                self.parent_window.set_ui_language(code)
        except Exception:
            pass

    def _open_debug(self):
        try:
            if self.parent_window and hasattr(self.parent_window, 'show_debug'):
                self.parent_window.show_debug()
        except Exception:
            pass

    def _refresh_prefixes(self):
        try:
            auth = getattr(self.parent_window, 'auth_tab', None)
            if auth and hasattr(auth, 'force_namespace_update'):
                auth.force_namespace_update()
        except Exception:
            pass

    def _check_updates(self):
        try:
            auth = getattr(self.parent_window, 'auth_tab', None)
            if auth and hasattr(auth, 'check_updates'):
                auth.check_updates()
        except Exception:
            pass

    def _reset_stats(self):
        try:
            if self.parent_window and hasattr(self.parent_window, 'reset_operation_stats'):
                self.parent_window.reset_operation_stats()
        except Exception:
            pass

    def set_ui_language(self, lang: str):
        self._ui_lang = 'en' if str(lang).lower().startswith('en') else 'ru'
        try:
            blocked = self.lang_combo.blockSignals(True)
            self.lang_combo.setCurrentIndex(1 if self._ui_lang == 'en' else 0)
            self.lang_combo.blockSignals(blocked)
        except Exception:
            pass
        self._apply_language_texts()
        self.update_ui_context(
            getattr(self.parent_window, '_theme_mode', None),
            getattr(self.parent_window, '_ui_lang', self._ui_lang),
        )

    def _apply_language_texts(self):
        self.overview_group.setTitle(self._t('ui.session_and_project'))
        self.settings_group.setTitle(self._t('ui.settings'))
        self.stats_group.setTitle(self._t('ui.statistics'))
        self.ov_status_title.setText(self._t('ui.session'))
        self.ov_project_title.setText(self._t('ui.project_label_short'))
        self.ov_version_title.setText(self._t('ui.version'))
        if self.ov_theme_title is not None:
            self.ov_theme_title.setText(self._t('ui.theme'))
        self.dev_caption.setText(self._t('ui.developer_solidest'))
        self.dev_debug_btn.setText(self._t('ui.debug'))
        self.dev_profile_btn.setText(self._t('ui.profile'))
        self.dev_github_btn.setText(self._t('ui.github'))
        self.lang_label.setText(self._t('ui.interface_language'))
        try:
            self.lang_combo.setItemText(0, self._t('ui.language_option_ru'))
            self.lang_combo.setItemText(1, self._t('ui.language_option_en'))
        except Exception:
            pass
        self.refresh_ns_btn.setText(self._t('ui.refresh_namespace_prefixes'))
        self.refresh_ns_btn.setToolTip(self._t('ui.refresh_namespace_prefixes_tooltip'))
        self.stats_title_label.setText(self._t('ui.operations_and_history'))
        self.stats_reset_btn.setToolTip(self._t('ui.reset_statistics'))
        self.history_title.setText(self._t('ui.history'))
        if self.stat_read_title is not None:
            self.stat_read_title.setText(self._t('ui.pages_read_label'))
        if self.stat_edit_title is not None:
            self.stat_edit_title.setText(self._t('ui.total_edits_label'))
        if self.stat_sync_transferred_title is not None:
            self.stat_sync_transferred_title.setText(self._t('ui.transferred_sync_label'))
        for key, lbl in self._run_title_labels.items():
            if key == 'replace':
                lbl.setText(self._t('ui.replace'))
            elif key == 'create':
                lbl.setText(self._t('ui.create'))
            elif key == 'rename':
                lbl.setText(self._t('ui.rename'))
            elif key == 'cleanup':
                lbl.setText(self._t('ui.redundant_short'))
            elif key == 'sync':
                lbl.setText(self._t('ui.sync_short'))
        for key, lbl in self._bar_title_labels.items():
            if key == 'replace':
                lbl.setText(self._t('ui.replace'))
            elif key == 'create':
                lbl.setText(self._t('ui.create'))
            elif key == 'rename':
                lbl.setText(self._t('ui.rename'))
            elif key == 'cleanup':
                lbl.setText(self._t('ui.redundant_categories'))
            elif key == 'sync':
                lbl.setText(self._t('ui.category_sync_label'))

    def update_ui_context(self, theme_mode: str | None, ui_lang: str | None):
        mode = str(theme_mode or '').lower()
        lang = str(ui_lang or self._ui_lang).lower()
        if self.ov_theme_value is not None:
            if lang.startswith('en'):
                theme_name = {
                    'light': 'winter light',
                    'teal': 'twilight teal',
                    'dark': 'midnight dark',
                }.get(mode, 'twilight teal')
            else:
                theme_name = {
                    'light': 'winter light',
                    'teal': 'twilight teal',
                    'dark': 'midnight dark',
                }.get(mode, 'twilight teal')
            self.ov_theme_value.setText(theme_name)
    def update_session(self, user: str | None, family: str | None, lang: str | None):
        active = bool(user)
        self.ov_status_value.setText(self._t('ui.active') if active else self._t('ui.inactive'))
        self.ov_project_value.setText(f'{family or "wikipedia"} / {lang or "ru"}')
        self.ov_version_value.setText(f'v{APP_VERSION}')

    def update_stats(self, stats: dict, history: Iterable[dict] | None = None):
        read_total = int(stats.get('read_pages_total', 0) or 0)
        edit_total = int(stats.get('edit_pages_total', 0) or 0)
        replace_runs = int(stats.get('replace_edits_total', stats.get('replace_runs', 0)) or 0)
        create_runs = int(stats.get('create_edits_total', stats.get('create_runs', 0)) or 0)
        rename_runs = int(stats.get('rename_edits_total', stats.get('rename_runs', 0)) or 0)
        cleanup_runs = int(stats.get('cleanup_edits_total', stats.get('cleanup_runs', 0)) or 0)
        sync_runs = int(stats.get('sync_edits_total', stats.get('sync_runs', 0)) or 0)
        sync_transferred_total = int(stats.get('sync_transferred_total', sync_runs) or 0)

        self.stat_read_value.setText(str(read_total))
        self.stat_edit_value.setText(str(edit_total))
        if self.stat_replace_runs is not None:
            self.stat_replace_runs.setText(str(replace_runs))
        if self.stat_create_runs is not None:
            self.stat_create_runs.setText(str(create_runs))
        if self.stat_rename_runs is not None:
            self.stat_rename_runs.setText(str(rename_runs))
        if self.stat_cleanup_runs is not None:
            self.stat_cleanup_runs.setText(str(cleanup_runs))
        if self.stat_sync_runs is not None:
            self.stat_sync_runs.setText(str(sync_runs))
        if self.stat_sync_transferred_value is not None:
            self.stat_sync_transferred_value.setText(str(sync_transferred_total))
        runs_max = max(1, replace_runs + create_runs + rename_runs + cleanup_runs + sync_runs)
        for bar, value in (
            (self.bar_replace, replace_runs),
            (self.bar_create, create_runs),
            (self.bar_rename, rename_runs),
            (self.bar_cleanup, cleanup_runs),
            (self.bar_sync, sync_runs),
        ):
            try:
                bar.setMaximum(runs_max)
                bar.setValue(max(0, value))
            except Exception:
                pass
        self.update_history(list(history or []))

    def update_history(self, history: list[dict]):
        try:
            self.history_list.clear()
        except Exception:
            pass
        if not history:
            return

        last_items = history[-120:]
        op_name = {
            'parse': 'ui.read',
            'replace': 'ui.replace',
            'create': 'ui.create',
            'rename': 'ui.rename',
            'cleanup': 'ui.redundant_categories',
            'sync': 'ui.category_sync_label',
        }
        for item in last_items:
            cnt = int(item.get('count', 0) or 0)
            try:
                op = str(item.get('op') or '')
                ts = str(item.get('ts') or '')
                disp = self._t(op_name.get(op, '')) or op
                row = f'{ts}  {disp}: +{cnt}'
                self.history_list.addItem(row)
            except Exception:
                pass
