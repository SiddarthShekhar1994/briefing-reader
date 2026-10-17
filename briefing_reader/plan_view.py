"""The day plan on Home in the hologram look (HOLOGRAM_SPEC 5, 6, 8, 13, 16.4 moment 2; round 2, part 3a).

Public API
==========

``PlanBlock`` / ``PlanEvent`` / ``PlanInfo``
    What the plan shows: each proposed block (its card's ``action_id``, naive wall ``start`` / ``end``, the
    ``time_text`` "10:30-12:00 PM", the ``title`` and the ``purpose`` "For: LING essay draft"), the day's
    existing events (quiet), the ``due`` items of that day (("DUE TONIGHT", "LING essay draft")), ``now``
    (a naive wall time, None when the plan is for another day), the day ``label`` ("TODAY - MON 12 OCT"),
    the ``caption`` (the sentence Jarvis says: counts and times only) and the ``steps_link`` ("live:<task>",
    "" without LIVE). Never a technical readout: runs, seconds and runs left live in LIVE.

``PlanStage``
    Home's plan moment: the DUE strip and the day label over a vertical day timeline (8 AM - 8 PM, wider when
    a block or an event falls outside, 52 px an hour, a ``holo`` now-line with its dot), existing events as
    quiet tiles, each proposed block as an amber dashed tile with its own **Add block** (never an "add all":
    one click per block); on the right the compact orb (``orb``), the caption (caption-sm, at most 10 words a
    page) and See every step. ``set_plan(info)``; ``sync_cards(lookup)`` reads each block's approval card
    (``lookup(action_id) -> hud.ActionCard | None``) and shows its state: Add block (off, with the card's
    reason, while the card's own button is off), the countdown strip "Adding the block in N s" with Undo and
    the 2 px drain bar, then ADDED and the block as a solid cyan event, or UNDONE / DENIED. A block whose card
    is gone is not shown. ``addBlock(action_id)`` / ``undoBlock(action_id)`` / ``openLink(url)`` /
    ``linkClicked(link_id)`` / ``closeRequested()`` (the close button or Esc). Nothing here adds anything: Add
    block goes through the block's approval card (its guards, the undo countdown and Undo, as on Approvals).

``BlockTile``
    One proposed block (``add_button``, ``undo_button``, ``open_button``; ``mode()`` is "add", "counting",
    "working", "added", "denied" or "failed").
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import shiboken6
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout,
                               QWidget)

from . import holo, hud
from . import orb as orb_mod

HOUR_PX = 52
FIRST_HOUR = 8
LAST_HOUR = 20
GUTTER = 74                  # the hour labels (52) and the tick before the tiles
PAD_Y = 8
BLOCK_MIN = 52               # a block's tile is at least this tall (its two lines and Add block)
EVENT_MIN = 22
SIDE_WIDTH = 280
STRIP_MAX = 244
CAPTION_WORDS = 10
PAGE_MS = 4000
ADD_TEXT = "Add block"
UNDO_TEXT = "Undo"
OPEN_TEXT = "Open"
SEE_STEPS_TEXT = "See every step"
CLOSE_NAME = "Close the plan"
COUNT_PHRASE = "Adding the block in {n} s"
DOT = "\u00b7"
_ADD = "add"
_COUNTING = "counting"
_WORKING = "working"
_ADDED = "added"
_DENIED = "denied"
_FAILED = "failed"
MODES = (_ADD, _COUNTING, _WORKING, _ADDED, _DENIED, _FAILED)


@dataclass(frozen=True)
class PlanBlock:
    action_id: str
    start: datetime = field(repr=False)
    end: datetime = field(repr=False)
    time_text: str = field(repr=False)
    title: str = field(repr=False)
    purpose: str = field(default="", repr=False)


@dataclass(frozen=True)
class PlanEvent:
    start: datetime = field(repr=False)
    end: datetime = field(repr=False)
    time_text: str = field(repr=False)
    title: str = field(repr=False)

    def __repr__(self) -> str:
        return "PlanEvent()"


@dataclass(frozen=True)
class PlanInfo:
    label: str
    blocks: tuple[PlanBlock, ...]
    events: tuple[PlanEvent, ...] = ()
    due: tuple[tuple[str, str], ...] = ()
    now: datetime | None = None
    caption: str = ""
    steps_link: str = ""

    def __repr__(self) -> str:
        return f"PlanInfo({len(self.blocks)} block(s), {len(self.events)} event(s), {len(self.due)} due)"


def _minutes(moment: datetime) -> float:
    return moment.hour * 60 + moment.minute + moment.second / 60


def hour_range(info: PlanInfo) -> tuple[int, int]:
    """8 AM - 8 PM, widened to every block and event of the plan."""
    first, last = FIRST_HOUR, LAST_HOUR
    for item in (*info.blocks, *info.events):
        first = min(first, item.start.hour)
        end = _minutes(item.end) if item.end.date() == item.start.date() else 24 * 60
        last = max(last, min(24, math.ceil(end / 60)))
    return first, last


def hour_label(hour: int, hour24: bool = False) -> str:
    if hour24:
        return f"{hour:02d}:00"
    return f"{hour % 12 or 12} {'AM' if hour < 12 or hour == 24 else 'PM'}"


# --------------------------------------------------------------------------
# One proposed block
# --------------------------------------------------------------------------

class BlockTile(QWidget):
    """One proposed block on the timeline; see the module docstring."""

    addClicked = Signal(str)
    undoClicked = Signal(str)
    openClicked = Signal(str)

    def __init__(self, block: PlanBlock, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.block = block
        self._mode = _ADD
        self._result = ""
        self._link = ""
        self._deadline: float | None = None
        self._seconds = 0.0
        self.add_button = holo.HoloButton(ADD_TEXT, holo.AMBER_COMMIT, size=holo.SIZE_SM, icon="calendar",
                                          parent=self)
        self.add_button.setAccessibleName(f"{ADD_TEXT}: {block.title}, {block.time_text}")
        self.undo_button = holo.HoloButton(UNDO_TEXT, holo.LINK, size=holo.SIZE_SM, parent=self)
        self.undo_button.setAccessibleName(f"{UNDO_TEXT}: {block.title}")
        self.open_button = holo.HoloButton(OPEN_TEXT, holo.LINK_CYAN, size=holo.SIZE_SM, parent=self)
        self.open_button.setAccessibleName("Open the event in Google Calendar")
        self.add_button.clicked.connect(lambda: self.addClicked.emit(self.block.action_id))
        self.undo_button.clicked.connect(lambda: self.undoClicked.emit(self.block.action_id))
        self.open_button.clicked.connect(lambda: self.openClicked.emit(self._link))
        self._tick = QTimer(self)
        self._tick.setInterval(66)               # the drain bar at 15 fps, the number once a second
        self._tick.timeout.connect(self._on_tick)
        self.setAccessibleName(f"Proposed block: {block.title}, {block.time_text}")
        self._sync_children()

    # ---- state --------------------------------------------------------------------------------

    def mode(self) -> str:
        return self._mode

    def result(self) -> str:
        return self._result

    def set_from_card(self, card: Any) -> None:
        """The block's state from its approval card (hud.ActionCard)."""
        status = card.status()
        deadline, seconds = card.countdown()
        if status == hud.CARD_COUNTDOWN:
            mode = _COUNTING
        elif status in (hud.CARD_WORKING, hud.CARD_SIGNIN):
            mode = _WORKING
        elif status in (hud.CARD_ADDED, hud.CARD_EXISTS, hud.CARD_DONE, hud.CARD_SENT):
            mode = _ADDED
        elif status == hud.CARD_DENIED:
            mode = _DENIED
        elif status in (hud.CARD_FAILED, hud.CARD_UNKNOWN):
            mode = _FAILED
        else:
            mode = _ADD
        button = card.approve_button
        self.set_state(mode, approve_text=card.approve_text() or ADD_TEXT,
                       enabled=button.isEnabled() and not card.is_locked(), tip=button.toolTip(),
                       result=card.result_text(), link=card.link(), deadline=deadline, seconds=seconds)

    def set_state(self, mode: str, *, approve_text: str = ADD_TEXT, enabled: bool = True, tip: str = "",
                  result: str = "", link: str = "", deadline: float | None = None, seconds: float = 0.0) -> None:
        focus = QApplication.focusWidget()
        add_had_focus = focus is self.add_button
        undo_had_focus = focus is self.undo_button
        self._mode = mode if mode in MODES else _ADD
        self._result = result
        self._link = link
        self._deadline, self._seconds = (deadline, seconds) if self._mode == _COUNTING else (None, 0.0)
        if self.add_button.text() != approve_text:
            self.add_button.setText(approve_text)
            self.add_button.setAccessibleName(f"{approve_text}: {self.block.title}, {self.block.time_text}")
        self.add_button.setEnabled(enabled)
        self.add_button.setToolTip(tip if not enabled else "")
        self._sync_children()
        # The keyboard follows the decision (spec 8): Add block pressed with a key hands the focus to Undo
        # while the countdown runs, and Undo back to Add block.
        if add_had_focus and self._mode == _COUNTING:
            self.undo_button.setFocus(Qt.FocusReason.OtherFocusReason)
        elif undo_had_focus and self._mode in (_ADD, _FAILED) and self.add_button.isEnabled():
            self.add_button.setFocus(Qt.FocusReason.OtherFocusReason)
        state = {_ADD: "waits for your OK", _COUNTING: "adding", _WORKING: "adding", _ADDED: "added",
                 _DENIED: "not added", _FAILED: "failed"}[self._mode]
        self.setAccessibleDescription(f"{state}{': ' + result.lower() if result and self._mode != _ADD else ''}")
        self.update()

    def seconds_left(self) -> int:
        if self._deadline is None:
            return 0
        return max(0, math.ceil(self._deadline - time.monotonic() - 1e-6))

    def phrase(self) -> str:
        return COUNT_PHRASE.format(n=self.seconds_left())

    def drain(self) -> float:
        if self._deadline is None or self._seconds <= 0:
            return 0.0
        return max(0.0, min(1.0, (self._deadline - time.monotonic()) / self._seconds))

    def _sync_children(self) -> None:
        mode = self._mode
        self.add_button.setVisible(mode in (_ADD, _FAILED))
        self.undo_button.setVisible(mode == _COUNTING)
        self.open_button.setVisible(mode == _ADDED and bool(self._link))
        self._sync_timer()
        self._place()

    def _sync_timer(self) -> None:
        running = self._mode == _COUNTING and self._deadline is not None and self.isVisible()
        if running and not self._tick.isActive():
            self._tick.start()
        elif not running:
            self._tick.stop()

    def _on_tick(self) -> None:
        if shiboken6.isValid(self):
            self.update(self._strip_rect().toAlignedRect().adjusted(-2, -2, 2, 2))

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._tick.stop()
        super().hideEvent(event)

    # ---- geometry -----------------------------------------------------------------------------

    def _right_width(self) -> float:
        return min(STRIP_MAX, self.width() * 0.58)

    def _strip_rect(self) -> QRectF:
        width = self._right_width()
        height = 40.0
        return QRectF(self.width() - 6 - width, (self.height() - height) / 2, width, height)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._place()

    def _place(self) -> None:
        strip = self._strip_rect()
        for button in (self.add_button, self.open_button):
            hint = button.sizeHint()
            button.setGeometry(round(strip.right() - hint.width() + holo.HoloButton.MARGIN),
                               round(strip.center().y() - hint.height() / 2), hint.width(), hint.height())
        hint = self.undo_button.sizeHint()
        self.undo_button.setGeometry(round(strip.right() - 5 - hint.width() + holo.HoloButton.MARGIN),
                                     round(strip.center().y() - hint.height() / 2), hint.width(), hint.height())

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(420, BLOCK_MIN)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(260, BLOCK_MIN)

    # ---- paint --------------------------------------------------------------------------------

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            mode = self._mode
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            path = QPainterPath()
            path.addRoundedRect(rect, 8, 8)
            if mode == _ADDED:
                painter.fillPath(path, QColor(holo.TILE_FOCUS))
                painter.fillPath(path, holo.alpha(holo.HOLO, 0.08))
                painter.setPen(QPen(holo.alpha(holo.HOLO, 0.55), 1.0))
                painter.drawPath(path)
                painter.save()
                painter.setClipPath(path)
                painter.fillRect(QRectF(0, 0, 3, self.height()), QColor(holo.HOLO))
                painter.restore()
            elif mode == _DENIED:
                painter.setOpacity(0.55)
                painter.fillPath(path, QColor(holo.TILE))
                pen = QPen(holo.alpha(holo.STEEL, 0.6), 1.0, Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.drawPath(path)
                painter.setOpacity(1.0)
            else:
                painter.fillPath(path, QColor(holo.TILE_AMBER))
                painter.save()
                painter.setClipPath(path)
                painter.fillRect(rect, holo.scan_brush(holo.SCAN_AMBER))
                painter.restore()
                style = Qt.PenStyle.SolidLine if mode in (_COUNTING, _WORKING) else Qt.PenStyle.DashLine
                painter.setPen(QPen(holo.alpha(holo.AMBER, 0.72 if mode == _COUNTING else 0.6), 1.0, style))
                painter.drawPath(path)
                holo.paint_brackets(painter, rect, holo.AMBER, leg=10, corners=holo.BRACKETS_DIAGONAL,
                                    radius=8)
            self._paint_texts(painter)
            if mode == _COUNTING:
                self._paint_strip(painter)
            elif mode in (_ADDED, _DENIED, _WORKING):
                self._paint_stamp(painter)
            elif mode == _FAILED:
                self._paint_failed(painter)
        finally:
            painter.end()

    def _text_right(self) -> float:
        return self.width() - 6 - self._right_width() - 12

    def _paint_texts(self, painter: QPainter) -> None:
        mode = self._mode
        block = self.block
        left = 16.0
        room = max(10.0, self._text_right() - left)
        first_h, title_h = 16.0, 20.0
        top = max(4.0, (self.height() - (first_h + 3 + title_h)) / 2)
        if mode == _ADDED:
            dot, time_color, for_color, title_color = holo.HOLO, holo.HOLO, holo.INK_3, holo.INK_1
        elif mode == _DENIED:
            dot, time_color, for_color, title_color = holo.STEEL, holo.INK_4, holo.INK_4, holo.INK_3
        else:
            dot, time_color, for_color, title_color = holo.AMBER, holo.AMBER, holo.INK_3, holo.AMBER_TX
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(dot))
        painter.drawEllipse(QPointF(left + 3.5, top + first_h / 2), 3.5, 3.5)
        x = left + 15
        readout = holo.font("readout")
        painter.setFont(readout)
        painter.setPen(QColor(time_color))
        time_text = QFontMetricsF(readout).elidedText(block.time_text, Qt.TextElideMode.ElideRight,
                                                       max(1.0, left + room - x))
        time_w = QFontMetricsF(readout).horizontalAdvance(time_text)
        align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        painter.drawText(QRectF(x, top, time_w + 2, first_h), align, time_text)
        x += time_w + 6
        if block.purpose and x < left + room - 24:
            body = holo.font("body_sm")
            painter.setPen(QColor(holo.INK_4))
            painter.setFont(body)
            painter.drawText(QRectF(x, top, 10, first_h), align, DOT)
            x += 12
            painter.setPen(QColor(for_color))
            painter.drawText(QRectF(x, top, left + room - x, first_h), align,
                             QFontMetricsF(body).elidedText(block.purpose, Qt.TextElideMode.ElideRight,
                                                            max(1.0, left + room - x)))
        title = holo.font("tile_title")
        title.setPixelSize(16)
        painter.setFont(title)
        painter.setPen(QColor(title_color))
        painter.drawText(QRectF(left, top + first_h + 3, room, title_h), align,
                         QFontMetricsF(title).elidedText(block.title, Qt.TextElideMode.ElideRight, room))

    def _paint_strip(self, painter: QPainter) -> None:
        strip = self._strip_rect()
        path = QPainterPath()
        path.addRoundedRect(strip.adjusted(0.5, 0.5, -0.5, -0.5), holo.RADIUS_COUNTDOWN, holo.RADIUS_COUNTDOWN)
        painter.fillPath(path, holo.alpha(holo.AMBER, 0.10))
        painter.setPen(QPen(holo.alpha(holo.AMBER, 0.35), 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        undo = self.undo_button.geometry()
        text_rect = QRectF(strip.left() + 12, strip.top(), max(1.0, undo.left() - strip.left() - 12), strip.height())
        font = holo.font("button")
        font.setWeight(font.Weight.Normal)
        painter.setFont(font)
        painter.setPen(QColor("#FFE9C6"))
        painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         QFontMetricsF(font).elidedText(self.phrase(), Qt.TextElideMode.ElideRight, text_rect.width()))
        painter.save()
        painter.setClipPath(path)
        painter.fillRect(QRectF(strip.left(), strip.bottom() - 2, strip.width() * self.drain(), 2), QColor(holo.AMBER))
        painter.restore()

    def _paint_stamp(self, painter: QPainter) -> None:
        strip = self._strip_rect()
        mode = self._mode
        kind = {_ADDED: holo.STAMP_OK, _DENIED: holo.STAMP_STEEL}.get(mode, holo.STAMP_HOLO)
        right = self.open_button.geometry().left() if self.open_button.isVisible() else strip.right()
        text = self._result or {_ADDED: "ADDED", _DENIED: "UNDONE " + DOT + " NOTHING WAS ADDED",
                                _WORKING: "ADDING..."}[mode]
        readout = holo.font("readout")
        metrics = QFontMetricsF(readout)
        room = max(1.0, right - strip.left() - 30)
        shown = metrics.elidedText(text, Qt.TextElideMode.ElideRight, room)
        width = metrics.horizontalAdvance(shown)
        x = max(strip.left(), right - 8 - width - 28)
        holo.paint_stamp(painter, QPointF(x + 10, strip.center().y()), kind)
        painter.setFont(readout)
        painter.setPen(QColor({_ADDED: holo.OK, _DENIED: holo.INK_3}.get(mode, holo.HOLO)))
        painter.drawText(QRectF(x + 28, strip.top(), width + 2, strip.height()),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), shown)

    def _paint_failed(self, painter: QPainter) -> None:
        strip = self._strip_rect()
        right = self.add_button.geometry().left()
        readout = holo.font("readout")
        painter.setFont(readout)
        painter.setPen(QColor(holo.ALERT_TX))
        text = QFontMetricsF(readout).elidedText("FAILED", Qt.TextElideMode.ElideRight, max(1.0, right - strip.left()))
        painter.drawText(QRectF(strip.left(), strip.top(), max(1.0, right - strip.left() - 4), strip.height()),
                         int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), text)


