"""Ask Jarvis in the app: the command bar's controller and the thread its planner runs on.

    AskController(config, planner, bar, *, context, blocked, calendars, live, parent)
        start()           checks Claude Code (no model request) and which accounts' mail Ask can read,
                          on the "ask" thread; the header chip and the bar follow
        submit(text)      one Ask (the bar's Enter / Ask): planner.plan() on the "ask" thread
        cancel()          ends a running Ask (the CLI is gone within a second); nothing is proposed
        refresh(full)     checks again: Claude Code and the mail after a failed run, only the mail
                          (full=False) after a Google sign-in
        shutdown()        cancels a running Ask and stops the thread (a daemon: never waited for)
    Signals (GUI thread):
        busyChanged(bool)            an Ask's planning really started (its first stage: the checks
                                     passed; the app pauses the briefing, the orb plans) / ended. An
                                     Ask refused before that (Claude Code missing or signed out, a
                                     limit) never pauses anything
        outcomeReady(object)         planner.AskOutcome of a finished Ask that was not cancelled
        askStarted(int, str)         an Ask was accepted and queued (seq, the request as typed, whitespace
                                     collapsed); never for a refused Enter (busy, empty, blocked). The
                                     app shows the request in its conversation; it is never logged
        askCancelled(int)            the Ask ``seq`` was cancelled: nothing was proposed
        chipChanged(str, str)        the header's ``claude`` chip: planner.CHIP_* and its tooltip
        mailSignInRequested(str)     the bar's link: sign this account in again so Ask may read its mail
        memoryRequested(object, str) a typed memory shortcut (ask.routes: "remember that ...", "forget
                                     that ...", "what do you remember?") and the words as typed: the app
                                     handles it at once - no planner run, no caps, no askStarted
        followupsRequested(str)      a typed "any follow-ups?" (ask.routes FOLLOWUPS) and the words as
                                     typed: the app answers from its follow-up list at once (no run)

The planner only proposes: its cards reach the app through ``outcomeReady`` and then go through the
same click, Undo and executor.run_action path as the briefing's. ``live`` (the app's
live.LiveStream): each accepted Ask is a LIVE view task, from "Your request" (ask.request) through
the planner's steps to "Answer and cards" (ask.cards), finished when the outcome is handed over
(or cancelled, or Jarvis closes); ``last_task_id`` is the last finished one. ``context()`` (GUI thread)
gives the briefing as Ask may see it and the briefing cards' ids; ``blocked()`` a reason not to start
now (a Google sign-in is open). Nothing here logs the request, the planner's text, the context or an
address: counts, kinds and durations only. One Ask at a time; a run that is cancelled or ends after
shutdown proposes nothing.

Typed routes (the productivity bundle, ``route_flags``): after the stop and empty checks and when the
text does not start with "web:", ask.routes.route() decides; a memory route is handed to the app
(``memoryRequested``) before the busy and blocked checks. The bar emits only while idle (it is
read-only while a request runs), so in practice a memory route arrives when nothing runs; MEMORY's
own Add field saves a note meanwhile. ``memory()`` (the notes the planner would get and how many
were left out) adds "Memory given" to each Ask's "Your request" step. A plan route ("plan my day")
goes through the busy and blocked checks and starts a planner mode run: submit_mode(request, words)
(also what a click on an offer calls) runs planner.plan_mode() on the ask thread with
``mode_inputs()`` (the app's to-dos and deadlines, read on the GUI thread) as a LIVE task of the
mode's own kind (live.TASK_PLAN) whose first step is "Your request" (live.MODE_REQUEST). Its
outcome comes through ``outcomeReady`` like an Ask's (AskOutcome.mode set). Only a run typed into
the bar ever clears the bar's text (R23): a click-started run leaves what the owner was typing.
A prep route ("prep me for my 2 PM") is a planner mode run the same way (live.TASK_PREP): its words
only pick the meeting (ModeRequest.target) and never reach Claude. A follow-up draft (live.TASK_NUDGE)
is only ever click-started (a FOLLOW-UPS row, a brief's "Draft a nudge", "Tell Jarvis what to say"):
submit_mode with ModeRequest(MODE_NUDGE, ...), so it never clears the bar.

Web research: a request that starts with "web:" (or "research:") is a WEB task (live.TASK_WEB;
``running_kind``) whose "Your request" says nothing is read from the accounts; the command still
goes to planner.plan() as typed, which runs only the research (ask.research). A request the
planner hands over to research stays in its ASK task. While the research runs the bar says
"Searching the web... N s"; its outcome (AskOutcome.research) ends the task with the cards in the
words every view uses (research_validate.cards_words: "2 source cards + 1 proposal"; CHECK when a
source or a suggestion was left out), and the "Answer and cards" step lists the sources' cards as
"ASK WEB - <site> - [n] <title>". Web research is part of Ask: it counts against the Ask caps and
its own ([research] max_per_hour, max_per_day); after a research (or its limit) the meta line shows
the web research runs left, and the research limit is its own kind (planner.RESEARCH_LIMIT).
"""

from __future__ import annotations

import dataclasses
import logging
import math
import queue
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from . import hud
from .actions import is_web_open, link_host
from .ask import planner as ask_planner
from .ask.context import COMMAND_CAP
from .ask.modes import (MODE_MEETING, MODE_NUDGE, MODE_PLAN, MODE_PREP, MODE_TASKS, MODE_TITLES, ORIGIN_AUTO,
                        ORIGIN_CHOICE, ORIGIN_TYPED, ModeInputs, ModeRequest)
from .ask.research import forced_question
from .ask.research_validate import NO_ANSWER, cards_words, source_card_ids
from .ask.routes import MEMORY_ROUTES, NO_ROUTE, ROUTE_FOLLOWUPS, ROUTE_PLAN, ROUTE_PREP, RouteFlags, route
from .ask.stream import CANCELLED, LIMIT, RESEARCH_CAP
from .config import Config
from .google_auth import PROBLEM_EXPIRED, PROBLEM_SCOPE, PROBLEM_SIGNED_OUT, logged_alias
from .live import (
    ASK_CARDS,
    ASK_REQUEST,
    BACKGROUND_KEY,
    BACKGROUND_TITLE,
    CLAUDE_CHECK,
    MODE_REQUEST,
    NO_STEP,
    NO_TASK,
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_WARN,
    TASK_ASK,
    TASK_BACKGROUND,
    TASK_WEB,
    LiveStep,
    LiveStream,
    LiveTask,
    quiet,
)

logger = logging.getLogger(__name__)

