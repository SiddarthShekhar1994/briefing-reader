"""The hologram's states (HOLOGRAM_SPEC 12.6, 16.7; round 2, part 3b): their words, the error tile and the toast.

Nothing here reads, sends or runs anything: every widget only shows what the app hands it, and every
action only emits a signal the app answers by opening a screen it already has.

Public API
==========

Words (plain text; British spelling; never a vendor name in what Jarvis says)
    ``waiting_line(run, address)``          "Checking for your AM briefing, sir."
    ``waiting_word(run)``                   "WAITING FOR THE AM BRIEFING" (the header readout)
    ``waiting_readout(interval, until)``    "EVERY MINUTE \u00b7 UNTIL 10:27 AM" (when Jarvis checks)
    ``OFFLINE_LINE`` / ``offline_line(address)``   "My usual voice is out of reach, sir. I'll keep trying."
    ``offline_badge(retry)``                "WINDOWS VOICE" or "WINDOWS VOICE \u00b7 RETRY 10:14"
    ``quiet_word(until)``                   "QUIET HOURS UNTIL 8:00 AM"
    ``clock_text(moment, hour24)``          "8:00 AM" / "08:00"
    ``toast_briefing_line(run, address)``   "Your AM briefing is ready, sir."
    ``toast_prep_line(title, minutes, address)``   "Project Development check-in in 15 minutes, sir."
    Toast lines hold at most ``TOAST_WORDS`` words; the address closes them, never opens them.

``QuietReadout``
    One quiet mono line (label type, ``ink-4``), centred: the waiting readout under Home's caption.

``StateBadge``
    The steel status badge (spec 7: 20 high, radius 4, steel border 45 %, fill 8 %), centred: the offline
    voice's ``WINDOWS VOICE`` under Home's caption.

``ErrorInfo`` / ``ErrorTile``
    An error at Home's focal point (beside the orb, where an offer's tile sits): the error look (coral
    brackets), the warning glyph and a kicker word, ONE sentence and ONE action (an amber button: the
    owner must act). ``set_error(info)`` (None hides it); ``actionClicked(key)``. An action whose ``help`` is
    set (How to fix) shows that help text in the tile (the existing help, nothing is opened or run); any
    other action is the app's to answer (Retry: the same Retry as before; it never sends by itself).

``Toast``
    Jarvis's own small window (TOAST_SIZE, ``raised``, radius 14, bottom-right of the work area, on top) -
    never a Windows notification, so nothing lands in Windows' notification history. It never takes the
    focus (``Qt.Tool``, ``WindowDoesNotAcceptFocus``, ``WA_ShowWithoutActivating``; its button takes no
    focus either). The mini orb, one line and ONE action. ``post(line, action, key, look=)`` shows it;
    it hides after ``TOAST_MS`` (hovering pauses the time; leaving gives the rest back); ``actionClicked(key)``
    after a click on its action (the toast hides first). The toast never acts by itself: the app only
    brings an existing screen forward.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, time as clock
from typing import Any

import shiboken6
from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import holo, hud, offer_view
from . import orb as orb_mod
from .persona import with_address

DOT = "\u00b7"
TOAST_WORDS = 8
TOAST_MS = 8000
TOAST_SIZE = QSize(360, 88)
TOAST_RADIUS = 14
TOAST_MARGIN = 16                 # from the work area's bottom-right corner
TOAST_LINES = 3                   # the line wraps beside the action (8 words fit)
OFFLINE_LINE = "My usual voice is out of reach, {sir}. I'll keep trying."
OFFLINE_BADGE = "WINDOWS VOICE"
QUIET_DIM = 0.7                   # the idle orb in quiet hours (spec 16.7)
VIEW_TEXT = "View"
PREP_TEXT = "Prepare brief"
RETRY_TEXT = "Retry"
HOW_TO_FIX_TEXT = "How to fix"
HIDE_HELP_TEXT = "Hide the fix"
_GLYPH = 14
_GLYPH_GAP = 6
_ACTION_GAP = 12
_HELP_LINES = 4


# --------------------------------------------------------------------------
# Words
# --------------------------------------------------------------------------

def _run_words(run: str | None) -> str:
    return f"{run} briefing" if run in ("AM", "PM") else "briefing"


def waiting_line(run: str | None, address: str = "sir") -> str:
    """"Checking for your AM briefing, sir." (spec 16.7; without a run label "your briefing")."""
    return with_address(f"Checking for your {_run_words(run)}, {{sir}}.", address)


def waiting_word(run: str | None) -> str:
    """The header readout while Jarvis waits: "WAITING FOR THE PM BRIEFING" (the orb's own word without a
    run label)."""
    if run in ("AM", "PM"):
        return f"WAITING FOR THE {run} BRIEFING"
    return orb_mod.READOUTS[orb_mod.WAITING]


def waiting_readout(interval: str, until: str) -> str:
    """"EVERY MINUTE \u00b7 UNTIL 10:27 AM": when Jarvis checks for it ("" when he does not poll)."""
    parts = [part.strip() for part in (interval, f"until {until}" if until else "") if part and part.strip()]
    return f" {DOT} ".join(parts).upper()


def offline_line(address: str = "sir") -> str:
    """"My usual voice is out of reach, sir. I'll keep trying." (no vendor names in Jarvis's lines)."""
    return with_address(OFFLINE_LINE, address)


def offline_badge(retry: str = "") -> str:
    """"WINDOWS VOICE", with " \u00b7 RETRY 10:14" when the app knows when the usual voice is tried again."""
    return f"{OFFLINE_BADGE} {DOT} RETRY {retry}".upper() if retry else OFFLINE_BADGE


def quiet_word(until: str) -> str:
    """The header readout in quiet hours: "QUIET HOURS UNTIL 8:00 AM"."""
    return f"QUIET HOURS UNTIL {until}".upper() if until else "QUIET HOURS"


def clock_text(moment: clock | datetime, hour24: bool = False) -> str:
    """"8:00 AM" (or "08:00")."""
    if hour24:
        return f"{moment.hour:02d}:{moment.minute:02d}"
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def _closed(words: list[str], address: str, keep: int = 0) -> str:
    """``words`` as one line closed by the address ("..., sir."), cut with an ellipsis so the whole line holds
    at most TOAST_WORDS words; the last ``keep`` words ("in 15 minutes") are never cut."""
    address_words = len(str(address or "").split())
    room = max(1 + keep, TOAST_WORDS - address_words)
    if len(words) > room:
        head, kept = words[:len(words) - keep], words[len(words) - keep:]
        head = head[:max(1, room - keep)]
        head[-1] = head[-1].rstrip(",.;:") + "\u2026"
        words = head + kept
    return " ".join(words) + with_address(", {sir}.", address)


def toast_briefing_line(run: str | None, address: str = "sir") -> str:
    """"Your AM briefing is ready, sir." (spec 16.7)."""
    return _closed(f"Your {_run_words(run)} is ready".split(), address)


def toast_prep_line(title: str, minutes: int, address: str = "sir") -> str:
    """"Project Development check-in in 15 minutes, sir." (a long title cut to fit TOAST_WORDS words)."""
    left = max(0, int(minutes))
    when = (f"in {left} minute{'' if left == 1 else 's'}" if left > 0 else "now").split()
    name = " ".join(str(title or "").split()) or "Your meeting"
    return _closed(name.split() + when, address, keep=len(when))


# --------------------------------------------------------------------------
# Quiet readout and the steel badge (Home's chip row)
# --------------------------------------------------------------------------

class QuietReadout(QWidget):
    """One centred mono readout in ``ink-4`` (label type, caps)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = ""
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.hide()

    def set_text(self, text: str) -> None:
        text = holo.role_text("label", text or "")
        if text != self._text:
            self._text = text
            self.setAccessibleName(text)
            self.updateGeometry()
            self.update()

    def text(self) -> str:
        return self._text

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        width = QFontMetricsF(holo.font("label")).horizontalAdvance(self._text)
        return QSize(math.ceil(width) + 8, holo.ActionChip.HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(40, holo.ActionChip.HEIGHT)

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            font = holo.font("label")
            painter.setFont(font)
            painter.setPen(QColor(holo.INK_4))
            text = QFontMetricsF(font).elidedText(self._text, Qt.TextElideMode.ElideRight, self.width() - 4)
            painter.drawText(QRectF(self.rect()), int(Qt.AlignmentFlag.AlignCenter), text)
        finally:
            painter.end()


class StateBadge(QWidget):
    """A centred status badge (steel by default): the offline voice's ``WINDOWS VOICE``."""

    def __init__(self, tone: str = holo.TONE_STEEL, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = ""
        self._tone = tone
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.hide()

    def set_text(self, text: str) -> None:
        if text != self._text:
            self._text = text or ""
            self.setAccessibleName(self._text)
            self.updateGeometry()
            self.update()

    def text(self) -> str:
        return self._text

    def tone(self) -> str:
        return self._tone

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(math.ceil(holo.status_badge_width(self._text)) + 4, holo.ActionChip.HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.sizeHint()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            width = holo.status_badge_width(self._text)
            holo.paint_status_badge(painter, QPointF((self.width() - width) / 2, self.height() / 2), self._text,
                                    self._tone)
        finally:
            painter.end()


# --------------------------------------------------------------------------
# The error tile (spec 5, 12.6, 16.7)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ErrorInfo:
    """One error as Home shows it: ``key`` (the app's: what the action answers), the ``kicker`` word, the ONE
    ``sentence``, the ONE ``action`` text and, for How to fix, the existing ``help`` text it shows."""

    key: str
    kicker: str
    sentence: str
    action: str
    help: str = ""


class ErrorTile(QWidget):
    """The error's tile; see the module docstring."""

    actionClicked = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._info: ErrorInfo | None = None
        self._help_shown = False
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.action_button = holo.HoloButton("", holo.AMBER_COMMIT, parent=self)
        self.action_button.clicked.connect(self._on_action)
        self.hide()

    # ---- what it shows -------------------------------------------------------------------------

    def set_error(self, info: ErrorInfo | None) -> None:
        """The error to show (None hides the tile). Another error starts with its help folded."""
        if info is None or self._info is None or info.key != self._info.key:
            self._help_shown = False
        self._info = info
        if info is None:
            self.hide()
            return
        self._sync_button()
        self.setAccessibleName(f"Problem: {info.sentence}")
        self.setAccessibleDescription(info.help if self._help_shown else "")
        self.updateGeometry()
        self._place_button()
        self.show()
        self.update()

    def info(self) -> ErrorInfo | None:
        return self._info

    def help_shown(self) -> bool:
        return self._help_shown

    def _sync_button(self) -> None:
        info = self._info
        if info is None:
            return
        text = HIDE_HELP_TEXT if info.help and self._help_shown else info.action
        self.action_button.setText(text)
        self.action_button.setAccessibleName(f"{text}: {info.sentence}")

    def texts(self, width: int | None = None) -> tuple[str, list[str], list[str]]:
        """(kicker, sentence lines, help lines) as drawn at ``width``."""
        info = self._info
        if info is None:
            return "", [], []
        inner = (width if width is not None else self.width()) - 2 * holo.TILE_PAD[1]
        kicker = QFontMetricsF(holo.font("label")).elidedText(holo.role_text("label", info.kicker),
                                                              Qt.TextElideMode.ElideRight, inner - _GLYPH - _GLYPH_GAP)
        sentence = offer_view.wrap_lines(info.sentence, holo.font("tile_title"), inner, 2)
        help_lines = (offer_view.wrap_lines(info.help, holo.font("body_sm"), inner, _HELP_LINES)
                      if info.help and self._help_shown else [])
        return kicker, sentence, help_lines

    # ---- geometry ------------------------------------------------------------------------------

    def height_for(self, width: int) -> int:
        _kicker, sentence, help_lines = self.texts(width)
        pad_v = holo.TILE_PAD[0]
        height = pad_v + holo.line_height("label") + holo.KICKER_GAP + len(sentence) * holo.line_height("tile_title")
        if help_lines:
            height += 4 + len(help_lines) * holo.line_height("body_sm")
        height += _ACTION_GAP + self.action_button.sizeHint().height() + pad_v - holo.HoloButton.MARGIN
        return height

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        return self.height_for(width)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(offer_view.TILE_MAX, self.height_for(offer_view.TILE_MAX))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(offer_view.TILE_MIN, self.height_for(offer_view.TILE_MIN))

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._place_button()

    def _place_button(self) -> None:
        margin = holo.HoloButton.MARGIN
        pad_h, pad_v = holo.TILE_PAD[1], holo.TILE_PAD[0]
        size = self.action_button.sizeHint()
        y = self.height() - pad_v + margin - size.height()
        right = self.width() - pad_h + margin
        self.action_button.setGeometry(right - size.width(), y, size.width(), size.height())

    # ---- the one action ------------------------------------------------------------------------

    def _on_action(self) -> None:
        info = self._info
        if info is None:
            return
        if info.help:
            # How to fix: the existing help text, in the tile (nothing is opened, run or changed).
            self._help_shown = not self._help_shown
            self._sync_button()
            self.setAccessibleDescription(info.help if self._help_shown else "")
            self.updateGeometry()
            parent = self.parentWidget()
            if parent is not None and hasattr(parent, "place_side_tile"):
                parent.place_side_tile()
            else:
                self.resize(self.width(), self.height_for(self.width()))
            self._place_button()
            self.update()
        self.actionClicked.emit(info.key)

    # ---- paint ---------------------------------------------------------------------------------

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        if self._info is None:
            return
        kicker, sentence, help_lines = self.texts()
        painter = QPainter(self)
        try:
            holo.paint_tile(painter, QRectF(self.rect()), holo.TILE_ERROR)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            pad_h, pad_v = holo.TILE_PAD[1], holo.TILE_PAD[0]
            inner = self.width() - 2 * pad_h
            y = float(pad_v)
            label_h = holo.line_height("label")
            holo.paint_icon(painter, QRectF(pad_h, y + (label_h - _GLYPH) / 2, _GLYPH, _GLYPH), "warning",
                            holo.ALERT_TX, stroke=2.0)
            painter.setFont(holo.font("label"))
            painter.setPen(QColor(holo.ALERT_TX))
            left = pad_h + _GLYPH + _GLYPH_GAP
            painter.drawText(QRectF(left, y, inner - _GLYPH - _GLYPH_GAP, label_h),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), kicker)
            y += label_h + holo.KICKER_GAP
            painter.setFont(holo.font("tile_title"))
            painter.setPen(QColor(holo.INK_1))
            line = holo.line_height("tile_title")
            for text in sentence:
                painter.drawText(QRectF(pad_h, y, inner, line),
                                 int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
                y += line
            if help_lines:
                y += 4
                painter.setFont(holo.font("body_sm"))
                painter.setPen(QColor(holo.INK_2))
                line = holo.line_height("body_sm")
                for text in help_lines:
                    painter.drawText(QRectF(pad_h, y, inner, line),
                                     int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
                    y += line
        finally:
            painter.end()


# --------------------------------------------------------------------------
# The toast (spec 16.7, 19)
# --------------------------------------------------------------------------

TOAST_FLAGS = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
               | Qt.WindowType.WindowDoesNotAcceptFocus)


class Toast(QWidget):
    """Jarvis's own toast window; see the module docstring."""

    actionClicked = Signal(str)

    def __init__(self) -> None:
        super().__init__(None, TOAST_FLAGS)
        self.setObjectName("JarvisToast")
        self.setWindowTitle("Jarvis")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(TOAST_SIZE)
        self._line = ""
        self._key = ""
        self._remaining_ms = TOAST_MS
        self._started = 0.0
        self._hovered = False
        self.mini = orb_mod.MiniOrb(self, ring_color=holo.RAISED)
        self.action_button = holo.HoloButton("", holo.GO, size=holo.SIZE_SM, parent=self)
        self.action_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # a click never moves the focus here
        self.action_button.clicked.connect(self._on_action)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._expire)
        self.setAccessibleName("Jarvis")

    # ---- API -------------------------------------------------------------------------------

    def post(self, line: str, action: str, key: str, *, look: tuple[str, bool, bool] | None = None) -> None:
        """Show ``line`` with its ONE action (``action`` text; ``key`` comes back in ``actionClicked``),
        bottom-right of the work area, without taking the focus; it hides after TOAST_MS."""
        self._line, self._key = " ".join(str(line or "").split()), key
        self.action_button.setText(action)
        self.action_button.setAccessibleName(action)
        self.setAccessibleDescription(self._line)
        base, muted, offline = look or (orb_mod.IDLE, False, False)
        self.mini.set_look(base, muted=muted, offline=offline)
        self._place_children()
        self.place()
        self._hovered = False
        self._remaining_ms = TOAST_MS
        self._start_timer()
        # Never the focus: the attribute covers the first show; once the native window exists Qt's Windows
        # plugin reads this property at each show (ui.show_without_activating says the same). On top by its
        # flag: no raise (it could bring the window forward on some systems).
        handle = self.windowHandle()
        if handle is not None:
            handle.setProperty("_q_showWithoutActivating", True)
        self.show()
        self.update()

    def line(self) -> str:
        return self._line

    def key(self) -> str:
        return self._key

    def action_text(self) -> str:
        return self.action_button.text()

    def remaining_ms(self) -> int:
        """What is left of the 8 s (paused while the pointer is over the toast)."""
        if self._timer.isActive():
            return max(0, self._timer.remainingTime())
        return self._remaining_ms

    def is_paused(self) -> bool:
        return self.isVisible() and not self._timer.isActive() and self._hovered

    def place(self) -> None:
        """Bottom-right of the primary screen's work area (above the taskbar)."""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        area: QRect = screen.availableGeometry()
        self.move(area.right() + 1 - TOAST_MARGIN - self.width(), area.bottom() + 1 - TOAST_MARGIN - self.height())

    def dismiss(self) -> None:
        self._timer.stop()
        self.hide()

    # ---- timer and hover -------------------------------------------------------------------

    def _start_timer(self) -> None:
        self._started = time.monotonic()
        self._timer.start(max(1, int(self._remaining_ms)))

    def _expire(self) -> None:
        if shiboken6.isValid(self):
            self._remaining_ms = 0
            self.hide()

    def pause(self) -> None:
        """The pointer is over the toast: the time stops."""
        self._hovered = True
        if self._timer.isActive():
            self._remaining_ms = max(1, self._timer.remainingTime())
            self._timer.stop()

    def resume(self) -> None:
        """The pointer left: the rest of the time runs."""
        self._hovered = False
        if self.isVisible() and not self._timer.isActive():
            self._start_timer()

    def enterEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().enterEvent(event)
        self.pause()

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().leaveEvent(event)
        self.resume()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._timer.stop()
        super().hideEvent(event)

    # ---- the one action --------------------------------------------------------------------

    def _on_action(self) -> None:
        key = self._key
        self.dismiss()
        if key:
            self.actionClicked.emit(key)

    # ---- layout and paint ------------------------------------------------------------------

    def _place_children(self) -> None:
        pad = 14
        self.mini.move(pad, (self.height() - self.mini.height()) // 2)
        size = self.action_button.sizeHint()
        self.action_button.setGeometry(self.width() - pad + holo.HoloButton.MARGIN - size.width(),
                                       (self.height() - size.height()) // 2, size.width(), size.height())

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._place_children()

    def text_rect(self) -> QRectF:
        left = self.mini.geometry().right() + 1 + 12
        right = self.action_button.geometry().left() + holo.HoloButton.MARGIN - 12
        return QRectF(left, 0, max(0, right - left), self.height())

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            painter.setPen(QPen(holo.LINE_STRONG, 1.0))
            painter.setBrush(QColor(holo.RAISED))
            painter.drawRoundedRect(rect, TOAST_RADIUS, TOAST_RADIUS)
            font = hud.display_font(14, 500, 0.02)
            painter.setFont(font)
            painter.setPen(QColor(holo.INK_1))
            box = self.text_rect()
            lines = offer_view.wrap_lines(self._line, font, box.width(), TOAST_LINES)
            metrics = QFontMetricsF(font)
            line_h = metrics.height() + 1
            y = box.center().y() - line_h * len(lines) / 2
            for text in lines:
                painter.drawText(QRectF(box.left(), y, box.width(), line_h),
                                 int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
                y += line_h
        finally:
            painter.end()
