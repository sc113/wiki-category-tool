# -*- coding: utf-8 -*-
"""
Вкладка переименования для Wiki Category Tool.

Этот модуль содержит компонент RenameTab, который обеспечивает:
- Загрузку TSV файла с данными для переименования страниц
- Переименование страниц с созданием перенаправлений
- Перенос содержимого категорий (прямые ссылки и шаблоны)
- Template review диалоги при find_in_templates=True
- Кэширование template rules в configs/template_rules.json
- Настройку всех параметров переименования
"""

import os
import json
from typing import Optional
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QLineEdit, 
    QComboBox, QPushButton, QToolButton, QTextEdit, QSizePolicy, QProgressBar,
    QMessageBox, QCheckBox, QGroupBox
)
from PySide6.QtCore import Qt, Signal, QUrl
from PySide6.QtGui import QFont, QDesktopServices

from ...constants import PREFIX_TOOLTIP
from ...utils import debug
from ...workers.rename_worker import RenameWorker
from ...core.template_manager import TemplateManager
from ...core.pywikibot_config import apply_pwb_config, _dist_configs_dir
from ..widgets.ui_helpers import (
    embed_button_in_lineedit, add_info_button, pick_file, 
    open_from_edit, set_start_stop_ratio,
    init_log_tree, log_tree_parse_and_add, log_tree_add, log_tree_add_event
)
from ..dialogs.template_review_dialog import TemplateReviewDialog


