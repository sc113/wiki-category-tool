"""
UI Helper functions for Wiki Category Tool GUI components.

This module contains utility functions for creating and managing UI elements,
file operations, message formatting, and window management.
"""

import os
import csv
import sys
import html
import re
import ctypes
from datetime import datetime
from PySide6.QtCore import Qt, QUrl, QTimer
from PySide6.QtWidgets import (
    QLineEdit, QPushButton, QTextEdit, QToolButton, QHBoxLayout,
    QFileDialog, QMessageBox, QDialog, QVBoxLayout, QTextBrowser,
    QTreeWidget, QTreeWidgetItem, QLabel, QHeaderView, QAbstractItemView,
    QWidget, QGridLayout
)
from PySide6.QtGui import QKeySequence, QGuiApplication, QShortcut
from PySide6.QtGui import QAction
from PySide6.QtGui import QDesktopServices
from ...core.localization import translate_key


# Структурированный приём событий из воркера
def log_tree_add_event(tree: QTreeWidget, event: dict) -> None:
    try:
        if not isinstance(event, dict):
            return
        ts = datetime.now().strftime('%H:%M:%S')
        et = (event.get('type') or '').strip()
        status = (event.get('status') or 'info').strip().lower()
        # Нормализуем статус к известным ключам
        if status not in ('success', 'skipped', 'error', 'not_found', 'info'):
            status = 'info'
        if et == 'category_move_start':
            old_cat = html.unescape((event.get('old_category') or '').strip())
            new_cat = html.unescape((event.get('new_category') or '').strip())
            cnt_str = (event.get('count_str') or str(
                event.get('count') or '')).strip()
            title_txt = _fmt(
                tree,
                'ui.log.category_move_start',
                'Category content transfer {old} → {new}',
                old=old_cat,
                new=new_cat,
            )
            log_tree_add(tree, ts, old_cat, title_txt, 'manual',
                         status or 'info', cnt_str or None, 'category', True)
            return
        if et == 'destination_exists':
            dst = html.unescape((event.get('title') or '').strip())
            # Определяем тип объекта для правильного отображения
            obj_type = _detect_object_type_by_ns(tree, dst)
            # В колонку «Страница» помещаем название (независимо от типа)
            title_txt = _fmt(
                tree,
                'ui.log.destination_page_exists',
                'Destination page {title} already exists.',
                title=dst,
            )
            log_tree_add(tree, ts, dst, title_txt, 'manual',
                         status or 'info', None, obj_type, True)
            return
        if et == 'redirect_retained':
            old_title = html.unescape((event.get('old_title') or '').strip())
            new_title = html.unescape((event.get('new_title') or '').strip())
            page_title = old_title or new_title
            obj_type = _detect_object_type_by_ns(tree, page_title)
            title_txt = (
                _fmt(
                    tree,
                    'ui.log.redirect_retained',
                    'Renamed, but redirect remained: {old} → {new} (possibly insufficient suppressredirect rights).',
                    old=old_title,
                    new=new_title,
                )
            )
            log_tree_add(tree, ts, page_title, title_txt, 'manual',
                         status or 'info', None, obj_type, True)
            return
    except Exception:
        pass


# Последняя пара переименования для системных сообщений (начало/успех)
_LAST_RENAME_OLD: str | None = None
_LAST_RENAME_NEW: str | None = None


def _apply_windows_dialog_titlebar_theme(widget: QWidget, dark: bool) -> None:
    """Задает цвет системного titlebar у диалога на Windows."""
    try:
        if not sys.platform.startswith('win') or widget is None:
            return
        hwnd = int(widget.winId())
        value = ctypes.c_int(1 if dark else 0)
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

        def _rgb(r: int, g: int, b: int) -> int:
            return (b << 16) | (g << 8) | r

        caption = ctypes.c_uint(_rgb(15, 27, 38) if dark else _rgb(245, 248, 252))
        text = ctypes.c_uint(_rgb(238, 246, 252) if dark else _rgb(34, 49, 61))
        border = ctypes.c_uint(_rgb(15, 27, 38) if dark else _rgb(210, 225, 237))
        for attr, val in ((35, caption), (36, text), (34, border)):
            try:
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


def _ui_translate(widget, text: str) -> str:
    raw_text = text or ''
    try:
        host = widget.window() if widget is not None and hasattr(widget, 'window') else None
    except Exception:
        host = None
    try:
        if host is not None and hasattr(host, 'translate_ui_text'):
            return host.translate_ui_text(raw_text)
    except Exception:
        pass
    return raw_text


def _ui_lang(widget) -> str:
    try:
        host = widget.window() if widget is not None and hasattr(widget, 'window') else None
        lang = str(getattr(host, '_ui_lang', 'ru')).lower() if host is not None else 'ru'
        return 'en' if lang.startswith('en') else 'ru'
    except Exception:
        return 'ru'


def _t(widget, key: str, default: str = '') -> str:
    try:
        return translate_key(key, _ui_lang(widget), default)
    except Exception:
        return default


def _fmt(widget, key: str, default: str = '', **kwargs) -> str:
    text = _t(widget, key, default)
    try:
        return text.format(**kwargs)
    except Exception:
        return text


