"""Undo local table edits without replaying or reversing wiki operations."""

from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from weakref import WeakMethod, ref

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QTableWidgetSelectionRange
from shiboken6 import isValid

from ...core.localization import translate_key


@dataclass
class _Snapshot:
    rows: tuple
    key: tuple
    current: tuple
    selection: tuple
    extra: object


class TableHistory(QObject):
    """One history per table; bulk changes form a single undo step."""

    def __init__(self, table, *, editable_columns=(), allowed=lambda: True,
                 restored=lambda _extra: None, capture_extra=lambda: None,
                 language=lambda: 'ru', limit=30):
        super().__init__(table)
        self._table = ref(table)
        self.columns = tuple(editable_columns)
        self.allowed, self.restored = self._weak_callback(allowed), self._weak_callback(restored)
        self.capture_extra, self.language = self._weak_callback(capture_extra), self._weak_callback(language)
        self.limit = limit
        self._undo, self._redo = [], []
        self._depth = 0
        self._restoring = False
        self._baseline = self._capture()
        self.undo_action = QAction(table)
        self.redo_action = QAction(table)
        for action, shortcut, callback in (
            (self.undo_action, QKeySequence.Undo, self.undo),
            (self.redo_action, QKeySequence.Redo, self.redo),
        ):
            action.setShortcut(shortcut)
            # Cell editors keep their own text undo while they have focus.
            action.setShortcutContext(Qt.WidgetShortcut)
            action.triggered.connect(callback)
            table.addAction(action)
        if table.contextMenuPolicy() == Qt.DefaultContextMenu:
            table.setContextMenuPolicy(Qt.ActionsContextMenu)
        table.itemChanged.connect(self._edited)
        self.refresh()

    @property
    def table(self):
        return self._table()

    @staticmethod
    def _weak_callback(callback):
        # Do not keep a controller alive through its table's QObject child.
        if getattr(callback, '__self__', None) is None:
            return callback
        target = WeakMethod(callback)
        def invoke(*args):
            method = target()
            return method(*args) if method else None
        return invoke

    def _capture(self):
        rows = tuple(tuple(self.table.item(row, col).clone() if self.table.item(row, col) else None
                           for col in range(self.table.columnCount()))
                     for row in range(self.table.rowCount()))
        columns = self.columns or range(self.table.columnCount())
        key = tuple(tuple(items[col].text() if items[col] else None for col in columns) for items in rows)
        selection = tuple((r.topRow(), r.leftColumn(), r.bottomRow(), r.rightColumn())
                          for r in self.table.selectedRanges())
        return _Snapshot(rows, key, (self.table.currentRow(), self.table.currentColumn()),
                         selection, self.capture_extra())

    def reset(self):
        """A new input, context or network operation starts a fresh history."""
        if self._depth or self._restoring:
            return
        self._undo.clear()
        self._redo.clear()
        self._baseline = self._capture()
        self.refresh()

    def refresh(self):
        lang = self.language() or 'ru'
        self.undo_action.setText(translate_key('ui.context.undo', lang, 'Undo'))
        self.redo_action.setText(translate_key('ui.context.redo', lang, 'Redo'))
        enabled = bool(self.allowed()) and not self._depth
        self.undo_action.setEnabled(enabled and bool(self._undo))
        self.redo_action.setEnabled(enabled and bool(self._redo))

    def _record(self, before, after):
        if before.key != after.key:
            self._undo.append((before, after))
            del self._undo[:-self.limit]
            self._redo.clear()
        self._baseline = after
        self.refresh()

    def _edited(self, item):
        if (self._depth or self._restoring or not self.allowed()
                or item.column() not in self.columns):
            return
        after = self._capture()
        if len(self._baseline.rows) == len(after.rows):
            self._record(self._baseline, after)
        else:
            self._baseline = after

    @contextmanager
    def transaction(self):
        if self._restoring or not self.allowed():
            yield
            return
        outer = self._depth == 0
        before = self._capture() if outer else None
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1
            table = self.table
            if outer and table is not None and isValid(table):
                after = self._capture()
                self._record(before, after)
                if before.key != after.key:
                    self.table.setFocus(Qt.OtherFocusReason)

    def _restore(self, snapshot):
        self._restoring = True
        table = self.table
        blocked = table.blockSignals(True)
        sorting = table.isSortingEnabled()
        table.setSortingEnabled(False)
        table.setUpdatesEnabled(False)
        try:
            table.setRowCount(0)
            table.setRowCount(len(snapshot.rows))
            for row, items in enumerate(snapshot.rows):
                for col, item in enumerate(items):
                    if item is not None:
                        table.setItem(row, col, item.clone())
            table.setSortingEnabled(sorting)
            table.clearSelection()
            if snapshot.current[0] >= 0:
                table.setCurrentCell(*snapshot.current)
            for bounds in snapshot.selection:
                table.setRangeSelected(QTableWidgetSelectionRange(*bounds), True)
            self.restored(snapshot.extra)
            self._baseline = self._capture()
        finally:
            table.setUpdatesEnabled(True)
            table.blockSignals(blocked)
            self._restoring = False
        table.setFocus(Qt.OtherFocusReason)
        table.viewport().update()
        self.refresh()

    def undo(self):
        if self.allowed() and self._undo and not self._depth:
            operation = self._undo.pop()
            self._redo.append(operation)
            self._restore(operation[0])

    def redo(self):
        if self.allowed() and self._redo and not self._depth:
            operation = self._redo.pop()
            self._undo.append(operation)
            self._restore(operation[1])


def table_action(history_name):
    """Record an existing local action, including its controller updates."""
    def decorate(callback):
        @wraps(callback)
        def apply(self, *args, **kwargs):
            with getattr(self, history_name).transaction():
                return callback(self, *args, **kwargs)
        return apply
    return decorate
