"""
Утилитарные функции и классы для Wiki Category Tool.

Содержит:
- Класс для перенаправления stdout/stderr в GUI
- Функции для работы с путями и ресурсами
- Функции для нормализации имен пользователей
- Функции для создания стандартных комментариев к правкам
- UI утилиты для работы с комбобоксами
- Систему логирования и отладки
- Функции для работы с файлами TSV
"""

import sys
import os
import io
import csv
from datetime import datetime
from threading import Lock, local
try:
    from PySide6.QtCore import QObject, Signal
except Exception:  # PySide6 может быть недоступен при импорт-тестах
    QObject = object  # type: ignore

    class Signal:  # type: ignore
        def __init__(self, *_, **__):
            pass

        def connect(self, *_args, **_kwargs):
            pass

        def emit(self, *_args, **_kwargs):
            pass

# Глобальные переменные для системы логирования
DEBUG_BUFFER = []
DEBUG_VIEW = None

# Блокировка для записи в файлы
write_lock = Lock()
_DBG_TLS = local()


class _DebugBridge(QObject):
    """Потокобезопасный мост для передачи debug-сообщений в GUI через сигнал."""
    try:
        message = Signal(str)
    except Exception:
        # Заглушка если нет Qt
        def message(self, *_args, **_kwargs):  # type: ignore
            pass


DEBUG_BRIDGE = _DebugBridge()


def get_debug_bridge():
    """Вернуть глобальный мост для debug-сообщений."""
    return DEBUG_BRIDGE


class GuiStdWriter(io.TextIOBase):
    """Класс для перенаправления stdout/stderr в GUI приложение."""

    def write(self, s: str) -> int:
        try:
            if s and s.strip():
                dbg = globals().get('debug')
                if callable(dbg):
                    dbg(str(s).rstrip())
        except Exception:
            pass
        return len(s or '')

    def flush(self) -> None:
        pass


def resource_path(relative: str) -> str:
    """Возвращает абсолютный путь к ресурсу, работает для dev и PyInstaller onefile."""
    try:
        base_path = getattr(sys, '_MEIPASS', None)
        if base_path and os.path.exists(base_path):
            return os.path.join(base_path, relative)
    except Exception:
        pass
    try:
        base = os.path.dirname(__file__)
    except Exception:
        base = os.getcwd()
    return os.path.join(base, relative)


def tool_base_dir() -> str:
    """Возвращает базовую директорию инструмента."""
    try:
        return os.path.dirname(__file__)
    except NameError:
        return os.getcwd()


def write_row(title, lines, writer):
    """Записывает строку в TSV файл с блокировкой потоков."""
    if not lines:
        return
    with write_lock:
        writer.writerow([title, *lines])


def normalize_username(name: str | None) -> str:
    """Нормализует имя пользователя для сравнения."""
    if not name:
        return ''
    return name.strip().replace('_', ' ').casefold()


# ==============================
# Нормализация пробелов/невидимых
# ==============================
def strip_invisible_marks(text: str | None) -> str:
    """Удаляет невидимые маркеры (LRM/RLM/ZWSP и пр.) и BOM.

    Возвращает исходную строку без символов:
    \u200B-\u200F, \u202A-\u202E, \u2066-\u2069, \uFEFF
    """
    if text is None:
        return ''
    try:
        import re as _re
        # Удаляем невидимые форматные символы, включая WORD JOINER (\u2060) и SOFT HYPHEN (\u00AD)
        return _re.sub(r"[\u00AD\u200B-\u200F\u202A-\u202E\u2060\u2066-\u2069\uFEFF]", "", text)
    except Exception:
        return text


def replace_unicode_spaces(text: str | None) -> str:
    """Заменяет все варианты юникод‑пробелов на обычный пробел.

    Включая: NBSP (\u00A0), NNBSP (\u202F), FIGURE SPACE (\u2007),
    EN/EM/THIN/HAIR/… (\u2000-\u200A), \u205F, \u3000.
    """
    if text is None:
        return ''
    try:
        import re as _re
        return _re.sub(r"[\s\u00A0\u202F\u1680\u2000-\u200A\u2007\u205F\u3000]", " ", text)
    except Exception:
        return text


