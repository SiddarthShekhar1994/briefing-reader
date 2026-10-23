"""Motion that makes the hologram feel alive (the approved motion prototype; HOLOGRAM_SPEC 13; ``[ui] motion``).

Everything here is a light overlay that ignores the mouse and never takes focus, runs only while it is
visible and stops when it is done: no permanent repaint, nothing waits for it (clicks, keys, the greeting).
The level comes from ``holo.motion_level()``: "lively", "calm" (slower, fewer effects) or "off" (nothing;
also while Windows' "Show animations in Windows" is off).

``ScreenTransition(stage)``
    A screen change over ``stage`` (the screens' area under the header): ``play(page)`` renders the
    incoming ``page`` once into a pixmap and paints it over the window's ground while its main panels
    (``main_panels(page)``) assemble: lively - each fades in, rises 14 px and is revealed from the top
    down (450 ms, 70 ms apart, at most 4 staggered) under one scan-line wipe that crosses the stage in
    550 ms; calm - the panels only fade in (600 ms, 90 ms apart), no wipe. The real page is underneath
    all the time: clicks go straight to it, and the first key, click or wheel turn in the window ends
    the effect at once (``finish()``), as does hiding it. ``set_ground(fn)`` paints the window's
    ground (``fn(painter, widget)``); ``is_running()``, ``elapsed()``, ``panel_rects()``, ``wipe_y()``.
``ScanSweep(parent)``
    The start-up's one scan line: a 2 px cyan line with a soft glow crossing ``parent`` top to bottom
    in 1.6 s (``play()``, ``is_running()``, ``line_y()``); it repaints only the band it crosses. Its painting is
    ``paint_scan_line(painter, y, left, right, opacity)``, which the switch's sweep paints too.
``CardFlight(parent)``
    A card leaving Home's plan or brief for Approvals (part B): ``fly(widget, target)`` paints a snapshot of
    ``widget`` (taken at once, from where it is on ``parent``) shrinking to 15 % and fading as it flies to
    ``target`` (a rect on ``parent``: the Dock's Approvals item) in ``FLIGHT_MS``
    (``cubic-bezier(.5,0,.75,0)``, the prototype's ``out``); calm: a plain fade where it is; off: nothing.
    Purely visual: the card itself, its hold, Undo and timings are untouched. ``is_running()``, ``flights()``,
    ``progress()``.
``WindowFlight(clock=time.monotonic)``
    The switch between the window and the desktop HUD (SPEC 8.2; Switch.dc.html ``.sw``), in a window of its own
    (``FLIGHT_FLAGS``: a tool window on top that never takes the focus, click-through, translucent, shown without
    activating) over the bounding rect of the Jarvis window and the HUD's reach on the window's screen - never the
    whole screen. ``to_hud(window, orb, hud, rect, screen)``: the window's content fades (200 ms) and its frame
    folds to 12 % into its orb (460 ms from 100 ms, ``cubic-bezier(.55,0,.8,.25)``), fading as it ends; the orb
    (``FlownOrb``: a snapshot) flies to the HUD's place (460 ms from 340 ms, an arc) with its dark disc and the
    switch's glow and becomes the HUD's live orb as it lands; the bar's clip opens from the orb's end (380 ms from
    720 ms) with what is in it fading in (250 ms from 860 ms) and the scan line running along the top of the work
    area; the chips drop in from 1000 ms, 80 ms apart, with their shadows and glows. ``handover`` once all of it has
    landed: the HUD's own windows show there and the flight leaves them be (only the glow and the scan line's tail
    go on); ``finished`` at 1.7 s. ``to_window(hud, orb, window, rect, screen)``: the reverse - the chips go, the bar
    folds into the orb, the orb flies home, the window's frame grows back out of it (480 ms from 620 ms,
    ``cubic-bezier(.16,1,.3,1)``); ``windowTime`` then (the window shows, under the flight) and the frame and the orb
    fade off it (320 ms); ``finished`` at 1.42 s. ``painted`` once its first frame is up (what it covers may hide);
    ``interrupted`` when something else hides it. Calm: every length 1.5 x (holo.motion_ms); motion off: it is never
    made. Pictures only (``WindowScene``, ``FlownOrb``, ``HudScene`` of ``FlightLayer`` pictures); it repaints only
    where a part moved, and its one timer runs only while it flies. ``tick()`` steps it to the clock now
    (``set_clock``: a fake clock in the tests and the harness), ``stop()`` ends it at once with nothing shown and no
    signal,
    ``after_tick(fn)`` calls ``fn`` at the next step (or the end); ``set_hud(scene)`` (the HUD's pictures anew);
    ``elapsed()`` (the timeline, lively ms), ``length()``, ``total_ms()``, ``handover_ms()``, ``direction()``,
    ``is_running()``, ``parts()``, ``state(part)``, ``orb_state()``, ``scenes()``.
``StartupPresence(play, clock=time.monotonic)``
    When the start-up plays: ``arm()`` (the app started), then ``note_shown(bool)`` as the window
    is shown, hidden or minimised: the first time it is shown after ``arm()``, and again when it is
    shown after being hidden for at least ``REPLAY_AFTER_S`` (30 minutes). ``plays()`` counts.
``main_panels(page, limit=6)``
    The widgets that assemble: the first level under ``page`` (through containers with a single
    visible child) that holds more than one visible widget, top to bottom.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Callable

import shiboken6
from PySide6.QtCore import (QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation,
                            Signal)
from PySide6.QtGui import (QBrush, QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPixmap, QRadialGradient,
                           QRegion)
from PySide6.QtWidgets import QApplication, QWidget

from . import holo

REPLAY_AFTER_S = 30 * 60
FLIGHT_MS = 600
WIPE_BAND = 120.0
_ENDING_EVENTS = (QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick,
                  QEvent.Type.Wheel)
_OUT = holo.bezier(holo.EASE_OUT)
_WIPE_EASE = holo.bezier((0.5, 0.0, 0.5, 1.0))
_IN_OUT = holo.bezier(holo.CSS_EASE_IN_OUT)
_FLIGHT = holo.bezier((0.5, 0.0, 0.75, 0.0))


def _clamp(value: float) -> float:
    return 0.0 if value <= 0.0 else 1.0 if value >= 1.0 else value


def main_panels(page: QWidget, limit: int = 6) -> list[QWidget]:
    """See the module docstring."""
    container = page
    direct = Qt.FindChildOption.FindDirectChildrenOnly
    for _ in range(8):
        children = [child for child in container.findChildren(QWidget, options=direct)
                    if child.isVisibleTo(page) and child.width() > 0 and child.height() > 0
                    and not child.isWindow()]
        if len(children) == 1:
            container = children[0]
            continue
        if not children:
            return [container]
        children.sort(key=lambda w: (w.mapTo(page, QPoint(0, 0)).y(), w.mapTo(page, QPoint(0, 0)).x()))
        return children[:limit]
    return [container]


class _Overlay(QWidget):
    """A child that covers its parent, ignores the mouse and never takes focus."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("")
        parent.installEventFilter(self)       # subclasses hide() at the end of their __init__

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if watched is self.parent() and event.type() == QEvent.Type.Resize and self.isVisible():
            self.setGeometry(self.parentWidget().rect())
        return False

    def _cover(self) -> None:
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()


