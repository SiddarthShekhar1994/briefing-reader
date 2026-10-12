"""Tests for briefing_reader.gcal: event bodies, matching, listing, sign-in, tokens and errors.

Nothing here touches the network or a browser. The Calendar service and the
OAuth flow are injected fakes, token refresh is patched, and every socket
connection raises :class:`NetworkUsed` (a BaseException, so no ``except
Exception`` in the code under test can swallow it). All token values are fake
and unique per test.
"""

from __future__ import annotations

import dataclasses
import http.client as http_client
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from google.auth.exceptions import RefreshError, TransportError
from google_auth_oauthlib.flow import WSGITimeoutError
from googleapiclient.errors import HttpError
from oauthlib.oauth2.rfc6749.errors import AccessDeniedError

from briefing_reader import config, gcal, google_auth
from briefing_reader.actions import CALENDAR, ProposedAction
from briefing_reader.config import REDACTED, RedactingFilter
from briefing_reader.gcal import (
    EVENT_FOOTER,
    LAST_RESORT_TIMEZONE,
    SCOPES,
    SIGN_IN_SUCCESS_MESSAGE,
    NO_TITLE,
    CalendarAuthError,
    CalendarError,
    CalendarEvent,
    CalendarNotSignedIn,
    CalendarSetupError,
    EventResult,
    GoogleCalendar,
    build_event_body,
    default_token_path,
)

GCAL_LOGGER = "briefing_reader.gcal"
CLIENT_ID = "1234567890-fakeclient.apps.googleusercontent.com"
TZ = "America/New_York"
EVENT_LINK = "https://www.google.com/calendar/event?eid=ZmFrZQ"


class NetworkUsed(BaseException):
    """Raised by the socket guard; a BaseException so broad handlers cannot hide it."""


def _no_network(*args: Any, **kwargs: Any) -> Any:
    raise NetworkUsed("a test tried to open a network connection")


def make_action(title: str = "Chess Club Weekly Meeting", *, start: datetime | None = None,
                end: datetime | None = None, all_day_start: date | None = None,
                all_day_end: date | None = None, rrule: str = "", where: str = "",
                notes: str = "", kind: str = CALENDAR, error: str = "") -> Any:
    if start is None and all_day_start is None and not error:
        start = datetime(2026, 10, 9, 15, 0)
        end = end or datetime(2026, 10, 9, 16, 0)
    return ProposedAction(
        id=uuid.uuid4().hex[:16], kind=kind, raw=f"Calendar: {title}", title=title,
        start=start, end=end, all_day_start=all_day_start, all_day_end=all_day_end,
        repeat="", rrule=rrule, where=where, notes=notes, error=error)


def http_error(status: int, message: str, reason: str = "", *, rpc_reason: str = "") -> HttpError:
    """An HttpError shaped like a real Calendar API error body."""
    error: dict[str, Any] = {"code": status, "message": message}
    if reason:
        error["errors"] = [{"domain": "global", "reason": reason, "message": message}]
    if rpc_reason:
        error["status"] = "PERMISSION_DENIED"
        error["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                             "reason": rpc_reason}]
    resp = SimpleNamespace(status=status, reason="HTTP reason")
    return HttpError(resp, json.dumps({"error": error}).encode("utf-8"),
                     uri="https://www.googleapis.com/calendar/v3/calendars/primary/events")


def utc_naive(delta: timedelta) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0) + delta


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class FakeRequest:
    def __init__(self, service: FakeService, name: str, kwargs: dict[str, Any]) -> None:
        self.service = service
        self.name = name
        self.kwargs = kwargs

    def execute(self, num_retries: int = 0) -> Any:
        self.service.calls.append((self.name, self.kwargs, num_retries))
        outcome = self.service.next_outcome(self.name, self.kwargs.get("calendarId"))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeService:
    """Records calls; ``outcomes[name]`` holds results or exceptions, consumed in order.

    ``outcomes["events.list@<calendar id>"]``, when present, answers that calendar only.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], int]] = []
        self.outcomes: dict[str, list[Any]] = {
            "settings.get": [{"kind": "calendar#setting", "id": "timezone", "value": TZ}],
            "events.list": [{"items": []}],
            "events.insert": [{"id": "evt123", "htmlLink": EVENT_LINK}],
        }

    def next_outcome(self, name: str, calendar_id: str | None = None) -> Any:
        queue = self.outcomes.get(f"{name}@{calendar_id}") or self.outcomes[name]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def calls_to(self, name: str) -> list[tuple[dict[str, Any], int]]:
        return [(kwargs, retries) for call, kwargs, retries in self.calls if call == name]

    def settings(self) -> Any:
        return SimpleNamespace(get=lambda **kw: FakeRequest(self, "settings.get", kw))

    def events(self) -> Any:
        return SimpleNamespace(list=lambda **kw: FakeRequest(self, "events.list", kw),
                               insert=lambda **kw: FakeRequest(self, "events.insert", kw),
                               get=lambda **kw: FakeRequest(self, "events.get", kw),
                               patch=lambda **kw: FakeRequest(self, "events.patch", kw),
                               delete=lambda **kw: FakeRequest(self, "events.delete", kw))


class FakeCreds:
    """What InstalledAppFlow.run_local_server returns; to_json is the real file format."""

    def __init__(self, token: str, refresh_token: str, client_secret: str) -> None:
        self.token = token
        self.refresh_token = refresh_token
        self.client_secret = client_secret
        self.valid = True

    def to_json(self) -> str:
        return json.dumps({
            "token": self.token, "refresh_token": self.refresh_token,
            "token_uri": "https://oauth2.googleapis.com/token", "client_id": CLIENT_ID,
            "client_secret": self.client_secret, "scopes": SCOPES,
            "expiry": f"{utc_naive(timedelta(hours=1)).isoformat()}Z"})


class FakeFlow:
    def __init__(self, result: Any = None, error: BaseException | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def run_local_server(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


# --------------------------------------------------------------------------
# Base
# --------------------------------------------------------------------------

class GcalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        for target, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                             (socket, "create_connection"), (socket, "getaddrinfo")):
            patcher = mock.patch.object(target, name, _no_network)
            patcher.start()
            self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.secret_path = self.root / "google_client_secret.json"
        self.token_path = self.root / "data" / "google_token.json"
        suffix = uuid.uuid4().hex
        self.client_secret = f"GOCSPX-fake-client-secret-{suffix}"
        self.access_token = f"ya29.fake-access-token-{suffix}"
        self.refresh_token = f"1//fake-refresh-token-{suffix}"
        self.service = FakeService()
        self.service_creds: list[Any] = []
        self.flow = FakeFlow(FakeCreds(f"ya29.fake-signed-in-token-{suffix}",
                                       f"1//fake-signed-in-refresh-{suffix}", self.client_secret))
        self.flow_args: list[tuple[Path, list[str]]] = []

    # -- builders --

    def write_client_secret(self, data: Any = None) -> None:
        if data is None:
            data = {"installed": {
                "client_id": CLIENT_ID, "client_secret": self.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"]}}
        text = data if isinstance(data, str) else json.dumps(data)
        self.secret_path.write_text(text, encoding="utf-8")

    def write_token(self, *, expiry: datetime | None = None, token: str | None = None) -> None:
        expiry = expiry or utc_naive(timedelta(hours=1))
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps({
            "token": token or self.access_token, "refresh_token": self.refresh_token,
            "token_uri": "https://oauth2.googleapis.com/token", "client_id": CLIENT_ID,
            "client_secret": self.client_secret, "scopes": SCOPES,
            "expiry": f"{expiry.isoformat()}Z"}), encoding="utf-8")

    def service_factory(self, creds: Any) -> FakeService:
        self.service_creds.append(creds)
        return self.service

    def flow_factory(self, path: Path, scopes: list[str]) -> FakeFlow:
        self.flow_args.append((path, scopes))
        return self.flow

    def make_calendar(self, **kwargs: Any) -> GoogleCalendar:
        return GoogleCalendar(client_secret_path=self.secret_path, token_path=self.token_path,
                              service_factory=self.service_factory,
                              flow_factory=self.flow_factory, **kwargs)

    def signed_in_calendar(self, **kwargs: Any) -> GoogleCalendar:
        self.write_client_secret()
        self.write_token()
        return self.make_calendar(**kwargs)

    @contextmanager
    def fails_with(self, error_type: type[CalendarError] = CalendarError) -> Iterator[Any]:
        """assertRaises(error_type) that also expects the failure to be logged once."""
        with self.assertLogs(GCAL_LOGGER, level="WARNING") as logs:
            with self.assertRaises(error_type) as ctx:
                yield ctx
        failures = [line for line in logs.output if "call failed" in line]
        self.assertEqual(len(failures), 1, logs.output)
        self.assertIn(str(ctx.exception), failures[0])

    def assert_redacted(self, *secrets: str) -> None:
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "values: %s",
                                   (" ".join(secrets),), None)
        RedactingFilter().filter(record)
        for secret in secrets:
            self.assertNotIn(secret, record.getMessage())
        self.assertIn(REDACTED, record.getMessage())


# --------------------------------------------------------------------------
# Module basics
# --------------------------------------------------------------------------

class ModuleTests(unittest.TestCase):
    def test_scopes_match_contract(self) -> None:
        self.assertEqual(SCOPES, ["https://www.googleapis.com/auth/calendar.events",
                                  "https://www.googleapis.com/auth/calendar.settings.readonly"])

    def test_error_hierarchy(self) -> None:
        self.assertTrue(issubclass(CalendarSetupError, CalendarError))
        self.assertTrue(issubclass(CalendarAuthError, CalendarError))
        self.assertTrue(issubclass(CalendarNotSignedIn, CalendarAuthError))
        self.assertEqual(CalendarError("x", status=500).status, 500)

    def test_default_token_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
            self.assertEqual(default_token_path(),
                             Path(tmp) / "briefing-reader" / "google_token.json")

    def test_import_is_lazy_and_qt_free(self) -> None:
        code = ("import sys, briefing_reader.gcal; "
                "print(sorted({m.split('.')[0] for m in sys.modules "
                "if m.split('.')[0] in ('PySide6', 'googleapiclient', 'google_auth_oauthlib', "
                "'oauthlib', 'httplib2')}))")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                cwd=config.PROJECT_ROOT, timeout=60, check=True)
        self.assertEqual(result.stdout.strip(), "[]")


# --------------------------------------------------------------------------
# Event bodies
# --------------------------------------------------------------------------

class EventBodyTests(unittest.TestCase):
    def test_the_footer_by_mode(self) -> None:
        """A day plan's block ends its description with its own footer (G24); every other card as before."""
        plan = dataclasses.replace(make_action(notes="For: Lab report"), mode="plan")
        self.assertEqual(gcal.event_footer(plan), "Added by Jarvis from your day plan.")
        self.assertEqual(gcal.event_footer(make_action()), EVENT_FOOTER)
        self.assertEqual(gcal.event_footer(dataclasses.replace(make_action(), mode="nudge")), EVENT_FOOTER)
        body = build_event_body(plan, TZ, footer=gcal.event_footer(plan))
        self.assertEqual(body["description"], "For: Lab report\n\nAdded by Jarvis from your day plan.")
        self.assertEqual(build_event_body(plan, TZ)["description"], f"For: Lab report\n\n{EVENT_FOOTER}")
        self.assertEqual(build_event_body(make_action(), TZ, footer="")["description"], EVENT_FOOTER)

    def test_timed_event(self) -> None:
        action = make_action(start=datetime(2026, 10, 9, 15, 0), end=datetime(2026, 10, 9, 16, 0))
        self.assertEqual(build_event_body(action, TZ), {
            "summary": "Chess Club Weekly Meeting",
            "description": EVENT_FOOTER,
            "reminders": {"useDefault": True},
            "start": {"dateTime": "2026-10-09T15:00:00", "timeZone": TZ},
            "end": {"dateTime": "2026-10-09T16:00:00", "timeZone": TZ},
        })

    def test_timed_event_without_usable_end_lasts_an_hour(self) -> None:
        for end in (None, datetime(2026, 10, 9, 15, 0), datetime(2026, 10, 9, 14, 0)):
            with self.subTest(end=end):
                action = make_action(start=datetime(2026, 10, 9, 23, 30), end=end)
                body = build_event_body(action, TZ)
                self.assertEqual(body["end"], {"dateTime": "2026-10-10T00:30:00", "timeZone": TZ})

    def test_overnight_end_is_kept(self) -> None:
        action = make_action(start=datetime(2026, 10, 9, 22, 0), end=datetime(2026, 10, 10, 1, 0))
        self.assertEqual(build_event_body(action, TZ)["end"]["dateTime"], "2026-10-10T01:00:00")

    def test_all_day_event_end_is_exclusive(self) -> None:
        action = make_action("HIST Essay #1 draft due", all_day_start=date(2026, 10, 5))
        body = build_event_body(action, TZ)
        self.assertEqual(body["start"], {"date": "2026-10-05"})
        self.assertEqual(body["end"], {"date": "2026-10-06"})
        self.assertNotIn("timeZone", body["start"])

    def test_multi_day_event_includes_last_day(self) -> None:
        action = make_action("Spring break", all_day_start=date(2027, 3, 22),
                             all_day_end=date(2027, 3, 26))
        body = build_event_body(action, TZ)
        self.assertEqual(body["start"], {"date": "2027-03-22"})
        self.assertEqual(body["end"], {"date": "2027-03-27"})

    def test_multi_day_crossing_month_and_year(self) -> None:
        action = make_action("Winter break", all_day_start=date(2026, 12, 21),
                             all_day_end=date(2026, 12, 31))
        self.assertEqual(build_event_body(action, TZ)["end"], {"date": "2027-01-01"})

    def test_recurring_rules_are_kept(self) -> None:
        cases = [
            (make_action(rrule="RRULE:FREQ=WEEKLY"), "RRULE:FREQ=WEEKLY"),
            (make_action(rrule="RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"),
             "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"),
            (make_action(rrule="RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=6"),
             "RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=6"),
            (make_action("Birthday", all_day_start=date(2026, 10, 5),
                         rrule="RRULE:FREQ=YEARLY;UNTIL=20301005"),
             "RRULE:FREQ=YEARLY;UNTIL=20301005"),
            (make_action("Standup", all_day_start=date(2026, 10, 5),
                         rrule="RRULE:FREQ=DAILY;UNTIL=20261211T235959Z"),
             "RRULE:FREQ=DAILY;UNTIL=20261211T235959Z"),
        ]
        for action, expected in cases:
            with self.subTest(rrule=action.rrule):
                self.assertEqual(build_event_body(action, TZ)["recurrence"], [expected])

    def test_rule_without_prefix_gets_one(self) -> None:
        body = build_event_body(make_action(rrule="FREQ=DAILY;COUNT=3"), TZ)
        self.assertEqual(body["recurrence"], ["RRULE:FREQ=DAILY;COUNT=3"])

    def test_one_off_has_no_recurrence(self) -> None:
        self.assertNotIn("recurrence", build_event_body(make_action(), TZ))

    def test_timed_end_of_day_until_moves_to_local_end_of_day(self) -> None:
        action = make_action(start=datetime(2026, 10, 9, 17, 0), end=datetime(2026, 10, 9, 18, 0),
                             rrule="RRULE:FREQ=WEEKLY;UNTIL=20261211T235959Z")
        local_end = datetime(2026, 12, 12, 7, 59, 59, tzinfo=timezone.utc)   # 23:59:59 PST
        with mock.patch.object(gcal, "_local_end_of_day_utc", return_value=local_end) as conv:
            body = build_event_body(action, TZ)
        conv.assert_called_once_with(date(2026, 12, 11))
        self.assertEqual(body["recurrence"], ["RRULE:FREQ=WEEKLY;UNTIL=20261212T075959Z"])

    def test_local_end_of_day_uses_machine_rules(self) -> None:
        result = gcal._local_end_of_day_utc(date(2026, 12, 11))
        expected = datetime(2026, 12, 11, 23, 59, 59).astimezone(timezone.utc)
        self.assertEqual(result, expected)
        self.assertEqual(result.tzinfo, timezone.utc)

    def test_location_and_notes(self) -> None:
        action = make_action(where=" https://meet.google.com/aaa-bbbb-ccc ",
                             notes=" Carol's team sync ")
        body = build_event_body(action, TZ)
        self.assertEqual(body["location"], "https://meet.google.com/aaa-bbbb-ccc")
        self.assertEqual(body["description"],
                         "Carol's team sync\n\nAdded by Jarvis from your Daily Briefing.")

    def test_empty_location_is_left_out(self) -> None:
        self.assertNotIn("location", build_event_body(make_action(where="   "), TZ))

    def test_not_actionable_is_refused(self) -> None:
        for action in (make_action(kind="reply"),
                       make_action(error="could not read the date"),
                       ProposedAction(id="x" * 16, kind=CALENDAR, raw="Calendar: x", title="x")):
            with self.subTest(action=action), self.assertRaises(CalendarError):
                build_event_body(action, TZ)


