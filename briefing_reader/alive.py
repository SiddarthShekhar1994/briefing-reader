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
    in 1.6 s (``play()``, ``is_running()``, ``line_y()``); it repaints only the band it crosses.
``StartupPresence(play, clock=time.monotonic)``
    When the start-up plays: ``arm()`` (the app started), then ``note_shown(bool)`` as the window
    is shown, hidden or minimised: the first time it is shown after ``arm()``, and again when it is
    shown after being hidden for at least ``REPLAY_AFTER_S`` (30 minutes). ``plays()`` counts.
``main_panels(page, limit=6)``
    The widgets that assemble: the first level under ``page`` (through containers with a single
    visible child) that holds more than one visible widget, top to bottom.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import shiboken6
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap, QRegion
from PySide6.QtWidgets import QApplication, QWidget

from . import holo

REPLAY_AFTER_S = 30 * 60
WIPE_BAND = 120.0
_ENDING_EVENTS = (QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick,
                  QEvent.Type.Wheel)
_OUT = holo.bezier(holo.EASE_OUT)
_WIPE_EASE = holo.bezier((0.5, 0.0, 0.5, 1.0))
_IN_OUT = holo.bezier(holo.CSS_EASE_IN_OUT)


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
            y = self.line_y()
            painter.setOpacity(self.line_opacity())
            glow = QLinearGradient(0.0, y - self.GLOW, 0.0, y + self.GLOW)
            glow.setColorAt(0.0, holo.alpha(holo.HOLO_HI, 0.0))
            glow.setColorAt(0.5, holo.alpha(holo.HOLO_HI, 0.35))
            glow.setColorAt(1.0, holo.alpha(holo.HOLO_HI, 0.0))
            painter.fillRect(QRectF(0.0, y - self.GLOW, self.width(), 2 * self.GLOW), glow)
            painter.fillRect(QRectF(0.0, y - 1.0, self.width(), 2.0), QColor(holo.HOLO_HI))
        finally:
            painter.end()


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
