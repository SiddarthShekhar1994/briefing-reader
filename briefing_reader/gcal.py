"""Google Calendar access for approved proposals and the day's agenda.

:class:`GoogleCalendar` turns an approved
:class:`~briefing_reader.actions.ProposedAction` into a calendar event, answers
invitations, moves and cancels events you organize, and reads the events the
reading window shows:

    sign_in()          OAuth installed-app flow in the user's browser (google_auth.GoogleAccount);
                       the token is saved in %LOCALAPPDATA%\\briefing-reader
    timezone()         the calendar's time zone setting (cached)
    find_existing(a)   an event with the same title and start, if there is one
    create_event(a)    find_existing first, else events.insert (never retried; a 5xx answer or
                       a lost answer is CalendarUnknownOutcome, and a retry finds the event)
    list_events(s, e)  CalendarEvents between two times; never signs in (raises
                       CalendarNotSignedIn instead)
    list_event_briefs(s, e)  what Ask Jarvis may see of those events (EventBrief: ids, title,
                       times, organizer, guests; never a description, location or link)
    get_event(id)      Google's own view of one event (EventDetails)
    respond(id, ...)   answer an invitation: events.patch with attendeesOmitted and only
                       your own attendee entry
    move(id, s, e)     new start and end for an event you organize (or may modify)
    cancel(id)         events.delete of an event you organize

``respond``, ``move`` and ``cancel`` always pass ``sendUpdates`` explicitly
(the REST default is "none"), fetch the event first and refuse what you may
not do (NotAllowed), and are never retried: a 5xx answer or a timeout after
the request went out raises CalendarUnknownOutcome (it may or may not have
happened), and the HTTP client never sends a change twice
(google_auth.single_send_http). They are idempotent (a second answer, move or
cancel finds it done and says ``already``), so a retry the user asks for
after an unknown outcome is safe. With ``interactive=False`` they never open
the browser sign-in (CalendarNotSignedIn instead).

A GoogleCalendar acts for one account (``account=``, see google_auth.py); the
old constructor (``client_secret_path`` + ``token_path``) builds a private
one, the single sign-in of older versions. Every call is blocking and may wait
on the network or, for sign-in, on the user's browser for up to
``open_timeout_s`` seconds, so callers run them on a worker thread. The Google
client libraries are imported lazily: importing this module stays cheap, needs
no Google package and never imports Qt.

The OAuth client secret and the access and refresh tokens are registered with
:func:`config.register_secret` as soon as they are read, so the log redacts
them. Exception messages carry only HTTP statuses, Google's error message
(redacted) and exception type names, never an event's title or guests; the log
names events by id only.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import socket
import ssl
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import default_data_dir, redact
from .google_auth import (
    CALENDAR_FEATURE,
    PROBLEM_EXPIRED,
    PROBLEM_FAILED,
    PROBLEM_SETUP,
    PROBLEM_SIGNED_OUT,
    AccountAuthError,
    AccountError,
    AccountNotSignedIn,
    AccountSetupError,
    GoogleAccount,
    blocked_code,
    logged_alias,
    scrub_alias,
    scopes_for,
    single_send_http,
)
from .google_auth import _installed_app_flow as _installed_app_flow  # noqa: PLC0414 - patched in tests
from .google_auth import _refresh_credentials as _account_refresh
from .google_auth import _register_credentials as _register_credentials  # noqa: PLC0414
from .google_auth import google_libraries_available as _account_libraries

if TYPE_CHECKING:
    from .actions import ProposedAction

logger = logging.getLogger(__name__)

SCOPES = scopes_for((CALENDAR_FEATURE,))
TOKEN_FILE_NAME = "google_token.json"
# Used when neither the calendar's setting nor Windows names a time zone.
LAST_RESORT_TIMEZONE = "UTC"
SETUP_HINT = "Set up Google Calendar: README step 8"
SIGN_IN_SUCCESS_MESSAGE = "Jarvis is connected to Google Calendar. You can close this tab."
EVENT_FOOTER = "Added by Jarvis from your Daily Briefing."
# The footer of an event by the card's mode (actions.ProposedAction.mode): a day plan's blocks say so.
PLAN_EVENT_FOOTER = "Added by Jarvis from your day plan."
EVENT_FOOTERS = {"": EVENT_FOOTER, "plan": PLAN_EVENT_FOOTER}
DEFAULT_EVENT_MINUTES = 60
NO_TITLE = "(No title)"
NOT_SIGNED_IN_MESSAGE = "Not signed in to Google Calendar"
# sendUpdates values (always passed; the REST default would be "none").
SEND_UPDATES = ("all", "externalOnly", "none")
# RSVP answers (actions.py's normalized answer=) -> attendee responseStatus.
RESPONSES = {"yes": "accepted", "no": "declined", "maybe": "tentative"}
HTTP_TIMEOUT_S = 30        # per request; a mutation that times out is an unknown outcome

# What list_event_briefs asks Google for (Ask Jarvis): never description, location, hangoutLink,
# conferenceData or attachments. transparency ("show as free") and eventType (a working location)
# tell the day plan which events leave their time free.
BRIEF_FIELDS = ("items(id,status,summary,start,end,recurringEventId,transparency,eventType,"
                "organizer(email,displayName,self),"
                "attendees(email,displayName,responseStatus,self,resource,organizer)),nextPageToken")
_READ_RETRIES = 2          # reads are safe to retry; insert, patch and delete are not retried
_MAX_LIST_PAGES = 5
_LIST_PAGE_SIZE = 50
_MAX_MESSAGE_LEN = 200
# 403s that are about quota or project setup, not about the user's sign-in.
_RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded",
                                 "dailyLimitExceeded", "RATE_LIMIT_EXCEEDED"})
_SETUP_REASONS = frozenset({"accessNotConfigured", "SERVICE_DISABLED"})
# 403s on one event that only mean "not this event" (no sign-in problem; the token stays).
_EVENT_REFUSED_REASONS = frozenset({"forbidden", "forbiddenForNonOrganizer", "requiredAccessLevel"})
# Failures that certainly happened before a request reached Google (a mutation that failed this
# way did not happen). Anything else after a mutation was sent is an unknown outcome.
_NOT_SENT_ERRORS: tuple[type[BaseException], ...] = (socket.gaierror, ConnectionRefusedError,
                                                     ssl.SSLCertVerificationError)
_NOT_SENT_NAMES = frozenset({"ServerNotFoundError", "TransportError"})
# actions.py writes "until <date>" on a timed event as the end of that day in UTC.
_END_OF_DAY_UNTIL_RE = re.compile(r"UNTIL=(\d{8})T235959Z")
_RULE_PREFIXES = ("RRULE:", "EXRULE:", "RDATE", "EXDATE")
# "Europe/Berlin", "America/Argentina/Buenos_Aires", "Etc/GMT+5", "UTC"
_IANA_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)*")
_local_timezone: str | None = None


# --------------------------------------------------------------------------
# Errors and results
# --------------------------------------------------------------------------

class CalendarError(Exception):
    """A Calendar failure whose message is safe to show and log (no tokens, no client secret).

    ``problem`` is the google_auth PROBLEM_* of a sign-in failure ("" for anything else).
    """

    def __init__(self, message: str = "", *, status: int | None = None, problem: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.problem = problem


class CalendarSetupError(CalendarError):
    """The OAuth client secret file is missing or invalid, or the Calendar API is not enabled."""


class CalendarAuthError(CalendarError):
    """Sign-in was cancelled, denied, blocked or timed out, or the saved sign-in was revoked.

    When Google rejects the saved sign-in, the token file is deleted first, so
    the next call signs in again.
    """


class CalendarNotSignedIn(CalendarAuthError):
    """There is no usable saved sign-in and the call may not open the browser to get one.

    Raised by :meth:`GoogleCalendar.list_events` and by ``get_event(interactive=False)``,
    which only an explicit click may turn into a sign-in.
    """


class EventGone(CalendarError):
    """Google has no such event (404 / 410), or it was cancelled."""


class NotAllowed(CalendarError):
    """You may not do this to the event (not a guest, not the organizer, all-day, in the past...)."""


class CalendarUnknownOutcome(CalendarError):
    """A change was sent but no clear answer came back (5xx, timeout, connection lost):
    it may or may not have happened. Never retried by itself."""


class EventChanged(CalendarError):
    """An Undo found the event gone, or changed since Jarvis's change (its etag / updated stamp is
    not the one Jarvis's change left): nothing was changed."""


@dataclass(frozen=True)
class EventResult:
    event_id: str
    link: str            # htmlLink
    existed: bool        # True when a matching event was already on the calendar (nothing created)
    stamp: str = ""      # the new event's etag (else its updated time) as Google answered ("" = unknown)


