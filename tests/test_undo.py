"""The owner's decision of 2026-10-17 below the UI: no countdown; a calendar change acts at once and its
Undo reverses exactly that change through the same account and calendar (gcal.undo_create /
undo_respond / undo_move, executor.CalendarBackend.undo, executor.run_undo), with the change's own
guest-notification policy, and only while the event is exactly as Jarvis's change left it (its etag);
otherwise nothing is changed ("Changed since - left as it is."). Every name, address and id is invented.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from briefing_reader import executor as executor_mod
from briefing_reader import gcal
from briefing_reader.actions import (
    STATUS_CREATED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SENT,
    STATUS_UNKNOWN,
    UNDO_INTERRUPTED,
    ActionStore,
    parse_action_line,
)
from briefing_reader.config import AccountConfig
from briefing_reader.executor import (
    UNDO_CHANGED_TEXT,
    UNDO_REFUSED,
    UNDONE,
    CalendarBackend,
    Executor,
    UndoRecord,
    run_action,
    run_undo,
)
from tests import test_gcal as tg

EDT = timezone(timedelta(hours=-4), "EDT")
ACCOUNTS = {"personal": AccountConfig("personal"), "work": AccountConfig("work")}
RSVP_LINE = ("RSVP: acct=work | event=abc123def456ghi789 | cal=primary | answer=yes | notify=external | "
             "title=Speaker series | at=2026-10-08 12:00-13:00 | due= | link= | body=")
MOVE_LINE = ("Move: acct=work | event=abc123def456ghi789 | cal=primary | when=2026-10-08 14:00-15:00 | notify=none | "
             "title=Project sync | at=2026-10-08 12:00-13:00 | link= | body=")
CALENDAR_LINE = "Calendar: Chess Club Weekly Meeting | 2026-10-09 15:00-16:00"


def etagged(item: dict[str, Any], etag: str) -> dict[str, Any]:
    return dict(item, etag=etag)


# --------------------------------------------------------------------------
# gcal: the forward calls record the stamp; the undo calls check it first
# --------------------------------------------------------------------------

class GcalUndoTests(tg.EventCallTestCase):
    def test_the_forward_calls_answer_the_new_stamp_and_the_view_before(self) -> None:
        self.service.outcomes["events.insert"] = [{"id": "evt9", "htmlLink": tg.EVENT_LINK, "etag": '"e-new"'}]
        cal = self.signed_in_calendar()
        created = cal.create_event(tg.make_action())
        self.assertEqual((created.event_id, created.stamp), ("evt9", '"e-new"'))
        self.service = tg.FakeService()
        before = etagged(tg._event_item(organizer_self=False, me="needsAction", comment="old note"), '"e1"')
        cal = self.event_calendar(before, patch={"id": tg.EVENT_ID, "htmlLink": tg.EVENT_LINK, "etag": '"e2"'})
        answered = cal.respond(tg.EVENT_ID, "yes")
        self.assertEqual(answered.stamp, '"e2"')
        self.assertEqual((answered.before.self_response, answered.before.self_comment, answered.before.stamp),
                         ("needsAction", "old note", '"e1"'))
        self.service = tg.FakeService()
        cal = self.event_calendar(etagged(tg._event_item(), '"e1"'),
                                  patch={"id": tg.EVENT_ID, "htmlLink": tg.EVENT_LINK, "updated": "2026-10-01T13:00Z"})
        moved = cal.move(tg.EVENT_ID, datetime(2026, 10, 8, 14), datetime(2026, 10, 8, 15), now=tg.BEFORE)
        self.assertEqual(moved.stamp, "2026-10-01T13:00Z")   # no etag: Google's updated time
        self.assertEqual(moved.before.start, datetime(2026, 10, 8, 12, tzinfo=EDT))

    def test_undo_create_deletes_exactly_that_event_telling_nobody(self) -> None:
        cal = self.event_calendar(etagged(tg._event_item(), '"e1"'))
        result = cal.undo_create(tg.EVENT_ID, stamp='"e1"')
        self.assertFalse(result.already)
        self.assertEqual(self.service.calls_to("events.delete"),
                         [({"calendarId": "primary", "eventId": tg.EVENT_ID, "sendUpdates": "none"}, 0)])
        self.assertEqual(self.service.calls_to("events.get")[0][0]["eventId"], tg.EVENT_ID)

    def test_undo_respond_puts_the_earlier_answer_back_with_the_same_notification(self) -> None:
        cal = self.event_calendar(etagged(tg._event_item(organizer_self=False, me="accepted"), '"e2"'),
                                  patch={"id": tg.EVENT_ID, "etag": '"e3"'})
        result = cal.undo_respond(tg.EVENT_ID, "needsAction", comment="old note", stamp='"e2"',
                                  send_updates="externalOnly", calendar_id=tg.TEAM)
        self.assertEqual(result.stamp, '"e3"')
        self.assertEqual(self.service.calls_to("events.patch"), [(
            {"calendarId": tg.TEAM, "eventId": tg.EVENT_ID, "sendUpdates": "externalOnly",
             "body": {"attendeesOmitted": True,
                      "attendees": [{"email": tg.YOU, "responseStatus": "needsAction", "comment": "old note"}]}},
            0)])

    def test_undo_move_puts_the_exact_old_start_and_end_back(self) -> None:
        cal = self.event_calendar(etagged(tg._event_item(start="2026-10-08T14:00:00-04:00",
                                                         end="2026-10-08T15:00:00-04:00"), '"e2"'))
        cal.undo_move(tg.EVENT_ID, datetime(2026, 10, 8, 12, tzinfo=EDT), datetime(2026, 10, 8, 13, tzinfo=EDT),
                      time_zone=tg.TZ, stamp='"e2"', send_updates="none")
        self.assertEqual(self.service.calls_to("events.patch"), [(
            {"calendarId": "primary", "eventId": tg.EVENT_ID, "sendUpdates": "none",
             "body": {"start": {"dateTime": "2026-10-08T12:00:00-04:00", "timeZone": tg.TZ},
                      "end": {"dateTime": "2026-10-08T13:00:00-04:00", "timeZone": tg.TZ}}}, 0)])

    def test_changed_or_gone_since_is_refused_and_nothing_is_changed(self) -> None:
        calls = (lambda cal: cal.undo_create(tg.EVENT_ID, stamp='"e1"'),
                 lambda cal: cal.undo_respond(tg.EVENT_ID, "declined", stamp='"e1"'),
                 lambda cal: cal.undo_move(tg.EVENT_ID, datetime(2026, 10, 8, 12, tzinfo=EDT),
                                           datetime(2026, 10, 8, 13, tzinfo=EDT), stamp='"e1"'))
        for outcome in (etagged(tg._event_item(), '"e7"'), tg._event_item(),
                        tg.http_error(404, "Not Found", "notFound"), tg._event_item(status="cancelled")):
            for call in calls:
                with self.subTest(outcome=str(outcome)[:30], call=call):
                    self.service = tg.FakeService()
                    cal = self.event_calendar(outcome)
                    with self.assertLogs(tg.GCAL_LOGGER, level="INFO"), self.assertRaises(gcal.EventChanged):
                        call(cal)
                    self.assert_no_change_sent()

    def test_an_undo_after_an_unknown_outcome_counts_a_finished_reversal_as_done(self) -> None:
        cal = self.event_calendar(tg.http_error(404, "Not Found", "notFound"))
        self.assertTrue(cal.undo_create(tg.EVENT_ID, stamp='"e1"', uncertain=True).already)
        self.assert_no_change_sent()
        self.service = tg.FakeService()
        cal = self.event_calendar(etagged(tg._event_item(me="needsAction", organizer_self=False), '"e9"'))
        self.assertTrue(cal.undo_respond(tg.EVENT_ID, "needsAction", stamp='"e2"', uncertain=True).already)
        self.assert_no_change_sent()

    def test_an_undo_never_signs_in(self) -> None:
        self.write_client_secret()   # no saved sign-in
        cal = self.make_calendar()
        with self.assertLogs(tg.GCAL_LOGGER, level="INFO"), self.assertRaises(gcal.CalendarNotSignedIn):
            cal.undo_create(tg.EVENT_ID, stamp='"e1"')
        self.assertEqual(self.flow.calls, [])


# --------------------------------------------------------------------------
# executor: the change's ExecResult carries its UndoRecord; Undo goes back through the same calendar
# --------------------------------------------------------------------------

class StampingCalendar:
    """A calendar that answers like Google with stamps (see tests.ui_fakes.FakeCalendar for the app)."""

    calendar_id = "primary"

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.stamp = '"e1"'
        self.refuse: BaseException | None = None
        self.before = tg_details(response="needsAction", comment="old note")

    def is_configured(self) -> bool:
        return True

    def is_signed_in(self) -> bool:
        return True

    def create_event(self, action: Any, **kwargs: Any) -> gcal.EventResult:
        self.calls.append(("create_event", action.title))
        return gcal.EventResult("evt1", tg.EVENT_LINK, existed=False, stamp=self.stamp)

    def respond(self, event_id: str, answer: str, **kwargs: Any) -> gcal.ChangeResult:
        self.calls.append(("respond", event_id, answer, kwargs.get("send_updates")))
        return gcal.ChangeResult(event_id, tg.EVENT_LINK, already=False, stamp=self.stamp, before=self.before)

    def move(self, event_id: str, start: datetime, end: datetime, **kwargs: Any) -> gcal.ChangeResult:
        self.calls.append(("move", event_id, kwargs.get("send_updates")))
        return gcal.ChangeResult(event_id, tg.EVENT_LINK, already=False, stamp=self.stamp, before=self.before)

    def _undo(self, *call: Any) -> gcal.ChangeResult:
        self.calls.append(call)
        if self.refuse is not None:
            raise self.refuse
        return gcal.ChangeResult(call[1], "", already=False)

    def undo_create(self, event_id: str, **kwargs: Any) -> gcal.ChangeResult:
        return self._undo("undo_create", event_id, kwargs["stamp"], kwargs["send_updates"])

    def undo_respond(self, event_id: str, response: str, **kwargs: Any) -> gcal.ChangeResult:
        return self._undo("undo_respond", event_id, response, kwargs["comment"], kwargs["stamp"],
                          kwargs["send_updates"])

    def undo_move(self, event_id: str, start: datetime, end: datetime, **kwargs: Any) -> gcal.ChangeResult:
        return self._undo("undo_move", event_id, start, end, kwargs["time_zone"], kwargs["stamp"],
                          kwargs["send_updates"])


def tg_details(*, response: str, comment: str = "") -> gcal.EventDetails:
    return gcal.EventDetails(
        event_id="abc123def456ghi789", calendar_id="primary", title="Project sync",
        start=datetime(2026, 10, 8, 12, tzinfo=EDT), end=datetime(2026, 10, 8, 13, tzinfo=EDT), all_day_start=None,
        all_day_end=None, status="confirmed", organizer_self=True, guests_can_modify=False,
        self_email="you@example.edu", self_response=response, attendee_count=3, recurring_instance=False,
        series=False, time_zone="America/New_York", link=tg.EVENT_LINK, self_comment=comment, stamp='"e0"')


class ExecutorUndoTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.store = ActionStore(Path(tmp.name) / "actions.json")
        self.personal, self.work = StampingCalendar(), StampingCalendar()
        backend = CalendarBackend({"personal": self.personal, "work": self.work},
                                  now=lambda: datetime(2026, 10, 1, 9, tzinfo=EDT))
        self.executor = Executor([backend], ACCOUNTS)

    def carry(self, line: str) -> Any:
        action = parse_action_line(line)
        with self.assertLogs("briefing_reader", level="INFO"):
            outcome = run_action(self.executor, action, store=self.store, writes_running=True)
        return action, outcome

    def test_each_calendar_change_carries_the_undo_of_exactly_that_change(self) -> None:
        action, outcome = self.carry(CALENDAR_LINE)
        self.assertEqual(outcome.status, STATUS_CREATED)
        self.assertEqual(outcome.result.undo, UndoRecord(action.kind, "personal", "evt1", '"e1"', send_updates="none",
                                                         calendar_id="primary"))
        action, outcome = self.carry(RSVP_LINE)
        self.assertEqual(outcome.status, STATUS_SENT)
        self.assertEqual(outcome.result.undo, UndoRecord("rsvp", "work", "abc123def456ghi789", '"e1"',
                                                         send_updates="externalOnly", calendar_id="primary",
                                                         response="needsAction", comment="old note"))
        action, outcome = self.carry(MOVE_LINE)
        record = outcome.result.undo
        self.assertEqual((record.kind, record.send_updates, record.start, record.end, record.time_zone),
                         ("move", "none", datetime(2026, 10, 8, 12, tzinfo=EDT), datetime(2026, 10, 8, 13, tzinfo=EDT),
                          "America/New_York"))

    def test_nothing_changed_or_no_stamp_offers_no_undo(self) -> None:
        self.work.stamp = ""
        self.assertIsNone(self.carry(RSVP_LINE)[1].result.undo)
        self.personal.create_event = lambda action, **kw: gcal.EventResult("old1", "", existed=True, stamp='"e1"')
        self.assertIsNone(self.carry(CALENDAR_LINE)[1].result.undo)
        cancel = ("Cancel: acct=work | event=abc123def456ghi789 | cal=primary | notify=all | title=Study group | "
                  "at=2026-10-09 18:00-19:00 | link= | body=")
        self.work.cancel = lambda event_id, **kw: gcal.ChangeResult(event_id, "", already=False, stamp='"e1"',
                                                                    before=self.work.before)
        self.assertIsNone(self.carry(cancel)[1].result.undo)   # a cancellation is held, never reversed

    def test_undo_reverses_through_the_same_calendar_with_the_same_policy_and_forgets_the_decision(self) -> None:
        for line, calendar, expected in (
                (CALENDAR_LINE, "personal", ("undo_create", "evt1", '"e1"', "none")),
                (RSVP_LINE, "work", ("undo_respond", "abc123def456ghi789", "needsAction", "old note", '"e1"',
                                     "externalOnly")),
                (MOVE_LINE, "work", ("undo_move", "abc123def456ghi789", datetime(2026, 10, 8, 12, tzinfo=EDT),
                                     datetime(2026, 10, 8, 13, tzinfo=EDT), "America/New_York", '"e1"', "none"))):
            with self.subTest(line=line[:10]):
                action, outcome = self.carry(line)
                with self.assertLogs("briefing_reader.executor", level="INFO") as logs:
                    undone = run_undo(self.executor, action, outcome.result.undo, store=self.store)
                self.assertEqual(undone.status, UNDONE)
                self.assertEqual(getattr(self, calendar).calls[-1], expected)
                self.assertIsNone(self.store.get(action.id))   # the card waits for a decision again
                self.assertIn(f"Undo of action {action.id} ({action.kind}, ", "\n".join(logs.output))
                for private in ("Speaker series", "Project sync", "Chess", "old note", "abc123def456ghi789"):
                    self.assertNotIn(private, "\n".join(logs.output))

    def test_an_undo_refused_or_failed_keeps_the_result(self) -> None:
        action, outcome = self.carry(RSVP_LINE)
        saved = self.store.get(action.id)
        on_disk: list[str] = []
        self.work.refuse = gcal.EventChanged("changed")
        original = self.work._undo

        def watching(*call: Any) -> Any:
            on_disk.append(json.loads(self.store.path.read_text(encoding="utf-8"))[action.id]["status"])
            return original(*call)

        self.work._undo = watching
        with self.assertLogs("briefing_reader", level="INFO"):
            refused = run_undo(self.executor, action, outcome.result.undo, store=self.store)
        self.assertEqual((refused.status, str(refused.error)), (UNDO_REFUSED, UNDO_CHANGED_TEXT))
        self.assertEqual(UNDO_CHANGED_TEXT, "Changed since - left as it is.")
        self.assertEqual(on_disk, [STATUS_RUNNING])   # "running" before the reversal, as every change
        self.assertEqual((self.store.get(action.id)["status"], self.store.get(action.id)["link"]),
                         (saved["status"], saved["link"]))
        for error, status in ((gcal.CalendarError("Google Calendar error (400)"), STATUS_FAILED),
                              (gcal.CalendarUnknownOutcome("no answer"), STATUS_UNKNOWN)):
            with self.subTest(status=status):
                self.work.refuse = error
                with self.assertLogs("briefing_reader", level="INFO"):
                    failed = run_undo(self.executor, action, outcome.result.undo, store=self.store)
                self.assertEqual(failed.status, status)
                self.assertEqual(self.store.get(action.id)["status"], saved["status"])

    def test_an_undo_jarvis_stopped_during_keeps_the_result_at_the_next_start(self) -> None:
        # S4: the file a crash mid-Undo leaves; the next start never turns it into UNKNOWN (whose Retry
        # would apply the change again) but keeps the forward result with "undo" = interrupted.
        for line in (MOVE_LINE, RSVP_LINE, CALENDAR_LINE):
            with self.subTest(line=line[:10]):
                action, outcome = self.carry(line)
                saved = self.store.get(action.id)
                snapshot = self.store.path.with_name("mid-undo.json")
                calendar = self.personal if line is CALENDAR_LINE else self.work
                original = calendar._undo

                def crash_here(*call: Any, original: Any = original) -> Any:
                    snapshot.write_bytes(self.store.path.read_bytes())   # the file as a crash now leaves it
                    return original(*call)

                calendar._undo = crash_here
                with self.assertLogs("briefing_reader", level="INFO"):
                    run_undo(self.executor, action, outcome.result.undo, store=self.store)
                calendar._undo = original
                on_disk = json.loads(snapshot.read_text(encoding="utf-8"))[action.id]
                self.assertEqual((on_disk["status"], on_disk["undo"]), (STATUS_RUNNING, saved["status"]))
                with self.assertLogs("briefing_reader.actions", level="WARNING") as logs:
                    entry = ActionStore(snapshot).get(action.id)
                self.assertEqual((entry["status"], entry["link"], entry["message"], entry["undo"]),
                                 (saved["status"], saved["link"], saved["message"], UNDO_INTERRUPTED))
                self.assertIn("1 Undo(s) were interrupted", "\n".join(logs.output))
        self.store.set("fwd1", STATUS_RUNNING, kind="move", account="work")   # a forward change: UNKNOWN as before
        with self.assertLogs("briefing_reader.actions", level="WARNING"):
            self.assertEqual(ActionStore(self.store.path).get("fwd1")["status"], STATUS_UNKNOWN)

    def test_an_undo_never_signs_in(self) -> None:
        action, outcome = self.carry(MOVE_LINE)
        self.work.is_signed_in = lambda: False
        with self.assertLogs("briefing_reader", level="INFO"):
            failed = run_undo(self.executor, action, outcome.result.undo, store=self.store)
        self.assertEqual(failed.status, STATUS_FAILED)
        self.assertNotIn("sign_in", [call[0] for call in self.work.calls])
        self.assertFalse(hasattr(executor_mod, "COUNTDOWN_ONLY_MESSAGE"))


if __name__ == "__main__":
    unittest.main()
