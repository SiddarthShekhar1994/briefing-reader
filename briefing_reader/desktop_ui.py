"""Jarvis on your desktop: the app's side of the HUD (SPEC sections 1, 8.3 and 12; Phase 1, part 4: the instant switch).

The AppController makes one ``DesktopController`` while [desktop] is on (none while it is off: no HUD window, no
hook). It keeps the owner's rules:

* The HUD is a sub-state of the assistant screen (STATE_READING). ``enter_hud()`` HIDES the window - never closes it
  (closing the window quits Jarvis) - and shows the HUD's windows (hud_overlay.HudOverlay) on the screen the window
  is on. ``leave_hud(screen)`` hides the HUD and brings the window back on ``screen`` (a chip's), else on the screen
  an automatic change asked for meanwhile, else Home. ``is_hud()`` says which. The switch is instant here (the
  animation is P1.5); a second enter or leave, or one during the switch, changes nothing.
* Only the owner starts it: the Dock's Desktop or the Find list's (``desktopRequested``); later the HUD key (P1.6)
  and a desktop request typed in the window's bar (P1.7). Every entry passes THE CONSENT GATE (``consent_given``,
  fail-closed), where P1.9 plugs in the first-run "Jarvis on your desktop" card.
* While the HUD shows nothing of Jarvis's comes forward by itself (hard rule 13): the window's set_tab only records
  the screen (ReadingView.hold_screens), the window comes back only on the owner's own click or key - Bring Jarvis
  back, a chip, the tray's Bring Jarvis back, the toast, the window key (``bring_forward``: never a synthetic Alt) -
  and the time counts as present for the start-up (ReadingView.set_hud_present). The AppController's own checks
  ask ``is_hud()``.
* The chips mirror the window's: "Briefing ready" while a briefing is NEW, "N need your OK" with the approvals' count
  and REC with the meeting's time and warning, live while the HUD shows (ReadingView.glanceChanged); the orb takes
  the window's look, its recording ring too. "Briefing ready" goes back to Briefing, "N need your OK" to Approvals
  and REC to Meetings (``chip_screens``).
* The meetings RecPill (top centre, where the bar is) and the toast are handed to the HUD (``set_rec_pill``,
  ``set_toast``; the pill also when a recording starts while the HUD shows): the pill is held hidden meanwhile -
  REC is a chip - and nothing of Jarvis's is stacked over the bar.
* A close of the HUD's own window (none can come from Jarvis) brings the window back instead: Jarvis is never left
  with nothing on screen. Not while the application quits (``set_quitting``: Windows ends the session - the app's
  aboutToQuit): Qt's quit then closes every visible window and is refused while one will not close, so the HUD's
  close goes through and from then on nothing switches or comes forward.
* ``shutdown()`` (Jarvis closing, next to the meetings') hides and deletes the HUD's windows.

Logs: statuses and screen numbers only.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

import shiboken6
from PySide6.QtCore import QEvent, QObject, QTimer, Signal

from . import hud_overlay
from .persona import with_address

logger = logging.getLogger(__name__)

BRING_BACK_TEXT = hud_overlay.BRING_BACK_TEXT      # the HUD's button and the tray's item while the HUD shows
HUD_CAPTION = "On your desktop, {sir}."            # the HUD's caption line as it opens


def show_window(window: Any) -> None:
    """The default way back (no Win32 here): shown (restored when minimised), raised and activated - never a key."""
    if window.isMinimized():
        window.showNormal()
    else:
        window.show()
    window.raise_()
    window.activateWindow()


class DesktopController(QObject):
    """See the module docstring."""

    hudChanged = Signal(bool)          # True: Jarvis went onto the desktop HUD; False: back in the window

    def __init__(self, config: Any, window: Any, overlay: hud_overlay.HudOverlay, *,
                 chip_screens: Mapping[str, int], home_screen: int,
                 may_enter: Callable[[], bool] | None = None,
                 bring_forward: Callable[[Any], None] | None = None,
                 address: str = "sir", parent: QObject | None = None) -> None:
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
        self.reading.desktopRequested.connect(self.enter_hud)
        self.reading.glanceChanged.connect(self.sync_glance)
        hud = overlay.hud
        hud.bringBackClicked.connect(self._on_bring_back)
        hud.chipClicked.connect(self._on_chip)
        hud.installEventFilter(self)

    # ---- state ----------------------------------------------------------------------------------

    def is_hud(self) -> bool:
        """Jarvis is on the desktop HUD: the window is hidden by Jarvis (never closed) and the HUD shows."""
        return self._hud

    def return_screen(self) -> int:
        """Where the window comes back without a chip's screen: the screen an automatic change asked for while on
        the HUD, else Home."""
        return self._home if self._return_screen is None else self._return_screen

    def set_quitting(self) -> None:
        """The application quits (Windows ends the session: the app's aboutToQuit, which Qt emits before its quit):
        from now on a close of the HUD's window goes through - never refused, never a bring-back - so Qt's quit is
        not cancelled, and nothing switches or comes forward any more."""
        if not self._quitting:
            self._quitting = True
            logger.info("Desktop: the application quits")

    def quitting(self) -> bool:
        return self._quitting

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

    # ---- the switch (instant; the animation is P1.5) -------------------------------------------------

    def enter_hud(self) -> bool:
        """The owner's Desktop (the Dock's or the Find list's): the window is hidden - never closed - and the HUD
        shows on the screen it was on, with the chips as the window has them. False when nothing changed: already
        on the HUD, during a switch, shut down or quitting, the assistant screen not up (or a dialog open), or the
        consent gate said no."""
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
            self.window.hide()                               # never close(): closing the window quits Jarvis
            self.overlay.show_on(screen)
        finally:
            self._switching = False
        logger.info("Desktop: on the HUD (instant switch)")
        self.hudChanged.emit(True)
        return True

    def leave_hud(self, screen: int | None = None) -> bool:
        """Back to the window - Bring Jarvis back, a chip, the tray's item, the toast's click, the window key (SPEC 9's
        HUD row; the rest of that table is P1.6): the HUD hides and the window comes back on ``screen`` (a chip's),
        else on the screen an automatic change asked for while on the HUD, else Home - brought forward on that
        click, never with a synthetic Alt. Not a restore: no greeting, no start-up. False when not on the HUD,
        during a switch, or while the application quits (nothing comes forward then). (Phase 3: never while Jarvis
        is in control - Stop first.)"""
        if self._shut or self._quitting or not self._hud or self._switching:
            return False
        target = self.return_screen() if screen is None else int(screen)
        self._switching = True
        try:
            self.overlay.hide()                              # the meetings pill is let go (back at its next tick)
            self._hud = False
            self.reading.hold_screens(None)
            self.reading.set_tab(target)
            self._bring_forward(self.window)
            self.reading.set_hud_present(False)
        finally:
            self._switching = False
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

    # ---- the chips and the neighbours ------------------------------------------------------------------

    def sync_glance(self) -> None:
        """While the HUD shows (and as it opens): its chips and orb as the window has them - "Briefing ready" while
        a briefing is NEW, "N need your OK", REC with the time and its warning, and the orb's look with the window's
        recording ring while a meeting records (still: no loop of its own)."""
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
        """Jarvis is closing (AppController.shutdown, next to the meetings'): the HUD's windows are hidden and
        deleted. The window is not touched here - the app hides it; nothing ever closes it."""
        if self._shut:
            return
        self._shut = True
        self._hud = False
        if shiboken6.isValid(self.reading):
            self.reading.hold_screens(None)
            self.reading.set_hud_present(False)
        self.overlay.shutdown()