# --------------------------------------------------------------------------
# Time zone
# --------------------------------------------------------------------------

class TimezoneTests(GcalTestCase):
    PC_ZONE = "Europe/Berlin"   # what this PC's own time zone is said to be

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(gcal, "local_timezone", return_value=self.PC_ZONE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reads_settings_once(self) -> None:
        cal = self.signed_in_calendar()
        self.assertEqual(cal.timezone(), TZ)
        self.assertEqual(cal.timezone(), TZ)
        cal.create_event(make_action())
        calls = self.service.calls_to("settings.get")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], {"setting": "timezone"})
        inserted = self.service.calls_to("events.insert")[0][0]["body"]
        self.assertEqual(inserted["start"]["timeZone"], TZ)

    def test_falls_back_with_warning_and_retries_later(self) -> None:
        self.service.outcomes["settings.get"] = [http_error(500, "Backend Error"),
                                                 {"value": TZ}]
        cal = self.signed_in_calendar()
        with self.assertLogs(GCAL_LOGGER, level="WARNING") as captured:
            self.assertEqual(cal.timezone(), self.PC_ZONE)
        self.assertIn(self.PC_ZONE, "\n".join(captured.output))
        self.assertEqual(cal.timezone(), TZ)   # the fallback was not cached
        self.assertTrue(self.token_path.exists())

    def test_falls_back_when_setting_is_missing(self) -> None:
        self.service.outcomes["settings.get"] = [{"id": "timezone"}]
        cal = self.signed_in_calendar()
        with self.assertLogs(GCAL_LOGGER, level="WARNING") as captured:
            self.assertEqual(cal.timezone(), self.PC_ZONE)
        self.assertIn(self.PC_ZONE, "\n".join(captured.output))

    def test_settings_403_falls_back_and_keeps_token(self) -> None:
        self.service.outcomes["settings.get"] = [
            http_error(403, "Request had insufficient authentication scopes.",
                       rpc_reason="ACCESS_TOKEN_SCOPE_INSUFFICIENT")]
        cal = self.signed_in_calendar()
        with self.assertLogs(GCAL_LOGGER, level="WARNING"):
            self.assertEqual(cal.timezone(), self.PC_ZONE)
        self.assertTrue(self.token_path.exists())

    def test_network_failure_falls_back(self) -> None:
        self.service.outcomes["settings.get"] = [ConnectionResetError("reset by peer")]
        cal = self.signed_in_calendar()
        with self.assertLogs(GCAL_LOGGER, level="WARNING"):
            self.assertEqual(cal.timezone(), self.PC_ZONE)

    def test_event_is_created_in_the_pc_zone_when_the_setting_cannot_be_read(self) -> None:
        self.service.outcomes["settings.get"] = [http_error(500, "Backend Error")]
        cal = self.signed_in_calendar()
        with self.assertLogs(GCAL_LOGGER, level="WARNING"):
            cal.create_event(make_action())
        inserted = self.service.calls_to("events.insert")[0][0]["body"]
        self.assertEqual(inserted["start"]["timeZone"], self.PC_ZONE)
        self.assertEqual(inserted["end"]["timeZone"], self.PC_ZONE)

    def test_settings_401_is_an_auth_error(self) -> None:
        self.service.outcomes["settings.get"] = [
            http_error(401, "Invalid Credentials", "authError")]
        cal = self.signed_in_calendar()
        with self.fails_with(CalendarAuthError):
            cal.timezone()
        self.assertFalse(self.token_path.exists())


class LocalTimezoneTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(gcal, "_local_timezone", None)   # no cached value
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_uses_the_name_windows_gives_and_caches_it(self) -> None:
        with mock.patch.object(gcal, "_icu_host_timezone", return_value="Asia/Kolkata") as icu:
            self.assertEqual(gcal.local_timezone(), "Asia/Kolkata")
            self.assertEqual(gcal.local_timezone(), "Asia/Kolkata")
        self.assertEqual(icu.call_count, 1)

    def test_falls_back_to_utc(self) -> None:
        with mock.patch.object(gcal, "_icu_host_timezone", return_value=None):
            self.assertEqual(gcal.local_timezone(), LAST_RESORT_TIMEZONE)
        self.assertEqual(LAST_RESORT_TIMEZONE, "UTC")

    def test_missing_icu_gives_none(self) -> None:
        import ctypes

        with mock.patch.object(ctypes, "WinDLL", side_effect=OSError("no icu.dll"), create=True):
            self.assertIsNone(gcal._icu_host_timezone())

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_windows_gives_an_iana_name(self) -> None:
        name = gcal._icu_host_timezone()
        if name is None:
            self.skipTest("this Windows has no usable icu.dll")
        self.assertRegex(name, r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+)*$")
        self.assertNotEqual(name, "Etc/Unknown")


# --------------------------------------------------------------------------
# Finding events that are already there
# --------------------------------------------------------------------------

def _timed_event(summary: str, start: str, event_id: str = "old1") -> dict[str, Any]:
    return {"id": event_id, "summary": summary, "status": "confirmed", "htmlLink": EVENT_LINK,
            "start": {"dateTime": start, "timeZone": TZ}}


class FindExistingTests(GcalTestCase):
    def test_list_query(self) -> None:
        cal = self.signed_in_calendar(calendar_id="team@group.calendar.google.com")
        self.assertIsNone(cal.find_existing(make_action()))
        (query, retries), = self.service.calls_to("events.list")
        self.assertEqual(query, {
            "calendarId": "team@group.calendar.google.com",
            "timeMin": "2026-10-08T15:00:00Z", "timeMax": "2026-10-10T15:00:00Z",
            "singleEvents": True, "q": "Chess Club Weekly Meeting", "timeZone": TZ, "maxResults": 50})
        self.assertGreater(retries, 0)

    def test_all_day_query_window(self) -> None:
        cal = self.signed_in_calendar()
        cal.find_existing(make_action("Due", all_day_start=date(2026, 10, 5)))
        (query, _), = self.service.calls_to("events.list")
        self.assertEqual((query["timeMin"], query["timeMax"]),
                         ("2026-10-04T00:00:00Z", "2026-10-07T00:00:00Z"))

    def test_matches_title_casefold_and_start(self) -> None:
        match = _timed_event("  chess club WEEKLY meeting ", "2026-10-09T15:00:00-04:00", "hit")
        self.service.outcomes["events.list"] = [{"items": [
            _timed_event("Chess Club Weekly Meeting", "2026-10-09T16:00:00-04:00", "other-start"),
            _timed_event("Chess Club Weekly Meeting (moved)", "2026-10-09T15:00:00-04:00", "other-title"),
            dict(match, id="cancelled", status="cancelled"),
            match,
        ]}]
        cal = self.signed_in_calendar()
        self.assertEqual(cal.find_existing(make_action())["id"], "hit")

    def test_no_match_returns_none(self) -> None:
        self.service.outcomes["events.list"] = [{"items": [
            _timed_event("Chess Club Weekly Meeting", "2026-10-16T15:00:00-04:00"),
            {"id": "all-day", "summary": "Chess Club Weekly Meeting", "start": {"date": "2026-10-09"}},
            {"id": "broken", "summary": "Chess Club Weekly Meeting", "start": {"dateTime": "soon"}},
            {"id": "no-start", "summary": "Chess Club Weekly Meeting"},
        ]}]
        cal = self.signed_in_calendar()
        self.assertIsNone(cal.find_existing(make_action()))

    def test_all_day_match(self) -> None:
        self.service.outcomes["events.list"] = [{"items": [
            {"id": "timed", "summary": "Spring break",
             "start": {"dateTime": "2027-03-22T00:00:00Z"}},
            {"id": "day", "summary": "spring break", "start": {"date": "2027-03-22"}},
        ]}]
        cal = self.signed_in_calendar()
        action = make_action("Spring break", all_day_start=date(2027, 3, 22),
                             all_day_end=date(2027, 3, 26))
        self.assertEqual(cal.find_existing(action)["id"], "day")

    def test_follows_next_page(self) -> None:
        self.service.outcomes["events.list"] = [
            {"items": [_timed_event("Something else", "2026-10-09T15:00:00-04:00")],
             "nextPageToken": "page2"},
            {"items": [_timed_event("Chess Club Weekly Meeting", "2026-10-09T15:00:00-04:00", "p2")]},
        ]
        cal = self.signed_in_calendar()
        self.assertEqual(cal.find_existing(make_action())["id"], "p2")
        queries = [query for query, _ in self.service.calls_to("events.list")]
        self.assertNotIn("pageToken", queries[0])
        self.assertEqual(queries[1]["pageToken"], "page2")

    def test_existing_event_is_not_created_again(self) -> None:
        self.service.outcomes["events.list"] = [{"items": [
            _timed_event("Chess Club Weekly Meeting", "2026-10-09T15:00:00-04:00", "already")]}]
        cal = self.signed_in_calendar()
        with self.assertLogs(GCAL_LOGGER, level="INFO") as captured:
            result = cal.create_event(make_action())
        self.assertEqual(result, EventResult(event_id="already", link=EVENT_LINK, existed=True))
        self.assertEqual(self.service.calls_to("events.insert"), [])
        self.assertIn("already on the calendar", "\n".join(captured.output))


