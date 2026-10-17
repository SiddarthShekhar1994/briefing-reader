"""Tests for briefing_reader.executor: backend choice, readiness, Calendar kinds, edits, one job
of the action worker (running before the call, unknown after a crash, never retried), accounts
and calendars from config.toml, and what the cards say.

Every Google call goes to an in-memory FakeCalendar; nothing touches the network, a browser or
the owner's data folder. Names, addresses and ids are invented.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import socket
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from briefing_reader import actions, executor, gcal, gmail, google_auth, live, recipients
from briefing_reader.actions import (
    CALENDAR,
    RSVP,
    INTERRUPTED_MESSAGE,
    STATUS_CREATED,
    STATUS_EXISTS,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SENT,
    STATUS_UNKNOWN,
    ActionStore,
    ProposedAction,
    parse_action_line,
)
from briefing_reader.config import AccountConfig, load_config
from briefing_reader.executor import (
    CALENDAR_SETUP_NOTE,
    COMPOSIO_NOTE,
    HOLD_ONLY_MESSAGE,
    DISCONNECT_FAILED_MESSAGE,
    MAIL_SETUP_NOTE,
    NEW_RECIPIENTS_MESSAGE,
    NOT_SAVED_MESSAGE,
    NOTHING_TO_DO,
    OUTCOME_FAILED,
    OUTCOME_UNKNOWN,
    STAGE_SIGNED_IN,
    STAGE_SIGNIN,
    STAGE_WORKING,
    ActionEdit,
    CalendarBackend,
    EditError,
    EventCheck,
    ExecError,
    ExecResult,
    Executor,
    GmailBackend,
    RunOutcome,
    account_of,
    apply_edit,
    build_accounts,
    build_calendars,
    build_executor,
    build_senders,
    check_event,
    check_failure,
    recipient_history,
    run_action,
    sign_in_note,
)
from briefing_reader.gmail import GmailError, GmailUnknownOutcome, OutgoingMail, SentMail
from briefing_reader.recipients import KNOWN, NEW, TRUSTED, RecipientHistory
from briefing_reader.gcal import (
    CalendarAuthError,
    CalendarError,
    CalendarNotSignedIn,
    CalendarUnknownOutcome,
    ChangeResult,
    EventDetails,
    EventGone,
    EventResult,
    GoogleCalendar,
    NotAllowed,
)
from briefing_reader.google_auth import (
    PROBLEM_BLOCKED,
    PROBLEM_CONFIRM,
    PROBLEM_DENIED,
    PROBLEM_EXPIRED,
    PROBLEM_FAILED,
    PROBLEM_IDENTITY,
    PROBLEM_SCOPE,
    PROBLEM_SETUP,
    PROBLEM_SIGNED_OUT,
    PROBLEM_TIMEOUT,
    AccountBindings,
    GoogleAccount,
)

DOT = " " + chr(0xB7) + " "
TODAY = date(2026, 10, 4)
EDT = timezone(timedelta(hours=-4), "EDT")
EVENT_LINK = "https://www.google.com/calendar/event?eid=ZmFrZQ"
EXECUTOR_LOGGER = "briefing_reader.executor"

PAIRS = {
    "RSVP": (("acct", "work"), ("event", "abc123def456ghi789"), ("cal", "primary"), ("answer", "yes"),
             ("notify", "all"), ("title", "Speaker series"), ("at", "2026-10-06 17:00-18:00"),
             ("due", "2026-10-06"), ("link", "https://calendar.google.com/calendar/event?eid=ZXhhbXBsZQ"),
             ("body", "")),
    "Move": (("acct", "work"), ("event", "abc123def456ghi789_20261008T190000Z"), ("cal", "primary"),
             ("when", "2026-10-08 14:00-15:00"), ("notify", "all"), ("title", "Project sync"),
             ("at", "2026-10-08 12:00-13:00"), ("link", ""), ("body", "Moving to 2 PM so everyone can join.")),
    "Cancel": (("acct", "personal"), ("event", "zyx987wvu654tsr321"), ("cal", "primary"), ("notify", "all"),
               ("title", "Study group"), ("at", "2026-10-09 18:00-19:00"), ("link", ""), ("body", "")),
    "Todo": (("title", "Work on Problem set 3"), ("due", "2026-10-07 23:59"), ("block", "2026-10-06 19:00-21:00"),
             ("acct", "work"), ("link", "https://example.instructure.com/courses/1/assignments/2")),
    "Reply": (("acct", "work"), ("thread", "18c0ffee00000001"), ("msgid", "CAExample0001@mail.example.com"),
              ("gmid", ""), ("to", "ana@example.edu, ben@example.edu"), ("cc", ""),
              ("subject", "Re: Thursday noon meeting"), ("replied", "no"), ("due", ""), ("link", ""),
              ("body", "Hi both")),
}
CHESS = "Calendar: Chess Club Weekly Meeting | 2026-10-09 15:00-16:00"


def example(label: str, **changes: str) -> ProposedAction:
    items = [f"{key}={changes.get(key, value)}" for key, value in PAIRS[label]]
    action = parse_action_line(f"{label}: " + " | ".join(items))
    assert not action.error, action.error
    return action


def details(*, organizer_self: bool = True, me: str = "needsAction", guests_can_modify: bool = False,
            series: bool = False, instance: bool = False, all_day: bool = False,
            organizer: str = "", title: str = "Project sync",
            start: datetime | None = datetime(2026, 10, 8, 12, 0, tzinfo=EDT)) -> EventDetails:
    start = None if all_day else start
    end = None if start is None else start + timedelta(hours=1)
    return EventDetails(
        event_id="abc123def456ghi789", calendar_id="primary", title=title, start=start, end=end,
        all_day_start=date(2026, 10, 8) if all_day else None, all_day_end=date(2026, 10, 8) if all_day else None,
        status="confirmed", organizer_self=organizer_self, guests_can_modify=guests_can_modify,
        self_email="you@example.edu" if me else "", self_response=me, attendee_count=3,
        recurring_instance=instance, series=series, time_zone="America/New_York", link=EVENT_LINK,
        organizer=organizer, organizer_email="" if organizer_self else "ana@example.edu")


SPEAKER = {"title": "Speaker Series: Dr. Example", "start": datetime(2026, 10, 6, 17, 0, tzinfo=EDT)}


class FakeCalendar:
    """gcal.GoogleCalendar's surface as used by CalendarBackend; records every call and, with
    ``store``, what actions.json said (on disk) at the moment of each change."""

    def __init__(self, *, configured: bool = True, signed_in: bool = True, store: ActionStore | None = None,
                 watch: str = "") -> None:
        self.configured = configured
        self.signed = signed_in
        self.store = store
        self.watch = watch
        self.calls: list[tuple[Any, ...]] = []
        self.on_disk_at_change: list[str | None] = []
        self.sign_in_error: BaseException | None = None
        self.outcomes: dict[str, Any] = {}

    # -- no network --
    def is_configured(self) -> bool:
        self.calls.append(("is_configured",))
        return self.configured

    def is_signed_in(self) -> bool:
        self.calls.append(("is_signed_in",))
        return self.signed

    # -- the browser --
    def sign_in(self) -> None:
        self.calls.append(("sign_in", self._on_disk()))
        if self.sign_in_error is not None:
            raise self.sign_in_error
        self.signed = True

    # -- Google --
    def _call(self, name: str, default: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((name, args, kwargs))
        if name != "get_event":
            self.on_disk_at_change.append(self._on_disk())
        outcome = self.outcomes.get(name, default)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def create_event(self, action: ProposedAction, **kwargs: Any) -> EventResult:
        return self._call("create_event", EventResult("evt1", EVENT_LINK, existed=False), action, **kwargs)

    def respond(self, event_id: str, answer: str, **kwargs: Any) -> ChangeResult:
        return self._call("respond", ChangeResult(event_id, EVENT_LINK, already=False), event_id, answer, **kwargs)

    def move(self, event_id: str, start: datetime, end: datetime, **kwargs: Any) -> ChangeResult:
        return self._call("move", ChangeResult(event_id, EVENT_LINK, already=False), event_id, start, end, **kwargs)

    def cancel(self, event_id: str, **kwargs: Any) -> ChangeResult:
        return self._call("cancel", ChangeResult(event_id, "", already=False), event_id, **kwargs)

    def get_event(self, event_id: str, **kwargs: Any) -> EventDetails:
        return self._call("get_event", details(), event_id, **kwargs)

    # -- helpers --
    def _on_disk(self) -> str | None:
        if self.store is None or not self.store.path.exists():
            return None
        entry = json.loads(self.store.path.read_text(encoding="utf-8")).get(self.watch)
        return entry["status"] if entry else None

    def changes(self) -> list[tuple[Any, ...]]:
        return [call for call in self.calls if call[0] in ("create_event", "respond", "move", "cancel")]

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


class HookCalendar(FakeCalendar):
    """A FakeCalendar that, like gcal.GoogleCalendar, takes on_request and tells it the request it
    would send (``tamper``: keys changed in it, as a bug in between would)."""

    REQUEST_HOOK = True
    ZONE = "America/New_York"

    def __init__(self, *, tamper: dict[str, Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.tamper = dict(tamper or {})
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def _call(self, name: str, default: Any, *args: Any, **kwargs: Any) -> Any:
        hook = kwargs.pop("on_request", None)
        request = self._request(name, *args, **kwargs)
        if hook is not None and request is not None:
            method, params = request
            params.update(self.tamper)
            self.requests.append((method, params))
            hook(method, json.loads(json.dumps(params)))
        return super()._call(name, default, *args, **kwargs)

    def _request(self, name: str, *args: Any, **kwargs: Any) -> tuple[str, dict[str, Any]] | None:
        notify = kwargs.get("send_updates", "all")
        updates = {"external": "externalOnly"}.get(notify, notify)
        target = {"calendarId": kwargs.get("calendar_id") or "primary"}
        if name == "create_event":
            footer = kwargs.get("footer", gcal.EVENT_FOOTER)
            return "events.insert", {**target, "body": gcal.build_event_body(args[0], self.ZONE, footer=footer)}
        if name == "respond":
            attendee = {"email": "me@example.edu", "responseStatus": gcal.RESPONSES[args[1]]}
            if kwargs.get("comment"):
                attendee["comment"] = kwargs["comment"]
            return "events.patch", {**target, "eventId": args[0], "sendUpdates": updates,
                                    "body": {"attendeesOmitted": True, "attendees": [attendee]}}
        if name == "move":
            wall = [moment.strftime("%Y-%m-%dT%H:%M:%S") for moment in args[1:3]]
            return "events.patch", {**target, "eventId": args[0], "sendUpdates": updates,
                                    "body": {"start": {"dateTime": wall[0], "timeZone": self.ZONE},
                                             "end": {"dateTime": wall[1], "timeZone": self.ZONE}}}
        if name == "cancel":
            return "events.delete", {**target, "eventId": args[0], "sendUpdates": updates}
        return None


ACCOUNTS = {"personal": AccountConfig("personal"), "work": AccountConfig("work")}


class ExecutorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = ActionStore(self.root / "actions.json")
        self.personal = FakeCalendar(store=self.store)
        self.work = FakeCalendar(store=self.store)
        self.now = datetime(2026, 10, 1, 9, 0, tzinfo=EDT)
        self.stages: list[str] = []

    def executor(self, accounts: dict[str, AccountConfig] | None = None,
                 calendars: dict[str, Any] | None = None) -> Executor:
        if calendars is None:
            calendars = {"personal": self.personal, "work": self.work}
        backend = CalendarBackend(calendars, now=lambda: self.now)
        return Executor([backend], ACCOUNTS if accounts is None else accounts)

    def watch(self, action: ProposedAction) -> ProposedAction:
        self.personal.watch = self.work.watch = action.id
        return action


# --------------------------------------------------------------------------
# Choosing the backend, readiness
# --------------------------------------------------------------------------

class SelectionTests(ExecutorTestCase):
    def test_account_of(self) -> None:
        self.assertEqual(account_of(example("RSVP")), "work")
        self.assertEqual(account_of(example("Cancel")), "personal")
        self.assertEqual(account_of(parse_action_line(CHESS)), "personal")
        self.assertEqual(account_of(example("Todo")), "personal")   # acct= of a to-do does not count yet

    def test_backend_by_kind_and_account(self) -> None:
        run = self.executor({"work": AccountConfig("work"), "lab": AccountConfig("lab", backend="composio")})
        self.assertIsInstance(run.backend_for(example("RSVP")), CalendarBackend)
        self.assertIsInstance(run.backend_for(parse_action_line(CHESS)), CalendarBackend)   # no [accounts] needed
        self.assertIsNone(run.backend_for(example("RSVP", acct="lab")))       # no Composio backend built
        self.assertIsNone(run.backend_for(example("RSVP", acct="school")))    # no such account
        self.assertIsNone(run.backend_for(example("Reply")))                  # nothing sends email yet

    def test_ready(self) -> None:
        run = self.executor()
        for action in (example("RSVP"), example("Move"), example("Cancel"), example("Todo"),
                       parse_action_line(CHESS)):
            with self.subTest(kind=action.kind):
                self.assertEqual(run.readiness(action), "")
        self.assertEqual(self.personal.names() + self.work.names(), ["is_configured"] * 5)

    def test_readiness_messages(self) -> None:
        accounts = {"work": AccountConfig("work", features=()), "lab": AccountConfig("lab", backend="composio"),
                    "personal": AccountConfig("personal")}
        run = self.executor(accounts)
        self.assertEqual(run.readiness(example("RSVP", acct="school")),
                         'No account named "school" in config.toml [accounts]')
        self.assertEqual(run.readiness(example("RSVP", acct="lab")), COMPOSIO_NOTE)
        self.assertEqual(run.readiness(example("Move")),
                         "The work account is not set up for calendar actions (config.toml [accounts.work] features)")
        self.assertEqual(run.readiness(example("Reply")),
                         "The work account is not set up for sending email (config.toml [accounts.work] features)")
        self.assertEqual(run.readiness(example("Reply", replied="yes")), NOTHING_TO_DO)
        self.assertEqual(run.readiness(example("Todo", block="")), NOTHING_TO_DO)
        self.assertEqual(run.readiness(parse_action_line("Calendar: Dentist | sometime")), NOTHING_TO_DO)

    def test_calendar_not_set_up(self) -> None:
        self.assertEqual(self.executor(calendars={"personal": self.personal}).readiness(example("RSVP")),
                         CALENDAR_SETUP_NOTE)
        self.work.configured = False
        self.assertEqual(self.executor().readiness(example("RSVP")), CALENDAR_SETUP_NOTE)
        self.assertEqual(self.executor(calendars={}).readiness(parse_action_line(CHESS)), CALENDAR_SETUP_NOTE)

    def test_not_ready_is_never_sent(self) -> None:
        run = self.executor({"personal": AccountConfig("personal")})
        for call in (run.execute, run.prepare, run.peek):
            with self.subTest(call=call.__name__), self.assertRaises(ExecError) as ctx:
                call(example("RSVP"))
            self.assertEqual((ctx.exception.problem, ctx.exception.outcome), (PROBLEM_SETUP, OUTCOME_FAILED))
        self.assertEqual(self.work.calls, [])

    def test_signed_in(self) -> None:
        run = self.executor()
        self.work.signed = False
        self.assertFalse(run.signed_in(example("RSVP")))
        self.assertTrue(run.signed_in(example("Cancel")))
        self.assertFalse(run.signed_in(example("RSVP", acct="school")))

    def test_a_broken_check_counts_as_not_ready(self) -> None:
        self.work.is_configured = mock.Mock(side_effect=OSError("disk"))   # type: ignore[method-assign]
        with self.assertLogs(EXECUTOR_LOGGER, level="DEBUG"):
            self.assertEqual(self.executor().readiness(example("RSVP")), CALENDAR_SETUP_NOTE)


# --------------------------------------------------------------------------
# Carrying out each kind
# --------------------------------------------------------------------------

class CalendarAndTodoTests(ExecutorTestCase):
    """A Calendar event and a Todo's block give the same results as before the executor."""

    def test_calendar_event(self) -> None:
        action = parse_action_line(CHESS)
        result = self.executor().execute(action, on_stage=self.stages.append)
        self.assertEqual(result, ExecResult(STATUS_CREATED, EVENT_LINK))
        self.assertEqual(self.personal.changes(), [("create_event", (action,), {})])
        self.assertEqual(self.stages, [STAGE_WORKING])
        self.personal.outcomes["create_event"] = EventResult("old1", EVENT_LINK, existed=True)
        self.assertEqual(self.executor().execute(action).status, STATUS_EXISTS)
        self.assertEqual(self.work.calls, [])

    def test_a_plan_block_carries_its_own_footer(self) -> None:
        """G24: the day plan's footer goes with the insert (and with the preview); every other card
        keeps the call exactly as before (no footer keyword)."""
        block = dataclasses.replace(parse_action_line(CHESS + " | | | For: Lab report"), mode="plan")
        self.executor().execute(block)
        self.assertEqual(self.personal.changes(), [("create_event", (block,), {"footer": gcal.PLAN_EVENT_FOOTER})])
        preview = self.executor().preview(block)
        self.assertEqual(preview.body, "For: Lab report" + "\n\n" + "Added by Jarvis from your day plan.")
        self.assertEqual(self.executor().preview(parse_action_line(CHESS)).body, gcal.EVENT_FOOTER)

    def test_todo_block_goes_to_the_personal_calendar_with_the_todo_id(self) -> None:
        todo = example("Todo")
        self.executor().execute(todo)
        ((_, (event,), _),) = self.personal.changes()
        self.assertEqual((event.kind, event.id, event.start, event.end),
                         (CALENDAR, todo.id, datetime(2026, 10, 6, 19, 0), datetime(2026, 10, 6, 21, 0)))
        self.assertEqual(self.work.changes(), [])

    def test_signs_in_first_when_needed(self) -> None:
        self.personal.signed = False
        self.executor().execute(parse_action_line(CHESS), on_stage=self.stages.append)
        self.assertEqual(self.stages, [STAGE_SIGNIN, STAGE_SIGNED_IN, STAGE_WORKING])
        self.assertLess(self.personal.names().index("sign_in"), self.personal.names().index("create_event"))

    def test_failures(self) -> None:
        self.personal.outcomes["create_event"] = CalendarError("Google Calendar error (500)", status=500)
        with self.assertRaises(ExecError) as ctx:
            self.executor().execute(parse_action_line(CHESS))
        self.assertEqual((str(ctx.exception), ctx.exception.status, ctx.exception.outcome),
                         ("Google Calendar error (500)", 500, OUTCOME_FAILED))
        self.personal.signed = False
        self.personal.sign_in_error = CalendarAuthError("Google sign-in was cancelled", problem=PROBLEM_DENIED)
        with self.assertRaises(ExecError) as ctx:
            self.executor().execute(parse_action_line(CHESS))
        self.assertEqual(ctx.exception.problem, PROBLEM_DENIED)
        self.assertEqual(len(self.personal.changes()), 1)


