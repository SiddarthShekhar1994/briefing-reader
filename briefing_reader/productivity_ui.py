"""The productivity bundle in the app: Jarvis's own day read and his offers (spec 4, 15).

    ProductivityController(config, *, sources, live, now, activity, facts, ask_state, inputs, ...)
        sync()                   the 60 s timer runs while the reading screen is visible or minimized
        tick()                   the lock polled, the day read when due, prep offers against the
                                 schedule, the plan offer candidate re-checked, held speech retried,
                                 offers expired (an unclicked recurring prep offer counts as ignored)
    A sentence is said only at a quiet moment, one offer's at a time, 5 minutes apart (one queued but
    not started yet counts), and never from a schedule older than 30 minutes or for a meeting a later
    read found moved or cancelled (it becomes text only).
        on_announced(key) ...    the app's hooks (the AM announcement, playback, viewed, tab changes,
                                 the voice starting or dropping a sentence, unlock, a plan made)
        link(id) / offer(id) / use_offer(id) / brief_ready(id, at) / set_entry(id, entry_id) / planned(key)
        on_muted()               Ctrl+M: every offer not yet said is text only for good (R37)
    Signals (GUI thread):
        offerPosted(Offer)       the app adds the JARVIS entry with its one link (and flashes the taskbar
                                 button for a minimized prep offer)
        offerSpeak(Offer)        the offer's one sentence may be said now (KIND_NOTICE, key offer:<id>)
        offerRetired(Offer, how) "expired" | "planned" | "ended" | "used": the entry loses its link
        offerChanged(Offer)      its sub line is refreshed (the cost against the limits, R49)
        offerUnsaid(Offer)       its sentence, queued but not started, is taken back (the app cancels the key
                                 offer:<id>): not said where it may not be (the reading screen gone, minimized,
                                 locked) or for a meeting a later read found moved or cancelled
        dayChanged()             the schedule was read again
        ticked()                 a tick ran (the Done tooltip follows the schedule, R34)
        followupsChanged(view)   the follow-up list changed (followups.FollowupView: a scan or a re-fetch
                                 landed, a row was hidden or shown again, the list was turned off or on)

The follow-up list (B5, spec 3.4, 5.3): while the reading screen is visible, 20 s after it came up and
then every scan_every_minutes, a _ScanJob on the prod thread reads each confirmed account's mail
headers (followups.MailScanner: metadata only, the last 14 days, SENT first); a Google sign-in that
opens cancels a running scan between two reads (R19) and bumps nothing until it completes
(on_signed_in bumps the account's sign-in counter, the scanner's stop key). refetch(row_id) reads one
listed thread's headers again (_RefetchJob, R47). request_scan / hide / unhide / set_off / followups()
/ participants() / followup_counts() are what the app asks; nothing here reads a body or calls Claude.

An offer never acts: nothing is read from mail and no planner runs until the owner clicks its link
(the app then starts the B2 plan or the B3 brief as a click-started mode run). The day schedule is
read by a daemon thread of its own ("prod"; never the calendar worker that runs approvals, never
the ask thread) with the personal calendar's zone and the owner's own addresses (R16); the GUI
thread never calls Google and never reads a file here. Every read and every offer decision is a
step of the rolling own-reads LIVE task, whatever [live] background says (R6). Each prod job runs
in its own try/except that logs only the job kind and the exception type (R8). Nothing here logs a
title, a guest, an address or Jarvis's words.

This module never imports ``ui`` (ui imports it; R22): results come back through queued signals
with the stop guard of ``_ProdWorker._emit``. ``inline=True`` runs the prod jobs on the caller's
thread (tests, harnesses).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from . import hud, persona
from . import live as _live
from .ask import dayplan, offers
from .ask.modes import EventRef, ModeInputs
from .ask.offers import QuietFacts
from .ask.planner import _day_bounds as day_bounds
from .ask.prep import find_event, other_guests
from .config import DEFAULT_ACCOUNT
from .followups import (NOT_SCANNED, PROBLEM_IDENTITY, PROBLEM_REFUSED, PROBLEM_STOPPED, PROBLEM_UNCONFIRMED,
                        PROBLEM_UNREADABLE, FollowupItem, FollowupView, MailScanner, ScanReport, make_view)
from .gcal import EventBrief, wall_in_zone
from .google_auth import logged_alias

logger = logging.getLogger(__name__)

TICK_MS = 60_000
DAY_READ_EVERY = timedelta(minutes=10)      # while visible
CAPS_STALE_S = 300.0                        # a cached CapCheck older than this is refreshed first (R13)
CAPS_REFRESH_EVERY = timedelta(minutes=1)   # ... asked for at most this often
QUEUED_FOR = timedelta(seconds=75)          # a queued sentence the voice never started was dropped (stale)
MINIMIZED_CHECK_HOURS = 1                   # the hour read around a meeting before a minimized offer (R35)
ACT_VISIBLE, ACT_MINIMIZED, ACT_OFF = "visible", "minimized", "off"
HELD, QUEUED, SPOKEN, TEXT_ONLY = "held", "queued", "spoken", "text_only"
RETIRE_EXPIRED, RETIRE_PLANNED, RETIRE_ENDED, RETIRE_USED = "expired", "planned", "ended", "used"
READ_SHOWN, READ_TICK, READ_PLAN, READ_MINIMIZED, READ_SIGN_IN = (
    "shown", "tick", "plan_offer", "minimized_check", "sign_in")
JOB_DAY, JOB_SCAN, JOB_REFETCH = "day", "scan", "refetch"
FIRST_SCAN_AFTER = timedelta(seconds=20)    # the first header scan waits this long after the screen came up
FIRST_SCAN_MS = 20_500
READ_OFF_NOTE = "read_off"                  # FollowupView.notes: [ask] read_mail is off
_LIVE_PLAN_KEY = "plan-offer"
_LIVE_DAY_KEY = "day"
LINK_GONE = "gone"                          # a click on a link with no valid offer behind it (used, retired, unknown)
# Not a meeting worth a LIVE line or a log line: all day, declined, already under way (or about to start).
_QUIET_PREP_REASONS = frozenset({"all_day", "declined", "late"})
# A quiet reason that settles a meeting whose "Not offered yet" line is already shown (the line says so).
_QUIET_WORDS = {"late": "too close to its start", "all_day": "all day", "declined": "you declined it",
                "guests": "no other guests"}


@dataclass(frozen=True)
class _DayJob:
    """One read of the schedule on the prod thread: today (``aliases`` None: every signed-in calendar)
    with the personal zone, or the hour around one meeting before a minimized prep offer."""

    seq: int
    start: datetime
    end: datetime
    aliases: tuple[str, ...] | None
    with_zone: bool
    reason: str
    key: str = ""                       # the prep offer key the minimized check serves

    def __repr__(self) -> str:
        return f"_DayJob({self.seq}, {self.reason})"


@dataclass(frozen=True)
class DayRead:
    """What a day job read (memory only): {alias: (briefs, status)} and the owner's own addresses
    (the bound address of every account, so a meeting's "other guests" leave him out)."""

    results: Mapping[str, tuple[tuple[EventBrief, ...], str]]
    own: frozenset[str] = frozenset()

    def __repr__(self) -> str:
        events = sum(len(briefs) for briefs, _status in self.results.values())
        return f"DayRead({len(self.results)} account(s), {events} event(s))"


@dataclass(frozen=True)
class _ScanJob:
    """One header scan of every account that may be read (B5), on the prod thread."""

    seq: int
    aliases: tuple[str, ...]
    cancel: threading.Event = field(repr=False, compare=False)
    counters: Mapping[str, int] = field(default_factory=dict, repr=False, compare=False)
    steps: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    reason: str = ""

    def __repr__(self) -> str:
        return f"_ScanJob({self.seq}, {len(self.aliases)} account(s))"


@dataclass(frozen=True)
class _RefetchJob:
    """One listed thread's headers read again (R47), on the prod thread."""

    seq: int
    alias: str
    thread_id: str = field(repr=False)
    row_id: str = field(default="", repr=False)
    reason: str = ""
    step: Any = field(default=None, repr=False, compare=False)

    def __repr__(self) -> str:
        return f"_RefetchJob({self.seq}, {self.reason})"


class _ProdBridge(QObject):
    """Signals the prod thread emits; connected queued, so the slots run on the GUI thread."""

    dayRead = Signal(int, object, object, str)   # seq, DayRead, read_at, personal zone (R16)
    scanned = Signal(int, object)                # seq, {alias: followups.ScanResult} (B5)
    refetched = Signal(int, object)              # seq, FollowupItem | reason word | None (R47)
    jobFailed = Signal(int, str)                 # seq, job kind ("day" | "scan" | "refetch"): "failed" (R8)


class _ProdWorker:
    """The daemon thread "prod": day reads, header scans and single-thread re-fetches, one job at a
    time (read only; never a planner run, a send or a sign-in). ``inline``: no thread, each job runs
    when put."""

    def __init__(self, sources: Any, bridge: _ProdBridge, stop: threading.Event, *, now: Callable[[], datetime],
                 calendars: Sequence[str], inline: bool = False) -> None:
        self._sources = sources
        self._bridge = bridge
        self._stop = stop
        self._now = now
        self.calendars = tuple(calendars)
        self._inline = inline
        # The header scan's parts (set by the controller; B5).
        self.scanner: MailScanner | None = None
        self.settings: Any = None
        self.known: Callable[[str], bool] = lambda _address: False
        self.hidden: Callable[[str], bool] = lambda _key: False
        self._jobs: queue.SimpleQueue[Any] | None = None if inline else queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        if not inline:
            self._thread = threading.Thread(target=self._run, name="prod", daemon=True)
            self._thread.start()

    def put(self, job: Any) -> None:
        if self._stop.is_set():
            return
        if self._jobs is None:
            self._run_job(job)
        else:
            self._jobs.put(job)

    def stop(self) -> None:
        if self._jobs is not None:
            self._jobs.put(None)

    def join(self, timeout: float) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        assert self._jobs is not None
        while True:
            job = self._jobs.get()
            if job is None or self._stop.is_set():
                return
            self._run_job(job)

    def _run_job(self, job: Any) -> None:
        kind = JOB_SCAN if isinstance(job, _ScanJob) else JOB_REFETCH if isinstance(job, _RefetchJob) else JOB_DAY
        try:
            if kind == JOB_SCAN:
                self._scan(job)
            elif kind == JOB_REFETCH:
                self._refetch(job)
            else:
                self._day(job)
        except Exception as exc:  # noqa: BLE001 - a header or a title could be in the message: the type only (R8)
            logger.error("Productivity: %s job failed (%s)", kind, type(exc).__name__)
            self._emit("jobFailed", getattr(job, "seq", 0), kind)

    def _scan(self, job: _ScanJob) -> None:
        scanner = self.scanner
        if scanner is None:
            return
        results: dict[str, Any] = {}
        for alias in job.aliases:
            if self._stop.is_set():
                return
            reader = self._reader(alias)
            if reader is None or not callable(getattr(reader, "scan_ids", None)):
                continue   # a reader that cannot list headers (never the app's GmailReader) is not scanned
            key = f"{_identity(reader)}|{int(job.counters.get(alias, 0))}"
            results[alias] = scanner.scan(alias, reader, now=self._now(), settings=self.settings, known=self.known,
                                          hidden=self.hidden, cancel=job.cancel, step=job.steps.get(alias),
                                          stop_key=key)
        self._emit("scanned", job.seq, results)

    def _refetch(self, job: _RefetchJob) -> None:
        scanner = self.scanner
        reader = self._reader(job.alias)
        result = None
        if scanner is not None and reader is not None:
            result = scanner.refetch(job.alias, job.thread_id, reader, now=self._now(), settings=self.settings,
                                     known=self.known, hidden=self.hidden, step=job.step)
        self._emit("refetched", job.seq, result)

    def _reader(self, alias: str) -> Any:
        """The account's Gmail reader (the Ask's: GoogleSources.readers), else None."""
        readers = getattr(self._sources, "readers", None)
        return readers.get(alias) if isinstance(readers, Mapping) else None

    def _day(self, job: _DayJob) -> None:
        aliases = job.aliases if job.aliases is not None else self._signed_in_aliases()
        results: dict[str, tuple[tuple[EventBrief, ...], str]] = {}
        for alias in aliases:
            if self._stop.is_set():
                return
            briefs, status = self._sources.event_briefs(alias, job.start, job.end, self.calendars)
            results[alias] = (tuple(briefs), str(status))
        zone = str(self._sources.time_zone() or "") if job.with_zone else ""
        own = _own_addresses(self._sources) if job.with_zone else frozenset()
        self._emit("dayRead", job.seq, DayRead(results, own), self._now(), zone)

    def _signed_in_aliases(self) -> list[str]:
        calendars = getattr(self._sources, "calendars", None) or {}
        found = []
        for alias, calendar in calendars.items():
            try:
                if calendar is not None and bool(calendar.is_signed_in()):
                    found.append(alias)
            except Exception:  # noqa: BLE001 - that account is left out of this read
                continue
        return found

    def _emit(self, name: str, *args: Any) -> None:
        if self._stop.is_set():
            return
        try:
            getattr(self._bridge, name).emit(*args)
        except RuntimeError:
            pass   # the bridge was deleted during shutdown


def _identity(reader: Any) -> str:
    getter = getattr(reader, "identity_key", None)
    try:
        return str(getter()) if callable(getter) else ""
    except Exception:  # noqa: BLE001
        return ""


def _own_addresses(sources: Any) -> frozenset[str]:
    """The owner's own addresses the app knows (each account's bound address; prod thread only: it
    may read the accounts file). Casefolded; never logged."""
    found: set[str] = set()
    for handle in list((getattr(sources, "senders", None) or {}).values()):
        getter = getattr(handle, "from_address", None)
        try:
            address = getter() if callable(getter) else ""
        except Exception:  # noqa: BLE001 - that one stays unknown
            address = ""
        if isinstance(address, str) and "@" in address:
            found.add(address.strip().casefold())
    for handle in list((getattr(sources, "calendars", None) or {}).values()):
        getter = getattr(getattr(handle, "account", None), "bound_email", None)
        try:
            address = getter() if callable(getter) else ""
        except Exception:  # noqa: BLE001
            address = ""
        if isinstance(address, str) and "@" in address:
            found.add(address.strip().casefold())
    return frozenset(found)


@dataclass
class Offer:
    """One offer (memory only): its kind, times, the event (prep), the entry it became, and what became
    of its sentence (R26: ``spoken`` only once the voice started it; R37: text only is never retried).
    ``event_title`` is shown on screen only; the repr names kinds and states."""

    id: int
    kind: str                          # offers.KIND_PLAN | offers.KIND_PREP
    posted_at: datetime                # the clock's time at posting
    expires_at: datetime               # plan: the wall time of plan_offer_until; prep: the meeting's end
    slot_key: str = ""                 # plan: today's "YYYY-MM-DD AM"
    event: EventRef | None = None
    event_title: str = field(default="", repr=False)
    alias: str = ""
    start: datetime | None = None      # prep: the meeting's start
    guests: int = 0
    lead_minutes: int = 0
    read_at: datetime | None = None    # prep: the schedule's read time ("as of 1:40 PM")
    entry_id: int = 0
    used: bool = False                 # its brief is below (R50)
    clicked: bool = False
    speech: str = ""                   # the locally built sentence (times and counts only)
    speech_state: str = HELD           # HELD | QUEUED | SPOKEN | TEXT_ONLY
    held_reason: str = ""              # the last may_speak reason word
    spoken_how: str = ""               # finished | viewed | after_20_min | restore | unlock | tick | posted
    queued_at: datetime | None = None  # when the sentence went to the voice (dropped if never started)
    group: tuple[int, ...] = ()        # offer ids spoken together (R51)
    series_key: str = ""               # prep_series.json key ("" when not recurring, R38)
    stale: bool = False                # a minimized offer from a schedule older than 30 min, not re-checked
    flash: bool = False                # the app flashes the taskbar button (minimized prep offer)
    sub: str = ""                      # the sub line as last shown (R49)
    live_key: str = field(default="", repr=False)

    def __repr__(self) -> str:
        return f"Offer({self.id}, {self.kind}, {self.speech_state})"


def _wall(moment: datetime, zone: str) -> datetime:
    return wall_in_zone(moment, zone) if zone else moment.replace(tzinfo=None)


class ProductivityController(QObject):
    """The bundle's timer, day read and offers (see the module docs). GUI thread only, except the
    prod jobs (read only) on their own thread."""

    offerPosted = Signal(object)
    offerSpeak = Signal(object)
    offerRetired = Signal(object, str)
    offerChanged = Signal(object)
    offerUnsaid = Signal(object)
    dayChanged = Signal()
    ticked = Signal()
    followupsChanged = Signal(object)

    def __init__(self, config: Any, *, sources: Any, live: Any, now: Callable[[], datetime],
                 activity: Callable[[], str], facts: Callable[[], QuietFacts],
                 ask_state: Callable[[], tuple[bool, bool, float]], inputs: Callable[[], ModeInputs],
                 known_recipient: Callable[[str], bool] | None = None, prefs: Any = None, hidden: Any = None,
                 series: Any = None, refresh_caps: Callable[[], None] = lambda: None,
                 signing_in: Callable[[], bool] = lambda: False, run_state: Any = None,
                 caps: Callable[[], Any] | None = None, parent: QObject | None = None,
                 inline: bool = False) -> None:
        super().__init__(parent)
        self.config = config
        self.settings = getattr(config, "productivity", None)
        self.sources = sources
        self._live = live
        self._now = now
        self._activity = activity
        self._facts = facts
        self._ask_state = ask_state
        self._inputs = inputs
        self._known_recipient = known_recipient   # the follow-ups build (B5)
        self._prefs = prefs
        self._hidden = hidden                     # the follow-ups build (B5)
        self._series = series
        self._refresh_caps = refresh_caps
        self._signing_in = signing_in
        self._run_state = run_state
        self._caps = caps or (lambda: None)       # the cached CapCheck (the offers' sub lines)
        self._inline = inline
        self._closed = False
        self._stop = threading.Event()
        self._bridge = _ProdBridge(self)
        connection = Qt.ConnectionType.DirectConnection if inline else Qt.ConnectionType.QueuedConnection
        self._bridge.dayRead.connect(self._on_day_read, connection)
        self._bridge.scanned.connect(self._on_scanned, connection)
        self._bridge.refetched.connect(self._on_refetched, connection)
        self._bridge.jobFailed.connect(self._on_job_failed, connection)
        calendars = tuple(getattr(getattr(config, "ask", None), "calendars", ()) or ("primary",))
        self._worker = _ProdWorker(sources, self._bridge, self._stop, now=now, calendars=calendars, inline=inline)
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self.tick)
        self._first_scan_timer = QTimer(self)
        self._first_scan_timer.setSingleShot(True)
        self._first_scan_timer.setInterval(FIRST_SCAN_MS)
        self._first_scan_timer.timeout.connect(self._first_scan)
        self.ticks = 0
        settings = self.settings
        self._plan_on = bool(getattr(settings, "plan_offer", False))
        self._prep_on = bool(getattr(settings, "prep_offers", False))
        # The schedule (memory only, never logged: counts only).
        self._day: dict[str, tuple[EventBrief, ...]] = {}
        self._day_statuses: dict[str, str] = {}
        self._day_own: frozenset[str] = frozenset()
        self._day_read_at: datetime | None = None
        self._alias_read_at: dict[str, datetime] = {}          # each calendar's last successful day read
        self._day_requested_at: datetime | None = None
        self._day_zone = ""
        self._day_complete = False
        self._day_seq = 0
        self._day_in_flight = False
        self._jobs: dict[int, tuple[_DayJob, datetime]] = {}
        self._hour_reads: tuple[date, int] = (date.min, 0)      # minimized checks today (<= prep_offers_per_day)
        self._pending_checks: dict[str, int] = {}               # prep offer key -> the hour read's seq
        # The hours re-read while minimized (R35): (alias, start, end, read_at); an event inside one read
        # within the last 30 minutes is as fresh as that read (never read twice for one candidate).
        self._hour_checked: list[tuple[str, datetime, datetime, datetime]] = []
        self._hour_failed: set[str] = set()                     # keys whose hour could not be read (not again)
        self._caps_asked_at: datetime | None = None
        # Offers.
        self._offers: dict[int, Offer] = {}
        self._offer_seq = 0
        self._last_spoken_at: datetime | None = None            # wall time
        self._last_activity = ACT_OFF
        self._shown_once = False
        self._locked_last = False
        # The plan offer candidate (4.3).
        self._plan_key = ""
        self._plan_candidate = False
        self._plan_offer: Offer | None = None
        self._plan_final_logged: set[str] = set()
        self._plan_pending = ""
        self._plan_offered_keys: set[str] = set()
        self._planned_keys: set[str] = set()
        self._announced_at: datetime | None = None
        self._playback_started = False
        self._finished = False
        self._viewed = False
        self._viewed_then_jarvis = False
        # Prep offers (4.4).
        self._prep_offered: set[str] = set()
        self._prep_skipped: dict[str, str] = {}
        self._prep_today: tuple[date, int] = (date.min, 0)
        self._last_group_at: datetime | None = None
        # The follow-up list (B5): memory only, never logged but counts.
        self._scanner = MailScanner(clock=now, sleep=(lambda _seconds: None) if inline else time.sleep)
        self._worker.scanner = self._scanner
        self._worker.settings = settings
        self._worker.known = self._known
        self._worker.hidden = self._is_hidden
        self._scan_seq = 0
        self._scan_job: _ScanJob | None = None
        self._scan_requested_at: datetime | None = None
        self._scanned_at: datetime | None = None
        self._visible_since: datetime | None = None
        self._signins: dict[str, int] = {}
        self._items: dict[str, list[FollowupItem]] = {}
        self._hidden_items: dict[str, list[FollowupItem]] = {}
        self._reports: dict[str, ScanReport] = {}
        self._participants: dict[str, dict[str, tuple[Any, ...]]] = {}
        self._checking: set[str] = set()
        self._refetches: dict[int, _RefetchJob] = {}
        logger.info("Productivity: on (plan offer %s, prep offers %s, follow-ups %s)",
                    "on" if self._plan_on else "off", "on" if self._prep_on else "off",
                    "on" if getattr(settings, "followups", False) else "off")

    # ---- lifecycle ----------------------------------------------------------------------------

    def sync(self) -> None:
        """Start or stop the timer from ``activity()``; the first visible show reads the day and
        posts the one-time candidates; a restore retries held speech. A sentence queued but not
        started that may not be said in the new state (the reading screen gone; minimized: the plan
        offer, or a prep offer with prep_speak_minimized off) is taken back and held (4.1, 4.2)."""
        if self._closed:
            return
        act = self._safe_activity()
        was = self._last_activity
        self._last_activity = act
        self._take_back(act)
        if act == ACT_OFF:
            self._timer.stop()
            self._visible_since = None
            self._first_scan_timer.stop()
            return
        if not self._timer.isActive():
            self._timer.start()
        if act != ACT_VISIBLE:
            self._visible_since = None
            self._first_scan_timer.stop()
        elif was != ACT_VISIBLE or self._visible_since is None:
            self._visible_since = self._now()
            if self.scan_on():
                self._first_scan_timer.start()   # the first scan 20 s after the screen came up (15)
        if act == ACT_VISIBLE:
            if not self._shown_once:
                self._shown_once = True
                self.on_reading_shown()
            elif was == ACT_MINIMIZED:
                self._retry_held("restore")
            if self._day_read_due(self._now()):
                self.request_day(READ_SHOWN)

    def shutdown(self) -> None:
        """Stop the timer and the prod thread; nothing is emitted after this."""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._timer.stop()
        self._first_scan_timer.stop()
        if self._scan_job is not None:
            self._scan_job.cancel.set()
        self._worker.stop()

    def join(self, timeout: float) -> None:
        """Tests: wait for the prod thread to end after shutdown()."""
        self._worker.join(timeout)

    @property
    def timer_active(self) -> bool:
        return self._timer.isActive()

    @property
    def closed(self) -> bool:
        return self._closed

    def tick(self) -> None:
        """Every 60 s while visible or minimized (spec 15): cheap, no I/O on the GUI thread. A sentence
        queued but not started when the lock is seen is taken back (said after the unlock)."""
        if self._closed:
            return
        self.ticks += 1
        now = self._now()
        locked = self._safe_facts().locked
        if self._locked_last and not locked:
            self._locked_last = False
            self.on_unlocked()
        self._locked_last = locked
        act = self._safe_activity()
        self._last_activity = act
        self._take_back(act, locked=locked)
        if self._scan_job is not None and self._safe_signing_in():
            self.cancel_scan()   # a Google sign-in opened: the running scan stops between two reads (R19)
        if act == ACT_OFF:
            self._timer.stop()
            return
        if act == ACT_VISIBLE and self._day_read_due(now):
            self.request_day(READ_TICK)
        if act == ACT_VISIBLE and self._scan_due(now):
            self.request_scan(READ_TICK)
        self._evaluate_prep_offers(now, act)
        self._evaluate_plan_offer(now, "tick")
        self._refresh_sub_lines()
        self._requeue_dropped(now)
        self._retry_held("tick")
        self._expire_offers(now)
        self.ticked.emit()

    # ---- hooks from the app --------------------------------------------------------------------

    def on_announced(self, key: str) -> None:
        """The AM briefing was announced (``_set_announced``): today's key becomes the plan offer
        candidate; the entry may be posted at once, its speech waits for "heard" (R26)."""
        if self._closed or not key.endswith(" AM"):
            return
        now = self._now()
        if key != self._plan_key:
            self._new_candidate(key)
        self._plan_candidate = True
        self._announced_at = now
        self._evaluate_plan_offer(now, "announced")

    def on_reading_shown(self) -> None:
        """The first visible show of the reading screen: on a day whose AM briefing was announced or
        viewed before (runstate), today's key becomes the candidate (a briefing viewed before counts
        as heard)."""
        if self._closed:
            return
        now = self._now()
        key = f"{_wall(now, self.day_zone()):%Y-%m-%d} AM"
        state = self._run_state
        if not self._plan_candidate and state is not None:
            try:
                announced = bool(state.is_announced(key))
                viewed = bool(state.is_viewed(key))
            except Exception:  # noqa: BLE001 - the run state is only a hint here
                announced = viewed = False
            if announced or viewed:
                self._new_candidate(key)
                self._plan_candidate = True
                self._announced_at = now
                self._viewed = self._viewed_then_jarvis = viewed
                self._evaluate_plan_offer(now, "shown")
        if self._day_read_due(now):
            self.request_day(READ_SHOWN)

    def _new_candidate(self, key: str) -> None:
        self._plan_key = key
        self._plan_final_logged = set()
        self._plan_pending = ""
        self._playback_started = self._finished = self._viewed = self._viewed_then_jarvis = False

    def on_playback_started(self, key: str = "") -> None:
        """The briefing ``key`` started playing (another slot's briefing is not the AM one)."""
        if not key or key == self._plan_key:
            self._playback_started = True

    def on_playback_finished(self, key: str = "") -> None:
        """The briefing ``key`` reached FINISHED: the AM briefing was heard (R26 a)."""
        if key and key != self._plan_key:
            return
        self._finished = True
        self._retry_held("finished")

    def on_viewed(self, key: str) -> None:
        if key and key == self._plan_key:
            self._viewed = True

    def on_tab_changed(self, tab: int) -> None:
        """JARVIS became current after the AM briefing was viewed: heard (R26 b)."""
        if tab == hud.TAB_JARVIS and self._viewed and not self._viewed_then_jarvis:
            self._viewed_then_jarvis = True
            self._retry_held("viewed")

    def on_speaking(self, text: str) -> None:
        """The voice started ``text``: the queued offer with that sentence is spoken (R26)."""
        if not text:
            return
        for offer in self._offers.values():
            if offer.speech_state == QUEUED and offer.speech == text:
                offer.speech_state = SPOKEN
                self._last_spoken_at = _wall(self._now(), self.day_zone())
                logger.info("%s offer: spoken (%s)", _kind_word(offer), offer.spoken_how or "tick")
                self._live_spoken(offer, "yes")

    def on_speech_dropped(self) -> None:
        """A queued sentence was dropped before it started (the owner pressed Play, a stop): the offer
        goes back to held (R26)."""
        for offer in self._offers.values():
            if offer.speech_state == QUEUED:
                self._back_to_held(offer)

    def on_muted(self) -> None:
        """The owner muted Jarvis (Ctrl+M or the speaker button): every offer whose sentence was not
        said yet (held, or queued and now dropped with the voice's queue) is text only for good, so
        unmuting never brings a late "Shall I ...?" (R37)."""
        for offer in self._offers.values():
            if offer.speech_state in (HELD, QUEUED):
                offer.speech_state = TEXT_ONLY
                offer.held_reason = "muted"
                offer.queued_at = None
                logger.info("%s offer: text only (muted)", _kind_word(offer))
                self._live_spoken(offer, "text only (muted)")

    def speech_failed(self, offer_id: int) -> None:
        """The voice would not take the sentence (off, muted meanwhile, failed): text only."""
        offer = self._offers.get(offer_id)
        if offer is not None and offer.speech_state == QUEUED:
            offer.speech_state = TEXT_ONLY
            offer.held_reason = "voice"
            self._live_spoken(offer, "text only (voice)")

    def on_unlocked(self) -> None:
        self._locked_last = False
        self._retry_held("unlock")

    def on_signed_in(self, alias: str) -> None:
        """A Google sign-in for ``alias`` completed: the schedule is read again when visible, and the
        account's sign-in counter goes up, so a scan its refusal stopped runs again (R19)."""
        if self._closed:
            return
        self._signins[alias] = self._signins.get(alias, 0) + 1   # a stopped account is scanned again (R19)
        self._day_read_at = None
        self._day_requested_at = None
        if self._safe_activity() == ACT_VISIBLE:
            self.request_day(READ_SIGN_IN)

    def planned(self, key: str) -> None:
        """A plan with at least one block was made for the day of ``key`` (3.2 step 9): remembered
        (runstate; also tomorrow's key, R29) and an open plan offer for it is retired."""
        if not key:
            return
        self._planned_keys.add(key)
        state = self._run_state
        if state is not None:
            try:
                state.record_planned(key)
            except Exception as exc:  # noqa: BLE001 - bookkeeping never stops the answer
                logger.warning("Plan offer: could not record the plan (%s)", type(exc).__name__)
        offer = self._plan_offer
        if offer is not None and offer.slot_key == key:
            self._retire(offer, RETIRE_PLANNED)
        if key == self._plan_key:
            self._plan_candidate = False

    def set_entry(self, offer_id: int, entry_id: int) -> None:
        offer = self._offers.get(offer_id)
        if offer is not None:
            offer.entry_id = int(entry_id)

    def link(self, offer_id: int) -> tuple[Offer | None, str]:
        """A click on an offer's link: (the offer, "") while it is valid; (None, "gone") for a used,
        retired or unknown one; (the offer, "expired" | "ended") for a plan offer past
        plan_offer_until or a prep offer whose meeting ended since the last tick. That one is retired
        at once (its entry says so; nothing is read), and a clicked recurring meeting never counts as
        ignored."""
        offer = self._offers.get(offer_id)
        if offer is None or offer.used:
            logger.info("Offer link: gone")
            return None, LINK_GONE
        if self._expired(offer, self._now()):
            logger.info("Offer link: expired")
            offer.clicked = True
            how = RETIRE_EXPIRED if offer.kind == offers.KIND_PLAN else RETIRE_ENDED
            if offer.kind == offers.KIND_PLAN:
                self._plan_candidate = False
            self._retire(offer, how)
            return offer, how
        return offer, ""

    def offer(self, offer_id: int) -> Offer | None:
        """The offer behind a link, when it is still valid (not used, expired or ended); else None
        (see link(): an expired one is retired)."""
        offer, state = self.link(offer_id)
        return offer if not state else None

    def use_offer(self, offer_id: int) -> Offer | None:
        """The click started its run: the offer's sentence is done with, a recurring series' ignored
        count is reset (R38)."""
        offer = self._offers.get(offer_id)
        if offer is None:
            return None
        offer.clicked = True
        if offer.speech_state in (HELD, QUEUED):
            offer.speech_state = TEXT_ONLY
            offer.held_reason = "clicked"
        logger.info("Offer link: used")
        if offer.series_key:
            self._reset_series(offer.series_key)
        return offer

    def brief_ready(self, offer_id: int, at: datetime) -> None:
        """The brief from this offer is below (R50): the entry says so and loses its link."""
        offer = self._offers.get(offer_id)
        if offer is None or offer.used:
            return
        offer.used = True
        offer.read_at = at
        self._retire(offer, RETIRE_USED)

    def on_brief(self, brief: Any) -> None:
        """A brief was made (typed or offered): its meeting's recurring series starts afresh (R38)."""
        ref = getattr(brief, "meeting", None)
        series = str(getattr(brief, "series_id", "") or "")
        if ref is None or not series:
            return
        self._reset_series(offers.series_key(ref.alias, ref.calendar_id, series))

    # ---- what the app may ask -------------------------------------------------------------------

    def request_day(self, reason: str) -> None:
        """Read today on every signed-in calendar, with the personal zone, on the prod thread (only
        while an offer needs the schedule and a calendar exists; never during a sign-in)."""
        if self._closed or self._day_in_flight or not self._reads_on() or self._safe_signing_in():
            return
        now = self._now()
        zone = self.day_zone()
        start, end = day_bounds(_wall(now, zone).date(), zone)
        self._day_seq += 1
        job = _DayJob(self._day_seq, start, end, None, True, reason)
        self._jobs[job.seq] = (job, now)
        self._day_in_flight = True
        self._day_requested_at = now
        self._live_day_started(job)
        self._worker.put(job)

    def day_briefs(self) -> Mapping[str, tuple[EventBrief, ...]]:
        return dict(self._day)

    def day_read_at(self) -> datetime | None:
        return self._day_read_at

    def day_zone(self) -> str:
        """The zone that came with the last day read, else the personal calendar's cached zone (no
        Google request on the GUI thread, R16)."""
        if self._day_zone:
            return self._day_zone
        cached = getattr(self.sources, "cached_time_zone", None)
        try:
            zone = cached() if callable(cached) else ""
        except Exception:  # noqa: BLE001
            zone = ""
        return zone if isinstance(zone, str) else ""

    def in_meeting(self, now: datetime) -> bool:
        return offers.in_meeting(self._day, now, self._own())

    def guest_meeting_left_today(self, now: datetime) -> bool:
        """A timed meeting with another guest, not declined, still ends after ``now`` today (the Done
        tooltip, R34)."""
        own = self._own()
        zone = self.day_zone()
        today = _wall(now, zone).date()
        for briefs in self._day.values():
            for event in briefs:
                span = offers.event_span(event, now)
                if span is None or event.self_response == "declined" or other_guests(event, own) < 1:
                    continue
                if span[1] > now and _wall(span[0], zone).date() == today:
                    return True
        return False

    def open_offers(self) -> tuple[Offer, ...]:
        """Every offer still open, oldest first (tests and harnesses)."""
        return tuple(self._offers.values())

    def plan_offer_open(self) -> Offer | None:
        return self._plan_offer

    # ---- the follow-up list (B5) -------------------------------------------------------------------

    def scan_on(self) -> bool:
        """The header scan may run: [productivity] followups, [ask] read_mail, an account whose mail may
        be read (gmail_read) and not turned off on this PC. Memory and the config only."""
        if self._closed or not bool(getattr(self.settings, "followups", False)) or self.followups_off():
            return False
        return self._read_mail_on() and bool(self.scan_aliases())

    def followups_off(self) -> bool:
        prefs = self._prefs
        try:
            return bool(getattr(prefs, "followups_off", False))
        except Exception:  # noqa: BLE001
            return False

    def _read_mail_on(self) -> bool:
        return bool(getattr(getattr(self.config, "ask", None), "read_mail", False))

    def scan_aliases(self) -> tuple[str, ...]:
        """The accounts whose headers the scan reads: the Ask readers ("gmail_read" with [ask] read_mail);
        attributes only, no file or request."""
        readers = getattr(self.sources, "readers", None)
        if not isinstance(readers, Mapping):
            return ()
        return tuple(sorted(alias for alias, reader in readers.items()
                            if callable(getattr(reader, "scan_ids", None))))

    def _scan_due(self, now: datetime) -> bool:
        if self._scan_job is not None or not self.scan_on() or self._safe_signing_in():
            return False
        since = self._visible_since
        if since is None or now - since < FIRST_SCAN_AFTER:
            return False
        last = self._scan_requested_at
        every = timedelta(minutes=int(getattr(self.settings, "scan_every_minutes", 30)))
        return last is None or now - last >= every

    def _first_scan(self) -> None:
        """20 s after the reading screen came up: the scan, when one is due."""
        if self._closed or self._safe_activity() != ACT_VISIBLE:
            return
        now = self._now()
        if self._scan_due(now):
            self.request_scan(READ_SHOWN)

    def request_scan(self, reason: str) -> bool:
        """Read every readable account's mail headers on the prod thread (never during a sign-in,
        never twice at once). False when it did not start."""
        if self._closed or self._scan_job is not None or not self.scan_on() or self._safe_signing_in():
            return False
        aliases = self.scan_aliases()
        self._scan_seq += 1
        self._scan_requested_at = self._now()
        steps = {alias: self._live_scan_step(alias) for alias in aliases}
        job = _ScanJob(self._scan_seq, aliases, threading.Event(), dict(self._signins), steps, reason)
        self._scan_job = job
        self._changed()                      # "scanning..."
        self._worker.put(job)
        return True

    def cancel_scan(self) -> None:
        """A Google sign-in opened in the browser: a running scan stops between two reads (R19)."""
        job = self._scan_job
        if job is not None and not job.cancel.is_set():
            job.cancel.set()

    def _on_scanned(self, seq: int, results: Any) -> None:
        if self._closed:
            return
        job = self._scan_job
        if job is None or job.seq != seq:
            return
        self._scan_job = None
        if self.followups_off():          # turned off while it ran: nothing of it is kept
            self._changed()
            return
        readable = set(self.scan_aliases())
        for alias in set(self._items) | set(self._reports):
            if alias not in readable:
                self._drop_alias(alias)
        for alias, result in dict(results or {}).items():
            report = getattr(result, "report", None)
            if report is None:
                continue
            self._reports[alias] = report
            if report.problem in (PROBLEM_UNCONFIRMED, PROBLEM_IDENTITY, PROBLEM_UNREADABLE):
                self._drop_alias(alias, keep_report=True)   # not this account's rows until it is fixed
            elif report.problem not in NOT_SCANNED:
                self._items[alias] = list(result.items)
                self._hidden_items[alias] = list(result.hidden)
                self._participants[alias] = dict(result.participants)
            self._live_rows(job.steps.get(alias), result.items)
            if report.problem == "cancelled":
                self._scan_requested_at = None   # stopped for a sign-in: due again once it is over
        self._scanned_at = self._now()
        self._changed()

    def _drop_alias(self, alias: str, *, keep_report: bool = False) -> None:
        self._items.pop(alias, None)
        self._hidden_items.pop(alias, None)
        self._participants.pop(alias, None)
        if not keep_report:
            self._reports.pop(alias, None)

    def followups(self) -> FollowupView:
        """The list as the app shows it (memory only)."""
        items = [item for alias in sorted(self._items) for item in self._items[alias]]
        hidden = [item for alias in sorted(self._hidden_items) for item in self._hidden_items[alias]]
        notes: list[tuple[str, str]] = []
        if not self._read_mail_on():
            notes.append(("", READ_OFF_NOTE))
        for alias in sorted(self._reports):
            problem = self._reports[alias].problem
            if problem in (PROBLEM_UNCONFIRMED, PROBLEM_IDENTITY, PROBLEM_UNREADABLE, PROBLEM_REFUSED,
                           PROBLEM_STOPPED):
                notes.append((alias, problem))
        return make_view(items, hidden, reports=tuple(self._reports[alias] for alias in sorted(self._reports)),
                         notes=notes, scanned_at=self._scanned_at, scanning=self._scan_job is not None,
                         off=self.followups_off())

    def followup_counts(self) -> tuple[int, int] | None:
        """(waiting, owed) over every account once a scan ran (the plan's <todos> "emails" row), else
        None. Counts only: the only follow-up data a run ever gets."""
        if self._scanned_at is None or self.followups_off():
            return None
        items = [item for rows in self._items.values() for item in rows]
        return (sum(1 for item in items if item.kind == "waiting"), sum(1 for item in items if item.kind == "owed"))

    def participants(self) -> dict[str, tuple[Any, ...]]:
        """mailbox -> the scan's newest threads with that person, every account together (prep's index:
        it keeps the meeting's account's unless prep_other_accounts, R3); {} while the list is off."""
        if self.followups_off() or not self.scan_on():
            return {}
        merged: dict[str, list[Any]] = {}
        for index in self._participants.values():
            for key, refs in index.items():
                merged.setdefault(key, []).extend(refs)
        return {key: tuple(sorted(refs, key=lambda ref: ref.last_at, reverse=True)) for key, refs in merged.items()}

    def item(self, row_id: str) -> FollowupItem | None:
        """The row behind an opaque row id (None: rescanned away, 8.5)."""
        for rows in (*self._items.values(), *self._hidden_items.values()):
            for item in rows:
                if item.row_id == row_id:
                    return item
        return None

    def checking(self, row_id: str) -> bool:
        """The row's headers are being read again (its menu is off meanwhile)."""
        return row_id in self._checking

    def hide(self, row_id: str, *, days: int | None) -> bool:
        """Hide a row for ``days`` days, or (None) until something new arrives (5.6)."""
        item = self.item(row_id)
        store = self._hidden
        if item is None or store is None or self._is_hidden(item.key):
            if item is None:
                logger.info("Follow-ups: that row is gone")
            return False
        until = self._now() + timedelta(days=days) if days is not None else None
        try:
            store.hide(item.key, until=until)
        except Exception as exc:  # noqa: BLE001 - hidden for this run only
            logger.warning("Follow-ups: could not save the hidden row (%s)", type(exc).__name__)
        rows = self._items.get(item.alias, [])
        if item in rows:
            rows.remove(item)
            self._hidden_items.setdefault(item.alias, []).append(item)
        logger.info("Follow-ups: a row hidden (%s)", "3_days" if days is not None else "until_new")
        self._changed()
        return True

    def unhide(self, row_id: str) -> bool:
        item = self.item(row_id)
        store = self._hidden
        if item is None:
            logger.info("Follow-ups: that row is gone")
            return False
        if store is not None:
            try:
                store.unhide(item.key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Follow-ups: could not save the shown row (%s)", type(exc).__name__)
        rows = self._hidden_items.get(item.alias, [])
        if item in rows:
            rows.remove(item)
            self._items.setdefault(item.alias, []).append(item)
        logger.info("Follow-ups: a row shown again")
        self._changed()
        return True

    def set_off(self, off: bool) -> None:
        """Turn the list off (or on again) on this PC (assistant.json followups_off)."""
        prefs = self._prefs
        setter = getattr(prefs, "set_followups_off", None)
        if callable(setter):
            try:
                setter(bool(off))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Follow-ups: could not save the switch (%s)", type(exc).__name__)
        logger.info("Follow-ups: turned %s on this PC", "off" if off else "on")
        if off:
            self.cancel_scan()
            # Nothing listed is kept (no refetch, no prep index from it); the reports stay for the notes.
            for table in (self._items, self._hidden_items, self._participants, self._checking, self._refetches):
                table.clear()
            self._scanned_at = self._scan_requested_at = None   # never "nothing waits" from the emptied list
        elif self._safe_activity() == ACT_VISIBLE:
            self.request_scan("turned_on")
        self._changed()

    def refetch(self, row_id: str, reason: str) -> bool:
        """One listed thread's headers again at once (R47: a guard stop, a sent reply); the row reads
        "replied - checking" until it lands. False for a gone row."""
        item = self.item(row_id)
        if item is None or self._closed or row_id in self._checking:
            if item is None:
                logger.info("Follow-ups: that row is gone")
            return False
        scanner = self._scanner
        if not self.scan_on() or scanner.stopped(item.alias) or self._safe_signing_in():
            return False      # off, refused until the next sign-in, or a sign-in is open: no read (R19)
        self._scan_seq += 1
        job = _RefetchJob(self._scan_seq, item.alias, item.thread_id, row_id, reason,
                          self._live_refetch_step(item.alias, reason))
        self._refetches[job.seq] = job
        self._checking.add(row_id)
        logger.info("Follow-ups: a thread checked again (%s)", reason)
        self._changed()
        self._worker.put(job)
        return True

    def refetch_thread(self, alias: str, thread_id: str, reason: str) -> bool:
        """refetch() for the listed row of that thread (a reply sent there); nothing for a thread that is
        not listed (no read)."""
        for rows in self._items.values():
            for item in rows:
                if item.alias == alias and item.thread_id == thread_id:
                    return self.refetch(item.row_id, reason)
        return False

    def row_of(self, alias: str, thread_id: str) -> FollowupItem | None:
        for rows in self._items.values():
            for item in rows:
                if item.alias == alias and item.thread_id == thread_id:
                    return item
        return None

    def _on_refetched(self, seq: int, result: Any) -> None:
        job = self._refetches.pop(seq, None)
        if job is None or self._closed:
            return
        self._checking.discard(job.row_id)
        rows = self._items.get(job.alias, [])
        old = next((item for item in rows if item.row_id == job.row_id), None)
        if isinstance(result, FollowupItem):
            if old is not None:
                rows[rows.index(old)] = result
            else:
                rows.append(result)
            self._items[job.alias] = rows
        elif isinstance(result, str) and old is not None:   # not listed any more (answered, hidden, gone ...)
            rows.remove(old)
        self._changed()

    def _changed(self) -> None:
        if not self._closed:
            self.followupsChanged.emit(self.followups())

    def _known(self, address: str) -> bool:
        known = self._known_recipient
        try:
            return bool(known(address)) if known is not None else False
        except Exception:  # noqa: BLE001
            return False

    def _is_hidden(self, key: str) -> bool:
        store = self._hidden
        if store is None:
            return False
        try:
            return bool(store.hidden(key, self._now()))
        except Exception:  # noqa: BLE001
            return False

    @_live.quiet(_live.NO_STEP)
    def _live_scan_step(self, alias: str) -> Any:
        return self._own_task().step(_live.MAIL_SCAN, f"Jarvis's own header scan: {alias} (read only)",
                                     key=f"scan:{alias}", fields=[("Account", alias)])

    @_live.quiet(_live.NO_STEP)
    def _live_refetch_step(self, alias: str, reason: str) -> Any:
        why = "a reply was sent in it" if reason == "sent" else "a draft found it changed"
        return self._own_task().step(_live.MAIL_SCAN, "Checked one thread again (headers only)",
                                     key=f"refetch:{alias}", fields=[("Account", alias), ("Why", why),
                                                                    ("Reads", "that thread's headers only")])

    @_live.quiet()
    def _live_step_failed(self, step: Any) -> None:
        if step is not None:
            step.done(_live.STATUS_FAILED, summary="failed")

    @_live.quiet()
    def _live_rows(self, step: Any, items: Sequence[FollowupItem]) -> None:
        """With [live] text: one row per listed thread ("WAITING ON YOU . <subject> . <who> . 3d")."""
        live = self._live
        if step is None or live is None or not getattr(live, "keep_text", False):
            return
        now = self._now()
        from .followups import age_words

        for item in sorted(items, key=lambda one: (one.kind, one.since or now))[:40]:
            label = "WAITING ON YOU" if item.kind == "waiting" else "OWED TO YOU"
            age = age_words(item.since, now) if item.since is not None else ""
            step.item(f" {_DOT} ".join(part for part in (label, item.subject, item.who, age) if part))

    # ---- the day read -----------------------------------------------------------------------------

    def _reads_on(self, now: datetime | None = None) -> bool:
        """The schedule is read only while an offer can use it and some calendar exists: prep offers
        on, or the plan offer candidate still judged before plan_offer_until (with prep offers off,
        nothing more is read once the plan offer was posted or settled)."""
        if not getattr(self.sources, "calendars", None):
            return False
        if self._prep_on:
            return True
        if not self._plan_on or not self._plan_candidate or self._plan_offer is not None:
            return False
        until = offers.clock_time(getattr(self.settings, "plan_offer_until", "13:30"))
        return _wall(now or self._now(), self.day_zone()).time() < until

    def _day_read_due(self, now: datetime) -> bool:
        if self._day_in_flight or not self._reads_on(now) or self._safe_signing_in():
            return False
        at = self._day_read_at
        if at is None:
            requested = self._day_requested_at
            return requested is None or now - requested >= DAY_READ_EVERY
        zone = self.day_zone()
        return now - at >= DAY_READ_EVERY or _wall(at, zone).date() != _wall(now, zone).date()

    def _on_day_read(self, seq: int, read: Any, read_at: Any, zone: str) -> None:
        if self._closed:
            return
        found = self._jobs.pop(seq, None)
        if found is None:
            return
        job, asked_at = found
        results = dict(getattr(read, "results", None) or {})
        if job.reason == READ_MINIMIZED:
            self._on_hour_read(job, results, read_at)
            return
        self._day_in_flight = False
        statuses = {alias: status for alias, (_briefs, status) in results.items()}
        self._day = {alias: tuple(briefs) for alias, (briefs, status) in results.items() if status == "yes"}
        self._day_statuses = statuses
        self._day_own = frozenset(getattr(read, "own", frozenset()) or frozenset())
        self._day_complete = bool(statuses) and all(status == "yes" for status in statuses.values())
        self._day_read_at = read_at if isinstance(read_at, datetime) else self._now()
        self._hour_failed.clear()   # a fresh schedule: a meeting whose hour could not be read is judged again
        if zone:
            self._day_zone = zone
        for alias, (briefs, status) in results.items():
            if status == "yes":
                self._alias_read_at[alias] = self._day_read_at
                self._recheck_offers(alias, tuple(briefs), job.start, job.end)
        events = sum(len(briefs) for briefs in self._day.values())
        took = (self._now() - asked_at).total_seconds()
        logger.info("Day read: %d event(s) on %d account(s) in %.1f s", events, len(self._day), max(0.0, took))
        for alias, status in statuses.items():
            if status != "yes":
                logger.info("Day read: %s %s", logged_alias(alias), status)
        self._live_day_done(job, results)
        self.dayChanged.emit()
        now = self._now()
        act = self._safe_activity()
        self._evaluate_plan_offer(now, "day")
        self._evaluate_prep_offers(now, act)

    def _on_job_failed(self, seq: int, kind: str) -> None:
        if kind == JOB_SCAN:
            job = self._scan_job
            if job is not None and job.seq == seq:
                self._scan_job = None
                for step in job.steps.values():
                    self._live_step_failed(step)
                self._changed()
            return
        if kind == JOB_REFETCH:
            refetch = self._refetches.pop(seq, None)
            if refetch is not None:
                self._checking.discard(refetch.row_id)
                self._live_step_failed(refetch.step)
                self._changed()
            return
        found = self._jobs.pop(seq, None)
        job = found[0] if found is not None else None
        if job is not None and job.reason == READ_MINIMIZED:
            self._pending_checks.pop(job.key, None)
        elif job is not None:
            self._day_in_flight = False
        key = f"hour:{job.key}" if job is not None and job.reason == READ_MINIMIZED else _LIVE_DAY_KEY
        self._live_failed(key)
        if job is not None and job.reason == READ_MINIMIZED:
            self._hour_unreadable(job.key)

    # ---- the plan offer (4.3) -------------------------------------------------------------------

    def _evaluate_plan_offer(self, now: datetime, how: str) -> None:
        if not self._plan_candidate or self._plan_offer is not None or self._closed:
            return
        if self._safe_activity() == ACT_OFF:
            return
        settings = self.settings
        key = self._plan_key
        ready, caps_ok, caps_age = self._safe_ask_state()
        zone = self.day_zone()
        wall = _wall(now, zone)
        offered = key in self._plan_offered_keys or self._state_true("plan_offered", key)
        planned = key in self._planned_keys or self._state_true("planned", key)
        read_at = _wall(self._day_read_at, zone) if self._day_read_at is not None else None
        personal_read = self._day_statuses.get(DEFAULT_ACCOUNT) == "yes"
        free = 0
        needs = False
        if read_at is not None and personal_read:
            window = dayplan.plan_window(wall, dayplan.PART_DAY, settings, zone,
                                         evening_from_hour=self._evening_from_hour())
            if isinstance(window, dayplan.PlanWindow) and not window.tomorrow:
                free = dayplan.free_minutes(dayplan.free_slots(self._day, window, own=self._own()))
            inputs = self._safe_inputs()
            inputs = replace(inputs, todos=dayplan.plan_todos(inputs, wall.date()))
            needs = dayplan.needs_time(inputs, wall)

        def due(caps: bool) -> str:
            found = offers.plan_offer_due(key=key, now=wall, settings=settings, offered=offered, planned=planned,
                                          ready=ready, caps_ok=caps, day_read_at=read_at,
                                          day_complete=self._day_complete, free_minutes=free, needs_time=needs)
            if found == "no_day" or (not found and not personal_read):
                return "no_day"
            return found

        reason = due(caps_ok)
        if reason in ("", "caps") and caps_age > CAPS_STALE_S and not due(True):
            # The cached check is old (R13): asked again first; the offer is judged at the next tick.
            self._refresh_caps_safe(now)
            self._live_plan_pending("caps checked again")
            return
        if not reason:
            self._post_plan_offer(now, wall, how)
            return
        if offers.plan_reason_transient(reason, self._day_complete):
            self._live_plan_pending(reason)
            if reason == "no_day" and self._safe_activity() == ACT_VISIBLE and self._day_read_due(now):
                self.request_day(READ_PLAN)   # never while minimized (4.5): read once the window is back
            return
        self._plan_candidate = False
        if reason not in self._plan_final_logged:
            self._plan_final_logged.add(reason)
            logger.info("Plan offer: skipped for %s (%s)", key, reason)
        self._live_offer(f"Not offered: plan my day ({_words(reason)})", key=_LIVE_PLAN_KEY, status=_live.STATUS_WARN,
                         fields=[("For", key), ("Why", _words(reason))])

    def _post_plan_offer(self, now: datetime, wall: datetime, how: str) -> None:
        settings = self.settings
        key = self._plan_key
        until = datetime.combine(wall.date(), offers.clock_time(getattr(settings, "plan_offer_until", "13:30")))
        self._offer_seq += 1
        offer = Offer(self._offer_seq, offers.KIND_PLAN, now, until, slot_key=key,
                      speech=persona.plan_offer_speech(self._address()), live_key=_LIVE_PLAN_KEY)
        offer.sub = self.sub_line(offer)
        self._offers[offer.id] = offer
        self._plan_offer = offer
        self._plan_offered_keys.add(key)
        state = self._run_state
        if state is not None:
            try:
                state.record_plan_offered(key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Plan offer: could not record the offer (%s)", type(exc).__name__)
        self._live_offer("Offered: plan my day - nothing read or run until you click", key=_LIVE_PLAN_KEY,
                         fields=[("For", key), ("Costs when clicked", "1 planner run")])
        disposition, reason = self._speech_disposition(offer)
        self.offerPosted.emit(offer)
        self._apply_disposition(offer, disposition, reason, how)
        logger.info("Plan offer: posted for %s (%s)", key, _speech_words(offer, reason))

    # ---- prep offers (4.4) ----------------------------------------------------------------------

    def _evaluate_prep_offers(self, now: datetime, act: str) -> None:
        if self._closed or not self._prep_on or not self._day or act == ACT_OFF:
            return
        if act == ACT_VISIBLE and (self._day_read_at is None or now - self._day_read_at > timedelta(
                minutes=offers.STALE_SCHEDULE_MINUTES)):
            return   # back from minimized (or a sign-in open): nothing from the old schedule; the read due now judges
        settings = self.settings
        ready, caps_ok, caps_age = self._safe_ask_state()
        own = self._own()
        # Minimized with a schedule older than 30 minutes: only what an hour read re-checked counts (R35).
        stale = act == ACT_MINIMIZED and (
            self._day_read_at is None or now - self._day_read_at > timedelta(minutes=offers.STALE_SCHEDULE_MINUTES))
        for alias, briefs in list(self._day.items()):
            for event in briefs:
                key = offers.offer_key(alias, event)
                if key in self._prep_offered or key in self._pending_checks:
                    continue

                def due(ready_now: bool, caps_now: bool) -> str:
                    return offers.prep_offer_due(alias, event, now=now, settings=settings, own=own,
                                                 offered=self._prep_offered, offered_today=self._prep_count(now),
                                                 last_group_at=self._last_group_at, ready=ready_now,
                                                 caps_ok=caps_now, ignored=self._ignored)

                reason = due(ready, caps_ok)
                if reason in ("", "caps") and caps_age > CAPS_STALE_S and not due(ready, True):
                    self._refresh_caps_safe(now)   # R13: judged again at the next tick
                    return
                if reason in ("not_ready", "caps"):
                    # Only a meeting that would be offered now waits for Claude Code or the limits: any
                    # other keeps its own reason (no "Not offered yet" line for a past or unfit one).
                    reason = due(True, True) or reason
                if reason:
                    self._prep_not_offered(key, event, reason, own)
                    continue
                checked = self._hour_checked_at(alias, event, now) if stale else None
                if stale and checked is None:
                    if key in self._hour_failed:
                        continue   # its hour could not be read: judged again once the window is back
                    if getattr(settings, "minimized_check", True):
                        if self._hour_reads_left(now):
                            self._request_hour(alias, event, key)
                        else:
                            self._prep_not_offered(key, event, "checks_used", own)
                        continue
                    self._post_prep_group(((alias, event),), now, act, stale=True)
                    continue
                day = self._checked_day(now) if stale else self._day
                group = offers.prep_offer_group(day, (alias, event), now=now, settings=settings, own=own,
                                                offered=self._prep_offered, ignored=self._ignored,
                                                offered_today=self._prep_count(now))
                self._post_prep_group(group, now, act, stale=False, read_at=checked)

    def _hour_checked_at(self, alias: str, event: EventBrief, now: datetime) -> datetime | None:
        """The read time of a minimized hour read within the last 30 minutes that covered the event's
        start on its own calendar (None: no such read)."""
        if event.start is None or event.all_day:
            return None
        return self._checked_at(alias, event.start, now)

    def _checked_at(self, alias: str, moment: datetime, now: datetime) -> datetime | None:
        limit = timedelta(minutes=offers.STALE_SCHEDULE_MINUTES)
        for checked_alias, start, end, read_at in reversed(self._hour_checked):
            if checked_alias == alias and start <= moment < end and now - read_at <= limit:
                return read_at
        return None

    def _prep_unverified(self, offer: Offer, now: datetime) -> bool:
        """No read of the last 30 minutes covered the prep offer's meeting (its calendar's day read, or
        an hour read around it): its sentence could name a meeting that moved or was cancelled since."""
        if offer.start is None:
            return True
        read_at = self._alias_read_at.get(offer.alias)
        if read_at is not None and now - read_at <= timedelta(minutes=offers.STALE_SCHEDULE_MINUTES):
            return False
        return self._checked_at(offer.alias, offer.start, now) is None

    def _recheck_offers(self, alias: str, briefs: Sequence[EventBrief], start: datetime, end: datetime) -> None:
        """A fresh read of ``alias`` over [start, end): an open prep offer whose meeting it no longer has
        (cancelled, or moved out of that range) or has at another time is not said any more (R35): its
        sentence would name a meeting that is not there. A group's one sentence goes with any member.
        The entry and its link stay (a click reads everything again before anything runs)."""
        for offer in list(self._offers.values()):
            ref = offer.event
            if (offer.kind != offers.KIND_PREP or offer.alias != alias or ref is None or offer.start is None
                    or offer.used or not start <= offer.start < end):
                continue
            found = find_event(briefs, ref.calendar_id, ref.event_id)
            if found is not None and not found.all_day and found.start == offer.start:
                continue
            speaker = self._offers.get(offer.group[0]) if offer.group else offer
            if speaker is not None:
                self._unsay(speaker, "gone" if found is None else "moved")

    def _unsay(self, offer: Offer, how: str) -> None:
        """The offer's sentence, not said yet, is text only for good; a queued one is taken back."""
        if offer.speech_state not in (HELD, QUEUED):
            return
        queued = offer.speech_state == QUEUED
        offer.speech_state = TEXT_ONLY
        offer.held_reason = how
        offer.queued_at = None
        logger.info("%s offer: text only (%s)", _kind_word(offer), how)
        self._live_spoken(offer, "text only (moved or cancelled)")
        if queued:
            self.offerUnsaid.emit(offer)

    def _checked_day(self, now: datetime) -> dict[str, tuple[EventBrief, ...]]:
        """The schedule's events an hour read re-checked within the last 30 minutes (minimized: a
        back-to-back group is built only from those, R35/R51)."""
        found: dict[str, tuple[EventBrief, ...]] = {}
        for alias, briefs in self._day.items():
            kept = tuple(event for event in briefs if self._hour_checked_at(alias, event, now) is not None)
            if kept:
                found[alias] = kept
        return found

    def _prep_not_offered(self, key: str, event: EventBrief, reason: str, own: frozenset[str]) -> None:
        shown = self._prep_skipped.get(key, "")   # the reason its LIVE line shows ("": no line)
        meeting_field = ("Meeting", _time_words(event, self.day_zone(), self._hour24()))
        if reason == "offered":
            return
        if reason == "early":
            if shown and shown not in offers.FINAL_PREP_REASONS:   # a clock that went back: not due yet
                self._prep_skipped.pop(key, None)
                self._live_offer("Not offered yet: a prep brief (not due yet)", key=f"prep:{key}",
                                 fields=[meeting_field])
            return
        quiet = reason in _QUIET_PREP_REASONS or (reason == "guests" and other_guests(event, own) < 1)
        if reason in offers.FINAL_PREP_REASONS:
            self._prep_offered.add(key)   # never looked at again: a passed or unfit meeting
            if shown == reason:
                return
            if quiet:
                if shown:   # its "Not offered yet" line is settled now (the meeting started meanwhile)
                    self._prep_skipped[key] = reason
                    self._live_offer(f"Not offered: a prep brief ({_QUIET_WORDS.get(reason, _words(reason))})",
                                     key=f"prep:{key}", status=_live.STATUS_WARN, fields=[meeting_field])
                return
            self._prep_skipped[key] = reason
            logger.info("Prep offer: skipped (%s)", reason)
            words = ('ignored 3 times - type "prep me" for this one' if reason == "ignored" else _words(reason))
            self._live_offer(f"Not offered: a prep brief ({words})", key=f"prep:{key}", status=_live.STATUS_WARN,
                             fields=[meeting_field])
            return
        if quiet or shown == reason:   # transient: one line, updated in place
            return
        self._prep_skipped[key] = reason
        self._live_offer(f"Not offered yet: a prep brief ({_words(reason)})", key=f"prep:{key}",
                         fields=[meeting_field])

    def _hour_unreadable(self, key: str) -> None:
        """The minimized check could not read the meeting's hour (not signed in, a network error, a
        failed job): no offer from the old schedule and no second read while minimized (one per
        candidate, R35); it is judged again from a fresh day read once the window is visible."""
        self._hour_failed.add(key)
        if self._prep_skipped.get(key) == "unreadable":
            return
        self._prep_skipped[key] = "unreadable"
        logger.info("Prep offer: skipped (%s)", "unreadable")
        self._live_offer("Not offered yet: a prep brief (its hour could not be read; judged again when the "
                         "window is back)", key=f"prep:{key}", status=_live.STATUS_WARN)

    def _request_hour(self, alias: str, event: EventBrief, key: str) -> None:
        """The minimized check (R35): one read of the hour around the meeting on its own calendar."""
        if event.start is None or self._safe_signing_in() or self._closed:
            return
        now = self._now()
        today = _wall(now, self.day_zone()).date()
        day, count = self._hour_reads
        self._hour_reads = (today, count + 1) if day == today else (today, 1)
        self._day_seq += 1
        job = _DayJob(self._day_seq, event.start - timedelta(hours=MINIMIZED_CHECK_HOURS),
                      event.start + timedelta(hours=MINIMIZED_CHECK_HOURS), (alias,), False, READ_MINIMIZED, key)
        self._jobs[job.seq] = (job, now)
        self._pending_checks[key] = job.seq
        self._live_day_started(job, alias=alias)
        self._worker.put(job)

    def _hour_reads_left(self, now: datetime) -> bool:
        today = _wall(now, self.day_zone()).date()
        day, count = self._hour_reads
        return day != today or count < int(getattr(self.settings, "prep_offers_per_day", 8))

    def _on_hour_read(self, job: _DayJob, results: Mapping[str, Any], read_at: Any) -> None:
        self._pending_checks.pop(job.key, None)
        alias = job.aliases[0] if job.aliases else ""
        briefs, status = results.get(alias, ((), "no"))
        self._live_day_done(job, results)
        parts = job.key.split("|")
        if len(parts) != 4 or status != "yes":
            self._hour_unreadable(job.key)
            return
        briefs = tuple(briefs)
        now = self._now()
        checked_at = read_at if isinstance(read_at, datetime) else now
        self._take_hour(alias, job, briefs, checked_at)
        _alias, calendar_id, event_id, _start = parts
        found = find_event(briefs, calendar_id, event_id)
        if found is None or found.start is None or found.all_day:
            self._prep_offered.add(job.key)
            logger.info("Prep offer: skipped (gone)")
            self._live_offer("Not offered: a prep brief (the meeting is gone)", key=f"prep:{job.key}",
                             status=_live.STATUS_WARN)
            return
        if offers.offer_key(alias, found) != job.key:   # moved: the new occurrence is judged at its own time
            self._prep_offered.add(job.key)
            self._live_offer("Not offered yet: a prep brief (the meeting moved; judged at its new time)",
                             key=f"prep:{job.key}")
            return
        ready, caps_ok, _age = self._safe_ask_state()
        own = self._own()
        reason = offers.prep_offer_due(alias, found, now=now, settings=self.settings, own=own,
                                       offered=self._prep_offered, offered_today=self._prep_count(now),
                                       last_group_at=self._last_group_at, ready=ready, caps_ok=caps_ok,
                                       ignored=self._ignored)
        if reason:
            self._prep_not_offered(job.key, found, reason, own)
            return
        act = self._safe_activity()
        if act == ACT_OFF:
            return   # the reading screen went away meanwhile: nothing posted or said (judged again when back)
        # Only what this read verified joins the group: its calendar, inside the hour read (R35, R51).
        verified = {alias: tuple(event for event in briefs if self._inside(job, event))}
        group = offers.prep_offer_group(verified, (alias, found), now=now, settings=self.settings, own=own,
                                        offered=self._prep_offered, ignored=self._ignored,
                                        offered_today=self._prep_count(now))
        self._post_prep_group(group, now, act, stale=False, read_at=checked_at)

    @staticmethod
    def _inside(job: _DayJob, event: EventBrief) -> bool:
        return event.start is not None and not event.all_day and job.start <= event.start < job.end

    def _take_hour(self, alias: str, job: _DayJob, briefs: tuple[EventBrief, ...], read_at: datetime) -> None:
        """An hour read replaces what the schedule held for that calendar and hour: a cancelled
        meeting goes, a moved or changed one is updated (an open offer for it is not said any more);
        the read is remembered for 30 minutes."""
        self._recheck_offers(alias, briefs, job.start, job.end)
        fresh = {(event.calendar_id, event.event_id) for event in briefs}
        kept = tuple(event for event in self._day.get(alias, ())
                     if (event.calendar_id, event.event_id) not in fresh and not self._inside(job, event))
        self._day[alias] = kept + briefs
        zone = self.day_zone()
        today = _wall(read_at, zone).date()
        self._hour_checked = [entry for entry in self._hour_checked if _wall(entry[3], zone).date() == today]
        self._hour_checked.append((alias, job.start, job.end, read_at))

    def _post_prep_group(self, group: Sequence[tuple[str, EventBrief]], now: datetime, act: str, *,
                         stale: bool, read_at: datetime | None = None) -> None:
        settings = self.settings
        zone = self.day_zone()
        own = self._own()
        lead = int(getattr(settings, "prep_lead_minutes", 15))
        checked = read_at is not None          # from minimized hour reads: each member as of its own read
        read_at = read_at or self._day_read_at
        made: list[Offer] = []
        for alias, event in group:
            if event.start is None:
                continue
            key = offers.offer_key(alias, event)
            member_read = (self._hour_checked_at(alias, event, now) or read_at) if checked else read_at
            self._prep_skipped.pop(key, None)
            self._offer_seq += 1
            end = event.end if event.end is not None else event.start
            offer = Offer(self._offer_seq, offers.KIND_PREP, now, max(end, event.start), alias=alias,
                          event=EventRef(alias, event.calendar_id, event.event_id, event.start),
                          event_title=event.title, start=event.start, guests=other_guests(event, own),
                          lead_minutes=lead, read_at=member_read, series_key=offers.prep_series_key(alias, event),
                          stale=stale, live_key=f"prep:{key}")
            self._offers[offer.id] = offer
            self._prep_offered.add(key)
            self._count_prep(now)
            made.append(offer)
        if not made:
            return
        self._last_group_at = now
        if len(made) > 1:
            ids = tuple(offer.id for offer in made)
            for offer in made:
                offer.group = ids
        first = made[0]
        first.speech = self._prep_speech(first, made, now)
        for offer in made[1:]:
            offer.speech_state = TEXT_ONLY
            offer.held_reason = "group"
        disposition, reason = self._speech_disposition(first)
        flash = act == ACT_MINIMIZED and disposition != offers.TEXT_ONLY
        for offer in made:
            offer.flash = flash and offer is first
            offer.sub = self.sub_line(offer)
        starts = [_wall(offer.start, zone) for offer in made if offer.start is not None]
        read_words = (f"from the read at {persona.clock_text(_wall(read_at, zone), self._hour24())}"
                      if read_at is not None else "from the read")
        if len(made) == 1:
            title = (f"Offered a prep brief: {persona.clock_text(starts[0], self._hour24())}, "
                     f"{first.guests} guest{'' if first.guests == 1 else 's'}, {read_words}")
            fields = [("Costs when clicked", "1 planner run (none without recent mail)")]
            if stale:
                fields.append(("Spoken", "text only (stale)"))
            self._live_offer(title, key=first.live_key, fields=fields)
        else:
            times = " and ".join(persona.clock_text(start, self._hour24()) for start in starts)
            self._live_offer(f"Offered together: {times} ({read_words})", key=first.live_key,
                             fields=[("Meetings", str(len(made)))])
            for offer, start in zip(made[1:], starts[1:]):
                # A member's own line (an earlier "Not offered yet", say) says where it went.
                if self._live_has(offer.live_key):
                    self._live_offer(f"Offered together with {persona.clock_text(starts[0], self._hour24())}: "
                                     f"{persona.clock_text(start, self._hour24())}", key=offer.live_key)
        for offer in made:
            self.offerPosted.emit(offer)
        self._apply_disposition(first, disposition, reason, "posted")
        flash_words = ", taskbar flash" if flash else ""
        for offer in made:
            logger.info("Prep offer: posted (%d guest(s), lead %d min, %s%s)", offer.guests, lead,
                        _speech_words(first, reason) if offer is first else "text only:group",
                        flash_words if offer is first else "")
        if len(made) > 1:
            logger.info("Prep offers: posted together (%d)", len(made))

    def _prep_speech(self, first: Offer, made: Sequence[Offer], now: datetime) -> str:
        """The group's one sentence, from its start times (and the minutes to go) only."""
        zone = self.day_zone()
        starts = [_wall(offer.start, zone) for offer in made if offer.start is not None]
        if len(starts) > 1:
            return persona.prep_offers_speech(starts, self._address())
        start = first.start
        if start is None:
            return ""
        minutes = max(1, int(round((start - now).total_seconds() / 60)))
        return persona.prep_offer_speech(_wall(start, zone), minutes, self._address())

    def _prep_count(self, now: datetime) -> int:
        today = _wall(now, self.day_zone()).date()
        day, count = self._prep_today
        return count if day == today else 0

    def _count_prep(self, now: datetime) -> None:
        today = _wall(now, self.day_zone()).date()
        day, count = self._prep_today
        self._prep_today = (today, count + 1) if day == today else (today, 1)

    def _ignored(self, key: str) -> int:
        series = self._series
        if series is None or not key:
            return 0
        try:
            return int(series.ignored(key))
        except Exception:  # noqa: BLE001
            return 0

    def _reset_series(self, key: str) -> None:
        series = self._series
        if series is None or not key:
            return
        try:
            if series.reset(key):
                logger.info("Prep series: reset")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Prep series: could not reset (%s)", type(exc).__name__)

    def _record_ignored(self, key: str) -> None:
        series = self._series
        if series is None or not key:
            return
        try:
            series.record_ignored(key)
            logger.info("Prep series: an offer was ignored (%d in a row)", series.ignored(key))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Prep series: could not record (%s)", type(exc).__name__)

    # ---- speech (4.2) ------------------------------------------------------------------------

    def _speech_disposition(self, offer: Offer) -> tuple[str, str]:
        """(SPEAK | HELD | TEXT_ONLY, the reason word) for the offer's sentence now. Another offer's
        sentence queued but not started yet counts as one just said (spacing, 4.2: the voice starts it
        a little later, so two sentences queued in one pass would play back to back). A prep sentence
        said while minimized needs a read of its meeting from the last 30 minutes (else stale, text
        only, R35); with prep_speak_minimized off it stays held for the restore, which waits for the
        fresh read (_retry_held)."""
        now = self._now()
        facts = self._safe_facts()
        wall = _wall(now, self.day_zone())
        heard = True
        if offer.kind == offers.KIND_PLAN:
            heard = offers.briefing_heard(finished=self._finished, viewed_then_jarvis=self._viewed_then_jarvis,
                                          announced_at=self._announced_at, playback_started=self._playback_started,
                                          now=now)
        stale = offer.stale or (offer.kind == offers.KIND_PREP and facts.activity == ACT_MINIMIZED
                                and bool(getattr(self.settings, "prep_speak_minimized", True))
                                and self._prep_unverified(offer, now))
        queued = any(other is not offer and other.speech_state == QUEUED for other in self._offers.values())
        reason = offers.may_speak(offer.kind, facts, now=wall, settings=self.settings,
                                  meeting=offers.in_meeting(self._day, now, self._own()),
                                  last_spoken=wall if queued else self._last_spoken_at, heard=heard, stale=stale)
        return offers.speech_disposition(offer.kind, reason), reason

    def _apply_disposition(self, offer: Offer, disposition: str, reason: str, how: str) -> None:
        offer.held_reason = reason
        if disposition == offers.SPEAK:
            if offer.kind == offers.KIND_PREP and not offer.group:
                offer.speech = self._prep_speech(offer, (offer,), self._now())   # the minutes as of now
            offer.speech_state = QUEUED
            offer.spoken_how = how
            offer.queued_at = self._now()
            self._live_spoken(offer, "queued")
            self.offerSpeak.emit(offer)
        elif disposition == offers.TEXT_ONLY:
            offer.speech_state = TEXT_ONLY
            self._live_spoken(offer, f"text only ({_words(reason)})")
        else:
            offer.speech_state = HELD
            self._live_spoken(offer, f"no ({_words(reason)})")

    def _consider_speech(self, offer: Offer, how: str) -> str:
        """May the offer's sentence be said now? Queued (the app says it; spoken once the voice starts
        it), held (retried at later ticks) or text only (never). The reason word."""
        if not offer.speech or offer.speech_state != HELD or offer.used or offer.clicked:
            return offer.held_reason
        disposition, reason = self._speech_disposition(offer)
        if disposition == offers.HELD and reason == offer.held_reason:
            return reason   # still held for the same reason: nothing new to show
        self._apply_disposition(offer, disposition, reason, how)
        return reason

    def _retry_held(self, how: str) -> None:
        """Held sentences at a quieter moment: prep offers first, the soonest meeting first (they cannot
        wait past 3 minutes before the start; the plan offer can), one at a time (spacing). On screen, a
        prep sentence whose meeting no read of the last 30 minutes covered waits for the day read that
        is due (back from minimized, a sign-in open): never said from an old schedule."""
        act = self._safe_activity()
        if self._closed or act == ACT_OFF:
            return
        now = self._now()
        for offer in sorted(self._offers.values(), key=_speech_order):
            if offer.speech_state != HELD or not offer.speech or offer.used or offer.clicked:
                continue
            if not self._speech_window_open(offer, now):
                continue
            if offer.kind == offers.KIND_PREP and act == ACT_VISIBLE and self._prep_unverified(offer, now):
                continue
            if offer.kind == offers.KIND_PLAN and how == "tick" and self._heard_late(now):
                how = "after_20_min"
            self._consider_speech(offer, how)

    def _heard_late(self, now: datetime) -> bool:
        """Heard only through the 20 minutes rule (R26 c)."""
        return (not self._finished and not self._viewed_then_jarvis
                and offers.briefing_heard(finished=False, viewed_then_jarvis=False, announced_at=self._announced_at,
                                          playback_started=self._playback_started, now=now))

    def _requeue_dropped(self, now: datetime) -> None:
        """A sentence queued long ago that the voice never started was dropped as stale: held again."""
        for offer in self._offers.values():
            if offer.speech_state == QUEUED and offer.queued_at is not None and now - offer.queued_at > QUEUED_FOR:
                self._back_to_held(offer)

    def _take_back(self, act: str, *, locked: bool = False) -> None:
        """A sentence queued but not started that may not be said now goes back to held and the app
        takes it from the voice (offerUnsaid): the reading screen went away (4.1), the window was
        minimized (the plan offer, or a prep offer with prep_speak_minimized off) or the session locked
        (4.2). It may still be said later, in its window, at a quiet moment."""
        speak_minimized = bool(getattr(self.settings, "prep_speak_minimized", True))
        for offer in list(self._offers.values()):
            if offer.speech_state != QUEUED:
                continue
            minimized = act == ACT_MINIMIZED and (offer.kind == offers.KIND_PLAN or not speak_minimized)
            if act == ACT_OFF or locked or minimized:
                self._back_to_held(offer)
                self.offerUnsaid.emit(offer)

    def _back_to_held(self, offer: Offer) -> None:
        offer.speech_state = HELD
        offer.held_reason = "dropped"
        offer.queued_at = None
        logger.info("%s offer: speech dropped; held again", _kind_word(offer))
        self._live_spoken(offer, "no (dropped before it started)")

    def _speech_window_open(self, offer: Offer, now: datetime) -> bool:
        if offer.kind == offers.KIND_PLAN:
            wall = _wall(now, self.day_zone())
            return now - offer.posted_at <= offers.PLAN_SPEECH_WINDOW and wall < offer.expires_at
        if offer.start is None:
            return False
        return now <= offer.start - timedelta(minutes=offers.PREP_MIN_BEFORE_START)

    # ---- retiring --------------------------------------------------------------------------------

    def _expired(self, offer: Offer, now: datetime) -> bool:
        if offer.kind == offers.KIND_PLAN:
            return _wall(now, self.day_zone()) >= offer.expires_at
        return now >= offer.expires_at

    def _expire_offers(self, now: datetime) -> None:
        for offer in list(self._offers.values()):
            if not self._expired(offer, now):
                continue
            if offer.kind == offers.KIND_PLAN:
                self._retire(offer, RETIRE_EXPIRED)
                self._plan_candidate = False
                continue
            if offer.series_key and not offer.clicked:
                self._record_ignored(offer.series_key)   # ended with no click (R38)
            self._retire(offer, RETIRE_ENDED)

    def _retire(self, offer: Offer, how: str) -> None:
        self._offers.pop(offer.id, None)
        if self._plan_offer is offer:
            self._plan_offer = None
        if offer.speech_state in (HELD, QUEUED):
            offer.speech_state = TEXT_ONLY
            offer.held_reason = how
        logger.info("%s offer: retired (%s)", _kind_word(offer), how)
        self.offerRetired.emit(offer, how)

    def sub_line(self, offer: Offer) -> str:
        """The offer's sub line (R49): the cost against the binding limit, from the cached CapCheck;
        a prep offer adds the schedule's read time."""
        caps = self._safe_caps()
        max_runs = int(getattr(self.settings, "max_runs_per_day", 20))
        left_bundle = int(getattr(caps, "left_bundle_day", 0)) if caps is not None else 0
        left_day = int(getattr(caps, "left_day", left_bundle)) if caps is not None else 0
        as_of = ""
        if offer.kind == offers.KIND_PREP and offer.read_at is not None:
            as_of = persona.clock_text(_wall(offer.read_at, self.day_zone()), self._hour24())
        return persona.offer_cost_sub(left_bundle, max_runs, left_day, prep=offer.kind == offers.KIND_PREP,
                                      as_of=as_of)

    def _refresh_sub_lines(self) -> None:
        for offer in self._offers.values():
            if offer.used:
                continue
            sub = self.sub_line(offer)
            if sub != offer.sub:
                offer.sub = sub
                self.offerChanged.emit(offer)

    # ---- small helpers ------------------------------------------------------------------------

    def _address(self) -> str:
        return str(getattr(getattr(self.config, "assistant", None), "address", "sir"))

    def _hour24(self) -> bool:
        return bool(getattr(getattr(self.config, "display", None), "hour24", False))

    def _evening_from_hour(self) -> int:
        try:
            return int(getattr(getattr(self.config, "agenda", None), "evening_from_hour", 18))
        except (TypeError, ValueError):
            return 18

    def _own(self) -> frozenset[str]:
        own = set(self._day_own)
        try:
            own |= {str(address).casefold() for address in self._safe_inputs().own if address}
        except Exception:  # noqa: BLE001
            pass
        return frozenset(own)

    def _state_true(self, name: str, key: str) -> bool:
        state = self._run_state
        if state is None:
            return False
        try:
            return bool(getattr(state, name)(key))
        except Exception:  # noqa: BLE001
            return False

    def _safe_activity(self) -> str:
        try:
            act = self._activity()
        except Exception:  # noqa: BLE001
            return ACT_OFF
        return act if act in (ACT_VISIBLE, ACT_MINIMIZED) else ACT_OFF

    def _safe_facts(self) -> QuietFacts:
        try:
            facts = self._facts()
        except Exception:  # noqa: BLE001
            return QuietFacts(activity=ACT_OFF, speak_ok=False)
        return facts if isinstance(facts, QuietFacts) else QuietFacts(activity=ACT_OFF, speak_ok=False)

    def _safe_ask_state(self) -> tuple[bool, bool, float]:
        try:
            ready, caps_ok, age = self._ask_state()
            return bool(ready), bool(caps_ok), float(age)
        except Exception:  # noqa: BLE001
            return False, False, 0.0

    def _safe_caps(self) -> Any:
        try:
            return self._caps()
        except Exception:  # noqa: BLE001
            return None

    def _safe_inputs(self) -> ModeInputs:
        try:
            inputs = self._inputs()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Productivity: the to-dos could not be read (%s)", type(exc).__name__)
            return ModeInputs()
        return inputs if isinstance(inputs, ModeInputs) else ModeInputs()

    def _safe_signing_in(self) -> bool:
        try:
            return bool(self._signing_in())
        except Exception:  # noqa: BLE001
            return False

    def _refresh_caps_safe(self, now: datetime) -> None:
        asked = self._caps_asked_at
        if asked is not None and now - asked < CAPS_REFRESH_EVERY:
            return
        self._caps_asked_at = now
        try:
            self._refresh_caps()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Productivity: caps refresh failed (%s)", type(exc).__name__)

    # ---- LIVE (the own-reads task; memory only, quiet) -------------------------------------------

    @_live.quiet(_live.NO_TASK)
    def _own_task(self) -> Any:
        live = self._live
        if live is None or not getattr(live, "enabled", False):
            return _live.NO_TASK
        return live.task(_live.TASK_OWN_READS, _live.OWN_READS_TITLE, compact=True, key=_live.OWN_READS_KEY)

    @_live.quiet()
    def _live_day_started(self, job: _DayJob, *, alias: str = "") -> None:
        zone = self.day_zone()
        hour24 = self._hour24()
        if job.reason == READ_MINIMIZED:
            first = persona.clock_text(_wall(job.start, zone), hour24)
            last = persona.clock_text(_wall(job.end, zone), hour24)
            title = f"Checked the {first}-{last} hours of {alias} before an offer (minimized)"
            key = f"hour:{job.key}"
        else:
            title = "Today's events with guests, for prep offers (read only)"
            key = _LIVE_DAY_KEY
        fields = [("Range", f"{_wall(job.start, zone):%a %b %d %H:%M} to {_wall(job.end, zone):%a %b %d %H:%M}"),
                  ("Calendars", ", ".join(self._worker.calendars)), ("Why", _words(job.reason)),
                  ("Read", "titles, times and guests only (no description, place or link); kept in memory")]
        if job.with_zone:
            fields.append(("Also read", "the personal calendar's time zone"))
        self._own_task().step(_live.CALENDAR_DAY, title, key=key, fields=fields)

    @_live.quiet()
    def _live_day_done(self, job: _DayJob, results: Mapping[str, tuple[Sequence[EventBrief], str]]) -> None:
        key = f"hour:{job.key}" if job.reason == READ_MINIMIZED else _LIVE_DAY_KEY
        step = self._own_task().find(_live.CALENDAR_DAY, key)
        counts = []
        problem = False
        for alias, (briefs, status) in results.items():
            if status == "yes":
                counts.append(f"{alias} {len(briefs)}")
                step.field(alias, f"{len(briefs)} event(s)")
            else:
                problem = True
                step.field(alias, f"not read ({status})")
        if not results:
            step.field("Calendars", "none signed in")
        summary = ("; ".join(counts) + "; read only") if counts else "nothing read"
        step.done(_live.STATUS_WARN if problem or not results else _live.STATUS_OK, summary=summary)

    @_live.quiet()
    def _live_failed(self, key: str) -> None:
        self._own_task().find(_live.CALENDAR_DAY, key).done(_live.STATUS_FAILED, summary="failed")

    @_live.quiet()
    def _live_offer(self, title: str, *, key: str = "", status: str = _live.STATUS_OK,
                    fields: Sequence[tuple[str, str]] = ()) -> None:
        self._own_task().step(_live.OFFER_MADE, title, status=status, key=key, fields=list(fields))

    @_live.quiet()
    def _live_plan_pending(self, reason: str) -> None:
        if reason == self._plan_pending:
            return   # the same line already says so
        self._plan_pending = reason
        until = str(getattr(self.settings, "plan_offer_until", "13:30"))
        self._live_offer(f"Not offered yet: plan my day ({_words(reason)})", key=_LIVE_PLAN_KEY,
                         fields=[("For", self._plan_key), ("Retried", f"each minute until {until}")])

    @_live.quiet(False)
    def _live_has(self, key: str) -> bool:
        """The own-reads task has an offer line of that key."""
        return self._own_task().find(_live.OFFER_MADE, key) is not _live.NO_STEP

    @_live.quiet()
    def _live_spoken(self, offer: Offer, words: str) -> None:
        if offer.live_key:
            self._own_task().find(_live.OFFER_MADE, offer.live_key).field("Spoken", words)


_DOT = "\u00b7"


def _kind_word(offer: Offer) -> str:
    return "Plan" if offer.kind == offers.KIND_PLAN else "Prep"


def _speech_order(offer: Offer) -> tuple[int, float, int]:
    """Prep offers first, the soonest meeting first; then the plan offer."""
    if offer.kind == offers.KIND_PREP and offer.start is not None:
        return 0, offer.start.timestamp(), offer.id
    return 1, 0.0, offer.id


def _words(reason: str) -> str:
    return reason.replace("_", " ")


def _speech_words(offer: Offer, reason: str) -> str:
    if offer.speech_state in (QUEUED, SPOKEN):
        return "spoken"
    if offer.speech_state == TEXT_ONLY:
        return f"text only:{reason or offer.held_reason}"
    return f"held:{reason or offer.held_reason}"


def _time_words(event: EventBrief, zone: str, hour24: bool) -> str:
    if event.start is None:
        return "all day"
    return persona.clock_text(_wall(event.start, zone), hour24)
