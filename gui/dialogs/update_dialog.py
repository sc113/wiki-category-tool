# -*- coding: utf-8 -*-
"""
Диалоговое окно для уведомления о новой версии приложения.
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QCheckBox
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
import webbrowser


class UpdateDialog(QDialog):
    """Диалоговое окно для уведомления о новой версии"""

    def __init__(self, current_version: str, new_version: str, download_url: str, parent=None):
        super().__init__(parent)
        self.download_url = download_url
        self.skip_version = False

        self.setWindowTitle("Доступно обновление")
        self.setMinimumWidth(450)
        self.setModal(True)

        # Основной layout
        layout = QVBoxLayout()
        layout.setSpacing(15)

        # Заголовок
        title_label = QLabel("🎉 Доступна новая версия!")
        title_font = title_label.font()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(title_label)

        # Информация о версиях
        info_layout = QVBoxLayout()
        info_layout.setSpacing(8)

        current_label = QLabel(f"Текущая версия: <b>{current_version}</b>")
        current_label.setStyleSheet("font-size: 10pt;")
        info_layout.addWidget(current_label)

        new_label = QLabel(
            f"Новая версия: <b style='color: #0b6623;'>{new_version}</b>")
        new_label.setStyleSheet("font-size: 10pt;")
        info_layout.addWidget(new_label)

        layout.addLayout(info_layout)

        # Описание
        desc_label = QLabel(
            "Рекомендуется обновить приложение до последней версии\n"
            "для получения новых функций и исправлений ошибок."
        )
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet(
            "color: #666; font-size: 9pt; padding: 10px 0;")
        desc_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(desc_label)

        # Чекбокс для пропуска версии
        self.skip_checkbox = QCheckBox(f"Не напоминать об этой версии")
        self.skip_checkbox.setStyleSheet("font-size: 9pt;")
        layout.addWidget(self.skip_checkbox)

        # Кнопки
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)

        later_button = QPushButton("Позже")
        later_button.setStyleSheet(
            "QPushButton { "
            "  background-color: #e5e7eb; "
            "  border: none; "
            "  border-radius: 5px; "
            "  padding: 8px 20px; "
            "  font-size: 10pt; "
            "}"
            "QPushButton:hover { background-color: #d1d5db; }"
        )
        later_button.clicked.connect(self.on_later)

        download_button = QPushButton("Скачать обновление")
        download_button.setStyleSheet(
            "QPushButton { "
            "  background-color: #0b6623; "
            "  color: white; "
            "  border: none; "
            "  border-radius: 5px; "
            "  padding: 8px 20px; "
            "  font-size: 10pt; "
            "  font-weight: bold; "
            "}"
            "QPushButton:hover { background-color: #094d1a; }"
        )
        download_button.clicked.connect(self.on_download)
        download_button.setDefault(True)

        button_layout.addStretch()
        button_layout.addWidget(later_button)
        button_layout.addWidget(download_button)

        layout.addLayout(button_layout)

        self.setLayout(layout)

    def on_download(self):
        """Открывает страницу загрузки и закрывает диалог"""
        try:
            webbrowser.open(self.download_url)
        except Exception:
            pass
        self.accept()

    def on_later(self):
        """Закрывает диалог с сохранением настройки пропуска"""
        self.skip_version = self.skip_checkbox.isChecked()
        self.reject()
