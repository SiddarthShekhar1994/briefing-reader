"""Fakes for the app-level Ask Jarvis tests (test_ask_controller, test_card_sources): an
AppController offscreen with a player that only keeps its state, Google calendars and senders
that never touch the network, Ask's real planner over tests.ask_fakes' runner (claude.exe is
never started), and a page snapshotter that opens no page (FakeSnapshotter: no web engine).
AppHarness takes a steppable clock (HarnessClock: the app's, the planner's and the productivity
bundle's time, stepped without real waits) and a productivity switch (the bundle on, its reads on
the GUI thread). Every name, address, id and text is invented.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import tempfile
import threading
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
from PySide6.QtWidgets import QApplication

from briefing_reader import config as config_module
from briefing_reader import hud, ui
from briefing_reader.actions import ActionStore
from briefing_reader.ask import planner as ask_planner
from briefing_reader.ask.planner import AskPlanner, ClaudeEngine, GoogleSources
from briefing_reader.ask.usage import UsageLog
from briefing_reader.config import ActionsConfig, CalendarConfig, load_config
from briefing_reader.gcal import RESPONSES, ChangeResult, EventBrief, EventChanged, EventDetails, EventGone,     EventResult, Guest
from briefing_reader.gmail import SentMail
from briefing_reader.notion_client import FixtureSession, NotionClient, fetch_briefing
from tests.ask_fakes import FakeRunner, plan_stream

PDT = timezone(timedelta(hours=-7))
NOW = datetime(2026, 10, 4, 13, 52, tzinfo=PDT)     # a Sunday
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fake_page.json"
COMMAND = "move my Jarvis test sync to Friday and tell the guest"
SAY = "Moved Jarvis test sync to Friday at 2 PM and drafted a note to Sam."
GUEST = "sam.example@example.com"
MOVE_LINE = ("Move: acct=work | event=jts0001aa | cal=primary | when=2026-10-09 14:00-15:00 | notify=all | "
             "title=Jarvis test sync | at=2026-10-05 14:00-15:00 | link= | body=")
EMAIL_LINE = (f"Email: acct=work | to={GUEST} | cc= | subject=Jarvis test sync moved | due= | link= | "
              "body=Hi Sam,\\nMoved to Friday at 2 PM.\\nThanks")
CAL_LINE = "Calendar: Study block | 2026-10-06 19:00-21:00 | | Library |"
B_CALENDAR = "Calendar: Club general meeting | 2026-10-06 17:00-18:00 | | Student union, room 2 |"
B_TODO = "Todo: title=Read chapter 4 | due=2026-10-06 | block= | acct= | link="
CONFIG = """
[ask]
enabled = {enabled}

[accounts.personal]
features = ["calendar", "gmail_send", "gmail_read"]

[accounts.work]
features = ["calendar", "gmail_send", "gmail_read"]
"""


def settle(rounds: int = 3) -> None:
    for _ in range(rounds):
        QApplication.processEvents()


def wait_for(predicate, timeout: float = 5.0) -> bool:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    QApplication.processEvents()
    return bool(predicate())


class FakePlayer(QObject):
    """BriefingPlayer's surface without QtMultimedia: it keeps its state and records calls."""

    itemChanged = Signal(int, int)
    sectionStarted = Signal(int)
    stateChanged = Signal(str)
    errorOccurred = Signal(str)

    def __init__(self, parent: QObject | None = None, **_kwargs: Any) -> None:
        super().__init__(parent)
        self.state = "idle"
        self.current_section: int | None = None
        self.starts: list[list[int]] = []
        self.pauses = 0

    def _set(self, state: str) -> None:
        self.state = state
        self.stateChanged.emit(state)

    def start(self, queue: Sequence[int]) -> None:
        self.starts.append(list(queue))
        self._set("playing")

    def pause(self) -> None:
        if self.state in ("playing", "waiting"):
            self.pauses += 1
            self._set("paused")

    def toggle(self) -> None:
        self._set("paused" if self.state in ("playing", "waiting") else "playing")

    def stop(self) -> None:
        self.state = "idle"

    def set_audio(self, *_args: Any) -> None:
        pass

    def mark_failed(self, *_args: Any) -> None:
        pass

    def skip_section(self) -> None:
        pass

    def replace_upcoming(self, *_args: Any) -> None:
        pass