DOT = hud.MIDDLE_DOT
STAGE_TEXTS = {
    ask_planner.STAGE_CONTEXT: "Reading your calendar...",
    ask_planner.STAGE_PLANNING: "Planning...",
    ask_planner.STAGE_MAIL: "Searching your mail...",
    ask_planner.STAGE_PLANNING_AGAIN: "Planning with your mail...",
    ask_planner.STAGE_RESEARCH: "Searching the web...",
    ask_planner.STAGE_DAY: "Reading your calendar...",
    ask_planner.STAGE_PREP_MAIL: "Reading your mail...",
    ask_planner.STAGE_THREAD: "Reading the thread...",
}
_TIMED_STAGES = (ask_planner.STAGE_PLANNING, ask_planner.STAGE_PLANNING_AGAIN, ask_planner.STAGE_RESEARCH)
# A planner mode's status while it runs (with the seconds; the meta line says Esc cancels).
MODE_STATUS = {MODE_PLAN: "Planning your day...", MODE_PREP: "Preparing your brief...",
               MODE_NUDGE: "Drafting a reply...", MODE_MEETING: "Writing the meeting summary..."}
MEETING_READY_TEXT = "Your meeting summary is ready on the Meetings screen"
# A click on an offer while a request runs: refused, nothing queued, the link stays (spec 4.1).
CLICK_BUSY_TEXT = "Jarvis is busy with another request; click the offer again when it's done"
CHOICE_BUSY_TEXT = "Jarvis is busy with another request; click Prep this one again when it's done"
NOTICE_S = 4.0       # a refused click's words stay this long before the running request's stage comes back
RUNNING_META = f"uses your Claude plan {DOT} Esc cancels"
IDLE_HINT = ("Type a request: Jarvis plans it with your calendar and today's briefing, and every "
             "proposal waits under NEEDS YOUR OK")
# The field's tooltip adds this while web research is on ([research] enabled).
WEB_HINT = ("Start with web: to look something up on the web: a separate search that gets only your words "
            "and today's date, never your calendar, mail or briefing")
WEB_MODE_TEXT = "web research (you started it with web:) - nothing is read from your accounts"
WEB_BRIEFING_TEXT = "none - web research gets only your words and today's date"
RESEARCH_LIMIT_META = "web research limit reached"   # the meta line while the research caps refuse
CHECKING_TEXT = "Checking Claude Code..."
STARTING_TEXT = "Starting..."
CANCELLING_TEXT = "Cancelling..."
CANCELLED_TEXT = "Cancelled; nothing was proposed"
EMPTY_TEXT = "Type what you'd like Jarvis to do"
BUSY_TEXT = "Jarvis is still finishing the last request; try again in a moment"
NOTHING_TEXT = "Nothing to propose for that"
MAIL_LINK_TEXT = "Allow {alias} mail"
MAIL_LINK_TIP = ("{message}. Opens Google's sign-in for the {alias} account in your browser: tick the box for "
                 "reading email (read only; Ask uses it only for the request you type)")
_TICK_MS = 1000
# The header chip's colour for each planner.CHIP_* state.
CHIP_STATUSES = {ask_planner.CHIP_OK: hud.STATUS_OK, ask_planner.CHIP_SIGN_IN: hud.STATUS_WARN,
                 ask_planner.CHIP_LIMIT: hud.STATUS_WARN, ask_planner.CHIP_OFF: hud.STATUS_OFF,
                 ask_planner.CHIP_ERR: hud.STATUS_ERROR}
# What a mail reader's problem means for the bar: these are fixed by one more Google sign-in.
_SIGN_IN_FIXES = (PROBLEM_SIGNED_OUT, PROBLEM_SCOPE, PROBLEM_EXPIRED)
_SOFT_KINDS = (ask_planner.EMPTY, ask_planner.CAPS, "busy", CANCELLED,   # amber, not red
               ask_planner.RESEARCH_OFF, RESEARCH_CAP, ask_planner.RESEARCH_LIMIT)


@dataclasses.dataclass(frozen=True)
class MailNeed:
    """An account whose mail Ask could read after one more Google sign-in (the message names no address)."""

    alias: str
    message: str


@dataclasses.dataclass(frozen=True)
class _CheckJob:
    seq: int
    full: bool = True          # Claude Code too (False: only which accounts' mail can be read)


@dataclasses.dataclass(frozen=True)
class _PlanJob:
    seq: int
    text: str
    briefing: Any
    page_ids: tuple[str, ...]
    cancel: threading.Event
    live: Any = None                    # the Ask's LiveTask (None: no LIVE view; the planner gets no keyword)


@dataclasses.dataclass(frozen=True)
class _ModeJob:
    seq: int
    request: ModeRequest
    inputs: ModeInputs
    briefing: Any
    page_ids: tuple[str, ...]
    cancel: threading.Event
    live: Any = None


@dataclasses.dataclass
class _Running:
    seq: int
    cancel: threading.Event
    started: float                      # time.monotonic() of the submit
    stage: str = ""                     # "" until the planner's first stage (its checks passed)
    cancel_requested: bool = False
    busy_sent: bool = False             # busyChanged(True) went out (on the first stage)
    task: LiveTask = NO_TASK            # the Ask's LIVE view task
    kind: str = TASK_ASK                # TASK_WEB: a request that starts with "web:" (web research only);
                                        # a planner mode's own task kind (live.TASK_PLAN)
    typed: bool = True                  # typed into the bar: only then is the bar cleared on success (R23)
    mode: str = ""                      # a planner mode's run (ask.modes.MODE_PLAN); "" for an Ask
    clear_text: str = ""                # a click-started run: the typed words that led to it, cleared on success
                                        # only while the bar still holds exactly them (R23)


class _AskBridge(QObject):
    """Signals the ask thread emits; connected queued, so the slots run on the GUI thread."""

    checked = Signal(int, object, object, object)   # seq, Readiness, CapCheck, [MailNeed]
    stage = Signal(int, str)                        # seq, planner.STAGE_*
    planned = Signal(int, object)                   # seq, AskOutcome


class _AskWorker:
    """One daemon thread named "ask": readiness checks and planner runs, one at a time."""

    def __init__(self, planner: Any, bridge: _AskBridge, stop: threading.Event,
                 mail_needs: Callable[[], list[MailNeed]], background: Callable[[], LiveTask] | None = None) -> None:
        self._planner = planner
        self._bridge = bridge
        self._stop = stop
        self._mail_needs = mail_needs
        self._background = background or (lambda: NO_TASK)   # the LIVE view's "Background reads"
        self._jobs: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, name="ask", daemon=True)
        self._thread.start()

    def put(self, job: Any) -> None:
        self._jobs.put(job)

    def stop(self) -> None:
        self._jobs.put(None)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None or self._stop.is_set():
                return
            try:
                if isinstance(job, _PlanJob):
                    self._plan(job)
                elif isinstance(job, _ModeJob):
                    self._mode(job)
                elif isinstance(job, _CheckJob):
                    self._check(job)
            except Exception as exc:  # noqa: BLE001 - a bug must not end the thread (or take the app down)
                logger.error("The Ask thread failed unexpectedly (%s)", type(exc).__name__)
                if isinstance(job, (_PlanJob, _ModeJob)):
                    self._emit("planned", job.seq, ask_planner.AskOutcome(
                        False, "error", "Ask stopped because of an unexpected error; nothing was proposed",
                        mode=job.request.mode if isinstance(job, _ModeJob) else ""))

    def _check(self, job: _CheckJob) -> None:
        step = _claude_check_step(self._background()) if job.full else None
        ready = self._planner.engine.readiness() if job.full else None   # None: keep the last answer
        caps = self._planner.usage.check()
        if step is not None:
            _claude_checked(step, ready, caps)
        self._emit("checked", job.seq, ready, caps, self._mail_needs())

    def _plan(self, job: _PlanJob) -> None:
        def on_stage(stage: str) -> None:
            self._emit("stage", job.seq, stage)

        extra = {"live": job.live} if job.live is not None else {}   # a planner without the keyword keeps working
        outcome = self._planner.plan(job.text, briefing=job.briefing, page_ids=job.page_ids, cancel=job.cancel,
                                     on_stage=on_stage, **extra)
        self._emit("planned", job.seq, outcome)

    def _mode(self, job: _ModeJob) -> None:
        def on_stage(stage: str) -> None:
            self._emit("stage", job.seq, stage)

        outcome = self._planner.plan_mode(job.request, inputs=job.inputs, briefing=job.briefing,
                                          page_ids=job.page_ids, cancel=job.cancel, on_stage=on_stage,
                                          live=job.live)
        self._emit("planned", job.seq, outcome)

    def _emit(self, name: str, *args: Any) -> None:
        if self._stop.is_set():
            return
        try:
            getattr(self._bridge, name).emit(*args)
        except RuntimeError:
            pass   # the bridge was deleted during shutdown