def _locale_tokens(key: str, *defaults: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for lang in ('ru', 'en'):
        try:
            raw = translate_key(key, lang, '')
        except Exception:
            raw = ''
        if raw:
            parts = [part.strip().lower() for part in raw.split('|') if part.strip()]
            for part in parts:
                if part not in tokens:
                    tokens.append(part)
    for value in defaults:
        text = str(value or '').strip().lower()
        if text and text not in tokens:
            tokens.append(text)
    return tuple(tokens)


def _locale_pattern(key: str, *defaults: str) -> str:
    tokens = sorted(_locale_tokens(key, *defaults), key=len, reverse=True)
    return '|'.join(re.escape(token) for token in tokens if token)


def _has_locale_token(text: str, key: str, *defaults: str) -> bool:
    low = (text or '').lower()
    return any(token in low for token in _locale_tokens(key, *defaults))


def _status_meta(widget=None) -> dict[str, dict[str, str]]:
    return {
        'success': {'emoji': '✅', 'color': '#4f83d1', 'label': _t(widget, 'ui.success', 'Success')},
        'skipped': {'emoji': '⏭️', 'color': '#6b7280', 'label': _t(widget, 'ui.skipped', 'Skipped')},
        'error': {'emoji': '❌', 'color': '#ef4444', 'label': _t(widget, 'ui.error', 'Error')},
        'not_found': {'emoji': '⚠️', 'color': '#f97316', 'label': _t(widget, 'ui.not_found', 'Not found')},
        'info': {'emoji': 'ℹ️', 'color': '#3b82f6', 'label': _t(widget, 'ui.info', 'Info')},
    }


def _mode_meta(widget=None) -> dict[str, dict[str, str]]:
    return {
        'auto': {'emoji': '⚡', 'label': _t(widget, 'ui.log.mode.auto', 'Auto-approved template parameter replacement')},
        'manual': {'emoji': '✍️', 'label': _t(widget, 'ui.log.mode.manual', 'Manual template parameter replacement')},
        'direct': {'emoji': '📝', 'label': _t(widget, 'ui.log.mode.direct', 'Direct category link replacement on page')},
    }


def _obj_meta(widget=None) -> dict[str, dict[str, str]]:
    return {
        'article': {'emoji': '📄', 'label': _t(widget, 'ui.log.object.article', 'Article')},
        'template': {'emoji': '⚛️', 'label': _t(widget, 'ui.log.object.template', 'Template')},
        'file': {'emoji': '🖼️', 'label': _t(widget, 'ui.log.object.file', 'File')},
        'category': {'emoji': '📁', 'label': _t(widget, 'ui.log.object.category', 'Category')},
    }


def _context_action_key(raw_text: str) -> str | None:
    try:
        base = (raw_text or '').replace('&', '')
        base = base.split('\t', 1)[0].strip().lower()
        aliases = {
            'undo': set(_locale_tokens('ui.context.alias.undo', 'undo')),
            'redo': set(_locale_tokens('ui.context.alias.redo', 'redo')),
            'cut': set(_locale_tokens('ui.context.alias.cut', 'cut')),
            'copy': set(_locale_tokens('ui.context.alias.copy', 'copy')),
            'paste': set(_locale_tokens('ui.context.alias.paste', 'paste')),
            'delete': set(_locale_tokens('ui.context.alias.delete', 'delete')),
            'select_all': set(_locale_tokens('ui.context.alias.select_all', 'select all')),
        }
        for key, names in aliases.items():
            if base in names:
                return key
    except Exception:
        pass
    return None


def _localize_context_menu_actions(menu, lang: str):
    labels = {
        'undo': translate_key('ui.context.undo', lang, 'Undo'),
        'redo': translate_key('ui.context.redo', lang, 'Redo'),
        'cut': translate_key('ui.context.cut', lang, 'Cut'),
        'copy': translate_key('ui.context.copy', lang, 'Copy'),
        'paste': translate_key('ui.context.paste', lang, 'Paste'),
        'delete': translate_key('ui.context.delete', lang, 'Delete'),
        'select_all': translate_key('ui.context.select_all', lang, 'Select All'),
    }
    try:
        for act in menu.actions():
            key = _context_action_key(act.text())
            if key and key in labels:
                act.setText(labels[key])
    except Exception:
        pass


def install_localized_context_menu(widget):
    """Устанавливает локализованное контекстное меню для text-edit виджетов."""
    try:
        if widget is None or not hasattr(widget, 'createStandardContextMenu'):
            return
        if bool(widget.property('_wct_ctx_localized')):
            return
        widget.setContextMenuPolicy(Qt.CustomContextMenu)

        def _show_context_menu(pos):
            try:
                menu = widget.createStandardContextMenu()
                _localize_context_menu_actions(menu, _ui_lang(widget))
                menu.exec(widget.mapToGlobal(pos))
            except Exception:
                pass

        widget.customContextMenuRequested.connect(_show_context_menu)
        widget.setProperty('_wct_ctx_localized', True)
    except Exception:
        pass


def show_help_dialog(parent_widget, text: str, title: str = ''):
    """Показывает справку в выделяемом текстовом блоке (как у кнопок `?`)."""
    raw = text or ''
    try:
        dlg = QDialog(parent_widget)
        dlg.setWindowTitle(_ui_translate(parent_widget, title or _t(parent_widget, 'ui.help', 'Help')))
        lay = QVBoxLayout(dlg)
        try:
            lay.setContentsMargins(8, 8, 8, 8)
            lay.setSpacing(4)
        except Exception:
            pass
        view = QTextBrowser()
        try:
            view.setOpenExternalLinks(False)
            view.setOpenLinks(False)
            view.setReadOnly(True)
            view.setTextInteractionFlags(
                Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
            )
            view.setStyleSheet(
                'QTextBrowser{padding:0;margin:0;background:transparent;border:0;}')
            view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        except Exception:
            pass

        def _build_html(s: str) -> str:
            try:
                html_text = html.escape(s).replace('\n', '<br/>')
                return f"<div style='font-size:12px; line-height:1.15'>{html_text}</div>"
            except Exception:
                return html.escape(s).replace('\n', '<br/>')

        view.setHtml(_build_html(_ui_translate(parent_widget, raw)))
        lay.addWidget(view)

        from PySide6.QtWidgets import QDialogButtonBox
        btns = QDialogButtonBox(QDialogButtonBox.Ok)
        try:
            btns.accepted.connect(dlg.accept)
        except Exception:
            pass
        lay.addWidget(btns)

        try:
            base_width = 640
            margin_left = margin_right = 8
            margin_top = margin_bottom = 8
            spacing = 4
            doc = view.document()
            try:
                inner_width = base_width - (margin_left + margin_right)
                doc.setTextWidth(inner_width)
            except Exception:
                pass
            try:
                sizef = doc.documentLayout().documentSize()
                content_h = int(sizef.height()) + 2
            except Exception:
                content_h = 320
            try:
                screen = QGuiApplication.primaryScreen()
                avail_h = screen.availableGeometry().height() if screen else 900
            except Exception:
                avail_h = 900
            btn_h = btns.sizeHint().height()
            total_needed = margin_top + content_h + spacing + btn_h + margin_bottom
            height = min(max(120, total_needed), int(avail_h * 0.9))
            visible_view_h = height - (margin_top + spacing + btn_h + margin_bottom)
            view.setFixedHeight(max(60, visible_view_h))
            dlg.resize(base_width, height)
        except Exception:
            dlg.resize(640, 360)

        try:
            host = parent_widget.window() if parent_widget is not None else None
            theme_mode = str(getattr(host, '_theme_mode', '')).lower()
            dark_title = theme_mode in ('teal', 'dark')
            _apply_windows_dialog_titlebar_theme(dlg, dark_title)
        except Exception:
            pass
        dlg.exec()
    except Exception:
        try:
            QMessageBox.information(
                parent_widget,
                _ui_translate(parent_widget, title or _t(parent_widget, 'ui.help', 'Help')),
                _ui_translate(parent_widget, raw),
            )
        except Exception:
            pass


def add_info_button(parent_widget, host_layout, text: str, inline: bool = False):
    """Insert an ℹ button.

    When inline=True and host_layout is QHBoxLayout, the button is placed
    immediately after the previous widget. Otherwise, it is aligned to the
    right edge of the host layout.

    Clicking the button shows *text* inside a modal information dialog.

    Args:
        parent_widget: Parent widget for the button (needed for message box)
        host_layout: Layout to add the button to
        text: Text to show in the information dialog
        inline: Whether to place button inline or at the right edge

    Returns:
        QToolButton: The created info button
    """
    btn = QToolButton()
    btn.setObjectName('infoButton')
    btn.setText('?')
    btn.setAutoRaise(True)
    btn.setToolTip(_ui_translate(parent_widget, _t(parent_widget, 'ui.help', 'Help')))
    try:
        btn.setFixedSize(23, 23)
    except Exception:
        pass

    btn.clicked.connect(
        lambda _=None, t=text: show_help_dialog(parent_widget, t, _t(parent_widget, 'ui.help', 'Help'))
    )

    if isinstance(host_layout, QHBoxLayout):
        if inline:
            # Добавляем кнопку сразу за предыдущим виджетом, без растяжки
            try:
                host_layout.addSpacing(6)
            except Exception:
                pass
            host_layout.addWidget(btn, 0, Qt.AlignLeft)
        else:
            host_layout.addStretch()
            host_layout.addWidget(btn)
    else:
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(btn)
        host_layout.insertLayout(0, row)
    return btn


def embed_button_in_lineedit(edit: QLineEdit, on_click):
    """Добавляет кнопку '…' внутрь правой части QLineEdit.

    Args:
        edit: QLineEdit widget to embed button into
        on_click: Callback function for button click

    Returns:
        QToolButton or None: The embedded button or None if failed
    """
    try:
        btn = QToolButton(edit)
        btn.setText('…')
        btn.setAutoRaise(True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFocusPolicy(Qt.NoFocus)
        btn.clicked.connect(on_click)

        def _reposition():
            try:
                # располагать кнопку справа, по центру по вертикали
                bw = btn.sizeHint().width()
                bh = btn.sizeHint().height()
                x = edit.rect().right() - bw - 4
                y = (edit.rect().height() - bh) // 2
                btn.move(x, y)
                edit.setTextMargins(0, 0, bw + 8, 0)
            except Exception:
                pass

        def _resize(ev):
            try:
                QLineEdit.resizeEvent(edit, ev)
            except Exception:
                pass
            _reposition()

        edit.resizeEvent = _resize
        _reposition()
        return btn
    except Exception:
        return None


def pick_file(parent_widget, edit: QLineEdit, pattern: str):
    """Open file dialog and set selected path to QLineEdit.

    Args:
        parent_widget: Parent widget for the dialog
        edit: QLineEdit to set the selected path
        pattern: File pattern filter (e.g., '*.tsv')
    """
    path, _ = QFileDialog.getOpenFileName(
        parent_widget, _t(parent_widget, 'ui.choose_file', 'Choose file'), filter=f'Files ({pattern})')
    if path:
        edit.setText(path)


def pick_save(parent_widget, edit: QLineEdit, default_ext: str):
    """Open save file dialog and set selected path to QLineEdit.

    Args:
        parent_widget: Parent widget for the dialog
        edit: QLineEdit to set the selected path
        default_ext: Default file extension (e.g., 'tsv')
    """
    path, _ = QFileDialog.getSaveFileName(
        parent_widget, _t(parent_widget, 'ui.save_to', 'Save to'), filter=f'*.{default_ext.lstrip(".")}')
    if path:
        edit.setText(path)


def open_from_edit(parent_widget, edit: QLineEdit):
    """Открыть файл из пути, указанного в QLineEdit. Для .tsv — создать пустой, если отсутствует.

    Args:
        parent_widget: Parent widget for message boxes
        edit: QLineEdit containing the file path
    """
    try:
        path = (edit.text() or '').strip()
        if not path:
            QMessageBox.warning(
                parent_widget,
                _t(parent_widget, 'ui.error', 'Error'),
                _t(parent_widget, 'ui.specify_file_path_first', 'First provide the path to the file.'),
            )
            return
        # Если это TSV и файла нет — создаём пустой файл (как с правилами замен)
        try:
            _, ext = os.path.splitext(path)
            if ext.lower() == '.tsv' and not os.path.exists(path):
                dir_name = os.path.dirname(path)
                if dir_name:
                    os.makedirs(dir_name, exist_ok=True)
                with open(path, 'w', encoding='utf-8') as f:
                    f.write('')
        except Exception:
            pass
        if not os.path.exists(path):
            QMessageBox.warning(
                parent_widget,
                _t(parent_widget, 'ui.error', 'Error'),
                _fmt(parent_widget, 'ui.file_not_found', 'File not found: {path}', path=path),
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(path)))
    except Exception as e:
        QMessageBox.warning(
            parent_widget,
            _t(parent_widget, 'ui.error', 'Error'),
            _fmt(parent_widget, 'ui.file_open_failed', 'Failed to open file: {error}', error=e),
        )


def pretty_format_msg(raw: str) -> tuple[str, bool]:
    """Преобразует типичные сообщения о переносе в формат с эмодзи и разделителями.

    Args:
        raw: Raw message string

    Returns:
        tuple[str, bool]: (formatted_string, is_html_escaped)
    """
    try:
        s = (raw or '').strip()
        # Шаблон: "→ Категория:Имя : "Статья" — тип/статус"
        m = re.match(
            r'^(?:→|▪️)\s+(?P<cat>[^:]+:.+?)\s*:\s*"(?P<title>[^"]+)"\s*—\s*(?P<rest>.+)', s)
        if m:
            cat = (m.group('cat') or '').strip()
            title = (m.group('title') or '').strip()
            rest = (m.group('rest') or '').strip()

            typ = None
            tail = rest
            rest_low = rest.lower()
            type_aliases = {
                'article': _locale_tokens('ui.log.object.article_alias', 'article', 'page'),
                'template': _locale_tokens('ui.log.object.template_alias', 'template', 'module'),
                'file': _locale_tokens('ui.log.object.file_alias', 'file', 'image'),
            }
            for typ_key, aliases in type_aliases.items():
                matched = next(
                    (alias for alias in sorted(aliases, key=len, reverse=True) if rest_low.startswith(alias)),
                    '',
                )
                if matched:
                    typ = typ_key
                    tail = rest[len(matched):].strip()
                    break

            folder_emoji = '📁'
            item_emoji = '📄'
            if typ == 'template':
                item_emoji = '⚛️'
            elif typ == 'file':
                item_emoji = '🖼️'

            low_tail = tail.lower()
            status_emoji = ''
            if any(token in low_tail for token in _locale_tokens('ui.log.keyword.error', 'error', 'failed')):
                status_emoji = '❌'
            elif any(token in low_tail for token in _locale_tokens('ui.log.keyword.skipped', 'skipped', 'skip')):
                status_emoji = '⏭️'
            elif any(token in low_tail for token in _locale_tokens('ui.log.keyword.transferred', 'transferred', 'moved')):
                status_emoji = '✅'
            elif any(token in low_tail for token in _locale_tokens('ui.log.keyword.renamed', 'renamed')):
                status_emoji = '🔁'
            elif any(token in low_tail for token in _locale_tokens('ui.log.keyword.created', 'created')):
                status_emoji = '🆕'
            elif any(token in low_tail for token in _locale_tokens('ui.log.keyword.written', 'written', 'saved')):
                status_emoji = '💾'
            elif any(token in low_tail for token in _locale_tokens('ui.log.keyword.not_exists', 'does not exist')):
                status_emoji = '⚠️'
            elif any(token in low_tail for token in _locale_tokens('ui.log.keyword.already_exists', 'already exists')):
                status_emoji = 'ℹ️'
            elif any(token in low_tail for token in _locale_tokens('ui.log.keyword.done', 'done', 'completed')):
                status_emoji = '✅'

            sep1 = ' • '
            sep2 = ' — '
            pretty = f"{folder_emoji} {cat}{sep1}{item_emoji} {title}{sep2}{status_emoji} {tail}".strip()
            try:
                return html.escape(pretty), True
            except Exception:
                return pretty, False
    except Exception:
        pass
    return raw, False


def _log_theme_mode(widget: QTextEdit) -> str:
    try:
        host = widget.window() if widget is not None and hasattr(widget, 'window') else None
    except Exception:
        host = None
    try:
        mode = str(getattr(host, '_theme_mode', 'teal') or 'teal').strip().lower()
    except Exception:
        mode = 'teal'
    if mode.startswith('light'):
        return 'light'
    if mode.startswith('dark'):
        return 'dark'
    return 'teal'


def _log_palette(widget: QTextEdit) -> dict[str, str]:
    mode = _log_theme_mode(widget)
    if mode == 'light':
        return {
            'text': '#20323f',
            'timestamp': '#6c7f90',
            'badge_bg': '#eaf1f7',
            'badge_border': '#c6d8e8',
            'badge_text': '#2b516c',
            'section_bg': '#f3f8fd',
            'section_border': '#9ebbd4',
            'success': '#1f7a3d',
            'warning': '#9a6a10',
            'error': '#b3261e',
            'info': '#2f5f8a',
            'progress': '#2f6fb1',
            'action': '#1f7c8b',
            'stop': '#9b4a00',
        }
    return {
        'text': '#e6edf5',
        'timestamp': '#90a7bf',
        'badge_bg': '#2a3746',
        'badge_border': '#43576d',
        'badge_text': '#d8e6f5',
        'section_bg': '#1f2935',
        'section_border': '#4a6580',
        'success': '#9fc1f3',
        'warning': '#e7c17b',
        'error': '#ff9cab',
        'info': '#9dc6f2',
        'progress': '#90b9ff',
        'action': '#9fb8d8',
        'stop': '#ffbc76',
    }


def _init_log_widget_style(widget: QTextEdit):
    try:
        if bool(widget.property('_wct_log_compact_css')):
            return
    except Exception:
        pass
    try:
        # Убираем большие отступы абзацев у QTextEdit.append(),
        # чтобы лог читался плотнее и единообразно.
        widget.document().setDefaultStyleSheet('p { margin: 0; }')
    except Exception:
        pass
    try:
        widget.setProperty('_wct_log_compact_css', True)
    except Exception:
        pass


def _extract_module_prefix(msg: str) -> tuple[str, str]:
    text = (msg or '').strip()
    m = re.match(r'^\[(?P<mod>[^\]]+)\]\s*(?P<body>.*)$', text)
    if not m:
        return '', text
    module = (m.group('mod') or '').strip()
    body = (m.group('body') or '').strip()
    # Игнорируем случай, если в квадратных скобках только время.
    if re.match(r'^\d{2}:\d{2}:\d{2}$', module):
        return '', text
    return module, body


def _is_section_line(msg: str) -> tuple[bool, str]:
    text = (msg or '').strip()
    m = re.match(r'^=+\s*(.*?)\s*=+$', text)
    if not m:
        return False, text
    return True, (m.group(1) or '').strip()


def _detect_log_level(msg: str) -> str:
    low = (msg or '').strip().lower()
    if not low:
        return 'info'
    if any(k in low for k in _locale_tokens('ui.log.level.stop', 'stopped', 'cancelled', 'aborted')):
        return 'stop'
    if any(
        k in low
        for k in _locale_tokens(
            'ui.log.level.error',
            'error',
            'failed',
            'traceback',
            'exception',
        )
    ):
        return 'error'
    if any(k in low for k in _locale_tokens('ui.log.level.warning', 'skip', 'not found', 'missing', 'does not exist')):
        return 'warning'
    if any(
        k in low
        for k in _locale_tokens('ui.log.level.action', 'starting', 'starting preview', 'started')
    ):
        return 'action'
    if re.search(r'\b\d+\s*/\s*\d+\b', low) or any(k in low for k in _locale_tokens('ui.log.level.progress', 'processed', 'progress')):
        return 'progress'
    if any(
        k in low
        for k in _locale_tokens(
            'ui.log.level.success',
            'completed',
            'done',
            'created',
            'written',
            'saved',
            'success',
        )
    ):
        return 'success'
    return 'info'


def _level_icon(level: str) -> str:
    return {
        'success': '✅',
        'warning': '⚠️',
        'error': '❌',
        'info': 'ℹ️',
        'progress': '⏳',
        'action': '▶️',
        'stop': '⏹️',
    }.get(level, 'ℹ️')


def log_message(widget: QTextEdit, msg: str, debug_func=None):
    """Единый формат логов для QTextEdit: компактно, структурно, с темой.

    Args:
        widget: QTextEdit widget to log to
        msg: Message to log
        debug_func: Optional debug function to call (e.g., from utils.debug)
    """
    if debug_func:
        debug_func(msg)

    msg = _ui_translate(widget, msg)
    _init_log_widget_style(widget)

    try:
        ts = datetime.now().strftime('%H:%M:%S')
        time_html = f"<span style='color:{_log_palette(widget)['timestamp']}'>[{html.escape(ts)}]</span>"
    except Exception:
        time_html = ''

    palette = _log_palette(widget)
    is_section, section_text = _is_section_line(msg)
    if is_section:
        body = html.escape(section_text).replace('\n', '<br/>')
        widget.append(
            (
                f"<div style='background:{palette['section_bg']}; "
                f"border-left:3px solid {palette['section_border']}; "
                f"border-radius:4px; padding:2px 7px;'>"
                f"{time_html} "
                f"<span style='color:{palette['action']}; font-weight:600;'>"
                f"▸ {body}"
                f"</span></div>"
            )
        )
        return

    module, body_raw = _extract_module_prefix(msg)
    pretty_body, pretty_escaped = pretty_format_msg(body_raw)
    if pretty_escaped:
        body_html = pretty_body
    else:
        body_html = html.escape(pretty_body).replace('\n', '<br/>')

    level = _detect_log_level(f"{module} {body_raw}".strip())
    icon = _level_icon(level)
    level_color = palette.get(level, palette['info'])

    badge_html = ''
    if module:
        badge_html = (
            f"<span style='background:{palette['badge_bg']}; "
            f"color:{palette['badge_text']}; "
            f"border:1px solid {palette['badge_border']}; "
            "border-radius:8px; padding:0 6px; margin-right:4px;'>"
            f"{html.escape(module)}"
            "</span>"
        )

    widget.append(
        (
            f"{time_html} {badge_html}"
            f"<span style='color:{level_color};'>{icon}</span> "
            f"<span style='color:{palette['text']};'>{body_html}</span>"
        ).strip()
    )


def set_start_stop_ratio(start_btn: QPushButton, stop_btn: QPushButton, ratio: int = 3):
    """Set the width ratio between start and stop buttons.

    Args:
        start_btn: Start button (will be wider)
        stop_btn: Stop button (will be narrower)
        ratio: Width ratio (start_width = ratio * stop_width)
    """
    try:
        sh_start = start_btn.sizeHint().width()
        sh_stop = stop_btn.sizeHint().width()
        ratio_use = max(1.25, min(float(ratio), 1.6))
        stop_w = max(sh_stop + 2, 72)
        start_w = max(sh_start + 4, int(stop_w * ratio_use))
        # Используем min-width вместо fixed-width, чтобы кнопки оставались компактными и гибкими.
        stop_btn.setMaximumWidth(16777215)
        start_btn.setMaximumWidth(16777215)
        stop_btn.setMinimumWidth(stop_w)
        start_btn.setMinimumWidth(start_w)
    except Exception:
        # Fallback
        stop_btn.setMinimumWidth(74)
        start_btn.setMinimumWidth(132)


def apply_cred_style(user_edit: QLineEdit, pass_edit: QLineEdit, lang_combo,
                     login_btn: QPushButton, switch_btn: QPushButton,
                     status_label, ok: bool) -> tuple[str | None, str | None]:
    """Apply credential styling to authentication UI elements.

    Args:
        user_edit: Username QLineEdit
        pass_edit: Password QLineEdit
        lang_combo: Language combo box
        login_btn: Login button
        switch_btn: Switch account button
        status_label: Status label
        ok: Whether authentication is successful

    Returns:
        tuple[str, str]: (current_user, current_lang)
    """
    css_ok = 'background-color:#d4edda'
    css_def = ''
    for w in (user_edit, pass_edit):
        w.setStyleSheet(css_ok if ok else css_def)
    user_edit.setReadOnly(ok)
    pass_edit.setReadOnly(ok)
    lang_combo.setEnabled(not ok)
    login_btn.setVisible(not ok)
    switch_btn.setVisible(ok)
    status_label.setText(
        _t(status_label, 'ui.authorized', 'Authorized')
        if ok
        else _t(status_label, 'ui.authentication_pywikibot', 'Authentication (pywikibot)')
    )

    current_user = user_edit.text().strip() if ok else None
    current_lang = (lang_combo.currentText() or 'ru').strip() if ok else None

    return current_user, current_lang


def force_on_top(window, enable: bool, delay_ms: int = 0) -> None:
    """Force window to stay on top or remove the flag.

    Args:
        window: Window widget to modify
        enable: Whether to enable stay-on-top
        delay_ms: Delay in milliseconds before applying
    """
    if delay_ms and delay_ms > 0:
        try:
            QTimer.singleShot(
                delay_ms, lambda: force_on_top(window, enable, 0))
            return
        except Exception:
            pass
    try:
        # Check if we have the _stay_on_top_active attribute
        if hasattr(window, '_stay_on_top_active'):
            if enable == window._stay_on_top_active:
                if enable:
                    window.raise_()
                    window.activateWindow()
                return
            window._stay_on_top_active = bool(enable)
        else:
            window._stay_on_top_active = bool(enable)

        was_visible = window.isVisible()
        window.setWindowFlag(Qt.WindowStaysOnTopHint,
                             window._stay_on_top_active)
        if was_visible:
            # пере-применить флаг и удержать окно активным
            window.show()
            window.raise_()
            window.activateWindow()
    except Exception:
        pass


def bring_to_front_sequence(window) -> None:
    """Многократное восстановление окна на передний план с задержками,
    чтобы перекрыть возможные асинхронные кражи фокуса.

    Args:
        window: Window widget to bring to front
    """
    try:
        def bring():
            try:
                if window.isMinimized():
                    window.showNormal()
                window.raise_()
                window.activateWindow()
                # Дополнительно — WinAPI на Windows
                if sys.platform.startswith('win'):
                    try:
                        hwnd = int(window.winId())
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


# ====== ЛОГ ДЕРЕВО: константы, инициализация, добавление строк ======

STATUS_INFO = {
    'success': {'emoji': '✅', 'color': '#4f83d1'},
    'skipped': {'emoji': '⏭️', 'color': '#6b7280'},
    'error': {'emoji': '❌', 'color': '#ef4444'},
    'not_found': {'emoji': '⚠️', 'color': '#f97316'},
    'info': {'emoji': 'ℹ️', 'color': '#3b82f6'},
}

MODE_INFO = {
    'auto': {'emoji': '⚡'},
    'manual': {'emoji': '✍️'},
    'direct': {'emoji': '📝'},
}

OBJ_INFO = {
    'article': {'emoji': '📄'},
    'template': {'emoji': '⚛️'},
    'file': {'emoji': '🖼️'},
    'category': {'emoji': '📁'},
}


# ====== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ NS (без хардкода языков) ======
def _resolve_ns_context_from_tree(tree: QTreeWidget):
    """Пытается достать NamespaceManager, текущие family/lang и выбранное NS из дерева.

    Возвращает кортеж (ns_manager, family, lang, selected_ns) либо (None, None, None, None).
    selected_ns может быть: int (конкретное NS), 'auto', или None.
    """
    try:
        parent = tree.parent()
        # Вкладки хранят ссылку на главное окно в поле parent_window
        mw = getattr(parent, 'parent_window', None) or getattr(
            parent, 'window', lambda: None)()
        ns_manager = getattr(mw, 'namespace_manager', None)
        family = getattr(mw, 'current_family', None)
        lang = getattr(mw, 'current_lang', None)

        # Пытаемся получить выбранное пространство имён из комбобокса вкладки
        selected_ns = None
        try:
            # parent это вкладка (RenameTab, CreateTab, ParseTab, ReplaceTab)
            # Ищем комбобокс с именем, содержащим 'ns_combo'
            ns_combo = (getattr(parent, 'rename_ns_combo', None) or
                        getattr(parent, 'create_ns_combo', None) or
                        getattr(parent, 'parse_ns_combo', None) or
                        getattr(parent, 'rep_ns_combo', None) or
                        getattr(parent, 'replace_ns_combo', None))
            if ns_combo and hasattr(ns_combo, 'currentData'):
                selected_ns = ns_combo.currentData()
                # Нормализуем: если строка 'auto', оставляем как есть; если int - оставляем
                if isinstance(selected_ns, str):
                    selected_ns = selected_ns.strip().lower() if selected_ns else 'auto'
        except Exception:
            selected_ns = None

        if ns_manager is None:
            try:
                from ...core.namespace_manager import get_namespace_manager
                ns_manager = get_namespace_manager()
            except Exception:
                ns_manager = None
        return ns_manager, family, lang, selected_ns
    except Exception:
        return None, None, None, None


def _detect_object_type_by_ns(tree: QTreeWidget, title: str) -> str:
    """Определяет тип объекта по локализованным префиксам NS (без хардкода).

    Возвращает 'template' | 'file' | 'category' | 'article'.
    """
    try:
        ns_manager, family, lang, _ = _resolve_ns_context_from_tree(tree)
        if ns_manager and family and lang:
            txt = (title or '').strip()
            # Template (10) и Module (828)
            if ns_manager.has_prefix_by_policy(family, lang, txt, {10, 828}):
                return 'template'
            # File (6)
            if ns_manager.has_prefix_by_policy(family, lang, txt, {6}):
                return 'file'
            # Category (14)
            if ns_manager.has_prefix_by_policy(family, lang, txt, {14}):
                return 'category'
    except Exception:
        pass
    # Фолбэк: простая эвристика (языконезависимые части и английские слова)
    lt = (title or '').lower()
    pref = lt.split(':', 1)[0].strip() if ':' in lt else ''
    if any(k in lt for k in ('template:', 'module:')) or any(k in pref for k in _locale_tokens('ui.log.object.template_prefixes', 'template', 'module', 'sablon', 'modul')):
        return 'template'
    if any(k in lt for k in ('file:', 'image:')) or any(k in pref for k in _locale_tokens('ui.log.object.file_prefixes', 'file', 'image', 'dosya', 'archivo', 'datei')):
        return 'file'
    if any(k in lt for k in _locale_tokens('ui.log.object.category_prefixes', 'category:', 'kategori:')) or any(k in pref for k in _locale_tokens('ui.log.object.category_prefix_roots', 'category', 'kategori', 'categoria', 'kategoria')):
        return 'category'
    return 'article'


def _fmt_cat_with_ns(tree: QTreeWidget, cat_name: str) -> str:
    """Форматирует название категории с правильным локальным префиксом.

    Если категория уже имеет префикс (любой локальный или английский) — возвращает как есть.
    Иначе добавляет локальный префикс категории для текущего языка.
    """
    cat = (cat_name or '').strip()
    if not cat:
        return cat
    try:
        ns_manager, family, lang, _ = _resolve_ns_context_from_tree(tree)
        if ns_manager and family and lang:
            # Проверяем, есть ли уже префикс категории
            if ns_manager.has_prefix_by_policy(family, lang, cat, {14}):
                return cat
            # Добавляем локальный префикс
            prefix = ns_manager.get_policy_prefix(
                family, lang, 14, 'Category:')
            return f"{prefix}{cat}"
    except Exception:
        pass
    # Fallback: keep existing namespace-like prefix or add the generic category prefix.
    cl = cat.lower()
    if any(cl.startswith(prefix) for prefix in _locale_tokens('ui.log.object.category_prefixes', 'category:', 'kategori:')):
        return cat
    if re.match(r'^[^\W\d_]+:', cat, re.UNICODE):
        return cat
    return f"{_t(tree, 'ui.log.object.category_prefix_default', 'Category:')}{cat}"


def _is_template_like_source(tree: QTreeWidget, source: str) -> bool:
    """Проверяет, является ли строка источника ссылкой на шаблон/модуль, с учётом локали."""
    try:
        if not source:
            return False
        ns_manager, family, lang, _ = _resolve_ns_context_from_tree(tree)
        if ns_manager and family and lang:
            return ns_manager.has_prefix_by_policy(family, lang, source, {10, 828})
    except Exception:
        pass
    sl = (source or '').lower()
    return any(k in sl for k in ('template:', 'module:'))


def _strip_template_source_prefix(widget, value: str) -> str:
    text = (value or '').strip()
    low = text.lower()
    prefixes = sorted(
        _locale_tokens('ui.log.template_source_prefix', 't:'),
        key=len,
        reverse=True,
    )
    for prefix in prefixes:
        if low.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def _build_mode_tooltip(widget, system: bool, mode: str, object_type: str | None) -> str:
    """Формирует понятную подсказку для колонки «Тип»."""
    try:
        if system:
            return _t(widget, 'ui.log.tooltip.system', '⚙️ Service process line (without direct page edit).')
        if object_type == 'template':
            if mode == 'auto':
                return _t(widget, 'ui.log.tooltip.template_auto', '⚡ Template parameter change with auto-approval.')
            return _t(widget, 'ui.log.tooltip.template_manual', '✍️ Template parameter change with manual confirmation.')
        return _t(widget, 'ui.log.tooltip.direct', '📝 Direct category link edit in page text.')
    except Exception:
        return _t(widget, 'ui.log.tooltip.operation_type', 'Operation type.')


def _build_status_tooltip(tree: QTreeWidget, status: str, title: str, source: str | None,
                          mode: str, object_type: str | None, system: bool) -> str:
    """Формирует подробную подсказку для колонки «Статус»."""
    try:
        low_title = (title or '').strip().lower()
        low_source = (source or '').strip().lower()

        reason = ''
        if status == 'skipped':
            if any(token in low_title for token in _locale_tokens('ui.log.keyword.user_cancelled', 'user', 'cancel')):
                reason = _t(tree, 'ui.log.reason.skipped_user', 'Skipped manually by the user in the confirmation dialog.')
            elif any(token in low_title for token in _locale_tokens('ui.log.keyword.automatic', 'automatic', 'auto')):
                reason = _t(tree, 'ui.log.reason.skipped_auto', 'Skipped automatically by a saved rule.')
            elif any(token in low_title for token in _locale_tokens('ui.log.keyword.no_changes', 'without changes', 'no changes')):
                reason = _t(tree, 'ui.log.reason.skipped_no_changes', 'No matching replacements were found.')
            elif any(token in low_title for token in _locale_tokens('ui.log.keyword.empty', 'empty')):
                reason = _t(tree, 'ui.log.reason.skipped_empty', 'The source category is empty, there is nothing to process.')
            else:
                reason = _t(tree, 'ui.log.reason.skipped_generic', 'The change was not applied.')
        elif status == 'success':
            if any(token in low_title for token in _locale_tokens('ui.log.keyword.renamed_success', 'renamed successfully')):
                reason = _t(tree, 'ui.log.reason.success_rename', 'Renaming completed successfully.')
            elif any(token in low_title for token in _locale_tokens('ui.log.keyword.transferred', 'transferred', 'moved')):
                reason = _t(tree, 'ui.log.reason.success_transfer', 'Changes were applied and saved.')
            else:
                reason = _t(tree, 'ui.log.reason.success_generic', 'The operation completed successfully.')
        elif status == 'error':
            reason = _t(tree, 'ui.log.reason.error', 'The operation ended with an error; see the "Action or title" column for details.')
        elif status == 'not_found':
            if any(token in low_title for token in _locale_tokens('ui.log.keyword.not_exists_no_pages', 'does not exist and has no pages')):
                reason = _t(tree, 'ui.log.reason.not_found_no_pages', 'The object does not exist and contains no pages.')
            elif any(token in low_title for token in _locale_tokens('ui.log.keyword.not_exists', 'does not exist')):
                reason = _t(tree, 'ui.log.reason.not_found_not_exists', 'The object does not exist.')
            else:
                reason = _t(tree, 'ui.log.reason.not_found_generic', 'The requested object was not found.')
        else:
            if any(token in low_title for token in _locale_tokens('ui.log.keyword.stopped', 'stopped')):
                reason = _t(tree, 'ui.log.reason.info_stopped', 'The process was stopped by the user.')
            elif any(token in low_title for token in _locale_tokens('ui.log.keyword.category_transfer_start', 'category content transfer')):
                reason = _t(tree, 'ui.log.reason.info_transfer', 'Service message about the start or progress of category content transfer.')
            elif any(token in low_title for token in _locale_tokens('ui.log.keyword.already_exists', 'already exists')):
                reason = _t(tree, 'ui.log.reason.info_already_exists', 'The target page already exists.')
            else:
                reason = _t(tree, 'ui.log.reason.info_generic', 'Informational message about operation progress.')

        details: list[str] = []
        if source:
            if _is_template_like_source(tree, source):
                if '[locative]' in low_source or any(token in low_source for token in _locale_tokens('ui.log.keyword.locative_tag', '[locative]')):
                    details.append(_t(tree, 'ui.log.detail.context_template_locative', 'Context: template parameter, locative heuristic.'))
                elif '[partial]' in low_source or any(token in low_source for token in _locale_tokens('ui.log.keyword.partial_tag', '[partial]')):
                    details.append(_t(tree, 'ui.log.detail.context_template_partial', 'Context: template parameter, partial match.'))
                else:
                    details.append(_t(tree, 'ui.log.detail.context_template_full', 'Context: template parameter, full match.'))
            elif low_source == 'api':
                details.append(_t(tree, 'ui.log.detail.context_api', 'Context: data/check via MediaWiki API.'))
            else:
                details.append(_fmt(tree, 'ui.log.detail.context_source', 'Context: {source}.', source=source))
        if object_type == 'template':
            details.append(
                _fmt(
                    tree,
                    'ui.log.detail.mode',
                    'Mode: {mode}.',
                    mode=_t(tree, 'ui.log.mode.auto_short', 'auto-approval') if mode == 'auto' else _t(tree, 'ui.log.mode.manual_short', 'manual confirmation'),
                )
            )
        if system:
            details.append(_t(tree, 'ui.log.detail.system', 'The entry was added by the log system.'))

        return '\n'.join([_fmt(tree, 'ui.log.detail.reason', 'Reason: {reason}', reason=reason)] + details)
    except Exception:
        return _t(tree, 'ui.log.detail.unavailable', 'Status details are unavailable.')


def init_log_tree(parent_widget) -> QTreeWidget:
    """Создаёт QTreeWidget для древовидного лога.

    Колонки:
      0) Время — всегда первая
      1) Тип один из типов: ⚡ Авто / ✍️ Ручное / 📝 Прямой перенос / ⚙️ Системное
      2) Статус — ✅/⏭️/❌/⚠️/ℹ️ + текст
      3) Заголовок — с иконкой объекта в начале (📄/⚛️/🖼️/📁), кроме чисто инфо‑сообщений
      4) Источник — например, «Шаблон:Категории»
    """
    tree = QTreeWidget(parent_widget)
    try:
        # Добавляем колонку «Страница» между Заголовком и Источником
        tree.setColumnCount(6)
        tree.setHeaderLabels([
            _t(parent_widget, 'ui.time', 'Time'),
            _t(parent_widget, 'ui.type', 'Type'),
            _t(parent_widget, 'ui.status', 'Status'),
            _t(parent_widget, 'ui.action_or_title', 'Action or title'),
            _t(parent_widget, 'ui.page', 'Page'),
            _t(parent_widget, 'ui.source', 'Source'),
        ])
        # Плоская таблица: без древовидности и индикаторов
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        tree.setUniformRowHeights(True)
        tree.setSortingEnabled(False)
        tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        try:
            tree.setExpandsOnDoubleClick(False)
            tree.setSelectionBehavior(QAbstractItemView.SelectRows)
            tree.setAllColumnsShowFocus(True)
        except Exception:
            pass
        # Режим изменения ширины столбцов пользователем
        hdr = tree.header()
        try:
            hdr.setStretchLastSection(False)
            hdr.setMinimumSectionSize(28)
            try:
                hdr.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            except Exception:
                pass
            for i in range(6):
                hdr.setSectionResizeMode(i, QHeaderView.Interactive)
        except Exception:
            pass
        # Базовые ширины: все колонки можно двигать, кроме «Тип».
        try:
            from PySide6.QtCore import Qt as _Qt
            fm0 = tree.fontMetrics()
            fmh = hdr.fontMetrics() if hasattr(hdr, 'fontMetrics') else fm0

            hdr.setSectionResizeMode(0, QHeaderView.Interactive)
            time_w = max(
                72,
                fm0.horizontalAdvance('00:00:00') + 12,
                fmh.horizontalAdvance(tree.headerItem().text(0) or '') + 14,
            )
            tree.setColumnWidth(0, time_w)

            hdr.setSectionResizeMode(1, QHeaderView.Fixed)
            try:
                emoji_samples = ['⚡', '✍️', '📝', '⚙️']
                emoji_w = max(fm0.horizontalAdvance(e) for e in emoji_samples)
            except Exception:
                emoji_w = 16
            header_txt = tree.headerItem().text(1) or ''
            head_w = fmh.horizontalAdvance(header_txt)
            t_w = max(emoji_w + 14, head_w + 12)
            t_w = max(46, t_w)
            tree.setColumnWidth(1, t_w)
            tree.headerItem().setTextAlignment(1, _Qt.AlignHCenter | _Qt.AlignVCenter)

            hdr.setSectionResizeMode(2, QHeaderView.Interactive)
            try:
                fm2 = tree.fontMetrics()
                status_meta = _status_meta(parent_widget)
                status_sample = f"{status_meta['skipped']['emoji']} {status_meta['skipped']['label']}"
                header_w = fmh.horizontalAdvance(tree.headerItem().text(2) or '') + 18
                status_w = fm2.horizontalAdvance(status_sample) + 22
                tree.setColumnWidth(2, max(118, status_w, header_w))
            except Exception:
                pass
            tree.setColumnWidth(3, max(280, fmh.horizontalAdvance(tree.headerItem().text(3) or '') + 28))
            tree.setColumnWidth(4, max(220, fmh.horizontalAdvance(tree.headerItem().text(4) or '') + 28))
            tree.setColumnWidth(5, max(160, fmh.horizontalAdvance(tree.headerItem().text(5) or '') + 28))
        except Exception:
            pass
        # Явно выравниваем заголовки по вертикальному центру.
        try:
            from PySide6.QtCore import Qt as _Qt
            for col in range(tree.columnCount()):
                align = _Qt.AlignLeft | _Qt.AlignVCenter
                if col == 1:
                    align = _Qt.AlignHCenter | _Qt.AlignVCenter
                tree.headerItem().setTextAlignment(col, align)
        except Exception:
            pass
        # Инициально подгоним ширины по заголовкам
        try:
            fm = tree.fontMetrics()
            extras = 16
            for i in range(tree.columnCount()):
                if i in (0, 1, 2):
                    continue
                try:
                    header_w = fm.horizontalAdvance(
                        tree.headerItem().text(i) or '') + extras
                    cur_w = tree.columnWidth(i)
                    if header_w > cur_w:
                        tree.setColumnWidth(i, header_w)
                except Exception:
                    pass
        except Exception:
            pass
        # Цвета/заголовки дерева задаются глобальной темой окна.
        # Разрешаем множественное копирование и контекстное меню «Открыть» по правому клику
        enable_tree_copy_shortcut(tree)
        _enable_open_on_title_right_click(tree)
    except Exception:
        pass
    return tree


def log_tree_add(tree: QTreeWidget, timestamp: str, page: str | None, title: str,
                 mode: str, status: str, source: str | None = None,
                 object_type: str | None = None, system: bool = False) -> None:
    """Добавить запись в лог-таблицу (плоский режим, 6 колонок).

    Args:
        tree: целевое дерево
        timestamp: строка времени HH:MM:SS
        page: обрабатываемая страница (статья, категория, шаблон и т.д.) или None для системных
        title: заголовок/описание действия
        mode: 'auto' | 'manual'
        status: 'success' | 'skipped' | 'error' | 'not_found' | 'info'
        source: строка источника (например, 'Шаблон:Категории')
        object_type: 'article' | 'template' | 'file' | 'category' | None
        system: True для служебных записей вне группировки
    """
    try:
        status_info = _status_meta(tree)
        mode_info = _mode_meta(tree)
        obj_info = _obj_meta(tree)
        st = status_info.get(status, status_info['success'])
        # Тип для колонки «Тип» (режим/прямой перенос) берём из object_type аргумента,
        # а иконку в заголовке определяем ТОЛЬКО по префиксу самого title
        title_obj_type = _detect_object_type_by_ns(tree, title)
        title_meta = obj_info.get(
            title_obj_type or 'article', obj_info['article'])
        obj_emoji = title_meta['emoji']

        status_text = f"{st['emoji']} {st['label']}"
        # Типодин тип. Для системных — ⚙️. Для прямого переноса — 📝.
        # Для изменений в шаблонах — режим (⚡/✍️). Комбинаций типа "✍️ + 📝" больше нет.
        if system:
            action_cell = '⚙️'
        elif object_type == 'template':
            action_cell = mode_info['auto']['emoji'] if (
                mode == 'auto') else mode_info['manual']['emoji']
        else:
            action_cell = mode_info['direct']['emoji']
        # Значок объекта переносим в начало заголовка.
        if status == 'info':
            low_title = (title or '').lower()
            if any(token in low_title for token in _locale_tokens('ui.log.keyword.skipped', 'skipped', 'skip')):
                title_cell = f"⏭️ {title or ''}"
            elif any(token in low_title for token in _locale_tokens('ui.log.keyword.stopped', 'stopped')):
                title_cell = f"⏹️ {title or ''}"
            elif any(token in low_title for token in _locale_tokens('ui.log.keyword.already_exists', 'already exists')):
                title_cell = f"ℹ️ {title or ''}"
            else:
                title_cell = f"{title or ''}"
        else:
            title_cell = f"{obj_emoji} {title or ''}"
        # Иконка источника: если источник — шаблон/модуль
        try:
            src_cell = source or ''
            src_tooltip = ''
            if _is_template_like_source(tree, source) and src_cell:
                # Показываем префикс «Ш:» и базовое имя без префикса
                try:
                    base = src_cell.split(
                        ':', 1)[-1] if ':' in src_cell else src_cell
                except Exception:
                    base = src_cell
                # Особая пометка для частичных совпадений: другой значок в «Источник»
                try:
                    low_base = (base or '').lower()
                except Exception:
                    low_base = str(base or '')
                is_partial_src = ('[partial]' in low_base) or any(token in low_base for token in _locale_tokens('ui.log.keyword.partial_tag', '[partial]'))
                is_loc_src = ('[locative]' in low_base) or any(token in low_base for token in _locale_tokens('ui.log.keyword.locative_tag', '[locative]'))
                # Уберём текстовые пометки из отображаемого имени
                try:
                    base_disp = (
                        base.replace('[partial]', '')
                        .replace('[locative]', '')
                        .strip()
                    )
                    for token in _locale_tokens('ui.log.keyword.partial_tag', '[partial]'):
                        base_disp = base_disp.replace(token, '').replace(token.title(), '')
                    for token in _locale_tokens('ui.log.keyword.locative_tag', '[locative]'):
                        base_disp = base_disp.replace(token, '').replace(token.title(), '')
                    base_disp = base_disp.strip()
                except Exception:
                    base_disp = base
                # Для частичных совпадений и локативов используем отдельные символы источника
                # Полные совпадения: ⚛️ Ш:Имя; Частичные: #️⃣ Ш:Имя; Локативы: 🌐 Ш:Имя
                src_emoji = '🌐' if is_loc_src else (
                    '#️⃣' if is_partial_src else obj_info['template']['emoji'])
                src_cell = f"{src_emoji} {_t(tree, 'ui.log.template_source_prefix', 'T:')}{base_disp}"
                # ToolTip для источника
                try:
                    if is_loc_src:
                        src_tooltip = _t(tree, 'ui.log.source_tooltip.locative', 'Locative heuristic replacement in template parameters.')
                    elif is_partial_src:
                        src_tooltip = _t(tree, 'ui.log.source_tooltip.partial', 'Partial-name replacement in template parameters.')
                    else:
                        src_tooltip = _t(tree, 'ui.log.source_tooltip.full', 'Full category-name replacement in a template parameter.')
                except Exception:
                    src_tooltip = ''
        except Exception:
            src_cell = source or ''
            src_tooltip = ''
        # Колонка «Страница» — тип определяем по самой странице,
        # а не по object_type (он может относиться к другому объекту в строке).
        try:
            page_txt = html.unescape((page or '').strip())
            if not page_txt:
                page_cell = ''
            else:
                page_obj_type = None
                try:
                    page_obj_type = _detect_object_type_by_ns(tree, page_txt)
                except Exception:
                    page_obj_type = None
                if page_obj_type not in ('article', 'template', 'file', 'category'):
                    page_obj_type = None
                # Для строк переноса: если тип страницы не распознан, но тип операции известен
                # как не-article, используем его как безопасный фолбэк.
                if (not page_obj_type or page_obj_type == 'article') and object_type in ('template', 'file', 'category'):
                    page_obj_type = object_type
                if not page_obj_type:
                    page_obj_type = 'article'

                # В колонке «Страница» показываем исходный заголовок как есть:
                # полный исходный префикс без подмены (например, Kategori:, Category: и т.д.).
                page_disp = page_txt
                if page_obj_type == 'category':
                    page_cell = f"{obj_info['category']['emoji']} {page_disp}"
                elif page_obj_type == 'template':
                    page_cell = f"{obj_info['template']['emoji']} {page_disp}"
                elif page_obj_type == 'file':
                    page_cell = f"{obj_info['file']['emoji']} {page_disp}"
                else:  # article
                    page_cell = f"{obj_info['article']['emoji']} {page_disp}"
        except Exception:
            page_cell = page or ''
        row = QTreeWidgetItem(
            [timestamp, action_cell, status_text, title_cell, page_cell, src_cell])
        try:
            for col in range(6):
                row.setText(col, _ui_translate(tree, row.text(col)))
                if col == 1:
                    row.setTextAlignment(col, Qt.AlignHCenter | Qt.AlignVCenter)
                else:
                    row.setTextAlignment(col, Qt.AlignLeft | Qt.AlignVCenter)
        except Exception:
            pass
        # Цвет статуса
        try:
            from PySide6.QtGui import QBrush, QColor
            row.setForeground(2, QBrush(QColor(st['color'])))
        except Exception:
            pass
        # Tooltips для колонок с эмодзи
        try:
            row.setToolTip(1, _build_mode_tooltip(
                tree,
                system, mode, object_type))
            row.setToolTip(2, _build_status_tooltip(
                tree, status, title or '', source, mode, object_type, system))
            # Отключаем подсказки для 3 и 4 колонок
            row.setToolTip(3, '')
            row.setToolTip(4, '')
            # Подсказка для «Источник»: различаем полное/частичное исправление
            try:
                if src_tooltip:
                    row.setToolTip(5, src_tooltip)
            except Exception:
                pass
            try:
                for col in range(6):
                    row.setToolTip(col, _ui_translate(tree, row.toolTip(col)))
            except Exception:
                pass
        except Exception:
            pass
        # Плоский режим: всегда добавляем как верхнеуровневую строку (с защитой от подряд-дубликатов)
        try:
            root = tree.invisibleRootItem()
            if root and root.childCount() > 0:
                last = root.child(root.childCount() - 1)
                same = True
                for i in range(tree.columnCount()):
                    if (last.text(i) or '') != (row.text(i) or ''):
                        same = False
                        break
                if same:
                    return
        except Exception:
            pass
        tree.addTopLevelItem(row)
        # Авторасширение столбцов под содержимое новой строки (без сужения и без влияния на «Тип»)
        try:
            _auto_expand_columns_for_row(tree, row)
        except Exception:
            pass
        # Автопрокрутка к добавленной строке
        try:
            tree.scrollToItem(row)
        except Exception:
            try:
                tree.scrollToBottom()
            except Exception:
                pass
    except Exception:
        pass


def log_tree_parse_and_add(tree: QTreeWidget, raw_msg: str) -> None:
    """Разобрать текст сообщения и добавить в древовидный лог.
    Реагирует на формат вида: "📁 Категория … • 📄 Заголовок — … (Источник)".
    """
    try:
        s = (raw_msg or '').strip()
        # Время — текущее
        ts = datetime.now().strftime('%H:%M:%S')
        # 0) Спец-обработка системных сообщений переименования
        try:
            import re as _re0
            # Приводим к plain‑тексту для устойчивого парсинга
            plain = html.unescape(_re0.sub(r'<[^>]+>', '', s))
            m_begin = _re0.search(
                r"(?P<prefix>[^:]+):\s*(?P<old>.+?)\s*→\s*(?P<new>.+)$",
                plain,
            )
            if m_begin and any((m_begin.group('prefix') or '').strip().lower().startswith(token) for token in _locale_tokens('ui.log.keyword.rename_started', 'starting rename')):
                try:
                    global _LAST_RENAME_OLD, _LAST_RENAME_NEW
                    _LAST_RENAME_OLD = (m_begin.group('old') or '').strip()
                    _LAST_RENAME_NEW = (m_begin.group('new') or '').strip()
                except Exception:
                    pass
                # Определяем тип объекта для правильного отображения
                obj_type = _detect_object_type_by_ns(tree, _LAST_RENAME_OLD)
                # В колонку «Страница» помещаем старое имя (независимо от типа)
                title_txt = _fmt(
                    tree,
                    'ui.log.rename_started_message',
                    'Starting rename: {old} → {new}',
                    old=_LAST_RENAME_OLD,
                    new=_LAST_RENAME_NEW,
                )
                log_tree_add(tree, ts, _LAST_RENAME_OLD, title_txt,
                             'manual', 'success', None, obj_type, True)
                return
            if _has_locale_token(plain, 'ui.log.keyword.renamed_success', 'renamed successfully'):
                try:
                    new_name = _LAST_RENAME_NEW or ''
                    old_name = _LAST_RENAME_OLD or ''
                except Exception:
                    new_name = ''
                    old_name = ''
                # Определяем тип по старому имени (которое мы запомнили при начале переименования)
                obj_type = _detect_object_type_by_ns(
                    tree, old_name) if old_name else 'article'
                # В колонку «Страница» помещаем новое имя (независимо от типа)
                log_tree_add(tree, ts, new_name, plain, 'manual',
                             'success', None, obj_type, True)
                return
        except Exception:
            pass

        # 1) Попытка распарсить сообщения нашего нового формата с эмодзи
        m = re.search(
            r"📁\s*(?P<cat>[^•]+)\s*•\s*📄\s*(?P<title>[^—]+)\s*—\s*(?P<status>[^()]+?)(?:\s*\((?P<src>[^)]+)\))?\s*$", s)
        if not m:
            # 2) Попытка разобрать старый формат через pretty_format_msg
            pretty, _ = pretty_format_msg(s)
            s = html.unescape(pretty)
            m = re.search(
                r"📁\s*(?P<cat>[^•]+)\s*•\s*📄\s*(?P<title>[^—]+)\s*—\s*(?P<status>[^()]+?)(?:\s*\((?P<src>[^)]+)\))?\s*$", s)
        if m:
            cat = html.unescape((m.group('cat') or '').strip())
            title = html.unescape((m.group('title') or '').strip())
            status_raw = html.unescape((m.group('status') or '').strip())
            status_text = status_raw.lower()
            source = html.unescape((m.group('src') or '').strip()) or None

            mode = 'auto' if _has_locale_token(status_text, 'ui.log.keyword.automatic', 'automatic', 'auto') else 'manual'
            if _has_locale_token(status_text, 'ui.log.keyword.error', 'error', 'failed'):
                status = 'error'
            elif _has_locale_token(status_text, 'ui.log.keyword.not_found', 'not found', 'does not exist'):
                status = 'not_found'
            elif _has_locale_token(status_text, 'ui.log.keyword.skipped', 'skipped', 'skip', 'cancelled'):
                status = 'skipped'
            else:
                status = 'success'

            # Тип объекта: используем NamespaceManager с поддержкой локализации
            object_type = _detect_object_type_by_ns(tree, title)
            # Если источник указывает на шаблон — трактуем как изменение через шаблон
            if object_type == 'article' and source and _is_template_like_source(tree, source):
                object_type = 'template'

            log_tree_add(tree, ts, cat, title, mode,
                         status, source, object_type)
            return

        category_pat = _locale_pattern('ui.log.parse.category_word', 'category')
        category_line = re.search(
            rf"(?:{category_pat})\s*<b>(?P<cat>[^<]+)</b>\s*(?P<tail>[^<]+)",
            s,
            re.I,
        )
        if category_line:
            cat = html.unescape((category_line.group('cat') or '').strip())
            tail = html.unescape((category_line.group('tail') or '').strip()).lower()
            if _has_locale_token(tail, 'ui.log.parse.not_exists_no_pages', 'does not exist and has no pages'):
                title = _fmt(
                    tree,
                    'ui.log.title.category_not_exists_no_pages',
                    '{title} - does not exist and has no pages',
                    title=_fmt_cat_with_ns(tree, cat),
                )
                log_tree_add(tree, ts, None, title, 'manual',
                             'not_found', 'API', 'category', True)
                return
            if _has_locale_token(tail, 'ui.log.parse.not_exists', 'does not exist'):
                title = _fmt(
                    tree,
                    'ui.log.title.category_not_exists',
                    '{title} - does not exist',
                    title=_fmt_cat_with_ns(tree, cat),
                )
                log_tree_add(tree, ts, None, title, 'manual',
                             'not_found', 'API', 'category', True)
                return
            if _has_locale_token(tail, 'ui.log.parse.empty', 'empty'):
                title = _fmt(
                    tree,
                    'ui.log.title.category_empty',
                    '{title} - empty',
                    title=_fmt_cat_with_ns(tree, cat),
                )
                log_tree_add(tree, ts, None, title, 'manual',
                             'skipped', 'API', 'category', True)
                return
        bold_message = re.search(
            r"<b>(?P<title>[^<]+)</b>\s*(?P<tail>[^<]+)",
            s,
            re.I,
        )
        if bold_message:
            title0 = html.unescape((bold_message.group('title') or '').strip())
            tail = html.unescape((bold_message.group('tail') or '').strip()).lower()
            if _has_locale_token(tail, 'ui.log.parse.not_found', 'not found'):
                obj_type = _detect_object_type_by_ns(tree, title0)
                cat_col = title0 if obj_type == 'category' else None
                title_txt = _fmt(
                    tree,
                    f'ui.log.title.object_not_found.{obj_type}',
                    '{title} - not found',
                    title=title0,
                )
                log_tree_add(tree, ts, cat_col, title_txt, 'manual',
                             'not_found', None, obj_type, False)
                return
        # 4) Универсальный фолбэк: вывести строку целиком в таблицу без потери информации
        try:
            # Оценим статус/режим/объект эвристически
            s_lower = s.lower()
            status = 'success'
            # Сообщения с префиксом ℹ️ считаем информационными
            if s.strip().startswith('ℹ️'):
                status = 'info'
            if _has_locale_token(s_lower, 'ui.log.keyword.stopped', 'stopped'):
                status = 'info'
            if _has_locale_token(s_lower, 'ui.log.keyword.error', 'error', 'failed', 'traceback'):
                status = 'error'
            elif _has_locale_token(s_lower, 'ui.log.keyword.not_found', 'not found', 'does not exist'):
                status = 'not_found'
            elif _has_locale_token(s_lower, 'ui.log.keyword.skipped', 'skipped', 'skip'):
                status = 'skipped'
            elif _has_locale_token(s_lower, 'ui.log.keyword.already_exists', 'already exists'):
                status = 'info'
            if _has_locale_token(s_lower, 'ui.log.keyword.category_rename_skipped_transfer', 'category rename skipped while transferring contents'):
                status = 'info'
            mode = 'auto' if _has_locale_token(s_lower, 'ui.log.keyword.automatic', 'automatic', 'auto') else 'manual'
            # Удалим HTML-теги, но сохраним текст
            import re as _re
            plain = html.unescape(_re.sub(r'<[^>]+>', '', s))
            # Попробуем выделить источник из скобок в конце
            msrc = _re.search(r'\(([^)]+)\)\s*$', plain)
            src = msrc.group(1) if msrc else ''
            title = plain if not msrc else plain[:msrc.start()].rstrip()
            # Попробуем выделить «→ Категория:… : "Заголовок" …»
            mcat = _re.search(
                r"→\s*(?P<cat>[^:]+:.+?)\s*:\s*\"(?P<title>[^\"]+)\"", title)
            if mcat:
                cat_guess = (mcat.group('cat') or '').strip()
                title_guess = (mcat.group('title') or '').strip()
                obj_type = _detect_object_type_by_ns(tree, title_guess)
                if obj_type == 'article' and src and _is_template_like_source(tree, src):
                    obj_type = 'template'
                log_tree_add(tree, ts, cat_guess, title_guess,
                             mode, status, src or None, obj_type, False)
            else:
                obj_type = _detect_object_type_by_ns(tree, title)
                if obj_type == 'article' and src and _is_template_like_source(tree, src):
                    obj_type = 'template'
                log_tree_add(tree, ts, None, title, mode,
                             status, src or None, obj_type, True)
        except Exception:
            # В самом крайнем случае — добавим как системную строку в колонку заголовка
            log_tree_add(tree, ts, None, s, 'manual',
                         'success', None, 'article', True)
    except Exception:
        pass


def log_tree_help_html(widget=None) -> str:
    """Возвращает HTML-справку по обозначениям лога (эмодзи и цвета)."""
    try:
        status_info = _status_meta(widget)
        mode_info = _mode_meta(widget)
        rows = [
            status_info['success'],
            status_info['skipped'],
            status_info['error'],
            status_info['not_found'],
        ]

        def _row(s):
            return (f"<tr>"
                    f"<td style='padding:4px 8px'>{s['emoji']}</td>"
                    f"<td style='padding:4px 8px'><span style='color:{s['color']}'><b>{s['label']}</b></span></td>"
                    f"</tr>")
        status_table = "".join(_row(s) for s in rows)
        mode_rows = (
            f"<tr><td style='padding:4px 8px'>{mode_info['auto']['emoji']}</td><td style='padding:4px 8px'><b>{mode_info['auto']['label']}</b> - {_t(widget, 'ui.log.help.mode.auto', 'automatic mode')}</td></tr>"
            f"<tr><td style='padding:4px 8px'>{mode_info['manual']['emoji']}</td><td style='padding:4px 8px'><b>{mode_info['manual']['label']}</b> - {_t(widget, 'ui.log.help.mode.manual', 'manual mode')}</td></tr>"
        )
        html_text = (
            "<div style='font-size:12px;line-height:1.35'>"
            f"<h3 style='margin:6px 0'>{_t(widget, 'ui.log.help.title', 'Log legend')}</h3>"
            f"<p>{_t(widget, 'ui.log.help.description', 'Time is always shown in the first column. Entries are grouped by categories as root tree nodes. Columns: Time, Action or title, Status.')}</p>"
            f"<h4 style='margin:6px 0'>{_t(widget, 'ui.log.help.statuses', 'Statuses')}</h4>"
            f"<table style='border-collapse:collapse'>{status_table}</table>"
            f"<h4 style='margin:6px 8px 4px 0'>{_t(widget, 'ui.log.help.modes', 'Modes')}</h4>"
            f"<table style='border-collapse:collapse'>{mode_rows}</table>"
            f"<p>{_t(widget, 'ui.log.help.example', 'Example entry:')} <code>⚡ Title ({_t(widget, 'ui.log.object.template', 'Template')}:Categories)</code></p>"
            "</div>"
        )
        return html_text
    except Exception:
        return _t(widget, 'ui.log.help.unavailable', 'Legend is unavailable')


def enable_tree_copy_shortcut(tree: QTreeWidget) -> None:
    """Включает копирование в буфер обмена выделенных строк таблицы (все столбцы, TSV).
    Работает с Ctrl+C и Shift+Insert.
    """
    try:
        def _collect_selected_rows() -> list[QTreeWidgetItem]:
            # Возвращаем строки в визуальном порядке обхода дерева
            selected = set(tree.selectedItems())
            result: list[QTreeWidgetItem] = []
            root = tree.invisibleRootItem()

            def walk(parent):
                for i in range(parent.childCount()):
                    it = parent.child(i)
                    if it in selected:
                        result.append(it)
                    if it.childCount() > 0:
                        walk(it)
            # top-level
            for i in range(root.childCount()):
                it = root.child(i)
                if it in selected:
                    result.append(it)
                if it.childCount() > 0:
                    walk(it)
            return result

        def _copy():
            try:
                rows = _collect_selected_rows()
                if not rows:
                    return
                # Заголовок
                hdr = [tree.headerItem().text(i)
                       for i in range(tree.columnCount())]
                lines = ['\t'.join(hdr)]
                for it in rows:
                    cols = [it.text(i) for i in range(tree.columnCount())]
                    lines.append('\t'.join(cols))
                txt = '\n'.join(lines)
                QGuiApplication.clipboard().setText(txt)
            except Exception:
                pass

        for seq in (QKeySequence.Copy, QKeySequence(Qt.SHIFT | Qt.Key_Insert)):
            try:
                sc = QShortcut(seq, tree)
                sc.activated.connect(_copy)
            except Exception:
                pass
    except Exception:
        pass


def _enable_open_on_title_right_click(tree: QTreeWidget) -> None:
    """Контекстное меню «Открыть» на колонках: Заголовок(3), Страница(4), Источник(5).

    - Разрешаем только для реальных страниц (не для статуса Инфо/Остановлено и т.п.).
    - Для «Заголовок» распознаём тип с учетом выбранного пространства имён.
    - Для «Страница» используем тип из эмодзи (📄/⚛️/🖼️/📁) и открываем исходный заголовок.
    - Для «Источник» открываем как шаблон (удалив эмодзи и префикс «Ш:»).
    """
    try:
        from PySide6.QtWidgets import QMenu

        def _show_menu(pos):
            try:
                item = tree.itemAt(pos)
                if not item:
                    return
                col = tree.columnAt(pos.x())
                if col not in (3, 4, 5):
                    return
                # Блокируем для информационных строк только для колонки «Действие или заголовок»
                try:
                    if col == 3:
                        st = (item.text(2) or '')
                        if _has_locale_token(st, 'ui.info', 'info') or 'ℹ' in st:
                            return
                except Exception:
                    pass
                # Дополнительно блокируем текстовые «служебные» строки — только для колонки 3
                if col == 3:
                    raw_all = ' '.join([(item.text(i) or '') for i in range(
                        tree.columnCount())]).strip().lower()
                    if _has_locale_token(raw_all, 'ui.log.keyword.category_rename_skipped_transfer', 'category rename skipped while transferring contents'):
                        return
                raw_text = (item.text(col) or '').strip()
                if not raw_text:
                    return
                # Для колонки «Источник»: не показываем меню, если это просто количество страниц
                if col == 5:
                    try:
                        import re as _re
                        txt_plain = raw_text
                        if txt_plain[:2] in ('⚛️ ', '#️⃣ ', '📁 ', '📄 ', '🖼️ '):
                            txt_plain = txt_plain[2:].strip()
                        if _re.match(r'^\s*\d+(?:\s+\w+)?\s*$', txt_plain, _re.I | _re.UNICODE):
                            return
                    except Exception:
                        pass
                # В колонке 3 «Действие или заголовок» открывать только явные названия страниц
                if col == 3:
                    # Блокируем системные строки (⚙️ в колонке «Тип»)
                    try:
                        if (item.text(1) or '').strip().startswith('⚙️'):
                            return
                    except Exception:
                        pass
                    # Дополнительная эвристика: исключаем строки с явными действиями/стрелками/длинными подписями
                    try:
                        ttxt = raw_text
                        if ttxt[:2] in ('📄 ', '🧩 ', '🖼️ ', '📁 '):
                            ttxt = ttxt[2:].strip()
                        low_t = ttxt.lower()
                        if (
                            ('→' in ttxt)
                            or (' - ' in ttxt)
                            or (' — ' in ttxt)
                            or any(low_t.startswith(token) for token in _locale_tokens('ui.log.keyword.rename_started', 'starting rename'))
                            or any(low_t.startswith(token) for token in _locale_tokens('ui.log.keyword.renamed_success', 'renamed successfully'))
                        ):
                            return
                    except Exception:
                        pass
                m = QMenu(tree)
                act_open = QAction(_t(tree, 'ui.open', 'Open'), m)

                def _open():
                    try:
                        # Пытаемся собрать URL из текущих family/lang и selected_ns главного окна
                        ns_manager, family, lang, selected_ns = _resolve_ns_context_from_tree(
                            tree)
                        if not (ns_manager and family and lang):
                            return
                        try:
                            from ..dialogs.template_review_dialog import TemplateReviewDialog
                            host = TemplateReviewDialog.build_host(
                                family, lang)
                        except Exception:
                            return
                        import urllib.parse as _up

                        def _add_prefix(title_base: str, ns_id: int | None) -> str:
                            if not ns_id:
                                return title_base
                            # Не добавляем префикс, если уже есть согласно политике NS
                            try:
                                if ns_manager.has_prefix_by_policy(family, lang, title_base, {ns_id}):
                                    return title_base
                            except Exception:
                                pass
                            try:
                                from ...constants import DEFAULT_EN_NS as _DEN
                                pref = ns_manager.get_policy_prefix(
                                    family, lang, ns_id, _DEN.get(ns_id, '')) if ns_manager else ''
                            except Exception:
                                from ...constants import DEFAULT_EN_NS as _DEN
                                pref = _DEN.get(ns_id, '')
                            return (pref + title_base) if pref else title_base

                        txt = raw_text
                        # Сначала удалим возможные эмодзи
                        if txt[:2] in ('📄 ', '⚛️ ', '🖼️ ', '📁 '):
                            txt = txt[2:].strip()
                        # Для «Источник»: уберём ведущие эмодзи и любой префикс до «Ш:»
                        if col == 5:
                            stripped = _strip_template_source_prefix(tree, txt)
                            if stripped != txt:
                                txt = stripped
                            elif txt[:2] in ('🌐 ', '#️⃣ '):
                                txt = txt[2:].strip()

                        # Колонка 3: заголовок — определяем ns с учетом приоритета выбранного пространства имён
                        if col == 3:
                            ns_id = None
                            # Приоритет 1: Если в комбобоксе выбрано конкретное NS (не "Авто"), используем его
                            if selected_ns is not None and isinstance(selected_ns, int):
                                ns_id = selected_ns
                            # Приоритет 2: Если "Авто" или не определено - автоматически по содержимому
                            elif not selected_ns or (isinstance(selected_ns, str) and selected_ns in ('auto', '')):
                                # Определяем тип объекта по самому заголовку через NamespaceManager
                                obj_type_detected = _detect_object_type_by_ns(
                                    tree, txt)
                                if obj_type_detected == 'template':
                                    ns_id = 10
                                elif obj_type_detected == 'file':
                                    ns_id = 6
                                elif obj_type_detected == 'category':
                                    ns_id = 14
                                # 'article' — обычная статья (ns_id None)
                            full_title = _add_prefix(txt, ns_id)
                        elif col == 4:
                            # Страница (колонка 4): определяем тип по эмодзи,
                            # но оставляем исходный заголовок (полный префикс).
                            txt_base = txt
                            detected_ns = None
                            # Тип берём из исходного текста с эмодзи (до среза).
                            if raw_text.startswith('📁 '):
                                detected_ns = 14  # категория
                            elif raw_text.startswith('⚛️ '):
                                detected_ns = 10  # шаблон/модуль
                            elif raw_text.startswith('🖼️ '):
                                detected_ns = 6   # файл
                            else:
                                detected_ns = None  # статья/прочее

                            # Фолбэк на определение по префиксу (на случай нестандартного формата строки).
                            if detected_ns is None:
                                obj_type_detected = _detect_object_type_by_ns(tree, txt_base)
                                if obj_type_detected == 'template':
                                    detected_ns = 10
                                elif obj_type_detected == 'file':
                                    detected_ns = 6
                                elif obj_type_detected == 'category':
                                    detected_ns = 14

                            # Применяем логику приоритета
                            ns_id = None
                            # Приоритет 1: Если в комбобоксе выбрано конкретное NS (не "Авто"), используем его
                            if selected_ns is not None and isinstance(selected_ns, int):
                                ns_id = selected_ns
                            # Приоритет 2: Если "Авто" - используем определённый тип из эмодзи
                            elif not selected_ns or (isinstance(selected_ns, str) and selected_ns in ('auto', '')):
                                ns_id = detected_ns
                            full_title = _add_prefix(txt_base, ns_id)
                        else:
                            # Источник: если отображается как «Ш:Имя», убираем «Ш:» и подставляем локальный префикс NS-10
                            stripped = _strip_template_source_prefix(tree, txt)
                            if stripped != txt:
                                txt_base = stripped
                                full_title = _add_prefix(txt_base, 10)
                            else:
                                # Используем как есть
                                full_title = txt

                        if not full_title:
                            return
                        url = f"https://{host}/wiki/" + \
                            _up.quote(full_title.replace(' ', '_'))
                        QDesktopServices.openUrl(QUrl(url))
                    except Exception:
                        pass
                act_open.triggered.connect(_open)
                m.addAction(act_open)
                m.exec(tree.viewport().mapToGlobal(pos))
            except Exception:
                pass
        tree.setContextMenuPolicy(Qt.CustomContextMenu)
        tree.customContextMenuRequested.connect(_show_menu)
    except Exception:
        pass


def _auto_expand_columns_for_row(tree: QTreeWidget, row: QTreeWidgetItem) -> None:
    """Расширяет столбцы при необходимости под содержимое добавленной строки.

    Не сужает уже выставленную пользователем ширину и не трогает колонку «Тип».
    """
    try:
        try:
            if bool(tree.property('_wct_disable_auto_expand')):
                return
        except Exception:
            pass
        fm = tree.fontMetrics()
        padding = 2
        vp_w = 0
        try:
            vp_w = tree.viewport().width()
        except Exception:
            try:
                vp_w = tree.width()
            except Exception:
                vp_w = 0
        for col in range(tree.columnCount()):
            if col in (0, 1, 2):
                continue  # не трогаем «Время», «Тип», «Статус»
            try:
                txt = row.text(col) or ''
                # Учитываем метрику текста и особенности эмодзи/иконок
                try:
                    w1 = fm.horizontalAdvance(txt)
                except Exception:
                    w1 = 0
                try:
                    w2 = fm.boundingRect(txt).width()
                except Exception:
                    w2 = 0
                width_needed = max(w1, w2) + padding
                # Небольшой запас на внутренние отступы и возможные отличия метрик эмодзи
                extra = 5
                if col == 3:
                    extra = 6
                try:
                    if txt and (txt[0:2] in ('📄 ', '⚛️ ', '🖼️ ', '📁 ', 'ℹ️ ')):
                        extra += 6
                except Exception:
                    pass
                width_needed += extra
                # Ограничиваем максимальную ширину для широких текстов
                if vp_w:
                    if col == 3:
                        # «Действие или заголовок»: не шире 50% видимой области, но не меньше 380
                        max_w = max(380, int(vp_w * 0.5))
                        if width_needed > max_w:
                            width_needed = max_w
                    elif col == 5:
                        # «Источник»: не шире 35% видимой области
                        max_w2 = max(240, int(vp_w * 0.35))
                        if width_needed > max_w2:
                            width_needed = max_w2
                cur = tree.columnWidth(col)
                if width_needed > cur:
                    tree.setColumnWidth(col, width_needed)
            except Exception:
                pass
    except Exception:
        pass


# ====== LOG HELPERS ======
def make_clear_button(parent_widget, on_click) -> QToolButton:
    """Создаёт кнопку очистки лога со стандартным стилем и поведением."""
    btn = QToolButton()
    btn.setText('🧹')
    btn.setAutoRaise(True)
    btn.setToolTip(_t(parent_widget, 'ui.clear', 'Clear'))
    try:
        btn.setStyleSheet('font-size: 13pt; padding: 0px;')
        btn.setFixedSize(27, 27)
        btn.setCursor(Qt.PointingHandCursor)
    except Exception:
        pass
    try:
        btn.clicked.connect(on_click)
    except Exception:
        pass
    return btn


def _default_log_header_html(parent_widget) -> str:
    return f"<b>{html.escape(_t(parent_widget, 'ui.log', 'Log'))}</b>"


def create_log_wrap(parent_widget, log_widget: QTextEdit, with_header: bool = False, header_text: str | None = None):
    """Создаёт обёртку для QTextEdit-лога с кнопкой очистки в правом нижнем углу.

    Args:
        parent_widget: владелец виджетов
        log_widget: QTextEdit, который будет добавлен внутрь
        with_header: добавить ли заголовок над логом
        header_text: HTML-текст заголовка

    Returns:
        QWidget: контейнер, содержащий лог и кнопку очистки
    """
    wrap = QWidget()
    grid = QGridLayout(wrap)
    try:
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(0)
    except Exception:
        pass

    row = 0
    if with_header:
        try:
            grid.addWidget(QLabel(_ui_translate(parent_widget, header_text or _default_log_header_html(parent_widget))), 0, 0)
            row = 1
        except Exception:
            row = 1

    grid.addWidget(log_widget, row, 0)
    btn_clear = make_clear_button(parent_widget, lambda: log_widget.clear())
    grid.addWidget(btn_clear, row, 0, Qt.AlignBottom | Qt.AlignRight)
    return wrap


# Вариант для QTreeWidget лога (как в RenameTab)
def create_tree_log_wrap(parent_widget, tree_widget: QTreeWidget, with_header: bool = False, header_text: str | None = None):
    """Создаёт обёртку для QTreeWidget-лога с кнопкой очистки в правом нижнем углу."""
    wrap = QWidget()
    grid = QGridLayout(wrap)
    try:
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(0)
    except Exception:
        pass

    row = 0
    if with_header:
        try:
            grid.addWidget(QLabel(_ui_translate(parent_widget, header_text or _default_log_header_html(parent_widget))), 0, 0)
            row = 1
        except Exception:
            row = 1

    grid.addWidget(tree_widget, row, 0)
    btn_clear = make_clear_button(parent_widget, lambda: tree_widget.clear())
    grid.addWidget(btn_clear, row, 0, Qt.AlignBottom | Qt.AlignRight)
    return wrap


# ====== TSV HELPERS ======
def tsv_preview_from_path(path: str) -> tuple[list[str], list[str], int]:
    """Читает TSV-файл и формирует данные для предпросмотра.

    Возвращает кортеж (left, right, count), где:
    - left: список заголовков (первая колонка, без BOM)
    - right: список склеенных хвостов (остальные колонки соединены через «\t»)
    - count: количество валидных строк
    """
    try:
        with open(path, newline='', encoding='utf-8-sig') as f:
            rows = list(csv.reader(f, delimiter='\t'))
    except Exception as e:
        raise e

    left: list[str] = []
    right: list[str] = []
    count = 0
    for r in rows:
        if not r:
            continue
        title = (r[0] or '').lstrip('\ufeff')
        tail = '\t'.join((c or '') for c in r[1:])
        left.append(title)
        right.append(tail)
        count += 1

    return left, right, count


# ====== TSV VALIDATION & COUNT HELPERS ======
def validate_tsv(file_path: str) -> bool:
    """Проверяет, что в TSV есть хотя бы одна валидная строка (≥2 колонки, непустой заголовок)."""
    try:
        with open(file_path, newline='', encoding='utf-8-sig') as f:
            rows = list(csv.reader(f, delimiter='\t'))
        valid_rows = 0
        for row in rows:
            if len(row) >= 2 and (row[0] or '').strip():
                valid_rows += 1
        return valid_rows > 0
    except Exception:
        return False


def check_tsv_format(file_path: str, allow_single_column: bool = False, widget=None) -> tuple[bool, str]:
    """Проверяет корректность TSV для простых табличных операций.

    По умолчанию ожидается минимум 2 колонки. Если `allow_single_column=True`,
    допускается и формат с одной колонкой, но смешанный файл (часть строк 1
    колонка, часть 2+) считается ошибкой.
    """
    try:
        with open(file_path, newline='', encoding='utf-8-sig') as f:
            rows = list(csv.reader(f, delimiter='\t'))
        if not rows:
            return False, _t(widget, 'ui.tsv.empty', 'File is empty')
        one_col_rows = 0
        multi_col_rows = 0
        valid_rows = 0
        for i, row in enumerate(rows):
            if not row or not any((cell or '').strip() for cell in row):
                continue
            if not (row[0] or '').strip():
                return False, _fmt(
                    widget,
                    'ui.tsv.row_empty_first_column',
                    'Row {row}: empty value in the first column',
                    row=i + 1,
                )
            valid_rows += 1
            second_col = (row[1] or '').strip() if len(row) >= 2 else ''
            if second_col:
                multi_col_rows += 1
            else:
                one_col_rows += 1
                if not allow_single_column:
                    return False, _fmt(
                        widget,
                        'ui.tsv.row_not_enough_columns',
                        'Row {row}: not enough columns (need at least 2)',
                        row=i + 1,
                    )
        if valid_rows == 0:
            return False, _t(widget, 'ui.tsv.no_valid_rows', 'File does not contain valid rows')
        if allow_single_column and one_col_rows and multi_col_rows:
            return False, _t(
                widget,
                'ui.tsv.mixed_format',
                'The file contains a mixed format: some rows have one column and others have two. Use a single format.',
            )
        return True, _t(widget, 'ui.tsv.valid_format', 'Format is valid')
    except Exception as e:
        return False, _fmt(widget, 'ui.tsv.read_error', 'File read error: {error}', error=e)


def count_non_empty_titles(file_path: str) -> int:
    """Считает количество строк, где первый столбец непустой."""
    with open(file_path, newline='', encoding='utf-8-sig') as f:
        rows = list(csv.reader(f, delimiter='\t'))
    return sum(1 for r in rows if r and (r[0] or '').strip())


# ====== SUMMARY HELPERS ======
def is_default_summary(text: str, default_fn) -> bool:
    """Проверяет, является ли text пустым или одним из дефолтных значений для стандартных языков."""
    try:
        val = (text or '').strip()
        if not val:
            return True
        langs = ['ru', 'uk', 'be', 'en', 'fr', 'es', 'de']
        return any(val == default_fn(lang_code) for lang_code in langs)
    except Exception:
        return False


# ====== PROGRESS HELPERS ======
def _localized_progress_label(widget, fallback: str = 'Processed') -> str:
    try:
        return translate_key('ui.processed_short', _ui_lang(widget), fallback)
    except Exception:
        return fallback


def init_progress(label_widget, bar_widget, total: int, processed_label: str | None = None) -> None:
    try:
        if processed_label is None:
            processed_label = _localized_progress_label(label_widget or bar_widget)
        total_val = int(total or 0)
        if total_val > 0:
            bar_widget.setMaximum(total_val)
        else:
            bar_widget.setMaximum(1)
        bar_widget.setValue(0)
        try:
            bar_widget.setTextVisible(True)
            bar_widget.setFormat(f'{processed_label} 0/{total_val}')
        except Exception:
            pass
        if label_widget is not None:
            try:
                label_widget.setText(f'{processed_label} 0/{total_val}')
            except Exception:
                pass
    except Exception:
        pass


def inc_progress(label_widget, bar_widget, processed_label: str | None = None) -> None:
    try:
        if processed_label is None:
            processed_label = _localized_progress_label(label_widget or bar_widget)
        val = bar_widget.value() + 1
        bar_widget.setValue(val)
        try:
            bar_widget.setTextVisible(True)
            bar_widget.setFormat(f'{processed_label} {val}/{bar_widget.maximum()}')
        except Exception:
            pass
        if label_widget is not None:
            try:
                label_widget.setText(
                    f'{processed_label} {val}/{bar_widget.maximum()}')
            except Exception:
                pass
    except Exception:
        pass
