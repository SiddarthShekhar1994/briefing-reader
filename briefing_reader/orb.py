"""The Jarvis orb (HOLOGRAM_SPEC section 12): ``OrbWidget`` (hero 320 / compact 120) and ``MiniOrb`` (36).

Every layer of the approved hologram is drawn with QPainter from paths built once per level of
detail (rings as flat- or round-capped arc outlines, never rotated images): halo, frame + track,
R1-R6, the amp ring, the thinking sweep, the 24-bar crown, the core glow, core and core ring, the
listening ripples, the waiting scanner, the amber needs arc + dot and the mute slash. Motion is a
function of a monotonic clock; ring angles are integrated from their speeds (the thinking "boost"
eases in over 600 ms and out over 900 ms), so no state change ever makes a ring jump. State
changes cross-fade: ring scale 800 ms (std curve), ring opacity 600 ms, halo 800 ms, crown 400 ms,
sweep 450 ms, amp 500 ms, colours 600 ms; the needs arc draws in over 1 s and fades out in 400 ms;
a ripple that started finishes, no new one starts.

Paint cost (spec 12.9 budget): strokes up to 4 device pixels wide are drawn as 1 px cosmetic lines
(several concentric ones for wider strokes) carrying the stroke's coverage in their alpha, which is
Qt's fast antialiased path; the fine marks (R1 ticks, R2 dots, R3 hatch, R6 tick band) are radial
hairlines; the two faint wide bands (R3 at .22, R6 at .14) and the sweep's trail fill without
antialiasing; group opacity goes into the colours. The ground slice, the halo and the static frame +
track come from one cached pixmap per breath step (``BREATH_STEPS``), an opaque copy at rest.
Two-rate rendering: while the look is active (listening, speaking: 30 fps) and steady, the backdrop
and R1-R6 are one cached layer turned at the idle rate (``RING_LAYER_MS``); each 33 ms frame blits it
and paints only the fast parts (amp ring, crown, core, ripples). Thinking (its boosted rings and the
sweep), transitions and stills keep painting every ring per frame.

States
    Base states ``IDLE``, ``LISTENING``, ``THINKING``, ``SPEAKING``, ``NEEDS``, ``WAITING``; the two
    modifiers ``MUTED`` and ``OFFLINE`` (offline voice) change colour / core / track over any base
    state. ``state_word(base, muted, offline)`` is the header readout (``STANDBY`` ...);
    ``frame_interval_ms(base, muted, deep)`` the frame rate: 30 fps listening / thinking / speaking,
    10 fps idle / needs / offline / muted / waiting, 5 fps in deep idle. (Spec 12.9 says 15 fps idle;
    the orchestrator's round 1 decision is 10 fps, measured cost 5-8 % of a core at 15 fps.)

Deep idle
    After ``DEEP_IDLE_S`` (2 minutes) with no look change on any orb and no input to the app (keys,
    clicks, the wheel, the mouse over a window, a window activation; ``watch_input()`` installs the
    application-wide watcher, ``note_activity()`` counts as input), every orb that is not listening,
    thinking or speaking eases into a slower breathing pace (``DEEP_PACE``: rings, core and crown
    run at half speed, eased over 1.5 s) at 5 fps. Any change, input or activation returns at once.

OrbWidget(px=HERO)
    ``set_state(state)`` (``MUTED`` / ``OFFLINE`` mean idle with that modifier alone),
    ``set_muted(bool)``, ``set_offline(bool)``, ``set_look(base, muted=, offline=)``, ``state()``,
    ``look()``; ``set_level(value | None)`` feeds the voice envelope 0..1 while speaking (bars =
    2 + 16 x level, core x (1 + 0.18 x level)); without fresh levels a synthetic speaking curve
    runs and the hand-over cross-fades. ``set_orb_size(px)``; the widget is ``widget_side(px)``
    square (the halo overflows the orb's box). ``set_ground(fn)`` paints the window ground under the
    orb (``fn(painter, widget)``) and makes the widget opaque, so a frame never repaints the parent.
    ``set_phase(seconds | None)`` freezes the motion (screenshots); ``settle()`` ends transitions.
    ``set_dim(level)`` draws the whole orb at ``level`` of its strength (quiet hours: 0.7), ``dim()``.
    ``is_animating()``, ``frame_interval()``, ``detail()`` (``"full"`` / ``"compact"``),
    ``sample(now=None) -> OrbFrame`` (the computed values, for tests), ``paint_times()``.
    Accessible name "Jarvis, <state>" (e.g. "Jarvis, needs you").

MiniOrb()
    The 36 px glyph for the Dock's Home slot, the LIVE pop-out header and the toast: same state API
    (``set_state`` / ``set_look``); still at rest, 15 fps (``FRAME_MINI_MS``) only while listening,
    thinking or speaking.

Frame clock
    One shared precise QTimer drives every orb at the fastest interval any running orb wants; each
    orb repaints only its own rect at its own rate. An orb stops when it or its window is hidden,
    the window is minimized, the session is locked (``set_session_locked(bool)``, or
    ``set_lock_probe(fn)`` polled every 2 s while an orb wants frames), reduced motion is on
    (``holo.reduced_motion()``: one repaint per state change) or ``set_animated(False)``.
    If the hero's average paint over 60 steady frames exceeds 4 ms it drops the fine layers (the
    compact layer set, hero strokes kept; ``detail()`` "reduced") and logs ``Orb: compact layers
    (paint %.1f ms)`` once (``auto_degrade``).
"""

from __future__ import annotations

import logging
import math
import time
import weakref
from collections import OrderedDict, deque
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable

import shiboken6
from PySide6.QtCore import QEvent, QLineF, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget

from . import holo

logger = logging.getLogger(__name__)

IDLE = "idle"
LISTENING = "listening"
THINKING = "thinking"
SPEAKING = "speaking"
NEEDS = "needs"
WAITING = "waiting"
MUTED = "muted"
OFFLINE = "offline"
BASE_STATES = (IDLE, LISTENING, THINKING, SPEAKING, NEEDS, WAITING)
STATES = BASE_STATES + (MUTED, OFFLINE)
ACTIVE_STATES = (LISTENING, THINKING, SPEAKING)
READOUTS = {
    IDLE: "STANDBY", LISTENING: "LISTENING", THINKING: "THINKING", SPEAKING: "SPEAKING", NEEDS: "NEEDS YOU",
    WAITING: "WAITING FOR THE AM BRIEFING", MUTED: "MUTED", OFFLINE: "OFFLINE VOICE",
}
HERO = 320
COMPACT = 120
MINI = 36
COMPACT_BELOW = 200            # sizes under 200 px get the compact level of detail
FRAME_ACTIVE_MS = 33           # 30 fps: listening, thinking, speaking
FRAME_IDLE_MS = 100            # 10 fps: idle, needs you, offline voice (round 1 decision; spec 12.9: 15 fps)
FRAME_SLOW_MS = 100            # 10 fps: muted, waiting
FRAME_DEEP_MS = 200            # 5 fps: deep idle
FRAME_MINI_MS = 66             # 15 fps: the mini orb while listening, thinking or speaking
DEEP_IDLE_S = 120.0            # no look change and no input this long: deep idle
DEEP_PACE = 0.5                # deep idle runs every motion at half speed ...
DEEP_EASE_S = 1.5              # ... eased in over this long
WAKE_EASE_S = 0.4              # and back to full speed this fast
MAX_STEP_S = 0.25              # the longest step one frame advances (a 5 fps frame is 0.2 s)
DEGRADE_MS = 4.0
LEVEL_FRESH_S = 0.3
LOCK_POLL_MS = 2000
RING_LAYER_MS = FRAME_IDLE_MS  # active looks: the backdrop + R1-R6 layer is rebuilt this often
_FILL_OPAQUE = Qt.GlobalColor.black  # under a ground: an opaque pixmap (no alpha) copies fastest
BREATH_STEPS = 5               # cached backdrops per look: the halo breath (.7 .. 1) in five steps

_STD = holo.bezier(holo.EASE_STD)
_EASE = holo.bezier(holo.CSS_EASE)
_IN_OUT = holo.bezier(holo.CSS_EASE_IN_OUT)
_RIPPLE_EASE = holo.bezier((0.4, 0.0, 0.2, 1.0))


def state_word(base: str, muted: bool = False, offline: bool = False) -> str:
    """The header readout for a look: MUTED wins, then an active base state, then OFFLINE VOICE."""
    if muted:
        return READOUTS[MUTED]
    if base in (LISTENING, THINKING, SPEAKING, NEEDS):
        return READOUTS[base]
    if offline:
        return READOUTS[OFFLINE]
    return READOUTS.get(base, READOUTS[IDLE])


def frame_interval_ms(base: str, muted: bool = False, deep: bool = False) -> int:
    """Frame interval of a look: 33 ms listening / thinking / speaking; 200 ms in deep idle; 100 ms
    otherwise (idle, needs you, offline voice, muted, waiting)."""
    if base in ACTIVE_STATES:
        return FRAME_ACTIVE_MS
    if deep:
        return FRAME_DEEP_MS
    if muted or base == WAITING:
        return FRAME_SLOW_MS
    return FRAME_IDLE_MS


def widget_side(px: int) -> int:
    """The square widget holding an orb of ``px``: room for the halo (r ~ 195 of 180 units), rounded up to
    a multiple of 4 px, so an opaque orb placed on a multiple of 4 covers whole device pixels at 125, 150
    and 175 % scaling too (never a column of the window left unpainted)."""
    side = px + 2 * math.ceil(px * 0.045)
    return side + (-side) % 4

# --------------------------------------------------------------------------
# Targets per look (spec 12.4 orbVals, generalised to base + modifiers)
# --------------------------------------------------------------------------

_ROWS: dict[str, tuple[tuple[float, ...], float, tuple[float, ...], float]] = {   # s1-6, s7, o1-6, halo
    IDLE: ((1, 1, 1, 1, 1, 1), 1.0, (0.6, 0.7, 0.75, 0.85, 0.8, 0.9), 0.55),
    LISTENING: ((0.86, 0.89, 0.92, 0.95, 0.97, 0.985), 0.9, (1, 0.95, 0.7, 1, 0.9, 1), 0.85),
    THINKING: ((0.98, 1.02, 0.98, 1.03, 1, 1), 1.0, (0.85, 1, 1, 1, 1, 1), 1.0),
    SPEAKING: ((1, 1, 1, 1, 1, 1), 1.02, (0.8, 0.9, 0.85, 1, 0.9, 1), 0.9),
    NEEDS: ((1, 1, 1, 1, 1, 1), 1.0, (0.5, 0.6, 0.6, 0.75, 0.7, 0.8), 0.75),
    WAITING: ((0.97,) * 6, 0.85, (0.3, 0.35, 0.35, 0.45, 0.4, 0.45), 0.25),
}
_MUTED_O = (0.4, 0.5, 0.45, 0.6, 0.55, 0.6)
CORE_HOT, CORE_FLAT, CORE_HOLLOW = "hot", "flat", "hollow"
CROWN_SPEAK, CROWN_REST, CROWN_STILL = "speak", "rest", "still"


