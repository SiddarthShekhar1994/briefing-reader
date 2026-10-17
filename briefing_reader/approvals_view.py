"""The Approvals screen in the hologram look (HOLOGRAM_SPEC 5, 7, 8, 16.2; round 2).

Public API
==========

``ApprovalsStage(cards)``
    The screen: four 18 px stage brackets; the NEEDS YOUR OK cards (``cards``, a ``hud.ActionList``) as
    the focal column (520-600 wide, centred while there is room, else pushed left so the queue fits) and,
    at its right, the queue (``queue``, an ``ApprovalQueue``, 240 wide behind a 40 px gap that holds the
    selected tile's tether). ``open_first()`` scrolls to the first undecided card and makes it the focal
    one (the screen opens on it: no overview first); ``select(action_id)`` does the same for any card (a
    queue tile's click). Under 760 px the queue gives way and the cards take the whole width.

``ApprovalQueue``
    ``NEEDS YOUR OK`` + the amber count badge + an amber hairline; one compact needs-you tile per card
    that waits (``QueueTile``: kind label with its dot and glyph, the title in two lines at most, a mono
    meta line; the selected one with all four brackets and a tether toward the card; dimmed to 45 % while
    another approval runs); then ``Decided today`` (collapsed; ``toggle_decided()``) with a row per card
    already decided (a 16 px ok disc or steel ring, the title, the stamp). The tiles and rows scroll when
    they need more height than the screen has (never squeezed on top of each other). ``sync(cards)``
    rebuilds it from the cards; ``tileClicked(action_id)``; ``set_selected(action_id)``; ``set_count(n)``.

Nothing here decides or sends anything: the tiles only point at the cards, whose buttons decide.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import shiboken6
from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from . import holo, hud

STAGE_BRACKET = 18
CARD_MIN = 420
CARD_MAX = 600
QUEUE_WIDTH = 240
QUEUE_GAP = 40           # between the card column and the queue: the selected tile's tether lives here
TETHER = 33
QUEUE_MIN_STAGE = CARD_MIN + QUEUE_GAP + QUEUE_WIDTH + 2 * 8
QUEUE_TITLE = "NEEDS YOUR OK"
DECIDED_TITLE = "Decided today"
EMPTY_TEXT = "Nothing needs your OK, sir."
# Card statuses that still wait for the owner (a queue tile) and those already decided (Decided today).
WAITING = (hud.CARD_PENDING, hud.CARD_FAILED, hud.CARD_UNKNOWN, hud.CARD_WORKING, hud.CARD_SIGNIN)
DECIDED = (hud.CARD_ADDED, hud.CARD_EXISTS, hud.CARD_SENT, hud.CARD_DONE, hud.CARD_DENIED)


def _elide(text: str, font: Any, width: float) -> str:
    return QFontMetricsF(font).elidedText(text, Qt.TextElideMode.ElideRight, max(1.0, width))


def _wrap(text: str, font: Any, width: float, lines: int) -> list[str]:
    """``text`` in at most ``lines`` word-wrapped lines of ``width`` px, the last one elided."""
    words = text.split()
    metrics = QFontMetricsF(font)
    out: list[str] = []
    current = ""
    for index, word in enumerate(words):
        trial = f"{current} {word}".strip()
        if metrics.horizontalAdvance(trial) <= width or not current:
            current = trial
            continue
        out.append(current)
        current = word
        if len(out) == lines - 1:
            current = " ".join(words[index:])
            break
    if current:
        out.append(current)
    if len(out) > lines:
        out = out[:lines]
    if out:
        out[-1] = metrics.elidedText(out[-1], Qt.TextElideMode.ElideRight, max(1.0, width))
    return out or [""]


class QueueTile(QAbstractButton):
    """One card that waits, as a compact needs-you tile (HOLOGRAM_SPEC 5: padding 12 x 14)."""

    PAD_Y = 12
    PAD_X = 14

    def __init__(self, card: hud.ActionCard, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.action_id = card.action_id
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._label = ""
        self._title = ""
        self._meta = ""
        self._mail = False
        self._selected = False
        self._dimmed = False
        self._any_selected = False
        self._focus_visible = False
        self.update_from(card)

    def update_from(self, card: hud.ActionCard) -> None:
        self._label = card.kind_label.full_text()
        self._title = card.title()
        self._meta = card.detail_label.text().upper()
        self._mail = card.is_mail()
        self._dimmed = card.is_locked()
        self.setAccessibleName(f"{self._label}: {self._title}")
        self.setToolTip(hud.plain_tooltip(self._title))
        self.updateGeometry()
        self.update()

    def set_selected(self, selected: bool, any_selected: bool) -> None:
        if (bool(selected), bool(any_selected)) != (self._selected, self._any_selected):
            self._selected, self._any_selected = bool(selected), bool(any_selected)
            self.update()

    def is_selected(self) -> bool:
        return self._selected

    def title_lines(self, width: int | None = None) -> list[str]:
        width = self.width() if width is None else width
        return _wrap(self._title, holo.font("tile_title"), width - 2 * self.PAD_X, 2)

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        lines = len(self.title_lines(width))
        return self.PAD_Y + 14 + 6 + 22 * lines + 4 + 16 + 13

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(QUEUE_WIDTH, self.heightForWidth(max(self.width(), QUEUE_WIDTH)))

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self.setFixedHeight(self.heightForWidth(event.size().width()))

    def focusInEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._focus_visible = holo.keyboard_focus(event.reason(), self._focus_visible)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
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
            if self._dimmed:
                painter.setOpacity(0.45)
            rect = QRectF(self.rect())
            state = holo.TILE_NEEDS_SELECTED if self._selected else holo.TILE_NEEDS
            holo.paint_tile(painter, rect, state)
            x, y = self.PAD_X, self.PAD_Y
            label_font = holo.font("label")
            dot = QPointF(x + 3.5, y + 7)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(holo.AMBER))
            painter.drawEllipse(dot, 3.5, 3.5)
            glyph = QRectF(rect.right() - self.PAD_X - 16, y - 1, 16, 16)
            holo.paint_icon(painter, glyph, "send" if self._mail else "calendar", holo.AMBER, stroke=1.8)
            painter.setFont(label_font)
            painter.setPen(QColor(holo.AMBER))
            label_rect = QRectF(x + 15, y, glyph.left() - x - 23, 14)
            painter.drawText(label_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             _elide(self._label, label_font, label_rect.width()))
            y += 14 + 6
            title_font = holo.font("tile_title")
            painter.setFont(title_font)
            bright = self._selected or not self._any_selected
            painter.setPen(QColor(holo.AMBER_TX if bright else holo.AMBER_TX2))
            for line in self.title_lines():
                painter.drawText(QRectF(x, y, rect.width() - 2 * x, 22),
                                 int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), line)
                y += 22
            y += 4
            meta_font = hud.mono_font(11, 500, 0.12)
            painter.setFont(meta_font)
            painter.setPen(QColor(holo.INK_KICKER))
            painter.drawText(QRectF(x, y, rect.width() - 2 * x, 16),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             _elide(self._meta, meta_font, rect.width() - 2 * x))
            if self.hasFocus() and self._focus_visible:
                painter.setOpacity(1.0)
                holo.paint_focus_ring(painter, rect.adjusted(1, 1, -1, -1), holo.RADIUS_TILE_AMBER)
        finally:
            painter.end()


class _DecidedToggle(QAbstractButton):
    """``Decided today`` and its count (Chakra 13 / 500, ink-4, 34 high): opens and closes the list."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(34)
        self.setText(DECIDED_TITLE)
        self._count = 0
        self._focus_visible = False

    def set_count(self, count: int) -> None:
        self._count = max(0, int(count))
        self.setAccessibleName(f"{DECIDED_TITLE}, {self._count}")
        self.update()

    def count(self) -> int:
        return self._count

    def focusInEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._focus_visible = holo.keyboard_focus(event.reason(), self._focus_visible)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().focusOutEvent(event)
        self.update()

    def enterEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = QRectF(self.rect())
            if self.underMouse():
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(holo.alpha(holo.INK_1, 0.06))
                painter.drawRoundedRect(rect, holo.RADIUS_BUTTON_SM, holo.RADIUS_BUTTON_SM)
            painter.setPen(QColor(holo.INK_1 if self.underMouse() else holo.INK_4))
            painter.setFont(hud.display_font(13, 500, 0.04))
            inner = rect.adjusted(8, 0, -8, 0)
            painter.drawText(inner, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.text())
            painter.setFont(hud.mono_font(11, 400, 0.12))
            painter.drawText(inner, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), str(self._count))
            if self.hasFocus() and self._focus_visible:
                holo.paint_focus_ring(painter, rect.adjusted(2, 2, -2, -2), holo.RADIUS_BUTTON_SM)
        finally:
            painter.end()


