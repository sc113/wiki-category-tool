"""
Template Review Dialog Module

Выделенный диалог для проверки изменений в шаблонах при переименовании категорий.
Заменяет встроенные template review диалоги из RenameWorker.
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QPlainTextEdit, QDialogButtonBox, QFrame, QGroupBox, QWidget,
    QRadioButton, QButtonGroup, QSizePolicy
)
from PySide6.QtCore import Qt, Signal, QPropertyAnimation, QEasingCurve, QTimer
from PySide6.QtGui import QFont, QKeySequence, QShortcut, QTextOption
import html
import urllib.parse


# UI/Animation constants
ANIM_DURATION_MS = 250            # Длительность анимаций
RESIZE_DELAY_MS = 50              # Задержка перед авто-изменением размера
SHRINK_DELAY_MS = 250             # Задержка перед авто-сжатием после сворачивания
MAX_WIDGET_HEIGHT = 16777215      # Практически «безлимитная» высота виджета в Qt


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
        
        # Анимации для плавного сворачивания
        self.animations = []
        
        self.setup_ui()
        self.setup_connections()
    
    def setup_ui(self):
        """Настройка пользовательского интерфейса"""
        # Заголовок: "Замена по параметрам шаблона"
        self.setWindowTitle("Замена по параметрам шаблона")
        
        # Размер по умолчанию; дальнейшее распределение свободного места — в пользу редактора
        self.resize(900, 700)
        self.setSizeGripEnabled(True)
        try:
            # Минимальный размер окна
            self.setMinimumSize(440, 260)
        except Exception:
            pass
        
        # Основной layout
        layout = QVBoxLayout(self)
        try:
            layout.setContentsMargins(10, 8, 10, 10)
            layout.setSpacing(6)
        except Exception:
            pass
        
        # Создаем header с информацией о переименовании (если есть page_title)
        if self.page_title:
            self.create_header_section(layout)
        
        # Компактный отступ
        layout.addSpacing(4)
        
        # Сообщение о типе замены (плотный блок: заголовок + сообщение без промежутков)
        is_direct = (self.mode == 'direct')
        is_partial = (self.mode == 'partial')
        is_locative = (self.mode == 'locative')

        msg_wrap = QWidget()
        msg_box = QVBoxLayout(msg_wrap)
        msg_box.setContentsMargins(0, 0, 0, 0)
        msg_box.setSpacing(0)

        _msg_title = QLabel('<b>Сообщение:</b>')
        try:
            _msg_title.setStyleSheet('margin:0')
        except Exception:
            pass
        msg_box.addWidget(_msg_title)

        if is_locative:
            # Красная крупная строка + пояснение
            red = QLabel("<span style='color:#b91c1c;font-weight:bold;font-size:16px'>Обнаружено применение локативов в параметрах шаблона</span>")
            red.setWordWrap(True)
            try:
                red.setStyleSheet('margin:0')
            except Exception:
                pass
            msg_box.addWidget(red)
            msg_box.addSpacing(6)
            desc = QLabel("С высокой вероятностью <b>нужны исправления вручную</b>. Ниже предлагается замена через автоматический подбор эвристик (логика на основе Шаблон:Локатив); проверьте корректность и при необходимости внесите исправления.")
            desc.setWordWrap(True)
            try:
                desc.setStyleSheet('margin:0')
            except Exception:
                pass
            msg_box.addWidget(desc)
        elif is_partial:
            amber = QLabel("<b>Обнаружены совпадения по частям</b>")
            amber.setWordWrap(True)
            try:
                amber.setStyleSheet('margin:0')
            except Exception:
                pass
            msg_box.addWidget(amber)
            msg_box.addSpacing(6)
            desc = QLabel("Категория на странице не найдена напрямую. Проверьте и при необходимости подредактируйте предложенную замену.")
            desc.setWordWrap(True)
            try:
                desc.setStyleSheet('margin:0')
            except Exception:
                pass
            msg_box.addWidget(desc)
        else:
            basic = QLabel("Категория на странице не найдена напрямую. Обнаружено совпадение в параметрах шаблона.")
            basic.setWordWrap(True)
            try:
                basic.setStyleSheet('margin:0')
            except Exception:
                pass
            msg_box.addWidget(basic)

        layout.addWidget(msg_wrap)
        layout.addSpacing(6)
        
        # Создаем блоки с исходным и предлагаемым вызовом
        self.create_template_sections(layout)
        try:
            # Блоки превью занимают минимум, остальное вверх не растягивается — всю высоту отдаём редактору
            layout.setStretch(0, 0)  # header
            layout.setStretch(1, 0)  # spacing
            layout.setStretch(2, 0)  # "Сообщение:" label
            layout.setStretch(3, 0)  # текст сообщения
            layout.setStretch(4, 0)  # превью 1
            layout.setStretch(5, 0)  # превью 2
        except Exception:
            pass

        # Блок предупреждения о дублях позиционных параметров
        if self.dup_warning and self.dup_idx1 and self.dup_idx2:
            self.create_dedupe_section(layout)
        
        # Поле для ручного редактирования
        layout.addSpacing(2)
        
        # Заголовок с кнопкой сворачивания для редактора
        header3_layout = QHBoxLayout()
        header3_layout.setContentsMargins(0, 0, 0, 0)
        header3_layout.addWidget(QLabel('<b>Ручное редактирование:</b>'))
        self.btn_collapse_edit = QPushButton('−')
        self.btn_collapse_edit.setFixedSize(20, 20)
        self.btn_collapse_edit.setToolTip('Свернуть/развернуть блок')
        header3_layout.addWidget(self.btn_collapse_edit)
        header3_layout.addStretch()
        layout.addLayout(header3_layout)
        
        self.edit_field = QPlainTextEdit()
        self.setup_edit_field()
        self.btn_collapse_edit.clicked.connect(lambda: self._toggle_block(self.edit_field, self.btn_collapse_edit))
        layout.addWidget(self.edit_field)
        try:
            # Свободное место отдаём под редактор
            layout.setStretchFactor(self.edit_field, 1)
        except Exception:
            pass
        
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
        hlay.setContentsMargins(10, 8, 10, 8)
        hlay.setSpacing(2)
        
        # Строим URLs
        host = self.build_host(family, lang)
        
        # Создаем ссылки для категорий
        old_url, old_hist = self._build_page_urls(host, self.old_full)
        new_url, new_hist = self._build_page_urls(host, self.new_full)
        
        # Старая и новая категории
        move1 = self._create_link_label(f"❌ {html.escape(self.old_full)}", old_url, old_hist)
        move2 = self._create_link_label(f"✅ {html.escape(self.new_full)}", new_url, new_hist)
        
        hlay.addWidget(move1)
        hlay.addWidget(move2)
        
        # Название страницы внутри карточки
        page_url, history_url = self._build_page_urls(host, self.page_title)
        
        page_line = self._create_link_label(f"⚜️ {html.escape(self.page_title)}", page_url, history_url)
        
        hlay.addSpacing(4)
        hlay.addWidget(page_line)
        
        layout.addWidget(header)
        layout.addSpacing(4)
    
    def create_template_sections(self, layout):
        """Создание секций с исходным и предлагаемым шаблоном"""
        # Подготавливаем highlighted версии
        highlighted_old, highlighted_new = self.prepare_highlighted_templates()
        # Вставляем мягкие переносы после разделителей, чтобы узкое окно не разъезжалось
        highlighted_old = self._add_soft_wraps(highlighted_old)
        highlighted_new = self._add_soft_wraps(highlighted_new)
        
        # Создаем блоки
        layout.addSpacing(4)
        self._create_template_block(layout, 'Исходный вызов', highlighted_old, '#f6f8fa', '#e1e4e8', 'old')
        layout.addSpacing(4)
        self._create_template_block(layout, 'Предлагаемая замена', highlighted_new, '#ecfdf5', '#d1fae5', 'new')
    
    def prepare_highlighted_templates(self):
        """Подготовка highlighted версий шаблонов с подсветкой изменений"""
        esc_tmpl = html.escape(self.template_str)
        
        if self.mode in ('direct', 'locative'):
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
    
    def _add_soft_wraps(self, text: str) -> str:
        """Добавляет мягкие переносы после разделителей для корректного отображения в узких окнах"""
        try:
            zwsp = '&#8203;'
            return text.replace('|', '|' + zwsp)
        except Exception:
            return text
    
    def _create_template_block(self, layout, title: str, content: str, bg_color: str, border_color: str, block_type: str):
        """Создает блок с шаблоном и кнопкой сворачивания"""
        html_content = (
            f"<div style='font-family:Consolas,\"Courier New\",monospace;background:{bg_color};"
            f"border:1px solid {border_color};border-radius:6px;padding:2px 8px 2px 8px;margin:2px 0 0 0'>"
            f"{content}</div>"
        )
        
        lbl = QLabel(html_content)
        lbl.setTextFormat(Qt.RichText)
        lbl.setWordWrap(True)
        lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        try:
            lbl.setStyleSheet('margin:5')
        except Exception:
            pass

        # Создаем контейнер с заголовком и кнопкой
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)
        
        # Заголовок с кнопкой сворачивания
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.addWidget(QLabel(f'<b>{title}:</b>'))
        
        btn = QPushButton('−')
        btn.setFixedSize(20, 20)
        btn.setToolTip('Свернуть/развернуть блок')
        btn.clicked.connect(lambda: self._toggle_block(lbl, btn))
        header_layout.addWidget(btn)
        header_layout.addStretch()
        
        # Сохраняем ссылку на кнопку для доступа извне
        setattr(self, f'btn_collapse_{block_type}', btn)
        
        container_layout.addLayout(header_layout)
        container_layout.addWidget(lbl)
        container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(container)
    
    def _build_page_urls(self, host: str, page_title: str) -> tuple[str, str]:
        """Строит URL для страницы и её истории"""
        encoded_title = urllib.parse.quote(page_title.replace(' ', '_'))
        page_url = f"https://{host}/wiki/{encoded_title}"
        history_url = f"https://{host}/w/index.php?title={encoded_title}&action=history"
        return page_url, history_url
    
    def _create_link_label(self, text: str, page_url: str, history_url: str) -> QLabel:
        """Создает QLabel со ссылками на страницу и историю"""
        label_text = f"{text} (<a href='{page_url}'>открыть</a> · <a href='{history_url}'>история</a>)"
        label = QLabel(label_text)
        label.setTextFormat(Qt.RichText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextBrowserInteraction)
        label.setOpenExternalLinks(True)
        return label
    
    def _toggle_block(self, widget, button):
        """Переключает видимость блока и меняет символ кнопки"""
        try:
            if widget.isVisible():
                # Сворачиваем с анимацией
                self._animate_collapse(widget, button)
            else:
                # Разворачиваем с анимацией
                self._animate_expand(widget, button)
        except Exception:
            pass
    
    def _auto_resize_if_needed(self):
        """Автоматически увеличивает размер окна, если содержимое не помещается"""
        try:
            # Получаем минимальный размер, необходимый для отображения всего содержимого
            self.adjustSize()
            current_size = self.size()
            minimum_size = self.minimumSizeHint()
            
            # Если текущий размер меньше необходимого, увеличиваем окно
            new_width = max(current_size.width(), minimum_size.width())
            new_height = max(current_size.height(), minimum_size.height())
            
            if new_width > current_size.width() or new_height > current_size.height():
                self.resize(new_width, new_height)
        except Exception:
            pass
    
    def _animate_height(self, widget, button, collapse=True):
        """Универсальный метод для анимации высоты блока"""
        try:
            # Останавливаем существующие анимации для этого виджета
            self._stop_animations_for_widget(widget)
            
            if collapse:
                # Сворачивание
                start_height = widget.height()
                end_height = 0
                final_button_text = '+'
                final_tooltip = 'Развернуть блок'
            else:
                # Разворачивание
                widget.show()
                widget.adjustSize()
                start_height = 0
                end_height = widget.sizeHint().height()
                final_button_text = '−'
                final_tooltip = 'Свернуть блок'
                widget.setMaximumHeight(0)
                button.setText('−')
                button.setToolTip('Свернуть блок')
            
            # Создаем анимацию
            animation = QPropertyAnimation(widget, b"maximumHeight")
            animation.setDuration(ANIM_DURATION_MS)
            animation.setEasingCurve(QEasingCurve.InOutCubic)
            animation.setStartValue(start_height)
            animation.setEndValue(end_height)
            
            # Обновление геометрии на каждом кадре
            def update_height(value):
                widget.setMaximumHeight(value)
                widget.updateGeometry()
            
            animation.valueChanged.connect(update_height)
            
            # Завершение анимации
            def on_finished():
                if collapse:
                    # При сворачивании сначала скрываем, потом восстанавливаем высоту
                    widget.hide()
                    widget.setMaximumHeight(MAX_WIDGET_HEIGHT)  # Восстанавливаем максимальную высоту
                    button.setText(final_button_text)
                    button.setToolTip(final_tooltip)
                    QTimer.singleShot(RESIZE_DELAY_MS, self._auto_shrink_if_needed)
                else:
                    # При разворачивании восстанавливаем высоту, потом обновляем
                    widget.setMaximumHeight(MAX_WIDGET_HEIGHT)  # Восстанавливаем максимальную высоту
                    button.setText(final_button_text)
                    button.setToolTip(final_tooltip)
                    widget.updateGeometry()
                    QTimer.singleShot(RESIZE_DELAY_MS, self._auto_resize_if_needed)
                
                # Удаляем анимацию из списка
                if animation in self.animations:
                    self.animations.remove(animation)
            
            animation.finished.connect(on_finished)
            self.animations.append(animation)
            animation.start()
            
        except Exception:
            # Fallback к мгновенному изменению
            if collapse:
                widget.hide()
                button.setText('+')
                button.setToolTip('Развернуть блок')
                self._auto_shrink_if_needed()
            else:
                widget.show()
                widget.setMaximumHeight(MAX_WIDGET_HEIGHT)
                button.setText('−')
                button.setToolTip('Свернуть блок')
                self._auto_resize_if_needed()
    
    def _animate_collapse(self, widget, button):
        """Анимированное сворачивание блока"""
        self._animate_height(widget, button, collapse=True)
    
    def _animate_expand(self, widget, button):
        """Анимированное разворачивание блока"""
        self._animate_height(widget, button, collapse=False)
    
    def _stop_animations_for_widget(self, widget):
        """Останавливает все анимации для указанного виджета"""
        try:
            animations_to_remove = []
            for animation in self.animations:
                if animation.targetObject() == widget:
                    animation.stop()
                    animations_to_remove.append(animation)
            
            for animation in animations_to_remove:
                self.animations.remove(animation)
        except Exception:
            pass
    
    def _auto_shrink_if_needed(self):
        """Автоматически уменьшает размер окна при сворачивании блоков"""
        try:
            # Небольшая задержка для корректного пересчета размеров после скрытия виджета
            QTimer.singleShot(SHRINK_DELAY_MS, self._perform_shrink)  # Увеличили задержку для завершения анимации
        except Exception:
            pass
    
    def _perform_shrink(self):
        """Выполняет уменьшение размера окна"""
        try:
            # Получаем оптимальный размер для текущего видимого содержимого
            self.adjustSize()
            optimal_size = self.sizeHint()
            current_size = self.size()
            
            # Уменьшаем окно до оптимального размера, но не меньше минимального
            min_size = self.minimumSize()
            new_width = max(optimal_size.width(), min_size.width())
            new_height = max(optimal_size.height(), min_size.height())
            
            # Уменьшаем только если новый размер меньше текущего
            if new_width < current_size.width() or new_height < current_size.height():
                self.resize(new_width, new_height)
        except Exception:
            pass
    
    def setup_edit_field(self):
        """Настройка поля для ручного редактирования"""
        # Устанавливаем начальный текст
        if self.mode in ('direct', 'locative'):
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
        # Поле редактирования — сжимается последним, поэтому оставим минимум больше, чем у превью
        self.edit_field.setMinimumHeight(110)
        try:
            self.edit_field.setMaximumHeight(260)
        except Exception:
            pass
        
        # Моноширинный шрифт
        mono = QFont('Consolas')
        mono.setStyleHint(QFont.Monospace)
        mono.setFixedPitch(True)
        self.edit_field.setFont(mono)
        # Перенос строк внутри редактора
        try:
            self.edit_field.setLineWrapMode(QPlainTextEdit.WidgetWidth)
            self.edit_field.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
            self.edit_field.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            # Последний приоритет сжатия - сжимается только когда блоки превью уже сжались
            self.edit_field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        except Exception:
            pass
    
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
        
        # При запуске диалога можем отключить массовые действия через request_data
        try:
            if bool(self.request_data.get('disable_mass_actions', False)):
                self.btn_confirm_all.setEnabled(False)
                self.btn_skip_all.setEnabled(False)
                self.btn_confirm_all.setToolTip('Недоступно для данного случая')
                self.btn_skip_all.setToolTip('Недоступно для данного случая')
        except Exception:
            pass
        
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
        # Закрытие окна (крестик) трактуем как явную отмену процесса
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