@dataclass(frozen=True)
class CalendarEvent:
    """One event from :meth:`GoogleCalendar.list_events`.

    Timed events have time-zone-aware ``start``/``end`` (the offsets Google
    sent). All-day events have ``start``/``end`` None and ``all_day_start``
    plus an inclusive ``all_day_end`` instead (Google's end date is exclusive).
    ``link`` is the event's page on Google Calendar (htmlLink); ``location``
    is the event's location as written, which may be a meeting URL.
    """

    title: str
    start: datetime | None = None
    end: datetime | None = None
    all_day_start: date | None = None
    all_day_end: date | None = None
    location: str = ""
    link: str = ""
    calendar_id: str = "primary"

    @property
    def all_day(self) -> bool:
        return self.all_day_start is not None


@dataclass(frozen=True)
class EventDetails:
    """Google's own view of one event (:meth:`GoogleCalendar.get_event`).

    ``start``/``end`` are aware, in the calendar's time zone (None for all-day events, which
    have ``all_day_start`` and an inclusive ``all_day_end``). ``self_email`` is your attendee
    address ("" when you are not a guest); it, ``organizer`` and ``title`` are shown on the
    card only, never logged.
    """

    event_id: str
    calendar_id: str
    title: str
    start: datetime | None
    end: datetime | None
    all_day_start: date | None
    all_day_end: date | None
    status: str                    # "confirmed" | "tentative" | "cancelled"
    organizer_self: bool
    guests_can_modify: bool
    self_email: str
    self_response: str             # needsAction | accepted | declined | tentative | ""
    attendee_count: int
    recurring_instance: bool       # one occurrence of a repeating event
    series: bool                   # the repeating event itself (every occurrence)
    time_zone: str
    link: str
    organizer: str = ""            # the organizer's name or address ("" when unknown)
    self_comment: str = ""         # your note on your answer, if any
    organizer_email: str = ""      # the organizer's address ("" when unknown)
    stamp: str = ""                # the event's etag (else its updated time); an Undo compares it

    @property
    def account_email(self) -> str:
        """The address Google answered as: your attendee address, or the organizer's when you
        organize it ("" when unknown). Shown on the card only, never logged."""
        return self.self_email or (self.organizer_email if self.organizer_self else "")

    @property
    def all_day(self) -> bool:
        return self.start is None


@dataclass(frozen=True)
class ChangeResult:
    event_id: str
    link: str
    already: bool        # nothing needed changing (already answered / at that time / cancelled)
    stamp: str = ""      # the event's etag (else its updated time) right after the change ("" = unknown)
    # Google's own view of the event right before the change (an Undo puts back what it held): an RSVP's
    # answer and note, a Move's start and end. Not part of equality.
    before: EventDetails | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Guest:
    """One guest of an EventBrief (never a room, never you). Shown to Ask Jarvis, never logged."""

    email: str
    name: str = ""
    response: str = ""             # needsAction | accepted | declined | tentative | ""
    organizer: bool = False


@dataclass(frozen=True)
class EventBrief:
    """What Ask Jarvis may know about one event (:meth:`GoogleCalendar.list_event_briefs`).

    Only the id, calendar, title, times, who organizes it and the guests: never the description,
    the location, a meeting link or attachments. Timed events have aware ``start`` / ``end`` in
    the calendar's time zone; all-day events have ``all_day_start`` and an inclusive
    ``all_day_end``. ``recurring``: one occurrence of a repeating event (its own instance id).
    ``transparent``: marked "show as free" (transparency "transparent"); ``event_type``: Google's
    eventType as a plain word ("workingLocation", "outOfOffice", "focusTime"; "" for a default
    event); ``self_response``: the owner's own answer (the attendee entry with self: true, which is
    never one of the guests; "accepted" when he organizes it; "" when Google lists none for him);
    ``series_id``: the repeating event's id (``recurringEventId``) for an occurrence of one, so the
    productivity bundle can count a series' ignored prep offers (as a hash; R38), "" otherwise.
    """

    event_id: str
    calendar_id: str
    title: str
    start: datetime | None = None
    end: datetime | None = None
    all_day_start: date | None = None
    all_day_end: date | None = None
    organizer_self: bool = False
    organizer_email: str = ""
    organizer_name: str = ""
    guests: tuple[Guest, ...] = ()
    recurring: bool = False
    transparent: bool = False
    event_type: str = ""
    self_response: str = ""
    series_id: str = ""

    @property
    def all_day(self) -> bool:
        return self.all_day_start is not None

    @property
    def free(self) -> bool:
        """The event leaves its time free: marked "show as free", or a working-location entry."""
        return self.transparent or self.event_type == WORKING_LOCATION


WORKING_LOCATION = "workingLocation"


def default_token_path() -> Path:
    """%LOCALAPPDATA%\\briefing-reader\\google_token.json (the single sign-in of older versions)."""
    return default_data_dir() / TOKEN_FILE_NAME


def local_timezone() -> str:
    """This PC's time zone as an IANA name ("Europe/Berlin"), else LAST_RESORT_TIMEZONE.

    The fallback for when the calendar's own time zone setting cannot be read.
    Python has no time zone database here (no tzdata), so the name comes from
    ICU, which Windows 10 (1903 and later) and 11 ship as icu.dll and which maps
    the Windows time zone setting to its IANA name. Cached after the first call.
    """
    global _local_timezone
    if _local_timezone is None:
        _local_timezone = _icu_host_timezone() or LAST_RESORT_TIMEZONE
    return _local_timezone


def _icu_host_timezone() -> str | None:
    """ICU's default (host) time zone id, or None when it cannot be read."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        get_zone = ctypes.WinDLL("icu").ucal_getDefaultTimeZone
        get_zone.argtypes = [ctypes.c_wchar_p, ctypes.c_int32, ctypes.POINTER(ctypes.c_int)]
        get_zone.restype = ctypes.c_int32
        buffer = ctypes.create_unicode_buffer(128)
        status = ctypes.c_int(0)
        length = get_zone(buffer, len(buffer), ctypes.byref(status))
    except (OSError, AttributeError, ValueError, TypeError):
        return None
    if status.value > 0 or not 0 < length < len(buffer):   # ICU: an error code > 0 is a failure
        return None
    name = buffer.value[:length]
    if not _IANA_NAME_RE.fullmatch(name) or name == "Etc/Unknown":
        return None
    return name


_ICU_ZONE_OFFSET, _ICU_DST_OFFSET, _ICU_GREGORIAN = 15, 16, 1   # UCAL_ZONE_OFFSET, UCAL_DST_OFFSET, UCAL_GREGORIAN
_icu_calls: Any = None
_icu_lock = threading.Lock()


def zone_offset(zone: str, moment: datetime) -> timedelta | None:
    """The UTC offset of the IANA ``zone`` at the instant ``moment`` (a naive moment is read as this
    PC's local time), daylight saving included; None when it can't be known. From Python's time
    zone database when there is one, else from Windows' ICU (icu.dll, as :func:`local_timezone`;
    Python has no tzdata here), else this PC's own rules when ``zone`` is this PC's zone. Never a
    request to Google; nothing is logged."""
    name = str(zone or "").strip()
    if not name or not _IANA_NAME_RE.fullmatch(name):
        return None
    instant = _as_aware(moment)
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:   # pragma: no cover - always there on 3.13
        ZoneInfo, ZoneInfoNotFoundError = None, Exception   # type: ignore[assignment,misc]
    if ZoneInfo is not None:
        try:
            return instant.astimezone(ZoneInfo(name)).utcoffset()
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    found = _icu_offset(name, instant)
    if found is not None:
        return found
    if name == local_timezone():
        return instant.astimezone().utcoffset()
    return None


def wall_in_zone(moment: datetime, zone: str) -> datetime:
    """``moment`` as a naive wall time in the IANA ``zone`` (zone_offset); a naive moment is a wall
    time already, and a moment whose zone's offset can't be known keeps its own wall time."""
    if moment.utcoffset() is None:
        return moment
    offset = zone_offset(zone, moment)
    if offset is None:
        return moment.replace(tzinfo=None)
    return (moment.astimezone(timezone.utc) + offset).replace(tzinfo=None)


def _icu_functions() -> Any:
    """icu.dll's ucal functions this module uses (False when there is no ICU)."""
    global _icu_calls
    with _icu_lock:
        if _icu_calls is not None:
            return _icu_calls
        _icu_calls = False
        if os.name != "nt":
            return _icu_calls
        try:
            import ctypes

            icu = ctypes.WinDLL("icu")
            status = ctypes.POINTER(ctypes.c_int)
            canonical = icu.ucal_getCanonicalTimeZoneID
            canonical.argtypes = [ctypes.c_wchar_p, ctypes.c_int32, ctypes.c_wchar_p, ctypes.c_int32,
                                  ctypes.POINTER(ctypes.c_byte), status]
            canonical.restype = ctypes.c_int32
            open_ = icu.ucal_open
            open_.argtypes = [ctypes.c_wchar_p, ctypes.c_int32, ctypes.c_char_p, ctypes.c_int, status]
            open_.restype = ctypes.c_void_p
            set_millis = icu.ucal_setMillis
            set_millis.argtypes = [ctypes.c_void_p, ctypes.c_double, status]
            set_millis.restype = None
            get = icu.ucal_get
            get.argtypes = [ctypes.c_void_p, ctypes.c_int, status]
            get.restype = ctypes.c_int32
            close = icu.ucal_close
            close.argtypes = [ctypes.c_void_p]
            close.restype = None
        except (OSError, AttributeError, ValueError, TypeError):
            return _icu_calls
        _icu_calls = (ctypes, canonical, open_, set_millis, get, close)
        return _icu_calls