class AskController(QObject):
    """The command bar's state machine: idle -> running (context, planning, mail, planning again)
    -> idle with the answer (or the reason nothing was proposed). See the module docs."""

    busyChanged = Signal(bool)
    outcomeReady = Signal(object)
    askStarted = Signal(int, str)
    askCancelled = Signal(int)
    chipChanged = Signal(str, str)
    mailSignInRequested = Signal(str)
    memoryRequested = Signal(object, str)
    followupsRequested = Signal(str)

    def __init__(self, config: Config, planner: Any, bar: hud.CommandBar, *,
                 context: Callable[[], tuple[Any, Sequence[str]]],
                 blocked: Callable[[], str] = lambda: "",
                 calendars: Mapping[str, Any] | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 live: LiveStream | None = None,
                 route_flags: Callable[[], RouteFlags] | None = None,
                 memory: Callable[[], tuple[Sequence[str], int]] | None = None,
                 mode_inputs: Callable[[], ModeInputs] | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.planner = planner
        self.bar = bar
        self.live = live
        self._route_flags = route_flags or RouteFlags
        self._memory = memory                 # (the notes the planner gets, how many left out); None: off
        self._mode_inputs = mode_inputs or ModeInputs   # the app's to-dos and deadlines (GUI thread)
        self.last_task_id = 0                  # the LIVE view task of the last finished Ask (0: none)
        # While outcomeReady is handled: the LIVE task of that answer (still open; NO_TASK otherwise).
        self.finishing_task: LiveTask = NO_TASK
        self._context = context
        self._blocked = blocked
        self._calendars = dict(calendars or {})
        self._monotonic = monotonic
        self._stop = threading.Event()
        self._bridge = _AskBridge(self)
        queued = Qt.ConnectionType.QueuedConnection
        self._bridge.checked.connect(self._on_checked, queued)
        self._bridge.stage.connect(self._on_stage, queued)
        self._bridge.planned.connect(self._on_planned, queued)
        self._worker: _AskWorker | None = None
        self._seq = 0
        self._running: _Running | None = None
        self.ready: Any = None                 # planner.Readiness of the last check (None before it)
        self.caps: Any = None                  # usage.CapCheck after the last check or Ask
        self.caps_at = -math.inf               # monotonic time of the last ``caps`` (offers re-check stale ones, R13)
        self.mail_needs: list[MailNeed] = []
        self._limit_message = ""               # the Claude plan's own limit, until a run succeeds
        self._answered = False                 # the status line shows an answer (not the idle hint)
        self._notice_until = 0.0               # monotonic: a refused click's words stay on the status line until then
        self._tick = QTimer(self)
        self._tick.setInterval(_TICK_MS)
        self._tick.timeout.connect(self._show_stage)
        bar.submitted.connect(self.submit)
        bar.cancelRequested.connect(self.cancel)
        bar.linkClicked.connect(self._on_link)
        bar.statusDismissed.connect(self._show_idle)
        bar.set_status(CHECKING_TEXT, hud.TONE_IDLE)
        bar.set_meta(f"uses your Claude plan {DOT} nothing happens without your OK")

    # ---- lifecycle ----------------------------------------------------------------------------

    def start(self) -> None:
        """The ask thread, then a readiness check on it (no model request)."""
        if self._worker is None and not self._stop.is_set():
            self._worker = _AskWorker(self.planner, self._bridge, self._stop, self._read_mail_needs,
                                      background=self._background_task)
            logger.info("Ask Jarvis is on (model %s; at most %d planner runs an hour, %d a day)",
                        self.config.ask.model, self.config.ask.max_per_hour, self.config.ask.max_per_day)
        self.refresh()

    def refresh(self, full: bool = True) -> None:
        """Check Claude Code (``full``; it runs only --version, --help and auth status, and only when the
        last check failed) and which accounts' mail can be read, on the ask thread after anything queued."""
        if self._worker is not None and not self._stop.is_set():
            self._worker.put(_CheckJob(self._seq, full))

    def shutdown(self) -> None:
        running, self._running = self._running, None
        if running is not None:
            running.cancel.set()   # the CLI is killed within a second; its Job Object goes with the app
            logger.info("Ask cancelled (app closed); nothing was proposed")
            running.task.finish(STATUS_CANCELLED, summary="Jarvis closed")
        self._stop.set()
        self._tick.stop()
        if self._worker is not None:
            self._worker.stop()

    @property
    def busy(self) -> bool:
        return self._running is not None

    @property
    def task_id(self) -> int:
        """The running Ask's LIVE view task id (0 when idle or without the LIVE view)."""
        return self._running.task.id if self._running is not None else 0

    def caps_age(self) -> float:
        """Seconds since ``caps`` was last updated (infinite before the first check; R13)."""
        return self._monotonic() - self.caps_at if self.caps is not None else math.inf

    @property
    def running_mode(self) -> str:
        """The running request's planner mode (ask.modes.MODE_MEETING ...; "" for an Ask or when idle)."""
        return self._running.mode if self._running is not None else ""

    @property
    def running_kind(self) -> str:
        """The running request's LIVE task kind: live.TASK_WEB for "web:" (web research only),
        live.TASK_ASK otherwise; "" when idle."""
        return self._running.kind if self._running is not None else ""

    def stage(self) -> str:
        """The running Ask's planner.STAGE_* ("" when idle)."""
        return self._running.stage if self._running is not None else ""

    def join(self, timeout: float) -> None:
        """Tests: wait for the ask thread to end after shutdown()."""
        if self._worker is not None:
            self._worker.join(timeout)

    # ---- one Ask --------------------------------------------------------------------------------

    def submit(self, text: str) -> None:
        text = " ".join((text or "").split())
        if self._stop.is_set():
            return
        found = NO_ROUTE
        if text and forced_question(text) is None:   # "web:" always wins over a shortcut
            found = self._route(text)
            if found.kind in MEMORY_ROUTES:
                logger.info("Ask: a typed %s shortcut (no planner run)", found.kind)
                self.memoryRequested.emit(found, text)
                return
            if found.kind == ROUTE_FOLLOWUPS:
                logger.info("Ask: a typed %s shortcut (no planner run)", found.kind)
                self.followupsRequested.emit(text)
                return
        if found.kind == ROUTE_PLAN:
            logger.info("Ask: a typed %s shortcut", found.kind)
            self.submit_mode(ModeRequest(MODE_PLAN, ORIGIN_TYPED, typed=found.payload, part=found.part,
                                         typed_route=True), text)
            return
        if found.kind == ROUTE_PREP:   # the words only pick the meeting: they never reach Claude
            logger.info("Ask: a typed %s shortcut", found.kind)
            self.submit_mode(ModeRequest(MODE_PREP, ORIGIN_TYPED, target=found.payload, typed_route=True), text)
            return
        if self._running is not None:
            self._status(BUSY_TEXT if self._running.cancel_requested else
                         "Jarvis is already planning; wait or Cancel it", hud.TONE_WARN)
            return
        if not text:
            self._status(EMPTY_TEXT, hud.TONE_WARN)
            return
        reason = self._blocked()
        if reason:
            self._status(reason, hud.TONE_WARN)
            return
        if self._worker is None:
            self.start()
        assert self._worker is not None
        briefing, page_ids = self._context()
        self._seq += 1
        kind = TASK_WEB if forced_question(text) is not None else TASK_ASK
        task = _request_task(self.live, text, briefing, kind=kind,
                             memory=self._memory_given() if kind == TASK_ASK else None)
        running = _Running(self._seq, threading.Event(), self._monotonic(), task=task, kind=kind)
        self._running = running
        logger.info("Ask %d started", running.seq)
        self._worker.put(_PlanJob(running.seq, text, briefing, tuple(page_ids), running.cancel,
                                  live=task if task is not NO_TASK else None))
        self.askStarted.emit(running.seq, text)
        self.bar.set_running(True)
        self.bar.set_meta(RUNNING_META)
        self._notice_until = 0.0
        self._show_stage()
        self._tick.start()
        # busyChanged(True) waits for the first stage: an Ask the planner refuses (Claude Code
        # missing or signed out, a limit) never pauses the briefing.

    def submit_mode(self, request: ModeRequest, words: str, *, clear_text: str = "") -> bool:
        """A planner mode run (the productivity bundle: a typed "plan my day", or a click on an offer):
        the same stop / busy / blocked guards as an Ask, then planner.plan_mode() on the ask thread.
        ``words``: what the owner typed (or the link's words for a click), shown as "Your request" and
        in the conversation; never logged. ``clear_text``: for a click-started run, the typed words
        that led to it (a prep's choices): on success the bar is cleared only while it still holds
        exactly them, so what the owner typed since stays (R23). False when it did not start (the
        status line says why, and keeps saying so for NOTICE_S seconds; nothing is queued)."""
        if self._stop.is_set():
            return False
        if self._running is not None:
            if request.typed_route:
                self._status(BUSY_TEXT if self._running.cancel_requested else
                             "Jarvis is already planning; wait or Cancel it", hud.TONE_WARN)
            else:
                self._status(CHOICE_BUSY_TEXT if request.origin == ORIGIN_CHOICE else CLICK_BUSY_TEXT, hud.TONE_WARN)
                self._notice_until = self._monotonic() + NOTICE_S   # the 1 s tick does not overwrite it
            return False
        reason = self._blocked()
        if reason:
            self._status(reason, hud.TONE_WARN)
            return False
        if self._worker is None:
            self.start()
        assert self._worker is not None
        briefing, page_ids = self._context()
        try:
            inputs = self._mode_inputs()
        except Exception as exc:  # noqa: BLE001 - the plan goes on without the app's list
            logger.warning("Ask: the to-dos for a %s run could not be read (%s)", request.mode, type(exc).__name__)
            inputs = ModeInputs()
        self._seq += 1
        kind = MODE_TASKS.get(request.mode, TASK_ASK)
        task = _mode_task(self.live, request, words, kind=kind,
                          memory=None if request.mode == MODE_MEETING else self._memory_given())
        running = _Running(self._seq, threading.Event(), self._monotonic(), task=task, kind=kind,
                           typed=request.typed_route, mode=request.mode, clear_text=clear_text)
        self._running = running
        logger.info("Ask %d started (mode %s, %s)", running.seq, request.mode, request.origin)
        self._worker.put(_ModeJob(running.seq, request, inputs, briefing, tuple(page_ids), running.cancel,
                                  live=task if task is not NO_TASK else None))
        self.askStarted.emit(running.seq, words)
        self.bar.set_running(True)
        self.bar.set_meta(RUNNING_META)
        self._notice_until = 0.0
        self._show_stage()
        self._tick.start()
        return True

    def _route(self, text: str) -> Any:
        try:
            return route(text, self._route_flags())
        except Exception as exc:  # noqa: BLE001 - a shortcut that can't be read is a normal Ask
            logger.warning("Ask: a typed shortcut could not be read (%s)", type(exc).__name__)
            return NO_ROUTE

    def _memory_given(self) -> tuple[Sequence[str], int] | None:
        """(the notes the planner gets, how many were left out) for "Your request"; None while off."""
        if self._memory is None:
            return None
        try:
            return self._memory()
        except Exception:  # noqa: BLE001 - only what the LIVE step shows
            return None

    def show_note(self, text: str, tone: str = hud.TONE_DONE, *, clear: bool = False) -> None:
        """A memory shortcut's answer in the status line (``clear``: the typed words are done with).
        Never while a request runs (the bar belongs to it then)."""
        if self._running is not None or self._stop.is_set():
            return
        self._status(text, tone)
        if clear:
            self.bar.clear_input()

    def cancel(self) -> None:
        running = self._running
        if running is None or running.cancel_requested:
            return
        running.cancel_requested = True
        running.cancel.set()
        logger.info("Ask %d: Cancel clicked", running.seq)
        running.task.note("Cancel clicked")
        self.bar.set_cancel_enabled(False)
        self._status(CANCELLING_TEXT, hud.TONE_WORKING)

    def _on_stage(self, seq: int, stage: str) -> None:
        running = self._running
        if running is None or seq != running.seq or running.cancel_requested:
            return
        running.stage = stage
        if not running.busy_sent:
            running.busy_sent = True
            self.busyChanged.emit(True)
        self._show_stage()

    def _show_stage(self) -> None:
        running = self._running
        if running is None or running.cancel_requested:
            return
        if self._monotonic() < self._notice_until:   # a refused click's words stay a few seconds
            return
        if running.mode:   # "Planning your day... 9 s" from the click on (the meta line: Esc cancels)
            text = f"{MODE_STATUS.get(running.mode, STARTING_TEXT)} {round(self._monotonic() - running.started)} s"
            self._status(text, hud.TONE_WORKING)
            return
        text = STAGE_TEXTS.get(running.stage, STARTING_TEXT)
        if running.stage in _TIMED_STAGES:
            text += f" {round(self._monotonic() - running.started)} s"
        self._status(text, hud.TONE_WORKING)

    def _on_planned(self, seq: int, outcome: Any) -> None:
        running = self._running
        if running is None or seq != running.seq:
            return   # cancelled and replaced, or the app is closing
        self._running = None
        self._tick.stop()
        self._notice_until = 0.0
        self.bar.set_running(False)
        if outcome.caps is not None:
            self.caps = outcome.caps
            self.caps_at = self._monotonic()
        if running.cancel_requested or outcome.kind == CANCELLED:
            logger.info("Ask %d: cancelled; nothing was proposed", seq)
            self._finish_task(running.task, outcome, cancelled=True)
            self._status(CANCELLED_TEXT, hud.TONE_IDLE)
            self._show_meta()
            if running.busy_sent:
                self.busyChanged.emit(False)
            self.askCancelled.emit(seq)
            self._update_chip()
            return
        if outcome.ok:
            self._limit_message = ""
            keep_link = bool(outcome.mail_needed) and any(need.alias == outcome.mail_needed for need in self.mail_needs)
            # A mode with no run is amber, but a brief of Jarvis's own is an answer (it stays as done).
            soft = (outcome.question and not outcome.cards) or (bool(getattr(outcome, "note_kind", ""))
                                                                and getattr(outcome, "brief", None) is None)
            self._status(self._answer(outcome), hud.TONE_WARN if soft else hud.TONE_DONE, keep_link=keep_link)
            done = any(not card.error for card in outcome.cards) or getattr(outcome, "brief", None) is not None
            if done and (running.typed or (running.clear_text and self.bar.text() == running.clear_text)):
                # A question back, choices or nothing to propose keep the draft to edit; a click-started
                # run (an offer, a choice) never touches what the owner may be typing (R23): a choice
                # clears only the very words that led to it, still untouched in the bar.
                self.bar.clear_input()
        else:
            if outcome.kind == LIMIT:
                self._limit_message = outcome.message
            tone = hud.TONE_WARN if outcome.kind in _SOFT_KINDS else hud.TONE_ERROR
            self._status(outcome.message or "Ask failed; nothing was proposed", tone)
        self._show_meta(outcome, web=running.kind == TASK_WEB)
        if running.busy_sent:
            self.busyChanged.emit(False)
        if outcome.ok:
            _cards_step(running.task, outcome, bool(getattr(getattr(self.config, "display", None), "hour24", False)))
        self.finishing_task = running.task
        try:
            self.outcomeReady.emit(outcome)
        finally:
            self.finishing_task = NO_TASK
        self._finish_task(running.task, outcome)
        self._update_chip()
        if not outcome.ok and outcome.kind not in (ask_planner.EMPTY, "busy"):
            self.refresh()   # a failed run checks Claude Code again (the planner forgot its readiness)
        elif outcome.mail.threads or outcome.search or outcome.message:
            self.refresh(full=False)   # mail was read (or could not be): which accounts may read now

    @quiet(NO_TASK)
    def _background_task(self) -> LiveTask:
        """The LIVE view's rolling "Background reads" task ([live] background), else NO_TASK."""
        live_config = getattr(self.config, "live", None)
        if self.live is None or not getattr(live_config, "background", True):
            return NO_TASK
        return self.live.task(TASK_BACKGROUND, BACKGROUND_TITLE, compact=True, key=BACKGROUND_KEY)

    def _finish_task(self, task: LiveTask, outcome: Any, *, cancelled: bool = False) -> None:
        """The Ask's LIVE view task ends as its outcome did (the steps still running end with it)."""
        if task is NO_TASK:
            return
        status, summary = _finish_words(outcome, cancelled=cancelled)
        task.finish(status, summary=summary)
        self.last_task_id = task.id

    @staticmethod
    def _answer(outcome: Any) -> str:
        """The planner's words and Jarvis's own note as a sentence of its own (never in brackets).
        With nothing to decide the note comes first: it says what to do (and survives a cut). A web
        research's answer keeps its [n] source marks (the JARVIS tab numbers the sources). A planner
        mode that did not run (``note_kind``) shows only Jarvis's own words."""
        if getattr(outcome, "mode", "") == MODE_PREP and getattr(outcome, "brief", None) is not None:
            from .persona import PREP_READY_STATUS

            return outcome.message if getattr(outcome, "note_kind", "") else PREP_READY_STATUS
        if getattr(outcome, "mode", "") == MODE_MEETING and getattr(outcome, "brief", None) is not None:
            return MEETING_READY_TEXT   # never the title: the summary is on its own screen
        if getattr(outcome, "note_kind", ""):
            return outcome.message
        parts = [part for part in (outcome.say, outcome.question) if part]
        if not parts and getattr(outcome, "research", None) is not None:
            parts.append(NO_ANSWER)
        if not parts and not outcome.cards:
            parts.append(NOTHING_TEXT + ("." if outcome.message else ""))
        note = (outcome.message or "").strip()
        if note:
            note = note[:1].upper() + note[1:]
            note = note if note.endswith((".", "!", "?", ")")) else note + "."
            first = not any(not card.error for card in outcome.cards)
            parts = [note, *parts] if first else [*parts, note]
        return " ".join(parts)

    # ---- checks, chip and the idle bar -------------------------------------------------------

    def _on_checked(self, _seq: int, ready: Any, caps: Any, needs: Any) -> None:
        if self._stop.is_set():
            return
        if ready is not None:
            self.ready = ready
        self.caps, self.mail_needs = caps, list(needs or [])
        if caps is not None:
            self.caps_at = self._monotonic()
        self._update_chip()
        self._update_link()
        if self._running is None and not self._answered:
            self._show_idle()

    def _show_idle(self) -> None:
        """The bar with nothing to report: why Ask can't run (Claude Code, a limit), or the hint,
        which folds away (the last answer is put away)."""
        if self._running is not None or self._stop.is_set():
            return
        self._answered = False
        ready = self.ready
        caps = self.caps
        if ready is None:
            self.bar.set_status(CHECKING_TEXT, hud.TONE_IDLE)
        elif not ready.ok:
            self.bar.set_status(ready.message, hud.TONE_WARN)
        elif caps is not None and not caps.allowed:
            self.bar.set_status(caps.message, hud.TONE_WARN)
        elif self._limit_message:
            self.bar.set_status(self._limit_message, hud.TONE_WARN)
        else:
            self.bar.set_hint(IDLE_HINT, tooltip=f"{IDLE_HINT}.\n{WEB_HINT}." if self._research_on() else "")
        self._show_meta()

    def _research_on(self) -> bool:
        """[research] enabled (web research needs Ask on too, which it is while this bar exists)."""
        return bool(getattr(getattr(self.config, "research", None), "enabled", False))

    def _status(self, text: str, tone: str, *, keep_link: bool = False) -> None:
        self._answered = True
        self.bar.set_status(text, tone, keep_link=keep_link)

    def _show_meta(self, outcome: Any = None, *, web: bool = False) -> None:
        """The bar's meta line: what the last answer put under ASK, how long it took and the runs
        left this hour; after a web research (or a "web:" request, or the research limit) the web
        research runs left, which the research caps can make fewer."""
        parts: list[str] = []
        report = getattr(outcome, "research", None) if outcome is not None and outcome.ok else None
        if report is not None:
            # "2 source cards + 1 proposal under ASK": the sources' Open cards, then the suggestions.
            proposals = research_proposals(outcome)
            words = cards_words(len(report.sources), proposals)
            parts.append(f"{words} under ASK" if report.sources or proposals else words)
        elif outcome is not None and outcome.ok and getattr(outcome, "mode", "") == MODE_PREP:
            parts += _prep_meta(outcome)
        elif outcome is not None and outcome.ok and getattr(outcome, "mode", "") == MODE_MEETING:
            drafts = sum(1 for card in outcome.cards if not card.error)
            parts.append("summary" + (f" + {_plural(drafts, 'follow-up draft')} under ASK" if drafts else ""))
        elif outcome is not None and outcome.ok:
            cards = sum(1 for card in outcome.cards if not card.error)
            noun = "block" if getattr(outcome, "mode", "") == MODE_PLAN else "proposal"
            if getattr(outcome, "note_kind", ""):
                parts.append("no planner run")
            else:
                parts.append(f"{cards} {noun}{'' if cards == 1 else 's'} under ASK" if cards else f"no {noun}s")
            if outcome.refused:   # a plan's refused blocks are "refused", as ACTIVITY and LIVE say
                parts.append(f"{outcome.refused} {'refused' if noun == 'block' else 'not doable'}")
        if outcome is not None and outcome.runs:
            parts.append(f"{outcome.duration_ms / 1000:.1f} s")
        caps = self.caps
        if caps is not None:
            research_on = self._research_on()
            web = web or report is not None or (outcome is not None
                                                and outcome.kind == ask_planner.RESEARCH_LIMIT)
            research_left = min(caps.left_hour, caps.left_research_hour, caps.left_research_day)
            if research_on and web:
                parts.append(f"{research_left} web research run{'' if research_left == 1 else 's'} left this hour")
            else:
                parts.append(f"{caps.left_hour} run{'' if caps.left_hour == 1 else 's'} left this hour")
                if research_on and min(caps.left_research_hour, caps.left_research_day) <= 0:
                    parts.append(RESEARCH_LIMIT_META)
        if not parts:
            parts.append("uses your Claude plan")
        self.bar.set_meta(f" {DOT} ".join(parts))

    def chip(self) -> tuple[str, str]:
        """The ``claude`` chip now: (planner.CHIP_*, tooltip)."""
        if self.ready is None:
            return ask_planner.CHIP_OFF, "Ask Jarvis: checking Claude Code..."
        if self._limit_message and self.ready.ok:
            return ask_planner.CHIP_LIMIT, self._limit_message
        state, tip = ask_planner.chip_state(self.config.ask.enabled, self.ready, self.caps,
                                            research=self._research_on())
        return state, tip

    def _update_chip(self) -> None:
        state, tip = self.chip()
        self.chipChanged.emit(state, f"Ask Jarvis: {state} - {tip}" if tip else f"Ask Jarvis: {state}")

    def _read_mail_needs(self) -> list[MailNeed]:
        """Ask thread: the accounts whose mail one more Google sign-in would let Ask read (files only)."""
        if not self.config.ask.read_mail:
            return []
        sources = getattr(self.planner, "sources", None)
        reader_of = getattr(sources, "reader", None)
        if not callable(reader_of):
            return []
        needs = []
        for alias in self.config.accounts:
            reader = reader_of(alias)
            if reader is None:
                continue
            try:
                problem, message = reader.available()
            except Exception as exc:  # noqa: BLE001 - only decides whether the bar offers a sign-in
                logger.debug("Ask: could not check the %s account's mail (%s)", logged_alias(alias), type(exc).__name__)
                continue
            if problem not in _SIGN_IN_FIXES:
                continue
            calendar = self._calendars.get(alias)
            if calendar is None:
                continue   # the sign-in goes through the account's calendar (every feature at once)
            if problem == PROBLEM_SIGNED_OUT and not _signed_in(calendar):
                continue   # not signed in at all: the account's own Sign in / Connect is the way
            needs.append(MailNeed(alias, message))
        return needs

    def _update_link(self) -> None:
        need = self.mail_needs[0] if self.mail_needs else None
        if need is None:
            self.bar.set_link("")
            return
        self.bar.set_link(MAIL_LINK_TEXT.format(alias=need.alias),
                          MAIL_LINK_TIP.format(message=need.message.rstrip("."), alias=need.alias))

    def _on_link(self) -> None:
        need = self.mail_needs[0] if self.mail_needs else None
        if need is not None:
            logger.info("Ask: sign-in to read the %s account's mail requested", logged_alias(need.alias))
            self.mailSignInRequested.emit(need.alias)


# --------------------------------------------------------------------------
# The LIVE view's first and last steps of an Ask (on screen only; quiet)
# --------------------------------------------------------------------------

# Refusals before or instead of a planner run: Jarvis's own checks or guards stopped the Ask
# (a web research too: off, a Claude Code without --allowedTools, or more searches than allowed).
_BLOCKED_KINDS = frozenset({ask_planner.DISABLED, ask_planner.CAPS, ask_planner.CLI_MISSING, "not_signed_in",
                            ask_planner.NOT_SUBSCRIPTION, ask_planner.WORKDIR, "guard", LIMIT, "busy",
                            ask_planner.EMPTY, ask_planner.RESEARCH_OFF, ask_planner.RESEARCH_UNSUPPORTED,
                            RESEARCH_CAP, ask_planner.RESEARCH_LIMIT})


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def research_proposals(outcome: Any) -> int:
    """A web research's decidable suggestion cards (a Todo, a new event, an Open page it suggests):
    never the Open cards of its sources (those are counted as sources)."""
    report = getattr(outcome, "research", None)
    sources = source_card_ids(report) if report is not None else frozenset()
    return sum(1 for card in outcome.cards if not card.error and card.id not in sources)


def _briefing_words(briefing: Any) -> str:
    if briefing is None:
        return "none loaded"
    parts = [f"{_plural(len(getattr(briefing, 'pending', ()) or ()), 'card')} waiting",
             _plural(len(getattr(briefing, "sections", ()) or ()), "section"),
             _plural(len(getattr(briefing, "deadlines", ()) or ()), "deadline")]
    return "today's briefing: " + ", ".join(parts)


def memory_words(notes: Sequence[str], left_out: int) -> str:
    """"3 notes, 412 characters (1 not sent: over the 4 KB limit)", or "none", for "Memory given"."""
    if not notes and not left_out:
        return "none"
    text = f"{_plural(len(notes), 'note')}, {sum(len(note) for note in notes):,} characters"
    if left_out:
        text += f" ({left_out} not sent: over the 4 KB limit)"
    return text


@quiet(NO_TASK)
def _request_task(live: LiveStream | None, text: str, briefing: Any, *, kind: str = TASK_ASK,
                  memory: tuple[Sequence[str], int] | None = None) -> LiveTask:
    """A new Ask's LIVE view task and its "Your request" step (NO_TASK without the LIVE view).
    ``kind`` TASK_WEB: a "web:" request (web research only: nothing is read from the accounts).
    ``memory``: (the notes the planner gets, how many left out) while memory is on."""
    if live is None:
        return NO_TASK
    task = live.task(kind, text)
    if task is NO_TASK:
        return NO_TASK
    if kind == TASK_WEB:
        fields = [("You typed", text), ("Mode", WEB_MODE_TEXT), ("Briefing given", WEB_BRIEFING_TEXT)]
    else:
        fields = [("You typed", text), ("Briefing given", _briefing_words(briefing))]
        if memory is not None:
            fields.append(("Memory given", memory_words(*memory)))
    step = task.step(ASK_REQUEST, "Your request", fields=fields)
    if len(text) > COMMAND_CAP:
        step.note(f"Jarvis uses the first {COMMAND_CAP} characters")
    step.done(STATUS_OK)
    return task


@quiet(NO_TASK)
def _mode_task(live: LiveStream | None, request: ModeRequest, words: str, *, kind: str,
               memory: tuple[Sequence[str], int] | None = None) -> LiveTask:
    """A planner mode run's LIVE view task (its own kind: live.TASK_PLAN) and its "Your request" step:
    what the owner typed or clicked (the planner adds what the plan is for and what it is given)."""
    if live is None:
        return NO_TASK
    title = words or f"{MODE_TITLES.get(request.mode, 'Ask')} request"
    task = live.task(kind, title)
    if task is NO_TASK:
        return NO_TASK
    fields = [("You typed" if request.typed_route else "You clicked", words or title)]
    if request.mode == MODE_MEETING:   # a meeting summary: what started it, and that it gets no notes or mail
        started = ("your tick \"summarise it\" on the meeting's start card (it ran once the transcript was ready)"
                   if request.origin == ORIGIN_AUTO else "your click on Summarise")
        given = ("the transcript and the invite's guest names only" if getattr(request.payload, "linked", True)
                 else "the transcript only (not linked to a calendar meeting: no guests)")
        fields = [("Started by", started), ("Given", given)]
    if memory is not None:
        fields.append(("Memory given", memory_words(*memory)))
    step = task.step(MODE_REQUEST, "Your request", fields=fields)
    step.done(STATUS_OK)
    return task


def _card_words(card: Any, hour24: bool = False) -> str:
    """"ASK MOVE - work - Team sync (event abc123) - to Fri Oct 9 2:00-3:00 PM - notify all"; a web
    research source "ASK WEB - example.org - [1] Visit - Example Museum"; a day plan's block "PLAN
    BLOCK - personal - Work on: Lab report - Wed Oct 7 10:00-11:00 AM"."""
    if is_web_open(card):
        return " - ".join(part for part in ("ASK WEB", link_host(card.link), card.title or card.raw) if part)
    if getattr(card, "mode", "") == MODE_PLAN:
        return " - ".join(["PLAN BLOCK", "personal", *ask_planner.card_details(card, hour24)])
    if getattr(card, "mode", "") == MODE_PREP:
        return " - ".join(["PREP REPLY", *ask_planner.card_details(card, hour24)])
    if getattr(card, "mode", "") == MODE_NUDGE:
        return " - ".join(["NUDGE REPLY", *ask_planner.card_details(card, hour24)])
    kind = {"rsvp": "RSVP"}.get(card.kind, card.kind.upper())
    return " - ".join([f"ASK {kind}", *ask_planner.card_details(card, hour24)])


@quiet()
def _cards_step(task: LiveTask, outcome: Any, hour24: bool = False) -> None:
    """Right before the answer is handed over: what Jarvis says and the cards, as returned (a web
    research: its answer, how many sources it showed and left out, and the sources' cards)."""
    if task is NO_TASK:
        return
    step = task.step(ASK_CARDS, "Answer and cards")
    for label, value in (("Jarvis says", outcome.say), ("Question", outcome.question), ("Note", outcome.message)):
        if value:
            step.field(label, value)
    report = getattr(outcome, "research", None)
    if report is not None:
        left = f" ({len(report.left_out)} left out)" if report.left_out else ""
        step.field("Sources", f"{len(report.sources)}{left}")
        if report.dropped:
            step.field("Suggestions left out", str(len(report.dropped)))
    for card in outcome.cards:
        if card.error:
            step.item(_card_words(card, hour24), status=STATUS_BLOCKED, note=f"information only: {card.error}")
        elif card.unverified:
            step.item(_card_words(card, hour24), status=STATUS_WARN,
                      note=f"{_plural(len(card.unverified), 'recipient')} you didn't type - check the card "
                           "before Send")
        else:
            step.item(_card_words(card, hour24), status=STATUS_OK)
    step.field("Answer", "in the JARVIS tab", link="tab:jarvis")
    decidable = sum(1 for card in outcome.cards if not card.error)
    if getattr(outcome, "mode", "") == MODE_PREP and getattr(outcome, "brief", None) is not None:
        summary = "a brief" + (f" and {_plural(decidable, 'reply')} under NEEDS YOUR OK" if decidable else "")
        if getattr(outcome, "note_kind", ""):
            summary += " (no planner run)"
    elif getattr(outcome, "note_kind", ""):
        summary = "no planner run"
    elif report is not None and decidable:
        summary = f"{cards_words(len(report.sources), research_proposals(outcome))} under NEEDS YOUR OK"
    elif decidable:
        summary = f"{_plural(decidable, 'card')} under NEEDS YOUR OK"
    elif outcome.question:
        summary = "a question back"
    else:
        summary = "no proposals"
    step.done(STATUS_OK, summary=summary)


@quiet(NO_STEP)
def _claude_check_step(task: LiveTask) -> LiveStep:
    return task.step(CLAUDE_CHECK, "Checked Claude Code (no model request)", key="claude")


@quiet()
def _claude_checked(step: LiveStep, ready: Any, caps: Any) -> None:
    if ready is None:
        return
    exe = getattr(ready, "exe", None)
    step.field("Version", f"{ready.version or 'unknown'}, {exe.source}" if exe is not None else "not found")
    auth = getattr(ready, "auth", None)
    if auth is not None:
        step.field("Sign-in", "claude.ai plan (claude auth status)" if auth.ok else
                   f"{auth.method or 'not signed in'} - not a claude.ai plan sign-in")
    if caps is not None:
        step.field("Runs left", f"{caps.left_hour} this hour, {caps.left_day} today")
    if ready.ok:
        step.done(STATUS_OK, summary="ready")
    else:
        step.done(STATUS_BLOCKED, summary=ready.message or "not ready")


def _finish_words(outcome: Any, *, cancelled: bool) -> tuple[str, str]:
    """The Ask task's end: DONE, or CHECK (amber) when Jarvis refused or dropped a proposal or a card
    has a recipient you didn't type - the header says so without opening a step."""
    if cancelled or outcome.kind == CANCELLED:
        return STATUS_CANCELLED, "Cancelled; nothing was proposed"
    report = getattr(outcome, "research", None)
    if outcome.ok and report is not None:
        # A web research: "2 source cards + 1 proposal"; CHECK when a source or a suggestion was left out.
        words = cards_words(len(report.sources), research_proposals(outcome))
        problems = [part for part in (f"{_plural(len(report.left_out), 'source')} left out" if report.left_out else "",
                                      f"{_plural(len(report.dropped), 'suggestion')} left out" if report.dropped
                                      else "",
                                      f"{outcome.refused} refused" if outcome.refused else "") if part]
        if problems:
            return STATUS_WARN, f"{words}, {', '.join(problems)}"
        return STATUS_OK, words
    if outcome.ok and getattr(outcome, "mode", "") == MODE_PREP:
        return _prep_finish_words(outcome)
    if outcome.ok and getattr(outcome, "mode", "") == MODE_MEETING:
        return _meeting_finish_words(outcome)
    if outcome.ok and getattr(outcome, "mode", "") == MODE_NUDGE and getattr(outcome, "note_kind", ""):
        from .persona import nudge_no_run_words

        return STATUS_OK, f"no planner run ({nudge_no_run_words(outcome.note_kind)})"
    if outcome.ok and getattr(outcome, "note_kind", ""):
        from .persona import plan_no_run_words

        return STATUS_OK, f"no planner run ({plan_no_run_words(outcome.note_kind)})"
    if outcome.ok:
        decidable = sum(1 for card in outcome.cards if not card.error)
        noun = "block" if getattr(outcome, "mode", "") == MODE_PLAN else "card"
        if decidable:
            words = _plural(decidable, noun)
        else:
            words = "a question back" if outcome.question else f"no {noun}s" if noun == "block" else "no proposals"
        new = sum(len(card.unverified) for card in outcome.cards if not card.error)
        problems = [part for part in (f"{outcome.refused} refused" if outcome.refused else "",
                                      f"{outcome.dropped} dropped" if outcome.dropped else "",
                                      f"{_plural(new, 'recipient')} you didn't type" if new else "") if part]
        if problems:
            return STATUS_WARN, f"{words}, {', '.join(problems)}"
        return STATUS_OK, words
    if outcome.kind in _BLOCKED_KINDS:
        return STATUS_BLOCKED, outcome.message or "Ask was refused; nothing was proposed"
    return STATUS_FAILED, outcome.message or "Ask failed; nothing was proposed"


def _prep_meta(outcome: Any) -> list[str]:
    """The meta line after a meeting brief: "brief + 1 reply under ASK", "brief (no planner run)",
    "no planner run" (choices, no such meeting ...)."""
    replies = sum(1 for card in outcome.cards if not card.error)
    if getattr(outcome, "brief", None) is None:
        return ["no planner run"]
    words = "brief" + (f" + {_plural(replies, 'reply')} under ASK" if replies else "")
    return [words + (" (no planner run)" if getattr(outcome, "note_kind", "") else "")]


def _prep_finish_words(outcome: Any) -> tuple[str, str]:
    """A meeting brief's LIVE task end: "brief ready (2 threads read)", "brief without a planner run
    (no recent thread)", "no planner run (no such meeting)"; CHECK when an item or a reply was left
    out or refused, or a reply has a recipient you didn't type."""
    from .persona import prep_no_run_words

    brief = getattr(outcome, "brief", None)
    kind = getattr(outcome, "note_kind", "")
    if brief is None:
        return STATUS_OK, f"no planner run ({prep_no_run_words(kind)})"
    if kind:
        return STATUS_OK, f"brief without a planner run ({prep_no_run_words(kind)})"
    replies = sum(1 for card in outcome.cards if not card.error)
    words = f"brief ready ({_plural(outcome.mail.threads, 'thread')} read)"
    if replies:
        words += f", {_plural(replies, 'reply')}"
    new = sum(len(card.unverified) for card in outcome.cards if not card.error)
    problems = [part for part in (f"{brief.dropped} item(s) left out" if brief.dropped else "",
                                  f"{outcome.refused} refused" if outcome.refused else "",
                                  f"{outcome.dropped} dropped" if outcome.dropped else "",
                                  f"{_plural(new, 'recipient')} you didn't type" if new else "") if part]
    if problems:
        return STATUS_WARN, f"{words}, {', '.join(problems)}"
    return STATUS_OK, words


def _meeting_finish_words(outcome: Any) -> tuple[str, str]:
    """A meeting summary's LIVE task end: "summary ready (3 actions), 1 follow-up draft"; CHECK when an item was
    left out, a draft refused, or a draft has a recipient you didn't type (NEW until its tick)."""
    summary = getattr(outcome, "brief", None)
    counts = summary.counts() if summary is not None else {}
    drafts = sum(1 for card in outcome.cards if not card.error)
    words = f"summary ready ({_plural(int(counts.get('actions', 0)), 'action')})"
    if drafts:
        words += f", {_plural(drafts, 'follow-up draft')}"
    new = sum(len(card.unverified) for card in outcome.cards if not card.error)
    dropped = int(counts.get("dropped", 0))
    problems = [part for part in (f"{dropped} item(s) left out" if dropped else "",
                                  f"{outcome.refused} refused" if outcome.refused else "",
                                  f"{_plural(new, 'recipient')} you didn't type" if new else "") if part]
    if problems:
        return STATUS_WARN, f"{words}, {', '.join(problems)}"
    return STATUS_OK, words


def _signed_in(calendar: Any) -> bool:
    try:
        return bool(calendar.is_signed_in())
    except Exception:  # noqa: BLE001
        return False


def default_planner(config: Config, calendars: Mapping[str, Any], senders: Mapping[str, Any],
                    accounts: Mapping[str, Any] | None = None) -> Any:
    """The app's planner: the owner's Claude Code (ClaudeEngine) and the app's own Google objects,
    read only (GoogleSources). ``accounts`` are executor.build_accounts' (shared with the calendars
    and senders, one sign-in each); the Gmail readers of the "gmail_read" accounts are built on them."""
    from .executor import build_readers

    readers = build_readers(config, accounts) if accounts and config.ask.read_mail else {}
    engine = ask_planner.ClaudeEngine(config.ask, config.data_dir)
    sources = ask_planner.GoogleSources(config, calendars, senders, readers)
    return ask_planner.AskPlanner(config, engine, sources)
