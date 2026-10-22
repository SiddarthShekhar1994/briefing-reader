"""Jarvis on your desktop: the HUD's windows (SPEC section 8.1; Phase 1, part 3; DesktopB.dc.html, Switch.dc.html).

Qt only. Nothing here reads, opens or changes anything outside Jarvis, and nothing here switches: the app (a later
part) shows and hides these windows and answers their signals. Separate top-level windows by job, never one big
partly click-through window; each is ``Tool | FramelessWindowHint | WindowStaysOnTopHint | WindowDoesNotAcceptFocus``
with ``WA_TranslucentBackground`` and ``WA_ShowWithoutActivating`` (states_view.TOAST_FLAGS): no taskbar button,
never the focus. Their margins are fully transparent, so clicks there reach the desktop (no setMask); every fill is
opaque (``field`` / ``tile``, never a blur), and no window that takes clicks paints a glow or a shadow (any alpha
above 0 catches clicks): the glows live in the DecorLayer - the prototypes' outer shadows and cyan glows of the bar,
the chips and the cards (``Glow``) as well as the brackets and tethers.

``HudWindow``
    The orb (``OrbWidget(ORB_PX)``: the compact level of detail in a 104 px widget, no ground, never resized per
    frame) over the left end of the command bar, a 760 px bar centred in the work area 16 px from its top
    (Switch.dc.html ``.hud``): the 44 px input row (a second ``hud.CommandBar`` in its HUD look: the chevron, "Ask
    Jarvis", the HUD key's keycaps - none while no key can work), Jarvis's caption line with the orb's state word, a
    hairline and "Bring Jarvis back"; under the bar the chips: "Briefing ready" (cyan), "N need your OK" (amber;
    ui.needs_ok_chip's words) and REC with a coral dot and the time (tabular figures: its width holds while the
    seconds tick). The caption wraps as the prototype's ``.bcap`` does, to at most ``CAPTION_LINES`` lines - the bar
    and the window grow with it, the chips move down - and only past that it is cut with an ellipsis, its whole text
    the tooltip (``WA_AlwaysShowToolTips``: the HUD is never the active window). ``place_on(screen)`` /
    ``place_in(area)``, ``area()``; ``set_caption(text)``; ``set_look(base, muted=, offline=)`` (the orb and the
    state word); ``set_briefing_ready(bool)``, ``set_pending(count)``, ``set_recording(elapsed, warning="")`` (a chip
    whose words did not change is not repainted); ``chip_texts()``; ``bar_rect()``, ``orb_center()``,
    ``occupied_rect()`` (global); ``glows()`` (the bar's and the chips' ``Glow``, global); ``cut_texts()`` (words
    cut or crowded: none at the sizes it is built for). Signals ``bringBackClicked()``, ``chipClicked(key)``
    (``CHIP_BRIEFING``, ``CHIP_APPROVALS``, ``CHIP_REC``) and ``shapeChanged()`` (the window, the bar or a chip moved
    or changed size); the bar's own signals are on ``bar``. The bar's status (an answer, a question, a problem, what
    Jarvis is doing) shows in the caption line without its technical readouts. While idle the window keeps
    ``WindowDoesNotAcceptFocus``: the input takes the keyboard only after the HUD key or a click (a later part). Its
    paint is one cached pixmap (the panel, the hairlines, the orb's dark disc): an orb frame repaints 104 px of it.

``ResultCard``
    A result floating beside the thing it is about (DesktopB ``.glass``): the kicker with its dot, the title, a meta
    line, a row of buttons (NoFocus; a click emits ``buttonClicked(key)``) with a mono tag at its right ("2 OTHER
    MATCHES") and a note. ``set_content(CardContent)``; tones ``TONE_CYAN`` (Jarvis points at something),
    ``TONE_AMBER`` (a decision waits), ``TONE_DONE``. ``place_beside(target, area=None, avoid=())`` puts it beside a
    global rect, never over it and clear of ``avoid`` (the HUD), kept inside the work area (ui._keep_on_screen's
    rule); ``cut_texts()``; ``glow()`` (its ``Glow``, global); ``shapeChanged()`` when it moves, changes size or
    tone.

``DecorLayer``
    One per QScreen, sized to its geometry, also ``WindowTransparentForInput``: the outer shadows and glows under the
    interactive windows (``Glow``: Switch.dc.html's box-shadows of the bar, the chips and the cards - painted once per
    size into a cached sprite), the cyan or amber brackets (all four corners, a tag on the top edge), tethers (a
    dashed line with a dot at each end and a mono label) and the amber in-control edge glow, from global ``Glow`` /
    ``Bracket`` / ``Tether`` items (an item over two screens is drawn on each). Static: it repaints only when its
    items change - only where a shadow moved, when only shadows did - and has no timer; with nothing to draw it stays
    hidden.

``HudOverlay``
    The set: the HUD window, the cards and one DecorLayer per screen (screens coming and going follow; a work area
    that changes moves the HUD with it). ``show_on(screen)`` (``screen_for(window)``: the screen the Jarvis window is
    on) / ``hide()`` / ``is_shown()`` / ``hud_screen()``; ``new_card()``, ``show_card(card, target)`` (clear of the
    bar and the chips), ``remove_card(card)``, ``cards()``; ``set_decor(brackets, tethers, glow=False)``;
    ``layers()``; ``windows()``; ``set_neighbours(toast=, rec_pill=)`` and ``restack()``: bottom to top the
    DecorLayers, the RecPill, the cards, the Toast, the HUD window - nothing of Jarvis's is ever over the bar; while
    the HUD shows, the layer under it shows its shadows and glows (they follow the bar, the chips and the cards: a
    caption growing, a chip coming or going, a card placed) and the meetings RecPill (top centre, where the bar is)
    is held hidden (``RecPill.set_held``: REC is a chip here) and it comes back when the HUD hides;
    ``stack_order()``; ``shutdown()``. ``create(config, native=None)`` is None while [desktop] is off: no HUD window
    then exists at all.

Capture and stacking go through a small seam, ``HudNative`` (``exclude_from_capture(hwnd)``: SetWindowDisplayAffinity
WDA_EXCLUDEFROMCAPTURE on every HUD window when [desktop] hide_from_capture is on, once per native window;
``stack(hwnds)``: SetWindowPos HWND_TOPMOST with SWP_NOACTIVATE, bottom to top - never a raise or an activation).
``default_native()`` is ``Win32Native`` only on the real Windows platform outside the tests (the desktop test guard),
else ``NoNative``, which makes no OS call; tests pass their own fake. Logs carry statuses and error numbers only.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, Protocol

import shiboken6
from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
    QRegion,
    QScreen,
)
from PySide6.QtWidgets import QAbstractButton, QSizePolicy, QWidget

from . import holo, hud, offer_view, settings_view
from . import orb as orb_mod
from .desktop import backend as desktop_backend
from .hotkey import describe_combo

logger = logging.getLogger(__name__)

HUD_FLAGS = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
             | Qt.WindowType.WindowDoesNotAcceptFocus)
DECOR_FLAGS = HUD_FLAGS | Qt.WindowType.WindowTransparentForInput

# The HUD window's layout (logical px; Switch.dc.html .hud, .bar, .qrow, .crow, .chips).
BAR_WIDTH = 760                # the bar, centred in the work area
MIN_BAR_WIDTH = 480            # ... narrower on a small screen, never below this
BAR_TOP = 16                   # from the top of the work area
BAR_RADIUS = 16
ROW_HEIGHT = 44                # the input row
CAPTION_LINE = 20              # one line of the caption (.bcap: Sora 14 light, line-height 1.4)
CAPTION_LINES = 3              # it wraps to at most this many lines (the bar grows with it), cut only past that
CAPTION_PAD = 7                # above and below the caption's lines
CAPTION_HEIGHT = 1 + CAPTION_PAD + CAPTION_LINE + CAPTION_PAD      # one line: a hairline, 7 px, 20 px, 7 px
BAR_HEIGHT = 1 + ROW_HEIGHT + CAPTION_HEIGHT + 1                     # with one caption line
ORB_PX = 92
ORB_SIDE = orb_mod.widget_side(ORB_PX)      # 104: the orb's widget (its halo overflows the 92 px box)
ORB_LEFT = ORB_SIDE // 2 + 2                # the bar starts here in the window: the orb's centre is 2 px left of it
ORB_DROP = 36                               # the orb's centre below the bar's top edge
ORB_PAD = 62                                # the bar's content starts this far in (room for the orb)
SIDE_PAD = 10                               # the bar's right padding before the hairline
RIGHT_MARGIN = 2
CHIP_GAP = 10                               # under the bar, and between chips
CHIP_HEIGHT = 36
SCREEN_MARGIN = 16                          # the least room beside the HUD on a narrow screen
WINDOW_HEIGHT = BAR_TOP + BAR_HEIGHT + CHIP_GAP + CHIP_HEIGHT + 2          # with one caption line
BACK_GAP = 12                               # around the hairline before "Bring Jarvis back" (.back's margins)
_ORB_DISC = 62.2                            # the dark disc's gradient radius (CSS farthest-corner of an 88 px box)
_BAR_LINE = holo.alpha(holo.HOLO, 0.38)
_BAR_HAIRLINE = holo.alpha(holo.HOLO, 0.16)
_BAR_SHINE = holo.alpha(holo.HOLO_HI, 0.12)

# The chips (under the bar) and the button beside it.
CHIP_BRIEFING = "briefing"
CHIP_APPROVALS = "approvals"
CHIP_REC = "rec"
CHIP_KEYS = (CHIP_BRIEFING, CHIP_APPROVALS, CHIP_REC)
BRIEFING_READY_TEXT = "Briefing ready"
BRING_BACK_TEXT = "Bring Jarvis back"
_AMBER_CHEVRON = "#C9A46A"

# Tones of the cards, brackets and tethers.
TONE_CYAN = "cyan"             # Jarvis points at something (a result, the window he opened)
TONE_AMBER = "amber"           # a decision waits; in control (Phase 3)
TONE_DONE = "done"
TONES = (TONE_CYAN, TONE_AMBER, TONE_DONE)
_TONE_INK = {TONE_CYAN: holo.HOLO, TONE_AMBER: holo.AMBER, TONE_DONE: holo.OK}

# The result card (DesktopB .glass).
CARD_WIDTH = 400
CARD_RADIUS = 12
CARD_PAD = (16, 18)            # vertical, horizontal
CARD_GAP = 24                  # between the card and the thing it is about
CARD_MARGIN = 16               # the least room between a card and the work area's edge (when there is room)
CARD_BRACKET = 14
_CARD_LOOKS = {   # fill, border
    TONE_CYAN: (holo.TILE, holo.alpha(holo.HOLO, 0.38)),
    TONE_AMBER: (holo.TILE_AMBER, holo.alpha(holo.AMBER, 0.60)),
    TONE_DONE: (holo.TILE_OK, holo.alpha(holo.OK, 0.50)),
}

# The decor (DesktopB .brk, .btag, .tsvg, .sglow).
BRACKET_LEG = 20
BRACKET_LEG_SMALL = 10
GLOW_DEPTH = 70

# The outer shadows and glows under the interactive windows (Switch.dc.html box-shadow: .hchip :182, .bar :197,
# .glass :248): per look the dark drop shadow (y offset px, blur px, alpha of black), per look and tone the glow (blur
# px, alpha of the tone's ink; no offset). A blur of b px is a Gaussian of sigma b / 2, as in CSS.
GLOW_BAR = "bar"
GLOW_CHIP = "chip"
GLOW_CARD = "card"
_GLOW_DARK = {GLOW_BAR: (10, 40, 0.45), GLOW_CHIP: (6, 22, 0.35), GLOW_CARD: (18, 48, 0.45)}
_GLOW_TINT = {(GLOW_BAR, TONE_CYAN): (24, 0.10), (GLOW_CHIP, TONE_CYAN): (16, 0.12),
              (GLOW_CHIP, TONE_AMBER): (16, 0.16), (GLOW_CARD, TONE_CYAN): (22, 0.10),
              (GLOW_CARD, TONE_AMBER): (26, 0.16)}

WDA_EXCLUDEFROMCAPTURE = 0x11
_HWND_TOPMOST = -1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SWP_NOOWNERZORDER = 0x0200


def needs_ok_text(count: int) -> str:
    """The amber chip: "1 needs your OK" / "3 need your OK" (ui.needs_ok_chip's words; ui is not imported here)."""
    return f"{count} needs your OK" if count == 1 else f"{count} need your OK"


def rec_text(elapsed: str) -> str:
    return f"REC {elapsed}"


def caption_height(lines: int) -> int:
    """The caption line's height with ``lines`` lines of text (a hairline, 7 px, 20 px a line, 7 px)."""
    return 1 + CAPTION_PAD + CAPTION_LINE * max(1, min(CAPTION_LINES, int(lines))) + CAPTION_PAD


def hud_key_caps(label: str) -> list[str]:
    """"Ctrl+Alt+H" (or "ctrl+alt+h") -> ["CTRL", "ALT", "H"]; "" -> []."""
    text = describe_combo(label) if label else ""
    return [part.upper() for part in text.split("+") if part]


def screen_area(point: QPoint) -> QRect:
    """The work area of the screen at ``point`` (the primary screen's when none is there)."""
    screen = QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
    return screen.availableGeometry() if screen is not None else QRect()


def screen_for(widget: QWidget | None) -> QScreen | None:
    """The screen ``widget`` (the Jarvis window) is on - the one under its centre, else its own - or the primary
    screen; the HUD shows there (``HudOverlay.show_on``). A hidden window still has its last place."""
    if widget is not None and shiboken6.isValid(widget):
        screen = QGuiApplication.screenAt(widget.frameGeometry().center()) or widget.screen()
        if screen is not None:
            return screen
    return QGuiApplication.primaryScreen()

# --------------------------------------------------------------------------
# The OS seam: capture exclusion and stacking
# --------------------------------------------------------------------------


class HudNative(Protocol):
    def exclude_from_capture(self, hwnd: int) -> bool:
        """Leave the window out of screenshots, recordings and screen sharing; True when Windows agreed."""

    def stack(self, hwnds: Sequence[int]) -> None:
        """Put the windows at the top of the always-on-top band in this order (the last on top), never activating."""


class NoNative:
    """Offscreen and in the tests: no OS call at all."""

    def exclude_from_capture(self, hwnd: int) -> bool:
        return False

    def stack(self, hwnds: Sequence[int]) -> None:
        return None


class Win32Native:
    """user32's SetWindowDisplayAffinity and SetWindowPos (ctypes). Refused while the desktop test guard is on."""

    def __init__(self) -> None:
        if desktop_backend.real_backend_blocked():
            raise RuntimeError("the HUD's OS calls are off while the tests run")
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, wintypes.UINT]
        user32.SetWindowPos.restype = wintypes.BOOL
        self._user32 = user32
        self._last_error = ctypes.get_last_error
        self._topmost = wintypes.HWND(_HWND_TOPMOST)

    def exclude_from_capture(self, hwnd: int) -> bool:
        if self._user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
            return True
        logger.info("HUD: capture exclusion refused (winerror %d)", self._last_error())
        return False

    def stack(self, hwnds: Sequence[int]) -> None:
        flags = _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_NOOWNERZORDER
        for hwnd in hwnds:
            if not self._user32.SetWindowPos(hwnd, self._topmost, 0, 0, 0, 0, flags):
                logger.debug("HUD: stacking refused (winerror %d)", self._last_error())


