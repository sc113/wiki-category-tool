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


class ConfirmationDialog(QDialog):
    """Диалог подтверждения массовых операций"""
    
    def __init__(self, operation_type: str, page_count: int, parent=None):
        super().__init__(parent)
        self.operation_type = operation_type  # "создать" или "заменить"
        self.page_count = page_count
        self.setup_ui()
        
    def setup_ui(self):
        """Создает пользовательский интерфейс диалога"""
        self.setWindowTitle("Подтверждение операции")
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
            icon_label.setPixmap(self.style().standardPixmap(self.style().StandardPixmap.SP_MessageBoxQuestion))
        except AttributeError:
            # Если иконка недоступна, используем текст
            icon_label.setText("❓")
            icon_label.setStyleSheet("font-size: 32px;")
        icon_label.setAlignment(Qt.AlignTop)
        message_layout.addWidget(icon_label)
        
        # Текст сообщения (корректные формы)
        if self.operation_type == "создать":
            message_text = f"Вы собираетесь создать {format_russian_pages_accusative(self.page_count)}."
        else:
            message_text = f"Вы собираетесь заменить содержимое {format_russian_pages_genitive_for_content(self.page_count)}."
        
        message_text += "\n\nПродолжить?"
        
        message_label = QLabel(message_text)
        message_label.setWordWrap(True)
        message_layout.addWidget(message_label, 1)
        
        layout.addLayout(message_layout)
        
        # Кнопки
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        self.yes_button = QPushButton("Да")
        self.yes_button.clicked.connect(self.accept)
        self.yes_button.setDefault(True)
        button_layout.addWidget(self.yes_button)
        
        self.no_button = QPushButton("Нет")
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