# --------------------------------------------------------------------------
# The day timeline
# --------------------------------------------------------------------------

class Timeline(QWidget):
    """The day's hours, the existing events (quiet), the now-line and the proposed blocks (BlockTile)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._info: PlanInfo | None = None
        self._hours = (FIRST_HOUR, LAST_HOUR)
        self._tiles: list[BlockTile] = []
        self.hour24 = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Today from 8 AM to 8 PM")

    def set_plan(self, info: PlanInfo | None, hour24: bool = False) -> list[BlockTile]:
        for tile in self._tiles:
            tile.hide()
            tile.deleteLater()
        self._tiles = []
        self._info = info
        self.hour24 = hour24
        self._hours = hour_range(info) if info is not None else (FIRST_HOUR, LAST_HOUR)
        first, last = self._hours
        self.setAccessibleName(f"From {hour_label(first)} to {hour_label(last)}")
        if info is not None:
            for block in sorted(info.blocks, key=lambda item: item.start):
                tile = BlockTile(block, self)
                tile.show()
                self._tiles.append(tile)
        self.setFixedHeight(self.height_needed())
        self._place_tiles()
        self.update()
        return list(self._tiles)

    def tiles(self) -> list[BlockTile]:
        return list(self._tiles)

    def height_needed(self) -> int:
        first, last = self._hours
        return (last - first) * HOUR_PX + 2 * PAD_Y

    def y_of(self, moment: datetime) -> float:
        first, _last = self._hours
        return PAD_Y + (_minutes(moment) - first * 60) * HOUR_PX / 60

    def _span(self, start: datetime, end: datetime, minimum: float) -> tuple[float, float]:
        top = self.y_of(start)
        bottom = self.y_of(end) if end.date() == start.date() else self.height_needed() - PAD_Y
        return top, max(minimum, bottom - top)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._place_tiles()

    def _place_tiles(self) -> None:
        width = max(0, self.width() - GUTTER)
        for tile in self._tiles:
            top, height = self._span(tile.block.start, tile.block.end, BLOCK_MIN)
            top = min(top, self.height_needed() - height)
            tile.setGeometry(GUTTER, round(top), width, round(height))
            tile.raise_()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            first, last = self._hours
            label_font = holo.font("label")
            label_font.setLetterSpacing(label_font.SpacingType.AbsoluteSpacing, 0.9)
            painter.setFont(label_font)
            for hour in range(first, last + 1):
                y = PAD_Y + (hour - first) * HOUR_PX
                painter.setPen(QColor(holo.INK_4))
                painter.drawText(QRectF(0, y - 7, 52, 14),
                                 int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                                 hour_label(hour, self.hour24))
                painter.fillRect(QRectF(58, y, 7, 1), holo.LINE_STRONG)
                painter.fillRect(QRectF(65, y, self.width() - 65, 1), holo.LINE_FAINT)
            painter.fillRect(QRectF(64, PAD_Y, 1, self.height() - 2 * PAD_Y), holo.LINE)
            info = self._info
            now = info.now if info is not None else None
            now_y = self.y_of(now) if now is not None else None
            if now_y is not None and PAD_Y < now_y:
                painter.fillRect(QRectF(65, PAD_Y, self.width() - 65, min(now_y, self.height() - PAD_Y) - PAD_Y),
                                 holo.alpha(holo.VOID, 0.5))
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            if info is not None:
                self._paint_events(painter, info.events)
            if now_y is not None and PAD_Y <= now_y <= self.height() - PAD_Y:
                painter.fillRect(QRectF(65, now_y - 0.5, self.width() - 65, 1.5), QColor(holo.HOLO))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(holo.HOLO))
                painter.drawEllipse(QPointF(65, now_y), 4, 4)
        finally:
            painter.end()

    def _paint_events(self, painter: QPainter, events: Sequence[PlanEvent]) -> None:
        readout = holo.font("label")
        title_font = holo.font("button_sm")
        for event in events:
            top, height = self._span(event.start, event.end, EVENT_MIN)
            rect = QRectF(GUTTER + 0.5, top + 0.5, self.width() - GUTTER - 1, height - 1)
            path = QPainterPath()
            path.addRoundedRect(rect, 6, 6)
            painter.fillPath(path, QColor(holo.TILE))
            painter.setPen(QPen(holo.LINE, 1.0))
            painter.drawPath(path)
            line = QRectF(rect.left() + 12, rect.top(), rect.width() - 24, min(22.0, rect.height()))
            painter.setFont(readout)
            painter.setPen(QColor(holo.INK_4))
            time_w = QFontMetricsF(readout).horizontalAdvance(event.time_text) + 2
            align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            painter.drawText(QRectF(line.left(), line.top(), time_w, line.height()), align, event.time_text)
            painter.setFont(title_font)
            painter.setPen(QColor(holo.INK_3))
            room = max(1.0, line.width() - time_w - 10)
            painter.drawText(QRectF(line.left() + time_w + 10, line.top(), room, line.height()), align,
                             QFontMetricsF(title_font).elidedText(event.title, Qt.TextElideMode.ElideRight, room))


class _DueStrip(QWidget):
    """The DUE strip over the timeline: an amber dot, "DUE TONIGHT" and the item (elided; "+N more")."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._due: tuple[tuple[str, str], ...] = ()
        self.setFixedHeight(20)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_due(self, due: Sequence[tuple[str, str]]) -> None:
        self._due = tuple(due)
        names = "; ".join(f"{word} {title}" for word, title in self._due)
        self.setAccessibleName(names)
        self.setVisible(bool(self._due))
        self.update()

    def due(self) -> tuple[tuple[str, str], ...]:
        return self._due

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(240, 20)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(80, 20)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        if not self._due:
            return
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            word, title = self._due[0]
            more = f"  +{len(self._due) - 1} more" if len(self._due) > 1 else ""
            label = holo.font("label")
            body = holo.font("button_sm")
            label_w = QFontMetricsF(label).horizontalAdvance(holo.role_text("label", word)) + 2
            more_w = QFontMetricsF(body).horizontalAdvance(more) if more else 0.0
            title_w = QFontMetricsF(body).horizontalAdvance(title) + 2
            total = 15 + label_w + 8 + title_w + more_w
            x = max(0.0, self.width() - total)
            cy = self.height() / 2
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(holo.AMBER))
            painter.drawEllipse(QPointF(x + 3.5, cy), 3.5, 3.5)
            x += 15
            align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            painter.setFont(label)
            painter.setPen(QColor(holo.AMBER))
            painter.drawText(QRectF(x, 0, label_w, self.height()), align, holo.role_text("label", word))
            x += label_w + 8
            painter.setFont(body)
            painter.setPen(QColor(holo.INK_2))
            room = max(1.0, self.width() - x - more_w)
            painter.drawText(QRectF(x, 0, room, self.height()), align,
                             QFontMetricsF(body).elidedText(title, Qt.TextElideMode.ElideRight, room))
            if more:
                painter.setPen(QColor(holo.INK_4))
                painter.drawText(QRectF(self.width() - more_w, 0, more_w, self.height()), align, more)
        finally:
            painter.end()


