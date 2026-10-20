"""Jarvis Hologram theme (the redesign's HOLOGRAM_SPEC sections 1-7 and 13), round 1.

The colour tokens, type roles, spacing, shapes, motion curves, the ground, the tile / button / chip
styles Home needs and the stroke icons. It lives beside ``hud.py``'s old tokens until round 2
restyles the other screens and removes those. Everything paints with QPainter; nothing here knows
about briefings, Google or Notion.

Public API
==========

Tokens
    Colours (hex strings): ``VOID``, ``VOID_EDGE``, ``DOCK``, ``TILE``, ``TILE_FOCUS``, ``TILE_AMBER``,
    ``TILE_OK``, ``TILE_ALERT``, ``RAISED``, ``FIELD``, ``PICTURE``, ``FOCUS``, ``HOLO`` (+ ``_HI``,
    ``_LO``, ``_INK``), ``INK_1`` .. ``INK_4``, ``INK_KICKER``, ``AMBER`` (+ ``_HI``, ``_LO``, ``_INK``,
    ``_TX``, ``_TX2``), ``OK``, ``OK_INK``, ``ALERT`` (+ ``_HI``, ``_LO``, ``_TX``, ``_TX2``, ``_INK``),
    ``STEEL``. Translucent tokens (QColor): ``GRID``, ``SCAN``, ``SCAN_AMBER``, ``LINE_FAINT``,
    ``LINE``, ``LINE_STRONG``, ``LINE_FOCUS``, ``LINE_INPUT``, ``AMBER_LINE``.
    ``alpha(color, a) -> QColor``; ``mix(a, b, t) -> QColor``.
    Spacing ``SPACE`` (2xs .. 6xl) and the named sizes (``DOCK_WIDTH``, ``HEADER_HEIGHT``, ...);
    radii ``RADIUS_*``.
Type
    ``TypeRole`` and ``ROLES`` (wordmark, greeting, question, caption, caption_sm, screen_title,
    tile_title, readout, readout_lg, label, badge, body, body_sm, lens, button, button_sm, input,
    tooltip, shortcut); ``font(role, weight=None) -> QFont`` (pixel size, weight, tracking, tabular figures);
    ``role_text(role, text)`` applies the role's caps.
Motion
    Durations ``*_MS``; easings ``EASE_STD``, ``EASE_OUT``, ``EASE_IN``, ``EASE_BACK``, ``CSS_EASE``,
    ``CSS_EASE_IN_OUT``, ``CSS_EASE_OUT`` as cubic-bezier tuples; ``bezier(curve) -> f(x)``;
    ``qt_easing(curve) -> QEasingCurve``. Reduced motion: ``reduced_motion()``,
    ``set_reduced_motion(value | None)`` (None follows Windows' "Show animations"),
    ``refresh_reduced_motion()`` (call on WM_SETTINGCHANGE), ``motion_settings().changed(bool)``.
    The owner's ``[ui] motion``: ``set_motion_level(level)`` (``MOTION_LIVELY`` default, ``MOTION_CALM``,
    ``MOTION_OFF``; anything else is lively), ``configured_motion()``, ``motion_level()`` (``MOTION_OFF``
    while reduced motion is on), ``motion_ms(ms)`` (a transition's length at that level: calm x1.5, off 0).
Ground
    ``paint_ground(painter, rect)`` (void, the 30 px triangle grid, the vignette) and
    ``GroundCache`` (``pixmap(size, dpr)``, ``paint_slice(painter, target, source)``).
Tiles
    States ``TILE_DEFAULT``, ``TILE_FOCUSED``, ``TILE_NEEDS``, ``TILE_DONE``, ``TILE_ERROR``,
    ``TILE_INFO``; ``TILE_STYLES`` (``TileStyle``); ``paint_tile(painter, rect, state)``,
    ``paint_brackets(...)``, ``scan_brush(color)``.
Buttons and chips
    ``HoloButton(text, variant, size=, icon=, diameter=)`` with variants ``AMBER``, ``GO``,
    ``GHOST``, ``AMBER_GHOST``, ``DANGER``, ``DANGER_GHOST``, ``ICON``, ``LINK``, ``LINK_CYAN`` and
    sizes ``SIZE_MD`` (38), ``SIZE_SM`` (32), ``SIZE_LG`` (56 round); its widget is ``MARGIN`` larger
    on each side than the drawn button (room for the hover and focus rings).
    ``ActionChip(text, tone=CHIP_CYAN|CHIP_AMBER, ping=False)``.
    ``paint_status_badge(painter, left_center, text, tone, dot=False) -> width``,
    ``status_badge_width(text, dot)``, ``count_text(n)``, ``count_font()``, ``paint_count_badge(painter, top_right,
    count, ring=DOCK, scale=1.0)``, ``paint_new_dot(painter, center, ring=DOCK, scale=1.0, opacity=1.0)``,
    ``paint_ping(painter, center, progress)``, ``paint_focus_ring(painter, rect, radius)``.
Icons
    ``svg_path(d) -> QPainterPath`` (M L H V C S Q T A Z, absolute and relative), ``ICONS`` (24-unit
    stroke glyphs), ``icon_path(name)``, ``paint_icon(painter, rect, name, color, stroke=1.6)``.
"""

from __future__ import annotations

import ctypes
import logging
import math
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QAbstractButton, QSizePolicy, QWidget

from . import hud

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Colour tokens (spec 1)
# --------------------------------------------------------------------------

VOID = "#05080C"
VOID_EDGE = "#030508"
DOCK = "#060A0E"
TILE = "#091318"
TILE_FOCUS = "#0A1A20"
TILE_AMBER = "#141310"
TILE_OK = "#080F11"
TILE_ALERT = "#170F12"
RAISED = "#0B141B"
FIELD = "#090F14"
PICTURE = "#070D11"
FOCUS = "#E6FBFF"

HOLO = "#3CD3EE"
HOLO_HI = "#6FE0F4"
HOLO_LO = "#2BB8D2"
HOLO_INK = "#04121A"

INK_1 = "#E6FBFF"
INK_2 = "#BFD7DE"
INK_3 = "#9FC0CA"
INK_4 = "#7F9DA8"
INK_KICKER = "#8FB3BF"

AMBER = "#F2B350"
AMBER_HI = "#FFC871"
AMBER_LO = "#D99A3A"
AMBER_INK = "#1A1204"
AMBER_TX = "#FFF4E2"
AMBER_TX2 = "#F7DDB0"

OK = "#62E6B0"
OK_INK = "#04140D"
ALERT = "#FF6B5E"
ALERT_HI = "#FF8577"
ALERT_LO = "#E85547"
ALERT_TX = "#FF9A8C"
ALERT_TX2 = "#FFD9D3"
ALERT_INK = "#1F0705"
STEEL = "#6C8A95"


def alpha(color: str | QColor, a: float) -> QColor:
    """``color`` at alpha ``a`` (0..1): the spec's ``#RRGGBB @ a``."""
    result = QColor(color)
    result.setAlphaF(max(0.0, min(1.0, a)))
    return result


def mix(a: str | QColor, b: str | QColor, t: float) -> QColor:
    """Linear blend of two colours (alpha included); ``t`` 0 gives ``a``, 1 gives ``b``."""
    ca, cb = QColor(a), QColor(b)
    t = max(0.0, min(1.0, t))
    return QColor.fromRgbF(ca.redF() + (cb.redF() - ca.redF()) * t, ca.greenF() + (cb.greenF() - ca.greenF()) * t,
                           ca.blueF() + (cb.blueF() - ca.blueF()) * t, ca.alphaF() + (cb.alphaF() - ca.alphaF()) * t)


GRID = alpha(HOLO, 0.043)
SCAN = alpha(HOLO, 0.043)
SCAN_AMBER = alpha(AMBER, 0.045)
LINE_FAINT = alpha(HOLO, 0.08)
LINE = alpha(HOLO, 0.20)
LINE_STRONG = alpha(HOLO, 0.33)
LINE_FOCUS = alpha(HOLO, 0.55)
LINE_INPUT = alpha(HOLO, 0.50)
AMBER_LINE = alpha(AMBER, 0.45)

# --------------------------------------------------------------------------
# Spacing and shape (spec 3, 4)
# --------------------------------------------------------------------------

SPACE = {"2xs": 2, "xs": 4, "s": 8, "m": 12, "l": 16, "xl": 20, "2xl": 24, "3xl": 32, "4xl": 40, "5xl": 56,
         "6xl": 80}