class CountdownKindTests(ExecutorTestCase):
    def test_rsvp_answers_with_the_note_and_explicit_notify(self) -> None:
        for answer, text in (("yes", "Accepted"), ("no", "Declined"), ("maybe", "Answered maybe")):
            for notify, send_updates in (("all", "all"), ("external", "externalOnly"), ("none", "none")):
                with self.subTest(answer=answer, notify=notify):
                    self.work.calls.clear()
                    action = example("RSVP", answer=answer, notify=notify, body="Running late", cal="team-cal")
                    result = self.executor().execute(action, on_stage=self.stages.append)
                    self.assertEqual(result, ExecResult(STATUS_SENT, EVENT_LINK, text))
                    self.assertEqual(self.work.changes(), [("respond", ("abc123def456ghi789", answer), {
                        "comment": "Running late", "send_updates": send_updates, "calendar_id": "team-cal",
                        "interactive": True})])
        self.assertEqual(self.personal.changes(), [])

    def test_move_and_cancel(self) -> None:
        move = example("Move", notify="none")
        self.assertEqual(self.executor().execute(move), ExecResult(STATUS_SENT, EVENT_LINK, "Moved"))
        self.assertEqual(self.work.changes(), [("move", ("abc123def456ghi789_20261008T190000Z",
                                                         datetime(2026, 10, 8, 14, 0), datetime(2026, 10, 8, 15, 0)),
                                                {"send_updates": "none", "calendar_id": "primary", "now": self.now,
                                                 "interactive": True})])
        cancel = example("Cancel")
        self.assertEqual(self.executor().execute(cancel), ExecResult(STATUS_SENT, "", "Cancelled"))
        self.assertEqual(self.personal.changes(), [("cancel", ("zyx987wvu654tsr321",), {
            "send_updates": "all", "calendar_id": "primary", "interactive": True})])

    def test_already_done(self) -> None:
        self.work.outcomes["respond"] = ChangeResult("abc123def456ghi789", EVENT_LINK, already=True)
        self.assertEqual(self.executor().execute(example("RSVP")).result_text, "Already accepted")
        self.personal.outcomes["cancel"] = ChangeResult("zyx987wvu654tsr321", "", already=True)
        self.assertEqual(self.executor().execute(example("Cancel")).result_text, "Already cancelled")
        self.work.outcomes["move"] = ChangeResult("x", EVENT_LINK, already=True)
        self.assertEqual(self.executor().execute(example("Move")).result_text, "Already at that time")

    def test_outcomes(self) -> None:
        cases = ((CalendarUnknownOutcome("Google Calendar answered 503", status=503), OUTCOME_UNKNOWN, ""),
                 (NotAllowed("You don't organize this event - decline it instead"), OUTCOME_FAILED, ""),
                 (EventGone("The event was cancelled"), OUTCOME_FAILED, ""),
                 (CalendarError("Google Calendar error (400)", status=400), OUTCOME_FAILED, ""),
                 (CalendarAuthError("Google sign-in for the work account expired", problem=PROBLEM_EXPIRED),
                  OUTCOME_FAILED, PROBLEM_EXPIRED))
        for error, outcome, problem in cases:
            with self.subTest(error=type(error).__name__):
                self.work.outcomes["respond"] = error
                with self.assertRaises(ExecError) as ctx:
                    self.executor().execute(example("RSVP"))
                self.assertEqual((str(ctx.exception), ctx.exception.outcome, ctx.exception.problem),
                                 (str(error), outcome, problem))
                self.assertEqual(ctx.exception.status, error.status)

    def test_without_interactive_a_missing_sign_in_sends_nothing(self) -> None:
        self.work.signed = False
        with self.assertRaises(ExecError) as ctx:
            self.executor().execute(example("RSVP"), interactive=False)
        self.assertEqual((ctx.exception.problem, ctx.exception.outcome), (PROBLEM_SIGNED_OUT, OUTCOME_FAILED))
        self.assertIn("nothing was sent", str(ctx.exception))
        self.assertNotIn("sign_in", self.work.names())
        self.assertEqual(self.work.changes(), [])

    def test_peek_never_signs_in(self) -> None:
        self.assertEqual(self.executor().peek(example("RSVP", cal="team-cal")), details())
        self.assertEqual(self.work.calls[-1], ("get_event", ("abc123def456ghi789",),
                                               {"calendar_id": "team-cal", "interactive": False}))
        self.assertNotIn("sign_in", self.work.names())

    def test_the_log_names_ids_kinds_and_aliases_only(self) -> None:
        action = example("RSVP", body="Running late", title="Secret strategy offsite")
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO") as logs:
            self.executor().execute(action)
        text = "\n".join(logs.output)
        self.assertIn(f"Carrying out action {action.id} (rsvp, work)", text)
        for private in ("Running late", "Secret strategy", "abc123def456ghi789"):
            self.assertNotIn(private, text)


# --------------------------------------------------------------------------
# Edits
# --------------------------------------------------------------------------

class ApplyEditTests(ExecutorTestCase):
    def test_rsvp_edit_keeps_the_id_and_changes_what_is_shown_and_done(self) -> None:
        original = example("RSVP")
        edited = apply_edit(original, ActionEdit(answer="Decline", notify="none", body="Can't make it\r\nsorry"))
        self.assertEqual(edited.id, original.id)
        self.assertEqual((edited.field("answer"), edited.field("notify"), edited.body),
                         ("no", "none", "Can't make it\nsorry"))
        self.assertEqual(edited.approve_label(), "Decline")
        self.assertTrue(edited.describe(TODAY).startswith("Answer: no"))
        self.assertEqual([name for name, _ in edited.fields], [name for name, _ in original.fields])
        self.executor().execute(edited)
        ((_, args, kwargs),) = self.work.changes()
        self.assertEqual((args[1], kwargs["comment"], kwargs["send_updates"]), ("no", "Can't make it\nsorry", "none"))

    def test_move_edit(self) -> None:
        original = example("Move")
        start, end = actions.parse_time_range("2026-10-09", "9:30 AM", "10:15")
        edited = apply_edit(original, ActionEdit(start=start, end=end, notify="external"))
        self.assertEqual(edited.id, original.id)
        self.assertEqual((edited.start, edited.end), (datetime(2026, 10, 9, 9, 30), datetime(2026, 10, 9, 10, 15)))
        self.assertEqual(edited.field("when"), "2026-10-09T09:30/2026-10-09T10:15")
        self.assertTrue(edited.describe(TODAY).startswith(f"New time: Fri Oct 9{DOT}9:30-10:15 AM"))
        self.assertEqual(edited.body, original.body)
        self.executor().execute(edited)
        ((_, args, kwargs),) = self.work.changes()
        self.assertEqual((args[1], args[2], kwargs["send_updates"]),
                         (datetime(2026, 10, 9, 9, 30), datetime(2026, 10, 9, 10, 15), "externalOnly"))

    def test_aware_times_are_read_as_wall_times(self) -> None:
        edited = apply_edit(example("Move"), ActionEdit(start=datetime(2026, 10, 9, 9, 0, 59, tzinfo=EDT),
                                                        end=datetime(2026, 10, 9, 10, 0, tzinfo=EDT)))
        self.assertEqual((edited.start, edited.end), (datetime(2026, 10, 9, 9, 0), datetime(2026, 10, 9, 10, 0)))

    def test_cancel_edit_and_an_empty_edit(self) -> None:
        cancel = example("Cancel")
        self.assertEqual(apply_edit(cancel, ActionEdit(notify="externalOnly")).field("notify"), "external")
        self.assertEqual(apply_edit(cancel, ActionEdit()), cancel)
        self.assertEqual(apply_edit(cancel, ActionEdit(body="  ")).body, "")

    def test_bad_edits(self) -> None:
        start = datetime(2026, 10, 9, 9, 0)
        cases = (
            (example("RSVP"), ActionEdit(answer="perhaps"), "answer=: use yes, no or maybe"),
            (example("RSVP"), ActionEdit(answer=3), "the value must be text"),   # type: ignore[arg-type]
            (example("Cancel"), ActionEdit(answer="yes"), "only an invitation has an answer"),
            (example("RSVP"), ActionEdit(notify="everyone"), "notify=: use all, external or none"),
            (example("Move"), ActionEdit(start=start), "only a move has a new time (start and end)"),
            (example("RSVP"), ActionEdit(start=start, end=start + timedelta(hours=1)),
             "only a move has a new time (start and end)"),
            (example("Move"), ActionEdit(start=start, end=start + timedelta(minutes=4)), "5 minutes to 12 hours"),
            (example("Move"), ActionEdit(start=start, end=start - timedelta(hours=1)), "5 minutes to 12 hours"),
            (example("Move"), ActionEdit(start=start, end=start + timedelta(hours=13)), "5 minutes to 12 hours"),
            (example("Cancel"), ActionEdit(body="x" * 1001), "the note is too long (1001 characters, at most 1000)"),
            (example("RSVP"), ActionEdit(to=("ana@example.edu",)), "can only be edited on a reply or an email"),
            (example("RSVP"), ActionEdit(subject="Hi"), "can only be edited on a reply or an email"),
            (example("RSVP"), ActionEdit(confirmed_new=frozenset({"ana@example.edu"})), "only be edited on a reply"),
            (example("Reply"), ActionEdit(notify="all"), "no answer, time or guest notice"),
            (example("Reply"), ActionEdit(subject="Something else"), "keeps the subject of its thread"),
            (example("Reply"), ActionEdit(to=("bob@",)), 'To: "bob@" is not an email address'),
            (example("Reply", replied="yes"), ActionEdit(body="x"), "This card cannot be edited"),
            (parse_action_line(CHESS), ActionEdit(notify="all"), "This card cannot be edited"),
        )
        for action, edit, words in cases:
            with self.subTest(words=words, kind=action.kind), self.assertRaises(EditError) as ctx:
                apply_edit(action, edit)
            self.assertIn(words, str(ctx.exception))

    def test_note_cleanup(self) -> None:
        edited = apply_edit(example("Cancel"), ActionEdit(body="\tline one\x00\x07\rline two\u2028 "))
        self.assertEqual(edited.body, "line one\nline two")
        self.assertEqual(len(apply_edit(example("Cancel"), ActionEdit(body="y" * 1000)).body), 1000)