class ScreenTransition(_Overlay):
    """See the module docstring."""

    _watching = False
    _anim: QVariantAnimation | None = None

    def __init__(self, stage: QWidget) -> None:
        super().__init__(stage)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._ground: Callable[[QPainter, QWidget], None] | None = None
        self._page: QWidget | None = None
        self._pixmap: QPixmap | None = None
        self._origin = QPoint()                 # the page's top-left in this overlay
        self._page_rect = QRect()               # ... and its rect there
        self._panels: list[QRectF] = []
        self._level = holo.MOTION_OFF
        self._elapsed = 0.0
        self._watching = False
        self._generation = 0
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.valueChanged.connect(self._on_tick)
        self._anim.finished.connect(self.finish)
        self.hide()                           # explicitly: never shown with its parent

    def set_ground(self, painter_fn: Callable[[QPainter, QWidget], None] | None) -> None:
        self._ground = painter_fn

    # ---- timing ------------------------------------------------------------------------------

    def _timing(self) -> tuple[float, float, float, bool]:
        """(panel length, stagger, fade of the rest, lively) in ms."""
        if self._level == holo.MOTION_CALM:
            return float(holo.CALM_FADE_MS), float(holo.CALM_STAGGER_MS), float(holo.CALM_FADE_MS), False
        return float(holo.ASSEMBLE_MS), float(holo.ASSEMBLE_STAGGER_MS), float(holo.FADE_MS), True

    def total_ms(self, level: str | None = None) -> int:
        level = self._level if level is None else level
        if level == holo.MOTION_OFF:
            return 0
        if level == holo.MOTION_CALM:
            return holo.CALM_FADE_MS + 3 * holo.CALM_STAGGER_MS
        return max(holo.WIPE_MS, holo.ASSEMBLE_MS + 3 * holo.ASSEMBLE_STAGGER_MS)

    # ---- play / finish -------------------------------------------------------------------------

    def play(self, page: QWidget) -> bool:
        """The incoming ``page`` assembles (lively / calm); False when motion is off or nothing is shown."""
        self.finish()
        level = holo.motion_level()
        stage = self.parentWidget()
        if level == holo.MOTION_OFF or stage is None or not stage.isVisible() or not page.isVisibleTo(stage):
            return False
        window = stage.window()
        if window is not None and window.isMinimized():
            return False
        self._level = level
        self._page = page
        self._elapsed = 0.0
        self._generation += 1
        self._cover()
        self._watch(True)
        total = self.total_ms()
        self._anim.setEndValue(float(total))
        self._anim.setDuration(total)
        self._anim.start()
        generation = self._generation
        QTimer.singleShot(0, lambda: self._capture(generation))   # once the incoming page is laid out
        return True

    def finish(self) -> None:
        """End at once: the real page shows."""
        self._generation += 1
        if self._anim.state() == QVariantAnimation.State.Running:
            self._anim.stop()
        self._watch(False)
        self._pixmap = None
        self._page = None
        self._panels = []
        if self.isVisible():
            self.hide()

    def is_running(self) -> bool:
        return self.isVisible() and self._anim.state() == QVariantAnimation.State.Running

    def elapsed(self) -> float:
        return self._elapsed

    def level(self) -> str:
        return self._level

    def has_snapshot(self) -> bool:
        return self._pixmap is not None

    def panel_rects(self) -> list[QRectF]:
        return list(self._panels)

    def _on_tick(self, value: Any) -> None:
        self._elapsed = float(value)
        self.update()

    def _capture(self, generation: int) -> None:
        page = self._page
        if (generation != self._generation or page is None or not shiboken6.isValid(page)
                or not self.isVisible() or not page.isVisible()):
            return
        stage = self.parentWidget()
        layout = stage.layout() if stage is not None else None
        if layout is not None:
            layout.activate()
        dpr = self.devicePixelRatioF()
        pixmap = QPixmap(max(1, round(page.width() * dpr)), max(1, round(page.height() * dpr)))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        page.render(pixmap, QPoint(), QRegion(page.rect()), QWidget.RenderFlag.DrawChildren)
        if generation != self._generation:
            return
        self._origin = page.mapTo(stage, QPoint(0, 0))
        self._page_rect = QRect(self._origin, page.size())
        self._pixmap = pixmap
        self._panels = [QRectF(QRect(w.mapTo(page, QPoint(0, 0)), w.size())) for w in main_panels(page)]
        self.update()

    # ---- the owner's input ends it -----------------------------------------------------------------

    def _watch(self, on: bool) -> None:
        app = QApplication.instance()
        if app is None or on == self._watching:
            return
        self._watching = on
        if on:
            app.installEventFilter(self)
        else:
            app.removeEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if self._watching and event.type() in _ENDING_EVENTS and isinstance(watched, QWidget):
            if watched.window() is self.window():
                QTimer.singleShot(0, self.finish)       # the event goes on to its widget untouched
            return False
        return super().eventFilter(watched, event)

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        if self._anim is not None and self._anim.state() == QVariantAnimation.State.Running:
            QTimer.singleShot(0, self.finish)

    # ---- painting --------------------------------------------------------------------------------

    def panel_state(self, index: int) -> tuple[float, float, float]:
        """(opacity, rise in px, revealed share from the top) of panel ``index`` now."""
        length, stagger, _fade, lively = self._timing()
        progress = _OUT(_clamp((self._elapsed - min(index, 3) * stagger) / length))
        if not lively:
            return progress, 0.0, 1.0
        return progress, holo.ASSEMBLE_RISE * (1.0 - progress), progress

    def wipe_y(self) -> float | None:
        """The wipe band's lower edge (lively, while it crosses), else None."""
        if self._level != holo.MOTION_LIVELY or self._elapsed >= holo.WIPE_MS:
            return None
        p = _WIPE_EASE(_clamp(self._elapsed / holo.WIPE_MS))
        return p * (self.height() + WIPE_BAND)       # from just above the stage to just below it

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        try:
            if self._ground is not None:
                self._ground(painter, self)
            else:
                painter.fillRect(self.rect(), QColor(holo.VOID))
            if self._pixmap is not None:
                self._paint_page(painter)
            self._paint_wipe(painter)
        finally:
            painter.end()

    def _paint_page(self, painter: QPainter) -> None:
        pixmap = self._pixmap
        assert pixmap is not None
        dpr = pixmap.devicePixelRatio()
        origin = QPointF(self._origin)
        _length, _stagger, fade, _lively = self._timing()
        rest = QRegion(self._page_rect)
        for rect in self._panels:
            rest -= QRegion(rect.translated(origin).toAlignedRect())
        painter.save()
        painter.setClipRegion(rest)
        painter.setOpacity(_OUT(_clamp(self._elapsed / fade)))
        painter.drawPixmap(origin, pixmap)
        painter.restore()
        for index, rect in enumerate(self._panels):
            opacity, rise, shown = self.panel_state(index)
            if opacity <= 0.0 or shown <= 0.0:
                continue
            target = rect.translated(origin.x(), origin.y() + rise)
            painter.save()
            painter.setClipRect(QRectF(target.x(), target.y(), target.width(), target.height() * shown))
            painter.setOpacity(opacity)
            source = QRectF(rect.x() * dpr, rect.y() * dpr, rect.width() * dpr, rect.height() * dpr)
            painter.drawPixmap(target, pixmap, source)
            painter.restore()

    def _paint_wipe(self, painter: QPainter) -> None:
        bottom = self.wipe_y()
        if bottom is None:
            return
        top = bottom - WIPE_BAND
        gradient = QLinearGradient(0.0, top, 0.0, bottom)
        gradient.setColorAt(0.0, holo.alpha(holo.HOLO, 0.0))
        gradient.setColorAt(0.8, holo.alpha(holo.HOLO, 0.10))
        gradient.setColorAt(0.98, holo.alpha(holo.HOLO_HI, 0.75))
        gradient.setColorAt(1.0, holo.alpha(holo.HOLO, 0.0))
        painter.fillRect(QRectF(0.0, top, self.width(), WIPE_BAND), gradient)


