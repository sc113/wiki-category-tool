# -*- coding: utf-8 -*-
"""
Диалог подтверждения операций для Wiki Category Tool.

Этот модуль содержит диалог подтверждения для массовых операций
создания и замены страниц.
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QMessageBox
)
from PySide6.QtCore import Qt
from ...utils import format_russian_pages_accusative, format_russian_pages_genitive_for_content
from PySide6.QtGui import QIcon, QPixmap
from ...core.localization import translate_key


class ConfirmationDialog(QDialog):
    """Диалог подтверждения массовых операций"""

    def __init__(self, operation_type: str, page_count: int, parent=None):
        super().__init__(parent)
        self.operation_type = operation_type  # "создать" или "заменить"
        self.page_count = page_count
        self.setup_ui()

    def _ui_lang(self) -> str:
        return getattr(self.parent(), '_ui_lang', 'ru') if self.parent() is not None else 'ru'

    def _t(self, key: str) -> str:
        return translate_key(key, self._ui_lang(), '')

    def _fmt(self, key: str, **kwargs) -> str:
        text = self._t(key)
        try:
            return text.format(**kwargs)
        except Exception:
            return text

    def _is_create_operation(self) -> bool:
        op = str(self.operation_type or '').strip().lower()
        return op in {'create', self._t('ui.confirm_dialog.create_operation').strip().lower()}

    def _page_phrase(self, for_content: bool = False) -> str:
        if self._ui_lang().startswith('en'):
            key = 'ui.confirm_dialog.page_singular' if self.page_count == 1 else 'ui.confirm_dialog.page_plural'
            return self._fmt(key, count=self.page_count)
        if for_content:
            return format_russian_pages_genitive_for_content(self.page_count)
        return format_russian_pages_accusative(self.page_count)

    def setup_ui(self):
        """Создает пользовательский интерфейс диалога"""
        self.setWindowTitle(self._t("ui.confirm_operation"))
        self.setModal(True)
        self.setFixedSize(400, 150)

        # Основной layout
        layout = QVBoxLayout(self)
        layout.setSpacing(20)

        # Иконка и текст сообщения
        message_layout = QHBoxLayout()

        # Иконка вопроса
        icon_label = QLabel()
        try:
            # Используем стандартную иконку вопроса
            icon_label.setPixmap(self.style().standardPixmap(
                self.style().StandardPixmap.SP_MessageBoxQuestion))
        except AttributeError:
            # Если иконка недоступна, используем текст
            icon_label.setText("❓")
            icon_label.setStyleSheet("font-size: 24pt;")
        icon_label.setAlignment(Qt.AlignTop)
        message_layout.addWidget(icon_label)

        # Текст сообщения (корректные формы)
        if self._is_create_operation():
            message_text = self._fmt("ui.confirm_dialog.create_message", pages=self._page_phrase())
        else:
            message_text = self._fmt("ui.confirm_dialog.replace_message", pages=self._page_phrase(for_content=True))

        message_text += f"\n\n{self._t('ui.continue_prompt')}"

        message_label = QLabel(message_text)
        message_label.setWordWrap(True)
        message_layout.addWidget(message_label, 1)

        layout.addLayout(message_layout)

        # Кнопки
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        self.yes_button = QPushButton(self._t("ui.yes"))
        self.yes_button.clicked.connect(self.accept)
        self.yes_button.setDefault(True)
        button_layout.addWidget(self.yes_button)

        self.no_button = QPushButton(self._t("ui.no"))
        self.no_button.clicked.connect(self.reject)
        button_layout.addWidget(self.no_button)

        layout.addLayout(button_layout)

    @staticmethod
    def confirm_operation(operation_type: str, page_count: int, parent=None) -> bool:
        """
        Статический метод для показа диалога подтверждения

        Args:
            operation_type: Тип операции ("создать" или "заменить")
            page_count: Количество страниц
            parent: Родительский виджет

        Returns:
            True если пользователь подтвердил операцию, False если отменил
        """
        dialog = ConfirmationDialog(operation_type, page_count, parent)
        return dialog.exec() == QDialog.Accepted
