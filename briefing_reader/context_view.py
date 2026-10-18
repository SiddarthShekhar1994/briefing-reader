"""Home's context tiles (the approved motion prototype, part B; HOLOGRAM_SPEC 11.7 "items fly in as tiles when
Jarvis names them", 5, 13, 16.0).

When Jarvis greets the owner on Home and when the briefing-ready announcement plays, up to four small tiles fly
out of the hero orb in the order he mentions them, each joined to the orb by a thin tether that draws in and then
carries a slow flowing dash: the next meeting today, the oldest thread waiting on the owner (the productivity
bundle), the nearest deadline and - amber, only when a decision waits - "N waiting for your OK". They show only
data the app already loaded (memory only: never logged, never spoken); a source that is empty has no tile. A tile
never acts: a click (or Enter / Space) only asks for its screen (``tileClicked(target)``: the Day view,
Follow-ups or Approvals). They stay while the owner is on Home and idle, and fade out after ``HOLD_S`` or when he
types, asks or leaves Home (``dismiss()``). Nothing is laid over the Ask bar or the caption (they are outside the
stage) or over an offer / error side tile, the Transcript button or the orb: the tiles take the free places
around the orb (two columns, a tile above and below its centre line) and skip what does not fit.

Data
====
``ContextFacts(meeting, thread, deadline, approvals, replies)``
    ``meeting``: (start, title) of the next timed meeting today; ``thread``: (who, subject) of the oldest thread
    waiting on the owner; ``deadline``: (due label, title) of the nearest deadline ("TODAY 5:00 PM" as
    agenda.due_label gives it) and its spoken ``deadline_at`` (a time today, or None); ``approvals``: cards
    waiting for his OK; ``replies``: threads waiting on him. ``repr`` carries counts only.
``context_tiles(facts) -> list[TileInfo]``
    The tiles in the order Jarvis mentions them (meeting, thread, deadline, approvals).
``spoken_context(facts, address="") -> str``
    ONE short sentence with counts and times only - never a name, a title, a subject or an address ("Your next
    meeting is at 2 PM, and two replies are waiting, sir."); "" when there is nothing to say. Waiting
    proposals alone keep the old line ("Two proposals are waiting for your OK.").

Qt
==
``ContextTile``: one tile (a button: kicker in mono caps, the title, one meta line; amber when a decision waits).
``ContextTiles(stage, geometry)``: the tiles and their tether layer on ``stage``; ``geometry()`` returns
    (orb centre, orb radius, the hero widget's rect, rects to keep clear). ``show_tiles(infos)``, ``dismiss()``,
    ``relayout()``, ``tiles()`` (the shown ContextTile widgets), ``infos()``, ``is_showing()``, ``phase()``,
    ``tether_lines()``, ``set_clock(fn)`` (stills); lively: each tile flies out of the orb (850 ms, scale .2 -> 1,
    one flicker), 350 ms apart, its tether drawing in over 700 ms, then the dash flows at 10 fps; calm: the tiles
    rise and fade in (700 ms, 500 ms apart), the tethers draw in and stay still; off (or reduced motion): everything
    at once, still.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import shiboken6
from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPen, QPixmap, QRegion
from PySide6.QtWidgets import QAbstractButton, QWidget

from . import holo
from .persona import count_words, spoken_clock

KIND_MEETING = "meeting"
KIND_THREAD = "thread"
KIND_DEADLINE = "deadline"
KIND_APPROVALS = "approvals"
TARGET_DAY = "day"                 # the Day view (TODAY / DEADLINES)
TARGET_FOLLOWUPS = "followups"
TARGET_APPROVALS = "approvals"
MEETING_KICKER = "NEXT MEETING"
THREAD_KICKER = "WAITING ON YOU"
DEADLINE_KICKER = "NEAREST DEADLINE"
APPROVALS_KICKER = "NEEDS YOUR OK"
MAX_TILES = 4
HOLD_S = 60.0
TILE_MIN_W = 170
TILE_MAX_W = 260
TILE_H = 94
ORB_GAP = 40                       # between the orb's rim and a tile (spec 16.0)
ROW_GAP = 8                        # between a tile and the orb's centre line
FLY_MS = 850
RISE_MS = 700                      # calm: rise and fade in
STAGGER_MS = 350
CALM_STAGGER_MS = 500
START_MS = 300                     # the first tile leaves the orb this long after the line starts
TETHER_DELAY_MS = 150
TETHER_MS = 700
CALM_TETHER_MS = 600
EXIT_MS = 220
FLOW_FRAME_MS = 100                # the flowing dash at 10 fps
FLOW_PX_S = 14.0 / 1.1             # the prototype's flow: 14 px a 1.1 s
_DASH = (2.0, 12.0)
_OUT = holo.bezier(holo.EASE_OUT)
_FLICKER = ((0.0, 0.0), (0.45, 0.9), (0.55, 0.4), (0.65, 1.0), (1.0, 1.0))


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ContextFacts:
    meeting: tuple[datetime, str] | None = field(default=None, repr=False)
    thread: tuple[str, str] | None = field(default=None, repr=False)
    deadline: tuple[str, str] | None = field(default=None, repr=False)
    deadline_at: datetime | None = field(default=None, repr=False)
    approvals: int = 0
    replies: int = 0

    def __repr__(self) -> str:
        return (f"ContextFacts(meeting={self.meeting is not None}, thread={self.thread is not None}, "
                f"deadline={self.deadline is not None}, approvals={self.approvals}, replies={self.replies})")


@dataclass(frozen=True)
class TileInfo:
    kind: str
    kicker: str
    title: str = field(repr=False)
    meta: str = field(repr=False)
    target: str
    amber: bool = False

    def accessible_name(self) -> str:
        where = {TARGET_DAY: "the Day view", TARGET_FOLLOWUPS: "Follow-ups", TARGET_APPROVALS: "Approvals"}
        return f"{self.kicker.capitalize()}: {self.title}, {self.meta}. Opens {where.get(self.target, 'it')}"


def _one_line(text: str, limit: int = 80) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "..."


def _clock(moment: datetime) -> str:
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def _due_meta(label: str) -> str:
    """agenda.due_label's text as a tile line: "TODAY 3:00 PM" -> "Due today 3:00 PM", "3D" -> "Due in 3 days"."""
    text = " ".join(str(label or "").split())
    if not text:
        return "Due soon"
    head, _, rest = text.partition(" ")
    if head.endswith("D") and head[:-1].isdigit():
        return f"Due in {int(head[:-1])} days"
    if head in ("TODAY", "TONIGHT", "TOMORROW"):
        return f"Due {head.lower()}" + (f" {rest}" if rest else "")
    return f"Due {text.capitalize()}" if len(text) > 3 else f"Due {text}"


