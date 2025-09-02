# -*- coding: utf-8 -*-
"""
Основное окно Wiki Category Tool.

Этот модуль содержит класс MainWindow, который объединяет все вкладки
и обеспечивает общую функциональность приложения.
"""

import os
from typing import Optional

from PySide6.QtCore import QEvent, QTimer, QSignalBlocker, QThread, Signal
from PySide6.QtWidgets import (
    QMainWindow, QTabWidget, QMessageBox, QToolButton
)
from PySide6.QtGui import QIcon

from ..core.api_client import WikimediaAPIClient
from ..core.namespace_manager import NamespaceManager
from ..core.pywikibot_config import PywikibotConfigManager
from ..core.template_manager import TemplateManager
from ..utils import debug, default_summary, default_create_summary, resource_path
from .widgets.ui_helpers import add_info_button as ui_add_info_button
from .widgets.ui_helpers import force_on_top as ui_force_on_top
from .widgets.ui_helpers import bring_to_front_sequence as ui_bring_to_front_sequence
from .tabs.auth_tab import AuthTab
from .tabs.parse_tab import ParseTab
from .tabs.replace_tab import ReplaceTab
from .tabs.create_tab import CreateTab
from .tabs.rename_tab import RenameTab


class NSLoadThread(QThread):
    finished_ok = Signal(bool)

    def __init__(self, namespace_manager, family: str, lang: str):
        super().__init__()
        self._nm = namespace_manager
        self._family = (family or 'wikipedia')
        self._lang = (lang or 'ru')

    def run(self):
        ok = False
        try:
            info = self._nm._load_ns_info(self._family, self._lang)
            ok = bool(info)
        except Exception:
            ok = False
        try:
            self.finished_ok.emit(ok)
        except Exception:
            pass


