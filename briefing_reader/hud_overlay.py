"""Jarvis on your desktop: the HUD's windows (SPEC section 8.1; Phase 1, part 3; DesktopB.dc.html, Switch.dc.html).

Qt only. Nothing here reads, opens or changes anything outside Jarvis, and nothing here switches: the app (a later
part) shows and hides these windows and answers their signals. Separate top-level windows by job, never one big
partly click-through window; each is ``Tool | FramelessWindowHint | WindowStaysOnTopHint | WindowDoesNotAcceptFocus``
with ``WA_TranslucentBackground`` and ``WA_ShowWithoutActivating`` (states_view.TOAST_FLAGS): no taskbar button,
never the focus. Their margins are fully transparent, so clicks there reach the desktop (no setMask); every fill is
opaque (``field`` / ``tile``, never a blur), and no window that takes clicks paints a glow or a shadow (any alpha
above 0 catches clicks): the glows live in the DecorLayer.

``HudWindow``
    The orb (``OrbWidget(ORB_PX)``: the compact level of detail in a 104 px widget, no ground, never resized per
    frame) over the left end of the command bar, a 760 px bar centred in the work area 16 px from its top
    (Switch.dc.html ``.hud``): the 44 px input row (a second ``hud.CommandBar`` in its HUD look: the chevron, "Ask
    Jarvis", the HUD key's keycaps), Jarvis's caption line with the orb's state word, a hairline and "Bring Jarvis
    back"; under the bar the chips: "Briefing ready" (cyan), "N need your OK" (amber; ui.needs_ok_chip's words) and
    REC with a coral dot and the time. ``place_on(screen)`` / ``place_in(area)``; ``set_caption(text)``;
    ``set_look(base, muted=, offline=)`` (the orb and the state word); ``set_briefing_ready(bool)``,
    ``set_pending(count)``, ``set_recording(elapsed, warning="")``; ``chip_texts()``; ``bar_rect()``,
    ``orb_center()`` (global). Signals ``bringBackClicked()`` and ``chipClicked(key)`` (``CHIP_BRIEFING``,
    ``CHIP_APPROVALS``, ``CHIP_REC``); the bar's own signals are on ``bar``. The bar's status (an answer, a question,
    a problem, what Jarvis is doing) shows in the caption line without its technical readouts. While idle the window
    keeps ``WindowDoesNotAcceptFocus``: the input takes the keyboard only after the HUD key or a click (a later part).
    Its paint is one cached pixmap (the panel, the hairlines, the orb's dark disc): an orb frame repaints 104 px of it.

``ResultCard``
    A result floating beside the thing it is about (DesktopB ``.glass``): the kicker with its dot, the title, a meta
    line, a row of buttons (NoFocus; a click emits ``buttonClicked(key)``) with a mono tag at its right ("2 OTHER
    MATCHES") and a note. ``set_content(CardContent)``; tones ``TONE_CYAN`` (Jarvis points at something),
    ``TONE_AMBER`` (a decision waits), ``TONE_DONE``. ``place_beside(target, area=None)`` puts it beside a global
    rect, never over it, kept inside the work area (ui._keep_on_screen's rule).

``DecorLayer``
    One per QScreen, sized to its geometry, also ``WindowTransparentForInput``: the cyan or amber brackets (all four
    corners, a tag on the top edge), tethers (a dashed line with a dot at each end and a mono label) and the amber
    in-control edge glow, from global ``Bracket`` / ``Tether`` items (a bracket over two screens is drawn on each).
    Static: it repaints only when its items change and has no timer; with nothing to draw it stays hidden.

``HudOverlay``
    The set: the HUD window, the cards and one DecorLayer per screen (screens coming and going follow).
    ``show_on(screen)`` / ``hide()`` / ``is_shown()``; ``new_card()``, ``show_card(card, target)``,
    ``remove_card(card)``, ``cards()``; ``set_decor(brackets, tethers, glow=False)``; ``layers()``; ``windows()``;
    ``set_neighbours(toast=, rec_pill=)`` and ``restack()``: bottom to top the DecorLayers, the cards, the HUD window,
    then the meetings RecPill and the Toast when they show (the recording sign and Jarvis's toast are never under
    the HUD); ``stack_order()``; ``shutdown()``. ``create(config, native=None)`` is None while [desktop] is off: no
    HUD window then exists at all.

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
    QFontMetricsF,
    QGuiApplication,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
    QScreen,
)
from PySide6.QtWidgets import QAbstractButton, QSizePolicy, QWidget

from . import holo, hud, offer_view
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
CAPTION_HEIGHT = 35            # the caption line: a hairline, 7 px, one 20 px line, 7 px
BAR_HEIGHT = 1 + ROW_HEIGHT + CAPTION_HEIGHT + 1
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
WINDOW_HEIGHT = BAR_TOP + BAR_HEIGHT + CHIP_GAP + CHIP_HEIGHT + 2
BACK_GAP = 12                               # around the hairline before "Bring Jarvis back"
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


def hud_key_caps(label: str) -> list[str]:
    """"Ctrl+Alt+H" (or "ctrl+alt+h") -> ["CTRL", "ALT", "H"]; "" -> []."""
    text = describe_combo(label) if label else ""
    return [part.upper() for part in text.split("+") if part]


def screen_area(point: QPoint) -> QRect:
    """The work area of the screen at ``point`` (the primary screen's when none is there)."""
    screen = QGuiApplication.screenAt(point) or QGuiApplication.primaryScreen()
    return screen.availableGeometry() if screen is not None else QRect()

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
        return bool(self._excluded) and self._excluded == int(self.winId())

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
    """The HUD key as keycaps after the input (DesktopB .kbd: mono 9, ink-3, a 2 px bottom edge)."""

    GAP = 6
    HEIGHT = 18

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
        return QFontMetricsF(self._font).horizontalAdvance(key) + 12

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
    """Jarvis's caption (Sora 14 light, one line, cut with an ellipsis; the whole text is its tooltip) or the bar's
    status in its tone, and at the right the orb's state word with its dot (DesktopB .cap, .status)."""

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

    def _word_rect(self) -> QRectF:
        width = QFontMetricsF(self._word_font).horizontalAdvance(self._word) + 2
        return QRectF(self.width() - width, 0, width, self.height())

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            align = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            word = self._word_rect()
            dot = QPointF(word.left() - 7 - 3.5, self.height() / 2)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(holo.alpha(self._dot, 0.22))
            painter.drawEllipse(dot, 6.0, 6.0)
            painter.setBrush(self._dot)
            painter.drawEllipse(dot, 3.5, 3.5)
            painter.setFont(self._word_font)
            painter.setPen(QColor(holo.INK_3))
            painter.drawText(word, align, self._word)
            room = max(0.0, dot.x() - 6 - 14)
            painter.setFont(self._font)
            painter.setPen(self._color())
            text = QFontMetricsF(self._font).elidedText(self.text(), Qt.TextElideMode.ElideRight, room)
            painter.drawText(QRectF(0, 0, room, self.height()), align, text)
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
    ping, no blinking dot: only the orb loops); NoFocus, a click emits ``clicked``."""

    PAD = 12

    def __init__(self, key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._key = key if key in _CHIP_LOOKS else CHIP_BRIEFING
        self._hover = False
        self._font = hud.body_font(13, 500)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def key(self) -> str:
        return self._key

    def setText(self, text: str) -> None:  # noqa: N802 - Qt override
        super().setText(text)
        self.setAccessibleName(text)
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        width = self.PAD + 7 + 8 + QFontMetricsF(self._font).horizontalAdvance(self.text()) + 8 + 14 + self.PAD
        return QSize(math.ceil(width), CHIP_HEIGHT)

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
            painter.setFont(self._font)
            painter.setPen(QColor(ink))
            width = QFontMetricsF(self._font).horizontalAdvance(self.text())
            painter.drawText(QRectF(x, 0, width + 2, self.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.text())
            holo.paint_icon(painter, QRectF(x + width + 8, rect.center().y() - 7, 14, 14), "chevron",
                            QColor(chevron), stroke=2.0)
        finally:
            painter.end()


class HudWindow(_OverlayWindow):
    """The orb, the command bar, the caption line, Bring Jarvis back and the chips; see the module docstring."""

    bringBackClicked = Signal()
    chipClicked = Signal(str)

    def __init__(self, *, hud_key: str = "", native: HudNative | None = None, hide_from_capture: bool = True) -> None:
        super().__init__(HUD_FLAGS, native, hide_from_capture)
        self.setObjectName("JarvisHud")
        self.setWindowTitle("Jarvis")
        self.setAccessibleName("Jarvis on your desktop")
        self._bar_width = BAR_WIDTH
        self._hairline_x = 0
        self._background: QPixmap | None = None
        self._background_key: tuple | None = None
        self._pending = 0
        self._recording = ""
        self._warning = ""
        self.orb = orb_mod.OrbWidget(ORB_PX, self)          # no ground: the window is translucent
        self.keycaps = _KeyCaps(hud_key)
        self.bar = hud.CommandBar(self)
        self.bar.set_hud(self.keycaps)
        self.caption = _CaptionLine(self)
        self.back_button = holo.HoloButton(BRING_BACK_TEXT, holo.GHOST, icon="window", parent=self)
        self.back_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)     # a click never moves the focus here
        self.back_button.setAccessibleName(BRING_BACK_TEXT)
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
        room = area.width() - 2 * SCREEN_MARGIN - ORB_LEFT - RIGHT_MARGIN
        self._bar_width = max(MIN_BAR_WIDTH, min(BAR_WIDTH, room))
        width = ORB_LEFT + self._bar_width + RIGHT_MARGIN
        bar_left = area.x() + (area.width() - self._bar_width) // 2
        left = max(area.x(), min(bar_left - ORB_LEFT, area.x() + area.width() - width))
        self.setGeometry(left, area.y(), width, WINDOW_HEIGHT)
        self._layout()

    def bar_rect_local(self) -> QRect:
        return QRect(ORB_LEFT, BAR_TOP, self._bar_width, BAR_HEIGHT)

    def bar_rect(self) -> QRect:
        """The bar in global coordinates."""
        return self.bar_rect_local().translated(self.geometry().topLeft())

    def orb_center(self) -> QPoint:
        """The orb's centre in global coordinates."""
        return self.geometry().topLeft() + QPoint(ORB_LEFT - 2, BAR_TOP + ORB_DROP)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._layout()

    def _layout(self) -> None:
        bar = self.bar_rect_local()
        center = QPoint(ORB_LEFT - 2, BAR_TOP + ORB_DROP)
        self.orb.move(center.x() - self.orb.width() // 2, center.y() - self.orb.height() // 2)
        back = self.back_button.sizeHint()
        margin = holo.HoloButton.MARGIN
        back_x = bar.x() + bar.width() - BACK_GAP - back.width() + margin
        self.back_button.setGeometry(back_x, bar.y() + (bar.height() - back.height()) // 2, back.width(),
                                     back.height())
        self._hairline_x = back_x + margin - BACK_GAP
        left = bar.x() + ORB_PAD
        width = max(0, self._hairline_x - SIDE_PAD - left)
        self.bar.setGeometry(left, bar.y() + 1, width, ROW_HEIGHT)
        self.caption.setGeometry(left, bar.y() + 1 + ROW_HEIGHT + 1, width, CAPTION_HEIGHT - 1)
        x, y = left, bar.y() + bar.height() + CHIP_GAP
        for chip in self.chips.values():
            if chip.isHidden():
                continue
            hint = chip.sizeHint()
            chip.setGeometry(x, y, hint.width(), CHIP_HEIGHT)
            x += hint.width() + CHIP_GAP
        self._background = None
        self.update()

    # ---- what it shows ---------------------------------------------------------------------

    def set_caption(self, text: str) -> None:
        """Jarvis's line ("On your desktop, sir."); the bar's status replaces it while there is one."""
        self.caption.set_caption(text)

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


@dataclass(frozen=True)
class CardButton:
    key: str
    text: str
    variant: str = holo.GHOST          # holo.GO for the one thing to do ("Open"); GHOST for the rest


@dataclass(frozen=True)
class CardContent:
    kicker: str                        # "FILE · WORD DOCUMENT"
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
        self._kicker_font = holo.font("label")
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

    def place_beside(self, target: QRect, area: QRect | None = None) -> QRect:
        """Beside ``target`` (global): right of it, else left, above, below - inside ``area`` (the work area of the
        target's screen by default) and never over it; when nothing fits, the right side kept on screen."""
        area = QRect(area) if area is not None else screen_area(target.center())
        size = self.size()
        w, h = size.width(), size.height()

        def clamp(value: int, low: int, high: int) -> int:
            return max(low, min(value, high))

        top = clamp(target.top(), area.top(), area.bottom() + 1 - h)
        left = clamp(target.left(), area.left(), area.right() + 1 - w)
        candidates = [QRect(target.right() + 1 + CARD_GAP, top, w, h),
                      QRect(target.left() - CARD_GAP - w, top, w, h),
                      QRect(left, target.top() - CARD_GAP - h, w, h),
                      QRect(left, target.bottom() + 1 + CARD_GAP, w, h)]
        chosen = next((rect for rect in candidates if area.contains(rect) and not rect.intersects(target)), None)
        if chosen is None:
            first = candidates[0]
            chosen = QRect(clamp(first.x(), area.left(), area.right() + 1 - w), first.y(), w, h)
        self.move(chosen.topLeft())
        return chosen

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
    tag: str = ""                      # "Downloads · 8 items", "WORD"
    small: bool = False                # a field: 10 px legs and a faint fill


@dataclass(frozen=True, eq=True)
class Tether:
    """A dashed line through global points, a dot at each end and a mono label beside the first point."""

    points: tuple[QPoint, ...]
    tone: str = TONE_CYAN
    label: str = ""


class DecorLayer(_OverlayWindow):
    """Brackets, tethers, tags and the in-control glow on one screen; see the module docstring."""

    def __init__(self, screen: QScreen, *, native: HudNative | None = None, hide_from_capture: bool = True) -> None:
        super().__init__(DECOR_FLAGS, native, hide_from_capture)
        self.setObjectName("JarvisDecor")
        self.setWindowTitle("Jarvis")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._screen = screen
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

    def set_decor(self, brackets: Sequence[Bracket] = (), tethers: Sequence[Tether] = (), glow: bool = False) -> bool:
        """The items on this screen (the others are left out); repaints only on a change. True when it changed."""
        geometry = self.geometry()
        mine_b = tuple(item for item in brackets if item.rect.intersects(geometry))
        mine_t = tuple(item for item in tethers if any(geometry.contains(point) for point in item.points)
                       or _bounds(item.points).intersects(geometry))
        new = (mine_b, mine_t, bool(glow))
        if new == (self._brackets, self._tethers, self._glow):
            return False
        self._brackets, self._tethers, self._glow = new
        self.update()
        return True

    def has_decor(self) -> bool:
        return bool(self._brackets or self._tethers or self._glow)

    def brackets(self) -> tuple[Bracket, ...]:
        return self._brackets

    def tethers(self) -> tuple[Tether, ...]:
        return self._tethers

    def glowing(self) -> bool:
        return self._glow

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        if not self.has_decor():
            return
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            if self._glow:
                self._paint_glow(painter)
            painter.translate(-QPointF(self.geometry().topLeft()))
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
        self._layers: list[DecorLayer] = []
        self._decor: tuple[tuple[Bracket, ...], tuple[Tether, ...], bool] = ((), (), False)
        self._toast: QWidget | None = None
        self._rec_pill: QWidget | None = None
        self._stack: list[QWidget] = []
        self.hud = HudWindow(hud_key=hud_key, native=self._native, hide_from_capture=self._hide_from_capture)
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
                self._layers.append(layer)
            else:
                layer.sync_geometry()
        self._apply_decor()

    # ---- showing ---------------------------------------------------------------------------

    def show_on(self, screen: QScreen | None = None) -> None:
        """Place the HUD on ``screen`` (the one the Jarvis window is on) and show it, its decor and its cards -
        without the focus, without raising anything; then the stacking order."""
        self.hud.place_on(screen)
        self._shown = True
        for layer in self._layers:
            if layer.has_decor():
                layer.show_quietly()
        for card in self._cards:
            card.show_quietly()
        self.hud.show_quietly()
        self.restack()

    def hide(self) -> None:
        self._shown = False
        for window in self.windows():
            window.hide()
        self._stack = []

    def is_shown(self) -> bool:
        return self._shown

    def new_card(self) -> ResultCard:
        card = ResultCard(native=self._native, hide_from_capture=self._hide_from_capture)
        self._cards.append(card)
        return card

    def show_card(self, card: ResultCard, target: QRect, area: QRect | None = None) -> QRect:
        """Place ``card`` beside ``target`` (global) and show it while the HUD shows."""
        rect = card.place_beside(target, area)
        if self._shown:
            card.show_quietly()
            self.restack()
        return rect

    def remove_card(self, card: ResultCard) -> None:
        if card in self._cards:
            self._cards.remove(card)
            card.hide()
            card.deleteLater()
            self._stack = [window for window in self._stack if window is not card]

    def set_decor(self, brackets: Sequence[Bracket] = (), tethers: Sequence[Tether] = (), glow: bool = False) -> None:
        """What the DecorLayers draw (global items; each screen draws its part). A layer shows only while it has
        something to draw, and the interactive windows go back above it."""
        self._decor = (tuple(brackets), tuple(tethers), bool(glow))
        self._apply_decor()

    def _apply_decor(self) -> None:
        brackets, tethers, glow = self._decor
        shown_new = False
        for layer in self._layers:
            layer.set_decor(brackets, tethers, glow)
            if not self._shown:
                continue
            if layer.has_decor() and layer.isHidden():
                layer.show_quietly()
                shown_new = True
            elif not layer.has_decor() and not layer.isHidden():
                layer.hide()
        if shown_new:
            self.restack()

    # ---- stacking --------------------------------------------------------------------------

    def set_neighbours(self, *, toast: QWidget | None = None, rec_pill: QWidget | None = None) -> None:
        """Jarvis's other always-on-top windows: the Toast (bottom right) and the meetings RecPill (top centre)."""
        self._toast, self._rec_pill = toast, rec_pill

    def stack_order(self) -> list[QWidget]:
        """Bottom to top, the windows ``restack`` put in order last."""
        return list(self._stack)

    def restack(self) -> None:
        """Bottom to top: the DecorLayers, the cards, the HUD window, the RecPill, the Toast (those showing)."""
        order: list[QWidget] = [layer for layer in self._layers if layer.isVisible()]
        order += [card for card in self._cards if card.isVisible()]
        if self.hud.isVisible():
            order.append(self.hud)
        for neighbour in (self._rec_pill, self._toast):
            if neighbour is not None and shiboken6.isValid(neighbour) and neighbour.isVisible():
                order.append(neighbour)
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
        self._cards, self._layers = [], []


def create(config: Any, *, native: HudNative | None = None) -> HudOverlay | None:
    """The HUD's windows while [desktop] is on (hidden until shown); None while it is off - nothing is created."""
    desktop = getattr(config, "desktop", None)
    if not getattr(desktop, "enabled", False):
        return None
    key = str(getattr(desktop, "hud_key", "") or "")
    return HudOverlay(hud_key=key, hide_from_capture=bool(getattr(desktop, "hide_from_capture", True)), native=native)