def _icu_offset(zone: str, instant: datetime) -> timedelta | None:
    calls = _icu_functions()
    if not calls:
        return None
    ctypes, canonical, open_, set_millis, get, close = calls
    try:
        buffer = ctypes.create_unicode_buffer(128)
        system = ctypes.c_byte(0)
        status = ctypes.c_int(0)
        canonical(zone, len(zone), buffer, len(buffer), ctypes.byref(system), ctypes.byref(status))
        if status.value > 0 or not system.value:   # not a zone ICU knows
            return None
        status = ctypes.c_int(0)
        handle = open_(zone, len(zone), None, _ICU_GREGORIAN, ctypes.byref(status))
        if not handle or status.value > 0:
            return None
        try:
            set_millis(handle, instant.timestamp() * 1000.0, ctypes.byref(status))
            raw = get(handle, _ICU_ZONE_OFFSET, ctypes.byref(status))
            dst = get(handle, _ICU_DST_OFFSET, ctypes.byref(status))
        finally:
            close(handle)
    except (OSError, ValueError, TypeError, OverflowError):
        return None
    if status.value > 0:
        return None
    return timedelta(milliseconds=int(raw) + int(dst))


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

class GoogleCalendar:
    """Google Calendar calls for one account. Blocking: use a worker thread.

    Calls that talk to Google are serialised by an internal lock.
    :meth:`is_configured` and :meth:`is_signed_in` never take that lock or
    touch the network, so the UI thread may call them while a sign-in waits.

    The changes (create_event, respond, move, cancel) take ``on_request(method, params)``: it hears
    the exact request (a copy) right before it goes to Google, for the LIVE view; it is never called
    when nothing is sent (already so), and a hook that fails changes nothing.
    """

    REQUEST_HOOK = True   # the changes take on_request= (executor passes it only then)

    def __init__(self, *, client_secret_path: Path | None = None, token_path: Path | None = None,
                 calendar_id: str = "primary",
                 service_factory: Callable[[Any], Any] | None = None,
                 flow_factory: Callable[[Path, list[str]], Any] | None = None,
                 open_timeout_s: int = 300, account: GoogleAccount | None = None) -> None:
        if account is None:
            if client_secret_path is None or token_path is None:
                raise ValueError("GoogleCalendar needs an account, or client_secret_path and token_path")
            # The one-account setup of older versions: its log lines and messages are kept, and
            # tests patch this module's _refresh_credentials / google_libraries_available.
            account = GoogleAccount(
                "", client_secret_path=Path(client_secret_path), token_path=Path(token_path),
                features=(CALENDAR_FEATURE,), flow_factory=flow_factory or _installed_app_flow,
                open_timeout_s=open_timeout_s, setup_hint=SETUP_HINT,
                success_message=SIGN_IN_SUCCESS_MESSAGE, log=logger,
                refresh=lambda creds: _refresh_credentials(creds),
                libraries_available=lambda: google_libraries_available())
        self._account = account
        self._calendar_id = calendar_id
        self._service_factory = service_factory or _build_service
        self._lock = threading.RLock()
        self._depth = 0   # nesting of public calls, so a failure is logged once
        self._creds: Any = None
        self._service: Any = None
        self._service_generation = -1
        self._timezone: str | None = None

    @property
    def calendar_id(self) -> str:
        return self._calendar_id

    @property
    def token_path(self) -> Path:
        return self._account.token_path

    @property
    def account(self) -> GoogleAccount:
        return self._account

    @property
    def alias(self) -> str:
        """The account alias ("" for the one-account setup of older versions)."""
        return self._account.alias

    def pending_confirmation(self) -> str:
        """The bound Google account's address while you have not confirmed it for this alias ("" when
        confirmed or not bound; google_auth). Jarvis changes no event meanwhile; reading goes on.
        No network, no lock."""
        return self._account.pending_confirmation()

    def confirm_account(self, address: str) -> bool:
        """You confirmed that ``address`` is this alias's Google account (accounts.json)."""
        return self._account.confirm_binding(address)

    def change_problem(self) -> tuple[str, str]:
        """Why Jarvis may not change anything on this alias's calendar now ((PROBLEM_*, message);
        ("", "") when it may): google_auth.GoogleAccount.change_problem, which fails closed.
        Reading is never held back. No network, no lock."""
        return self._account.change_problem()

    def needs_confirmation(self) -> bool:
        """The alias has no binding you confirmed: its next sign-in leaves one to confirm first."""
        return self._account.needs_confirmation()

    def disconnect(self) -> None:
        """Forget the saved sign-in and which Google account the alias is (the next sign-in binds
        again). Raises google_auth.AccountError, the binding kept, when the saved sign-in could
        not be deleted."""
        with self._lock:
            try:
                self._account.disconnect()
            finally:
                self._creds = None
                self._service = None
                self._timezone = None

    def _require_change_allowed(self) -> None:
        """Right before a change goes to Google (after any sign-in): refused unless the saved
        sign-in is the alias's confirmed Google account (CalendarAuthError with the problem;
        executor.CalendarBackend checks this first, before "running" is saved)."""
        problem, message = self._account.change_problem()
        if problem:
            raise CalendarAuthError(message or f"Nothing was changed for the {self.alias} account",
                                    problem=problem)

    @contextmanager
    def _guarded(self) -> Iterator[None]:
        """Hold the lock; log a CalendarError once, at the outermost public call.

        CalendarNotSignedIn is the normal state before the first sign-in (the
        agenda asks every few minutes), and EventGone / NotAllowed are answers
        about one event, not failures of the app, so those are logged at INFO.
        """
        with self._lock:
            self._depth += 1
            try:
                yield
            except CalendarError as exc:
                if self._depth == 1:
                    quiet = isinstance(exc, (CalendarNotSignedIn, EventGone, NotAllowed))
                    log = logger.info if quiet else logger.warning
                    # The message names the account (shown on the card); the log writes an
                    # alias other than work / personal as "other".
                    log("%s call failed (%s): %s", self._log_name(), type(exc).__name__,
                        scrub_alias(str(exc), self.alias))
                raise
            finally:
                self._depth -= 1

    def _log_name(self) -> str:
        return f"Google Calendar ({logged_alias(self.alias)})" if self.alias else "Google Calendar"

    @contextmanager
    def _account_errors(self) -> Iterator[None]:
        """google_auth's AccountErrors as the matching CalendarErrors (same message and problem)."""
        try:
            yield
        except AccountSetupError as exc:
            raise CalendarSetupError(str(exc), status=exc.status, problem=PROBLEM_SETUP) from None
        except AccountNotSignedIn:
            raise CalendarNotSignedIn(self._not_signed_in_message(), problem=PROBLEM_SIGNED_OUT) from None
        except AccountAuthError as exc:
            raise CalendarAuthError(str(exc), status=exc.status, problem=exc.problem) from None
        except AccountError as exc:
            raise CalendarError(str(exc), status=exc.status, problem=exc.problem) from None

    def _not_signed_in_message(self) -> str:
        if self.alias:
            return f"{NOT_SIGNED_IN_MESSAGE} ({self.alias} account)"
        return NOT_SIGNED_IN_MESSAGE

    def is_configured(self) -> bool:
        """True when the OAuth client secret file exists."""
        return self._account.is_configured()

    def is_signed_in(self) -> bool:
        """True when a saved sign-in exists that is valid or can be refreshed and allows Calendar."""
        return self._account.is_signed_in(CALENDAR_FEATURE)

    def sign_in_problem(self) -> tuple[str, str]:
        """(google_auth PROBLEM_*, message) of the last failed sign-in or rejected token, or ("", "")."""
        return self._account.problem()

    # ---- sign-in -----------------------------------------------------------

    def sign_in(self) -> None:
        """Open the Google sign-in in the browser and save the token.

        Blocks until the user finishes or ``open_timeout_s`` passes. Raises
        CalendarSetupError (no usable client secret), CalendarAuthError
        (cancelled, denied, blocked, timed out, a permission unticked) or
        CalendarError.
        """
        with self._guarded():
            _require_libraries()
            with self._account_errors():
                self._account.sign_in()
            self._creds = None
            self._service = None
            self._timezone = None
            if CALENDAR_FEATURE not in self._account.granted_features():
                problem, message = self._account.problem()
                raise CalendarAuthError(message or self._account.missing_message(CALENDAR_FEATURE),
                                        problem=problem or PROBLEM_FAILED)

    # ---- credentials and service -------------------------------------------

    def _get_service(self, *, interactive: bool = True) -> Any:
        if not self.is_configured():
            raise CalendarSetupError(
                f"{SETUP_HINT} ({self._account.client_secret_path.name} was not found)",
                problem=PROBLEM_SETUP)
        _require_libraries()
        with self._account_errors():
            creds = self._account.credentials(interactive=interactive, need=CALENDAR_FEATURE)
        if self._service is None or creds is not self._creds \
                or self._service_generation != self._account.generation:
            try:
                self._service = self._service_factory(creds)
            except Exception as exc:  # noqa: BLE001 - mapped to a safe message
                raise CalendarError(
                    f"Could not start the Google Calendar client ({type(exc).__name__})") from None
            self._creds = creds
            self._service_generation = self._account.generation
        return self._service

    def _execute(self, request: Any, what: str, *, retries: int = 0, forgiving: bool = False,
                 mutation: bool = False, event_call: bool = False) -> Any:
        """Run one API request, mapping every failure to a CalendarError.

        ``mutation``: the request changes something (never retried; a 5xx or a lost answer is
        CalendarUnknownOutcome). ``event_call``: it names one event, so 404 / 410 mean
        EventGone and a 403 about that event means NotAllowed (the token stays).
        """
        from googleapiclient.errors import HttpError

        try:
            result = request.execute(num_retries=retries)
        except HttpError as exc:
            raise self._http_error(exc, what, forgiving=forgiving, mutation=mutation,
                                   event_call=event_call) from None
        except Exception as exc:  # noqa: BLE001 - socket/ssl/httplib2 errors share no base
            raise self._transport_error(exc, what, mutation=mutation) from None
        if self._creds is not None:
            _register_credentials(self._creds)   # the library may have refreshed the token
        return result

    def _http_error(self, exc: Any, what: str, *, forgiving: bool, mutation: bool = False,
                    event_call: bool = False) -> CalendarError:
        """401/403 -> CalendarAuthError (token deleted); other statuses -> CalendarError.

        403s about quota stay CalendarError and a disabled Calendar API is a
        CalendarSetupError; neither deletes the token. With ``forgiving`` (a
        read that has a fallback) only 401 is treated as a sign-in problem.
        """
        status = _status_of(exc)
        message = _google_message(exc)
        detail = f"{status}: {message}" if message else str(status)
        reasons = _error_reasons(exc)
        if event_call and status in (404, 410):
            return EventGone(f"Google Calendar has no such event ({status})", status=status)
        if status == 403 and reasons & _SETUP_REASONS:
            return CalendarSetupError(
                f"{SETUP_HINT}: the Google Calendar API is not enabled ({detail})", status=status,
                problem=PROBLEM_SETUP)
        if event_call and status == 403 and reasons & _EVENT_REFUSED_REASONS:
            return NotAllowed(f"Google Calendar refused to change this event ({detail})", status=status)
        blocked = blocked_code(f"{message} {' '.join(sorted(reasons))}")
        if blocked and status in (401, 403):
            error = self._account.rejected(blocked)
            return CalendarAuthError(str(error), status=status, problem=error.problem)
        auth_problem = status == 401 or (
            status == 403 and not forgiving and not reasons & _RATE_LIMIT_REASONS)
        if auth_problem:
            if self.alias:
                text = (f"Google Calendar refused access to the {self.alias} account while {what} "
                        f"({detail}); sign in again")
            else:
                text = (f"Google Calendar refused access while {what} ({detail}). "
                        "Approve again to sign in.")
            self._account.forget(f"Google answered {status}", problem=PROBLEM_EXPIRED, message=text)
            return CalendarAuthError(text, status=status, problem=PROBLEM_EXPIRED)
        if mutation and status >= 500:
            return CalendarUnknownOutcome(
                f"Google Calendar answered {detail} while {what}; it may or may not have "
                "happened - check the calendar before retrying", status=status)
        return CalendarError(f"Google Calendar error while {what} ({detail})", status=status)

    def _transport_error(self, exc: Exception, what: str, *, mutation: bool = False) -> CalendarError:
        """A non-HTTP failure: a rejected token refresh (token deleted) or the network."""
        from google.auth.exceptions import RefreshError

        if isinstance(exc, RefreshError):
            if exc.retryable:
                return CalendarError("Google could not refresh the sign-in just now; try again")
            error = self._account.rejected(str(exc))
            return CalendarAuthError(str(error), problem=error.problem)
        name = type(exc).__name__
        if mutation and not (isinstance(exc, _NOT_SENT_ERRORS) or name in _NOT_SENT_NAMES):
            return CalendarUnknownOutcome(
                f"No answer from Google Calendar while {what} ({name}); it may or may not have "
                "happened - check the calendar before retrying")
        return CalendarError(f"Could not reach Google Calendar while {what} ({name})")

    # ---- calendar ------------------------------------------------------------

    def timezone(self, *, interactive: bool = True) -> str:
        """The calendar's time zone setting (cached), else this PC's zone with a warning.

        The fallback (:func:`local_timezone`) is not cached here, so the next
        call asks Google again.
        """
        with self._guarded():
            if self._timezone is None:
                value = self._read_timezone(interactive=interactive)
                if value is None:
                    return local_timezone()
                self._timezone = value
            return self._timezone

    def cached_timezone(self) -> str | None:
        """The time zone setting only when :meth:`timezone` read it already (None otherwise):
        never a request to Google (web research is told the zone without one)."""
        return self._timezone

    def _read_timezone(self, *, interactive: bool = True) -> str | None:
        service = self._get_service(interactive=interactive)
        try:
            setting = self._execute(service.settings().get(setting="timezone"),
                                    "reading your time zone", retries=_READ_RETRIES,
                                    forgiving=True)
        except (CalendarAuthError, CalendarSetupError):
            raise
        except CalendarError as exc:
            logger.warning("Google Calendar: could not read your time zone (%s); using this "
                           "PC's time zone, %s", scrub_alias(str(exc), self.alias), local_timezone())
            return None
        value = setting.get("value") if isinstance(setting, dict) else None
        if not isinstance(value, str) or not value.strip():
            logger.warning("Google Calendar: your settings have no time zone; using this PC's "
                           "time zone, %s", local_timezone())
            return None
        return value.strip()

    def find_existing(self, action: ProposedAction) -> dict | None:
        """The event already on the calendar with the same title and start, else None."""
        with self._guarded():
            _require_actionable(action)
            return self._find_existing(action, self.timezone())

    def _find_existing(self, action: ProposedAction, tz: str, *, interactive: bool = True) -> dict | None:
        """events.list around the first occurrence, rendered in ``tz``; match title + start."""
        service = self._get_service(interactive=interactive)
        title = action.title.strip().casefold()
        start = _start_key(action)
        time_min, time_max = _search_window(action)
        page_token: str | None = None
        for _ in range(_MAX_LIST_PAGES):
            query: dict[str, Any] = {
                "calendarId": self._calendar_id, "timeMin": time_min, "timeMax": time_max,
                "singleEvents": True, "q": action.title.strip(), "timeZone": tz,
                "maxResults": 50}
            if page_token:
                query["pageToken"] = page_token
            page = self._execute(service.events().list(**query), "checking the calendar",
                                 retries=_READ_RETRIES)
            page = page if isinstance(page, dict) else {}
            for event in page.get("items") or ():
                if isinstance(event, dict) and _matches(event, title, start):
                    return event
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        return None

    def create_event(self, action: ProposedAction, *, interactive: bool = True,
                     on_request: RequestHook | None = None, footer: str = EVENT_FOOTER) -> EventResult:
        """Add ``action`` to the calendar unless a matching event is already there (its description
        ends with ``footer``: the card's event_footer).

        Signs in first when there is no saved sign-in (without ``interactive``:
        CalendarNotSignedIn instead, and nothing opens). The insert is never
        retried: a 5xx answer or no answer after it went out raises
        CalendarUnknownOutcome (a retry the user asks for finds the event).
        Raises CalendarError (or a subclass) with a message that is safe to show.
        """
        with self._guarded():
            _require_actionable(action)
            tz = self.timezone(interactive=interactive)
            existing = self._find_existing(action, tz, interactive=interactive)
            if existing is not None:
                event_id = str(existing.get("id") or "")
                logger.info("Google Calendar: already on the calendar (event %s)", event_id)
                return EventResult(event_id=event_id, link=str(existing.get("htmlLink") or ""),
                                   existed=True)
            service = self._get_service(interactive=interactive)
            self._require_change_allowed()
            body = build_event_body(action, tz, footer=footer)
            request = service.events().insert(calendarId=self._calendar_id, body=body)
            _tell_request(on_request, "events.insert", {"calendarId": self._calendar_id, "body": body})
            created = self._execute(request, "adding the event", mutation=True)
            created = created if isinstance(created, dict) else {}
            event_id = str(created.get("id") or "")
            logger.info("Google Calendar: created event %s", event_id or "(no id returned)")
            return EventResult(event_id=event_id, link=str(created.get("htmlLink") or ""),
                               existed=False, stamp=_stamp(created))

    def list_events(self, start: datetime, end: datetime,
                    calendar_ids: Sequence[str] | str = ("primary",)) -> list[CalendarEvent]:
        """The events between ``start`` and ``end`` on each calendar, sorted by start.

        Never signs in: without a usable saved sign-in this raises
        CalendarNotSignedIn before any request and opens nothing. Naive bounds
        are local time. Cancelled events are skipped and an event that is on
        two of the calendars is listed once. A calendar that fails with a plain
        CalendarError (say, an unknown id) is skipped with a warning when
        another one answered; sign-in and setup problems always raise.
        """
        ids = _calendar_ids(calendar_ids)
        with self._guarded():
            if not ids or _as_aware(end) <= _as_aware(start):
                return []
            service = self._get_service(interactive=False)
            time_min, time_max = _rfc3339(start), _rfc3339(end)
            events: list[CalendarEvent] = []
            seen: set[tuple[str, str]] = set()
            failures: list[CalendarError] = []
            for calendar_id in ids:
                try:
                    items = self._list_calendar(service, calendar_id, time_min, time_max)
                except (CalendarAuthError, CalendarSetupError):
                    raise
                except CalendarError as exc:
                    failures.append(exc)
                    continue
                events.extend(_new_events(items, calendar_id, seen))
            if failures and len(failures) == len(ids):
                raise failures[0]
            for exc in failures:
                logger.warning("Google Calendar: skipped a calendar that could not be read (%s)",
                               exc)
            events.sort(key=_event_sort_key)
            logger.debug("Google Calendar: %d event(s) from %d calendar(s)", len(events),
                         len(ids) - len(failures))
            return events

    def _list_calendar(self, service: Any, calendar_id: str, time_min: str,
                       time_max: str) -> list[dict]:
        """Every events.list item of one calendar (up to _MAX_LIST_PAGES pages)."""
        items: list[dict] = []
        page_token: str | None = None
        for _ in range(_MAX_LIST_PAGES):
            query: dict[str, Any] = {
                "calendarId": calendar_id, "timeMin": time_min, "timeMax": time_max,
                "singleEvents": True, "orderBy": "startTime", "maxResults": _LIST_PAGE_SIZE,
                "showDeleted": False}
            if page_token:
                query["pageToken"] = page_token
            page = self._execute(service.events().list(**query), "reading your calendar",
                                 retries=_READ_RETRIES)
            page = page if isinstance(page, dict) else {}
            items.extend(item for item in page.get("items") or () if isinstance(item, dict))
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        return items

    def list_event_briefs(self, start: datetime, end: datetime,
                          calendar_ids: Sequence[str] | str = ("primary",)) -> list[EventBrief]:
        """What Ask Jarvis may see of the events between ``start`` and ``end`` on each calendar
        (EventBrief), sorted by start. Read only, and like list_events it never signs in
        (CalendarNotSignedIn before any request). Each occurrence of a repeating event is its own
        brief (singleEvents), with times in the calendar's time zone. Google is asked only for the
        id, status, title, times, organizer, guests and the series id (a ``fields`` mask): never a
        description, location, meeting link or attachment. Cancelled events, rooms and your own
        guest entry are left out. A calendar that fails with a plain CalendarError is skipped
        (with a warning) when another one answered."""
        ids = _calendar_ids(calendar_ids)
        with self._guarded():
            if not ids or _as_aware(end) <= _as_aware(start):
                return []
            service = self._get_service(interactive=False)
            zone = self.timezone(interactive=False)
            time_min, time_max = _rfc3339(start), _rfc3339(end)
            briefs: list[EventBrief] = []
            seen: set[tuple[str, str]] = set()
            failures: list[CalendarError] = []
            for calendar_id in ids:
                try:
                    items = self._list_briefs(service, calendar_id, time_min, time_max, zone)
                except (CalendarAuthError, CalendarSetupError):
                    raise
                except CalendarError as exc:
                    failures.append(exc)
                    continue
                for item in items:
                    brief = _brief_from(item, calendar_id)
                    if brief is not None and (calendar_id, brief.event_id) not in seen:
                        seen.add((calendar_id, brief.event_id))
                        briefs.append(brief)
            if failures and len(failures) == len(ids):
                raise failures[0]
            for exc in failures:
                logger.warning("Google Calendar: skipped a calendar that could not be read (%s)", exc)
            briefs.sort(key=_brief_sort_key)
            logger.debug("Google Calendar: %d event brief(s) from %d calendar(s)", len(briefs),
                         len(ids) - len(failures))
            return briefs

    def _list_briefs(self, service: Any, calendar_id: str, time_min: str, time_max: str,
                     zone: str) -> list[dict]:
        items: list[dict] = []
        page_token: str | None = None
        for _ in range(_MAX_LIST_PAGES):
            query: dict[str, Any] = {
                "calendarId": calendar_id, "timeMin": time_min, "timeMax": time_max,
                "singleEvents": True, "orderBy": "startTime", "maxResults": _LIST_PAGE_SIZE,
                "showDeleted": False, "timeZone": zone, "fields": BRIEF_FIELDS}
            if page_token:
                query["pageToken"] = page_token
            page = self._execute(service.events().list(**query), "reading your calendar",
                                 retries=_READ_RETRIES)
            page = page if isinstance(page, dict) else {}
            items.extend(item for item in page.get("items") or () if isinstance(item, dict))
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        return items

    # ---- one event: read, answer, move, cancel ---------------------------------------------

    def get_event(self, event_id: str, *, calendar_id: str | None = None,
                  interactive: bool = True) -> EventDetails:
        """Google's own view of one event (events.get; a read, retried like the others).

        Raises EventGone when Google has no such event or it was cancelled. Without
        ``interactive`` a missing sign-in raises CalendarNotSignedIn and opens nothing.
        """
        calendar = calendar_id or self._calendar_id
        with self._guarded():
            service = self._get_service(interactive=interactive)
            item = self._execute(service.events().get(calendarId=calendar, eventId=event_id),
                                 "reading the event", retries=_READ_RETRIES, event_call=True)
            details = _event_details(item if isinstance(item, dict) else {}, event_id, calendar)
            if details.status == "cancelled":
                raise EventGone("The event was cancelled")
            return details

    def respond(self, event_id: str, answer: str, *, comment: str = "", send_updates: str = "all",
                calendar_id: str | None = None, interactive: bool = True,
                on_request: RequestHook | None = None) -> ChangeResult:
        """Answer an invitation ("yes" / "no" / "maybe"), with an optional note to the organizer.

        events.patch with ``attendeesOmitted`` and only your own attendee entry, so nobody
        else's answer is touched. ``already`` when Google has that answer (and note) already.
        """
        status = RESPONSES.get(answer)
        if status is None:
            raise CalendarError("The answer must be yes, no or maybe")
        updates = _send_updates(send_updates)
        calendar = calendar_id or self._calendar_id
        with self._guarded():
            details = self.get_event(event_id, calendar_id=calendar, interactive=interactive)
            if not details.self_email:
                raise NotAllowed("You are not on this event's guest list")
            if details.self_response == status and (not comment or comment == details.self_comment):
                logger.info("%s: invitation already answered (event %s)", self._log_name(), event_id)
                return ChangeResult(event_id=event_id, link=details.link, already=True)
            attendee: dict[str, Any] = {"email": details.self_email, "responseStatus": status}
            if comment:
                attendee["comment"] = comment
            service = self._get_service(interactive=interactive)
            self._require_change_allowed()
            body = {"attendeesOmitted": True, "attendees": [attendee]}
            request = service.events().patch(calendarId=calendar, eventId=event_id, sendUpdates=updates, body=body)
            _tell_request(on_request, "events.patch", {"calendarId": calendar, "eventId": event_id,
                                                       "sendUpdates": updates, "body": body})
            result = self._execute(request, "answering the invitation", mutation=True, event_call=True)
            result = result if isinstance(result, dict) else {}
            logger.info("%s: answered the invitation (event %s)", self._log_name(), event_id)
            return ChangeResult(event_id=event_id, link=str(result.get("htmlLink") or details.link),
                                already=False, stamp=_stamp(result), before=details)

    def move(self, event_id: str, start: datetime, end: datetime, *, send_updates: str = "all",
             calendar_id: str | None = None, now: datetime | None = None,
             interactive: bool = True, on_request: RequestHook | None = None) -> ChangeResult:
        """Give an event you organize (or may modify) a new start and end.

        ``start``/``end`` are naive wall times in the calendar's time zone (as the briefing
        writes them and the card shows them); the new start and end are sent as those wall
        times in that zone, so an event kept in another zone moves to the calendar's zone
        (Python has no time zone database here to convert). Refused for events you neither
        organize nor may modify, all-day events, a whole repeating series and a new start in
        the past. ``already`` when the event is at that time already.
        """
        start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
        if end <= start:
            raise CalendarError("The new end is not after the new start")
        updates = _send_updates(send_updates)
        calendar = calendar_id or self._calendar_id
        with self._guarded():
            details = self.get_event(event_id, calendar_id=calendar, interactive=interactive)
            if details.series:
                raise NotAllowed("This is a whole repeating series; Jarvis moves single events or "
                                 "occurrences only - open it in Google Calendar")
            if not (details.organizer_self or details.guests_can_modify):
                raise NotAllowed("You don't organize this event, so it can't be moved - "
                                 "answer the invitation instead")
            if details.start is None or details.end is None:
                raise NotAllowed("All-day events can't be moved from here")
            current = (_wall(details.start), _wall(details.end))
            if current == (start.replace(second=0, microsecond=0), end.replace(second=0, microsecond=0)):
                logger.info("%s: the event is at that time already (event %s)", self._log_name(), event_id)
                return ChangeResult(event_id=event_id, link=details.link, already=True)
            if _as_aware(start) <= (now or datetime.now().astimezone()):
                raise NotAllowed("The new time has already begun")
            tz = self.timezone(interactive=interactive)
            service = self._get_service(interactive=interactive)
            self._require_change_allowed()
            body = {"start": {"dateTime": _wall_time(start), "timeZone": tz},
                    "end": {"dateTime": _wall_time(end), "timeZone": tz}}
            request = service.events().patch(calendarId=calendar, eventId=event_id, sendUpdates=updates, body=body)
            _tell_request(on_request, "events.patch", {"calendarId": calendar, "eventId": event_id,
                                                       "sendUpdates": updates, "body": body})
            result = self._execute(request, "moving the event", mutation=True, event_call=True)
            result = result if isinstance(result, dict) else {}
            logger.info("%s: moved event %s", self._log_name(), event_id)
            return ChangeResult(event_id=event_id, link=str(result.get("htmlLink") or details.link),
                                already=False, stamp=_stamp(result), before=details)

    def cancel(self, event_id: str, *, send_updates: str = "all",
               calendar_id: str | None = None, interactive: bool = True,
               on_request: RequestHook | None = None) -> ChangeResult:
        """Cancel (delete) an event you organize; guests are told as ``send_updates`` says.

        ``already`` when Google has no such event any more. Refused for events you do not
        organize (decline those instead) and for a whole repeating series.
        """
        updates = _send_updates(send_updates)
        calendar = calendar_id or self._calendar_id
        with self._guarded():
            try:
                details = self.get_event(event_id, calendar_id=calendar, interactive=interactive)
            except EventGone:
                logger.info("%s: the event is gone already (event %s)", self._log_name(), event_id)
                return ChangeResult(event_id=event_id, link="", already=True)
            if details.series:
                raise NotAllowed("This is a whole repeating series; Jarvis cancels single events or "
                                 "occurrences only - open it in Google Calendar")
            if not details.organizer_self:
                raise NotAllowed("You don't organize this event - decline it instead")
            service = self._get_service(interactive=interactive)
            self._require_change_allowed()
            request = service.events().delete(calendarId=calendar, eventId=event_id,
                                              sendUpdates=updates)
            _tell_request(on_request, "events.delete", {"calendarId": calendar, "eventId": event_id,
                                                        "sendUpdates": updates})
            try:
                self._execute(request, "cancelling the event", mutation=True, event_call=True)
            except EventGone:
                return ChangeResult(event_id=event_id, link="", already=True)
            logger.info("%s: cancelled event %s", self._log_name(), event_id)
            return ChangeResult(event_id=event_id, link="", already=False)

    # ---- Undo: reverse exactly Jarvis's own change (only on the owner's click on that card's Undo) ----

    def _unchanged(self, event_id: str, calendar: str, stamp: str, *, uncertain: bool) -> EventDetails | None:
        """Google's view of the event while it is still exactly as Jarvis's change left it (its stamp is
        ``stamp``). EventChanged when it is gone or changed since; None (nothing to do) when
        ``uncertain`` (an earlier Undo may have happened) and it is gone."""
        try:
            details = self.get_event(event_id, calendar_id=calendar, interactive=False)
        except EventGone:
            if uncertain:
                return None
            raise EventChanged("The event is gone since Jarvis's change") from None
        if not stamp or details.stamp != stamp:
            raise EventChanged("The event changed since Jarvis's change")
        return details

    def undo_create(self, event_id: str, *, stamp: str, send_updates: str = "none",
                    calendar_id: str | None = None, uncertain: bool = False,
                    on_request: RequestHook | None = None) -> ChangeResult:
        """Delete the event Jarvis created (exactly ``event_id``) unless it is gone or changed since
        (EventChanged). ``send_updates``: the insert's own policy (it told nobody: "none"). Never
        signs in. ``uncertain``: an earlier Undo may already have deleted it (gone counts as done)."""
        updates = _send_updates(send_updates)
        calendar = calendar_id or self._calendar_id
        with self._guarded():
            details = self._unchanged(event_id, calendar, stamp, uncertain=uncertain)
            if details is None:
                logger.info("%s: the created event is gone already (event %s)", self._log_name(), event_id)
                return ChangeResult(event_id=event_id, link="", already=True)
            service = self._get_service(interactive=False)
            self._require_change_allowed()
            request = service.events().delete(calendarId=calendar, eventId=event_id, sendUpdates=updates)
            _tell_request(on_request, "events.delete", {"calendarId": calendar, "eventId": event_id,
                                                        "sendUpdates": updates})
            self._execute(request, "removing the event", mutation=True, event_call=True)
            logger.info("%s: removed event %s (undo)", self._log_name(), event_id)
            return ChangeResult(event_id=event_id, link="", already=False)

    def undo_respond(self, event_id: str, response: str, *, comment: str = "", stamp: str,
                     send_updates: str = "all", calendar_id: str | None = None, uncertain: bool = False,
                     on_request: RequestHook | None = None) -> ChangeResult:
        """Put your answer to an invitation back to ``response`` (Google's own word from before Jarvis's
        answer: needsAction / accepted / declined / tentative) and your note back to ``comment``, with
        the answer's own ``send_updates``; only your attendee entry is sent. EventChanged when the
        event is gone or changed since. Never signs in."""
        if response not in ("needsAction", "accepted", "declined", "tentative"):
            raise CalendarError("There is no earlier answer to put back")
        updates = _send_updates(send_updates)
        calendar = calendar_id or self._calendar_id
        with self._guarded():
            try:
                details = self._unchanged(event_id, calendar, stamp, uncertain=False)
            except EventChanged:
                if uncertain and self._answer_is(event_id, calendar, response, comment):
                    return ChangeResult(event_id=event_id, link="", already=True)
                raise
            if details is None or not details.self_email:
                raise EventChanged("You are no longer on this event's guest list")
            attendee: dict[str, Any] = {"email": details.self_email, "responseStatus": response,
                                        "comment": comment}
            service = self._get_service(interactive=False)
            self._require_change_allowed()
            body = {"attendeesOmitted": True, "attendees": [attendee]}
            request = service.events().patch(calendarId=calendar, eventId=event_id, sendUpdates=updates, body=body)
            _tell_request(on_request, "events.patch", {"calendarId": calendar, "eventId": event_id,
                                                       "sendUpdates": updates, "body": body})
            result = self._execute(request, "putting your answer back", mutation=True, event_call=True)
            result = result if isinstance(result, dict) else {}
            logger.info("%s: put the answer back (event %s, undo)", self._log_name(), event_id)
            return ChangeResult(event_id=event_id, link=str(result.get("htmlLink") or details.link),
                                already=False, stamp=_stamp(result))

    def _answer_is(self, event_id: str, calendar: str, response: str, comment: str) -> bool:
        try:
            details = self.get_event(event_id, calendar_id=calendar, interactive=False)
        except EventGone:
            return False
        return details.self_response == response and details.self_comment == comment

    def undo_move(self, event_id: str, start: datetime, end: datetime, *, time_zone: str = "", stamp: str,
                  send_updates: str = "all", calendar_id: str | None = None, uncertain: bool = False,
                  on_request: RequestHook | None = None) -> ChangeResult:
        """Move the event back to ``start`` / ``end`` (aware: the times Google had before Jarvis's move,
        kept in ``time_zone`` when known), with the move's own ``send_updates``. EventChanged when it
        is gone or changed since. Never signs in."""
        updates = _send_updates(send_updates)
        calendar = calendar_id or self._calendar_id
        with self._guarded():
            try:
                details = self._unchanged(event_id, calendar, stamp, uncertain=False)
            except EventChanged:
                if uncertain and self._time_is(event_id, calendar, start, end):
                    return ChangeResult(event_id=event_id, link="", already=True)
                raise
            if details is None:
                raise EventChanged("The event is gone since Jarvis's change")
            service = self._get_service(interactive=False)
            self._require_change_allowed()
            body = {"start": _instant_body(start, time_zone), "end": _instant_body(end, time_zone)}
            request = service.events().patch(calendarId=calendar, eventId=event_id, sendUpdates=updates, body=body)
            _tell_request(on_request, "events.patch", {"calendarId": calendar, "eventId": event_id,
                                                       "sendUpdates": updates, "body": body})
            result = self._execute(request, "moving the event back", mutation=True, event_call=True)
            result = result if isinstance(result, dict) else {}
            logger.info("%s: moved event %s back (undo)", self._log_name(), event_id)
            return ChangeResult(event_id=event_id, link=str(result.get("htmlLink") or details.link),
                                already=False, stamp=_stamp(result))

    def _time_is(self, event_id: str, calendar: str, start: datetime, end: datetime) -> bool:
        try:
            details = self.get_event(event_id, calendar_id=calendar, interactive=False)
        except EventGone:
            return False
        return details.start == _as_aware(start) and details.end == _as_aware(end)