class MainWindow(QMainWindow):
    """Основное окно приложения"""
    ns_update_finished = Signal(str, str, bool)  # family, lang, ok
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Wiki Category Tool')
        try:
            icon_path = resource_path('icon.ico')
            if os.path.exists(icon_path):
                self.setWindowIcon(QIcon(icon_path))
                debug(f"MainWindow icon set: {icon_path}")
                # Дополнительно: на Windows принудительно установим иконку через WinAPI (WM_SETICON)
                try:
                    import sys
                    if sys.platform.startswith('win'):
                        import ctypes
                        hwnd = int(self.winId())
                        WM_SETICON = 0x0080
                        ICON_SMALL = 0
                        ICON_BIG = 1
                        IMAGE_ICON = 1
                        LR_LOADFROMFILE = 0x0010
                        LR_DEFAULTSIZE = 0x0040
                        hicon = ctypes.windll.user32.LoadImageW(None, icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
                        if hicon:
                            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon)
                            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon)
                            debug('WM_SETICON applied for small and big icons')
                except Exception as _e:
                    try:
                        debug(f'WM_SETICON failed: {_e}')
                    except Exception:
                        pass
            else:
                debug("MainWindow icon not found: icon.ico")
        except Exception:
            pass
        

        # Стартовый размер как раньше, но можно сжимать до меньшего минимума
        self.resize(1200, 700)
        self.setMinimumSize(900, 540)
        
        # Создание центрального виджета с вкладками
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        # Состояние приложения
        self.current_user: Optional[str] = None
        self.current_password: Optional[str] = None
        self.current_lang: Optional[str] = None
        self.current_family: Optional[str] = None
        self.prev_lang = 'ru'
        self._secret_buffer = ''
        self._stay_on_top_active = False
        
        # Запоминание флага «автоподтверждать прямые совпадения» между диалогами
        self._auto_confirm_direct_all_ui: bool = False

        # Инициализация core компонентов
        self.init_core_components()

        # Инициализация вкладок
        self.init_tabs()
        
        # Установка начальных значений
        self.current_lang = 'ru'
        self.current_family = 'wikipedia'
        
        # Не создаём файл правил заранее — он появится только при первом сохранении правил
        try:
            self._rules_file_path = os.path.join(self.config_manager._dist_configs_dir(), 'template_rules.json')
        except Exception:
            self._rules_file_path = None

    def init_core_components(self):
        """Инициализация всех core компонентов"""
        # Создание API клиента
        self.api_client = WikimediaAPIClient()
        
        # Создание менеджера пространств имен
        self.namespace_manager = NamespaceManager(self.api_client)
        
        # Создание менеджера конфигурации Pywikibot
        self.config_manager = PywikibotConfigManager()
        
        # Создание менеджера шаблонов
        self.template_manager = TemplateManager()

    def init_tabs(self):
        """Инициализация всех вкладок"""
        # Создание вкладок с передачей core компонентов
        self.auth_tab = AuthTab(self)
        self.parse_tab = ParseTab(self)
        self.replace_tab = ReplaceTab(self)
        self.create_tab = CreateTab(self)
        self.rename_tab = RenameTab(self)
        
        # Передача core компонентов во вкладки
        for tab in [self.auth_tab, self.parse_tab, self.replace_tab, self.create_tab, self.rename_tab]:
            if hasattr(tab, 'set_core_components'):
                tab.set_core_components(
                    api_client=self.api_client,
                    namespace_manager=self.namespace_manager,
                    config_manager=self.config_manager,
                    template_manager=self.template_manager
                )
        
        # Добавление вкладок в TabWidget
        self.tabs.addTab(self.auth_tab, "Авторизация")
        self.tabs.addTab(self.parse_tab, "Чтение")
        self.tabs.addTab(self.replace_tab, "Перезапись")
        self.tabs.addTab(self.create_tab, "Создание")
        self.tabs.addTab(self.rename_tab, "Переименование")
        
        # Подключение сигналов для передачи данных между вкладками
        self.auth_tab.login_success.connect(self._on_login_success)
        self.auth_tab.logout_success.connect(self._on_logout_success)
        self.auth_tab.lang_changed.connect(self._on_lang_change)
        
        # Обновление семейства проекта по выбору в AuthTab (без тяжёлых операций)
        if hasattr(self.auth_tab, 'family_combo') and self.auth_tab.family_combo:
            try:
                self.auth_tab.family_combo.currentTextChanged.connect(self.update_family)
            except Exception:
                pass
        
        # Обновление только при потере фокуса (если lineEdit доступен)
        if (hasattr(self.auth_tab, 'family_combo') and 
            self.auth_tab.family_combo and 
            hasattr(self.auth_tab.family_combo, 'lineEdit') and
            self.auth_tab.family_combo.lineEdit()):
            self.auth_tab.family_combo.lineEdit().editingFinished.connect(
                lambda: self.update_family(self.auth_tab.family_combo.currentText())
            )
        
        # Загрузка сохраненных учетных данных
        try:
            self.auth_tab.load_creds()
        except Exception:
            pass
            
        # Связать выпадающие списки namespace между вкладками
        QTimer.singleShot(150, self._link_ns_combos)

        # Обновлять namespace при первом/каждом открытии содержательных вкладок
        try:
            self.tabs.currentChanged.connect(self._on_tab_changed)
        except Exception:
            pass

    def _on_login_success(self, username: str, password: str, lang: str, family: str):
        """Обработка успешной авторизации"""
        self.current_user = username
        self.current_password = password
        self.current_lang = lang
        self.current_family = family
        
        # Передача данных авторизации во все вкладки
        for tab in self.content_tabs():
            if hasattr(tab, 'set_auth_data'):
                tab.set_auth_data(username, lang, family)
        
        # При авторизации обновляем namespace только если нет кэша для текущей пары
        try:
            cache_path = self.namespace_manager._ns_cache_file(family or 'wikipedia', lang or 'ru')
            if not os.path.isfile(cache_path):
                debug(f'Авторизация: кэш namespace отсутствует — выполняем форс-загрузку для {family}:{lang}')
                QTimer.singleShot(0, lambda: self.update_namespace_combos(family, lang, force=True))
            else:
                debug('Авторизация: кэш namespace найден — форс-загрузка не требуется')
        except Exception:
            pass

    def _on_logout_success(self):
        """Обработка выхода из системы"""
        self.current_user = None
        self.current_password = None
        self.current_lang = None
        self.current_family = None
        
        # Очистка данных авторизации во всех вкладках
        for tab in self.content_tabs():
            if hasattr(tab, 'clear_auth_data'):
                tab.clear_auth_data()

        # Снять зелёную подсветку логина/пароля на вкладке авторизации
        try:
            if hasattr(self.auth_tab, 'clear_auth_highlight'):
                self.auth_tab.clear_auth_highlight()
        except Exception:
            pass

    def _on_lang_change(self, new_lang: str):
        """Обработка изменения языка"""
        self.current_lang = new_lang
        
        # Обновление summary полей в зависимости от языка
        edits = []
        
        # Собираем все summary поля из вкладок
        if hasattr(self.replace_tab, 'summary_edit'):
            edits.append((self.replace_tab.summary_edit, default_summary))
        if hasattr(self.create_tab, 'summary_edit_create'):
            edits.append((self.create_tab.summary_edit_create, default_create_summary))
        
        # Обновляем текст в полях summary
        for widget, func in edits:
            if widget is None:
                continue
            cur = widget.text().strip()
            if cur == '' or cur == func(self.prev_lang):
                widget.setText(func(new_lang))
        
        self.prev_lang = new_lang
        
        # Обновление namespace комбобоксов во всех вкладках
        family = getattr(self.auth_tab, 'family_combo', None)
        if family:
            family_text = family.currentText() or 'wikipedia'
            self.current_family = family_text
            debug(f'Пропускаем обновление namespace при изменении языка на {new_lang}')
        
        # Уведомление вкладок об изменении языка
        for tab in self.content_tabs():
            if hasattr(tab, 'update_language'):
                tab.update_language(new_lang)

        # После обновления — вновь связать комбобоксы
        self._link_ns_combos()

    def _update_all_namespaces(self, lang: str, family: str):
        """Обновление namespace комбобоксов во всех вкладках"""
        # Обновление namespace комбобоксов для всех вкладок
        try:
            combos = self._gather_ns_combos()
            for combo in combos:
                try:
                    self.namespace_manager.populate_ns_combo(combo, family, lang)
                    self.namespace_manager._adjust_combo_popup_width(combo)
                except Exception:
                    pass
        except Exception as e:
            debug(f"Ошибка при обновлении namespace комбобоксов: {e}")

    def update_namespace_combos(self, family: str, lang: str, force: bool = False):
        """Публичный метод для обновления namespace комбобоксов.

        Args:
            family: семейство проектов
            lang: язык проекта
            force: зарезервировано для принудительного обновления (для совместимости)
        """
        debug(f'update_namespace_combos вызван с параметрами: family={family}, lang={lang}, force={force}')
        try:
            combos = self._gather_ns_combos()
            for combo in combos:
                try:
                    # Если force=True — разрешаем сетевую загрузку и обновление кэша
                    self.namespace_manager.populate_ns_combo(combo, family, lang, force_load=force)
                    self.namespace_manager._adjust_combo_popup_width(combo)
                except Exception:
                    pass
            debug('update_namespace_combos выполнен успешно')
            # После заполнения — связать комбобоксы
            self._link_ns_combos()
        except Exception as e:
            debug(f'Ошибка в update_namespace_combos: {e}')
            import traceback
            debug(f'Traceback: {traceback.format_exc()}')

    def _on_tab_changed(self, index: int):
        """Обработка переключения вкладок: при открытии вкладок 2–5 обновляем NS.

        Выполняем сетевую загрузку только если отсутствует кэш для текущего языка/проекта.
        """
        try:
            # Вкладки: 0=Авторизация, 1=Чтение, 2=Перезапись, 3=Создание, 4=Переименование
            if index in (1, 2, 3, 4):
                fam = self.current_family or 'wikipedia'
                lang = self.current_lang or 'ru'
                force_needed = False
                try:
                    cache_path = self.namespace_manager._ns_cache_file(fam, lang)
                    force_needed = not os.path.isfile(cache_path)
                except Exception:
                    force_needed = False
                debug(f'Открыта вкладка {index}: обновление namespace (force={force_needed})')
                self.update_namespace_combos(fam, lang, force=force_needed)
        except Exception as e:
            try:
                debug(f'Ошибка в _on_tab_changed: {e}')
            except Exception:
                pass
    
    def force_update_namespace_combos(self, family: str, lang: str):
        """Асинхронная загрузка NS в кэш и обновление комбобоксов после завершения."""
        try:
            # Не запускаем второй поток, если первый ещё работает
            running = getattr(self, '_ns_thread', None)
            if running is not None and hasattr(running, 'isRunning') and running.isRunning():
                return
        except Exception:
            pass

        t = NSLoadThread(self.namespace_manager, family, lang)
        self._ns_thread = t

        def _on_done(ok: bool):
            try:
                self.update_namespace_combos(family, lang, force=False)
            except Exception:
                pass
            try:
                self.ns_update_finished.emit(family, lang, ok)
            except Exception:
                pass
            try:
                self._ns_thread = None
            except Exception:
                pass

        try:
            t.finished_ok.connect(_on_done)
            t.start()
        except Exception:
            # Фолбэк: синхронно (нежелательно, но лучше, чем отсутствие действия)
            try:
                self.update_namespace_combos(family, lang, force=True)
                self.ns_update_finished.emit(family, lang, True)
            except Exception:
                try:
                    self.ns_update_finished.emit(family, lang, False)
                except Exception:
                    pass

    def update_family(self, new_family: str):
        """Обновление семейства проектов"""
        self.current_family = new_family
        
        debug(f'Пропускаем обновление namespace при изменении семейства на {new_family}')

        # Уведомление вкладок об изменении семейства
        for tab in self.content_tabs():
            if hasattr(tab, 'update_family'):
                tab.update_family(new_family)

        # После обновления — вновь связать комбобоксы
        self._link_ns_combos()

    def content_tabs(self):
        """Возвращает список вкладок с основными операциями (без авторизации)."""
        return [self.parse_tab, self.replace_tab, self.create_tab, self.rename_tab]

    # ===== Связь выпадающих списков Namespace между вкладками =====
    def _gather_ns_combos(self):
        combos = []
        mapping = [
            (self.parse_tab, 'ns_combo_parse'),
            (self.replace_tab, 'rep_ns_combo'),
            (self.create_tab, 'ns_combo_create'),
            (self.rename_tab, 'rename_ns_combo')
        ]
        for tab, attr in mapping:
            try:
                combo = getattr(tab, attr, None)
                if combo is not None:
                    combos.append(combo)
            except Exception:
                pass
        return combos

    def _unlink_ns_combos(self):
        if not hasattr(self, '_ns_combo_slots'):
            self._ns_combo_slots = {}
        # Отключаем предыдущие слоты, если были
        for combo, slot in list(self._ns_combo_slots.items()):
            try:
                combo.currentIndexChanged.disconnect(slot)
            except Exception:
                pass
        self._ns_combo_slots.clear()

    def _link_ns_combos(self):
        try:
            combos = self._gather_ns_combos()
            if not combos:
                return
            self._unlink_ns_combos()

            def make_slot(src_combo):
                def _on_changed(idx):
                    try:
                        data_val = src_combo.itemData(idx)
                    except Exception:
                        data_val = None
                    for other in combos:
                        if other is src_combo:
                            continue
                        try:
                            if data_val is None:
                                with QSignalBlocker(other):
                                    other.setCurrentIndex(idx)
                            else:
                                row = other.findData(data_val)
                                if row != -1 and row != other.currentIndex():
                                    with QSignalBlocker(other):
                                        other.setCurrentIndex(row)
                        except Exception:
                            pass
                return _on_changed

            # Подключаем слоты
            for c in combos:
                slot = make_slot(c)
                try:
                    c.currentIndexChanged.connect(slot)
                    self._ns_combo_slots[c] = slot
                except Exception:
                    pass

            # Начальная синхронизация: берём текущий индекс первого
            leader = combos[0]
            try:
                idx = leader.currentIndex()
                # руками вызываем слот лидера для выравнивания
                if leader in self._ns_combo_slots:
                    self._ns_combo_slots[leader](idx)
            except Exception:
                pass
        except Exception:
            pass

    def _add_info_button(self, host_layout, text: str, inline: bool = False):
        """Делегирует создание кнопки справки в ui_helpers.add_info_button"""
        try:
            return ui_add_info_button(self, host_layout, text, inline)
        except Exception:
            # Fallback на простое сообщение
            try:
                btn = QToolButton()
                btn.setText('❔')
                btn.setAutoRaise(True)
                btn.setToolTip(text)
                btn.clicked.connect(lambda _=None, t=text: QMessageBox.information(self, 'Справка', t))
                if hasattr(host_layout, 'addWidget'):
                    host_layout.addWidget(btn)
                return btn
            except Exception:
                return None

    def _force_on_top(self, enable: bool, delay_ms: int = 0) -> None:
        """Делегирует в ui_helpers.force_on_top"""
        try:
            ui_force_on_top(self, enable, delay_ms)
        except Exception:
            pass

    def _bring_to_front_sequence(self) -> None:
        """Делегирует в ui_helpers.bring_to_front_sequence"""
        try:
            ui_bring_to_front_sequence(self)
        except Exception:
            pass

    def show_debug(self):
        """Показать/скрыть окно отладки (singleton поведение)"""
        from .dialogs.debug_dialog import DebugDialog
        
        # Проверяем, есть ли уже открытое окно debug
        if hasattr(self, '_debug_dialog') and self._debug_dialog is not None:
            if self._debug_dialog.isVisible():
                # Если окно видимо, скрываем его
                self._debug_dialog.hide()
            else:
                # Если окно скрыто, показываем его
                self._debug_dialog.show()
                self._debug_dialog.raise_()
                self._debug_dialog.activateWindow()
        else:
            # Создаем новое окно debug
            self._debug_dialog = DebugDialog(self)
            # Подключаем сигнал закрытия для сброса ссылки
            self._debug_dialog.finished.connect(self._on_debug_dialog_closed)
            self._debug_dialog.show()
    
    def _on_debug_dialog_closed(self):
        """Обработка закрытия debug диалога"""
        self._debug_dialog = None

    def eventFilter(self, obj, event):
        """Фильтр событий для обработки специальных событий"""
        try:
            from ..utils import get_debug_view
            debug_view = get_debug_view()
            if event.type() == QEvent.KeyPress and debug_view is not None and obj is debug_view:
                pass
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def get_core_components(self):
        """Получить все core компоненты для использования в вкладках"""
        return {
            'api_client': self.api_client,
            'namespace_manager': self.namespace_manager,
            'config_manager': self.config_manager,
            'template_manager': self.template_manager
        }

    def closeEvent(self, event):
        """Обработка закрытия окна"""
        # Проверяем запущенные потоки как в оригинале
        running_threads = []
        tab_workers = [
            ('parse', self.parse_tab, 'worker'),
            ('replace', self.replace_tab, 'rworker'), 
            ('create', self.create_tab, 'cworker'),
            ('rename', self.rename_tab, 'mrworker'),
            ('auth', self.auth_tab, '_login_worker')
        ]
        
        for tab_name, tab, worker_attr in tab_workers:
            worker = getattr(tab, worker_attr, None)
            if worker and hasattr(worker, 'isRunning') and worker.isRunning():
                running_threads.append(tab_name)
        
        if running_threads:
            res = QMessageBox.question(
                self, 
                'Внимание', 
                f'Операции ещё выполняются в вкладках: {", ".join(running_threads)}. Закрыть программу?',
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if res != QMessageBox.Yes:
                event.ignore()
                return
        
        # Не пытаемся сохранять/валидировать учётные данные при закрытии, чтобы не появлялись диалоги
        # (раньше вызывался save_creds(), что могло запрашивать ввод пароля при закрытии окна)
        
        # Точечная остановка LoginWorker, чтобы избежать предупреждения QThread при выходе
        try:
            login_worker = getattr(self.auth_tab, '_login_worker', None)
            if login_worker and hasattr(login_worker, 'isRunning') and login_worker.isRunning():
                try:
                    if hasattr(login_worker, 'request_stop'):
                        login_worker.request_stop()
                except Exception:
                    pass
                try:
                    login_worker.wait(1500)
                except Exception:
                    pass
                try:
                    if hasattr(login_worker, 'isRunning') and login_worker.isRunning() and hasattr(login_worker, 'terminate'):
                        login_worker.terminate()
                        login_worker.wait(1000)
                except Exception:
                    pass
        except Exception:
            pass
        
        super().closeEvent(event)