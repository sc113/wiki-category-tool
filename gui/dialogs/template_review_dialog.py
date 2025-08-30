"""
Template Review Dialog Module

Выделенный диалог для проверки изменений в шаблонах при переименовании категорий.
Заменяет встроенные template review диалоги из RenameWorker.
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QPlainTextEdit, QDialogButtonBox, QFrame, QGroupBox, QWidget,
    QRadioButton, QButtonGroup
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QKeySequence, QShortcut
import html
import urllib.parse


class TemplateReviewDialog(QDialog):
    """
    Диалог для проверки изменений в шаблонах при переименовании категорий.
    
    Поддерживает два режима:
    - direct: прямые совпадения в параметрах шаблона
    - partial: частичные замены в параметрах шаблона
    """
    
    # Сигналы для взаимодействия с worker'ом
    response_ready = Signal(dict)
    
    def __init__(self, parent=None, request_data=None):
        super().__init__(parent)
        
        # Данные запроса
        self.request_data = request_data or {}
        self.request_id = self.request_data.get('request_id', '')
        self.page_title = self.request_data.get('page_title', '')
        self.template_str = self.request_data.get('template', '')
        self.old_full = self.request_data.get('old_full', '')
        self.new_full = self.request_data.get('new_full', '')
        self.mode = self.request_data.get('mode', 'direct')  # 'direct' или 'partial'
        self.proposed_template = self.request_data.get('proposed_template', '')
        self.old_sub = self.request_data.get('old_sub', '')
        self.new_sub = self.request_data.get('new_sub', '')
        self.old_direct = self.request_data.get('old_direct', '')
        self.new_direct = self.request_data.get('new_direct', '')
        # Дубли безымянных параметров (опционально)
        self.dup_warning = bool(self.request_data.get('dup_warning', False))
        try:
            self.dup_idx1 = int(self.request_data.get('dup_idx1', 0))
            self.dup_idx2 = int(self.request_data.get('dup_idx2', 0))
        except Exception:
            self.dup_idx1, self.dup_idx2 = 0, 0
        self.selected_dedupe_mode = 'keep_both'
        
        # Результат диалога
        self.result_action = 'cancel'
        self.auto_confirm_all = False
        self.auto_skip_all = False
        self.edited_template = None
        
        self.setup_ui()
        self.setup_connections()
    
    def setup_ui(self):
        """Настройка пользовательского интерфейса"""
        # Заголовок: "Замена по параметрам шаблона"
        self.setWindowTitle("Замена по параметрам шаблона")
        
        # Размер: 900x700 с возможностью изменения
        self.resize(900, 700)
        self.setSizeGripEnabled(True)
        
        # Основной layout
        layout = QVBoxLayout(self)
        
        # Создаем header с информацией о переименовании (если есть page_title)
        if self.page_title:
            self.create_header_section(layout)
        
        # Добавляем отступ
        layout.addSpacing(6)
        
        # Сообщение о типе замены
        layout.addWidget(QLabel('<b>Сообщение:</b>'))
        
        is_direct = (self.mode == 'direct')
        message_text = (
            "Категория на странице не найдена напрямую. Обнаружено совпадение в параметрах шаблона."
            if is_direct else
            "Категория на странице не найдена напрямую. Обнаружены совпадения по частям в параметрах шаблона. "
            "Проверьте и при необходимости подредактируйте."
        )
        
        msg_label = QLabel(message_text)
        msg_label.setWordWrap(True)
        layout.addWidget(msg_label)
        
        # Создаем блоки с исходным и предлагаемым вызовом
        self.create_template_sections(layout)

        # Блок предупреждения о дублях позиционных параметров
        if self.dup_warning and self.dup_idx1 and self.dup_idx2:
            self.create_dedupe_section(layout)
        
        # Поле для ручного редактирования
        layout.addSpacing(6)
        layout.addWidget(QLabel('<b>Ручное редактирование:</b>'))
        
        self.edit_field = QPlainTextEdit()
        self.setup_edit_field()
        layout.addWidget(self.edit_field)
        
        # Панель управления с кнопками
        self.create_control_panel(layout)

    def create_dedupe_section(self, layout):
        """Блок предупреждения о дублях и выбор политики дедупликации."""
        box = QGroupBox("Дубликаты позиционных параметров")
        try:
            box.setStyleSheet(
                "QGroupBox { border:1px solid #f59e0b; border-radius:6px; margin-top: 10px; } "
                "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }"
            )
            box.setToolTip(
                "В позиционных параметрах получатся одинаковые значения.\n"
                "Выберите, что оставить при сохранении и в будущих аналогичных случаях."
            )
        except Exception:
            pass
        v = QVBoxLayout(box)
        msg = QLabel(f"<b>Обнаружено два одинаковых значения</b> в параметрах {self.dup_idx1} и {self.dup_idx2}.")
        msg.setWordWrap(True)
        v.addWidget(msg)

        hint = QLabel(
            "При подтверждении можно: оставить оба значения, или удалить одно из дубликатов.\n"
            "Выбранная политика будет сохранена в правила и применяться автоматически."
        )
        try:
            hint.setStyleSheet("color:#6b7280;font-size:12px")
        except Exception:
            pass
        hint.setWordWrap(True)
        v.addWidget(hint)

        # Радио‑кнопки выбора
        rb_keep_both = QRadioButton('Оставить оба')
        rb_keep_first = QRadioButton('Оставлять параметр слева')
        rb_keep_second = QRadioButton('Оставлять параметр справа')
        try:
            rb_keep_both.setToolTip('Ничего не удалять: оба одинаковых значения останутся')
            rb_keep_first.setToolTip('Удалить правый дубликат и оставить левый (первый)')
            rb_keep_second.setToolTip('Удалить левый дубликат и оставить правый (последний)')
        except Exception:
            pass
        rb_keep_both.setChecked(True)

        self._dedupe_group = QButtonGroup(self)
        self._dedupe_group.addButton(rb_keep_both, 0)
        self._dedupe_group.addButton(rb_keep_first, 1)
        self._dedupe_group.addButton(rb_keep_second, 2)

        v.addWidget(rb_keep_both)
        v.addWidget(rb_keep_first)
        v.addWidget(rb_keep_second)

        layout.addWidget(box)
    
    def create_header_section(self, layout):
        """Создание header секции с информацией о переименовании"""
        # Определяем family и lang из контекста (можно передать в request_data)
        family = self.request_data.get('family', 'wikipedia')
        lang = self.request_data.get('lang', 'ru')
        
        # Верхний блок-«карточка» с информацией о переименовании категории
        header = QFrame()
        header.setObjectName('reviewHeader')
        header.setStyleSheet(
            "QFrame#reviewHeader { "
            "background:#f8fafc; border:1px solid #e5e7eb; border-radius:10px; "
            "} QLabel { font-size:13px; }"
        )
        
        hlay = QVBoxLayout(header)
        hlay.setContentsMargins(12, 10, 12, 10)
        hlay.setSpacing(4)
        
        # Строим URLs
        host = self.build_host(family, lang)
        
        old_url = f"https://{host}/wiki/" + urllib.parse.quote(self.old_full.replace(' ', '_'))
        old_hist = f"https://{host}/w/index.php?title=" + urllib.parse.quote(self.old_full.replace(' ', '_')) + "&action=history"
        new_url = f"https://{host}/wiki/" + urllib.parse.quote(self.new_full.replace(' ', '_'))
        new_hist = f"https://{host}/w/index.php?title=" + urllib.parse.quote(self.new_full.replace(' ', '_')) + "&action=history"
        
        # Старая категория
        move1 = QLabel(f"❌ {html.escape(self.old_full)} (<a href='{old_url}'>открыть</a> · <a href='{old_hist}'>история</a>)")
        move1.setTextFormat(Qt.RichText)
        move1.setWordWrap(True)
        move1.setTextInteractionFlags(Qt.TextBrowserInteraction)
        move1.setOpenExternalLinks(True)
        
        # Новая категория
        move2 = QLabel(f"✅ {html.escape(self.new_full)} (<a href='{new_url}'>открыть</a> · <a href='{new_hist}'>история</a>)")
        move2.setTextFormat(Qt.RichText)
        move2.setWordWrap(True)
        move2.setTextInteractionFlags(Qt.TextBrowserInteraction)
        move2.setOpenExternalLinks(True)
        
        hlay.addWidget(move1)
        hlay.addWidget(move2)
        
        # Название страницы внутри карточки
        page_url = f"https://{host}/wiki/" + urllib.parse.quote(self.page_title.replace(' ', '_'))
        history_url = f"https://{host}/w/index.php?title=" + urllib.parse.quote(self.page_title.replace(' ', '_')) + "&action=history"
        
        page_line = QLabel(
            f"⚜️ {html.escape(self.page_title)} (<a href='{page_url}'>открыть</a> · <a href='{history_url}'>история</a>)"
        )
        page_line.setTextFormat(Qt.RichText)
        page_line.setWordWrap(True)
        page_line.setTextInteractionFlags(Qt.TextBrowserInteraction)
        page_line.setOpenExternalLinks(True)
        
        hlay.addSpacing(4)
        hlay.addWidget(page_line)
        
        layout.addWidget(header)
        layout.addSpacing(6)
    
    def create_template_sections(self, layout):
        """Создание секций с исходным и предлагаемым шаблоном"""
        # Подготавливаем highlighted версии
        highlighted_old, highlighted_new = self.prepare_highlighted_templates()
        
        # Отступ перед блоком «Исходный вызов»
        layout.addSpacing(6)
        
        lbl_old = QLabel(
            f"<b>Исходный вызов:</b><br/>"
            f"<div style='font-family:Consolas,\"Courier New\",monospace;background:#f6f8fa;"
            f"border:1px solid #e1e4e8;border-radius:6px;padding:2px 8px 2px 8px;margin:0'>"
            f"{highlighted_old}</div>"
        )
        lbl_old.setTextFormat(Qt.RichText)
        layout.addWidget(lbl_old)
        
        # Отступ перед блоком «Предлагаемая замена»
        layout.addSpacing(6)
        
        lbl_new = QLabel(
            f"<b>Предлагаемая замена:</b><br/>"
            f"<div style='font-family:Consolas,\"Courier New\",monospace;background:#ecfdf5;"
            f"border:1px solid #d1fae5;border-radius:6px;padding:2px 8px 2px 8px;margin:0'>"
            f"{highlighted_new}</div>"
        )
        lbl_new.setTextFormat(Qt.RichText)
        layout.addWidget(lbl_new)
    
    def prepare_highlighted_templates(self):
        """Подготовка highlighted версий шаблонов с подсветкой изменений"""
        esc_tmpl = html.escape(self.template_str)
        
        if self.mode == 'direct':
            # Прямые совпадения
            old_direct = self.old_direct
            new_direct = self.new_direct
            
            esc_old_direct = html.escape(old_direct)
            esc_new_direct = html.escape(new_direct)
            
            # Подсветка изменений: зеленый цвет для новых значений
            highlighted_old = esc_tmpl.replace(
                esc_old_direct, 
                f"<span style='color:#8b0000;font-weight:bold'>{esc_old_direct}</span>"
            )
            
            proposed_raw = self.template_str.replace(old_direct, new_direct, 1)
            highlighted_new = html.escape(proposed_raw).replace(
                esc_new_direct, 
                f"<span style='color:#0b6623;font-weight:bold'>{esc_new_direct}</span>"
            )
        else:
            # Частичные замены
            old_sub = self.old_sub
            new_sub = self.new_sub
            
            esc_old_sub = html.escape(old_sub)
            esc_new_sub = html.escape(new_sub)
            
            highlighted_old = esc_tmpl
            if esc_old_sub:
                highlighted_old = highlighted_old.replace(
                    esc_old_sub, 
                    f"<span style='color:#8b0000;font-weight:bold'>{esc_old_sub}</span>"
                )
            
            proposed_template = self.proposed_template or (
                self.template_str.replace(old_sub, new_sub, 1) if old_sub and new_sub else self.template_str
            )
            esc_prop = html.escape(proposed_template)
            highlighted_new = esc_prop
            
            if esc_new_sub:
                highlighted_new = highlighted_new.replace(
                    esc_new_sub, 
                    f"<span style='color:#0b6623;font-weight:bold'>{esc_new_sub}</span>"
                )
        
        return highlighted_old, highlighted_new
    
    def setup_edit_field(self):
        """Настройка поля для ручного редактирования"""
        # Устанавливаем начальный текст
        if self.mode == 'direct':
            initial_text = self.template_str.replace(
                self.old_direct or self.old_full, 
                self.new_direct or self.new_full, 1
            )
        else:
            initial_text = self.proposed_template or (
                self.template_str.replace(self.old_sub, self.new_sub, 1) 
                if self.old_sub and self.new_sub else self.template_str
            )
        
        self.edit_field.setPlainText(initial_text)
        self.edit_field.setMinimumHeight(160)
        
        # Моноширинный шрифт
        mono = QFont('Consolas')
        mono.setStyleHint(QFont.Monospace)
        mono.setFixedPitch(True)
        self.edit_field.setFont(mono)
    
    def create_control_panel(self, layout):
        """Создание панели управления с кнопками"""
        # Нижняя панель управления: слева — массовые действия, справа — стандартные кнопки
        controls = QHBoxLayout()
        
        # Группа массовых действий
        mass_group = QGroupBox("Массовые действия")
        mass_group.setStyleSheet(
            "QGroupBox { border: 1px solid lightgray; border-radius: 5px; margin-top: 10px; } "
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px 0 5px; }"
        )
        
        mass_layout = QHBoxLayout(mass_group)
        mass_layout.setContentsMargins(8, 8, 8, 8)
        mass_layout.setSpacing(6)
        
        # Чекбоксы: "Автоподтверждать прямые совпадения", "Автопропускать все"
        self.btn_confirm_all = QPushButton('Подтверждать все аналогичные')
        self.btn_skip_all = QPushButton('Пропускать все аналогичные')
        
        mass_layout.addWidget(self.btn_confirm_all)
        mass_layout.addWidget(self.btn_skip_all)
        
        controls.addWidget(mass_group)
        controls.addStretch()
        
        # Кнопки: "Подтвердить и сохранить", "Пропустить", "Отмена"
        # QDialogButtonBox с правильными ролями кнопок
        button_box = QDialogButtonBox()
        
        self.btn_confirm = QPushButton('Подтвердить и сохранить')
        self.btn_skip = QPushButton('Пропустить')
        self.btn_cancel = QPushButton('Отмена')
        
        button_box.addButton(self.btn_confirm, QDialogButtonBox.AcceptRole)
        button_box.addButton(self.btn_skip, QDialogButtonBox.ActionRole)
        button_box.addButton(self.btn_cancel, QDialogButtonBox.RejectRole)
        
        controls.addWidget(button_box)
        layout.addLayout(controls)
        
        # Горячие клавиши: Enter = подтвердить, Esc = отмена
        QShortcut(QKeySequence(Qt.Key_Return), self, activated=self.on_confirm)
        QShortcut(QKeySequence(Qt.Key_Enter), self, activated=self.on_confirm)
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self.on_cancel)
        
        # Устанавливаем кнопку по умолчанию
        self.btn_confirm.setAutoDefault(True)
        self.btn_confirm.setDefault(True)
        self.btn_confirm.setFocus()
    
    def setup_connections(self):
        """Настройка соединений сигналов и слотов"""
        self.btn_confirm.clicked.connect(self.on_confirm)
        self.btn_skip.clicked.connect(self.on_skip)
        self.btn_cancel.clicked.connect(self.on_cancel)
        self.btn_confirm_all.clicked.connect(self.on_confirm_all)
        self.btn_skip_all.clicked.connect(self.on_skip_all)
        
        # Обработка закрытия диалога выполняется через переопределённый reject()
    
    def on_confirm(self):
        """Подтвердить и сохранить"""
        self.result_action = 'confirm'

        # Сохраним выбранный режим дедупликации (если показывался блок)
        try:
            if hasattr(self, '_dedupe_group') and self.dup_warning:
                checked_id = self._dedupe_group.checkedId()
                if checked_id == 1:
                    self.selected_dedupe_mode = 'left'
                elif checked_id == 2:
                    self.selected_dedupe_mode = 'right'
                else:
                    self.selected_dedupe_mode = 'keep_both'
        except Exception:
            self.selected_dedupe_mode = 'keep_both'
        
        # Проверяем, было ли отредактировано содержимое
        current_text = self.edit_field.toPlainText().strip()
        if current_text != self.template_str:
            self.edited_template = current_text
        
        self.accept()
    
    def on_skip(self):
        """Пропустить"""
        self.result_action = 'skip'
        self.accept()
    
    def on_cancel(self):
        """Отмена"""
        self.result_action = 'cancel'
        self.reject()
    
    def reject(self):
        """Безопасное закрытие диалога как отмены без рекурсии."""
        self.result_action = 'cancel'
        try:
            return super().reject()
        except Exception:
            from PySide6.QtWidgets import QDialog as _QDialog
            return _QDialog.reject(self)
    
    def on_confirm_all(self):
        """Автоподтверждение работает для прямых совпадений"""
        self.auto_confirm_all = True
        self.on_confirm()
    
    def on_skip_all(self):
        """Автопропуск работает для всех последующих диалогов"""
        self.auto_skip_all = True
        self.on_skip()
    
    def get_result(self):
        """
        Возврат правильного результата: 'confirm', 'skip', 'cancel'
        Сохранение template rules в кэш при подтверждении
        """
        payload = {
            'request_id': self.request_id,
            'action': self.result_action
        }
        
        if self.result_action == 'confirm' and self.edited_template is not None:
            payload['edited_template'] = self.edited_template
        
        if self.auto_confirm_all:
            payload['auto_confirm_all'] = True
        
        if self.auto_skip_all:
            payload['auto_skip_all'] = True

        # Возвращаем выбранную политику дедупликации (если блок отображался)
        if self.dup_warning and self.dup_idx1 and self.dup_idx2:
            payload['dedupe_mode'] = self.selected_dedupe_mode
        
        return payload

    # Совместимость с вызывающим кодом (_on_review_request) — ожидаются эти геттеры
    def get_action(self) -> str:
        """Вернуть действие в формате, ожидаемом worker: 'apply' | 'skip' | 'cancel'."""
        if self.result_action == 'confirm':
            return 'apply'
        return self.result_action or 'cancel'

    def get_auto_confirm(self) -> bool:
        """Вернуть флаг «Подтверждать все аналогичные» для прямых совпадений."""
        return bool(self.auto_confirm_all)

    def get_auto_skip(self) -> bool:
        """Вернуть флаг «Пропускать все аналогичные» для следующих диалогов."""
        return bool(self.auto_skip_all)

    def get_edited_template(self) -> str:
        """Вернуть отредактированный текст шаблона (если менялся)."""
        return self.edited_template or ''

    def get_dedupe_mode(self) -> str:
        """Вернуть выбранный режим дедупликации."""
        return self.selected_dedupe_mode
    
    @staticmethod
    def build_host(family: str, lang: str) -> str:
        """Построение хоста для проекта Wikimedia"""
        fam = (family or 'wikipedia').strip()
        lng = (lang or 'ru').strip()
        
        if fam == 'commons':
            return 'commons.wikimedia.org'
        elif fam == 'wikidata':
            return 'www.wikidata.org'
        elif fam == 'meta':
            return 'meta.wikimedia.org'
        else:
            return f'{lng}.{fam}.org'


def show_template_review_dialog(parent=None, request_data=None):
    """
    Функция для показа диалога проверки шаблонов.
    
    Args:
        parent: Родительский виджет
        request_data: Данные запроса с информацией о шаблоне
    
    Returns:
        dict: Результат диалога с действием пользователя
    """
    try:
        dialog = TemplateReviewDialog(parent, request_data)
        result = dialog.exec()
        if result == QDialog.Accepted:
            return dialog.get_result()
        else:
            return {
                'request_id': request_data.get('request_id', '') if request_data else '',
                'action': 'cancel'
            }
    except RecursionError:
        # Безопасный фолбэк, чтобы не валить поток при редких рекурсивных раскладках
        return {
            'request_id': request_data.get('request_id', '') if request_data else '',
            'action': 'cancel'
        }