class ScanSweep(_Overlay):
    """See the module docstring."""

    GLOW = 12.0
    _anim: QVariantAnimation | None = None

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._progress = 0.0
        self._band = QRect()
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(holo.STARTUP_SCAN_MS)
        self._anim.valueChanged.connect(self._on_tick)
        self._anim.finished.connect(self.stop)
        self.hide()

    def play(self) -> bool:
        parent = self.parentWidget()
        if holo.motion_level() != holo.MOTION_LIVELY or parent is None or not parent.isVisible():
            return False
        self._progress = 0.0
        self._band = QRect()
        self._cover()
        self._anim.stop()
        self._anim.start()
        return True

    def stop(self) -> None:
        self._anim.stop()
        if self.isVisible():
            self.hide()

    def is_running(self) -> bool:
        return self.isVisible() and self._anim.state() == QVariantAnimation.State.Running

    def line_y(self) -> float:
        h = float(self.height())
        return -0.1 * h + _IN_OUT(self._progress) * 1.2 * h

    def line_opacity(self) -> float:
        p = self._progress
        return _clamp(p / 0.1) if p < 0.1 else _clamp((1.0 - p) / 0.1) if p > 0.9 else 1.0

    def _on_tick(self, value: Any) -> None:
        self._progress = float(value)
        y = self.line_y()
        band = QRect(0, int(y - self.GLOW) - 2, self.width(), int(2 * self.GLOW) + 6)
        self.update(band.united(self._band) if not self._band.isNull() else band)
        self._band = band

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        if self._anim is not None:
            self._anim.stop()

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        if self._anim is None or self._anim.state() == QVariantAnimation.State.Stopped:
            return
        painter = QPainter(self)
        try:
            paint_scan_line(painter, self.line_y(), 0.0, self.width(), self.line_opacity(), glow=self.GLOW)
        finally:
            painter.end()


def paint_scan_line(painter: QPainter, y: float, left: float, right: float, opacity: float = 1.0, *,
                    glow: float = ScanSweep.GLOW, thick: float = 2.0, ink: Any = None) -> None:
    """The start-up's scan line (``ScanSweep``) at ``y`` from ``left`` to ``right``: a soft HOLO_HI glow ``glow`` px
    above and below and the ``thick`` px line in HOLO_HI (or ``ink``: a colour or a brush) - the switch's sweep
    paints it too."""
    painter.setOpacity(opacity)
    band = QLinearGradient(0.0, y - glow, 0.0, y + glow)
    band.setColorAt(0.0, holo.alpha(holo.HOLO_HI, 0.0))
    band.setColorAt(0.5, holo.alpha(holo.HOLO_HI, 0.35))
    band.setColorAt(1.0, holo.alpha(holo.HOLO_HI, 0.0))
    painter.fillRect(QRectF(left, y - glow, right - left, 2 * glow), band)
    painter.fillRect(QRectF(left, y - thick / 2, right - left, thick), ink if ink is not None else QColor(holo.HOLO_HI))


