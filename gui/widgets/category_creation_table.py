"""Editable category table with a fill handle for the wikitext column."""

from PySide6.QtCore import QPoint, QRect, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QAbstractItemView, QLayout, QSizePolicy, QTableWidget, QTableWidgetItem, QTableWidgetSelectionRange, QTabWidget


class CategoryCountItem(QTableWidgetItem):
    """Sort page + category counters numerically, including after undo."""

    def __lt__(self, other):
        return tuple(self.data(Qt.UserRole)) < tuple(other.data(Qt.UserRole))

    def clone(self):
        return CategoryCountItem(self)


class TransferAutofillTabs(QTabWidget):
    """Let wrapped panel controls keep their height without shrinking the table."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width = max(1, self.contentsRect().width() - 8)
        heights = []
        for index in range(self.count()):
            page = self.widget(index)
            layout = page.layout()
            height = layout.totalHeightForWidth(width)
            if height < 0:
                height = layout.sizeHint().height()
            page.setMinimumHeight(height)
            heights.append(height)
        if heights:
            self.setMinimumHeight(max(heights) + self.tabBar().height() + 8)


class TransferActionsLayout(QLayout):
    """Keep related actions together and wrap whole buttons on narrow pages."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        left, top, right, bottom = self.getContentsMargins()
        return size + QSize(left + right, top + bottom)

    def _arrange(self, rect, apply):
        left, top, right, bottom = self.getContentsMargins()
        x, y, row_height = rect.x() + left, rect.y() + top, 0
        for item in self._items:
            size = item.sizeHint().expandedTo(item.minimumSize())
            if row_height and x + size.width() > rect.right() - right + 1:
                x = rect.x() + left
                y += row_height + self.spacing()
                row_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), size))
            x += size.width() + self.spacing()
            row_height = max(row_height, size.height())
        return y - rect.y() + row_height + bottom


