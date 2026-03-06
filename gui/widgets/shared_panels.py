# -*- coding: utf-8 -*-
"""
Переиспользуемые панели интерфейса для вкладок.
"""

import urllib.parse
import webbrowser

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QPushButton, QToolButton, QTextEdit, QMessageBox, QSpinBox,
    QGridLayout, QSizePolicy, QGroupBox, QSplitter
)

from ...constants import PREFIX_TOOLTIP, REQUEST_HEADERS
from ...core.api_client import WikimediaAPIClient, REQUEST_SESSION, _rate_wait
from ...utils import debug, format_russian_subcategories_nominative
from .ui_helpers import add_info_button, pick_file, open_from_edit, log_message


_GROUP_STYLE = (
    "QGroupBox { border: 1px solid lightgray; border-radius: 5px; margin-top: 10px; } "
    "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px 0 5px; }"
)


class CategorySourcePanel(QGroupBox):
    """Универсальная левая панель источника страниц."""

    def __init__(
        self,
        parent=None,
        parent_window=None,
        log_widget: QTextEdit | None = None,
        help_text: str = '',
        group_title: str = 'Источник',
        category_section_label: str = '<b>Получить содержимое категории:</b>',
        category_placeholder: str = 'Название корневой категории',
        manual_label: str = '<b>Список страниц:</b>',
        manual_placeholder: str = 'Список страниц (по одной на строку)',
        file_section_label: str = '<b>Или загрузить из файла:</b>',
        file_caption: str = 'Файл (.txt):',
        default_input_path: str = '',
    ):
        super().__init__(group_title, parent)
        self.parent_window = parent_window or parent
        self.log_widget = log_widget
        self.help_text = help_text
        self.setStyleSheet(_GROUP_STYLE)
        self._setup_ui(
            category_section_label=category_section_label,
            category_placeholder=category_placeholder,
            manual_label=manual_label,
            manual_placeholder=manual_placeholder,
            file_section_label=file_section_label,
            file_caption=file_caption,
            default_input_path=default_input_path,
        )

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
        prefix_label = QLabel('Префиксы:')
        prefix_label.setToolTip(PREFIX_TOOLTIP)
        prefix_layout.addWidget(prefix_label)

        self.ns_combo = QComboBox()
        self.ns_combo.setEditable(False)
        prefix_layout.addWidget(self.ns_combo)
        prefix_layout.addStretch(1)
        add_info_button(self, prefix_layout, self.help_text, inline=True)
        layout.addLayout(prefix_layout)

        layout.addSpacing(6)
        layout.addWidget(QLabel(category_section_label))

        self.cat_edit = QLineEdit()
        self.cat_edit.setPlaceholderText(category_placeholder)

        category_fetch_layout = QGridLayout()
        try:
            category_fetch_layout.setContentsMargins(0, 0, 0, 0)
            category_fetch_layout.setHorizontalSpacing(8)
            category_fetch_layout.setVerticalSpacing(6)
            category_fetch_layout.setColumnStretch(0, 1)
        except Exception:
            pass
        category_fetch_layout.addWidget(self.cat_edit, 0, 0)

        self.get_pages_btn = QPushButton('Получить страницы')
        self.get_pages_btn.setToolTip(
            'Получить страницы категории через API с учётом глубины')
        self.get_pages_btn.clicked.connect(self.fetch_category_pages)

        self.petscan_btn = QPushButton('Получить подкатегории')
        self.petscan_btn.setToolTip(
            'Получить подкатегории через API с учётом глубины')
        self.petscan_btn.clicked.connect(self.open_petscan)

        self.open_petscan_btn = QPushButton('PetScan')
        self.open_petscan_btn.setToolTip(
            'Открыть PetScan с расширенными настройками для указанной категории')
        self.open_petscan_btn.clicked.connect(self.open_petscan_in_browser)

        depth_label = QLabel('Глубина:')
        depth_label.setToolTip(
            'Глубина рекурсивного обхода категории. '
            'Для «Получить страницы»: 0 = только корневая категория, 1 = + прямые подкатегории. '
            'Для «Получить подкатегории»: 0 = только прямые подкатегории.'
        )
        self.depth_spin = QSpinBox()
        self.depth_spin.setMinimum(0)
        self.depth_spin.setMaximum(10)
        self.depth_spin.setValue(0)
        self.depth_spin.setToolTip(
            'Глубина рекурсивного обхода категории для страниц и подкатегорий')
        try:
            self.depth_spin.setFixedWidth(60)
        except Exception:
            pass

        category_fetch_layout.addWidget(depth_label, 0, 1)
        category_fetch_layout.addWidget(self.depth_spin, 0, 2)

        buttons_layout = QHBoxLayout()
        try:
            buttons_layout.setContentsMargins(0, 0, 0, 0)
            buttons_layout.setSpacing(8)
        except Exception:
            pass
        for button in (
            self.get_pages_btn,
            self.petscan_btn,
            self.open_petscan_btn,
        ):
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            buttons_layout.addWidget(button)
        category_fetch_layout.addLayout(buttons_layout, 1, 0, 1, 3)
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
            btn_browse_in.setFixedSize(28, 28)
            btn_browse_in.setCursor(Qt.PointingHandCursor)
            btn_browse_in.setToolTip('Выбрать файл')
        except Exception:
            pass
        btn_browse_in.clicked.connect(
            lambda: pick_file(self, self.in_path, '*.txt'))
        file_layout.addWidget(btn_browse_in)

        btn_open_in = QPushButton('Открыть')
        btn_open_in.clicked.connect(lambda: open_from_edit(self, self.in_path))
        file_layout.addWidget(btn_open_in)

        layout.addLayout(file_layout)
        self.file_edit = self.in_path

    def set_log_widget(self, log_widget: QTextEdit | None) -> None:
        self.log_widget = log_widget

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
            QMessageBox.warning(self, 'Ошибка', 'Введите название категории.')
            return

        lang = self.get_current_language()
        fam = self.get_current_family()
        depth = self.depth_spin.value()
        petscan_url = self._build_petscan_url(fam, lang, category, depth)
        debug(f'Open Petscan URL: {petscan_url}')
        webbrowser.open_new_tab(petscan_url)

    def open_petscan(self) -> None:
        category = self.cat_edit.text().strip()
        if not category:
            QMessageBox.warning(self, 'Ошибка', 'Введите название категории.')
            return

        lang = self.get_current_language()
        fam = self.get_current_family()

        from ...core.namespace_manager import has_prefix_by_policy, get_policy_prefix
        if has_prefix_by_policy(fam, lang, category, {14}):
            cat_full = category
        else:
            cat_prefix = get_policy_prefix(fam, lang, 14, 'Category:')
            cat_full = cat_prefix + category

        depth = self.depth_spin.value()
        api_client = WikimediaAPIClient()
        try:
            subcats = self._fetch_subcats_recursive(
                api_client, cat_full, lang, fam, depth, 0, set())
            if subcats:
                subcats_sorted = sorted(subcats, key=lambda value: value.casefold())
                self.manual_list.setPlainText('\n'.join(subcats_sorted))
                self._log(
                    f'Получено {format_russian_subcategories_nominative(len(subcats_sorted))} '
                    f'(глубина: {depth}, отсортировано)'
                )
            else:
                self._log('Подкатегории не найдены.')
        except Exception as exc:
            self._log(f'Ошибка API: {exc}')
        self.petscan_btn.setEnabled(True)

    def fetch_category_pages(self) -> None:
        category = self.cat_edit.text().strip()
        if not category:
            QMessageBox.warning(self, 'Ошибка', 'Введите название категории.')
            return

        lang = self.get_current_language()
        fam = self.get_current_family()

        try:
            self.get_pages_btn.setEnabled(False)
        except Exception:
            pass

        from ...core.namespace_manager import has_prefix_by_policy, get_policy_prefix
        if has_prefix_by_policy(fam, lang, category, {14}):
            cat_full = category
        else:
            cat_prefix = get_policy_prefix(fam, lang, 14, 'Category:')
            cat_full = cat_prefix + category

        depth = self.depth_spin.value()
        api_client = WikimediaAPIClient()

        try:
            categories = [cat_full]
            if depth > 0:
                categories.extend(self._fetch_subcats_recursive(
                    api_client, cat_full, lang, fam, depth - 1, 0, set()))
            categories = list(dict.fromkeys(categories))

            titles: list[str] = []
            for current_category in categories:
                titles.extend(self._fetch_pages_for_category(
                    api_client, current_category, lang, fam))

            if titles:
                titles_sorted = sorted(set(titles), key=lambda value: value.casefold())
                self.manual_list.setPlainText('\n'.join(titles_sorted))
                self._log(
                    f'Получено страниц: {len(titles_sorted)} '
                    f'(глубина: {depth}, отсортировано)'
                )
            else:
                self._log('Страницы в категории не найдены.')
        except Exception as exc:
            self._log(f'Ошибка API: {exc}')
        finally:
            try:
                self.get_pages_btn.setEnabled(True)
            except Exception:
                pass

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
                    f'HTTP {response.status_code} при запросе страниц для {category}')
                break

            try:
                payload = response.json()
            except Exception:
                self._log(f'Не удалось распарсить JSON для {category}')
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
                        f'HTTP {response.status_code} при запросе подкатегорий для {category}')
                    break
                try:
                    payload = response.json()
                except Exception:
                    debug(f'Не удалось распарсить JSON для {category}')
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
                f'Глубина {current_depth}: {category} -> {len(direct_subcats)} подкатегорий')
        except Exception as exc:
            debug(f'Ошибка получения подкатегорий для {category}: {exc}')
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
        header_text: str = '<b>Предпросмотр:</b>',
        left_header: str = '',
        right_header: str = '',
        left_stretch: int = 1,
        right_stretch: int = 2,
    ):
        super().__init__(parent)
        self._setup_ui(
            header_text=header_text,
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

        if header_text:
            layout.addWidget(QLabel(header_text))

        self.titles_edit = QTextEdit()
        self.titles_edit.setReadOnly(True)
        self._configure_preview_edit(self.titles_edit)

        self.content_edit = QTextEdit()
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
        self.splitter.addWidget(left_pane)
        self.splitter.addWidget(right_pane)
        self.splitter.setStretchFactor(0, left_stretch)
        self.splitter.setStretchFactor(1, right_stretch)
        layout.addWidget(self.splitter, 1)

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