class DecidedRow(QWidget):
    """A card decided today: a 16 px stamp disc, its title (Chakra 13, ink-3) and its stamp (mono 11)."""

    def __init__(self, title: str, stamp: str, ok: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title, self.stamp, self.ok = title, stamp, bool(ok)
        self.setFixedHeight(46)
        self.setAccessibleName(f"{title}, {stamp}")
        self.setToolTip(hud.plain_tooltip(title))

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            holo.paint_stamp(painter, QPointF(8 + 8, 6 + 2 + 8), holo.STAMP_OK if self.ok else holo.STAMP_STEEL,
                             size=16)
            x = 8 + 16 + 10
            width = self.width() - x - 8
            title_font = hud.display_font(13, 400, 0.02)
            painter.setFont(title_font)
            painter.setPen(QColor(holo.INK_3))
            painter.drawText(QRectF(x, 6, width, 20), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             _elide(self.title, title_font, width))
            stamp_font = hud.mono_font(11, 500, 0.12)
            painter.setFont(stamp_font)
            painter.setPen(QColor(holo.OK if self.ok else holo.INK_4))
            painter.drawText(QRectF(x, 26, width, 14), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             _elide(self.stamp, stamp_font, width))
        finally:
            painter.end()


class ApprovalQueue(QWidget):
    """The queue at the right of the Approvals screen; see the module docstring."""

    tileClicked = Signal(str)
    HEADER = 20

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._count = 0
        self._selected = ""
        self._tiles: list[QueueTile] = []
        self._rows: list[DecidedRow] = []
        self.setAccessibleName(QUEUE_TITLE)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(QUEUE_GAP, self.HEADER + 12, 0, 0)
        outer.setSpacing(0)
        # The tiles and Decided today scroll (a plain content widget: its layout's minimum makes the area
        # scroll instead of squeezing the fixed-height tiles together).
        self.scroll = QScrollArea(self)
        content = hud._transparent_scroll(self.scroll, QWidget())
        self.scroll.verticalScrollBar().valueChanged.connect(self.update)   # the tether follows its tile
        outer.addWidget(self.scroll, 1)
        self._column = QVBoxLayout(content)
        self._column.setContentsMargins(0, 0, 0, 0)
        self._column.setSpacing(12)
        self._tile_box = QVBoxLayout()
        self._tile_box.setContentsMargins(0, 0, 0, 0)
        self._tile_box.setSpacing(12)
        self._column.addLayout(self._tile_box)
        self.decided_toggle = _DecidedToggle()
        self.decided_toggle.toggled.connect(self._on_toggle)
        self._decided_box = QVBoxLayout()
        self._decided_box.setContentsMargins(0, 0, 0, 0)
        self._decided_box.setSpacing(2)
        self._rule = QWidget()
        self._rule.setFixedHeight(1)
        self._column.addWidget(self._rule)
        self._column.addWidget(self.decided_toggle)
        self._column.addLayout(self._decided_box)
        self._column.addStretch(1)

    # ---- content ---------------------------------------------------------------------------

    def set_count(self, count: int) -> None:
        self._count = max(0, int(count))
        self.setAccessibleName(f"{QUEUE_TITLE}, {self._count}")
        self.update()

    def count(self) -> int:
        return self._count

    def sync(self, cards: Sequence[hud.ActionCard]) -> None:
        """One tile per card that waits, one row per card decided (same order as the cards)."""
        waiting = [card for card in cards if card.actionable and card.status() in WAITING]
        decided = [card for card in cards if card.actionable and card.status() in DECIDED]
        if [tile.action_id for tile in self._tiles] != [card.action_id for card in waiting]:
            focus_was = any(tile.hasFocus() for tile in self._tiles)
            for tile in self._tiles:
                self._tile_box.removeWidget(tile)
                tile.hide()
                tile.deleteLater()
            self._tiles = []
            for card in waiting:
                tile = QueueTile(card)
                tile.clicked.connect(self._on_tile)
                self._tile_box.addWidget(tile)
                self._tiles.append(tile)
            if focus_was:
                self.decided_toggle.setFocus(Qt.FocusReason.OtherFocusReason)   # never another tile by itself
        else:
            for tile, card in zip(self._tiles, waiting):
                tile.update_from(card)
        for row in self._rows:
            self._decided_box.removeWidget(row)
            row.hide()
            row.deleteLater()
        self._rows = []
        for card in decided:
            row = DecidedRow(card.title(), card.result_text(), card.status() != hud.CARD_DENIED)
            row.setVisible(self.decided_toggle.isChecked())
            self._decided_box.addWidget(row)
            self._rows.append(row)
        self.decided_toggle.set_count(len(decided))
        self._sync_selection()
        self.update()

    def tiles(self) -> list[QueueTile]:
        return list(self._tiles)

    def tile(self, action_id: str) -> QueueTile | None:
        return next((tile for tile in self._tiles if tile.action_id == action_id), None)

    def decided_rows(self) -> list[DecidedRow]:
        return list(self._rows)

    def set_selected(self, action_id: str) -> None:
        self._selected = action_id or ""
        self._sync_selection()
        tile = self.tile(self._selected)
        if tile is not None and self.isVisible():
            self.scroll.ensureWidgetVisible(tile, 0, 12)   # the selected tile in sight (its tether too)
        self.update()

    def selected(self) -> str:
        return self._selected

    def toggle_decided(self) -> None:
        self.decided_toggle.toggle()

    def decided_open(self) -> bool:
        return self.decided_toggle.isChecked()

    def _sync_selection(self) -> None:
        present = any(tile.action_id == self._selected for tile in self._tiles)
        for tile in self._tiles:
            tile.set_selected(tile.action_id == self._selected, present)

    def _on_toggle(self, checked: bool) -> None:
        self.decided_toggle.setAccessibleDescription("open" if checked else "closed")
        for row in self._rows:
            row.setVisible(bool(checked))

    def _on_tile(self) -> None:
        tile = self.sender()
        if isinstance(tile, QueueTile):
            self.tileClicked.emit(tile.action_id)

    # ---- painting --------------------------------------------------------------------------

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            x = QUEUE_GAP + 2
            label_font = holo.font("label")
            painter.setFont(label_font)
            painter.setPen(QColor(holo.INK_KICKER))
            title_w = QFontMetricsF(label_font).horizontalAdvance(QUEUE_TITLE)
            painter.drawText(QRectF(x, 0, title_w + 4, self.HEADER),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), QUEUE_TITLE)
            x += title_w + 10
            if self._count:
                rect = holo.count_badge_rect(QPointF(0, 0), self._count)
                rect = holo.paint_count_badge(painter, QPointF(x + rect.width(), (self.HEADER - 16) / 2),
                                              self._count, ring=holo.VOID)
                x = rect.right() + 10
            right = self.width()
            if right - x > 16:
                y = self.HEADER / 2
                for i in range(24):   # a hairline fading from amber 0.3 to nothing
                    a = 0.3 * (1 - i / 24)
                    x0 = x + (right - x) * i / 24
                    x1 = x + (right - x) * (i + 1) / 24
                    painter.setPen(QPen(holo.alpha(holo.AMBER, a), 1))
                    painter.drawLine(QPointF(x0, y), QPointF(x1, y))
            if self._rule.isVisible():
                rule_y = self._rule.mapTo(self, QPoint(0, 0)).y()
                view = self.scroll.geometry()
                if view.top() <= rule_y <= view.bottom():
                    painter.setPen(QPen(holo.LINE_FAINT, 1))
                    painter.drawLine(QPointF(QUEUE_GAP, rule_y + 0.5), QPointF(self.width(), rule_y + 0.5))
            selected = self.tile(self._selected)
            if selected is not None and selected.isVisible():
                top_left = selected.mapTo(self, QPoint(0, 0))
                geo = selected.geometry().translated(top_left - selected.geometry().topLeft())
                view = self.scroll.viewport()
                shown = view.geometry().translated(self.scroll.geometry().topLeft())
                if not shown.contains(QPoint(shown.left() + 1, geo.center().y())):
                    return   # scrolled out of sight: no tether
                cy = geo.center().y() + 0.5
                painter.setPen(QPen(holo.alpha(holo.AMBER, 0.6), 1))
                painter.drawLine(QPointF(geo.left() - TETHER, cy), QPointF(geo.left(), cy))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(holo.AMBER))
                painter.drawEllipse(QPointF(geo.left() - TETHER - 2.5, cy), 2.5, 2.5)
        finally:
            painter.end()