class ParseTimeRangeTests(unittest.TestCase):
    def test_reads_like_a_when_value(self) -> None:
        parse = actions.parse_time_range
        self.assertEqual(parse("2026-10-08", "14:00", "15:00"),
                         (datetime(2026, 10, 8, 14, 0), datetime(2026, 10, 8, 15, 0)))
        self.assertEqual(parse(" 2026-10-08 ", "2 pm", "3:30 PM"),
                         (datetime(2026, 10, 8, 14, 0), datetime(2026, 10, 8, 15, 30)))
        self.assertEqual(parse("2026-10-08", "23:00", "01:00"),
                         (datetime(2026, 10, 8, 23, 0), datetime(2026, 10, 9, 1, 0)))

    def test_errors(self) -> None:
        cases = ((("", "14:00", "15:00"), "give the new date"), (("2026-10-08", " ", "15:00"), "give the new start"),
                 (("Oct 8", "14:00", "15:00"), "YYYY-MM-DD"), (("2026-02-30", "14:00", "15:00"), "not a real date"),
                 (("2026-10-08", "14:00", "14:03"), "5 minutes to 12 hours"),
                 (("2026-10-08", "14:00-15:00", "16:00"), "as times"),
                 (("2026-10-08", "14:00 | x", "15:00"), "can't be read"),
                 (("2026-10-08", "1" * 41, "15:00"), "can't be read"),
                 (("2026-10-08", None, "15:00"), "give the new start"),
                 (("2026-10-08", "25:00", "26:00"), "25:00"))
        for args, words in cases:
            with self.subTest(args=args), self.assertRaises(actions.EditInvalid) as ctx:
                actions.parse_time_range(*args)   # type: ignore[arg-type]
            self.assertIn(words, str(ctx.exception))
            self.assertNotIn("when=", str(ctx.exception))


# --------------------------------------------------------------------------
# One job of the action worker
# --------------------------------------------------------------------------

class RunActionTests(ExecutorTestCase):
    def run_job(self, action: ProposedAction, **kwargs: Any) -> RunOutcome:
        kwargs.setdefault("store", self.store)
        kwargs.setdefault("writes_running", True)
        return run_action(self.executor(), self.watch(action), on_stage=self.stages.append, **kwargs)

    def saved(self, action: ProposedAction) -> dict[str, str] | None:
        return ActionStore(self.store.path).get(action.id)

    def test_running_is_on_disk_before_the_call_and_sent_after(self) -> None:
        action = example("RSVP")
        outcome = self.run_job(action)
        self.assertEqual(outcome, RunOutcome(STATUS_SENT, ExecResult(STATUS_SENT, EVENT_LINK, "Accepted"), None))
        self.assertEqual(self.work.on_disk_at_change, [STATUS_RUNNING])
        saved = self.saved(action)
        self.assertEqual({key: saved[key] for key in ("status", "link", "message", "kind", "account")},
                         {"status": STATUS_SENT, "link": EVENT_LINK, "message": "Accepted", "kind": "rsvp",
                          "account": "work"})
        self.assertTrue(self.store.is_decided(action.id))
        ((_, _, kwargs),) = self.work.changes()
        self.assertIs(kwargs["interactive"], False)   # no sign-in once "running" is saved
        self.assertEqual(self.stages, [STAGE_WORKING])

    def test_the_sign_in_comes_before_running(self) -> None:
        self.work.signed = False
        action = example("Move")
        self.run_job(action)
        sign_in = next(call for call in self.work.calls if call[0] == "sign_in")
        self.assertEqual(sign_in[1], None)   # nothing saved yet while the browser was open
        self.assertEqual(self.work.on_disk_at_change, [STATUS_RUNNING])
        self.assertEqual(self.stages, [STAGE_SIGNIN, STAGE_SIGNED_IN, STAGE_WORKING])

    def test_a_sign_in_that_ends_after_quit_starts_no_call(self) -> None:
        self.work.signed = False
        action = example("Move")
        outcome = self.run_job(action, proceed=lambda: False)   # the app was closing meanwhile
        self.assertEqual(outcome.status, STATUS_FAILED)
        self.assertEqual(str(outcome.error), executor.CLOSING_MESSAGE)
        self.assertEqual(self.work.changes(), [])
        self.assertTrue(any(call[0] == "sign_in" for call in self.work.calls))
        self.assertEqual(self.saved(action)["status"], STATUS_FAILED)   # never "running"
        self.assertNotIn(STATUS_SENT, [stage for stage in self.stages])
        self.assertEqual(self.run_job(example("RSVP"), proceed=lambda: True).status, STATUS_SENT)

    def test_an_unknown_outcome_is_saved_and_never_retried(self) -> None:
        action = example("Cancel")
        self.personal.outcomes["cancel"] = CalendarUnknownOutcome(
            "No answer from Google Calendar while cancelling the event (TimeoutError)")
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO") as logs:
            outcome = self.run_job(action)
        self.assertEqual(outcome.status, STATUS_UNKNOWN)
        self.assertEqual(outcome.error.outcome, OUTCOME_UNKNOWN)
        self.assertEqual(len(self.personal.changes()), 1)
        saved = self.saved(action)
        self.assertEqual((saved["status"], saved["kind"], saved["account"]), (STATUS_UNKNOWN, "cancel", "personal"))
        self.assertEqual(saved["message"], "No answer from Google Calendar while cancelling the event (TimeoutError)")
        self.assertFalse(self.store.is_decided(action.id))   # the card keeps Deny / Retry
        self.assertIn(f"Action {action.id} (cancel, personal) not carried out: unknown", "\n".join(logs.output))

    def test_a_server_error_logs_the_status_code_only(self) -> None:
        action = example("RSVP")
        self.work.outcomes["respond"] = CalendarUnknownOutcome("Google Calendar answered 503: Backend gone",
                                                               status=503)
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO") as logs:
            self.run_job(action)
        text = "\n".join(logs.output)
        self.assertIn("unknown (HTTP 503)", text)
        self.assertNotIn("Backend gone", text)

    def test_a_refusal_after_running_is_failed(self) -> None:
        for error in (NotAllowed("You don't organize this event"), CalendarError("Bad Request (400)", status=400),
                      EventGone("The event was cancelled")):
            with self.subTest(error=type(error).__name__):
                self.work.calls.clear()
                action = example("Move")
                self.work.outcomes["move"] = error
                outcome = self.run_job(action)
                self.assertEqual((outcome.status, outcome.error.outcome), (STATUS_FAILED, OUTCOME_FAILED))
                self.assertEqual((self.saved(action)["status"], self.saved(action)["message"]),
                                 (STATUS_FAILED, str(error)))
                self.assertEqual(len(self.work.changes()), 1)

    def test_a_bug_after_running_is_unknown_and_logged_by_type_only(self) -> None:
        action = example("RSVP")
        self.work.outcomes["respond"] = TypeError("ya29.fake-token-in-a-message")
        with self.assertLogs(EXECUTOR_LOGGER, level="DEBUG") as logs:
            outcome = self.run_job(action)
        self.assertEqual((outcome.status, str(outcome.error)), (STATUS_UNKNOWN, "unexpected error (TypeError)"))
        self.assertEqual(self.saved(action)["status"], STATUS_UNKNOWN)
        self.assertNotIn("ya29.fake-token-in-a-message", "\n".join(logs.output))

    def test_a_failed_sign_in_sends_nothing_and_saves_failed(self) -> None:
        self.work.signed = False
        self.work.sign_in_error = CalendarAuthError(
            "Google blocked the sign-in: the work account's administrator does not allow this app "
            "(admin_policy_enforced)", problem=PROBLEM_BLOCKED)
        action = example("RSVP")
        outcome = self.run_job(action)
        self.assertEqual((outcome.status, outcome.error.problem), (STATUS_FAILED, PROBLEM_BLOCKED))
        self.assertEqual(self.work.changes(), [])
        self.assertEqual(self.saved(action)["status"], STATUS_FAILED)

    def test_a_bug_before_running_is_failed(self) -> None:
        self.work.signed = False
        self.work.sign_in_error = RuntimeError("boom")
        with self.assertLogs(EXECUTOR_LOGGER, level="ERROR"):
            outcome = self.run_job(example("RSVP"))
        self.assertEqual((outcome.status, outcome.error.outcome), (STATUS_FAILED, OUTCOME_FAILED))
        self.assertEqual(self.work.changes(), [])

    def test_nothing_is_sent_when_running_cannot_be_saved(self) -> None:
        action = example("RSVP")
        with mock.patch.object(actions, "_write_entries", return_value=False), \
                self.assertLogs(EXECUTOR_LOGGER, level="INFO"):
            outcome = self.run_job(action)
        self.assertEqual((outcome.status, str(outcome.error)), (STATUS_FAILED, NOT_SAVED_MESSAGE))
        self.assertEqual(self.work.changes(), [])
        self.assertEqual(self.store.get(action.id)["status"], STATUS_FAILED)
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO"):
            outcome = run_action(self.executor(), action, writes_running=True)   # no store at all
        self.assertEqual((outcome.status, str(outcome.error)), (STATUS_FAILED, NOT_SAVED_MESSAGE))
        self.assertEqual(self.work.changes(), [])

    def test_not_ready_saves_failed_without_any_call(self) -> None:
        action = example("RSVP", acct="school")
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO"):
            outcome = self.run_job(action)
        self.assertEqual((outcome.status, outcome.error.problem), (STATUS_FAILED, PROBLEM_SETUP))
        self.assertEqual(self.work.calls + self.personal.calls, [])

    def test_a_crash_mid_call_leaves_running_which_the_next_start_reads_as_unknown(self) -> None:
        class Crash(BaseException):
            pass

        action = example("RSVP")
        self.work.outcomes["respond"] = Crash()
        with self.assertRaises(Crash):
            self.run_job(action)
        on_disk = json.loads(self.store.path.read_text(encoding="utf-8"))[action.id]
        self.assertEqual(on_disk["status"], STATUS_RUNNING)   # what a crashed app leaves
        with self.assertLogs("briefing_reader.actions", level="WARNING"):
            restarted = ActionStore(self.store.path)
        entry = restarted.get(action.id)
        self.assertEqual((entry["status"], entry["message"], entry["kind"], entry["account"]),
                         (STATUS_UNKNOWN, INTERRUPTED_MESSAGE, "rsvp", "work"))
        self.assertFalse(restarted.is_decided(action.id))

    def test_calendar_and_todo_jobs_save_nothing_themselves(self) -> None:
        for action in (parse_action_line(CHESS), example("Todo")):
            with self.subTest(kind=action.kind):
                outcome = run_action(self.executor(), action, store=self.store)
                self.assertEqual(outcome.status, STATUS_CREATED)
                self.assertIsNone(self.store.get(action.id))
        self.personal.outcomes["create_event"] = CalendarError("Google Calendar error (500)", status=500)
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO"):
            outcome = run_action(self.executor(), parse_action_line(CHESS), store=self.store)
        self.assertEqual(outcome.status, STATUS_FAILED)
        self.assertFalse(self.store.path.exists())

    def test_every_failure_makes_exactly_one_call(self) -> None:
        errors = (CalendarUnknownOutcome("x"), NotAllowed("x"), CalendarError("x", status=500), TypeError("x"))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.work.calls.clear()
                self.work.outcomes["respond"] = error
                with self.assertLogs(EXECUTOR_LOGGER, level="INFO"):
                    self.run_job(example("RSVP"))
                self.assertEqual(len(self.work.changes()), 1)


# --------------------------------------------------------------------------
# Accounts and calendars from config.toml
# --------------------------------------------------------------------------

class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.environ = {"NOTION_TOKEN": "ntn_FakeTokenForExecutorTests0123456789abcdef",
                        "LOCALAPPDATA": str(self.root / "localappdata")}

    def config(self, text: str) -> Any:
        (self.root / "config.toml").write_text(text, encoding="utf-8")
        return load_config(self.root, environ=self.environ)

    def test_one_account_per_google_alias_and_the_legacy_token_moves_to_personal(self) -> None:
        cfg = self.config('[calendar]\ncalendar_id = "team@group.calendar.google.com"\n'
                          '[accounts.personal]\nfeatures = []\n[accounts.work]\n'
                          '[accounts.lab]\nbackend = "composio"\n')
        cfg.data_dir.mkdir(parents=True)
        legacy = cfg.data_dir / "google_token.json"
        legacy.write_text('{"token": "fake"}', encoding="utf-8")
        accounts = build_accounts(cfg)
        self.assertEqual(list(accounts), ["personal", "work"])
        self.assertFalse(legacy.exists())
        self.assertTrue((cfg.data_dir / "google_token_personal.json").exists())
        self.assertEqual(accounts["personal"].token_path, cfg.data_dir / "google_token_personal.json")
        self.assertEqual(accounts["work"].token_path, cfg.data_dir / "google_token_work.json")
        self.assertEqual(accounts["personal"].features, ("calendar",))   # Calendar proposals and the agenda
        self.assertEqual(accounts["personal"].client_secret_path, cfg.calendar.client_secret_path)
        calendars = build_calendars(cfg, accounts)
        self.assertEqual(list(calendars), ["personal", "work"])
        self.assertIsInstance(calendars["work"], GoogleCalendar)
        self.assertEqual((calendars["personal"].calendar_id, calendars["work"].calendar_id),
                         ("team@group.calendar.google.com", "primary"))
        self.assertIs(calendars["work"].account, accounts["work"])
        self.assertEqual(calendars["work"].alias, "work")

    def test_an_account_without_the_calendar_feature_has_no_calendar(self) -> None:
        cfg = self.config("[accounts.work]\nfeatures = []\n")
        self.assertEqual(list(build_accounts(cfg)), ["personal"])
        self.assertEqual(list(build_calendars(cfg)), ["personal"])

    def test_defaults_without_accounts(self) -> None:
        cfg = self.config("")
        calendars = build_calendars(cfg)
        self.assertEqual(list(calendars), ["personal"])
        self.assertFalse(calendars["personal"].is_signed_in())
        self.assertFalse(cfg.data_dir.exists())   # building opens nothing and writes nothing


# --------------------------------------------------------------------------
# What the cards say
# --------------------------------------------------------------------------