GUTTER_SIDE = 24
GUTTER_TOP = 20
GUTTER_BOTTOM = 24
DOCK_WIDTH = 64
DOCK_WIDTH_LABELLED = 200      # the Dock with each screen's name (the motion prototype); 64 folds to glyphs
DOCK_BUTTON = 44
DOCK_GAP = 6
HEADER_HEIGHT = 52
TILE_PAD = (14, 16)            # vertical, horizontal
TILE_PAD_COMPACT = (12, 14)
KICKER_GAP = 6
TILE_GAP = 12
TILE_ROW_GAP = 32
TETHER = 44
TETHER_DOT = 5
CAPTION_MIN_HEIGHT = 120
CAPTION_MAX_WIDTH = 760
COMMAND_MAX_WIDTH = 620
BUTTON_GAP = 8
SECTION_GAP = 10

RADIUS_TILE = 10
RADIUS_TILE_AMBER = 12
RADIUS_BUTTON = 8
RADIUS_BUTTON_SM = 6
RADIUS_DOCK_BUTTON = 12
RADIUS_TOOLTIP = 7
RADIUS_BADGE = 4
RADIUS_FRAME = 6
RADIUS_DIALOG = 14
RADIUS_CHECKBOX = 4
BRACKET_WIDTH = 1.5

# --------------------------------------------------------------------------
# Type roles (spec 2)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TypeRole:
    family: str
    px: int
    line: int
    weight: int
    tracking: float          # em
    caps: bool = False
    tabular: bool = False


_D, _B, _M = hud.DISPLAY_FAMILY, hud.BODY_FAMILY, hud.MONO_FAMILY
ROLES: dict[str, TypeRole] = {
    "wordmark": TypeRole(_D, 15, 20, 600, 0.34, caps=True),
    "greeting": TypeRole(_D, 28, 36, 500, 0.08),
    "question": TypeRole(_D, 26, 35, 500, 0.03),
    "caption": TypeRole(_D, 24, 36, 400, 0.02),
    "caption_sm": TypeRole(_D, 18, 26, 400, 0.02),
    "screen_title": TypeRole(_D, 20, 26, 600, 0.06),
    "tile_title": TypeRole(_D, 17, 22, 500, 0.01),
    "readout": TypeRole(_M, 12, 16, 500, 0.12, caps=True, tabular=True),
    "readout_lg": TypeRole(_D, 20, 24, 500, 0.04, tabular=True),
    "label": TypeRole(_M, 11, 14, 500, 0.20, caps=True),
    "badge": TypeRole(_M, 10, 12, 500, 0.16, caps=True, tabular=True),
    "body": TypeRole(_B, 15, 24, 400, 0.0),
    "body_sm": TypeRole(_B, 13, 20, 400, 0.0),
    "lens": TypeRole(_B, 22, 33, 400, 0.0),
    "button": TypeRole(_D, 14, 18, 600, 0.02),
    "button_sm": TypeRole(_D, 13, 16, 500, 0.06),
    "input": TypeRole(_D, 16, 20, 400, 0.02),
    "tooltip": TypeRole(_D, 12, 16, 500, 0.04),
    "shortcut": TypeRole(_M, 11, 14, 500, 0.12, caps=True),      # the shortcut after a Dock label
    "dock_label": TypeRole(_B, 14, 18, 500, 0.0),                 # a screen's name in the labelled Dock
    "find_item": TypeRole(_B, 14, 18, 400, 0.0),                  # a row of the Find list
    "kbd": TypeRole(_M, 10, 12, 500, 0.08, caps=True),            # a key hint chip (CTRL K)
    "dock_tag": TypeRole(_M, 9, 12, 500, 0.14, caps=True),        # the Dock's Desktop: its HUD tag
    "dock_key": TypeRole(_M, 9, 12, 500, 0.06),                   # ... the HUD key's keycaps under it
    "dock_hint": TypeRole(_M, 10, 14, 400, 0.0),                  # ... and "switches"
}
_FONT_MAKERS: dict[str, Callable[[int, int, float], QFont]] = {
    _D: hud.display_font, _B: hud.body_font, _M: hud.mono_font,
}


def font(role: str, weight: int | None = None) -> QFont:
    """The QFont of a type role (pixel size, weight, tracking; tabular figures where the role wants them)."""
    spec = ROLES[role]
    result = _FONT_MAKERS[spec.family](spec.px, weight or spec.weight, spec.tracking)
    if spec.tabular:
        try:
            result.setFeature(QFont.Tag("tnum"), 1)
        except (AttributeError, TypeError, ValueError):   # Qt before 6.7: JetBrains Mono is tabular anyway
            pass
    return result


def role_text(role: str, text: str) -> str:
    """``text`` as the role shows it (caps roles upper-case it)."""
    return text.upper() if ROLES[role].caps else text


def line_height(role: str) -> int:
    return ROLES[role].line

# --------------------------------------------------------------------------
# Motion (spec 13)
# --------------------------------------------------------------------------

HOVER_MS = 120
PRESS_MS = 80
FADE_MS = 200
TOOLTIP_FADE_MS = 180
TOOLTIP_DELAY_MS = 400
SWAP_MS = 320
ENTER_MS = 700
TETHER_MS = 400
SWEEP_MS = 1000
EXIT_MS = 220
SCREEN_OUT_MS = 140
SCREEN_IN_MS = 280
DOCK_SLIDE_MS = 200
DRAWER_IN_MS = 280
DRAWER_OUT_MS = 180
ORB_STATE_MS = 800
POP_MS = 400
PING_MS = 1700
REDUCED_FADE_MS = 150
DOCK_RAIL_MS = 400             # the motion prototype: the Dock's glowing rail slides to the current screen
WIPE_MS = 550                  # a screen change: the scan-line wipe over the stage
ASSEMBLE_MS = 450              # ... each main panel's fade + rise + top-to-bottom reveal
ASSEMBLE_STAGGER_MS = 70
ASSEMBLE_RISE = 14.0
CALM_FADE_MS = 600             # calm: the panels only fade in
CALM_STAGGER_MS = 90
STARTUP_SCAN_MS = 1600         # the start-up's one scan line
STARTUP_TYPE_DELAY_MS = 250    # J.A.R.V.I.S. types in after this
STARTUP_TYPE_MS = 1100

Curve = tuple[float, float, float, float]
EASE_STD: Curve = (0.2, 0.8, 0.2, 1.0)
EASE_OUT: Curve = (0.16, 1.0, 0.3, 1.0)
EASE_IN: Curve = (0.4, 0.0, 1.0, 1.0)
EASE_BACK: Curve = (0.2, 1.4, 0.4, 1.0)
CSS_EASE: Curve = (0.25, 0.1, 0.25, 1.0)
CSS_EASE_IN_OUT: Curve = (0.42, 0.0, 0.58, 1.0)
CSS_EASE_OUT: Curve = (0.0, 0.0, 0.58, 1.0)
LINEAR: Curve = (0.0, 0.0, 1.0, 1.0)


@lru_cache(maxsize=32)
def bezier(curve: Curve) -> Callable[[float], float]:
    """CSS ``cubic-bezier(x1, y1, x2, y2)`` as a function of progress 0..1 (clamped)."""
    x1, y1, x2, y2 = curve
    if curve == LINEAR:
        return lambda x: max(0.0, min(1.0, x))
    cx, bx = 3.0 * x1, 3.0 * (x2 - x1) - 3.0 * x1
    ax = 1.0 - cx - bx
    cy, by = 3.0 * y1, 3.0 * (y2 - y1) - 3.0 * y1
    ay = 1.0 - cy - by

    def ease(x: float) -> float:
        if x <= 0.0:
            return 0.0
        if x >= 1.0:
            return 1.0
        t = x
        for _ in range(8):              # Newton on x(t) = x, then bisection if the slope is flat
            err = ((ax * t + bx) * t + cx) * t - x
            if abs(err) < 1e-5:
                break
            slope = (3.0 * ax * t + 2.0 * bx) * t + cx
            if abs(slope) < 1e-6:
                lo, hi = 0.0, 1.0
                for _ in range(20):
                    t = (lo + hi) / 2
                    if ((ax * t + bx) * t + cx) * t < x:
                        lo = t
                    else:
                        hi = t
                break
            t -= err / slope
        return ((ay * t + by) * t + cy) * t

    return ease


