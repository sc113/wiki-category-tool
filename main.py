"""
Точка входа для рефакторированного Wiki Category Tool.
Инициализирует все компоненты и запускает GUI приложение.

Этот модуль содержит функцию main() и все необходимые функции инициализации:
- Настройка окружения и путей
- Инициализация pywikibot
- Перенаправление stdout/stderr для GUI
- Настройка иконки и темы приложения
- Создание и запуск главного окна
"""

import sys
import os
import ctypes
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon

# Настройка путей и окружения
def setup_environment():
    """Настройка окружения для работы приложения (делегировано core.pywikibot_config)."""
    try:
        from .core.pywikibot_config import ensure_base_env
        cfg_dir = ensure_base_env()
        # Возвращаем базовую директорию (родитель configs)
        return os.path.dirname(cfg_dir)
    except Exception:
        # Fallback к предыдущей логике вычисления base_dir
        if getattr(sys, 'frozen', False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.dirname(__file__))

def setup_stdout_redirect():
    """Настройка перенаправления stdout/stderr для GUI приложения"""
    try:
        from .utils import setup_gui_stdout_redirect
        setup_gui_stdout_redirect()
    except Exception:
        # Fallback на прямую установку, если импорт не удался
        from .utils import GuiStdWriter
        if getattr(sys, 'stdout', None) is None:
            sys.stdout = GuiStdWriter()
        if getattr(sys, 'stderr', None) is None:
            sys.stderr = GuiStdWriter()

def setup_pywikibot():
    """Инициализация pywikibot с перехватом вывода"""
    import pywikibot
    from .utils import debug
    
    # Перехват вывода pywikibot
    def _pywb_log(msg, *_args, **_kwargs):
        debug('PYWIKIBOT: ' + str(msg))
    
    pywikibot.output = _pywb_log
    pywikibot.warning = _pywb_log
    pywikibot.error = _pywb_log



def setup_windows_taskbar():
    """Настройка иконки в панели задач Windows"""
    try:
        if sys.platform.startswith('win'):
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('sc113.WikiCatTool')
    except Exception as e:
        try:
            from .utils import debug
            debug(f"Taskbar ID setup failed: {e}")
        except Exception:
            pass

def setup_application_icon(app: QApplication):
    """Настройка иконки приложения"""
    try:
        from .utils import resource_path, debug
        icon_path = resource_path('icon.ico')
        if os.path.exists(icon_path):
            app.setWindowIcon(QIcon(icon_path))
            debug(f"Icon set: {icon_path}")
        else:
            debug("Icon file not found: icon.ico")
    except Exception as e:
        try:
            from .utils import debug
            debug(f"Icon setup failed: {e}")
        except Exception:
            pass

def main():
    """Главная функция приложения"""
    # Настройка окружения
    setup_environment()
    
    # Настройка перенаправления вывода
    setup_stdout_redirect()
    
    # Инициализация pywikibot
    setup_pywikibot()
    
    # Настройка Windows taskbar
    setup_windows_taskbar()
    
    # Создание приложения Qt
    app = QApplication(sys.argv)
    
    # Настройка иконки
    setup_application_icon(app)
    
    # Создание и показ главного окна
    from .gui.main_window import MainWindow
    
    window = MainWindow()
    # Загрузка сохраненных учетных данных через auth_tab
    try:
        window.auth_tab.load_creds()
    except Exception:
        pass
    window.show()
    
    # Запуск приложения
    sys.exit(app.exec())

if __name__ == '__main__':
    main()