def default_native() -> HudNative:
    """``Win32Native`` on the real Windows platform outside the tests; ``NoNative`` offscreen and in the tests."""
    if QGuiApplication.platformName() != "windows" or desktop_backend.real_backend_blocked():
        return NoNative()
    try:
        return Win32Native()
    except (OSError, AttributeError, RuntimeError) as exc:
        logger.info("HUD: no native window calls (%s)", type(exc).__name__)
        return NoNative()

# --------------------------------------------------------------------------
# The shared window behaviour
# --------------------------------------------------------------------------


class _OverlayWindow(QWidget):
    """A HUD window: the flags, never the focus, capture exclusion once per native window."""

    def __init__(self, flags: Qt.WindowType, native: HudNative | None, hide_from_capture: bool) -> None:
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._native: HudNative = native if native is not None else default_native()
        self._hide_from_capture = bool(hide_from_capture)
        self._excluded = 0           # the native window last left out of capture

    def show_quietly(self) -> None:
        """Show without taking the focus or bringing anything forward (on top by the flag; never a raise)."""
        handle = self.windowHandle()
        if handle is not None:
            handle.setProperty("_q_showWithoutActivating", True)
        self.show()

    def excluded_from_capture(self) -> bool:
        """True while this window's native window is left out of capture (never creates a native window)."""
        return bool(self._excluded) and self._excluded == int(self.internalWinId())

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        handle = self.windowHandle()
        if handle is not None:
            # Qt's Windows plugin reads this property at each show once the native window exists.
            handle.setProperty("_q_showWithoutActivating", True)
        if self._hide_from_capture:
            hwnd = int(self.winId())
            if hwnd and hwnd != self._excluded and self._native.exclude_from_capture(hwnd):
                self._excluded = hwnd

# --------------------------------------------------------------------------
# The HUD window's parts
# --------------------------------------------------------------------------