@dataclass(frozen=True)
class _Targets:
    s: tuple[float, ...]
    s7: float
    o: tuple[float, ...]
    halo: float
    halo_color: str
    halo_a: float
    halo_b: float
    lit: bool
    slow: float
    ink: str
    boost: float
    amp: float
    amp_op: float
    sweep: float
    crown: str
    crown_op: float
    core_op: float
    core_anim: str
    core: str
    glow: float
    ripple: bool
    scan: float
    alert: float
    slash: float
    frame: float
    dashed: bool


def _targets(base: str, muted: bool, offline: bool) -> _Targets:
    s, s7, o, halo = _ROWS.get(base, _ROWS[IDLE])
    if muted:
        s7 = min(s7, 0.94)
        o = tuple(min(a, b) for a, b in zip(o, _MUTED_O))
        halo = 0.3
    elif offline:
        if base == IDLE:
            s7 = 0.96
        o = (0.35,) + tuple(o[1:])
        halo = 0.45
    lit = not (muted or base == WAITING)
    speaking = base == SPEAKING
    if speaking:
        crown = CROWN_SPEAK
    else:
        crown = CROWN_REST if lit else CROWN_STILL
    if muted:
        core_anim = "none"
    else:
        core_anim = {IDLE: "breath", LISTENING: "listen", THINKING: "flicker", SPEAKING: "speak", NEEDS: "breath",
                     WAITING: "breath_slow"}.get(base, "breath")
    halo_color = holo.STEEL if muted else (holo.AMBER if base == NEEDS else holo.HOLO)
    return _Targets(
        s=tuple(float(v) for v in s), s7=float(s7), o=tuple(float(v) for v in o), halo=halo,
        halo_color=halo_color, halo_a=0.2 if halo_color == holo.AMBER else 0.15,
        halo_b=0.06 if halo_color == holo.AMBER else 0.05, lit=lit, slow=2.0 if not lit else 1.0,
        ink=holo.STEEL if muted else holo.HOLO, boost=1.0 if base == THINKING else 0.0,
        amp=1.0 if speaking else 0.0, amp_op=1.0 if speaking else (0.3 if lit else 0.15),
        sweep=1.0 if base == THINKING else 0.0, crown=crown,
        crown_op=0.95 if speaking else (0.4 if base == NEEDS else (0.55 if lit else 0.3)),
        core_op=0.6 if base == WAITING else 1.0, core_anim=core_anim,
        core=CORE_HOLLOW if muted else (CORE_FLAT if offline else CORE_HOT),
        glow=0.0 if muted else (0.5 if offline else 1.0), ripple=base == LISTENING,
        scan=1.0 if base == WAITING else 0.0, alert=1.0 if base == NEEDS else 0.0, slash=1.0 if muted else 0.0,
        frame=0.5 if (offline or base == WAITING) else 1.0, dashed=offline,
    )

# --------------------------------------------------------------------------
# Curves (spec 12.3 keyframes)
# --------------------------------------------------------------------------


def _frames(points: tuple[tuple[float, ...], ...], p: float, ease: Callable[[float], float]) -> tuple[float, ...]:
    """Keyframes ``(pos, v1, v2, ...)`` at progress ``p`` (0..1), ``ease`` applied per segment."""
    for (p0, *a), (p1, *b) in zip(points, points[1:]):
        if p <= p1:
            k = ease((p - p0) / (p1 - p0)) if p1 > p0 else 1.0
            return tuple(x + (y - x) * k for x, y in zip(a, b))
    return tuple(points[-1][1:])


def _linear(x: float) -> float:
    return x


_BREATH = ((0.0, 0.94, 0.86), (0.5, 1.05, 1.0), (1.0, 0.94, 0.86))
_LISTEN = ((0.0, 0.92, 1.0), (0.5, 1.02, 1.0), (1.0, 0.92, 1.0))
_FLICKER = ((0.0, 1.0, 1.0), (0.18, 1.07, 0.72), (0.34, 0.98, 1.0), (0.52, 1.1, 0.8), (0.70, 0.97, 1.0),
            (0.86, 1.05, 0.86), (1.0, 1.0, 1.0))
_SPEAK = ((0.0, 1.0), (0.11, 1.16), (0.22, 0.97), (0.35, 1.11), (0.46, 1.01), (0.58, 1.2), (0.70, 0.96),
          (0.84, 1.09), (1.0, 1.0))
_AMP = ((0.0, 1.0, 0.4), (0.24, 1.035, 0.95), (0.48, 1.008, 0.55), (0.72, 1.05, 1.0), (1.0, 1.0, 0.4))
_HALO = ((0.0, 0.7), (0.5, 1.0), (1.0, 0.7))
_DOT = ((0.0, 0.35), (0.5, 1.0), (1.0, 0.35))
_CROWN_PERIODS = (0.42, 0.55, 0.67)
_CROWN_DELAYS = (-0.1, -0.33, -0.21)


def _cycle(t: float, period: float) -> float:
    return (t % period) / period


def core_curve(anim: str, t: float) -> tuple[float, float]:
    """(scale, opacity) of the core animation ``anim`` at time ``t`` seconds."""
    if anim == "breath":
        return _frames(_BREATH, _cycle(t, 4.6), _IN_OUT)  # type: ignore[return-value]
    if anim == "breath_slow":
        return _frames(_BREATH, _cycle(t, 6.0), _IN_OUT)  # type: ignore[return-value]
    if anim == "listen":
        return _frames(_LISTEN, _cycle(t, 1.1), _IN_OUT)  # type: ignore[return-value]
    if anim == "flicker":
        return _frames(_FLICKER, _cycle(t, 1.2), _linear)  # type: ignore[return-value]
    if anim == "speak":
        return _frames(_SPEAK, _cycle(t, 1.05), _linear)[0], 1.0
    return 1.0, 1.0


def _alternate(t: float, period: float, delay: float) -> float:
    """CSS ``alternate`` ease-in-out progress 0..1..0 with a (negative) delay."""
    tau = t - delay
    n = math.floor(tau / period)
    local = (tau - n * period) / period
    return _IN_OUT(1.0 - local if n % 2 else local)


def crown_lengths(mode: str, t: float) -> tuple[float, float, float]:
    """Bar lengths (units) of the three crown groups for ``mode`` at ``t`` (synthetic, no voice)."""
    if mode == CROWN_SPEAK:
        return tuple(3.0 + 15.0 * _alternate(t, d, delay)  # type: ignore[return-value]
                     for d, delay in zip(_CROWN_PERIODS, _CROWN_DELAYS))
    if mode == CROWN_REST:
        return tuple(2.0 + 2.5 * _alternate(t, d * 3.4, delay)  # type: ignore[return-value]
                     for d, delay in zip(_CROWN_PERIODS, _CROWN_DELAYS))
    return (3.0, 3.0, 3.0)


def synthetic_level(t: float) -> float:
    """The speaking envelope without voice data: the ``hgSpeak`` curve mapped to 0..1."""
    scale = _frames(_SPEAK, _cycle(t, 1.05), _linear)[0]
    return max(0.0, min(1.0, (scale - 0.96) / 0.24))

# --------------------------------------------------------------------------
# Geometry (built once per level of detail)
# --------------------------------------------------------------------------


def _polar(r: float, degrees: float) -> QPointF:
    a = math.radians(degrees)
    return QPointF(r * math.cos(a), r * math.sin(a))


def _dashes(pattern: tuple[float, ...] | None, offset: float = 0.0) -> list[tuple[float, float]]:
    """SVG dash intervals (degrees, pathLength 360, starting at 3 o'clock clockwise)."""
    if pattern is None:
        return [(0.0, 360.0)]
    pat = list(pattern) * (2 if len(pattern) % 2 else 1)
    total = sum(pat)
    out: list[tuple[float, float]] = []
    pos = -(offset % total)
    while pos < 360.0:
        q = pos
        for i, seg in enumerate(pat):
            if i % 2 == 0:
                start, end = max(q, 0.0), min(q + seg, 360.0)
                if end > start:
                    out.append((start, end))
            q += seg
        pos += total
    return out


def _band(path: QPainterPath, r: float, width: float, a0: float, a1: float) -> None:
    """A flat-capped stroke of the arc a0..a1 (degrees clockwise) on radius r as a fill outline."""
    ro, ri = r + width / 2, max(0.0, r - width / 2)
    span = a1 - a0
    if span >= 359.999:
        path.addEllipse(QPointF(0, 0), ro, ro)
        path.addEllipse(QPointF(0, 0), ri, ri)
        return
    if span < 4.0:
        path.moveTo(_polar(ro, a0))
        path.lineTo(_polar(ro, a1))
        path.lineTo(_polar(ri, a1))
        path.lineTo(_polar(ri, a0))
        path.closeSubpath()
        return
    outer, inner = QRectF(-ro, -ro, 2 * ro, 2 * ro), QRectF(-ri, -ri, 2 * ri, 2 * ri)
    path.arcMoveTo(outer, -a0)
    path.arcTo(outer, -a0, -span)
    path.arcTo(inner, -a1, span)
    path.closeSubpath()


def _round_band(path: QPainterPath, r: float, width: float, a0: float, a1: float) -> None:
    """A round-capped stroke of the arc a0..a1: outer arc, end cap, inner arc back, start cap."""
    half = width / 2
    ro, ri = r + half, r - half
    outer, inner = QRectF(-ro, -ro, 2 * ro, 2 * ro), QRectF(-ri, -ri, 2 * ri, 2 * ri)

    def cap(a: float) -> QRectF:
        c = _polar(r, a)
        return QRectF(c.x() - half, c.y() - half, width, width)

    span = a1 - a0
    path.arcMoveTo(outer, -a0)
    path.arcTo(outer, -a0, -span)
    path.arcTo(cap(a1), -a1, -180.0)
    path.arcTo(inner, -a1, span)
    path.arcTo(cap(a0), 180.0 - a0, -180.0)
    path.closeSubpath()


