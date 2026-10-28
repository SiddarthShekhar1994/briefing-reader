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
  agent's "desktop" message; AppController.handle_activation decides by SPEC 9's table) and a desktop request typed
  in the window's bar (P1.7). Every entry passes THE CONSENT GATE (``consent_given``, fail-closed: [desktop] enabled
  AND the owner's Turn on on the first-run "Jarvis on your desktop" card, stored in the app's prefs - anything
  unknown, unreadable or failing is a no). Until then nothing desktop-side works: the Dock's Desktop (and the Find
  list's) emit ``consentNeeded(ENTRY_DOCK)`` instead of switching - the app shows that card in the window (the HUD
  key, a --hud start and a typed request show it too, through the app) - and ``request()`` and the result cards'
  clicks refuse. ``turn_on()`` stores the owner's Turn on (it counts only once saved), ``turn_off()`` (Settings >
  Desktop) takes it back; ``consentChanged(bool)`` follows. A Turn off stops what is on its way at once (P1.R): every
  desktop job - a request being looked up, a card's click, a planner run's carry-out - asks the gate again before
  each step and right before each OS call (``_Gate``: fail-closed, and set for good by a Turn off after the job
  started), so nothing opens after it, and a request looked up meanwhile starts no planner run.
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
* Desktop requests (P1.7, P1.8; SPEC 3 and 6.4): ``request(words, planner=)`` - a request typed on the HUD's bar
  that ask.routes calls a desktop request - goes to the "desktop" thread (desktop.worker: searches and opens off the
  GUI thread, one at a time; desktop.service does the work there, its backend made there at the first request -
  ``backend_factory``, the real Win32Backend in the app, which refuses to be made while the tests run). There it is
  resolved: a local route (desktop.routes: "open Downloads", "open <app>", "open <https address>", "find <words>")
  is carried out at once - re-checked right before its OS call (desktop.executor) - and comes back by signal
  (``answered``: Jarvis's own words for what happened, with its LIVE task "Handled on this PC"); anything else comes
  back as the planner's job (``plannerNeeded``: ask.desktop_mode.DesktopJob with this PC's candidates - names only
  reach the planner - and ``carry_out``, through which the Ask thread has the checked plan carried out on the
  desktop thread). Meanwhile ``busy()`` is true and a second request is refused. ``context_source(words)``, when
  set, stands in for this PC's candidates (the tests' invented ones).
* What came of a request (``report_plan`` for the planner's outcome, or a local route) is kept in memory:
  ``last_plan``, ``last_run`` (desktop.executor.DesktopRun), ``pending`` (the ONE-CLICK and CARD steps held for the
  owner's click) and ``found`` (what a find found). INSTANT steps have opened by then (owner decision D5: a file's
  best match opens at once; the other matches are kept with it).
* Results on the desktop (P1.9; desktop_results.ResultCards, ``results``): the run's cards (DesktopRun.shown, made on
  the desktop thread right after it) float beside what they are about - "Opened LING essay draft.docx in Word.
  Nothing else changed." beside Word's window with a cyan bracket, its tag and a tether, the other matches as "Open
  this instead" rows; a held step's card with Open; what a find found. A new request takes the last ones away, and
  so does leaving the HUD. Cards that come while Jarvis is not on the HUD - in the window, or with the switch back on
  its way - are held as data only (no card window, no window followed meanwhile) and show when the HUD next shows:
  a step that waits for the owner's click or OK keeps its card, as the words say (P1.R). A card's click
  (``clickRequested``) is carried out on the desktop thread (desktop.service.click: the card's OWN item re-checked by
  desktop.executor.run_click, at most the tier the card stood for) - one at a time, ``busy()`` meanwhile; one that
  takes more than RESOLVE_TIMEOUT_S is stopped, so it opens nothing after "I stopped" - and what came of it updates
  the cards, the caption (``answered``, in its tone: amber only while a decision waits, coral when it was not done -
  hard rule 17) and LIVE ("Approved by your click 10:22 AM", each item and its result). Not now takes a card away and
  carries nothing out. ``window_api``: the window seam the cards' winwatch uses (tests pass a fake; by default
  winwatch.default_api(), which does nothing while the tests run).

* P2.3, the NEEDS YOUR OK card of file steps (desktop.tidy; hud_overlay.DesktopCard through ``results``): a request's
  file steps wait on ONE card that shows every row of their frozen plan - ticks, the rows' flags in words, Not now and
  "Move 5 files" ("Nothing ticked", disabled, while nothing is). The card's state is kept here (``tidy()``), the
  window is only its view: on the HUD it shows beside the File Explorer window of its folder (a cyan bracket, its tag,
  a tether; none open: under the HUD); going back to the window drops the view - no card window, no hook - and keeps
  the state, which shows again with the HUD. While it waits it counts in the HUD's "N need your OK" and the window's
  Approvals shows "Waiting on your desktop - Show me" (``waitingChanged(count)``, ``waiting_count()``). Its click
  passes the app's click guards (``click_guard``: AppController's _decision_click_too_soon and _locked_out, with a
  "dsk:" id) and THE CONSENT GATE again, then the desktop thread carries exactly the ticked rows of the plan as shown
  out (desktop.service.carry_out_card: cardplan.carry_out - expired or changed: nothing runs, said plainly) - never
  the calendar's worker; "Moving 2 of 5" as each row ends; Stop on the card or Esc anywhere (desktop.stopkeys: held
  only while the batch acts - ``stop_keys``, tests pass a fake) stops it after the row on its way, and so does a Turn
  off; then the stamp. Its Undo (the owner's click only) puts the items back on the desktop thread, newest first,
  each re-checked (desktop.service.undo_card), with the partial stamp ("PUT BACK 4 OF 5 - 1 MOVED SINCE"). LIVE gets
  each click's task: "Approved by your click 10:22 AM", each row's kind and result, the stop's reason and how long it
  took - kinds, counts and codes only. ``journal_folder``: where the runs' journals go (the app's own folder by
  default; tests pass a temporary one). A new request drops a card that is not at work; Turn off drops it.

Logs: statuses, durations, screen numbers, run ids, skill kinds, counts and refusal codes only - never what was
typed, a name or a path.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from concurrent.futures import CancelledError, Future
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta
from typing import Any

import shiboken6
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap, QRegion
from PySide6.QtWidgets import QWidget

from . import alive, desktop_results, dock, holo, hud_overlay
from . import live as live_events
from . import orb as orb_mod
from .ask import desktop_mode
from .desktop import executor as desktop_executor
from .desktop import results as card_results
from .desktop import journal as desktop_journal
from .desktop import routes as desktop_routes
from .desktop import service as desktop_service
from .desktop import stopkeys
from .desktop import tidy as desktop_tidy
from .desktop import winwatch
from .desktop.context import DesktopContext
from .desktop.worker import DesktopWorker
from .hud import TONE_DONE, TONE_ERROR, TONE_WARN
from .persona import clock_text, with_address

logger = logging.getLogger(__name__)