def _stamp(item: Any) -> str:
    """An event's version as Google answered it: its etag, else its updated time ("" when neither)."""
    if not isinstance(item, dict):
        return ""
    return str(item.get("etag") or item.get("updated") or "")


def _instant_body(moment: datetime, time_zone: str) -> dict[str, str]:
    """An event's start or end as Google takes it back: the exact instant, in ``time_zone`` when known."""
    body = {"dateTime": _rfc3339(moment)}
    if time_zone:
        body["timeZone"] = time_zone
    return body


RequestHook = Callable[[str, dict[str, Any]], None]


def _tell_request(on_request: RequestHook | None, method: str, params: dict[str, Any]) -> None:
    """``on_request(method, a copy of params)`` right before a change goes to Google (the LIVE
    view shows it); whatever goes wrong in it changes nothing here."""
    if on_request is None:
        return
    try:
        on_request(method, copy.deepcopy(params))
    except Exception as exc:  # noqa: BLE001 - only what the LIVE view shows
        logger.debug("Live view: on_request failed (%s)", type(exc).__name__)


def _send_updates(value: str) -> str:
    """sendUpdates for a change: "all", "externalOnly" or "none" (a line's "external" works too)."""
    value = {"external": "externalOnly"}.get(value, value)
    if value not in SEND_UPDATES:
        raise CalendarError("Who to notify must be all, external or none")
    return value