class CheckEventTests(unittest.TestCase):
    def test_rsvp(self) -> None:
        check = check_event(example("RSVP"), details(organizer_self=False, organizer="Ana Example", **SPEAKER),
                            TODAY)
        line = (f"Google: Speaker Series: Dr. Example{DOT}Tue Oct 6{DOT}5:00-6:00 PM{DOT}organised by Ana Example"
                f"{DOT}you haven't answered{DOT}as you@example.edu")
        self.assertEqual(check, EventCheck(   # Google's own title and time first: is it the right event?
            line, allowed=True, title="Speaker Series: Dr. Example", identity="you@example.edu", link=EVENT_LINK,
            tooltip=line))
        accepted = check_event(example("RSVP"), details(me="accepted", **SPEAKER), TODAY)
        self.assertIn(f"{DOT}you organise{DOT}you accepted{DOT}", accepted.text)
        refused = check_event(example("RSVP"), details(organizer_self=False, me="", **SPEAKER), TODAY)
        self.assertFalse(refused.allowed)
        self.assertIn("not on this event's guest list", refused.reason)
        self.assertIn("organised by someone else", refused.text)
        self.assertEqual(refused.identity, "")   # no attendee entry, not the organizer

    def test_move(self) -> None:
        line = f"Google: Project sync{DOT}Thu Oct 8{DOT}12:00-1:00 PM{DOT}you organise{DOT}as you@example.edu"
        self.assertEqual(check_event(example("Move"), details(), TODAY),
                         EventCheck(line, allowed=True, title="Project sync", identity="you@example.edu",
                                    link=EVENT_LINK, tooltip=line))
        guest = check_event(example("Move"), details(organizer_self=False, guests_can_modify=True), TODAY)
        self.assertTrue(guest.allowed)
        self.assertIn(f"{DOT}organised by someone else{DOT}guests may change it{DOT}", guest.text)
        self.assertIn(f"12:00-1:00 PM{DOT}one occurrence{DOT}",
                      check_event(example("Move"), details(instance=True), TODAY).text)
        for item, words in ((details(organizer_self=False), "don't organise"),
                            (details(series=True), "whole repeating series"),
                            (details(all_day=True), "All-day")):
            with self.subTest(words=words):
                check = check_event(example("Move"), item, TODAY)
                self.assertFalse(check.allowed)
                self.assertIn(words, check.reason)

    def test_cancel(self) -> None:
        study = details(title="Study group", start=datetime(2026, 10, 9, 18, 0, tzinfo=EDT))
        self.assertTrue(check_event(example("Cancel"), study, TODAY).allowed)
        self.assertEqual(check_event(example("Cancel"), study, TODAY).mismatch, "")
        for item, words in ((details(organizer_self=False, guests_can_modify=True), "decline instead"),
                            (details(series=True), "whole repeating series")):
            with self.subTest(words=words):
                check = check_event(example("Cancel"), item, TODAY)
                self.assertFalse(check.allowed)
                self.assertIn(words, check.reason)

    def test_googles_times_are_kept_for_the_live_picture(self) -> None:
        check = check_event(example("Move"), details(), TODAY)
        self.assertEqual((check.start, check.end), (datetime(2026, 10, 8, 12, 0, tzinfo=EDT),
                                                    datetime(2026, 10, 8, 13, 0, tzinfo=EDT)))
        self.assertEqual((check.all_day_start, check.all_day_end), (None, None))
        all_day = check_event(example("Cancel"), details(all_day=True), TODAY)
        self.assertEqual((all_day.start, all_day.all_day_start, all_day.all_day_end),
                         (None, date(2026, 10, 8), date(2026, 10, 8)))
        failed = check_failure(example("Move"), EventGone("gone", status=404))
        self.assertEqual((failed.start, failed.end, failed.all_day_start), (None, None, None))
        self.assertNotIn("2026-10-08", repr(failed))

    def test_long_title_is_shortened_on_the_line_only(self) -> None:
        long_title = "Quarterly planning review with the whole department and guests from the lab"
        check = check_event(example("Move", title=long_title), details(title=long_title), TODAY)
        self.assertTrue(check.text.startswith(f"Google: Quarterly planning review with the\u2026{DOT}Thu Oct 8"),
                        check.text)
        self.assertIn(long_title, check.tooltip)
        self.assertEqual((check.title, check.mismatch), (long_title, ""))

    def test_mismatch_with_the_line(self) -> None:
        move = example("Move")   # title=Project sync, at=2026-10-08 12:00-13:00
        same = (details(), details(title="project-sync (weekly)"), details(title="Sync"),
                details(start=datetime(2026, 10, 8, 12, 0).astimezone()))   # the same instant on this PC
        for item in same:
            with self.subTest(title=item.title, start=item.start):
                self.assertEqual(check_event(move, item, TODAY).mismatch, "")
        title = check_event(move, details(title="Dentist"), TODAY)
        self.assertEqual(title.mismatch, executor.MISMATCH_TITLE)
        self.assertTrue(title.allowed)   # a warning on the card, not a refusal
        self.assertTrue(title.text.startswith(f"Google (other title): Dentist{DOT}Thu Oct 8"), title.text)
        self.assertIn(f"The briefing says: Project sync{DOT}Thu Oct 8{DOT}12:00-1:00 PM", title.tooltip)
        moved = check_event(move, details(start=datetime(2026, 10, 8, 12, 30, tzinfo=EDT)), TODAY)   # :30 anywhere
        self.assertEqual(moved.mismatch, executor.MISMATCH_TIME)
        self.assertTrue(moved.text.startswith("Google (other time): Project sync"), moved.text)
        both = check_event(example("RSVP"), details(), TODAY)   # the RSVP line is the Speaker series
        self.assertEqual(both.mismatch, executor.MISMATCH_BOTH)
        self.assertTrue(both.text.startswith("Google (other title and time): "), both.text)
        self.assertEqual(check_event(move, details(all_day=True), TODAY).mismatch, executor.MISMATCH_TIME)
        self.assertEqual(check_event(move, details(title=gcal.NO_TITLE), TODAY).mismatch, "")
        bare = example("Cancel", title="", at="")
        self.assertEqual(check_event(bare, details(title="Anything", start=datetime(2027, 1, 1, tzinfo=EDT)),
                                     TODAY).mismatch, "")
        day = example("Cancel", at="2026-10-08")
        self.assertEqual(check_event(day, details(title="Study group"), TODAY).mismatch, "")
        self.assertEqual(check_event(day, details(title="Study group", all_day=True), TODAY).mismatch, "")
        self.assertEqual(check_event(day, details(title="Study group",
                                                  start=datetime(2026, 10, 9, 9, 0, tzinfo=EDT)), TODAY).mismatch,
                         executor.MISMATCH_TIME)

    def test_failures(self) -> None:
        rsvp = example("RSVP")
        self.assertEqual(check_failure(rsvp, CalendarNotSignedIn("Not signed in", problem=PROBLEM_SIGNED_OUT)),
                         EventCheck("Not signed in to the work account - Sign in to check this event", allowed=False))
        gone = check_failure(rsvp, EventGone("Google Calendar has no such event (404)", status=404))
        self.assertEqual((gone.allowed, gone.text), (False, "Google: event not found (deleted, cancelled or a wrong id)"))
        expired = check_failure(rsvp, CalendarAuthError("expired", problem=PROBLEM_EXPIRED))
        self.assertEqual(expired.text, sign_in_note("work", problem=PROBLEM_EXPIRED))
        refused = check_failure(rsvp, NotAllowed("Google Calendar refused to change this event (403: Forbidden)"))
        self.assertEqual((refused.allowed, refused.reason),
                         (False, "Google Calendar refused to change this event (403: Forbidden)"))
        setup = check_failure(rsvp, ExecError(CALENDAR_SETUP_NOTE, problem=PROBLEM_SETUP))
        self.assertEqual(setup.text, CALENDAR_SETUP_NOTE)
        other = check_failure(rsvp, CalendarError("Could not reach Google Calendar (TimeoutError)"))
        self.assertEqual(other.text, "Couldn't check the event with Google: Could not reach Google Calendar "
                                     "(TimeoutError)")
        self.assertFalse(other.allowed)


class SignInNoteTests(unittest.TestCase):
    def test_per_problem(self) -> None:
        self.assertEqual(sign_in_note("work", signed_in=True, problem=PROBLEM_EXPIRED), "")
        self.assertEqual(sign_in_note("work"), "Not signed in to the work account - click Sign in")
        self.assertEqual(sign_in_note("work", problem=PROBLEM_EXPIRED),
                         "The work account's Google sign-in expired or was revoked - click Sign in to sign in again")
        blocked = "Google blocked the sign-in: the work account's administrator does not allow this app (x)"
        self.assertEqual(sign_in_note("work", problem=PROBLEM_BLOCKED, message=blocked), blocked)
        # Every sign-in of an account with gmail_send asks to send email too: a block says how to keep
        # Calendar actions without it.
        self.assertEqual(sign_in_note("work", problem=PROBLEM_BLOCKED, message=blocked, sends_mail=True),
                         blocked + ". Every work sign-in also asks to send email; if the administrator only blocks "
                         'that, remove "gmail_send" from config.toml [accounts.work] features and sign in again to '
                         "keep Calendar actions")
        self.assertNotIn("gmail_send", sign_in_note("work", problem=PROBLEM_EXPIRED, sends_mail=True))
        # With gmail_read (Ask Jarvis reading mail, a restricted permission administrators block first)
        # the block says to remove that first, then gmail_send.
        self.assertEqual(sign_in_note("work", problem=PROBLEM_BLOCKED, message=blocked, sends_mail=True,
                                      reads_mail=True),
                         blocked + ". Every work sign-in also asks to read email (Ask Jarvis), which administrators "
                         'often block: remove "gmail_read" from config.toml [accounts.work] features and sign in '
                         'again; if it is still blocked, remove "gmail_send" from config.toml [accounts.work] '
                         "features and sign in again, to keep Calendar actions")
        self.assertNotIn("gmail_send", sign_in_note("work", problem=PROBLEM_BLOCKED, message=blocked, reads_mail=True))
        self.assertNotIn("gmail_read", sign_in_note("work", problem=PROBLEM_EXPIRED, reads_mail=True))
        self.assertIn("administrator", sign_in_note("work", problem=PROBLEM_BLOCKED))
        self.assertIn("cancelled or access was denied", sign_in_note("work", problem=PROBLEM_DENIED))
        self.assertIn("tick every box", sign_in_note("work", problem=PROBLEM_SCOPE))
        self.assertIn("not finished in time", sign_in_note("work", problem=PROBLEM_TIMEOUT))
        self.assertEqual(sign_in_note("work", problem=PROBLEM_SETUP), CALENDAR_SETUP_NOTE)
        self.assertEqual(sign_in_note("work", problem=PROBLEM_FAILED, message="Google sign-in failed (x)"),
                         "Google sign-in failed (x) - click Sign in to try again")
        self.assertEqual(sign_in_note("work", problem=PROBLEM_IDENTITY),
                         "This is not the Google account set up as the work account - click Sign in with that one")
        self.assertEqual(sign_in_note("work", problem=PROBLEM_IDENTITY, message="That Google account is taken"),
                         "That Google account is taken - click Sign in")




# --------------------------------------------------------------------------
# Reply and Email: GmailBackend, MailStatus, run_action
# --------------------------------------------------------------------------

ME = "you@example.edu"
EMAIL_PAIRS = (("acct", "personal"), ("to", "office@example.edu"), ("cc", ""),
               ("subject", "Question about the lab schedule"), ("due", ""), ("link", ""),
               ("body", "Hello,\\nIs the lab open on Saturday?\\nThanks"))
MAIL_ACCOUNTS = {"personal": AccountConfig("personal", features=("calendar", "gmail_send")),
                 "work": AccountConfig("work", features=("calendar", "gmail_send"))}


def email_line(**changes: str) -> ProposedAction:
    items = [f"{key}={changes.get(key, value)}" for key, value in EMAIL_PAIRS]
    action = parse_action_line("Email: " + " | ".join(items))
    assert not action.error, action.error
    return action


class FakeSender:
    """gmail.GmailSender's surface as GmailBackend uses it; records calls and, with ``store``,
    what actions.json said (on disk) when the message went out."""

    def __init__(self, *, address: str = ME, problem: str = "", message: str = "", configured: bool = True,
                 store: ActionStore | None = None) -> None:
        self.address = address
        self.problem = problem
        self.message = message
        self.configured = configured
        self.store = store
        self.watch = ""
        self.calls: list[tuple[Any, ...]] = []
        self.sent: list[OutgoingMail] = []
        self.outcome: Any = None
        self.sign_in_error: BaseException | None = None

    def is_configured(self) -> bool:
        return self.configured

    def is_signed_in(self) -> bool:
        return not self.problem

    def from_address(self) -> str:
        return self.address

    def ready(self) -> tuple[str, str]:
        return self.problem, self.message

    def sign_in(self) -> None:
        self.calls.append(("sign_in",))
        if self.sign_in_error is not None:
            raise self.sign_in_error
        self.problem = self.message = ""

    def send(self, mail: OutgoingMail) -> SentMail:
        on_disk = None
        if self.store is not None and self.store.path.exists():
            entry = json.loads(self.store.path.read_text(encoding="utf-8")).get(self.watch)
            on_disk = entry["status"] if entry else None
        self.calls.append(("send", on_disk))
        self.sent.append(mail)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome or SentMail("18c0ffee000000aa", "18c0ffee00000001",
                                        "https://mail.google.com/mail/#all/18c0ffee00000001")