BRING_BACK_TEXT = hud_overlay.BRING_BACK_TEXT      # the HUD's button and the tray's item while the HUD shows
HUD_CAPTION = "On your desktop, {sir}."            # the HUD's caption line as it opens
NO_ORB_PX = 112                                    # a window with no orb shown: the flight's orb comes from its centre
                                                   # at Switch.dc.html's side size
LOOKING_TEXT = "Looking on this PC..."             # the caption while a request is resolved on the desktop thread
BUSY_TEXT = "Jarvis is still looking on this PC; try again in a moment"   # a second request meanwhile
UNAVAILABLE_TEXT = "I can't look on this PC right now, {sir} - nothing was opened."
TOO_LONG_TEXT = "Looking on this PC took too long, {sir} - I stopped."
RESOLVE_TIMEOUT_S = 30.0                           # a request is waited for this long at most (then nothing opens)
CARRY_OUT_TIMEOUT_S = 60.0                         # the Ask thread waits this long for a plan's steps at most
CLICK_TOO_LONG_TEXT = "That took too long, {sir} - I stopped."
TURNED_OFF_TEXT = "Jarvis on your desktop was turned off, {sir} - nothing was opened."   # a request on its way then
# Where the owner asked to go onto the desktop from (the first-run card goes on to it after his Turn on).
ENTRY_DOCK, ENTRY_KEY, ENTRY_START, ENTRY_REQUEST = "dock", "key", "start", "request"
ENTRIES = (ENTRY_DOCK, ENTRY_KEY, ENTRY_START, ENTRY_REQUEST)
CLICK_TASK_TITLE = "{button} - your click on a result card"
APPROVED_TEXT = "Approved by your click {time}"
CARD_TASK_TITLE = "{button} - your click on a NEEDS YOUR OK card"     # P2.3: the tidy card's click in LIVE
UNDO_TASK_TITLE = "Undo - your click on the card's stamp"
STOP_WORDS = {"esc": "Esc", "stop": "Stop on the card", "off": "Turned off"}
TIDY_ID = "dsk:"                                   # the click guards' id namespace for desktop cards (SPEC 12)


def _real_backend() -> Any:
    """The app's backend: the real one (on the desktop thread; it refuses to be made while the tests run)."""
    from .desktop.backend import Win32Backend

    return Win32Backend()


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


LOCAL_ROUTE_TEXT = "on this PC - no planner run"


@live_events.quiet(None)
def _local_task(live: Any, words: str, found: Any, run: Any, said: str, soft: bool) -> Any:
    """A local route's LIVE task (TASK_DESKTOP; never opens LIVE by itself): what was typed and the route ("on this
    PC"), the step carried out (its skill, tier, status and code: "Carrying it out"), and its skill, check and
    Jarvis's words - never a name of an app, a file or a site, a path or an address. Finished. None without the LIVE
    view."""
    if live is None:
        return None
    task = live.task(live_events.TASK_DESKTOP, words or "Desktop request")
    if task is live_events.NO_TASK:
        return None
    step = task.step(live_events.DESKTOP_REQUEST, "Your request", fields=[("You typed", words or "-"),
                                                                          ("Route", LOCAL_ROUTE_TEXT)])
    step.done(live_events.STATUS_OK, summary="handled on this PC")
    if run is not None and run.results and not found.code:
        desktop_mode.run_step(task, run)
    summary = "not yet possible" if found.code else desktop_mode.run_summary(run)
    result = task.step(live_events.DESKTOP_PLAN, "What Jarvis does with it",
                       fields=[("Step", found.kind), ("Check", f"refused: {found.code}" if found.code else found.tier),
                               ("Jarvis says", said)])
    result.done(live_events.STATUS_WARN if soft else live_events.STATUS_OK, summary=summary)
    task.finish(live_events.STATUS_WARN if soft else live_events.STATUS_OK, summary="handled on this PC - " + summary)
    return task


@live_events.quiet(None)
def _click_task(live: Any, button: str, run: Any, said: str, soft: bool, approved_at: str) -> Any:
    """A result card's click in LIVE (TASK_DESKTOP; SPEC 10.1; never opens LIVE by itself): "Approved by your click
    10:22 AM" with the button's words, each item carried out (its skill, tier, status and code: "Carrying it out")
    and Jarvis's words - never a name, a path or an address. None without the LIVE view."""
    if live is None:
        return None
    task = live.task(live_events.TASK_DESKTOP, CLICK_TASK_TITLE.format(button=button))
    if task is live_events.NO_TASK:
        return None
    approved = APPROVED_TEXT.format(time=approved_at)
    step = task.step(live_events.DESKTOP_REQUEST, "Your click", fields=[("You clicked", button),
                                                                        ("Approved", approved)])
    step.done(live_events.STATUS_OK, summary=approved)
    if run is not None and run.results:
        desktop_mode.run_step(task, run)
    summary = desktop_mode.run_summary(run)
    result = task.step(live_events.DESKTOP_PLAN, "What Jarvis does with it", fields=[("Jarvis says", said)])
    result.done(live_events.STATUS_WARN if soft else live_events.STATUS_OK, summary=summary)
    task.finish(live_events.STATUS_WARN if soft else live_events.STATUS_OK, summary=f"{approved} - {summary}")
    return task


def _row_line(card: Any, end: Any, undo: bool = False) -> str:
    """A row's line in LIVE: its number, kind and end - never a name or a path."""
    row = card.row(end.number) if card is not None else None
    kind = row.kind if row is not None else "item"
    if undo:
        state = "put back" if end.state == desktop_journal.UNDONE else f"left: {end.reason or 'not put back'}"
    else:
        state = end.state + (f": {end.reason}" if end.reason else "")
    return f"Row {end.number} - {kind} - {state}"