def _wall(moment: datetime) -> datetime:
    return moment.replace(tzinfo=None, second=0, microsecond=0)


def _event_details(item: dict, event_id: str, calendar_id: str) -> EventDetails:
    """One events.get answer -> EventDetails (missing parts read as empty or False)."""
    start = item.get("start") if isinstance(item.get("start"), dict) else {}
    end = item.get("end") if isinstance(item.get("end"), dict) else {}
    organizer = item.get("organizer") if isinstance(item.get("organizer"), dict) else {}
    attendees = [entry for entry in item.get("attendees") or () if isinstance(entry, dict)]
    me = next((entry for entry in attendees if entry.get("self") is True), {})
    begin = finish = None
    first = last = None
    if start.get("dateTime"):
        begin = _parse_instant(start.get("dateTime"))
        finish = _parse_instant(end.get("dateTime"))
        if begin is not None and (finish is None or finish < begin):
            finish = begin
    else:
        first = _parse_day(start.get("date"))
        after = _parse_day(end.get("date"))   # exclusive
        if first is not None:
            last = after - timedelta(days=1) if after is not None and after > first else first
    name = organizer.get("displayName") or organizer.get("email") or ""
    return EventDetails(
        event_id=str(item.get("id") or event_id), calendar_id=calendar_id,
        title=" ".join(str(item.get("summary") or "").split()) or NO_TITLE,
        start=begin, end=finish, all_day_start=first, all_day_end=last,
        status=str(item.get("status") or "confirmed"),
        organizer_self=organizer.get("self") is True,
        guests_can_modify=item.get("guestsCanModify") is True,
        self_email=str(me.get("email") or ""), self_response=str(me.get("responseStatus") or ""),
        attendee_count=len(attendees), recurring_instance=bool(item.get("recurringEventId")),
        series=bool(item.get("recurrence")), time_zone=str(start.get("timeZone") or ""),
        link=str(item.get("htmlLink") or ""), organizer=" ".join(str(name).split()),
        self_comment=str(me.get("comment") or ""), organizer_email=str(organizer.get("email") or ""),
        stamp=_stamp(item))


