"""Jarvis on your desktop, in the window: the first-run "Jarvis on your desktop" card and Settings > Desktop's own
part (hard rule 15; SPEC sections 10.3 and 11; Phase 1, part 9; hologram look, British spelling, amber only because
a decision waits).

Nothing here turns anything on, stores anything or goes onto the desktop: every click only emits a signal, and the app
(ui.AppController with desktop_ui.DesktopController) stores the owner's OK and goes on from there.

``ConsentCard(parent)``
    The first-run card, over the window's screen (the Find list's way: a ``void`` scrim and a panel in the middle):
    the kicker "DESKTOP <middle dot> NEEDS YOUR OK", the title "Jarvis on your desktop", what Jarvis will and won't do in this
    phase (``CONSENT_ROWS``: SPEC 10.3's rows that are live in Phase 1), where it can be turned off again, and "Not now"
    (ghost, left) / "Turn on" (amber, right; DesktopB's order). ``open_card(entry)`` shows it for an entry (the Dock's
    Desktop, the HUD key, a --hud start or a typed request - desktop_ui.ENTRIES), ``close_card()``, ``is_open()``,
    ``entry()``, ``set_problem(text)`` (a line in coral: the OK could not be saved). The keyboard: the focus starts on
    Not now - Enter or Space there turns nothing on -, Tab and Shift+Tab stay between the two buttons, Esc and a click
    on the scrim are Not now; when it closes the focus goes back where it was. Signals ``turnOnClicked(entry)`` and
    ``notNowClicked(entry)``.

``DesktopSettingsPanel()``
    Under Settings > Desktop's rows: while the owner's OK is stored, a line saying so and "Turn off" (ghost; it takes
    the OK back - nothing desktop-side works until he turns it on again from the card); before that, how to turn it on
    (Desktop in the Dock). ``set_state(turned_on, problem="")``; signal ``turnOffClicked()``.
"""

from __future__ import annotations

from typing import Any

import shiboken6
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from . import holo, hud
from .memory_view import body_label

CONSENT_KICKER = "DESKTOP \u00b7 NEEDS YOUR OK"
CONSENT_TITLE = "Jarvis on your desktop"
# SPEC 10.3's first-run rows, only those live in Phase 1 (moving, timers, music and work inside apps come later).
CONSENT_ROWS = ("I open files, apps, folders and sites you name.",
                "I only work in the folders and apps you allow.",
                "To plan, file, folder and app names go to your Claude plan. They're never kept in logs.")
CONSENT_FOOT = "You can turn it off again in Settings \u203a Desktop."
NOT_NOW_TEXT = "Not now"
TURN_ON_TEXT = "Turn on"
NOT_SAVED_TEXT = "Your OK couldn't be saved, so nothing was turned on. Try again."
TURN_OFF_TEXT = "Turn off"
SETTINGS_ON_TEXT = ("On, because you clicked Turn on. Turn off takes your OK back - nothing on your desktop works "
                    "until you turn it on again.")
SETTINGS_OFF_TEXT = ("To turn it on, click Desktop in the Dock and read its card - nothing on your desktop works until "
                     "you click Turn on.")
TURN_OFF_NOT_SAVED_TEXT = ("Turned off for now - it couldn't be saved, so it will be on again when Jarvis restarts.")
PANEL_WIDTH = 540
PANEL_PAD = (20, 22)                   # vertical, horizontal
PANEL_RADIUS = 12
BRACKET_LEG = 14
ROW_GAP = 6