def normalize_spaces_for_compare(text: str | None) -> str:
    """Нормализует строку для сравнения:
    1) убирает невидимые маркеры
    2) приводит все пробелы к обычному пробелу
    3) схлопывает повторные пробелы и обрезает края
    """
    if text is None:
        return ''
    try:
        import re as _re
        s = strip_invisible_marks(text)
        s = replace_unicode_spaces(s)
        s = _re.sub(r"\s+", " ", s)
        return (s or '').strip()
    except Exception:
        return (text or '').strip()


def align_first_letter_case(source: str, target: str) -> str:
    """Делает первую букву target соответствующей регистру первой буквы source.

    Если source начинается с заглавной буквы, а target с маленькой (или наоборот),
    изменяет первый символ target так, чтобы он соответствовал source, 
    оставляя остальную часть target без изменений. Безопасно для пустых строк.
    """
    try:
        s0 = (source or '')[:1]
        if not target:
            return target or ''
        t0 = target[:1]
        tail = target[1:]
        if not s0 or not t0:
            return target
        if s0.islower() and t0.isupper():
            return t0.lower() + tail
        if s0.isupper() and t0.islower():
            return t0.upper() + tail
        return target
    except Exception:
        return target


def build_ws_fuzzy_pattern(text: str) -> str:
    """Строит regex-паттерн для текста с учётом всех видов пробелов и невидимых.

    Используется для поиска фраз в вики‑ссылках/параметрах, где между словами
    могут появляться NBSP/NNBSP и невидимые символы.
    """
    try:
        import re as _re
        tokens = [t for t in _re.split(r"\s+", (text or '').strip()) if t]
        if not tokens:
            return _re.escape((text or ''))
        # Класс пробелов и невидимых между токенами
        invis = r"[\u200B-\u200F\u202A-\u202E\u2066-\u2069\uFEFF]"
        spaces = r"[\s\u00A0\u202F\u1680\u2000-\u200A\u2007\u205F\u3000]"
        sep = rf"(?:{invis}*{spaces}{invis}*)+"
        return sep.join(_re.escape(t) for t in tokens)
    except Exception:
        return text


def default_summary(lang: str) -> str:
    """Возвращает стандартный комментарий для правок в зависимости от языка."""
    mapping = {
        'ru': 'Замена содержимого страницы на единообразное наполнение: $1',
        'uk': 'Заміна вмісту сторінки на одноманітне наповнення: $1',
        'be': 'Замена зместу старонкі на адзіную структуру: $1',
        'en': 'Replacement of the page content with uniform filling: $1',
        'fr': 'Remplacement du contenu pour cohérence: $1',
        'es': 'Sustitución del contenido para uniformidad: $1',
        'de': 'Ersetzung des Seiteninhalts für Konsistenz: $1'
    }
    return mapping.get(lang, 'Consistency content replacement: $1')


def default_create_summary(lang: str) -> str:
    """Возвращает стандартный комментарий для создания страниц в зависимости от языка."""
    mapping = {
        'ru': 'Создание новой категории с заготовленным содержимым: $1',
        'uk': 'Створення нової категорії з уніфікованим наповненням: $1',
        'be': 'Стварэнне новай катэгорыі з адзінай структурай: $1',
        'en': 'Creation of a new category with prepared content: $1',
        'fr': 'Création d\'une nouvelle catégorie avec contenu préparé: $1',
        'es': 'Creación de una nueva categoría con contenido preparado: $1',
        'de': 'Erstellung einer neuen Kategorie mit vorbereitetem Inhalt: $1'
    }
    return mapping.get(lang, 'Category creation with prepared content: $1')


def adjust_combo_popup_width(combo) -> None:
    """Настройка ширины выпадающего списка комбобокса."""
    try:
        view = combo.view()
        fm = view.fontMetrics() if hasattr(view, 'fontMetrics') else combo.fontMetrics()
        max_w = 0
        for i in range(combo.count()):
            text = combo.itemText(i) or ''
            w = fm.horizontalAdvance(text)
            if w > max_w:
                max_w = w
        max_w += 48
        try:
            view.setMinimumWidth(max_w)
        except Exception:
            pass
    except Exception:
        pass


