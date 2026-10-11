"""A compact, localized action plan for category write confirmations."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QGroupBox, QLabel


def category_plan_widget(plan, translate, parent=None):
    group = QGroupBox(translate('plan_title'), parent)
    group.setObjectName('categoryActionPlan')
    grid = QGridLayout(group)
    grid.setHorizontalSpacing(24)
    grid.setVerticalSpacing(8)
    for key in ('create', 'link', 'labels', 'retry', 'items', 'complete', 'drafts', 'errors', 'conflicts'):
        count = plan[key]
        if not count:
            continue
        row = grid.rowCount()
        label = QLabel(translate('plan_' + key))
        label.setWordWrap(True)
        grid.addWidget(label, row, 0)
        value = QLabel(str(count))
        value.setObjectName('planCount_' + key)
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        font = value.font()
        font.setBold(True)
        value.setFont(font)
        grid.addWidget(value, row, 1)
    grid.setColumnStretch(0, 1)
    return group