def qt_easing(curve: Curve) -> QEasingCurve:
    """A QEasingCurve following the CSS cubic-bezier (for QVariantAnimation / QPropertyAnimation)."""
    result = QEasingCurve(QEasingCurve.Type.BezierSpline)
    result.addCubicBezierSegment(QPointF(curve[0], curve[1]), QPointF(curve[2], curve[3]), QPointF(1.0, 1.0))
    return result


class MotionSettings(QObject):
    """Emits ``changed(reduced)`` when reduced motion turns on or off."""

    changed = Signal(bool)


_motion_settings: MotionSettings | None = None
_reduced_override: bool | None = None
_reduced_system: bool | None = None
_SPI_GETCLIENTAREAANIMATION = 0x1042


def motion_settings() -> MotionSettings:
    global _motion_settings
    if _motion_settings is None:
        _motion_settings = MotionSettings()
    return _motion_settings


def _windows_animations_off() -> bool:
    """Windows' "Show animations in Windows" is off (only on the real Windows platform)."""
    if sys.platform != "win32" or QGuiApplication.platformName() != "windows":
        return False
    try:
        value = ctypes.c_int(1)
        ok = ctypes.windll.user32.SystemParametersInfoW(_SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(value), 0)
        return bool(ok) and not value.value
    except Exception as exc:  # noqa: BLE001 - an unknown answer keeps the motion
        logger.debug("Could not read the animation setting: %s", exc)
        return False


MOTION_LIVELY = "lively"
MOTION_CALM = "calm"
MOTION_OFF = "off"
MOTION_LEVELS = (MOTION_LIVELY, MOTION_CALM, MOTION_OFF)
_motion_level = MOTION_LIVELY


def set_motion_level(level: str | None) -> None:
    """The owner's ``[ui] motion`` ("lively", "calm", "off"; anything else is lively)."""
    global _motion_level
    text = str(level or "").strip().casefold()
    _motion_level = text if text in MOTION_LEVELS else MOTION_LIVELY


def configured_motion() -> str:
    return _motion_level


def motion_level() -> str:
    """The motion now: the owner's setting, but "off" while reduced motion is on (Windows' "Show animations in
    Windows" off, or forced)."""
    return MOTION_OFF if reduced_motion() else _motion_level


def motion_ms(ms: int) -> int:
    """A transition's length at the current motion level (calm 1.5 x, off 0)."""
    level = motion_level()
    if level == MOTION_OFF:
        return 0
    return round(ms * 1.5) if level == MOTION_CALM else int(ms)


def reduced_motion() -> bool:
    """True when motion should be still: the app's override, else Windows (read once, see refresh)."""
    global _reduced_system
    if _reduced_override is not None:
        return _reduced_override
    if _reduced_system is None:
        _reduced_system = _windows_animations_off()
    return _reduced_system


def set_reduced_motion(value: bool | None) -> None:
    """Force reduced motion on or off; None follows Windows again."""
    global _reduced_override
    before = reduced_motion()
    _reduced_override = value
    if reduced_motion() != before:
        motion_settings().changed.emit(reduced_motion())


def refresh_reduced_motion() -> None:
    """Re-read Windows' setting (WM_SETTINGCHANGE)."""
    global _reduced_system
    before = reduced_motion()
    _reduced_system = _windows_animations_off()
    if reduced_motion() != before:
        motion_settings().changed.emit(reduced_motion())

# --------------------------------------------------------------------------
# Ground (spec 4)
# --------------------------------------------------------------------------

GRID_STEP = 30


