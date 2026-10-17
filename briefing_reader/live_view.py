"""The Live screen and its pop-out in the hologram look (HOLOGRAM_SPEC 2, 5-8, 10, 13, 15, 16.3; round 2).

Public API
==========

``LiveStage(log)``
    The Live screen. The strip (44 px, a ``line-faint`` hairline under it): ``readout`` (a LiveReadout:
    ``IDLE`` / ``WORKING 0:12`` / ``SENDING IN 7 S``), the focal step's title, ``follow_button`` ("Follow
    live", while the owner picked a step), ``pop_button`` (Pop out, an icon) and ``clear_button`` (Clear,
    ghost sm). Under it the timeline (``log``, a hud.LiveLog: tasks as groups, steps as one line each),
    TIMELINE_WIDTH wide, with ``set_activity(widget)`` under it, beside the focal column (``focal``, a
    FocalView: the focal step's picture in a viewfinder up to 640 x 400 and its exact text behind "Show
    the exact text"). ``set_popped(True)``: the body says the steps are in their own window (``away``,
    ``away_label``, ``show_button``, ``back_button``). Signals: ``popOutClicked``, ``clearClicked``,
    ``showWindowClicked``, ``bringBackClicked``.

``FocalView(log)``
    Follows ``log``: the focal step is the one the owner clicked (until it goes, or ``follow()`` /
    "Follow live"), else during an approved card's hidden hold what it sends or changes (its payload
    step: the confirmed look, mint brackets and ``SENDING``, never a number), else the newest step of the newest
    task. ``frame`` (a PictureFrame; a click or Enter on a picture shows it larger in the log's viewer),
    ``exact`` (an ExactText). ``focal_step()``, ``pinned()``, ``select(step_id)``, ``follow()``.

``PictureFrame``
    The viewfinder (spec 10): a 1 px ``line`` border, radius 6, four 14 px brackets (``holo`` at 70 %;
    mint during a Send's hidden hold), the 28 px header (the kind and verb, ``EMAIL - READ`` / ``CALENDAR -
    MOVE`` / ``WEB SEARCH`` / ``PAGE - EXAMPLE.ORG``; at the right the account, ``SENDING`` or the stamp:
    ``SENT`` in mint), the picture (a page snapshot at 90 %), and the 26 px footer when needed: ``WRITTEN
    BY OTHER PEOPLE`` on every email, search and page picture and on a step whose text is other people's
    (never left out), a page's ``READ IN FULL`` / ``CITED, NOT READ``. A picture on its way, unavailable
    or not kept: a dashed border without brackets and its note. A step without a picture is drawn as
    text (its title, summary, status and fields). Nothing in it is a link.

``ExactText``
    "Show the exact text" / "Hide the exact text" (a cyan text link with a chevron; collapsed at first)
    and, open, the focal step's exact text (mono 12, selectable, read only; its fields, items and text
    blocks) under ``WRITTEN BY OTHER PEOPLE`` when any of it is.

``LiveReadout``
    hud.LiveStatus's states and texts in the hologram readout (the word mono 12; the working time in
    readout-lg; a blinking 8 px square while working or during a Send's hidden hold).

``PopOutHeader``
    The pop-out's 44 px header (spec 16.3): the mini orb, the readout, Clear and "Bring it back" (ghost
    sm), minimize and close; it is the window's drag area.

Rules: LIVE content lives in memory only. Nothing here writes a file, logs a text, a title or a picture,
or loads anything: the pictures are the ones the steps already carry (page pictures only of the public
https pages a research read or cites, made elsewhere), drawn by the existing drawing code. The views keep
only the step views their log shows and let them go when it does (Clear, a dock). Other people's text
always keeps its WRITTEN BY OTHER PEOPLE mark.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

import shiboken6
from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPen, QTextLayout, QTextOption
from PySide6.QtWidgets import (
    QAbstractButton,
    QHBoxLayout,
    QMenu,
    QPlainTextEdit,
    QSizePolicy,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from . import holo, hud
from . import live as live_events
from . import orb as orb_mod
from . import pictures as live_pictures

TIMELINE_WIDTH = 300
TIMELINE_GAP = 24
FOCAL_MAX_W = 640
FOCAL_MAX_H = 400
STRIP_HEIGHT = 44
POP_HEADER_HEIGHT = 44
POP_COLUMN_MAX = 960          # a large pop-out keeps one column of at most this
POP_FOCAL_SHARE = 0.45        # the pop-out's picture takes at most this much of its height
FRAME_HEADER = 28
FRAME_FOOTER = 26
FRAME_BRACKET = 14
FRAME_EMPTY_H = 180
THUMB = (160, 100)            # the timeline's picture thumbnails (spec 10)
EXACT_MAX_H = 240             # the open exact text grows to this, then scrolls

SHOW_EXACT = "Show the exact text"
HIDE_EXACT = "Hide the exact text"
OTHERS_MARK = "WRITTEN BY OTHER PEOPLE"
FOLLOW_TEXT = "Follow live"
FOLLOW_TIP = "Show the newest step again"
POP_OUT_LABEL = "Pop out LIVE into its own window"
CLEAR_TEXT = "Clear"
EMPTY_TEXT = "Nothing running, sir."
AWAY_TEXT = "LIVE is in its own window, sir."
SHOW_WINDOW_TEXT = "Show window"
BRING_BACK_TEXT = "Bring it back"
BRING_BACK_TIP = "Bring LIVE back into the main window"
MINIMIZE_LABEL = "Minimize"
CLOSE_LABEL = "Close: LIVE goes back into the main window"
READ_IN_FULL = "READ IN FULL"
CITED_NOT_READ = "CITED, NOT READ"
SEE_LARGER = "Press Enter to see it larger"
DOT = hud.MIDDLE_DOT

# Steps whose text is other people's words (an email Jarvis read or searched, the web): the mark shows.
OTHERS_KINDS = frozenset({live_events.MAIL_THREAD, live_events.MAIL_SEARCH, live_events.WEB_SEARCH,
                          live_events.WEB_FETCH, live_events.WEB_SOURCE})
OTHERS_PICTURES = frozenset({live_pictures.KIND_EMAIL, live_pictures.KIND_SEARCH, live_pictures.KIND_PAGE})
_CHANGE_WORDS = {live_pictures.CHANGE_ADD: "ADD", live_pictures.CHANGE_TODO: "BLOCK", live_pictures.CHANGE_MOVE: "MOVE",
                 live_pictures.CHANGE_CANCEL: "CANCEL", live_pictures.CHANGE_RSVP: "RSVP",
                 live_pictures.CHANGE_PLAN: "PLAN"}
_TONE_COLORS = {live_pictures.TONE_WILL: holo.AMBER, live_pictures.TONE_DOING: holo.HOLO,
                live_pictures.TONE_DONE: holo.OK, live_pictures.TONE_FAILED: holo.ALERT_TX,
                live_pictures.TONE_UNDONE: holo.INK_4, live_pictures.TONE_CHECK: holo.AMBER}

# Frame states
EMPTY = "empty"
TEXT = "text"           # a step without a picture, drawn as text
READY = "ready"
PENDING = "pending"
UNAVAILABLE = "unavailable"
DROPPED = "dropped"

_fonts: dict[tuple[str, int], QFont] = {}


def _font(role: str, weight: int | None = None) -> QFont:
    """holo.font(role), made once (the frame paints often while a step runs)."""
    key = (role, weight or 0)
    font = _fonts.get(key)
    if font is None:
        font = _fonts[key] = holo.font(role, weight)
    return font


def _advance(font: QFont, text: str) -> float:
    return QFontMetricsF(font).horizontalAdvance(text)


def _wrap(text: str, font: QFont, width: float, max_lines: int = 0) -> list[str]:
    """``text`` word-wrapped to ``width`` px (a long token breaks anywhere), at most ``max_lines`` lines
    (0: all), the last one elided. Nothing of the layout outlives the call."""
    text = " ".join(str(text or "").split())
    if not text:
        return []
    width = max(1.0, float(width))
    layout = QTextLayout(text, font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    spans: list[tuple[int, int]] = []
    layout.beginLayout()
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        spans.append((line.textStart(), line.textLength()))
        if max_lines and len(spans) > max_lines:
            break
    layout.endLayout()
    lines = [text[start:start + length].rstrip() for start, length in spans]
    if max_lines and len(lines) > max_lines:
        begin = spans[max_lines - 1][0]
        tail = QFontMetricsF(font).elidedText(text[begin:], Qt.TextElideMode.ElideRight, width)
        lines = lines[:max_lines - 1] + [tail]
    return lines


def _line_height(font: QFont) -> float:
    return QFontMetricsF(font).lineSpacing()


# --------------------------------------------------------------------------
# What a step shows
# --------------------------------------------------------------------------


def kind_words(kind: str) -> str:
    """"ask.request" -> "ASK - REQUEST" (a step without a picture names its kind in the header)."""
    parts = [part for part in str(kind or "").replace("_", " ").split(".") if part]
    return f" {DOT} ".join(part.upper() for part in parts) or "STEP"


def picture_label(view: live_events.StepView | None, sending: bool = False) -> str:
    """The frame's header label: the picture's kind and verb (spec 10), else the step's kind."""
    if view is None:
        return ""
    picture = view.picture
    kind = getattr(picture, "kind", "")
    if kind == live_pictures.KIND_EMAIL:
        return f"EMAIL {DOT} READ"
    if kind == live_pictures.KIND_OUTGOING:
        return f"EMAIL {DOT} SENDING" if sending else f"EMAIL {DOT} OUTGOING"
    if kind == live_pictures.KIND_DAY:
        change = getattr(getattr(picture, "day", None), "change", "")
        return f"CALENDAR {DOT} {_CHANGE_WORDS.get(change, 'CHANGE')}"
    if kind == live_pictures.KIND_WEEK:
        return f"CALENDAR {DOT} READ"
    if kind == live_pictures.KIND_SEARCH:
        return "WEB SEARCH"
    if kind == live_pictures.KIND_PAGE:
        host = getattr(getattr(picture, "page", None), "host", "")
        return f"PAGE {DOT} {host.upper()}" if host else "PAGE"
    return kind_words(view.kind)