# --------------------------------------------------------------------------
# The plan moment
# --------------------------------------------------------------------------

class PlanStage(QWidget):
    """Home's plan moment; see the module docstring."""

    addBlock = Signal(str)
    undoBlock = Signal(str)
    openLink = Signal(str)
    linkClicked = Signal(str)
    closeRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("Plan my day")
        self._info: PlanInfo | None = None
        self._pages: list[str] = []
        self._page = 0
        self.day_label = QLabel()
        self.day_label.setFont(holo.font("label"))
        hud.set_label_color(self.day_label, holo.INK_KICKER)
        self.due_strip = _DueStrip()
        self.close_button = holo.HoloButton(CLOSE_NAME, holo.ICON, icon="close", diameter=34)
        self.close_button.setToolTip(CLOSE_NAME)
        self.close_button.clicked.connect(self.closeRequested)
        head = QHBoxLayout()
        head.setContentsMargins(GUTTER, 0, 0, 0)
        head.setSpacing(16)
        head.addWidget(self.day_label, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.due_strip, 1, Qt.AlignmentFlag.AlignVCenter)
        self.timeline = Timeline()
        self.scroll = QScrollArea()
        hud._transparent_scroll(self.scroll, self.timeline)
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(6)
        left.addLayout(head)
        left.addWidget(self.scroll, 1)
        self.orb = orb_mod.OrbWidget(orb_mod.COMPACT)
        self.caption = QLabel()
        self.caption.setFont(holo.font("caption_sm"))
        self.caption.setWordWrap(True)
        self.caption.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.caption.setTextFormat(Qt.TextFormat.PlainText)
        self.caption.setFixedHeight(2 * holo.line_height("caption_sm") + 4)
        hud.set_label_color(self.caption, holo.INK_1)
        self.steps_button = holo.HoloButton(SEE_STEPS_TEXT, holo.LINK_CYAN, size=holo.SIZE_SM)
        self.steps_button.clicked.connect(self._on_steps)
        side = QVBoxLayout()
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(10)
        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.addStretch(1)
        top_row.addWidget(self.close_button)
        side.addLayout(top_row)
        side.addStretch(1)
        side.addWidget(self.orb, 0, Qt.AlignmentFlag.AlignHCenter)
        side.addWidget(self.caption)
        side.addWidget(self.steps_button, 0, Qt.AlignmentFlag.AlignHCenter)
        side.addStretch(2)
        self._side = QWidget()
        self._side.setLayout(side)
        self._side.setFixedWidth(SIDE_WIDTH)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 8, 0, 8)
        row.setSpacing(24)
        row.addLayout(left, 1)
        row.addWidget(self._side)
        self._page_timer = QTimer(self)
        self._page_timer.setInterval(PAGE_MS)
        self._page_timer.timeout.connect(self._next_page)
        self.hide()

    # ---- what it shows ------------------------------------------------------------------------

    def set_plan(self, info: PlanInfo | None, *, hour24: bool = False) -> None:
        self._info = info
        self.day_label.setText(holo.role_text("label", info.label) if info is not None else "")
        self.due_strip.set_due(info.due if info is not None else ())
        for tile in self.timeline.set_plan(info, hour24):
            tile.addClicked.connect(self.addBlock)
            tile.undoClicked.connect(self.undoBlock)
            tile.openClicked.connect(self.openLink)
        from .home import paginate

        self._pages = paginate(info.caption, CAPTION_WORDS) if info is not None and info.caption else []
        self._page = 0
        self.caption.setText(self._pages[0] if self._pages else "")
        self.caption.setAccessibleName(info.caption if info is not None else "")
        self._sync_pages()
        self.steps_button.setVisible(bool(info is not None and info.steps_link))
        QTimer.singleShot(0, self._scroll_to_first)

    def info(self) -> PlanInfo | None:
        return self._info

    def tiles(self) -> list[BlockTile]:
        return self.timeline.tiles()

    def tile(self, action_id: str) -> BlockTile | None:
        return next((tile for tile in self.timeline.tiles() if tile.block.action_id == action_id), None)

    def sync_cards(self, lookup: Callable[[str], Any]) -> None:
        """Each block's state from its approval card; a block whose card is gone is not shown."""
        for tile in self.timeline.tiles():
            card = lookup(tile.block.action_id)
            if card is None or not shiboken6.isValid(card):
                tile.hide()
                continue
            tile.set_from_card(card)
            tile.show()

    def caption_text(self) -> str:
        return self.caption.text()

    # ---- caption pages ------------------------------------------------------------------------

    def _sync_pages(self) -> None:
        if len(self._pages) > 1 and self._page < len(self._pages) - 1 and self.isVisible():
            if not self._page_timer.isActive():
                self._page_timer.start()
        else:
            self._page_timer.stop()

    def _next_page(self) -> None:
        if self._page < len(self._pages) - 1:
            self._page += 1
            self.caption.setText(self._pages[self._page])
        self._sync_pages()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._sync_pages()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._page_timer.stop()
        super().hideEvent(event)

    def _scroll_to_first(self) -> None:
        """The first block (or now) in view, never by moving the keyboard focus."""
        if not shiboken6.isValid(self) or self._info is None:
            return
        tiles = [tile for tile in self.timeline.tiles() if not tile.isHidden()]
        anchor = min((tile.y() for tile in tiles), default=0)
        bar = self.scroll.verticalScrollBar()
        bar.setValue(max(0, min(bar.maximum(), anchor - HOUR_PX)))

    def _on_steps(self) -> None:
        if self._info is not None and self._info.steps_link:
            self.linkClicked.emit(self._info.steps_link)

    # ---- keys and layout ----------------------------------------------------------------------

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self.closeRequested.emit()
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        narrow = event.size().width() < 700
        self._side.setFixedWidth(200 if narrow else SIDE_WIDTH)
        self.orb.setVisible(event.size().height() >= 300)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(560, 240)
