# -*- coding: utf-8 -*-
"""
Переиспользуемые панели интерфейса для вкладок.
"""

import urllib.parse
import webbrowser
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QPushButton, QToolButton, QTextEdit, QMessageBox, QSpinBox,
    QGridLayout, QSizePolicy, QGroupBox, QSplitter, QAbstractSpinBox
)

from ...constants import PREFIX_TOOLTIP, REQUEST_HEADERS
from ...core.api_client import WikimediaAPIClient, REQUEST_SESSION, _rate_wait
from ...core.localization import translate_key
from ...utils import debug
from ...workers.category_fetch_worker import CategoryFetchWorker
from .ui_helpers import add_info_button, pick_file, open_from_edit, log_message


_GROUP_STYLE = (
    ""
)

_FETCH_MODE_CATEGORIES_ONLY = 'categories_only'
_FETCH_MODE_NON_CATEGORIES_ONLY = 'non_categories_only'
_FETCH_MODE_BOTH = 'both'


class CategorySourcePanel(QGroupBox):
    """Универсальная левая панель источника страниц."""

    def __init__(
        self,
        parent=None,
        parent_window=None,
        log_widget: QTextEdit | None = None,
        help_text: str = '',
        group_title: str = '',
        category_section_label: str = '',
        category_placeholder: str = '',
        manual_label: str = '',
        manual_placeholder: str = '',
        file_section_label: str = '',
        file_caption: str = '',
        default_input_path: str = '',
    ):
        self.parent_window = parent_window or parent
        super().__init__(group_title or self._t('ui.source', 'Source'), parent)
        self.log_widget = log_widget
        self.help_text = help_text
        self._fetch_worker = None
        if _GROUP_STYLE:
            self.setStyleSheet(_GROUP_STYLE)
        self._setup_ui(
            category_section_label=category_section_label or self._t('ui.fetch_category_content', '<b>Fetch category content</b>'),
            category_placeholder=category_placeholder or self._t('ui.root_category_name', 'Root category name'),
            manual_label=manual_label or self._t('ui.list_of_pages_to_process', '<b>List of pages</b>'),
            manual_placeholder=manual_placeholder or self._t('ui.page_list_one_per_line', 'Page list (one per line)'),
            file_section_label=file_section_label or self._t('ui.or_load_list_from_file', '<b>Or load list from file</b>'),
            file_caption=file_caption or self._t('ui.file_txt', 'File (.txt):'),
            default_input_path=default_input_path,
        )
        self._refresh_fetch_mode_combo_texts()

    def _ui_lang(self) -> str:
        try:
            raw = str(getattr(self.parent_window, '_ui_lang', 'ru')).lower()
        except Exception:
            raw = 'ru'
        return 'en' if raw.startswith('en') else 'ru'

    def _t(self, key: str, fallback: str) -> str:
        try:
            return translate_key(key, self._ui_lang(), fallback)
        except Exception:
            return fallback

    def _refresh_fetch_mode_combo_texts(self) -> None:
        try:
            current_data = str(self.fetch_mode_combo.currentData() or 'categories_only')
        except Exception:
            current_data = 'categories_only'

        labels = {
            'categories_only': self._t(
                'ui.source.fetch_mode.categories_only', 'Categories only'
            ),
            'non_categories_only': self._t(
                'ui.source.fetch_mode.non_categories_only_alt', 'Except categories',
            ),
            'both': self._t('ui.source.fetch_mode.both_alt', 'All pages'),
        }
        try:
            for idx in range(self.fetch_mode_combo.count()):
                data = str(self.fetch_mode_combo.itemData(idx) or '')
                self.fetch_mode_combo.setItemText(idx, labels.get(data, ''))
        except Exception:
            pass

        try:
            self.fetch_mode_combo.setToolTip(
                self._t(
                    'ui.source.fetch_mode_tooltip',
                    'What to load from the category: only categories, everything except categories, or all pages.',
                )
            )
        except Exception:
            pass

        try:
            self.fetch_mode_combo.view().setTextElideMode(Qt.ElideNone)
            popup_width = max(
                self.fetch_mode_combo.fontMetrics().horizontalAdvance(
                    self.fetch_mode_combo.itemText(i)
                )
                for i in range(self.fetch_mode_combo.count())
            ) + 48
            self.fetch_mode_combo.setMinimumWidth(max(136, popup_width))
            self.fetch_mode_combo.setMaximumWidth(16777215)
            self.fetch_mode_combo.view().setMinimumWidth(popup_width)
        except Exception:
            pass

        try:
            idx = self.fetch_mode_combo.findData(current_data)
            if idx >= 0:
                self.fetch_mode_combo.setCurrentIndex(idx)
        except Exception:
            pass

    def refresh_localized_texts(self) -> None:
        try:
            self.replace_list_btn.setText(self._t('ui.source.replace_list_short', 'Replace'))
            self.append_list_btn.setText(self._t('ui.source.append_list_short', 'Append'))
        except Exception:
            pass
        self._refresh_fetch_mode_combo_texts()
        self._sync_source_action_button_widths()

    def _sync_source_action_button_widths(self) -> None:
        buttons = (
            getattr(self, 'replace_list_btn', None),
            getattr(self, 'append_list_btn', None),
            getattr(self, 'open_petscan_btn', None),
        )
        widths: list[int] = []
        for btn in buttons:
            if btn is None:
                continue
            try:
                txt = str(btn.text() or '').replace('&', '').strip()
                need = btn.fontMetrics().horizontalAdvance(txt) + 28
            except Exception:
                need = 0
            widths.append(max(96, int(need)))
        if not widths:
            return
        target = max(widths)
        for btn in buttons:
            if btn is None:
                continue
            try:
                btn.setMinimumWidth(target)
                btn.setMaximumWidth(16777215)
            except Exception:
                pass

    def _setup_ui(
        self,
        *,
        category_section_label: str,
        category_placeholder: str,
        manual_label: str,
        manual_placeholder: str,
        file_section_label: str,
        file_caption: str,
        default_input_path: str,
    ) -> None:
        layout = QVBoxLayout(self)
        try:
            layout.setContentsMargins(8, 12, 8, 8)
            layout.setSpacing(2)
        except Exception:
            pass

        prefix_layout = QHBoxLayout()
        self.prefix_row = QWidget(self)
        self.prefix_label = QLabel(self._t('ui.prefixes', 'Prefixes:'), self.prefix_row)
        self.prefix_label.setToolTip(PREFIX_TOOLTIP)
        prefix_layout.addWidget(self.prefix_label)

        self.ns_combo = QComboBox()
        self.ns_combo.setEditable(False)
        prefix_layout.addWidget(self.ns_combo)
        prefix_layout.addStretch(1)
        self.prefix_help_btn = add_info_button(self, prefix_layout, self.help_text, inline=True)
        try:
            self.prefix_row.setLayout(prefix_layout)
        except Exception:
            pass
        layout.addWidget(self.prefix_row)

        layout.addSpacing(6)

        self.cat_edit = QLineEdit()
        self.cat_edit.setPlaceholderText(category_placeholder)

        category_fetch_layout = QGridLayout()
        try:
            category_fetch_layout.setContentsMargins(0, 0, 0, 0)
            category_fetch_layout.setHorizontalSpacing(8)
            category_fetch_layout.setVerticalSpacing(6)
            category_fetch_layout.setColumnStretch(0, 1)
            category_fetch_layout.setColumnStretch(4, 1)
        except Exception:
            pass
        self.fetch_mode_combo = QComboBox()
        self.fetch_mode_combo.setObjectName('sourceFetchModeCombo')
        self.fetch_mode_combo.addItem('', _FETCH_MODE_CATEGORIES_ONLY)
        self.fetch_mode_combo.addItem('', _FETCH_MODE_NON_CATEGORIES_ONLY)
        self.fetch_mode_combo.addItem('', _FETCH_MODE_BOTH)
        try:
            self.fetch_mode_combo.setCurrentIndex(0)
        except Exception:
            pass
        try:
            self.fetch_mode_combo.setSizeAdjustPolicy(
                QComboBox.AdjustToMinimumContentsLengthWithIcon
            )
            self.fetch_mode_combo.setMinimumContentsLength(1)
            self.fetch_mode_combo.setMinimumWidth(132)
            self.fetch_mode_combo.setMaximumWidth(16777215)
            self.fetch_mode_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        except Exception:
            pass

        self.replace_list_btn = QPushButton(
            self._t('ui.source.replace_list_short', 'Replace')
        )
        self.replace_list_btn.setObjectName('sourceActionButton')
        self.replace_list_btn.setToolTip(
            self._t(
                'ui.source.replace_list_tooltip',
                'Replace the current list with fetched results.',
            )
        )
        self.replace_list_btn.clicked.connect(self.fetch_selected_mode_replace)

        self.append_list_btn = QPushButton(
            self._t('ui.source.append_list_short', 'Append')
        )
        self.append_list_btn.setObjectName('sourceActionButton')
        self.append_list_btn.setToolTip(
            self._t(
                'ui.source.append_list_tooltip',
                'Append fetched results to the end of the current list on a new line.',
            )
        )
        self.append_list_btn.clicked.connect(self.fetch_selected_mode_append)

        self.open_petscan_btn = QPushButton(
            self._t('ui.source.open_petscan_short', 'PetScan')
        )
        self.open_petscan_btn.setObjectName('sourceActionButton')
        self.open_petscan_btn.setToolTip(
            self._t(
                'ui.source.open_petscan_tooltip',
                'Open PetScan with advanced settings for the selected category',
            )
        )
        self.open_petscan_btn.clicked.connect(self.open_petscan_in_browser)

        depth_label = QLabel(self._t('ui.depth', 'Depth:'))
        depth_label.setToolTip(
            self._t(
                'ui.source.depth_tooltip',
                'Category recursion depth. For "Pages": 0 = root category only, 1 = include direct subcategories. For "Subcategories": 0 = direct subcategories only.',
            )
        )
        self.depth_spin = QSpinBox()
        self.depth_spin.setObjectName('depthSpin')
        self.depth_spin.setMinimum(0)
        self.depth_spin.setMaximum(99)
        self.depth_spin.setValue(0)
        self.depth_spin.setToolTip(
            self._t(
                'ui.category_recursion_depth_for_pages_and_subcategories',
                'Category recursion depth for pages and subcategories',
            )
        )
        try:
            self.depth_spin.setFixedWidth(38)
            self.depth_spin.setAlignment(Qt.AlignCenter)
            self.depth_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        except Exception:
            pass

        self.depth_plus_btn = QToolButton()
        self.depth_plus_btn.setObjectName('depthStepButton')
        self.depth_plus_btn.setText('+')
        self.depth_plus_btn.setToolTip(
            self._t('ui.sync.depth_plus', 'Increase depth')
        )
        self.depth_plus_btn.clicked.connect(self.depth_spin.stepUp)
        self.depth_minus_btn = QToolButton()
        self.depth_minus_btn.setObjectName('depthStepButton')
        self.depth_minus_btn.setText('-')
        self.depth_minus_btn.setToolTip(
            self._t('ui.sync.depth_minus', 'Decrease depth')
        )
        self.depth_minus_btn.clicked.connect(self.depth_spin.stepDown)
        try:
            self.depth_plus_btn.setFixedSize(14, 14)
            self.depth_minus_btn.setFixedSize(14, 14)
        except Exception:
            pass

        depth_step_layout = QVBoxLayout()
        try:
            depth_step_layout.setContentsMargins(0, 0, 0, 0)
            depth_step_layout.setSpacing(0)
        except Exception:
            pass
        depth_step_layout.addWidget(self.depth_plus_btn)
        depth_step_layout.addWidget(self.depth_minus_btn)

        depth_wrap = QWidget()
        depth_wrap_layout = QHBoxLayout(depth_wrap)
        try:
            depth_wrap_layout.setContentsMargins(0, 0, 0, 0)
            depth_wrap_layout.setSpacing(2)
        except Exception:
            pass
        depth_wrap_layout.addWidget(self.depth_spin)
        depth_wrap_layout.addLayout(depth_step_layout)

        layout.addWidget(QLabel(category_section_label))

        row_two_layout = QHBoxLayout()
        try:
            row_two_layout.setContentsMargins(0, 0, 0, 0)
            row_two_layout.setSpacing(8)
        except Exception:
            pass
        row_two_layout.addWidget(self.cat_edit, 1)
        row_two_layout.addWidget(depth_label, 0)
        row_two_layout.addWidget(depth_wrap, 0)
        category_fetch_layout.addLayout(row_two_layout, 0, 0, 1, 5)

        buttons_layout = QHBoxLayout()
        try:
            buttons_layout.setContentsMargins(0, 0, 0, 0)
            buttons_layout.setSpacing(4)
        except Exception:
            pass
        self.replace_list_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.append_list_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.open_petscan_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        buttons_layout.addWidget(self.replace_list_btn, 1)
        buttons_layout.addWidget(self.append_list_btn, 1)
        buttons_layout.addWidget(self.open_petscan_btn, 1)
        buttons_layout.addSpacing(6)
        buttons_layout.addWidget(self.fetch_mode_combo, 1)
        self._sync_source_action_button_widths()
        category_fetch_layout.addLayout(buttons_layout, 1, 0, 1, 5)
        layout.addLayout(category_fetch_layout)

        layout.addSpacing(6)
        layout.addWidget(QLabel(manual_label))
        self.manual_list = QTextEdit()
        self.manual_list.setPlaceholderText(manual_placeholder)
        self.manual_list.setMinimumHeight(220)
        layout.addWidget(self.manual_list, 1)
        self.list_edit = self.manual_list
        try:
            layout.setStretchFactor(self.manual_list, 1)
        except Exception:
            pass

        layout.addWidget(QLabel(file_section_label))
        file_layout = QHBoxLayout()
        file_layout.addWidget(QLabel(file_caption))

        self.in_path = QLineEdit(default_input_path)
        self.in_path.setMinimumWidth(0)
        file_layout.addWidget(self.in_path, 1)

        btn_browse_in = QToolButton()
        btn_browse_in.setText('…')
        btn_browse_in.setAutoRaise(False)
        try:
            btn_browse_in.setFixedSize(27, 27)
            btn_browse_in.setCursor(Qt.PointingHandCursor)
            btn_browse_in.setToolTip(self._t('ui.choose_file', 'Choose file'))
        except Exception:
            pass
        btn_browse_in.clicked.connect(
            lambda: pick_file(self, self.in_path, '*.txt'))
        file_layout.addWidget(btn_browse_in)

        btn_open_in = QPushButton(self._t('ui.open', 'Open'))
        btn_open_in.clicked.connect(lambda: open_from_edit(self, self.in_path))
        file_layout.addWidget(btn_open_in)

        layout.addLayout(file_layout)
        self.file_edit = self.in_path

    def set_log_widget(self, log_widget: QTextEdit | None) -> None:
        self.log_widget = log_widget

    def set_prefix_controls_visible(self, visible: bool) -> None:
        try:
            self.prefix_row.setVisible(bool(visible))
        except Exception:
            try:
                self.ns_combo.setVisible(bool(visible))
                self.prefix_label.setVisible(bool(visible))
                if getattr(self, 'prefix_help_btn', None) is not None:
                    self.prefix_help_btn.setVisible(bool(visible))
            except Exception:
                pass

    def _log(self, message: str) -> None:
        if self.log_widget is not None:
            log_message(self.log_widget, message, debug)
            return
        debug(message)

    def update_namespace_combo(self, family: str, lang: str) -> None:
        try:
            nm = getattr(self.parent_window, 'namespace_manager', None)
            if nm:
                nm.populate_ns_combo(self.ns_combo, family, lang)
                nm._adjust_combo_popup_width(self.ns_combo)
        except Exception:
            pass

    def get_current_language(self) -> str:
        try:
            lang = getattr(self.parent_window, 'current_lang', None)
            if lang:
                return lang
        except Exception:
            pass
        try:
            auth = getattr(self.parent_window, 'auth_tab', None)
            if auth and hasattr(auth, 'lang_combo') and auth.lang_combo:
                return auth.lang_combo.currentText() or 'ru'
        except Exception:
            pass
        return 'ru'

    def get_current_family(self) -> str:
        try:
            fam = getattr(self.parent_window, 'current_family', None)
            if fam:
                return fam
            auth = getattr(self.parent_window, 'auth_tab', None)
            if auth and hasattr(auth, 'family_combo') and auth.family_combo:
                return auth.family_combo.currentText() or 'wikipedia'
        except Exception:
            pass
        return 'wikipedia'

    def load_titles_from_inputs(self) -> list[str]:
        in_file = (self.in_path.text() or '').strip()
        if in_file:
            with open(in_file, encoding='utf-8') as file_obj:
                return [line.strip() for line in file_obj if line.strip()]
        return [
            line.strip()
            for line in self.manual_list.toPlainText().splitlines()
            if line.strip()
        ]

    def _build_petscan_url(self, family: str, lang: str, category: str, depth: int = 0) -> str:
        from ...core.namespace_manager import strip_ns_prefix

        base = strip_ns_prefix(family, lang, category, 14)
        cat_param = urllib.parse.quote_plus(base)
        petscan_url = (
            'https://petscan.wmcloud.org/?combination=subset&interface_language=en&ores_prob_from=&'
            'referrer_name=&ores_prob_to=&min_sitelink_count=&wikidata_source_sites=&templates_yes=&'
            'sortby=title&pagepile=&cb_labels_no_l=1&show_disambiguation_pages=both&language=' + lang +
            '&max_sitelink_count=&cb_labels_yes_l=1&outlinks_any=&common_wiki=auto&categories=' + cat_param +
            '&edits%5Bbots%5D=both&wikidata_prop_item_use=&ores_prediction=any&outlinks_no=&source_combination=&'
            'ns%5B14%5D=1&sitelinks_any=&cb_labels_any_l=1&edits%5Banons%5D=both&links_to_no=&search_wiki=&'
            f'project={family}&after=&wikidata_item=no&search_max_results=1000&langs_labels_no=&langs_labels_yes=&'
            'sortorder=ascending&templates_any=&show_redirects=both&active_tab=tab_output&wpiu=any&doit='
        )
        if depth > 0:
            petscan_url += f'&depth={depth}'
        return petscan_url

    def open_petscan_in_browser(self) -> None:
        category = self.cat_edit.text().strip()
        if not category:
            QMessageBox.warning(
                self,
                self._t('ui.error', 'Error'),
                self._t('ui.enter_category_name', 'Enter a category name.'),
            )
            return

        lang = self.get_current_language()
        fam = self.get_current_family()
        depth = self.depth_spin.value()
        petscan_url = self._build_petscan_url(fam, lang, category, depth)
        debug(f'Open Petscan URL: {petscan_url}')
        webbrowser.open_new_tab(petscan_url)

    def open_petscan(self) -> None:
        self.fetch_titles(mode='categories_only', append=False)

    def fetch_category_pages(self) -> None:
        self.fetch_titles(mode='non_categories_only', append=False)

    def fetch_selected_mode_replace(self) -> None:
        self.fetch_titles(append=False)

    def fetch_selected_mode_append(self) -> None:
        self.fetch_titles(append=True)

    def _get_selected_fetch_mode(self) -> str:
        try:
            mode = str(self.fetch_mode_combo.currentData() or '').strip()
        except Exception:
            mode = ''
        if mode in {'categories_only', 'non_categories_only', 'both'}:
            return mode
        return 'non_categories_only'

    def _get_fetch_mode_label(self, mode: str) -> str:
        labels = {
            'categories_only': self._t(
                'ui.source.fetch_mode.categories_only', 'Categories only'
            ),
            'non_categories_only': self._t(
                'ui.source.fetch_mode.non_categories_only_alt', 'Except categories',
            ),
            'both': self._t('ui.source.fetch_mode.both_alt', 'All pages'),
        }
        return labels.get(mode, labels['non_categories_only'])

    def _set_fetch_controls_enabled(self, enabled: bool) -> None:
        for control_name in (
            'replace_list_btn',
            'append_list_btn',
            'fetch_mode_combo',
            'open_petscan_btn',
            'cat_edit',
            'depth_spin',
            'depth_plus_btn',
            'depth_minus_btn',
        ):
            try:
                getattr(self, control_name).setEnabled(bool(enabled))
            except Exception:
                pass

    def _replace_or_append_titles(self, titles: list[str], *, append: bool) -> None:
        new_text = '\n'.join(titles)
        if not append:
            self.manual_list.setPlainText(new_text)
            return

        current_text = self.manual_list.toPlainText()
        if not current_text.strip():
            self.manual_list.setPlainText(new_text)
            return
        if not new_text.strip():
            return

        separator = '\n' if not current_text.endswith('\n') else ''
        self.manual_list.setPlainText(current_text + separator + new_text)

    def _resolve_category_title(self, fam: str, lang: str, category: str) -> str:
        from ...core.namespace_manager import has_prefix_by_policy, get_policy_prefix
        if has_prefix_by_policy(fam, lang, category, {14}):
            return category
        cat_prefix = get_policy_prefix(fam, lang, 14, 'Category:')
        return cat_prefix + category

    def fetch_titles(self, *, mode: str | None = None, append: bool = False) -> None:
        worker = getattr(self, '_fetch_worker', None)
        try:
            if worker is not None and worker.isRunning():
                self._log(
                    self._t(
                        'ui.source.fetch_busy',
                        'Category reading is already running. Please wait for the current request to finish.',
                    )
                )
                return
        except Exception:
            pass

        category = self.cat_edit.text().strip()
        if not category:
            QMessageBox.warning(
                self,
                self._t('ui.error', 'Error'),
                self._t('ui.enter_category_name', 'Enter a category name.'),
            )
            return

        lang = self.get_current_language()
        fam = self.get_current_family()
        selected_mode = mode or self._get_selected_fetch_mode()

        try:
            cat_full = self._resolve_category_title(fam, lang, category)
        except Exception:
            cat_full = category

        depth = self.depth_spin.value()
        self._set_fetch_controls_enabled(False)
        self._log(
            self._t(
                'ui.source.fetch_started_background',
                'Reading category "{category}" in background (mode: {mode}, depth: {depth})...',
            ).format(
                category=cat_full,
                mode=self._get_fetch_mode_label(selected_mode),
                depth=depth,
            )
        )

        worker = CategoryFetchWorker(
            category=cat_full,
            lang=lang,
            family=fam,
            depth=depth,
            mode=selected_mode,
        )
        worker.progress.connect(self._log)
        worker.result_ready.connect(
            lambda titles, stats, _append=append, _mode=selected_mode, _depth=depth: self._on_fetch_titles_ready(
                titles,
                stats,
                append=_append,
                mode=_mode,
                depth=_depth,
            )
        )
        worker.failed.connect(self._on_fetch_titles_failed)
        worker.finished.connect(self._on_fetch_titles_finished)
        self._fetch_worker = worker
        worker.start()

    def _on_fetch_titles_ready(
        self,
        titles: list[str],
        stats: dict[str, int],
        *,
        append: bool,
        mode: str,
        depth: int,
    ) -> None:
        if titles:
            self._replace_or_append_titles(list(titles), append=append)
            self._log(
                self._t(
                    'ui.source.fetch_result_appended' if append else 'ui.source.fetch_result_replaced',
                    'List appended: {count} (mode: {mode}, categories: {categories}, others: {non_categories}, depth: {depth})'
                    if append else
                    'List replaced: {count} (mode: {mode}, categories: {categories}, others: {non_categories}, depth: {depth})',
                ).format(
                    count=len(titles),
                    mode=self._get_fetch_mode_label(mode),
                    categories=int((stats or {}).get('categories', 0)),
                    non_categories=int((stats or {}).get('non_categories', 0)),
                    depth=depth,
                )
            )
        else:
            self._log(
                self._t(
                    'ui.source.fetch_result_empty',
                    'Nothing was found for the selected mode.',
                )
            )

    def _on_fetch_titles_failed(self, message: str) -> None:
        text = str(message or '').strip()
        if text:
            self._log(text)
            return
        self._log(self._t('ui.source.api_error', 'API error: {error}'))

    def _on_fetch_titles_finished(self) -> None:
        self._set_fetch_controls_enabled(True)
        worker = getattr(self, '_fetch_worker', None)
        self._fetch_worker = None
        try:
            if worker is not None:
                worker.deleteLater()
        except Exception:
            pass

    def _fetch_titles_for_mode(
        self,
        api_client,
        *,
        category: str,
        lang: str,
        fam: str,
        depth: int,
        mode: str,
    ) -> tuple[list[str], dict[str, int]]:
        categories_only: list[str] = []
        non_categories_only: list[str] = []

        if mode in {'categories_only', 'both'}:
            categories_only = sorted(
                set(
                    self._fetch_subcats_recursive(
                        api_client, category, lang, fam, depth, 0, set()
                    )
                ),
                key=lambda value: value.casefold(),
            )

        if mode in {'non_categories_only', 'both'}:
            categories_for_pages = [category]
            if depth > 0:
                categories_for_pages.extend(
                    self._fetch_subcats_recursive(
                        api_client, category, lang, fam, depth - 1, 0, set()
                    )
                )
            categories_for_pages = list(dict.fromkeys(categories_for_pages))

            page_titles: list[str] = []
            for current_category in categories_for_pages:
                page_titles.extend(
                    self._fetch_pages_for_category(
                        api_client, current_category, lang, fam
                    )
                )
            non_categories_only = sorted(
                set(page_titles),
                key=lambda value: value.casefold(),
            )

        combined_titles = list(categories_only)
        existing_keys = {value.casefold() for value in combined_titles}
        for title in non_categories_only:
            title_key = title.casefold()
            if title_key in existing_keys:
                continue
            combined_titles.append(title)
            existing_keys.add(title_key)

        return combined_titles, {
            'categories': len(categories_only),
            'non_categories': len(non_categories_only),
        }

    def _fetch_pages_for_category(self, api_client, category: str, lang: str, fam: str) -> list[str]:
        api_url = api_client._build_api_url(fam, lang)
        params = {
            'action': 'query',
            'list': 'categorymembers',
            'cmtitle': category,
            'cmtype': 'page',
            'cmlimit': 'max',
            'format': 'json',
        }

        titles: list[str] = []
        while True:
            _rate_wait()
            response = REQUEST_SESSION.get(
                api_url, params=params, timeout=10, headers=REQUEST_HEADERS)
            if response.status_code != 200:
                self._log(
                    self._t(
                        'ui.source.http_error_pages',
                        'HTTP {status} while fetching pages for {category}'  ).format(status=response.status_code, category=category)
                )
                break

            try:
                payload = response.json()
            except Exception:
                self._log(
                    self._t(
                        'ui.source.json_parse_error',
                        'Failed to parse JSON for {category}').format(category=category)
                )
                break

            batch = [
                member.get('title', '')
                for member in payload.get('query', {}).get('categorymembers', [])
            ]
            titles.extend([title for title in batch if title])

            if 'continue' in payload:
                params.update(payload['continue'])
            else:
                break

        return titles

    def _fetch_subcats_recursive(
        self,
        api_client,
        category: str,
        lang: str,
        fam: str,
        max_depth: int,
        current_depth: int,
        visited: set[str],
    ) -> list[str]:
        if current_depth > max_depth:
            return []

        category_key = category.casefold()
        if category_key in visited:
            return []
        visited.add(category_key)

        api_url = api_client._build_api_url(fam, lang)
        params = {
            'action': 'query',
            'list': 'categorymembers',
            'cmtitle': category,
            'cmtype': 'subcat',
            'cmlimit': 'max',
            'format': 'json',
        }

        direct_subcats: list[str] = []
        try:
            while True:
                _rate_wait()
                response = REQUEST_SESSION.get(
                    api_url, params=params, timeout=10, headers=REQUEST_HEADERS)
                if response.status_code != 200:
                    debug(
                        self._t(
                            'ui.source.http_error_subcategories',
                            'HTTP {status} while fetching subcategories for {category}' ).format(status=response.status_code, category=category)
                    )
                    break
                try:
                    payload = response.json()
                except Exception:
                    debug(
                        self._t(
                            'ui.source.json_parse_error',
                            'Failed to parse JSON for {category}'    ).format(category=category)
                    )
                    break

                batch = [
                    member['title']
                    for member in payload.get('query', {}).get('categorymembers', [])
                ]
                direct_subcats.extend(batch)

                if 'continue' in payload:
                    params.update(payload['continue'])
                else:
                    break

            debug(
                self._t(
                    'ui.source.depth_subcategories_debug',
                    'Depth {depth}: {category} -> {count} subcategories'
                ).format(
                    depth=current_depth,
                    category=category,
                    count=len(direct_subcats),
                )
            )
        except Exception as exc:
            debug(
                self._t(
                    'ui.source.fetch_subcategories_error',
                    'Failed to fetch subcategories for {category}: {error}'
                ).format(category=category, error=exc)
            )
            return []

        if current_depth >= max_depth:
            return direct_subcats

        all_subcats = list(direct_subcats)
        for subcat in direct_subcats:
            all_subcats.extend(self._fetch_subcats_recursive(
                api_client, subcat, lang, fam, max_depth, current_depth + 1, visited))
        return all_subcats


