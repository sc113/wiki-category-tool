"""A full-size editor for a preparation table's category wikitext cell."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout


class CategoryTextDialog(QDialog):
    def __init__(self, text, category, *, translate, parent=None):
        super().__init__(parent)
        self.setWindowTitle(translate('edit_category_text'))
        self.resize(760, 500)
        self.setMinimumSize(460, 280)
        layout = QVBoxLayout(self)
        heading = QLabel(category)
        heading.setTextFormat(Qt.PlainText)
        heading.setWordWrap(True)
        layout.addWidget(heading)
        self.editor = QPlainTextEdit(text)
        self.editor.setFont(QFont('Consolas', 10))
        layout.addWidget(self.editor, 1)
        buttons = QDialogButtonBox()
        buttons.addButton(translate('save_text'), QDialogButtonBox.AcceptRole).setToolTip(translate('save_text_tip'))
        buttons.addButton(translate('cancel'), QDialogButtonBox.RejectRole).setToolTip(translate('cancel_text_tip'))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
