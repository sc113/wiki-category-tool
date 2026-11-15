"""Localized work-list totals and counts for selected files."""

import csv
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, Qt, QTimer
from PySide6.QtWidgets import QLabel

from ...core.localization import translate_key


def format_item_count(kind: str, count: int, language: str) -> str:
    form = 'one' if count == 1 else 'many'
    if language.startswith('ru') and not 11 <= count % 100 <= 14:
        last_digit = count % 10
        if last_digit == 1:
            form = 'one'
        elif 2 <= last_digit <= 4:
            form = 'few'
    return translate_key(f'ui.count.{kind}.{form}', language).format(count=count)


class ItemCountLabel(QLabel):
    """A quiet counter next to the heading or file it describes."""

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self._kind = ''
        self._total = None
        self._from_txt = False
        self.setObjectName('itemCount')
        self.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.hide()

    def set_total(self, kind, total, *, from_txt=False):
        self._kind, self._total, self._from_txt = kind, total, from_txt
        self.refresh_localized_texts()

    def refresh_localized_texts(self):
        if not self._total:
            self.clear()
            self.hide()
            return
        host = getattr(self.owner, 'parent_window', None) or self.owner
        language = getattr(host, '_ui_lang', 'ru')
        count = format_item_count(self._kind, self._total, language)
        if self._from_txt:
            count = translate_key('ui.count.from_txt', language).format(count=count)
        self.setText(count)
        self.show()


class FileCount(QObject):
    """Refresh a file's count after path edits and saves by an external editor."""

    def __init__(self, path_edit, reader, update):
        super().__init__(path_edit)
        self.path_edit, self.reader, self.update = path_edit, reader, update
        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self.refresh)
        self.watcher.directoryChanged.connect(self.refresh)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._read)
        path_edit.textChanged.connect(self.refresh)
        path_edit.editingFinished.connect(self.refresh)
        self.refresh()

    def refresh(self, *_args):
        self.update(None)
        self.timer.start()

    def _read(self):
        raw = self.path_edit.text().strip()
        path = Path(raw).absolute() if raw else None
        total = None
        watch = set()
        if path is not None:
            try:
                total = self.reader(path)
                watch.add(str(path))
            except (OSError, ValueError, csv.Error):
                if path.is_file():
                    watch.add(str(path))
                elif path.parent.is_dir():
                    # Discover a file created after the path was entered.
                    watch.add(str(path.parent))
        previous = set(self.watcher.files()) | set(self.watcher.directories())
        if previous - watch:
            self.watcher.removePaths(list(previous - watch))
        if watch - previous:
            self.watcher.addPaths(list(watch - previous))
        self.update(total)