def others_text(view: live_events.StepView | None) -> bool:
    """The step shows other people's words (an email, a search, a page; an untrusted text block)."""
    if view is None:
        return False
    if view.kind in OTHERS_KINDS or getattr(view.picture, "kind", "") in OTHERS_PICTURES:
        return True
    return any(block.untrusted for block in view.blocks)


def exact_text(view: live_events.StepView | None) -> str:
    """The focal step's exact text: its fields ("Label: value"), items and text blocks, as the stream
    keeps them (a block not kept says its size; a cut one how much is left out). Progress notes are in
    the timeline's details, not here."""
    if view is None:
        return ""
    lines: list[str] = [f"{item.label}: {item.value}" for item in view.fields]
    for item in view.items:
        word = item.status_text or (live_events.STATUS_WORDS.get(item.status, "") if item.status else "")
        lines.append(f"- {item.text}" + (f" ({word})" if word else ""))
        if item.note:
            lines.append(f"  {item.note}")
    if view.items_more:
        lines.append(f"+{view.items_more:,} more")
    for block in view.blocks:
        if lines:
            lines.append("")
        lines.append(f"{block.label}:")
        if not block.kept:
            lines.append(hud.LIVE_NOT_KEPT_TEXT.format(chars=f"{block.chars:,}"))
            continue
        lines.append(block.text)
        if block.cut:
            lines.append(hud.LIVE_CUT_TEXT.format(cut=f"{block.cut:,}"))
    return "\n".join(lines)


def _running_hold(task: live_events.TaskView) -> live_events.StepView | None:
    """The approved card's running hold step (a Send's or Cancel's hidden hold), if any."""
    if task.status != live_events.STATUS_RUNNING:
        return None
    return next((step for step in reversed(task.steps)
                 if step.status == live_events.STATUS_RUNNING and step.deadline is not None), None)


# --------------------------------------------------------------------------
# The readout
# --------------------------------------------------------------------------


