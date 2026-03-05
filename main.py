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
from PySide6.QtCore import QTimer, QThread, Signal

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
            # Устанавливаем AppUserModelID для правильного отображения иконки
            import ctypes
            # Стабильный ID без версии, чтобы Windows не теряла привязку иконки между сборками.
            myappid = 'sc113.WikiCatTool'
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                myappid)
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
        candidates = []
        # 1) Путь к добавленному ресурсу (dev/onefile _MEIPASS)
        candidates.append(resource_path('icon.ico'))
        # 2) Папка рядом с exe (на случай внешнего запуска)
        try:
            exe_dir = os.path.dirname(sys.executable)
            candidates.append(os.path.join(exe_dir, 'icon.ico'))
        except Exception:
            pass
        # 3) Fallback: иконка, вшитая в сам exe (Windows)
        if getattr(sys, 'frozen', False):
            candidates.append(sys.executable)

        for icon_path in candidates:
            try:
                if not icon_path or not os.path.exists(icon_path):
                    continue
                icon = QIcon(icon_path)
                if icon.isNull():
                    continue
                app.setWindowIcon(icon)
                debug(f"Icon set: {icon_path}")
                return
            except Exception:
                continue
        debug("Icon setup: no valid icon source found")
    except Exception as e:
        try:
            from .utils import debug
            debug(f"Icon setup failed: {e}")
        except Exception:
            pass


class UpdateCheckerThread(QThread):
    """Поток для проверки обновлений в фоновом режиме"""
    update_found = Signal(str, str)  # new_version, download_url

    def run(self):
        """Выполняется в фоновом потоке"""
        try:
            from .core.update_checker import check_for_updates
            from .core.update_settings import UpdateSettings
            from .utils import resource_path, debug
            from .constants import APP_VERSION

            debug(f"Проверка обновлений... (текущая версия: {APP_VERSION})")

            # Получаем путь к директории настроек
            settings_dir = resource_path('configs')
            update_settings = UpdateSettings(settings_dir)

            # Проверяем наличие обновлений с коротким таймаутом
            update_info = check_for_updates(timeout=3)

            if update_info:
                new_version, download_url = update_info

                # Проверяем, не пропущена ли эта версия
                if not update_settings.is_version_skipped(new_version):
                    debug(f"Найдена новая версия: {new_version}")
                    # Отправляем сигнал в главный поток
                    try:
                        self.update_found.emit(new_version, download_url)
                    except Exception as e:
                        debug(f"Ошибка при отправке сигнала обновления: {e}")
                else:
                    debug(
                        f"Новая версия {new_version} найдена, но пропущена пользователем")
            else:
                debug("Обновлений не найдено")
        except Exception as e:
            # Если проверка не удалась, просто игнорируем
            try:
                debug(f"Ошибка при проверке обновлений: {e}")
            except Exception:
                pass


def show_update_dialog(window, new_version, download_url):
    """Показывает диалог обновления (вызывается в главном потоке)"""
    try:
        from .gui.dialogs import UpdateDialog
        from .core.update_settings import UpdateSettings
        from .constants import APP_VERSION
        from .utils import resource_path, debug

        settings_dir = resource_path('configs')
        update_settings = UpdateSettings(settings_dir)

        # Показываем диалог
        dialog = UpdateDialog(APP_VERSION, new_version, download_url, window)
        result = dialog.exec()

        # Если пользователь выбрал пропустить версию
        if dialog.skip_version:
            update_settings.skip_version(new_version)
            debug(f"Версия {new_version} добавлена в пропущенные")
    except Exception as e:
        debug(f"Ошибка при показе диалога обновления: {e}")


def main():
    """Главная функция приложения"""
    # Настройка окружения
    setup_environment()

    # Настройка перенаправления вывода
    setup_stdout_redirect()

    # Настройка Windows taskbar
    setup_windows_taskbar()

    # Создание приложения Qt
    app = QApplication(sys.argv)

    # Настройка иконки
    setup_application_icon(app)

    # Создание и показ лёгкой оболочки окна
    from .gui.main_window import MainWindow

    window = MainWindow()
    window.show()

    # Принудительно обрабатываем события Qt, чтобы окно отрисовалось до тяжёлых импортов
    app.processEvents()

    def finish_startup():
        try:
            window.set_startup_status('Подключаем pywikibot...')
            app.processEvents()
            setup_pywikibot()
            window.set_startup_status('Подгружаем вкладки...')
            app.processEvents()
            window.complete_startup()
        except Exception as e:
            try:
                from .utils import debug
                debug(f'Ошибка отложенного запуска: {e}')
            except Exception:
                pass
            try:
                window.set_startup_status(f'Ошибка запуска: {e}')
            except Exception:
                pass
            return

        # Функция для отложенного запуска проверки обновлений
        def start_update_check():
            try:
                update_thread = UpdateCheckerThread()
                update_thread.update_found.connect(
                    lambda v, u: show_update_dialog(window, v, u))
                update_thread.start()
                # Сохраняем ссылку на поток
                window._update_thread = update_thread
            except Exception:
                pass

        QTimer.singleShot(1000, start_update_check)

    # Сначала рисуем окно, затем догружаем pywikibot и вкладки
    QTimer.singleShot(0, finish_startup)

    # Запуск приложения - ВАЖНО: event loop должен запуститься сразу после show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()