# --------------------------------------------------------------------------
# Creating events
# --------------------------------------------------------------------------

class CreateEventTests(GcalTestCase):
    def test_creates_event(self) -> None:
        cal = self.signed_in_calendar()
        action = make_action(where="Central Library, Media Lab", notes="Bring notes",
                             rrule="RRULE:FREQ=WEEKLY;COUNT=4")
        with self.assertLogs(GCAL_LOGGER, level="INFO") as captured:
            result = cal.create_event(action)
        self.assertEqual(result, EventResult(event_id="evt123", link=EVENT_LINK, existed=False))
        (kwargs, retries), = self.service.calls_to("events.insert")
        self.assertEqual(retries, 0)   # an insert is never retried blindly
        self.assertEqual(kwargs["calendarId"], "primary")
        self.assertEqual(kwargs["body"], build_event_body(action, TZ))
        self.assertEqual(kwargs["body"]["location"], "Central Library, Media Lab")
        self.assertEqual(kwargs["body"]["recurrence"], ["RRULE:FREQ=WEEKLY;COUNT=4"])
        self.assertIn("created event evt123", "\n".join(captured.output))

    def test_creates_a_plan_block_with_its_footer(self) -> None:
        cal = self.signed_in_calendar()
        cal.create_event(make_action(notes="For: Lab report"), footer=gcal.PLAN_EVENT_FOOTER)
        (kwargs, _retries), = self.service.calls_to("events.insert")
        self.assertEqual(kwargs["body"]["description"], "For: Lab report\n\nAdded by Jarvis from your day plan.")
        self.assertEqual(self.flow.calls, [])

    def test_check_runs_before_insert(self) -> None:
        cal = self.signed_in_calendar()
        cal.create_event(make_action())
        names = [name for name, _, _ in self.service.calls]
        self.assertEqual(names, ["settings.get", "events.list", "events.insert"])

    def test_custom_calendar_id(self) -> None:
        cal = self.signed_in_calendar(calendar_id="team@group.calendar.google.com")
        cal.create_event(make_action())
        (kwargs, _), = self.service.calls_to("events.insert")
        self.assertEqual(kwargs["calendarId"], "team@group.calendar.google.com")
        self.assertEqual(cal.calendar_id, "team@group.calendar.google.com")

    def test_service_is_built_once(self) -> None:
        cal = self.signed_in_calendar()
        cal.create_event(make_action("One"))
        cal.create_event(make_action("Two"))
        self.assertEqual(len(self.service_creds), 1)

    def test_not_actionable_is_refused_before_any_request(self) -> None:
        cal = self.signed_in_calendar()
        with self.fails_with() as ctx:
            cal.create_event(make_action(kind="reply"))
        self.assertNotIsInstance(ctx.exception, (CalendarAuthError, CalendarSetupError))
        self.assertEqual(self.service.calls, [])

    def test_no_secret_in_logs(self) -> None:
        self.write_client_secret()
        cal = self.make_calendar()
        with self.assertLogs(GCAL_LOGGER, level="DEBUG") as captured:
            cal.create_event(make_action())
        text = "\n".join(captured.output)
        for secret in (self.client_secret, self.flow.result.token, self.flow.result.refresh_token):
            self.assertNotIn(secret, text)


# --------------------------------------------------------------------------
# Sign-in
# --------------------------------------------------------------------------

class SignInTests(GcalTestCase):
    def test_runs_installed_app_flow(self) -> None:
        self.write_client_secret()
        cal = self.make_calendar(open_timeout_s=120)
        with self.assertLogs(GCAL_LOGGER, level="INFO") as captured:
            cal.sign_in()
        self.assertEqual(self.flow_args, [(self.secret_path, SCOPES)])
        (kwargs,) = self.flow.calls
        self.assertEqual(kwargs["port"], 0)
        self.assertIs(kwargs["open_browser"], True)
        self.assertEqual(kwargs["timeout_seconds"], 120)
        self.assertEqual(kwargs["authorization_prompt_message"], "")
        self.assertEqual(kwargs["success_message"], SIGN_IN_SUCCESS_MESSAGE)
        self.assertEqual(SIGN_IN_SUCCESS_MESSAGE,
                         "Jarvis is connected to Google Calendar. You can close this tab.")
        self.assertIn("Google Calendar: signed in", "\n".join(captured.output))

    def test_default_timeout_is_five_minutes(self) -> None:
        self.write_client_secret()
        self.make_calendar().sign_in()
        self.assertEqual(self.flow.calls[0]["timeout_seconds"], 300)

    def test_saves_token_atomically(self) -> None:
        self.write_client_secret()
        cal = self.make_calendar()
        self.assertFalse(cal.is_signed_in())
        with mock.patch.object(gcal.os, "replace", wraps=os.replace) as replace:
            cal.sign_in()
        replace.assert_called_once()
        source, target = replace.call_args.args
        self.assertEqual(Path(target), self.token_path)
        self.assertNotEqual(Path(source), self.token_path)
        self.assertEqual(Path(source).parent, self.token_path.parent)
        saved = json.loads(self.token_path.read_text(encoding="utf-8"))
        expected = json.loads(self.flow.result.to_json())
        expected["expiry"] = saved["expiry"]   # to_json() stamps the time it is called
        self.assertEqual(saved, expected)
        self.assertEqual(list(self.token_path.parent.iterdir()), [self.token_path])
        self.assertTrue(cal.is_signed_in())

    def test_registers_tokens_and_client_secret_for_redaction(self) -> None:
        self.write_client_secret()
        creds = self.flow.result
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "%s", (creds.token,), None)
        RedactingFilter().filter(record)
        self.assertIn(creds.token, record.getMessage())   # not registered yet
        self.make_calendar().sign_in()
        self.assert_redacted(creds.token, creds.refresh_token, self.client_secret)

    def test_signs_in_after_previous_token_was_lost(self) -> None:
        self.write_client_secret()
        cal = self.make_calendar()
        result = cal.create_event(make_action())
        self.assertFalse(result.existed)
        self.assertEqual(len(self.flow.calls), 1)
        self.assertIs(self.service_creds[0], self.flow.result)
        self.assertTrue(self.token_path.exists())

    def test_save_failure_is_logged_and_sign_in_still_works(self) -> None:
        self.write_client_secret()
        blocker = self.root / "blocker"
        blocker.write_text("a file, not a folder", encoding="utf-8")
        cal = GoogleCalendar(client_secret_path=self.secret_path,
                             token_path=blocker / "google_token.json",
                             service_factory=self.service_factory, flow_factory=self.flow_factory)
        with self.assertLogs(GCAL_LOGGER, level="WARNING") as captured:
            cal.sign_in()
        self.assertIn("could not save", "\n".join(captured.output))
        self.assertEqual(cal.create_event(make_action()).event_id, "evt123")

    def test_timeout_is_an_auth_error(self) -> None:
        self.write_client_secret()
        self.flow.error = WSGITimeoutError(
            "Timed out waiting for response from authorization server")
        with self.fails_with(CalendarAuthError) as ctx:
            self.make_calendar().sign_in()
        self.assertEqual(str(ctx.exception), "Google sign-in timed out")
        self.assertFalse(self.token_path.exists())

    def test_denied_is_an_auth_error(self) -> None:
        self.write_client_secret()
        self.flow.error = AccessDeniedError(description="The user denied access")
        with self.fails_with(CalendarAuthError) as ctx:
            self.make_calendar().sign_in()
        self.assertIn("denied", str(ctx.exception))

    def test_unticked_permission_is_an_auth_error(self) -> None:
        self.write_client_secret()
        warning = Warning('Scope has changed from "a b" to "a".')
        warning.token = {"access_token": self.access_token}  # type: ignore[attr-defined]
        self.flow.error = warning
        with self.fails_with(CalendarAuthError) as ctx:
            self.make_calendar().sign_in()
        self.assertNotIn(self.access_token, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)

    def test_other_failure_is_a_calendar_error_without_details(self) -> None:
        self.write_client_secret()
        self.flow.error = RuntimeError(f"boom with {self.access_token}")
        with self.fails_with() as ctx:
            self.make_calendar().sign_in()
        self.assertNotIsInstance(ctx.exception, CalendarAuthError)
        self.assertEqual(str(ctx.exception), "Google sign-in failed (RuntimeError)")


# --------------------------------------------------------------------------
# Setup (client secret file)
# --------------------------------------------------------------------------