def _centre(r: float, pattern: tuple[float, ...] | None, offset: float = 0.0) -> QPainterPath:
    """The dashes' centre lines (for hairline strokes)."""
    path = QPainterPath()
    if pattern is None:
        path.addEllipse(QPointF(0, 0), r, r)
        return path
    rect = QRectF(-r, -r, 2 * r, 2 * r)
    for a0, a1 in _dashes(pattern, offset):
        path.arcMoveTo(rect, -a0)
        path.arcTo(rect, -a0, -(a1 - a0))
    return path


def _ring(r: float, width: float, pattern: tuple[float, ...] | None, offset: float = 0.0,
          round_cap: bool = False) -> QPainterPath:
    path = QPainterPath()
    for a0, a1 in _dashes(pattern, offset):
        (_round_band if round_cap else _band)(path, r, width, a0, a1)
    return path


def _marks(r: float, width: float, pattern: tuple[float, ...], offset: float = 0.0) -> tuple[QLineF, ...]:
    """Fine dashes (under ~1.5 px wide) as radial centre lines; painted as cosmetic hairlines."""
    lines = []
    for a0, a1 in _dashes(pattern, offset):
        a = math.radians((a0 + a1) / 2)
        ca, sa = math.cos(a), math.sin(a)
        lines.append(QLineF((r - width / 2) * ca, (r - width / 2) * sa, (r + width / 2) * ca, (r + width / 2) * sa))
    return tuple(lines)


@dataclass(frozen=True)
class _Elem:
    """One SVG ring element: a fill outline, or fine marks (``lines``, each ``mark`` units wide)."""

    alpha: float
    path: QPainterPath | None = None
    lines: tuple[QLineF, ...] | None = None
    mark: float = 0.0
    centre: QPainterPath | None = None
    width: float = 0.0
    aa: bool = True            # False: a faint wide band filled without antialiasing (4x cheaper, edges at <= .2)


def _fine(r: float, width: float, pattern: tuple[float, ...], alpha: float, offset: float = 0.0) -> _Elem:
    return _Elem(alpha, lines=_marks(r, width, pattern, offset), mark=math.radians(pattern[0]) * r)


@dataclass(frozen=True)
class _Lod:
    compact: bool
    h1: float
    h2: float
    h3: float
    dot_r: float
    cut: float
    bar_deg: float
    major: tuple[float, float]
    rings: tuple[tuple[_Elem, ...], ...]                          # R1..R6
    ticks: QPainterPath
    track: _Elem
    track_dashed: _Elem
    r4_dot: QPointF
    sweep: tuple[tuple[QPainterPath, float, bool], ...]          # (path, alpha, antialiased)
    core_ring_w: float
    core_edge_w: float


_SPINS = ((1, 140.0, 1, 5.0), (-1, 70.0, -1, 2.6), (1, 40.0, 1, 1.6), (-1, 28.0, -1, 1.2), (1, 90.0, 1, 3.0),
          (1, 180.0, -1, 2.2))       # (direction, period s, boost direction, boost period s)
_TICK_BAND = (0.35, 4.65, 0.35, 4.65, 0.35, 4.65, 0.35, 4.65, 0.35, 15.65)


@lru_cache(maxsize=4)
def _lod(compact: bool, fine: bool | None = None) -> _Lod:
    """Geometry for compact (120 px) or hero strokes; ``fine`` (default: hero only) adds the fine layers
    (R1 ticks, R2 dots, R3 hatch, R6 tick band). The degraded hero is ``_lod(False, False)``."""
    h1, h2, h3 = (2.4, 3.6, 5.4) if compact else (1.0, 1.5, 2.6)
    major = (1.8, 28.2) if compact else (0.9, 29.1)
    fine = (not compact) if fine is None else fine

    def elems(*items: _Elem | None) -> tuple[_Elem, ...]:
        return tuple(item for item in items if item is not None)

    def band(r: float, width: float, pattern: tuple[float, ...] | None, alpha: float, offset: float = 0.0,
             round_cap: bool = False, aa: bool = True) -> _Elem:
        return _Elem(alpha, path=_ring(r, width, pattern, offset, round_cap), centre=_centre(r, pattern, offset),
                     width=width, aa=aa)

    rings = (
        elems(_fine(160, 5, (0.45, 2.55), 0.5) if fine else None, band(160, 11, major, 0.95),
              band(167, h1, (40, 5, 2, 5, 8, 30), 0.32)),
        elems(band(144, h2, (62, 6, 20, 6, 3, 23), 0.9), _fine(149, 1.2, (0.5, 2.5), 0.4) if fine else None),
        elems(_fine(127, 10, (0.5, 1.5), 0.2) if fine else None, band(127, 10, (34, 146), 0.22, aa=False),
              band(134, h2, (34, 146), 0.9), band(120, h1, (34, 146), 0.45)),
        elems(band(110, h3, (84, 36), 0.95, round_cap=True)),
        elems(band(96, h1, None, 0.5), band(96, 6, (1.4, 88.6), 0.95), band(100, h1, (20, 25), 0.35)),
        elems(band(79, 15, (30, 6), 0.14, aa=False), _fine(79, 15, _TICK_BAND, 0.35, -5) if fine else None,
              band(86.5, h2, (30, 6), 0.75), band(71.5, h2, (30, 6), 0.75), band(64, h1, None, 0.4)),
    )
    ticks = QPainterPath()
    half = h2 / 2
    for x0, y0, x1, y1 in ((0, -177, 0, -168), (0, 168, 0, 177), (-177, 0, -168, 0), (168, 0, 177, 0)):
        ticks.addRect(QRectF(min(x0, x1) - (half if x0 == x1 else 0), min(y0, y1) - (half if y0 == y1 else 0),
                             abs(x1 - x0) or h2, abs(y1 - y0) or h2))
    # The mockup's sweep: three trailing bands (70 / 46 / 22 deg at .07 / .08 / .10) and a 0.6 deg
    # leading edge at .9, layered so they composite into steps exactly like the SVG.
    sweep = tuple((_ring(124, 42, dash, offset), a, a > 0.5) for dash, offset, a in
                  (((70, 290), 0, 0.07), ((46, 314), -24, 0.08), ((22, 338), -48, 0.1), ((0.6, 359.4), -69.4, 0.9)))
    nominal = COMPACT if compact else HERO
    return _Lod(compact=compact, h1=h1, h2=h2, h3=h3, dot_r=7.0 if compact else 3.5, cut=16.0 if compact else 8.0,
                bar_deg=6.0 if compact else 3.5, major=major, rings=rings, ticks=ticks,
                track=band(173, h1, None, 0.22), track_dashed=band(173, h1, (5, 5), 0.22), r4_dot=QPointF(0, -110),
                sweep=sweep, core_ring_w=360.0 / nominal, core_edge_w=(1.5 if compact else 2.0) * 360.0 / nominal)


def _glow_brush(level: float) -> QBrush:
    gradient = QRadialGradient(QPointF(0, 0), 68.4 * math.sqrt(2))
    gradient.setColorAt(0.0, holo.alpha(holo.HOLO, 0.53 * level))
    gradient.setColorAt(0.42, holo.alpha(holo.HOLO, 0.15 * level))
    gradient.setColorAt(0.70, holo.alpha(holo.HOLO, 0.0))
    gradient.setColorAt(1.0, holo.alpha(holo.HOLO, 0.0))
    return QBrush(gradient)


@lru_cache(maxsize=4)
def _core_brush(look: str) -> QBrush:
    if look == CORE_FLAT:
        gradient = QRadialGradient(QPointF(0, 0), 27 * math.sqrt(2))
        gradient.setColorAt(0.0, holo.alpha(holo.HOLO, 0.95))
        gradient.setColorAt(0.7, holo.alpha(holo.HOLO, 0.7))
        gradient.setColorAt(1.0, holo.alpha(holo.HOLO, 0.3))
        return QBrush(gradient)
    # radial-gradient(circle at 50% 46%, ...) in a 54-unit box: farthest corner 39.74 from (0, -2.16)
    gradient = QRadialGradient(QPointF(0, -2.16), 39.74)
    gradient.setColorAt(0.0, QColor("#FFFFFF"))
    gradient.setColorAt(0.24, QColor("#EFFFFF"))
    gradient.setColorAt(0.60, QColor(holo.HOLO))
    gradient.setColorAt(0.82, holo.alpha(holo.HOLO, 0.6))
    gradient.setColorAt(1.0, holo.alpha(holo.HOLO, 0.2))
    return QBrush(gradient)


_GLOW: QBrush | None = None


def _glow() -> QBrush:
    """The core glow at full strength; offline draws it at half opacity (glowLow = half the alphas)."""
    global _GLOW
    if _GLOW is None:
        _GLOW = _glow_brush(1.0)
    return _GLOW


_GLOW_BOX = QRectF(-68.4, -68.4, 136.8, 136.8)
_CORE_BOX = QRectF(-27.0, -27.0, 54.0, 54.0)
_CORE_PIXMAPS: dict[tuple[str, int, float], QPixmap] = {}
_CORE_OVERSCALE = 1.3          # the core curve and s7 scale it up to ~1.23: render a little larger


def _core_pixmap(look: str, px: int, dpr: float) -> QPixmap:
    """The glow (``"glow"``) or a core look as a pixmap, drawn scaled by the core curve each frame."""
    key = (look, px, round(dpr, 3))
    pixmap = _CORE_PIXMAPS.get(key)
    if pixmap is not None:
        return pixmap
    box = _GLOW_BOX if look == "glow" else _CORE_BOX
    scale = px / 360.0 * dpr * _CORE_OVERSCALE
    side = max(2, math.ceil(box.width() * scale))
    pixmap = QPixmap(side, side)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.translate(side / 2, side / 2)
        painter.scale(side / box.width(), side / box.width())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_glow() if look == "glow" else _core_brush(look))
        painter.drawEllipse(QPointF(0, 0), box.width() / 2, box.width() / 2)
    finally:
        painter.end()
    if len(_CORE_PIXMAPS) > 24:
        _CORE_PIXMAPS.clear()
    _CORE_PIXMAPS[key] = pixmap
    return pixmap


_HALO_CACHE: dict[tuple[str, float, float, int, float], QPixmap] = {}


