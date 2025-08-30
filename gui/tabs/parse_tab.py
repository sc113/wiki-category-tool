# -*- coding: utf-8 -*-
"""
Вкладка чтения для Wiki Category Tool.

Этот модуль содержит компонент ParseTab, который обеспечивает:
- Получение подкатегорий через API или PetScan
- Ручной ввод списка категорий
- Загрузку списка из файла
- Чтение содержимого страниц
- Сохранение результатов в TSV формате
"""

import os
import re
import urllib.parse
import webbrowser
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QComboBox, QPushButton, QToolButton, QTextEdit, QProgressBar, 
    QGroupBox, QMessageBox
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont

from ...constants import PREFIX_TOOLTIP, REQUEST_HEADERS
from ...core.api_client import WikimediaAPIClient, REQUEST_SESSION, _rate_wait
from ...utils import debug, format_russian_subcategories_nominative
from ...workers.parse_worker import ParseWorker
from ..widgets.ui_helpers import (
    embed_button_in_lineedit, add_info_button, pick_file, pick_save, 
    open_from_edit, log_message, set_start_stop_ratio
)


class ParseTab(QWidget):
    """Вкладка для чтения содержимого страниц"""
    
    # Сигналы для взаимодействия с главным окном
    language_changed = Signal(str)
    family_changed = Signal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent
        
        # Инициализация worker'а
        self.worker = None
        
        # Данные авторизации
        self.current_user = None
        self.current_lang = None
        self.current_family = None
        
        # Создание UI
        self.setup_ui()
        
    def setup_ui(self):
        """Создание пользовательского интерфейса вкладки чтения"""
        main_layout = QVBoxLayout(self)
        
        # Справочная информация (кратко и по делу)
        parse_help_left = (
            'Префиксы/NS: «Авто» — без изменения исходного списка. Выбирайте NS, если в списке названия без префиксов.\n'
            'Локальные и английские префиксы в исходном списке распознаются.\n'
            'Ctrl+клик по «Получить подкатегории» — открыть PetScan для указанной категории.'
        )
        parse_help_right = (
            'Результат: .tsv (UTF‑8 с BOM). Формат: Title<TAB>строка1<TAB>строка2…\n'
            'В файл записываются только найденные страницы; отсутствующие отражаются в логе.\n'
            'Список берётся из .txt (если указан), иначе — из поля слева.\n'
            'При лимитах API выполняются автоматические паузы/повторы.'
        )
        
        # === ГОРИЗОНТАЛЬНОЕ РАЗДЕЛЕНИЕ ===
        h_main = QHBoxLayout()
        
        # === ЛЕВАЯ ПАНЕЛЬ: ВВОД ДАННЫХ ===
        left_group = QGroupBox("Источник")
        left_group.setStyleSheet("QGroupBox { border: 1px solid lightgray; border-radius: 5px; margin-top: 10px; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px 0 5px; }")
        left_layout = QVBoxLayout(left_group)
        try:
            left_layout.setContentsMargins(8, 12, 8, 8)  # не двигаем (i) у верхней кромки
            left_layout.setSpacing(2)  # уменьшили зазор между (i) и строкой «Префиксы»
        except Exception:
            pass
        # (i) будет добавлена в строку «Префиксы» справа — без влияния на вертикальные отступы
        
        # Префиксы пространства имён
        prefix_layout = QHBoxLayout()
        prefix_label = QLabel('Префиксы:')
        prefix_label.setToolTip(PREFIX_TOOLTIP)
        prefix_layout.addWidget(prefix_label)
        self.ns_combo_parse = QComboBox()
        self.ns_combo_parse.setEditable(False)
        # Заполнение будет происходить через метод update_namespace_combo
        prefix_layout.addWidget(self.ns_combo_parse)
        # Толкаем (i) к правому краю в этой же строке
        prefix_layout.addStretch()
        add_info_button(self, prefix_layout, parse_help_left, inline=True)
        left_layout.addLayout(prefix_layout)
        
        # Получение подкатегорий
        left_layout.addSpacing(6)
        left_layout.addWidget(QLabel('<b>Получить подкатегории:</b>'))
        petscan_input_layout = QHBoxLayout()
        self.cat_edit = QLineEdit()
        self.cat_edit.setPlaceholderText('Название корневой категории')
        petscan_input_layout.addWidget(self.cat_edit, 1)
        
        self.petscan_btn = QPushButton('Получить подкатегории')
        self.petscan_btn.setToolTip('Клик — получить подкатегории через API.\nCtrl+клик — открыть Petscan с расширенными настройками')
        self.petscan_btn.clicked.connect(self.open_petscan)
        petscan_input_layout.addWidget(self.petscan_btn)
        left_layout.addLayout(petscan_input_layout)
        
        # Ручной ввод списка
        left_layout.addSpacing(6)
        lbl_left_top = QLabel('<b>Список категорий для считывания:</b>')
        left_layout.addWidget(lbl_left_top)
        self.manual_list = QTextEdit()
        self.manual_list.setPlaceholderText('Список страниц (по одной на строку)')
        self.manual_list.setMinimumHeight(220)
        left_layout.addWidget(self.manual_list, 1)
        
        # Создаем алиас для совместимости с тестами
        self.list_edit = self.manual_list
        try:
            left_layout.setStretchFactor(self.manual_list, 1)
        except Exception:
            pass
        
        # Загрузка из файла
        left_layout.addWidget(QLabel('<b>Или загрузить из файла:</b>'))
        file_layout = QHBoxLayout()
        file_layout.addWidget(QLabel('Файл (.txt):'))
        
        self.in_path = QLineEdit()
        self.in_path.setMinimumWidth(0)
        file_layout.addWidget(self.in_path, 1)
        # Кнопка «…» справа
        btn_browse_in = QToolButton()
        btn_browse_in.setText('…')
        btn_browse_in.setAutoRaise(False)
        try:
            btn_browse_in.setFixedSize(28, 28)
            btn_browse_in.setCursor(Qt.PointingHandCursor)
            btn_browse_in.setToolTip('Выбрать файл')
        except Exception:
            pass
        btn_browse_in.clicked.connect(lambda: pick_file(self, self.in_path, '*.txt'))
        file_layout.addWidget(btn_browse_in)
        
        # Создаем алиас для совместимости с тестами
        self.file_edit = self.in_path
        
        btn_open_in = QPushButton('Открыть')
        btn_open_in.clicked.connect(lambda: open_from_edit(self, self.in_path))
        file_layout.addWidget(btn_open_in)
        left_layout.addLayout(file_layout)
        
        # === ПРАВАЯ ПАНЕЛЬ: ХОД СЧИТЫВАНИЯ ===
        right_group = QGroupBox("Настройки и результат")
        right_group.setStyleSheet("QGroupBox { border: 1px solid lightgray; border-radius: 5px; margin-top: 10px; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px 0 5px; }")
        right_layout = QVBoxLayout(right_group)
        try:
            right_layout.setContentsMargins(8, 12, 8, 8)
            right_layout.setSpacing(8)
        except Exception:
            pass
        
        # i-кнопка справа будет прикреплена к рамке, без участия в layout
        
        # Файл сохранения
        save_layout = QHBoxLayout()
        save_layout.addWidget(QLabel('Сохранить в:'))
        
        self.out_path = QLineEdit('categories.tsv')
        self.out_path.setMinimumWidth(0)
        save_layout.addWidget(self.out_path, 1)
        # Кнопка «…» справа
        btn_browse_out = QToolButton()
        btn_browse_out.setText('…')
        btn_browse_out.setAutoRaise(False)
        try:
            btn_browse_out.setFixedSize(28, 28)
            btn_browse_out.setCursor(Qt.PointingHandCursor)
            btn_browse_out.setToolTip('Выбрать путь сохранения')
        except Exception:
            pass
        btn_browse_out.clicked.connect(lambda: pick_save(self, self.out_path, '.tsv'))
        save_layout.addWidget(btn_browse_out)
        
        btn_open_out = QPushButton('Открыть')
        btn_open_out.clicked.connect(lambda: open_from_edit(self, self.out_path))
        save_layout.addWidget(btn_open_out)
        # Толкаем (i) к правому краю верхней строки справа
        save_layout.addStretch()
        add_info_button(self, save_layout, parse_help_right, inline=True)
        right_layout.addLayout(save_layout)
        
        # Небольшой отступ и лог процесса (основная область)
        right_layout.addSpacing(6)
        lbl_right_top = QLabel('<b>Лог выполнения:</b>')
        # Заголовок
        right_layout.addWidget(lbl_right_top)
        # Контейнер для лога с кнопкой очистки в правом нижнем углу
        self.parse_log = QTextEdit()
        self.parse_log.setReadOnly(True)
        self.parse_log.setMinimumHeight(220)
        
        # Устанавливаем моноширинный шрифт для лога
        mono_font = QFont("Consolas", 9)
        if not mono_font.exactMatch():
            mono_font = QFont("Courier New", 9)
        mono_font.setFixedPitch(True)
        self.parse_log.setFont(mono_font)
        from ..widgets.ui_helpers import create_log_wrap
        parse_log_wrap = create_log_wrap(self, self.parse_log, with_header=False)
        right_layout.addWidget(parse_log_wrap, 1)
        try:
            right_layout.setStretchFactor(parse_log_wrap, 1)
        except Exception:
            pass
        
        # Прогресс-бар
        self.parse_bar = QProgressBar()
        self.parse_bar.setMaximum(1)
        self.parse_bar.setValue(0)
        right_layout.addWidget(self.parse_bar)
        try:
            # Синхронизируем нижние поля по высоте (лог справа и список слева)
            right_layout.setStretchFactor(self.parse_log, 1)
        except Exception:
            pass
        
        # Кнопки управления
        control_layout = QHBoxLayout()
        control_layout.addStretch()
        self.parse_btn = QPushButton('Начать считывание')
        self.parse_btn.clicked.connect(self.start_parse)
        control_layout.addWidget(self.parse_btn)
        self.parse_stop_btn = QPushButton('Остановить')
        self.parse_stop_btn.setEnabled(False)
        self.parse_stop_btn.clicked.connect(self.stop_parse)
        control_layout.addWidget(self.parse_stop_btn)
        control_layout.addStretch()
        right_layout.addLayout(control_layout)
        set_start_stop_ratio(self.parse_btn, self.parse_stop_btn, 3)
        
        # Добавление панелей в основной макет
        h_main.addWidget(left_group, 1)
        h_main.addWidget(right_group, 1)
        main_layout.addLayout(h_main)

    def update_namespace_combo(self, family: str, lang: str):
        """Обновление комбобокса пространств имен для текущего языка/проекта"""
        try:
            nm = getattr(self.parent_window, 'namespace_manager', None)
            if nm:
                nm.populate_ns_combo(self.ns_combo_parse, family, lang)
                nm._adjust_combo_popup_width(self.ns_combo_parse)
        except Exception:
            pass
    
    def get_current_language(self) -> str:
        """Получение текущего языка из родительского окна"""
        if hasattr(self.parent_window, 'lang_combo'):
            return self.parent_window.lang_combo.currentText() or 'ru'
        return 'ru'
    
    def get_current_family(self) -> str:
        """Получение текущего проекта из родительского окна (или из AuthTab)."""
        try:
            # Предпочитаем текущее состояние MainWindow.current_family, которое синхронизируется с AuthTab
            fam = getattr(self.parent_window, 'current_family', None)
            if fam:
                return fam
            # Фолбэк: попытаться взять из AuthTab
            auth = getattr(self.parent_window, 'auth_tab', None)
            if auth and hasattr(auth, 'family_combo') and auth.family_combo:
                txt = auth.family_combo.currentText() or 'wikipedia'
                return txt
        except Exception:
            pass
        return 'wikipedia'
    
    def open_petscan(self):
        """Получение подкатегорий через API или открытие PetScan"""
        if not hasattr(self, 'cat_edit') or self.cat_edit is None:
            debug("Error: cat_edit not initialized")
            return
            
        debug(f"Fetch subcats btn pressed: cat={self.cat_edit.text().strip()}")
        category = self.cat_edit.text().strip()
        if not category:
            QMessageBox.warning(self, 'Ошибка', 'Введите название категории.')
            return
        lang = self.get_current_language()
        fam = self.get_current_family()

        # --- Ctrl+click → открыть Petscan URL в браузере ---
        from PySide6.QtWidgets import QApplication
        mods = QApplication.keyboardModifiers()
        if mods & Qt.ControlModifier:
            cat_param = urllib.parse.quote_plus(category)
            petscan_url = (
                'https://petscan.wmcloud.org/?combination=subset&interface_language=en&ores_prob_from=&'
                'referrer_name=&ores_prob_to=&min_sitelink_count=&wikidata_source_sites=&templates_yes=&'
                'sortby=title&pagepile=&cb_labels_no_l=1&show_disambiguation_pages=both&language=' + lang +
                '&max_sitelink_count=&cb_labels_yes_l=1&outlinks_any=&common_wiki=auto&categories=' + cat_param +
                '&edits%5Bbots%5D=both&wikidata_prop_item_use=&ores_prediction=any&outlinks_no=&source_combination=&'
                'ns%5B14%5D=1&sitelinks_any=&cb_labels_any_l=1&edits%5Banons%5D=both&links_to_no=&search_wiki=&'
                f'project={fam}&after=&wikidata_item=no&search_max_results=1000&langs_labels_no=&langs_labels_yes=&'
                'sortorder=ascending&templates_any=&show_redirects=both&active_tab=tab_output&wpiu=any&doit='
            )
            debug(f"Open Petscan URL: {petscan_url}")
            webbrowser.open_new_tab(petscan_url)
            self.petscan_btn.setEnabled(True)
            return

        # --- API режим --- формируем полное имя категории ---
        if re.match(r'(?i)^(категория|category):', category):
            cat_full = category
        else:
            cat_full = ('Категория:' if lang == 'ru' else 'Category:') + category

        api_client = WikimediaAPIClient()
        api_url = api_client._build_api_url(fam, lang)
        params = {
            'action': 'query',
            'list': 'categorymembers',
            'cmtitle': cat_full,
            'cmtype': 'subcat',
            'cmlimit': 'max',
            'format': 'json'
        }
        subcats = []
        try:
            while True:
                _rate_wait()
                r = REQUEST_SESSION.get(api_url, params=params, timeout=10, headers=REQUEST_HEADERS)
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code} при запросе {api_url}")
                try:
                    resp = r.json()
                except Exception:
                    snippet = (r.text or '')[:200].replace('\n', ' ')
                    ct = r.headers.get('Content-Type')
                    raise RuntimeError(f"Не удалось распарсить JSON: Content-Type={ct}, тело[:200]={snippet!r}")
                debug(f"API GET subcats len={len(resp.get('query',{}).get('categorymembers',[]))}")
                subcats.extend(m['title'] for m in resp.get('query', {}).get('categorymembers', []))
                if 'continue' in resp:
                    params.update(resp['continue'])
                else:
                    break
            if subcats:
                subcats_sorted = sorted(subcats, key=lambda s: s.casefold())
                self.manual_list.setPlainText('\n'.join(subcats_sorted))
                log_message(self.parse_log, f"Получено {format_russian_subcategories_nominative(len(subcats_sorted))} (отсортировано)", debug)
            else:
                log_message(self.parse_log, 'Подкатегории не найдены.', debug)
        except Exception as e:
            log_message(self.parse_log, f"Ошибка API: {e}", debug)
        debug("Subcat fetch finished")
        self.petscan_btn.setEnabled(True)

        # Если нужно открыть ссылку вручную, скопируйте URL из логов
    
    def start_parse(self):
        """Запуск процесса чтения страниц"""
        if not self.out_path.text():
            QMessageBox.warning(self, 'Ошибка', 'Укажите файл результата.')
            return

        titles = []
        # Приоритет: если указан файл — читаем из него; иначе берём из текстового поля
        in_file = self.in_path.text().strip()
        if in_file:
            try:
                with open(in_file, encoding='utf-8') as f:
                    titles = [l.strip() for l in f if l.strip()]
            except Exception as e:
                QMessageBox.critical(self, 'Ошибка', str(e))
                return
        else:
            manual_lines = self.manual_list.toPlainText().splitlines()
            titles = [l.strip() for l in manual_lines if l.strip()]

        if not titles:
            QMessageBox.warning(self, 'Ошибка', 'Не указан ни файл со списком, ни текст списка.')
            return
        
        lang = self.get_current_language()
        fam = self.get_current_family()

        self.parse_bar.setMaximum(len(titles))
        self.parse_bar.setValue(0)
        self.parse_btn.setEnabled(False)
        ns_sel = self.ns_combo_parse.currentData()
        self.worker = ParseWorker(titles, self.out_path.text(), ns_sel, lang, fam)
        self.worker.progress.connect(lambda m: [self._inc_parse_prog(), log_message(self.parse_log, m, debug)])
        self.worker.finished.connect(self._on_parse_finished)
        self.parse_btn.setEnabled(False)
        # Возвращаем кнопку в режим «Остановить» при старте нового считывания
        try:
            try:
                self.parse_stop_btn.clicked.disconnect()
            except Exception:
                pass
            self.parse_stop_btn.setText('Остановить')
            self.parse_stop_btn.clicked.connect(self.stop_parse)
        except Exception:
            pass
        self.parse_stop_btn.setEnabled(True)
        # не очищаем лог автоматически — пользователь может очистить вручную
        self.worker.start()
    
    def stop_parse(self):
        """Остановка процесса чтения"""
        w = getattr(self, 'worker', None)
        if w and w.isRunning():
            w.request_stop()

            try:
                self.parse_stop_btn.setEnabled(False)
                log_message(self.parse_log, 'Останавливаю...', debug)
            except Exception:
                pass
        else:
            # Если не запущено — считаем, что кнопка в режиме «Открыть» и ничего не делаем
            pass
    
    def _on_parse_finished(self):
        """Обработка завершения процесса чтения"""
        # Переключаем кнопку «Остановить» → «Открыть» по завершении
        try:
            self.parse_btn.setEnabled(True)
            if getattr(self, 'worker', None) and getattr(self.worker, '_stop', False):
                # Если остановлено — вернём кнопку к исходному состоянию
                self.parse_stop_btn.setText('Остановить')
                self.parse_stop_btn.setEnabled(False)
                msg = 'Остановлено!'
            else:
                # Завершено штатно — меняем на «Открыть»
                self.parse_stop_btn.setText('Открыть')
                self.parse_stop_btn.setEnabled(True)
                # Привяжем к открытию файла результата
                out_path = self.out_path.text().strip()
                def _open_result():
                    try:
                        if out_path and os.path.isfile(out_path):
                            os.startfile(out_path)
                        else:
                            QMessageBox.information(self, 'Файл не найден', 'Файл результата не найден.')
                    except Exception as e:
                        QMessageBox.warning(self, 'Ошибка', str(e))
                try:
                    # Сбрасываем старые коннекты, если были
                    try:
                        self.parse_stop_btn.clicked.disconnect()
                    except Exception:
                        pass
                    self.parse_stop_btn.clicked.connect(_open_result)
                except Exception:
                    pass
                msg = 'Готово!'
            log_message(self.parse_log, msg, debug)
        except Exception:
            pass

    def _inc_parse_prog(self):
        """Увеличение значения прогресс-бара"""
        self.parse_bar.setValue(self.parse_bar.value() + 1)
    
    def set_auth_data(self, username: str, lang: str, family: str):
        """Установить данные авторизации"""
        self.current_user = username
        self.current_lang = lang
        self.current_family = family
    
    def clear_auth_data(self):
        """Очистить данные авторизации"""
        self.current_user = None
        self.current_lang = None
        self.current_family = None
    
    def pick_file(self, line_edit, filter_str: str):
        """Выбор файла для загрузки"""
        from ..widgets.ui_helpers import pick_file
        pick_file(self, line_edit, filter_str)
    
    def open_from_edit(self, line_edit):
        """Открытие файла из поля ввода"""
        from ..widgets.ui_helpers import open_from_edit
        open_from_edit(self, line_edit)