@live_events.quiet(None)
def _tidy_task(live: Any, button: str, state: Any, said: str, soft: bool, approved_at: str, stop: str,
               stop_ms: int, undo: bool) -> Any:
    """A NEEDS YOUR OK card's click in LIVE (TASK_DESKTOP; SPEC 10.1): "Approved by your click 10:22 AM", each row
    carried out (or put back) with its kind and result, the stop's reason and how long it took to stop, and Jarvis's
    words - never a name or a path. None without the LIVE view."""
    if live is None:
        return None
    task = live.task(live_events.TASK_DESKTOP, UNDO_TASK_TITLE if undo else CARD_TASK_TITLE.format(button=button))
    if task is live_events.NO_TASK:
        return None
    approved = APPROVED_TEXT.format(time=approved_at)
    step = task.step(live_events.DESKTOP_REQUEST, "Your click", fields=[("You clicked", button),
                                                                        ("Approved", approved)])
    step.done(live_events.STATUS_OK, summary=approved)
    card = state.card
    if undo:
        outcome = state.undo
        work = task.step(live_events.DESKTOP_UNDO, "Putting it back",
                         fields=[("Order", "newest first, each checked again"),
                                 ("Result", outcome.stamp if outcome is not None and not outcome.code else
                                  f"refused: {outcome.code if outcome is not None else 'unknown'}")])
        number_of = {end.seq: number for number, end in state.ends.items()}
        for end in (outcome.rows if outcome is not None else ()):
            shown = desktop_tidy.RowEnd(number_of.get(end.seq, 0), end.state, end.reason, end.seq)
            work.item(_row_line(card, shown, undo=True),
                      status=live_events.STATUS_OK if end.state == desktop_journal.UNDONE else live_events.STATUS_WARN)
    else:
        outcome = state.outcome
        checked = f"refused: {outcome.code}" if outcome is not None and outcome.code else \
            f"{len(state.chosen)} of {len(card.rows)} row(s) ticked; each checked again right before it"
        work = task.step(live_events.DESKTOP_STEP, "Carrying it out", fields=[("Checked again", checked)])
        for number in state.chosen:
            end = state.ends.get(number)
            if end is not None:
                done = end.state == desktop_journal.DONE
                work.item(_row_line(card, end), status=live_events.STATUS_OK if done else live_events.STATUS_WARN)
    work.done(live_events.STATUS_WARN if soft else live_events.STATUS_OK, summary=said)
    if stop:
        stopped = task.step(live_events.DESKTOP_STOP, "Stopped",
                            fields=[("Why", STOP_WORDS.get(stop, stop)), ("Took", f"{stop_ms} ms to stop")])
        stopped.done(live_events.STATUS_WARN, summary=f"stopped by {STOP_WORDS.get(stop, stop)}")
    result = task.step(live_events.DESKTOP_PLAN, "What Jarvis does with it", fields=[("Jarvis says", said)])
    result.done(live_events.STATUS_WARN if soft else live_events.STATUS_OK, summary=said)
    task.finish(live_events.STATUS_WARN if soft else live_events.STATUS_OK, summary=f"{approved} - {said}")
    return task


class _Either:
    """Set as soon as any of its events is (the executor only asks ``is_set()`` before each step and right before each
    OS call): the owner's Cancel, the app giving up waiting, or THE CONSENT GATE's no (``_Gate``)."""

    def __init__(self, *events: Any) -> None:
        self._events = [event for event in events if event is not None]

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)


class _Gate:
    """THE CONSENT GATE as a job's stop (P1.R): set once the owner's Turn off came after the job started
    (``turned_off``: an event the Turn off sets - a Turn on again later does not undo it), and whenever the gate,
    asked anew each time (``allowed``: DesktopController.consent_given), says no - fail-closed: anything failing is a
    no. The executor asks it before each step and right before each OS call."""

    def __init__(self, allowed: Callable[[], bool], turned_off: threading.Event) -> None:
        self._allowed = allowed
        self._turned_off = turned_off

    def is_set(self) -> bool:
        if self._turned_off.is_set():
            return True
        try:
            return self._allowed() is not True
        except Exception:  # noqa: BLE001 - unknown: no
            return True


def run_tone(run: Any, plan: Any = None) -> str:
    """The caption's tone for what a run came to (hard rule 17): amber (TONE_WARN) only while a decision waits - a
    step held for the owner's OK on a NEEDS YOUR OK card -; coral (TONE_ERROR) when something was refused, failed or
    cancelled, or nothing was carried out (desktop.executor.run_soft); else the done tone."""
    if desktop_executor.run_waits(run):
        return TONE_WARN
    return TONE_ERROR if desktop_executor.run_soft(run, plan) else TONE_DONE


class _Bridge(QObject):
    """The desktop thread's results, delivered on the GUI thread (connected queued)."""

    resolved = Signal(int, object)          # the request's number, the Future of its desktop_service.Resolved
    clicked = Signal(int, object)           # a card click's number, the Future of its desktop.executor.DesktopRun
    tidy_started = Signal(int, bool)        # the tidy card's job began acting; Esc is held for it
    tidy_row = Signal(int, object, int, int)    # a row ended (desktop.tidy.RowEnd), rows ended, rows in all
    tidy_esc = Signal(int)                  # Esc was pressed during the job
    tidy_done = Signal(int, object)         # the job's Future (desktop.tidy.CardOutcome / UndoOutcome)


def _deliver(bridge: _Bridge, seq: int, future: Future, signal: str = "resolved") -> None:
    """A Future's end (on the desktop thread, or at once) as the bridge's signal - never after the bridge is gone."""
    try:
        if shiboken6.isValid(bridge):
            getattr(bridge, signal).emit(seq, future)
    except RuntimeError:                    # the app is closing: nobody waits for it any more
        pass