def _halo_pixmap(color: str, a: float, b: float, px: int, dpr: float) -> QPixmap:
    """The halo: radial c@a at 0, c@b at 112 units, clear at 195 (CSS farthest-corner of inset -8 %)."""
    key = (color, a, b, px, round(dpr, 3))
    pixmap = _HALO_CACHE.get(key)
    if pixmap is not None:
        return pixmap
    scale = px / 360.0
    radius = 195.0 * scale
    side = math.ceil(2 * radius * dpr)
    pixmap = QPixmap(side, side)
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        center = QPointF(side / dpr / 2, side / dpr / 2)
        gradient = QRadialGradient(center, 295.3 * scale)
        gradient.setColorAt(0.0, holo.alpha(color, a))
        gradient.setColorAt(0.38, holo.alpha(color, b))
        gradient.setColorAt(0.66, holo.alpha(color, 0.0))
        gradient.setColorAt(1.0, holo.alpha(color, 0.0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(gradient))
        painter.drawEllipse(center, radius, radius)
    finally:
        painter.end()
    if len(_HALO_CACHE) > 24:
        _HALO_CACHE.clear()
    _HALO_CACHE[key] = pixmap
    return pixmap

# --------------------------------------------------------------------------
# Tweens
# --------------------------------------------------------------------------


class _Tween:
    """A scalar easing from its value at the last retarget to the target over ``dur`` seconds."""

    __slots__ = ("a", "b", "t0", "dur", "ease")

    def __init__(self, value: float) -> None:
        self.a = self.b = float(value)
        self.t0 = 0.0
        self.dur = 0.0
        self.ease: Callable[[float], float] = _STD

    def to(self, value: float, now: float, dur: float, ease: Callable[[float], float] = _STD) -> None:
        value = float(value)
        if value == self.b:
            return
        self.a, self.b, self.t0, self.dur, self.ease = self.value(now), value, now, dur, ease

    def value(self, now: float) -> float:
        if self.dur <= 0.0 or now >= self.t0 + self.dur:
            return self.b
        return self.a + (self.b - self.a) * self.ease(max(0.0, (now - self.t0) / self.dur))

    def done(self, now: float) -> bool:
        return self.dur <= 0.0 or now >= self.t0 + self.dur

    def settle(self) -> None:
        self.a, self.dur = self.b, 0.0


class _Fade:
    """A cross-fade between a previous and a current discrete value (colour, core look)."""

    __slots__ = ("prev", "cur", "t0", "dur")

    def __init__(self, value: Any) -> None:
        self.prev = self.cur = value
        self.t0 = 0.0
        self.dur = 0.0

    def to(self, value: Any, now: float, dur: float) -> None:
        if value == self.cur:
            return
        self.prev = self.cur if self.mix(now) >= 0.5 else self.prev
        self.cur, self.t0, self.dur = value, now, dur

    def mix(self, now: float) -> float:
        if self.dur <= 0.0 or now >= self.t0 + self.dur:
            return 1.0
        return _EASE(max(0.0, (now - self.t0) / self.dur))

    def done(self, now: float) -> bool:
        return self.mix(now) >= 1.0

    def settle(self) -> None:
        self.prev, self.dur = self.cur, 0.0

# --------------------------------------------------------------------------
# Frame clock
# --------------------------------------------------------------------------


class _FrameClock(QObject):
    """One precise timer for every orb, at the fastest interval a running orb wants."""

    def __init__(self) -> None:
        super().__init__()
        self._wanted: weakref.WeakKeyDictionary[QWidget, int] = weakref.WeakKeyDictionary()
        self._timer = QTimer(self)
        # Windows rounds a coarse timer up to its 15.6 ms tick; a precise one keeps the rate.
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self._lock_timer = QTimer(self)
        self._lock_timer.setInterval(LOCK_POLL_MS)
        self._lock_timer.timeout.connect(self._poll_lock)
        self._locked = False
        self._probe: Callable[[], bool] | None = None
        self._orbs: weakref.WeakSet[QWidget] = weakref.WeakSet()
        self._activity_at = time.monotonic()
        self._deep = False

    def register(self, orb: QWidget) -> None:
        """Every orb hears deep idle start and end (also while it is still)."""
        self._orbs.add(orb)

    def want(self, orb: QWidget, interval_ms: int | None) -> None:
        if interval_ms is None:
            self._wanted.pop(orb, None)
        else:
            self._wanted[orb] = interval_ms
        self._retime()

    def wants(self, orb: QWidget) -> bool:
        return orb in self._wanted

    def running(self) -> bool:
        try:
            return self._timer.isActive()
        except RuntimeError:          # Qt is shutting down
            return False

    def interval(self) -> int:
        return self._timer.interval()

    def locked(self) -> bool:
        return self._locked

    def set_locked(self, locked: bool) -> None:
        if locked != self._locked:
            self._locked = locked
            self._retime()
            if not locked:
                for orb in list(self._wanted.keys()):
                    if shiboken6.isValid(orb):
                        orb.update()

    def set_probe(self, probe: Callable[[], bool] | None) -> None:
        self._probe = probe
        self._retime()

    # ---- deep idle ------------------------------------------------------------------------

    def deep(self) -> bool:
        return self._deep

    def idle_seconds(self) -> float:
        return time.monotonic() - self._activity_at

    def note_activity(self) -> None:
        """A look change, input or a window activation: deep idle ends at once (and its clock restarts)."""
        self._activity_at = time.monotonic()
        if self._deep:
            self.set_deep(False)

    def set_deep(self, deep: bool) -> None:
        """Enter or leave deep idle now (the clock does this itself after DEEP_IDLE_S; tests and harnesses)."""
        deep = bool(deep)
        if deep == self._deep:
            return
        self._deep = deep
        if not deep:
            self._activity_at = time.monotonic()
        for orb in list(self._orbs):
            if shiboken6.isValid(orb):
                orb._deep_changed(deep)
        self._retime()

    def _retime(self) -> None:
        try:
            live = [ms for orb, ms in list(self._wanted.items()) if shiboken6.isValid(orb)]
            if live and not self._locked:
                interval = min(live)
                if self._timer.interval() != interval:
                    self._timer.setInterval(interval)
                if not self._timer.isActive():
                    self._timer.start()
            elif self._timer.isActive():
                self._timer.stop()
            if live and self._probe is not None:
                if not self._lock_timer.isActive():
                    self._lock_timer.start()
            elif self._lock_timer.isActive():
                self._lock_timer.stop()
        except RuntimeError:          # timers already deleted while Qt shuts down
            pass

    def _poll_lock(self) -> None:
        if self._probe is None:
            return
        try:
            locked = bool(self._probe())
        except Exception as exc:  # noqa: BLE001 - an unknown answer keeps the orb running
            logger.debug("Lock probe failed: %s", exc)
            locked = False
        self.set_locked(locked)

    def _tick(self) -> None:
        now = time.monotonic()
        if not self._deep and now - self._activity_at >= DEEP_IDLE_S:
            self.set_deep(True)
        for orb, ms in list(self._wanted.items()):
            if not shiboken6.isValid(orb):
                self._wanted.pop(orb, None)
                continue
            if now - getattr(orb, "_last_frame", 0.0) >= (ms - 6) / 1000.0:
                orb._request_frame()


_clock: _FrameClock | None = None


def frame_clock() -> _FrameClock:
    global _clock
    if _clock is None:
        _clock = _FrameClock()
    return _clock


def set_session_locked(locked: bool) -> None:
    """Stop (True) or resume (False) every orb because the Windows session is locked."""
    frame_clock().set_locked(locked)


def set_lock_probe(probe: Callable[[], bool] | None) -> None:
    """A callable answering "is the session locked?", polled every 2 s while an orb wants frames."""
    frame_clock().set_probe(probe)


def note_activity() -> None:
    """Input or a state change outside the orbs: deep idle ends and its 2 minutes start again."""
    frame_clock().note_activity()


class _InputWatch(QObject):
    """The application-wide input watcher behind deep idle: only the event's type is looked at."""

    _TYPES = frozenset({QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress, QEvent.Type.Wheel,
                        QEvent.Type.MouseMove, QEvent.Type.WindowActivate, QEvent.Type.ApplicationActivate})

    def eventFilter(self, _watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if event.type() in self._TYPES:
            frame_clock().note_activity()
        return False


_input_watch: _InputWatch | None = None
_watched_targets: weakref.WeakSet[QObject] = weakref.WeakSet()


def watch_input(target: QObject | None = None) -> None:
    """Install the input watcher on ``target`` (once per target): a window's QWindow (the app passes its
    main window's handle, which sees every key, click, wheel and mouse move of that window and nothing
    else, so the watcher costs next to nothing), or, with None, the whole application."""
    global _input_watch
    from PySide6.QtWidgets import QApplication

    target = target or QApplication.instance()
    if target is None or target in _watched_targets:
        return
    if _input_watch is None or not shiboken6.isValid(_input_watch):
        _input_watch = _InputWatch()
    target.installEventFilter(_input_watch)
    _watched_targets.add(target)


def watching(target: QObject) -> bool:
    return target in _watched_targets

# --------------------------------------------------------------------------
# Shared widget behaviour
# --------------------------------------------------------------------------


class _OrbBase(QWidget):
    """Look, run conditions (hidden / minimized / locked / reduced motion) and the frame clock hook."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._base = IDLE
        self._muted = False
        self._offline = False
        self._animated = True
        self._frozen: float | None = None
        self._t_origin = time.monotonic()
        self._last_frame = 0.0
        self._watched: weakref.ref[QWidget] | None = None
        holo.motion_settings().changed.connect(self._on_motion_changed)
        frame_clock().register(self)
        self._update_name()

    # ---- look -------------------------------------------------------------

    def state(self) -> str:
        return self._base

    def is_muted(self) -> bool:
        return self._muted

    def is_offline(self) -> bool:
        return self._offline

    def look(self) -> tuple[str, bool, bool]:
        return self._base, self._muted, self._offline

    def state_word(self) -> str:
        return state_word(self._base, self._muted, self._offline)

    def set_state(self, state: str) -> None:
        """A base state; ``MUTED`` / ``OFFLINE`` mean idle with only that modifier (the style guide's tiles)."""
        if state == MUTED:
            self.set_look(IDLE, muted=True, offline=False)
        elif state == OFFLINE:
            self.set_look(IDLE, muted=False, offline=True)
        else:
            self.set_look(state if state in BASE_STATES else IDLE, muted=self._muted, offline=self._offline)

    def set_muted(self, muted: bool) -> None:
        self.set_look(self._base, muted=muted, offline=self._offline)

    def set_offline(self, offline: bool) -> None:
        self.set_look(self._base, muted=self._muted, offline=offline)

    def set_look(self, base: str, *, muted: bool = False, offline: bool = False) -> None:
        base = base if base in BASE_STATES else IDLE
        if (base, bool(muted), bool(offline)) == (self._base, self._muted, self._offline):
            return
        self._base, self._muted, self._offline = base, bool(muted), bool(offline)
        frame_clock().note_activity()      # a state change ends deep idle
        self._look_changed(self._now())
        self._update_name()
        self._sync()
        self.update()

    def _look_changed(self, now: float) -> None:
        """Retarget the transitions (subclasses)."""

    def _deep_changed(self, _deep: bool) -> None:
        """Deep idle started or ended (the frame clock tells every orb)."""
        self._sync()

    def _update_name(self) -> None:
        self.setAccessibleName(f"Jarvis, {self.state_word().lower()}")
        self.setAccessibleDescription(f"Jarvis is {self._describe()}")

    def _describe(self) -> str:
        words = {IDLE: "standing by", LISTENING: "listening", THINKING: "thinking", SPEAKING: "speaking",
                 NEEDS: "waiting for your decision", WAITING: "waiting for the AM briefing"}
        text = words.get(self._base, "standing by")
        if self._muted:
            text += ", muted"
        elif self._offline:
            text += ", on the offline voice"
        return text

    # ---- time ---------------------------------------------------------------

    def _now(self) -> float:
        return self._frozen if self._frozen is not None else time.monotonic() - self._t_origin

    def set_phase(self, seconds: float | None) -> None:
        """Freeze the motion at ``seconds`` (deterministic screenshots); None resumes."""
        self._frozen = seconds
        if seconds is not None:
            self.settle()
        self._sync()
        self.update()

    def settle(self) -> None:
        """End every running transition at its target."""

    def set_animated(self, animated: bool) -> None:
        self._animated = animated
        self._sync()

    # ---- run conditions ------------------------------------------------------

    def frame_interval(self) -> int | None:
        """The interval this orb wants now (ms), or None when it should be still."""
        return frame_interval_ms(self._base, self._muted, frame_clock().deep())

    def _should_run(self) -> bool:
        window = self.window()
        return (self._animated and self._frozen is None and not holo.reduced_motion() and self.isVisible()
                and not (window is not None and window.isMinimized()) and self.frame_interval() is not None)

    def _sync(self) -> None:
        clock = frame_clock()
        clock.want(self, self.frame_interval() if self._should_run() else None)

    def is_animating(self) -> bool:
        clock = frame_clock()
        return clock.wants(self) and clock.running() and not clock.locked()

    def _request_frame(self) -> None:
        self._last_frame = time.monotonic()
        self.update()

    def _on_motion_changed(self, _reduced: bool) -> None:
        if shiboken6.isValid(self):
            self.settle()
            self._sync()
            self.update()

    def _watch_window(self) -> None:
        # Minimizing does not hide child widgets: listen to the window's state changes.
        window = self.window()
        if window is not self and (self._watched is None or self._watched() is not window):
            window.installEventFilter(self)
            self._watched = weakref.ref(window)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if event.type() == QEvent.Type.WindowStateChange and watched is self.window():
            QTimer.singleShot(0, self._sync_if_valid)
        return False

    def _sync_if_valid(self) -> None:
        if shiboken6.isValid(self):
            self._sync()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._watch_window()
        if not event.spontaneous():
            frame_clock().note_activity()    # an orb coming into view (a screen opened) is not idle
        self._sync()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self._sync()

    def changeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self._sync()

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        frame_clock().want(self, None)
        super().closeEvent(event)

# --------------------------------------------------------------------------
# The orb
# --------------------------------------------------------------------------


_COLORS: dict[tuple[int, int], QColor] = {}
_PENS: dict[tuple[int, int, float, bool], QPen] = {}


def _rgba(ink: QColor, a: float) -> QColor:
    """``ink`` at alpha ``a``, cached by (rgb, alpha / 1024) so steady frames reuse the objects."""
    key = (ink.rgb(), max(0, min(1024, round(a * 1024))))
    color = _COLORS.get(key)
    if color is None:
        if len(_COLORS) > 4096:
            _COLORS.clear()
        color = QColor(ink)
        color.setAlphaF(key[1] / 1024)
        _COLORS[key] = color
    return color


def _pen(color: QColor, width: float, cosmetic: bool) -> QPen:
    key = (color.rgba(), color.alpha(), round(width, 3), cosmetic)
    pen = _PENS.get(key)
    if pen is None:
        if len(_PENS) > 2048:
            _PENS.clear()
        pen = QPen(color, width)
        pen.setCosmetic(cosmetic)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        _PENS[key] = pen
    return pen


HAIRLINE_PX = 1.5        # strokes up to this many device pixels are drawn as one 1 px cosmetic line
STRAND_MAX_PX = 4.0      # strokes up to this wide are drawn as round(width) parallel 1 px lines


def _line_pen(color: QColor, width: float, pixel: float) -> QPen:
    """A pen for a ``width``-unit line: a 1 px cosmetic hairline carrying the line's coverage in its
    alpha when it is thin (Qt's fast antialiased path), else a normal pen."""
    px = width * pixel
    if px <= HAIRLINE_PX:
        return _pen(_rgba(color, min(1.0, color.alphaF() * px)), 1.0, True)
    return _pen(color, width, False)


def _paint_elem(painter: QPainter, elem: _Elem, color: Callable[[float], QColor], opacity: float,
                pixel: float) -> None:
    """One ring element at group ``opacity`` (folded into the colour: no painter opacity)."""
    if elem.lines is not None:              # fine marks: hairlines carrying the mark's width
        painter.setPen(_pen(color(min(1.0, elem.alpha * opacity * elem.mark * pixel)), 1.0, True))
        painter.drawLines(elem.lines)
    elif elem.centre is not None and elem.width * pixel <= STRAND_MAX_PX:
        # Qt's antialiased cosmetic lines are several times cheaper than filling a thin outline:
        # a stroke of w device pixels becomes round(w) concentric 1 px lines carrying its coverage.
        px = elem.width * pixel
        n = 1 if px <= HAIRLINE_PX else round(px)
        painter.setPen(_pen(color(min(1.0, elem.alpha * opacity * px / n)), 1.0, True))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if n == 1:
            painter.drawPath(elem.centre)
            return
        base = painter.transform()
        radius = elem.centre.boundingRect().width() / 2
        for i in range(n):
            k = (radius + (i - (n - 1) / 2) * elem.width / n) / radius
            painter.scale(k, k)
            painter.drawPath(elem.centre)
            painter.setTransform(base)
    elif elem.path is not None:
        if not elem.aa:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillPath(elem.path, color(elem.alpha * opacity))
        if not elem.aa:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)


@dataclass
class OrbFrame:
    """Everything one frame paints (computed by ``OrbWidget.sample``)."""

    t: float
    halo_level: float
    halo_breath: float
    halo_steady: bool
    frame_steady: bool
    angles: tuple[float, ...]
    scales: tuple[float, ...]
    opacities: tuple[float, ...]
    s7: float
    halo: float
    ink: QColor
    frame: float
    amp_op: float
    amp_scale: float
    sweep: float
    sweep_angle: float
    crown_op: float
    bars: tuple[float, float, float]
    core_op: float
    core_scale: float
    core_opacity: float
    glow: float
    level: float
    voice: float
    ripples: tuple[tuple[float, float], ...]
    scan: float
    scan_angle: float
    alert: float
    arc: float
    dot: float
    slash: float
    core_looks: tuple[tuple[str, float], ...]
    halos: tuple[tuple[tuple[str, float, float], float], ...]
    dashed: bool
    compact: bool
    fine: bool


class OrbWidget(_OrbBase):
    """The hero (320) / compact (120) orb; see the module docstring."""

    auto_degrade = True

    def __init__(self, px: int = HERO, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._px = px
        self._ground: Callable[[QPainter, QWidget], None] | None = None
        self._degraded = False
        self._paint_ms: deque[float] = deque(maxlen=60)
        self._backdrops: OrderedDict[tuple, QPixmap] = OrderedDict()
        self._steady = False
        self._built = False
        self._layer: QPixmap | None = None          # the ring layer (two-rate rendering)
        self._layer_key: tuple | None = None
        self._layer_at = 0.0
        self._layer_builds = 0
        self._level_target: float | None = None
        self._level_at = -1.0
        self._env = 0.0
        self._last_advance: float | None = None
        self._angles = [0.0] * 6
        self._sweep_angle = 0.0
        self._scan_angle = 0.0
        self._listen_since = 0.0
        self._listen_stop: float | None = None
        self._alert_since = -10.0
        self._crown_prev = CROWN_REST
        self._core_prev = "breath"
        targets = _targets(IDLE, False, False)
        self._targets = targets
        self._tw: dict[str, _Tween] = {}
        for i in range(6):
            self._tw[f"s{i}"] = _Tween(targets.s[i])
            self._tw[f"o{i}"] = _Tween(targets.o[i])
        for name in ("s7", "halo", "frame", "amp_op", "sweep", "crown_op", "core_op", "glow", "scan", "alert",
                     "slash", "slow", "boost", "amp", "lit"):
            value = getattr(targets, name) if name != "lit" else float(targets.lit)
            self._tw[name] = _Tween(value)
        self._tw["crown_mix"] = _Tween(1.0)
        self._tw["core_mix"] = _Tween(1.0)
        self._tw["voice"] = _Tween(0.0)
        self._tw["pace"] = _Tween(DEEP_PACE if frame_clock().deep() else 1.0)
        self._lag = 0.0                     # seconds the motion fell behind the clock in deep idle
        self._dim = 1.0                     # quiet hours: the idle orb at 70 % (set_dim)
        self._ink = _Fade(targets.ink)
        self._halo_look = _Fade((targets.halo_color, targets.halo_a, targets.halo_b))
        self._core_look = _Fade(targets.core)
        self.set_orb_size(px)

    # ---- API --------------------------------------------------------------------

    def orb_size(self) -> int:
        return self._px

    def set_orb_size(self, px: int) -> None:
        self._px = int(px)
        side = widget_side(self._px)
        self.setFixedSize(side, side)
        self._backdrops.clear()
        self._layer = None
        self._paint_ms.clear()
        self._degraded = False
        self.update()

    def orb_rect(self) -> QRectF:
        """The orb's own box (``px`` square) inside the widget."""
        side = self.width()
        off = (side - self._px) / 2
        return QRectF(off, off, self._px, self._px)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        side = widget_side(self._px)
        return QSize(side, side)

    def detail(self) -> str:
        """``"full"`` (hero, all 18 layers), ``"reduced"`` (hero without the fine layers, after slow
        paints) or ``"compact"`` (under 200 px)."""
        if self._compact():
            return "compact"
        return "reduced" if self._degraded else "full"

    def is_degraded(self) -> bool:
        return self._degraded

    def paint_times(self) -> list[float]:
        return list(self._paint_ms)

    def ring_layer_builds(self) -> int:
        """How often the ring layer was built (two-rate rendering; tests and measurements)."""
        return self._layer_builds

    def set_ground(self, painter_fn: Callable[[QPainter, QWidget], None] | None) -> None:
        """``painter_fn(painter, widget)`` paints the ground under the orb; the orb then paints opaquely."""
        self._ground = painter_fn
        self._backdrops.clear()
        self._layer = None
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, painter_fn is not None)
        self.update()

    def set_dim(self, level: float) -> None:
        """Draw the whole orb at ``level`` (0..1) of its strength over the ground: quiet hours dim the idle
        orb to 0.7 (HOLOGRAM_SPEC 16.7); 1 is the orb as it is."""
        level = max(0.0, min(1.0, float(level)))
        if level != self._dim:
            self._dim = level
            self.update()

    def dim(self) -> float:
        return self._dim

    def set_level(self, level: float | None) -> None:
        """The voice envelope 0..1 now (about 30 Hz while speaking); None returns to the synthetic curve."""
        if level is None:
            self._level_target = None
            self._level_at = -1.0
            return
        self._level_target = max(0.0, min(1.0, float(level)))
        self._level_at = time.monotonic()

    def level(self) -> float:
        """The smoothed envelope the crown and core use (synthetic when no voice data is fresh)."""
        return self._env

    def settle(self) -> None:
        for tween in self._tw.values():
            tween.settle()
        self._ink.settle()
        self._halo_look.settle()
        self._core_look.settle()
        self._crown_prev = self._targets.crown
        self._core_prev = self._targets.core_anim
        self._alert_since = -10.0
        if not self._targets.ripple:
            self._listen_stop = -10.0
        self._last_advance = None

    # ---- transitions -----------------------------------------------------------------

    def _compact(self) -> bool:
        return self._px < COMPACT_BELOW

    def _look_changed(self, now: float) -> None:
        old, new = self._targets, _targets(self._base, self._muted, self._offline)
        self._targets = new
        tw = self._tw
        for i in range(6):
            tw[f"s{i}"].to(new.s[i], now, 0.8, _STD)
            tw[f"o{i}"].to(new.o[i], now, 0.6, _EASE)
        tw["s7"].to(new.s7, now, 0.8, _STD)
        tw["halo"].to(new.halo, now, 0.8, _EASE)
        tw["frame"].to(new.frame, now, 0.6, _EASE)
        tw["amp_op"].to(new.amp_op, now, 0.5, _EASE)
        tw["amp"].to(new.amp, now, 0.5, _EASE)
        tw["sweep"].to(new.sweep, now, 0.45, _EASE)
        tw["crown_op"].to(new.crown_op, now, 0.4, _EASE)
        tw["core_op"].to(new.core_op, now, 0.6, _EASE)
        tw["glow"].to(new.glow, now, 0.6, _EASE)
        tw["scan"].to(new.scan, now, 0.45, _EASE)
        tw["slash"].to(new.slash, now, 0.6, _EASE)
        tw["slow"].to(new.slow, now, 0.8, _STD)
        tw["lit"].to(float(new.lit), now, 0.6, _EASE)
        boost = tw["boost"]
        boost.to(new.boost, now, 0.6 if new.boost > boost.value(now) else 0.9, _IN_OUT)
        if new.alert and not old.alert:
            self._alert_since = now
            tw["alert"].to(1.0, now, 0.0)
            tw["alert"].settle()
        elif not new.alert and old.alert:
            tw["alert"].to(0.0, now, 0.4, _EASE)
        if new.ripple and not old.ripple:
            self._listen_since, self._listen_stop = now, None
        elif old.ripple and not new.ripple:
            self._listen_stop = now
        if new.crown != old.crown:
            self._crown_prev = old.crown
            tw["crown_mix"] = _Tween(0.0)
            tw["crown_mix"].to(1.0, now, 0.4, _EASE)
        if new.core_anim != old.core_anim:
            self._core_prev = old.core_anim
            tw["core_mix"] = _Tween(0.0)
            tw["core_mix"].to(1.0, now, 0.4, _EASE)
        self._ink.to(new.ink, now, 0.6)
        self._halo_look.to((new.halo_color, new.halo_a, new.halo_b), now, 0.6)
        self._core_look.to(new.core, now, 0.6)
        if holo.reduced_motion() or self._frozen is not None:
            self.settle()

    def _deep_changed(self, deep: bool) -> None:
        now = self._now()
        pace = self._tw["pace"]
        pace.to(DEEP_PACE if deep else 1.0, now, DEEP_EASE_S if deep else WAKE_EASE_S, _STD)
        if holo.reduced_motion() or self._frozen is not None:
            pace.settle()
        self._sync()
        self.update()

    def pace(self) -> float:
        """The motion's speed now: 1.0, or DEEP_PACE in deep idle (eased between them)."""
        return self._tw["pace"].value(self._now())

    def frame_interval(self) -> int | None:
        ms = frame_interval_ms(self._base, self._muted, frame_clock().deep())
        now = self._now()
        if any(not tween.done(now) for tween in self._tw.values()) or not self._ink.done(now):
            ms = min(ms, FRAME_ACTIVE_MS)      # transitions run smoothly, then the rate drops
        return ms

    # ---- frame values ----------------------------------------------------------------------

    def _advance(self, now: float) -> None:
        last = self._last_advance
        self._last_advance = now
        dt = 0.0 if last is None else max(0.0, min(MAX_STEP_S, now - last))
        slow, boost = self._tw["slow"].value(now), self._tw["boost"].value(now)
        pace = self._tw["pace"].value(now)
        if self._frozen is not None:            # a still: the angles a constant look reaches at t
            for i, (direction, period, bdir, bperiod) in enumerate(_SPINS):
                speed = direction * 360.0 / (period * slow) + boost * bdir * 360.0 / bperiod
                self._angles[i] = (speed * now) % 360.0
            self._sweep_angle = (now * 288.0) % 360.0
            self._scan_angle = (now * 60.0) % 360.0
        elif not holo.reduced_motion():
            # Deep idle slows every motion (pace < 1): the rings turn slower and the breathing curves
            # run on a clock that falls behind by ``_lag``, so nothing jumps when the pace changes.
            self._lag += (1.0 - pace) * dt
            for i, (direction, period, bdir, bperiod) in enumerate(_SPINS):
                speed = direction * 360.0 / (period * slow) + boost * bdir * 360.0 / bperiod
                self._angles[i] = (self._angles[i] + speed * pace * dt) % 360.0
            self._sweep_angle = (self._sweep_angle + 288.0 * pace * dt) % 360.0
            self._scan_angle = (self._scan_angle + 60.0 * pace * dt) % 360.0
        fresh = self._level_target is not None and time.monotonic() - self._level_at <= LEVEL_FRESH_S
        voice = self._tw["voice"]
        voice.to(1.0 if fresh else 0.0, now, 0.25, _EASE)
        target = self._level_target if fresh else synthetic_level(now)
        if self._frozen is not None:
            self._env = target
        elif dt > 0.0:
            tau = 0.04 if target > self._env else 0.12      # fast attack, slower release
            self._env += (target - self._env) * (1.0 - math.exp(-dt / tau))

    def sample(self, now: float | None = None) -> OrbFrame:
        """Advance to ``now`` (default: the orb's clock) and return the frame's values."""
        now = self._now() if now is None else now
        self._advance(now)
        tw = self._tw
        still = holo.reduced_motion()
        t = 0.0 if still else (now if self._frozen is not None else now - self._lag)
        new = self._targets
        speaking = new.crown == CROWN_SPEAK
        voice = tw["voice"].value(now) if speaking else 0.0
        level = self._env
        # crown
        mix = tw["crown_mix"].value(now)
        if still:
            bars = (3.0, 3.0, 3.0) if new.crown != CROWN_SPEAK else (8.0, 8.0, 8.0)
        else:
            cur = crown_lengths(new.crown, t)
            if speaking and voice > 0.0:
                bands = tuple(1.0 + 0.18 * math.sin(2 * math.pi * t / p) for p in _CROWN_PERIODS)
                live = tuple(2.0 + 16.0 * level * b for b in bands)
                cur = tuple(c + (v - c) * voice for c, v in zip(cur, live))
            if mix < 1.0:
                prev = crown_lengths(self._crown_prev, t)
                cur = tuple(p + (c - p) * mix for p, c in zip(prev, cur))
            bars = cur  # type: ignore[assignment]
        # core
        if still:
            core_scale, core_opacity = 1.0, 1.0
        else:
            core_scale, core_opacity = core_curve(new.core_anim, t)
            if new.core_anim == "speak" and voice > 0.0:
                core_scale += (1.0 + 0.18 * level - core_scale) * voice
            cmix = tw["core_mix"].value(now)
            if cmix < 1.0:
                ps, po = core_curve(self._core_prev, t)
                core_scale = ps + (core_scale - ps) * cmix
                core_opacity = po + (core_opacity - po) * cmix
        # halo breathing (only while lit), amp ring
        lit = tw["lit"].value(now)
        breath = 1.0 if still else _frames(_HALO, _cycle(t, 5.0), _IN_OUT)[0]
        halo_level = tw["halo"].value(now)
        halo_breath = 1.0 + (breath - 1.0) * lit
        halo = halo_level * halo_breath
        amp = tw["amp"].value(now)
        if still or amp <= 0.0:
            amp_scale, amp_opacity = 1.0, 1.0
        else:
            a_scale, a_op = _frames(_AMP, _cycle(t, 0.95), _IN_OUT)
            amp_scale, amp_opacity = 1.0 + (a_scale - 1.0) * amp, 1.0 + (a_op - 1.0) * amp
        # ripples
        ripples: list[tuple[float, float]] = []
        if not still:
            for delay in (0.0, 0.5, 1.0):
                local = now - self._listen_since - delay
                if local < 0.0 or (self._listen_stop is None and not new.ripple):
                    continue
                n = math.floor(local / 1.5)
                if self._listen_stop is not None and self._listen_since + delay + n * 1.5 >= self._listen_stop:
                    continue
                p = (local - n * 1.5) / 1.5
                scale = 1.25 + (0.6 - 1.25) * _RIPPLE_EASE(p)
                op = 0.8 * _RIPPLE_EASE(p / 0.25) if p < 0.25 else 0.8 * (1.0 - _RIPPLE_EASE((p - 0.25) / 0.75))
                ripples.append((scale, op))
        # needs arc
        alert = tw["alert"].value(now)
        arc = 1.0 if still else _STD(max(0.0, min(1.0, now - self._alert_since)))
        dot = 1.0 if still else _frames(_DOT, _cycle(t, 1.4), _IN_OUT)[0]
        ink_mix = self._ink.mix(now)
        ink = holo.mix(self._ink.prev, self._ink.cur, ink_mix)
        core_mix = self._core_look.mix(now)
        looks = ((self._core_look.cur, core_mix),) if core_mix >= 1.0 else (
            (self._core_look.prev, 1.0 - core_mix), (self._core_look.cur, core_mix))
        halo_mix = self._halo_look.mix(now)
        halos = ((self._halo_look.cur, 1.0),) if halo_mix >= 1.0 else (
            (self._halo_look.prev, 1.0 - halo_mix), (self._halo_look.cur, halo_mix))
        return OrbFrame(
            t=now, halo_level=halo_level, halo_breath=halo_breath,
            halo_steady=halo_mix >= 1.0 and tw["halo"].done(now) and tw["lit"].done(now),
            frame_steady=tw["frame"].done(now) and ink_mix >= 1.0, angles=tuple(self._angles),
            scales=tuple(tw[f"s{i}"].value(now) for i in range(6)),
            opacities=tuple(tw[f"o{i}"].value(now) for i in range(6)), s7=tw["s7"].value(now), halo=halo,
            ink=ink, frame=tw["frame"].value(now), amp_op=tw["amp_op"].value(now) * amp_opacity,
            amp_scale=amp_scale, sweep=tw["sweep"].value(now), sweep_angle=self._sweep_angle,
            crown_op=tw["crown_op"].value(now), bars=bars, core_op=tw["core_op"].value(now),
            core_scale=core_scale, core_opacity=core_opacity, glow=tw["glow"].value(now), level=level,
            voice=voice, ripples=tuple(ripples), scan=tw["scan"].value(now), scan_angle=self._scan_angle,
            alert=alert, arc=arc, dot=dot, slash=tw["slash"].value(now), core_looks=looks, halos=halos,
            dashed=new.dashed, compact=self._compact(), fine=not (self._compact() or self._degraded),
        )

    # ---- painting ------------------------------------------------------------------------------

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        started = time.perf_counter()
        self._steady = False
        self._built = False
        frame = self.sample()
        painter = QPainter(self)
        try:
            self._paint(painter, frame)
            if self._dim < 1.0:
                self._paint_dim(painter)
        finally:
            painter.end()
        if self.is_animating():
            if self._steady and not self._built:
                self._paint_ms.append((time.perf_counter() - started) * 1000.0)
            self._maybe_degrade()
            self._sync()          # the rate follows the look (transitions run at 30 fps, then drop)

    def _paint_dim(self, painter: QPainter) -> None:
        """The orb at ``dim`` of its strength: the ground (or the void) laid over it at the rest, so where no
        orb is drawn nothing changes."""
        painter.resetTransform()
        painter.setOpacity(1.0 - self._dim)
        if self._ground is not None:
            self._ground(painter, self)
        else:
            painter.fillRect(self.rect(), QColor(holo.VOID))
        painter.setOpacity(1.0)

    @staticmethod
    def _paint_frame(painter: QPainter, f: OrbFrame, lod: _Lod, color: Callable[[float], QColor],
                     pixel: float) -> None:
        painter.setOpacity(1.0)
        painter.fillPath(lod.ticks, color(0.75 * f.frame))
        _paint_elem(painter, lod.track_dashed if f.dashed else lod.track, color, f.frame, pixel)

    def _paint_backdrop(self, painter: QPainter, f: OrbFrame, dpr: float) -> None:
        """The ground slice, the halo and (when steady) the frame + track. At rest they come from one
        cached pixmap per breath step (an opaque copy when there is a ground); during a transition
        they are painted directly."""
        if f.halo_steady:
            self._steady = True
            painter.drawPixmap(QPoint(0, 0), self._steady_backdrop(f, dpr))
            return
        if self._ground is not None:
            self._ground(painter, self)
        self._paint_halos(painter, f.halos, f.halo, dpr)
        painter.setOpacity(1.0)

    def _steady_backdrop(self, f: OrbFrame, dpr: float) -> QPixmap:
        """The cached ground + halo (+ frame and track when steady) for this breath step."""
        look = f.halos[-1][0]
        step = round((max(0.7, f.halo_breath) - 0.7) / 0.3 * (BREATH_STEPS - 1))
        level = f.halo_level * (0.7 + 0.3 * step / (BREATH_STEPS - 1))
        frame = (f.frame, f.ink.rgba(), f.dashed, f.compact) if f.frame_steady else None
        return self._backdrop(look, round(level, 4), dpr, frame)

    def _layered(self, f: OrbFrame) -> bool:
        """Two-rate rendering applies: an active look (30 fps) at rest whose rings turn at idle speed."""
        if self._frozen is not None or not (f.halo_steady and f.frame_steady) or f.sweep > 0.003:
            return False
        if frame_interval_ms(self._base, self._muted, frame_clock().deep()) >= RING_LAYER_MS:
            return False                      # idle rates: every frame turns the rings anyway
        tw, now = self._tw, f.t
        if tw["boost"].value(now) > 0.0 or not (tw["boost"].done(now) and tw["slow"].done(now)):
            return False
        return all(tw[f"s{i}"].done(now) and tw[f"o{i}"].done(now) for i in range(6))

    def _ring_layer(self, f: OrbFrame, lod: _Lod, dpr: float) -> QPixmap:
        """The backdrop and R1-R6 in one pixmap, rebuilt every ``RING_LAYER_MS`` (or when it changes)."""
        backdrop = self._steady_backdrop(f, dpr)
        key = (backdrop.cacheKey(), f.ink.rgba(), f.scales, f.opacities, f.compact, f.fine)
        layer = self._layer
        if layer is not None and key == self._layer_key and 0.0 <= f.t - self._layer_at < RING_LAYER_MS / 1000.0:
            return layer
        if layer is None or layer.size() != backdrop.size():
            layer = QPixmap(backdrop.size())
        layer.setDevicePixelRatio(backdrop.devicePixelRatio())
        layer.fill(_FILL_OPAQUE if self._ground is not None else Qt.GlobalColor.transparent)
        painter = QPainter(layer)
        try:
            painter.drawPixmap(QPoint(0, 0), backdrop)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.translate(self.orb_rect().center())
            k = self._px / 360.0
            painter.scale(k, k)
            ink = f.ink
            self._paint_rings(painter, f, lod, lambda a: _rgba(ink, a), k * dpr, with_amp=False)
        finally:
            painter.end()
        self._layer, self._layer_key, self._layer_at = layer, key, f.t
        self._layer_builds += 1
        return layer

    def _paint_halos(self, painter: QPainter, halos: tuple, level: float, dpr: float) -> None:
        center = QRectF(self.rect()).center()
        for (color, a, b), weight in halos:
            if level * weight > 0.003:
                pixmap = _halo_pixmap(color, a, b, self._px, dpr)
                side = pixmap.width() / pixmap.devicePixelRatio()
                painter.setOpacity(level * weight)
                painter.drawPixmap(QPointF(center.x() - side / 2, center.y() - side / 2), pixmap)

    def _backdrop_key(self) -> tuple:
        if self._ground is None:
            return (None,)
        window = self.window()
        return (id(self._ground), id(window), window.width(), window.height(),
                self.mapTo(window, QPoint(0, 0)).toTuple())

    def _backdrop(self, look: tuple[str, float, float], level: float, dpr: float,
                  frame: tuple[float, int, bool, bool] | None = None) -> QPixmap:
        key = (self._backdrop_key(), look, self._px, round(dpr, 3), level, frame)
        pixmap = self._backdrops.get(key)
        if pixmap is not None:
            self._backdrops.move_to_end(key)
            return pixmap
        self._built = True
        side = self.width()
        pixmap = QPixmap(round(side * dpr), round(side * dpr))
        pixmap.setDevicePixelRatio(dpr)
        # Over a ground the copy is opaque (the ground covers it): a plain copy to the screen.
        pixmap.fill(_FILL_OPAQUE if self._ground is not None else Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            if self._ground is not None:
                self._ground(painter, self)
            self._paint_halos(painter, ((look, 1.0),), level, dpr)
            if frame is not None and frame[0] > 0.0:
                opacity, rgba, dashed, compact = frame
                ink = QColor.fromRgba(rgba)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.translate(QRectF(self.rect()).center())
                k = self._px / 360.0
                painter.scale(k, k)

                def color(a: float) -> QColor:
                    c = QColor(ink)
                    c.setAlphaF(a)
                    return c

                lod = _lod(compact, False)            # the frame and track have no fine layers
                painter.setOpacity(1.0)
                painter.fillPath(lod.ticks, color(0.75 * opacity))
                _paint_elem(painter, lod.track_dashed if dashed else lod.track, color, opacity, k * dpr)
        finally:
            painter.end()
        self._backdrops[key] = pixmap
        while len(self._backdrops) > BREATH_STEPS:
            self._backdrops.popitem(last=False)
        return pixmap

    def backdrop_count(self) -> int:
        """Cached backdrops (at most ``BREATH_STEPS``)."""
        return len(self._backdrops)

    def _maybe_degrade(self) -> None:
        if (self.auto_degrade and not self._degraded and self._px >= COMPACT_BELOW
                and len(self._paint_ms) == self._paint_ms.maxlen):
            average = sum(self._paint_ms) / len(self._paint_ms)
            if average > DEGRADE_MS:
                self._degraded = True
                self._backdrops.clear()
                self._layer = None
                logger.info("Orb: compact layers (paint %.1f ms)", average)

    def _paint(self, painter: QPainter, f: OrbFrame) -> None:
        lod = _lod(f.compact, f.fine)
        rect = self.orb_rect()
        center = rect.center()
        k = self._px / 360.0
        dpr = self.devicePixelRatioF()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        layered = self._layered(f)
        if layered:
            # 0-10 without the amp ring: the cached layer of ground, halo, frame, track and R1-R6
            self._steady = True
            painter.drawPixmap(QPoint(0, 0), self._ring_layer(f, lod, dpr))
        else:
            # 0-1 ground and halo (Qt skips the 3 % scale breathing)
            self._paint_backdrop(painter, f, dpr)
        painter.translate(center)
        painter.scale(k, k)
        base = painter.transform()
        ink = f.ink
        pixel = k * dpr                        # device pixels per unit

        def color(a: float) -> QColor:
            return _rgba(ink, a)

        if layered:
            # The amp ring (r 153.5 x 1..1.05) overlaps only R1, which it covers in DOM order too.
            self._paint_amp(painter, f, lod, color, pixel)
            painter.setTransform(base)
        else:
            # 2 frame + track (in the cached backdrop while steady)
            if f.frame > 0.0 and not f.frame_steady:
                self._paint_frame(painter, f, lod, color, pixel)
            self._paint_rings(painter, f, lod, color, pixel, with_amp=True)
        # 11 crown
        if f.crown_op > 0.003:
            painter.setOpacity(f.crown_op)
            if f.s7 != 1.0:
                painter.scale(f.s7, f.s7)
            pen = QPen(ink, 2 * math.pi * 49 * lod.bar_deg / 360.0)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            painter.setPen(pen)
            lines = []
            for j in range(24):
                length = f.bars[j % 3]
                a = math.radians(lod.bar_deg / 2 + 15.0 * j)
                ca, sa = math.cos(a), math.sin(a)
                r0, r1 = 49.0 - length / 2, 49.0 + length / 2
                lines.append(QLineF(r0 * ca, r0 * sa, r1 * ca, r1 * sa))
            painter.drawLines(lines)
            painter.setTransform(base)
        # 12-14 core glow, core, core ring
        if f.core_op > 0.003:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.scale(f.s7, f.s7)
            inner = painter.transform()
            painter.scale(f.core_scale, f.core_scale)
            if f.glow > 0.003:
                painter.setOpacity(f.core_op * f.core_opacity * f.glow)
                glow = _core_pixmap("glow", self._px, dpr)
                painter.drawPixmap(_GLOW_BOX, glow, QRectF(glow.rect()))
            for look, weight in f.core_looks:
                if weight <= 0.003:
                    continue
                painter.setOpacity(f.core_op * f.core_opacity * weight)
                if look == CORE_HOLLOW:
                    painter.setPen(QPen(QColor(holo.STEEL), lod.core_edge_w))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    r = 27.0 - lod.core_edge_w / 2
                    painter.drawEllipse(QPointF(0, 0), r, r)
                    painter.setPen(Qt.PenStyle.NoPen)
                else:
                    core = _core_pixmap(look, self._px, dpr)
                    painter.drawPixmap(_CORE_BOX, core, QRectF(core.rect()))
            painter.setTransform(inner)
            painter.setOpacity(f.core_op)
            ring_color = holo.mix(holo.alpha(holo.HOLO, 0.33), holo.alpha(holo.STEEL, 0.4), f.slash)
            painter.setPen(_line_pen(ring_color, lod.core_ring_w, pixel * f.s7))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            r = 37.8 - lod.core_ring_w / 2
            painter.drawEllipse(QPointF(0, 0), r, r)
            painter.setTransform(base)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # 15 ripples
        for scale, op in f.ripples:
            if op <= 0.003:
                continue
            painter.setOpacity(1.0)
            painter.setPen(_line_pen(color(op), lod.h1 * scale, pixel))
            r = 130.0 * scale
            painter.drawEllipse(QPointF(0, 0), r, r)
        # 16 waiting scanner
        if f.scan > 0.003:
            painter.setOpacity(f.scan)
            pen = QPen(ink, lod.h3)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.rotate(f.scan_angle)
            painter.drawArc(QRectF(-160, -160, 320, 320), 0, -20 * 16)
            painter.setTransform(base)
        # 17 needs arc + dot (Qt angles run counter-clockwise: SVG 27 deg clockwise = -27)
        if f.alert > 0.003:
            painter.setOpacity(f.alert * f.arc)
            pen = QPen(QColor(holo.AMBER), lod.h3)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            span = 36.0 * f.arc
            if span > 0.2:
                painter.drawArc(QRectF(-153, -153, 306, 306), -27 * 16, round(-span * 16))
            painter.setOpacity(f.alert * f.dot)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(holo.AMBER))
            painter.drawEllipse(QPointF(108.2, 108.2), lod.dot_r, lod.dot_r)
            painter.setBrush(Qt.BrushStyle.NoBrush)
        # 18 mute slash
        if f.slash > 0.003:
            painter.setOpacity(f.slash)
            cut = QPen(QColor(holo.VOID), lod.cut)
            cut.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(cut)
            painter.drawLine(QPointF(-34, 34), QPointF(34, -34))
            over = QPen(ink, lod.h3)
            over.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(over)
            painter.drawLine(QPointF(-34, 34), QPointF(34, -34))
        painter.setOpacity(1.0)

    @staticmethod
    def _paint_rings(painter: QPainter, f: OrbFrame, lod: _Lod, color: Callable[[float], QColor], pixel: float,
                     *, with_amp: bool) -> None:
        """R1-R6 (with the amp ring and the sweep in DOM order when ``with_amp``) around the origin."""
        base = painter.transform()
        # 3-10 rings (R1, R2, amp, R3, sweep, R4, R5, R6 in DOM order)
        for i in range(6):
            if i == 2 and with_amp:
                OrbWidget._paint_amp(painter, f, lod, color, pixel)
                painter.setTransform(base)
            if i == 3 and with_amp and f.sweep > 0.003:
                painter.setOpacity(1.0)
                painter.rotate(f.sweep_angle)
                for path, a, aa in lod.sweep:
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing, aa)
                    painter.fillPath(path, color(a * f.sweep))
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setTransform(base)
            o = f.opacities[i]
            if o <= 0.003:
                continue
            painter.setOpacity(1.0)            # the group opacity goes into the colours (a faster fill)
            s = f.scales[i]
            if s != 1.0:
                painter.scale(s, s)
            painter.rotate(f.angles[i])
            for elem in lod.rings[i]:
                _paint_elem(painter, elem, color, o, pixel)
            if i == 3:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color(o))
                painter.drawEllipse(lod.r4_dot, lod.dot_r, lod.dot_r)
            painter.setTransform(base)

    @staticmethod
    def _paint_amp(painter: QPainter, f: OrbFrame, lod: _Lod, color: Callable[[float], QColor],
                   pixel: float) -> None:
        if f.amp_op <= 0.003:
            return
        painter.setOpacity(1.0)
        if f.amp_scale != 1.0:
            painter.scale(f.amp_scale, f.amp_scale)
        painter.setPen(_line_pen(color(0.6 * f.amp_op), lod.h1, pixel * f.amp_scale))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(0, 0), 153.5, 153.5)