class MailTestCase(ExecutorTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.history = RecipientHistory(self.root / "recipients.json")
        self.senders = {"work": FakeSender(store=self.store), "personal": FakeSender(address="me@example.com",
                                                                                      store=self.store)}

    def mail_executor(self, *, trusted: tuple[str, ...] = ("example.edu",),
                      accounts: dict[str, AccountConfig] | None = None) -> Executor:
        backends = [CalendarBackend({"personal": self.personal, "work": self.work}, now=lambda: self.now),
                    GmailBackend(self.senders, history=self.history, trusted_domains=trusted)]
        return Executor(backends, MAIL_ACCOUNTS if accounts is None else accounts)

    def run_mail(self, action: ProposedAction, **kwargs: Any) -> RunOutcome:
        kwargs.setdefault("store", self.store)
        kwargs.setdefault("writes_running", True)
        for sender in self.senders.values():
            sender.watch = action.id
        return run_action(kwargs.pop("executor", None) or self.mail_executor(), action,
                          on_stage=self.stages.append, **kwargs)


class MailSelectionTests(MailTestCase):
    def test_backend_and_readiness(self) -> None:
        run = self.mail_executor()
        self.assertIsInstance(run.backend_for(example("Reply")), GmailBackend)
        self.assertIsInstance(run.backend_for(email_line()), GmailBackend)
        self.assertEqual(account_of(example("Reply")), "work")
        self.assertEqual(account_of(email_line()), "personal")
        self.assertEqual(run.readiness(example("Reply")), "")
        self.assertEqual(run.readiness(email_line(acct="school")), 'No account named "school" in config.toml [accounts]')
        self.senders["work"].configured = False
        self.assertEqual(run.readiness(example("Reply")), MAIL_SETUP_NOTE)
        del self.senders["work"]
        self.assertEqual(self.mail_executor().readiness(example("Reply")), MAIL_SETUP_NOTE)
        tricky = email_line(subject="=?utf-8?b?SGVsbG8=?=")
        self.assertEqual(run.readiness(tricky), actions.ENCODED_WORD_PROBLEM)
        lab = {"work": AccountConfig("work", backend="composio")}
        self.assertEqual(self.mail_executor(accounts=lab).readiness(example("Reply")), COMPOSIO_NOTE)
        calendar_only = {"work": AccountConfig("work")}
        self.assertEqual(self.mail_executor(accounts=calendar_only).readiness(example("Reply")),
                         "The work account is not set up for sending email (config.toml [accounts.work] features)")
        no_gmail = Executor([CalendarBackend({"work": self.work})], MAIL_ACCOUNTS)   # no GmailBackend at all
        self.assertIsNone(no_gmail.backend_for(example("Reply")))
        self.assertEqual(no_gmail.readiness(example("Reply")), "Jarvis can't carry this out in this version")
        self.assertTrue(no_gmail.mail_status(example("Reply")).hand_off)

    def test_peek_is_for_events_only(self) -> None:
        for action in (example("Reply"), email_line(), parse_action_line(CHESS)):
            with self.subTest(kind=action.kind), self.assertRaises(ExecError):
                self.mail_executor().peek(action)


class MailStatusTests(MailTestCase):
    def test_ready_with_from_and_recipients(self) -> None:
        status = self.mail_executor().mail_status(example("Reply"))
        self.assertEqual((status.alias, status.sender, status.from_text), ("work", ME, f"From: work ({ME})"))
        self.assertEqual((status.problem, status.note, status.sign_in, status.hand_off), ("", "", False, False))
        self.assertTrue(status.ready)
        self.assertFalse(status.needs_edit)
        self.assertEqual(status.review.kinds, (("ana@example.edu", TRUSTED), ("ben@example.edu", TRUSTED)))

    def test_new_recipients_need_a_tick_in_edit(self) -> None:
        action = email_line(to="office@example.edu, eve@example.com", cc="cy@example.org")
        status = self.mail_executor().mail_status(action)
        self.assertEqual(status.review.new, ("eve@example.com", "cy@example.org"))
        self.assertTrue(status.needs_edit)
        self.assertFalse(status.ready)
        self.assertEqual(status.note, executor.NEW_RECIPIENT_NOTE)
        edited = apply_edit(action, ActionEdit(confirmed_new=frozenset({"EVE@example.com", "cy@example.org"})))
        self.assertTrue(self.mail_executor().mail_status(edited).ready)
        nobody_trusted = self.mail_executor(trusted=()).mail_status(example("Reply"))
        self.assertEqual(nobody_trusted.review.new, ("ana@example.edu", "ben@example.edu"))
        with self.assertLogs("briefing_reader.recipients", level="INFO"):
            self.history.add(["ana@example.edu", "ben@example.edu"])
        known = self.mail_executor(trusted=()).mail_status(example("Reply"))
        self.assertEqual((known.review.kinds[0][1], known.ready), (KNOWN, True))

    def test_an_ask_recipient_you_did_not_type_needs_a_tick_even_when_trusted(self) -> None:
        line = example("Reply")
        ask = dataclasses.replace(line, source="ask", unverified=frozenset({"ana@example.edu"}))
        status = self.mail_executor().mail_status(ask)
        self.assertEqual(status.review.kinds, (("ana@example.edu", NEW), ("ben@example.edu", TRUSTED)))
        self.assertTrue(status.needs_edit)
        self.assertFalse(status.ready)
        ticked = apply_edit(ask, ActionEdit(confirmed_new=frozenset({"ana@example.edu"})))
        self.assertTrue(self.mail_executor().mail_status(ticked).ready)
        outcome = self.run_mail(ask)   # Send without the tick sends nothing
        self.assertEqual(self.senders["work"].sent, [])
        self.assertNotEqual(outcome.status, actions.STATUS_SENT)

    def test_the_own_address_refuses_the_card(self) -> None:
        """Any recipient that is the sending account itself (any spelling) refuses the card with a
        clear note; nothing is left out quietly and Send opens nothing."""
        for to, cc in ((f"ana@example.edu, {ME.upper()}", ""), (ME, "ana@example.edu"),
                       ("ana@example.edu", "you+lab@example.edu")):
            with self.subTest(to=to, cc=cc):
                status = self.mail_executor().mail_status(example("Reply", to=to, cc=cc))
                self.assertEqual((status.ready, status.refused, status.needs_edit, status.problem),
                                 (False, True, False, ""))
                self.assertEqual(status.note, f"This would send to the work account ({ME}) itself - edit the recipients")
                self.assertEqual(len(status.review.own), 1)
                self.assertIn(status.review.own[0], status.review.recipients)   # shown, not dropped

    def test_an_alias_bound_to_the_cards_only_recipient(self) -> None:
        """The live case: "personal" bound to the address the card writes to. Refused with the note;
        a run sends nothing and saves a message without the address."""
        self.senders["personal"].address = "office@example.edu"
        for to in ("office@example.edu", "Office@Example.edu", "office+jarvis@example.edu"):
            with self.subTest(to=to):
                action = email_line(to=to)
                status = self.mail_executor().mail_status(action)
                self.assertEqual((status.ready, status.refused), (False, True))
                self.assertEqual(status.note, "This would send to the personal account (office@example.edu) itself - "
                                              "edit the recipients")
                with self.assertLogs(EXECUTOR_LOGGER, level="INFO") as logs:
                    outcome = self.run_mail(action)
                self.assertEqual((outcome.status, str(outcome.error)),
                                 (STATUS_FAILED, recipients.OWN_RECIPIENT_MESSAGE.format(alias="personal")))
                self.assertEqual(self.senders["personal"].calls, [])
                saved = ActionStore(self.store.path).get(action.id)
                self.assertEqual(saved["status"], STATUS_FAILED)   # never "running"
                self.assertNotIn("@", saved["message"])
                self.assertNotIn("office", "\n".join(logs.output))

    def test_account_problems(self) -> None:
        cases = (
            (PROBLEM_SIGNED_OUT, "Not signed in to the work account", True, False,
             "Send signs in to Google for the work account first - then check From and click Send again"),
            (PROBLEM_SCOPE, "x", True, False,
             "Google did not allow sending email for work - click Send to sign in again and tick that box"),
            (PROBLEM_EXPIRED, "x", True, False,
             "The work account's Google sign-in expired or was revoked - click Send to sign in again"),
            (PROBLEM_IDENTITY, "Jarvis doesn't know yet", True, False,
             "Jarvis doesn't know yet which Google account work is - click Send to sign in and confirm it"),
            (PROBLEM_IDENTITY, "This is not the Google account set up as the work account; sign in with that one",
             True, False, "This is not the Google account set up as the work account; sign in with that one - "
                          "click Send to sign in with the right account"),
            (PROBLEM_DENIED, "x", True, False,
             "Google sign-in for the work account was cancelled or access was denied - click Send to try again"),
            (PROBLEM_BLOCKED, "x", False, True,
             "The work account's administrator does not allow this app to send email (admin_policy_enforced) - "
             'use Copy instead. To keep Calendar actions for work, remove "gmail_send" from config.toml '
             "[accounts.work] features and sign in again"),
            (PROBLEM_BLOCKED, "The work account's administrator does not allow this app to send email "
             "(domainPolicy); nothing was sent", False, True,
             "The work account's administrator does not allow this app to send email (domainPolicy) - "
             'use Copy instead. To keep Calendar actions for work, remove "gmail_send" from config.toml '
             "[accounts.work] features and sign in again"),
            (PROBLEM_SETUP, "Set up email sending: README step 8b (the Google packages are missing)", False, True,
             "Set up email sending: README step 8b (the Google packages are missing)"),
        )
        for problem, message, sign_in, hand_off, note in cases:
            with self.subTest(problem=problem, message=message[:20]):
                self.senders["work"].problem, self.senders["work"].message = problem, message
                status = self.mail_executor().mail_status(example("Reply"))
                self.assertEqual((status.problem, status.sign_in, status.hand_off, status.note),
                                 (problem, sign_in, hand_off, note))
                self.assertFalse(status.ready)
                self.assertNotIn("@", status.note)
        # A card with a link offers Open too.
        self.senders["work"].problem, self.senders["work"].message = PROBLEM_BLOCKED, ""
        linked = example("Reply", link="https://mail.google.com/mail/u/0/#inbox/18c0ffee00000001")
        self.assertIn("use Copy and Open instead", self.mail_executor().mail_status(linked).note)

    def test_not_set_up_is_a_hand_off_and_from_says_so(self) -> None:
        self.senders["work"].address = ""
        self.senders["work"].problem = PROBLEM_IDENTITY
        status = self.mail_executor().mail_status(example("Reply"))
        self.assertEqual(status.from_text, "From: work (account not confirmed yet)")
        calendar_only = self.mail_executor(accounts={"work": AccountConfig("work")}).mail_status(example("Reply"))
        self.assertTrue(calendar_only.hand_off)
        self.assertIn("not set up for sending email", calendar_only.note)
        other = self.mail_executor().mail_status(example("RSVP"))
        self.assertTrue(other.hand_off)
        self.assertIsNone(other.review)


class MailRunTests(MailTestCase):
    def test_running_is_on_disk_before_the_send_and_sent_after(self) -> None:
        action = example("Reply", cc="cy@example.edu")
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO") as logs, \
                self.assertLogs("briefing_reader.recipients", level="INFO"):
            outcome = self.run_mail(action)
        self.assertEqual(outcome.status, STATUS_SENT)
        self.assertEqual(outcome.result, ExecResult(STATUS_SENT, "https://mail.google.com/mail/#all/18c0ffee00000001",
                                                    "Sent"))
        self.assertEqual(self.senders["work"].calls, [("send", STATUS_RUNNING)])
        (mail,) = self.senders["work"].sent
        self.assertEqual(mail, OutgoingMail(account="work", from_addr=ME, to=("ana@example.edu", "ben@example.edu"),
                                            cc=("cy@example.edu",), subject="Re: Thursday noon meeting", body="Hi both",
                                            thread_id="18c0ffee00000001",
                                            in_reply_to="<CAExample0001@mail.example.com>"))
        saved = ActionStore(self.store.path).get(action.id)
        self.assertEqual({key: saved[key] for key in ("status", "message", "kind", "account")},
                         {"status": STATUS_SENT, "message": "Sent", "kind": "reply", "account": "work"})
        self.assertTrue(self.store.is_decided(action.id))
        self.assertEqual(self.stages, [STAGE_WORKING])
        self.assertTrue(self.history.knows("cy@example.edu"))   # needs no tick next time
        text = "\n".join(logs.output)
        self.assertIn(f"Sending action {action.id} (reply, work) to 3 recipient(s)", text)
        for private in ("ana@example.edu", "cy@example.edu", "Thursday", "Hi both", ME, "CAExample"):
            self.assertNotIn(private, text)

    def test_what_the_edit_dialog_shows_is_what_is_sent(self) -> None:
        action = email_line()
        mine = apply_edit(action, ActionEdit(cc=("me@example.com",)))   # the account itself in Cc: refused
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO"):
            self.assertEqual(self.run_mail(mine).status, STATUS_FAILED)
        self.assertEqual(self.senders["personal"].sent, [])
        edited = apply_edit(action, ActionEdit(to=("Office <office@example.edu>", "eve@example.com"),
                                               cc=(), subject="Lab hours", body="Is it open?\r\nThanks",
                                               confirmed_new=frozenset({"eve@example.com"})))
        self.assertEqual(edited.id, action.id)
        with self.assertLogs("briefing_reader.recipients", level="INFO"):
            outcome = self.run_mail(edited)
        self.assertEqual(outcome.status, STATUS_SENT)
        (mail,) = self.senders["personal"].sent
        self.assertEqual((mail.to, mail.cc, mail.subject, mail.body, mail.thread_id, mail.in_reply_to),
                         (("office@example.edu", "eve@example.com"), (), "Lab hours", "Is it open?\nThanks", "", ""))

    def test_execute_checks_the_message_itself_once_more(self) -> None:
        """Right before the send, the message's own recipients are checked against From and against
        the account as it is now (a review that let the account's address through sends nothing)."""
        action = example("Reply")
        own = recipients.OWN_RECIPIENT_MESSAGE.format(alias="work")
        bad = ((OutgoingMail(account="work", from_addr=ME, to=("ana@example.edu",), cc=("You+x@example.edu",),
                             subject="Re: x", body="Hi"), own, ""),
               (OutgoingMail(account="work", from_addr="other@example.edu", to=(ME,), cc=(), subject="Re: x",
                             body="Hi"), own, ""),
               (OutgoingMail(account="work", from_addr="", to=("ana@example.edu",), cc=(), subject="Re: x", body="Hi"),
                executor.mail_note("work", PROBLEM_IDENTITY), PROBLEM_IDENTITY))
        for mail, message, problem in bad:
            with self.subTest(mail=mail.cc or mail.to), \
                    mock.patch.object(GmailBackend, "_message", return_value=mail), \
                    self.assertLogs(EXECUTOR_LOGGER, level="INFO"):
                outcome = self.run_mail(action)
            self.assertEqual((outcome.status, str(outcome.error), outcome.error.problem),
                             (STATUS_FAILED, message, problem))
        self.assertEqual(self.senders["work"].calls, [])

    def test_a_binding_not_confirmed_yet_sends_nothing(self) -> None:
        self.senders["work"].problem = PROBLEM_CONFIRM
        self.senders["work"].message = "You haven't confirmed yet that this is the right Google account"
        status = self.mail_executor().mail_status(example("Reply"))
        self.assertEqual((status.problem, status.confirm, status.sign_in, status.hand_off, status.ready),
                         (PROBLEM_CONFIRM, True, False, False, False))
        self.assertEqual(status.note, "Is From the right Google account for work? Click Send to confirm it (or to "
                                      "use another account) - nothing is sent until you do")
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO"):
            outcome = self.run_mail(example("Reply"))
        self.assertEqual((outcome.status, outcome.error.problem), (STATUS_FAILED, PROBLEM_CONFIRM))
        self.assertEqual(self.senders["work"].calls, [])

    def test_unconfirmed_new_recipients_send_nothing(self) -> None:
        action = email_line(to="eve@example.com")
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO"):
            outcome = self.run_mail(action)
        self.assertEqual((outcome.status, str(outcome.error)), (STATUS_FAILED, NEW_RECIPIENTS_MESSAGE))
        self.assertEqual(self.senders["personal"].calls, [])
        self.assertEqual(self.store.get(action.id)["status"], STATUS_FAILED)   # never "running"
        self.assertFalse(self.history.knows("eve@example.com"))

    def test_mail_goes_out_only_through_the_running_path(self) -> None:
        action = example("Reply")
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO"):
            outcome = self.run_mail(action, writes_running=False)
        self.assertEqual((outcome.status, str(outcome.error)), (STATUS_FAILED, HOLD_ONLY_MESSAGE))
        for call in (lambda: self.mail_executor().execute(action),
                     lambda: self.mail_executor().execute(action, interactive=True)):
            with self.assertRaises(ExecError) as ctx:
                call()
            self.assertEqual(str(ctx.exception), HOLD_ONLY_MESSAGE)
        self.assertEqual(self.senders["work"].calls, [])

    def test_a_signed_out_account_sends_nothing_and_opens_nothing(self) -> None:
        for problem in (PROBLEM_SIGNED_OUT, PROBLEM_SCOPE, PROBLEM_IDENTITY, PROBLEM_BLOCKED):
            with self.subTest(problem=problem):
                self.senders["work"].problem = problem
                action = example("Reply")
                with self.assertLogs(EXECUTOR_LOGGER, level="INFO"):
                    outcome = self.run_mail(action)
                self.assertEqual((outcome.status, outcome.error.problem), (STATUS_FAILED, problem))
                self.assertEqual(self.senders["work"].calls, [])   # no send and no sign-in
                self.assertEqual(self.stages, [])

    def test_outcomes_after_running(self) -> None:
        cases = ((GmailUnknownOutcome("Gmail answered 503; check Sent mail", status=503), STATUS_UNKNOWN, OUTCOME_UNKNOWN),
                 (GmailError("Gmail refused the message (400); nothing was sent", status=400), STATUS_FAILED,
                  OUTCOME_FAILED),
                 (gmail.GmailAuthError("expired; nothing was sent", problem=PROBLEM_EXPIRED), STATUS_FAILED,
                  OUTCOME_FAILED),
                 (TypeError("ya29.fake-token-in-a-message"), STATUS_UNKNOWN, OUTCOME_UNKNOWN))
        for error, status, outcome_kind in cases:
            with self.subTest(error=type(error).__name__):
                self.senders["work"].calls.clear()
                self.senders["work"].outcome = error
                action = example("Reply")
                with self.assertLogs(EXECUTOR_LOGGER, level="INFO") as logs:
                    outcome = self.run_mail(action)
                self.assertEqual((outcome.status, outcome.error.outcome), (status, outcome_kind))
                self.assertEqual(self.senders["work"].calls, [("send", STATUS_RUNNING)])   # exactly one, no retry
                self.assertEqual(ActionStore(self.store.path).get(action.id)["status"], status)
                self.assertFalse(self.history.knows("ana@example.edu"))
                self.assertNotIn("ya29.fake-token-in-a-message", "\n".join(logs.output))

    def test_sign_in_is_its_own_click(self) -> None:
        self.senders["work"].problem = PROBLEM_SIGNED_OUT
        run = self.mail_executor()
        run.sign_in(example("Reply"), on_stage=self.stages.append)
        self.assertEqual((self.senders["work"].calls, self.stages), ([("sign_in",)], [STAGE_SIGNIN, STAGE_SIGNED_IN]))
        self.senders["work"].sign_in_error = gmail.GmailAuthError("blocked", problem=PROBLEM_BLOCKED)
        with self.assertRaises(ExecError) as ctx:
            run.sign_in(example("Reply"))
        self.assertEqual((ctx.exception.problem, ctx.exception.outcome), (PROBLEM_BLOCKED, OUTCOME_FAILED))
        with self.assertRaises(ExecError) as ctx:
            self.mail_executor(accounts={"work": AccountConfig("work")}).sign_in(example("Reply"))
        self.assertEqual(ctx.exception.problem, PROBLEM_SETUP)
        self.work.signed = False
        run.sign_in(example("RSVP"))
        self.assertIn("sign_in", self.work.names())


class ConfirmingCalendar(FakeCalendar):
    """A FakeCalendar whose alias was just bound by a first sign-in (``pending``: its address) and
    not confirmed yet; ``confirm_account`` / ``disconnect`` as gcal.GoogleCalendar's."""

    def __init__(self, *, pending: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.pending = pending
        self.disconnects = 0

    def pending_confirmation(self) -> str:
        return self.pending

    def confirm_account(self, address: str) -> bool:
        if not self.pending or address.strip().casefold() != self.pending.casefold():
            return False
        self.pending = ""
        return True

    def disconnect(self) -> None:
        self.disconnects += 1
        self.signed = False
        self.pending = ""


class ConfirmingSender(FakeSender):
    def __init__(self, *, pending: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.pending = pending
        self.disconnects = 0

    def pending_confirmation(self) -> str:
        return self.pending

    def ready(self) -> tuple[str, str]:
        return (PROBLEM_CONFIRM, "not confirmed") if self.pending else super().ready()

    def confirm_account(self, address: str) -> bool:
        if not self.pending or address.strip().casefold() != self.pending.casefold():
            return False
        self.pending = ""
        return True

    def disconnect(self) -> None:
        self.disconnects += 1
        self.pending = ""
        self.address = ""
        self.problem = PROBLEM_SIGNED_OUT


class UnknownAccountCalendar(FakeCalendar):
    """A FakeCalendar whose saved sign-in names a Google account Jarvis has no binding for
    (google_auth PROBLEM_IDENTITY, as gcal.GoogleCalendar.change_problem says); ``problem`` None
    makes that check itself break."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.problem: str | None = PROBLEM_IDENTITY
        self.unconfirmed = True

    def change_problem(self) -> tuple[str, str]:
        if self.problem is None:
            raise OSError("disk")
        return self.problem, ("Jarvis doesn't know which Google account the work account's sign-in is; sign in "
                              "again to confirm it - nothing was sent or changed")

    def needs_confirmation(self) -> bool:
        return self.unconfirmed


class AccountConfirmationTests(MailTestCase):
    """A first sign-in binds an alias, unconfirmed: Jarvis sends and changes nothing for it until you
    confirm (reading the calendar goes on); "No, use another account" disconnects it."""

    def test_calendar_changes_wait_for_the_confirmation(self) -> None:
        self.work = ConfirmingCalendar(pending="ana@example.edu", store=self.store)
        self.personal = ConfirmingCalendar(pending="me@example.com", store=self.store)
        run = self.executor()
        for action in (example("RSVP"), example("Cancel"), parse_action_line(CHESS), example("Todo")):
            with self.subTest(kind=action.kind):
                self.watch(action)
                with self.assertLogs(EXECUTOR_LOGGER, level="INFO"):
                    outcome = run_action(run, action, store=self.store, writes_running=True)
                self.assertEqual((outcome.status, outcome.error.problem), (STATUS_FAILED, PROBLEM_CONFIRM))
                self.assertIn("Confirm first", str(outcome.error))
                self.assertNotIn("@", str(outcome.error))
                self.assertEqual(self.store.get(action.id)["status"], STATUS_FAILED)   # never "running"
                with self.assertRaises(ExecError):
                    run.execute(action)   # the older interactive path refuses too
        self.assertEqual(self.work.changes() + self.personal.changes(), [])
        self.assertEqual(run.peek(example("RSVP")).title, "Project sync")   # reading goes on
        self.assertTrue(run.confirm_account("work", "ana@example.edu"))
        action = self.watch(example("RSVP", event="abc123def456ghi780"))
        self.assertEqual(run_action(run, action, store=self.store, writes_running=True).status, STATUS_SENT)

    def test_the_executor_asks_confirms_and_disconnects(self) -> None:
        shared = ConfirmingSender(address=ME, pending=ME, store=self.store)
        self.senders["work"] = shared
        self.work = ConfirmingCalendar(pending=ME, store=self.store)
        run = self.mail_executor()
        self.assertEqual((run.pending_confirmation("work"), run.pending_confirmation("personal")), (ME, ""))
        status = run.mail_status(example("Reply"))
        self.assertEqual((status.confirm, status.problem), (True, PROBLEM_CONFIRM))
        self.assertFalse(run.confirm_account("work", "someone.else@example.edu"))
        self.assertEqual(run.pending_confirmation("work"), ME)
        self.assertTrue(run.confirm_account("work", ME.upper()))
        self.assertEqual(run.pending_confirmation("work"), "")
        self.assertTrue(run.mail_status(example("Reply")).ready)
        run.disconnect("work")
        self.assertEqual((shared.disconnects, self.work.disconnects), (1, 1))
        self.assertEqual(run.mail_status(example("Reply")).problem, PROBLEM_SIGNED_OUT)
        self.assertFalse(run.confirm_account("lab", ME))   # no such account: nothing to confirm
        run.disconnect("lab")


    def test_a_calendar_that_cannot_tell_its_account_changes_nothing(self) -> None:
        """Fails closed: a saved sign-in Jarvis has no binding for (PROBLEM_IDENTITY: signs in
        again), or a check that breaks, refuses before "running" is saved."""
        self.work = UnknownAccountCalendar(store=self.store)
        run = self.executor()
        self.assertEqual((run.change_problem("work"), run.change_problem("personal")), (PROBLEM_IDENTITY, ""))
        self.assertEqual(run.pending_confirmation("work"), "")   # nothing to ask: it signs in again first
        action = self.watch(example("RSVP"))
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO"):
            outcome = run_action(run, action, store=self.store, writes_running=True)
        self.assertEqual((outcome.status, outcome.error.problem), (STATUS_FAILED, PROBLEM_IDENTITY))
        self.assertIn("sign in again", str(outcome.error))
        self.assertEqual(self.store.get(action.id)["status"], STATUS_FAILED)   # never "running"
        self.work.problem = None   # the check itself breaks
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO") as logs:
            outcome = run_action(run, action, store=self.store, writes_running=True)
        self.assertEqual((outcome.status, outcome.error.problem), (STATUS_FAILED, PROBLEM_FAILED))
        self.assertIn("Could not check which Google account the sign-in is", "\n".join(logs.output))
        with self.assertLogs(EXECUTOR_LOGGER, level="WARNING"):
            self.assertEqual(run.change_problem("work"), PROBLEM_FAILED)
        self.assertEqual(self.work.changes(), [])

    def test_which_accounts_sign_in_before_anything_changes(self) -> None:
        self.work = UnknownAccountCalendar(store=self.store)
        self.work.unconfirmed = True
        run = self.executor()
        self.assertEqual((run.needs_confirmation("work"), run.needs_confirmation("personal")), (True, False))
        self.work.unconfirmed = False
        self.assertFalse(run.needs_confirmation("work"))

    def test_a_disconnect_that_could_not_delete_the_sign_in_is_reported(self) -> None:
        self.work = ConfirmingCalendar(pending=ME, store=self.store)
        self.work.disconnect = mock.Mock(side_effect=gcal.CalendarError("locked"))   # type: ignore[method-assign]
        run = self.executor()
        with self.assertLogs(EXECUTOR_LOGGER, level="WARNING"), self.assertRaises(ExecError) as ctx:
            run.disconnect("work")
        self.assertEqual(ctx.exception.problem, PROBLEM_FAILED)
        self.assertEqual(str(ctx.exception), DISCONNECT_FAILED_MESSAGE.format(alias="work"))
        self.assertNotIn("locked", str(ctx.exception))
        self.assertEqual(run.pending_confirmation("work"), ME)   # still asked about; nothing sent

class _CalService:
    """An in-memory Calendar API: one event you are invited to; records the changes."""

    def __init__(self) -> None:
        self.changes: list[str] = []

    def _request(self, name: str) -> Any:
        service = self

        class Request:
            def execute(self, num_retries: int = 0) -> Any:
                if name in ("events.patch", "events.delete", "events.insert"):
                    service.changes.append(name)
                if name == "events.get":
                    return {"id": "abc123def456ghi789", "status": "confirmed", "summary": "Speaker series",
                            "htmlLink": EVENT_LINK, "organizer": {"email": "org@example.edu"},
                            "attendees": [{"email": ME, "self": True, "responseStatus": "needsAction"}],
                            "start": {"dateTime": "2026-10-06T17:00:00-04:00"},
                            "end": {"dateTime": "2026-10-06T18:00:00-04:00"}}
                return {"id": "abc123def456ghi789", "htmlLink": EVENT_LINK}
        return Request()

    def events(self) -> Any:
        return SimpleNamespace(get=lambda **kw: self._request("events.get"),
                               patch=lambda **kw: self._request("events.patch"),
                               delete=lambda **kw: self._request("events.delete"),
                               insert=lambda **kw: self._request("events.insert"),
                               list=lambda **kw: self._request("events.list"))


class _GmailService:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def users(self) -> Any:
        return self

    def messages(self) -> Any:
        return self

    def send(self, **kwargs: Any) -> Any:
        service = self

        class Request:
            def execute(self, num_retries: int = 0) -> Any:
                service.sent.append(kwargs)
                return {"id": "18c0ffee000000aa", "threadId": "18c0ffee000000bb"}
        return Request()


class FailClosedAccountTests(unittest.TestCase):
    """The real account objects (google_auth, gcal, gmail) behind the Executor, with in-memory Google
    services and files written by hand (no browser, no network): an answer, move, cancel or email
    needs the saved sign-in's own Google account, confirmed for the alias. The cases from the
    review: "No, use another account" while the token file is locked, and accounts.json deleted,
    unreadable or without the alias's entry while its sign-in is kept."""

    SUB_P = "100000000000000000001"

    def setUp(self) -> None:
        for target, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                             (socket, "create_connection"), (socket, "getaddrinfo")):
            patcher = mock.patch.object(target, name, side_effect=AssertionError("network used"))
            patcher.start()
            self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / "data"
        secret = self.root / "google_client_secret.json"
        secret.write_text(json.dumps({"installed": {
            "client_id": "1234567890-fakeclient.apps.googleusercontent.com",
            "client_secret": "GOCSPX-fake-for-tests", "token_uri": "https://oauth2.googleapis.com/token"}}),
            encoding="utf-8")
        self.bindings = AccountBindings(self.data / "accounts.json")
        self.account = GoogleAccount("work", client_secret_path=secret, data_dir=self.data,
                                     features=("calendar", "gmail_send"), bindings=self.bindings,
                                     refresh=lambda creds: None)
        self.cal_service, self.gmail_service = _CalService(), _GmailService()
        calendar = GoogleCalendar(account=self.account, service_factory=lambda creds: self.cal_service)
        sender = gmail.GmailSender(self.account, service_factory=lambda creds: self.gmail_service)
        self.run = Executor([CalendarBackend({"work": calendar}),
                             GmailBackend({"work": sender}, history=RecipientHistory(self.root / "recipients.json"),
                                          trusted_domains=("example.edu",))], MAIL_ACCOUNTS)
        self.store = ActionStore(self.root / "actions.json")
        # "work" was signed in with the PERSONAL Google account by mistake, not confirmed yet.
        scopes = list(google_auth.IDENTITY_SCOPES) + list(google_auth.scopes_for(("calendar", "gmail_send")))
        self.data.mkdir(parents=True)
        self.account.token_path.write_text(json.dumps({
            "token": "ya29.fake-for-tests", "refresh_token": "1//fake-refresh-for-tests",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "1234567890-fakeclient.apps.googleusercontent.com",
            "client_secret": "GOCSPX-fake-for-tests", "scopes": scopes,
            "expiry": (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)).isoformat() + "Z",
            google_auth.TOKEN_SUB_KEY: self.SUB_P, google_auth.TOKEN_ASKED_KEY: scopes}), encoding="utf-8")
        with self.assertLogs("briefing_reader.google_auth", level="INFO"):
            self.bindings.bind("work", "ana@example.com", self.SUB_P)

    def outcome(self, action: ProposedAction) -> RunOutcome:
        with self.assertLogs(level="INFO"):
            return run_action(self.run, action, store=self.store, writes_running=True)

    def assert_nothing_done(self, problem: str) -> None:
        for action in (example("RSVP"), email_line(acct="work")):
            outcome = self.outcome(action)
            self.assertEqual(outcome.status, STATUS_FAILED, action.kind)
            if action.kind == RSVP:
                self.assertEqual(outcome.error.problem, problem)
        self.assertEqual((self.cal_service.changes, self.gmail_service.sent), ([], []))

    def test_no_while_the_sign_in_cannot_be_deleted_keeps_everything_refused(self) -> None:
        self.assert_nothing_done(PROBLEM_CONFIRM)
        real_unlink = Path.unlink

        def locked(path: Path, *args: Any, **kwargs: Any) -> None:
            if path == self.account.token_path:
                raise PermissionError(13, "The process cannot access the file")
            real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", locked), self.assertLogs(level="WARNING"), \
                self.assertRaises(ExecError) as ctx:
            self.run.disconnect("work")   # "No, use another account"
        self.assertEqual(ctx.exception.problem, PROBLEM_FAILED)
        self.assertTrue(self.account.token_path.exists())
        self.assertEqual(self.run.pending_confirmation("work"), "ana@example.com")   # asked again, not unbound
        self.assert_nothing_done(PROBLEM_CONFIRM)

    def test_a_sign_in_without_its_binding_changes_and_sends_nothing(self) -> None:
        self.bindings.confirm("work", "ana@example.com")
        self.assertEqual(self.outcome(example("RSVP")).status, STATUS_SENT)   # confirmed: it works
        self.assertEqual(self.cal_service.changes, ["events.patch"])
        self.cal_service.changes.clear()
        for case in ("entry removed", "deleted", "unreadable"):
            with self.subTest(case=case):
                path = self.bindings.path
                if case == "entry removed":
                    path.write_text(json.dumps({"personal": {"email": "ben@example.edu",
                                                             "sub": "100000000000000000009"}}), encoding="utf-8")
                elif case == "deleted":
                    path.unlink()
                else:
                    path.write_text('{"work": {"email": "ana@example.com",}', encoding="utf-8")
                unreadable = case == "unreadable"
                with (self.assertLogs("briefing_reader.google_auth", level="WARNING") if unreadable
                      else contextlib.nullcontext()):
                    self.assertEqual(self.run.change_problem("work"), PROBLEM_IDENTITY)
                    self.assertEqual(self.run.pending_confirmation("work"), "")
                    self.assertTrue(self.run.needs_confirmation("work"))
                    self.assertTrue(self.account.is_signed_in("calendar"))   # reading goes on
                    self.assert_nothing_done(PROBLEM_IDENTITY)


class CarriedCalendarTests(ExecutorTestCase):
    """Calendar events and to-do blocks right after the click: "running" first, no browser after."""

    def run_job(self, action: ProposedAction) -> RunOutcome:
        self.watch(action)
        return run_action(self.executor(), action, store=self.store, writes_running=True,
                          on_stage=self.stages.append)

    def test_running_before_the_insert_and_the_result_after(self) -> None:
        for action, status in ((parse_action_line(CHESS), STATUS_CREATED), (example("Todo"), STATUS_CREATED)):
            with self.subTest(kind=action.kind):
                self.personal.calls.clear()
                self.personal.on_disk_at_change.clear()
                outcome = self.run_job(action)
                self.assertEqual(outcome.status, status)
                self.assertEqual(self.personal.on_disk_at_change, [STATUS_RUNNING])
                ((_, (event,), kwargs),) = self.personal.changes()
                self.assertEqual((event.id, kwargs), (action.id, {"interactive": False}))
                saved = ActionStore(self.store.path).get(action.id)
                self.assertEqual((saved["status"], saved["link"], saved["kind"]), (status, EVENT_LINK, action.kind))
        self.personal.outcomes["create_event"] = EventResult("old1", EVENT_LINK, existed=True)
        self.assertEqual(self.run_job(parse_action_line(CHESS)).status, STATUS_EXISTS)

    def test_the_sign_in_comes_before_running(self) -> None:
        self.personal.signed = False
        self.run_job(parse_action_line(CHESS))
        sign_in = next(call for call in self.personal.calls if call[0] == "sign_in")
        self.assertIsNone(sign_in[1])
        self.assertEqual(self.stages, [STAGE_SIGNIN, STAGE_SIGNED_IN, STAGE_WORKING])

    def test_an_unknown_insert_is_saved_and_never_retried(self) -> None:
        self.personal.outcomes["create_event"] = CalendarUnknownOutcome("Google Calendar answered 503", status=503)
        action = parse_action_line(CHESS)
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO"):
            outcome = self.run_job(action)
        self.assertEqual(outcome.status, STATUS_UNKNOWN)
        self.assertEqual(len(self.personal.changes()), 1)
        self.assertEqual(ActionStore(self.store.path).get(action.id)["status"], STATUS_UNKNOWN)
        self.assertFalse(self.store.is_decided(action.id))


class MailBuildTests(BuildTests):
    def test_senders_share_the_accounts_and_bindings(self) -> None:
        cfg = self.config('[accounts.personal]\nfeatures = ["calendar"]\n[accounts.work]\n'
                          'features = ["calendar", "gmail_send"]\n[accounts.lab]\nfeatures = ["gmail_send"]\n'
                          '[actions]\ntrusted_domains = ["example.edu"]\n')
        self.assertEqual(MAIL_SETUP_NOTE, build_executor(cfg, {}, build_senders(cfg)).mail_status(example("Reply")).note)
        cfg.calendar.client_secret_path.write_text(json.dumps({"installed": {
            "client_id": "1234567890-fakeclient.apps.googleusercontent.com", "client_secret": "GOCSPX-fake-for-tests",
            "token_uri": "https://oauth2.googleapis.com/token"}}), encoding="utf-8")
        accounts = build_accounts(cfg)
        self.assertEqual(list(accounts), ["personal", "work", "lab"])
        self.assertIn("openid", accounts["work"].requested_scopes())
        senders = build_senders(cfg, accounts)
        self.assertEqual(list(senders), ["work", "lab"])
        self.assertIs(senders["work"].account, accounts["work"])
        calendars = build_calendars(cfg, accounts)
        self.assertEqual(list(calendars), ["personal", "work"])
        self.assertIs(calendars["work"].account, senders["work"].account)   # one sign-in per alias
        run = build_executor(cfg, calendars, senders)
        self.assertIsInstance(run.backend_for(example("Reply")), GmailBackend)
        self.assertIsInstance(run.backend_for(example("RSVP")), CalendarBackend)
        self.assertEqual(run.mail_status(example("Reply")).problem, PROBLEM_SIGNED_OUT)
        self.assertEqual(run.mail_status(example("Reply")).review.kinds[0][1], TRUSTED)
        self.assertFalse(cfg.data_dir.exists())   # building opens nothing and writes nothing
        self.assertEqual(recipient_history(cfg).path, cfg.data_dir / "recipients.json")


class ImportTests(unittest.TestCase):
    def test_qt_free_and_lazy(self) -> None:
        import subprocess
        import sys

        code = ("import sys, briefing_reader.executor; "
                "print(sorted({m.split('.')[0] for m in sys.modules "
                "if m.split('.')[0] in ('PySide6', 'googleapiclient', 'google_auth_oauthlib', "
                "'oauthlib', 'httplib2')}))")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parent.parent, timeout=60, check=True)
        self.assertEqual(result.stdout.strip(), "[]")

    def test_sources_are_ascii(self) -> None:
        for name in ("executor.py", "google_auth.py", "gcal.py", "gmail.py", "recipients.py"):
            with self.subTest(name=name):
                self.assertTrue((Path(executor.__file__).parent / name).read_bytes().isascii())


