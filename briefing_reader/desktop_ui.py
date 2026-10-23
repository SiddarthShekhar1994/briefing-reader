"""Jarvis on your desktop: the app's side of the HUD (SPEC sections 1, 8.2, 8.3, 9 and 12; Phase 1, parts 4 to 6: the
switch, its flight, the HUD key and the bar's keyboard).

The AppController makes one ``DesktopController`` while [desktop] is on (none while it is off: no HUD window, no
hook). It keeps the owner's rules:

* The HUD is a sub-state of the assistant screen (STATE_READING). ``enter_hud()`` HIDES the window - never closes it
  (closing the window quits Jarvis) - and shows the HUD's windows (hud_overlay.HudOverlay) on the screen the window
  is on. ``leave_hud(screen)`` hides the HUD and brings the window back on ``screen`` (a chip's), else on the screen
  an automatic change asked for meanwhile, else Home. ``is_hud()`` says which: true from the moment the switch to the
  HUD starts until the window shows again on the way back. A second enter or leave, or one during a switch
  (``is_switching()``), changes nothing.
* The switch flies (SPEC 8.2, Switch.dc.html; alive.WindowFlight in a click-through window of its own that never
  takes the focus): to the HUD the window's content fades, its frame folds into its orb, the orb flies to the HUD's
  place and becomes the HUD's live orb as it lands, the bar opens from it with the scan line along the top of the
  work area and the chips drop in - their counts as they are then (``sync_glance`` gives the flight the HUD anew);
  the window hides once the flight's first frame is up, and the HUD's own windows show once everything has landed
  (about 1.5 s; the flight ends at 1.7 s). Back, the reverse: the chips go, the bar folds into the orb, the orb flies
  home and the window's frame grows back out of it (the hidden window rendered anew on its screen - if that comes out
  blank, its frame as it left); then the window shows under the flight - at opacity 0, then 1 - brought forward on the
  owner's click, its screen assembling (ReadingView.replay_screen), and the flight fades off it (1.42 s). Calm: 1.5 x
  slower; [ui] motion off, Windows' reduced motion, a window not shown (or minimised), the HUD on another screen or a
  flight that cannot be set up: the instant swap, and no flight window is made at all. One flight per switch: hidden
  and deleted as it ends, no timer left (hard rule 17: only the orb loops).
* Only the owner starts it: the Dock's Desktop or the Find list's (``desktopRequested``), the HUD key (the hotkey
  agent's "desktop" message; AppController.handle_activation decides by SPEC 9's table) and later a desktop request
  typed in the window's bar (P1.7). Every entry passes THE CONSENT GATE (``consent_given``, fail-closed), where P1.9
  plugs in the first-run "Jarvis on your desktop" card.
* The bar's keyboard (SPEC 8.3): ``take_keyboard()`` (the HUD key) gives the HUD's bar the keyboard - at once, or as
  the HUD shows when the switch to it is on its way (hud_overlay.HudWindow.take_keyboard: the window that had it is
  remembered and gets it back after Enter or Esc in an empty bar); ``has_keyboard()`` says whether it has it (the next
  HUD key then goes back to the window). Going back to the window lets it go first - the window takes it on the
  owner's click - and closing gives it back to the window that had it. ``bar()``: the HUD's bar, which Ask is bound to
  while the HUD shows (the app's ``AskController.set_bar``).
* ``in_control()``: Phase 3's control session (none yet: never). Meanwhile both keys are ignored and nothing leaves
  the HUD - Esc or Stop first.
* While the HUD shows - and while either switch flies - nothing of Jarvis's comes forward by itself (hard rule 13):
  the window's set_tab only records the screen (ReadingView.hold_screens; the latest one wins when the window comes
  back), the window comes back only on the owner's own click or key - Bring Jarvis back, a chip, the tray's Bring
  Jarvis back, the toast, the window key (``bring_forward``: never a synthetic Alt) - and only once it shows at the
  end of the flight, and the time counts as present for the start-up (ReadingView.set_hud_present). The
  AppController's own checks ask ``is_hud()``.
* The chips mirror the window's: "Briefing ready" while a briefing is NEW, "N need your OK" with the approvals' count
  and REC with the meeting's time and warning, live while the HUD shows (ReadingView.glanceChanged); the orb takes
  the window's look, its recording ring too. "Briefing ready" goes back to Briefing, "N need your OK" to Approvals
  and REC to Meetings (``chip_screens``).
* The meetings RecPill (top centre, where the bar is) and the toast are handed to the HUD (``set_rec_pill``,
  ``set_toast``; the pill also when a recording starts while the HUD shows): the pill is held hidden meanwhile -
  from the switch's start until the window is back - REC is a chip - and nothing of Jarvis's is stacked over the bar.
* A close of the HUD's own window (none can come from Jarvis) brings the window back instead: Jarvis is never left
  with nothing on screen. Not while the application quits (``set_quitting``: Windows ends the session - the app's
  aboutToQuit): Qt's quit then closes every visible window and is refused while one will not close, so the HUD's
  close goes through and from then on nothing switches or comes forward; a switch on its way stops at once.
* ``shutdown()`` (Jarvis closing, next to the meetings') stops a switch on its way and hides and deletes the HUD's
  windows.

Logs: statuses, durations and screen numbers only.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

import shiboken6
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap, QRegion
from PySide6.QtWidgets import QWidget

from . import alive, dock, holo, hud_overlay
from . import orb as orb_mod
from .persona import with_address

logger = logging.getLogger(__name__)

BRING_BACK_TEXT = hud_overlay.BRING_BACK_TEXT      # the HUD's button and the tray's item while the HUD shows
HUD_CAPTION = "On your desktop, {sir}."            # the HUD's caption line as it opens
NO_ORB_PX = 112                                    # a window with no orb shown: the flight's orb comes from its centre
                                                   # at Switch.dc.html's side size


def show_window(window: Any) -> None:
    """The default way back (no Win32 here): shown (restored when minimised), raised and activated - never a key."""
    if window.isMinimized():
        window.showNormal()
    else:
        window.show()
    window.raise_()
    window.activateWindow()


def render(widget: QWidget, flags: QWidget.RenderFlag) -> QPixmap:
    """``widget`` as it paints - shown or hidden - in a transparent pixmap at its device pixel ratio (CardFlight's
    snapshot): with ``DrawChildren`` all of it, without only its own paint (the window: its ground and outline)."""
    dpr = max(1.0, widget.devicePixelRatioF())
    pixmap = QPixmap(max(1, round(widget.width() * dpr)), max(1, round(widget.height() * dpr)))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    widget.render(pixmap, QPoint(), QRegion(), flags)
    return pixmap


def is_blank(pixmap: QPixmap, step: int = 16) -> bool:
    """True when ``pixmap`` holds nothing (every pixel on a ``step`` px grid fully transparent)."""
    image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    for y in range(0, image.height(), step):
        for x in range(0, image.width(), step):
            if image.pixelColor(x, y).alpha():
                return False
    return True


class DesktopController(QObject):
    """See the module docstring."""

    hudChanged = Signal(bool)          # True: Jarvis went onto the desktop HUD; False: back in the window

    def __init__(self, config: Any, window: Any, overlay: hud_overlay.HudOverlay, *,
                 chip_screens: Mapping[str, int], home_screen: int,
                 may_enter: Callable[[], bool] | None = None,
                 bring_forward: Callable[[Any], None] | None = None,
                 address: str = "sir", flight_clock: Callable[[], float] | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.window = window
        self.reading = window.reading
        self.overlay = overlay
        self._chip_screens = dict(chip_screens)
        self._home = int(home_screen)
        self._may_enter = may_enter or (lambda: True)        # the app's side: its assistant screen is up
        self._bring_forward = bring_forward or show_window
        self._caption = with_address(HUD_CAPTION, address)
        self._hud = False
        self._switching = False
        self._shut = False
        self._quitting = False                               # the application quits: nothing switches any more
        self._return_screen: int | None = None               # asked for by an automatic set_tab on the HUD
        self._toast: Any = None
        self._rec_pill: Any = None
        self._flight: alive.WindowFlight | None = None       # the switch on its way (one per switch)
        self._spent: alive.WindowFlight | None = None        # the last one, deleted (deleteLater), its wrapper kept
        self._flight_screen: Any = None
        self._flight_clock = flight_clock or time.monotonic
        self._left_frame: QPixmap | None = None              # the window's frame as it left: the way back's fallback
        self._keyboard_wanted = False                        # the HUD key on the way in: the bar takes it as it shows
        self.reading.desktopRequested.connect(self.enter_hud)
        self.reading.glanceChanged.connect(self.sync_glance)
        hud = overlay.hud
        hud.bringBackClicked.connect(self._on_bring_back)
        hud.chipClicked.connect(self._on_chip)
        hud.shapeChanged.connect(self._on_hud_shape)
        hud.installEventFilter(self)

    # ---- state ----------------------------------------------------------------------------------

    def is_hud(self) -> bool:
        """Jarvis is on the desktop HUD: the window is hidden by Jarvis (never closed) and the HUD shows - from the
        moment the switch to it starts until the window shows again on the way back."""
        return self._hud

    def is_switching(self) -> bool:
        """A switch is on its way (its flight; the instant swap only within the call)."""
        return self._switching

    def flight(self) -> alive.WindowFlight | None:
        """The switch's flight while it flies (tests and the harness step it), else None."""
        return self._flight

    def set_flight_clock(self, clock: Callable[[], float]) -> None:
        """The clock the next flights run on (a fake clock in the tests and the harness)."""
        self._flight_clock = clock
        if self._flight is not None:
            self._flight.set_clock(clock)

    def return_screen(self) -> int:
        """Where the window comes back without a chip's screen: the screen an automatic change asked for while on
        the HUD, else Home."""
        return self._home if self._return_screen is None else self._return_screen

    def set_quitting(self) -> None:
        """The application quits (Windows ends the session: the app's aboutToQuit, which Qt emits before its quit):
        from now on a close of the HUD's window goes through - never refused, never a bring-back - so Qt's quit is
        not cancelled, and nothing switches or comes forward any more; a switch on its way stops at once."""
        if not self._quitting:
            self._quitting = True
            self._drop_flight()
            logger.info("Desktop: the application quits")

    def quitting(self) -> bool:
        return self._quitting

    def in_control(self) -> bool:
        """Jarvis is in control of an app (Phase 3: the amber pill, Esc or Stop to end it). Meanwhile the window key and
        the HUD key are ignored and nothing leaves the HUD (SPEC 1, 9). Phase 3's control session answers here; until
        then never."""
        return False

    # ---- the bar's keyboard (SPEC 8.3, 9) --------------------------------------------------------------

    def bar(self) -> Any:
        """The HUD's command bar (the app binds Ask to it while the HUD shows)."""
        return self.overlay.hud.bar

    def take_keyboard(self) -> bool:
        """The HUD key: the HUD's bar takes the keyboard (hud_overlay.HudWindow.take_keyboard) - at once while the HUD
        shows, or as soon as it shows when the switch to it is on its way. False when not on the HUD, on the way back,
        in control, shut or quitting."""
        if self._shut or self._quitting or not self._hud or self.in_control():
            return False
        if self.overlay.is_shown():
            return self.overlay.hud.take_keyboard("key")
        flight = self._flight
        if self._switching and flight is not None and flight.direction() == alive.TO_HUD:
            self._keyboard_wanted = True
            return True
        return False

    def has_keyboard(self) -> bool:
        """The HUD's bar has the keyboard (the next HUD key goes back to the window)."""
        return self._hud and self.overlay.is_shown() and self.overlay.hud.holds_keyboard()

    def _keyboard_on_show(self) -> None:
        """The HUD's windows have just shown: a HUD key pressed on the way in gives the bar the keyboard now."""
        if self._keyboard_wanted:
            self._keyboard_wanted = False
            self.overlay.hud.take_keyboard("key")

    # ---- THE CONSENT GATE (P1.9 plugs in here) -----------------------------------------------------

    def consent_given(self) -> bool:
        """THE consent gate: the one place that decides whether Jarvis may go onto the desktop now. Fail-closed:
        anything unknown, missing or failing is a no.

        P1.9 PLUGS IN HERE: the first-run "Jarvis on your desktop" card (SPEC 10.3: Not now / Turn on). Until the
        owner's Turn on is stored (a boolean in prefs) this answers False and that card shows instead; nothing
        desktop-side works before it. Until then (P1.4) it allows entry only while [desktop] is enabled."""
        try:
            return getattr(getattr(self.config, "desktop", None), "enabled", False) is True
        except Exception:  # noqa: BLE001 - unknown: no
            return False

    # ---- the switch ------------------------------------------------------------------------------------

    def enter_hud(self) -> bool:
        """The owner's Desktop (the Dock's or the Find list's): the window is hidden - never closed - and the HUD
        shows on the screen it was on, with the chips as the window has them; in flight unless motion is off (the
        module docstring). False when nothing changed: already on the HUD, during a switch, shut down or quitting,
        the assistant screen not up (or a dialog open), or the consent gate said no."""
        if self._shut or self._quitting or self._hud or self._switching:
            return False
        try:
            allowed = bool(self._may_enter())
        except Exception as exc:  # noqa: BLE001 - unknown: not now
            logger.warning("Desktop: the app could not say whether the HUD may show (%s)", type(exc).__name__)
            allowed = False
        if not allowed:
            logger.info("Desktop: the HUD waits (the assistant screen is not up)")
            return False
        if not self.consent_given():
            logger.info("Desktop: the HUD was refused (not turned on)")
            return False
        screen = hud_overlay.screen_for(self.window)         # the screen the window is on
        self._switching = True
        flying = False
        try:
            # On the HUD first, then hidden: the window's hide is then neither a minimise nor an absence, and an
            # automatic screen change from here on only records its screen.
            self._hud = True
            self._return_screen = None
            self.reading.hold_screens(self.record_screen)
            self.reading.set_hud_present(True)
            self.overlay.hud.set_caption(self._caption)
            self._apply_neighbours()
            self.sync_glance()
            flying = self._fly_to_hud(screen)
            if not flying:
                self.window.hide()                           # never close(): closing the window quits Jarvis
                self.overlay.show_on(screen)
        finally:
            if not flying:
                self._switching = False
        if flying and self._flight is not None:
            logger.info("Desktop: switching to the HUD (%d ms)", self._flight.total_ms())
        else:
            logger.info("Desktop: on the HUD (instant switch)")
        self.hudChanged.emit(True)
        return True

    def leave_hud(self, screen: int | None = None) -> bool:
        """Back to the window - Bring Jarvis back, a chip, the tray's item, the toast's click, the window key or the HUD
        key while the bar has the keyboard (SPEC 9): the bar lets the keyboard go, the HUD hides and the window comes
        back on ``screen`` (a chip's), else on the screen an automatic change asked for while on the HUD, else Home -
        brought forward on that click or key, never with a synthetic Alt, once it shows at the end of the flight. Not
        a restore: no greeting, no start-up. False when not on the HUD, during a switch, while the application quits
        (nothing comes forward then) or while Jarvis is in control (``in_control``: Esc or Stop first)."""
        if self._shut or self._quitting or not self._hud or self._switching:
            return False
        if self.in_control():
            logger.info("Desktop: Jarvis is in control; the window waits (Esc or Stop first)")
            return False
        target = self.return_screen() if screen is None else int(screen)
        self._keyboard_wanted = False
        self.overlay.hud.give_back_keyboard(False, "back to the window")    # the window takes it, on the owner's click
        self._switching = True
        flying = False
        try:
            flying = self._fly_to_window(target)
            if not flying:
                self.overlay.hide()                          # the meetings pill is let go (back at its next tick)
                self._hud = False
                self._left_frame = None
                self.reading.hold_screens(None)
                self.reading.set_tab(target)
                self._bring_forward(self.window)
                self.reading.set_hud_present(False)
        finally:
            if not flying:
                self._switching = False
        if flying and self._flight is not None:
            logger.info("Desktop: switching back to the window (screen %d, %d ms)", target, self._flight.total_ms())
            return True
        logger.info("Desktop: back in the window (screen %d)", target)
        self.hudChanged.emit(False)
        return True

    def record_screen(self, index: int) -> None:
        """An automatic set_tab while on the HUD (ReadingView.hold_screens): the window keeps its screen and comes
        back on this one."""
        if self._hud:
            self._return_screen = int(index)

    def _on_bring_back(self) -> None:
        self.leave_hud()

    def _on_chip(self, key: str) -> None:
        """A chip's click: the window back on its screen ("Briefing ready" Briefing, "N need your OK" Approvals,
        REC Meetings)."""
        screen = self._chip_screens.get(key)
        if screen is not None:
            self.leave_hud(screen)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        """A close of the HUD's window (nothing of Jarvis's asks for one) brings the window back instead - unless the
        application quits or the HUD is shut: then the close goes through and nothing is brought back."""
        if event.type() == QEvent.Type.Close and not (self._shut or self._quitting):
            event.ignore()
            QTimer.singleShot(0, self._on_bring_back)
            return True
        return False

    # ---- the flight (SPEC 8.2; alive.WindowFlight) ---------------------------------------------------------

    def _may_fly(self) -> bool:
        """The switch flies: motion is lively or calm ([ui] motion off or Windows' reduced motion: the instant swap)."""
        return holo.motion_level() != holo.MOTION_OFF

    def _fly_to_hud(self, screen: Any) -> bool:
        """Window -> HUD in flight: the HUD placed (still hidden), the window's snapshot and its orb's, the flight
        over the window and the HUD's reach; the window hides at the flight's first frame (``_on_flight_painted``)
        and the HUD's windows show once everything has landed (``_on_handover``). False: the instant swap - motion
        off, the window not shown or minimised, or the flight could not be set up (logged by its type)."""
        window = self.window
        if not self._may_fly() or not window.isVisible() or window.isMinimized():
            return False
        try:
            self.overlay.prepare(screen)
            scene, orb_widget = self._window_scene(content=True)
            self._left_frame = scene.frame
            flight = self._new_flight(screen)
            flight.handover.connect(self._on_handover)
            flight.to_hud(scene, self._flown_orb(orb_widget), self.overlay.flight_scene(),
                          self._flight_rect(scene.rect, screen), screen)
            self._exclude(flight)
        except Exception as exc:  # noqa: BLE001 - the instant swap instead
            logger.warning("Desktop: the switch could not fly (%s); instant instead", type(exc).__name__)
            self._drop_flight(keep_switching=True)
            return False
        return True

    def _fly_to_window(self, target: int) -> bool:
        """HUD -> window in flight: the HUD's pictures as it shows, the hidden window turned to ``target`` and
        rendered anew (blank: its frame as it left), the flight; the HUD hides at the flight's first frame (the pill
        stays held), the window shows under it once its frame is whole (``_window_back``). False: the instant swap -
        motion off, the HUD not shown or the window already shown, the HUD on another screen than the window's, or
        the flight could not be set up (logged by its type)."""
        screen = self.overlay.hud_screen()
        if (not self._may_fly() or not self.overlay.is_shown() or self.window.isVisible()
                or screen is not hud_overlay.screen_for(self.window)):
            return False
        try:
            hud = self.overlay.flight_scene()
            self.reading.hold_screens(None)
            self.reading.set_tab(target)                     # the hidden window turns to its screen ...
            self.reading.hold_screens(self.record_screen)    # ... and on the way an automatic change only records
            self._return_screen = None
            scene, orb_widget = self._window_scene(content=False)
            if is_blank(scene.frame) and self._left_frame is not None:
                scene.frame = self._left_frame
                logger.info("Desktop: the hidden window rendered blank; its frame as it left flies back")
            flight = self._new_flight(screen)
            flight.windowTime.connect(self._window_back)
            flight.to_window(hud, self._flown_orb(orb_widget), scene, self._flight_rect(scene.rect, screen), screen)
            self._exclude(flight)
        except Exception as exc:  # noqa: BLE001 - the instant swap instead
            logger.warning("Desktop: the switch could not fly (%s); instant instead", type(exc).__name__)
            self._drop_flight(keep_switching=True)
            return False
        return True

    def _new_flight(self, screen: Any) -> alive.WindowFlight:
        flight = alive.WindowFlight(clock=self._flight_clock)
        flight.painted.connect(self._on_flight_painted)
        flight.finished.connect(self._drop_flight)
        flight.interrupted.connect(self._on_flight_interrupted)
        self._flight, self._flight_screen = flight, screen
        return flight

    def _window_orb(self) -> tuple[QPointF, float, Any]:
        """The window's orb as it shows (or would show, hidden): its centre (global), its size and its widget - the
        largest orb on the screen shown (Home's hero, Briefing's), else the Dock's mini orb, else the window's centre
        (no widget)."""
        window = self.window
        widget: Any = None
        for orb in window.findChildren(orb_mod.OrbWidget):
            if orb.isVisibleTo(window) and orb.width() > 0 and (widget is None or orb.orb_size() > widget.orb_size()):
                widget = orb
        if widget is None:
            mini = getattr(self.reading.dock.button(dock.HOME), "mini", None)
            if mini is not None and mini.isVisibleTo(window):
                widget = mini
        if widget is None:
            return QPointF(window.geometry().center()), float(NO_ORB_PX), None
        center = (QPointF(window.geometry().topLeft()) + QPointF(widget.mapTo(window, QPoint(0, 0)))
                  + QPointF(widget.width() / 2, widget.height() / 2))
        px = widget.orb_size() if isinstance(widget, orb_mod.OrbWidget) else widget.width()
        return center, float(px), widget

    def _window_scene(self, content: bool) -> tuple[alive.WindowScene, Any]:
        """The window as the flight shows it: its frame (its own paint: the ground and the outline), with ``content``
        all of it, its rect and its orb; and that orb's widget."""
        center, px, widget = self._window_orb()
        window = self.window
        frame = render(window, QWidget.RenderFlag(0))
        full = render(window, QWidget.RenderFlag.DrawChildren) if content else None
        return alive.WindowScene(frame, full, QRect(window.geometry()), center, px), widget

    def _flown_orb(self, widget: Any) -> alive.FlownOrb:
        """The orb that flies: the window's own when it is a full one (Home's hero, Briefing's: never smaller than the
        HUD's, so it only ever shrinks), else the HUD's."""
        if isinstance(widget, orb_mod.OrbWidget) and widget.orb_size() >= hud_overlay.ORB_PX:
            return alive.FlownOrb(widget.snapshot(), float(widget.orb_size()))
        orb = self.overlay.hud.orb
        return alive.FlownOrb(orb.snapshot(), float(orb.orb_size()))

    def _flight_rect(self, window_rect: QRect, screen: Any) -> QRect:
        """The window's rect and the HUD's reach together, on the screen: never the whole screen."""
        rect = window_rect.united(self.overlay.reach())
        if screen is not None and shiboken6.isValid(screen):
            rect = rect.intersected(screen.geometry())
        return rect

    def _exclude(self, flight: alive.WindowFlight) -> None:
        """[desktop] hide_from_capture: the flight too is left out of capture, as every HUD window is."""
        if self.overlay.hides_from_capture():
            try:
                self.overlay.native().exclude_from_capture(int(flight.winId()))
            except Exception as exc:  # noqa: BLE001 - best effort
                logger.debug("Desktop: the flight's capture exclusion failed (%s)", type(exc).__name__)

    def _on_flight_painted(self) -> None:
        """The flight's first frame is up: what it stands in for goes - the window (hidden, never closed) or the HUD's
        windows (the meetings pill stays held until the window is back)."""
        flight = self._flight
        if flight is None:
            return
        if flight.direction() == alive.TO_HUD:
            if self._hud and self.window.isVisible():
                self.window.hide()                           # never close(): closing the window quits Jarvis
        else:
            self.overlay.hide(release=False)

    def _on_handover(self) -> None:
        """To the HUD: everything has landed - the HUD's own windows show (the orb live, the shadows and glows in their
        layer); the flight leaves them be from its next step."""
        if self._flight is None or self._shut or not self._hud:
            return
        if self.window.isVisible():
            self.window.hide()
        self.overlay.show_on(self._flight_screen)
        logger.info("Desktop: on the HUD")
        self._keyboard_on_show()

    def _window_back(self) -> None:
        """Back to the window: its frame is whole in the flight - the window shows there, under the flight (at
        opacity 0, then 1 at the flight's next step), brought forward on the owner's own click (never a synthetic
        Alt), its screen assembling (ReadingView.replay_screen); the flight then fades off it. From here it is no
        longer the HUD; an automatic change asked for on the way wins, as it would in the window."""
        if self._shut or not self._hud:
            return
        recorded = self._return_screen
        self._hud = False
        self._left_frame = None                              # the way back's fallback: no longer needed
        self.reading.hold_screens(None)
        if recorded is not None:
            self.reading.set_tab(recorded)
        window = self.window
        hidden = not window.isVisible()
        if hidden:
            window.setWindowOpacity(0.0)
        self._bring_forward(window)
        self.reading.replay_screen()
        if hidden and self._flight is not None:
            self._flight.after_tick(self._opaque_window)
        else:
            self._opaque_window()
        self.overlay.release()
        self.reading.set_hud_present(False)
        logger.info("Desktop: back in the window (screen %d)", self.reading.current_tab())
        self.hudChanged.emit(False)

    def _opaque_window(self) -> None:
        if shiboken6.isValid(self.window) and self.window.windowOpacity() < 1.0:
            self.window.setWindowOpacity(1.0)

    def _on_flight_interrupted(self) -> None:
        """Something else hid the flight before its end (nothing of Jarvis's does): the switch lands at once."""
        flight = self._flight
        if flight is not None and not self._shut:
            if flight.direction() == alive.TO_HUD:
                if self._hud:
                    if self.window.isVisible():
                        self.window.hide()
                    if not self.overlay.is_shown():
                        self.overlay.show_on(self._flight_screen)
                    self._keyboard_on_show()
            elif self._hud:
                if self.overlay.is_shown():
                    self.overlay.hide(release=False)
                self._window_back()
        self._drop_flight()

    def _drop_flight(self, keep_switching: bool = False) -> None:
        """The flight is over or stops at once (shutdown, the application quitting): hidden and deleted - no window,
        no timer left - the window never left see-through, and the switch over (unless the caller ends it)."""
        flight, self._flight = self._flight, None
        if not keep_switching:
            self._switching = False
        if flight is not None and shiboken6.isValid(flight):
            flight.stop()                                    # stopped, it signals nothing more
            flight.deleteLater()
            # Its wrapper is kept until the next switch: a window made here with no parent is the wrapper's, and
            # letting go of it now (often from the flight's own timer) would destroy it there - not at deleteLater's.
            self._spent = flight
        self._opaque_window()

    def _on_hud_shape(self) -> None:
        """The HUD's window changed shape (its caption's lines, a chip) while it flies in: the flight takes it anew."""
        self._refresh_flight()

    def _refresh_flight(self) -> None:
        flight = self._flight
        if (flight is not None and flight.is_running() and flight.direction() == alive.TO_HUD
                and not self.overlay.is_shown()):
            flight.set_hud(self.overlay.flight_scene())

    # ---- the chips and the neighbours ------------------------------------------------------------------

    def sync_glance(self) -> None:
        """While the HUD shows (and as it opens, its flight too): its chips and orb as the window has them - "Briefing
        ready" while a briefing is NEW, "N need your OK", REC with the time and its warning, and the orb's look with
        the window's recording ring while a meeting records (still: no loop of its own)."""
        if self._shut or not self._hud:
            return
        reading, win = self.reading, self.overlay.hud
        if not (shiboken6.isValid(reading) and shiboken6.isValid(win)):
            return
        win.set_briefing_ready(reading.is_new_briefing())
        win.set_pending(reading.pending_count())
        elapsed, warning = reading.recording_state()
        win.set_recording(elapsed, warning)
        win.orb.set_recording(bool(elapsed))
        base, muted, offline = reading.look()
        win.set_look(base, muted=muted, offline=offline)
        self._refresh_flight()                               # the chips arrive with the counts as they are now

    def set_toast(self, toast: Any) -> None:
        """Jarvis's toast (made at its first post): the HUD keeps it under its bar."""
        self._toast = toast
        self._apply_neighbours()

    def set_rec_pill(self, pill: Any) -> None:
        """The meetings RecPill (made when a recording starts, also while the HUD shows): held hidden while the HUD
        shows - REC is a chip there - and back when it hides."""
        self._rec_pill = pill
        self._apply_neighbours()

    def _apply_neighbours(self) -> None:
        if not self._shut:
            self.overlay.set_neighbours(toast=self._toast, rec_pill=self._rec_pill)

    # ---- closing --------------------------------------------------------------------------------------

    def shutdown(self) -> None:
        """Jarvis is closing (AppController.shutdown, next to the meetings'): a switch on its way stops at once and the
        HUD's windows are hidden and deleted. The window is not touched here - the app hides it; nothing ever closes
        it."""
        if self._shut:
            return
        self._keyboard_wanted = False
        if shiboken6.isValid(self.overlay.hud):
            self.overlay.hud.give_back_keyboard(True, "Jarvis closes")      # the window that had it gets it back
        self._shut = True
        self._drop_flight()
        self._hud = False
        if shiboken6.isValid(self.reading):
            self.reading.hold_screens(None)
            self.reading.set_hud_present(False)
        self.overlay.shutdown()
