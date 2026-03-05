# -*- coding: utf-8 -*-
"""
Entry-point to run Wiki Category Tool via `python -m wiki_cat_tool` or directly.
"""

import os
import sys as _sys


def _set_windows_app_id_early() -> None:
    """Выставить AppUserModelID как можно раньше (до импортов Qt)."""
    try:
        if _sys.platform.startswith("win"):
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "sc113.WikiCatTool"
            )
    except Exception:
        pass


# Для стабильного отображения иконки в taskbar на Windows.
_set_windows_app_id_early()

# Всегда добавляем корень проекта (родитель каталога пакета) в sys.path ПЕРЕД импортами,
# чтобы абсолютный импорт гарантированно подтянул локальный пакет, а не установленный.
try:
    _project_root = os.path.dirname(os.path.dirname(__file__))
    if _project_root and _project_root not in _sys.path:
        _sys.path.insert(0, _project_root)
except Exception:
    pass

try:
    # Предпочитаем относительный импорт, если модуль запущен как пакет
    from .main import main  # type: ignore
except Exception:
    # Абсолютный импорт теперь также возьмёт локальный пакет благодаря вставке в sys.path
    from wiki_cat_tool.main import main  # type: ignore


if __name__ == "__main__":
    main()