def context_tiles(facts: ContextFacts) -> list[TileInfo]:
    """See the module docstring."""
    tiles: list[TileInfo] = []
    if facts.meeting is not None and _one_line(facts.meeting[1]):
        start, title = facts.meeting
        tiles.append(TileInfo(KIND_MEETING, MEETING_KICKER, _one_line(title), f"Today at {_clock(start)}",
                              TARGET_DAY))
    if facts.thread is not None and (_one_line(facts.thread[0]) or _one_line(facts.thread[1])):
        who, subject = facts.thread
        name = _one_line(who, 40) or "Someone"
        tiles.append(TileInfo(KIND_THREAD, THREAD_KICKER, _one_line(subject) or "(no subject)",
                              f"From {name}", TARGET_FOLLOWUPS))
    if facts.deadline is not None and _one_line(facts.deadline[1]):
        label, title = facts.deadline
        tiles.append(TileInfo(KIND_DEADLINE, DEADLINE_KICKER, _one_line(title), _due_meta(label), TARGET_DAY))
    if facts.approvals > 0:
        count = int(facts.approvals)
        tiles.append(TileInfo(KIND_APPROVALS, APPROVALS_KICKER, f"{count} waiting for your OK",
                              "Each waits for your click.", TARGET_APPROVALS, amber=True))
    return tiles[:MAX_TILES]