class SetupTests(GcalTestCase):
    def test_not_configured(self) -> None:
        cal = self.make_calendar()
        self.assertFalse(cal.is_configured())
        self.assertFalse(cal.is_signed_in())
        for call in (cal.sign_in, lambda: cal.create_event(make_action()),
                     lambda: cal.find_existing(make_action())):
            with self.subTest(call=call), self.fails_with(CalendarSetupError) as ctx:
                call()
            self.assertIn("README step 8", str(ctx.exception))
        self.assertEqual(self.flow.calls, [])
        self.assertEqual(self.service_creds, [])

    def test_saved_token_without_client_secret_is_still_not_configured(self) -> None:
        self.write_token()
        cal = self.make_calendar()
        self.assertTrue(cal.is_signed_in())
        with self.fails_with(CalendarSetupError):
            cal.create_event(make_action())
        self.assertEqual(self.service.calls, [])

    def test_invalid_client_secret_files(self) -> None:
        cases = {
            "not json": "{nope",
            "web client": {"web": {"client_id": CLIENT_ID, "client_secret": self.client_secret}},
            "no secret": {"installed": {"client_id": CLIENT_ID}},
            "a list": [1, 2],
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                self.write_client_secret(data)
                cal = self.make_calendar()
                self.assertTrue(cal.is_configured())
                with self.fails_with(CalendarSetupError) as ctx:
                    cal.sign_in()
                self.assertIn("README step 8", str(ctx.exception))
                self.assertNotIn(self.client_secret, str(ctx.exception))
        self.assertEqual(self.flow.calls, [])

    def test_utf8_bom_client_secret_is_accepted(self) -> None:
        self.write_client_secret()
        self.secret_path.write_text(self.secret_path.read_text(encoding="utf-8"),
                                    encoding="utf-8-sig")
        self.make_calendar().sign_in()
        self.assertEqual(len(self.flow.calls), 1)

    def test_flow_factory_failure_is_a_setup_error(self) -> None:
        self.write_client_secret()

        def broken(path: Path, scopes: list[str]) -> Any:
            raise ValueError("Client secrets must be for a web or installed app.")

        cal = GoogleCalendar(client_secret_path=self.secret_path, token_path=self.token_path,
                             service_factory=self.service_factory, flow_factory=broken)
        with self.fails_with(CalendarSetupError):
            cal.sign_in()

    def test_missing_google_packages(self) -> None:
        cal = self.signed_in_calendar()
        with mock.patch.object(gcal, "google_libraries_available", return_value=False):
            self.assertFalse(cal.is_signed_in())
            with self.fails_with(CalendarSetupError) as ctx:
                cal.create_event(make_action())
        self.assertIn("requirements.txt", str(ctx.exception))

    def test_google_packages_are_installed(self) -> None:
        self.assertTrue(gcal.google_libraries_available())


# --------------------------------------------------------------------------
# Saved token: load and refresh
# --------------------------------------------------------------------------

class TokenTests(GcalTestCase):
    def test_valid_token_is_loaded_and_registered(self) -> None:
        cal = self.signed_in_calendar()
        self.assertTrue(cal.is_signed_in())
        cal.create_event(make_action())
        self.assertEqual(self.flow.calls, [])
        (creds,) = self.service_creds
        self.assertEqual(creds.token, self.access_token)
        self.assertEqual(creds.refresh_token, self.refresh_token)
        self.assert_redacted(self.access_token, self.refresh_token, self.client_secret)

    def test_expired_token_is_refreshed_and_saved(self) -> None:
        self.write_client_secret()
        self.write_token(expiry=utc_naive(timedelta(hours=-2)))
        new_token = f"ya29.fake-refreshed-token-{uuid.uuid4().hex}"

        def refresh(creds: Any) -> None:
            creds.token = new_token
            creds.expiry = utc_naive(timedelta(hours=1))

        cal = self.make_calendar()
        self.assertTrue(cal.is_signed_in())   # expired but refreshable
        with mock.patch.object(gcal, "_refresh_credentials", side_effect=refresh) as patched:
            cal.create_event(make_action())
        patched.assert_called_once()
        self.assertEqual(self.service_creds[0].token, new_token)
        saved = json.loads(self.token_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["token"], new_token)
        self.assertEqual(saved["refresh_token"], self.refresh_token)
        self.assert_redacted(new_token)
        self.assertEqual(self.flow.calls, [])

    def test_revoked_refresh_token_is_deleted(self) -> None:
        self.write_client_secret()
        self.write_token(expiry=utc_naive(timedelta(hours=-2)))
        error = RefreshError("invalid_grant: Token has been expired or revoked.",
                             {"error": "invalid_grant"})
        cal = self.make_calendar()
        with mock.patch.object(gcal, "_refresh_credentials", side_effect=error), \
                self.fails_with(CalendarAuthError):
            cal.create_event(make_action())
        self.assertFalse(self.token_path.exists())
        self.assertFalse(cal.is_signed_in())
        self.assertEqual(self.service.calls, [])

    def test_retryable_refresh_failure_keeps_token(self) -> None:
        self.write_client_secret()
        self.write_token(expiry=utc_naive(timedelta(hours=-2)))
        error = RefreshError("temporarily unavailable", retryable=True)
        with mock.patch.object(gcal, "_refresh_credentials", side_effect=error), \
                self.fails_with() as ctx:
            self.make_calendar().create_event(make_action())
        self.assertNotIsInstance(ctx.exception, CalendarAuthError)
        self.assertTrue(self.token_path.exists())

    def test_offline_refresh_keeps_token(self) -> None:
        self.write_client_secret()
        self.write_token(expiry=utc_naive(timedelta(hours=-2)))
        error = TransportError(f"connection failed for {self.refresh_token}")
        with mock.patch.object(gcal, "_refresh_credentials", side_effect=error), \
                self.fails_with() as ctx:
            self.make_calendar().create_event(make_action())
        self.assertNotIsInstance(ctx.exception, CalendarAuthError)
        self.assertIn("TransportError", str(ctx.exception))
        self.assertNotIn(self.refresh_token, str(ctx.exception))
        self.assertTrue(self.token_path.exists())

    def test_corrupt_token_means_sign_in_again(self) -> None:
        self.write_client_secret()
        self.token_path.parent.mkdir(parents=True)
        for text in ("{broken", "[]", json.dumps({"token": "only-an-access-token"})):
            with self.subTest(text=text):
                self.token_path.write_text(text, encoding="utf-8")
                self.assertFalse(self.make_calendar().is_signed_in())
        with self.assertLogs(GCAL_LOGGER, level="WARNING"):
            self.make_calendar().create_event(make_action())
        self.assertEqual(len(self.flow.calls), 1)
        saved = json.loads(self.token_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["token"], self.flow.result.token)


# --------------------------------------------------------------------------
# API errors
# --------------------------------------------------------------------------

class ApiErrorTests(GcalTestCase):
    def insert_fails_with(self, error: BaseException) -> CalendarError:
        self.service.outcomes["events.insert"] = [error]
        cal = self.signed_in_calendar()
        with self.fails_with() as ctx:
            cal.create_event(make_action())
        return ctx.exception

    def test_401_is_auth_error_and_deletes_token(self) -> None:
        error = self.insert_fails_with(http_error(401, "Invalid Credentials", "authError"))
        self.assertIsInstance(error, CalendarAuthError)
        self.assertEqual(error.status, 401)
        self.assertIn("401", str(error))
        self.assertFalse(self.token_path.exists())

    def test_403_is_auth_error_and_deletes_token(self) -> None:
        cases = [
            http_error(403, "Request had insufficient authentication scopes.",
                       rpc_reason="ACCESS_TOKEN_SCOPE_INSUFFICIENT"),
            http_error(403, "You need to have writer access to this calendar.",
                       "requiredAccessLevel"),
        ]
        for http in cases:
            with self.subTest(reason=http.reason):
                error = self.insert_fails_with(http)
                self.assertIsInstance(error, CalendarAuthError)
                self.assertEqual(error.status, 403)
                self.assertIn(http.reason, str(error))
                self.assertFalse(self.token_path.exists())

    def test_auth_error_then_next_call_signs_in_again(self) -> None:
        self.service.outcomes["events.insert"] = [http_error(401, "Invalid Credentials"),
                                                  {"id": "evt2", "htmlLink": EVENT_LINK}]
        cal = self.signed_in_calendar()
        with self.fails_with(CalendarAuthError):
            cal.create_event(make_action())
        self.assertEqual(cal.create_event(make_action()).event_id, "evt2")
        self.assertEqual(len(self.flow.calls), 1)

    def test_rate_limit_403_is_not_an_auth_error(self) -> None:
        error = self.insert_fails_with(http_error(403, "Rate Limit Exceeded", "rateLimitExceeded"))
        self.assertNotIsInstance(error, (CalendarAuthError, CalendarSetupError))
        self.assertEqual(error.status, 403)
        self.assertTrue(self.token_path.exists())

    def test_disabled_api_is_a_setup_error(self) -> None:
        error = self.insert_fails_with(http_error(
            403, "Google Calendar API has not been used in project 123 before or it is disabled.",
            "accessNotConfigured"))
        self.assertIsInstance(error, CalendarSetupError)
        self.assertIn("README step 8", str(error))
        self.assertTrue(self.token_path.exists())

    def test_other_status_is_calendar_error_with_google_message(self) -> None:
        error = self.insert_fails_with(http_error(500, "Backend Error", "backendError"))
        self.assertNotIsInstance(error, (CalendarAuthError, CalendarSetupError))
        self.assertEqual(error.status, 500)
        self.assertIn("500", str(error))
        self.assertIn("Backend Error", str(error))
        self.assertIsNone(error.__cause__)
        self.assertTrue(error.__suppress_context__)
        self.assertTrue(self.token_path.exists())

    def test_error_message_never_contains_secrets(self) -> None:
        message = f"Bad request near {self.access_token} and {self.client_secret}"
        error = self.insert_fails_with(http_error(400, message, "badRequest"))
        text = str(error)
        self.assertIn("400", text)
        self.assertNotIn(self.access_token, text)
        self.assertNotIn(self.client_secret, text)
        self.assertIn(REDACTED, text)

    def test_long_google_message_is_shortened(self) -> None:
        error = self.insert_fails_with(http_error(400, "x" * 1000))
        self.assertLess(len(str(error)), 300)

    def test_network_error_is_calendar_error_with_type_only(self) -> None:
        error = self.insert_fails_with(ConnectionResetError(f"reset; header {self.access_token}"))
        self.assertNotIsInstance(error, CalendarAuthError)
        self.assertIn("ConnectionResetError", str(error))
        self.assertNotIn(self.access_token, str(error))
        self.assertTrue(self.token_path.exists())

    def test_refresh_rejected_during_request_deletes_token(self) -> None:
        error = self.insert_fails_with(RefreshError("invalid_grant: Token has been revoked."))
        self.assertIsInstance(error, CalendarAuthError)
        self.assertFalse(self.token_path.exists())

    def test_list_error_stops_before_insert(self) -> None:
        self.service.outcomes["events.list"] = [http_error(503, "Service Unavailable")]
        cal = self.signed_in_calendar()
        with self.fails_with() as ctx:
            cal.create_event(make_action())
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(self.service.calls_to("events.insert"), [])

    def test_an_insert_that_may_have_happened_is_unknown_and_never_retried(self) -> None:
        for error in (http_error(500, "Backend Error", "backendError"), http_error(503, "Service Unavailable"),
                      socket.timeout("timed out"), ConnectionResetError("reset"),
                      google_auth.RequestNotResent("not sent twice")):
            with self.subTest(error=type(error).__name__):
                self.service = FakeService()
                self.service.outcomes["events.insert"] = [error]
                cal = self.signed_in_calendar()
                with self.fails_with(gcal.CalendarUnknownOutcome) as ctx:
                    cal.create_event(make_action())
                self.assertIn("check the calendar before retrying", str(ctx.exception))
                self.assertEqual([retries for _, retries in self.service.calls_to("events.insert")], [0])
        refused = self.insert_fails_with(http_error(400, "Bad Request", "badRequest"))
        self.assertNotIsInstance(refused, gcal.CalendarUnknownOutcome)
        not_sent = self.insert_fails_with(socket.gaierror("no dns"))
        self.assertNotIsInstance(not_sent, gcal.CalendarUnknownOutcome)

    def test_a_retry_after_an_unknown_insert_finds_the_event(self) -> None:
        action = make_action()
        self.service.outcomes["events.insert"] = [socket.timeout("timed out")]
        cal = self.signed_in_calendar()
        with self.fails_with(gcal.CalendarUnknownOutcome):
            cal.create_event(action)
        start = action.start.strftime("%Y-%m-%dT%H:%M:%S") + "-07:00"
        self.service.outcomes["events.list"] = [{"items": [{"id": "evt9", "summary": action.title,
                                                            "htmlLink": EVENT_LINK, "start": {"dateTime": start}}]}]
        result = cal.create_event(action)
        self.assertEqual((result.event_id, result.existed), ("evt9", True))
        self.assertEqual(len(self.service.calls_to("events.insert")), 1)

    def test_without_interactive_a_missing_sign_in_creates_nothing_and_opens_nothing(self) -> None:
        self.write_client_secret()
        cal = self.make_calendar()
        with self.assertLogs(GCAL_LOGGER, level="INFO"), self.assertRaises(CalendarNotSignedIn):
            cal.create_event(make_action(), interactive=False)
        self.assertEqual((self.flow.calls, self.service.calls), ([], []))
        signed = self.signed_in_calendar()
        self.assertEqual(signed.create_event(make_action(), interactive=False).event_id, "evt123")
        self.assertEqual(self.flow.calls, [])


# --------------------------------------------------------------------------
# Listing events (the reading window's agenda)
# --------------------------------------------------------------------------

PDT = timezone(timedelta(hours=-7), "PDT")
DAY_START = datetime(2026, 10, 5, 0, 0, tzinfo=PDT)
DAY_END = datetime(2026, 10, 6, 0, 0, tzinfo=PDT)
TEAM = "team@group.calendar.google.com"


def _listed(summary: str, start: str, end: str | None = None, uid: str = "",
            **extra: Any) -> dict[str, Any]:
    """An events.list item for a timed event."""
    item: dict[str, Any] = {"id": uid or uuid.uuid4().hex, "status": "confirmed",
                            "summary": summary, "start": {"dateTime": start, "timeZone": TZ}}
    if end is not None:
        item["end"] = {"dateTime": end, "timeZone": TZ}
    if uid:
        item["iCalUID"] = f"{uid}@google.com"
    item.update(extra)
    return item


def _listed_all_day(summary: str, first: str, after: str, uid: str = "",
                    **extra: Any) -> dict[str, Any]:
    """An events.list item for an all-day event (``after`` is Google's exclusive end)."""
    item: dict[str, Any] = {"id": uid or uuid.uuid4().hex, "status": "confirmed",
                            "summary": summary, "start": {"date": first}, "end": {"date": after}}
    if uid:
        item["iCalUID"] = f"{uid}@google.com"
    item.update(extra)
    return item


class CalendarEventTests(unittest.TestCase):
    def test_defaults_and_all_day(self) -> None:
        timed = CalendarEvent("Lab", start=DAY_START, end=DAY_END)
        self.assertFalse(timed.all_day)
        self.assertEqual((timed.location, timed.link, timed.calendar_id), ("", "", "primary"))
        self.assertTrue(CalendarEvent("Holiday", all_day_start=date(2026, 10, 5),
                                      all_day_end=date(2026, 10, 5)).all_day)


class ListEventsTests(GcalTestCase):
    def assert_no_sign_in(self) -> None:
        self.assertEqual(self.flow.calls, [])
        self.assertEqual(self.flow_args, [])

    def test_list_query(self) -> None:
        cal = self.signed_in_calendar()
        self.assertEqual(cal.list_events(DAY_START, DAY_END), [])
        (query, retries), = self.service.calls_to("events.list")
        self.assertEqual(query, {
            "calendarId": "primary",
            "timeMin": "2026-10-05T00:00:00-07:00", "timeMax": "2026-10-06T00:00:00-07:00",
            "singleEvents": True, "orderBy": "startTime", "maxResults": 50,
            "showDeleted": False})
        self.assertGreater(retries, 0)
        self.assertEqual(self.service.calls_to("settings.get"), [])
        self.assertEqual(self.service.calls_to("events.insert"), [])
        self.assert_no_sign_in()

    def test_bounds_are_rfc3339_with_offset(self) -> None:
        india = timezone(timedelta(hours=5, minutes=30))
        cases = [
            (datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc), "2026-10-05T00:00:00+00:00"),
            (datetime(2026, 10, 5, 9, 30, 15, 987654, tzinfo=india), "2026-10-05T09:30:15+05:30"),
            (datetime(2026, 12, 31, 18, 0, tzinfo=PDT), "2026-12-31T18:00:00-07:00"),
        ]
        cal = self.signed_in_calendar()
        for start, expected in cases:
            with self.subTest(start=start):
                self.service.calls.clear()
                cal.list_events(start, start + timedelta(days=1))
                (query, _), = self.service.calls_to("events.list")
                self.assertEqual(query["timeMin"], expected)
                end = (start + timedelta(days=1)).isoformat(timespec="seconds")
                self.assertEqual(query["timeMax"], end)

    def test_naive_bounds_are_local_time(self) -> None:
        cal = self.signed_in_calendar()
        start = datetime(2026, 10, 5, 0, 0)
        cal.list_events(start, start + timedelta(days=1))
        (query, _), = self.service.calls_to("events.list")
        self.assertEqual(query["timeMin"], start.astimezone().isoformat(timespec="seconds"))
        self.assertRegex(query["timeMin"], r"^2026-10-05T00:00:00[+-]\d\d:\d\d$")
        self.assertRegex(query["timeMax"], r"^2026-10-06T00:00:00[+-]\d\d:\d\d$")

    def test_timed_and_all_day_events(self) -> None:
        self.service.outcomes["events.list"] = [{"items": [
            _listed("  Film Club   General Meeting ", "2026-10-05T17:00:00-07:00",
                    "2026-10-05T18:00:00-07:00", location="Central Library", htmlLink=EVENT_LINK),
            _listed_all_day("HIST Essay #1 draft due", "2026-10-05", "2026-10-06"),
            _listed_all_day("Fall break", "2026-10-03", "2026-10-08"),
            _listed("", "2026-10-05T16:00:00Z", "2026-10-05T16:30:00Z",
                    location="https://meet.google.com/aaa-bbbb-ccc"),
        ]}]
        cal = self.signed_in_calendar()
        events = cal.list_events(DAY_START, DAY_END)
        self.assertEqual(events, [
            CalendarEvent("Fall break", all_day_start=date(2026, 10, 3),
                          all_day_end=date(2026, 10, 7)),
            CalendarEvent("HIST Essay #1 draft due", all_day_start=date(2026, 10, 5),
                          all_day_end=date(2026, 10, 5)),
            CalendarEvent(NO_TITLE, start=datetime(2026, 10, 5, 9, 0, tzinfo=PDT),
                          end=datetime(2026, 10, 5, 9, 30, tzinfo=PDT),
                          location="https://meet.google.com/aaa-bbbb-ccc"),
            CalendarEvent("Film Club General Meeting", start=datetime(2026, 10, 5, 17, 0, tzinfo=PDT),
                          end=datetime(2026, 10, 5, 18, 0, tzinfo=PDT), location="Central Library",
                          link=EVENT_LINK),
        ])
        self.assertEqual(NO_TITLE, "(No title)")
        for event in events:
            with self.subTest(title=event.title):
                if event.all_day:
                    self.assertIsNone(event.start)
                    self.assertIsNone(event.end)
                else:
                    self.assertIsNotNone(event.start.utcoffset())
                    self.assertIsNotNone(event.end.utcoffset())
                    self.assertIsNone(event.all_day_start)
                self.assertEqual(event.calendar_id, "primary")

    def test_cancelled_and_unreadable_items_are_skipped(self) -> None:
        self.service.outcomes["events.list"] = [{"items": [
            _listed("Cancelled sync", "2026-10-05T10:00:00-07:00", "2026-10-05T11:00:00-07:00",
                    status="cancelled"),
            _listed_all_day("Cancelled day", "2026-10-05", "2026-10-06", status="cancelled"),
            {"id": "no-start", "summary": "No start"},
            _listed("Bad time", "soon"),
            _listed_all_day("Bad day", "2026-13-01", "2026-13-02"),
            "not an event",
            _listed("No end", "2026-10-05T12:00:00-07:00"),
            _listed("Ends first", "2026-10-05T14:00:00-07:00", "2026-10-05T13:00:00-07:00"),
            _listed_all_day("Odd end", "2026-10-05", "2026-10-05"),
        ]}]
        cal = self.signed_in_calendar()
        events = cal.list_events(DAY_START, DAY_END)
        self.assertEqual([event.title for event in events], ["Odd end", "No end", "Ends first"])
        self.assertEqual(events[0].all_day_end, date(2026, 10, 5))
        self.assertEqual(events[1].end, events[1].start)
        self.assertEqual(events[2].end, events[2].start)

    def test_multiple_calendars_are_merged_and_sorted(self) -> None:
        self.service.outcomes["events.list@primary"] = [{"items": [
            _listed("Lecture", "2026-10-05T10:00:00-07:00", "2026-10-05T11:00:00-07:00", "lec"),
            _listed("Lab", "2026-10-05T15:00:00-07:00", "2026-10-05T16:00:00-07:00", "lab"),
        ]}]
        self.service.outcomes[f"events.list@{TEAM}"] = [{"items": [
            _listed("Team lunch", "2026-10-05T12:00:00-07:00", "2026-10-05T13:00:00-07:00",
                    "lunch"),
            # The same lecture, shared with this calendar and rendered in UTC.
            _listed("Lecture", "2026-10-05T17:00:00Z", "2026-10-05T18:00:00Z", "lec"),
            _listed_all_day("Holiday", "2026-10-05", "2026-10-06", "hol"),
        ]}]
        cal = self.signed_in_calendar()
        events = cal.list_events(DAY_START, DAY_END, calendar_ids=["primary", TEAM, "primary", " "])
        queried = [query["calendarId"] for query, _ in self.service.calls_to("events.list")]
        self.assertEqual(queried, ["primary", TEAM])
        self.assertEqual([(event.title, event.calendar_id) for event in events], [
            ("Holiday", TEAM), ("Lecture", "primary"), ("Team lunch", TEAM), ("Lab", "primary")])

    def test_recurring_instances_are_all_kept(self) -> None:
        self.service.outcomes["events.list"] = [{"items": [
            _listed("Standup", "2026-10-05T09:00:00-07:00", "2026-10-05T09:15:00-07:00", "s"),
            _listed("Standup", "2026-10-05T16:00:00-07:00", "2026-10-05T16:15:00-07:00", "s"),
        ]}]
        events = self.signed_in_calendar().list_events(DAY_START, DAY_END)
        self.assertEqual(len(events), 2)

    def test_single_string_calendar_id(self) -> None:
        cal = self.signed_in_calendar()
        cal.list_events(DAY_START, DAY_END, calendar_ids=TEAM)
        (query, _), = self.service.calls_to("events.list")
        self.assertEqual(query["calendarId"], TEAM)

    def test_empty_window_or_no_calendars_makes_no_request(self) -> None:
        cal = self.signed_in_calendar()
        self.assertEqual(cal.list_events(DAY_END, DAY_START), [])
        self.assertEqual(cal.list_events(DAY_START, DAY_START), [])
        self.assertEqual(cal.list_events(DAY_START, DAY_END, calendar_ids=()), [])
        self.assertEqual(cal.list_events(DAY_START, DAY_END, calendar_ids=["", "  "]), [])
        self.assertEqual(self.service.calls, [])
        self.assertEqual(self.service_creds, [])

    def test_follows_next_page(self) -> None:
        self.service.outcomes["events.list"] = [
            {"items": [_listed("One", "2026-10-05T09:00:00-07:00", "2026-10-05T10:00:00-07:00")],
             "nextPageToken": "page2"},
            {"items": [_listed("Two", "2026-10-05T11:00:00-07:00", "2026-10-05T12:00:00-07:00")]},
        ]
        events = self.signed_in_calendar().list_events(DAY_START, DAY_END)
        self.assertEqual([event.title for event in events], ["One", "Two"])
        queries = [query for query, _ in self.service.calls_to("events.list")]
        self.assertNotIn("pageToken", queries[0])
        self.assertEqual(queries[1]["pageToken"], "page2")

    def test_not_signed_in_raises_without_any_flow_call(self) -> None:
        self.write_client_secret()
        cal = self.make_calendar()
        with self.assertLogs(GCAL_LOGGER, level="INFO") as logs:
            with self.assertRaises(CalendarNotSignedIn) as ctx:
                cal.list_events(DAY_START, DAY_END)
        self.assertIsInstance(ctx.exception, CalendarAuthError)
        self.assertEqual(str(ctx.exception), "Not signed in to Google Calendar")
        self.assert_no_sign_in()
        self.assertEqual(self.service_creds, [])
        self.assertEqual(self.service.calls, [])
        self.assertFalse(self.token_path.exists())
        self.assertFalse([line for line in logs.output if line.startswith("WARNING")], logs.output)
        # An explicit Approve still signs in; listing then works without another sign-in.
        cal.create_event(make_action())
        self.assertEqual(len(self.flow.calls), 1)
        cal.list_events(DAY_START, DAY_END)
        self.assertEqual(len(self.flow.calls), 1)

    def test_unrefreshable_token_is_not_signed_in(self) -> None:
        self.write_client_secret()
        self.token_path.parent.mkdir(parents=True)
        self.token_path.write_text(json.dumps({
            "token": self.access_token, "refresh_token": "",
            "token_uri": "https://oauth2.googleapis.com/token", "client_id": CLIENT_ID,
            "client_secret": self.client_secret, "scopes": SCOPES,
            "expiry": f"{utc_naive(timedelta(hours=-2)).isoformat()}Z"}), encoding="utf-8")
        cal = self.make_calendar()
        with self.assertLogs(GCAL_LOGGER, level="INFO"), self.assertRaises(CalendarNotSignedIn):
            cal.list_events(DAY_START, DAY_END)
        self.assert_no_sign_in()
        self.assertFalse(self.token_path.exists())

    def test_expired_token_is_refreshed_without_sign_in(self) -> None:
        self.write_client_secret()
        self.write_token(expiry=utc_naive(timedelta(hours=-2)))

        def refresh(creds: Any) -> None:
            creds.token = f"ya29.fake-refreshed-token-{uuid.uuid4().hex}"
            creds.expiry = utc_naive(timedelta(hours=1))

        cal = self.make_calendar()
        with mock.patch.object(gcal, "_refresh_credentials", side_effect=refresh) as patched:
            self.assertEqual(cal.list_events(DAY_START, DAY_END), [])
        patched.assert_called_once()
        self.assert_no_sign_in()
        self.assertEqual(len(self.service.calls_to("events.list")), 1)

    def test_not_configured_is_a_setup_error(self) -> None:
        cal = self.make_calendar()
        with self.fails_with(CalendarSetupError) as ctx:
            cal.list_events(DAY_START, DAY_END)
        self.assertIn("README step 8", str(ctx.exception))
        self.assert_no_sign_in()

    def test_missing_google_packages_is_a_setup_error(self) -> None:
        cal = self.signed_in_calendar()
        with mock.patch.object(gcal, "google_libraries_available", return_value=False), \
                self.fails_with(CalendarSetupError):
            cal.list_events(DAY_START, DAY_END)
        self.assertEqual(self.service.calls, [])

    def test_401_is_an_auth_error_then_not_signed_in(self) -> None:
        self.service.outcomes["events.list"] = [http_error(401, "Invalid Credentials", "authError")]
        cal = self.signed_in_calendar()
        with self.fails_with(CalendarAuthError) as ctx:
            cal.list_events(DAY_START, DAY_END)
        self.assertNotIsInstance(ctx.exception, CalendarNotSignedIn)
        self.assertEqual(ctx.exception.status, 401)
        self.assertFalse(self.token_path.exists())
        with self.assertLogs(GCAL_LOGGER, level="INFO"), self.assertRaises(CalendarNotSignedIn):
            cal.list_events(DAY_START, DAY_END)
        self.assert_no_sign_in()

    def test_403_scope_is_an_auth_error(self) -> None:
        self.service.outcomes["events.list"] = [
            http_error(403, "Request had insufficient authentication scopes.",
                       rpc_reason="ACCESS_TOKEN_SCOPE_INSUFFICIENT")]
        cal = self.signed_in_calendar()
        with self.fails_with(CalendarAuthError):
            cal.list_events(DAY_START, DAY_END)
        self.assertFalse(self.token_path.exists())
        self.assert_no_sign_in()

    def test_rate_limit_keeps_token(self) -> None:
        self.service.outcomes["events.list"] = [
            http_error(403, "Rate Limit Exceeded", "rateLimitExceeded")]
        cal = self.signed_in_calendar()
        with self.fails_with() as ctx:
            cal.list_events(DAY_START, DAY_END)
        self.assertNotIsInstance(ctx.exception, (CalendarAuthError, CalendarSetupError))
        self.assertTrue(self.token_path.exists())

    def test_revoked_refresh_token_is_an_auth_error(self) -> None:
        self.write_client_secret()
        self.write_token(expiry=utc_naive(timedelta(hours=-2)))
        error = RefreshError("invalid_grant: Token has been expired or revoked.",
                             {"error": "invalid_grant"})
        with mock.patch.object(gcal, "_refresh_credentials", side_effect=error), \
                self.fails_with(CalendarAuthError):
            self.make_calendar().list_events(DAY_START, DAY_END)
        self.assertFalse(self.token_path.exists())
        self.assert_no_sign_in()

    def test_network_failure_is_a_calendar_error(self) -> None:
        self.service.outcomes["events.list"] = [ConnectionResetError(f"reset {self.access_token}")]
        cal = self.signed_in_calendar()
        with self.fails_with() as ctx:
            cal.list_events(DAY_START, DAY_END)
        self.assertIn("ConnectionResetError", str(ctx.exception))
        self.assertNotIn(self.access_token, str(ctx.exception))
        self.assertTrue(self.token_path.exists())

    def test_unreadable_calendar_is_skipped_when_another_answers(self) -> None:
        self.service.outcomes["events.list@primary"] = [{"items": [
            _listed("Lecture", "2026-10-05T10:00:00-07:00", "2026-10-05T11:00:00-07:00")]}]
        self.service.outcomes[f"events.list@{TEAM}"] = [http_error(404, "Not Found", "notFound")]
        cal = self.signed_in_calendar()
        with self.assertLogs(GCAL_LOGGER, level="WARNING") as logs:
            events = cal.list_events(DAY_START, DAY_END, calendar_ids=(TEAM, "primary"))
        self.assertEqual([event.title for event in events], ["Lecture"])
        output = "\n".join(logs.output)
        self.assertIn("skipped a calendar", output)
        self.assertIn("404", output)
        self.assertNotIn("call failed", output)
        self.assertNotIn(TEAM, output)
        self.assertTrue(self.token_path.exists())

    def test_every_calendar_failing_raises(self) -> None:
        self.service.outcomes["events.list@primary"] = [http_error(500, "Backend Error")]
        self.service.outcomes[f"events.list@{TEAM}"] = [http_error(404, "Not Found", "notFound")]
        cal = self.signed_in_calendar()
        with self.fails_with() as ctx:
            cal.list_events(DAY_START, DAY_END, calendar_ids=("primary", TEAM))
        self.assertEqual(ctx.exception.status, 500)

    def test_auth_error_on_any_calendar_raises(self) -> None:
        self.service.outcomes[f"events.list@{TEAM}"] = [http_error(401, "Invalid Credentials")]
        cal = self.signed_in_calendar()
        with self.fails_with(CalendarAuthError):
            cal.list_events(DAY_START, DAY_END, calendar_ids=("primary", TEAM))
        self.assertFalse(self.token_path.exists())