def paint_ground(painter: QPainter, rect: QRectF | QRect) -> None:
    """``void``, the 1 px triangle grid every 30 px at 0 / 60 / 120 degrees and the vignette."""
    rect = QRectF(rect)
    painter.save()
    try:
        painter.fillRect(rect, QColor(VOID))
        painter.setClipRect(rect)
        pen = QPen(GRID, 1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = rect.width(), rect.height()
        cx, cy = rect.center().x(), rect.center().y()
        reach = math.hypot(w, h) / 2 + GRID_STEP
        for degrees in (0.0, 60.0, 120.0):
            # CSS repeating-linear-gradient(<deg>): the bands are perpendicular to the gradient
            # direction (sin a, -cos a), counted from the start of the gradient line.
            a = math.radians(degrees)
            dx, dy = math.sin(a), -math.cos(a)
            half = abs(w / 2 * dx) + abs(h / 2 * dy)
            start = -half
            k = 0
            while start + k * GRID_STEP <= half:
                d = start + k * GRID_STEP + 0.5
                px, py = cx + dx * d, cy + dy * d
                painter.drawLine(QPointF(px - dy * reach, py + dx * reach), QPointF(px + dy * reach, py - dx * reach))
                k += 1
        painter.setPen(Qt.PenStyle.NoPen)
        gradient = QRadialGradient(QPointF(0.0, 0.0), 1.0)
        gradient.setColorAt(0.0, alpha(VOID, 0.0))
        gradient.setColorAt(0.72, alpha(VOID, 0.5))
        gradient.setColorAt(1.0, alpha(VOID_EDGE, 0.92))
        painter.translate(rect.left() + w * 0.5, rect.top() + h * 0.45)
        painter.scale(max(1.0, w * 0.75), max(1.0, h * 0.65))
        painter.fillRect(QRectF(-2.0, -2.0, 4.0, 4.0), QBrush(gradient))   # covers the clip in these units
    finally:
        painter.restore()


class GroundCache:
    """The window ground as one pixmap per (size, device pixel ratio); widgets paint slices of it."""

    def __init__(self) -> None:
        self._key: tuple[int, int, float] | None = None
        self._pixmap: QPixmap | None = None

    def pixmap(self, size: QSize, dpr: float = 1.0) -> QPixmap:
        key = (size.width(), size.height(), round(dpr, 3))
        if self._pixmap is None or self._key != key:
            pixmap = QPixmap(max(1, round(size.width() * dpr)), max(1, round(size.height() * dpr)))
            pixmap.setDevicePixelRatio(dpr)
            painter = QPainter(pixmap)
            try:
                paint_ground(painter, QRectF(0, 0, size.width(), size.height()))
            finally:
                painter.end()
            self._pixmap, self._key = pixmap, key
        return self._pixmap

    def paint_slice(self, painter: QPainter, target: QRect | QRectF, source: QRect | QRectF,
                    window_size: QSize, dpr: float = 1.0) -> None:
        """Paint the part ``source`` (window coordinates) of the ground into ``target``."""
        pixmap = self.pixmap(window_size, dpr)
        s = QRectF(source)
        painter.drawPixmap(QRectF(target), pixmap, QRectF(s.left() * dpr, s.top() * dpr, s.width() * dpr,
                                                          s.height() * dpr))

    def clear(self) -> None:
        self._key, self._pixmap = None, None

# --------------------------------------------------------------------------
# Tiles (spec 5)
# --------------------------------------------------------------------------

TILE_DEFAULT = "default"
TILE_FOCUSED = "focused"
TILE_NEEDS = "needs"
TILE_DONE = "done"
TILE_ERROR = "error"
TILE_INFO = "info"
TILE_NEEDS_FOCUS = "needs_focus"          # round 2: the focal needs-you card (all four brackets)
TILE_NEEDS_SELECTED = "needs_selected"    # the queue's selected tile (all four brackets, stronger border)
TILE_STEEL = "steel"                      # denied: nothing was sent

BRACKETS_DIAGONAL = "diagonal"     # top-left + bottom-right
BRACKETS_ALL = "all"               # the "viewfinder"
BRACKETS_NONE = "none"


@dataclass(frozen=True)
class TileStyle:
    fill: str
    border: QColor
    bracket: str | None
    corners: str
    leg: float
    radius: float
    scan: QColor | None
    kicker: str
    title: str


TILE_STYLES: dict[str, TileStyle] = {
    TILE_DEFAULT: TileStyle(TILE, LINE, HOLO, BRACKETS_DIAGONAL, 10, RADIUS_TILE, SCAN, HOLO, INK_1),
    TILE_FOCUSED: TileStyle(TILE_FOCUS, LINE_FOCUS, HOLO, BRACKETS_ALL, 10, RADIUS_TILE, SCAN, HOLO, INK_1),
    TILE_NEEDS: TileStyle(TILE_AMBER, alpha(AMBER, 0.50), AMBER, BRACKETS_DIAGONAL, 12, RADIUS_TILE_AMBER,
                          SCAN_AMBER, AMBER, AMBER_TX),
    TILE_DONE: TileStyle(TILE_OK, alpha(OK, 0.30), None, BRACKETS_NONE, 0, RADIUS_TILE, None, OK, INK_3),
    TILE_ERROR: TileStyle(TILE_ALERT, alpha(ALERT, 0.50), ALERT, BRACKETS_DIAGONAL, 10, RADIUS_TILE, None,
                          ALERT_TX, INK_1),
    TILE_INFO: TileStyle(TILE, LINE_FAINT, None, BRACKETS_NONE, 0, RADIUS_TILE, None, INK_4, INK_2),
    # Round 2 (Approvals): the card in focus (the screen's focal card; the queue's selected tile with a
    # stronger border: all four brackets) and a denied card (steel).
    TILE_NEEDS_FOCUS: TileStyle(TILE_AMBER, alpha(AMBER, 0.50), AMBER, BRACKETS_ALL, 12, RADIUS_TILE_AMBER,
                                SCAN_AMBER, AMBER, AMBER_TX),
    TILE_NEEDS_SELECTED: TileStyle(TILE_AMBER, alpha(AMBER, 0.80), AMBER, BRACKETS_ALL, 12, RADIUS_TILE_AMBER,
                                   SCAN_AMBER, AMBER, AMBER_TX),
    TILE_STEEL: TileStyle(TILE, alpha(STEEL, 0.45), None, BRACKETS_NONE, 0, RADIUS_TILE_AMBER, None, INK_4, INK_3),
}

_SCAN_BRUSHES: dict[int, QBrush] = {}


def scan_brush(color: QColor) -> QBrush:
    """A 4 x 4 texture brush: one 1 px line of ``color`` every 4 px (the tile scanlines)."""
    key = color.rgba()
    brush = _SCAN_BRUSHES.get(key)
    if brush is None:
        pixmap = QPixmap(4, 4)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.fillRect(0, 3, 4, 1, color)
        painter.end()
        brush = QBrush(pixmap)
        _SCAN_BRUSHES[key] = brush
    return brush


def _corner_path(rect: QRectF, corner: str, leg: float, radius: float, width: float) -> QPainterPath:
    """One L-shaped bracket (a stroke centre line) at ``corner`` ('tl', 'tr', 'br', 'bl')."""
    inset = width / 2
    r = max(0.0, min(radius, leg) - inset)
    left, top = rect.left() + inset, rect.top() + inset
    right, bottom = rect.right() - inset, rect.bottom() - inset
    path = QPainterPath()
    if corner == "tl":
        path.moveTo(left, rect.top() + leg)
        path.lineTo(left, top + r)
        path.arcTo(QRectF(left, top, 2 * r, 2 * r), 180, -90)
        path.lineTo(rect.left() + leg, top)
    elif corner == "tr":
        path.moveTo(rect.right() - leg, top)
        path.lineTo(right - r, top)
        path.arcTo(QRectF(right - 2 * r, top, 2 * r, 2 * r), 90, -90)
        path.lineTo(right, rect.top() + leg)
    elif corner == "br":
        path.moveTo(right, rect.bottom() - leg)
        path.lineTo(right, bottom - r)
        path.arcTo(QRectF(right - 2 * r, bottom - 2 * r, 2 * r, 2 * r), 0, -90)
        path.lineTo(rect.right() - leg, bottom)
    else:
        path.moveTo(rect.left() + leg, bottom)
        path.lineTo(left + r, bottom)
        path.arcTo(QRectF(left, bottom - 2 * r, 2 * r, 2 * r), 270, -90)
        path.lineTo(left, rect.bottom() - leg)
    return path


def paint_brackets(painter: QPainter, rect: QRectF, color: str | QColor, *, leg: float = 10,
                   radius: float = RADIUS_TILE, corners: str = BRACKETS_DIAGONAL,
                   width: float = BRACKET_WIDTH) -> None:
    """The hologram corner brackets (1.5 px L strokes whose corner follows the tile's radius)."""
    if corners == BRACKETS_NONE:
        return
    names = ("tl", "br") if corners == BRACKETS_DIAGONAL else ("tl", "tr", "br", "bl")
    pen = QPen(QColor(color), width)
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for name in names:
        painter.drawPath(_corner_path(rect, name, leg, radius, width))
    painter.restore()


def tile_path(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


def paint_tile(painter: QPainter, rect: QRectF | QRect, state: str = TILE_DEFAULT, *,
               opacity: float = 1.0) -> TileStyle:
    """A tile's body: fill + 1 px border + scanlines + corner brackets. Returns its style (for the text)."""
    style = TILE_STYLES.get(state, TILE_STYLES[TILE_DEFAULT])
    rect = QRectF(rect)
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setOpacity(painter.opacity() * opacity)
        inner = rect.adjusted(0.5, 0.5, -0.5, -0.5)
        path = tile_path(inner, style.radius)
        painter.fillPath(path, QColor(style.fill))
        if style.scan is not None:
            painter.save()
            painter.setClipPath(path)
            painter.setBrushOrigin(QPointF(rect.left(), rect.bottom() - 4 * math.floor(rect.height() / 4)))
            painter.fillRect(rect, scan_brush(style.scan))
            painter.restore()
        painter.setPen(QPen(style.border, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        if style.bracket is not None:
            paint_brackets(painter, rect, style.bracket, leg=style.leg, radius=style.radius, corners=style.corners)
    finally:
        painter.restore()
    return style

# --------------------------------------------------------------------------
# Icons (24-unit viewBox stroke glyphs)
# --------------------------------------------------------------------------

_PATH_TOKEN = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _arc_to(path: QPainterPath, x1: float, y1: float, rx: float, ry: float, phi_deg: float,
            large: bool, sweep: bool, x2: float, y2: float) -> None:
    """SVG elliptical arc (endpoint form) as cubic segments (SVG 1.1 F.6.5)."""
    if (x1, y1) == (x2, y2):
        return
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0:
        path.lineTo(x2, y2)
        return
    phi = math.radians(phi_deg)
    cos_p, sin_p = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cos_p * dx + sin_p * dy, -sin_p * dx + cos_p * dy
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        coef = -coef
    cxp, cyp = coef * rx * y1p / ry, -coef * ry * x1p / rx
    cx = cos_p * cxp - sin_p * cyp + (x1 + x2) / 2
    cy = sin_p * cxp + cos_p * cyp + (y1 + y2) / 2

    def angle(ux: float, uy: float, vx: float, vy: float) -> float:
        return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)

    t1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = angle((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    segments = max(1, math.ceil(abs(dt) / (math.pi / 2) - 1e-9))
    delta = dt / segments
    k = 4.0 / 3.0 * math.tan(delta / 4)

    def point(t: float) -> tuple[float, float]:
        return (cx + rx * math.cos(t) * cos_p - ry * math.sin(t) * sin_p,
                cy + rx * math.cos(t) * sin_p + ry * math.sin(t) * cos_p)

    def tangent(t: float) -> tuple[float, float]:
        return (-rx * math.sin(t) * cos_p - ry * math.cos(t) * sin_p,
                -rx * math.sin(t) * sin_p + ry * math.cos(t) * cos_p)

    t = t1
    for _ in range(segments):
        (ax, ay), (adx, ady) = point(t), tangent(t)
        (bx, by), (bdx, bdy) = point(t + delta), tangent(t + delta)
        path.cubicTo(ax + k * adx, ay + k * ady, bx - k * bdx, by - k * bdy, bx, by)
        t += delta


def svg_path(d: str) -> QPainterPath:
    """An SVG path ``d`` string as a QPainterPath (M L H V C S Q T A Z, absolute and relative)."""
    tokens = _PATH_TOKEN.findall(d)
    path = QPainterPath()
    i, cmd = 0, ""
    x = y = sx = sy = 0.0
    ctrl: tuple[float, float] | None = None       # last cubic / quadratic control point (S / T)
    quad = False

    def num() -> float:
        nonlocal i
        value = float(tokens[i])
        i += 1
        return value

    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
            if cmd in "Zz":
                path.closeSubpath()
                x, y, ctrl = sx, sy, None
                continue
        if not cmd:
            raise ValueError(f"path data must start with a command: {d!r}")
        rel = cmd.islower()
        ox, oy = (x, y) if rel else (0.0, 0.0)
        c = cmd.upper()
        if c == "M":
            x, y = ox + num(), oy + num()
            path.moveTo(x, y)
            sx, sy, ctrl = x, y, None
            cmd = "l" if rel else "L"
        elif c == "L":
            x, y = ox + num(), oy + num()
            path.lineTo(x, y)
            ctrl = None
        elif c == "H":
            x = ox + num()
            path.lineTo(x, y)
            ctrl = None
        elif c == "V":
            y = (y if rel else 0.0) + num()
            path.lineTo(x, y)
            ctrl = None
        elif c in "CS":
            if c == "C":
                x1, y1 = ox + num(), oy + num()
            else:
                x1, y1 = (2 * x - ctrl[0], 2 * y - ctrl[1]) if ctrl and not quad else (x, y)
            x2, y2 = ox + num(), oy + num()
            x, y = ox + num(), oy + num()
            path.cubicTo(x1, y1, x2, y2, x, y)
            ctrl, quad = (x2, y2), False
        elif c in "QT":
            if c == "Q":
                qx, qy = ox + num(), oy + num()
            else:
                qx, qy = (2 * x - ctrl[0], 2 * y - ctrl[1]) if ctrl and quad else (x, y)
            x, y = ox + num(), oy + num()
            path.quadTo(qx, qy, x, y)
            ctrl, quad = (qx, qy), True
        elif c == "A":
            rx, ry, rot = num(), num(), num()
            large, sweep = bool(num()), bool(num())
            ex, ey = ox + num(), oy + num()
            _arc_to(path, x, y, rx, ry, rot, large, sweep, ex, ey)
            x, y, ctrl = ex, ey, None
        else:
            raise ValueError(f"unsupported path command {cmd!r}")
    return path


# Stroke glyphs in a 24-unit box (round caps and joins), from the StyleGuide / Hologram boards.
ICONS: dict[str, tuple[str, ...]] = {
    "briefing": ("M7 3.5h7l4 4V20a.5.5 0 0 1-.5.5h-10a.5.5 0 0 1-.5-.5V4a.5.5 0 0 1 .5-.5z", "M14 3.5V8h4",
                 "M9.5 12.5h5M9.5 16h5"),
    "approvals": ("M12 3l7 3v5.5c0 4.3-3 7.7-7 9.5-4-1.8-7-5.2-7-9.5V6z", "M8.8 12.2l2.2 2.2 4.4-4.6"),
    "live": ("M3 12h4l2.5-6 5 12 2.5-6h4",),
    "memory": ("M5 6a7 2.8 0 1 0 14 0a7 2.8 0 1 0-14 0z", "M5 6v6c0 1.5 3.1 2.8 7 2.8s7-1.3 7-2.8V6",
               "M5 12v6c0 1.5 3.1 2.8 7 2.8s7-1.3 7-2.8v-6"),
    "followups": ("M9.5 7L4.5 12l5 5", "M4.5 12H14a5.5 5.5 0 0 1 5.5 5.5V19"),
    "settings": ("M4 7h10M18 7h2M4 17h4M12 17h8", "M14 7a2 2 0 1 0 4 0a2 2 0 1 0-4 0z",
                 "M8 17a2 2 0 1 0 4 0a2 2 0 1 0-4 0z"),
    "speaker": ("M4 9.5h3.5L12 5.5v13l-4.5-4H4z", "M16 9.5c1 .8 1.5 1.6 1.5 2.5s-.5 1.7-1.5 2.5",
                "M18.5 7c1.8 1.4 2.7 3.1 2.7 5s-.9 3.6-2.7 5"),
    "speaker_muted": ("M4 9.5h3.5L12 5.5v13l-4.5-4H4z", "M3.5 3.5l17 17"),
    "minimize": ("M6 12h12",),
    "close": ("M7 7l10 10M17 7L7 17",),
    "ask": ("M12 4c.7 4.6 2.4 6.3 8 8-5.6 1.7-7.3 3.4-8 8-.7-4.6-2.4-6.3-8-8 5.6-1.7 7.3-3.4 8-8z",),
    "send_arrow": ("M12 19V5", "M6 11l6-6 6 6"),
    "transcript": ("M4.5 5.5h15v10h-8l-4 3.5v-3.5h-3z", "M8 9h8M8 12h5"),
    "chevron": ("M9 6l6 6-6 6",),
    "send": ("M20.5 3.5L10 14", "M20.5 3.5l-6.5 17-4-6.5-6.5-4z"),
    "popout": ("M14 4h6v6", "M20 4l-8 8", "M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"),
    "check": ("M5 12.5l4.2 4.2L19 7",),                      # the YOU echo's tick (Home)
    "stop": ("M7 7h10v10H7z",),                               # the command bar while a request runs
    # Round 2 (the Briefing and Approvals boards).
    "pause": ("M9 6v12M15 6v12",),
    "replay": ("M4 12a8 8 0 1 0 2.45-5.75", "M4 4.5v4.5h4.5"),
    "skip": ("M6 6.5l8.5 5.5L6 17.5z", "M18 6v12"),
    "notion": ("M13.5 4.5H19.5V10.5", "M19.5 4.5l-8.5 8.5",
               "M17.5 13.5v5a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1v-11a1 1 0 0 1 1-1h5"),
    "read_all": ("M5 6h14M5 10h14M5 14h14M5 18h8",),
    "edit": ("M4 20h4L19 9l-4-4L4 16z", "M13.5 6.5l4 4"),
    "info": ("M12 3.5a8.5 8.5 0 1 0 0 17a8.5 8.5 0 1 0 0-17z", "M12 11v5", "M12 8v.01"),
    "calendar": ("M5.5 5h13a2 2 0 0 1 2 2v11.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z", "M3.5 10h17",
                 "M8 3v4M16 3v4"),
    "warning": ("M12 4l9 16H3z", "M12 10v4.5", "M12 17.5v.01"),
    # Round 2, part 2b (Memory and Follow-ups).
    "plus": ("M12 5v14M5 12h14",),
    "more": ("M4.6 12a1.4 1.4 0 1 0 2.8 0a1.4 1.4 0 1 0-2.8 0z", "M10.6 12a1.4 1.4 0 1 0 2.8 0a1.4 1.4 0 1 0-2.8 0z",
             "M16.6 12a1.4 1.4 0 1 0 2.8 0a1.4 1.4 0 1 0-2.8 0z"),
    # The motion prototype (the Find list and the labelled Dock).
    "search": ("M11 5a6 6 0 1 0 0 12a6 6 0 1 0 0-12z", "M20 20l-4.5-4.5"),
    "home": ("M4 11l8-7 8 7v8a1 1 0 0 1-1 1h-4v-6h-6v6H5a1 1 0 0 1-1-1z",),
    "plan": ("M4 5h16v15H4z", "M4 10h16", "M8 3v4M16 3v4", "M8 14h4"),
    "prep": ("M6 4h12v16H6z", "M9 8h6M9 12h6M9 16h3"),
    # Meetings (a microphone; a record dot).
    "meetings": ("M12 3.5a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0v-5a3 3 0 0 0-3-3z", "M6 11a6 6 0 0 0 12 0",
                 "M12 17v3.5M8.5 20.5h7"),
    "record": ("M12 6a6 6 0 1 0 0 12a6 6 0 1 0 0-12z",),
    # Desktop (the Switch prototype's monitor: the Dock's switch to the HUD).
    "desktop": ("M4.5 4.5h15a1.5 1.5 0 0 1 1.5 1.5v8.5a1.5 1.5 0 0 1-1.5 1.5h-15a1.5 1.5 0 0 1-1.5-1.5V6"
                "a1.5 1.5 0 0 1 1.5-1.5z", "M8.5 20h7M12 16v4"),
}
FILLED_ICONS: dict[str, str] = {
    "play": "M8 5.5v13l10.5-6.5z",
    "play_lg": "M7.5 5.2v13.6L18.5 12z",
}


@lru_cache(maxsize=64)
def icon_path(name: str) -> QPainterPath:
    """The glyph ``name`` (ICONS or FILLED_ICONS) as one QPainterPath in its 24-unit box."""
    path = QPainterPath()
    for d in ICONS.get(name, ()) or (FILLED_ICONS[name],):
        path.addPath(svg_path(d))
    return path


def paint_icon(painter: QPainter, rect: QRectF | QRect, name: str, color: str | QColor, *,
               stroke: float = 1.6) -> None:
    """Paint glyph ``name`` scaled into ``rect`` (square), stroke width in 24-unit box units."""
    rect = QRectF(rect)
    side = min(rect.width(), rect.height())
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.translate(rect.center().x() - side / 2, rect.center().y() - side / 2)
        painter.scale(side / 24.0, side / 24.0)
        path = icon_path(name)
        if name in FILLED_ICONS:
            pen = QPen(QColor(color), 1.5)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(QColor(color))
        else:
            pen = QPen(QColor(color), stroke)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
    finally:
        painter.restore()

# --------------------------------------------------------------------------
# Badges, dots, focus ring (spec 7, 15)
# --------------------------------------------------------------------------

TONE_CYAN = "cyan"
TONE_AMBER = "amber"
TONE_OK = "ok"
TONE_ALERT = "alert"
TONE_STEEL = "steel"
_TONES: dict[str, tuple[str, str]] = {          # (signal colour for border / fill, text colour)
    TONE_CYAN: (HOLO, HOLO),
    TONE_AMBER: (AMBER, AMBER),
    TONE_OK: (OK, OK),
    TONE_ALERT: (ALERT, ALERT_TX),
    TONE_STEEL: (STEEL, INK_4),
}
BADGE_HEIGHT = 20
COUNT_BADGE = 16
NEW_DOT = 7


def count_text(count: int) -> str:
    """A count badge's text: the number, ``9+`` past nine."""
    return "9+" if count > 9 else str(max(0, count))


def status_badge_width(text: str, dot: bool = False) -> float:
    metrics = QFontMetricsF(font("badge"))
    return 14 + metrics.horizontalAdvance(role_text("badge", text)) + (12 if dot else 0)


def paint_status_badge(painter: QPainter, left_center: QPointF, text: str, tone: str = TONE_CYAN, *,
                       dot: bool = False) -> float:
    """A status badge (height 20, radius 4, 1 px border at 45 %, fill 8 %); returns its width."""
    signal, ink = _TONES.get(tone, _TONES[TONE_CYAN])
    width = status_badge_width(text, dot)
    rect = QRectF(left_center.x(), left_center.y() - BADGE_HEIGHT / 2, width, BADGE_HEIGHT)
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(alpha(signal, 0.45), 1.0))
        painter.setBrush(alpha(signal, 0.08))
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), RADIUS_BADGE, RADIUS_BADGE)
        x = rect.left() + 7
        if dot:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(signal))
            painter.drawEllipse(QPointF(x + 3, rect.center().y()), 3, 3)
            x += 12
        painter.setFont(font("badge"))
        painter.setPen(QColor(ink))
        painter.drawText(QRectF(x, rect.top(), rect.right() - x, rect.height()),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), role_text("badge", text))
    finally:
        painter.restore()
    return width