class _Panel(QWidget):
    """The amber NEEDS YOUR OK card (DesktopB .glass.amb): opaque ``tile-amber``, an amber line and corner brackets,
    the faint scan lines."""

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = QRectF(self.rect())
            path = QPainterPath()
            path.addRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), PANEL_RADIUS, PANEL_RADIUS)
            painter.fillPath(path, QColor(holo.TILE_AMBER))
            painter.save()
            painter.setClipPath(path)
            painter.fillRect(rect, holo.scan_brush(holo.SCAN_AMBER))
            painter.restore()
            painter.setPen(QPen(holo.alpha(holo.AMBER, 0.60), 1.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            holo.paint_brackets(painter, rect, QColor(holo.AMBER), leg=BRACKET_LEG, radius=PANEL_RADIUS, width=2.0)
        finally:
            painter.end()


class _Kicker(QWidget):
    """The kicker with its amber dot (DesktopB .kick .kd)."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = text
        self._font = hud.mono_font(10, 500, 0.22)
        self.setFixedHeight(14)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(text)

    def text(self) -> str:
        return self._text

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            dot = QPointF(3, self.height() / 2)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(holo.alpha(holo.AMBER, 0.3))
            painter.drawEllipse(dot, 5.0, 5.0)
            painter.setBrush(QColor(holo.AMBER))
            painter.drawEllipse(dot, 3.0, 3.0)
            painter.setFont(self._font)
            painter.setPen(QColor(holo.AMBER))
            room = QRectF(14, 0, self.width() - 14, self.height())
            painter.drawText(room, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             QFontMetricsF(self._font).elidedText(self._text, Qt.TextElideMode.ElideRight,
                                                                  room.width()))
        finally:
            painter.end()


class _Dot(QWidget):
    """A line's small amber dot, on its first line."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(6, 20)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(holo.alpha(holo.AMBER, 0.85))
            painter.drawEllipse(QPointF(3, 10), 2.5, 2.5)
        finally:
            painter.end()


class _Bullet(QWidget):
    """One of the card's lines: a small amber dot and the line, wrapping."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.label = body_label(text, "body_sm", holo.AMBER_TX2)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(_Dot(), 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.label, 1)


class ConsentCard(QWidget):
    """The first-run card; see the module docstring."""

    turnOnClicked = Signal(str)
    notNowClicked = Signal(str)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("desktopConsentCard")
        self.setAccessibleName(CONSENT_TITLE)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._entry = ""
        self._opener: QWidget | None = None
        self.panel = _Panel(self)
        self.kicker = _Kicker(CONSENT_KICKER)
        self.title = body_label(CONSENT_TITLE, "screen_title", holo.AMBER_TX, wrap=False, name="desktopConsentTitle")
        self.rows = [_Bullet(text) for text in CONSENT_ROWS]
        self.foot = body_label(CONSENT_FOOT, "body_sm", holo.AMBER_TX2, name="desktopConsentFoot")
        self.problem = body_label("", "body_sm", holo.ALERT_TX, name="desktopConsentProblem")
        self.problem.hide()
        self.not_now = holo.HoloButton(NOT_NOW_TEXT, holo.AMBER_GHOST)
        self.not_now.clicked.connect(self._on_not_now)
        self.turn_on = holo.HoloButton(TURN_ON_TEXT, holo.AMBER_COMMIT)
        self.turn_on.clicked.connect(self._on_turn_on)
        for button in (self.not_now, self.turn_on):
            button.installEventFilter(self)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(holo.BUTTON_GAP)
        buttons.addWidget(self.not_now)
        buttons.addStretch(1)
        buttons.addWidget(self.turn_on)
        column = QVBoxLayout(self.panel)
        column.setContentsMargins(PANEL_PAD[1], PANEL_PAD[0], PANEL_PAD[1], PANEL_PAD[0])
        column.setSpacing(0)
        column.addWidget(self.kicker)
        column.addSpacing(8)
        column.addWidget(self.title)
        column.addSpacing(12)
        for index, row in enumerate(self.rows):
            if index:
                column.addSpacing(ROW_GAP)
            column.addWidget(row)
        column.addSpacing(12)
        column.addWidget(self.foot)
        column.addWidget(self.problem)
        column.addSpacing(16)
        column.addLayout(buttons)
        parent.installEventFilter(self)
        self.hide()

    # ---- open / close ---------------------------------------------------------------------------

    def is_open(self) -> bool:
        return self.isVisible()

    def entry(self) -> str:
        """What the owner asked for when it opened (desktop_ui.ENTRIES)."""
        return self._entry

    def open_card(self, entry: str) -> None:
        """Show the card for ``entry`` over the window's screen, the focus on Not now (the one before kept)."""
        if not self.isVisible():
            focus = self.window().focusWidget()
            self._opener = focus if focus is not None and not self.isAncestorOf(focus) else None
        self._entry = str(entry or "")
        self.set_problem("")
        self.setGeometry(self.parentWidget().rect())
        self._place_panel()
        self.show()
        self.raise_()
        self.not_now.setFocus(Qt.FocusReason.OtherFocusReason)

    def close_card(self, restore_focus: bool = True) -> None:
        if not self.isVisible():
            return
        opener, self._opener = self._opener, None
        self.hide()
        if restore_focus and opener is not None and shiboken6.isValid(opener) and opener.isVisible():
            opener.setFocus(Qt.FocusReason.OtherFocusReason)

    def set_problem(self, text: str) -> None:
        self.problem.setText(text)
        self.problem.setVisible(bool(text))
        if self.isVisible():
            self._place_panel()

    def texts(self) -> list[str]:
        """Every text the card shows (tests and the harness)."""
        return [self.kicker.text(), self.title.text(), *(row.label.text() for row in self.rows), self.foot.text(),
                *([self.problem.text()] if self.problem.isVisible() else []), self.not_now.text(),
                self.turn_on.text()]

    def _place_panel(self) -> None:
        width = min(PANEL_WIDTH, max(300, self.width() - 32))
        self.panel.setFixedWidth(width)
        self.panel.adjustSize()
        height = self.panel.sizeHint().height()
        self.panel.setGeometry(int((self.width() - width) / 2), int(max(16, (self.height() - height) / 2)),
                               int(width), int(height))

    # ---- clicks and keys ----------------------------------------------------------------------------

    def _on_turn_on(self) -> None:
        self.turnOnClicked.emit(self._entry)

    def _on_not_now(self) -> None:
        entry = self._entry
        self.close_card()
        self.notNowClicked.emit(entry)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize and self.isVisible():
            self.setGeometry(self.parentWidget().rect())
            self._place_panel()
        elif watched in (self.not_now, self.turn_on) and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self._on_not_now()
                return True
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):     # trapped between the two buttons
                other = self.turn_on if watched is self.not_now else self.not_now
                other.setFocus(Qt.FocusReason.TabFocusReason)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                watched.click()
                return True
        return False

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape:
            self._on_not_now()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if not self.panel.geometry().contains(event.position().toPoint()):
            self._on_not_now()                           # the scrim: Not now
        event.accept()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._place_panel()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), holo.alpha(holo.VOID, 0.6))
        finally:
            painter.end()