class CardFlight(_Overlay):
    """See the module docstring."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._flights: list[tuple[QPixmap, QRectF, QRectF, float]] = []   # (snapshot, from, to, started)
        self._calm = False
        self._dirty = QRect()
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._on_tick)
        self.hide()

    def fly(self, widget: QWidget, target: QRectF | QRect) -> bool:
        """Fly a snapshot of ``widget`` to ``target``; False when nothing flies (motion off, hidden)."""
        parent = self.parentWidget()
        level = holo.motion_level()
        if (level == holo.MOTION_OFF or parent is None or not parent.isVisible() or not shiboken6.isValid(widget)
                or widget.width() <= 0 or widget.height() <= 0):
            return False
        source = QRectF(QRect(widget.mapTo(parent, QPoint(0, 0)), widget.size()))
        if not QRectF(parent.rect()).intersects(source):
            return False
        dpr = max(1.0, widget.devicePixelRatioF())
        snapshot = QPixmap(round(widget.width() * dpr), round(widget.height() * dpr))
        snapshot.setDevicePixelRatio(dpr)
        snapshot.fill(Qt.GlobalColor.transparent)
        widget.render(snapshot, QPoint(0, 0), QRegion(),
                      QWidget.RenderFlag.DrawChildren)
        self._calm = level == holo.MOTION_CALM
        self._flights.append((snapshot, source, QRectF(target), time.monotonic()))
        self._cover()
        if not self._timer.isActive():
            self._timer.start()
        self._dirty = source.toAlignedRect().adjusted(-2, -2, 2, 2)
        self.update(self._dirty)
        return True

    def flights(self) -> int:
        return len(self._flights)

    def is_running(self) -> bool:
        return bool(self._flights) and self.isVisible()

    def progress(self, index: int = 0, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        return _clamp((now - self._flights[index][3]) / (FLIGHT_MS / 1000.0))

    def frame(self, index: int = 0, now: float | None = None) -> tuple[QRectF, float]:
        """(rect, opacity) of a flight now."""
        snapshot, source, target, _started = self._flights[index]
        p = self.progress(index, now)
        if self._calm:
            return QRectF(source), 1.0 - _OUT(p)
        e = _FLIGHT(p)
        scale = 1.0 - 0.85 * e
        c0, c1 = source.center(), target.center()
        cx, cy = c0.x() + (c1.x() - c0.x()) * e, c0.y() + (c1.y() - c0.y()) * e
        w, h = source.width() * scale, source.height() * scale
        return QRectF(cx - w / 2, cy - h / 2, w, h), 1.0 - e

    def _on_tick(self) -> None:
        now = time.monotonic()
        self._flights = [flight for flight in self._flights if now - flight[3] < FLIGHT_MS / 1000.0]
        if not self._flights:
            self._timer.stop()
            self.hide()
            return
        dirty = QRect()
        for index in range(len(self._flights)):
            dirty = dirty.united(self.frame(index, now)[0].toAlignedRect().adjusted(-2, -2, 2, 2))
        self.update(dirty.united(self._dirty))     # only where the cards were and are
        self._dirty = dirty

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self._timer.stop()
        self._flights = []

    def paintEvent(self, _event: Any) -> None:  # noqa: N802 - Qt override
        if not self._flights:
            return
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            now = time.monotonic()
            for index, (snapshot, _source, _target, _started) in enumerate(self._flights):
                rect, opacity = self.frame(index, now)
                painter.setOpacity(max(0.0, min(1.0, opacity)))
                painter.drawPixmap(rect, snapshot, QRectF(snapshot.rect()))
        finally:
            painter.end()


# --------------------------------------------------------------------------
# The switch between the window and the desktop HUD (SPEC 8.2; Switch.dc.html .sw)
# --------------------------------------------------------------------------

FLIGHT_FLAGS = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.WindowTransparentForInput)
TO_HUD = "hud"
TO_WINDOW = "window"
FLIGHT_TICK_MS = 16
# Window -> HUD, lively ms (Switch.dc.html .sw.m-hud; calm is 1.5 x, holo.motion_ms):
CONTENT_FADE_MS = 200                          # .jc: the window's content fades
FOLD_AT_MS, FOLD_MS = 100, 460                 # .jwin: the frame folds to FOLD_SCALE into the window's orb ...
FOLD_SCALE = 0.12
FRAME_FADE_AT_MS, FRAME_FADE_MS = 360, 200     # ... and fades as it ends
ORB_AT_MS, ORB_MS = 340, 460                   # .forb: the orb flies (it lands at 800 ms)
BAR_AT_MS, BAR_MS = 720, 380                   # .bar: its clip opens from the orb's end ...
BAR_FADE_AT_MS, BAR_FADE_MS = 700, 140
INNER_AT_MS, INNER_MS = 860, 250               # .bin: what is in the bar fades in
SWEEP_AT_MS, SWEEP_MS = 740, 620               # .sw-head: the scan line runs along the top of the work area
EDGE_AT_MS, EDGE_MS = 800, 900                 # .sw-edge: the top edge glows (1.1 s in the prototype; here it ends
                                               # with the switch)
CHIP_AT_MS, CHIP_STAGGER_MS = 1000, 80         # .hchip: the chips drop in, one after the other
CHIP_MS, CHIP_FADE_MS = 420, 220
CHIP_RISE = 16.0
GLOW_FADE_MS = 300                             # .ofx: the orb's switch glow fades in, and out at the end
SWITCH_MS = 1700
# HUD -> window, lively ms (.sw.m-win):
LEAVE_CHIPS_MS, LEAVE_CHIPS_FADE_MS = 180, 160
LEAVE_INNER_MS = 100
CLOSE_AT_MS, CLOSE_MS = 80, 260                # the bar folds into the orb ...
CLOSE_FADE_AT_MS, CLOSE_FADE_MS = 280, 120
GROW_AT_MS, GROW_MS = 620, 480                 # the window's frame grows back out of the orb
GROW_FADE_MS = 160
REVEAL_MS = 320                                # .jc: the window shows under the flight, which fades off it
RETURN_MS = GROW_AT_MS + GROW_MS + REVEAL_MS
# Their shapes (Switch.dc.html px):
BAR_OUTSET = 40                                # .bar's clip-path inset(-40px ...) reaches past the bar ...
BAR_CLIP_START = 44                            # ... and starts as its left 44 px, round the orb
BAR_CLIP_RADIUS = 56
SWEEP_HEAD = 470                               # .sw-head: 470 x 2 px, from 480 px left of the strip to past its end
SWEEP_FROM = 480
SWEEP_FADE = 48                                # where the flight's edge cuts the strip, it fades out over this
ORB_GLOW = 1.28                                # .ofx: inset -14 %
_FOLD = holo.bezier((0.55, 0.0, 0.8, 0.25))
_ARC_SIDE = holo.bezier((0.6, 0.0, 0.3, 1.0))      # the orb's flight: one axis sets off slowly ...
_ARC_RISE = holo.bezier((0.2, 0.75, 0.3, 1.0))     # ... the other at once: an arc
_SIZE = holo.bezier((0.4, 0.0, 0.2, 1.0))
_CLOSE = holo.bezier((0.7, 0.0, 0.84, 0.0))
_EASE = holo.bezier(holo.CSS_EASE)
_EASE_IN = holo.bezier((0.42, 0.0, 1.0, 1.0))      # CSS ease-in
_CSS_OUT = holo.bezier(holo.CSS_EASE_OUT)
_SWEEP = holo.bezier((0.45, 0.0, 0.2, 1.0))


@dataclass
class FlightLayer:
    """A picture the switch shows: ``image`` (a QPixmap or QImage, with its device pixel ratio) with its top-left at
    ``at`` (global, logical px)."""

    image: Any
    at: QPoint

    def rect(self) -> QRect:
        dpr = self.image.devicePixelRatio() or 1.0
        return QRect(self.at, QSize(math.ceil(self.image.width() / dpr), math.ceil(self.image.height() / dpr)))


@dataclass
class WindowScene:
    """The Jarvis window as the switch shows it: its ``frame`` (the ground and the outline, nothing in it) and its
    ``content`` (as it shows; None on the way back, where the window itself comes in under the flight), both of
    ``rect`` (global); ``orb`` is its orb's centre (global: the frame folds into it and grows out of it), ``orb_px``
    that orb's size."""

    frame: QPixmap
    content: QPixmap | None
    rect: QRect
    orb: QPointF
    orb_px: float


@dataclass
class FlownOrb:
    """The orb that flies: a snapshot (``pixmap``) with the orb ``px`` square in its middle."""

    pixmap: QPixmap
    px: float


@dataclass
class HudScene:
    """The desktop HUD as the switch shows it (hud_overlay.HudOverlay.flight_scene; global, logical px): the ``bar``
    (its clip opens from the orb's end), the bar's ``panel``, ``inner`` (the hairlines, the input row, the caption and
    Bring Jarvis back) and ``bar_glow`` (its outer shadow and glow), the ``chips`` with their glows (left to right),
    the HUD's live ``orb`` (an OrbWidget: ``snapshot()``, ``frame_interval()``, ``orb_size()``) centred at
    ``orb_center``, and the ``strip`` along the top of the work area where the scan line runs."""

    bar: QRect
    panel: FlightLayer
    inner: FlightLayer
    bar_glow: FlightLayer | None
    chips: list[tuple[FlightLayer, FlightLayer | None]]
    orb: Any
    orb_center: QPointF
    strip: QRect


def _draw(painter: QPainter, layer: FlightLayer, dy: float = 0.0) -> None:
    point = QPointF(layer.at) + QPointF(0.0, dy)
    if isinstance(layer.image, QImage):
        painter.drawImage(point, layer.image)
    else:
        painter.drawPixmap(point, layer.image)


def _box(rect: QRectF | QRect) -> QRect:
    """A painted rect grown to whole pixels and the antialiasing round it (a dirty rect)."""
    return QRectF(rect).toAlignedRect().adjusted(-2, -2, 2, 2)