class RecordingVoice:
    """speech.Voice that records what Jarvis was asked to say and never synthesizes or plays anything.

    ``said`` holds (kind, text, key) for every accepted ``say``; ``speaks`` is what ``say`` answers
    (False: as if speech were off or failed). Muted, nothing is recorded and ``say`` answers False.
    """

    def __init__(self, *, speaks: bool = True) -> None:
        self.speaks = speaks
        self.said: list[tuple[str, str, str]] = []
        self.cancelled: list[str] = []
        self.stops = 0
        self.pumps = 0
        self.owner_actions = 0
        self.shut_down = False
        self._muted = False

    @property
    def speaking(self) -> bool:
        return False

    @property
    def muted(self) -> bool:
        return self._muted

    def say(self, kind: str, text: str, *, key: str = "") -> bool:
        if self._muted:
            return False
        self.said.append((kind, text, key))
        return self.speaks

    def cancel_key(self, key: str) -> None:
        self.cancelled.append(key)

    def stop(self) -> None:
        self.stops += 1

    def pump(self) -> None:
        self.pumps += 1

    def owner_acted(self) -> None:
        self.owner_actions += 1

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)

    def shutdown(self) -> None:
        self.shut_down = True

    def kinds(self) -> list[str]:
        return [kind for kind, _text, _key in self.said]

    def texts(self) -> list[str]:
        return [text for _kind, text, _key in self.said]


def event(event_id: str, title: str, start: datetime, *, organizer_self: bool = True) -> EventDetails:
    return EventDetails(event_id=event_id, calendar_id="primary", title=title, start=start,
                        end=start + timedelta(hours=1), all_day_start=None, all_day_end=None, status="confirmed",
                        organizer_self=organizer_self, guests_can_modify=False, self_email="me@example.edu",
                        self_response="accepted", attendee_count=2, recurring_instance=False, series=False,
                        time_zone="America/Los_Angeles", link="https://www.google.com/calendar/event?eid=x",
                        organizer="")