class EventBriefTests(GcalTestCase):
    """list_event_briefs: what Ask Jarvis may see (no descriptions, locations or links)."""

    def briefs_item(self) -> dict[str, Any]:
        return _listed("  Project   sync ", "2026-10-05T14:00:00-04:00", "2026-10-05T15:00:00-04:00",
                       uid="evt0001aa", recurringEventId="series01",
                       description="SECRET agenda", location="https://meet.example.edu/x",
                       hangoutLink="https://meet.google.com/aaa-bbbb-ccc",
                       organizer={"email": "you@example.edu", "self": True},
                       attendees=[{"email": "you@example.edu", "self": True, "responseStatus": "accepted"},
                                  {"email": "ana@example.edu", "displayName": "Ana  Lima",
                                   "responseStatus": "needsAction"},
                                  {"email": "room-1@resource.calendar.google.com", "resource": True},
                                  {"displayName": "No address"}])

    def test_query_asks_only_for_the_brief_fields(self) -> None:
        cal = self.signed_in_calendar()
        self.assertEqual(cal.list_event_briefs(DAY_START, DAY_END), [])
        (query, retries), = self.service.calls_to("events.list")
        self.assertEqual(query, {
            "calendarId": "primary", "timeMin": "2026-10-05T00:00:00-07:00",
            "timeMax": "2026-10-06T00:00:00-07:00", "singleEvents": True, "orderBy": "startTime",
            "maxResults": 50, "showDeleted": False, "timeZone": TZ, "fields": gcal.BRIEF_FIELDS})
        self.assertGreater(retries, 0)
        for never in ("description", "location", "hangoutLink", "conferenceData", "attachments", "htmlLink"):
            self.assertNotIn(never, gcal.BRIEF_FIELDS)
        self.assertEqual(self.flow.calls, [])
        # The day plan (R21): "show as free" and the event's type, pinned on purpose.
        self.assertEqual(gcal.BRIEF_FIELDS, "items(id,status,summary,start,end,recurringEventId,transparency,"
                                            "eventType,organizer(email,displayName,self),attendees(email,"
                                            "displayName,responseStatus,self,resource,organizer)),nextPageToken")

    def test_free_working_location_and_the_owners_own_answer(self) -> None:
        """R21 and the owner's own response (the attendee with self: true is never a guest)."""
        self.service.outcomes["events.list"] = [{"items": [
            _listed("Office hours", "2026-10-05T16:00:00-04:00", "2026-10-05T17:00:00-04:00", uid="evt0003cc",
                    transparency="transparent", eventType="default",
                    organizer={"email": "dean@example.edu"},
                    attendees=[{"email": "you@example.edu", "self": True, "responseStatus": "declined"},
                               {"email": "dean@example.edu", "organizer": True, "responseStatus": "accepted"}]),
            _listed("Home", "2026-10-05T09:00:00-04:00", "2026-10-05T17:00:00-04:00", uid="evt0004dd",
                    eventType="workingLocation", transparency="opaque"),
            _listed("Odd", "2026-10-05T18:00:00-04:00", "2026-10-05T19:00:00-04:00", uid="evt0005ee",
                    eventType="not a type!", attendees=[{"email": "you@example.edu", "self": True,
                                                         "responseStatus": "bogus"}]),
        ]}]
        office, home, odd = sorted(self.signed_in_calendar().list_event_briefs(DAY_START, DAY_END),
                                   key=lambda brief: brief.event_id)
        self.assertEqual((office.transparent, office.event_type, office.self_response, office.free),
                         (True, "", "declined", True))
        self.assertEqual([guest.email for guest in office.guests], ["dean@example.edu"])
        self.assertEqual((home.transparent, home.event_type, home.self_response, home.free),
                         (False, gcal.WORKING_LOCATION, "", True))
        self.assertEqual((odd.event_type, odd.self_response, odd.free), ("", "", False))

    def test_the_owners_own_answer_for_a_meeting_brief(self) -> None:
        """Meeting prep: self_response from the attendee with self: true (never one of the guests);
        "accepted" when he organizes it and no entry says otherwise; the guests unchanged."""
        self.service.outcomes["events.list"] = [{"items": [
            _listed("Maybe", "2026-10-05T10:00:00-04:00", "2026-10-05T11:00:00-04:00", uid="evt0006ff",
                    organizer={"email": "ana@example.edu", "displayName": "Ana Lima"},
                    attendees=[{"email": "ana@example.edu", "organizer": True, "responseStatus": "accepted"},
                               {"email": "you@example.edu", "self": True, "responseStatus": "tentative"},
                               {"email": "ben@example.org", "displayName": "Ben Ode",
                                "responseStatus": "needsAction"}]),
            _listed("Mine", "2026-10-05T12:00:00-04:00", "2026-10-05T13:00:00-04:00", uid="evt0007gg",
                    organizer={"email": "you@example.edu", "self": True},
                    attendees=[{"email": "ben@example.org", "responseStatus": "accepted"}]),
        ]}]
        maybe, mine = sorted(self.signed_in_calendar().list_event_briefs(DAY_START, DAY_END),
                             key=lambda brief: brief.event_id)
        self.assertEqual((maybe.self_response, mine.self_response), ("tentative", "accepted"))
        self.assertEqual([(guest.email, guest.response, guest.organizer) for guest in maybe.guests],
                         [("ana@example.edu", "accepted", True), ("ben@example.org", "needsAction", False)])
        self.assertEqual([guest.email for guest in mine.guests], ["ben@example.org"])

    def test_brief_of_an_event(self) -> None:
        self.service.outcomes["events.list"] = [{"items": [
            self.briefs_item(),
            _listed_all_day("Fall break", "2026-10-03", "2026-10-08", uid="evt0002bb",
                            organizer={"email": "dean@example.edu", "displayName": "Dean"}),
            _listed("Gone", "2026-10-05T10:00:00-04:00", "2026-10-05T11:00:00-04:00", status="cancelled"),
        ]}]
        briefs = self.signed_in_calendar().list_event_briefs(DAY_START, DAY_END)
        self.assertEqual(briefs, [
            gcal.EventBrief("evt0002bb", "primary", "Fall break", all_day_start=date(2026, 10, 3),
                            all_day_end=date(2026, 10, 7), organizer_email="dean@example.edu",
                            organizer_name="Dean"),
            gcal.EventBrief("evt0001aa", "primary", "Project sync",
                            start=datetime(2026, 10, 5, 14, 0, tzinfo=timezone(timedelta(hours=-4))),
                            end=datetime(2026, 10, 5, 15, 0, tzinfo=timezone(timedelta(hours=-4))),
                            organizer_self=True, organizer_email="you@example.edu",
                            guests=(gcal.Guest("ana@example.edu", "Ana Lima", "needsAction"),), recurring=True,
                            self_response="accepted"),   # he organizes it (the field came with the day plan)
        ])
        self.assertTrue(briefs[0].all_day)
        self.assertNotIn("SECRET", repr(briefs))
        self.assertNotIn("meet", repr(briefs))

    def test_never_signs_in(self) -> None:
        self.write_client_secret()
        with self.assertRaises(CalendarNotSignedIn):
            self.make_calendar().list_event_briefs(DAY_START, DAY_END)
        self.assertEqual(self.flow.calls, [])
        self.assertEqual(self.service.calls, [])

    def test_one_failing_calendar_is_skipped(self) -> None:
        self.service.outcomes["events.list@team"] = [http_error(404, "Not Found", "notFound")]
        self.service.outcomes["events.list"] = [{"items": [self.briefs_item()]}]
        with self.assertLogs(GCAL_LOGGER, level="WARNING"):
            briefs = self.signed_in_calendar().list_event_briefs(DAY_START, DAY_END, ["primary", "team"])
        self.assertEqual([brief.event_id for brief in briefs], ["evt0001aa"])
        self.assertEqual(self.signed_in_calendar().list_event_briefs(DAY_END, DAY_START), [])


