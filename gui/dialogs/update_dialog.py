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

from ...core.localization import translate_key


class UpdateDialog(QDialog):
    """Диалоговое окно для уведомления о новой версии"""

    def __init__(self, current_version: str, new_version: str, download_url: str, parent=None):
        super().__init__(parent)
        self.download_url = download_url
        self.skip_version = False

        self.setWindowTitle(self._t("ui.update_available"))
        self.setMinimumWidth(450)
        self.setModal(True)

        # Основной layout
        layout = QVBoxLayout()
        layout.setSpacing(15)

        # Заголовок
        title_label = QLabel(self._t("ui.new_version_available_title"))
        title_font = title_label.font()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(title_label)

        # Информация о версиях
        info_layout = QVBoxLayout()
        info_layout.setSpacing(8)

        current_label = QLabel(self._fmt("ui.current_version_label", version=current_version))
        current_label.setStyleSheet("font-size: 10pt;")
        info_layout.addWidget(current_label)

        new_label = QLabel(self._fmt("ui.new_version_label", version=new_version))
        new_label.setStyleSheet("font-size: 10pt;")
        info_layout.addWidget(new_label)

        layout.addLayout(info_layout)

        # Описание
        desc_label = QLabel(self._t("ui.update_recommended"))
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet(
            "color: #666; font-size: 9pt; padding: 10px 0;")
        desc_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(desc_label)

        # Чекбокс для пропуска версии
        self.skip_checkbox = QCheckBox(self._t("ui.skip_version_reminder"))
        self.skip_checkbox.setStyleSheet("font-size: 9pt;")
        layout.addWidget(self.skip_checkbox)

        # Кнопки
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)

        later_button = QPushButton(self._t("ui.later"))
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

        download_button = QPushButton(self._t("ui.download_update"))
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