class FakeCalendar:
    """gcal.GoogleCalendar's surface for one account (never the network). ``calls`` and ``threads``
    record what happened where. ``stamps``: like Google, every change answers with the event's new
    version stamp (an etag) and Google's view from before it, so the app offers Undo; ``events``
    then follow every change (an added event, an answer, a move) and the undo_* calls reverse them,
    refused (EventChanged) when the event is gone or its stamp moved on."""

    def __init__(self, alias: str, *, signed_in: bool = True, events: dict | None = None,
                 guests: dict | None = None, stamps: bool = False) -> None:
        self.alias = alias
        self.stamps = stamps
        self.version = 0
        self.notified: list[tuple[str, str]] = []   # (call, sendUpdates) of every change and Undo
        self.signed_in = signed_in
        self.events = dict(events or {})
        self.guests = dict(guests or {})
        self.calls: list[tuple] = []
        self.threads: set[str] = set()
        self.footers: list[str] = []
        self.on_sign_in = None
        self.lock = threading.Lock()
        # EventBriefs as given (list_event_briefs then returns those that start inside the window;
        # None: built from ``events`` and ``guests`` as before). ``brief_error``: raised by a read.
        self.briefs: list[EventBrief] | None = None
        self.brief_error: Exception | None = None

    def _note(self, *call: Any) -> None:
        with self.lock:
            self.calls.append(call)
            self.threads.add(threading.current_thread().name)

    def is_configured(self) -> bool:
        return True

    def is_signed_in(self) -> bool:
        return self.signed_in

    def sign_in_problem(self) -> tuple[str, str]:
        return ("", "")

    def change_problem(self) -> tuple[str, str]:
        return ("", "")

    def needs_confirmation(self) -> bool:
        return False

    def pending_confirmation(self) -> str:
        return ""

    def sign_in(self) -> None:
        self._note("sign_in")
        self.signed_in = True
        if self.on_sign_in is not None:
            self.on_sign_in()

    def timezone(self, *, interactive: bool = True) -> str:
        return "America/Los_Angeles"

    def list_events(self, start, end, calendar_ids=("primary",)):
        self._note("list")
        return []

    def list_event_briefs(self, start, end, calendar_ids=("primary",)):
        self._note("briefs")
        if self.brief_error is not None:
            raise self.brief_error
        if self.briefs is not None:
            return [brief for brief in self.briefs
                    if brief.start is None or (brief.start < end and (brief.end or brief.start) > start)]
        return [EventBrief(e.event_id, "primary", e.title, start=e.start, end=e.end, organizer_self=e.organizer_self,
                           guests=self.guests.get(e.event_id, ())) for e in self.events.values()]

    def get_event(self, event_id: str, *, calendar_id=None, interactive: bool = True) -> EventDetails:
        self._note("get", event_id)
        if event_id not in self.events:
            raise EventGone("Google has no such event (HTTP 404)", status=404)
        return self.events[event_id]

    def _stamped(self, event_id: str, **changes: Any) -> EventDetails:
        """``event_id``'s details with ``changes`` and a new version stamp (stored)."""
        self.version += 1
        details = dataclasses.replace(self.events[event_id], stamp=f"etag-{event_id}-{self.version}", **changes)
        self.events[event_id] = details
        return details

    def touch(self, event_id: str) -> None:
        """Someone else changed the event in Google Calendar since (its stamp moves on)."""
        self._stamped(event_id)

    def move(self, event_id, start, end, *, send_updates="all", calendar_id=None, now=None, interactive=True,
             **_kw: Any):
        self._note("move", event_id)
        self.notified.append(("move", send_updates))
        if not self.stamps:
            return ChangeResult(event_id=event_id, link="", already=False)
        before = self.events[event_id]
        after = self._stamped(event_id, start=start.replace(tzinfo=PDT), end=end.replace(tzinfo=PDT))
        return ChangeResult(event_id=event_id, link="", already=False, stamp=after.stamp, before=before)

    def respond(self, event_id, answer, *, comment="", send_updates="all", calendar_id=None, interactive=True,
                **_kw: Any):
        self._note("respond", event_id, answer)
        self.notified.append(("respond", send_updates))
        before = self.events[event_id]
        if not self.stamps:
            return ChangeResult(event_id=event_id, link="", already=False)
        after = self._stamped(event_id, self_response=RESPONSES[answer], self_comment=comment or before.self_comment)
        return ChangeResult(event_id=event_id, link="", already=False, stamp=after.stamp, before=before)

    def cancel(self, event_id, *, send_updates="all", calendar_id=None, interactive=True, **_kw: Any):
        self._note("cancel", event_id)
        self.notified.append(("cancel", send_updates))
        self.events.pop(event_id, None)
        return ChangeResult(event_id=event_id, link="", already=False)

    def create_event(self, action, interactive: bool = True, **kwargs: Any) -> EventResult:
        self._note("create", action.title)
        self.footers.append(kwargs.get("footer", ""))   # "" = the default footer (the keyword is left out)
        link = "https://www.google.com/calendar/event?eid=new"
        if not self.stamps:
            return EventResult(event_id="evt-new", link=link, existed=False)
        event_id = f"evt-new-{self.version + 1}"
        start = action.start if action.start is not None else NOW
        self.events[event_id] = event(event_id, action.title, start.replace(tzinfo=PDT))
        details = self._stamped(event_id)
        return EventResult(event_id=event_id, link=link, existed=False, stamp=details.stamp)

    def _unchanged(self, event_id: str, stamp: str) -> EventDetails:
        details = self.events.get(event_id)
        if details is None or details.stamp != stamp:
            raise EventChanged("The event changed since Jarvis's change")
        return details

    def undo_create(self, event_id, *, stamp, send_updates="none", calendar_id=None, uncertain=False, **_kw: Any):
        self._note("undo_create", event_id)
        self.notified.append(("undo_create", send_updates))
        self._unchanged(event_id, stamp)
        del self.events[event_id]
        return ChangeResult(event_id=event_id, link="", already=False)

    def undo_respond(self, event_id, response, *, comment="", stamp, send_updates="all", calendar_id=None,
                     uncertain=False, **_kw: Any):
        self._note("undo_respond", event_id, response)
        self.notified.append(("undo_respond", send_updates))
        self._unchanged(event_id, stamp)
        after = self._stamped(event_id, self_response=response, self_comment=comment)
        return ChangeResult(event_id=event_id, link="", already=False, stamp=after.stamp)

    def undo_move(self, event_id, start, end, *, time_zone="", stamp, send_updates="all", calendar_id=None,
                  uncertain=False, **_kw: Any):
        self._note("undo_move", event_id)
        self.notified.append(("undo_move", send_updates))
        self._unchanged(event_id, stamp)
        after = self._stamped(event_id, start=start, end=end)
        return ChangeResult(event_id=event_id, link="", already=False, stamp=after.stamp)


