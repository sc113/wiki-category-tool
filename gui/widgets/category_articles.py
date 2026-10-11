"""Inspect matched articles without adding columns to the scanning table."""

from urllib.parse import quote

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHeaderView, QLabel,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ...core.api_client import WikimediaAPIClient


class CategoryArticlesDialog(QDialog):
    def __init__(self, data, *, language, family, translate, parent=None):
        super().__init__(parent)
        self._t, self.language, self.family = translate, language, family
        self.setObjectName('categoryArticlesDialog')
        self.setWindowTitle(translate('articles_title'))
        self.resize(720, 480)
        self.articles = tuple(dict.fromkeys(data.get('target_articles', ())))
        self.has_target = bool(data.get('target_title'))
        count = len(self.articles) if 'target_articles' in data else data.get('potential', 0)
        layout = QVBoxLayout(self)
        heading = QLabel(data['source_title'] + (' → ' + data['target_title'] if self.has_target else ''))
        heading.setWordWrap(True)
        heading.setTextFormat(Qt.PlainText)
        layout.addWidget(heading)
        self.summary = QLabel(translate('articles_summary', total=count, existing='—', additional='—')
                              if self.has_target else translate('articles_new', total=count))
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.table = QTableWidget(len(self.articles), 2 if self.has_target else 1)
        self.table.setHorizontalHeaderLabels([translate('article_column'), translate('membership_column')]
                                            if self.has_target else [translate('article_column')])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
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
        buttons.addButton(translate('close'), QDialogButtonBox.RejectRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def show_membership(self, results):
        membership = dict(results)
        self.articles = tuple(membership)
        self.table.setRowCount(len(self.articles))
        for row, title in enumerate(self.articles):
            item = QTableWidgetItem(title)
            item.setToolTip(title)
            self.table.setItem(row, 0, item)
        if not self.has_target:
            self.summary.setText(self._t('articles_new', total=len(self.articles)))
            self.status.clear()
            return
        existing = 0
        for row, title in enumerate(self.articles):
            included = membership[title]
            existing += included
            self.table.setItem(row, 1, QTableWidgetItem(self._t('article_yes' if included else 'article_no')))
        self.summary.setText(self._t('articles_summary', total=len(self.articles), existing=existing,
                                    additional=len(self.articles) - existing))
        self.status.clear()

    def show_progress(self, text):
        self.status.setText(str(text)[:500])

    def _open_article(self, row, _column):
        host = WikimediaAPIClient()._build_api_url(self.family, self.language).split('/w/')[0]
        title = self.table.item(row, 0).text()
        QDesktopServices.openUrl(QUrl(host + '/wiki/' + quote(title.replace(' ', '_'))))