class DesktopSettingsPanel(QWidget):
    """Settings > Desktop's own part; see the module docstring."""

    turnOffClicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.line: QLabel = body_label(SETTINGS_OFF_TEXT, "body_sm", holo.INK_2, name="desktopSettingsLine")
        self.problem: QLabel = body_label("", "body_sm", holo.ALERT_TX, name="desktopSettingsProblem")
        self.problem.hide()
        self.turn_off = holo.HoloButton(TURN_OFF_TEXT, holo.GHOST, size=holo.SIZE_SM)
        self.turn_off.setAccessibleName("Turn off Jarvis on your desktop")
        self.turn_off.clicked.connect(self.turnOffClicked)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        row.addWidget(self.line, 1)
        row.addWidget(self.turn_off, 0, Qt.AlignmentFlag.AlignTop)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        column.addLayout(row)
        column.addWidget(self.problem)
        self.set_state(False)

    def set_state(self, turned_on: bool, problem: str = "") -> None:
        """The owner's OK as stored now (``turned_on``) and a problem line (coral; "" none)."""
        self.line.setText(SETTINGS_ON_TEXT if turned_on else SETTINGS_OFF_TEXT)
        self.turn_off.setVisible(bool(turned_on))
        self.problem.setText(problem)
        self.problem.setVisible(bool(problem))