class FakeSender:
    def __init__(self, alias: str, address: str) -> None:
        self.alias = alias
        self.address = address
        self.sent: list[Any] = []

    def is_configured(self) -> bool:
        return True

    def is_signed_in(self) -> bool:
        return True

    def from_address(self) -> str:
        return self.address

    def sign_in_problem(self) -> tuple[str, str]:
        return ("", "")

    def ready(self) -> tuple[str, str]:
        return ("", "")

    def pending_confirmation(self) -> str:
        return ""

    def send(self, mail: Any) -> SentMail:
        self.sent.append(mail)
        return SentMail("m1", "t1", "")


def work_calendar() -> FakeCalendar:
    return FakeCalendar("work", events={"jts0001aa": event("jts0001aa", "Jarvis test sync",
                                                           datetime(2026, 10, 5, 14, tzinfo=PDT))},
                        guests={"jts0001aa": (Guest(GUEST, "Sam Example", "accepted"),)})


class AskFactory:
    """ask_factory for AppController: the real AskPlanner over a FakeRunner (``runs``), the app's own
    (fake) calendars and senders through GoogleSources, and ``readers``."""

    def __init__(self, root: Path, *runs: Any, auth: str | None = None, readers: dict | None = None,
                 max_per_hour: int = 20, clock: Any = None, planner_clock: Any = None) -> None:
        self.root = root
        self.runs = list(runs)
        self.auth = auth
        self.readers = dict(readers or {})
        self.max_per_hour = max_per_hour
        self.clock = clock
        self.planner_clock = planner_clock or (lambda: NOW)   # the planner's and the usage log's time
        self.runner: FakeRunner | None = None
        self.planner: AskPlanner | None = None
        self.quick_threads: list[str] = []

    def __call__(self, config: Any, calendars: Any, senders: Any, _accounts: Any) -> AskPlanner:
        kwargs = {} if self.auth is None else {"auth": self.auth}
        runner = FakeRunner(*self.runs, **kwargs)
        quick = runner.run

        def run(*args: Any, **kw: Any) -> Any:
            self.quick_threads.append(threading.current_thread().name)
            return quick(*args, **kw)

        runner.run = run   # type: ignore[method-assign]
        self.runner = runner
        exe = self.root / "bin" / "claude.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"MZ fake")
        engine_kwargs = {"clock": self.clock} if self.clock is not None else {}
        engine = ClaudeEngine(config.ask, config.data_dir, runner=runner,
                              environ={"PATH": "C:/Windows", "ANTHROPIC_API_KEY": "sk-ant-dummy-not-for-the-child",
                                       "LOCALAPPDATA": str(self.root / "local")},
                              locate=lambda _env: ask_planner.cli.ClaudeExe(exe, "standalone install"), **engine_kwargs)
        sources = GoogleSources(config, calendars, senders, self.readers)
        usage = UsageLog(config.data_dir / "ask_usage.json", max_per_hour=self.max_per_hour,
                         max_per_day=config.ask.max_per_day, clock=self.planner_clock)
        self.planner = AskPlanner(config, engine, sources, usage=usage, clock=self.planner_clock)
        return self.planner