# --------------------------------------------------------------------------
# One event: get_event, respond, move, cancel
# --------------------------------------------------------------------------

YOU = "you@example.edu"
EVENT_ID = "abc123def456ghi789"
EDT = timezone(timedelta(hours=-4), "EDT")
BEFORE = datetime(2026, 10, 1, 9, 0, tzinfo=EDT)   # "now" for moves: before every test event
COMMENT = "Running late, sorry"


def _event_item(*, organizer_self: bool = True, me: str | None = "needsAction", comment: str = "",
                guests: int = 2, start: str = "2026-10-08T12:00:00-04:00",
                end: str = "2026-10-08T13:00:00-04:00", all_day: tuple[str, str] | None = None,
                status: str = "confirmed", guests_can_modify: bool = False, recurrence: bool = False,
                instance: bool = False, summary: str | None = "Project sync") -> dict[str, Any]:
    """An events.get answer shaped like Google's (invented people and ids)."""
    attendees: list[dict[str, Any]] = [{"email": f"guest{n}@example.edu", "responseStatus": "accepted"}
                                       for n in range(guests)]
    if me is not None:
        entry: dict[str, Any] = {"email": YOU, "self": True, "responseStatus": me}
        if comment:
            entry["comment"] = comment
        attendees.insert(min(1, len(attendees)), entry)
    organizer: dict[str, Any] = ({"email": YOU, "self": True} if organizer_self
                                 else {"email": "ana@example.edu", "displayName": "Ana Example"})
    item: dict[str, Any] = {"id": EVENT_ID, "status": status, "htmlLink": EVENT_LINK,
                            "organizer": organizer, "attendees": attendees}
    if summary is not None:
        item["summary"] = summary
    if all_day is not None:
        item["start"], item["end"] = {"date": all_day[0]}, {"date": all_day[1]}
    else:
        item["start"] = {"dateTime": start, "timeZone": TZ}
        item["end"] = {"dateTime": end, "timeZone": TZ}
    if guests_can_modify:
        item["guestsCanModify"] = True
    if recurrence:
        item["recurrence"] = ["RRULE:FREQ=WEEKLY"]
    if instance:
        item["recurringEventId"] = "abc123series"
    return item


class EventCallTestCase(GcalTestCase):
    def event_calendar(self, item: dict[str, Any] | BaseException | None = None, **outcomes: Any) -> GoogleCalendar:
        """A signed-in calendar whose events.get answers ``item``; ``outcomes`` for patch / delete."""
        self.service.outcomes["events.get"] = [_event_item() if item is None else item]
        self.service.outcomes["events.patch"] = [outcomes.get("patch", {"id": EVENT_ID, "htmlLink": EVENT_LINK})]
        self.service.outcomes["events.delete"] = [outcomes.get("delete", "")]
        return self.signed_in_calendar()

    @contextmanager
    def refused(self, error_type: type[CalendarError] = gcal.NotAllowed) -> Iterator[Any]:
        """assertRaises(error_type), logged once at INFO (an answer about one event, not a failure)."""
        with self.assertLogs(GCAL_LOGGER, level="INFO") as logs:
            with self.assertRaises(error_type) as ctx:
                yield ctx
        failures = [line for line in logs.output if "call failed" in line]
        self.assertEqual(len(failures), 1, logs.output)
        self.assertTrue(failures[0].startswith("INFO:"), failures)

    def assert_no_change_sent(self) -> None:
        self.assertEqual(self.service.calls_to("events.patch"), [])
        self.assertEqual(self.service.calls_to("events.delete"), [])

    def change_calls(self) -> list[tuple[dict[str, Any], int]]:
        return self.service.calls_to("events.patch") + self.service.calls_to("events.delete")


