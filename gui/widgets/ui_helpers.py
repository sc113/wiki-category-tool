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
            old_cat = (event.get('old_category') or '').strip()
            new_cat = (event.get('new_category') or '').strip()
            cnt_str = (event.get('count_str') or str(
                event.get('count') or '')).strip()
            title_txt = f"ℹ️ Перенос содержимого категории {old_cat} → {new_cat}"
            log_tree_add(tree, ts, old_cat, title_txt, 'manual',
                         status or 'info', cnt_str or None, 'category', True)
            return
        if et == 'destination_exists':
            dst = (event.get('title') or '').strip()
            # Определяем тип объекта для правильного отображения
            obj_type = _detect_object_type_by_ns(tree, dst)
            # В колонку «Страница» помещаем название (независимо от типа)
            title_txt = f"Страница назначения {dst} уже существует."
            log_tree_add(tree, ts, dst, title_txt, 'manual',
                         status or 'info', None, obj_type, True)
            return
    except Exception:
        pass


# Последняя пара переименования для системных сообщений (начало/успех)
_LAST_RENAME_OLD: str | None = None
_LAST_RENAME_NEW: str | None = None


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
    btn.setText('❔')
    btn.setAutoRaise(True)
    btn.setToolTip(text)

    def _show_info_dialog(raw: str):
        try:
            dlg = QDialog(parent_widget)
            dlg.setWindowTitle('Справка')
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
                view.setStyleSheet('QTextBrowser{padding:0;margin:0;}')
                # скрываем скроллы — высоту подгоним по содержимому
                view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
                view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            except Exception:
                pass

            def _build_html(s: str) -> str:
                try:
                    # Простой HTML: экранируем и заменяем переводы строк.
                    html_text = html.escape(s).replace('\n', '<br/>')
                    return f"<div style='font-size:12px; line-height:1.15'>{html_text}</div>"
                except Exception:
                    return html.escape(s).replace('\n', '<br/>')

            view.setHtml(_build_html(raw or ''))
            lay.addWidget(view)
            # Кнопка OK для закрытия
            from PySide6.QtWidgets import QDialogButtonBox
            btns = QDialogButtonBox(QDialogButtonBox.Ok)
            try:
                btns.accepted.connect(dlg.accept)
            except Exception:
                pass
            lay.addWidget(btns)
            # Подгоним высоту окна под содержимое (с ограничением по экрану)
            try:
                base_width = 640
                # Внешние поля layout
                margin_left = margin_right = 8
                margin_top = margin_bottom = 8
                spacing = 4
                # Зафиксируем ширину вёрстки для корректного расчёта высоты
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
                # если есть запас — показываем содержимое полностью; иначе подрезаем под доступную высоту
                visible_view_h = height - \
                    (margin_top + spacing + btn_h + margin_bottom)
                view.setFixedHeight(max(60, visible_view_h))
                dlg.resize(base_width, height)
            except Exception:
                dlg.resize(640, 360)
            dlg.exec()
        except Exception:
            try:
                QMessageBox.information(parent_widget, 'Справка', raw)
            except Exception:
                pass

    btn.clicked.connect(lambda _=None, t=text: _show_info_dialog(t))

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
        parent_widget, 'Выберите файл', filter=f'Files ({pattern})')
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
        parent_widget, 'Куда сохранить', filter=f'*.{default_ext.lstrip(".")}')
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
            QMessageBox.warning(parent_widget, 'Ошибка',
                                'Сначала укажите путь к файлу.')
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
            QMessageBox.warning(parent_widget, 'Ошибка',
                                'Файл не найден: ' + path)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(path)))
    except Exception as e:
        QMessageBox.warning(parent_widget, 'Ошибка',
                            f'Не удалось открыть файл: {e}')


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

            # Извлечь тип сущности в начале сообщения, если есть
            typ = None
            tail = rest
            m2 = re.match(
                r'^(?P<typ>статья|страница|шаблон|модуль|файл)\b\s*(?P<tail>.*)', rest, flags=re.I)
            if m2:
                typ = (m2.group('typ') or '').lower()
                tail = (m2.group('tail') or '').strip()

            folder_emoji = '📁'
            # Эмодзи по типу элемента
            item_emoji = '📄'
            if typ in ('шаблон', 'модуль'):
                item_emoji = '⚛️'
            elif typ in ('файл', 'изображение'):
                item_emoji = '🖼️'

            low_tail = tail.lower()
            # Эмодзи статуса
            status_emoji = ''
            if 'ошибка' in low_tail:
                status_emoji = '❌'
            elif 'пропущено' in low_tail:
                status_emoji = '⏭️'
            elif 'перенес' in low_tail:
                status_emoji = '✅'
            elif 'переименован' in low_tail or 'переименована' in low_tail:
                status_emoji = '🔁'
            elif 'создано' in low_tail:
                status_emoji = '🆕'
            elif 'записано' in low_tail:
                status_emoji = '💾'
            elif 'не существует' in low_tail:
                status_emoji = '⚠️'
            elif 'уже существует' in low_tail:
                status_emoji = 'ℹ️'
            elif 'готово' in low_tail:
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