def count_font() -> QFont:
    """The count badge's figures: JetBrains Mono 10 / 500, tabular, no tracking (the mockup's badge)."""
    result = hud.mono_font(10, 500, 0.0)
    try:
        result.setFeature(QFont.Tag("tnum"), 1)
    except (AttributeError, TypeError, ValueError):
        pass
    return result


def count_badge_rect(top_right: QPointF, count: int) -> QRectF:
    metrics = QFontMetricsF(count_font())
    width = max(float(COUNT_BADGE), 8 + metrics.horizontalAdvance(count_text(count)))
    return QRectF(top_right.x() - width, top_right.y(), width, COUNT_BADGE)


def paint_count_badge(painter: QPainter, top_right: QPointF, count: int, *, ring: str | QColor = DOCK,
                      scale: float = 1.0) -> QRectF:
    """The amber count badge (min 16 x 16, radius 8) with a 2 px ring of the surface around it."""
    rect = count_badge_rect(top_right, count)
    if scale <= 0.0:
        return rect
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if scale != 1.0:
            c = rect.center()
            painter.translate(c)
            painter.scale(scale, scale)
            painter.translate(-c)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(ring))
        outer = rect.adjusted(-2, -2, 2, 2)
        painter.drawRoundedRect(outer, outer.height() / 2, outer.height() / 2)
        painter.setBrush(QColor(AMBER))
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        painter.setFont(count_font())
        painter.setPen(QColor(AMBER_INK))
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), count_text(count))
    finally:
        painter.restore()
    return rect


