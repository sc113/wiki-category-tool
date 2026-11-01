"""Inspect matched articles without adding columns to the scanning table."""

from urllib.parse import quote

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHeaderView, QLabel,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QPushButton, QStyle,
)

from ...core.api_client import WikimediaAPIClient
from .table_history import TableHistory


class CategoryMembersTable(QTableWidget):
    exclude_requested = Signal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            self.exclude_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class CategoryArticlesDialog(QDialog):
    def __init__(self, data, *, language, family, translate, parent=None, editable=False, only_additional=False):
        super().__init__(parent)
        self._t, self.language, self.family = translate, language, family
        self.include_subcategories = data.get('total_categories') is not None
        self.setObjectName('categoryArticlesDialog')
        self.setWindowTitle(translate('subpages_title' if self.include_subcategories else 'articles_title'))
        self.resize(720, 480)
        self.articles = tuple(dict.fromkeys([*data.get('target_articles', ()), *data.get('target_categories', ())]))
        self.original_articles = self.articles
        self._membership = {}
        self.editable = editable
        self.only_additional = only_additional
        self.has_target = bool(data.get('target_title'))
        count = len(self.articles) if 'target_articles' in data else (data.get('potential') or 0) + (data.get('potential_categories') or 0)
        layout = QVBoxLayout(self)
        heading = QLabel(data['source_title'] + (' → ' + data['target_title'] if self.has_target else ''))
        heading.setWordWrap(True)
        heading.setTextFormat(Qt.PlainText)
        layout.addWidget(heading)
        self.summary = QLabel(translate('articles_summary', total=count, existing='—', additional='—')
                              if self.has_target else translate('articles_new', total=count))
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.table = CategoryMembersTable(len(self.articles), 2 if self.has_target else 1)
        page_caption = translate('page_column' if self.include_subcategories else 'article_column')
        self.table.setHorizontalHeaderLabels([page_caption, translate('membership_column')]
                                            if self.has_target else [page_caption])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        source_table = getattr(parent, 'table', None)
        if source_table is not None:
            self.table.setStyleSheet(source_table.styleSheet())
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        if self.has_target:
            self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        for row, title in enumerate(self.articles):
            item = QTableWidgetItem(title)
            item.setToolTip(title)
            self.table.setItem(row, 0, item)
            if self.has_target:
                self.table.setItem(row, 1, QTableWidgetItem('…'))
        self.table.cellDoubleClicked.connect(self._open_article)
        layout.addWidget(self.table, 1)
        self.status = QLabel(translate('articles_loading') if 'target_articles' not in data
                             else translate('articles_checking') if self.has_target else '')
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox()
        if editable:
            self.remove_btn = QPushButton()
            self.remove_btn.setIcon(self.style().standardIcon(QStyle.SP_TrashIcon))
            self.remove_btn.setToolTip(translate('exclude_members_tip'))
            self.remove_btn.setAccessibleName(translate('exclude_members'))
            buttons.addButton(self.remove_btn, QDialogButtonBox.ActionRole)
            self.remove_btn.clicked.connect(self.remove_selected)
            self.table.exclude_requested.connect(self.remove_selected)
            self.table.itemSelectionChanged.connect(self._update_remove_button)
            self.remove_btn.setEnabled(False)
            self.history = TableHistory(self.table, editable_columns=(0,), restored=self._restore_selection,
                                        language=lambda: getattr(getattr(parent, 'parent_window', parent), '_ui_lang', 'ru'))
        buttons.addButton(translate('close'), QDialogButtonBox.RejectRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def show_membership(self, results):
        membership = dict(results)
        self._membership = membership
        if not self.editable:
            self.articles = tuple(title for title, present in membership.items() if not self.only_additional or not present)
        blocked = self.table.blockSignals(True)
        self.table.setRowCount(len(self.articles))
        for row, title in enumerate(self.articles):
            item = QTableWidgetItem(title)
            item.setToolTip(title)
            self.table.setItem(row, 0, item)
        self.table.blockSignals(blocked)
        self._refresh_summary()

    def _refresh_summary(self):
        if not self.has_target:
            self.summary.setText(self._t('articles_new', total=len(self.articles)))
            self.status.clear()
            return
        existing = 0
        for row, title in enumerate(self.articles):
            included = self._membership.get(title)
            existing += bool(included)
            self.table.setItem(row, 1, QTableWidgetItem(self._t('article_yes' if included else 'article_no')
                                                      if included is not None else '…'))
        known = all(title in self._membership for title in self.articles)
        self.summary.setText(self._t('articles_summary', total=len(self.articles), existing=existing if known else '—',
                                    additional=len(self.articles) - existing if known else '—'))
        self.status.setText('' if known else self._t('articles_checking'))

    def _restore_selection(self, _extra=None):
        self.articles = tuple(self.table.item(row, 0).text() for row in range(self.table.rowCount()))
        self._refresh_summary()

    def _update_remove_button(self):
        self.remove_btn.setEnabled(bool(self.table.selectedIndexes()))

    def remove_selected(self):
        if not self.editable:
            return
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        with self.history.transaction():
            for row in rows:
                self.table.removeRow(row)
        self._restore_selection()

    def show_progress(self, text):
        self.status.setText(str(text)[:500])

    def _open_article(self, row, _column):
        host = WikimediaAPIClient()._build_api_url(self.family, self.language).split('/w/')[0]
        title = self.table.item(row, 0).text()
        QDesktopServices.openUrl(QUrl(host + '/wiki/' + quote(title.replace(' ', '_'))))
