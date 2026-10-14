"""Shared title-template preview for creation and interwiki linking."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHeaderView,
    QLabel, QLineEdit, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ...core.category_transfer import parse_title_number, plan_numbered_titles, title_number_matches


def title_fill_dialog(parent, rows, selected, translate, style, template="", *, only_empty=True):
    t = translate
    if not template:
        for row in rows:
            source, target = title_number_matches(row.source_title), title_number_matches(row.target_title)
            if len(source) == len(target) == 1 and parse_title_number(source[0].group())[0] == parse_title_number(target[0].group())[0]:
                match = target[0]
                token = t("roman_token" if match.group().isalpha() else "number_token")
                template = row.target_title[:match.start()] + token + row.target_title[match.end():]
                break
    dialog = QDialog(parent)
    dialog.setObjectName("categoryTitleFillDialog")
    dialog.setWindowTitle(t("number_titles"))
    dialog.resize(760, 540)
    layout = QVBoxLayout(dialog)
    help_label = QLabel(t("number_help"))
    help_label.setWordWrap(True)
    layout.addWidget(help_label)
    fields = QFormLayout()
    target_edit = QLineEdit(template)
    target_edit.setObjectName("titleTemplateEdit")
    target_edit.setPlaceholderText(t("number_target_example"))
    source_edit = QLineEdit()
    source_edit.setPlaceholderText(t("number_source_auto"))
    fields.addRow(t("number_target_template"), target_edit)
    fields.addRow(t("number_source_pattern"), source_edit)
    layout.addLayout(fields)
    selected_only = QCheckBox(t("number_selected"))
    selected_only.setEnabled(bool(selected))
    selected_only.setChecked(len(selected) > 1)
    layout.addWidget(selected_only)
    summary = QLabel()
    summary.setWordWrap(True)
    summary.setTextFormat(Qt.PlainText)
    layout.addWidget(summary)
    preview = QTableWidget(0, 3)
    preview.setStyleSheet(style)
    preview.setHorizontalHeaderLabels([t("source_category"), t("new_category"), t("status")])
    preview.setEditTriggers(QAbstractItemView.NoEditTriggers)
    preview.setAlternatingRowColors(True)
    preview.verticalHeader().hide()
    preview.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
    preview.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
    preview.setColumnWidth(2, 150)
    layout.addWidget(preview, 1)
    buttons = QDialogButtonBox()
    apply_button = buttons.addButton(t("number_apply"), QDialogButtonBox.AcceptRole)
    buttons.addButton(t("cancel"), QDialogButtonBox.RejectRole)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    plan = ()

    def refresh():
        nonlocal plan
        try:
            plan = plan_numbered_titles(rows, source_edit.text(), target_edit.text(),
                                        selected if selected_only.isChecked() else None, only_empty=only_empty)
        except ValueError:
            plan = ()
            summary.setText(t("number_template_invalid"))
        else:
            summary.setText(t("number_summary", **{code: sum(p.status == code for p in plan)
                            for code in ("number_fill", "number_kept", "number_unmatched", "number_conflict")}))
        apply_button.setEnabled(any(p.status == "number_fill" for p in plan))
        preview.setRowCount(len(plan))
        for index, proposal in enumerate(plan):
            for col, value in enumerate((rows[proposal.index].source_title, proposal.target_title, t(proposal.status))):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                preview.setItem(index, col, item)

    source_edit.textChanged.connect(refresh)
    target_edit.textChanged.connect(refresh)
    selected_only.toggled.connect(refresh)
    refresh()
    target_edit.setFocus()
    return (plan, target_edit.text()) if dialog.exec() == QDialog.Accepted else None