def debug(msg: str):
    """Добавляет сообщение в буфер отладки с временной меткой."""
    global DEBUG_VIEW
    ts = datetime.now().strftime('%H:%M:%S')
    formatted_msg = f"[{ts}] {msg}"
    DEBUG_BUFFER.append(formatted_msg)
    # Защита от реэнтрантности (например, при ошибках вывода stderr)
    try:
        if getattr(_DBG_TLS, 'in_debug', False):
            return
        _DBG_TLS.in_debug = True
        try:
            # Отправляем в GUI через сигнал (безопасно из фоновых потоков)
            get_debug_bridge().message.emit(formatted_msg)
        finally:
            _DBG_TLS.in_debug = False
    except Exception:
        try:
            _DBG_TLS.in_debug = False
        except Exception:
            pass
        pass


def setup_gui_stdout_redirect():
    """Настраивает перенаправление stdout/stderr для GUI приложения."""
    if getattr(sys, 'stdout', None) is None:
        sys.stdout = GuiStdWriter()
    if getattr(sys, 'stderr', None) is None:
        sys.stderr = GuiStdWriter()


def get_debug_buffer():
    """Возвращает текущий буфер отладки."""
    return DEBUG_BUFFER


def clear_debug_buffer():
    """Очищает буфер отладки."""
    DEBUG_BUFFER.clear()


def set_debug_view(view):
    """Устанавливает виджет для отображения отладочных сообщений."""
    global DEBUG_VIEW
    DEBUG_VIEW = view


def get_debug_view():
    """Возвращает текущий виджет отладки."""
    return DEBUG_VIEW


def get_debug_view_ref():
    """Возвращает ссылку на DEBUG_VIEW как список для передачи в диалоги."""
    global DEBUG_VIEW
    return [DEBUG_VIEW]


# ==========================
# Русское склонение по числу
# ==========================
def ru_plural(n: int, form1: str, form2: str, form5: str) -> str:
    """Возвращает корректную форму существительного по числительному.

    Правило (русский):
    - 1, 21, 31, ... → form1 (например: "страница")
    - 2–4, 22–24, 32–34, ... → form2 ("страницы")
    - 0, 5–20, 25–30, ... → form5 ("страниц")

    Args:
        n: Число
        form1: Форма для 1 (ед.ч., напр. "страница" или в винительном "страницу")
        form2: Форма для 2–4 (мн.ч., напр. "страницы")
        form5: Форма для 5+ (род. мн.ч., напр. "страниц")

    Returns:
        Строка с корректной формой.
    """
    try:
        n_abs = abs(int(n))
    except Exception:
        # На случай некорректного ввода — не падаем
        n_abs = 0
    n10 = n_abs % 10
    n100 = n_abs % 100
    if n10 == 1 and n100 != 11:
        return form1
    if 2 <= n10 <= 4 and not (12 <= n100 <= 14):
        return form2
    return form5


def format_russian_pages_nominative(n: int) -> str:
    """Возвращает строку вида "N страница/страницы/страниц" (именительный падеж)."""
    return f"{n} {ru_plural(n, 'страница', 'страницы', 'страниц')}"


def format_russian_pages_accusative(n: int) -> str:
    """Возвращает строку вида "N страницу/страницы/страниц" (винительный падеж для глагола "создать")."""
    return f"{n} {ru_plural(n, 'страницу', 'страницы', 'страниц')}"


def format_russian_pages_genitive_for_content(n: int) -> str:
    """Возвращает строку вида "N страницы/страниц" для оборота "содержимое N ...".

    Для 1: "1 страницы" (род. ед.), для 2–4 и 5+: "страниц" (род. мн.).
    """
    return f"{n} {ru_plural(n, 'страницы', 'страниц', 'страниц')}"


# ==============================
# «Категория» и «подкатегория»
# ==============================
def format_russian_categories_nominative(n: int) -> str:
    """N категория/категории/категорий (именительный падеж)."""
    return f"{n} {ru_plural(n, 'категория', 'категории', 'категорий')}"


def format_russian_subcategories_nominative(n: int) -> str:
    """N подкатегория/подкатегории/подкатегорий (именительный падеж)."""
    return f"{n} {ru_plural(n, 'подкатегория', 'подкатегории', 'подкатегорий')}"