def log_message(widget: QTextEdit, msg: str, debug_func=None):
    """Log a message to a QTextEdit widget with color formatting.

    Args:
        widget: QTextEdit widget to log to
        msg: Message to log
        debug_func: Optional debug function to call (e.g., from utils.debug)
    """
    if debug_func:
        debug_func(msg)

    lower = msg.lower()
    color = None
    if 'ошибка' in lower or 'не найдено' in lower:
        color = 'red'
    elif 'не существует' in lower:
        # тёмный оранжевый для статуса "не существует"
        color = '#cc6a00'
    elif 'уже существует' in lower:
        # тёмно-жёлтый для статуса "уже существует"
        color = '#b8860b'
    elif 'перенесена' in lower:
        # тёмно-синий для успешно перенесённых участников категории/статей
        color = '#1e3a8a'
    elif any(k in lower for k in ('записано', 'создано', 'переименована', 'готово')):
        # более тёмный зелёный для лучшей читаемости на светлой теме
        color = '#2e7d32'

    # Преобразуем переводы строк в <br/>, чтобы многострочные сообщения корректно отображались в HTML
    def _html_lines(s: str) -> str:
        try:
            return s.replace('\n', '<br/>')
        except Exception:
            return s

    # Префикс времени [HH:MM:SS] с разделителем
    try:
        ts = datetime.now().strftime('%H:%M:%S')
        prefix = f"[{ts}] "
    except Exception:
        prefix = ''

    # Попробовать преобразовать в красивый формат
    formatted, escaped = pretty_format_msg(msg)
    text_to_show = (html.escape(prefix) +
                    formatted) if escaped else (prefix + formatted)

    if color:
        widget.append(
            f"<span style='color:{color}'>" + _html_lines(text_to_show) + "</span>")
    else:
        widget.append(_html_lines(text_to_show))


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
        base = max(sh_start, ratio * sh_stop)
        stop_w = max(sh_stop, (base + ratio - 1) // ratio)
        start_w = ratio * stop_w
        stop_btn.setFixedWidth(stop_w)
        start_btn.setFixedWidth(start_w)
    except Exception:
        # Fallback to fixed sizes
        stop_btn.setFixedWidth(100)
        start_btn.setFixedWidth(300)


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
    status_label.setText('Авторизовано' if ok else 'Авторизация (pywikibot)')

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
    'success': {'emoji': '✅', 'color': '#0f766e', 'label': 'Успешно'},
    'skipped': {'emoji': '⏭️', 'color': '#6b7280', 'label': 'Пропущено'},
    'error': {'emoji': '❌', 'color': '#ef4444', 'label': 'Ошибка'},
    'not_found': {'emoji': '⚠️', 'color': '#f97316', 'label': 'Не найдено'},
    'info': {'emoji': 'ℹ️', 'color': '#3b82f6', 'label': 'Инфо'},
}

MODE_INFO = {
    'auto': {'emoji': '⚡', 'label': 'Автоподтверждение замены параметров в шаблоне'},
    'manual': {'emoji': '✍️', 'label': 'Ручное подтверждение замены параметров в шаблоне'},
    # Отдельный тип для прямого переноса — не комбинируется с auto/manual
    'direct': {'emoji': '📝', 'label': 'Прямой замена категорий на странице'},
}