class LiveRunTests(MailTestCase):
    """run_action / Executor.execute / preview with a LIVE view task: the steps (checks, sign-in,
    payload, call) and that every outcome and store write is the same with or without them."""

    def setUp(self) -> None:
        super().setUp()
        self.stream = live.LiveStream()

    def task(self) -> live.LiveTask:
        return self.stream.task(live.TASK_ACTION, "card")

    def steps(self, task: live.LiveTask) -> tuple[live.StepView, ...]:
        return self.stream.task_view(task.id).steps

    def step(self, task: live.LiveTask, kind: str) -> live.StepView:
        return next(step for step in self.steps(task) if step.kind == kind)

    @staticmethod
    def fields(step: live.StepView) -> dict[str, str]:
        return {field.label: field.value for field in step.fields}

    def stored(self, action: ProposedAction) -> dict | None:
        saved = ActionStore(self.store.path).get(action.id)
        return {key: value for key, value in saved.items() if key not in ("at", "decided_at", "updated")} \
            if saved else None

    def test_a_reply_shows_exactly_what_was_sent(self) -> None:
        action = example("Reply", cc="cy@example.edu")
        run = self.mail_executor()
        preview = run.preview(action)
        task = self.task()
        payload = task.step(live.ACTION_PAYLOAD, preview.title, status_text="PREVIEW")
        payload.show(preview)
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO"), \
                self.assertLogs("briefing_reader.recipients", level="INFO"):
            outcome = self.run_mail(action, executor=run, live=task)
        self.assertEqual(outcome.status, STATUS_SENT)
        self.assertEqual(outcome.result.ref, "18c0ffee000000aa")
        kinds = [step.kind for step in self.steps(task)]
        self.assertEqual(kinds, [live.ACTION_PAYLOAD, live.ACTION_CHECKS, live.ACTION_CALL])
        (mail,) = self.senders["work"].sent
        view = gmail.mail_view(mail)
        shown = self.step(task, live.ACTION_PAYLOAD)
        self.assertEqual((shown.status, shown.status_text), (live.STATUS_OK, "SENT EXACTLY THIS"))
        fields = self.fields(shown)
        self.assertEqual((fields["From"], fields["To"], fields["Cc"], fields["Subject"]),
                         (view.from_addr, view.to, view.cc, view.subject))
        self.assertEqual((fields["In-Reply-To"], fields["References"], fields["Gmail thread"]),
                         (view.in_reply_to, view.references, view.thread_id))
        body = shown.blocks[0]
        self.assertEqual((body.label, body.text, body.start_open), ("Message", view.body, True))
        call = self.step(task, live.ACTION_CALL)
        self.assertEqual((call.status, call.status_text), (live.STATUS_OK, "SENT"))
        self.assertEqual(self.fields(call)["Request"], "users.messages.send")
        self.assertEqual(self.fields(call)["Gmail message id"], "18c0ffee000000aa")
        self.assertEqual(self.fields(call)["Recipients remembered"], "3")
        link = next(field.link for field in call.fields if field.label == "Thread")
        self.assertEqual(link, "https://mail.google.com/mail/#all/18c0ffee00000001")
        checks = self.step(task, live.ACTION_CHECKS)
        self.assertEqual(checks.status, live.STATUS_OK)
        self.assertEqual([note.text for note in checks.notes],
                         ["Account ready (the Google account you confirmed)",
                          'Saved "running" before the call (so a crash shows UNKNOWN, never a silent resend)'])
        self.assertFalse(task.finished)   # the worker finishes the task

    def test_preview_makes_no_call_and_equals_what_is_sent(self) -> None:
        action = email_line()
        run = self.mail_executor()
        preview = run.preview(action)
        self.assertEqual(self.senders["personal"].sent, [])
        self.assertEqual(self.senders["personal"].calls, [])
        task = self.task()
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO"), \
                self.assertLogs("briefing_reader.recipients", level="INFO"):
            self.run_mail(action, executor=run, live=task)
        recorded = self.step(task, live.ACTION_PAYLOAD)
        self.assertEqual([(field.label, field.value) for field in recorded.fields],
                         [(field.label, field.value) for field in preview.fields])
        self.assertEqual(recorded.blocks[0].text, preview.body)
        self.assertEqual(preview.values()["To"], "office@example.edu")
        for kind in ("RSVP", "Move", "Cancel", "Todo"):
            with self.subTest(kind=kind):
                before = len(self.work.calls) + len(self.personal.calls)
                calendar_preview = run.preview(example(kind))
                self.assertIsNotNone(calendar_preview)
                self.assertEqual(len(self.work.calls) + len(self.personal.calls), before)   # no Google call
        self.assertIsNone(run.preview(example("Reply", replied="yes")))   # nothing to carry out
        self.assertIsNone(run.preview(example("RSVP", acct="school")))    # no backend

    def test_a_payload_that_changed_since_the_countdown_says_so(self) -> None:
        action = email_line()
        run = self.mail_executor()
        task = self.task()
        payload = task.step(live.ACTION_PAYLOAD, "Will send exactly this", status_text="PREVIEW")
        payload.show(run.preview(apply_edit(action, ActionEdit(body="Another text"))))
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO"), \
                self.assertLogs("briefing_reader.recipients", level="INFO"):
            self.run_mail(action, executor=run, live=task)
        shown = self.step(task, live.ACTION_PAYLOAD)
        self.assertEqual((shown.status, shown.status_text), (live.STATUS_WARN, "CHECK"))
        self.assertEqual(shown.notes[0].text, "Not what the card showed: Message changed")

    def test_a_mail_payload_carries_the_email_drawn_as_it_goes_out(self) -> None:
        from briefing_reader import pictures

        view = gmail.MailView("me@example.edu", "ana@example.edu", "cy@example.edu", "Re: Lunch", "<abc@example.com>",
                              "<abc@example.com>", "18c0ffee00000001", "Hi Ana,\nThursday works.\n")
        payload = executor.mail_payload(view, "work")
        picture = payload.picture
        self.assertEqual((picture.kind, picture.mail.account, picture.mail.body),
                         (pictures.KIND_OUTGOING, "work", "Hi Ana,\nThursday works.\n"))
        self.assertEqual((picture.mail.head.sender, picture.mail.head.to, picture.mail.head.cc),
                         ("me@example.edu", "ana@example.edu", "cy@example.edu"))
        self.assertEqual(executor.mail_payload(view).picture.mail.account, "")
        # A Reply's preview has it, and what was sent replaces it with the message actually built.
        action = example("Reply", cc="cy@example.edu")
        run = self.mail_executor()
        preview = run.preview(action)
        self.assertEqual((preview.picture.kind, preview.picture.mail.account), (pictures.KIND_OUTGOING, "work"))
        self.assertEqual(preview.picture.mail.head.subject, "Re: Thursday noon meeting")
        task = self.task()
        payload_step = task.step(live.ACTION_PAYLOAD, preview.title, status_text="PREVIEW")
        payload_step.show(preview)
        self.assertIs(self.step(task, live.ACTION_PAYLOAD).picture, preview.picture)
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO"), \
                self.assertLogs("briefing_reader.recipients", level="INFO"):
            self.run_mail(action, executor=run, live=task)
        shown = self.step(task, live.ACTION_PAYLOAD)
        (mail,) = self.senders["work"].sent
        self.assertEqual(shown.picture.mail.head.to, gmail.mail_view(mail).to)
        self.assertEqual(pictures.stamp(shown.picture, shown.status, shown.status_text), ("SENT", pictures.TONE_DONE))
        # An Email's preview too; a calendar preview has none (the app draws its day).
        self.assertEqual(run.preview(email_line()).picture.kind, pictures.KIND_OUTGOING)
        self.assertIsNone(run.preview(example("Move")).picture)

    def test_calendar_kinds(self) -> None:
        cases = {"RSVP": ("Google Calendar: answer the invitation", "events.patch (your attendee entry only)",
                          "ACCEPTED", "abc123def456ghi789"),
                 "Move": ("Google Calendar: move the event", "events.patch (start, end)", "MOVED",
                          "abc123def456ghi789_20261008T190000Z"),
                 "Cancel": ("Google Calendar: cancel the event", "events.delete", "CANCELLED EVENT",
                            "zyx987wvu654tsr321"),
                 "Todo": ("Google Calendar: add the block", "events.insert (after looking for the same event)",
                          "ADDED", "evt1")}
        for kind, (title, request, word, ref) in cases.items():
            with self.subTest(kind=kind):
                task = self.task()
                outcome = self.run_mail(example(kind), live=task)
                self.assertEqual((outcome.status, outcome.result.ref), (STATUS_SENT if kind != "Todo" else
                                                                        STATUS_CREATED, ref))
                kinds = [step.kind for step in self.steps(task)]
                self.assertEqual(kinds, [live.ACTION_CHECKS, live.ACTION_PAYLOAD, live.ACTION_CALL])
                call = self.step(task, live.ACTION_CALL)
                self.assertEqual((call.title, self.fields(call)["Request"], call.status_text, call.status),
                                 (title, request, word, live.STATUS_OK))
                self.assertEqual(self.fields(call)["Event id"], ref)
                payload = self.step(task, live.ACTION_PAYLOAD)
                self.assertEqual(payload.status, live.STATUS_OK)
                self.assertIn(payload.status_text, ("CHANGED", "ADDED"))
        rsvp = self.run_mail(example("RSVP"), live=(task := self.task()))
        self.assertEqual(rsvp.status, STATUS_SENT)
        fields = self.fields(self.step(task, live.ACTION_PAYLOAD))
        self.assertEqual((fields["Event"], fields["Event id"], fields["Answer"], fields["Who gets an email"]),
                         ("Speaker series", "abc123def456ghi789", "yes (accepted)",
                          live.notify_words("all")))
        move = self.fields(self.step(self.last_task(example("Move")), live.ACTION_PAYLOAD))
        self.assertEqual(move["New time"], "Thu Oct 8 2:00-3:00 PM, in your calendar's time zone")
        chess = parse_action_line(CHESS)
        task = self.task()
        self.run_mail(chess, live=task)
        payload = self.step(task, live.ACTION_PAYLOAD)
        self.assertEqual(self.fields(payload)["Title"], "Chess Club Weekly Meeting")
        self.assertEqual(self.fields(payload)["Calendar"], "personal (primary)")
        self.assertEqual(payload.blocks[0].text, gcal.EVENT_FOOTER)   # as build_event_body writes it
        # A day plan's block: the payload shows its own footer, and the request sent matches it.
        block = dataclasses.replace(parse_action_line(CHESS + " | | | For: Lab report"), mode="plan")
        self.personal.__class__ = HookCalendar
        self.personal.tamper, self.personal.requests = {}, []
        task = self.task()
        self.run_mail(block, live=task)
        payload = self.step(task, live.ACTION_PAYLOAD)
        self.assertTrue(payload.blocks[0].text.endswith("Added by Jarvis from your day plan."))
        self.assertNotEqual(payload.status_text, "CHANGED SINCE")
        sent = self.personal.requests[-1][1]["body"]["description"]
        self.assertEqual(sent, "For: Lab report" + "\n\n" + "Added by Jarvis from your day plan.")

    def test_already_so_sends_nothing_and_says_so(self) -> None:
        cases = {"RSVP": ("respond", ChangeResult("abc123def456ghi789", EVENT_LINK, already=True), "ALREADY ANSWERED",
                          "none - Google has that answer already (read with events.get)"),
                 "Move": ("move", ChangeResult("abc123def456ghi789_20261008T190000Z", EVENT_LINK, already=True),
                          "ALREADY AT THAT TIME", "none - the event is at that time already (read with events.get)"),
                 "Cancel": ("cancel", ChangeResult("zyx987wvu654tsr321", "", already=True), "ALREADY GONE",
                            "none - Google has no such event any more (read with events.get)"),
                 "Todo": ("create_event", EventResult("evt1", EVENT_LINK, existed=True), "ALREADY THERE",
                          "none - the same event is already there (found with events.list)")}
        for kind, (name, answer, word, request) in cases.items():
            with self.subTest(kind=kind):
                for calendar in (self.work, self.personal):
                    calendar.outcomes[name] = answer
                task = self.task()
                self.run_mail(example(kind), live=task)
                call = self.step(task, live.ACTION_CALL)
                self.assertEqual((call.status, call.status_text), (live.STATUS_OK, word))
                self.assertEqual(self.fields(call)["Request"], request)
                self.assertEqual(self.fields(call)["Already that way"], "yes - nothing changed")
                payload = self.step(task, live.ACTION_PAYLOAD)
                self.assertEqual((payload.status, payload.status_text), (live.STATUS_OK, "NOTHING SENT"))
                self.assertIn("nothing was sent", payload.summary)

    def hook_executor(self, tamper: dict[str, Any] | None = None) -> Executor:
        self.work, self.personal = (HookCalendar(store=self.store, tamper=tamper),
                                    HookCalendar(store=self.store, tamper=tamper))
        return self.mail_executor()

    def test_the_exact_request_to_google_is_shown_and_checked(self) -> None:
        for kind in ("RSVP", "Move", "Cancel", "Todo"):
            with self.subTest(kind=kind):
                run = self.hook_executor()
                task = self.task()
                self.run_mail(example(kind), executor=run, live=task)
                calendar = self.personal if kind in ("Cancel", "Todo") else self.work   # account_of
                ((method, params),) = calendar.requests
                call = self.step(task, live.ACTION_CALL)
                self.assertEqual(self.fields(call)["Request"], executor._request_line(method, params))
                self.assertTrue(self.fields(call)["Request"].startswith(method + " calendarId=primary"))
                payload = self.step(task, live.ACTION_PAYLOAD)
                self.assertEqual(payload.status, live.STATUS_OK)
                self.assertIn(payload.status_text, ("CHANGED", "ADDED"))
                blocks = {block.label: block.text for block in payload.blocks}
                if "body" in params:
                    self.assertEqual(json.loads(blocks[executor.REQUEST_BLOCK]), params["body"])
                else:
                    self.assertNotIn(executor.REQUEST_BLOCK, blocks)   # a delete has no body
                self.assertEqual(executor.request_differs(example(kind), method, params), [])
        # A request that is not what the countdown showed says so (CHECK), whatever Google answers.
        run = self.hook_executor(tamper={"eventId": "another0event"})
        task = self.task()
        self.run_mail(example("Move"), executor=run, live=task)
        payload = self.step(task, live.ACTION_PAYLOAD)
        self.assertEqual((payload.status, payload.status_text), (live.STATUS_WARN, "CHECK"))
        self.assertEqual(payload.notes[-1].text, "Not what the card showed: Event id differs in the request to "
                                                 "Google")
        move = example("Move")
        good = {"calendarId": "primary", "eventId": move.field("event"), "sendUpdates": "all",
                "body": {"start": {"dateTime": "2026-10-08T14:00:00", "timeZone": "X"},
                         "end": {"dateTime": "2026-10-08T15:00:00", "timeZone": "X"}}}
        self.assertEqual(executor.request_differs(move, "events.patch", good), [])
        late = dict(good, sendUpdates="none", body={"start": {"dateTime": "2026-10-08T16:00:00"},
                                                   "end": {"dateTime": "2026-10-08T17:00:00"}})
        self.assertEqual(executor.request_differs(move, "events.patch", late), ["Who gets an email", "New time"])
        self.assertEqual(executor.request_differs(move, "events.delete", good), ["Request"])

    def test_a_plan_blocks_footer_differing_from_the_preview_reads_check(self) -> None:
        """G24: the countdown showed the day plan's footer; a request carrying another description
        (the briefing's footer here) is not what was shown."""
        block = dataclasses.replace(parse_action_line("Calendar: Work on: Lab report | 2026-10-08 15:00-16:00 | | | "
                                                      "For: Lab report"), mode="plan")
        body = gcal.build_event_body(block, "America/Los_Angeles", footer=gcal.event_footer(block))
        self.assertTrue(str(body["description"]).endswith(gcal.PLAN_EVENT_FOOTER))
        params = {"calendarId": "primary", "body": body}
        self.assertEqual(executor.request_differs(block, "events.insert", params), [])
        briefing_footer = gcal.build_event_body(block, "America/Los_Angeles", footer=gcal.EVENT_FOOTER)
        wrong = {"calendarId": "primary", "body": briefing_footer}
        self.assertTrue(str(briefing_footer["description"]).endswith(gcal.EVENT_FOOTER))
        self.assertEqual(executor.request_differs(block, "events.insert", wrong), ["Description"])
        no_notes = dict(body, description="For: Lab report")
        self.assertEqual(executor.request_differs(block, "events.insert", {"body": no_notes}), ["Description"])

    def last_task(self, action: ProposedAction) -> live.LiveTask:
        task = self.task()
        self.run_mail(action, live=task)
        return task

    def test_sign_in_stages(self) -> None:
        self.work.signed = False
        task = self.task()
        self.run_mail(example("Move"), live=task)
        self.assertEqual(self.stages, [STAGE_SIGNIN, STAGE_SIGNED_IN, STAGE_WORKING])   # passed on unchanged
        signin = self.step(task, live.ACTION_SIGNIN)
        self.assertEqual((signin.status, self.fields(signin)["Account"]), (live.STATUS_OK, "work"))
        self.assertEqual([step.kind for step in self.steps(task)],
                         [live.ACTION_CHECKS, live.ACTION_SIGNIN, live.ACTION_PAYLOAD, live.ACTION_CALL])
        self.stages.clear()
        self.work.signed = False
        self.work.sign_in_error = CalendarAuthError("Google sign-in was cancelled", problem=PROBLEM_DENIED)
        task = self.task()
        outcome = self.run_mail(example("Move"), live=task)
        self.assertEqual(outcome.status, STATUS_FAILED)
        signin = self.step(task, live.ACTION_SIGNIN)
        self.assertEqual((signin.status, signin.summary), (live.STATUS_FAILED, "Google sign-in was cancelled"))

    def test_unknown_failed_and_refused(self) -> None:
        self.senders["work"].outcome = GmailUnknownOutcome("No answer from Gmail while sending (TimeoutError); "
                                                           "the message may or may not have been sent", status=503)
        task = self.task()
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO"):
            outcome = self.run_mail(example("Reply"), live=task)
        self.assertEqual(outcome.status, STATUS_UNKNOWN)
        call = self.step(task, live.ACTION_CALL)
        self.assertEqual((call.status, call.status_text), (live.STATUS_WARN, "UNKNOWN"))
        self.assertIn("HTTP 503", call.summary)
        self.assertEqual(self.step(task, live.ACTION_PAYLOAD).status_text, "UNKNOWN")
        self.senders["work"].outcome = GmailError("Gmail refused the message (400: Bad); nothing was sent", status=400)
        task = self.task()
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO"):
            self.run_mail(example("Reply"), live=task)
        call = self.step(task, live.ACTION_CALL)
        self.assertEqual(call.status, live.STATUS_FAILED)
        self.assertEqual(call.summary, "Gmail refused the message (400: Bad); nothing was sent (HTTP 400)")
        self.assertEqual((self.step(task, live.ACTION_PAYLOAD).status_text,
                          self.step(task, live.ACTION_PAYLOAD).summary), ("NOT SENT", "Not sent"))
        # The sending account itself as a recipient: Jarvis's own guard, before any call.
        self.senders["work"].outcome = None
        self.senders["work"].sent.clear()
        mine = apply_edit(example("Reply"), ActionEdit(cc=(ME,)))
        task = self.task()
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO"):
            outcome = self.run_mail(mine, live=task)
        self.assertEqual(outcome.status, STATUS_FAILED)
        checks = self.step(task, live.ACTION_CHECKS)
        self.assertEqual(checks.status, live.STATUS_BLOCKED)
        self.assertEqual(checks.summary, str(outcome.error))
        self.assertNotIn(live.ACTION_CALL, [step.kind for step in self.steps(task)])
        self.assertEqual(self.senders["work"].sent, [])
        # Not saved: nothing is sent and there is no call step.
        task = self.task()
        with self.assertLogs(EXECUTOR_LOGGER, level="INFO"):
            outcome = self.run_mail(example("Reply"), store=None, live=task)
        self.assertEqual(str(outcome.error), NOT_SAVED_MESSAGE)
        checks = self.step(task, live.ACTION_CHECKS)
        self.assertEqual((checks.status, checks.summary), (live.STATUS_FAILED, NOT_SAVED_MESSAGE))
        self.assertEqual([step.kind for step in self.steps(task)], [live.ACTION_CHECKS])

    def test_outcomes_and_store_writes_are_the_same_with_or_without_live(self) -> None:
        cases = [("reply", lambda: example("Reply", cc="cy@example.edu"), None),
                 ("email", email_line, None),
                 ("rsvp", lambda: example("RSVP"), None),
                 ("move", lambda: example("Move"), None),
                 ("cancel", lambda: example("Cancel"), None),
                 ("todo", lambda: example("Todo"), None),
                 ("calendar", lambda: parse_action_line(CHESS), None),
                 ("unknown", lambda: example("Reply"), GmailUnknownOutcome("No answer (TimeoutError)", status=502)),
                 ("refused", lambda: apply_edit(example("Reply"), ActionEdit(cc=(ME,))), None)]
        for name, make, failure in cases:
            with self.subTest(case=name):
                results = []
                for with_live in (False, True):
                    self.setUp()
                    self.senders["work"].outcome = failure
                    kwargs = {"live": self.task()} if with_live else {}
                    with self.assertLogs("briefing_reader", level="DEBUG") as logs:
                        logging.getLogger("briefing_reader").debug("marker")
                        outcome = self.run_mail(make(), **kwargs)
                    sent = [(mail.to, mail.cc, mail.subject, mail.body) for mail in
                            self.senders["work"].sent + self.senders["personal"].sent]
                    results.append((outcome.status, outcome.result, outcome.result.ref if outcome.result else "",
                                    str(outcome.error), type(outcome.error).__name__, self.stored(make()), sent,
                                    [line for line in logs.output], self.stages[:], self.work.changes(),
                                    self.personal.changes()))
                self.assertEqual(results[0], results[1])
                self.assertNotIn("Live view", "\n".join(results[1][7]))


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
