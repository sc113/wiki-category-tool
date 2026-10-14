"""Shared stage controls and a collapsible log beside the working table."""

from PySide6.QtCore import QEvent, QRect, Qt
from PySide6.QtGui import QFont, QPainter, QPalette
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QSplitter, QToolButton,
    QVBoxLayout, QWidget, QProgressBar, QPushButton, QStyle,
)

from ...core.localization import translate_key
from .category_creation_table import TransferActionsLayout, TransferToolbarLayout
from .ui_helpers import create_log_wrap, init_progress


class CategorySettingsLayout(QGridLayout):
    """Aligned setting rows, with enough space between labels and controls."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setContentsMargins(12, 12, 12, 12)
        self.setHorizontalSpacing(12)
        self.setVerticalSpacing(12)
        self.setColumnStretch(1, 1)

    def add_setting(self, row, caption, control):
        self.addWidget(caption, row, 0, Qt.AlignVCenter)
        if control.sizePolicy().horizontalPolicy() == QSizePolicy.Fixed:
            self.addWidget(control, row, 1, Qt.AlignLeft | Qt.AlignVCenter)
        else:
            self.addWidget(control, row, 1)


def category_toolbar(source, target, options, translate):
    toolbar = QWidget()
    toolbar.setObjectName('categoryStageToolbar')
    layout = QHBoxLayout(toolbar)
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
    if options is not None:
        row.addWidget(options)
    layout.addWidget(context, 0, Qt.AlignVCenter)
    toolbar.context = context
    files = QWidget()
    file_layout = QHBoxLayout(files)
    file_layout.setContentsMargins(0, 0, 0, 0)
    file_layout.setSpacing(6)
    files.setMinimumWidth(0)
    layout.addWidget(files, 1)
    return toolbar, file_layout


class CategoryInlineParameters(QScrollArea):
    """One aligned row; small windows can scroll it without widening the page."""

    def __init__(self, groups, parent=None):
        super().__init__(parent)
        self.setObjectName('categoryInlineParameters')
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet('QScrollArea#categoryInlineParameters { background: transparent; border: none; }')
        self.viewport().setObjectName('categoryInlineParametersViewport')
        self.viewport().setStyleSheet('QWidget#categoryInlineParametersViewport { background: transparent; }')
        self.setWidgetResizable(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setMinimumWidth(0)
        content = QWidget()
        content.setObjectName('categoryInlineParametersContent')
        content.setStyleSheet('QWidget#categoryInlineParametersContent { background: transparent; }')
        row = QHBoxLayout(content)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)
        for controls in groups:
            separator = QFrame()
            separator.setFrameShape(QFrame.VLine)
            separator.setFixedHeight(24)
            row.addWidget(separator, 0, Qt.AlignVCenter)
            group = QWidget()
            group_row = QHBoxLayout(group)
            group_row.setContentsMargins(0, 0, 0, 0)
            group_row.setSpacing(8)
            for control in controls:
                group_row.addWidget(control, 0, Qt.AlignVCenter)
            row.addWidget(group, 0, Qt.AlignVCenter)
        row.addStretch(1)
        self.setWidget(content)
        content.setAutoFillBackground(False)
        self.setFixedHeight(content.sizeHint().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        height = self.widget().sizeHint().height()
        if self.widget().minimumSizeHint().width() > self.viewport().width():
            height += self.style().pixelMetric(QStyle.PM_ScrollBarExtent)
        self.setFixedHeight(height)


def refresh_category_toolbar(toolbar, translate):
    for label in toolbar.findChildren(QLabel):
        key = label.property('categoryStageLabelKey')
        if key:
            label.setText(translate(key))
        if label.objectName() == 'targetLangCodeBadge':
            label.setToolTip(translate('current_lang_tip'))


class CategoryActions(QGroupBox):
    def __init__(self, *, framed=False):
        super().__init__()
        self.framed = framed

    def setTitle(self, title):
        if self.framed:
            super().setTitle(title)
        self.setAccessibleName(title)


def category_actions(title, *, framed=False):
    group = CategoryActions(framed=framed)
    group.setTitle(title)
    if framed:
        group.setObjectName('categoryTableActions')
    else:
        group.setObjectName('categoryStageActions')
        group.setStyleSheet('QGroupBox#categoryStageActions { border: none; margin: 0; padding: 0; background: transparent; }')
    layout = TransferActionsLayout(group)
    margin = 8 if framed else 0
    layout.setContentsMargins(margin, margin, margin, margin)
    layout.setSpacing(4)
    return group, layout


def set_category_button_tooltip(button, key, translate):
    tooltip_key = 'button_' + key + '_tip'
    button.setProperty('categoryButtonTooltipKey', tooltip_key)
    button.setToolTip(translate(tooltip_key))


def refresh_category_button_tooltips(root, translate):
    for button in root.findChildren(QPushButton) + root.findChildren(QToolButton):
        key = button.property('categoryButtonTooltipKey')
        if button.objectName() in ('ScrollLeftButton', 'ScrollRightButton'):
            key = 'button_tabs_left_tip' if button.objectName() == 'ScrollLeftButton' else 'button_tabs_right_tip'
        if key:
            button.setToolTip(translate(key))


class CategoryRunLayout(TransferToolbarLayout):
    """Stretch the working controls and keep the next action at the right edge."""

    def _arrange(self, rect, apply):
        if self.count() != 2:
            return super()._arrange(rect, apply)
        left, top, right, bottom = self.getContentsMargins()
        controls, next_action = self._items
        width = max(1, rect.width() - left - right)
        next_size = next_action.sizeHint().expandedTo(next_action.minimumSize())
        wrapped = controls.minimumSize().width() + next_size.width() + self.spacing() > width
        controls_width = width if wrapped else max(1, width - next_size.width() - self.spacing())
        controls_height = controls.heightForWidth(controls_width) if controls.hasHeightForWidth() else controls.sizeHint().height()
        controls_height = max(controls_height, controls.minimumSize().height())
        row_height = max(controls_height, next_size.height())
        next_y = rect.y() + top + (controls_height + self.spacing() if wrapped else row_height - next_size.height())
        if apply:
            controls.setGeometry(QRect(rect.x() + left, rect.y() + top, controls_width, controls_height))
            next_action.setGeometry(QRect(rect.right() - right - next_size.width() + 1, next_y,
                                          next_size.width(), next_size.height()))
        return top + bottom + (controls_height + self.spacing() + next_size.height() if wrapped else row_height)


class CategoryRunRow(QWidget):
    def __init__(self, controls, next_action, *, export_action=None):
        super().__init__()
        if export_action is not None:
            end_actions = QWidget()
            end_actions.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
            end_row = QHBoxLayout(end_actions)
            end_row.setContentsMargins(0, 0, 0, 0)
            end_row.setSpacing(4)
            end_row.addWidget(export_action, 0, Qt.AlignBottom)
            end_row.addWidget(next_action, 0, Qt.AlignBottom)
            next_action = end_actions
        layout = CategoryRunLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(controls)
        layout.addWidget(next_action)


class CategoryProgressBar(QProgressBar):
    def paintEvent(self, event):
        super().paintEvent(event)
        if self.minimum() == self.maximum() == 0 and self.isTextVisible():
            painter = QPainter(self)
            painter.setPen(self.palette().color(QPalette.Text))
            painter.drawText(self.rect(), Qt.AlignCenter, self.format())


class CategoryProgressFooter(QWidget):
    """One progress/stop row with identical sizing across category stages."""

    def __init__(self, stop, language, parent=None):
        super().__init__(parent)
        self.language = language
        self.setObjectName('categoryProgressFooter')
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.label = QLabel(self)
        self.label.setMaximumHeight(42)
        self.label.setTextFormat(Qt.PlainText)
        self.label.hide()
        self.bar = CategoryProgressBar(self)
        self.stop_btn = QPushButton(self)
        self.stop_btn.setFixedWidth(112)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(stop)
        row.addWidget(self.bar, 1)
        row.addWidget(self.stop_btn)
        self.refresh_localized_texts()
        init_progress(self.label, self.bar, 0,
                      processed_label=translate_key('ui.processed_short', language(), 'Processed'))

    def refresh_localized_texts(self):
        self.stop_btn.setText(translate_key('ui.stop', self.language(), 'Stop'))
        self.stop_btn.setToolTip(translate_key('ui.transfer.button_stop_tip', self.language(),
                                               'Stop the current operation; completed results are kept.'))


class CategoryWorkspaceFrame(QGroupBox):
    """One outer frame, with its header above equally sized table/log panes."""

    def __init__(self, title, toolbar, workspace, parent=None):
        super().__init__(title, parent)
        self.setObjectName('categoryWorkspaceFrame')
        row = QVBoxLayout(self)
        row.setContentsMargins(8, 8, 8, 8)
        row.setSpacing(6)
        row.addWidget(toolbar)
        row.addWidget(workspace, 1)


def highlight_next_action(button, active, theme):
    active = bool(active and button.isEnabled())
    if button.property('categoryNextAction') == active and button.property('categoryActionTheme') == theme:
        return
    button.setProperty('categoryNextAction', active)
    button.setProperty('categoryActionTheme', theme)
    color = {'light': '#287fa3', 'dark': '#72c9f0', 'teal': '#64cad0'}.get(theme, '#64cad0')
    button.setStyleSheet(f'QPushButton, QToolButton {{ border: 1px solid {color}; }}' if active else '')


def remove_inner_frame(group):
    group.setTitle('')
    group.setObjectName('categoryWorkspaceContent')
    group.setStyleSheet('QGroupBox#categoryWorkspaceContent { border: none; margin: 0; padding: 0; background: transparent; }')
    group.layout().setContentsMargins(0, 0, 0, 0)


class CategoryLogPanel(QWidget):
    """A right-hand log controlled by one button above the working table."""

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
        log.setMinimumHeight(0)
        log.setMaximumHeight(16777215)
        font = QFont('Consolas', 9)
        font.setFixedPitch(True)
        log.setFont(font)
        self.wrap = create_log_wrap(self, log, with_header=False)
        layout.addWidget(self.wrap, 1)
        self.refresh_localized_texts()
        self.set_expanded(False)
        self.hide()

    def refresh_localized_texts(self):
        self.toggle.setText(self._translate('log'))
        self.toggle.setToolTip(self._translate('hide_log' if self.expanded else 'show_log'))
        self.wrap.clear_button.setToolTip(self._translate('button_clear_log_tip'))

    def set_expanded(self, expanded):
        self.expanded = bool(expanded)
        blocked = self.toggle.blockSignals(True)
        self.toggle.setChecked(self.expanded)
        self.toggle.blockSignals(blocked)
        self.toggle.setArrowType(Qt.RightArrow if self.expanded else Qt.LeftArrow)
        self.refresh_localized_texts()
        self.fit_workspace()

    def fit_workspace(self):
        splitter = self.parentWidget()
        if isinstance(splitter, CategoryWorkspace):
            splitter.fit_log()


class CategoryTableToolbar(QWidget):
    """Keep table context on the left and the log toggle on the right."""

    def __init__(self, tools, parent=None):
        super().__init__(parent)
        self.setObjectName('categoryTableToolbar')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(tools, 1)

    def set_log_panel(self, panel):
        self.layout().addWidget(panel.toggle, 0, Qt.AlignTop)
        panel.toggle.show()


class CategoryParametersPanel(QScrollArea):
    """Expand settings vertically without reserving space while collapsed."""

    def __init__(self, settings, options, parent=None, *, minimum_visible_height=160):
        super().__init__(parent)
        self.setObjectName('categorySettingsScroll')
        self.options = options
        self.settings = settings
        self.minimum_visible_height = minimum_visible_height
        self.setFrameShape(QFrame.NoFrame)
        self.setWidgetResizable(True)
        self.setStyleSheet('QScrollArea#categorySettingsScroll { background: transparent; border: none; }')
        self.viewport().setObjectName('categorySettingsViewport')
        self.viewport().setStyleSheet('QWidget#categorySettingsViewport { background: transparent; }')
        self.setWidget(settings)
        settings.setAutoFillBackground(False)
        settings.installEventFilter(self)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        options.toggled.connect(self.set_expanded)
        self.set_expanded(options.isChecked())

    def set_expanded(self, checked):
        self.settings.show()
        self.setVisible(checked)
        self.options.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
        self._fit_settings()

    def _fit_settings(self):
        layout = self.settings.layout()
        natural = (layout.totalHeightForWidth(max(1, self.width() - 16))
                   if layout.hasHeightForWidth() else self.settings.sizeHint().height())
        self.settings.setFixedHeight(natural)
        available = max(self.minimum_visible_height, (self.window().height() - 540) // 2)
        self.setFixedHeight(min(210, available, natural))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_settings()

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_settings()

    def eventFilter(self, obj, event):
        if obj is self.settings and event.type() == QEvent.LayoutRequest and self.isVisible():
            self._fit_settings()
        return super().eventFilter(obj, event)


class CategoryWorkspace(QSplitter):
    """The same table/log arrangement for every independent stage."""

    def __init__(self, content, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setObjectName('categoryStageWorkspace')
        self.log_panel = None
        self._log_fraction = .4
        self.setStyleSheet('QSplitter#categoryStageWorkspace::handle { background: transparent; }')
        self.setChildrenCollapsible(False)
        self.setHandleWidth(6)
        content.setMinimumWidth(0)
        content.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.addWidget(content)
        self.setStretchFactor(0, 1)

    def set_log_panel(self, panel):
        previous = panel.parentWidget()
        if isinstance(previous, CategoryWorkspace) and previous is not self:
            previous.log_panel = None
        self.log_panel = panel
        self.addWidget(panel)
        self.setStretchFactor(1, 1)
        self.fit_log()

    def fit_log(self):
        if not self.log_panel:
            return
        if self.log_panel.expanded:
            self.log_panel.show()
            width = max(240, min(int(self.width() * self._log_fraction), max(240, self.width() - 280)))
            self.setSizes([max(1, self.width() - width), width])
        else:
            if self.log_panel.isVisible() and self.sizes()[1] > 0:
                self._log_fraction = self.sizes()[1] / max(1, sum(self.sizes()))
            self.log_panel.hide()
            self.setSizes([max(1, self.width()), 0])