class _KeyCaps(QWidget):
    """The HUD key as keycaps after the input (DesktopB / Switch .kbd: mono 9, ink-3, 6 px padding and a 1 px border
    each side, a 2 px bottom edge - the text's advance + 14 wide, 19 high - and the row's 10 px gap between caps)."""

    GAP = 10
    HEIGHT = 19
    CHROME = 14                # the padding and the border on both sides of a cap's text

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._keys = hud_key_caps(label)
        self._font = hud.mono_font(9, 500, 0.12)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("+".join(self._keys))

    def keys(self) -> list[str]:
        return list(self._keys)

    def _cap_width(self, key: str) -> float:
        return QFontMetricsF(self._font).horizontalAdvance(key) + self.CHROME

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        if not self._keys:
            return QSize(0, self.HEIGHT)
        width = sum(self._cap_width(key) for key in self._keys) + self.GAP * (len(self._keys) - 1)
        return QSize(math.ceil(width), self.HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.sizeHint()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        if not self._keys:
            return
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setFont(self._font)
            x = 0.0
            for key in self._keys:
                cap = QRectF(x, 0, self._cap_width(key), self.HEIGHT)
                painter.setPen(QPen(holo.alpha(holo.HOLO, 0.28), 1.0))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(cap.adjusted(0.5, 0.5, -0.5, -1.5), 5, 5)
                painter.fillRect(QRectF(cap.left() + 3, cap.bottom() - 2, cap.width() - 6, 1.5),
                                 holo.alpha(holo.HOLO, 0.28))         # the keycap's 2 px bottom edge
                painter.setPen(QColor(holo.INK_3))
                painter.drawText(cap.adjusted(0, 0, 0, -1), int(Qt.AlignmentFlag.AlignCenter), key)
                x += cap.width() + self.GAP
        finally:
            painter.end()


class _CaptionLine(QWidget):
    """Jarvis's caption (Sora 14 light, 20 px lines) or the bar's status in its tone, wrapped at word boundaries to
    at most CAPTION_LINES lines as the prototype's .bcap wraps (the HUD window gives it the height its lines take) and
    cut with an ellipsis only past that - the whole text is its tooltip -, and at the right the orb's state word with
    its dot, centred on the lines (DesktopB .cap, .status)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._caption = ""
        self._status = ""
        self._status_tone = hud.TONE_IDLE
        self._word = orb_mod.READOUTS[orb_mod.IDLE]
        self._dot = QColor(holo.HOLO)
        self._font = hud.body_font(14, 300)
        self._word_font = holo.font("badge")
        self.setAccessibleName("Jarvis says")

    def set_caption(self, text: str) -> None:
        text = " ".join(str(text or "").split())
        if text != self._caption:
            self._caption = text
            self._sync()

    def set_status(self, text: str, tone: str) -> None:
        text = " ".join(str(text or "").split())
        if (text, tone) != (self._status, self._status_tone):
            self._status, self._status_tone = text, tone
            self._sync()

    def set_word(self, word: str, dot: str | QColor) -> None:
        if (word, QColor(dot)) != (self._word, self._dot):
            self._word, self._dot = word, QColor(dot)
            self.update()

    def text(self) -> str:
        """What the line says now (the bar's status while there is one, else the caption)."""
        return self._status or self._caption

    def word(self) -> str:
        return self._word

    def lines(self, width: float | None = None) -> list[str]:
        """The text as painted in a caption line ``width`` px wide (its own width by default): wrapped at word
        boundaries to at most CAPTION_LINES lines, cut with an ellipsis only where it needs more (or one word is wider
        than the line)."""
        text = self.text()
        if not text:
            return []
        room = self._room(self.width() if width is None else width)
        metrics = QFontMetricsF(self._font)
        return [metrics.elidedText(line, Qt.TextElideMode.ElideRight, room)
                for line in offer_view.wrap_lines(text, self._font, room, CAPTION_LINES)]

    def line_count(self, width: float | None = None) -> int:
        """How many lines the text takes in a caption line ``width`` px wide (at least one)."""
        return max(1, len(self.lines(width)))

    def shown_text(self) -> str:
        """The text as painted, its lines joined: the whole text unless it is cut."""
        return " ".join(self.lines())

    def _room(self, width: float) -> float:
        """The text's width in a caption line ``width`` px wide: up to the state word's dot."""
        return max(0.0, width - self._word_width() - 7 - 3.5 - 6 - 14)

    def _word_width(self) -> float:
        return QFontMetricsF(self._word_font).horizontalAdvance(self._word) + 2

    def _color(self) -> QColor:
        if not self._status:
            return QColor(holo.INK_1)
        return QColor({hud.TONE_WARN: holo.AMBER_TX2, hud.TONE_ERROR: holo.ALERT_TX,
                       hud.TONE_WORKING: holo.INK_2}.get(self._status_tone, holo.INK_1))

    def _sync(self) -> None:
        text = self.text()
        self.setToolTip(hud.plain_tooltip(text) if text else "")
        self.setAccessibleDescription(text)
        self.update()

    def word_rect(self) -> QRectF:
        """Where the state word is painted (at the line's right end, centred on its lines)."""
        width = self._word_width()
        return QRectF(self.width() - width, 0, width, self.height())

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            word = self.word_rect()
            dot = QPointF(word.left() - 7 - 3.5, self.height() / 2)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(holo.alpha(self._dot, 0.22))
            painter.drawEllipse(dot, 6.0, 6.0)
            painter.setBrush(self._dot)
            painter.drawEllipse(dot, 3.5, 3.5)
            painter.setFont(self._word_font)
            painter.setPen(QColor(holo.INK_3))
            painter.drawText(word, align, self._word)
            painter.setFont(self._font)
            painter.setPen(self._color())
            room = self._room(self.width())
            for index, line in enumerate(self.lines()):
                painter.drawText(QRectF(0, CAPTION_PAD + CAPTION_LINE * index, room, CAPTION_LINE), align, line)
        finally:
            painter.end()


_CHIP_LOOKS = {   # fill, border, hover border, text, dot, chevron
    CHIP_BRIEFING: (holo.TILE, holo.alpha(holo.HOLO, 0.42), QColor(holo.HOLO), holo.INK_1, holo.HOLO, holo.INK_4),
    CHIP_APPROVALS: (holo.TILE_AMBER, holo.alpha(holo.AMBER, 0.60), QColor(holo.AMBER), holo.AMBER_TX, holo.AMBER,
                     _AMBER_CHEVRON),
    CHIP_REC: (holo.RAISED, holo.alpha(holo.ALERT, 0.55), QColor(holo.ALERT), holo.INK_1, holo.ALERT, holo.INK_4),
}


class HudChip(QAbstractButton):
    """A chip under the HUD bar (Switch.dc.html .hchip): 36 high, opaque, a dot, the words and a chevron. Static (no
    ping, no blinking dot: only the orb loops; words that did not change repaint nothing); REC's figures are tabular,
    so its width holds while the seconds tick (the window's REC readout does the same). NoFocus, a click emits
    ``clicked``."""

    PAD = 12

    def __init__(self, key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._key = key if key in _CHIP_LOOKS else CHIP_BRIEFING
        self._hover = False
        self._font = hud.body_font(13, 500)
        if self._key == CHIP_REC:
            try:
                self._font.setFeature(QFont.Tag("tnum"), 1)      # as holo.font does for its tabular roles
            except (AttributeError, TypeError, ValueError):       # Qt before 6.7: proportional figures
                pass
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def key(self) -> str:
        return self._key

    def setText(self, text: str) -> None:  # noqa: N802 - Qt override
        if text == self.text():
            return                         # unchanged (a glance that changed nothing): no repaint, no new size
        super().setText(text)
        self.setAccessibleName(text)
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        width = self._chrome() + QFontMetricsF(self._font).horizontalAdvance(self.text())
        return QSize(math.ceil(width), CHIP_HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.sizeHint()

    def _chrome(self) -> float:
        """Everything but the words: the padding, the dot and the chevron with their gaps."""
        return self.PAD + 7 + 8 + 8 + 14 + self.PAD

    def shown_text(self) -> str:
        """The words as painted: cut with an ellipsis only when the chip is narrower than its words."""
        room = max(0.0, self.width() - self._chrome())
        return QFontMetricsF(self._font).elidedText(self.text(), Qt.TextElideMode.ElideRight, room + 0.5)

    def enterEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().enterEvent(event)
        self._hover = True
        self.update()

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().leaveEvent(event)
        self._hover = False
        self.update()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        fill, border, hover, ink, dot, chevron = _CHIP_LOOKS[self._key]
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            radius = rect.height() / 2
            painter.setPen(QPen(hover if self._hover else border, 1.0))
            painter.setBrush(QColor(fill))
            painter.drawRoundedRect(rect, radius, radius)
            center = QPointF(self.PAD + 3.5, rect.center().y())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(holo.alpha(dot, 0.25))
            painter.drawEllipse(center, 6.0, 6.0)
            painter.setBrush(QColor(dot))
            painter.drawEllipse(center, 3.5, 3.5)
            x = self.PAD + 7 + 8
            text = self.shown_text()
            painter.setFont(self._font)
            painter.setPen(QColor(ink))
            width = QFontMetricsF(self._font).horizontalAdvance(text)
            painter.drawText(QRectF(x, 0, width + 2, self.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
            holo.paint_icon(painter, QRectF(x + width + 8, rect.center().y() - 7, 14, 14), "chevron",
                            QColor(chevron), stroke=2.0)
        finally:
            painter.end()


class _BackButton(QAbstractButton):
    """"Bring Jarvis back" at the bar's right end (Switch.dc.html .back): 38 high, radius 10, the window glyph and
    Sora 13 on a faint cyan fill, brighter on hover and pressed. NoFocus: a click never moves the focus."""

    HEIGHT = 38
    PAD = 14
    ICON = 17
    GAP = 8
    RADIUS = 10

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hover = False
        self._font = hud.body_font(13, 500)
        self.setText(text)
        self.setAccessibleName(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        width = self.PAD + self.ICON + self.GAP + QFontMetricsF(self._font).horizontalAdvance(self.text()) + self.PAD
        return QSize(math.ceil(width), self.HEIGHT)

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

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        if self.isDown():
            fill, border = holo.alpha(holo.HOLO, 0.28), QColor(holo.HOLO_HI)
        elif self._hover:
            fill, border = holo.alpha(holo.HOLO, 0.16), QColor(holo.HOLO)
        else:
            fill, border = holo.alpha(holo.HOLO, 0.08), holo.alpha(holo.HOLO, 0.45)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            painter.setPen(QPen(border, 1.0))
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, self.RADIUS, self.RADIUS)
            ink = QColor(holo.INK_1)
            box = QRectF(self.PAD, (self.height() - self.ICON) / 2, self.ICON, self.ICON)
            holo.paint_icon(painter, box, "window", ink, stroke=1.7)
            painter.setFont(self._font)
            painter.setPen(ink)
            x = self.PAD + self.ICON + self.GAP
            painter.drawText(QRectF(x, 0, self.width() - x, self.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.text())
        finally:
            painter.end()


class HudWindow(_OverlayWindow):
    """The orb, the command bar, the caption line, Bring Jarvis back and the chips; see the module docstring."""

    bringBackClicked = Signal()
    chipClicked = Signal(str)
    shapeChanged = Signal()            # the window, the bar or a shown chip moved or changed size

    def __init__(self, *, hud_key: str = "", native: HudNative | None = None, hide_from_capture: bool = True) -> None:
        super().__init__(HUD_FLAGS, native, hide_from_capture)
        self.setObjectName("JarvisHud")
        self.setWindowTitle("Jarvis")
        self.setAccessibleName("Jarvis on your desktop")
        # Never the active window (WindowDoesNotAcceptFocus): Qt shows a tooltip (a cut caption's whole text) only
        # for the active window or one with this attribute.
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
        self._bar_width = BAR_WIDTH
        self._caption_lines = 1                             # the caption's lines at the bar's width
        self._area = QRect()                                # the work area it was placed in (none yet)
        self._hairline_x = 0
        self._layout_key: tuple | None = None
        self._shape_key: tuple | None = None
        self._background: QPixmap | None = None
        self._background_key: tuple | None = None
        self._pending = 0
        self._recording = ""
        self._warning = ""
        self.orb = orb_mod.OrbWidget(ORB_PX, self)          # no ground: the window is translucent
        self.keycaps = _KeyCaps(hud_key, self)
        self.bar = hud.CommandBar(self)
        # The keycaps say the HUD key only while one can work (settings_view.effective_hud_key); none, none shown.
        self.bar.set_hud(self.keycaps if self.keycaps.keys() else None)
        self.keycaps.setVisible(bool(self.keycaps.keys()))
        self.caption = _CaptionLine(self)
        self.back_button = _BackButton(BRING_BACK_TEXT, self)
        self.back_button.clicked.connect(self.bringBackClicked)
        self.chips: dict[str, HudChip] = {}
        for key in CHIP_KEYS:
            chip = HudChip(key, self)
            chip.hide()
            chip.clicked.connect(partial(self.chipClicked.emit, key))
            self.chips[key] = chip
        self.chips[CHIP_BRIEFING].setText(BRIEFING_READY_TEXT)
        self.bar.statusChanged.connect(self._on_bar_status)
        self.set_look(orb_mod.IDLE)
        self.resize(ORB_LEFT + BAR_WIDTH + RIGHT_MARGIN, WINDOW_HEIGHT)
        self._layout()

    # ---- placement ------------------------------------------------------------------------

    def place_on(self, screen: QScreen | None) -> None:
        """On ``screen``'s work area (the screen the Jarvis window is on; the primary screen for None)."""
        screen = screen or QGuiApplication.primaryScreen()
        if screen is not None:
            self.place_in(screen.availableGeometry())

    def place_in(self, area: QRect) -> None:
        """The bar centred in ``area``, BAR_TOP below its top; narrower on a small screen, the orb kept inside."""
        self._area = QRect(area)
        room = area.width() - 2 * (ORB_LEFT + SCREEN_MARGIN)       # the orb's room on both sides keeps it centred
        self._bar_width = max(MIN_BAR_WIDTH, min(BAR_WIDTH, room))
        self._caption_lines = self.caption.line_count(self._caption_width())
        bar_left = area.x() + (area.width() - self._bar_width) // 2
        left = max(area.x(), min(bar_left - ORB_LEFT, area.x() + area.width() - ORB_LEFT - self._bar_width
                                 - RIGHT_MARGIN))
        self.setGeometry(left, area.y(), self._wanted_width(left), self._window_height())
        self._layout()

    def area(self) -> QRect:
        """The work area the HUD was placed in (empty before ``place_on`` / ``place_in``)."""
        return QRect(self._area)

    def bar_height(self) -> int:
        """The bar: the input row and the caption with its lines (BAR_HEIGHT with one; it grows 20 px a line)."""
        return 1 + ROW_HEIGHT + caption_height(self._caption_lines) + 1

    def caption_lines(self) -> int:
        """The lines the caption takes now (1 to CAPTION_LINES)."""
        return self._caption_lines

    def bar_rect_local(self) -> QRect:
        return QRect(ORB_LEFT, BAR_TOP, self._bar_width, self.bar_height())

    def bar_rect(self) -> QRect:
        """The bar in global coordinates."""
        return self.bar_rect_local().translated(self.geometry().topLeft())

    def orb_center(self) -> QPoint:
        """The orb's centre in global coordinates."""
        return self.geometry().topLeft() + QPoint(ORB_LEFT - 2, BAR_TOP + ORB_DROP)

    def occupied_rect(self) -> QRect:
        """What the window paints, in global coordinates: the orb, the bar and the chips shown (a card keeps out of
        it; nothing else of Jarvis's is ever over it)."""
        rect = self.bar_rect_local().united(self.orb.geometry())
        for chip in self.chips.values():
            if not chip.isHidden():
                rect = rect.united(chip.geometry())
        return rect.translated(self.geometry().topLeft())

    def glows(self) -> list[Glow]:
        """The bar's and the shown chips' outer shadows and glows (global): the DecorLayer under this window paints
        them (Switch.dc.html .bar and .hchip box-shadows) - this window never does (its margins stay clear)."""
        origin = self.geometry().topLeft()
        glows = [Glow(self.bar_rect_local().translated(origin), BAR_RADIUS, GLOW_BAR)]
        for key, chip in self.chips.items():
            if not chip.isHidden():
                glows.append(Glow(chip.geometry().translated(origin), CHIP_HEIGHT / 2, GLOW_CHIP,
                                  TONE_AMBER if key == CHIP_APPROVALS else TONE_CYAN))
        return glows

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._layout()

    def moveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().moveEvent(event)
        self._check_shape()

    def _check_shape(self) -> None:
        """``shapeChanged`` once the window, the bar or a shown chip moved or changed size (the shadows follow)."""
        key = (self.geometry().getRect(), self.bar_rect_local().getRect(),
               tuple((name, chip.geometry().getRect()) for name, chip in self.chips.items() if not chip.isHidden()))
        if key != self._shape_key:
            self._shape_key = key
            self.shapeChanged.emit()

    def _caption_width(self) -> int:
        """The caption line's width at the bar's width: from the orb's room to the hairline before Bring Jarvis
        back."""
        back_x = ORB_LEFT + self._bar_width - 1 - BACK_GAP - self.back_button.sizeHint().width()
        return max(0, back_x - BACK_GAP - 1 - SIDE_PAD - (ORB_LEFT + ORB_PAD))

    def _window_height(self) -> int:
        return BAR_TOP + self.bar_height() + CHIP_GAP + CHIP_HEIGHT + 2

    def _chips_width(self) -> int:
        hints = [chip.sizeHint().width() for chip in self.chips.values() if not chip.isHidden()]
        return sum(hints) + CHIP_GAP * max(0, len(hints) - 1)

    def _wanted_width(self, left: int | None = None) -> int:
        """The window's width: the orb and the bar, wider to the right when the chips need it (as far as the work
        area goes)."""
        base = ORB_LEFT + self._bar_width + RIGHT_MARGIN
        wanted = max(base, ORB_LEFT + ORB_PAD + self._chips_width() + RIGHT_MARGIN)
        if wanted > base and self._area.isValid():
            x = self.x() if left is None else left
            wanted = max(base, min(wanted, self._area.x() + self._area.width() - x))
        return wanted

    def _layout(self) -> None:
        self._caption_lines = self.caption.line_count(self._caption_width())
        size = (self._wanted_width(), self._window_height())
        if size != (self.width(), self.height()):
            self.resize(*size)                          # the caption's lines grow the bar, the window grows down
        bar = self.bar_rect_local()
        center = QPoint(ORB_LEFT - 2, BAR_TOP + ORB_DROP)
        self.orb.move(center.x() - self.orb.width() // 2, center.y() - self.orb.height() // 2)
        back = self.back_button.sizeHint()
        back_x = bar.x() + bar.width() - 1 - BACK_GAP - back.width()       # inside the bar's 1 px border
        self.back_button.setGeometry(back_x, bar.y() + (bar.height() - back.height()) // 2, back.width(),
                                     back.height())
        self._hairline_x = back_x - BACK_GAP - 1
        left = bar.x() + ORB_PAD
        width = max(0, self._hairline_x - SIDE_PAD - left)
        self.bar.setGeometry(left, bar.y() + 1, width, ROW_HEIGHT)
        self.caption.setGeometry(left, bar.y() + 1 + ROW_HEIGHT + 1, width, caption_height(self._caption_lines) - 1)
        x, y = left, bar.y() + bar.height() + CHIP_GAP
        for chip in self.chips.values():
            if chip.isHidden():
                continue
            hint = chip.sizeHint()
            room = max(0, self.width() - RIGHT_MARGIN - x)
            chip.setGeometry(x, y, min(hint.width(), room), CHIP_HEIGHT)     # cut (never past the window)
            x += hint.width() + CHIP_GAP
        key = (self.width(), self.height(), self._bar_width, self._hairline_x)
        if key != self._layout_key:       # the panel moved: repaint it (a chip's change repaints only the chips)
            self._layout_key = key
            self.update()
        self._check_shape()

    def cut_texts(self) -> list[str]:
        """The words this window cuts or crowds now (none at the sizes it is built for; tests and the harness)."""
        for layout in (self.bar.layout(), self.bar.field.layout()):    # laid out as shown, though it may not be yet
            if layout is not None:
                layout.activate()
        cut: list[str] = []
        line_edit = self.bar.input
        if not line_edit.text():
            needed = QFontMetricsF(line_edit.font()).horizontalAdvance(line_edit.placeholderText())
            if needed > line_edit.width() - 4:
                cut.append("placeholder")
        if self.keycaps.keys() and (self.keycaps.width() < self.keycaps.sizeHint().width()
                                    or self.keycaps.geometry().right() >= self.bar.field.width()):
            cut.append("keycaps")
        if self.caption.shown_text() != self.caption.text():
            cut.append("caption")
        if self.caption.word_rect().left() < 0:
            cut.append("state word")
        bar = self.bar_rect_local()
        back = self.back_button.geometry()
        if back.width() < self.back_button.sizeHint().width() or back.left() < bar.left() + ORB_PAD:
            cut.append("back")
        for key, chip in self.chips.items():
            if not chip.isHidden() and (chip.shown_text() != chip.text() or chip.geometry().right() >= self.width()):
                cut.append(f"chip {key}")
        return cut

    # ---- what it shows ---------------------------------------------------------------------

    def set_caption(self, text: str) -> None:
        """Jarvis's line ("On your desktop, sir."), wrapped to at most CAPTION_LINES lines - the bar grows with it;
        the bar's status replaces it while there is one."""
        self.caption.set_caption(text)
        self._layout()

    def caption_text(self) -> str:
        return self.caption.text()

    def set_look(self, base: str, *, muted: bool = False, offline: bool = False) -> None:
        """The orb's look and the state word beside the caption (amber dot when he needs you)."""
        self.orb.set_look(base, muted=muted, offline=offline)
        word = orb_mod.state_word(base, muted, offline)
        if base == orb_mod.WAITING and not muted and not offline:
            word = orb_mod.READOUTS[orb_mod.IDLE]       # the long waiting readout stays in the window's header
        dot = holo.AMBER if base == orb_mod.NEEDS else holo.STEEL if (muted or offline) else holo.HOLO
        self.caption.set_word(word, dot)
        self._layout()                                  # a longer word leaves the caption less room

    def state_word(self) -> str:
        return self.caption.word()

    def set_briefing_ready(self, ready: bool) -> None:
        self._show_chip(CHIP_BRIEFING, bool(ready))

    def set_pending(self, count: int) -> None:
        """"N need your OK" (amber) while ``count`` approvals wait; none at 0."""
        self._pending = max(0, int(count))
        if self._pending:
            self.chips[CHIP_APPROVALS].setText(needs_ok_text(self._pending))
        self._show_chip(CHIP_APPROVALS, self._pending > 0)

    def set_recording(self, elapsed: str = "", warning: str = "") -> None:
        """REC with the time while a meeting records ("12:04"; "" when none) and its warning ("No call audio")."""
        self._recording, self._warning = str(elapsed or ""), str(warning or "")
        if self._recording:
            text = rec_text(self._recording) + (f" {hud.MIDDLE_DOT} {self._warning}" if self._warning else "")
            self.chips[CHIP_REC].setText(text)
        self._show_chip(CHIP_REC, bool(self._recording))

    def chip_texts(self) -> list[str]:
        """The chips shown, left to right."""
        return [chip.text() for chip in self.chips.values() if not chip.isHidden()]

    def _show_chip(self, key: str, shown: bool) -> None:
        chip = self.chips[key]
        if chip.isHidden() == shown or chip.sizeHint().width() != chip.width():
            chip.setVisible(shown)
            self._layout()

    def _on_bar_status(self, text: str, tone: str) -> None:
        shown = hud.quiet_readouts(text) if tone != hud.TONE_IDLE else ""
        self.caption.set_status(shown, tone)
        self._layout()

    # ---- paint -----------------------------------------------------------------------------

    def background(self, dpr: float | None = None) -> QPixmap:
        """The window's static paint (the panel, its hairlines and brackets, the orb's dark disc), cached per size
        and device pixel ratio."""
        dpr = float(dpr or self.devicePixelRatioF() or 1.0)
        key = (self.width(), self.height(), self._bar_width, self._hairline_x, dpr)
        if self._background is None or self._background_key != key:
            pixmap = QPixmap(max(1, round(self.width() * dpr)), max(1, round(self.height() * dpr)))
            pixmap.setDevicePixelRatio(dpr)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            try:
                self._paint_background(painter)
            finally:
                painter.end()
            self._background, self._background_key = pixmap, key
        return self._background

    def _paint_background(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bar = QRectF(self.bar_rect_local())
        edge = bar.adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(edge, BAR_RADIUS, BAR_RADIUS)
        painter.fillPath(path, QColor(holo.FIELD))
        painter.setPen(QPen(_BAR_SHINE, 1.0))
        painter.drawLine(QPointF(bar.left() + BAR_RADIUS, bar.top() + 1.5), QPointF(bar.right() - BAR_RADIUS,
                                                                                     bar.top() + 1.5))
        painter.setPen(QPen(_BAR_LINE, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        holo.paint_brackets(painter, bar, holo.HOLO, leg=12, radius=BAR_RADIUS, corners=holo.BRACKETS_RIGHT,
                            width=2.0)
        divider = bar.top() + 1 + ROW_HEIGHT + 0.5
        painter.setPen(QPen(_BAR_HAIRLINE, 1.0))
        painter.drawLine(QPointF(bar.left() + 1, divider), QPointF(self._hairline_x, divider))
        painter.drawLine(QPointF(self._hairline_x + 0.5, bar.top() + 12), QPointF(self._hairline_x + 0.5,
                                                                                 bar.bottom() - 12))
        center = QPointF(ORB_LEFT - 2, BAR_TOP + ORB_DROP)
        disc = QRadialGradient(center, _ORB_DISC)
        void = QColor(holo.VOID)
        disc.setColorAt(0.0, holo.alpha(void, 0.82))
        disc.setColorAt(0.52, holo.alpha(void, 0.82))
        disc.setColorAt(0.72, holo.alpha(void, 0.0))
        disc.setColorAt(1.0, holo.alpha(void, 0.0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(disc)
        painter.drawEllipse(center, ORB_PX / 2 - 2, ORB_PX / 2 - 2)

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        pixmap = self.background()
        dpr = pixmap.devicePixelRatio()
        area = QRectF(event.rect())
        painter = QPainter(self)
        try:
            painter.drawPixmap(area, pixmap, QRectF(area.x() * dpr, area.y() * dpr, area.width() * dpr,
                                                    area.height() * dpr))
        finally:
            painter.end()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        handle = self.windowHandle()
        if handle is not None:
            orb_mod.watch_input(handle)            # input to the HUD ends the orb's deep idle

# --------------------------------------------------------------------------
# The result card
# --------------------------------------------------------------------------


def _whole(text: str, lines: Sequence[str], font: Any, width: float) -> bool:
    """True when ``lines`` (offer_view.wrap_lines' result) hold all of ``text`` and none is wider than ``width``."""
    metrics = QFontMetricsF(font)
    return " ".join(lines) == " ".join(str(text or "").split()) and all(
        metrics.horizontalAdvance(line) <= width + 0.5 for line in lines)


@dataclass(frozen=True)
class CardButton:
    key: str
    text: str
    variant: str = holo.GHOST          # holo.GO for the one thing to do ("Open"); GHOST for the rest


@dataclass(frozen=True)
class CardContent:
    kicker: str                        # "FILE <middle dot> WORD DOCUMENT"
    title: str
    meta: str = ""                     # where it is, when it changed
    buttons: tuple[CardButton, ...] = ()
    tag: str = ""                      # mono caps at the right of the buttons ("2 OTHER MATCHES")
    note: str = ""                     # the last line ("Opened in Word. Nothing else changed.")
    note_tone: str = TONE_DONE         # its colour: done (mint), cyan (ink-3) or amber
    tone: str = TONE_CYAN


class ResultCard(_OverlayWindow):
    """A result beside the thing it is about; see the module docstring."""

    buttonClicked = Signal(str)
    shapeChanged = Signal()            # it moved, changed size or changed tone (its shadow follows)

    TITLE_LINES = 2
    META_LINES = 2
    NOTE_LINES = 2

    def __init__(self, *, native: HudNative | None = None, hide_from_capture: bool = True) -> None:
        super().__init__(HUD_FLAGS, native, hide_from_capture)
        self.setObjectName("JarvisResult")
        self.setWindowTitle("Jarvis")
        self._content = CardContent("", "")
        self._buttons: dict[str, holo.HoloButton] = {}
        self._title_font = hud.body_font(18, 500)
        self._meta_font = hud.body_font(13)
        self._kicker_font = hud.mono_font(10, 500, 0.22)           # DesktopB .kick
        self._tag_font = hud.mono_font(10, 500, 0.2)
        self._rows: dict[str, QRectF] = {}
        self.setFixedSize(CARD_WIDTH, 120)

    def content(self) -> CardContent:
        return self._content

    def buttons(self) -> dict[str, holo.HoloButton]:
        return dict(self._buttons)

    def set_content(self, content: CardContent) -> None:
        self._content = content
        for button in self._buttons.values():
            button.hide()
            button.deleteLater()
        self._buttons = {}
        for spec in content.buttons:
            button = holo.HoloButton(spec.text, spec.variant, parent=self)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)       # a click never moves the focus here
            button.setAccessibleName(spec.text)
            button.clicked.connect(partial(self.buttonClicked.emit, spec.key))
            button.show()
            self._buttons[spec.key] = button
        self.setAccessibleName(content.title)
        self.setAccessibleDescription(" ".join(part for part in (content.kicker, content.meta, content.note) if part))
        self._relayout()
        self.shapeChanged.emit()                        # its tone (and its size) may have changed

    def glow(self) -> Glow:
        """Its outer shadow and glow (global; Switch.dc.html .glass, amber for a decision): the DecorLayer under it
        paints them, this window never does."""
        tone = TONE_AMBER if self._content.tone == TONE_AMBER else TONE_CYAN
        return Glow(self.geometry(), CARD_RADIUS, GLOW_CARD, tone)

    def moveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().moveEvent(event)
        self.shapeChanged.emit()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self.shapeChanged.emit()

    def _relayout(self) -> None:
        """Rows top to bottom; the card's height follows them."""
        content = self._content
        pad_v, pad_h = CARD_PAD
        inner = CARD_WIDTH - 2 * pad_h
        rows: dict[str, QRectF] = {}
        y = float(pad_v)
        rows["kicker"] = QRectF(pad_h, y, inner, 14)
        y += 14 + 6
        title = self._title_lines()
        rows["title"] = QRectF(pad_h, y, inner, 24 * len(title))
        y += 24 * len(title)
        meta = self._wrap(content.meta, self._meta_font, self.META_LINES)
        if meta:
            y += 4
            rows["meta"] = QRectF(pad_h, y, inner, 20 * len(meta))
            y += 20 * len(meta)
        if self._buttons or content.tag:
            y += 14
            margin = holo.HoloButton.MARGIN
            x = pad_h - margin
            for button in self._buttons.values():
                hint = button.sizeHint()
                button.setGeometry(round(x), round(y) - margin, hint.width(), hint.height())
                x += hint.width() - 2 * margin + holo.BUTTON_GAP
            rows["buttons"] = QRectF(pad_h, y, inner, holo.SIZE_MD)
            y += holo.SIZE_MD
        note = self._wrap(content.note, self._meta_font, self.NOTE_LINES)
        if note:
            y += 12
            rows["note"] = QRectF(pad_h, y, inner, 20 * len(note))
            y += 20 * len(note)
        self._rows = rows
        self.setFixedSize(CARD_WIDTH, math.ceil(y + pad_v))
        self.update()

    def _wrap(self, text: str, font: Any, lines: int) -> list[str]:
        """``text`` in at most ``lines`` lines of the card's inner width, the last one cut with an ellipsis."""
        return offer_view.wrap_lines(text, font, CARD_WIDTH - 2 * CARD_PAD[1], lines) if text else []

    def _title_lines(self) -> list[str]:
        return self._wrap(self._content.title, self._title_font, self.TITLE_LINES) or [""]

    def row_rect(self, name: str) -> QRectF:
        """A row's rect ("kicker", "title", "meta", "buttons", "note"); empty when it is not shown."""
        return QRectF(self._rows.get(name, QRectF()))

    def place_beside(self, target: QRect, area: QRect | None = None, avoid: Sequence[QRect] = ()) -> QRect:
        """Beside ``target`` (global): right of it, else left, above, below - inside ``area`` (the work area of the
        target's screen by default), never over the target and clear of ``avoid`` (the HUD: its bar and chips);
        when nothing fits, the right side kept on screen and below the HUD."""
        area = QRect(area) if area is not None else screen_area(target.center())
        avoid = [QRect(rect) for rect in avoid if rect.isValid()]
        size = self.size()
        w, h = size.width(), size.height()
        if area.width() >= w + 2 * CARD_MARGIN and area.height() >= h + 2 * CARD_MARGIN:
            area = area.adjusted(CARD_MARGIN, CARD_MARGIN, -CARD_MARGIN, -CARD_MARGIN)

        def clamp(value: int, low: int, high: int) -> int:
            return max(low, min(value, high))

        tops = [clamp(target.top(), area.top(), area.bottom() + 1 - h)]
        tops += [clamp(rect.bottom() + 1 + CARD_GAP, area.top(), area.bottom() + 1 - h) for rect in avoid]
        left = clamp(target.left(), area.left(), area.right() + 1 - w)
        candidates = [QRect(target.right() + 1 + CARD_GAP, top, w, h) for top in tops]
        candidates += [QRect(target.left() - CARD_GAP - w, top, w, h) for top in tops]
        candidates += [QRect(left, target.top() - CARD_GAP - h, w, h), QRect(left, target.bottom() + 1 + CARD_GAP, w, h)]
        chosen = next((rect for rect in candidates if area.contains(rect) and not rect.intersects(target)
                       and not any(rect.intersects(other) for other in avoid)), None)
        if chosen is None:
            first = candidates[0]
            chosen = QRect(clamp(first.x(), area.left(), area.right() + 1 - w), first.y(), w, h)
            for other in avoid:
                if chosen.intersects(other):
                    chosen.moveTop(clamp(other.bottom() + 1 + CARD_GAP, area.top(), area.bottom() + 1 - h))
        self.move(chosen.topLeft())
        return chosen

    def cut_texts(self) -> list[str]:
        """The words this card cuts or crowds now: an ellipsis in a row, or the buttons running into the tag (none
        for the results it is built for; tests and the harness)."""
        content = self._content
        inner = CARD_WIDTH - 2 * CARD_PAD[1]
        cut: list[str] = []
        kicker = QFontMetricsF(self._kicker_font).horizontalAdvance(content.kicker.upper())
        if kicker > inner - 14:
            cut.append("kicker")
        for name, text, font, lines in (("title", content.title, self._title_font, self.TITLE_LINES),
                                        ("meta", content.meta, self._meta_font, self.META_LINES),
                                        ("note", content.note, self._meta_font, self.NOTE_LINES)):
            if text and not _whole(text, self._wrap(text, font, lines), font, inner):
                cut.append(name)
        if self._buttons or content.tag:
            right = max((button.geometry().right() - holo.HoloButton.MARGIN for button in self._buttons.values()),
                        default=CARD_PAD[1])
            tag = QFontMetricsF(self._tag_font).horizontalAdvance(content.tag.upper()) if content.tag else 0.0
            if right + (12 + tag if tag else 0) > CARD_WIDTH - CARD_PAD[1]:
                cut.append("buttons")
        return cut

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        content = self._content
        tone = content.tone if content.tone in _CARD_LOOKS else TONE_CYAN
        fill, border = _CARD_LOOKS[tone]
        ink = QColor(_TONE_INK[tone])
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = QRectF(self.rect())
            edge = rect.adjusted(0.5, 0.5, -0.5, -0.5)
            path = QPainterPath()
            path.addRoundedRect(edge, CARD_RADIUS, CARD_RADIUS)
            painter.fillPath(path, QColor(fill))
            painter.save()
            painter.setClipPath(path)
            painter.fillRect(rect, holo.scan_brush(holo.SCAN_AMBER if tone == TONE_AMBER else holo.SCAN))
            painter.restore()
            painter.setPen(QPen(border, 1.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            holo.paint_brackets(painter, rect, ink, leg=CARD_BRACKET, radius=CARD_RADIUS, width=2.0)
            align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            kicker = self.row_rect("kicker")
            dot = QPointF(kicker.left() + 3, kicker.center().y())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(holo.alpha(ink, 0.3))
            painter.drawEllipse(dot, 5.0, 5.0)
            painter.setBrush(ink)
            painter.drawEllipse(dot, 3.0, 3.0)
            painter.setFont(self._kicker_font)
            painter.setPen(ink)
            room = kicker.adjusted(14, 0, 0, 0)
            painter.drawText(room, align, QFontMetricsF(self._kicker_font).elidedText(
                content.kicker.upper(), Qt.TextElideMode.ElideRight, room.width()))
            title = self.row_rect("title")
            painter.setFont(self._title_font)
            painter.setPen(QColor(holo.AMBER_TX if tone == TONE_AMBER else holo.INK_1))
            for index, line in enumerate(self._title_lines()):
                painter.drawText(QRectF(title.left(), title.top() + 24 * index, title.width(), 24), align, line)
            painter.setFont(self._meta_font)
            meta = self.row_rect("meta")
            painter.setPen(QColor(holo.AMBER_TX2 if tone == TONE_AMBER else holo.INK_3))
            for index, line in enumerate(self._wrap(content.meta, self._meta_font, self.META_LINES)):
                painter.drawText(QRectF(meta.left(), meta.top() + 20 * index, meta.width(), 20), align, line)
            note = self.row_rect("note")
            painter.setPen(QColor({TONE_DONE: holo.OK, TONE_AMBER: holo.AMBER_TX2}.get(content.note_tone,
                                                                                      holo.INK_3)))
            for index, line in enumerate(self._wrap(content.note, self._meta_font, self.NOTE_LINES)):
                painter.drawText(QRectF(note.left(), note.top() + 20 * index, note.width(), 20), align, line)
            if content.tag:
                row = self.row_rect("buttons")
                painter.setFont(self._tag_font)
                painter.setPen(QColor(holo.INK_4))
                painter.drawText(row, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                                 content.tag.upper())
        finally:
            painter.end()

# --------------------------------------------------------------------------
# The decor layer (one per screen)
# --------------------------------------------------------------------------


@dataclass(frozen=True, eq=True)
class Bracket:
    """Corner brackets round a global rect (a window Jarvis opened; a field in control), with an optional tag."""

    rect: QRect
    tone: str = TONE_CYAN
    tag: str = ""                      # "Downloads <middle dot> 8 items", "WORD"
    small: bool = False                # a field: 10 px legs and a faint fill


@dataclass(frozen=True, eq=True)
class Tether:
    """A dashed line through global points, a dot at each end and a mono label beside the first point."""

    points: tuple[QPoint, ...]
    tone: str = TONE_CYAN
    label: str = ""


@dataclass(frozen=True, eq=True)
class Glow:
    """The outer shadow and the glow under an interactive window's rounded shape (a global rect): the bar
    (``GLOW_BAR``), a chip (``GLOW_CHIP``) or a card (``GLOW_CARD``), cyan or amber (Switch.dc.html box-shadows)."""

    rect: QRect
    radius: float
    look: str = GLOW_BAR
    tone: str = TONE_CYAN


def _glow_tint(glow: Glow) -> tuple[float, float]:
    return _GLOW_TINT.get((glow.look, glow.tone)) or _GLOW_TINT[(glow.look, TONE_CYAN)]


def glow_margin(glow: Glow) -> int:
    """How far ``glow``'s shadows reach beyond its rect (the offset and the blur)."""
    offset, blur, _alpha = _GLOW_DARK[glow.look]
    return int(math.ceil(max(abs(offset) + blur, _glow_tint(glow)[0])))


def glow_extent(glow: Glow) -> QRect:
    """Where ``glow`` paints (global): its rect and the room its shadows reach."""
    margin = glow_margin(glow)
    return glow.rect.adjusted(-margin, -margin, margin, margin)


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _paint_box_shadow(painter: QPainter, shape: QRectF, radius: float, offset: float, blur: float, ink: QColor,
                      alpha: float) -> None:
    """One CSS box-shadow of a rounded rect (no spread): its Gaussian falloff (sigma = blur / 2) as nested rounded
    rects from the outside in, each adding what the falloff gains inside it (painted over each other, so no seam)."""
    sigma = max(0.5, blur / 2.0)
    shape = shape.translated(0.0, offset)
    deepest = min(2.0 * sigma, shape.width() / 2.0, shape.height() / 2.0)     # how far in the falloff is painted
    step = 1.0 if sigma <= 12 else 2.0
    grows: list[float] = []
    grow = 2.0 * sigma
    while grow > -deepest:
        grows.append(grow)
        grow -= step
    grows.append(-deepest)
    color = QColor(ink)
    painted = 0.0                                 # the alpha already painted inside the next rect
    for index, grow in enumerate(grows):
        inner = grows[index + 1] if index + 1 < len(grows) else grow
        wanted = alpha * _normal_cdf(-(grow + inner) / 2.0 / sigma)
        if wanted <= painted:
            continue
        box = shape.adjusted(-grow, -grow, grow, grow)
        if box.width() <= 0 or box.height() <= 0:
            continue
        corner = max(0.0, min(radius + grow, box.width() / 2.0, box.height() / 2.0))
        color.setAlphaF(min(1.0, (wanted - painted) / (1.0 - painted)))
        path = QPainterPath()
        path.addRoundedRect(box, corner, corner)
        painter.fillPath(path, color)
        painted = wanted


_GLOW_SPRITES: dict[tuple, QImage] = {}
_GLOW_SPRITES_KEPT = 24


def glow_sprite(glow: Glow, dpr: float) -> QImage:
    """``glow``'s shadows painted once per size, look, tone and device pixel ratio (cached): an image of
    ``glow_extent(glow)``'s size. The part the window's own opaque fill covers is left out (never seen)."""
    rect = glow.rect
    key = (rect.width(), rect.height(), float(glow.radius), glow.look, glow.tone, round(float(dpr), 3))
    sprite = _GLOW_SPRITES.get(key)
    if sprite is not None:
        return sprite
    margin = glow_margin(glow)
    width, height = rect.width() + 2 * margin, rect.height() + 2 * margin
    sprite = QImage(max(1, round(width * dpr)), max(1, round(height * dpr)), QImage.Format.Format_ARGB32_Premultiplied)
    sprite.setDevicePixelRatio(dpr)
    sprite.fill(Qt.GlobalColor.transparent)
    shape = QRectF(margin, margin, rect.width(), rect.height())
    painter = QPainter(sprite)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Under the window's own opaque shape nothing shows: leave its middle (the rect less its corners) out.
        middle = QRegion(QRect(0, 0, math.ceil(width), math.ceil(height)))
        r = math.ceil(glow.radius)
        for cut in (shape.toRect().adjusted(r, 1, -r, -1), shape.toRect().adjusted(1, r, -1, -r)):
            if cut.isValid():
                middle = middle.subtracted(QRegion(cut))
        painter.setClipRegion(middle)
        blur, tint_alpha = _glow_tint(glow)
        tint = holo.AMBER if glow.tone == TONE_AMBER else holo.HOLO
        _paint_box_shadow(painter, shape, glow.radius, 0.0, blur, QColor(tint), tint_alpha)   # the glow, under
        offset, blur, dark_alpha = _GLOW_DARK[glow.look]
        _paint_box_shadow(painter, shape, glow.radius, offset, blur, QColor(0, 0, 0), dark_alpha)   # the shadow
    finally:
        painter.end()
    while len(_GLOW_SPRITES) >= _GLOW_SPRITES_KEPT:
        _GLOW_SPRITES.pop(next(iter(_GLOW_SPRITES)))
    _GLOW_SPRITES[key] = sprite
    return sprite


class DecorLayer(_OverlayWindow):
    """The shadows and glows under the interactive windows, brackets, tethers, tags and the in-control glow on one
    screen; see the module docstring."""

    def __init__(self, screen: QScreen, *, native: HudNative | None = None, hide_from_capture: bool = True) -> None:
        super().__init__(DECOR_FLAGS, native, hide_from_capture)
        self.setObjectName("JarvisDecor")
        self.setWindowTitle("Jarvis")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._screen = screen
        self._glows: tuple[Glow, ...] = ()
        self._brackets: tuple[Bracket, ...] = ()
        self._tethers: tuple[Tether, ...] = ()
        self._glow = False
        self._tag_font = hud.display_font(11, 500, 0.04)
        self._label_font = hud.mono_font(9, 500, 0.18)
        self.sync_geometry()

    def screen_ref(self) -> QScreen:
        return self._screen

    def sync_geometry(self) -> None:
        """Cover the screen (after it moved or changed size)."""
        if shiboken6.isValid(self._screen):
            self.setScreen(self._screen)
            self.setGeometry(self._screen.geometry())

    def set_decor(self, brackets: Sequence[Bracket] = (), tethers: Sequence[Tether] = (), glow: bool = False,
                  glows: Sequence[Glow] = ()) -> bool:
        """The items on this screen (the others are left out); repaints only on a change - only where a shadow was
        and is, when only the shadows changed. True when it changed."""
        geometry = self.geometry()
        mine_g = tuple(item for item in glows if glow_extent(item).intersects(geometry))
        mine_b = tuple(item for item in brackets if item.rect.intersects(geometry))
        mine_t = tuple(item for item in tethers if any(geometry.contains(point) for point in item.points)
                       or _bounds(item.points).intersects(geometry))
        new = (mine_b, mine_t, bool(glow))
        old = (self._brackets, self._tethers, self._glow)
        if new == old and mine_g == self._glows:
            return False
        if new == old:
            dirty = QRegion()
            for item in [*(g for g in self._glows if g not in mine_g), *(g for g in mine_g if g not in self._glows)]:
                dirty = dirty.united(glow_extent(item).translated(-geometry.topLeft()))
            self._glows = mine_g
            self.update(dirty)
            return True
        self._brackets, self._tethers, self._glow = new
        self._glows = mine_g
        self.update()
        return True

    def has_decor(self) -> bool:
        return bool(self._glows or self._brackets or self._tethers or self._glow)

    def glows(self) -> tuple[Glow, ...]:
        return self._glows

    def brackets(self) -> tuple[Bracket, ...]:
        return self._brackets

    def tethers(self) -> tuple[Tether, ...]:
        return self._tethers

    def glowing(self) -> bool:
        return self._glow

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if not self.has_decor():
            return
        painter = QPainter(self)
        try:
            dirty = event.rect()
            origin = self.geometry().topLeft()
            dpr = self.devicePixelRatioF() or 1.0
            for glow in self._glows:                    # under everything: the interactive windows' shadows
                extent = glow_extent(glow).translated(-origin)
                if extent.intersects(dirty):
                    painter.drawImage(QPointF(extent.topLeft()), glow_sprite(glow, dpr))
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            if self._glow:
                self._paint_glow(painter)
            painter.translate(-QPointF(origin))
            for tether in self._tethers:
                self._paint_tether(painter, tether)
            for bracket in self._brackets:
                self._paint_bracket(painter, bracket)
        finally:
            painter.end()

    def _paint_glow(self, painter: QPainter) -> None:
        """The in-control edge glow (DesktopB .sglow), static: a 3 px amber frame and a 70 px inner fade."""
        rect = QRectF(self.rect())
        amber = QColor(holo.AMBER)
        for start, end, strip in (
                (rect.topLeft(), QPointF(rect.left(), rect.top() + GLOW_DEPTH),
                 QRectF(rect.left(), rect.top(), rect.width(), GLOW_DEPTH)),
                (rect.bottomLeft(), QPointF(rect.left(), rect.bottom() - GLOW_DEPTH),
                 QRectF(rect.left(), rect.bottom() - GLOW_DEPTH, rect.width(), GLOW_DEPTH)),
                (rect.topLeft(), QPointF(rect.left() + GLOW_DEPTH, rect.top()),
                 QRectF(rect.left(), rect.top(), GLOW_DEPTH, rect.height())),
                (rect.topRight(), QPointF(rect.right() - GLOW_DEPTH, rect.top()),
                 QRectF(rect.right() - GLOW_DEPTH, rect.top(), GLOW_DEPTH, rect.height()))):
            fade = QLinearGradient(start, end)
            fade.setColorAt(0.0, holo.alpha(amber, 0.28))
            fade.setColorAt(1.0, holo.alpha(amber, 0.0))
            painter.fillRect(strip, fade)
        painter.setPen(QPen(holo.alpha(amber, 0.85), 3.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect.adjusted(1.5, 1.5, -1.5, -1.5))

    def _paint_bracket(self, painter: QPainter, bracket: Bracket) -> None:
        ink = QColor(_TONE_INK.get(bracket.tone, holo.HOLO))
        rect = QRectF(bracket.rect)
        leg = BRACKET_LEG_SMALL if bracket.small else BRACKET_LEG
        if bracket.small:
            painter.fillRect(rect, holo.alpha(ink, 0.06))
        holo.paint_brackets(painter, rect.adjusted(-2, -2, 2, 2), holo.alpha(ink, 0.30), leg=leg, radius=0,
                            corners=holo.BRACKETS_ALL, width=6.0)            # its soft glow
        holo.paint_brackets(painter, rect, ink, leg=leg, radius=0, corners=holo.BRACKETS_ALL, width=2.0)
        if bracket.tag:
            metrics = QFontMetricsF(self._tag_font)
            width = metrics.horizontalAdvance(bracket.tag) + 18
            x = rect.right() - 12 - width if bracket.small else rect.left() + 16
            tag = QRectF(x, rect.top() - 10, width, 19)
            fill = holo.TILE_AMBER if bracket.tone == TONE_AMBER else holo.TILE
            painter.setPen(QPen(holo.alpha(ink, 0.75), 1.0))
            painter.setBrush(QColor(fill))
            painter.drawRoundedRect(tag.adjusted(0.5, 0.5, -0.5, -0.5), 9.5, 9.5)
            painter.setFont(self._tag_font)
            painter.setPen(QColor(holo.AMBER_HI if bracket.tone == TONE_AMBER else holo.HOLO_HI))
            painter.drawText(tag, int(Qt.AlignmentFlag.AlignCenter), bracket.tag)

    def _paint_tether(self, painter: QPainter, tether: Tether) -> None:
        if len(tether.points) < 2:
            return
        ink = QColor(_TONE_INK.get(tether.tone, holo.HOLO))
        path = QPainterPath(QPointF(tether.points[0]))
        for point in tether.points[1:]:
            path.lineTo(QPointF(point))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(holo.alpha(ink, 0.16), 6.0))
        painter.drawPath(path)
        dashed = QPen(ink, 1.2)
        dashed.setDashPattern([5 / 1.2, 4 / 1.2])
        painter.setPen(dashed)
        painter.drawPath(path)
        first, last = QPointF(tether.points[0]), QPointF(tether.points[-1])
        painter.setPen(QPen(ink, 1.0))
        painter.drawEllipse(first, 6.0, 6.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ink)
        painter.drawEllipse(first, 2.5, 2.5)
        painter.drawEllipse(last, 3.0, 3.0)
        if tether.label:
            painter.setFont(self._label_font)
            painter.setPen(QColor(holo.HOLO_HI if tether.tone == TONE_CYAN else ink))
            painter.drawText(QPointF(first.x() + 10, first.y() - 9), tether.label.upper())


def _bounds(points: Sequence[QPoint]) -> QRect:
    if not points:
        return QRect()
    xs, ys = [p.x() for p in points], [p.y() for p in points]
    return QRect(QPoint(min(xs), min(ys)), QPoint(max(xs), max(ys)))

# --------------------------------------------------------------------------
# The set
# --------------------------------------------------------------------------


class HudOverlay(QObject):
    """The HUD window, the result cards and one DecorLayer per screen; see the module docstring."""

    def __init__(self, *, hud_key: str = "", hide_from_capture: bool = True, native: HudNative | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._native: HudNative = native if native is not None else default_native()
        self._hide_from_capture = bool(hide_from_capture)
        self._shown = False
        self._closed = False
        self._cards: list[ResultCard] = []
        self._placed: list[ResultCard] = []             # the cards show_card placed: they show with the HUD
        self._layers: list[DecorLayer] = []
        self._decor: tuple[tuple[Bracket, ...], tuple[Tether, ...], bool] = ((), (), False)
        self._toast: QWidget | None = None
        self._rec_pill: QWidget | None = None
        self._stack: list[QWidget] = []
        self._screen: QScreen | None = None            # the screen the HUD shows on
        self.hud = HudWindow(hud_key=hud_key, native=self._native, hide_from_capture=self._hide_from_capture)
        self.hud.shapeChanged.connect(self._apply_decor)   # its shadows follow the bar and the chips
        app = QGuiApplication.instance()
        if app is not None:
            app.screenAdded.connect(self._sync_screens)
            app.screenRemoved.connect(self._sync_screens)
        self._sync_screens()

    # ---- the windows -----------------------------------------------------------------------

    def windows(self) -> list[QWidget]:
        return [self.hud, *self._cards, *self._layers]

    def layers(self) -> list[DecorLayer]:
        return list(self._layers)

    def layer_for(self, screen: QScreen) -> DecorLayer | None:
        return next((layer for layer in self._layers if layer.screen_ref() is screen), None)

    def cards(self) -> list[ResultCard]:
        return list(self._cards)

    def native(self) -> HudNative:
        return self._native

    def _sync_screens(self, *_args: Any) -> None:
        if self._closed:
            return
        screens = list(QGuiApplication.screens())
        for layer in list(self._layers):
            if not any(screen is layer.screen_ref() for screen in screens) or not shiboken6.isValid(
                    layer.screen_ref()):
                self._layers.remove(layer)
                layer.hide()
                layer.deleteLater()
        for screen in screens:
            layer = self.layer_for(screen)
            if layer is None:
                layer = DecorLayer(screen, native=self._native, hide_from_capture=self._hide_from_capture)
                screen.geometryChanged.connect(layer.sync_geometry)
                screen.availableGeometryChanged.connect(self._on_area_changed)
                self._layers.append(layer)
            else:
                layer.sync_geometry()
        if self._shown and not any(screen is self._screen for screen in screens):
            self._place(None)                            # its screen went: the primary screen
        self._apply_decor()

    def _on_area_changed(self, *_args: Any) -> None:
        if self._shown and not self._closed:
            self._place(self._screen)                    # the taskbar moved, or the screen changed size

    def _place(self, screen: QScreen | None) -> None:
        screen = screen if screen is not None and shiboken6.isValid(screen) else QGuiApplication.primaryScreen()
        self._screen = screen
        self.hud.place_on(screen)

    def hud_screen(self) -> QScreen | None:
        """The screen the HUD shows on (None before it first shows)."""
        return self._screen

    # ---- showing ---------------------------------------------------------------------------

    def show_on(self, screen: QScreen | None = None) -> None:
        """Place the HUD on ``screen`` (the one the Jarvis window is on: ``screen_for(window)``; the primary screen
        for None) and show it, its decor - with the shadows and glows under the bar, the chips and the cards - and its
        cards, without the focus, without raising anything. The meetings RecPill is held hidden meanwhile (REC is a
        chip here); then the stacking order: the interactive windows above the layers."""
        self._place(screen)
        self._shown = True
        self._hold(self._rec_pill, True)
        self._sync_decor()
        for layer in self._layers:
            if layer.has_decor():
                layer.show_quietly()
        for card in self._cards:
            if card in self._placed:
                card.show_quietly()
        self.hud.show_quietly()
        self.restack()

    def hide(self) -> None:
        """Hide every HUD window (the layers and their shadows too); the RecPill may show again (at its next tick
        while a meeting records)."""
        self._shown = False
        for window in self.windows():
            window.hide()
        self._stack = []
        self._sync_decor()                               # no shadows while the HUD is away
        self._hold(self._rec_pill, False)

    def is_shown(self) -> bool:
        return self._shown

    def new_card(self) -> ResultCard:
        """A card of this HUD (hidden until ``show_card`` places it)."""
        card = ResultCard(native=self._native, hide_from_capture=self._hide_from_capture)
        card.shapeChanged.connect(self._apply_decor)    # its shadow follows it
        self._cards.append(card)
        return card

    def show_card(self, card: ResultCard, target: QRect, area: QRect | None = None) -> QRect:
        """Place ``card`` beside ``target`` (global), clear of the HUD's bar and chips, and show it while the HUD
        shows (from now on it shows with the HUD, its shadow under it)."""
        avoid = [self.hud.occupied_rect()] if self._shown or self.hud.area().isValid() else []
        rect = card.place_beside(target, area, avoid)
        if card in self._cards and card not in self._placed:
            self._placed.append(card)
        if self._shown:
            card.show_quietly()
            self.restack()
        self._apply_decor()
        return rect

    def remove_card(self, card: ResultCard) -> None:
        if card in self._cards:
            self._cards.remove(card)
            if card in self._placed:
                self._placed.remove(card)
            card.hide()
            card.deleteLater()
            self._stack = [window for window in self._stack if window is not card]
            self._apply_decor()

    def set_decor(self, brackets: Sequence[Bracket] = (), tethers: Sequence[Tether] = (), glow: bool = False) -> None:
        """What the DecorLayers draw (global items; each screen draws its part). A layer shows only while it has
        something to draw, and the interactive windows go back above it."""
        self._decor = (tuple(brackets), tuple(tethers), bool(glow))
        self._apply_decor()

    def glows(self) -> list[Glow]:
        """The shadows and glows under the interactive windows while the HUD shows: the bar's, the shown chips' and
        each placed card's (none while it is hidden)."""
        if not self._shown or self._closed:
            return []
        return [*self.hud.glows(), *(card.glow() for card in self._cards if card in self._placed)]

    def _sync_decor(self) -> None:
        """Each layer's items as they are now (a layer repaints only what changed)."""
        brackets, tethers, glow = self._decor
        glows = self.glows()
        for layer in self._layers:
            layer.set_decor(brackets, tethers, glow, glows)

    def _apply_decor(self) -> None:
        if self._closed:
            return
        self._sync_decor()
        if not self._shown:
            return
        shown_new = False
        for layer in self._layers:
            if layer.has_decor() and layer.isHidden():
                layer.show_quietly()
                shown_new = True
            elif not layer.has_decor() and not layer.isHidden():
                layer.hide()
        if shown_new:
            self.restack()                               # the interactive windows above the layer it showed

    # ---- stacking --------------------------------------------------------------------------

    def set_neighbours(self, *, toast: QWidget | None = None, rec_pill: QWidget | None = None) -> None:
        """Jarvis's other always-on-top windows (any time; None for none): the Toast (states_view.Toast, bottom right
        of the primary screen) and the meetings RecPill (meetings_view.RecPill, top centre - where the bar is).
        While the HUD shows, the pill is held hidden (``set_held``; REC is a chip of the HUD's own) and ``restack``
        keeps both off the bar."""
        if self._rec_pill is not None and self._rec_pill is not rec_pill:
            self._hold(self._rec_pill, False)
        self._toast, self._rec_pill = toast, rec_pill
        if self._shown:
            self._hold(rec_pill, True)
            self.restack()

    def neighbours(self) -> tuple[QWidget | None, QWidget | None]:
        """(toast, rec_pill) as ``set_neighbours`` gave them (None once deleted)."""
        return _alive(self._toast), _alive(self._rec_pill)

    @staticmethod
    def _hold(pill: QWidget | None, held: bool) -> None:
        pill = _alive(pill)
        if pill is None:
            return
        set_held = getattr(pill, "set_held", None)
        if callable(set_held):
            set_held(held)
        elif held:
            pill.hide()

    def stack_order(self) -> list[QWidget]:
        """Bottom to top, the windows ``restack`` put in order last."""
        return list(self._stack)

    def restack(self) -> None:
        """Bottom to top, those showing: the DecorLayers; the RecPill (held hidden while the HUD shows; were it up,
        never over a card or the bar); the cards; the Toast (bottom right: over a card it may cover, never near the
        bar); the HUD window last, so nothing of Jarvis's is ever over the bar. Never a raise or an activation: the
        native seam moves each to the top of the always-on-top band in this order (SWP_NOACTIVATE)."""
        toast, pill = self.neighbours()
        order: list[QWidget] = [layer for layer in self._layers if layer.isVisible()]
        if pill is not None and pill.isVisible():
            order.append(pill)
        order += [card for card in self._cards if card.isVisible()]
        if toast is not None and toast.isVisible():
            order.append(toast)
        if self.hud.isVisible():
            order.append(self.hud)
        self._stack = order
        if order:
            self._native.stack([int(window.winId()) for window in order])

    def shutdown(self) -> None:
        """Hide and delete every HUD window (the app quits, or [desktop] went off)."""
        if self._closed:
            return
        self._closed = True
        app = QGuiApplication.instance()
        if app is not None:
            for signal in (app.screenAdded, app.screenRemoved):
                try:
                    signal.disconnect(self._sync_screens)
                except (RuntimeError, TypeError):
                    pass
        self.hide()
        for window in self.windows():
            window.deleteLater()
        self._cards, self._placed, self._layers = [], [], []


def _alive(widget: QWidget | None) -> QWidget | None:
    return widget if widget is not None and shiboken6.isValid(widget) else None


def create(config: Any, *, native: HudNative | None = None) -> HudOverlay | None:
    """The HUD's windows while [desktop] is on (hidden until shown); None while it is off - nothing is created. The
    keycaps say the HUD key that can work (settings_view.effective_hud_key: none while [hotkey] is off)."""
    desktop = getattr(config, "desktop", None)
    if not getattr(desktop, "enabled", False):
        return None
    return HudOverlay(hud_key=settings_view.effective_hud_key(config),
                      hide_from_capture=bool(getattr(desktop, "hide_from_capture", True)), native=native)