class GetEventTests(EventCallTestCase):
    def test_log_names_only_known_aliases(self) -> None:
        # The card's message names the account; the log writes any alias but work / personal
        # as "other".
        self.write_client_secret()   # no saved sign-in
        for alias, logged in (("school", "other"), ("work", "work")):
            with self.subTest(alias=alias):
                account = google_auth.GoogleAccount(alias, client_secret_path=self.secret_path,
                                                    data_dir=self.root / "aliases", flow_factory=self.flow_factory)
                cal = GoogleCalendar(account=account, service_factory=self.service_factory)
                with self.assertLogs(GCAL_LOGGER, level="INFO") as logs, \
                        self.assertRaises(CalendarNotSignedIn) as ctx:
                    cal.get_event(EVENT_ID, interactive=False)
                self.assertIn(f"({alias} account)", str(ctx.exception))
                text = "\n".join(logs.output)
                self.assertIn(f"Google Calendar ({logged}) call failed", text)
                self.assertIn(f"({logged} account)", text)
                if alias != logged:
                    self.assertNotIn(alias, text)

    def test_maps_googles_view_of_the_event(self) -> None:
        cal = self.event_calendar(_event_item(organizer_self=False, comment=COMMENT, instance=True))
        details = cal.get_event(EVENT_ID)
        self.assertEqual(details, gcal.EventDetails(
            event_id=EVENT_ID, calendar_id="primary", title="Project sync",
            start=datetime(2026, 10, 8, 12, 0, tzinfo=EDT), end=datetime(2026, 10, 8, 13, 0, tzinfo=EDT),
            all_day_start=None, all_day_end=None, status="confirmed", organizer_self=False,
            guests_can_modify=False, self_email=YOU, self_response="needsAction", attendee_count=3,
            recurring_instance=True, series=False, time_zone=TZ, link=EVENT_LINK, organizer="Ana Example",
            self_comment=COMMENT, organizer_email="ana@example.edu"))
        self.assertFalse(details.all_day)
        self.assertEqual(details.account_email, YOU)   # the address Google answered as
        self.assertEqual(self.service.calls_to("events.get"),
                         [({"calendarId": "primary", "eventId": EVENT_ID}, gcal._READ_RETRIES)])
        self.assertEqual(self.event_calendar(_event_item(me=None)).get_event(EVENT_ID).account_email, YOU)

    def test_all_day_event_and_missing_parts(self) -> None:
        item = _event_item(all_day=("2026-10-09", "2026-10-11"), me=None, guests=0, summary=None,
                           recurrence=True)
        del item["organizer"]
        details = self.event_calendar(item).get_event(EVENT_ID, calendar_id=TEAM)
        self.assertEqual((details.start, details.end), (None, None))
        self.assertEqual((details.all_day_start, details.all_day_end), (date(2026, 10, 9), date(2026, 10, 10)))
        self.assertTrue(details.all_day)
        self.assertEqual((details.title, details.self_email, details.self_response, details.organizer),
                         (NO_TITLE, "", "", ""))
        self.assertEqual((details.organizer_self, details.series, details.attendee_count, details.calendar_id),
                         (False, True, 0, TEAM))
        self.assertEqual(self.service.calls_to("events.get")[0][0]["calendarId"], TEAM)

    def test_cancelled_or_missing_event_is_gone_and_the_token_stays(self) -> None:
        for outcome in (_event_item(status="cancelled"), http_error(404, "Not Found", "notFound"),
                        http_error(410, "Resource has been deleted", "deleted")):
            with self.subTest(outcome=outcome if isinstance(outcome, dict) else outcome.status_code):
                cal = self.event_calendar(outcome)
                with self.refused(gcal.EventGone):
                    cal.get_event(EVENT_ID)
                self.assertTrue(self.token_path.exists())

    def test_not_interactive_never_opens_the_sign_in(self) -> None:
        self.write_client_secret()   # no saved sign-in
        cal = self.make_calendar()
        with self.assertLogs(GCAL_LOGGER, level="INFO"), self.assertRaises(CalendarNotSignedIn):
            cal.get_event(EVENT_ID, interactive=False)
        self.assertEqual(self.flow.calls, [])
        self.assertEqual(self.service.calls, [])

    def test_interactive_signs_in_first_when_needed(self) -> None:
        self.write_client_secret()
        self.service.outcomes["events.get"] = [_event_item()]
        self.make_calendar().get_event(EVENT_ID)
        self.assertEqual(len(self.flow.calls), 1)


class RespondTests(EventCallTestCase):
    def test_patch_names_only_your_attendee_entry_with_explicit_send_updates(self) -> None:
        for answer, status in (("yes", "accepted"), ("no", "declined"), ("maybe", "tentative")):
            for notify, send_updates in (("all", "all"), ("externalOnly", "externalOnly"),
                                         ("external", "externalOnly"), ("none", "none")):
                with self.subTest(answer=answer, notify=notify):
                    self.service = FakeService()
                    cal = self.event_calendar(_event_item(organizer_self=False, guests=4))
                    result = cal.respond(EVENT_ID, answer, send_updates=notify)
                    self.assertEqual(result, gcal.ChangeResult(event_id=EVENT_ID, link=EVENT_LINK, already=False))
                    self.assertEqual(self.service.calls_to("events.patch"), [(
                        {"calendarId": "primary", "eventId": EVENT_ID, "sendUpdates": send_updates,
                         "body": {"attendeesOmitted": True,
                                  "attendees": [{"email": YOU, "responseStatus": status}]}}, 0)])

    def test_default_notifies_everyone_and_the_comment_goes_with_the_answer(self) -> None:
        cal = self.event_calendar(_event_item(organizer_self=False))
        cal.respond(EVENT_ID, "no", comment=COMMENT, calendar_id=TEAM)
        ((kwargs, retries),) = self.service.calls_to("events.patch")
        self.assertEqual((kwargs["sendUpdates"], kwargs["calendarId"], retries), ("all", TEAM, 0))
        self.assertEqual(kwargs["body"]["attendees"],
                         [{"email": YOU, "responseStatus": "declined", "comment": COMMENT}])

    def test_already_answered_sends_nothing(self) -> None:
        cases = ((_event_item(me="accepted"), "yes", "", True),
                 (_event_item(me="tentative", comment=COMMENT), "maybe", COMMENT, True),
                 (_event_item(me="tentative", comment=COMMENT), "maybe", "", True),
                 (_event_item(me="tentative"), "maybe", COMMENT, False),
                 (_event_item(me="accepted"), "no", "", False))
        for item, answer, comment, already in cases:
            with self.subTest(answer=answer, comment=comment, already=already):
                self.service = FakeService()
                result = self.event_calendar(item).respond(EVENT_ID, answer, comment=comment)
                self.assertEqual(result.already, already)
                self.assertEqual(len(self.service.calls_to("events.patch")), 0 if already else 1)

    def test_not_on_the_guest_list_is_refused(self) -> None:
        cal = self.event_calendar(_event_item(me=None))
        with self.refused() as ctx:
            cal.respond(EVENT_ID, "yes")
        self.assertIn("guest list", str(ctx.exception))
        self.assert_no_change_sent()
        self.assertTrue(self.token_path.exists())

    def test_bad_answer_or_notify_is_refused_before_any_request(self) -> None:
        cal = self.event_calendar()
        for call in (lambda: cal.respond(EVENT_ID, "accepted"), lambda: cal.respond(EVENT_ID, "yes", send_updates=""),
                     lambda: cal.respond(EVENT_ID, "yes", send_updates="everyone")):
            with self.subTest(call=call), self.assertRaises(CalendarError):
                call()
        self.assertEqual(self.service.calls, [])

    def test_an_answer_that_may_have_happened_is_an_unknown_outcome_and_never_retried(self) -> None:
        for error in (http_error(500, "Backend Error", "backendError"), http_error(503, "Service Unavailable"),
                      socket.timeout("timed out"), ConnectionResetError("reset"),
                      google_auth.RequestNotResent("not sent twice")):
            with self.subTest(error=type(error).__name__):
                self.service = FakeService()
                cal = self.event_calendar(patch=error)
                with self.fails_with(gcal.CalendarUnknownOutcome) as ctx:
                    cal.respond(EVENT_ID, "yes")
                self.assertIn("check the calendar before retrying", str(ctx.exception))
                self.assertEqual(len(self.service.calls_to("events.patch")), 1)
                self.assertEqual(self.service.calls_to("events.patch")[0][1], 0)
                self.assertTrue(self.token_path.exists())

    def test_failures_before_or_refused_by_google_are_not_unknown(self) -> None:
        cases = ((ConnectionRefusedError("refused"), CalendarError), (socket.gaierror("no dns"), CalendarError),
                 (http_error(400, "Bad Request", "badRequest"), CalendarError),
                 (http_error(409, "Conflict", "conflict"), CalendarError))
        for error, error_type in cases:
            with self.subTest(error=type(error).__name__):
                self.service = FakeService()
                cal = self.event_calendar(patch=error)
                with self.fails_with(error_type) as ctx:
                    cal.respond(EVENT_ID, "yes")
                self.assertNotIsInstance(ctx.exception, gcal.CalendarUnknownOutcome)
                self.assertTrue(self.token_path.exists())

    def test_event_level_403_is_not_allowed_and_keeps_the_token(self) -> None:
        cal = self.event_calendar(patch=http_error(403, "Forbidden", "forbiddenForNonOrganizer"))
        with self.refused() as ctx:
            cal.respond(EVENT_ID, "yes")
        self.assertEqual(ctx.exception.status, 403)
        self.assertTrue(self.token_path.exists())

    def test_401_on_the_change_is_a_sign_in_problem_not_unknown(self) -> None:
        cal = self.event_calendar(patch=http_error(401, "Invalid Credentials", "authError"))
        with self.fails_with(CalendarAuthError) as ctx:
            cal.respond(EVENT_ID, "yes")
        self.assertNotIsInstance(ctx.exception, gcal.CalendarUnknownOutcome)
        self.assertFalse(self.token_path.exists())

    def test_not_interactive_without_a_sign_in_sends_nothing(self) -> None:
        self.write_client_secret()
        cal = self.make_calendar()
        with self.assertLogs(GCAL_LOGGER, level="INFO"), self.assertRaises(CalendarNotSignedIn):
            cal.respond(EVENT_ID, "yes", interactive=False)
        self.assertEqual((self.flow.calls, self.service.calls), ([], []))

    def test_logs_name_the_event_by_id_only(self) -> None:
        cal = self.event_calendar(_event_item(organizer_self=False))
        with self.assertLogs(level="DEBUG") as logs:
            cal.respond(EVENT_ID, "yes", comment=COMMENT)
        text = "\n".join(logs.output)
        self.assertIn(EVENT_ID, text)
        for private in (YOU, "Project sync", COMMENT, "Ana Example", "guest0@example.edu"):
            self.assertNotIn(private, text)


class MoveTests(EventCallTestCase):
    NEW_START = datetime(2026, 10, 8, 14, 0)
    NEW_END = datetime(2026, 10, 8, 15, 0)

    def move(self, cal: GoogleCalendar, **kwargs: Any) -> gcal.ChangeResult:
        kwargs.setdefault("now", BEFORE)
        return cal.move(EVENT_ID, kwargs.pop("start", self.NEW_START), kwargs.pop("end", self.NEW_END), **kwargs)

    def test_patch_sends_the_new_wall_time_in_the_calendar_zone(self) -> None:
        result = self.move(self.event_calendar())
        self.assertEqual(result, gcal.ChangeResult(event_id=EVENT_ID, link=EVENT_LINK, already=False))
        self.assertEqual(self.service.calls_to("events.patch"), [(
            {"calendarId": "primary", "eventId": EVENT_ID, "sendUpdates": "all",
             "body": {"start": {"dateTime": "2026-10-08T14:00:00", "timeZone": TZ},
                      "end": {"dateTime": "2026-10-08T15:00:00", "timeZone": TZ}}}, 0)])

    def test_send_updates_is_always_explicit(self) -> None:
        for notify, send_updates in (("all", "all"), ("external", "externalOnly"), ("none", "none")):
            with self.subTest(notify=notify):
                self.service = FakeService()
                self.move(self.event_calendar(), send_updates=notify)
                self.assertEqual(self.service.calls_to("events.patch")[0][0]["sendUpdates"], send_updates)

    def test_a_guest_may_move_it_when_guests_can_modify(self) -> None:
        self.move(self.event_calendar(_event_item(organizer_self=False, guests_can_modify=True)))
        self.assertEqual(len(self.service.calls_to("events.patch")), 1)

    def test_one_occurrence_of_a_repeating_event_moves(self) -> None:
        self.move(self.event_calendar(_event_item(instance=True)))
        self.assertEqual(self.service.calls_to("events.patch")[0][0]["eventId"], EVENT_ID)

    def test_refusals_send_nothing(self) -> None:
        cases = {
            "not yours": (_event_item(organizer_self=False), {}, "don't organize"),
            "all day": (_event_item(all_day=("2026-10-08", "2026-10-09")), {}, "All-day"),
            "series": (_event_item(recurrence=True), {}, "repeating series"),
            "past": (_event_item(), {"now": datetime(2026, 10, 9, 9, 0, tzinfo=EDT)}, "already begun"),
        }
        for name, (item, kwargs, words) in cases.items():
            with self.subTest(name=name):
                self.service = FakeService()
                cal = self.event_calendar(item)
                with self.refused() as ctx:
                    self.move(cal, **kwargs)
                self.assertIn(words, str(ctx.exception))
                self.assert_no_change_sent()

    def test_already_at_that_time_sends_nothing(self) -> None:
        result = self.move(self.event_calendar(), start=datetime(2026, 10, 8, 12, 0),
                           end=datetime(2026, 10, 8, 13, 0, tzinfo=EDT))
        self.assertTrue(result.already)
        self.assert_no_change_sent()

    def test_end_not_after_start_is_refused_before_any_request(self) -> None:
        cal = self.event_calendar()
        with self.assertRaises(CalendarError):
            self.move(cal, end=self.NEW_START)
        self.assertEqual(self.service.calls, [])

    def test_server_error_is_unknown_and_sent_once(self) -> None:
        cal = self.event_calendar(patch=http_error(502, "Bad Gateway"))
        with self.fails_with(gcal.CalendarUnknownOutcome):
            self.move(cal)
        self.assertEqual([retries for _, retries in self.change_calls()], [0])