def _count_phrase(count: int, one: str, many: str) -> str:
    return f"{count_words(count)} {one if count == 1 else many}"


def spoken_context(facts: ContextFacts, address: str = "") -> str:
    """See the module docstring."""
    parts: list[str] = []
    if facts.meeting is not None:
        parts.append(f"your next meeting is at {spoken_clock(facts.meeting[0])}")
    if facts.replies > 0:
        parts.append(_count_phrase(facts.replies, "reply is", "replies are") + " waiting")
    if facts.deadline_at is not None and len(parts) < 2:
        parts.append(f"a deadline is due at {spoken_clock(facts.deadline_at)}")
    approvals = ""
    if facts.approvals > 0:
        approvals = _count_phrase(facts.approvals, "proposal is", "proposals are") + " waiting for your OK"
    if not parts and not approvals:
        return ""
    if not parts:
        sentence = approvals[0].upper() + approvals[1:] + "."
        return sentence[:-1] + f", {address}." if address else sentence
    parts = parts[:1 if approvals else 2]
    if approvals:
        parts.append(approvals)
    text = parts[0] if len(parts) == 1 else ", and ".join(parts) if len(parts) == 2 else (
        ", ".join(parts[:-1]) + ", and " + parts[-1])
    text = text[0].upper() + text[1:]
    return f"{text}, {address}." if address else f"{text}."


# --------------------------------------------------------------------------
# Qt: the tile
# --------------------------------------------------------------------------