def _emit(bridge: _Bridge, signal: str, *args: Any) -> None:
    """A signal of the bridge from the desktop thread (or Esc's) - never after the bridge is gone."""
    try:
        if shiboken6.isValid(bridge):
            getattr(bridge, signal).emit(*args)
    except RuntimeError:                    # the app is closing
        pass


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
    # A request handled without the planner: Jarvis's own words, their tone (hard rule 17: TONE_DONE; TONE_WARN -
    # amber - only while a decision waits; TONE_ERROR - coral - when it was not done: refused, failed, stopped, too
    # long, turned off), the owner's words and whether they were taken (handled on this PC: they leave the bar; not
    # handled: they stay).
    answered = Signal(str, str, str, bool)
    # A request for the planner: its job (ask.desktop_mode.DesktopJob; None when the planner may not run) and the
    # owner's words.
    plannerNeeded = Signal(object, str)
    consentNeeded = Signal(str)        # the owner asked to go onto the desktop before his Turn on: the first-run card
    consentChanged = Signal(bool)      # his Turn on (True) or Turn off (False) was stored
    waitingChanged = Signal(int)       # NEEDS YOUR OK cards of file steps waiting for him (P2.3: Approvals' reminder)

    def __init__(self, config: Any, window: Any, overlay: hud_overlay.HudOverlay, *,
                 chip_screens: Mapping[str, int], home_screen: int,
                 may_enter: Callable[[], bool] | None = None,
                 bring_forward: Callable[[Any], None] | None = None,
                 address: str = "sir", flight_clock: Callable[[], float] | None = None,
                 live: Any = None, backend_factory: Callable[[], Any] | None = None,
                 consent_store: Callable[[], Any] | None = None, window_api: Any = None,
                 click_guard: Callable[[str], bool] | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.window = window
        self.reading = window.reading
        self.overlay = overlay
        self.live = live                                     # the app's live.LiveStream (None: no LIVE view)
        self._chip_screens = dict(chip_screens)
        self._home = int(home_screen)
        self._may_enter = may_enter or (lambda: True)        # the app's side: its assistant screen is up
        self._bring_forward = bring_forward or show_window
        self._address = address
        self._caption = with_address(HUD_CAPTION, address)
        # THE CONSENT GATE's store: the app's prefs (prefs.AssistantPrefs: desktop_turned_on and its setter), asked
        # at each check; None - no store - is a no.
        self._consent_store = consent_store
        self._hour24 = bool(getattr(getattr(config, "display", None), "hour24", False))
        # The result cards (P1.9) and the window seam their winwatch uses (made at the first card that needs it).
        self._window_api = window_api
        self.results = desktop_results.ResultCards(overlay, watch_factory=self._new_watch, hour24=self._hour24,
                                                   parent=self)
        self.results.clickRequested.connect(self._on_card_click)
        self.results.tidyClicked.connect(self._on_tidy_click)
        self.results.tidyTicked.connect(self._on_tidy_tick)
        # P2.3: the NEEDS YOUR OK card of file steps - its state (the window is only its view), the job carrying it
        # out or putting it back (number, "batch" / "undo", its stop, THE CONSENT GATE, the button's words, when the
        # stop was asked), the app's click guards, the Esc seam (made at the first batch) and the journals' folder.
        self._tidy: desktop_tidy.TidyState | None = None
        self._tidy_job: list[Any] | None = None
        self._tidy_seq = 0
        self._approved_at = ""
        self._click_guard = click_guard
        self.stop_keys: Any = None
        self.journal_folder: Callable[[], Any] | None = None
        self._click_seq = 0
        # The click being carried out: (number, card entry, key, the windows before it, the button's words, its stop -
        # set when the app stops waiting: nothing opens for it any more).
        self._clicking: tuple[int, Any, str, frozenset[int], str, threading.Event] | None = None
        self._stopped_click: tuple[int, Any, str, frozenset[int], str, threading.Event] | None = None
        self._before: frozenset[int] = frozenset()           # the windows as the last request started
        # Set by the owner's Turn off (then a fresh one): every desktop job started before it opens nothing more.
        self._turned_off = threading.Event()
        # A request's result cards that came while Jarvis was not on the HUD (in the window, or the switch back on its
        # way): (its run, the windows before it) - data only, shown when the HUD next shows.
        self._held: tuple[Any, frozenset[int]] | None = None
        # Desktop requests (P1.8): the desktop thread and its service, made at the first request; the backend made
        # there (``backend_factory``; the real one by default). ``context_source(words)``: the tests' stand-in for
        # this PC's candidates (None: this PC's, from the desktop thread).
        self._backend_factory = backend_factory or _real_backend
        self._worker: DesktopWorker | None = None
        self._service: desktop_service.DesktopService | None = None
        self._bridge = _Bridge(self)
        self._bridge.resolved.connect(self._on_resolved, Qt.ConnectionType.QueuedConnection)
        self._bridge.clicked.connect(self._on_clicked, Qt.ConnectionType.QueuedConnection)
        self._bridge.tidy_started.connect(self._on_tidy_started, Qt.ConnectionType.QueuedConnection)
        self._bridge.tidy_row.connect(self._on_tidy_row, Qt.ConnectionType.QueuedConnection)
        self._bridge.tidy_esc.connect(self._on_tidy_esc, Qt.ConnectionType.QueuedConnection)
        self._bridge.tidy_done.connect(self._on_tidy_done, Qt.ConnectionType.QueuedConnection)
        self._request_seq = 0
        # The request being resolved: (number, words, planner, run id, its stop - set when the app stops waiting -,
        # THE CONSENT GATE as it stood when it started: set once the owner turns it off).
        self._looking: tuple[int, str, bool, str, threading.Event, _Gate] | None = None
        self._resolve_timer = QTimer(self)
        self._resolve_timer.setSingleShot(True)
        self._resolve_timer.timeout.connect(self._on_resolve_timeout)
        self.context_source: Callable[[str], Any] | None = None
        self.last_plan: Any = None                           # the last checked plan (memory only)
        self.last_route: Any = None                          # the last local route answered on this PC
        self.last_run: Any = None                            # what came of the last request (desktop.executor)
        self.pending: tuple[Any, ...] = ()                   # its steps held for the owner's click (P1.9's cards)
        self.found: tuple[Any, ...] = ()                     # what its finds found (P1.9's list)
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
        self.reading.desktopRequested.connect(self._on_desktop_requested)
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

    # ---- THE CONSENT GATE (hard rule 15; SPEC 10.3) ----------------------------------------------------

    def consent_given(self) -> bool:
        """THE consent gate: the one place that decides whether Jarvis may go onto the desktop now - [desktop] is
        enabled AND the owner's Turn on on the first-run "Jarvis on your desktop" card is stored (the app's prefs:
        ``desktop_turned_on`` exactly True). Fail-closed: anything unknown, missing, unreadable or failing is a no."""
        try:
            if getattr(getattr(self.config, "desktop", None), "enabled", False) is not True:
                return False
            store = self._consent_store() if self._consent_store is not None else None
            return store is not None and getattr(store, "desktop_turned_on", False) is True
        except Exception as exc:  # noqa: BLE001 - unknown: no
            logger.warning("Desktop: the owner's OK could not be read (%s); taken as no", type(exc).__name__)
            return False

    def turn_on(self) -> bool:
        """The owner's Turn on on the first-run card: stored in the app's prefs - it counts only once saved (False:
        it could not be saved, so nothing is turned on)."""
        return self._store_consent(True)

    def turn_off(self) -> bool:
        """Settings > Desktop's Turn off: the owner's OK goes (for this run even when it could not be saved; False
        then). Nothing desktop-side works again until he turns it on, and what is on its way stops at once: every job
        started before it - a request being looked up, a card's click, a planner run's carry-out - opens nothing more
        (its gate is set: the executor asks it right before each OS call), a request looked up meanwhile starts no
        planner run, and the result cards - those waiting for the HUD too - go now."""
        turned_off, self._turned_off = self._turned_off, threading.Event()
        turned_off.set()
        self.results.clear()
        self._held = None
        self._set_tidy(None)                                 # its card goes; a batch on its way stops at the next row
        self.pending, self.found = (), ()
        return self._store_consent(False)

    def _gate(self) -> _Gate:
        """THE CONSENT GATE as a new job's stop: asked anew right before each of its OS calls, and set for good by a
        Turn off after the job started (``turn_off``)."""
        return _Gate(lambda: self.consent_given(), self._turned_off)

    def _store_consent(self, on: bool) -> bool:
        if self._shut:
            return False
        store = None
        try:
            store = self._consent_store() if self._consent_store is not None else None
            saved = bool(store.set_desktop_turned_on(bool(on))) if store is not None else False
        except Exception as exc:  # noqa: BLE001 - not saved: fail-closed
            logger.warning("Desktop: the owner's OK could not be saved (%s)", type(exc).__name__)
            saved = False
        logger.info("Desktop: %s%s", "turned on" if on else "turned off", "" if saved else " (not saved)")
        self.consentChanged.emit(self.consent_given())
        return saved

    def _on_desktop_requested(self) -> None:
        """The Dock's Desktop, Ctrl+8 or the Find list's Desktop: onto the HUD - or, before the owner's Turn on, the
        first-run card instead (``consentNeeded``: the app shows it in the window)."""
        if self._shut or self._quitting:
            return
        if not self.consent_given():
            logger.info("Desktop: not turned on yet; the first-run card instead")
            self.consentNeeded.emit(ENTRY_DOCK)
            return
        self.enter_hud()

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
            self._show_held()                                # the HUD shows: the cards that waited show with it
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
        self.results.clear()                                 # the result cards are the HUD's: they go with it
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
        self._show_held()

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
        self.results.clear()                                 # nothing of the HUD's - no card, no hook - in the window
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
                    self._show_held()
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

    # ---- desktop requests (P1.7: understood and checked; P1.8: found and opened) -------------------------

    def busy(self) -> bool:
        """A request is being resolved on the desktop thread (its search, a local route's open), a result card's click
        is being carried out there, or a NEEDS YOUR OK card's rows are (or its Undo): no other may start."""
        return self._looking is not None or self._clicking is not None or self._tidy_job is not None

    def _new_watch(self) -> winwatch.WindowWatch:
        """The result cards' window watcher, over the window seam given (``window_api``) or the default one."""
        return winwatch.WindowWatch(self._window_api)

    def join(self, timeout: float) -> None:
        """Tests: wait for the desktop thread to end after shutdown()."""
        if self._worker is not None:
            self._worker.join(timeout)

    def _ensure_worker(self) -> tuple[DesktopWorker, desktop_service.DesktopService]:
        """The desktop thread and its service, made at the first request ([desktop] folders and extra_folders; the
        Jarvis project and its data folder never a place)."""
        if self._worker is None or self._service is None:
            settings = getattr(self.config, "desktop", None)
            excluded = [str(path) for path in (getattr(self.config, "project_root", None),
                                               getattr(self.config, "data_dir", None)) if path]
            self._service = desktop_service.DesktopService(
                folders=tuple(getattr(settings, "folders", ()) or ()),
                extra_folders=tuple(getattr(settings, "extra_folders", ()) or ()),
                excluded=excluded, backend_factory=self._backend_factory, hour24=self._hour24,
                journal_folder=self._journal_folder, undo_days=int(getattr(settings, "undo_days", 7) or 7))
            self._worker = DesktopWorker()
        return self._worker, self._service

    def request(self, words: str, *, planner: bool = True) -> bool:
        """A desktop request typed on the HUD (SPEC 3): it goes to the desktop thread (``busy`` meanwhile), which
        carries a local route out or builds the planner's candidates; the answer comes back on the GUI thread
        (``answered`` or ``plannerNeeded``). ``planner``: whether the planner may run (Ask on). False when it can't
        go now (another is being resolved or a card's click carried out, Jarvis is closing, or not turned on - THE
        CONSENT GATE again: nothing desktop-side works before the owner's Turn on). The last result cards go."""
        if self._shut or self._quitting or self.busy():
            return False
        if not self.consent_given():
            logger.info("Desktop: a request was refused (not turned on)")
            return False
        self.results.clear()
        self._held = None                                    # the last request's cards that waited for the HUD too
        self._set_tidy(None)                                 # ... and a NEEDS YOUR OK card not at work
        self._before = self.results.snapshot()               # the window an open brings is the one not among these
        worker, service = self._ensure_worker()
        self._request_seq += 1
        seq, run_id, stop, gate = self._request_seq, desktop_mode.new_run_id(), threading.Event(), self._gate()
        self._looking = (seq, words, bool(planner), run_id, stop, gate)
        future = worker.submit(service.resolve, words, planner=bool(planner), override=self.context_source,
                               run_id=run_id, cancel=_Either(stop, gate))
        bridge = self._bridge
        future.add_done_callback(lambda done, seq=seq: _deliver(bridge, seq, done))
        self._resolve_timer.start(int(RESOLVE_TIMEOUT_S * 1000))
        logger.info("Desktop %s: a request goes to the desktop thread", run_id)
        return True

    def _on_resolve_timeout(self) -> None:
        """The desktop thread took too long (a search or the shell hanging): the app stops waiting - nothing opens for
        that request any more (its stop is set), the words stay, and the next request may come. A card's click that
        took too long likewise: its stop is set, so it opens nothing from now on (only an OS call already in flight
        completes - and should one have, its end still says so: ``_on_clicked``)."""
        clicking, self._clicking = self._clicking, None
        if clicking is not None and not self._shut:
            clicking[5].set()
            self._stopped_click = clicking
            logger.warning("Desktop: a card's click took more than %d s; stopped", int(RESOLVE_TIMEOUT_S))
            self.answered.emit(with_address(CLICK_TOO_LONG_TEXT, self._address), TONE_ERROR, "", False)
        looking, self._looking = self._looking, None
        if looking is None or self._shut:
            return
        _seq, words, _planner, run_id, stop, _gate = looking
        stop.set()
        logger.warning("Desktop %s: the desktop thread took more than %d s; stopped waiting", run_id,
                       int(RESOLVE_TIMEOUT_S))
        self.answered.emit(with_address(TOO_LONG_TEXT, self._address), TONE_ERROR, words, False)

    def _on_resolved(self, seq: int, future: Future) -> None:
        """The desktop thread resolved request ``seq``: a local route carried out (``answered`` with Jarvis's words;
        its LIVE task), the planner's job (``plannerNeeded``), or nothing could be looked at (``answered``, the
        words left as they are)."""
        looking = self._looking
        if looking is None or looking[0] != seq:
            return
        self._looking = None
        self._resolve_timer.stop()
        _seq, words, planner, run_id, _stop, gate = looking
        if self._shut:
            return
        try:
            resolved = future.result()
        except CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - a bug on the desktop thread: nothing was done
            logger.error("Desktop %s: the request could not be resolved (%s)", run_id, type(exc).__name__)
            resolved = desktop_service.Resolved(desktop_service.UNAVAILABLE, run_id)
        if resolved.kind == desktop_service.LOCAL:
            self._answer_local(resolved, words)              # what came of it (stopped, if turned off meanwhile)
        elif gate.is_set():
            # Turned off (or the gate says no) while it was looked up: no planner run - nothing goes out.
            logger.info("Desktop %s: turned off meanwhile; no planner run", run_id)
            self.answered.emit(with_address(TURNED_OFF_TEXT, self._address), TONE_ERROR, words, False)
        elif resolved.kind == desktop_service.PLANNER:
            job = None
            if planner:
                context = resolved.context if isinstance(resolved.context, DesktopContext) else DesktopContext()
                job = desktop_mode.DesktopJob(run_id, context, carry_out=self._carry_out_for(context))
            logger.info("Desktop %s: no local route; %s (%.2f s)", run_id,
                        "the planner's job" if job is not None else "the planner is off", resolved.seconds)
            self.plannerNeeded.emit(job, words)
        else:
            logger.info("Desktop %s: Jarvis can't look on this PC now; nothing was done", run_id)
            self.answered.emit(with_address(UNAVAILABLE_TEXT, self._address), TONE_ERROR, words, False)

    def _answer_local(self, resolved: Any, words: str) -> None:
        """A local route carried out on the desktop thread: kept (``last_route``, ``last_run``, ``pending``,
        ``found``), its LIVE task, the log line (kinds, statuses and codes), Jarvis's words (``answered``) and its
        result cards (``_show_or_hold``)."""
        found, run = resolved.route, resolved.run
        self.last_route = found
        self._keep(run)
        if found.code:
            said, soft, tone = desktop_routes.route_words(found, self._address), True, TONE_ERROR
            logger.info("Desktop %s: local route %s (%s)", resolved.run_id, found.kind, found.code)
        else:
            said = desktop_executor.run_words(run, None, self._address)
            soft, tone = desktop_executor.run_soft(run), run_tone(run)
            logger.info("Desktop %s: local route %s: %s (%.2f s)", resolved.run_id, found.kind,
                        run.statuses_text() if run is not None else "nothing", resolved.seconds)
        _local_task(self.live, words, found, run, said, soft)
        self.answered.emit(said, tone, words, True)
        self._show_or_hold(run)

    def _leaving(self) -> bool:
        """The switch back to the window is on its way (``is_hud()`` stays true until the window shows)."""
        flight = self._flight
        return self._switching and flight is not None and flight.direction() == alive.TO_WINDOW

    def _show_or_hold(self, run: Any) -> None:
        """A request's result cards (``run.shown``): on the HUD - from the switch to it on - they show now; in the
        window, or with the switch back on its way, they are held as data only (no card window, no window followed
        meanwhile) and show when the HUD next shows (``_show_held``) - a step that waits for the owner's click or OK
        keeps its card, as the words say. None once the gate says no (Turn off)."""
        if run is None or self._shut or not getattr(run, "shown", ()) or not self.consent_given():
            return
        tidy_cards = [card for card in run.shown if isinstance(card, desktop_tidy.TidyCard)]
        if tidy_cards and not self.busy():
            self._set_tidy(desktop_tidy.TidyState(tidy_cards[-1]))     # shown now on the HUD, else with it
        if not any(isinstance(card, card_results.Card) for card in run.shown):
            return
        if self._hud and not self._leaving():
            self.results.show_run(run, self._before)
            return
        self._held = (run, self._before)
        logger.info("Desktop %s: %d result card(s) wait for the HUD", getattr(run, "run_id", "") or "-",
                    len(run.shown))

    def _show_held(self) -> None:
        """The HUD's windows have just shown (the instant swap, or the flight's handover): the result cards that
        waited for it show with them - unless a request typed meanwhile took them away."""
        held, self._held = self._held, None
        if self._shut or not self._hud or not self.consent_given():
            return
        self._show_tidy()
        if held is None:
            return
        run, before = held
        shown = self.results.show_run(run, before)
        logger.info("Desktop %s: %d result card(s) that waited show on the HUD", getattr(run, "run_id", "") or "-",
                    shown)

    def _carry_out_for(self, context: DesktopContext) -> Callable[..., Any]:
        """The planner's way to carry its checked plan out: on the desktop thread, with this request's candidates for
        D5's other matches. The Ask thread waits for it - at most CARRY_OUT_TIMEOUT_S (TimeoutError) - and passes the
        request's Cancel on as it comes: no new step starts after either. THE CONSENT GATE is asked again right before
        each OS call (fail-closed), and a Turn off after this job was made stops it for good (``_gate``)."""
        worker, service = self._ensure_worker()
        gate = self._gate()

        def carry_out(plan: Any, words: str, cancel: threading.Event | None = None) -> Any:
            stop = threading.Event()
            future = worker.submit(service.carry_out, plan, words, context, _Either(stop, cancel, gate))
            try:
                return future.result(timeout=CARRY_OUT_TIMEOUT_S)
            except FutureTimeout:
                stop.set()                                   # too long: no new step starts
                raise TimeoutError("the desktop thread took too long") from None

        return carry_out

    # ---- the result cards' clicks (P1.9) ------------------------------------------------------------------

    def _on_card_click(self, entry: Any, key: str) -> None:
        """A result card's button (desktop_results.ResultCards.clickRequested): Not now takes the card away; anything
        else is carried out on the desktop thread with the card's OWN item (desktop.results.click_for, then
        desktop.service.click) - only after the owner's Turn on, one at a time, never while Jarvis closes."""
        card = getattr(entry, "card", None)
        if key == card_results.DISMISS_KEY:
            self.results.remove(entry)
            logger.info("Desktop: a result card was put away (Not now)")
            return
        if self._shut or self._quitting or card is None:
            return
        if not self.consent_given():
            logger.info("Desktop: a card's click was refused (not turned on)")
            return
        if self.busy():
            logger.info("Desktop: a card's click while another desktop job runs; ignored")
            return
        click = card_results.click_for(card, key)
        if click is None:
            return
        button = next((spec.text for spec in entry.window.content().buttons if spec.key == key), "")
        button = button or next((row.action for row in entry.window.content().rows if row.key == key), "a button")
        worker, service = self._ensure_worker()
        before = self.results.snapshot()
        self._click_seq += 1
        seq, stop = self._click_seq, threading.Event()
        self._clicking = (seq, entry, key, before, button, stop)
        # Its stop (the app stops waiting) and THE CONSENT GATE, asked again right before its OS call.
        future = worker.submit(service.click, click, _Either(stop, self._gate()))
        bridge = self._bridge
        future.add_done_callback(lambda done, seq=seq: _deliver(bridge, seq, done, "clicked"))
        self._resolve_timer.start(int(RESOLVE_TIMEOUT_S * 1000))
        logger.info("Desktop %s: a card's click goes to the desktop thread (%s, %s)", card.run_id or "-",
                    click.skill, click.granted)

    def _on_clicked(self, seq: int, future: Future) -> None:
        """A card's click was carried out on the desktop thread: the cards follow what came of it, the caption says
        Jarvis's words for it and LIVE gets its task ("Approved by your click ..."). One that took too long was
        stopped (``_on_resolve_timeout``: "I stopped"); its late end is heard only when an OS call already in flight
        did open something after all - then the caption says what opened, so its words stay true."""
        clicking = self._clicking
        late = clicking is None or clicking[0] != seq
        if late:
            clicking = self._stopped_click
            if clicking is None or clicking[0] != seq:
                return
            self._stopped_click = None
        else:
            self._clicking = None
            self._resolve_timer.stop()
        if self._shut:
            return
        _seq, entry, key, before, button, _stop = clicking
        try:
            run = future.result()
        except CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - a bug on the desktop thread: nothing was done
            logger.error("Desktop: a card's click could not be carried out (%s)", type(exc).__name__)
            if not late:
                self.answered.emit(with_address(UNAVAILABLE_TEXT, self._address), TONE_ERROR, "", False)
            return
        if late and not (run is not None and run.done()):
            logger.info("Desktop: a stopped click ended with nothing opened")
            return                                           # "I stopped" stays true
        said = desktop_executor.run_words(run, None, self._address)
        soft = desktop_executor.run_soft(run)
        logger.info("Desktop %s: a card's click came to %s (%.2f s)%s", getattr(run, "run_id", "") or "-",
                    run.statuses_text() if run is not None else "nothing", getattr(run, "seconds", 0.0),
                    " after it was stopped" if late else "")
        if self._hud and not self._leaving():
            self.results.apply_click(entry, key, run, before)
        _click_task(self.live, button, run, said, soft, clock_text(datetime.now(), self._hour24))
        if said:
            self.answered.emit(said, run_tone(run), "", False)

    def _keep(self, run: Any) -> None:
        """What came of a request, in memory: the run, its steps held for the owner's click and what its finds
        found (P1.9 shows them)."""
        self.last_run = run
        if run is None:
            self.pending, self.found = (), ()
            return
        self.pending = tuple(run.waiting())
        self.found = tuple(item for result in run.found() for item in result.found)

    def report_plan(self, outcome: Any) -> str:
        """The planner's outcome for a desktop request (ui's _on_ask_outcome): the checked plan and what came of it are
        kept (``last_plan``, ``last_run``, ``pending``, ``found``; memory only) and Jarvis's own words for them come
        back (the bar's status already shows them in the caption). Counts and codes only in the log."""
        plan = getattr(outcome, "brief", None)
        if not getattr(outcome, "ok", False) or plan is None:
            logger.info("Desktop %s: no plan (%s)", getattr(outcome, "target", "") or "-",
                        getattr(outcome, "kind", "error"))
            return str(getattr(outcome, "message", "") or "")
        self.last_plan = plan
        run = getattr(outcome, "desktop_run", None)
        self._keep(run)
        logger.info("Desktop %s: plan kept, %d step(s) passed, %d refused; %s", plan.run_id or "-",
                    len(plan.accepted()), len(plan.refused()),
                    run.statuses_text() if run is not None else "nothing carried out")
        self._show_or_hold(run)                              # in the window meanwhile: they wait for the HUD
        return str(getattr(outcome, "message", "") or "")

    # ---- the NEEDS YOUR OK card of file steps (P2.3) ---------------------------------------------------------

    def tidy(self) -> desktop_tidy.TidyState | None:
        """The NEEDS YOUR OK card of file steps (its state; memory only), or None."""
        return self._tidy

    def waiting_count(self) -> int:
        """NEEDS YOUR OK cards of file steps waiting for the owner's decision (the chip counts them)."""
        return 1 if self._tidy is not None and self._tidy.waiting() else 0

    def _journal_folder(self) -> Any:
        folder = self.journal_folder
        return folder() if folder is not None else desktop_journal.default_folder()

    def _keys(self) -> Any:
        if self.stop_keys is None:
            self.stop_keys = stopkeys.default_stopkeys()      # the tests' guard: NoStopKeys (registers nothing)
        return self.stop_keys

    def _set_tidy(self, state: desktop_tidy.TidyState | None) -> None:
        """The card's state anew (or none): its view follows on the HUD, the chip and Approvals' reminder too."""
        if state is None and self._tidy is not None and self._tidy.busy() and self._tidy_job is not None:
            self._tidy_job[2].set()                          # dropped while at work: it stops at the next row
        self._tidy = state
        if state is None:
            self.results.drop_tidy()
        elif self._hud and not self._leaving():
            self._show_tidy()
        self._tidy_changed()

    def _show_tidy(self) -> None:
        state = self._tidy
        if state is None or self._shut or not self._hud:
            return
        self.results.show_tidy(state, desktop_tidy.tidy_text(state, self._hour24))

    def _refresh_tidy(self) -> None:
        state = self._tidy
        if state is not None and self.results.tidy() is not None:
            self.results.refresh_tidy(desktop_tidy.tidy_text(state, self._hour24))

    def _tidy_changed(self) -> None:
        self.sync_glance()
        self.waitingChanged.emit(self.waiting_count())

    def _on_tidy_tick(self, number: int, ticked: bool) -> None:
        state = self._tidy
        if state is not None and state.toggle(number, ticked):
            self._refresh_tidy()

    def _on_tidy_click(self, key: str) -> None:
        """The tidy card's buttons: Not now puts it away (nothing carried out); the commit carries the ticked rows
        out; Stop stops the batch (or the Undo) after the row on its way; Undo puts the items back."""
        state = self._tidy
        if state is None or self._shut:
            return
        if key == desktop_tidy.DISMISS_KEY:
            if state.stage == desktop_tidy.WAITING:
                logger.info("Desktop %s: the NEEDS YOUR OK card was put away (Not now); nothing done",
                            state.card.run_id or "-")
                self._set_tidy(None)
        elif key == desktop_tidy.GO_KEY:
            self._start_tidy(undo=False)
        elif key == desktop_tidy.UNDO_KEY:
            self._start_tidy(undo=True)
        elif key == desktop_tidy.STOP_KEY:
            self._stop_tidy("stop")

    def _guarded(self, state: desktop_tidy.TidyState) -> bool:
        """The app's click guards (a click too soon after another decision, or the cards locked out meanwhile): True
        when the click must be ignored (fail-closed)."""
        guard = self._click_guard
        if guard is None:
            return False
        try:
            return bool(guard(TIDY_ID + (state.card.run_id or "-")))
        except Exception as exc:  # noqa: BLE001 - unknown: ignored
            logger.warning("Desktop: the click guard could not be asked (%s); ignored", type(exc).__name__)
            return True

    def _start_tidy(self, *, undo: bool) -> None:
        """The owner's click on the card's commit (or on its Undo): only his click, after THE CONSENT GATE and the
        app's click guards, one desktop job at a time - then on the desktop thread, never the calendar's worker."""
        state = self._tidy
        if state is None or self._shut or self._quitting:
            return
        if undo and not state.undoable():
            return
        days = int(getattr(getattr(self.config, "desktop", None), "undo_days", 7) or 7)
        if undo and state.at is not None and datetime.now() - state.at > timedelta(days=days):
            state.undo_expired = True                        # offered for [desktop] undo_days only (D9)
            logger.info("Desktop %s: Undo refused (older than undo_days)", state.card.run_id or "-")
            self._refresh_tidy()
            self.answered.emit(f"That was more than {days} days ago, {self._address} - Undo isn't offered any more.",
                               TONE_ERROR, "", False)
            return
        if not undo and (state.stage != desktop_tidy.WAITING or not state.ticked_numbers()):
            return
        if not self.consent_given():
            logger.info("Desktop %s: a NEEDS YOUR OK card's click was refused (not turned on)",
                        state.card.run_id or "-")
            return
        if self._guarded(state):
            logger.info("Desktop %s: a NEEDS YOUR OK card's click was ignored (too soon or locked)",
                        state.card.run_id or "-")
            return
        if self.busy():
            logger.info("Desktop %s: a NEEDS YOUR OK card's click while another desktop job runs; ignored",
                        state.card.run_id or "-")
            return
        content = self.results.tidy().window.desk() if self.results.tidy() is not None else None
        key = desktop_tidy.UNDO_KEY if undo else desktop_tidy.GO_KEY
        button = desktop_tidy.UNDO_TEXT
        if content is not None and not undo:
            button = next((spec.text for spec in content.buttons if spec.key == key), button)
        worker, service = self._ensure_worker()
        self._tidy_seq += 1
        seq, stop, gate = self._tidy_seq, threading.Event(), self._gate()
        self._tidy_job = [seq, "undo" if undo else "batch", stop, gate, button, 0.0, ""]
        self._approved_at = clock_text(datetime.now(), self._hour24)
        bridge = self._bridge

        def on_stop(seq: int = seq, stop: threading.Event = stop) -> None:      # Esc, on its own thread
            stop.set()
            _emit(bridge, "tidy_esc", seq)

        common = {"cancel": _Either(stop, gate), "stop_keys": self._keys(), "on_stop": on_stop,
                  "on_start": lambda held, seq=seq: _emit(bridge, "tidy_started", seq, bool(held))}
        if undo:
            seqs = state.undo_seqs()
            state.start_undo()
            future = worker.submit(service.undo_card, state.card.run_id, seqs, **common)
            logger.info("Desktop %s: Undo goes to the desktop thread (%d item(s))", state.card.run_id or "-",
                        len(seqs))
        else:
            ticked = state.ticked_numbers()
            state.start()
            future = worker.submit(service.carry_out_card, state.card, sha=state.card.sha, ticked=ticked,
                                   on_row=lambda end, ended, total, seq=seq: _emit(bridge, "tidy_row", seq, end,
                                                                                  int(ended), int(total)),
                                   **common)
            logger.info("Desktop %s: the card's click goes to the desktop thread (%d of %d row(s) ticked)",
                        state.card.run_id or "-", len(ticked), len(state.card.rows))
        future.add_done_callback(lambda done, seq=seq: _deliver(bridge, seq, done, "tidy_done"))
        self._refresh_tidy()
        self._tidy_changed()

    def _stop_tidy(self, reason: str) -> None:
        """Stop on the card (or Esc, or a Turn off): the job stops after the row on its way."""
        job, state = self._tidy_job, self._tidy
        if job is None:
            return
        job[2].set()
        if not job[6]:
            job[5], job[6] = time.monotonic(), reason
            logger.info("Desktop %s: stop asked (%s)", state.card.run_id if state is not None else "-", reason)
        if state is not None and state.stop_asked(reason):
            self._refresh_tidy()

    def _on_tidy_started(self, seq: int, held: bool) -> None:
        job, state = self._tidy_job, self._tidy
        if job is None or job[0] != seq or state is None:
            return
        state.started(held)
        self._refresh_tidy()

    def _on_tidy_row(self, seq: int, end: Any, ended: int, _total: int) -> None:
        job, state = self._tidy_job, self._tidy
        if job is None or job[0] != seq or state is None or not isinstance(end, desktop_tidy.RowEnd):
            return
        state.row_ended(end, ended)
        self._refresh_tidy()

    def _on_tidy_esc(self, seq: int) -> None:
        job = self._tidy_job
        if job is not None and job[0] == seq:
            self._stop_tidy("esc")

    def _on_tidy_done(self, seq: int, future: Future) -> None:
        """The card's batch (or its Undo) ended on the desktop thread: the stamp, Jarvis's words, LIVE's task and the
        log line (counts and codes)."""
        job = self._tidy_job
        if job is None or job[0] != seq:
            return
        self._tidy_job = None
        _seq, kind, _stop, gate, button, asked, reason = job
        state = self._tidy
        run_id = state.card.run_id if state is not None else "-"
        undo = kind == "undo"
        try:
            outcome = future.result()
        except CancelledError:
            outcome = None
        except Exception as exc:  # noqa: BLE001 - a bug on the desktop thread
            logger.error("Desktop %s: the card's job could not be carried out (%s)", run_id, type(exc).__name__)
            outcome = None
        if outcome is None:
            outcome = (desktop_tidy.UndoOutcome(run_id, code=desktop_tidy.UNAVAILABLE) if undo else
                       desktop_tidy.CardOutcome(run_id, desktop_tidy.UNAVAILABLE))
        if gate.is_set() and not reason:
            reason = "off"
            asked = asked or time.monotonic()
        if not undo and outcome.code == desktop_tidy.STOPPED_FIRST and reason == "off":
            outcome = desktop_tidy.CardOutcome(outcome.run_id, desktop_tidy.TURNED_OFF, esc=outcome.esc)
        stop_ms = int(round((time.monotonic() - asked) * 1000)) if reason and asked else 0
        self._log_tidy(run_id, undo, outcome, reason, stop_ms)
        if self._shut or state is None:
            return                                       # turned off or closing meanwhile: its card is gone
        now = datetime.now()
        if undo:
            state.finish_undo(outcome, now)
            soft = bool(outcome.code) or outcome.put_back != len(outcome.rows)
        else:
            state.finish(outcome, now)
            soft = bool(outcome.code) or len(state.done_numbers()) != len(state.chosen)
        said = desktop_tidy.said(state, self._address)
        _tidy_task(self.live, button, state, said, soft, self._approved_at, reason,
                   stop_ms if reason and not outcome.code else 0, undo)
        self._refresh_tidy()
        self._tidy_changed()
        if said:
            self.answered.emit(said, TONE_ERROR if soft else TONE_DONE, "", False)

    @staticmethod
    def _log_tidy(run_id: str, undo: bool, outcome: Any, reason: str, stop_ms: int) -> None:
        if outcome.code:
            logger.info("Desktop %s: %s refused (%s)", run_id, "Undo" if undo else "the card's click", outcome.code)
            return
        counts = Counter(row.state for row in outcome.rows)
        if undo:
            logger.info("Desktop %s: put back %d/%d, left %d, %.1f s", run_id, counts.get(desktop_journal.UNDONE, 0),
                        len(outcome.rows), len(outcome.rows) - counts.get(desktop_journal.UNDONE, 0),
                        outcome.seconds)
        else:
            logger.info("Desktop %s: done %d/%d, skipped %d, failed %d, %.1f s", run_id,
                        counts.get(desktop_journal.DONE, 0), len(outcome.rows),
                        counts.get(desktop_journal.SKIPPED, 0), counts.get(desktop_journal.FAILED, 0),
                        outcome.seconds)
        if reason:
            ended = sum(1 for row in outcome.rows if row.reason != "stopped")
            logger.info("Desktop %s: stopped %s at row %d/%d, %d ms to stop", run_id, reason, ended,
                        len(outcome.rows), stop_ms)

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
        win.set_pending(reading.pending_count() + self.waiting_count())    # a NEEDS YOUR OK card counts (P2.3)
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
        """Jarvis is closing (AppController.shutdown, next to the meetings'): a switch on its way stops at once, the
        desktop thread takes no new job (the one running ends as it is; the queued ones are cancelled) and the HUD's
        windows are hidden and deleted. The window is not touched here - the app hides it; nothing ever closes it."""
        if self._shut:
            return
        self._keyboard_wanted = False
        if shiboken6.isValid(self.overlay.hud):
            self.overlay.hud.give_back_keyboard(True, "Jarvis closes")      # the window that had it gets it back
        self._shut = True
        looking, self._looking = self._looking, None
        if looking is not None:
            looking[4].set()                                 # a request being resolved opens nothing now
        clicking, self._clicking, self._stopped_click = self._clicking, None, None
        if clicking is not None:
            clicking[5].set()                                # ... nor a card's click
        job, self._tidy_job = self._tidy_job, None
        if job is not None:
            job[2].set()                                     # ... and a card's batch stops at its next row
        self._tidy = None
        self._resolve_timer.stop()
        self._held = None
        self.results.clear()                                 # the cards, their brackets, hooks and timers go
        if self._worker is not None:
            self._worker.stop()                              # the job running ends as it is; nothing new starts
        self._drop_flight()
        self._hud = False
        if shiboken6.isValid(self.reading):
            self.reading.hold_screens(None)
            self.reading.set_hud_present(False)
        self.overlay.shutdown()