class RenameTab(QWidget):
    """Вкладка для переименования страниц и переноса содержимого категорий"""
    
    # Сигналы для взаимодействия с главным окном
    language_changed = Signal(str)
    family_changed = Signal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent
        
        # Инициализация worker'а
        self.mrworker = None
        
        # Кэш template rules для UI
        self._template_auto_cache_ui = {}
        
        # Данные авторизации
        self.current_user = None
        self.current_lang = None
        self.current_family = None
        
        # Создание UI
        self.setup_ui()
        
    def setup_ui(self):
        """Создает пользовательский интерфейс вкладки"""
        # Основной layout
        v = QVBoxLayout(self)
        
        # Текст справки
        rename_help = (
            'TSV‑вход: OldTitle<TAB>NewTitle[<TAB>Комментарий]\n'
            '— Одна строка = одно переименование/перенос.\n'
            '— Комментарий в 3‑й колонке опционален (но табуляция важна). Поле ниже «Комментарий к правке» переопределяет все.\n\n'
            'Префиксы/пространства имён:\n'
            '— Список пространств имён нормализует оба столбца к выбранному NS.\n'
            '— Можно писать названия без префикса или с локальным/английским — распознаётся.\n'
            '— «Авто» не меняет заголовки из файла.\n\n'
            'Переименование страниц:\n'
            '— Управляется галкой «Переименовывать страницы»; перенаправления настраиваются отдельно для категорий и прочих.\n\n'
            'Перенос содержимого категорий (если включено):\n'
            '1) Прямые включения: [[Категория:Старая|Ключ]] → [[Категория:Новая|Ключ]]. Ключ «|…» сохраняется.\n'
            '   Для «Шаблон:»/«Модуль:» дополнительно обрабатываются основная страница и её /doc.\n'
            '2) Через параметры шаблонов: исправляются значения параметров с названием категории.\n'
            '   Полные совпадения можно применить/пропустить «для всех», частичные подтверждаются в диалоге с редактированием.\n'
            '   Принятые решения сохраняются в configs/template_rules.json и переиспользуются.\n\n'
            'Поведение и лог:\n'
            '— Ограничения API обрабатываются автоматически (повторы/замедление).\n'
            '— В логе — детальный ход и сводка: Прямые / Через шаблоны / Осталось.\n\n'
            'Важно:\n'
            '— Фильтр «Фильтр содержимого категории…» влияет только на участников категории и не ограничивает переименования из TSV.'
        )
        
        # Строка выбора файла и настроек
        h = QHBoxLayout()
        
        # Поле файла с кнопкой
        self.rename_file_edit = QLineEdit('categories.tsv')
        self.rename_file_edit.setMinimumWidth(0)
        self.rename_file_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        
        h.addWidget(QLabel('Список для переименования (.tsv):'))
        h.addWidget(self.rename_file_edit, 1)
        # Кнопка «…» справа
        btn_browse_rename = QToolButton()
        btn_browse_rename.setText('…')
        btn_browse_rename.setAutoRaise(False)
        try:
            btn_browse_rename.setFixedSize(28, 28)
            btn_browse_rename.setCursor(Qt.PointingHandCursor)
            btn_browse_rename.setToolTip('Выбрать файл')
        except Exception:
            pass
        btn_browse_rename.clicked.connect(lambda: pick_file(self, self.rename_file_edit, '*.tsv'))
        h.addWidget(btn_browse_rename)
        
        # Кнопка "Открыть"
        btn_open_tsv_rename = QPushButton('Открыть')
        btn_open_tsv_rename.clicked.connect(lambda: open_from_edit(self, self.rename_file_edit))
        btn_open_tsv_rename.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        h.addWidget(btn_open_tsv_rename)
        
        # Компактный выбор префикса (выпадающий список)
        prefix_label_rename = QLabel('Префиксы:')
        prefix_label_rename.setToolTip(PREFIX_TOOLTIP)
        h.addWidget(prefix_label_rename)
        
        self.rename_ns_combo = QComboBox()
        self.rename_ns_combo.setEditable(False)
        # Заполнение будет происходить при установке языка/семейства
        h.addWidget(self.rename_ns_combo)
        
        # Кнопка ℹ в строке выбора файла
        add_info_button(self, h, rename_help)
        
        v.addLayout(h)
        
        # Опции в две колонки
        # Подсказки для опций переноса
        phase1_help = (
            'Прямые ссылки на категорию на страницах-участниках.\n\n'
            'Пример: [[Категория:Старая|Ключ]] → [[Категория:Новая|Ключ]].\n'
            'Ключ сортировки после «|» сохраняется. Для «Шаблон:»/«Модуль:» проверяются также основная страница и её /doc.\n'
        )
        phase2_help = (
            'Категория в параметрах шаблонов (позиционные и именованные).\n\n'
            'Режимы:\n'
            '— Полные совпадения значения параметра: можно «Подтверждать/Пропускать все аналогичные».\n'
            '— Поиск по частям названия: каждый случай подтверждается в диалоге с предпросмотром и возможностью ручного правки.\n\n'
            'Выбранные правила автоприменения сохраняются и доступны через «Открыть/Очистить правила».\n'
            'Префикс «Категория:» в параметрах обычно опускают — это учитывается.'
        )
        locative_help = (
            'Обработка локативов в параметрах шаблонов.\n'
            'По умолчанию выключено; используйте, когда категории называются в одном падеже, но задаются в шаблоне через другой падеж (напр. {{МестоРождения|Москва}}).\n\n'
            'Если прямая категоризация не найдена, но шаблон присваивает категории через склонение названий\n'
            '(\"в Москве\" → категория \"Родившиеся в Москве\"), будет предложено правило замены для начальной формы (именительного падежа) значения параметра.\n'
        )
        
        # Первая опция: прямые ссылки
        row_p1 = QHBoxLayout()
        self.phase1_enabled_cb = QCheckBox('Обычное перемещение содержимого (категории прямо указаны на странице)')
        self.phase1_enabled_cb.setChecked(True)
        try:
            self.phase1_enabled_cb.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        except Exception:
            pass
        row_p1.addWidget(self.phase1_enabled_cb)
        add_info_button(self, row_p1, phase1_help, inline=True)
        try:
            row_p1.addStretch(1)
        except Exception:
            pass
        
        # Опция: переименовывать саму категорию
        row_move_cat = QHBoxLayout()
        self.move_members_cb = QCheckBox('Переименовывать страницы')
        self.move_members_cb.setChecked(True)
        try:
            self.move_members_cb.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        except Exception:
            pass
        row_move_cat.addWidget(self.move_members_cb)
        try:
            row_move_cat.addStretch(1)
        except Exception:
            pass
        
        # Вторая опция: параметры шаблонов
        row_p2 = QHBoxLayout()
        self.find_in_templates_cb = QCheckBox('Поиск и исправление категоризации через параметры шаблонов')
        self.find_in_templates_cb.setChecked(True)
        try:
            self.find_in_templates_cb.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        except Exception:
            pass
        row_p2.addWidget(self.find_in_templates_cb)
        add_info_button(self, row_p2, phase2_help, inline=True)
        try:
            row_p2.addStretch(1)
        except Exception:
            pass

        # Опция: Локативы
        row_loc = QHBoxLayout()
        self.locatives_cb = QCheckBox('Локативы (склонения в параметрах шаблонов)')
        self.locatives_cb.setChecked(False)
        try:
            self.locatives_cb.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        except Exception:
            pass
        row_loc.addWidget(self.locatives_cb)
        add_info_button(self, row_loc, locative_help, inline=True)
        try:
            row_loc.addStretch(1)
        except Exception:
            pass
        
        # Кнопки правил (будут прикреплены к правому заголовку)
        btn_show_rules = QPushButton('Показать правила замен')
        btn_clear_rules = QPushButton('Очистить правила')
        
        # Подключаем обработчики кнопок правил
        btn_show_rules.clicked.connect(self._open_rules_dialog)
        btn_clear_rules.clicked.connect(self._clear_rules)
        
        # Чекбоксы перенаправлений
        self.leave_cat_redirect_cb = QCheckBox('Оставлять перенаправления для категорий')
        try:
            self.leave_cat_redirect_cb.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        except Exception:
            pass
        self.leave_cat_redirect_cb.setChecked(False)
        
        self.leave_other_redirect_cb = QCheckBox('Оставлять перенаправления для остальных')
        try:
            self.leave_other_redirect_cb.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        except Exception:
            pass
        self.leave_other_redirect_cb.setChecked(True)
        
        # Управление доступностью перенаправлений по галке «Переименовывать страницы»
        try:
            self.leave_cat_redirect_cb.setEnabled(self.move_members_cb.isChecked())
            self.leave_other_redirect_cb.setEnabled(self.move_members_cb.isChecked())
            self.move_members_cb.toggled.connect(self.leave_cat_redirect_cb.setEnabled)
            self.move_members_cb.toggled.connect(self.leave_other_redirect_cb.setEnabled)
        except Exception:
            pass
        
        # Сетка 2x3 — выравнивание строк между колонками
        grid = QGridLayout()
        try:
            grid.setHorizontalSpacing(24)
            grid.setVerticalSpacing(4)
            grid.setContentsMargins(0, 0, 0, 0)
        except Exception:
            pass
        
        # Заголовки
        lbl_left = QLabel('<b>Переименование</b>')
        grid.addWidget(lbl_left, 0, 0)
        # Правый заголовок + кнопки правил справа
        right_header = QWidget()
        right_header_lay = QHBoxLayout(right_header)
        try:
            right_header_lay.setContentsMargins(0, 0, 0, 0)
            right_header_lay.setSpacing(6)
        except Exception:
            pass
        right_header_lay.addWidget(QLabel('<b>Перенос содержимого категорий</b>'))
        right_header_lay.addStretch(1)
        right_header_lay.addWidget(btn_show_rules)
        right_header_lay.addWidget(btn_clear_rules)
        grid.addWidget(right_header, 0, 1)
        
        # Утилита-обёртка для QHBoxLayout
        def _wrap(layout_obj):
            try:
                w = QWidget()
                lay = QHBoxLayout(w)
                lay.setContentsMargins(0, 0, 0, 0)
                lay.setSpacing(0)
                lay.addLayout(layout_obj)
                return w
            except Exception:
                return QWidget()
        
        # Левая колонка: 3 строки
        grid.addWidget(_wrap(row_move_cat), 1, 0)
        row_redirect_cat = QHBoxLayout()
        row_redirect_cat.addWidget(self.leave_cat_redirect_cb)
        row_redirect_cat.addStretch(1)
        row_redirect_other = QHBoxLayout()
        row_redirect_other.addWidget(self.leave_other_redirect_cb)
        row_redirect_other.addStretch(1)
        grid.addWidget(_wrap(row_redirect_cat), 2, 0)
        grid.addWidget(_wrap(row_redirect_other), 3, 0)
        
        # Правая колонка: 3 строки
        grid.addWidget(_wrap(row_p1), 1, 1)
        grid.addWidget(_wrap(row_p2), 2, 1)
        grid.addWidget(_wrap(row_loc), 3, 1)
        
        try:
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
        except Exception:
            pass
        v.addLayout(grid)

        # Комментарий к правке — перенесён вниз (между фильтром и прогрессом)
        self.rename_comment_edit = QLineEdit()
        try:
            self.rename_comment_edit.setPlaceholderText('Единый комментарий ко всем действиям (перезапишет комментарии из файла)')
        except Exception:
            pass

        # Постоянно видимые шкалы прогресса (создаём сейчас, добавим в нижний ряд)
        # Левая: общий прогресс по TSV
        self.rename_outer_label = QLabel('Обработано 0/0')
        self.rename_outer_bar = QProgressBar()
        try:
            self.rename_outer_bar.setMaximum(1)
            self.rename_outer_bar.setValue(0)
            self.rename_outer_bar.setTextVisible(False)
            self.rename_outer_bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            self.rename_outer_bar.setMinimumWidth(220)
        except Exception:
            pass
        # Правая: прогресс переноса участников текущей категории
        self.rename_inner_label = QLabel('Перенесено: 0/0')
        self.rename_inner_bar = QProgressBar()
        try:
            self.rename_inner_bar.setMaximum(1)
            self.rename_inner_bar.setValue(0)
            self.rename_inner_bar.setTextVisible(False)
            self.rename_inner_bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            self.rename_inner_bar.setMinimumWidth(220)
        except Exception:
            pass

        # Поле фильтра перенесем ниже — перед комментарием (см. ниже)
        
        # Лог выполнения: заголовок + легенда (ℹ)
        log_header_row = QHBoxLayout()
        log_header_row.addWidget(QLabel('<b>Лог выполнения:</b>'))
        v.addLayout(log_header_row)

        # Дерево лога вместо QTextEdit
        self.rename_log_tree = init_log_tree(self)

        from ..widgets.ui_helpers import create_tree_log_wrap
        rename_wrap = create_tree_log_wrap(self, self.rename_log_tree, with_header=False)
        v.addWidget(rename_wrap, 1)
        
        # Поле фильтрации переносимых страниц по заголовку (регулярное выражение)
        regex_help = (
            'Что делает фильтр:\n'
            '— Обрабатывает только содержимое категории (участники). На переименование из TSV не влияет.\n\n'
            'Простые примеры:\n'
            '— Содержит слово: «Москва».\n'
            '— Кусок внутри слова: «ана» (найдёт «Канал» и «Банан», но не «Аналог»).\n'
            '— Начинается с: «^Категория:».\n'
            '— Заканчивается на: «биологи$».\n'
            '— Между словами что‑то есть: «Москва.*река».\n'
            '— Две формы регистра: «(Москва|москва)» или короче «[Мм]осква».\n'
            '— Исключить все, где встречается слово/фрагмент в любой части: «^(?!.*слово).*$».\n'
            '  Игнорировать регистр: «^(?!.*(?i:слово)).*$».\n\n'
            'Регистр (большие/маленькие буквы):\n'
            '— По умолчанию учитывается.\n'
            '— Игнорировать регистр: «(?i)москва» или «(?i:скв)» или указывать через квадратные скобки.\n\n'
            'Отдельное слово:\n'
            '— «\\bключ\\b» — именно слово «ключ», работает и в начале/конце строки.\n\n'
            'Варианты и наборы букв:\n'
            '— Скобки ( … ): «москв(а|е|ой)» — варианты слова.\n'
            '— Квадратные скобки [ … ]: «[Мм]осква», «[0-9]», «[А-Яа-я]».\n\n'
            'Повторы части:\n'
            '— ( … )+ — один или больше раз: «(аб)+» → «аб», «абаб».\n'
            '— ( … )* — ноль или больше раз (может отсутствовать): «(аб)*».\n'
            'Памятка по символам:\n'
            '— ^ начало строки, $ конец строки, . любой символ, * сколько угодно, | «или».\n\n'
            'Если в названии есть «особые» символы (. * + ? ( ) [ ] { } | ^ $ \\):\n'
            '— Пишите с обратным слешем: \\. \\* \\+ \\? \\( \\) \\[ \\] \\{ \\} \\| \\^ \\$.\n\n'
            'Пустое поле — фильтр выключен.'
        )

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel('Фильтр содержимого категории по заголовкам:'))
        self.title_regex_edit = QLineEdit()
        try:
            self.title_regex_edit.setPlaceholderText('Укажите регулярное выражение для заголовков страниц, которые будут исключены из перемещния')
        except Exception:
            pass
        filter_row.addWidget(self.title_regex_edit, 1)
        add_info_button(self, filter_row, regex_help, inline=True)
        v.addLayout(filter_row)
        comment_row_bottom = QHBoxLayout()
        comment_row_bottom.addWidget(QLabel('Комментарий к правке (опционально):'))
        comment_row_bottom.addWidget(self.rename_comment_edit, 1)
        v.addLayout(comment_row_bottom)

        # Валидация регулярного выражения по вводу
        self._title_regex_valid = True
        try:
            self.title_regex_edit.textChanged.connect(self._on_title_regex_changed)
        except Exception:
            pass

        # Кнопки управления
        self.rename_btn = QPushButton('Начать переименование')
        self.rename_btn.clicked.connect(self.start_rename)
        
        self.rename_stop_btn = QPushButton('Остановить')
        self.rename_stop_btn.setEnabled(False)
        self.rename_stop_btn.clicked.connect(self.stop_rename)
        
        row_run = QHBoxLayout()
        # Группа прогресса, растягивается до кнопки «Начать»
        progress_wrap = QWidget()
        progress_layout = QHBoxLayout(progress_wrap)
        try:
            progress_layout.setContentsMargins(0, 0, 0, 0)
            progress_layout.setSpacing(6)
        except Exception:
            pass
        progress_layout.addWidget(self.rename_outer_label)
        progress_layout.addWidget(self.rename_outer_bar)
        try:
            progress_layout.setStretchFactor(self.rename_outer_label, 0)
            progress_layout.setStretchFactor(self.rename_outer_bar, 1)
        except Exception:
            pass
        progress_layout.addSpacing(12)
        progress_layout.addWidget(self.rename_inner_label)
        progress_layout.addWidget(self.rename_inner_bar)
        try:
            progress_layout.setStretchFactor(self.rename_inner_label, 0)
            progress_layout.setStretchFactor(self.rename_inner_bar, 1)
        except Exception:
            pass
        row_run.addWidget(progress_wrap, 1)
        row_run.addWidget(self.rename_btn)
        row_run.addWidget(self.rename_stop_btn)
        v.addLayout(row_run)
        
        # Устанавливаем соотношение размеров кнопок
        set_start_stop_ratio(self.rename_btn, self.rename_stop_btn, 3)

    def _on_title_regex_changed(self, _=None):
        """Проверяет валидность регулярного выражения и подсвечивает поле."""
        try:
            text = (self.title_regex_edit.text() or '').strip()
        except Exception:
            text = ''
        if not text:
            # Пусто — считается валидным (фильтр выключен)
            self._title_regex_valid = True
            try:
                self.title_regex_edit.setStyleSheet('')
                self.title_regex_edit.setToolTip('')
            except Exception:
                pass
            return
        try:
            import re as _re
            _re.compile(text)
            self._title_regex_valid = True
            try:
                self.title_regex_edit.setStyleSheet('')
                self.title_regex_edit.setToolTip('')
            except Exception:
                pass
        except Exception as e:
            self._title_regex_valid = False
            try:
                self.title_regex_edit.setStyleSheet('background-color:#fdecea')
                self.title_regex_edit.setToolTip(f'Ошибка RegEx: {e}')
            except Exception:
                pass
        
    def _open_rules_dialog(self):
        """Открыть файл правил в системе (папка configs/template_rules.json)"""
        try:
            # Унифицированное определение пути к правилам
            path = self._resolve_rules_path()
            
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not os.path.exists(path):
                with open(path, 'w', encoding='utf-8') as f:
                    f.write('{}')
            
            url = QUrl.fromLocalFile(os.path.abspath(path))
            ok = QDesktopServices.openUrl(url)
            if not ok:
                # Фолбэк для Windows: прямой запуск
                try:
                    os.startfile(path)
                    ok = True
                except Exception:
                    ok = False
            if not ok:
                # Фолбэк: открыть Проводник и выделить файл
                try:
                    import subprocess
                    subprocess.Popen(['explorer', '/select,', path])
                    ok = True
                except Exception:
                    ok = False
            if not ok:
                # Последняя попытка: открыть папку
                dir_url = QUrl.fromLocalFile(os.path.dirname(os.path.abspath(path)))
                ok = QDesktopServices.openUrl(dir_url)
            if not ok:
                raise RuntimeError('Не удалось открыть файл или папку с правилами')
        except Exception:
            QMessageBox.warning(self, 'Ошибка', 'Не удалось открыть файл правил замен.')
    
    def _clear_rules(self):
        """Очистить правила замен"""
        try:
            cleared = False
            # Очищаем через TemplateManager, если доступен у воркера
            w = getattr(self, 'mrworker', None)
            try:
                if w and hasattr(w, 'template_manager') and w.template_manager:
                    w.template_manager.clear_template_cache()
                    cleared = True
            except Exception:
                pass
            # Очистим и локальный UI‑кэш
            if hasattr(self, '_template_auto_cache_ui'):
                try:
                    self._template_auto_cache_ui.clear()
                    cleared = True or cleared
                except Exception:
                    pass
            # Фолбэк: принудительно записать пустой JSON в файл
            if not cleared:
                path = self._resolve_rules_path()
                if path:
                    try:
                        with open(path, 'w', encoding='utf-8') as f:
                            f.write('{}')
                        cleared = True or cleared
                    except Exception:
                        pass
            if cleared:
                QMessageBox.information(self, 'Готово', 'Правила замен очищены.')
            else:
                QMessageBox.information(self, 'Инфо', 'Кэш правил замен уже пуст.')
        except Exception:
            QMessageBox.warning(self, 'Ошибка', 'Не удалось очистить правила замен.')

    def _resolve_rules_path(self) -> str:
        """Единая точка получения пути к файлу правил шаблонов."""
        try:
            # 1) Через активный TemplateManager воркера
            w = getattr(self, 'mrworker', None)
            if w and hasattr(w, 'template_manager') and w.template_manager:
                p = w.template_manager.get_rules_file_path()
                if p:
                    return p
        except Exception:
            pass
        # 2) Вычислить путь по политике TemplateManager
        try:
            return TemplateManager.resolve_rules_file_path()
        except Exception:
            # 3) Жёсткий фолбэк: рядом с GUI модулем
            try:
                base = _dist_configs_dir()
            except Exception:
                base = os.path.join(os.path.dirname(__file__), 'configs')
            return os.path.join(base, 'template_rules.json')
    
    def start_rename(self):
        """Запускает процесс переименования"""
        debug(f'Start rename: file={self.rename_file_edit.text()}')
        
        if not self.rename_file_edit.text():
            QMessageBox.warning(self, 'Ошибка', 'Укажите TSV.')
            return
        
        # Получаем данные авторизации от родительского окна
        if not self.parent_window:
            QMessageBox.warning(self, 'Ошибка', 'Нет доступа к данным авторизации.')
            return
        
        # Получаем данные из родительского окна (будет реализовано в main_window)
        user = getattr(self.parent_window, 'current_user', None)
        pwd = getattr(self.parent_window, 'current_password', None)
        lang = getattr(self.parent_window, 'current_lang', 'ru')
        fam = getattr(self.parent_window, 'current_family', 'wikipedia')
        
        debug(f'Rename: получены данные авторизации - user={user}, pwd={"***" if pwd else None}, lang={lang}, fam={fam}')
        
        if not user or not pwd:
            debug(f'Rename: ошибка авторизации - user={user}, pwd={"установлен" if pwd else "не установлен"}')
            QMessageBox.warning(self, 'Ошибка', 'Необходимо войти в систему.')
            return
        
        # Предупреждение, если выбран «Авто» и в TSV, похоже, названия без префиксов
        try:
            ns_sel_preview = self.rename_ns_combo.currentData()
        except Exception:
            ns_sel_preview = 'auto'
        try:
            is_auto = isinstance(ns_sel_preview, str) and (ns_sel_preview or '').strip().lower() == 'auto'
        except Exception:
            is_auto = True

        if is_auto:
            try:
                import csv as _csv
                plain_rows = 0
                checked = 0
                with open(self.rename_file_edit.text(), newline='', encoding='utf-8-sig') as _f:
                    _reader = _csv.reader(_f, delimiter='\t')
                    for _row in _reader:
                        if len(_row) < 2:
                            continue
                        _old = (_row[0] or '').strip()
                        _new = (_row[1] or '').strip()
                        if not _old and not _new:
                            continue
                        checked += 1
                        if (':' not in _old) and (':' not in _new):
                            plain_rows += 1
                        if checked >= 30:
                            break
                if checked > 0 and plain_rows == checked:
                    # Информируем в лог
                    try:
                        info_msg = 'В файле заголовки без префиксов; «Авто» начнёт переименование статей.'
                        log_tree_add(self.rename_log_tree, datetime.now().strftime('%H:%M:%S'), None, info_msg, 'manual', 'info', None, None, True)
                    except Exception:
                        pass
                    msg = (
                        'В файле обнаружены заголовки без префиксов пространств имён.\n'
                        'В списке «Префиксы» выбран режим «Авто».\n\n'
                        'Будет запущено переименование обычных статей; перенос содержимого категорий выполнен не будет.\n\n'
                        'Вы уверены, что хотите продолжить?'
                    )
                    res = QMessageBox.question(self, 'Подтвердите запуск', msg, QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                    if res != QMessageBox.Yes:
                        try:
                            log_tree_add(self.rename_log_tree, datetime.now().strftime('%H:%M:%S'), None, 'Запуск отменён пользователем.', 'manual', 'info', None, None, True)
                        except Exception:
                            pass
                        return
                    else:
                        try:
                            log_tree_add(self.rename_log_tree, datetime.now().strftime('%H:%M:%S'), None, 'Подтверждено: запуск переименования статей без нормализации NS.', 'manual', 'info', None, None, True)
                        except Exception:
                            pass
            except Exception:
                # Любые ошибки эвристики не должны мешать запуску
                pass

        apply_pwb_config(lang, fam)

        # Блокируем кнопки и очищаем лог
        self.rename_btn.setEnabled(False)
        self.rename_stop_btn.setEnabled(True)
        try:
            self.rename_outer_bar.setVisible(True)
            self.rename_outer_label.setVisible(True)
            self.rename_outer_bar.setMaximum(1)
            self.rename_outer_bar.setValue(0)
            self.rename_inner_bar.setVisible(True)
            self.rename_inner_label.setVisible(True)
            self.rename_inner_bar.setMaximum(1)
            self.rename_inner_bar.setValue(0)
        except Exception:
            pass
        
        ns_sel = self.rename_ns_combo.currentData()
        
        # Валидация регулярного выражения фильтра
        title_regex = (self.title_regex_edit.text() or '').strip()
        if title_regex:
            try:
                import re as _re
                _re.compile(title_regex)
            except Exception as e:
                QMessageBox.warning(self, 'Ошибка', f'Некорректное регулярное выражение фильтра заголовков:\n{e}')
                return

        # Создаем и запускаем worker
        self.mrworker = RenameWorker(
            self.rename_file_edit.text(),
            user, pwd, lang, fam,
            ns_sel,
            self.leave_cat_redirect_cb.isChecked(),
            self.leave_other_redirect_cb.isChecked(),
            True,  # move_members - всегда True если хотя бы одна фаза включена
            self.find_in_templates_cb.isChecked(),
            self.phase1_enabled_cb.isChecked(),
            self.move_members_cb.isChecked(),
            (self.rename_comment_edit.text() or '').strip(),
            title_regex,
            self.locatives_cb.isChecked()
        )
        
        # Подключаем сигналы
        self.mrworker.progress.connect(lambda m: log_tree_parse_and_add(self.rename_log_tree, m))
        # Подключаем структурированные события
        try:
            self.mrworker.log_event.connect(lambda e: log_tree_add_event(self.rename_log_tree, e))
        except Exception:
            pass
        # Прогресс по файлу TSV
        try:
            self.mrworker.tsv_progress_init.connect(self._rename_outer_init)
            self.mrworker.tsv_progress_inc.connect(self._rename_outer_inc)
        except Exception:
            pass
        # Прогресс по участникам категории
        try:
            self.mrworker.inner_progress_init.connect(self._rename_inner_init)
            self.mrworker.inner_progress_inc.connect(self._rename_inner_inc)
            self.mrworker.inner_progress_reset.connect(self._rename_inner_reset)
        except Exception:
            pass
        self.mrworker.finished.connect(self._on_rename_finished)
        
        # Подключаем template review диалоги
        self.mrworker.template_review_request.connect(self._on_review_request)
        
        self.mrworker.start()
    
    def stop_rename(self):
        """Останавливает процесс переименования"""
        w = getattr(self, 'mrworker', None)
        if w and hasattr(w, 'isRunning') and w.isRunning():
            try:
                # Сначала мягкая остановка
                w.request_stop()
                # Если поток ещё активен — ждём недолго и жёстко завершаем
                if not w.wait(1200):
                    w.graceful_stop(5000)
            except Exception:
                try:
                    w.graceful_stop(5000)
                except Exception:
                    pass
    
    def _on_rename_finished(self):
        """Обработчик завершения процесса переименования"""
        self.rename_btn.setEnabled(True)
        self.rename_stop_btn.setEnabled(False)
        msg = 'Остановлено!' if getattr(self, 'mrworker', None) and getattr(self.mrworker, '_stop', False) else 'Переименование завершено!'
        try:
            # Служебное системное сообщение: статус ℹ️, без иконки объекта
            log_tree_add(self.rename_log_tree, datetime.now().strftime('%H:%M:%S'), None, msg, 'manual', 'info', None, None, True)
        except Exception:
            pass
        # Прогресс-бары остаются видимыми по требованию UX
        try:
            self.rename_outer_bar.setVisible(True)
            self.rename_outer_label.setVisible(True)
            self.rename_inner_bar.setVisible(True)
            self.rename_inner_label.setVisible(True)
        except Exception:
            pass

    def _rename_outer_init(self, total: int):
        try:
            self.rename_outer_bar.setVisible(True)
            self.rename_outer_label.setVisible(True)
            self.rename_outer_bar.setMaximum(max(1, int(total)))
            self.rename_outer_bar.setValue(0)
            try:
                self.rename_outer_label.setText(f"Обработано 0/{max(1, int(total))}")
            except Exception:
                pass
        except Exception:
            pass

    def _rename_outer_inc(self):
        try:
            val = self.rename_outer_bar.value() + 1
            self.rename_outer_bar.setValue(val)
            try:
                self.rename_outer_label.setText(f"Обработано {val}/{self.rename_outer_bar.maximum()}")
            except Exception:
                pass
        except Exception:
            pass

    def _rename_inner_init(self, total: int):
        try:
            self.rename_inner_bar.setVisible(True)
            self.rename_inner_label.setVisible(True)
            self.rename_inner_bar.setMaximum(max(1, int(total)))
            self.rename_inner_bar.setValue(0)
            try:
                self.rename_inner_label.setText(f"Перенесено: 0/{max(1, int(total))}")
            except Exception:
                pass
        except Exception:
            pass

    def _rename_inner_inc(self):
        try:
            val = self.rename_inner_bar.value() + 1
            self.rename_inner_bar.setValue(val)
            try:
                self.rename_inner_label.setText(f"Перенесено: {val}/{self.rename_inner_bar.maximum()}")
            except Exception:
                pass
        except Exception:
            pass

    def _rename_inner_reset(self):
        try:
            self.rename_inner_bar.setMaximum(1)
            self.rename_inner_bar.setValue(0)
            try:
                self.rename_inner_label.setText('')
            except Exception:
                pass
        except Exception:
            pass
    
    def _on_review_request(self, payload):
        """Обработчик запроса на проверку изменений в шаблоне"""
        try:
            debug(f'Получен запрос на диалог подтверждения: {payload}')
            
            # Добавим контекст проекта (family/lang) для корректных ссылок «открыть/история»
            try:
                fam = None
                lng = None
                try:
                    fam = getattr(self.mrworker, 'family', None)
                    lng = getattr(self.mrworker, 'lang', None)
                except Exception:
                    fam = None
                    lng = None
                if not fam:
                    fam = getattr(self, 'current_family', None) or (
                        getattr(self.parent_window, 'current_family', None)
                    ) or 'wikipedia'
                if not lng:
                    lng = getattr(self, 'current_lang', None) or (
                        getattr(self.parent_window, 'current_lang', None)
                    ) or 'ru'
                payload = dict(payload)
                payload['family'] = fam
                payload['lang'] = lng
            except Exception:
                pass

            # Показываем диалог проверки шаблона
            dialog = TemplateReviewDialog(self, payload)
            result = dialog.exec()
            
            debug(f'Результат диалога: {result}')
            
            # Отправляем результат обратно в worker
            # Если пользователь выбрал «Подтверждать все аналогичные» — пометим правило auto=approve
            # dedupe_mode передаём только если действительно есть предупреждение о дублях
            try:
                if (payload.get('dup_warning') and payload.get('dup_idx1') and payload.get('dup_idx2')):
                    dm_resp = dialog.get_dedupe_mode() if hasattr(dialog, 'get_dedupe_mode') else None
                else:
                    dm_resp = None
            except Exception:
                dm_resp = None
            response_data = {
                'req_id': payload.get('request_id'),
                'result': dialog.get_action(),
                'auto_confirm': dialog.get_auto_confirm(),
                'auto_skip': dialog.get_auto_skip(),
                'edited_template': dialog.get_edited_template() if hasattr(dialog, 'get_edited_template') else '',
                'dedupe_mode': dm_resp
            }
            
            debug(f'Отправляем ответ: {response_data}')
            
            # Установим флаг авто для правила сразу при подтверждении всех аналогичных
            try:
                if response_data.get('result') == 'apply' and response_data.get('auto_confirm'):
                    # Передаём в воркер через отдельный канал шаблон исходный/новый, чтобы он мог установить auto
                    if hasattr(self.mrworker, 'template_manager'):
                        tm = self.mrworker.template_manager
                        before = payload.get('template') or ''
                        after = response_data.get('edited_template') or (payload.get('proposed_template') or '')
                        if before and after:
                            # dedupe сохраняем только если в диалоге действительно были дубли
                            dm = response_data.get('dedupe_mode', None)
                            try:
                                if not (payload.get('dup_warning') and payload.get('dup_idx1') and payload.get('dup_idx2')):
                                    dm = None
                            except Exception:
                                dm = None
                            # Нормализуем режим дедупликации к left/right при необходимости
                            if dm == 'keep_first':
                                dm = 'left'
                            elif dm == 'keep_second':
                                dm = 'right'
                            tm.update_template_cache_from_edit(self.mrworker.family, self.mrworker.lang, before, after, 'approve', dm)
            except Exception:
                pass

            if hasattr(self.mrworker, 'review_response'):
                self.mrworker.review_response.emit(response_data)
            else:
                debug('Worker не имеет сигнала review_response')
                
        except Exception as e:
            debug(f'Ошибка в диалоге подтверждения шаблона: {e}')
            # При ошибке безопасно пропускаем кейс, не останавливая процесс
            try:
                # Логируем как ошибку, но продолжаем
                msg = f"Ошибка диалога подтверждения: {e}. Случай пропущен, продолжаем."
                log_tree_add(self.rename_log_tree, datetime.now().strftime('%H:%M:%S'), None, msg, 'manual', 'error', None, None, True)
            except Exception:
                pass
            response_data = {
                'req_id': payload.get('request_id'),
                'result': 'skip',
                'skip_reason': 'dialog_error',
                'error': str(e)
            }
            if hasattr(self.mrworker, 'review_response'):
                self.mrworker.review_response.emit(response_data)
    
    def update_language(self, lang: str):
        """Обновляет язык интерфейса и настройки"""
        # Обновляем комбобокс пространств имен
        if self.parent_window:
            family = getattr(self.parent_window, 'current_family', None) or (
                getattr(getattr(self.parent_window, 'auth_tab', None), 'family_combo', None).currentText()
                if getattr(getattr(self.parent_window, 'auth_tab', None), 'family_combo', None) else 'wikipedia'
            )
            try:
                nm = getattr(self.parent_window, 'namespace_manager', None)
                if nm:
                    nm.populate_ns_combo(self.rename_ns_combo, family, lang)
            except Exception:
                pass
    
    def update_family(self, family: str):
        """Обновляет семейство проектов"""
        if self.parent_window:
            lang = getattr(self.parent_window, 'current_lang', None) or (
                getattr(getattr(self.parent_window, 'auth_tab', None), 'lang_combo', None).currentText()
                if getattr(getattr(self.parent_window, 'auth_tab', None), 'lang_combo', None) else 'ru'
            )
            try:
                nm = getattr(self.parent_window, 'namespace_manager', None)
                if nm:
                    nm.populate_ns_combo(self.rename_ns_combo, family, lang)
            except Exception:
                pass
    
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