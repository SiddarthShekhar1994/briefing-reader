"""Home and the hologram chrome every screen shares (HOLOGRAM_SPEC 11.2-11.5, 14, 16.0; round 1).

Public API
==========

``paginate(text, max_words=14) -> list[str]``
    Caption pages (spec 14.2): split at sentence ends, then at clause marks (``,`` ``;`` `` - ``
    ``:``), then evenly, never between a number and its unit (``12:30 PM``), between two capitalised
    words (a name or title) or after a little word ("at", "the" ...).

``thinking_line(text) -> str``
    The command bar's working status as the caption's thinking line: no seconds counter (technical
    readouts live in LIVE) and at most 8 words.

``CaptionView``
    One caption at a time under the hero orb. ``set_resting(text)`` (the greeting, at most 6 words),
    ``set_question(text)`` (an open offer's question: shown instead of the greeting, in question type,
    until it is answered or expires; spec 14.5), ``set_thinking(text)`` (``ink-2`` line with the scan
    line under it), ``say(text, spoken=True, duration_ms=0)`` (pages of at most 14 words revealed word
    by word: over ``duration_ms`` when the audio's length is known (a briefing item), else at the
    voice's estimated pace, ``SPOKEN_WPS``; muted or silent: ``MUTED_WPS``), ``finish()`` (the voice
    stopped: the last page shows whole and holds ``HOLD_SPOKEN_MS``, then fades to the resting line; a
    silent caption holds ``HOLD_MUTED_MS``). Times on a page (``10:30 AM``, the hour in "at 10") are
    ``holo``; amber never appears. ``state()`` is one of ``RESTING``, ``THINKING``, ``SPEAKING``,
    ``HOLDING``; ``stateChanged(str)``.

``HoloHeader``
    The 52 px title bar of the main window (drag area): the ``J.A.R.V.I.S.`` wordmark and the state
    readout (7 px dot + mono word, ``set_state_word(word, tone)``), a note per service that is not
    OK (``set_service(name, status, tooltip)``: dot + mono name, the service chip's text as tooltip and
    accessible name; until round 2's error tiles), a Send's or Cancel's hidden hold away from its card
    (``set_hold(word)``: the confirmed "SENT" with an **Undo** link, ``undoClicked``; never a number),
    the "Find or ask anything" pill (``find_button``, a find_view.FindButton: ``findClicked``; Ctrl+K;
    folded to a round search button when the header is too narrow), then the clock button (opens the
    Day view: ``clockClicked``), a divider, mute (``muteClicked``; ``set_mute_visible``,
    ``set_muted``), minimize (``minimizeClicked``) and close = Done (``closeClicked``).
    ``type_wordmark()`` types J.A.R.V.I.S. in (the start-up: 1.1 s after 250 ms, letter by letter; the
    accessible name stays "Jarvis"), ``wordmark_typing()``.

``HomeStage``
    The stage: the hero orb (``hero``, an ``orb.OrbWidget`` sized to fit, 200-320 px) centred, four
    18 px corner brackets, a faint hairline through the orb, and the Transcript button bottom-left
    (``transcript_button``). Round 2, part 3a: ``set_side_tile(tile)`` puts the one amber tile (an offer,
    offer_view.OfferTile) bottom-right of the orb, tethered to it; the orb gives it room while it shows.

``EchoPill``
    ``YOU`` + the owner's request in mono 13, under the orb while it is handled.

``Drawer``
    A panel that slides in from the right (420 wide, ``raised``, 280 ms in, 180 ms out), with a title
    and a close button; Esc closes it and the focus goes back to the opener. ``body_layout`` holds
    the content (the Transcript's conversation, the Day view's agenda).

Settings has its own module since round 2, part 2b (settings_view.SettingsStage).
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime
from typing import Any, Callable

import shiboken6
from PySide6.QtCore import (
    QEvent,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import find_view, holo, offer_view
from . import orb as orb_mod

# --------------------------------------------------------------------------
# Caption pages (spec 14)
# --------------------------------------------------------------------------

CAPTION_WORDS = 14
THINKING_WORDS = 8
SPOKEN_WPS = 2.7           # the voice's pace as estimated from the text (when the audio's length is unknown)
WORD_MS_RANGE = (150, 1200)  # a word's reveal interval paced from a known audio length stays in this range
MUTED_WPS = 3.5            # muted or silent captions (spec 14.7)
HOLD_SPOKEN_MS = 3000
HOLD_MUTED_MS = 5000
REVEAL_MS = 450
RISE_PX = 10.0
SWAP_OUT_MS = 160
FADE_OUT_MS = 600
_FRAME_MS = 33

RESTING = "resting"
THINKING = "thinking"
SPEAKING = "speaking"
HOLDING = "holding"

_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+")
_DASHES = frozenset({"-", "\u2013", "\u2014"})
_NUMBER = re.compile(r"^[\d$\u00a3\u20ac][\d.,:%/]*$")
_UNIT = re.compile(r"^(?:am|pm|a\.m\.|p\.m\.|%|min|mins|minutes|hours?|days?|percent|o'clock)[.,;:!?]*$",
                   re.IGNORECASE)
_LITTLE = frozenset({"a", "an", "the", "at", "on", "in", "to", "of", "for", "by", "with", "and", "or", "from",
                     "your", "my", "his", "her", "its", "our", "their", "this", "that", "mr", "mrs", "ms", "dr"})
_TIME = re.compile(r"\b\d{1,2}(?::\d{2})?\s?(?:AM|PM|am|pm)\b|\b\d{1,2}:\d{2}\b"
                   # a bare hour after at / by / from / until / till ("the check-in at 10")
                   r"|(?:(?<=\bat )|(?<=\bby )|(?<=\bfrom )|(?<=\buntil )|(?<=\btill ))(?:[01]?\d|2[0-3])\b(?![:.]\d)"
                   r"(?!\s*(?:%|percent|minutes?|mins?|hours?|days?|weeks?|months?|years?|people|of)\b)",
                   re.IGNORECASE)
_SECONDS = re.compile(r"\s*\b\d+(?:\.\d+)?\s?s\b\.?$")


def _words(text: str) -> list[str]:
    return text.split()


def _bad_break(left: str, right: str) -> bool:
    """A page must not end between ``left`` and ``right``."""
    if _NUMBER.match(left) and _UNIT.match(right):
        return True
    if left[:1].isupper() and right[:1].isupper() and not left.endswith((",", ".", ";", ":")):
        return True
    return left.lower().strip(".,;:") in _LITTLE


def _split_even(words: list[str], max_words: int) -> list[list[str]]:
    """Split a run of words with no punctuation into pages of at most ``max_words``, as even as can be,
    moving each break to the nearest allowed place."""
    if len(words) <= max_words:
        return [words]
    count = math.ceil(len(words) / max_words)
    size = math.ceil(len(words) / count)
    pages: list[list[str]] = []
    start = 0
    while len(words) - start > max_words:
        ideal = min(start + size, len(words) - 1)
        best = ideal
        for delta in range(0, size):
            hit = None
            for cut in (ideal - delta, ideal + delta):
                if start < cut < len(words) and cut - start <= max_words and not _bad_break(words[cut - 1], words[cut]):
                    hit = cut
                    break
            if hit is not None:
                best = hit
                break
        best = max(start + 1, min(best, start + max_words))
        pages.append(words[start:best])
        start = best
    pages.append(words[start:])
    return pages


def _clauses(words: list[str]) -> list[list[str]]:
    """The sentence's clauses: each ends after a word ending in ``,`` ``;`` ``:`` or with a dash (the
    marks stay with the clause on their left)."""
    clauses: list[list[str]] = []
    current: list[str] = []
    for word in words:
        current.append(word)
        if word in _DASHES or word.endswith((",", ";", ":")):
            clauses.append(current)
            current = []
    if current:
        clauses.append(current)
    return clauses


def paginate(text: str, max_words: int = CAPTION_WORDS) -> list[str]:
    """Caption pages of at most ``max_words`` words (spec 14.2)."""
    text = " ".join((text or "").split())
    if not text:
        return []
    pages: list[str] = []
    for sentence in (part for part in _SENTENCE_END.split(text) if part.strip()):
        words = _words(sentence)
        if len(words) <= max_words:
            pages.append(sentence.strip())
            continue
        current: list[str] = []
        for clause in _clauses(words):
            if len(current) + len(clause) <= max_words:
                current += clause
                continue
            if current:
                pages.append(" ".join(current))
            if len(clause) <= max_words:
                current = list(clause)
            else:
                parts = _split_even(clause, max_words)
                pages.extend(" ".join(part) for part in parts[:-1])
                current = list(parts[-1])
        if current:
            pages.append(" ".join(current))
    return pages


def thinking_line(text: str) -> str:
    """The working status as a thinking line: no seconds counter, at most 8 words."""
    text = _SECONDS.sub("", " ".join((text or "").split())).strip()
    words = text.split()
    if len(words) > THINKING_WORDS:
        text = " ".join(words[:THINKING_WORDS]).rstrip(".,;:") + "..."
    return text


_READOUTS = (re.compile(r"^\d+(?:\.\d+)?\s?s$"), re.compile(r"\bplanner runs?\b", re.IGNORECASE),
             re.compile(r"\bruns?\s+left\b", re.IGNORECASE), re.compile(r"\bleft (?:today|this hour)\b", re.IGNORECASE),
             re.compile(r"^\d+\s+(?:planner\s+)?runs?$", re.IGNORECASE),
             re.compile(r"\b\d+(?:\.\d+)?\s?(?:kb|mb|bytes?)\b", re.IGNORECASE))
_SUB_DOT = " \u00b7 "


def quiet_sub(sub: str) -> str:
    """A conversation entry's sub line without its technical readouts (owner decision 1: seconds,
    planner runs, runs left, sizes live in LIVE only): "2 proposals . 3.1 s . 1 planner run" ->
    "2 proposals"."""
    parts = [part.strip() for part in (sub or "").split(_SUB_DOT.strip())]
    kept = [part for part in parts if part and not any(pattern.search(part) for pattern in _READOUTS)]
    return _SUB_DOT.join(kept)


def _ease_out(p: float) -> float:
    p = max(0.0, min(1.0, p))
    return 1.0 - (1.0 - p) ** 3


class CaptionView(QWidget):
    """The caption band under the hero orb; see the module docstring."""

    stateChanged = Signal(str)
    pageShown = Signal(str)              # a page's last word is revealed (screen readers: once a page)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setAccessibleName("Jarvis says")
        self._resting = ""
        self._thinking = ""
        self._pages: list[str] = []
        self._page = -1
        self._words: list[str] = []
        self._revealed_at: list[float] = []
        self._spoken = True
        self._pace_ms = 0                   # a word's interval from the audio's known length (0: estimated)
        self._question = ""
        self._finished = False
        self._state = RESTING
        self._fade = 1.0                    # the whole caption's opacity (fade-out at the end)
        self._fade_from = 0.0               # when the fade-out started (monotonic), 0 = none
        self._swap_from = 0.0               # when the last page swap started (monotonic), 0 = none
        self._word_timer = QTimer(self)
        self._word_timer.setSingleShot(True)
        self._word_timer.timeout.connect(self._next_word)
        self._page_timer = QTimer(self)
        self._page_timer.setSingleShot(True)
        self._page_timer.timeout.connect(self._advance_page)
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._start_fade)
        self._frame = QTimer(self)
        self._frame.setInterval(_FRAME_MS)
        self._frame.timeout.connect(self._on_frame)

    # ---- API ---------------------------------------------------------------------------

    def set_resting(self, text: str) -> None:
        """The resting line (the greeting); shown while nothing is said or thought."""
        self._resting = " ".join((text or "").split())
        if self._state == RESTING:
            self.update()

    def resting(self) -> str:
        return self._resting

    def set_question(self, text: str) -> None:
        """An open offer's question (spec 14.5): the resting line until it is answered or expires ("")."""
        self._question = " ".join((text or "").split())
        self.setAccessibleDescription(self._question or self.page_text())
        if self._state == RESTING:
            self.update()

    def question(self) -> str:
        return self._question

    def set_thinking(self, text: str) -> None:
        """The thinking line ("" ends it); a caption being said keeps the band until it is done."""
        self._thinking = thinking_line(text)
        if self._state in (RESTING, THINKING):
            self._set_state(THINKING if self._thinking else RESTING)
            self.update()

    def thinking(self) -> str:
        return self._thinking

    def say(self, text: str, *, spoken: bool = True, duration_ms: int = 0) -> None:
        """Show ``text`` as pages revealed word by word (``spoken`` False: no voice, the muted pace;
        ``duration_ms``: how long the audio says it, which paces the words; 0: the estimated pace)."""
        pages = paginate(text)
        if not pages:
            return
        self._hold_timer.stop()
        self._page_timer.stop()
        self._pages = pages
        self._spoken = bool(spoken)
        steps = sum(len(page.split()) for page in pages) + len(pages) - 1   # each word, and a beat per page
        low, high = WORD_MS_RANGE
        self._pace_ms = max(low, min(high, round(duration_ms / steps))) if spoken and duration_ms > 0 else 0
        self._finished = not spoken
        self._fade, self._fade_from = 1.0, 0.0
        self._set_state(SPEAKING)
        self._show_page(0)

    def finish(self) -> None:
        """The voice stopped: the last page shows whole, holds, then the band rests."""
        if self._state != SPEAKING:
            return
        self._finished = True
        self._page_timer.stop()
        if self._page < len(self._pages) - 1:
            self._show_page(len(self._pages) - 1, reveal_all=True)
        else:
            self._reveal_all()
        self._page_done()

    def clear(self) -> None:
        """Back to the resting (or thinking) line at once."""
        self._word_timer.stop()
        self._page_timer.stop()
        self._hold_timer.stop()
        self._pages, self._page, self._words, self._revealed_at = [], -1, [], []
        self._fade, self._fade_from = 1.0, 0.0
        self._set_state(THINKING if self._thinking else RESTING)
        self._sync_frame()
        self.update()

    def state(self) -> str:
        return self._state

    def pages(self) -> list[str]:
        return list(self._pages)

    def page_index(self) -> int:
        return self._page

    def page_text(self) -> str:
        return " ".join(self._words)

    def revealed_words(self) -> int:
        return len(self._revealed_at)

    def shown_text(self) -> str:
        """What the band shows now (the revealed words of the page, the thinking or the resting line)."""
        if self._state in (SPEAKING, HOLDING):
            return " ".join(self._words[:len(self._revealed_at)])
        if self._state == THINKING:
            return self._thinking
        return self._question or self._resting

    def highlighted(self) -> list[str]:
        """The page's words drawn in ``holo`` (times; at most 3)."""
        return [self._words[i] for i in sorted(self._highlights())]

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(holo.CAPTION_MAX_WIDTH, self.minimumSizeHint().height() + 16)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(200, 2 * holo.line_height("caption") + 4)

    # ---- pages and words -----------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.stateChanged.emit(state)

    def _interval_ms(self) -> int:
        if self._pace_ms:
            return self._pace_ms
        return round(1000.0 / (SPOKEN_WPS if self._spoken else MUTED_WPS))

    def _show_page(self, index: int, *, reveal_all: bool = False) -> None:
        self._page = index
        self._words = self._pages[index].split()
        self._revealed_at = []
        now = time.monotonic()
        self._swap_from = now if index > 0 and not holo.reduced_motion() else 0.0
        if reveal_all or holo.reduced_motion():
            self._reveal_all()
            if not reveal_all:
                self._page_done()
        else:
            self._next_word()
        self._sync_frame()
        self.update()

    def _reveal_all(self) -> None:
        now = time.monotonic() - (0.0 if holo.reduced_motion() else REVEAL_MS / 1000.0)
        while len(self._revealed_at) < len(self._words):
            self._revealed_at.append(now)
        self._word_timer.stop()
        self.setAccessibleDescription(self.page_text())
        self.update()

    def _next_word(self) -> None:
        if self._state != SPEAKING or not self._words:
            return
        if len(self._revealed_at) < len(self._words):
            self._revealed_at.append(time.monotonic())
            self._sync_frame()
            self.update()
        if len(self._revealed_at) < len(self._words):
            self._word_timer.start(self._interval_ms())
            return
        self._page_done()

    def _page_done(self) -> None:
        """The page is revealed whole: the next page after a beat, or the hold."""
        self._word_timer.stop()
        self.setAccessibleDescription(self.page_text())
        self.pageShown.emit(self.page_text())
        if self._page < len(self._pages) - 1:
            self._page_timer.start(self._interval_ms())
            return
        if self._finished:
            self._set_state(HOLDING)
            self._hold_timer.start(HOLD_SPOKEN_MS if self._spoken else HOLD_MUTED_MS)

    def _advance_page(self) -> None:
        if self._state == SPEAKING and self._page < len(self._pages) - 1:
            self._show_page(self._page + 1)

    def _start_fade(self) -> None:
        if holo.reduced_motion():
            self.clear()
            return
        self._fade_from = time.monotonic()
        self._sync_frame()

    # ---- animation ----------------------------------------------------------------------------

    def _animating(self, now: float) -> bool:
        if holo.reduced_motion() or not self.isVisible():
            return False
        if self._fade_from:
            return True
        if self._swap_from and now - self._swap_from < (SWAP_OUT_MS + REVEAL_MS) / 1000.0:
            return True
        return bool(self._revealed_at) and now - self._revealed_at[-1] < REVEAL_MS / 1000.0

    def _sync_frame(self) -> None:
        running = self._animating(time.monotonic())
        if running and not self._frame.isActive():
            self._frame.start()
        elif not running and self._frame.isActive():
            self._frame.stop()

    def _on_frame(self) -> None:
        now = time.monotonic()
        if self._fade_from:
            self._fade = max(0.0, 1.0 - (now - self._fade_from) / (FADE_OUT_MS / 1000.0))
            if self._fade <= 0.0:
                self.clear()
                return
        self.update()
        self._sync_frame()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._sync_frame()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self._frame.stop()

    # ---- painting -------------------------------------------------------------------------------

    def _highlights(self) -> set[int]:
        """The words of the page's first 3 times (a time is one item even when it is two words)."""
        text = self.page_text()
        if not text:
            return set()
        starts = []
        position = 0
        for word in self._words:
            starts.append(text.index(word, position))
            position = starts[-1] + len(word)
        marks: set[int] = set()
        for match in list(_TIME.finditer(text))[:3]:
            for i, start in enumerate(starts):
                if start < match.end() and start + len(self._words[i]) > match.start():
                    marks.add(i)
        return marks

    def _lines(self, font: QFont, width: float) -> list[list[int]]:
        metrics = QFontMetricsF(font)
        space = metrics.horizontalAdvance(" ")
        lines: list[list[int]] = [[]]
        used = 0.0
        for i, word in enumerate(self._words):
            w = metrics.horizontalAdvance(word)
            if lines[-1] and used + space + w > width:
                lines.append([])
                used = 0.0
            used += (space if lines[-1] else 0.0) + w
            lines[-1].append(i)
        return lines

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            width = min(float(self.width()), float(holo.CAPTION_MAX_WIDTH))
            left = (self.width() - width) / 2
            if self._state in (SPEAKING, HOLDING):
                self._paint_page(painter, left, width)
            elif self._state == THINKING:
                self._paint_thinking(painter)
            elif self._question or self._resting:
                painter.setFont(holo.font("question" if self._question else "greeting"))
                painter.setPen(QColor(holo.INK_1))
                rect = QRectF(left, 0, width, self.height())
                painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap),
                                 self._question or self._resting)
        finally:
            painter.end()

    def _paint_thinking(self, painter: QPainter) -> None:
        font = holo.font("caption_sm")
        line_h = holo.line_height("caption_sm")
        top = (self.height() - line_h - 16) / 2
        painter.setFont(font)
        painter.setPen(QColor(holo.INK_2))
        painter.drawText(QRectF(0, top, self.width(), line_h), int(Qt.AlignmentFlag.AlignCenter), self._thinking)
        scan = QRectF(self.width() / 2 - 110, top + line_h + 14, 220, 1)
        painter.fillRect(scan, holo.alpha(holo.HOLO, 0.13))
        gradient = QLinearGradient(scan.left() + 44, 0, scan.left() + 132, 0)
        gradient.setColorAt(0.0, holo.alpha(holo.HOLO, 0.0))
        gradient.setColorAt(0.5, QColor(holo.HOLO))
        gradient.setColorAt(1.0, holo.alpha(holo.HOLO, 0.0))
        painter.fillRect(QRectF(scan.left() + 44, scan.top(), 88, 1), gradient)   # still: only the orb loops

    def _paint_page(self, painter: QPainter, left: float, width: float) -> None:
        font = holo.font("caption")
        metrics = QFontMetricsF(font)
        line_h = holo.line_height("caption")
        lines = self._lines(font, width)
        top = (self.height() - line_h * len(lines)) / 2
        now = time.monotonic()
        still = holo.reduced_motion()
        marks = self._highlights()
        newest = len(self._revealed_at) - 1
        swap = 1.0
        if self._swap_from and not still:
            swap = _ease_out((now - self._swap_from) / (SWAP_OUT_MS / 1000.0))
        painter.setFont(font)
        space = metrics.horizontalAdvance(" ")
        for row, indices in enumerate(lines):
            line_w = sum(metrics.horizontalAdvance(self._words[i]) for i in indices) + space * (len(indices) - 1)
            x = left + (width - line_w) / 2
            baseline = top + row * line_h + (line_h - metrics.height()) / 2 + metrics.ascent()
            for i in indices:
                word = self._words[i]
                w = metrics.horizontalAdvance(word)
                if i < len(self._revealed_at):
                    p = 1.0 if still else (now - self._revealed_at[i]) / (REVEAL_MS / 1000.0)
                    opacity = _ease_out(p) * self._fade * swap
                    rise = 0.0 if still else RISE_PX * (1.0 - _ease_out(p))
                    color = QColor(holo.HOLO if i in marks else holo.INK_1)
                    if i != newest and i not in marks:
                        color.setAlphaF(0.9)
                    painter.setOpacity(max(0.0, min(1.0, opacity)))
                    painter.setPen(color)
                    painter.drawText(QPointF(x, baseline + rise), word)
                x += w + space
        painter.setOpacity(1.0)


