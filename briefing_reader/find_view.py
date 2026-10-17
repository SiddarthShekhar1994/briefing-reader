"""The Find list (the approved motion prototype): Ctrl+K or the header's "Find or ask anything" pill.

An overlay (HOLOGRAM_SPEC 11.6: a `raised` panel, radius 14, `line-strong` border, over a flat `void`
scrim; enter = fade + 8 px rise 280 ms, exit 180 ms; none when motion is off) with a text field and a
filtered list. The list only offers what the app already has: every screen (its shortcut as the hint and
what waits on it in words, "Approvals - 2 waiting"), the actions that exist as links right now (an open
offer's Plan my day / Prepare a brief) and, when the text matches nothing, "Ask Jarvis: <text>". Picking
emits ``picked(FindItem)``; the screen that owns the overlay does exactly what the Dock button, the link or
Enter in the Ask bar does. Nothing here approves, sends or changes anything.

Keyboard: Up / Down choose (wrapping), Home / End, Enter picks, Esc closes; Tab stays in the field (focus
is trapped in the overlay); closing returns focus to the opener. A click on the scrim closes it.

Public API
==========

``FindItem(kind, key, label, hint, icon)``: ``KIND_SCREEN`` (key: a Dock slot), ``KIND_ACTION`` (key: the
link id), ``KIND_ASK`` (key: the text), ``KIND_NONE`` (a note, never picked).
``filter_items(items, query) -> list[FindItem]``: case-insensitive, every word of ``query`` in the label.
``FindButton()``: the header pill ("Find or ask anything" + CTRL K); ``set_compact(bool)`` folds it to a
40 px round search button (narrow windows), ``is_compact()``.
``FindOverlay(parent)``: ``open_list(provider, opener=None)`` (``provider(query) -> list[FindItem]`` is asked
again on every keystroke, so counts are current), ``close_list(restore_focus=True)``, ``is_open()``,
``items()``, ``current()``, ``current_item()``, ``set_current(index)``, ``pick_current()``; signals
``picked(object)`` and ``closed()``; ``field``, ``panel``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import shiboken6
from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QLineEdit, QSizePolicy, QVBoxLayout, QWidget

from . import holo

KIND_SCREEN = "screen"
KIND_ACTION = "action"
KIND_ASK = "ask"
KIND_NONE = "none"
FIND_TEXT = "Find or ask anything"
FIND_NAME = "Find a screen or ask Jarvis"
FIND_SHORTCUT = "Ctrl+K"
FIND_KBD = "CTRL K"
PLACEHOLDER = "Go to a screen, or type a request\u2026"
ASK_PREFIX = "Ask Jarvis: "
NOTHING_TEXT = "Nothing matches"
PANEL_WIDTH = 580
PANEL_TOP = 84
PANEL_PAD = 12
FIELD_HEIGHT = 46
ROW_HEIGHT = 46
PILL_WIDTH = 320
PILL_HEIGHT = 40


@dataclass(frozen=True)
class FindItem:
    kind: str
    key: str
    label: str
    hint: str = ""
    icon: str = ""

    def pickable(self) -> bool:
        return self.kind != KIND_NONE


def filter_items(items: list[FindItem], query: str) -> list[FindItem]:
    words = query.casefold().split()
    return [item for item in items if all(word in item.label.casefold() for word in words)]


def _paint_kbd(painter: QPainter, right: float, center_y: float, text: str) -> float:
    """A key hint chip ending at ``right`` (mono 10, ``ink-4``, a holo @ .25 border); returns its width."""
    font = holo.font("kbd")
    width = QFontMetricsF(font).horizontalAdvance(text) + 12
    rect = QRectF(right - width, center_y - 10, width, 20)
    painter.setPen(QPen(holo.alpha(holo.HOLO, 0.25), 1.0))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
    painter.setFont(font)
    painter.setPen(QColor(holo.INK_4))
    painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), text)
    return width


class FindButton(QAbstractButton):
    """The header pill: a search glyph, "Find or ask anything" and CTRL K (40 px tall, radius 20)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._compact = False
        self._hover = False
        self._keyboard = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAccessibleName(FIND_NAME)
        self.setAccessibleDescription(f"Shortcut {FIND_SHORTCUT}")
        self.setToolTip(f"{FIND_TEXT} ({FIND_SHORTCUT})")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_compact(self, compact: bool) -> None:
        compact = bool(compact)
        if compact != self._compact:
            self._compact = compact
            self.updateGeometry()
            self.update()

    def is_compact(self) -> bool:
        return self._compact

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(PILL_HEIGHT if self._compact else PILL_WIDTH, PILL_HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.sizeHint()

    def enterEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().enterEvent(event)
        self._hover = True
        self.update()

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().leaveEvent(event)
        self._hover = False
        self.update()

    def focusInEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._keyboard = holo.keyboard_focus(event.reason(), self._keyboard)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.reason() != Qt.FocusReason.ActiveWindowFocusReason:
            self._keyboard = False
        super().focusOutEvent(event)
        self.update()

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.animateClick()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            radius = rect.height() / 2
            if self._hover:
                painter.setPen(QPen(holo.alpha(holo.HOLO, 0.10), 4.0))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), radius - 2, radius - 2)
            painter.setPen(QPen(QColor(holo.HOLO) if self._hover else holo.LINE_STRONG, 1.0))
            painter.setBrush(holo.alpha(holo.TILE, 0.72))
            painter.drawRoundedRect(rect, radius, radius)
            glyph = QRectF(0, 0, 18, 18)
            if self._compact:
                glyph.moveCenter(rect.center())
            else:
                glyph.moveCenter(QPointF(14 + 9, rect.center().y()))
            holo.paint_icon(painter, glyph, "search", holo.INK_3 if not self._hover else holo.INK_1, stroke=1.6)
            if not self._compact:
                kbd = _paint_kbd(painter, rect.right() - 8, rect.center().y(), FIND_KBD)
                font = holo.font("find_item")
                painter.setFont(font)
                painter.setPen(QColor(holo.INK_3))
                room = QRectF(glyph.right() + 10, 0, rect.right() - 8 - kbd - 10 - glyph.right() - 10, self.height())
                text = QFontMetricsF(font).elidedText(FIND_TEXT, Qt.TextElideMode.ElideRight, room.width())
                painter.drawText(room, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
            if self._keyboard and self.hasFocus():
                holo.paint_focus_ring(painter, rect, radius, offset=1.0)
        finally:
            painter.end()


class _FindField(QLineEdit):
    """The overlay's field: 46 px, radius 10, `void` fill, a holo border (brighter while focused)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(FIELD_HEIGHT)
        self.setFont(holo.font("input"))
        self.setPlaceholderText(PLACEHOLDER)
        self.setAccessibleName(FIND_NAME)
        self.setFrame(False)
        self.setTextMargins(14, 0, 14, 0)
        self.setStyleSheet(f"QLineEdit {{ background: transparent; color: {holo.INK_1}; border: none; "
                           f"selection-background-color: {holo.HOLO_LO}; }}")

    def focusNextPrevChild(self, _next: bool) -> bool:  # noqa: N802 - Qt override
        return True                         # Tab stays here: focus is trapped in the overlay

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            painter.setPen(QPen(QColor(holo.HOLO) if self.hasFocus() else holo.alpha(holo.HOLO, 0.3), 1.0))
            painter.setBrush(QColor(holo.VOID))
            painter.drawRoundedRect(rect, 10, 10)
        finally:
            painter.end()
        super().paintEvent(event)


class _FindRows(QWidget):
    """The filtered rows (46 px each): glyph, label, key hint; the current one on `holo` @ .10."""

    rowClicked = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._items: list[FindItem] = []
        self._current = 0
        self._hover = -1
        self._first = 0                     # the first row shown (the list scrolls with the current row)
        self._rows = 9

    def set_items(self, items: list[FindItem], current: int, rows: int) -> None:
        self._items = list(items)
        self._rows = max(1, rows)
        self._current = current
        self._scroll_to(current)
        self.setFixedHeight(ROW_HEIGHT * max(1, min(len(self._items), self._rows)))
        self.update()

    def shown_rows(self) -> int:
        return max(1, min(len(self._items), self._rows))

    def _scroll_to(self, index: int) -> None:
        if index < self._first:
            self._first = index
        elif index >= self._first + self._rows:
            self._first = index - self._rows + 1
        self._first = max(0, min(self._first, max(0, len(self._items) - self._rows)))

    def row_at(self, y: float) -> int:
        index = self._first + int(y // ROW_HEIGHT)
        return index if 0 <= index < len(self._items) else -1

    def mouseMoveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        hover = self.row_at(event.position().y())
        if hover != self._hover:
            self._hover = hover
            self.update()

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().leaveEvent(event)
        self._hover = -1
        self.update()

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.row_at(event.position().y())
            if index >= 0:
                self.rowClicked.emit(index)
                return
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            font = holo.font("find_item")
            metrics = QFontMetricsF(font)
            for row in range(self.shown_rows()):
                index = self._first + row
                if index >= len(self._items):
                    break
                item = self._items[index]
                rect = QRectF(0, row * ROW_HEIGHT, self.width(), ROW_HEIGHT)
                if item.pickable() and index in (self._current, self._hover):
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(holo.alpha(holo.HOLO, 0.10 if index == self._current else 0.06))
                    painter.drawRoundedRect(rect, 10, 10)
                left = 12.0
                if item.icon:
                    box = QRectF(left, rect.center().y() - 9, 18, 18)
                    holo.paint_icon(painter, box, item.icon, holo.HOLO, stroke=1.6)
                    left += 18 + 12
                right = rect.right() - 12
                if item.hint:
                    right -= _paint_kbd(painter, right, rect.center().y(), item.hint) + 12
                painter.setFont(font)
                painter.setPen(QColor(holo.INK_1 if item.pickable() else holo.INK_4))
                text = metrics.elidedText(item.label, Qt.TextElideMode.ElideRight, right - left)
                painter.drawText(QRectF(left, rect.top(), right - left, ROW_HEIGHT),
                                 int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
        finally:
            painter.end()


class _Panel(QWidget):
    """The `raised` panel (radius 14, `line-strong` border)."""

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(holo.LINE_STRONG, 1.0))
            painter.setBrush(QColor(holo.RAISED))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), holo.RADIUS_DIALOG,
                                    holo.RADIUS_DIALOG)
        finally:
            painter.end()


class FindOverlay(QWidget):
    """See the module docstring."""

    picked = Signal(object)
    closed = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAccessibleName("Find or ask")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._provider: Callable[[str], list[FindItem]] | None = None
        self._items: list[FindItem] = []
        self._current = 0
        self._opener: QWidget | None = None
        self._reveal = 0.0
        self._closing = False
        self.panel = _Panel(self)
        column = QVBoxLayout(self.panel)
        column.setContentsMargins(PANEL_PAD, PANEL_PAD, PANEL_PAD, PANEL_PAD)
        column.setSpacing(8)
        self.field = _FindField(self.panel)
        self.field.textChanged.connect(self._refresh)
        self.field.installEventFilter(self)
        self.rows = _FindRows(self.panel)
        self.rows.rowClicked.connect(self._on_row_clicked)
        column.addWidget(self.field)
        column.addWidget(self.rows)
        self._anim = QVariantAnimation(self)
        self._anim.valueChanged.connect(self._on_reveal)
        self._anim.finished.connect(self._on_anim_done)
        parent.installEventFilter(self)
        self.hide()

    # ---- open / close ---------------------------------------------------------------------------

    def is_open(self) -> bool:
        return self.isVisible() and not self._closing

    def open_list(self, provider: Callable[[str], list[FindItem]], opener: QWidget | None = None) -> None:
        self._provider = provider
        if not self.is_open():
            focus = opener if opener is not None else self.window().focusWidget()
            self._opener = focus if focus is not None and not self.isAncestorOf(focus) else None
        self._closing = False
        self.field.blockSignals(True)
        self.field.clear()
        self.field.blockSignals(False)
        self._current = 0
        self.setGeometry(self.parentWidget().rect())
        self._refresh()
        self.show()
        self.raise_()
        self.field.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self._animate(1.0, holo.motion_ms(holo.DRAWER_IN_MS))

    def close_list(self, restore_focus: bool = True) -> None:
        if not self.isVisible() or self._closing:
            return
        opener = self._opener
        self._opener = None
        if restore_focus and opener is not None and shiboken6.isValid(opener) and opener.isVisible():
            opener.setFocus(Qt.FocusReason.OtherFocusReason)
        elif self.field.hasFocus():
            self.field.clearFocus()
        duration = holo.motion_ms(holo.DRAWER_OUT_MS)
        if duration <= 0:
            self._finish_close()
            return
        self._closing = True
        self._animate(0.0, duration)

    def _finish_close(self) -> None:
        self._closing = False
        self._anim.stop()
        self._reveal = 0.0
        self.hide()
        self.closed.emit()

    def _animate(self, target: float, duration: int) -> None:
        self._anim.stop()
        if duration <= 0:
            self._on_reveal(target)
            return
        self._anim.setStartValue(self._reveal)
        self._anim.setEndValue(target)
        self._anim.setDuration(duration)
        self._anim.setEasingCurve(holo.qt_easing(holo.EASE_STD if target > 0 else holo.EASE_IN))
        self._anim.start()

    def _on_reveal(self, value: Any) -> None:
        self._reveal = float(value)
        self._place_panel()
        self.update()

    def _on_anim_done(self) -> None:
        if self._closing:
            self._finish_close()

    def reveal(self) -> float:
        return self._reveal

    # ---- items ---------------------------------------------------------------------------------

    def items(self) -> list[FindItem]:
        return list(self._items)

    def current(self) -> int:
        return self._current

    def current_item(self) -> FindItem | None:
        if 0 <= self._current < len(self._items) and self._items[self._current].pickable():
            return self._items[self._current]
        return None

    def set_current(self, index: int) -> None:
        if not self._items:
            return
        self._current = index % len(self._items)
        self._sync_rows()

    def _refresh(self, *_args: Any) -> None:
        self._items = list(self._provider(self.field.text())) if self._provider is not None else []
        self._current = 0
        self._sync_rows()

    def _sync_rows(self) -> None:
        self.rows.set_items(self._items, self._current, self._max_rows())
        item = self.current_item()
        self.field.setAccessibleDescription(f"{item.label}. {item.hint}".strip(". ") if item is not None
                                            else (self._items[0].label if self._items else ""))
        self._place_panel()

    def _max_rows(self) -> int:
        room = self.height() - PANEL_TOP - 2 * PANEL_PAD - FIELD_HEIGHT - 8 - 16
        return max(3, int(room // ROW_HEIGHT))

    def _place_panel(self) -> None:
        width = min(PANEL_WIDTH, max(280, self.width() - 32))
        height = 2 * PANEL_PAD + FIELD_HEIGHT + 8 + self.rows.height()
        rise = 8.0 * (1.0 - self._reveal)
        top = min(PANEL_TOP, max(16, (self.height() - height) // 2))
        self.panel.setGeometry(int((self.width() - width) / 2), int(top + rise), int(width), int(height))

    # ---- picking ---------------------------------------------------------------------------------

    def pick_current(self) -> None:
        item = self.current_item()
        if item is None:
            return
        self.close_list(restore_focus=True)
        self.picked.emit(item)

    def _on_row_clicked(self, index: int) -> None:
        if 0 <= index < len(self._items) and self._items[index].pickable():
            self._current = index
            self.pick_current()

    # ---- events ------------------------------------------------------------------------------------

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize and self.isVisible():
            self.setGeometry(self.parentWidget().rect())
            self._sync_rows()
        elif watched is self.field and event.type() == QEvent.Type.KeyPress:
            return self._on_key(event.key())
        return False

    def _on_key(self, key: int) -> bool:
        if key == Qt.Key.Key_Escape:
            self.close_list(restore_focus=True)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.pick_current()
        elif key == Qt.Key.Key_Down:
            self.set_current(self._current + 1)
        elif key == Qt.Key.Key_Up:
            self.set_current(self._current - 1)
        elif key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            pass                            # trapped: the field keeps the focus
        else:
            return False
        return True

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape:
            self.close_list(restore_focus=True)
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if not self.panel.geometry().contains(event.position().toPoint()):
            self.close_list(restore_focus=True)        # the scrim
        event.accept()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._place_panel()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), holo.alpha(holo.VOID, 0.6 * self._reveal))
        finally:
            painter.end()
