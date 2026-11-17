# -*- coding: utf-8 -*-
"""
Entry-point to run Wiki Category Tool via `python -m wiki_cat_tool` or directly.
"""

import os
import sys as _sys


def _set_windows_app_id_early() -> None:
    """Keep a separate taskbar identity when running under the Python interpreter."""
    try:
        # A portable build already has its own identity and icon in the executable.
        # Let Explorer associate the button with that file, even if it was renamed.
        if _sys.platform == "win32" and not getattr(_sys, "frozen", False):
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "sc113.WikiCatTool"
            )
    except Exception:
        pass


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