class ApprovalsStage(QWidget):
    """The Approvals screen; see the module docstring."""

    def __init__(self, cards: hud.ActionList, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.cards = cards
        cards.setParent(self)
        self.queue = ApprovalQueue(self)
        self.queue.tileClicked.connect(self.select)
        cards.changed.connect(self._schedule_sync)
        self._sync_pending = False
        self._schedule_sync()

    def _schedule_sync(self) -> None:
        if not self._sync_pending:
            self._sync_pending = True
            QTimer.singleShot(0, self._sync)

    def _sync(self) -> None:
        self._sync_pending = False
        if not shiboken6.isValid(self):
            return
        self.queue.sync(self.cards.cards())
        self.queue.set_selected(self.cards.focal_id())

    def sync_now(self) -> None:
        """Rebuild the queue from the cards at once (tests and screenshots)."""
        self._sync()

    def select(self, action_id: str) -> None:
        """Make ``action_id``'s card the focal one and bring it to the top of the column (a queue tile)."""
        card = self.cards.card(action_id)
        if card is None:
            return
        self.cards.set_focal(action_id)
        self.cards.scroll_to(card)
        self.queue.set_selected(action_id)

    def open_first(self) -> None:
        """The screen opens on the first undecided card (HOLOGRAM_SPEC 16.2), keeping a focal one that waits."""
        cards = [card for card in self.cards.cards() if card.actionable]
        focal = self.cards.card(self.cards.focal_id())
        if focal is not None and focal.status() in WAITING:
            self.select(focal.action_id)
            return
        first = next((card for card in cards if card.status() in WAITING), None)
        if first is not None:
            self.select(first.action_id)
        else:
            self.queue.set_selected("")

    # ---- geometry ----------------------------------------------------------------------------

    def queue_shown(self) -> bool:
        return self.width() >= QUEUE_MIN_STAGE

    def _place(self) -> None:
        width, height = self.width(), self.height()
        top = 24 if height < 640 else 56
        bottom = 16
        pad = 8
        show_queue = width >= QUEUE_MIN_STAGE
        if show_queue:
            card_w = max(CARD_MIN, min(CARD_MAX, width - 2 * pad - QUEUE_GAP - QUEUE_WIDTH))
            centred = (width - card_w) / 2
            x = max(pad, min(centred, width - pad - QUEUE_GAP - QUEUE_WIDTH - card_w))
        else:
            card_w = min(CARD_MAX + 40, width - 2 * pad)
            x = (width - card_w) / 2
        self.cards.setGeometry(round(x), top, round(card_w), max(0, height - top - bottom))
        self.queue.setVisible(show_queue)
        if show_queue:
            self.queue.setGeometry(round(x + card_w), top, QUEUE_GAP + QUEUE_WIDTH, max(0, height - top - bottom))

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._place()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._place()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(CARD_MIN, 200)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            painter.setPen(QPen(holo.LINE_STRONG, 1.0))
            leg = STAGE_BRACKET
            for x, y, dx, dy in ((rect.left(), rect.top(), 1, 1), (rect.right(), rect.top(), -1, 1),
                                 (rect.left(), rect.bottom(), 1, -1), (rect.right(), rect.bottom(), -1, -1)):
                painter.drawLine(QPointF(x, y), QPointF(x + dx * leg, y))
                painter.drawLine(QPointF(x, y), QPointF(x, y + dy * leg))
        finally:
            painter.end()