class CategoryCandidatesTable(QTableWidget):
    """Keep category headers legible; allow scrolling when space is limited."""

    def resizeEvent(self, event):
        super().resizeEvent(event)
        remaining = self.viewport().width() - sum(self.columnWidth(col) for col in range(2, self.columnCount()))
        width = max(190, remaining // 2)
        self.setColumnWidth(0, width)
        self.setColumnWidth(1, width)


class TransferToolbarLayout(TransferActionsLayout):
    """Context on the left, files on the right; wrap files as a whole."""

    def _arrange(self, rect, apply):
        if len(self._items) != 2:
            return super()._arrange(rect, apply)
        left, top, right, bottom = self.getContentsMargins()
        context, files = self._items
        first = context.sizeHint().expandedTo(context.minimumSize())
        second = files.sizeHint().expandedTo(files.minimumSize())
        width = rect.width() - left - right
        wrapped = first.width() + second.width() + self.spacing() > width
        file_y = rect.y() + top + (first.height() + self.spacing() if wrapped else 0)
        if apply:
            context.setGeometry(QRect(QPoint(rect.x() + left, rect.y() + top), first))
            files.setGeometry(QRect(QPoint(rect.right() - right - second.width() + 1, file_y), second))
        return top + bottom + (first.height() + self.spacing() + second.height()
                               if wrapped else max(first.height(), second.height()))


class CategoryCreationTable(QTableWidget):
    delete_rows_requested = Signal()
    fill_requested = Signal(int, int, str, bool)
    text_edit_requested = Signal(int)

    def __init__(self, rows, columns, parent=None):
        super().__init__(rows, columns, parent)
        self.setMouseTracking(True)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._fill_anchor = None
        self._fill_end = None
        self._fill_value = ""
        self._fill_position = QPoint()
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(80)
        self._scroll_timer.timeout.connect(self._scroll_fill)
        self._custom_column_widths = False
        self._fitting_columns = False
        self.horizontalHeader().sectionResized.connect(self._remember_column_resize)

    def _remember_column_resize(self, *_args):
        if self.isVisible() and not self._fitting_columns:
            self._custom_column_widths = True

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.columnCount() == 6 and not self._custom_column_widths and not self._fitting_columns:
            self._fitting_columns = True
            try:
                remaining = self.viewport().width() - sum(self.columnWidth(col) for col in range(3, 6))
                # Fit untouched columns, but preserve widths chosen by the user
                # when scrollbars, the log or the window change the viewport.
                for col in range(3):
                    self.setColumnWidth(col, max(200, remaining // 3))
            finally:
                self._fitting_columns = False

    def _handle_rect(self):
        row = self._fill_end if self._fill_anchor is not None else self.currentRow()
        if (not self.isEnabled() or self.editTriggers() == QAbstractItemView.NoEditTriggers
                or row is None or row < 0 or self.currentColumn() != 2):
            return QRect()
        rect = self.visualRect(self.model().index(row, 2))
        return QRect(rect.right() - 7, rect.bottom() - 7, 8, 8) if rect.isValid() else QRect()

    def paintEvent(self, event):
        super().paintEvent(event)
        rect = self._handle_rect()
        if not rect.isEmpty():
            painter = QPainter(self.viewport())
            painter.fillRect(rect, QColor("#48aabc"))
            painter.setPen(QColor("#d6f7fb"))
            painter.drawRect(rect.adjusted(0, 0, -1, -1))

    def keyPressEvent(self, event):
        if (event.key() == Qt.Key_Delete and self.editTriggers() != QAbstractItemView.NoEditTriggers
                and self.state() != QAbstractItemView.EditingState):
            self.delete_rows_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        point = event.position().toPoint()
        if event.button() == Qt.LeftButton and self._handle_rect().contains(point):
            self._fill_anchor = self._fill_end = self.currentRow()
            self._fill_value = self.item(self._fill_anchor, 2).text()
            self._fill_position = point
            self._scroll_timer.start()
            event.accept()
            return
        super().mousePressEvent(event)

    def _extend_fill(self, point):
        index = self.indexAt(point)
        if index.isValid():
            self._fill_end = index.row()
        start, end = sorted((self._fill_anchor, self._fill_end))
        self.clearSelection()
        self.setRangeSelected(QTableWidgetSelectionRange(start, 2, end, 2), True)
        self.viewport().update()

    def mouseMoveEvent(self, event):
        point = event.position().toPoint()
        if self._fill_anchor is not None:
            self._fill_position = point
            self._extend_fill(point)
            event.accept()
            return
        index = self.indexAt(point)
        qid = index.data() if index.isValid() and index.column() in (3, 4) else ""
        linked = isinstance(qid, str) and qid.startswith("Q") and qid[1:].isdigit()
        self.viewport().setCursor(Qt.CrossCursor if self._handle_rect().contains(point)
                                  else Qt.PointingHandCursor if linked else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def _scroll_fill(self):
        if self._fill_anchor is None:
            return
        y = self._fill_position.y()
        if y < 16 or y > self.viewport().height() - 16:
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() + (-1 if y < 16 else 1))
            point = QPoint(self._fill_position.x(), max(1, min(self.viewport().height() - 2, y)))
            self._extend_fill(point)

    def mouseReleaseEvent(self, event):
        if self._fill_anchor is not None and event.button() == Qt.LeftButton:
            start, end = sorted((self._fill_anchor, self._fill_end))
            value = self._fill_value
            self._fill_anchor = self._fill_end = None
            self._scroll_timer.stop()
            self.fill_requested.emit(start, end, value, False)
            self.viewport().update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and self._handle_rect().contains(event.position().toPoint()):
            row = self.currentRow()
            value = self.item(row, 2).text()
            self._fill_anchor = self._fill_end = None
            self._scroll_timer.stop()
            if value.strip() and row + 1 < self.rowCount():
                self.fill_requested.emit(row + 1, self.rowCount() - 1, value, True)
            event.accept()
            return
        index = self.indexAt(event.position().toPoint())
        if (event.button() == Qt.LeftButton and index.isValid() and index.column() == 2
                and self.editTriggers() != QAbstractItemView.NoEditTriggers):
            triggers = self.editTriggers()
            self.setEditTriggers(triggers & ~QAbstractItemView.DoubleClicked)
            super().mouseDoubleClickEvent(event)
            self.setEditTriggers(triggers)
            self.text_edit_requested.emit(index.row())
            return
        super().mouseDoubleClickEvent(event)