def paint_new_dot(painter: QPainter, center: QPointF, *, ring: str | QColor = DOCK, color: str | QColor = HOLO,
                  scale: float = 1.0, opacity: float = 1.0) -> None:
    """The 7 px "new" dot with a 2 px ring of the surface around it."""
    if scale <= 0.0 or opacity <= 0.0:
        return
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(ring))
    r = NEW_DOT / 2 * scale
    painter.drawEllipse(center, r + 2, r + 2)
    painter.setOpacity(painter.opacity() * opacity)
    painter.setBrush(QColor(color))
    painter.drawEllipse(center, r, r)
    painter.restore()


def paint_ping(painter: QPainter, center: QPointF, progress: float, *, color: str | QColor = HOLO,
               radius: float = NEW_DOT / 2) -> None:
    """One ``hgPing`` frame: a dot growing to 3x while fading from 0.8 to 0 (progress 0..1, ease-out)."""
    if not 0.0 <= progress < 1.0:
        return
    k = bezier(CSS_EASE_OUT)(progress)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(alpha(color, 0.8 * (1.0 - k)))
    r = radius * (1.0 + 2.0 * k)
    painter.drawEllipse(center, r, r)
    painter.restore()


def paint_focus_ring(painter: QPainter, rect: QRectF | QRect, radius: float, *, offset: float = 2.0) -> None:
    """The keyboard focus ring: 2 px ``focus`` colour, ``offset`` px outside ``rect``."""
    r = QRectF(rect).adjusted(-offset - 1, -offset - 1, offset + 1, offset + 1)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(QPen(QColor(FOCUS), 2.0))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    rr = radius + offset + 1 if radius < r.height() / 2 else r.height() / 2
    painter.drawRoundedRect(r, rr, rr)
    painter.restore()


_KEYBOARD_FOCUS = (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                   Qt.FocusReason.ShortcutFocusReason)


def keyboard_focus(reason: Qt.FocusReason, previous: bool) -> bool:
    """CSS :focus-visible: a ring for keyboard focus, none for mouse or programmatic focus."""
    if reason in _KEYBOARD_FOCUS:
        return True
    if reason == Qt.FocusReason.ActiveWindowFocusReason:
        return previous
    return False

# --------------------------------------------------------------------------
# Buttons (spec 6)
# --------------------------------------------------------------------------

AMBER_COMMIT = "amber"
GO = "go"
GHOST = "ghost"
AMBER_GHOST = "amber_ghost"
DANGER = "danger"
DANGER_GHOST = "danger_ghost"
ICON = "icon"
LINK = "link"
LINK_CYAN = "link_cyan"
VARIANTS = (AMBER_COMMIT, GO, GHOST, AMBER_GHOST, DANGER, DANGER_GHOST, ICON, LINK, LINK_CYAN)
SIZE_MD = 38
SIZE_SM = 32
SIZE_LG = 56


@dataclass(frozen=True)
class ButtonLook:
    """(fill, border, label) at rest, on hover and pressed; ``ring`` = the 3 px hover ring colour."""

    rest: tuple[QColor, QColor, QColor]
    hover: tuple[QColor, QColor, QColor]
    pressed: tuple[QColor, QColor, QColor]
    ring: QColor | None
    weight: int


def _solid(fill: str, hi: str, lo: str, ink: str) -> ButtonLook:
    f, h, p, k = QColor(fill), QColor(hi), QColor(lo), QColor(ink)
    return ButtonLook((f, f, k), (h, h, k), (p, p, k), alpha(fill, 0.18), 600)


def _ghost(border: QColor, label: str, hover_fill: QColor, hover_border: QColor, hover_label: str,
           pressed_fill: QColor) -> ButtonLook:
    clear = alpha(VOID, 0.0)
    return ButtonLook((clear, border, QColor(label)), (hover_fill, hover_border, QColor(hover_label)),
                      (pressed_fill, hover_border, QColor(hover_label)), None, 500)