# --------------------------------------------------------------------------
# Event bodies
# --------------------------------------------------------------------------

def event_footer(action: Any) -> str:
    """The footer an event added for ``action`` ends with: EVENT_FOOTERS by the card's mode (a day
    plan's block: PLAN_EVENT_FOOTER); EVENT_FOOTER for every other card, a Todo's block included."""
    return EVENT_FOOTERS.get(str(getattr(action, "mode", "") or ""), EVENT_FOOTER)


def build_event_body(action: ProposedAction, time_zone: str, *, footer: str = EVENT_FOOTER) -> dict[str, Any]:
    """The events.insert body for ``action`` in ``time_zone``; its description ends with ``footer``."""
    _require_actionable(action)
    notes = action.notes.strip()
    footer = footer or EVENT_FOOTER
    body: dict[str, Any] = {
        "summary": action.title.strip(),
        "description": f"{notes}\n\n{footer}" if notes else footer,
        "reminders": {"useDefault": True},
    }
    if action.where.strip():
        body["location"] = action.where.strip()
    if action.start is not None:
        start = action.start.replace(tzinfo=None)
        end = action.end.replace(tzinfo=None) if action.end is not None else None
        if end is None or end <= start:
            end = start + timedelta(minutes=DEFAULT_EVENT_MINUTES)
        body["start"] = {"dateTime": _wall_time(start), "timeZone": time_zone}
        body["end"] = {"dateTime": _wall_time(end), "timeZone": time_zone}
    else:
        first = action.all_day_start
        assert first is not None
        last = action.all_day_end if action.all_day_end and action.all_day_end >= first else first
        body["start"] = {"date": first.isoformat()}
        body["end"] = {"date": (last + timedelta(days=1)).isoformat()}   # Google's end is exclusive
    rule = action.rrule.strip()
    if rule:
        if not rule.upper().startswith(_RULE_PREFIXES):
            rule = "RRULE:" + rule
        if action.start is not None:
            rule = _localize_until(rule)
        body["recurrence"] = [rule]
    return body