def tiny_jpeg(width: int = 48, height: int = 30) -> bytes:
    """A small JPEG drawn by QImage (two colour bands: not "blank") - a fake page picture."""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QColor, QImage

    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#e9eef2"))
    for y in range(height // 3):
        for x in range(width):
            image.setPixelColor(x, y, QColor("#2a6f97"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "JPG", 80)
    buffer.close()
    return bytes(data.data())


class FakeSnapshotter:
    """snapshot_engine.PageSnapshotter's surface without a web engine: no page is ever opened.

    ``request`` refuses what the real one refuses (shut down, text off, not public https, a search
    results page, past ``max_per_run``, the same page again; an UNAVAILABLE picture) and otherwise
    shows the PENDING picture, like the real one. Every request with a LIVE step is recorded in
    ``requests`` as (url, task_id) and its step in ``steps``; ``complete(index, ok=True)`` delivers
    request ``index``'s READY picture (a tiny JPEG drawn by QImage) or an unavailable one.
    ``ended``, ``cancelled``, ``cleared`` and ``shutdowns`` record the other calls."""

    def __init__(self, *, keep_text: bool = True, max_per_run: int = 4) -> None:
        from briefing_reader import snapshots

        self.keep_text = keep_text
        self.max_per_run = max_per_run
        self.requests: list[tuple[str, int]] = []
        self.steps: list[Any] = []
        self.pending: list[bool] = []       # per request: it got a PENDING picture (not refused)
        self.ended: list[int] = []
        self.cancelled: list[int] = []
        self.cleared = 0
        self.shutdowns = 0
        self.threads: list[str] = []
        self._budget = snapshots.RunBudget()
        self._lock = threading.Lock()

    def request(self, step: Any, url: str, *, task_id: int) -> None:
        from briefing_reader import pictures, snapshots

        if step is None or step.view() is None:
            return
        with self._lock:
            self.requests.append((url, task_id))
            self.steps.append(step)
            self.threads.append(threading.current_thread().name)
        if self.shutdowns:
            reason = snapshots.R_SHUTDOWN
        elif not self.keep_text:
            reason = snapshots.R_TEXT_OFF
        else:
            reason = snapshots.snapshot_url_problem(url) or self._budget.admit(task_id, url, self.max_per_run)
        with self._lock:
            self.pending.append(not reason)
        step.picture(pictures.page_unavailable(url, reason) if reason else pictures.page_pending(url))

    def complete(self, index: int, ok: bool = True, reason: str = "", final_url: str = "") -> None:
        """Request ``index``'s picture: READY (``final_url``: the page ended up there) or unavailable."""
        from briefing_reader import pictures, snapshots

        assert self.pending[index], "that request was refused: it has no picture to complete"
        url, step = self.requests[index][0], self.steps[index]
        if ok:
            step.picture(pictures.page_ready(url, image=tiny_jpeg(), image_format="JPG", width=48, height=30,
                                             final_url=final_url or url, took_ms=1500, still_loading=False,
                                             blocked=1))
        else:
            step.picture(pictures.page_unavailable(url, reason or snapshots.R_LOAD_FAILED))

    def end_run(self, task_id: int) -> None:
        self._budget.end(task_id)
        with self._lock:
            self.ended.append(task_id)

    def cancel_task(self, task_id: int) -> None:
        self._budget.cancel(task_id)
        with self._lock:
            self.cancelled.append(task_id)

    def drop_cleared(self) -> None:
        self.cleared += 1

    def shutdown(self) -> None:
        self.shutdowns += 1

    def idle(self) -> bool:
        return True

    def counts(self) -> dict[str, int]:
        return {"requested": len(self.requests), "taken": 0, "unavailable": 0,
                "refused": sum(1 for pending in self.pending if not pending)}


def plan(say: str = SAY, question: str = "", lines: Sequence[str] = (MOVE_LINE, EMAIL_LINE)) -> list[str]:
    return plan_stream({"say": say, "question": question, "lines": list(lines)})


def page_fixture(root: Path, lines: Sequence[str]) -> Path:
    """fake_page.json with a "Proposed actions" section of ``lines``."""
    data = copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8")))
    blocks = data["responses"][data["page_id"]][-1]["results"]

    def block(kind: str, text: str, n: int) -> dict:
        rich = [{"type": "text", "text": {"content": text}, "annotations": {"bold": False}, "plain_text": text}]
        return {"object": "block", "id": f"uf-{n:04d}-0000-0000-0000-000000000000", "type": kind,
                kind: {"rich_text": rich}, "has_children": False}

    blocks.append(block("heading_2", "Proposed actions", 0))
    blocks.extend(block("bulleted_list_item", line, n + 1) for n, line in enumerate(lines))
    path = root / "page.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class HarnessClock:
    """A wall clock a test steps by hand (R24): ``now()`` is what the app, the planner and the
    productivity bundle see; ``advance(minutes)`` moves it on; ``set(moment)`` puts it anywhere."""

    def __init__(self, start: datetime = NOW) -> None:
        self.at = start

    def now(self) -> datetime:
        return self.at

    def advance(self, minutes: float = 0, *, seconds: float = 0) -> datetime:
        self.at = self.at + timedelta(minutes=minutes, seconds=seconds)
        return self.at

    def set(self, moment: datetime) -> datetime:
        self.at = moment
        return self.at


PRODUCTIVITY_ON = "\n[productivity]\nenabled = true\n"


class AppHarness:
    """One AppController (offscreen, nothing real reached) and what it was built with.

    Page pictures: ``snapshots`` is a FakeSnapshotter the controller gets (while [live]
    page_snapshots is on) - no web engine, no page; ``snapshot_factory`` replaces it (a harness
    passes the real snapshotter with local HTML).

    ``clock``: a HarnessClock (default: fixed at NOW) for the app, the planner and the bundle.
    ``productivity``: the bundle on ([productivity] enabled plus ``prod_extra`` keys, unless
    ``config_extra`` has the table already); ``prod_inline``: its reads run on the GUI thread
    (deterministic tests). ``prepare(config)`` runs before the controller is made (a test puts a file in
    its data folder, as an earlier run would have left it). close() stops the bundle's timer and thread.

    The desktop HUD ([desktop] enabled in ``config_extra``): ``desktop_factory(config)`` makes its
    windows (a test passes hud_overlay.create with a fake native seam; None: hud_overlay.create, whose
    seam makes no OS call while the tests run); ``desktop_backend()`` makes the desktop thread's backend
    (a test passes a tests.desktop_fakes fake; None: the real one, which refuses to be made while the tests
    run - a desktop request then says Jarvis can't look on this PC). close() shuts the desktop controller
    down (its thread ends).
    ``launch`` (models.LAUNCH_*; None: derived as the app derives it) and ``desktop_launch`` (--hud)
    reach the controller for a test that calls its start()."""

    def __init__(self, test: Any, *, ask: bool = True, runs: Sequence[Any] = (), auth: str | None = None,
                 readers: dict | None = None, ask_mode: bool = False, run_state: Any = None,
                 lines: Sequence[str] = (B_CALENDAR, B_TODO), engine_clock: Any = None,
                 max_per_hour: int = 20, config_extra: str = "", snapshot_factory: Any = None,
                 clock: HarnessClock | None = None, productivity: bool = False, prod_extra: str = "",
                 prod_inline: bool = True, desktop_factory: Any = None, launch: str | None = None,
                 desktop_launch: bool = False, prepare: Any = None, desktop_backend: Any = None) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        test.addCleanup(tmp.cleanup)
        self.root = root = Path(tmp.name)
        self.clock = clock or HarnessClock()
        if productivity and "[productivity]" not in config_extra:
            config_extra = PRODUCTIVITY_ON + prod_extra + config_extra
        (root / "config.toml").write_text(CONFIG.format(enabled="true" if ask else "false") + config_extra,
                                          encoding="utf-8")
        base = load_config(root, environ={"LOCALAPPDATA": str(root / "local")})
        self.fixture = page_fixture(root, lines)
        session = FixtureSession(self.fixture)
        self.config = dataclasses.replace(
            base, page_id=session.page_id, audio_root=root / "audio",
            calendar=CalendarConfig(enabled=True, client_secret_path=root / "absent.json"),
            actions=ActionsConfig(undo_seconds=3, trusted_domains=("example.edu",)))
        self.client = NotionClient("", session=session, notion_version=self.config.notion_version)
        self.calendars = {"personal": FakeCalendar("personal"), "work": work_calendar()}
        self.senders = {"personal": FakeSender("personal", "me@example.com"),
                        "work": FakeSender("work", "me@example.edu")}
        self.factory = AskFactory(root, *runs, auth=auth, readers=readers, clock=engine_clock,
                                  max_per_hour=max_per_hour, planner_clock=self.clock.now)
        self.store = ActionStore(root / "actions.json")
        self.voice = RecordingVoice()   # Jarvis's own voice: recorded, never synthesized
        live_config = self.config.live
        self.snapshots = FakeSnapshotter(keep_text=live_config.text,
                                         max_per_run=max(0, min(4, self.config.research.max_fetches)))
        if prepare is not None:
            prepare(self.config)
        with mock.patch.object(ui, "BriefingPlayer", FakePlayer), mock.patch.object(ui, "PROD_INLINE", prod_inline):
            self.c = ui.AppController(self.config, self.client, expected_run="AM", now_mode=False, volume=0.0,
                                      now_func=self.clock.now, calendar_factory=lambda _cfg: self.calendars,
                                      sender_factory=lambda _cfg: self.senders, action_store=self.store,
                                      click_clock=_StepClock(), ask_factory=self.factory, ask_mode=ask_mode,
                                      run_state=run_state, voice_factory=lambda _controller: self.voice,
                                      snapshot_factory=snapshot_factory or (lambda _controller: self.snapshots),
                                      desktop_factory=desktop_factory, launch=launch, desktop_launch=desktop_launch,
                                      desktop_backend=desktop_backend)
        self.c._start_fetch = lambda *args, **kwargs: None   # never Notion: the page is handed over below
        test.addCleanup(self.close)

    def briefing(self) -> None:
        """The fixture page, as a fetch would hand it over (its Proposed actions become the cards)."""
        self.c._accept_briefing(fetch_briefing(self.client, self.config.page_id))

    def reading(self, *, autoplay: bool = True) -> None:
        self.briefing()
        self.c.enter_reading(autoplay=autoplay)
        self.c.window.resize(1120, 700)
        self.c.window.show()
        settle()

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return   # closed already (a test may close early; the cleanup comes back here)
        self._closed = True
        c = self.c
        c.live.set_listener(None)   # the LIVE feed hears nothing more
        c._live_feed.stop()
        if c.ask is not None:
            c.ask.shutdown()
            c.ask.join(5)
        if getattr(c, "productivity", None) is not None:
            c.productivity.shutdown()   # its timer stops and its thread ends (R24)
            c.productivity.join(5)
        if getattr(c, "meetings", None) is not None:
            c.meetings.shutdown()       # a recording stops and is saved; the pill goes
        if getattr(c, "desktop", None) is not None:
            c.desktop.shutdown()        # the HUD's windows go
            c.desktop.join(5)           # ... and the desktop thread ends
        if c._snapshots is not None:
            c._snapshots.shutdown()   # a real snapshotter (a harness's) closes its hidden view
        for timer in (c._ignore_timer, c._tick_timer, c._snooze_timer, c._send_hold_timer, c._undo_timer,
                      c._agenda_timer):
            timer.stop()
        if c._calendar_worker is not None:
            c._closing.set()
            c._calendar_worker.stop()
        c.state = ui.STATE_QUITTING
        for window in (c._live_window, c.window):
            if window is not None:
                window.hide()
                window.deleteLater()
        settle()
        # Delete the windows and the controller now, on this (the GUI) thread: left to Python's
        # garbage collector (the controller and its Ask controller refer to each other), they
        # could be deleted by a collection that happens to run on a worker thread of a later test.
        c.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


class _StepClock:
    """Click clock that is 5 s later at every reading (no click is ever "too soon")."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 5.0
        return self.t


def fonts() -> None:
    hud.load_fonts(config_module.PROJECT_ROOT / "fonts")