BUTTON_LOOKS: dict[str, ButtonLook] = {
    AMBER_COMMIT: _solid(AMBER, AMBER_HI, AMBER_LO, AMBER_INK),
    GO: _solid(HOLO, HOLO_HI, HOLO_LO, HOLO_INK),
    DANGER: _solid(ALERT, ALERT_HI, ALERT_LO, ALERT_INK),
    GHOST: _ghost(LINE_STRONG, INK_2, alpha(INK_1, 0.06), alpha(INK_1, 0.45), INK_1, alpha(INK_1, 0.10)),
    AMBER_GHOST: _ghost(alpha(AMBER, 0.42), AMBER_TX2, alpha(AMBER, 0.12), alpha(AMBER, 0.42), AMBER_TX,
                        alpha(AMBER, 0.18)),
    DANGER_GHOST: _ghost(alpha(ALERT, 0.55), ALERT_TX, alpha(ALERT, 0.12), alpha(ALERT, 0.55), ALERT_TX,
                         alpha(ALERT, 0.18)),
    ICON: _ghost(LINE, INK_3, alpha(INK_1, 0.08), LINE, INK_1, alpha(INK_1, 0.12)),
    LINK: ButtonLook((alpha(VOID, 0.0), alpha(VOID, 0.0), QColor(AMBER)),
                     (alpha(AMBER, 0.10), alpha(VOID, 0.0), QColor("#FFD58F")),
                     (alpha(AMBER, 0.14), alpha(VOID, 0.0), QColor("#FFD58F")), None, 600),
    LINK_CYAN: ButtonLook((alpha(VOID, 0.0), alpha(VOID, 0.0), QColor(HOLO)),
                          (alpha(HOLO, 0.10), alpha(VOID, 0.0), QColor("#9BEBFA")),
                          (alpha(HOLO, 0.14), alpha(VOID, 0.0), QColor("#9BEBFA")), None, 600),
}