def _wall_time(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _localize_until(rule: str) -> str:
    """Move an end-of-day UTC UNTIL to the end of that day in local time.

    "weekly until Dec 11" at 5 PM Pacific starts at 01:00Z on Dec 12, which a
    literal UNTIL=20261211T235959Z would cut off. Python has no time zone
    database here (no tzdata), so the machine's own rules are used; this
    assumes the calendar and the PC share a time zone.
    """
    match = _END_OF_DAY_UNTIL_RE.search(rule)
    if match is None:
        return rule
    try:
        day = datetime.strptime(match.group(1), "%Y%m%d").date()
        until = _local_end_of_day_utc(day)
    except (ValueError, OverflowError, OSError):
        return rule
    return f"{rule[:match.start()]}UNTIL={until:%Y%m%dT%H%M%S}Z{rule[match.end():]}"


def _local_end_of_day_utc(day: date) -> datetime:
    """23:59:59 local time on ``day``, as a UTC datetime (machine time zone rules)."""
    local = datetime(day.year, day.month, day.day, 23, 59, 59)
    return local.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Matching existing events
# --------------------------------------------------------------------------

def _require_actionable(action: ProposedAction) -> None:
    """Only a Calendar proposal (or a Todo's block, which is one) becomes a new event."""
    if not getattr(action, "actionable", False) or getattr(action, "kind", "") != "calendar":
        reason = getattr(action, "error", "")
        raise CalendarError("This proposal cannot be added to the calendar"
                            + (f": {reason}" if reason else ""))
    if action.start is None and action.all_day_start is None:
        raise CalendarError("This proposal has no date")


def _start_key(action: ProposedAction) -> datetime | date:
    if action.start is not None:
        return action.start.replace(tzinfo=None, second=0, microsecond=0)
    assert action.all_day_start is not None
    return action.all_day_start


def _search_window(action: ProposedAction) -> tuple[str, str]:
    """timeMin/timeMax in UTC wide enough for any time zone offset (up to 14 h)."""
    if action.start is not None:
        start = action.start.replace(tzinfo=None, second=0, microsecond=0)
        low, high = start - timedelta(days=1), start + timedelta(days=1)
    else:
        assert action.all_day_start is not None
        day = datetime.combine(action.all_day_start, datetime.min.time())
        low, high = day - timedelta(days=1), day + timedelta(days=2)
    return f"{low:%Y-%m-%dT%H:%M:%S}Z", f"{high:%Y-%m-%dT%H:%M:%S}Z"


def _matches(event: dict, title: str, start: datetime | date) -> bool:
    if event.get("status") == "cancelled":
        return False
    summary = event.get("summary")
    if not isinstance(summary, str) or summary.strip().casefold() != title:
        return False
    when = event.get("start")
    if not isinstance(when, dict):
        return False
    if isinstance(start, datetime):
        return _parse_wall_time(when.get("dateTime")) == start
    return when.get("date") == start.isoformat()


def _parse_wall_time(raw: Any) -> datetime | None:
    """The local wall time of an RFC 3339 dateTime ("2026-10-09T15:00:00-07:00")."""
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=None, second=0, microsecond=0)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Reading events (list_events)
# --------------------------------------------------------------------------