OBJ_INFO = {
    'article': {'emoji': '📄', 'label': 'Статья'},
    'template': {'emoji': '⚛️', 'label': 'Шаблон'},
    'file': {'emoji': '🖼️', 'label': 'Файл'},
    'category': {'emoji': '📁', 'label': 'Категория'},
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
    if any(k in lt for k in ('template:', 'module:')):
        return 'template'
    if any(k in lt for k in ('file:', 'image:')):
        return 'file'
    if any(k in lt for k in ('category:',)):
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
    # Фолбэк: проверяем известные префиксы и добавляем русский по умолчанию
    cl = cat.lower()
    if cl.startswith('категория:') or cl.startswith('category:'):
        return cat
    # Проверяем наличие любого ns-подобного префикса (слово + двоеточие)
    if re.match(r'^[a-zA-Zа-яА-ЯіїєґІЇЄҐёЁ]+:', cat):
        return cat
    return f"Категория:{cat}"


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
        tree.setHeaderLabels(
            ['Время', 'Тип', 'Статус', 'Действие или заголовок', 'Страница', 'Источник'])
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
            hdr.setMinimumSectionSize(1)
            for i in range(6):
                hdr.setSectionResizeMode(i, QHeaderView.Interactive)
        except Exception:
            pass
        # Фиксируем базовые узкие столбцы
        try:
            # Время — фиксированная ширина по формату 00:00:00 (ровно по цифрам)
            from PySide6.QtCore import Qt as _Qt
            hdr.setSectionResizeMode(0, QHeaderView.Fixed)
            fm0 = tree.fontMetrics()
            # Ровно по цифрам +6 px (чуть шире для читабельности)
            time_w = fm0.horizontalAdvance('00:00:00') + 6
            tree.setColumnWidth(0, time_w)

            # Сузим колонку «Тип» фиксированной шириной с центровкой (по ширине иконок)
            hdr.setSectionResizeMode(1, QHeaderView.Fixed)
            try:
                emoji_samples = ['⚡', '✍️', '📝', '⚙️']
                emoji_w = max(fm0.horizontalAdvance(e) for e in emoji_samples)
            except Exception:
                emoji_w = 16
            try:
                fmh = hdr.fontMetrics() if hasattr(hdr, 'fontMetrics') else tree.fontMetrics()
                header_txt = (tree.headerItem().text(1) or '')
                head_w = fmh.horizontalAdvance(header_txt)
            except Exception:
                head_w = 18
            t_w = max(emoji_w, head_w) + 1
            # Минимальная защита, чтобы колонка не схлопывалась
            t_w = max(20, t_w + 3)  # добавим ещё немного ширины
            tree.setColumnWidth(1, t_w)
            tree.headerItem().setTextAlignment(1, _Qt.AlignHCenter)

            # Статус — фиксированная ширина по самому длинному тексту статуса
            hdr.setSectionResizeMode(2, QHeaderView.Fixed)
            try:
                fm2 = tree.fontMetrics()
                # Ориентируемся на «⏭️ Пропущено» — самые короткие
                status_sample = f"{STATUS_INFO['skipped']['emoji']} {STATUS_INFO['skipped']['label']}"
                status_w = fm2.horizontalAdvance(status_sample) + 2
                tree.setColumnWidth(2, max(status_w, tree.columnWidth(2)))
            except Exception:
                pass
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
        # Более светлая подсветка выбора/строк
        try:
            tree.setStyleSheet(
                "QTreeWidget{alternate-background-color:#fafafa;}"
                "QTreeWidget::item:selected{background:#eef6ff;color:inherit;}"
                "QTreeWidget::item{selection-background-color:#eef6ff;}"
            )
        except Exception:
            pass
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
        mode_info = MODE_INFO.get(mode, MODE_INFO['manual'])
        st = STATUS_INFO.get(status, STATUS_INFO['success'])
        # Тип для колонки «Тип» (режим/прямой перенос) берём из object_type аргумента,
        # а иконку в заголовке определяем ТОЛЬКО по префиксу самого title
        obj_meta = OBJ_INFO.get(object_type or 'article', OBJ_INFO['article'])
        type_emoji_meta = obj_meta
        title_obj_type = _detect_object_type_by_ns(tree, title)
        title_meta = OBJ_INFO.get(
            title_obj_type or 'article', OBJ_INFO['article'])
        obj_emoji = title_meta['emoji']

        status_text = f"{st['emoji']} {st['label']}"
        # Типодин тип. Для системных — ⚙️. Для прямого переноса — 📝.
        # Для изменений в шаблонах — режим (⚡/✍️). Комбинаций типа "✍️ + 📝" больше нет.
        if system:
            action_cell = '⚙️'
        elif object_type == 'template':
            action_cell = MODE_INFO['auto']['emoji'] if (
                mode == 'auto') else MODE_INFO['manual']['emoji']
        else:
            action_cell = MODE_INFO['direct']['emoji']
        # Значок объекта переносим в начало заголовка.
        if status == 'info':
            low_title = (title or '').lower()
            if 'пропущено' in low_title:
                title_cell = f"⏭️ {title or ''}"
            elif 'остановлено' in low_title or 'остановлен' in low_title:
                title_cell = f"⏹️ {title or ''}"
            elif 'уже существ' in low_title:
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
                is_partial_src = ('частично' in low_base)
                is_loc_src = ('локатив' in low_base)
                # Уберём текстовые пометки из отображаемого имени
                try:
                    base_disp = base.replace('[частично]', '').replace(
                        '[локатив]', '').strip()
                except Exception:
                    base_disp = base
                # Для частичных совпадений и локативов используем отдельные символы источника
                # Полные совпадения: ⚛️ Ш:Имя; Частичные: #️⃣ Ш:Имя; Локативы: 🌐 Ш:Имя
                src_emoji = '🌐' if is_loc_src else (
                    '#️⃣' if is_partial_src else OBJ_INFO['template']['emoji'])
                src_cell = f"{src_emoji} Ш:{base_disp}"
                # ToolTip для источника
                try:
                    if is_loc_src:
                        src_tooltip = '🌐 Исправление категоризации через части названия в параметрах с применением локативов (смена падежей)'
                    elif is_partial_src:
                        src_tooltip = '#️⃣ Исправление категоризации через части названия в параметрах'
                    else:
                        src_tooltip = '⚛️ Исправление категоризации через полное указание названия категории в параметре шаблона'
                except Exception:
                    src_tooltip = ''
        except Exception:
            src_cell = source or ''
            src_tooltip = ''
        # Колонка «Страница» — отображение с иконкой и коротким префиксом по типу объекта
        try:
            page_txt = (page or '').strip()
            if not page_txt:
                page_cell = ''
            else:
                # Убираем префикс пространства имён из полного названия
                page_disp = page_txt.split(
                    ':', 1)[-1] if ':' in page_txt else page_txt
                # Определяем эмодзи и короткий префикс по object_type
                if object_type == 'category':
                    page_cell = f"{OBJ_INFO['category']['emoji']} К:{page_disp}"
                elif object_type == 'template':
                    page_cell = f"{OBJ_INFO['template']['emoji']} Ш:{page_disp}"
                elif object_type == 'file':
                    page_cell = f"{OBJ_INFO['file']['emoji']} Ф:{page_disp}"
                else:  # article
                    page_cell = f"{OBJ_INFO['article']['emoji']} {page_disp}"
        except Exception:
            page_cell = page or ''
        row = QTreeWidgetItem(
            [timestamp, action_cell, status_text, title_cell, page_cell, src_cell])
        # Цвет статуса
        try:
            from PySide6.QtGui import QBrush, QColor
            row.setForeground(2, QBrush(QColor(st['color'])))
        except Exception:
            pass
        # Tooltips для колонок с эмодзи
        try:
            if system:
                row.setToolTip(1, '⚙️ Системное сообщение')
            elif object_type == 'template':
                row.setToolTip(1, f"{MODE_INFO.get('auto' if mode == 'auto' else 'manual')['emoji']} "
                               f"{MODE_INFO.get('auto' if mode == 'auto' else 'manual')['label']}")
            else:
                row.setToolTip(
                    1, f"{MODE_INFO['direct']['emoji']} {MODE_INFO['direct']['label']}")
            row.setToolTip(2, f"{st['emoji']} {st['label']}")
            # Отключаем подсказки для 3 и 4 колонок
            row.setToolTip(3, '')
            row.setToolTip(4, '')
            # Подсказка для «Источник»: различаем полное/частичное исправление
            try:
                if src_tooltip:
                    row.setToolTip(5, src_tooltip)
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
            plain = _re0.sub(r'<[^>]+>', '', s)
            # Начало переименования: «Начинаем переименование: Old → New»
            m_begin = _re0.search(
                r"Начинаем\s+переименовани[ея]:\s*(?P<old>.+?)\s*→\s*(?P<new>.+)$", plain)
            if m_begin:
                try:
                    global _LAST_RENAME_OLD, _LAST_RENAME_NEW
                    _LAST_RENAME_OLD = (m_begin.group('old') or '').strip()
                    _LAST_RENAME_NEW = (m_begin.group('new') or '').strip()
                except Exception:
                    pass
                # Определяем тип объекта для правильного отображения
                obj_type = _detect_object_type_by_ns(tree, _LAST_RENAME_OLD)
                # В колонку «Страница» помещаем старое имя (независимо от типа)
                title_txt = f"Начинаем переименование: {_LAST_RENAME_OLD} → {_LAST_RENAME_NEW}"
                log_tree_add(tree, ts, _LAST_RENAME_OLD, title_txt,
                             'manual', 'success', None, obj_type, True)
                return
            # Завершение: «Переименовано успешно — …»
            if plain.startswith('Переименовано успешно'):
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
            cat = (m.group('cat') or '').strip()
            title = (m.group('title') or '').strip()
            status_text = (m.group('status') or '').strip().lower()
            source = (m.group('src') or '').strip() or None

            # Режим: авто/ручное
            mode = 'auto' if 'автоматичес' in status_text else 'manual'
            # Статус
            if any(k in status_text for k in ('ошиб', 'error')):
                status = 'error'
            elif any(k in status_text for k in ('не найден', 'не сущест')):
                status = 'not_found'
            elif any(k in status_text for k in ('пропущ', 'отмен')):
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

        # 3) Специальные системные сообщения от воркера без эмодзи/ссылок
        # "Категория <b>Old</b> не существует и не содержит страниц."
        m_nf_empty = re.search(
            r"Категория\s*<b>(?P<cat>[^<]+)</b>\s*не существует\s*и\s*не содержит страниц", s, re.I)
        if m_nf_empty:
            cat = (m_nf_empty.group('cat') or '').strip()
            title = f"{_fmt_cat_with_ns(tree, cat)} — не существует и не содержит страниц"
            log_tree_add(tree, ts, None, title, 'manual',
                         'not_found', 'API', 'category', True)
            return
        # "Категория <b>Old</b> не существует"
        m_nf = re.search(
            r"Категория\s*<b>(?P<cat>[^<]+)</b>\s*не существует", s, re.I)
        if m_nf:
            cat = (m_nf.group('cat') or '').strip()
            title = f"{_fmt_cat_with_ns(tree, cat)} — не существует"
            log_tree_add(tree, ts, None, title, 'manual',
                         'not_found', 'API', 'category', True)
            return
        # Новый формат: "<b>Категория:Name</b> не найдена." или любой <b>Title</b> не найден(а/о)
        m_nf_generic = re.search(
            r"<b>(?P<title>[^<]+)</b>\s*не найден[ао]\.?", s, re.I)
        if m_nf_generic:
            title0 = (m_nf_generic.group('title') or '').strip()
            obj_type = _detect_object_type_by_ns(tree, title0)
            # Если это категория — продублируем в соответствующую колонку
            cat_col = title0 if obj_type == 'category' else None
            # Статусный хвост по роду объекта
            if obj_type == 'category':
                tail = 'не найдена'
            elif obj_type == 'file':
                tail = 'не найден'
            elif obj_type == 'template':
                tail = 'не найден'
            else:
                tail = 'не найдено'
            title_txt = f"{title0} — {tail}"
            log_tree_add(tree, ts, cat_col, title_txt, 'manual',
                         'not_found', None, obj_type, False)
            return
        # "Категория <b>Old</b> пуста."
        m_empty = re.search(
            r"Категория\s*<b>(?P<cat>[^<]+)</b>\s*пуста\.?", s, re.I)
        if m_empty:
            cat = (m_empty.group('cat') or '').strip()
            title = f"{_fmt_cat_with_ns(tree, cat)} — пустая"
            log_tree_add(tree, ts, None, title, 'manual',
                         'skipped', 'API', 'category', True)
            return
        # 4) Универсальный фолбэк: вывести строку целиком в таблицу без потери информации
        try:
            # Оценим статус/режим/объект эвристически
            s_lower = s.lower()
            status = 'success'
            # Сообщения с префиксом ℹ️ считаем информационными
            if s.strip().startswith('ℹ️'):
                status = 'info'
            if 'остановлено' in s_lower:
                status = 'info'
            if any(k in s_lower for k in ('ошиб', 'error', 'traceback')):
                status = 'error'
            elif any(k in s_lower for k in ('не найден', 'не существует')):
                status = 'not_found'
            elif 'пропущ' in s_lower:
                status = 'skipped'
            elif 'уже существ' in s_lower:
                status = 'info'
            # Спец-случай: «Пропущено переименование категории … Переносим содержимое…» — это информационная строка
            if 'пропущено переименование категории' in s_lower:
                status = 'info'
            mode = 'auto' if 'автоматичес' in s_lower else 'manual'
            # Удалим HTML-теги, но сохраним текст
            import re as _re
            plain = _re.sub(r'<[^>]+>', '', s)
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


def log_tree_help_html() -> str:
    """Возвращает HTML-справку по обозначениям лога (эмодзи и цвета)."""
    try:
        # Небольшая табличка-легенда
        rows = [
            (STATUS_INFO['success'], 'Успешно выполнено'),
            (STATUS_INFO['skipped'], 'Пропущено'),
            (STATUS_INFO['error'], 'Ошибка'),
            (STATUS_INFO['not_found'], 'Не найдено'),
        ]

        def _row(s):
            return (f"<tr>"
                    f"<td style='padding:4px 8px'>{s['emoji']}</td>"
                    f"<td style='padding:4px 8px'><span style='color:{s['color']}'><b>{s['label']}</b></span></td>"
                    f"</tr>")
        status_table = "".join(_row(s)
                               for s, _ in [(r[0], r[1]) for r in rows])
        mode_rows = (
            f"<tr><td style='padding:4px 8px'>{MODE_INFO['auto']['emoji']}</td><td style='padding:4px 8px'><b>{MODE_INFO['auto']['label']}</b> — автоматический режим</td></tr>"
            f"<tr><td style='padding:4px 8px'>{MODE_INFO['manual']['emoji']}</td><td style='padding:4px 8px'><b>{MODE_INFO['manual']['label']}</b> — ручной режим</td></tr>"
        )
        html_text = (
            "<div style='font-size:12px;line-height:1.35'>"
            "<h3 style='margin:6px 0'>Легенда лога</h3>"
            "<p>Время всегда отображается первым столбцом. Записи сгруппированы по категориям (корневые узлы дерева)."
            "Столбцы: <b>Время</b> • <b>Событие</b> • <b>Статус</b>.</p>"
            "<h4 style='margin:6px 0'>Статусы</h4>"
            f"<table style='border-collapse:collapse'>{status_table}</table>"
            "<h4 style='margin:6px 8px 4px 0'>Режимы</h4>"
            f"<table style='border-collapse:collapse'>{mode_rows}</table>"
            "<p>Пример события: <code>⚡ Заголовок (Шаблон:Категории)</code> — автоматическая операция над страницей.</p>"
            "</div>"
        )
        return html_text
    except Exception:
        return 'Легенда недоступна'


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
    - Для «Страница» используем тип из эмодзи (📄/⚛️/🖼️/📁) и применяем логику приоритета NS.
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
                        if 'Инфо' in st or 'ℹ' in st:
                            return
                except Exception:
                    pass
                # Дополнительно блокируем текстовые «служебные» строки — только для колонки 3
                if col == 3:
                    raw_all = ' '.join([(item.text(i) or '') for i in range(
                        tree.columnCount())]).strip().lower()
                    if 'пропущено переименование категории' in raw_all:
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
                        if _re.match(r'^\s*\d+\s*(страниц(?:а|ы)?|строк(?:а|и)?)?\s*$', txt_plain, _re.I):
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
                        if ('→' in ttxt) or (' — ' in ttxt) or low_t.startswith('начинаем переимен') or low_t.startswith('переименовано успешно'):
                            return
                    except Exception:
                        pass
                m = QMenu(tree)
                act_open = QAction('Открыть', m)

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
                            try:
                                idx = txt.find('Ш:')
                            except Exception:
                                idx = -1
                            if idx != -1:
                                txt = txt[idx:].strip()
                            else:
                                # Фолбэк: попытка срезать известные эмодзи источника
                                if txt[:2] in ('🌐 ', '#️⃣ '):
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
                            # Страница (колонка 4): определяем тип по эмодзи и убираем префикс
                            txt_base = txt
                            detected_ns = None
                            # Удаляем эмодзи и короткий префикс, определяя тип
                            if txt.startswith('📁 К:'):
                                txt_base = txt[4:].strip()  # "📁 К:Имя" → "Имя"
                                detected_ns = 14  # категория
                            elif txt.startswith('⚛️ Ш:'):
                                # "⚛️ Ш:Имя" → "Имя"
                                txt_base = txt[4:].strip()
                                detected_ns = 10  # шаблон
                            elif txt.startswith('🖼️ Ф:'):
                                # "🖼️ Ф:Имя" → "Имя"
                                txt_base = txt[4:].strip()
                                detected_ns = 6  # файл
                            elif txt.startswith('📄 '):
                                txt_base = txt[2:].strip()  # "📄 Имя" → "Имя"
                                detected_ns = None  # статья

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
                            if txt.startswith('Ш:'):
                                txt_base = txt[2:].strip()
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
    btn.setToolTip('<span style="font-size:12px">Очистить</span>')
    try:
        btn.setStyleSheet('font-size: 15pt; padding: 0px;')
        btn.setFixedSize(32, 32)
        btn.setCursor(Qt.PointingHandCursor)
    except Exception:
        pass
    try:
        btn.clicked.connect(on_click)
    except Exception:
        pass
    return btn


def create_log_wrap(parent_widget, log_widget: QTextEdit, with_header: bool = False, header_text: str = '<b>Лог выполнения:</b>'):
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
            grid.addWidget(QLabel(header_text), 0, 0)
            row = 1
        except Exception:
            row = 1

    grid.addWidget(log_widget, row, 0)
    btn_clear = make_clear_button(parent_widget, lambda: log_widget.clear())
    grid.addWidget(btn_clear, row, 0, Qt.AlignBottom | Qt.AlignRight)
    return wrap


# Вариант для QTreeWidget лога (как в RenameTab)
def create_tree_log_wrap(parent_widget, tree_widget: QTreeWidget, with_header: bool = False, header_text: str = '<b>Лог выполнения:</b>'):
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
            grid.addWidget(QLabel(header_text), 0, 0)
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


def check_tsv_format(file_path: str) -> tuple[bool, str]:
    """Возвращает (ok, msg) — корректен ли формат TSV (≥2 колонки и непустой заголовок в каждой строке)."""
    try:
        with open(file_path, newline='', encoding='utf-8-sig') as f:
            rows = list(csv.reader(f, delimiter='\t'))
        if not rows:
            return False, 'Файл пуст'
        for i, row in enumerate(rows):
            if len(row) < 2:
                return False, f'Строка {i+1}: недостаточно колонок (нужно минимум 2)'
            if not (row[0] or '').strip():
                return False, f'Строка {i+1}: пустое название страницы'
        return True, 'Формат корректен'
    except Exception as e:
        return False, f'Ошибка чтения файла: {e}'


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
        return any(val == default_fn(l) for l in langs)
    except Exception:
        return False


# ====== PROGRESS HELPERS ======
def init_progress(label_widget, bar_widget, total: int, processed_label: str = 'Обработано') -> None:
    try:
        if total and total > 0:
            bar_widget.setMaximum(total)
        else:
            bar_widget.setMaximum(1)
        bar_widget.setValue(0)
        try:
            label_widget.setText(f'{processed_label} 0/{total}')
        except Exception:
            pass
    except Exception:
        pass


def inc_progress(label_widget, bar_widget, processed_label: str = 'Обработано') -> None:
    try:
        val = bar_widget.value() + 1
        bar_widget.setValue(val)
        try:
            label_widget.setText(
                f'{processed_label} {val}/{bar_widget.maximum()}')
        except Exception:
            pass
    except Exception:
        pass