class WindowFlight(QWidget):
    """See the module docstring."""

    painted = Signal()          # its first frame is on screen: what it covers may hide now
    handover = Signal()         # to the HUD: everything has landed - the HUD's own windows show now
    windowTime = Signal()       # to the window: the frame is whole - the window shows now, under the flight
    finished = Signal()         # it ended by itself (never after stop())
    interrupted = Signal()      # hidden or closed from outside before its end (nothing of Jarvis's does that)

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(None, FLIGHT_FLAGS)
        self.setObjectName("JarvisFlight")
        self.setWindowTitle("Jarvis")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._clock = clock
        self._direction = ""
        self._window: WindowScene | None = None
        self._hud: HudScene | None = None
        self._orb: FlownOrb | None = None
        self._hud_n = 0                          # the HUD's pictures were taken anew this often (set_hud)
        self._k = 1.0                            # its lengths: 1 lively, 1.5 calm
        self._started = 0.0
        self._t = 0.0                            # where it is on the timeline (lively ms)
        self._running = False
        self._shown = False                      # its first frame is on screen (``painted``)
        self._handed: float | None = None        # to the HUD: when it handed over (lively ms)
        self._window_time = False
        self._after: list[Callable[[], None]] = []
        self._live: QPixmap | None = None        # the HUD's live orb's last frame ...
        self._live_at = -1.0                     # ... taken then (clock s)
        self._live_n = 0
        self._parts: dict[str, tuple[tuple, QRect]] = {}     # each part as last painted: its state and rect (global)
        self._head: QImage | None = None         # the scan line's head, painted once
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(FLIGHT_TICK_MS)
        self._timer.timeout.connect(self.tick)

    # ---- flying ----------------------------------------------------------------------------------

    def set_clock(self, clock: Callable[[], float]) -> None:
        self._clock = clock

    def to_hud(self, window: WindowScene, orb: FlownOrb, hud: HudScene, rect: QRect, screen: Any = None) -> None:
        """Window -> HUD over ``rect`` (global) on ``screen``; see the module docstring."""
        self._start(TO_HUD, window, orb, hud, rect, screen)

    def to_window(self, hud: HudScene, orb: FlownOrb, window: WindowScene, rect: QRect, screen: Any = None) -> None:
        """HUD -> window over ``rect`` (global) on ``screen``; see the module docstring."""
        self._start(TO_WINDOW, window, orb, hud, rect, screen)

    def set_hud(self, hud: HudScene) -> None:
        """The HUD's pictures taken anew (a chip came, went or changed its words): they fly from now on."""
        if self._hud is not None:
            self._hud = hud
            self._hud_n += 1
            self._refresh()

    def tick(self) -> None:
        """Step to the clock now (its timer does this every 16 ms while it flies)."""
        if not self._running:
            return
        self._run_after()
        elapsed = max(0.0, (self._clock() - self._started) * 1000.0)
        # to the microsecond: a step to a mark of the timeline never falls a hair short of it in floating point
        self._t = round(elapsed / self._k, 3) if self._k > 0.0 else float(self.length())
        self._take_live()
        self._refresh()
        if not self._shown:
            self._on_painted()           # its timer's first step: what it covers goes even had no paint come yet
        if self._direction == TO_HUD:
            if self._handed is None and self._t >= self.handover_ms():
                self._handed = self._t
                self.handover.emit()
        elif not self._window_time and self._t >= GROW_AT_MS + GROW_MS:
            self._window_time = True
            self.windowTime.emit()
        if self._running and self._t >= self.length():
            self._end()

    def stop(self) -> None:
        """End at once with nothing shown and no signal (Jarvis shuts the HUD down, or the application quits)."""
        self._running = False
        self._timer.stop()
        self._run_after()
        if self.isVisible():
            self.hide()

    def after_tick(self, fn: Callable[[], None]) -> None:
        """Call ``fn`` at the next step - or when it ends or stops, whichever comes first."""
        self._after.append(fn)

    # ---- what it is doing --------------------------------------------------------------------------

    def is_running(self) -> bool:
        return self._running

    def direction(self) -> str:
        """``TO_HUD`` or ``TO_WINDOW`` ("" before it flew)."""
        return self._direction

    def elapsed(self) -> float:
        """Where it is on the timeline (lively ms: at calm it moves 1.5 x slower)."""
        return self._t

    def length(self) -> int:
        """Its length on the timeline (lively ms)."""
        return SWITCH_MS if self._direction == TO_HUD else RETURN_MS

    def total_ms(self) -> int:
        """How long it takes at the motion level it started at (holo.motion_ms: calm 1.5 x)."""
        return round(self.length() * self._k)

    def handover_ms(self) -> float:
        """To the HUD: when everything has landed (lively ms) - what is in the bar, then the last chip."""
        chips = len(self._hud.chips) if self._hud is not None else 0
        last = CHIP_AT_MS + (chips - 1) * CHIP_STAGGER_MS + CHIP_MS if chips else 0
        return float(max(INNER_AT_MS + INNER_MS, last))

    def parts(self) -> list[str]:
        """The parts it shows now: "window", "bar", "chip<n>", "orb", "sweep" (tests and the harness)."""
        return sorted(self._parts)

    def state(self, part: str) -> tuple | None:
        """A part's state now - what decides its pixels; None while it does not show: "window" (x, y, width, height,
        the frame's opacity, the content's), "bar" (how far its clip is open, its opacity, what is in it's, the
        HUD's pictures' count), "chip<n>" (its drop, its opacity, ...), "orb" (x, y, size, picture, its frame's
        count, its opacity, the disc's, the glow's), "sweep" ((the head's x, its opacity) or None, the edge's)."""
        item = self._parts.get(part)
        return item[0] if item is not None else None

    def scenes(self) -> tuple[WindowScene | None, FlownOrb | None, HudScene | None]:
        """What it flies: the window, the orb and the HUD (tests and the harness)."""
        return self._window, self._orb, self._hud

    def orb_state(self) -> tuple[str, QPointF, float] | None:
        """The orb now: ("flown" - the snapshot - or "live" - the HUD's own orb, or "" - only its glow -, its
        centre (global), its size), or None."""
        part = self._parts.get("orb")
        if part is None:
            return None
        cx, cy, px, picture = part[0][:4]
        return picture, QPointF(cx, cy), px

    # ---- the timeline ---------------------------------------------------------------------------------

    def _start(self, direction: str, window: WindowScene, orb: FlownOrb, hud: HudScene, rect: QRect,
               screen: Any) -> None:
        self._direction = direction
        self._window, self._orb, self._hud = window, orb, hud
        self._k = holo.motion_ms(1000) / 1000.0
        self._t = 0.0
        self._handed = None
        self._window_time = self._shown = False
        self._live, self._live_at = None, -1.0
        self._parts = {}
        if screen is not None and shiboken6.isValid(screen):
            self.setScreen(screen)
        self.setGeometry(rect)
        self._started = self._clock()
        self._running = True
        self._take_live()
        self._refresh()
        handle = self.windowHandle()
        if handle is not None:
            handle.setProperty("_q_showWithoutActivating", True)
        self.show()
        self._timer.start()

    def _end(self) -> None:
        self._running = False
        self._timer.stop()
        self._run_after()
        self.hide()
        self.finished.emit()

    def _run_after(self) -> None:
        calls, self._after = self._after, []
        for fn in calls:
            fn()

    def _on_painted(self) -> None:
        if self._running and not self._shown:
            self._shown = True
            self.painted.emit()

    def _dropped(self) -> bool:
        """To the HUD, from the step after the handover: the HUD's own windows show its bar, chips and orb."""
        return self._handed is not None and self._t > self._handed

    def _live_now(self) -> bool:
        """The HUD's own orb shows in the flight: to the HUD once it landed, back until it flies."""
        if self._hud is None or self._hud.orb is None:
            return False
        if self._direction == TO_HUD:
            return self._t >= ORB_AT_MS + ORB_MS and not self._dropped()
        return self._t < ORB_AT_MS

    def _take_live(self) -> None:
        """A frame of the HUD's live orb whenever its own frame is due (it is hidden: its clock does not run it)."""
        if not self._live_now():
            return
        orb = self._hud.orb
        now = self._clock()
        interval = orb.frame_interval()
        if self._live is not None and (interval is None or now - self._live_at < interval / 1000.0):
            return
        self._live = orb.snapshot()
        self._live_at = now
        self._live_n += 1

    def _window_frame(self, t: float) -> tuple[QRectF, float, float] | None:
        """(rect, frame opacity, content opacity) of the window's picture now, or None."""
        scene = self._window
        if scene is None:
            return None
        if self._direction == TO_HUD:
            scale = 1.0 - (1.0 - FOLD_SCALE) * _FOLD(_clamp((t - FOLD_AT_MS) / FOLD_MS))
            frame = 1.0 - _clamp((t - FRAME_FADE_AT_MS) / FRAME_FADE_MS)
            content = 1.0 - _EASE(_clamp(t / CONTENT_FADE_MS)) if scene.content is not None else 0.0
        else:
            scale = FOLD_SCALE + (1.0 - FOLD_SCALE) * _OUT(_clamp((t - GROW_AT_MS) / GROW_MS))
            frame = _clamp((t - GROW_AT_MS) / GROW_FADE_MS) * self._reveal(t)
            content = 0.0
        if frame <= 0.0:
            return None
        rect, origin = QRectF(scene.rect), scene.orb
        return (QRectF(origin.x() + (rect.x() - origin.x()) * scale, origin.y() + (rect.y() - origin.y()) * scale,
                       rect.width() * scale, rect.height() * scale), frame, content)

    def _reveal(self, t: float) -> float:
        """Back to the window: 1 until the window shows, then down to 0 as the flight fades off it."""
        return 1.0 - _EASE(_clamp((t - GROW_AT_MS - GROW_MS) / REVEAL_MS))

    def _bar_frame(self, t: float) -> tuple[float, float, float] | None:
        """(how far its clip is open 0..1, its opacity, what is in it's opacity) now, or None."""
        if self._hud is None or self._dropped():
            return None
        if self._direction == TO_HUD:
            opened = _OUT(_clamp((t - BAR_AT_MS) / BAR_MS))
            alpha = _clamp((t - BAR_FADE_AT_MS) / BAR_FADE_MS)
            inner = _EASE(_clamp((t - INNER_AT_MS) / INNER_MS))
        else:
            opened = 1.0 - _CLOSE(_clamp((t - CLOSE_AT_MS) / CLOSE_MS))
            alpha = 1.0 - _clamp((t - CLOSE_FADE_AT_MS) / CLOSE_FADE_MS)
            inner = 1.0 - _EASE(_clamp(t / LEAVE_INNER_MS))
        return (opened, alpha, inner) if alpha > 0.0 else None

    def _chip_frames(self, t: float) -> list[tuple[float, float] | None]:
        """Each chip's (drop in px, opacity) now, or None."""
        if self._hud is None or self._dropped():
            return []
        frames: list[tuple[float, float] | None] = []
        for index in range(len(self._hud.chips)):
            if self._direction == TO_HUD:
                local = t - CHIP_AT_MS - index * CHIP_STAGGER_MS
                dy = -CHIP_RISE * (1.0 - _OUT(_clamp(local / CHIP_MS)))
                alpha = _clamp(local / CHIP_FADE_MS)
            else:
                dy = -CHIP_RISE * _EASE_IN(_clamp(t / LEAVE_CHIPS_MS))
                alpha = 1.0 - _clamp(t / LEAVE_CHIPS_FADE_MS)
            frames.append((dy, alpha) if alpha > 0.0 else None)
        return frames

    def _orb_frame(self, t: float) -> tuple[float, float, float, str, float, float, float] | None:
        """(centre x, y, size, picture - "flown", "live" or "" -, its opacity, the disc's, the glow's) now. As CSS
        moves .forb: its box's left and top each on its own curve (an arc) and its size on a third, so the box's top
        reaches the HUD's height early while the orb is still large and stays on screen."""
        scene, hud, flown = self._window, self._hud, self._orb
        if scene is None or hud is None or flown is None:
            return None
        hud_px = float(hud.orb.orb_size()) if hud.orb is not None else flown.px
        p = _clamp((t - ORB_AT_MS) / ORB_MS)
        rise = _clamp(t / GLOW_FADE_MS)
        if self._direction == TO_HUD:
            (sx, sy), (ex, ey), (spx, epx) = scene.orb.toTuple(), hud.orb_center.toTuple(), (scene.orb_px, hud_px)
            ease_x, ease_y = _ARC_SIDE, _ARC_RISE
            picture = "live" if self._live_now() and self._live is not None else "flown"
            alpha, disc = 1.0, rise
            glow = rise * (1.0 - _clamp((t - SWITCH_MS + GLOW_FADE_MS) / GLOW_FADE_MS))
            if self._dropped():
                picture, disc = "", 0.0         # the HUD's own orb and disc show: only the glow fades round them
        else:
            (sx, sy), (ex, ey), (spx, epx) = hud.orb_center.toTuple(), scene.orb.toTuple(), (hud_px, scene.orb_px)
            ease_x, ease_y = _ARC_RISE, _ARC_SIDE
            picture = "live" if self._live_now() and self._live is not None else "flown"
            fade = self._reveal(t)
            # The orb stays whole to the end (it is never part of the window's content), then the window's own takes
            # over; its disc and the switch's glow fade with the frame.
            alpha, disc, glow = 1.0, fade, rise * fade
        if alpha <= 0.0 and glow <= 0.0:
            return None
        size = spx + (epx - spx) * _SIZE(p)
        left = sx - spx / 2 + ((ex - epx / 2) - (sx - spx / 2)) * ease_x(p)
        top = sy - spx / 2 + ((ey - epx / 2) - (sy - spx / 2)) * ease_y(p)
        return left + size / 2, top + size / 2, size, picture, alpha, disc, glow

    def _sweep_frame(self, t: float) -> tuple[tuple[float, float] | None, float] | None:
        """To the HUD: ((the head's left (global x), its opacity) or None, the top edge's opacity) now, or None."""
        if self._direction != TO_HUD or self._hud is None:
            return None
        strip = QRectF(self._hud.strip)
        head = None
        p = _clamp((t - SWEEP_AT_MS) / SWEEP_MS)
        if 0.0 < p < 1.0:
            x = strip.left() - SWEEP_FROM + (strip.width() + 20.0 + SWEEP_FROM) * _SWEEP(p)
            head = (x, 1.0 if p <= 0.82 else 1.0 - _SWEEP((p - 0.82) / 0.18))
        q = _clamp((t - EDGE_AT_MS) / EDGE_MS)
        edge = 0.0
        if 0.0 < q < 1.0:
            edge = 0.75 * (_CSS_OUT(q / 0.3) if q < 0.3 else 1.0 - _CSS_OUT((q - 0.3) / 0.7))
        if head is None and edge <= 0.0:
            return None
        return head, edge

    # ---- repainting only what moved --------------------------------------------------------------------

    def _compute(self) -> dict[str, tuple[tuple, QRect]]:
        """Each part's state (what decides its pixels) and the rect it paints (global) now."""
        t = self._t
        parts: dict[str, tuple[tuple, QRect]] = {}
        window = self._window_frame(t)
        if window is not None:
            rect, frame, content = window
            parts["window"] = ((round(rect.x(), 2), round(rect.y(), 2), round(rect.width(), 2),
                                round(rect.height(), 2), round(frame, 4), round(content, 4)), _box(rect))
        hud = self._hud
        bar = self._bar_frame(t)
        if bar is not None and hud is not None:
            opened, alpha, inner = bar
            extent = QRectF(hud.bar).adjusted(-BAR_OUTSET, -BAR_OUTSET, BAR_OUTSET, BAR_OUTSET)
            if hud.bar_glow is not None:
                extent = extent.united(QRectF(hud.bar_glow.rect()))
            clip = self._bar_clip(opened, self._glow_outset()).boundingRect()
            parts["bar"] = ((round(opened, 4), round(alpha, 4), round(inner, 4), self._hud_n),
                            _box(extent.intersected(clip)))
        if hud is not None:
            for index, chip in enumerate(self._chip_frames(t)):
                if chip is None:
                    continue
                dy, alpha = chip
                layer, glow = hud.chips[index]
                rect = QRectF(layer.rect()).united(QRectF(glow.rect()) if glow is not None else QRectF())
                parts[f"chip{index}"] = ((round(dy, 3), round(alpha, 4), self._hud_n), _box(rect.translated(0.0, dy)))
        orb = self._orb_frame(t)
        if orb is not None:
            cx, cy, px, picture, alpha, disc, glow = orb
            side = px * max(ORB_GLOW, self._picture_side(picture) / max(1.0, self._picture_px(picture)))
            state = (round(cx, 2), round(cy, 2), round(px, 2), picture, self._live_n if picture == "live" else 0,
                     round(alpha, 4), round(disc, 4), round(glow, 4))
            parts["orb"] = (state, _box(QRectF(cx - side / 2, cy - side / 2, side, side)))
        sweep = self._sweep_frame(t)
        if sweep is not None and hud is not None:
            head, edge = sweep
            state = ((round(head[0], 2), round(head[1], 4)) if head is not None else None, round(edge, 4))
            parts["sweep"] = (state, _box(QRectF(hud.strip)))
        return parts

    def _refresh(self) -> None:
        """Take the parts now and repaint only where one changed: where it was and where it is."""
        parts = self._compute()
        origin = self.geometry().topLeft()
        dirty = QRegion()
        for key in set(parts) | set(self._parts):
            old, new = self._parts.get(key), parts.get(key)
            if old is not None and new is not None and old[0] == new[0]:
                continue
            for item in (old, new):
                if item is not None:
                    dirty = dirty.united(QRegion(item[1].translated(-origin)))
        self._parts = parts
        if not dirty.isEmpty() and self.isVisible():
            self.update(dirty)

    def _glow_outset(self) -> float:
        """How far the bar's clip reaches past the bar for its shadow and glow (at least the prototype's 40 px)."""
        hud = self._hud
        if hud is None or hud.bar_glow is None:
            return float(BAR_OUTSET)
        glow, bar = hud.bar_glow.rect(), hud.bar
        return float(max(BAR_OUTSET, bar.left() - glow.left(), bar.top() - glow.top(), glow.right() - bar.right(),
                         glow.bottom() - bar.bottom()))

    def _bar_clip(self, opened: float, outset: float) -> QPainterPath:
        """The bar's clip (Switch.dc.html .bar clip-path): from its left 44 px round the orb to all of it, ``outset`` px
        past it, the corners rounded 56 px (less while it is narrow, as CSS does)."""
        bar = QRectF(self._hud.bar) if self._hud is not None else QRectF()
        right = bar.left() + BAR_CLIP_START + (bar.width() - BAR_CLIP_START + outset) * opened
        clip = QRectF(bar.left() - outset, bar.top() - outset, right - bar.left() + outset, bar.height() + 2 * outset)
        radius = BAR_CLIP_RADIUS * min(1.0, clip.width() / (2 * BAR_CLIP_RADIUS), clip.height() / (2 * BAR_CLIP_RADIUS))
        path = QPainterPath()
        path.addRoundedRect(clip, radius, radius)
        return path

    def _picture(self, picture: str) -> QPixmap | None:
        if picture == "live":
            return self._live
        if picture == "flown" and self._orb is not None:
            return self._orb.pixmap
        return None

    def _picture_px(self, picture: str) -> float:
        """The orb's size inside ``picture``."""
        if picture == "live" and self._hud is not None and self._hud.orb is not None:
            return float(self._hud.orb.orb_size())
        return float(self._orb.px) if self._orb is not None else 1.0

    def _picture_side(self, picture: str) -> float:
        pixmap = self._picture(picture)
        if pixmap is None:
            return 0.0
        dpr = pixmap.devicePixelRatio() or 1.0
        return max(pixmap.width(), pixmap.height()) / dpr

    # ---- painting --------------------------------------------------------------------------------------

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        handle = self.windowHandle()
        if handle is not None:
            handle.setProperty("_q_showWithoutActivating", True)     # read at each show (Windows)

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        if self._running:                       # hidden or closed from outside before its end
            self._running = False
            self._timer.stop()
            self._run_after()
            self.interrupted.emit()

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        parts = self._parts
        origin = self.geometry().topLeft()
        area = event.rect().translated(origin)
        painter = QPainter(self)
        try:
            painter.setClipRegion(event.region())
            painter.translate(-QPointF(origin))
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            self._paint_window(painter, parts.get("window"), area)
            self._paint_hud(painter, parts, area)
            self._paint_orb(painter, parts.get("orb"), area)
            self._paint_sweep(painter, parts.get("sweep"), area)
        finally:
            painter.end()
        if not self._shown and self._running:
            QTimer.singleShot(0, self, self._on_painted)

    def _paint_window(self, painter: QPainter, part: tuple[tuple, QRect] | None, area: QRect) -> None:
        scene = self._window
        if part is None or scene is None or not part[1].intersects(area):
            return
        x, y, w, h, frame, content = part[0]
        rect = QRectF(x, y, w, h)
        painter.setOpacity(frame)
        painter.drawPixmap(rect, scene.frame, QRectF(scene.frame.rect()))
        if content > 0.0 and scene.content is not None:
            painter.setOpacity(frame * content)                 # .jc is inside .jwin
            painter.drawPixmap(rect, scene.content, QRectF(scene.content.rect()))
        painter.setOpacity(1.0)

    def _paint_hud(self, painter: QPainter, parts: dict[str, tuple[tuple, QRect]], area: QRect) -> None:
        """The bar and the chips - their shadows and glows first, under everything, as the decor layer has them."""
        hud = self._hud
        if hud is None:
            return
        bar = parts.get("bar")
        chips = [(index, parts.get(f"chip{index}")) for index in range(len(hud.chips))]
        if bar is not None and bar[1].intersects(area) and hud.bar_glow is not None:
            opened, alpha = bar[0][0], bar[0][1]
            painter.save()
            painter.setClipPath(self._bar_clip(opened, self._glow_outset()), Qt.ClipOperation.IntersectClip)
            painter.setOpacity(alpha)
            _draw(painter, hud.bar_glow)
            painter.restore()
        for index, part in chips:
            glow = hud.chips[index][1]
            if part is not None and glow is not None and part[1].intersects(area):
                painter.setOpacity(part[0][1])
                _draw(painter, glow, part[0][0])
        if bar is not None and bar[1].intersects(area):
            opened, alpha, inner = bar[0][:3]
            painter.save()
            painter.setClipPath(self._bar_clip(opened, BAR_OUTSET), Qt.ClipOperation.IntersectClip)
            painter.setOpacity(alpha)
            _draw(painter, hud.panel)
            if inner > 0.0:
                painter.setOpacity(alpha * inner)                # .bin is inside .bar
                _draw(painter, hud.inner)
            painter.restore()
        for index, part in chips:
            if part is not None and part[1].intersects(area):
                painter.setOpacity(part[0][1])
                _draw(painter, hud.chips[index][0], part[0][0])
        painter.setOpacity(1.0)

    def _paint_orb(self, painter: QPainter, part: tuple[tuple, QRect] | None, area: QRect) -> None:
        """The flying orb as Switch.dc.html's .forb: its dark disc (::before), the switch's glow (.ofx), the orb."""
        if part is None or not part[1].intersects(area):
            return
        cx, cy, px, picture, _n, alpha, disc, glow = part[0]
        center = QPointF(cx, cy)
        painter.setPen(Qt.PenStyle.NoPen)
        if disc > 0.0:
            radius = max(0.0, px / 2 - 2)
            gradient = QRadialGradient(center, radius * math.sqrt(2.0))   # CSS farthest-corner of its box
            void = QColor(holo.VOID)
            gradient.setColorAt(0.0, holo.alpha(void, 0.82))
            gradient.setColorAt(0.52, holo.alpha(void, 0.82))
            gradient.setColorAt(0.72, holo.alpha(void, 0.0))
            gradient.setColorAt(1.0, holo.alpha(void, 0.0))
            painter.setOpacity(disc)
            painter.setBrush(gradient)
            painter.drawEllipse(center, radius, radius)
        if glow > 0.0:
            half = px * ORB_GLOW / 2
            gradient = QRadialGradient(center, half * math.sqrt(2.0))
            gradient.setColorAt(0.0, holo.alpha(holo.HOLO, 0.32))
            gradient.setColorAt(0.66, holo.alpha(holo.HOLO, 0.0))
            gradient.setColorAt(1.0, holo.alpha(holo.HOLO, 0.0))
            painter.setOpacity(glow)
            painter.setBrush(gradient)
            painter.drawEllipse(center, half, half)
        pixmap = self._picture(picture)
        if pixmap is not None and alpha > 0.0:
            dpr = pixmap.devicePixelRatio() or 1.0
            scale = px / max(1.0, self._picture_px(picture))
            w, h = pixmap.width() / dpr * scale, pixmap.height() / dpr * scale
            painter.setOpacity(alpha)
            painter.drawPixmap(QRectF(cx - w / 2, cy - h / 2, w, h), pixmap, QRectF(pixmap.rect()))
        painter.setOpacity(1.0)

    def _paint_sweep(self, painter: QPainter, part: tuple[tuple, QRect] | None, area: QRect) -> None:
        """The scan line along the top of the work area (Switch.dc.html .sweep: a 16 px strip): the top edge's glow
        and the running head, both ScanSweep's painting."""
        hud = self._hud
        if part is None or hud is None or not part[1].intersects(area):
            return
        head, edge = part[0]
        strip = QRectF(hud.strip)
        shown = strip.intersected(QRectF(self.geometry()))         # the flight never covers the whole screen
        if shown.isEmpty():
            return
        dpr = self.devicePixelRatioF() or 1.0
        image = QImage(math.ceil(shown.width() * dpr), math.ceil(shown.height() * dpr),
                       QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(Qt.GlobalColor.transparent)
        strip_painter = QPainter(image)
        try:
            strip_painter.translate(-shown.topLeft())
            if edge > 0.0:
                paint_scan_line(strip_painter, strip.top() + 0.5, shown.left(), shown.right(), edge, glow=8.0,
                                thick=1.0, ink=holo.alpha(holo.HOLO_HI, 0.7))
            if head is not None:
                strip_painter.setOpacity(head[1])
                strip_painter.drawImage(QPointF(head[0], strip.top() + 1.0 - ScanSweep.GLOW), self._head_image())
            # Where the flight's edge cuts the strip short of the work area's, it fades out there rather than stops.
            cut_left, cut_right = shown.left() > strip.left() + 0.5, shown.right() < strip.right() - 0.5
            if cut_left or cut_right:
                span = min(0.5, SWEEP_FADE / max(1.0, shown.width()))
                mask = QLinearGradient(shown.left(), 0.0, shown.right(), 0.0)
                mask.setColorAt(0.0, QColor(0, 0, 0, 0 if cut_left else 255))
                mask.setColorAt(span, QColor(0, 0, 0, 255))
                mask.setColorAt(1.0 - span, QColor(0, 0, 0, 255))
                mask.setColorAt(1.0, QColor(0, 0, 0, 0 if cut_right else 255))
                strip_painter.setOpacity(1.0)
                strip_painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
                strip_painter.fillRect(shown, mask)
        finally:
            strip_painter.end()
        painter.drawImage(shown.topLeft(), image)

    def _head_image(self) -> QImage:
        """The scan line's head (Switch.dc.html .sw-head: 470 x 2 px, clear at its tail, bright at its front), with
        ScanSweep's glow fading along it the same way; painted once per device pixel ratio."""
        dpr = self.devicePixelRatioF() or 1.0
        if self._head is None or self._head.devicePixelRatio() != dpr:
            glow = ScanSweep.GLOW
            image = QImage(round(SWEEP_HEAD * dpr), round(2 * glow * dpr), QImage.Format.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(dpr)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            try:
                paint_scan_line(painter, glow, 0.0, SWEEP_HEAD, 1.0, glow=glow, thick=0.0)      # the glow only
                fade = QLinearGradient(0.0, 0.0, SWEEP_HEAD, 0.0)
                fade.setColorAt(0.0, QColor(0, 0, 0, 0))
                fade.setColorAt(0.7, QColor(0, 0, 0, 128))
                fade.setColorAt(1.0, QColor(0, 0, 0, 255))
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
                painter.fillRect(QRectF(0.0, 0.0, SWEEP_HEAD, 2 * glow), fade)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
                ink = QLinearGradient(0.0, 0.0, SWEEP_HEAD, 0.0)
                ink.setColorAt(0.0, holo.alpha(holo.HOLO, 0.0))
                ink.setColorAt(0.7, holo.alpha(holo.HOLO, 0.5))
                ink.setColorAt(1.0, QColor("#E9FDFF"))
                painter.fillRect(QRectF(0.0, glow - 1.0, SWEEP_HEAD, 2.0), QBrush(ink))
            finally:
                painter.end()
            self._head = image
        return self._head


class StartupPresence:
    """See the module docstring."""

    def __init__(self, play: Callable[[], None], clock: Callable[[], float] = time.monotonic) -> None:
        self._play = play
        self._clock = clock
        self._armed = False
        self._shown = False
        self._played = False
        self._hidden_at: float | None = None
        self._plays = 0

    def arm(self) -> None:
        self._armed = True

    def armed(self) -> bool:
        return self._armed

    def plays(self) -> int:
        return self._plays

    def set_clock(self, clock: Callable[[], float]) -> None:
        self._clock = clock

    def note_shown(self, shown: bool) -> None:
        shown = bool(shown)
        if shown == self._shown:
            return
        self._shown = shown
        now = self._clock()
        if not shown:
            self._hidden_at = now
            return
        if not self._armed:
            return
        hidden_at, self._hidden_at = self._hidden_at, None
        if self._played and (hidden_at is None or now - hidden_at < REPLAY_AFTER_S):
            return
        self._played = True
        self._plays += 1
        self._play()
