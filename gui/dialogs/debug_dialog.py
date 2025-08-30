"""
Debug Dialog Module

Выделенный диалог отладки для отображения логов системы.
Заменяет встроенный debug dialog из метода show_debug.
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, 
    QPlainTextEdit, QFileDialog, QMessageBox
)
from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QFont, QTextCursor
from datetime import datetime
import os


class DebugDialog(QDialog):
    """
    Диалог отладки.
    
    Отображает все записи из DEBUG_BUFFER с автообновлением,
    поддерживает очистку и сохранение логов в файл.
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        self.setup_ui()
        self.setup_connections()
        self.load_existing_logs()
        
        # Устанавливаем себя как текущий DEBUG_VIEW
        from ...utils import get_debug_bridge
        # Подключаем сигнал моста к методу добавления в лог
        try:
            get_debug_bridge().message.connect(self.append_log)
        except Exception:
            pass
    
    def setup_ui(self):
        """Настройка пользовательского интерфейса"""
        # Заголовок окна: "Debug log"
        self.setWindowTitle("Debug log")
        
        # Размер окна: 800x600 с возможностью изменения
        self.resize(800, 600)
        
        # Основной layout
        layout = QVBoxLayout(self)
        
        # QPlainTextEdit для логов с моноширинным шрифтом
        self.text_edit = QPlainTextEdit()
        self.text_edit.setReadOnly(True)
        
        # Моноширинный шрифт
        font = QFont("Consolas", 9)
        if not font.exactMatch():
            font = QFont("Courier New", 9)
        if not font.exactMatch():
            font = QFont("monospace", 9)
        self.text_edit.setFont(font)
        
        # Отображение будет загружено в load_existing_logs
        
        layout.addWidget(self.text_edit)
        
        # Кнопки: "Очистить", "Сохранить в файл", "Закрыть"
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        self.clear_button = QPushButton("Очистить")
        self.save_button = QPushButton("Сохранить в файл")
        self.close_button = QPushButton("Закрыть")
        
        button_layout.addWidget(self.clear_button)
        button_layout.addWidget(self.save_button)
        button_layout.addWidget(self.close_button)
        
        layout.addLayout(button_layout)
    
    def setup_connections(self):
        """Настройка соединений сигналов и слотов"""
        self.clear_button.clicked.connect(self.clear_logs)
        self.save_button.clicked.connect(self.save_logs)
        self.close_button.clicked.connect(self.hide)
        
        # Обработка закрытия окна
        self.destroyed.connect(self.on_dialog_destroyed)
    
    def load_existing_logs(self):
        """Загружает существующие записи из буфера"""
        from ...utils import DEBUG_BUFFER
        if DEBUG_BUFFER:
            self.text_edit.setPlainText('\n'.join(DEBUG_BUFFER))
            # Автопрокрутка к последней записи
            cursor = self.text_edit.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self.text_edit.setTextCursor(cursor)
    
    def clear_logs(self):
        """
        Кнопка "Очистить" очищает и буфер и отображение
        """
        # Очищаем буфер
        from ...utils import clear_debug_buffer
        clear_debug_buffer()
        
        # Очищаем отображение
        self.text_edit.clear()
    
    def save_logs(self):
        """
        Кнопка "Сохранить" экспортирует лог в текстовый файл
        """
        from ...utils import DEBUG_BUFFER
        if not DEBUG_BUFFER:
            QMessageBox.information(self, "Информация", "Нет логов для сохранения")
            return
        
        # Предлагаем имя файла с текущей датой и временем
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_filename = f"debug_log_{timestamp}.txt"
        
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить лог в файл",
            default_filename,
            "Текстовые файлы (*.txt);;Все файлы (*)"
        )
        
        if filename:
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(DEBUG_BUFFER))
                QMessageBox.information(self, "Успех", f"Лог сохранен в файл:\n{filename}")
            except Exception as e:
                QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить файл:\n{str(e)}")
    
    def append_log(self, message: str):
        """
        Автообновление при добавлении новых записей
        """
        self.text_edit.appendPlainText(message)
        # Автопрокрутка к последней записи
        cursor = self.text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.text_edit.setTextCursor(cursor)
    
    def closeEvent(self, event):
        """Переопределяем закрытие окна - скрываем вместо закрытия"""
        event.ignore()  # Игнорируем событие закрытия
        self.hide()     # Скрываем окно
    
    def on_dialog_destroyed(self):
        """Обработка уничтожения диалога"""
        # Отключаем сигнал моста
        try:
            from ...utils import get_debug_bridge
            get_debug_bridge().message.disconnect(self.append_log)
        except Exception:
            pass
    
    def installEventFilter(self, filter_obj):
        """Установка фильтра событий для text_edit"""
        self.text_edit.installEventFilter(filter_obj)