# --------------------------------------------------------------------------
# The mini orb (36 px glyph)
# --------------------------------------------------------------------------


@lru_cache(maxsize=2)
def _mini_paths() -> tuple[QPainterPath, QPainterPath, QPainterPath]:
    """(solid track, dashed track, segmented ring) in 36-unit coordinates around the centre."""
    return _ring(16.5, 1.0, None), _ring(16.5, 1.0, (4, 4)), _ring(11.0, 3.2, (30, 6))


def paint_mini_orb(painter: QPainter, center: QPointF, base: str, muted: bool = False, offline: bool = False, *,
                   t: float = 0.0, ring_angle: float = 0.0, ring_color: str | QColor = holo.DOCK,
                   scale: float = 1.0) -> None:
    """The mini orb (spec 12.5): outer track, one segmented ring, the core, the slash and the amber dot."""
    ink = QColor(holo.STEEL if muted else holo.HOLO)
    solid, dashed, ring = _mini_paths()
    painter.save()
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.translate(center)
        if scale != 1.0:
            painter.scale(scale, scale)
        painter.fillPath(dashed if offline else solid, holo.alpha(ink, 0.45))
        painter.save()
        painter.setOpacity(painter.opacity() * (0.4 if base == WAITING else 0.85))
        painter.rotate(ring_angle)
        painter.fillPath(ring, ink)
        painter.restore()
        if base == SPEAKING:
            pulse = _frames(_SPEAK, _cycle(t, 1.05), _linear)[0]
        elif base == LISTENING:
            pulse = _frames(_LISTEN, _cycle(t, 1.1), _IN_OUT)[0]
        else:
            pulse = 1.0
        r = 4.6 * pulse
        if muted:
            painter.setPen(QPen(ink, 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ink)
        painter.drawEllipse(QPointF(0, 0), r, r)
        if muted:
            pen = QPen(ink, 1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(QPointF(-6, 6), QPointF(6, -6))
        if base == NEEDS:
            painter.setPen(QPen(QColor(ring_color), 1.5))
            painter.setBrush(QColor(holo.AMBER))
            painter.drawEllipse(QPointF(11.5, 11.5), 3.6, 3.6)
    finally:
        painter.restore()


class MiniOrb(_OrbBase):
    """The 36 px orb glyph; still at rest, 15 fps only while listening, thinking or speaking."""

    def __init__(self, parent: QWidget | None = None, *, ring_color: str | QColor = holo.DOCK) -> None:
        super().__init__(parent)
        self._ring_angle = 0.0
        self._last_advance: float | None = None
        self._ring_color = ring_color
        self.setFixedSize(MINI, MINI)

    def frame_interval(self) -> int | None:
        return FRAME_MINI_MS if self._base in ACTIVE_STATES else None

    def settle(self) -> None:
        self._last_advance = None

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        now = self._now()
        still = holo.reduced_motion()
        if self._base == THINKING and not still:
            if self._frozen is not None:
                self._ring_angle = (now * 360.0 / 2.2) % 360.0
            elif self._last_advance is not None:
                step = 360.0 / 2.2 * max(0.0, min(0.2, now - self._last_advance))
                self._ring_angle = (self._ring_angle + step) % 360.0
        self._last_advance = now
        painter = QPainter(self)
        try:
            paint_mini_orb(painter, QPointF(self.width() / 2, self.height() / 2), self._base, self._muted,
                           self._offline, t=0.25 if still else now, ring_angle=self._ring_angle,
                           ring_color=self._ring_color)
        finally:
            painter.end()


def ground_painter(cache: holo.GroundCache) -> Callable[[QPainter, QWidget], None]:
    """A ``set_ground`` function painting the widget's slice of its window's cached ground."""

    def paint(painter: QPainter, widget: QWidget) -> None:
        window = widget.window()
        origin = widget.mapTo(window, widget.rect().topLeft())
        source = QRect(origin, widget.size())
        cache.paint_slice(painter, widget.rect(), source, window.size(), widget.devicePixelRatioF())

    return paint