class CancelTests(EventCallTestCase):
    def test_delete_with_explicit_send_updates(self) -> None:
        for notify, send_updates in (("all", "all"), ("external", "externalOnly"), ("none", "none")):
            with self.subTest(notify=notify):
                self.service = FakeService()
                result = self.event_calendar().cancel(EVENT_ID, send_updates=notify)
                self.assertEqual(result, gcal.ChangeResult(event_id=EVENT_ID, link="", already=False))
                self.assertEqual(self.service.calls_to("events.delete"), [
                    ({"calendarId": "primary", "eventId": EVENT_ID, "sendUpdates": send_updates}, 0)])

    def test_only_the_organizer_cancels_and_never_a_whole_series(self) -> None:
        for item, words in ((_event_item(organizer_self=False, guests_can_modify=True), "decline it instead"),
                            (_event_item(recurrence=True), "repeating series")):
            with self.subTest(words=words):
                self.service = FakeService()
                cal = self.event_calendar(item)
                with self.refused() as ctx:
                    cal.cancel(EVENT_ID)
                self.assertIn(words, str(ctx.exception))
                self.assert_no_change_sent()

    def test_an_event_that_is_gone_is_already_cancelled(self) -> None:
        for item in (_event_item(status="cancelled"), http_error(404, "Not Found", "notFound"),
                     http_error(410, "Resource has been deleted", "deleted")):
            with self.subTest(item=item if isinstance(item, dict) else item.status_code):
                self.service = FakeService()
                result = self.event_calendar(item).cancel(EVENT_ID)
                self.assertEqual(result, gcal.ChangeResult(event_id=EVENT_ID, link="", already=True))
                self.assert_no_change_sent()
        self.service = FakeService()
        result = self.event_calendar(delete=http_error(410, "Resource has been deleted", "deleted")).cancel(EVENT_ID)
        self.assertTrue(result.already)

    def test_delete_without_an_answer_is_unknown_and_sent_once(self) -> None:
        for error in (http_error(500, "Backend Error"), socket.timeout("timed out")):
            with self.subTest(error=type(error).__name__):
                self.service = FakeService()
                cal = self.event_calendar(delete=error)
                with self.fails_with(gcal.CalendarUnknownOutcome):
                    cal.cancel(EVENT_ID)
                self.assertEqual([retries for _, retries in self.change_calls()], [0])


class RequestHookTests(EventCallTestCase):
    """on_request (the LIVE view): the exact request, right before it goes; never when nothing is sent."""

    def heard(self) -> tuple[list[tuple[str, dict[str, Any]]], Any]:
        calls: list[tuple[str, dict[str, Any]]] = []
        return calls, lambda method, params: calls.append((method, params))

    def test_each_change_hears_exactly_what_is_sent(self) -> None:
        self.assertTrue(GoogleCalendar.REQUEST_HOOK)
        calls, hook = self.heard()
        cal = self.signed_in_calendar()
        action = make_action(rrule="RRULE:FREQ=WEEKLY;COUNT=4")
        cal.create_event(action, on_request=hook)
        (kwargs, _), = self.service.calls_to("events.insert")
        self.assertEqual(calls, [("events.insert", kwargs)])
        calls.clear()
        self.event_calendar().respond(EVENT_ID, "yes", comment=COMMENT, on_request=hook)
        (kwargs, _), = self.service.calls_to("events.patch")
        self.assertEqual(calls, [("events.patch", kwargs)])
        for name, call in (("move", lambda cal: cal.move(EVENT_ID, MoveTests.NEW_START, MoveTests.NEW_END, now=BEFORE,
                                                         on_request=hook)),
                           ("cancel", lambda cal: cal.cancel(EVENT_ID, send_updates="none", on_request=hook))):
            with self.subTest(change=name):
                self.service = FakeService()
                calls.clear()
                call(self.event_calendar())
                (kwargs, _), = self.change_calls()
                self.assertEqual(calls, [("events.patch" if name == "move" else "events.delete", kwargs)])

    def test_nothing_sent_nothing_heard_and_a_failing_hook_changes_nothing(self) -> None:
        calls, hook = self.heard()
        self.event_calendar(_event_item(status="cancelled")).cancel(EVENT_ID, on_request=hook)
        self.assertEqual(calls, [])
        self.service = FakeService()

        def broken(_method: str, params: dict[str, Any]) -> None:
            params["eventId"] = "changed"   # a copy: the request is untouched
            raise RuntimeError("hook")

        with self.assertLogs(GCAL_LOGGER, level="DEBUG") as logs:
            result = self.event_calendar().cancel(EVENT_ID, on_request=broken)
        self.assertEqual(result, gcal.ChangeResult(event_id=EVENT_ID, link="", already=False))
        self.assertEqual(self.service.calls_to("events.delete")[0][0]["eventId"], EVENT_ID)
        self.assertIn("Live view: on_request failed (RuntimeError)", " ".join(logs.output))


class _StaleConnection:
    """A connection whose first answer is lost (a broken status line), then answers 200."""

    def __init__(self) -> None:
        self.sock: Any = object()
        self.host = "www.googleapis.com"
        self.requests: list[str] = []
        self.answers: list[Any] = [http_client.BadStatusLine(""), _JsonAnswer()]

    def connect(self) -> None:
        self.sock = object()

    def close(self) -> None:
        self.sock = None

    def request(self, method: str, uri: str, body: Any = None, headers: Any = None) -> None:
        self.requests.append(method)

    def getresponse(self) -> Any:
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class _JsonAnswer(dict):
    def __init__(self) -> None:
        super().__init__({"status": "200", "content-type": "application/json; charset=UTF-8"})

    def read(self) -> bytes:
        return json.dumps({"id": EVENT_ID, "htmlLink": EVENT_LINK}).encode("utf-8")


class BuildServiceTests(GcalTestCase):
    def test_the_service_never_sends_a_change_twice_and_has_a_timeout(self) -> None:
        service = gcal._build_service(SimpleNamespace(token="fake"))
        http = service._http.http
        self.assertEqual(type(http).__name__, "SingleSendHttp")
        self.assertEqual(http.timeout, gcal.HTTP_TIMEOUT_S)
        self.assertEqual(gcal.HTTP_TIMEOUT_S, 30)

    def stale_service(self) -> tuple[Any, _StaleConnection]:
        from google.oauth2.credentials import Credentials

        creds = Credentials(token=self.access_token, expiry=utc_naive(timedelta(hours=1)))
        service = gcal._build_service(creds)
        conn = _StaleConnection()
        service._http.http.connections["https:www.googleapis.com"] = conn
        return service, conn

    def test_through_the_real_client_stack_a_lost_answer_to_a_change_is_not_sent_again(self) -> None:
        for name, request in (
                ("patch", lambda s: s.events().patch(calendarId="primary", eventId=EVENT_ID, sendUpdates="all",
                                                     body={"attendeesOmitted": True, "attendees": []})),
                ("delete", lambda s: s.events().delete(calendarId="primary", eventId=EVENT_ID, sendUpdates="all"))):
            with self.subTest(name=name):
                service, conn = self.stale_service()
                with self.assertRaises(google_auth.RequestNotResent):
                    request(service).execute(num_retries=0)
                self.assertEqual(len(conn.requests), 1)

    def test_through_the_real_client_stack_a_read_is_sent_again_on_a_stale_connection(self) -> None:
        service, conn = self.stale_service()
        item = service.events().get(calendarId="primary", eventId=EVENT_ID).execute(num_retries=0)
        self.assertEqual(item["id"], EVENT_ID)
        self.assertEqual(conn.requests, ["GET", "GET"])


class ChangesNeedAConfirmedAccountTests(EventCallTestCase):
    """The last check right before a change goes to Google (google_auth.GoogleAccount.change_problem,
    which fails closed): a saved sign-in whose Google account you have not confirmed for the alias,
    or one Jarvis has no binding for (accounts.json deleted or unreadable), changes nothing; reading
    the calendar goes on. executor.CalendarBackend checks the same before "running" is saved."""

    SUB = "100000000000000000077"

    def bound_calendar(self, state: str) -> GoogleCalendar:
        self.write_client_secret()
        data = self.root / uuid.uuid4().hex
        bindings = google_auth.AccountBindings(data / "accounts.json")
        account = google_auth.GoogleAccount("work", client_secret_path=self.secret_path, data_dir=data,
                                            bindings=bindings, flow_factory=self.flow_factory)
        self.token_path = account.token_path
        self.write_token()
        info = json.loads(self.token_path.read_text(encoding="utf-8"))
        info[google_auth.TOKEN_SUB_KEY] = self.SUB   # the account the sign-in was issued for
        self.token_path.write_text(json.dumps(info), encoding="utf-8")
        if state != "no binding":
            with self.assertLogs("briefing_reader.google_auth", level="INFO"):
                bindings.bind("work", YOU, self.SUB)
                if state == "confirmed":
                    bindings.confirm("work", YOU)
        if state == "unreadable":
            bindings.path.write_text('{"work": {"email": ', encoding="utf-8")   # a slip while editing it
        self.service.outcomes["events.get"] = [_event_item()]
        self.service.outcomes["events.patch"] = [{"id": EVENT_ID, "htmlLink": EVENT_LINK}]
        self.service.outcomes["events.delete"] = [""]
        return GoogleCalendar(account=account, service_factory=self.service_factory)

    def test_no_change_without_a_confirmed_account(self) -> None:
        changes = (
            ("respond", lambda cal: cal.respond(EVENT_ID, "yes", interactive=False)),
            ("move", lambda cal: cal.move(EVENT_ID, datetime(2026, 10, 8, 14, 0), datetime(2026, 10, 8, 15, 0),
                                          now=BEFORE, interactive=False)),
            ("cancel", lambda cal: cal.cancel(EVENT_ID, interactive=False)),
            ("create", lambda cal: cal.create_event(make_action(), interactive=False)))
        for state, problem in (("unconfirmed", google_auth.PROBLEM_CONFIRM),
                               ("no binding", google_auth.PROBLEM_IDENTITY),
                               ("unreadable", google_auth.PROBLEM_IDENTITY)):
            for name, change in changes:
                with self.subTest(state=state, change=name):
                    cal = self.bound_calendar(state)
                    with self.assertLogs(level="WARNING") as logs, self.assertRaises(CalendarAuthError) as ctx:
                        change(cal)
                    self.assertEqual(ctx.exception.problem, problem)
                    self.assertIn("nothing was sent or changed", str(ctx.exception))
                    for text in ("\n".join(logs.output), str(ctx.exception)):
                        self.assertNotIn(YOU, text)
                    self.assert_no_change_sent()
                    self.assertEqual(self.service.calls_to("events.insert"), [])
                    self.assertTrue(self.token_path.exists())   # the sign-in itself is kept
            with self.subTest(state=state, change="read"):
                self.assertEqual(self.bound_calendar(state).get_event(EVENT_ID, interactive=False).title,
                                 "Project sync")   # reading goes on
        cal = self.bound_calendar("confirmed")
        self.assertFalse(cal.respond(EVENT_ID, "yes", interactive=False).already)
        self.assertEqual(len(self.service.calls_to("events.patch")), 1)

    def test_the_one_account_setup_is_not_held_back(self) -> None:
        cal = self.event_calendar()   # GoogleCalendar's own constructor: alias "", no bindings
        self.assertEqual((cal.change_problem(), cal.needs_confirmation()), (("", ""), False))
        cal.respond(EVENT_ID, "yes", interactive=False)
        self.assertEqual(len(self.service.calls_to("events.patch")), 1)


if __name__ == "__main__":
    unittest.main()