class HoloButton(QAbstractButton):
    """One button for every variant of spec 6 (QPainter only).

    The widget is ``MARGIN`` px larger than the drawn button on every side, so the 3 px hover ring
    and the 2 px focus ring (2 px offset) fit inside it. Colours follow hover over 120 ms; the focus
    ring shows for keyboard focus only. Disabled: 45 % opacity and no hover. ``icon`` names a glyph
    of ``ICONS`` (16 px before the label; an ``ICON`` button shows only the glyph, ``diameter`` round).
    """

    MARGIN = 4

    def __init__(self, text: str = "", variant: str = GHOST, *, size: int = SIZE_MD, icon: str | None = None,
                 diameter: int | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._variant = variant if variant in BUTTON_LOOKS else GHOST
        self._size = size
        self._icon = icon
        self._diameter = diameter or (40 if variant == ICON else None)
        self._hover = 0.0
        self._focus_ring = False
        self.setText(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        if variant == ICON and text:
            self.setAccessibleName(text)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(HOVER_MS)
        self._anim.setEasingCurve(qt_easing(CSS_EASE_OUT))
        self._anim.valueChanged.connect(self._on_hover_value)

    # ---- API ------------------------------------------------------------------

    def variant(self) -> str:
        return self._variant

    def set_variant(self, variant: str) -> None:
        if variant in BUTTON_LOOKS and variant != self._variant:
            self._variant = variant
            self.updateGeometry()
            self.update()

    def set_icon_name(self, icon: str | None) -> None:
        self._icon = icon
        self.updateGeometry()
        self.update()

    def has_focus_ring(self) -> bool:
        return self._focus_ring

    def button_rect(self) -> QRectF:
        """The drawn button inside the widget (without the ring margin)."""
        m = self.MARGIN
        return QRectF(self.rect()).adjusted(m, m, -m, -m)

    # ---- geometry -------------------------------------------------------------

    def _font(self) -> QFont:
        look = BUTTON_LOOKS[self._variant]
        role = "button_sm" if self._size == SIZE_SM else "button"
        return font(role, 600 if look.weight == 600 else None)

    def _round(self) -> bool:
        return self._variant == ICON or self._size == SIZE_LG

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        m = 2 * self.MARGIN
        if self._round():
            d = self._diameter or self._size
            return QSize(d + m, d + m)
        if self._variant in (LINK, LINK_CYAN):
            metrics = QFontMetricsF(font("button"))
            return QSize(math.ceil(metrics.horizontalAdvance(self.text()) + 18) + m, SIZE_SM + m)
        metrics = QFontMetricsF(self._font())
        pad = 16 if self._size == SIZE_MD else 12
        lead = (pad - 3 if self._size == SIZE_MD else pad - 2) if self._icon else pad
        width = lead + pad + metrics.horizontalAdvance(self.text()) + (24 if self._icon else 0)
        return QSize(max(64, math.ceil(width)) + m, self._size + m)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.sizeHint()

    # ---- state ------------------------------------------------------------------

    def _on_hover_value(self, value: Any) -> None:
        self._hover = float(value)
        self.update()

    def _animate_hover(self, target: float) -> None:
        if reduced_motion():
            self._anim.stop()
            self._hover = target
            self.update()
            return
        self._anim.stop()
        self._anim.setStartValue(self._hover)
        self._anim.setEndValue(target)
        self._anim.start()

    def enterEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().enterEvent(event)
        if self.isEnabled():
            self._animate_hover(1.0)

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().leaveEvent(event)
        self._animate_hover(0.0)

    def focusInEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._focus_ring = keyboard_focus(event.reason(), self._focus_ring)
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.reason() != Qt.FocusReason.ActiveWindowFocusReason:
            self._focus_ring = False
        super().focusOutEvent(event)
        self.update()

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.isEnabled():
            self.animateClick()
            return
        super().keyPressEvent(event)

    def changeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().changeEvent(event)
        if not self.isEnabled():
            self._anim.stop()
            self._hover = 0.0

    # ---- painting -----------------------------------------------------------------

    def colors(self) -> tuple[QColor, QColor, QColor]:
        """(fill, border, label) for the current hover / pressed state."""
        look = BUTTON_LOOKS[self._variant]
        if self.isDown() and self.isEnabled():
            return look.pressed
        if self._hover <= 0.0:
            return look.rest
        return tuple(mix(a, b, self._hover) for a, b in zip(look.rest, look.hover))  # type: ignore[return-value]

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        look = BUTTON_LOOKS[self._variant]
        fill, border, label = self.colors()
        rect = self.button_rect()
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            if not self.isEnabled():
                painter.setOpacity(0.45)
            radius = rect.height() / 2 if self._round() else (RADIUS_BUTTON_SM if self._size == SIZE_SM
                                                               else RADIUS_BUTTON)
            if look.ring is not None and self._hover > 0.0 and self.isEnabled():
                ring = QColor(look.ring)
                ring.setAlphaF(ring.alphaF() * self._hover)
                painter.setPen(QPen(ring, 3.0))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                outer = rect.adjusted(-1.5, -1.5, 1.5, 1.5)
                painter.drawRoundedRect(outer, radius + 1.5, radius + 1.5)
            painter.setPen(QPen(border, 1.0) if border.alpha() else Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
            self._paint_content(painter, rect, label)
            if self._focus_ring and self.hasFocus():
                painter.setOpacity(1.0)
                paint_focus_ring(painter, rect, radius)
        finally:
            painter.end()

    def _paint_content(self, painter: QPainter, rect: QRectF, label: QColor) -> None:
        if self._variant == ICON or (self._size == SIZE_LG and not self.text()):
            glyph = 18 if (self._diameter or 40) >= 40 else 16
            if self._icon:
                box = QRectF(0, 0, glyph, glyph)
                box.moveCenter(rect.center())
                paint_icon(painter, box, self._icon, label, stroke=1.8)
            return
        text_font = font("button") if self._variant in (LINK, LINK_CYAN) else self._font()
        text_font.setUnderline(self._variant in (LINK, LINK_CYAN))
        painter.setFont(text_font)
        painter.setPen(label)
        x = rect.left()
        if self._icon:
            lead = 13 if self._size == SIZE_MD else 10
            box = QRectF(x + lead, rect.center().y() - 8, 16, 16)
            paint_icon(painter, box, self._icon, label, stroke=1.9)
            text_rect = QRectF(box.right() + 8, rect.top(), rect.right() - box.right() - 8, rect.height())
            painter.drawText(text_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.text())
        else:
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), self.text())

# --------------------------------------------------------------------------
# Action chip (spec 7)
# --------------------------------------------------------------------------

CHIP_CYAN = TONE_CYAN
CHIP_AMBER = TONE_AMBER
_CHIP_LOOKS = {   # border, fill, label, hover fill, hover label
    CHIP_CYAN: (alpha(HOLO, 0.25), alpha(HOLO, 0.05), INK_2, alpha(HOLO, 0.12), "#FFFFFF"),
    CHIP_AMBER: (alpha(AMBER, 0.45), alpha(AMBER, 0.06), AMBER_TX2, alpha(AMBER, 0.14), AMBER_TX),
}


class ActionChip(QAbstractButton):
    """The pill on Home ("Briefing ready" or "2 need your OK" + a chevron): 32 high, the chevron trailing.

    ``ping`` adds the leading cyan dot that pings three times when the chip appears (not in
    reduced motion). Same ``MARGIN`` convention as HoloButton.
    """

    MARGIN = 4
    HEIGHT = 32

    def __init__(self, text: str = "", tone: str = CHIP_CYAN, *, ping: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._tone = tone if tone in _CHIP_LOOKS else CHIP_CYAN
        self._ping = ping
        self._hover = 0.0
        self._focus_ring = False
        self._ping_progress = -1.0
        self.setText(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(HOVER_MS)
        self._hover_anim.valueChanged.connect(lambda v: self._set_hover(float(v)))
        self._ping_anim = QVariantAnimation(self)
        self._ping_anim.setStartValue(0.0)
        self._ping_anim.setEndValue(1.0)
        self._ping_anim.setDuration(1800)
        self._ping_anim.setLoopCount(3)
        self._ping_anim.valueChanged.connect(self._on_ping)
        self._ping_anim.finished.connect(lambda: self._on_ping(-1.0))

    def tone(self) -> str:
        return self._tone

    def set_tone(self, tone: str) -> None:
        if tone in _CHIP_LOOKS:
            self._tone = tone
            self.update()

    def start_ping(self) -> None:
        """Ping the leading dot three times (once per arrival)."""
        if self._ping and not reduced_motion():
            self._ping_anim.stop()
            self._ping_anim.start()

    def is_pinging(self) -> bool:
        return self._ping_anim.state() == QVariantAnimation.State.Running

    def _on_ping(self, value: Any) -> None:
        self._ping_progress = float(value)
        self.update()

    def _set_hover(self, value: float) -> None:
        self._hover = value
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        metrics = QFontMetricsF(font("button_sm"))
        width = 13 + (15 if self._ping else 0) + metrics.horizontalAdvance(self.text()) + 8 + 13 + 10
        return QSize(math.ceil(width) + 2 * self.MARGIN, self.HEIGHT + 2 * self.MARGIN)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.sizeHint()

    def enterEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().enterEvent(event)
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover)
        self._hover_anim.setEndValue(1.0)
        self._hover_anim.start()

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().leaveEvent(event)
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover)
        self._hover_anim.setEndValue(0.0)
        self._hover_anim.start()

    def focusInEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        self._focus_ring = keyboard_focus(event.reason(), self._focus_ring)
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
        border, fill, label, hover_fill, hover_label = _CHIP_LOOKS[self._tone]
        m = self.MARGIN
        rect = QRectF(self.rect()).adjusted(m, m, -m, -m)
        radius = rect.height() / 2
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(border, 1.0))
            painter.setBrush(mix(fill, hover_fill, self._hover))
            painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
            x = rect.left() + 13
            if self._ping:
                center = QPointF(x + 3.5, rect.center().y())
                paint_ping(painter, center, self._ping_progress)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(HOLO))
                painter.drawEllipse(center, 3.5, 3.5)
                x += 15
            ink = mix(label, hover_label, self._hover)
            painter.setFont(font("button_sm"))
            painter.setPen(ink)
            metrics = QFontMetricsF(font("button_sm"))
            text_w = metrics.horizontalAdvance(self.text())
            painter.drawText(QRectF(x, rect.top(), text_w + 2, rect.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.text())
            chevron = QRectF(x + text_w + 8, rect.center().y() - 6.5, 13, 13)
            paint_icon(painter, chevron, "chevron", ink, stroke=2.0)
            if self._focus_ring and self.hasFocus():
                paint_focus_ring(painter, rect, radius)
        finally:
            painter.end()

# --------------------------------------------------------------------------
# Round 2: shared faces for widgets that are not HoloButtons (spec 6, 8, 9)
# --------------------------------------------------------------------------

STAMP_OK = "ok"          # an ok disc with a check (sent, added, moved, accepted, cancelled, done)
STAMP_STEEL = "steel"    # a steel ring (denied, undone: nothing was sent or changed)
STAMP_ALERT = "alert"    # the warning glyph (failed)
STAMP_AMBER = "amber"    # an amber dot (unknown: check before retrying)
STAMP_HOLO = "holo"      # a cyan dot (working, waiting for the sign-in)
STAMP_DISC = 20          # a stamp's disc (16 in compact rows)
TICK_BOX = 18            # the NEW RECIPIENT tick's box


def paint_button_face(painter: QPainter, rect: QRectF, variant: str, *, hover: bool = False, down: bool = False,
                      enabled: bool = True, focus: bool = False, text: str = "", text_font: QFont | None = None,
                      icon: str | None = None, small: bool = False) -> None:
    """A spec 6 button drawn into ``rect`` (the button itself, without ring room) for widgets that keep their
    own class (the approval card's buttons): the variant's fill, border and label, the 3 px hover ring of a
    commit / go / danger button, 45 % when disabled and the keyboard focus ring. A link is its text alone."""
    look = BUTTON_LOOKS.get(variant, BUTTON_LOOKS[GHOST])
    if down and enabled:
        fill, border, label = look.pressed
    elif hover and enabled:
        fill, border, label = look.hover
    else:
        fill, border, label = look.rest
    radius = RADIUS_BUTTON_SM if small else RADIUS_BUTTON
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if not enabled:
            painter.setOpacity(painter.opacity() * 0.45)
        if look.ring is not None and hover and enabled:
            painter.setPen(QPen(look.ring, 3.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(-1.5, -1.5, 1.5, 1.5), radius + 1.5, radius + 1.5)
        painter.setPen(QPen(border, 1.0) if border.alpha() else Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        link = variant in (LINK, LINK_CYAN)
        use = QFont(text_font) if text_font is not None else font("button_sm" if small else "button",
                                                                    600 if look.weight == 600 else None)
        use.setUnderline(link)
        painter.setFont(use)
        painter.setPen(label)
        if icon:
            metrics = QFontMetricsF(use)
            lead = 10 if small else 13
            width = 16 + 8 + metrics.horizontalAdvance(text)
            left = max(rect.left() + lead, rect.center().x() - width / 2)
            box = QRectF(left, rect.center().y() - 8, 16, 16)
            paint_icon(painter, box, icon, label, stroke=1.9)
            painter.drawText(QRectF(box.right() + 8, rect.top(), rect.right() - box.right() - 8, rect.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
        else:
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextShowMnemonic), text)
        if focus:
            painter.setOpacity(1.0)
            paint_focus_ring(painter, rect, radius)
    finally:
        painter.restore()


def paint_stamp(painter: QPainter, center: QPointF, kind: str, *, size: float = STAMP_DISC) -> None:
    """A result stamp's disc (spec 8): ``kind`` is STAMP_OK, _STEEL, _ALERT, _AMBER or _HOLO."""
    r = size / 2
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if kind == STAMP_OK:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(OK))
            painter.drawEllipse(center, r, r)
            box = QRectF(center.x() - r * 0.62, center.y() - r * 0.62, r * 1.24, r * 1.24)
            paint_icon(painter, box, "check", OK_INK, stroke=3.0)
        elif kind == STAMP_STEEL:
            painter.setPen(QPen(QColor(STEEL), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, r - 0.75, r - 0.75)
        elif kind == STAMP_ALERT:
            box = QRectF(center.x() - r, center.y() - r, size, size)
            paint_icon(painter, box, "warning", ALERT, stroke=2.0)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(AMBER if kind == STAMP_AMBER else HOLO))
            painter.drawEllipse(center, 3.5, 3.5)
    finally:
        painter.restore()


def paint_tick_box(painter: QPainter, rect: QRectF, checked: bool, *, enabled: bool = True,
                   nudge: float = 0.0) -> None:
    """The NEW RECIPIENT tick's box (spec 9): 18 x 18, radius 4, a 1.5 px coral border; ticked = coral fill
    with a dark check. ``nudge`` (1..0) draws the coral ring that says "tick this first" (a click on the
    disabled Send)."""
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if not enabled:
            painter.setOpacity(painter.opacity() * 0.45)
        if nudge > 0.0:
            painter.setPen(QPen(alpha(ALERT, nudge), 2.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(-5, -5, 5, 5), 7, 7)
        painter.setPen(QPen(QColor(ALERT), 1.5))
        painter.setBrush(QColor(ALERT) if checked else alpha(ALERT, 0.0))
        painter.drawRoundedRect(rect.adjusted(0.75, 0.75, -0.75, -0.75), RADIUS_CHECKBOX, RADIUS_CHECKBOX)
        if checked:
            paint_icon(painter, rect.adjusted(2, 2, -2, -2), "check", ALERT_INK, stroke=3.0)
    finally:
        painter.restore()