class TsvPreviewPanel(QWidget):
    """Двухколоночный предпросмотр TSV для переиспользования во вкладках."""

    def __init__(
        self,
        parent=None,
        header_text: str = '',
        left_header: str = '',
        right_header: str = '',
        left_stretch: int = 1,
        right_stretch: int = 2,
    ):
        super().__init__(parent)
        self._setup_ui(
            header_text=header_text or translate_key('ui.preview_header', getattr(parent, '_ui_lang', 'ru') if parent is not None else 'ru', '<b>Preview</b>'),
            left_header=left_header,
            right_header=right_header,
            left_stretch=left_stretch,
            right_stretch=right_stretch,
        )

    def _setup_ui(
        self,
        *,
        header_text: str,
        left_header: str,
        right_header: str,
        left_stretch: int,
        right_stretch: int,
    ) -> None:
        layout = QVBoxLayout(self)
        try:
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
        except Exception:
            pass

        body_layout = layout
        if header_text:
            title = re.sub(r'<[^>]+>', '', str(header_text or '')).strip()
            preview_group = QGroupBox(title or translate_key('ui.preview', getattr(self.parent(), '_ui_lang', 'ru') if self.parent() is not None else 'ru', 'Preview'))
            preview_group.setObjectName('previewGroup')
            preview_layout = QVBoxLayout(preview_group)
            try:
                preview_layout.setContentsMargins(6, 8, 6, 6)
                preview_layout.setSpacing(2)
            except Exception:
                pass
            layout.addWidget(preview_group, 1)
            body_layout = preview_layout
        self._body_layout = body_layout

        self.titles_edit = QTextEdit()
        self.titles_edit.setObjectName('tsvPreviewLeft')
        self.titles_edit.setReadOnly(True)
        self._configure_preview_edit(self.titles_edit)

        self.content_edit = QTextEdit()
        self.content_edit.setObjectName('tsvPreviewRight')
        self.content_edit.setReadOnly(True)
        self._configure_preview_edit(self.content_edit)

        left_pane = QWidget()
        left_layout = QVBoxLayout(left_pane)
        try:
            left_layout.setContentsMargins(0, 0, 0, 0)
            left_layout.setSpacing(2)
        except Exception:
            pass
        self.left_header_label = QLabel(left_header or '')
        try:
            self.left_header_label.setAlignment(Qt.AlignLeft | Qt.AlignBottom)
        except Exception:
            pass
        self.left_header_label.setVisible(bool(left_header))
        left_layout.addWidget(self.left_header_label)
        left_layout.addWidget(self.titles_edit, 1)

        right_pane = QWidget()
        right_layout = QVBoxLayout(right_pane)
        try:
            right_layout.setContentsMargins(0, 0, 0, 0)
            right_layout.setSpacing(2)
        except Exception:
            pass
        self.right_header_label = QLabel(right_header or '')
        try:
            self.right_header_label.setAlignment(Qt.AlignLeft | Qt.AlignBottom)
        except Exception:
            pass
        self.right_header_label.setVisible(bool(right_header))
        right_layout.addWidget(self.right_header_label)
        right_layout.addWidget(self.content_edit, 1)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setObjectName('tsvPreviewSplitter')
        try:
            self.splitter.setChildrenCollapsible(False)
            self.splitter.setHandleWidth(2)
        except Exception:
            pass
        self.splitter.addWidget(left_pane)
        self.splitter.addWidget(right_pane)
        self.splitter.setStretchFactor(0, left_stretch)
        self.splitter.setStretchFactor(1, right_stretch)
        body_layout.addWidget(self.splitter, 1)

        try:
            bar_left = self.titles_edit.verticalScrollBar()
            bar_right = self.content_edit.verticalScrollBar()
            bar_left.valueChanged.connect(
                lambda value: bar_right.setValue(value) if bar_right.value() != value else None)
            bar_right.valueChanged.connect(
                lambda value: bar_left.setValue(value) if bar_left.value() != value else None)
        except Exception:
            pass

    @staticmethod
    def _configure_preview_edit(edit: QTextEdit) -> None:
        try:
            edit.setLineWrapMode(QTextEdit.NoWrap)
            edit.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        except Exception:
            pass

    def set_preview(self, left: list[str], right: list[str]) -> None:
        self.titles_edit.setPlainText('\n'.join(left))
        self.content_edit.setPlainText('\n'.join(right))

    def clear(self) -> None:
        self.titles_edit.clear()
        self.content_edit.clear()

    def add_top_layout(self, extra_layout: QVBoxLayout | QHBoxLayout) -> None:
        try:
            self._body_layout.insertLayout(0, extra_layout)
        except Exception:
            pass

    def add_top_widget(self, widget: QWidget) -> None:
        try:
            self._body_layout.insertWidget(0, widget)
        except Exception:
            pass