class ContextTile(QAbstractButton):
    """One context tile: never acts, a click only asks for its screen (``clicked``)."""

    def __init__(self, info: TileInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.info = info
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)      # never takes the focus by itself
        self.setAccessibleName(info.accessible_name())
        self.setToolTip("")
        self._fade = 1.0
        self._lift = 0.0
        self._hover = False
        self.setFixedHeight(TILE_H)

    def set_fade(self, fade: float, lift: float = 0.0) -> None:
        fade = max(0.0, min(1.0, fade))
        if (fade, lift) != (self._fade, self._lift):
            self._fade, self._lift = fade, lift
            self.update()

    def fade(self) -> float:
        return self._fade

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(TILE_MAX_W, TILE_H)

    def enterEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paint_body(self, painter: QPainter, rect: QRectF) -> None:
        """The tile (also into the flight's pixmap)."""
        info = self.info
        style = holo.paint_tile(painter, rect, holo.TILE_NEEDS if info.amber else holo.TILE_DEFAULT)
        if self._hover:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(QColor(holo.AMBER if info.amber else holo.HOLO), 1.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(holo.tile_path(rect.adjusted(0.5, 0.5, -0.5, -0.5), style.radius))
            painter.restore()
        pad_v, pad_h = holo.TILE_PAD
        width = rect.width() - 2 * pad_h
        x, y = rect.left() + pad_h, rect.top() + pad_v
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            for role, text, color, gap in (
                    ("label", holo.role_text("label", info.kicker), style.kicker, 6),
                    ("tile_title", info.title, style.title, 4),
                    ("body_sm", info.meta, holo.AMBER_TX2 if info.amber else holo.INK_3, 0)):
                font = holo.font(role)
                painter.setFont(font)
                painter.setPen(QColor(color))
                line = holo.line_height(role)
                elided = QFontMetricsF(font).elidedText(text, Qt.TextElideMode.ElideRight, width)
                painter.drawText(QRectF(x, y, width, line), int(Qt.AlignmentFlag.AlignLeft
                                                                 | Qt.AlignmentFlag.AlignVCenter), elided)
                y += line + gap
        finally:
            painter.restore()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setOpacity(self._fade)
            rect = QRectF(self.rect())
            if self._lift:
                painter.translate(0.0, -self._lift)
            self.paint_body(painter, rect)
            if self.hasFocus():
                holo.paint_focus_ring(painter, rect.adjusted(2, 2, -2, -2), holo.RADIUS_TILE)
        finally:
            painter.end()

    def pixmap(self) -> QPixmap:
        dpr = max(1.0, self.devicePixelRatioF())
        pixmap = QPixmap(round(self.width() * dpr), round(self.height() * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            self.paint_body(painter, QRectF(0, 0, self.width(), self.height()))
        finally:
            painter.end()
        return pixmap


# --------------------------------------------------------------------------
# Qt: the tiles, their flights and tethers
# --------------------------------------------------------------------------

def _keyframes(points: Sequence[tuple[float, float]], p: float) -> float:
    p = max(0.0, min(1.0, p))
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if p <= x1:
            return y0 + (y1 - y0) * ((p - x0) / (x1 - x0) if x1 > x0 else 1.0)
    return points[-1][1]


@dataclass
class _Entry:
    info: TileInfo
    tile: ContextTile
    start: float                       # monotonic: when its flight begins
    pixmap: QPixmap | None = None
    anchor: QPointF = field(default_factory=QPointF)
    rim: QPointF = field(default_factory=QPointF)


class _Layer(QWidget):
    """The flights, tethers and nodes over the stage (ignores the mouse, never takes the focus)."""

    def __init__(self, owner: ContextTiles, parent: QWidget) -> None:
        super().__init__(parent)
        self._owner = owner
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            self._owner.paint_layer(painter)
        finally:
            painter.end()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self._owner.sync_timer()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._owner.sync_timer()


class ContextTiles:
    """See the module docstring."""

    def __init__(self, stage: QWidget,
                 geometry: Callable[[], tuple[QPointF, float, QRectF, list[QRectF]]],
                 on_click: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._stage = stage
        self._geometry = geometry
        self._on_click = on_click
        self._clock = clock
        self._entries: list[_Entry] = []
        self._shown_at = 0.0
        self._exit_from = 0.0
        self._level = holo.MOTION_LIVELY
        self._timer = QTimer(stage)
        self._timer.timeout.connect(self._tick)
        self._hold = QTimer(stage)
        self._hold.setSingleShot(True)
        self._hold.timeout.connect(self.dismiss)
        self._layer = _Layer(self, stage)
        self._layer.hide()

    # ---- API ------------------------------------------------------------------------

    def set_clock(self, clock: Callable[[], float]) -> None:
        """The monotonic clock the motion follows (stills and tests freeze it)."""
        self._clock = clock
        self._layer.update()

    def infos(self) -> list[TileInfo]:
        return [entry.info for entry in self._entries]

    def tiles(self) -> list[ContextTile]:
        return [entry.tile for entry in self._entries if shiboken6.isValid(entry.tile)]

    def is_showing(self) -> bool:
        return bool(self._entries)

    def layer(self) -> QWidget:
        return self._layer

    def phase(self) -> str:
        """"" (none), "entering", "steady" or "leaving"."""
        if not self._entries:
            return ""
        if self._exit_from:
            return "leaving"
        return "entering" if self._entering(self._clock()) else "steady"

    def show_tiles(self, infos: Sequence[TileInfo], *, hold_s: float = HOLD_S) -> int:
        """Fly ``infos`` out of the orb (replacing any shown); the number placed (those that fit)."""
        self._clear()
        self._level = holo.motion_level()
        now = self._clock()
        stagger = {holo.MOTION_LIVELY: STAGGER_MS, holo.MOTION_CALM: CALM_STAGGER_MS}.get(self._level, 0) / 1000.0
        delay = START_MS / 1000.0 if self._level != holo.MOTION_OFF else 0.0
        for index, info in enumerate(list(infos)[:MAX_TILES]):
            tile = ContextTile(info, self._stage)
            tile.clicked.connect(lambda _checked=False, target=info.target: self._clicked(target))
            tile.hide()
            self._entries.append(_Entry(info, tile, now + delay + index * stagger))
        self._shown_at = now
        self.relayout()
        if self._entries:
            self._hold.start(max(1, int(hold_s * 1000)))
            self._layer.show()
            self._raise()
        self._tick()
        return len(self._entries)

    def dismiss(self) -> None:
        """Fade the tiles out (220 ms, 6 px up; at once without motion)."""
        self._hold.stop()
        if not self._entries or self._exit_from:
            return
        if self._level == holo.MOTION_OFF or holo.reduced_motion() or not self._layer.isVisible():
            self._clear()
            return
        self._exit_from = self._clock()
        self.sync_timer()

    def relayout(self) -> None:
        """Place the tiles in the free places around the orb (those that do not fit are dropped)."""
        if not self._entries:
            return
        center, radius, hero, keep_clear = self._geometry()
        stage = QRectF(self._stage.rect()).adjusted(4, 4, -4, -4)
        room_left = center.x() - radius - ORB_GAP - stage.left()
        room_right = stage.right() - (center.x() + radius + ORB_GAP)
        width = int(max(TILE_MIN_W, min(TILE_MAX_W, math.floor(min(room_left, room_right)))))
        slots = [  # (left column?, above the centre line?) in the order the tiles are named
            (True, True), (False, True), (True, False), (False, False)]
        spots: list[QRectF] = []            # the free places, in the order the tiles are named
        for left, above in slots:
            x = center.x() - radius - ORB_GAP - width if left else center.x() + radius + ORB_GAP
            y = center.y() - ROW_GAP - TILE_H if above else center.y() + ROW_GAP
            rect = QRectF(x, y, width, TILE_H)
            candidates = [rect]
            blocking = [other for other in keep_clear + spots if rect.intersects(other)]
            if blocking:        # past what is in the way (above the line: higher, below: lower), else short of it
                away, toward = QRectF(rect), QRectF(rect)
                if above:
                    away.moveBottom(min(other.top() for other in blocking) - ROW_GAP)
                    toward.moveTop(max(other.bottom() for other in blocking) + 2)
                else:
                    away.moveTop(max(other.bottom() for other in blocking) + ROW_GAP)
                    toward.moveBottom(min(other.top() for other in blocking) - 2)
                candidates += [away, toward]
            for candidate in candidates:
                if stage.contains(candidate) and not any(candidate.intersects(other) for other in keep_clear + spots):
                    spots.append(candidate)
                    break
        wanted = self._keep(len(spots))
        placed: list[_Entry] = []
        for entry, spot in zip([entry for entry in self._entries if entry in wanted], spots):
            entry.tile.setGeometry(spot.toRect())
            left = spot.right() < center.x()
            entry.anchor = QPointF(spot.right() if left else spot.left(), spot.top() + 24.0)
            dx, dy = entry.anchor.x() - center.x(), entry.anchor.y() - center.y()
            distance = math.hypot(dx, dy) or 1.0
            rim = radius + 4.0
            entry.rim = QPointF(center.x() + dx / distance * rim, center.y() + dy / distance * rim)
            entry.pixmap = None
            placed.append(entry)
        for entry in self._entries:
            if entry not in placed and shiboken6.isValid(entry.tile):
                entry.tile.hide()
                entry.tile.deleteLater()
        self._entries = placed
        self._layer.setGeometry(self._stage.rect())
        if not placed:
            self._clear()
            return
        self._raise()
        self._layer.update()

    def tether_lines(self) -> list[tuple[QPointF, QPointF, bool]]:
        """(orb rim point, tile anchor, amber) of each tether."""
        return [(entry.rim, entry.anchor, entry.info.amber) for entry in self._entries]

    # ---- internals ------------------------------------------------------------------

    def _keep(self, slots: int) -> list[_Entry]:
        """When fewer places than tiles: the amber one stays, then the meeting, the thread, the deadline."""
        if len(self._entries) <= slots:
            return list(self._entries)
        order = {KIND_APPROVALS: 0, KIND_MEETING: 1, KIND_THREAD: 2, KIND_DEADLINE: 3}
        ranked = sorted(self._entries, key=lambda entry: order.get(entry.info.kind, 9))
        return ranked[:slots]

    def _clicked(self, target: str) -> None:
        if self._on_click is not None:
            self._on_click(target)

    def _raise(self) -> None:
        self._layer.raise_()
        for entry in self._entries:
            if shiboken6.isValid(entry.tile):
                entry.tile.raise_()

    def _clear(self) -> None:
        self._hold.stop()
        self._timer.stop()
        for entry in self._entries:
            if shiboken6.isValid(entry.tile):
                entry.tile.hide()
                entry.tile.deleteLater()
        self._entries = []
        self._exit_from = 0.0
        if shiboken6.isValid(self._layer):
            self._layer.hide()

    def _durations(self) -> tuple[float, float]:
        """(flight, tether draw) in seconds at the level the tiles started with."""
        if self._level == holo.MOTION_LIVELY:
            return FLY_MS / 1000.0, TETHER_MS / 1000.0
        if self._level == holo.MOTION_CALM:
            return RISE_MS / 1000.0, CALM_TETHER_MS / 1000.0
        return 0.0, 0.0

    def _still(self) -> bool:
        return self._level == holo.MOTION_OFF or holo.reduced_motion()

    def _entering(self, now: float) -> bool:
        if self._still():
            return False
        fly, draw = self._durations()
        return any(now < entry.start + max(fly, TETHER_DELAY_MS / 1000.0 + draw) for entry in self._entries)

    def flight(self, entry_index: int, now: float | None = None) -> float:
        """0..1: how far a tile's entrance is (1 when it landed; tests)."""
        entry = self._entries[entry_index]
        fly, _draw = self._durations()
        now = self._clock() if now is None else now
        if self._still() or fly <= 0.0:
            return 1.0
        return max(0.0, min(1.0, (now - entry.start) / fly))

    def tether_progress(self, entry_index: int, now: float | None = None) -> float:
        entry = self._entries[entry_index]
        _fly, draw = self._durations()
        now = self._clock() if now is None else now
        if self._still() or draw <= 0.0:
            return 1.0
        return max(0.0, min(1.0, (now - entry.start - TETHER_DELAY_MS / 1000.0) / draw))

    def sync_timer(self) -> None:
        """Run the frame timer only while something moves on a visible layer."""
        if not shiboken6.isValid(self._layer):
            return
        visible = self._layer.isVisible() and bool(self._entries)
        now = self._clock()
        if self._exit_from and not visible:
            self._clear()                   # leaving while hidden (the owner left Home): gone at once
            return
        if not visible or self._still():
            self._timer.stop()
            return
        if self._exit_from or self._entering(now):
            interval = 33
        elif self._level == holo.MOTION_LIVELY:
            interval = FLOW_FRAME_MS
        else:
            self._timer.stop()              # calm: the tethers stay still once drawn
            return
        if self._timer.interval() != interval or not self._timer.isActive():
            self._timer.start(interval)

    def _tick(self) -> None:
        if not self._entries or not shiboken6.isValid(self._layer):
            self._timer.stop()
            return
        now = self._clock()
        if self._exit_from:
            p = (now - self._exit_from) / (EXIT_MS / 1000.0)
            if p >= 1.0:
                self._clear()
                return
            for entry in self._entries:
                entry.tile.set_fade(1.0 - p, 6.0 * p)
            self._layer.update()
            self.sync_timer()
            return
        entering = self._entering(now)
        for index, entry in enumerate(self._entries):
            landed = self.flight(index, now) >= 1.0
            if landed and entry.tile.isHidden():
                entry.tile.show()
                entry.tile.raise_()
        if entering:
            self._layer.update()
        elif self._level == holo.MOTION_LIVELY and not self._still():
            self._layer.update(self._flow_region())
        else:
            self._layer.update()
        self.sync_timer()

    def _flow_region(self) -> QRegion:
        """The tethers' small rects (the dash flows only there; under the orb it moves with the orb's frames)."""
        region = QRegion()
        _center, _radius, hero, _keep = self._geometry()
        for entry in self._entries:
            a, b = entry.rim, entry.anchor
            steps = max(1, int(math.hypot(b.x() - a.x(), b.y() - a.y()) // 40) + 1)
            for i in range(steps):
                p0 = QPointF(a.x() + (b.x() - a.x()) * i / steps, a.y() + (b.y() - a.y()) * i / steps)
                p1 = QPointF(a.x() + (b.x() - a.x()) * (i + 1) / steps, a.y() + (b.y() - a.y()) * (i + 1) / steps)
                rect = QRectF(p0, p1).normalized().adjusted(-4, -4, 4, 4)
                region = region.united(QRect(rect.toAlignedRect()))
        return region.subtracted(QRegion(hero.toAlignedRect()))

    def paint_layer(self, painter: QPainter) -> None:
        now = self._clock()
        still = self._still()
        fly, draw = self._durations()
        center, _radius, _hero, _keep = self._geometry()
        fade = 1.0
        if self._exit_from:
            fade = max(0.0, 1.0 - (now - self._exit_from) / (EXIT_MS / 1000.0))
        for index, entry in enumerate(self._entries):
            # the tether: draws in from the orb, then the dash flows outward
            progress = self.tether_progress(index, now)
            if progress > 0.0:
                a, b = entry.rim, entry.anchor
                end = QPointF(a.x() + (b.x() - a.x()) * _OUT(progress), a.y() + (b.y() - a.y()) * _OUT(progress))
                base = holo.alpha(holo.AMBER, 0.7) if entry.info.amber else holo.alpha(holo.HOLO, 0.55)
                painter.setOpacity(fade)
                painter.setPen(QPen(base, 1.0))
                painter.drawLine(a, end)
                if progress >= 1.0 and not still and self._level == holo.MOTION_LIVELY:
                    pen = QPen(QColor(holo.AMBER_HI if entry.info.amber else holo.HOLO_HI), 1.4)
                    pen.setDashPattern([_DASH[0] / 1.4, _DASH[1] / 1.4])
                    pen.setDashOffset(-((now - entry.start) * FLOW_PX_S) / 1.4)
                    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
                    flow_in = (now - entry.start - TETHER_DELAY_MS / 1000.0 - draw) / 0.3
                    painter.setOpacity(fade * max(0.0, min(1.0, flow_in)))
                    painter.setPen(pen)
                    painter.drawLine(a, b)
                if progress >= 0.93:
                    pulse = 1.0
                    if not still and self._level == holo.MOTION_LIVELY:
                        pulse = 0.35 + 0.65 * (0.5 - 0.5 * math.cos(2 * math.pi * ((now - entry.start) / 1.4)))
                    painter.setOpacity(fade * pulse)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor(holo.AMBER if entry.info.amber else holo.HOLO))
                    painter.drawEllipse(b, 3.0, 3.0)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
            # the flight (the real tile shows once it landed)
            p = self.flight(index, now)
            if still or p >= 1.0 or p <= 0.0:
                continue
            if entry.pixmap is None:
                entry.pixmap = entry.tile.pixmap()
            rect = QRectF(entry.tile.geometry())
            eased = _OUT(p)
            if self._level == holo.MOTION_LIVELY:
                scale = 0.2 + 0.8 * eased
                c = rect.center()
                cx = center.x() + (c.x() - center.x()) * eased
                cy = center.y() + (c.y() - center.y()) * eased
                opacity = _keyframes(_FLICKER, p)
            else:
                scale = 0.95 + 0.05 * eased
                cx, cy = rect.center().x(), rect.center().y() + 24.0 * (1.0 - eased)
                opacity = eased
            w, h = rect.width() * scale, rect.height() * scale
            painter.setOpacity(max(0.0, min(1.0, opacity)) * fade)
            painter.drawPixmap(QRectF(cx - w / 2, cy - h / 2, w, h), entry.pixmap, QRectF(entry.pixmap.rect()))
        painter.setOpacity(1.0)