# --------------------------------------------------------------------------
# Header (spec 11.2)
# --------------------------------------------------------------------------

HEADER_HEIGHT = holo.HEADER_HEIGHT
TONE_HOLO = "holo"
TONE_AMBER = "amber"
TONE_STEEL = "steel"
_TONE_DOTS = {TONE_HOLO: holo.HOLO, TONE_AMBER: holo.AMBER, TONE_STEEL: holo.STEEL}
_WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
                "October", "November", "December")
MUTE_LABEL = "Mute Jarvis"
MINIMIZE_LABEL = "Minimise"
CLOSE_LABEL = "Done, close Jarvis"


def clock_texts(moment: datetime, hour24: bool = False) -> tuple[str, str]:
    """("9:51 AM", "MON 12 OCT") or ("21:51", ...)."""
    if hour24:
        time_text = f"{moment.hour:02d}:{moment.minute:02d}"
    else:
        hour = moment.hour % 12 or 12
        time_text = f"{hour}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"
    return time_text, f"{_WEEKDAYS[moment.weekday()]} {moment.day:02d} {_MONTHS[moment.month - 1]}"


class _StateReadout(QWidget):
    """The 7 px dot and the mono state word (a polite status for screen readers)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._word = orb_mod.READOUTS[orb_mod.IDLE]
        self._tone = TONE_HOLO
        self.setAccessibleName(self._word)
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)

    def set_word(self, word: str, tone: str = TONE_HOLO) -> None:
        if (word, tone) == (self._word, self._tone):
            return
        self._word, self._tone = word, tone if tone in _TONE_DOTS else TONE_HOLO
        self.setAccessibleName(word)
        self.updateGeometry()
        self.update()

    def word(self) -> str:
        return self._word

    def tone(self) -> str:
        return self._tone

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        metrics = QFontMetricsF(holo.font("label"))
        return QSize(math.ceil(15 + metrics.horizontalAdvance(self._word)) + 2, 20)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(15, 20)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(_TONE_DOTS[self._tone]))
            painter.drawEllipse(QPointF(3.5, self.height() / 2), 3.5, 3.5)
            font = holo.font("label")
            painter.setFont(font)
            painter.setPen(QColor(holo.INK_KICKER))
            text = QFontMetricsF(font).elidedText(self._word, Qt.TextElideMode.ElideRight, self.width() - 15)
            painter.drawText(QRectF(15, 0, self.width() - 15, self.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
        finally:
            painter.end()


SERVICE_WARN = "warn"        # hud.STATUS_WARN / STATUS_ERROR (the service chips' statuses)
SERVICE_ERROR = "error"
_SERVICE_DOTS = {SERVICE_WARN: holo.STEEL, SERVICE_ERROR: holo.ALERT}


class _ServiceNote(QWidget):
    """A service that is not OK: a 7 px dot (steel: a problem that may pass; coral: an error) and its
    mono name (the dot alone in a header too narrow for it); the service chip's text is its tooltip and
    accessible name."""

    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._name = name
        self._word = name.upper()
        self._status = SERVICE_WARN
        self._compact = False
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_compact(self, compact: bool) -> None:
        if bool(compact) != self._compact:
            self._compact = bool(compact)
            self.updateGeometry()
            self.update()

    def is_compact(self) -> bool:
        return self._compact

    def set_status(self, status: str, tooltip: str) -> None:
        self._status = status if status in _SERVICE_DOTS else SERVICE_WARN
        text = tooltip or f"{self._word.title()}: a problem"
        self.setToolTip(text)
        self.setAccessibleName(text)
        self.update()

    def status(self) -> str:
        return self._status

    def word(self) -> str:
        return self._word

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        if self._compact:
            return QSize(9, 20)
        metrics = QFontMetricsF(holo.font("label"))   # + room for the letter spacing at 150 % and up
        return QSize(math.ceil(13 + metrics.horizontalAdvance(self._word)) + 6, 20)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(_SERVICE_DOTS[self._status]))
            painter.drawEllipse(QPointF(3.5, self.height() / 2), 3.5, 3.5)
            if self._compact:
                return
            painter.setFont(holo.font("label"))
            painter.setPen(QColor(holo.INK_KICKER))
            painter.drawText(QRectF(13, 0, self.width() - 13, self.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self._word)
        finally:
            painter.end()


class _HoldNote(QWidget):
    """A Send's or Cancel's hidden hold out of its card's sight: the confirmed dot and "SENT" (or
    "CANCELLED"), never a number (the owner's decision of 2026-10-17: no countdown anywhere). Screen readers
    hear it once: "Sent. Undo is available."."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._word = "SENT"
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_word(self, word: str) -> None:
        self._word = (word or "Sent").upper()
        self.setAccessibleName(f"{self._word.capitalize()}. Undo is available.")
        self.updateGeometry()
        self.update()

    def text(self) -> str:
        return self._word

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        metrics = QFontMetricsF(holo.font("label"))
        return QSize(math.ceil(13 + metrics.horizontalAdvance(self._word)) + 6, 20)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(holo.OK))
            painter.drawEllipse(QPointF(3.5, self.height() / 2), 3.5, 3.5)
            painter.setFont(holo.font("label"))
            painter.setPen(QColor(holo.OK))
            painter.drawText(QRectF(13, 0, self.width() - 13, self.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self._word)
        finally:
            painter.end()


class _ClockButton(QAbstractButton):
    """The header clock: time (readout-lg) over the date (label, ``ink-4``); opens the Day view."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._texts = ("", "")
        self._hover = False
        self._focus_ring = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_texts(self, time_text: str, date_text: str, name: str) -> None:
        if (time_text, date_text) != self._texts:
            self._texts = (time_text, date_text)
            self.updateGeometry()
            self.update()
        self.setAccessibleName(name)

    def texts(self) -> tuple[str, str]:
        return self._texts

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        time_w = QFontMetricsF(holo.font("readout_lg")).horizontalAdvance(self._texts[0] or "12:00 PM")
        date_w = QFontMetricsF(holo.font("label")).horizontalAdvance(self._texts[1] or "MON 12 OCT")
        return QSize(math.ceil(max(time_w, date_w)) + 12, 44)

    def enterEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().enterEvent(event)
        self._hover = True
        self.update()

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().leaveEvent(event)
        self._hover = False
        self.update()

    def focusInEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._focus_ring = holo.keyboard_focus(event.reason(), self._focus_ring)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.reason() != Qt.FocusReason.ActiveWindowFocusReason:
            self._focus_ring = False
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
            rect = QRectF(self.rect())
            if self._hover:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(holo.alpha(holo.INK_1, 0.05))
                painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 8, 8)
            right = rect.right() - 6
            painter.setFont(holo.font("readout_lg"))
            painter.setPen(QColor(holo.INK_1))
            painter.drawText(QRectF(rect.left(), rect.top() + 2, right - rect.left(), 24),
                             int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), self._texts[0])
            painter.setFont(holo.font("label"))
            painter.setPen(QColor(holo.INK_4))
            painter.drawText(QRectF(rect.left(), rect.top() + 26, right - rect.left(), 16),
                             int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), self._texts[1])
            if self._focus_ring and self.hasFocus():
                holo.paint_focus_ring(painter, rect.adjusted(3, 3, -3, -3), 8)
        finally:
            painter.end()


class _HeaderIcon(holo.HoloButton):
    """A 34 px round header button; ``pressed`` (the mute) draws the glyph steel on a steel border."""

    def __init__(self, label: str, icon: str, parent: QWidget | None = None) -> None:
        super().__init__(label, holo.ICON, icon=icon, diameter=34, parent=parent)
        self._pressed_look = False

    def set_pressed_look(self, pressed: bool) -> None:
        self._pressed_look = bool(pressed)
        self.update()

    def colors(self) -> tuple[QColor, QColor, QColor]:
        fill, border, label = super().colors()
        if self._pressed_look:
            return fill, holo.alpha(holo.STEEL, 0.6), QColor(holo.STEEL)
        return fill, border, label


class _Divider(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(13, 30)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.fillRect(QRectF(6, 0, 1, self.height()), holo.LINE)
        painter.end()


class HoloHeader(QWidget):
    """The main window's 52 px title bar; see the module docstring."""

    clockClicked = Signal()
    findClicked = Signal()               # the "Find or ask anything" pill (Ctrl+K does the same)
    undoClicked = Signal()               # the hold's Undo (while ``set_hold`` shows one)
    muteClicked = Signal()
    minimizeClicked = Signal()
    closeClicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(HEADER_HEIGHT)
        self._now: Callable[[], datetime] = datetime.now
        self._hour24 = False
        self._drag: QPoint | None = None
        self._wordmark_text = holo.role_text("wordmark", "J.A.R.V.I.S.")
        self.wordmark = QLabel(self._wordmark_text)
        self.wordmark.setFont(holo.font("wordmark"))
        holo_label_color(self.wordmark, holo.INK_1)
        self.wordmark.setAccessibleName("Jarvis")
        self._typing = QVariantAnimation(self)
        self._typing.setStartValue(0.0)
        self._typing.setEndValue(float(holo.STARTUP_TYPE_DELAY_MS + holo.STARTUP_TYPE_MS))
        self._typing.setDuration(holo.STARTUP_TYPE_DELAY_MS + holo.STARTUP_TYPE_MS)
        self._typing.valueChanged.connect(self._on_typing)
        self._typing.finished.connect(self._end_typing)
        self.find_button = find_view.FindButton()
        self.find_button.clicked.connect(self.findClicked)
        self.readout = _StateReadout()
        self._services: dict[str, _ServiceNote] = {}
        self._service_row = QHBoxLayout()
        self._service_row.setContentsMargins(12, 0, 0, 0)      # apart from the state word
        self._service_row.setSpacing(12)
        self.hold = _HoldNote()
        self.hold.hide()
        # Beside the mint SENT: a holo link (amber only when a decision waits, spec 6 / decision 0.2).
        self.undo_button = holo.HoloButton("Undo", holo.LINK_CYAN, size=holo.SIZE_SM)
        self.undo_button.setAccessibleName("Undo, nothing is sent")
        self.undo_button.clicked.connect(self.undoClicked)
        self.undo_button.hide()
        self.clock = _ClockButton()
        self.clock.clicked.connect(self.clockClicked)
        self.mute_button = _HeaderIcon(MUTE_LABEL, "speaker")
        self.mute_button.setCheckable(False)
        self.mute_button.clicked.connect(self.muteClicked)
        self.mute_button.hide()
        self.minimize_button = _HeaderIcon(MINIMIZE_LABEL, "minimize")
        self.minimize_button.clicked.connect(self.minimizeClicked)
        self.close_button = _HeaderIcon(CLOSE_LABEL, "close")
        self.close_button.clicked.connect(self.closeClicked)
        self._divider = _Divider()
        row = QHBoxLayout(self)
        row.setContentsMargins(holo.GUTTER_SIDE, 0, holo.GUTTER_SIDE - holo.HoloButton.MARGIN, 0)
        row.setSpacing(0)
        row.addWidget(self.wordmark, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addSpacing(16)
        row.addWidget(self.readout, 1, Qt.AlignmentFlag.AlignVCenter)
        row.addLayout(self._service_row)
        row.addSpacing(12)
        row.addWidget(self.hold, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.undo_button, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addSpacing(12)
        row.addWidget(self.find_button, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addSpacing(8)
        row.addWidget(self.clock, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._divider, 0, Qt.AlignmentFlag.AlignVCenter)
        for button in (self.mute_button, self.minimize_button, self.close_button):
            row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.refresh_clock)
        self.refresh_clock()

    # ---- state readout ----------------------------------------------------------------------

    def set_state_word(self, word: str, tone: str = TONE_HOLO) -> None:
        self.readout.set_word(word, tone)
        self._fit_notes()

    # ---- the start-up: J.A.R.V.I.S. types in ---------------------------------------------------

    def type_wordmark(self) -> None:
        """The wordmark types in, letter by letter (motion "lively" only; never changes the layout)."""
        if holo.motion_level() != holo.MOTION_LIVELY:
            return
        self.wordmark.setMinimumWidth(self.wordmark.sizeHint().width())
        self._typing.stop()
        self.wordmark.setText("")
        self._typing.start()

    def wordmark_typing(self) -> bool:
        return self._typing.state() == QVariantAnimation.State.Running

    def wordmark_shown(self) -> str:
        return self.wordmark.text()

    def _on_typing(self, value: Any) -> None:
        share = (float(value) - holo.STARTUP_TYPE_DELAY_MS) / holo.STARTUP_TYPE_MS
        count = max(0, min(len(self._wordmark_text), math.floor(share * len(self._wordmark_text) + 1e-6)))
        text = self._wordmark_text[:count]
        if text != self.wordmark.text():
            self.wordmark.setText(text)

    def _end_typing(self) -> None:
        self._typing.stop()
        self.wordmark.setText(self._wordmark_text)
        self.wordmark.setMinimumWidth(0)

    def state_word(self) -> str:
        return self.readout.word()

    # ---- services that are not OK (spec 11.7 moves them to error tiles: round 2) ---------------------

    def set_service(self, name: str, status: str, tooltip: str = "") -> None:
        """A service chip's state (notion, voice, calendar, claude): a note while it is a warning or an
        error, none while it is OK or off."""
        note = self._services.get(name)
        if status not in _SERVICE_DOTS:
            if note is not None:
                note.hide()
            return
        if note is None:
            note = self._services[name] = _ServiceNote(name)
            self._service_row.addWidget(note, 0, Qt.AlignmentFlag.AlignVCenter)
        note.set_status(status, tooltip)
        note.show()
        self._fit_notes()

    def set_hold(self, word: str = "") -> None:
        """A Send's or Cancel's hidden hold whose card is out of sight: "Sent" (``word``: "Sent" /
        "Cancelled") and an Undo link; "" hides both. Never a number."""
        shown = bool(word)
        if shown:
            self.hold.set_word(word)
        self.hold.setVisible(shown)
        self.undo_button.setVisible(shown)
        self._fit_notes()

    def _fit_notes(self) -> None:
        """Too narrow for everything (the smallest window, a hold showing): first the Find pill folds to its
        round search button, then the notes show their dots only (their names stay as long as they fit)."""
        notes = [note for note in self._services.values() if not note.isHidden()]
        layout = self.layout()
        for compact_notes, compact_find in ((False, False), (False, True), (True, True)):
            if compact_notes and not notes:
                return
            for note in notes:
                note.set_compact(compact_notes)
            self.find_button.set_compact(compact_find)
            layout.invalidate()
            if layout.minimumSize().width() <= self.width():
                return

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._fit_notes()

    def hold_text(self) -> str:
        """What the header's hold note says ("SENT", "CANCELLED"; "" while none shows)."""
        return self.hold.text() if not self.hold.isHidden() else ""

    def service_notes(self) -> list[tuple[str, str, str]]:
        """(name, status, tooltip) of the notes shown, in the order they first appeared."""
        return [(name, note.status(), note.toolTip()) for name, note in self._services.items() if not note.isHidden()]

    # ---- clock ---------------------------------------------------------------------------------

    def set_clock(self, now: Callable[[], datetime]) -> None:
        self._now = now
        self.refresh_clock()

    def set_hour24(self, hour24: bool) -> None:
        self._hour24 = bool(hour24)
        self.refresh_clock()

    def refresh_clock(self) -> None:
        try:
            moment = self._now()
        except Exception:  # noqa: BLE001 - a broken clock keeps the last texts
            return
        time_text, date_text = clock_texts(moment, self._hour24)
        name = (f"{time_text}, {_WEEKDAY_NAMES[moment.weekday()]} {moment.day} {_MONTH_NAMES[moment.month - 1]}. "
                "Open the day view")
        self.clock.set_texts(time_text, date_text, name)
        if self.isVisible():
            self._timer.start(max(1000, (60 - moment.second) * 1000))

    def clock_texts(self) -> tuple[str, str]:
        return self.clock.texts()

    # ---- mute ------------------------------------------------------------------------------------

    def set_mute_visible(self, visible: bool) -> None:
        self.mute_button.setVisible(bool(visible))

    def mute_visible(self) -> bool:
        return not self.mute_button.isHidden()

    def set_muted(self, muted: bool) -> None:
        self.mute_button.set_icon_name("speaker_muted" if muted else "speaker")
        self.mute_button.set_pressed_look(muted)
        self.mute_button.setAccessibleDescription("Pressed: Jarvis's own voice is off" if muted else "")

    def muted(self) -> bool:
        return self.mute_button._pressed_look

    # ---- Qt ---------------------------------------------------------------------------------------

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self.refresh_clock()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self._timer.stop()
        if self.wordmark_typing():
            self._end_typing()

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
            self._drag = event.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._drag = None
        super().mouseReleaseEvent(event)


def holo_label_color(label: QLabel, color: str | QColor) -> None:
    palette = label.palette()
    palette.setColor(label.foregroundRole(), QColor(color))
    label.setPalette(palette)


# --------------------------------------------------------------------------
# The stage (spec 16.0)
# --------------------------------------------------------------------------

STAGE_BRACKET = 18
ORB_MIN = 200
SIDE_GAP = 40              # between the orb and its side tile (spec 16.0)


class HomeStage(QWidget):
    """The hero orb, the stage brackets, the hairline and the Transcript button; see the module docs."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.hero = orb_mod.OrbWidget(orb_mod.HERO, self)
        self.transcript_button = holo.HoloButton("Transcript", holo.ICON, icon="transcript", diameter=34,
                                                 parent=self)
        self._unread = False
        self._side: QWidget | None = None

    def set_side_tile(self, tile: QWidget | None) -> None:
        """The one tile bottom-right of the orb (an offer: offer_view.OfferTile; an error: states_view.ErrorTile),
        tethered to it; the hero gives it room (220 px at least) while it shows. None: no tile."""
        if self._side is not None and self._side is not tile and shiboken6.isValid(self._side):
            self._side.removeEventFilter(self)
        self._side = tile
        if tile is not None:
            if tile.parentWidget() is not self:
                tile.setParent(self)        # (a new parent hides it; a tile already here keeps its look)
            tile.installEventFilter(self)
        self._relayout()

    def side_tile(self) -> QWidget | None:
        return self._side

    def _side_shown(self) -> bool:
        side = self._side
        return side is not None and shiboken6.isValid(side) and not side.isHidden()

    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802 - Qt override
        if watched is self._side and event.type() in (QEvent.Type.Show, QEvent.Type.Hide):
            QTimer.singleShot(0, self._relayout_if_valid)
        return super().eventFilter(watched, event)

    def place_side_tile(self) -> None:
        """The side tile changed what it shows (its height): place it again."""
        self._relayout()

    def _relayout_if_valid(self) -> None:
        if shiboken6.isValid(self):
            self._relayout()

    def _relayout(self) -> None:
        px = self.orb_px()
        if px != self.hero.orb_size():
            self.hero.set_orb_size(px)
        self._place_hero()
        self.update()

    def _place_side(self) -> None:
        """The side tile: its top on the orb's centre line, 40 px right of the orb (220-290 wide); on a
        narrow stage it keeps inside the stage."""
        if not self._side_shown():
            return
        tile = self._side
        assert tile is not None
        center = QRectF(self.hero.geometry()).center()
        radius = self.hero.orb_size() / 2
        room = self.width() - (center.x() + radius + SIDE_GAP) - 4
        width = offer_view.tile_width(room)
        height = tile.heightForWidth(width) if tile.hasHeightForWidth() else tile.sizeHint().height()
        x = min(center.x() + radius + SIDE_GAP, self.width() - 4 - width)
        y = max(4.0, min(center.y() - 12, self.height() - 4 - height))
        geometry = QRect(round(x), round(y), width, height)
        if tile.geometry() != geometry:
            tile.setGeometry(geometry)
        tile.raise_()

    def _paint_tether(self, painter: QPainter) -> None:
        """The amber tether from the side tile's kicker to the orb's rim, with its 5 px dot (spec 5, 13)."""
        assert self._side is not None
        tile = QRectF(self._side.geometry())
        center = QRectF(self.hero.geometry()).center()
        start = QPointF(tile.left(), tile.top() + holo.TILE_PAD[0] + holo.line_height("label") / 2)
        dx, dy = center.x() - start.x(), center.y() - start.y()
        distance = math.hypot(dx, dy)
        radius = self.hero.orb_size() / 2 + 4
        if distance <= radius:
            return
        end = QPointF(center.x() - dx / distance * radius, center.y() - dy / distance * radius)
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(holo.alpha(holo.AMBER, 0.45), 1.0))
            painter.drawLine(start, end)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(holo.AMBER))
            painter.drawEllipse(end, holo.TETHER_DOT / 2, holo.TETHER_DOT / 2)
        finally:
            painter.restore()

    def set_unread(self, unread: bool) -> None:
        """A cyan dot on the Transcript button: something new was added while the drawer was closed."""
        self._unread = bool(unread)
        self.transcript_button.setAccessibleName("Transcript, new" if unread else "Transcript")
        self.update()

    def unread(self) -> bool:
        return self._unread

    def orb_px(self, size: QSize | None = None) -> int:
        """The hero's size for the stage: at most 320, at least 200 (its widget is a multiple of 4 px wide:
        placed by ``_place_hero`` it covers whole device pixels at 125-175 % too)."""
        size = size or self.size()
        room = min(size.width() - 2 * STAGE_BRACKET, size.height() - 8)
        px = min(orb_mod.HERO, room)
        if self._side_shown():      # room for the side tile on both sides, so the orb stays centred
            px = min(px, size.width() - 2 * (STAGE_BRACKET + offer_view.TILE_MIN + SIDE_GAP))
        while px > ORB_MIN and orb_mod.widget_side(px) > room:
            px -= 2
        return max(ORB_MIN, px)

    def _place_hero(self) -> None:
        """Centre the hero, its window position on a multiple of 4 px (whole device pixels)."""
        side = self.hero.width()
        origin = self.mapTo(self.window(), QPoint(0, 0))

        def snap(local: float, base: int) -> int:
            return int(round((base + local) / 4.0)) * 4 - base

        x = snap((self.width() - side) / 2, origin.x())
        y = snap((self.height() - side) / 2, origin.y())
        if (x, y) != (self.hero.x(), self.hero.y()):
            self.hero.move(x, y)
        self._place_side()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._relayout()
        QTimer.singleShot(0, self._place_hero_if_valid)      # the stage's own position settles after this
        button = self.transcript_button
        button.resize(button.sizeHint())
        button.move(0, self.height() - button.height())

    def moveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().moveEvent(event)
        self._place_hero()

    def _place_hero_if_valid(self) -> None:
        if shiboken6.isValid(self):
            self._place_hero()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(orb_mod.widget_side(ORB_MIN) + 2 * STAGE_BRACKET, orb_mod.widget_side(ORB_MIN) + 8)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        side = orb_mod.widget_side(orb_mod.HERO)
        return QSize(side + 2 * STAGE_BRACKET, side + 8)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            pen = QPen(holo.alpha(holo.HOLO, 0.33), 1.0)
            painter.setPen(pen)
            leg = STAGE_BRACKET
            for x, y, dx, dy in ((rect.left(), rect.top(), 1, 1), (rect.right(), rect.top(), -1, 1),
                                 (rect.left(), rect.bottom(), 1, -1), (rect.right(), rect.bottom(), -1, -1)):
                if dy < 0 and dx > 0:
                    continue          # the Transcript button sits in the bottom-left corner
                painter.drawLine(QPointF(x, y), QPointF(x + dx * leg, y))
                painter.drawLine(QPointF(x, y), QPointF(x, y + dy * leg))
            cy = self.hero.geometry().center().y() + 0.5
            hairline = QLinearGradient(rect.left() + 28, 0, rect.right() - 28, 0)
            line = holo.alpha(holo.HOLO, 0.12)
            hairline.setColorAt(0.0, holo.alpha(holo.HOLO, 0.0))
            hairline.setColorAt(0.16, line)
            hairline.setColorAt(0.84, line)
            hairline.setColorAt(1.0, holo.alpha(holo.HOLO, 0.0))
            painter.fillRect(QRectF(rect.left() + 28, cy, rect.width() - 56, 1), hairline)
            if self._side_shown():
                self._paint_tether(painter)
            if self._unread:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                b = QRectF(self.transcript_button.geometry())
                holo.paint_new_dot(painter, QPointF(b.right() - 9, b.top() + 9), ring=holo.VOID)
        finally:
            painter.end()


class EchoPill(QWidget):
    """``YOU`` + the owner's request (mono 13), under the orb while it is handled."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = ""
        self.setFixedHeight(28)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def set_text(self, text: str) -> None:
        self._text = " ".join((text or "").split())
        self.setAccessibleName(f"You: {self._text}" if self._text else "")
        self.updateGeometry()
        self.update()

    def text(self) -> str:
        return self._text

    def _mono(self) -> QFont:
        font = holo.font("readout")
        font.setCapitalization(QFont.Capitalization.MixedCase)
        font.setPixelSize(13)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.26)
        return font

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        label_w = QFontMetricsF(holo.font("label")).horizontalAdvance("YOU")
        text_w = QFontMetricsF(self._mono()).horizontalAdvance(self._text)
        return QSize(math.ceil(5 + 20 + 10 + label_w + 10 + text_w + 14) + 6, 28)   # + rounding slack

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(80, 28)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            painter.setPen(QPen(holo.alpha(holo.HOLO, 0.12), 1.0))
            painter.setBrush(holo.alpha("#080E13", 0.65))
            painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
            tick = QPointF(rect.left() + 5 + 10, rect.center().y())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(holo.alpha(holo.HOLO, 0.12))
            painter.drawEllipse(tick, 10, 10)
            holo.paint_icon(painter, QRectF(tick.x() - 6, tick.y() - 6, 12, 12), "check", QColor(holo.HOLO),
                            stroke=2.6)
            x = tick.x() + 10 + 10
            label_font = holo.font("label")
            painter.setFont(label_font)
            painter.setPen(QColor(holo.INK_4))
            label_w = QFontMetricsF(label_font).horizontalAdvance("YOU")
            painter.drawText(QRectF(x, rect.top(), label_w + 2, rect.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), "YOU")
            x += label_w + 10
            mono = self._mono()
            painter.setFont(mono)
            painter.setPen(QColor(holo.INK_2))
            text = QFontMetricsF(mono).elidedText(self._text, Qt.TextElideMode.ElideRight, rect.right() - 14 - x)
            painter.drawText(QRectF(x, rect.top(), rect.right() - 14 - x, rect.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
        finally:
            painter.end()


# --------------------------------------------------------------------------
# Drawers (spec 11.4, 11.5)
# --------------------------------------------------------------------------

DRAWER_WIDTH = 420
DRAWER_RADIUS = 14


class Drawer(QWidget):
    """A panel sliding in from the right edge of its parent; see the module docstring."""

    closed = Signal()

    def __init__(self, title: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAutoFillBackground(False)
        self.setAccessibleName(title)
        self._opener: QWidget | None = None
        self._title = QLabel(holo.role_text("label", title))
        self._title.setFont(holo.font("label"))
        holo_label_color(self._title, holo.INK_KICKER)
        self.close_button = holo.HoloButton(f"Close {title.lower()}", holo.ICON, icon="close", diameter=34)
        self.close_button.clicked.connect(self.close_drawer)
        head = QHBoxLayout()
        head.setContentsMargins(20, 10, 12, 4)
        head.addWidget(self._title, 1, Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.body_layout = QVBoxLayout()
        self.body_layout.setContentsMargins(8, 0, 8, 12)
        self.body_layout.setSpacing(0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(head)
        layout.addLayout(self.body_layout, 1)
        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.finished.connect(self._on_anim_finished)
        self._closing = False
        self.hide()

    def is_open(self) -> bool:
        return self.isVisible() and not self._closing

    def _target(self) -> QRect:
        host = self.parentWidget()
        width = min(DRAWER_WIDTH, max(280, host.width() - 80))
        top = HEADER_HEIGHT
        return QRect(host.width() - width, top, width, host.height() - top)

    def place(self) -> None:
        if self.isVisible() and not self._closing and self._anim.state() != QPropertyAnimation.State.Running:
            self.setGeometry(self._target())

    def open_drawer(self, opener: QWidget | None = None) -> None:
        """Slide in (the owner's own click) and take the focus to the close button."""
        if opener is not None:
            self._opener = opener
        target = self._target()
        self._closing = False
        self._anim.stop()
        self.raise_()
        if holo.reduced_motion():
            self.setGeometry(target)
            self.show()
        else:
            start = QPoint(target.x() + target.width(), target.y())
            self.setGeometry(QRect(start, target.size()))
            self.show()
            self._anim.setDuration(holo.DRAWER_IN_MS)
            self._anim.setEasingCurve(holo.qt_easing(holo.EASE_OUT))
            self._anim.setStartValue(start)
            self._anim.setEndValue(target.topLeft())
            self._anim.start()
        self.close_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def close_drawer(self) -> None:
        if not self.isVisible() or self._closing:
            return
        self._closing = True
        self._anim.stop()
        if holo.reduced_motion():
            self._finish_close()
            return
        self._anim.setDuration(holo.DRAWER_OUT_MS)
        self._anim.setEasingCurve(holo.qt_easing(holo.EASE_IN))
        self._anim.setStartValue(self.pos())
        self._anim.setEndValue(QPoint(self.parentWidget().width(), self.y()))
        self._anim.start()

    def _on_anim_finished(self) -> None:
        if self._closing:
            self._finish_close()

    def _finish_close(self) -> None:
        had_focus = self.isAncestorOf(self.focusWidget()) if self.focusWidget() is not None else False
        self.hide()
        self._closing = False
        opener = self._opener
        if had_focus and opener is not None and shiboken6.isValid(opener) and opener.isVisible():
            opener.setFocus(Qt.FocusReason.OtherFocusReason)
        self.closed.emit()

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self.close_drawer()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, 8.0, -0.5)      # square right edge (off the side)
            path = QPainterPath()
            path.addRoundedRect(rect, DRAWER_RADIUS, DRAWER_RADIUS)
            painter.fillPath(path, QColor(holo.RAISED))
            painter.setPen(QPen(holo.LINE_STRONG, 1.0))
            painter.drawPath(path)
        finally:
            painter.end()