def _calendar_ids(calendar_ids: Sequence[str] | str) -> list[str]:
    """Stripped, non-empty ids in order without repeats; a single string is one id."""
    raw = (calendar_ids,) if isinstance(calendar_ids, str) else tuple(calendar_ids or ())
    ids: list[str] = []
    for value in raw:
        text = str(value or "").strip()
        if text and text not in ids:
            ids.append(text)
    return ids


def _as_aware(moment: datetime) -> datetime:
    """``moment`` itself when it has a time zone, else read as local time."""
    return moment if moment.utcoffset() is not None else moment.astimezone()


def _rfc3339(moment: datetime) -> str:
    """"2026-10-05T00:00:00-07:00" (seconds precision; a naive time is local)."""
    return _as_aware(moment).isoformat(timespec="seconds")


def _new_events(items: list[dict], calendar_id: str,
                seen: set[tuple[str, str]]) -> Iterator[CalendarEvent]:
    """The usable events of one calendar that ``seen`` does not hold yet (``seen`` is updated)."""
    for item in items:
        event = _event_from(item, calendar_id)
        if event is None:
            continue
        uid = str(item.get("iCalUID") or item.get("id") or "")
        if uid:
            key = (uid, _start_text(event))
            if key in seen:
                continue
            seen.add(key)
        yield event


def _start_text(event: CalendarEvent) -> str:
    """The start as text that is equal for the same moment whatever offset Google used."""
    if event.start is not None:
        return event.start.astimezone(timezone.utc).isoformat()
    return event.all_day_start.isoformat() if event.all_day_start is not None else ""


def _event_from(item: dict, calendar_id: str) -> CalendarEvent | None:
    """One events.list item -> CalendarEvent; None when cancelled or without a readable start."""
    if item.get("status") == "cancelled":
        return None
    start = item.get("start") if isinstance(item.get("start"), dict) else {}
    end = item.get("end") if isinstance(item.get("end"), dict) else {}
    fields: dict[str, Any] = {
        "title": " ".join(str(item.get("summary") or "").split()) or NO_TITLE,
        "location": " ".join(str(item.get("location") or "").split()),
        "link": str(item.get("htmlLink") or ""),
        "calendar_id": calendar_id,
    }
    if start.get("dateTime"):
        begin = _parse_instant(start.get("dateTime"))
        if begin is None:
            return None
        finish = _parse_instant(end.get("dateTime"))
        if finish is None or finish < begin:
            finish = begin
        return CalendarEvent(start=begin, end=finish, **fields)
    first = _parse_day(start.get("date"))
    if first is None:
        return None
    after = _parse_day(end.get("date"))   # exclusive
    last = after - timedelta(days=1) if after is not None and after > first else first
    return CalendarEvent(all_day_start=first, all_day_end=last, **fields)


def _brief_from(item: dict, calendar_id: str) -> EventBrief | None:
    """One events.list item -> EventBrief; None when cancelled, without an id or a readable start."""
    event = _event_from(item, calendar_id)
    event_id = item.get("id")
    if event is None or not isinstance(event_id, str) or not event_id.strip():
        return None
    organizer = item.get("organizer") if isinstance(item.get("organizer"), dict) else {}
    guests: list[Guest] = []
    self_response = "accepted" if organizer.get("self") is True else ""
    for entry in item.get("attendees") or ():
        if not isinstance(entry, dict) or entry.get("resource") is True:
            continue
        if entry.get("self") is True:   # the owner's own entry: his answer, never a guest
            self_response = _response_word(entry.get("responseStatus")) or self_response
            continue
        address = entry.get("email")
        if not isinstance(address, str) or "@" not in address:
            continue
        guests.append(Guest(email=address.strip(), name=" ".join(str(entry.get("displayName") or "").split()),
                            response=str(entry.get("responseStatus") or ""),
                            organizer=entry.get("organizer") is True))
    organizer_email = organizer.get("email") if isinstance(organizer.get("email"), str) else ""
    return EventBrief(
        event_id=event_id.strip(), calendar_id=calendar_id, title=event.title, start=event.start, end=event.end,
        all_day_start=event.all_day_start, all_day_end=event.all_day_end,
        organizer_self=organizer.get("self") is True, organizer_email=organizer_email.strip(),
        organizer_name=" ".join(str(organizer.get("displayName") or "").split()), guests=tuple(guests),
        recurring=bool(item.get("recurringEventId")), transparent=item.get("transparency") == "transparent",
        event_type=_type_word(item.get("eventType")), self_response=self_response,
        series_id=_series_id(item.get("recurringEventId")))


_RESPONSE_WORDS = frozenset({"needsAction", "accepted", "declined", "tentative"})
_SERIES_ID_RE = re.compile(r"[A-Za-z0-9_@.-]{1,128}")


def _series_id(value: Any) -> str:
    """The repeating event's id as Google gives it (one token; anything else is "")."""
    text = value.strip() if isinstance(value, str) else ""
    return text if text and _SERIES_ID_RE.fullmatch(text) else ""
_EVENT_TYPE_RE = re.compile(r"[A-Za-z]{1,40}")


def _response_word(value: Any) -> str:
    return value if isinstance(value, str) and value in _RESPONSE_WORDS else ""


def _type_word(value: Any) -> str:
    """eventType as a plain word ("" for "default" or anything odd)."""
    if not isinstance(value, str) or value == "default" or not _EVENT_TYPE_RE.fullmatch(value):
        return ""
    return value


def _brief_sort_key(brief: EventBrief) -> tuple[datetime, int, str]:
    if brief.start is not None:
        return brief.start, 1, brief.title.casefold()
    assert brief.all_day_start is not None
    return datetime.combine(brief.all_day_start, time()).astimezone(), 0, brief.title.casefold()


def _parse_instant(raw: Any) -> datetime | None:
    """An aware datetime from an RFC 3339 dateTime (no offset: local time); None if unreadable."""
    if not isinstance(raw, str):
        return None
    try:
        return _as_aware(datetime.fromisoformat(raw.strip()))
    except (ValueError, OverflowError, OSError):
        return None


def _parse_day(raw: Any) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _event_sort_key(event: CalendarEvent) -> tuple[datetime, int, str]:
    """By start; an all-day event comes first on its day (local midnight)."""
    if event.start is not None:
        return event.start, 1, event.title.casefold()
    assert event.all_day_start is not None
    return datetime.combine(event.all_day_start, time()).astimezone(), 0, event.title.casefold()


# --------------------------------------------------------------------------
# Google library glue (lazy imports; patched or injected in tests)
# --------------------------------------------------------------------------

def _build_service(creds: Any) -> Any:
    import google_auth_httplib2
    from googleapiclient.discovery import build

    # An explicit timeout: a change that gets no answer in HTTP_TIMEOUT_S is an unknown outcome
    # (never retried) rather than a wait without end; and a change is never sent twice by the
    # HTTP library itself (single_send_http). static_discovery uses the API description
    # bundled with the library: no extra request.
    http = google_auth_httplib2.AuthorizedHttp(creds, http=single_send_http(HTTP_TIMEOUT_S))
    return build("calendar", "v3", http=http, cache_discovery=False, static_discovery=True)


def _refresh_credentials(creds: Any) -> None:
    _account_refresh(creds)


def google_libraries_available() -> bool:
    """True when the Google client packages from requirements.txt are installed."""
    return _account_libraries()


def _require_libraries() -> None:
    if not google_libraries_available():
        raise CalendarSetupError(
            f"{SETUP_HINT} (the Google packages are missing: "
            "py -3.13 -m pip install -r requirements.txt)", problem=PROBLEM_SETUP)


def _status_of(exc: Any) -> int:
    status = getattr(getattr(exc, "resp", None), "status", 0)
    try:
        return int(status)
    except (TypeError, ValueError):
        return 0


def _google_message(exc: Any) -> str:
    raw = getattr(exc, "reason", "")
    text = " ".join(str(raw or "").split())
    text = redact(text)
    if len(text) > _MAX_MESSAGE_LEN:
        text = text[:_MAX_MESSAGE_LEN - 3] + "..."
    return text


def _error_reasons(exc: Any) -> set[str]:
    """The machine-readable reasons in a Google error body (both error formats)."""
    try:
        error = json.loads(getattr(exc, "content", b"").decode("utf-8"))["error"]
    except (ValueError, KeyError, TypeError, AttributeError):
        return set()
    if not isinstance(error, dict):
        return set()
    reasons: set[str] = set()
    if isinstance(error.get("status"), str):
        reasons.add(error["status"])
    for key in ("errors", "details"):
        for item in error.get(key) or ():
            if isinstance(item, dict) and isinstance(item.get("reason"), str):
                reasons.add(item["reason"])
    return reasons