class LiveReadout(hud.LiveStatus):
    """The Live strip's readout (HOLOGRAM_SPEC 2 / 16.3): ``IDLE`` in ink-4; ``WORKING`` (cyan, mono 12)
    and its time in readout-lg; ``SENDING`` in the confirmed mint during a Send's hidden hold (never a
    number). A blinking 8 px square while working or holding (it repaints only then). hud.LiveStatus's
    API: ``state()``, ``text()``, ``set_state``."""

    _SQUARE = 8
    _GAP = 8
    _HEIGHT = 32

    def __init__(self, parent: QWidget | None = None, *, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(parent, clock=clock)
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setSizePolicy(policy)

    def color(self) -> str:
        return {hud.LIVE_WORKING: holo.HOLO, hud.LIVE_HOLD: holo.OK}.get(self.state(), holo.INK_4)

    def parts(self) -> tuple[str, str]:
        """(word, time): ("WORKING", "0:12"), ("SENDING", ""), ("IDLE", "")."""
        text = self.text()
        if self.state() == hud.LIVE_WORKING:
            word, _space, elapsed = text.partition(" ")
            return word, elapsed
        return text, ""

    def _lead(self) -> float:
        return float(self._SQUARE + self._GAP) if self.state() != hud.LIVE_IDLE else 0.0

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        word, elapsed = self.parts()
        width = _advance(_font("readout"), word)
        if self.state() == hud.LIVE_WORKING:
            width += 12 + max(_advance(_font("readout_lg"), "88:88"), _advance(_font("readout_lg"), elapsed))
        return QSize(math.ceil(self._lead() + width) + 2, self._HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(min(self.sizeHint().width(), 72), self._HEIGHT)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            rect = QRectF(self.rect())
            color = QColor(self.color())
            middle = rect.height() / 2
            x = 0.0
            if self.state() != hud.LIVE_IDLE:
                painter.fillRect(QRectF(0, middle - self._SQUARE / 2, self._SQUARE, self._SQUARE),
                                 holo.alpha(color, self.blink_opacity()))
                x = self._lead()
            word, elapsed = self.parts()
            word_font = _font("readout")
            metrics = QFontMetricsF(word_font)
            room = max(1.0, rect.width() - x)
            shown = metrics.elidedText(word, Qt.TextElideMode.ElideRight, room)
            painter.setFont(word_font)
            painter.setPen(color)
            baseline = middle + (metrics.ascent() - metrics.descent()) / 2
            painter.drawText(QPointF(x, baseline), shown)
            if elapsed and shown == word:
                x += metrics.horizontalAdvance(word) + 12
                time_font = _font("readout_lg")
                time_metrics = QFontMetricsF(time_font)
                if x + time_metrics.horizontalAdvance(elapsed) <= rect.width() + 1:
                    painter.setFont(time_font)
                    painter.setPen(QColor(holo.INK_1))
                    painter.drawText(QPointF(x, middle + (time_metrics.ascent() - time_metrics.descent()) / 2), elapsed)
        finally:
            painter.end()


class _ElidedText(QWidget):
    """One line of painted text, elided to its width (its whole text in the tooltip)."""

    def __init__(self, font: QFont, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._font = font
        self._color = color
        self._text = ""
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(math.ceil(_line_height(font)) + 4)

    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        text = " ".join(str(text or "").split())
        if text != self._text:
            self._text = text
            self.setToolTip(hud.plain_tooltip(text) if text else "")
            self.setAccessibleName(text)
            self.updateGeometry()
            self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(math.ceil(_advance(self._font, self._text)) + 2, self.height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(0, self.height())

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        if not self._text:
            return
        painter = QPainter(self)
        try:
            metrics = QFontMetricsF(self._font)
            painter.setFont(self._font)
            painter.setPen(QColor(self._color))
            text = metrics.elidedText(self._text, Qt.TextElideMode.ElideRight, max(1.0, self.width() - 1.0))
            painter.drawText(QRectF(self.rect()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
        finally:
            painter.end()


# --------------------------------------------------------------------------
# The picture frame
# --------------------------------------------------------------------------


class PictureFrame(QAbstractButton):
    """The focal picture's viewfinder (HOLOGRAM_SPEC 10); see the module docstring. It is as wide as
    its column (at most ``max_size`` wide) and as tall as its picture needs there (at most ``max_size``
    high). A click, Space or Enter on a READY picture: ``clicked`` (it is shown larger). Pixmaps are
    drawn on demand and dropped when it hides or its step goes."""

    def __init__(self, parent: QWidget | None = None, *, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(parent)
        self._clock = clock
        self._max = QSize(FOCAL_MAX_W, FOCAL_MAX_H)
        self._view: live_events.StepView | None = None
        self._held = False
        self._pixmaps: dict[tuple, Any] = {}
        self._texts: dict[int, tuple[list[tuple[QFont, str, str, float, float]], float]] = {}
        self._heights: dict[int, int] = {}
        self._focus_visible = False
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setAccessibleName(EMPTY_TEXT)

    # ---- API ------------------------------------------------------------------------------------

    def step(self) -> live_events.StepView | None:
        return self._view

    def step_id(self) -> int:
        return self._view.id if self._view is not None else 0

    def set_max_size(self, size: QSize) -> None:
        size = QSize(max(160, size.width()), max(120, size.height()))
        if size != self._max:
            self._max = size
            self._heights.clear()
            self._pixmaps.clear()
            self.updateGeometry()
            self.update()

    def max_size(self) -> QSize:
        return QSize(self._max)

    def set_step(self, view: live_events.StepView | None, held: bool = False) -> None:
        """Show ``view`` (None: the empty state); ``held``: its card's Send / Cancel is in its hidden hold
        (the confirmed look and "SENDING", never a number)."""
        old = self._view
        same = old is not None and view is not None and old.id == view.id and old.revision == view.revision
        held = bool(held)
        if same and held == self._held:
            return
        if not same:
            self._pixmaps.clear()
            self._texts.clear()
            self._heights.clear()
        self._view, self._held = view, held
        ready = self.state() == READY
        self.setCursor(Qt.CursorShape.PointingHandCursor if ready else Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus if ready else Qt.FocusPolicy.NoFocus)
        self.setAccessibleName(self.accessible_text())
        self.updateGeometry()
        self.update()

    def tick(self) -> None:
        """The log's timer ticked: nothing in the frame counts (a hold shows no number)."""

    def focus_ring(self) -> bool:
        """It has the keyboard focus with its ring (the viewer gives it back the same way)."""
        return self._focus_visible and self.hasFocus()

    def held(self) -> bool:
        """The confirmed look of a Send's or Cancel's hidden hold ("SENDING")."""
        return self._held and self._view is not None

    def state(self) -> str:
        view = self._view
        if view is None:
            return EMPTY
        picture = view.picture
        if picture is None:
            return TEXT
        return {live_events.PICTURE_READY: READY, live_events.PICTURE_PENDING: PENDING,
                live_events.PICTURE_DROPPED: DROPPED}.get(picture.state, UNAVAILABLE)

    def stamp(self) -> tuple[str, str]:
        view = self._view
        if view is None or view.picture is None:
            return "", ""
        return live_pictures.stamp(view.picture, view.status, view.status_text)

    def header_texts(self) -> tuple[str, str]:
        """(left, right): ("EMAIL - SENDING", "SENDING"), ("CALENDAR - MOVE", "MOVED"), ..."""
        view = self._view
        if view is None:
            return "", ""
        left = picture_label(view, self.held())
        if self.held():
            return left, "SENDING"
        words, _tone = self.stamp()
        if words:
            return left, words
        picture = view.picture
        account = (getattr(getattr(picture, "mail", None), "account", "")
                   or getattr(getattr(picture, "day", None), "account", ""))
        if account:
            return left, account.upper()
        if view.status == live_events.STATUS_RUNNING:
            return left, "RUNNING"
        return left, live_events.status_word(view).upper()

    def footer_texts(self) -> tuple[str, str]:
        """(left, right): WRITTEN BY OTHER PEOPLE (never left out when it is) and a page's read status."""
        view = self._view
        if view is None:
            return "", ""
        left = OTHERS_MARK if others_text(view) else ""
        right = ""
        if getattr(view.picture, "kind", "") == live_pictures.KIND_PAGE or view.kind in (live_events.WEB_FETCH,
                                                                                        live_events.WEB_SOURCE):
            right = CITED_NOT_READ if view.kind == live_events.WEB_SOURCE else (
                READ_IN_FULL if view.kind == live_events.WEB_FETCH and view.status == live_events.STATUS_OK else "")
        return left, right

    def accessible_text(self) -> str:
        view = self._view
        if view is None:
            return EMPTY_TEXT
        left, right = self.header_texts()
        picture = view.picture
        what = getattr(picture, "alt", "") or view.title
        parts = [f"{left}: {what}" if left else what]
        if right:
            parts.append(right)
        footer = self.footer_texts()[0]
        if footer:
            parts.append(footer.capitalize())
        text = ", ".join(parts)
        if self.state() == READY:
            return f"{text}. {SEE_LARGER}"
        if picture is not None:
            return f"{text}. {hud.picture_state_text(picture)}"
        return text

    # ---- geometry -------------------------------------------------------------------------------

    def _frame_width(self, width: int) -> float:
        return float(max(1, min(width, self._max.width())))

    def _canvas_cap(self, footer: bool) -> float:
        return max(40.0, self._max.height() - FRAME_HEADER - (FRAME_FOOTER if footer else 0) - 2)

    def _canvas_height(self, width: float, footer: bool) -> float:
        view = self._view
        state = self.state()
        cap = self._canvas_cap(footer)
        inner = max(1.0, width - 2)
        if state == READY:
            picture = view.picture
            if picture.kind == live_pictures.KIND_PAGE:
                return min(cap, hud._page_size(picture.page, inner, cap)[1] or cap)
            natural = hud.picture_height(picture, int(inner), self.stamp(), cap)
            return float(max(60.0, min(cap, natural)))
        if state == TEXT:
            return float(max(96.0, min(cap, self._text_ops(int(inner))[1])))
        return float(min(cap, 120.0))

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        cached = self._heights.get(width)
        if cached is not None:
            return cached
        if len(self._heights) > 8:
            self._heights.clear()
        if self._view is None:
            height = FRAME_EMPTY_H
        else:
            footer = any(self.footer_texts())
            frame_w = self._frame_width(width)
            canvas = self._canvas_height(frame_w, footer)
            height = math.ceil(FRAME_HEADER + canvas + (FRAME_FOOTER if footer else 0) + 2)
        self._heights[width] = height
        return height

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(self._max.width(), self.heightForWidth(self._max.width()))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(160, 100)

    def frame_rect(self) -> QRectF:
        """Where the frame is drawn (its column's width up to the largest; centred)."""
        width = self._frame_width(self.width())
        return QRectF((self.width() - width) / 2, 0, width, self.height())

    # ---- the text picture of a step without one -------------------------------------------------

    def _text_ops(self, width: int) -> tuple[list[tuple[QFont, str, str, float, float]], float]:
        """(font, colour, text, x, top) lines of a step drawn as text at ``width``, and their height."""
        cached = self._texts.get(width)
        if cached is not None:
            return cached
        if len(self._texts) > 4:
            self._texts.clear()
        view = self._view
        ops: list[tuple[QFont, str, str, float, float]] = []
        pad_x, room = 16.0, max(1.0, width - 32.0)
        y = 14.0
        if view is not None:
            def add(lines: list[str], font: QFont, color: str, gap: float = 0.0) -> None:
                nonlocal y
                if not lines:
                    return
                y += gap
                step = _line_height(font)
                for line in lines:
                    ops.append((font, color, line, pad_x, y))
                    y += step

            add(_wrap(view.title, _font("tile_title"), room, 2), _font("tile_title"), holo.INK_1)
            add(_wrap(view.summary, _font("body_sm"), room, 3), _font("body_sm"), holo.INK_3, 4)
            word = live_events.status_word(view, self._clock())
            elapsed = live_events.elapsed_text(view.elapsed(self._clock()))
            add([f"{word} {DOT} {elapsed}"], _font("readout"), hud.live_mark_color(view.status), 6)
            for field in view.fields[:12]:
                add(_wrap(field.label, _font("label"), room, 1), _font("label"), holo.INK_4, 10)
                value_font = hud.mono_font(12) if field.mono else _font("body_sm")
                add(_wrap(field.value, value_font, room, 2), value_font, holo.INK_2, 2)
            if len(view.fields) > 12:
                add([f"+{len(view.fields) - 12} more"], _font("label"), holo.INK_4, 8)
            items = list(view.items[:5])
            if items:
                y += 8
            for item in items:
                add(_wrap(f"{DOT} {item.text}", _font("body_sm"), room, 1), _font("body_sm"), holo.INK_2)
            more = len(view.items) - len(items) + view.items_more
            if more > 0:
                add([f"+{more:,} more"], _font("label"), holo.INK_4, 4)
        result = (ops, y + 14.0)
        self._texts[width] = result
        return result

    # ---- painting -------------------------------------------------------------------------------

    def _pixmap(self, width: float, cap: float) -> Any:
        view = self._view
        dpr = self.devicePixelRatioF() or 1.0
        stamp = self.stamp()
        key = (round(width, 2), round(cap, 2), round(dpr, 3), stamp)
        pixmap = self._pixmaps.get(key)
        if pixmap is None:
            self._pixmaps.clear()
            pixmap = self._pixmaps[key] = hud.picture_pixmap(view.picture, width, dpr, stamp, cap)
        return pixmap

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            frame = self.frame_rect()
            if self._view is None:
                self._paint_empty(painter, frame)
                return
            self._paint_frame(painter, frame)
        finally:
            painter.end()

    def _paint_empty(self, painter: QPainter, frame: QRectF) -> None:
        font = _font("caption_sm")
        painter.setFont(font)
        painter.setPen(QColor(holo.INK_3))
        painter.drawText(frame, Qt.AlignmentFlag.AlignCenter, EMPTY_TEXT)

    def _paint_frame(self, painter: QPainter, frame: QRectF) -> None:
        state = self.state()
        will = self.held()
        inner = frame.adjusted(0.5, 0.5, -0.5, -0.5)
        path = holo.tile_path(inner, holo.RADIUS_FRAME)
        painter.fillPath(path, QColor(holo.PICTURE))
        pen = QPen(holo.LINE, 1)
        if state in (PENDING, UNAVAILABLE, DROPPED):
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        footer_left, footer_right = self.footer_texts()
        footer = bool(footer_left or footer_right)
        # The header: kind and verb at the left, the account / SENDING / stamp at the right.
        left, right = self.header_texts()
        header = QRectF(frame.left() + 12, frame.top(), frame.width() - 24, FRAME_HEADER)
        font = hud.mono_font(11, 500, 0.16)
        metrics = QFontMetricsF(font)
        painter.setFont(font)
        right_w = metrics.horizontalAdvance(right)
        right_color = holo.INK_KICKER
        if will:
            right_color = holo.OK
        else:
            words, tone = self.stamp()
            if words and right == words:
                right_color = _TONE_COLORS.get(tone, holo.INK_KICKER)
        painter.setPen(QColor(holo.OK if will else holo.INK_KICKER))
        painter.drawText(QRectF(header.left(), header.top(), max(1.0, header.width() - right_w - 12), FRAME_HEADER),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         metrics.elidedText(left, Qt.TextElideMode.ElideRight, max(1.0, header.width() - right_w - 12)))
        painter.setPen(QColor(right_color))
        painter.drawText(header, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, right)
        painter.fillRect(QRectF(frame.left() + 1, frame.top() + FRAME_HEADER, frame.width() - 2, 1), holo.LINE_FAINT)
        top = frame.top() + FRAME_HEADER + 1
        bottom = frame.bottom() - 1 - (FRAME_FOOTER if footer else 0)
        canvas = QRectF(frame.left() + 1, top, frame.width() - 2, max(1.0, bottom - top))
        painter.save()
        painter.setClipRect(canvas)
        if state == READY:
            self._paint_picture(painter, canvas)
        elif state == TEXT:
            self._paint_text(painter, canvas)
        else:
            self._paint_note(painter, canvas)
        painter.restore()
        if footer:
            y = frame.bottom() - FRAME_FOOTER
            painter.fillRect(QRectF(frame.left() + 1, y, frame.width() - 2, 1), holo.LINE_FAINT)
            foot_font = hud.mono_font(11, 500, 0.14)
            painter.setFont(foot_font)
            painter.setPen(QColor(holo.INK_4))
            row = QRectF(frame.left() + 12, y, frame.width() - 24, FRAME_FOOTER)
            painter.drawText(row, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, footer_left)
            painter.drawText(row, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, footer_right)
        if state not in (PENDING, UNAVAILABLE, DROPPED):
            color = holo.OK if will else holo.alpha(holo.HOLO, 0.70)
            holo.paint_brackets(painter, frame, color, leg=FRAME_BRACKET, radius=holo.RADIUS_FRAME,
                                corners=holo.BRACKETS_ALL)
        if self._focus_visible and self.hasFocus():
            painter.setPen(QPen(QColor(holo.FOCUS), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(frame.adjusted(1, 1, -1, -1), holo.RADIUS_FRAME, holo.RADIUS_FRAME)

    def _paint_picture(self, painter: QPainter, canvas: QRectF) -> None:
        picture = self._view.picture
        if picture.kind == live_pictures.KIND_PAGE:
            w, h = hud._page_size(picture.page, canvas.width(), canvas.height())
            pixmap = self._pixmap(canvas.width(), canvas.height())
            painter.setOpacity(0.9)   # page snapshots are bright: 90 % here (100 % when enlarged)
            painter.drawPixmap(QPointF(canvas.left() + (canvas.width() - w) / 2, canvas.top()), pixmap)
            painter.setOpacity(1.0)
            return
        painter.drawPixmap(QPointF(canvas.left(), canvas.top()), self._pixmap(canvas.width(), canvas.height()))

    def _paint_text(self, painter: QPainter, canvas: QRectF) -> None:
        ops, height = self._text_ops(int(canvas.width()))
        for font, color, text, x, y in ops:
            if canvas.top() + y > canvas.bottom():
                break
            painter.setFont(font)
            painter.setPen(QColor(color))
            metrics = QFontMetricsF(font)
            painter.drawText(QPointF(canvas.left() + x, canvas.top() + y + metrics.ascent()), text)
        if height > canvas.height() + 1:
            fade = QRectF(canvas.left(), canvas.bottom() - 28, canvas.width(), 28)
            gradient = QLinearGradient(fade.topLeft(), fade.bottomLeft())
            gradient.setColorAt(0.0, holo.alpha(holo.PICTURE, 0.0))
            gradient.setColorAt(1.0, QColor(holo.PICTURE))
            painter.fillRect(fade, gradient)

    def _paint_note(self, painter: QPainter, canvas: QRectF) -> None:
        text = hud.picture_state_text(self._view.picture)
        font = _font("body_sm")
        painter.setFont(font)
        painter.setPen(QColor(holo.INK_4))
        lines = _wrap(text, font, canvas.width() - 48, 4)
        step = _line_height(font)
        y = canvas.center().y() - step * len(lines) / 2
        for line in lines:
            painter.drawText(QRectF(canvas.left() + 24, y, canvas.width() - 48, step), Qt.AlignmentFlag.AlignCenter,
                             line)
            y += step

    # ---- events ---------------------------------------------------------------------------------

    def hitButton(self, pos: QPoint) -> bool:  # noqa: N802 - Qt override
        return self.state() == READY and self.frame_rect().contains(QPointF(pos))

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.state() == READY:
            self.click()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._focus_visible = holo.keyboard_focus(event.reason(), self._focus_visible)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().focusOutEvent(event)
        self.update()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            self._texts.clear()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self._pixmaps.clear()   # drawn again on the next paint; nothing kept while it is not seen


# --------------------------------------------------------------------------
# The exact text
# --------------------------------------------------------------------------


class _ExactToggle(QAbstractButton):
    """"Show the exact text" / "Hide the exact text": a cyan text link (Chakra 14 / 600, underlined) with
    a chevron that turns down when open; 32 px high."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setText(SHOW_EXACT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._focus_visible = False
        self._font = hud.display_font(14, 600, 0.04)
        self._font.setUnderline(True)
        self.toggled.connect(self._on_toggled)

    def _on_toggled(self, checked: bool) -> None:
        self.setText(HIDE_EXACT if checked else SHOW_EXACT)
        self.setAccessibleDescription("shown" if checked else "hidden")
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        widest = max(_advance(self._font, SHOW_EXACT), _advance(self._font, HIDE_EXACT))
        return QSize(math.ceil(8 + 14 + 6 + widest + 8), 32)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.sizeHint()

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._focus_visible = holo.keyboard_focus(event.reason(), self._focus_visible)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().focusOutEvent(event)
        self.update()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect())
            if self.underMouse() or self.isDown():
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(holo.alpha(holo.HOLO, 0.10))
                painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), holo.RADIUS_BUTTON_SM, holo.RADIUS_BUTTON_SM)
            color = QColor(holo.HOLO_HI if self.underMouse() else holo.HOLO)
            chevron = QRectF(8, rect.center().y() - 7, 14, 14)
            painter.save()
            painter.translate(chevron.center())
            painter.rotate(90 if self.isChecked() else 0)
            painter.translate(-chevron.center())
            holo.paint_icon(painter, chevron, "chevron", color, stroke=2.0)
            painter.restore()
            painter.setFont(self._font)
            painter.setPen(color)
            painter.drawText(rect.adjusted(28, 0, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             self.text())
            if self._focus_visible and self.hasFocus():
                holo.paint_focus_ring(painter, rect.adjusted(3, 3, -3, -3), holo.RADIUS_BUTTON_SM, offset=1)
        finally:
            painter.end()


class _ExactPlain(QPlainTextEdit):
    """The exact text: read only, plain text (nothing is a link, nothing loads), mono 12, selectable,
    wrapped at its width; as tall as its text up to EXACT_MAX_H, then it scrolls. Tab leaves it; its
    context menu has Copy and Select All only."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setTabChangesFocus(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                     | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.setFont(hud.mono_font(12))
        self.document().setDocumentMargin(0)
        self.setStyleSheet("QPlainTextEdit { color: %s; background: transparent; border: none; "
                           "selection-background-color: rgba(60, 211, 238, 82); selection-color: %s; }"
                           % (holo.INK_2, holo.INK_1))
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(40)
        self._text = ""

    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        self.setPlainText(text)
        QTimer.singleShot(0, self._fit)

    def _fit(self) -> None:
        if not shiboken6.isValid(self):
            return
        total = 0.0
        block = self.document().firstBlock()
        while block.isValid() and total <= EXACT_MAX_H:
            total += self.blockBoundingRect(block).height()
            block = block.next()
        height = max(20, min(EXACT_MAX_H, math.ceil(total) + 4))
        if height != self.height():
            self.setFixedHeight(height)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            QTimer.singleShot(0, self._fit)

    def contextMenuEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        menu = QMenu(self)
        copy = menu.addAction("Copy")
        copy.setEnabled(self.textCursor().hasSelection())
        copy.triggered.connect(self.copy)
        menu.addAction("Select All").triggered.connect(self.selectAll)
        menu.exec(event.globalPos())
        menu.deleteLater()


class _ExactBox(QWidget):
    """The open exact text's panel: ``field`` fill, a ``line-faint`` border, radius 6, padding 10 x 12."""

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            painter.setPen(QPen(holo.LINE_FAINT, 1))
            painter.setBrush(QColor(holo.FIELD))
            painter.drawRoundedRect(rect, holo.RADIUS_FRAME, holo.RADIUS_FRAME)
        finally:
            painter.end()


class ExactText(QWidget):
    """"Show the exact text" under the focal picture (collapsed at first); open, the step's exact text with
    WRITTEN BY OTHER PEOPLE above it when any of it is other people's. Built only while open, and only
    when the step's text changed."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._view: live_events.StepView | None = None
        self.toggle = _ExactToggle()
        self.toggle.toggled.connect(self._sync)
        self.box = _ExactBox()
        self.mark = _ElidedText(hud.mono_font(11, 500, 0.14), holo.INK_4)
        self.mark.set_text(OTHERS_MARK)
        self.text_edit = _ExactPlain()
        box = QVBoxLayout(self.box)
        box.setContentsMargins(12, 10, 12, 10)
        box.setSpacing(6)
        box.addWidget(self.mark)
        box.addWidget(self.text_edit)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.box)
        self.box.hide()
        self.toggle.hide()

    def is_open(self) -> bool:
        return self.toggle.isChecked()

    def set_open(self, open_: bool) -> None:
        self.toggle.setChecked(bool(open_))

    def others(self) -> bool:
        return others_text(self._view)

    def text(self) -> str:
        return self.text_edit.text() if self.is_open() else ""

    def set_step(self, view: live_events.StepView | None) -> None:
        self._view = view
        self._sync()

    def _sync(self) -> None:
        view = self._view
        has = view is not None and bool(view.fields or view.items or view.items_more or view.blocks)
        if self.toggle.isHidden() == has:
            self.toggle.setVisible(has)
        shown = has and self.is_open()
        if shown:
            self.mark.setVisible(others_text(view))
            self.text_edit.set_text(exact_text(view))
        else:
            self.text_edit.set_text("")   # kept only while it shows (a cleared step's text is let go)
        if self.box.isHidden() == shown:
            self.box.setVisible(shown)


# --------------------------------------------------------------------------
# The focal view
# --------------------------------------------------------------------------


class FocalView(QWidget):
    """The focal step's frame and exact text, following ``log`` (see the module docstring)."""

    focalChanged = Signal()

    def __init__(self, log: hud.LiveLog, parent: QWidget | None = None, *, max_width: int = FOCAL_MAX_W,
                 fit_height: bool = True) -> None:
        super().__init__(parent)
        self._log = log
        self._max_width = max_width
        self._max_height = FOCAL_MAX_H
        self._fit_height = fit_height   # the frame never taller than this view leaves it (the main screen)
        self._pinned = 0
        self._hold_task = 0
        self._focal = 0
        self.frame = PictureFrame(clock=log.now)
        self.frame.clicked.connect(self._open_larger)
        self.exact = ExactText()
        self.column = QWidget()
        self.column.setMaximumWidth(max_width)
        column = QVBoxLayout(self.column)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        column.addWidget(self.frame)
        column.addWidget(self.exact)
        column.addStretch(1)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self.column, 1)
        log.changed.connect(self.sync)
        log.stepSelected.connect(self.select)
        log.ticked.connect(self.frame.tick)
        self.sync()

    # ---- API ------------------------------------------------------------------------------------

    def focal_step(self) -> int:
        return self._focal

    def set_max_height(self, height: int) -> None:
        """The frame's largest height (the pop-out: a share of its window)."""
        self._max_height = max(120, min(FOCAL_MAX_H, int(height)))
        self._fit_frame()

    def _fit_frame(self) -> None:
        height = self._max_height
        if self._fit_height and self.height() > 0:
            toggle = self.exact.toggle
            room = self.height() - (toggle.sizeHint().height() + 8 if not toggle.isHidden() else 0)
            height = max(120, min(height, room))
        self.frame.set_max_size(QSize(self._max_width, height))

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._fit_frame()

    def pinned(self) -> bool:
        """The owner picked the focal step (Follow live brings the newest back)."""
        return bool(self._pinned)

    def select(self, step_id: int) -> None:
        """The owner clicked a step: it becomes the focal one (a running one: back to following)."""
        found = self._find(int(step_id))
        if found is None:
            return
        step, _task = found
        self._pinned = 0 if step.status == live_events.STATUS_RUNNING else step.id
        self.sync()

    def follow(self) -> None:
        if self._pinned:
            self._pinned = 0
            self.sync()

    # ---- following ------------------------------------------------------------------------------

    def _find(self, step_id: int) -> tuple[live_events.StepView, live_events.TaskView] | None:
        for task in self._log.task_views():
            for step in task.steps:
                if step.id == step_id:
                    return step, task
        return None

    def sync(self) -> None:
        """Choose the focal step again (the log changed, or the owner picked one)."""
        if not shiboken6.isValid(self._log):
            return
        tasks = self._log.task_views()
        hold_task, hold = None, None
        for task in reversed(tasks):
            if task.kind == live_events.TASK_ACTION and not task.pinned:
                hold = _running_hold(task)
                if hold is not None:
                    hold_task = task
                    break
        new_hold = hold_task.id if hold_task is not None else 0
        if new_hold and new_hold != self._hold_task:
            self._pinned = 0   # an approved card's hidden hold: what it sends comes into view
        self._hold_task = new_hold
        focal: tuple[live_events.StepView, live_events.TaskView] | None = None
        if self._pinned:
            focal = self._find(self._pinned)
            if focal is None:
                self._pinned = 0
        if focal is None and hold_task is not None:
            payload = next((step for step in hold_task.steps if step.kind == live_events.ACTION_PAYLOAD), None)
            focal = (payload or hold, hold_task)
        if focal is None:
            for task in reversed(tasks):
                if not task.pinned and task.steps:
                    focal = (task.steps[-1], task)
                    break
        step, task = focal if focal is not None else (None, None)
        self.frame.set_step(step, step is not None and task is hold_task and hold is not None)
        self.exact.set_step(step)
        focal_id = step.id if step is not None else 0
        self._log.set_focal_step(focal_id)
        if focal_id != self._focal:
            self._focal = focal_id
            self.focalChanged.emit()

    def _open_larger(self) -> None:
        step_id = self.frame.step_id()
        if step_id:
            self._log.open_picture(step_id, self.frame)   # its close gives the focus back to the frame


# --------------------------------------------------------------------------
# The Live screen
# --------------------------------------------------------------------------


class _Strip(QWidget):
    """The Live strip: 44 px, a ``line-faint`` hairline at the bottom."""

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.fillRect(QRectF(0, self.height() - 1, self.width(), 1), holo.LINE_FAINT)
        finally:
            painter.end()


class LiveStage(QWidget):
    """The Live screen (HOLOGRAM_SPEC 16.3); see the module docstring."""

    popOutClicked = Signal()
    clearClicked = Signal()
    showWindowClicked = Signal()
    bringBackClicked = Signal()

    def __init__(self, log: hud.LiveLog, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.log = log
        # The strip.
        self.readout = LiveReadout()
        self.step_label = _ElidedText(hud.display_font(14, 500, 0.02), holo.INK_3)
        self.follow_button = holo.HoloButton(FOLLOW_TEXT, holo.LINK_CYAN, size=holo.SIZE_SM)
        self.follow_button.setToolTip(FOLLOW_TIP)
        self.follow_button.hide()
        self.pop_button = holo.HoloButton(POP_OUT_LABEL, holo.ICON, icon="popout", diameter=40)
        self.pop_button.setToolTip(POP_OUT_LABEL)
        self.clear_button = holo.HoloButton(CLEAR_TEXT, holo.GHOST, size=holo.SIZE_SM)
        self.strip = _Strip()
        self.strip.setFixedHeight(STRIP_HEIGHT + 10)
        strip = QHBoxLayout(self.strip)
        strip.setContentsMargins(0, 0, 0, 10)
        strip.setSpacing(12)
        strip.addWidget(self.readout, 0, Qt.AlignmentFlag.AlignVCenter)
        strip.addWidget(self.step_label, 1, Qt.AlignmentFlag.AlignVCenter)
        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        buttons.addWidget(self.follow_button, 0, Qt.AlignmentFlag.AlignVCenter)
        buttons.addWidget(self.pop_button, 0, Qt.AlignmentFlag.AlignVCenter)
        buttons.addWidget(self.clear_button, 0, Qt.AlignmentFlag.AlignVCenter)
        strip.addLayout(buttons)
        # The body: the timeline and the focal column.
        self.focal = FocalView(log)
        self.timeline = QWidget()
        self.timeline.setFixedWidth(TIMELINE_WIDTH)
        self._timeline_layout = QVBoxLayout(self.timeline)
        self._timeline_layout.setContentsMargins(0, 0, 0, 0)
        self._timeline_layout.setSpacing(12)
        self._timeline_layout.addWidget(log, 1)
        log.setFixedWidth(TIMELINE_WIDTH)
        self.body = QWidget()
        body = QHBoxLayout(self.body)
        body.setContentsMargins(0, 12, 0, 0)
        body.setSpacing(TIMELINE_GAP)
        body.addWidget(self.timeline)
        body.addWidget(self.focal, 1)
        # While the pop-out shows the steps.
        self.away = QWidget()
        self.away_icon = _AwayIcon()
        self.away_label = _ElidedText(_font("caption_sm"), holo.INK_2)
        self.away_label.set_text(AWAY_TEXT)
        self.away_label.setObjectName("liveAway")
        self.show_button = holo.HoloButton(SHOW_WINDOW_TEXT, holo.GHOST, size=holo.SIZE_SM)
        self.back_button = holo.HoloButton(BRING_BACK_TEXT, holo.GHOST, size=holo.SIZE_SM)
        self.back_button.setToolTip(BRING_BACK_TIP)
        away = QVBoxLayout(self.away)
        away.setContentsMargins(24, 24, 24, 24)
        away.setSpacing(16)
        away.addStretch(1)
        away.addWidget(self.away_icon, 0, Qt.AlignmentFlag.AlignHCenter)
        away.addWidget(self.away_label, 0, Qt.AlignmentFlag.AlignHCenter)
        links = QHBoxLayout()
        links.setSpacing(8)
        links.addStretch(1)
        links.addWidget(self.show_button)
        links.addWidget(self.back_button)
        links.addStretch(1)
        away.addLayout(links)
        away.addStretch(2)
        self._switch = QStackedLayout()
        holder = QWidget()
        holder.setLayout(self._switch)
        self._switch.addWidget(self.body)
        self._switch.addWidget(self.away)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.strip)
        layout.addWidget(holder, 1)
        self.follow_button.clicked.connect(self.focal.follow)
        self.pop_button.clicked.connect(self.popOutClicked)
        self.clear_button.clicked.connect(self.clearClicked)
        self.show_button.clicked.connect(self.showWindowClicked)
        self.back_button.clicked.connect(self.bringBackClicked)
        self.focal.focalChanged.connect(self._sync_strip)
        log.changed.connect(self._sync_strip)
        self._sync_strip()

    # ---- API ------------------------------------------------------------------------------------

    def set_activity(self, widget: QWidget | None) -> None:
        """A panel under the timeline (ACTIVITY), at most a third of its height."""
        if widget is None:
            return
        self._timeline_layout.addWidget(widget, 0)
        widget.setMaximumHeight(220)

    def set_popped(self, popped: bool) -> None:
        self._switch.setCurrentWidget(self.away if popped else self.body)
        self.pop_button.setEnabled(not popped)
        self._sync_strip()

    def popped(self) -> bool:
        return self._switch.currentWidget() is self.away

    def timeline_room(self) -> int:
        """The height the timeline has (estimated from the stage while it is not laid out)."""
        if self.log.isVisible():
            return self.log.height()
        return max(0, self.height() - self.strip.height() - 12)

    def _sync_strip(self) -> None:
        """The strip: the focal step's title while Jarvis works; Follow live while the owner picked one."""
        step = self.focal.frame.step()
        working = self.readout.state() != hud.LIVE_IDLE
        self.step_label.set_text(step.title if step is not None and working and not self.popped() else "")
        follow = self.focal.pinned() and not self.popped()
        if self.follow_button.isHidden() == follow:
            self.follow_button.setVisible(follow)

    def set_state(self, state: str, text: str | None = None, *, since: float | None = None) -> None:
        self.readout.set_state(state, text, since=since)
        self._sync_strip()


class _AwayIcon(QWidget):
    """The pop-out glyph (28 px, cyan) above "LIVE is in its own window, sir."."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(28, 28)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            holo.paint_icon(painter, QRectF(self.rect()), "popout", holo.HOLO, stroke=1.6)
        finally:
            painter.end()


# --------------------------------------------------------------------------
# The pop-out's header
# --------------------------------------------------------------------------


class PopOutHeader(QWidget):
    """The pop-out's 44 px header (HOLOGRAM_SPEC 16.3); see the module docstring. Narrower than all of it
    needs, Clear, then the mini orb, then minimize give way (the taskbar button still minimizes); the
    readout, Bring it back and close always stay whole."""

    minimizeClicked = Signal()
    closeClicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(POP_HEADER_HEIGHT)
        self._drag: QPoint | None = None
        self.orb = orb_mod.MiniOrb(ring_color=holo.VOID)
        self.orb.setAccessibleName("Jarvis")
        self.readout = LiveReadout()
        self.clear_button = holo.HoloButton(CLEAR_TEXT, holo.GHOST, size=holo.SIZE_SM)
        self.back_button = holo.HoloButton(BRING_BACK_TEXT, holo.GHOST, size=holo.SIZE_SM)
        self.back_button.setToolTip(BRING_BACK_TIP)
        self.minimize_button = holo.HoloButton(MINIMIZE_LABEL, holo.ICON, icon="minimize", diameter=34)
        self.close_button = holo.HoloButton(CLOSE_LABEL, holo.ICON, icon="close", diameter=34)
        self.minimize_button.clicked.connect(self.minimizeClicked)
        self.close_button.clicked.connect(self.closeClicked)
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self.orb, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.readout, 1, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.clear_button, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.back_button, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.minimize_button, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_state(self, state: str, text: str | None = None, *, since: float | None = None) -> None:
        self.readout.set_state(state, text, since=since)
        self.orb.set_state(orb_mod.IDLE if state == hud.LIVE_IDLE else orb_mod.THINKING)
        self._fit()

    def _fit(self) -> None:
        """Clear, then the mini orb, then minimize give way when the header is too narrow for all."""
        spacing = self.layout().spacing()
        margins = self.layout().contentsMargins()
        need = margins.left() + margins.right() + self.readout.sizeHint().width() + spacing
        need += self.back_button.sizeHint().width() + spacing + self.close_button.sizeHint().width()
        room = self.width()
        shown: dict[QWidget, bool] = {}
        for widget, width in ((self.minimize_button, self.minimize_button.sizeHint().width()),
                              (self.orb, self.orb.width()), (self.clear_button, self.clear_button.sizeHint().width())):
            shown[widget] = need + width + spacing <= room
            if shown[widget]:
                need += width + spacing
        for widget, show in shown.items():
            if widget.isHidden() == show:
                widget.setVisible(show)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._fit()

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None and handle.startSystemMove():
                return
            self._drag = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._drag = None
        super().mouseReleaseEvent(event)


class PopOutBody(QWidget):
    """The pop-out's content under its header (HOLOGRAM_SPEC 16.3): the focal picture full width, its
    exact text (collapsed) and the timeline below as compact rows, in one column of at most
    POP_COLUMN_MAX. The picture takes at most POP_FOCAL_SHARE of the height (and 400 px)."""

    def __init__(self, log: hud.LiveLog, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.log = log
        self.focal = FocalView(log, max_width=POP_COLUMN_MAX, fit_height=False)
        self.column = QWidget()
        self.column.setMaximumWidth(POP_COLUMN_MAX)
        column = QVBoxLayout(self.column)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        column.addWidget(self.focal, 0)
        column.addWidget(log, 1)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 8, 0, 0)
        row.setSpacing(0)
        row.addWidget(self.column, 1)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self.focal.set_max_height(int(self.height() * POP_FOCAL_SHARE))

