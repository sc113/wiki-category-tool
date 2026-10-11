"""Shared stage controls and a collapsible log beside the working table."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QGroupBox, QHBoxLayout, QLabel, QSizePolicy, QSplitter, QToolButton,
    QVBoxLayout, QWidget,
)

from .category_creation_table import TransferActionsLayout, TransferToolbarLayout
from .ui_helpers import create_log_wrap


def category_toolbar(source, target, options, translate):
    toolbar = QWidget()
    toolbar.setObjectName('categoryStageToolbar')
    layout = TransferToolbarLayout(toolbar)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    context = QWidget()
    row = QHBoxLayout(context)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(10)
    source_caption = QLabel(translate('source_lang'))
    source_caption.setProperty('categoryStageLabelKey', 'source_lang')
    row.addWidget(source_caption)
    source.setFixedWidth(62)
    row.addWidget(source)
    target_caption = QLabel(translate('current_lang_caption'))
    target_caption.setProperty('categoryStageLabelKey', 'current_lang_caption')
    row.addWidget(target_caption)
    target.setObjectName('targetLangCodeBadge')
    target.setMinimumWidth(42)
    target.setMaximumWidth(50)
    target.setToolTip(translate('current_lang_tip'))
    row.addWidget(target)
    row.addWidget(options)
    layout.addWidget(context)
    files = QWidget()
    file_layout = QHBoxLayout(files)
    file_layout.setContentsMargins(0, 0, 0, 0)
    file_layout.setSpacing(6)
    layout.addWidget(files)
    return toolbar, file_layout


def refresh_category_toolbar(toolbar, translate):
    for label in toolbar.findChildren(QLabel):
        key = label.property('categoryStageLabelKey')
        if key:
            label.setText(translate(key))
        if label.objectName() == 'targetLangCodeBadge':
            label.setToolTip(translate('current_lang_tip'))


def category_actions(title):
    group = QGroupBox(title)
    group.setObjectName('categoryStageActions')
    layout = TransferActionsLayout(group)
    layout.setContentsMargins(10, 8, 10, 8)
    layout.setSpacing(8)
    return group, layout


class CategoryLogPanel(QWidget):
    """Keep a visible log toggle when the log is collapsed to the right."""

    def __init__(self, log, translate, parent=None):
        super().__init__(parent)
        self._translate = translate
        self.expanded = False
        self.setObjectName('categoryStageLog')
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.toggle = QToolButton()
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle.toggled.connect(self.set_expanded)
        layout.addWidget(self.toggle, 0, Qt.AlignLeft)
        log.setMinimumHeight(0)
        log.setMaximumHeight(16777215)
        font = QFont('Consolas', 9)
        font.setFixedPitch(True)
        log.setFont(font)
        self.wrap = create_log_wrap(self, log, with_header=False)
        layout.addWidget(self.wrap, 1)
        self.collapsed_space = QWidget()
        layout.addWidget(self.collapsed_space, 1)
        self.refresh_localized_texts()
        self.set_expanded(False)

    def refresh_localized_texts(self):
        self.toggle.setText(self._translate('log'))
        self.toggle.setToolTip(self._translate('hide_log' if self.expanded else 'show_log'))
        if not self.expanded:
            self.setFixedWidth(max(62, self.toggle.sizeHint().width()))

    def set_expanded(self, expanded):
        self.expanded = bool(expanded)
        self.wrap.setVisible(self.expanded)
        self.collapsed_space.setVisible(not self.expanded)
        self.toggle.setArrowType(Qt.RightArrow if self.expanded else Qt.LeftArrow)
        if self.expanded:
            self.setMinimumWidth(240)
            self.setMaximumWidth(16777215)
        self.refresh_localized_texts()
        self.fit_workspace()

    def fit_workspace(self):
        splitter = self.parentWidget()
        if isinstance(splitter, QSplitter):
            if self.expanded:
                log_width = min(380, max(240, splitter.width() // 3))
            else:
                log_width = self.maximumWidth()
            splitter.setSizes([max(1, splitter.width() - log_width), log_width])


class CategoryWorkspace(QSplitter):
    """The same table/log arrangement for every independent stage."""

    def __init__(self, content, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setObjectName('categoryStageWorkspace')
        self.setChildrenCollapsible(False)
        self.setHandleWidth(6)
        content.setMinimumWidth(0)
        content.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.addWidget(content)
        self.setStretchFactor(0, 1)

    def set_log_panel(self, panel):
        self.addWidget(panel)
        self.setStretchFactor(1, 0)
        panel.show()
        panel.fit_workspace()
