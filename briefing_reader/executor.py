"""Carrying out approved proposals: which service acts for which account, and how.

    Executor(backends, accounts)   picks the backend for a proposal: its kind and the
                                   ``[accounts.<alias>]`` table of its account in config.toml
    CalendarBackend(calendars)     Google Calendar, one gcal.GoogleCalendar per account alias:
                                   a Calendar event or a Todo's block (always the "personal"
                                   calendar, as before), and an RSVP, Move or Cancel (the line's
                                   ``acct=``)
    GmailBackend(senders, ...)     Gmail (gmail.send), one gmail.GmailSender per alias: a Reply or
                                   an Email from the line's ``acct=``, after the recipient check
    run_action(executor, action)   one job of the action worker: with ``writes_running`` (the
                                   click on a calendar card, or the end of a Send's / Cancel's
                                   hidden hold) "running" is saved before the call and the result
                                   after it; a Reply or Email is sent only that way. ``live`` (an
                                   approved card's LIVE view task): its last checks, sign-in, the
                                   exact payload and the call, as they happen
    run_undo(executor, action, r)  one Undo of the action worker (only the owner's click on that
                                   card's Undo): reverse exactly Jarvis's own change (UndoRecord,
                                   from the change's ExecResult.undo) unless the event is gone or
                                   changed since (UndoRefused: "Changed since - left as it is.")
    preview(action)                Executor: what execute would send or change (live.Payload; no
                                   network, never a sign-in), for the LIVE view
    mail_status(action)            Executor: what a Reply / Email card shows (From, recipients,
                                   why it can't send yet) and what its Send click does
    pending_confirmation(alias)    Executor: a new binding you have not confirmed yet ("Signed in
                                   as X for 'personal' - is that right?"); nothing is sent or
                                   changed for that alias until confirm_account(), and
                                   disconnect() is "No, use another account"
    change_problem(alias)          Executor: why nothing may be changed for the alias although it
                                   is signed in (fails closed: also a saved sign-in Jarvis has no
                                   binding for, which signs in again)
    build_accounts(config)         one google_auth.GoogleAccount per alias (the single sign-in
                                   of older versions becomes "personal"), bound via accounts.json
    build_calendars(config)        one gcal.GoogleCalendar per alias that has the calendar feature
    build_senders(config)          one gmail.GmailSender per alias that has the gmail_send feature
    build_executor(config, ...)    the Executor of both backends (recipients.json for the history)
    apply_edit(action, edit)       the Edit dialog's changes (memory only; same id)
    check_event(action, details)   the card's check line: Google's own title, time and organizer,
                                   and whether Jarvis may act on that event
    sign_in_note(alias, ...)       what a card says while its account cannot be used

``readiness`` answers without the network or a sign-in why a proposal cannot be
carried out ("" when it can). ``prepare`` signs the account in when needed (a
browser sign-in, only ever after a click; never for email: the card's Send
signs in first, so its From line shows the account before its Send);
``execute`` makes the one call and returns an ExecResult or raises ExecError,
whose ``outcome`` says whether the change certainly did not happen ("failed")
or may have happened ("unknown": never retried by itself). ``peek`` reads the
event without ever signing in. Nothing here retries a change: a Retry is a new
click on the card, and one approval sends at most one email
(google_auth.single_send_http).

Composio is a seam only: ``backend = "composio"`` is accepted by config.py and
answered here with "Composio is not built into this version"; a later backend
implements the same small protocol and nothing else changes.

Qt-free and blocking (callers use the worker thread). Logs name only action
ids, kinds, account aliases ("work", "personal", else "other"), statuses and
HTTP status codes. The LIVE view (``live=``) only shows what happens: with or
without it every status, store write, exception and log line is the same. A
calendar change shows the exact request gcal sends (its ``on_request`` hook,
only on gcal.GoogleCalendar), checked against what the card showed
(``request_differs``); when Google already had the change nothing was sent and
the steps say so (NOTHING SENT, ALREADY ...).
"""

from __future__ import annotations

import json
import logging
import re
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Protocol

from .actions import (
    CALENDAR,
    CANCEL,
    EVENT_KINDS,
    MAIL_KINDS,
    MOVE,
    REPLY,
    RSVP,
    STATUS_CREATED,
    STATUS_EXISTS,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SENT,
    STATUS_UNKNOWN,
    TODO,
    EditInvalid,
    ProposedAction,
    edit_action,
    edit_mail,
    mail_problem,
    open_text,
    sent_text,
    stated_when,
    when_text,
)
from .config import BACKEND_COMPOSIO, BACKEND_GOOGLE, DEFAULT_ACCOUNT, AccountConfig
from .gcal import (
    EVENT_FOOTER,
    NO_TITLE,
    RESPONSES,
    CalendarError,
    CalendarNotSignedIn,
    CalendarUnknownOutcome,
    EventChanged,
    EventDetails,
    EventGone,
    GoogleCalendar,
    NotAllowed,
    build_event_body,
    event_footer,
)
from .gmail import GmailError, GmailReader, GmailSender, GmailUnknownOutcome, MailView, OutgoingMail, mail_view
from .google_auth import (
    ACCOUNTS_FILE,
    CALENDAR_FEATURE,
    FEATURE_SCOPES,
    GMAIL_FEATURE,
    GMAIL_READ_FEATURE,
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
    logged_alias,
    migrate_legacy_token,
)
from .live import (
    ACTION_CALL,
    ACTION_CHECKS,
    ACTION_PAYLOAD,
    ACTION_SIGNIN,
    NO_STEP,
    NO_TASK,
    STATUS_BLOCKED,
    STATUS_FAILED as LIVE_FAILED,
    STATUS_OK,
    STATUS_RUNNING as LIVE_RUNNING,
    STATUS_WARN,
    Field,
    LiveStep,
    LiveTask,
    Payload,
    notify_words,
    quiet,
)
from .recipients import (
    OWN_RECIPIENT_MESSAGE,
    RECIPIENTS_FILE,
    RecipientHistory,
    RecipientReview,
    own_recipients,
    review,
)

if TYPE_CHECKING:
    from .actions import ActionStore
    from .config import Config

logger = logging.getLogger(__name__)

STAGE_SIGNIN = "signin"          # the browser sign-in is open
STAGE_SIGNED_IN = "signedin"     # signed in just now
STAGE_WORKING = "working"        # the call to Google is on its way
OUTCOME_FAILED = "failed"        # certainly not done (the card keeps Deny / Retry)
OUTCOME_UNKNOWN = "unknown"      # may have happened: check before retrying, never retried by itself
CALENDAR_SETUP_NOTE = "Google Calendar is not set up yet - see README step 8"
COMPOSIO_NOTE = "Composio is not built into this version"
NOT_SAVED_MESSAGE = "Could not save the decision before sending, so nothing was sent"
CLOSING_MESSAGE = "Jarvis was closing when the sign-in finished, so nothing was sent - click again"
NOTHING_TO_DO = "There is nothing for Jarvis to carry out on this card"
MAIL_SETUP_NOTE = "Sending email is not set up yet - see README step 8b"
# A Reply or Email goes out only through run_action(writes_running=True): at the end of its Send's
# hidden hold, with "running" saved first.
HOLD_ONLY_MESSAGE = "Email is only sent through its card's Send; nothing was sent"
# An Undo (run_undo): the event is gone or changed since Jarvis's change, so nothing is changed.
UNDO_CHANGED_TEXT = "Changed since - left as it is."
UNDO_NOT_SAVED_MESSAGE = "Could not save before undoing, so nothing was changed"
NO_UNDO_MESSAGE = "There is nothing Jarvis can undo on this card"
UNDONE = "undone"                 # RunOutcome.status of an Undo that reversed the change
UNDO_REFUSED = "refused"          # ... of an Undo that found the event gone or changed (nothing changed)
NEW_RECIPIENTS_MESSAGE = "Tick the new recipients on the card first; nothing was sent"
NO_EVENT_MESSAGE = "There is no event to check on this card"
UNCONFIRMED_FROM = "account not confirmed yet"
# A new binding you have not confirmed yet (google_auth.PROBLEM_CONFIRM): nothing is sent or changed.
CONFIRM_FIRST_MESSAGE = ("Confirm first that Jarvis signed in to the right Google account for {alias}; "
                         "nothing was changed")
# A saved sign-in Jarvis has no binding for (google_auth.PROBLEM_IDENTITY): sign in again.
UNKNOWN_ACCOUNT_MESSAGE = ("Jarvis doesn't know which Google account the {alias} account's sign-in is; sign in "
                           "again to confirm it - nothing was sent or changed")
CHECK_ACCOUNT_FAILED_MESSAGE = "Could not check which Google account this is; nothing was sent or changed"
# "No, use another account" when the saved sign-in can't be deleted: the binding stays unconfirmed.
DISCONNECT_FAILED_MESSAGE = ("Could not forget the {alias} account's Google sign-in (its file is in use); nothing "
                             "was sent or changed")
_NOTIFY = {"all": "all", "external": "externalOnly", "none": "none"}
_SEPARATOR = " \u00b7 "   # middle dot, as on the cards
_RESPONSE_WORDS = {"accepted": "you accepted", "declined": "you declined", "tentative": "you said maybe",
                   "needsAction": "you haven't answered"}

OnStage = Callable[[str], None]


def _no_stage(_stage: str) -> None:
    pass


# Carried out for the line's acct= (a Calendar event and a Todo's block use the personal calendar).
ACCOUNT_KINDS = EVENT_KINDS | MAIL_KINDS


def account_of(action: ProposedAction) -> str:
    """The alias that acts for ``action``: its ``acct=`` for an RSVP, Move, Cancel, Reply or Email;
    a Calendar event and a Todo's block always go to the personal calendar (as in earlier
    versions)."""
    if action.kind in ACCOUNT_KINDS:
        return action.account
    return DEFAULT_ACCOUNT


# --------------------------------------------------------------------------
# Edits
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ActionEdit:
    """The Edit dialog's changes to one card, kept in memory only (keyed by action id).

    ``None`` = unchanged. RSVP / Move / Cancel: ``answer``, ``notify``, ``start`` / ``end``,
    ``body`` (the note). Reply / Email: ``to``, ``cc`` (the chips), ``subject`` (an Email's),
    ``body`` (the text as sent) and ``confirmed_new``: every NEW RECIPIENT address ticked in the
    dialog (the whole set each time the dialog is saved; empty = none ticked).
    """

    answer: str | None = None          # RSVP: yes / no / maybe
    notify: str | None = None          # RSVP / Move / Cancel: all / external / none
    start: datetime | None = None      # Move
    end: datetime | None = None
    body: str | None = None            # the note
    to: tuple[str, ...] | None = None
    cc: tuple[str, ...] | None = None
    subject: str | None = None
    confirmed_new: frozenset[str] = frozenset()


class EditError(ValueError):
    """Why an edit cannot be used (shown in the dialog, which stays open)."""


def apply_edit(action: ProposedAction, edit: ActionEdit) -> ProposedAction:
    """``action`` with ``edit`` applied and checked like a line; the id never changes.

    Exactly what this returns is what the card shows and what Approve carries out.
    """
    try:
        if action.kind in MAIL_KINDS:
            if edit.answer is not None or edit.notify is not None or edit.start is not None \
                    or edit.end is not None:
                raise EditError("A reply or an email has no answer, time or guest notice to edit")
            return edit_mail(action, to=edit.to, cc=edit.cc, subject=edit.subject, body=edit.body,
                             confirmed=tuple(edit.confirmed_new))
        if edit.to is not None or edit.cc is not None or edit.subject is not None or edit.confirmed_new:
            raise EditError("Recipients and subjects can only be edited on a reply or an email")
        return edit_action(action, answer=edit.answer, notify=edit.notify, start=edit.start,
                           end=edit.end, body=edit.body)
    except EditInvalid as exc:
        raise EditError(str(exc)) from None


# --------------------------------------------------------------------------
# Results and errors
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class UndoRecord:
    """What a card's Undo reverses: exactly Jarvis's own change of one event (memory only, for 10
    minutes; logged by action id, kind and status only). ``stamp`` is the event's etag (else its
    updated time) as the change left it: an Undo first reads the event again and changes nothing
    when it is gone or its stamp differs. ``send_updates`` is the change's own guest-notification
    policy ("none" for an added event: its insert told nobody)."""

    kind: str                       # CALENDAR / TODO (delete the event), RSVP (answer back), MOVE (time back)
    alias: str
    event_id: str
    stamp: str
    send_updates: str = "none"
    calendar_id: str = ""           # "" = the calendar's own (an added event)
    response: str = ""              # RSVP: your answer before (needsAction / accepted / declined / tentative)
    comment: str = ""               # RSVP: your note before
    start: datetime | None = None   # MOVE: the event's aware start and end before
    end: datetime | None = None
    time_zone: str = ""             # MOVE: the time zone its start was kept in
    uncertain: bool = False         # an earlier Undo may or may not have happened (Retry checks first)


@dataclass(frozen=True)
class ExecResult:
    status: str             # STATUS_CREATED / STATUS_EXISTS / STATUS_SENT
    link: str = ""          # the event's page (htmlLink), "" when there is none (a cancelled event)
    result_text: str = ""   # "Accepted", "Moved", "Already cancelled" ... ("" = the card's default)
    # Gmail's id of the sent message / the event's id (the LIVE view shows it); not part of equality.
    ref: str = field(default="", compare=False)
    # What the card's Undo reverses (a calendar change Jarvis made; None when nothing changed or the
    # change can't be checked again); not part of equality.
    undo: UndoRecord | None = field(default=None, compare=False)


class ExecError(Exception):
    """Why a proposal was not carried out; the message is safe to show (never logged here).

    ``outcome``: OUTCOME_FAILED (not done) or OUTCOME_UNKNOWN (may have happened).
    ``problem``: google_auth's PROBLEM_* for a sign-in or setup problem, else "".
    """

    def __init__(self, message: str, *, outcome: str = OUTCOME_FAILED, problem: str = "",
                 status: int | None = None) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.problem = problem
        self.status = status


class UndoRefused(ExecError):
    """An Undo found the event gone or changed since Jarvis's change: nothing was changed (the card says
    UNDO_CHANGED_TEXT)."""

    def __init__(self, message: str = UNDO_CHANGED_TEXT) -> None:
        super().__init__(message)


@dataclass(frozen=True)
class EventCheck:
    """The card's check line for an RSVP, Move or Cancel.

    Everything but ``allowed`` / ``reason`` / ``mismatch`` is shown on the card only, never logged.
    """

    text: str               # "Google: Project sync \u00b7 Thu Oct 8 \u00b7 12:00-1:00 PM \u00b7 you organise"
    allowed: bool           # Jarvis may carry the card out on this event
    reason: str = ""        # why not (the card's amber note)
    mismatch: str = ""      # MISMATCH_*: Google's title or time is not the line's ("" = they agree)
    title: str = ""         # Google's own title, whole
    identity: str = ""      # the address Google answered as (see EventDetails.account_email)
    link: str = ""          # Google's page of the event (htmlLink)
    tooltip: str = ""       # the whole check: Google's whole title, and what the briefing said
    # Google's own times of the event (the LIVE day picture draws it there; shown, never logged).
    # Not part of equality: ``text`` already names them.
    start: datetime | None = field(default=None, compare=False)
    end: datetime | None = field(default=None, compare=False)
    all_day_start: date | None = field(default=None, compare=False)
    all_day_end: date | None = field(default=None, compare=False)


@dataclass(frozen=True)
class MailStatus:
    """What a Reply / Email card shows about sending, and what its Send click does (no network).

    ``sender`` / ``from_text`` are shown on the card only, never logged. ``review`` names every
    recipient as OWN / TRUSTED / KNOWN / NEW (recipients.py) for the chips; None only when the
    card is not a Reply / Email Jarvis sends.
    """

    alias: str
    sender: str                 # the bound account's address ("" while Jarvis doesn't know it)
    from_text: str              # "From: work (ana@example.edu)" / "From: work (account not confirmed yet)"
    problem: str = ""           # google_auth PROBLEM_* why it can't send now ("" = it can)
    note: str = ""              # the card's amber note for that problem ("" when there is none)
    sign_in: bool = False       # a Send click opens the Google sign-in first (nothing is sent)
    hand_off: bool = False      # Jarvis won't send from here: Copy / Open, and Done instead of Send
    review: RecipientReview | None = None
    confirm: bool = False       # a Send click asks first whether the bound Google account is the right one

    @property
    def refused(self) -> bool:
        """The recipients as they stand can't be sent to (one is the sending account itself, or To
        is empty): a Send click only says so; Edit fixes it."""
        return self.review is not None and bool(self.review.problem or self.review.own)

    @property
    def needs_edit(self) -> bool:
        """A NEW RECIPIENT is not ticked on the card yet: Send waits (it is disabled)."""
        return self.review is not None and bool(self.review.unconfirmed)

    @property
    def ready(self) -> bool:
        """A Send click may send (still subject to a long body's review)."""
        return not self.problem and not self.hand_off and self.review is not None and self.review.ready


@dataclass(frozen=True)
class RunOutcome:
    """What one run_action call ended with."""

    status: str                         # STATUS_CREATED / EXISTS / SENT / FAILED / UNKNOWN
    result: ExecResult | None = None
    error: ExecError | None = None


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------

class Backend(Protocol):
    name: str

    def handles(self, action: ProposedAction) -> bool: ...
    def readiness(self, action: ProposedAction) -> str: ...
    def signed_in(self, action: ProposedAction) -> bool: ...
    def prepare(self, action: ProposedAction, *, on_stage: OnStage) -> None: ...
    def execute(self, action: ProposedAction, *, on_stage: OnStage,
                interactive: bool = True) -> ExecResult: ...
    def peek(self, action: ProposedAction) -> EventDetails: ...
    def sign_in(self, alias: str, *, on_stage: OnStage) -> None: ...


class CalendarBackend:
    """Google Calendar for every alias in ``calendars`` (alias -> gcal.GoogleCalendar or a fake)."""

    name = BACKEND_GOOGLE
    KINDS = frozenset({CALENDAR, TODO, RSVP, MOVE, CANCEL})

    def __init__(self, calendars: Mapping[str, Any], *,
                 now: Callable[[], datetime] | None = None) -> None:
        self._calendars = dict(calendars)
        self._now = now or (lambda: datetime.now().astimezone())

    def handles(self, action: ProposedAction) -> bool:
        return action.kind in self.KINDS

    def calendar(self, alias: str) -> Any | None:
        return self._calendars.get(alias)

    def aliases(self) -> list[str]:
        return list(self._calendars)

    def readiness(self, action: ProposedAction) -> str:
        """"" when the account's calendar is there and set up; never the network."""
        calendar = self._calendars.get(account_of(action))
        if calendar is None or not _call_bool(calendar, "is_configured"):
            return CALENDAR_SETUP_NOTE
        return ""

    def signed_in(self, action: ProposedAction) -> bool:
        calendar = self._calendars.get(account_of(action))
        return calendar is not None and _call_bool(calendar, "is_signed_in")

    def prepare(self, action: ProposedAction, *, on_stage: OnStage) -> None:
        """Sign the account in first when there is no usable sign-in (opens the browser). Nothing is
        changed unless the saved sign-in is the account's confirmed Google account (ExecError
        PROBLEM_CONFIRM for a binding you have not confirmed, PROBLEM_IDENTITY for a sign-in
        Jarvis has no binding for; before "running" is saved)."""
        calendar = self._require(action)
        if not self.signed_in(action):
            on_stage(STAGE_SIGNIN)
            try:
                calendar.sign_in()
            except CalendarError as exc:
                raise _exec_error(exc) from None
            on_stage(STAGE_SIGNED_IN)
        self._require_confirmed(action, calendar)

    def _require_confirmed(self, action: ProposedAction, calendar: Any) -> None:
        """Fails closed (google_auth.GoogleAccount.change_problem): a Google account you did not
        confirm for the alias, or one Jarvis can't tell, changes nothing."""
        problem, message = _change_problem(calendar)
        if problem == PROBLEM_CONFIRM:
            raise ExecError(CONFIRM_FIRST_MESSAGE.format(alias=account_of(action)), problem=PROBLEM_CONFIRM)
        if problem:
            raise ExecError(message or UNKNOWN_ACCOUNT_MESSAGE.format(alias=account_of(action)), problem=problem)

    def execute(self, action: ProposedAction, *, on_stage: OnStage,
                interactive: bool = True, live: LiveTask | None = None) -> ExecResult:
        """The one call. Without ``interactive`` nothing opens the browser: a missing sign-in is
        a failure (nothing was sent). ``live``: the LIVE view's payload (preview() again, right
        before the call) and call steps."""
        calendar = self._require(action)
        if interactive:
            self.prepare(action, on_stage=on_stage)
        elif not self.signed_in(action):
            raise ExecError(_not_signed_in_text(account_of(action)), problem=PROBLEM_SIGNED_OUT)
        self._require_confirmed(action, calendar)
        on_stage(STAGE_WORKING)
        task = live or NO_TASK
        payload = _show_payload(task, self._preview_quietly(action) if live is not None else None)
        call = _call_step(task, *_CALENDAR_CALLS.get(action.kind, ("Google Calendar", "")))
        field = action.field
        # The LIVE view hears the exact request right before it goes (gcal's on_request); only a
        # calendar that takes it gets it, and only with the LIVE view on.
        sent: list[str] = []
        hook: dict[str, Any] = {}
        if live is not None and call is not NO_STEP and getattr(calendar, "REQUEST_HOOK", False) is True:
            hook["on_request"] = _request_hook(payload, call, action, sent)
        try:
            if action.kind in (CALENDAR, TODO):
                event = action.block_event() if action.kind == TODO else action
                if event is None:
                    raise ExecError("This to-do has no block time")
                # The card's own footer (a day plan's block says so; the card, not its block_event,
                # decides). Passed only when it is not the default, so any calendar keeps working.
                footer = event_footer(action)
                if footer != EVENT_FOOTER:
                    hook["footer"] = footer
                # Without interactive ("running" is saved) nothing may open the browser any more.
                created = (calendar.create_event(event, **hook) if interactive
                           else calendar.create_event(event, interactive=False, **hook))
                result = ExecResult(STATUS_EXISTS if created.existed else STATUS_CREATED, created.link,
                                    ref=_text(getattr(created, "event_id", "")),
                                    undo=_created_undo(action, calendar, created))
                _calendar_done(call, payload, action, result, already=bool(created.existed), requested=bool(sent))
                return result
            send_updates = _NOTIFY.get(field("notify", "all"), "all")
            calendar_id = field("cal", "primary")
            if action.kind == RSVP:
                change = calendar.respond(field("event"), field("answer"), comment=action.body,
                                          send_updates=send_updates, calendar_id=calendar_id,
                                          interactive=interactive, **hook)
            elif action.kind == MOVE:
                if action.start is None or action.end is None:
                    raise ExecError("This move has no new time")
                change = calendar.move(field("event"), action.start, action.end, send_updates=send_updates,
                                       calendar_id=calendar_id, now=self._now(), interactive=interactive, **hook)
            else:
                change = calendar.cancel(field("event"), send_updates=send_updates, calendar_id=calendar_id,
                                         interactive=interactive, **hook)
        except CalendarError as exc:
            raise _exec_error(exc) from None
        result = ExecResult(STATUS_SENT, change.link, sent_text(action, already=change.already),
                            ref=_text(getattr(change, "event_id", "")),
                            undo=_change_undo(action, change, send_updates, calendar_id))
        _calendar_done(call, payload, action, result, already=bool(change.already), requested=bool(sent))
        return result

    def undo(self, record: UndoRecord) -> ExecResult:
        """Reverse exactly Jarvis's own change (``record``), never signing in: delete the added event,
        put the answer back or move the event back, with the change's own ``send_updates``. UndoRefused
        when the event is gone or changed since (nothing changed); ExecError (with its outcome) when
        Google refused or did not answer."""
        calendar = self._calendars.get(record.alias)
        if calendar is None or not _call_bool(calendar, "is_configured"):
            raise ExecError(CALENDAR_SETUP_NOTE, problem=PROBLEM_SETUP)
        if not _call_bool(calendar, "is_signed_in"):
            raise ExecError(_not_signed_in_text(record.alias), problem=PROBLEM_SIGNED_OUT)
        problem, message = _change_problem(calendar)
        if problem == PROBLEM_CONFIRM:
            raise ExecError(CONFIRM_FIRST_MESSAGE.format(alias=record.alias), problem=PROBLEM_CONFIRM)
        if problem:
            raise ExecError(message or UNKNOWN_ACCOUNT_MESSAGE.format(alias=record.alias), problem=problem)
        calendar_id = record.calendar_id or None
        try:
            if record.kind in (CALENDAR, TODO):
                change = calendar.undo_create(record.event_id, stamp=record.stamp, send_updates=record.send_updates,
                                              calendar_id=calendar_id, uncertain=record.uncertain)
            elif record.kind == RSVP:
                change = calendar.undo_respond(record.event_id, record.response, comment=record.comment,
                                               stamp=record.stamp, send_updates=record.send_updates,
                                               calendar_id=calendar_id, uncertain=record.uncertain)
            elif record.kind == MOVE and record.start is not None and record.end is not None:
                change = calendar.undo_move(record.event_id, record.start, record.end, time_zone=record.time_zone,
                                            stamp=record.stamp, send_updates=record.send_updates,
                                            calendar_id=calendar_id, uncertain=record.uncertain)
            else:
                raise ExecError(NO_UNDO_MESSAGE)
        except EventChanged:
            raise UndoRefused() from None
        except CalendarError as exc:
            raise _exec_error(exc) from None
        return ExecResult(UNDONE, _text(getattr(change, "link", "")), ref=record.event_id)

    def preview(self, action: ProposedAction) -> Payload:
        """What execute() sends or changes for ``action``, in words (pure: no network, never a
        sign-in; the calendar's own time zone is applied by Google, so times are named as "in your
        calendar's time zone"). ExecError when it has nothing to carry out."""
        field = action.field
        today = self._now().date()
        if action.kind in (CALENDAR, TODO):
            event = action.block_event() if action.kind == TODO else action
            if event is None:
                raise ExecError("This to-do has no block time")
            calendar = self._calendars.get(account_of(action))
            calendar_id = _text(getattr(calendar, "calendar_id", "")) or "primary"
            fields = [Field("Title", event.title or NO_TITLE), Field("When", _when_words(event, today))]
            if event.where.strip():
                fields.append(Field("Where", event.where.strip()))
            if event.repeat:
                fields.append(Field("Repeats", event.repeat))
            fields.append(Field("Calendar", f"{account_of(action)} ({calendar_id})"))
            try:
                description = str(build_event_body(event, "", footer=event_footer(action)).get("description", ""))
            except CalendarError:
                description = ""
            return Payload("Will add exactly this", tuple(fields), "Description", description, start_open=False)
        if action.kind not in EVENT_KINDS:
            raise ExecError(NOTHING_TO_DO)
        # What changes first (a short LIVE tab shows the top rows), then which event it is.
        fields = [Field("Event", field("title") or action.title or NO_TITLE)]
        if action.kind == RSVP:
            fields.append(Field("Answer", _ANSWER_WORDS.get(field("answer"), field("answer"))))
            fields.append(Field("Note to the organizer", action.body or "none"))
        elif action.kind == MOVE:
            if action.start is None or action.end is None:
                raise ExecError("This move has no new time")
            fields.append(Field("New time", f"{_when_words(action, today)}, in your calendar's time zone"))
        else:
            fields.append(Field("Change", "cancel (delete) the event"))
        fields.append(Field("Who gets an email", notify_words(_NOTIFY.get(field("notify", "all"), "all"))))
        fields += [Field("Event id", field("event"), mono=True), Field("Calendar", field("cal", "primary"), mono=True)]
        return Payload("Will change exactly this", tuple(fields))

    def _preview_quietly(self, action: ProposedAction) -> Payload | None:
        try:
            return self.preview(action)
        except Exception as exc:  # noqa: BLE001 - only what the LIVE view shows
            logger.debug("Live view: preview failed (%s)", type(exc).__name__)
            return None

    def peek(self, action: ProposedAction) -> EventDetails:
        """Google's own view of the card's event; never signs in (CalendarNotSignedIn instead)."""
        if action.kind not in EVENT_KINDS:
            raise ExecError(NO_EVENT_MESSAGE)
        calendar = self._require(action)
        return calendar.get_event(action.field("event"), calendar_id=action.field("cal", "primary"),
                                  interactive=False)

    def sign_in(self, alias: str, *, on_stage: OnStage = _no_stage) -> None:
        """The browser sign-in of ``alias``'s account (a click on Sign in)."""
        calendar = self._calendars.get(alias)
        if calendar is None:
            raise ExecError(CALENDAR_SETUP_NOTE, problem=PROBLEM_SETUP)
        on_stage(STAGE_SIGNIN)
        try:
            calendar.sign_in()
        except CalendarError as exc:
            raise _exec_error(exc) from None
        on_stage(STAGE_SIGNED_IN)

    def _require(self, action: ProposedAction) -> Any:
        calendar = self._calendars.get(account_of(action))
        if calendar is None:
            raise ExecError(CALENDAR_SETUP_NOTE, problem=PROBLEM_SETUP)
        return calendar


class GmailBackend:
    """Gmail for every alias in ``senders`` (alias -> gmail.GmailSender or a fake): a Reply or an
    Email from the line's ``acct=``.

    Nothing here opens the browser except ``sign_in`` (a click). A message goes out only from
    ``execute(..., interactive=False)`` - run_action's "running" path at the end of Send's hidden hold -
    and only when the saved sign-in is the bound Google account and you confirmed that binding,
    the card's text is sendable (actions.mail_problem), no recipient is the sending account itself
    (checked on the card's recipients, on the message and once more right before the send) and
    every NEW RECIPIENT was ticked on its card. After a send the recipients are remembered
    (recipients.json), so they need no tick next time.
    """

    name = BACKEND_GOOGLE
    KINDS = MAIL_KINDS

    def __init__(self, senders: Mapping[str, Any], *, history: RecipientHistory | None = None,
                 trusted_domains: Sequence[str] = ()) -> None:
        self._senders = dict(senders)
        self._history = history
        self._trusted = tuple(trusted_domains)

    def handles(self, action: ProposedAction) -> bool:
        return action.kind in self.KINDS

    def sender(self, alias: str) -> Any | None:
        return self._senders.get(alias)

    def aliases(self) -> list[str]:
        return list(self._senders)

    def readiness(self, action: ProposedAction) -> str:
        """"" when the account's sending is set up and the card's text is sendable; no network."""
        sender = self._senders.get(action.account)
        if sender is None or not _call_bool(sender, "is_configured"):
            return MAIL_SETUP_NOTE
        return mail_problem(action)

    def signed_in(self, action: ProposedAction) -> bool:
        sender = self._senders.get(action.account)
        return sender is not None and _call_bool(sender, "is_signed_in")

    def review(self, action: ProposedAction) -> RecipientReview:
        """The card's recipients, each one OWN (the sending account itself: the card is refused) /
        TRUSTED / KNOWN / NEW, the new ones not yet ticked on the card."""
        sender = self._senders.get(action.account)
        own = _call_text(sender, "from_address") if sender is not None else ""
        return review(action.recipients(), action.cc(), own=own, account=action.account,
                      confirmed=action.confirmed, trusted_domains=self._trusted, history=self._history,
                      unverified=action.unverified)

    def status(self, action: ProposedAction, reason: str = "") -> MailStatus:
        """See MailStatus; ``reason`` is the Executor's readiness answer (not set up: a hand-off)."""
        alias = action.account
        sender = self._senders.get(alias)
        address = _call_text(sender, "from_address") if sender is not None else ""
        from_text = f"From: {alias} ({address or UNCONFIRMED_FROM})"
        recipients = self.review(action)
        if reason:
            return MailStatus(alias, address, from_text, PROBLEM_SETUP, reason, hand_off=True, review=recipients)
        problem, message = _sender_ready(sender)
        tools = hand_off_tools(action)
        if problem in (PROBLEM_SETUP, PROBLEM_BLOCKED):
            note = mail_note(alias, problem, message, tools=tools)
            return MailStatus(alias, address, from_text, problem, note, hand_off=True, review=recipients)
        if problem == PROBLEM_CONFIRM:
            return MailStatus(alias, address, from_text, problem, mail_note(alias, problem, message, tools=tools),
                              review=recipients, confirm=True)
        if problem:
            return MailStatus(alias, address, from_text, problem, mail_note(alias, problem, message, tools=tools),
                              sign_in=True, review=recipients)
        note = recipients.problem or (NEW_RECIPIENT_NOTE if recipients.unconfirmed else "")
        return MailStatus(alias, address, from_text, "", note, review=recipients)

    def prepare(self, action: ProposedAction, *, on_stage: OnStage = _no_stage) -> None:
        """Check that the message may go out now. Never opens the browser: a card's Send signs in
        first, so its From line shows the account before its Send."""
        self._message(action)

    def execute(self, action: ProposedAction, *, on_stage: OnStage = _no_stage,
                interactive: bool = True, live: LiveTask | None = None) -> ExecResult:
        """The one send (run_action's "running" path only: ``interactive`` must be False).
        ``live``: the LIVE view's payload (the message exactly as built, right before the send)
        and call steps."""
        if interactive:
            raise ExecError(HOLD_ONLY_MESSAGE)
        mail = self._message(action)
        sender = self._senders[action.account]
        # Once more right before the send, against the message itself and the account as it is
        # now: never without a known sender, never to the sending account's own mailbox.
        if not mail.from_addr:
            raise ExecError(mail_note(action.account, PROBLEM_IDENTITY), problem=PROBLEM_IDENTITY)
        if (own_recipients(mail.to + mail.cc, mail.from_addr)
                or own_recipients(mail.to + mail.cc, _call_text(sender, "from_address"))):
            logger.info("Action %s (%s, %s) not sent: a recipient is the sending account itself", action.id,
                        action.kind, logged_alias(action.account))
            raise ExecError(OWN_RECIPIENT_MESSAGE.format(alias=action.account))
        logger.info("Sending action %s (%s, %s) to %d recipient(s)", action.id, action.kind,
                    logged_alias(action.account), mail.recipient_count)
        on_stage(STAGE_WORKING)
        task = live or NO_TASK
        payload = _show_mail(task, mail) if live is not None else NO_STEP
        call = _call_step(task, "Gmail: send the message (one call, never retried)", "users.messages.send")
        try:
            sent = sender.send(mail)
        except GmailUnknownOutcome as exc:
            _call_failed(call, payload, str(exc), status=exc.status, unknown=True)
            raise ExecError(str(exc), outcome=OUTCOME_UNKNOWN, problem=exc.problem, status=exc.status) from None
        except GmailError as exc:
            _call_failed(call, payload, str(exc), status=exc.status, unknown=False)
            raise ExecError(str(exc), problem=exc.problem, status=exc.status) from None
        remembered = 0
        if self._history is not None:
            try:
                self._history.add(mail.to + mail.cc)
                remembered = mail.recipient_count
            except Exception as exc:  # noqa: BLE001 - the message went out; only the memory failed
                logger.warning("Could not remember the recipients of action %s (%s)", action.id,
                               type(exc).__name__)
        result = ExecResult(STATUS_SENT, getattr(sent, "link", "") or "", sent_text(action),
                            ref=_text(getattr(sent, "message_id", "")))
        _mail_sent(call, payload, result, remembered)
        return result

    def preview(self, action: ProposedAction) -> Payload:
        """The message execute() would send for ``action``, exactly as built (no network, never a
        sign-in); ExecError when it may not go out as it stands."""
        mail = self._message(action)
        try:
            view = mail_view(mail)
        except GmailError as exc:
            raise ExecError(str(exc) or type(exc).__name__, problem=exc.problem) from None
        return mail_payload(view, action.account)

    def peek(self, action: ProposedAction) -> EventDetails:
        raise ExecError(NO_EVENT_MESSAGE)

    def sign_in(self, alias: str, *, on_stage: OnStage = _no_stage) -> None:
        """The browser sign-in of ``alias`` for sending (a click on the card's Send or Sign in)."""
        sender = self._senders.get(alias)
        if sender is None:
            raise ExecError(MAIL_SETUP_NOTE, problem=PROBLEM_SETUP)
        on_stage(STAGE_SIGNIN)
        try:
            sender.sign_in()
        except GmailError as exc:
            raise ExecError(str(exc) or type(exc).__name__, problem=exc.problem or PROBLEM_FAILED,
                            status=exc.status) from None
        on_stage(STAGE_SIGNED_IN)

    def _message(self, action: ProposedAction) -> OutgoingMail:
        """The message exactly as the card shows it, or ExecError (failed: nothing was sent)."""
        sender = self._senders.get(action.account)
        if sender is None:
            raise ExecError(MAIL_SETUP_NOTE, problem=PROBLEM_SETUP)
        problem = mail_problem(action)
        if problem:
            raise ExecError(problem)
        state, message = _sender_ready(sender)
        if state:
            raise ExecError(mail_note(action.account, state, message), problem=state)
        recipients = self.review(action)
        if recipients.own:   # the card's note names the address; this message (saved, logged) does not
            raise ExecError(OWN_RECIPIENT_MESSAGE.format(alias=action.account))
        if recipients.problem:
            raise ExecError(f"{recipients.problem}; nothing was sent")
        if recipients.unconfirmed:
            raise ExecError(NEW_RECIPIENTS_MESSAGE)
        reply = action.kind == REPLY
        from_addr = _call_text(sender, "from_address")
        if not from_addr:   # the account it sends from must be known (gmail.GmailSender refuses too)
            raise ExecError(mail_note(action.account, PROBLEM_IDENTITY), problem=PROBLEM_IDENTITY)
        return OutgoingMail(account=action.account, from_addr=from_addr,
                            to=recipients.to, cc=recipients.cc, subject=action.field("subject") or action.title,
                            body=action.body, thread_id=action.field("thread") if reply else "",
                            in_reply_to=action.field("msgid") if reply else "")


NEW_RECIPIENT_NOTE = "New recipient: tick it on the card before Send"


def _sender_ready(sender: Any) -> tuple[str, str]:
    """(PROBLEM_*, message) from the sender's ready(); a failing check counts as not set up."""
    if sender is None:
        return PROBLEM_SETUP, MAIL_SETUP_NOTE
    try:
        problem, message = sender.ready()
    except Exception as exc:  # noqa: BLE001 - only decides what the card offers
        logger.debug("Could not check the sending account (%s)", type(exc).__name__)
        return PROBLEM_SETUP, MAIL_SETUP_NOTE
    return str(problem or ""), str(message or "")


def _call_text(target: Any, name: str) -> str:
    try:
        value = getattr(target, name)()
    except Exception as exc:  # noqa: BLE001 - only decides what a card shows
        logger.debug("Could not read %s (%s)", name, type(exc).__name__)
        return ""
    return value if isinstance(value, str) else ""


def _change_problem(handle: Any) -> tuple[str, str]:
    """Why nothing may be sent or changed for a calendar's / sender's alias now: its
    ``change_problem()`` (google_auth.GoogleAccount's, which fails closed), or for an object
    without one (an older or a test double) only ``pending_confirmation()``. A check that breaks
    refuses too (PROBLEM_FAILED)."""
    getter = getattr(handle, "change_problem", None)
    if not callable(getter):
        return (PROBLEM_CONFIRM, "") if _call_text(handle, "pending_confirmation") else ("", "")
    try:
        problem, message = getter()
    except Exception as exc:  # noqa: BLE001 - refused: nothing is changed without a known account
        logger.warning("Could not check which Google account the sign-in is (%s)", type(exc).__name__)
        return PROBLEM_FAILED, CHECK_ACCOUNT_FAILED_MESSAGE
    if not isinstance(problem, str) or not isinstance(message, str):
        return PROBLEM_FAILED, CHECK_ACCOUNT_FAILED_MESSAGE
    return problem, message


def hand_off_tools(action: ProposedAction) -> str:
    """What a card offers instead when Jarvis can't send it: "Copy and Open" ("Copy" without a
    link)."""
    return "Copy and Open" if open_text(action) else "Copy"


def keep_calendar_hint(alias: str) -> str:
    """How to keep an account's Calendar actions when its administrator blocks sending email."""
    return f'remove "gmail_send" from config.toml [accounts.{alias}] features and sign in again'


_BLOCK_CODE_RE = re.compile(r"\((admin_policy_enforced|org_internal|domainPolicy)\)")


def mail_note(alias: str, problem: str, message: str = "", *, tools: str = "Copy and Open") -> str:
    """What a Reply / Email card of ``alias`` says while it can't send (no address in it).
    ``tools``: what the card offers instead ("Copy and Open", or "Copy" on a card without a link)."""
    if problem == PROBLEM_BLOCKED:
        match = _BLOCK_CODE_RE.search(message)
        code = match.group(1) if match else "admin_policy_enforced"
        return (f"The {alias} account's administrator does not allow this app to send email "
                f"({code}) - use {tools} instead. To keep Calendar actions for {alias}, "
                f"{keep_calendar_hint(alias)}")
    if problem == PROBLEM_SETUP:
        return message or MAIL_SETUP_NOTE
    if problem == PROBLEM_SCOPE:
        return f"Google did not allow sending email for {alias} - click Send to sign in again and tick that box"
    if problem == PROBLEM_EXPIRED:
        return f"The {alias} account's Google sign-in expired or was revoked - click Send to sign in again"
    if problem == PROBLEM_DENIED:
        return f"Google sign-in for the {alias} account was cancelled or access was denied - click Send to try again"
    if problem == PROBLEM_TIMEOUT:
        return (f"Google sign-in for the {alias} account was not finished in time - click Send to try again "
                "(if Google said \"Access blocked\", its administrator does not allow this app)")
    if problem == PROBLEM_IDENTITY:
        if message.startswith(("This is not", "That Google account", "Google did not say")):
            return f"{message} - click Send to sign in with the right account"
        return f"Jarvis doesn't know yet which Google account {alias} is - click Send to sign in and confirm it"
    if problem == PROBLEM_CONFIRM:
        return (f"Is From the right Google account for {alias}? Click Send to confirm it (or to use another "
                "account) - nothing is sent until you do")
    if problem == PROBLEM_FAILED and message:
        return f"{message} - click Send to try again"
    return f"Send signs in to Google for the {alias} account first - then check From and click Send again"


def _exec_error(exc: CalendarError) -> ExecError:
    outcome = OUTCOME_UNKNOWN if isinstance(exc, CalendarUnknownOutcome) else OUTCOME_FAILED
    message = str(exc) or type(exc).__name__
    return ExecError(message, outcome=outcome, problem=getattr(exc, "problem", ""),
                     status=getattr(exc, "status", None))


def _call_bool(target: Any, name: str) -> bool:
    try:
        return bool(getattr(target, name)())
    except Exception as exc:  # noqa: BLE001 - only decides what the card offers
        logger.debug("Could not check %s (%s)", name, type(exc).__name__)
        return False


def _not_signed_in_text(alias: str) -> str:
    return f"Not signed in to the {alias} account; nothing was sent - click Sign in"


# --------------------------------------------------------------------------
# Executor
# --------------------------------------------------------------------------

class Executor:
    """Chooses the backend for a proposal and carries it out (one call; see the module docs)."""

    def __init__(self, backends: Sequence[Backend], accounts: Mapping[str, AccountConfig]) -> None:
        self._backends = list(backends)
        self._accounts = dict(accounts)

    @property
    def accounts(self) -> Mapping[str, AccountConfig]:
        return dict(self._accounts)

    def backend_for(self, action: ProposedAction) -> Backend | None:
        """The backend of the action's account that handles its kind, or None.

        A Calendar event and a Todo's block always use Google (the personal calendar, as in
        earlier versions); an RSVP, Move or Cancel uses its account's ``backend``.
        """
        name = BACKEND_GOOGLE
        if action.kind in ACCOUNT_KINDS:
            account = self._accounts.get(action.account)
            if account is None:
                return None
            name = account.backend
        return next((backend for backend in self._backends
                     if backend.name == name and backend.handles(action)), None)

    def readiness(self, action: ProposedAction) -> str:
        """"" when ``action`` can be carried out now, else why not (no network, no sign-in)."""
        if not action.actionable:
            return NOTHING_TO_DO
        if action.kind in ACCOUNT_KINDS:
            alias = action.account
            account = self._accounts.get(alias)
            if account is None:
                return f'No account named "{alias}" in config.toml [accounts]'
            if account.backend == BACKEND_COMPOSIO:
                return COMPOSIO_NOTE
            if action.kind in MAIL_KINDS:
                if GMAIL_FEATURE not in account.features:
                    return (f"The {alias} account is not set up for sending email "
                            f"(config.toml [accounts.{alias}] features)")
            elif CALENDAR_FEATURE not in account.features:
                return (f"The {alias} account is not set up for calendar actions "
                        f"(config.toml [accounts.{alias}] features)")
        backend = self.backend_for(action)
        if backend is None:
            return "Jarvis can't carry this out in this version"
        return backend.readiness(action)

    def signed_in(self, action: ProposedAction) -> bool:
        """The account that acts for ``action`` has a usable saved sign-in (no network)."""
        backend = self.backend_for(action)
        return backend is not None and backend.signed_in(action)

    def prepare(self, action: ProposedAction, *, on_stage: OnStage = _no_stage) -> None:
        """Sign the action's account in when needed (the browser; only after a click)."""
        self._backend(action).prepare(action, on_stage=on_stage)

    def execute(self, action: ProposedAction, *, on_stage: OnStage = _no_stage,
                interactive: bool = True, live: LiveTask | None = None) -> ExecResult:
        """One call to the backend; ExecResult, or ExecError with its outcome. Never retried.
        ``live`` (the LIVE view) is passed on only when given."""
        backend = self._backend(action)
        logger.info("Carrying out action %s (%s, %s)", action.id, action.kind,
                    logged_alias(account_of(action)))
        extra = {"live": live} if live is not None else {}
        return backend.execute(action, on_stage=on_stage, interactive=interactive, **extra)

    def undo(self, action: ProposedAction, record: UndoRecord) -> ExecResult:
        """Reverse Jarvis's own change of ``action`` (the card's Undo; never signs in). See
        CalendarBackend.undo."""
        backend = self._backend(action)
        undo = getattr(backend, "undo", None)
        if not callable(undo):
            raise ExecError(NO_UNDO_MESSAGE)
        logger.info("Undoing action %s (%s, %s)", action.id, action.kind, logged_alias(record.alias))
        return undo(record)

    def preview(self, action: ProposedAction) -> Payload | None:
        """What execute() would send or change for ``action`` (the LIVE view's payload once it is
        approved): no network, never a sign-in. None when there is no backend for it, the
        backend can't say (ExecError) or has no preview."""
        if not action.actionable:
            return None
        try:
            backend = self.backend_for(action)
            get = getattr(backend, "preview", None)
            return get(action) if callable(get) else None
        except ExecError:
            return None
        except Exception as exc:  # noqa: BLE001 - only what the LIVE view shows
            logger.debug("Live view: preview failed (%s)", type(exc).__name__)
            return None

    def peek(self, action: ProposedAction) -> EventDetails:
        """Google's own view of the card's event (never signs in)."""
        if action.kind not in EVENT_KINDS:
            raise ExecError(NO_EVENT_MESSAGE)
        return self._backend(action).peek(action)

    def mail_status(self, action: ProposedAction) -> MailStatus:
        """What a Reply / Email card shows about sending and what its Send click does (no network);
        see MailStatus. Any other card: a hand-off with Executor.readiness as the note."""
        reason = self.readiness(action)
        backend = self.backend_for(action) if action.sends_mail else None
        status = getattr(backend, "status", None)
        if status is None:
            alias = action.account
            return MailStatus(alias, "", f"From: {alias} ({UNCONFIRMED_FROM})", PROBLEM_SETUP,
                              reason or NOTHING_TO_DO, hand_off=True)
        return status(action, reason)

    def sign_in(self, action: ProposedAction, *, on_stage: OnStage = _no_stage) -> None:
        """The browser sign-in of the account that acts for ``action`` (only after a click on
        Sign in, or on a Send whose account is not ready). Raises ExecError with the problem."""
        reason = self.readiness(action)
        backend = self.backend_for(action)
        if reason or backend is None:
            raise ExecError(reason or NOTHING_TO_DO, problem=PROBLEM_SETUP)
        backend.sign_in(account_of(action), on_stage=on_stage)

    # ---- which Google account an alias is: the first sign-in's question ------------------------

    def _account_handles(self, alias: str) -> list[Any]:
        """The objects that act for ``alias`` (its Gmail sender and its calendar; in the app both
        share one google_auth.GoogleAccount)."""
        handles: list[Any] = []
        for backend in self._backends:
            for getter in ("sender", "calendar"):
                get = getattr(backend, getter, None)
                handle = get(alias) if callable(get) else None
                if handle is not None and all(handle is not other for other in handles):
                    handles.append(handle)
        return handles

    def pending_confirmation(self, alias: str) -> str:
        """The address of the Google account ``alias`` was bound to while you have not confirmed it
        ("" when there is nothing to confirm). Nothing is sent or changed for the alias meanwhile;
        reading its calendar goes on. Files only (no network)."""
        for handle in self._account_handles(alias):
            address = _call_text(handle, "pending_confirmation")
            if address:
                return address
        return ""

    def confirm_account(self, alias: str, address: str) -> bool:
        """You answered Yes to "Signed in as ``address`` for ``alias`` - is that right?" (kept in
        accounts.json). False when ``alias`` is not bound to that address (any more)."""
        confirmed = False
        for handle in self._account_handles(alias):
            confirm = getattr(handle, "confirm_account", None)
            if callable(confirm):
                try:
                    confirmed = bool(confirm(address)) or confirmed
                except Exception as exc:  # noqa: BLE001 - not confirmed: it is asked again
                    logger.warning("Could not confirm the %s account (%s)", logged_alias(alias), type(exc).__name__)
        return confirmed and not self.pending_confirmation(alias)

    def change_problem(self, alias: str) -> str:
        """Why nothing may be sent or changed for ``alias`` now although it may be signed in
        (google_auth PROBLEM_*; "" when it may): PROBLEM_CONFIRM (a binding you have not
        confirmed: ask), PROBLEM_IDENTITY (a saved sign-in Jarvis has no binding for: sign in
        again, which binds and asks), PROBLEM_SIGNED_OUT, or PROBLEM_FAILED when it could not be
        checked. Files only (no network)."""
        for handle in self._account_handles(alias):
            problem, _message = _change_problem(handle)
            if problem:
                return problem
        return ""

    def needs_confirmation(self, alias: str) -> bool:
        """``alias`` has no binding you confirmed, so its next sign-in leaves one to confirm before
        anything is changed (a Calendar event's or Todo block's Approve signs in and asks before
        anything is added then). False for objects that do not say (test doubles)."""
        for handle in self._account_handles(alias):
            if callable(getattr(handle, "needs_confirmation", None)) and _call_bool(handle, "needs_confirmation"):
                return True
        return False

    def disconnect(self, alias: str) -> None:
        """You answered "No, use another account": forget ``alias``'s saved sign-in and which
        Google account it is (blocking: deletes files; run it on the action worker). ExecError
        (DISCONNECT_FAILED_MESSAGE) when the saved sign-in could not be deleted: the binding is
        kept, unconfirmed, and the sign-in must not start."""
        for handle in self._account_handles(alias):
            disconnect = getattr(handle, "disconnect", None)
            if callable(disconnect):
                try:
                    disconnect()
                except Exception as exc:  # noqa: BLE001 - google_auth logged it; the card says why
                    logger.warning("Could not disconnect the %s account (%s)", logged_alias(alias), type(exc).__name__)
                    raise ExecError(DISCONNECT_FAILED_MESSAGE.format(alias=alias), problem=PROBLEM_FAILED) from None

    def _backend(self, action: ProposedAction) -> Backend:
        reason = self.readiness(action)
        if reason:
            raise ExecError(reason, problem=PROBLEM_SETUP)
        backend = self.backend_for(action)
        assert backend is not None
        return backend


# --------------------------------------------------------------------------
# One job of the action worker
# --------------------------------------------------------------------------

def run_action(executor: Executor, action: ProposedAction, *, store: ActionStore | None = None,
               writes_running: bool = False, on_stage: OnStage = _no_stage,
               proceed: Callable[[], bool] | None = None, live: LiveTask | None = None) -> RunOutcome:
    """Carry out one approved proposal, exactly once (never retried here or anywhere else).

    ``writes_running`` (any kind Jarvis carries out: a Calendar event, a Todo's block, an RSVP or
    Move right after the click; a Cancel, Reply or Email at the end of its hidden hold): sign the
    account in first when needed (never for email: its card signs in before Send), save "running"
    (with kind and account)
    to ``store`` right before the call - nothing is sent when that could not be saved -, make
    the one call without any further sign-in, and save the result over "running": "created" /
    "exists" / "sent", "failed" (certainly not done) or "unknown" (it may have happened: the
    call was sent but no clear answer came back, or something unexpected broke after
    "running" was saved). The result is saved here, on the worker thread, so it is kept even
    when the app quits meanwhile; a crash leaves "running", which the next start turns into
    "unknown". ``proceed`` (the worker's "the app is not closing") is asked right after the
    sign-in, before "running" is saved: False means nothing is sent (failed, CLOSING_MESSAGE),
    so a browser sign-in that finishes after Quit starts no call.

    Without ``writes_running`` (a Calendar event or a Todo's block approved the older way)
    nothing is saved here: the caller saves the result (created / exists /
    failed). A Reply or Email is never sent that way (failed: nothing was sent). Never raises
    for an ExecError or a bug in a backend (a bug is logged by type only: its message could
    carry a token).

    ``live`` (an approved card's LIVE view task) gets its "Last checks before the call" step, the
    browser sign-in (from ``on_stage``, recorded before it is passed on), and through the backend
    the exact payload and the call; nothing it records changes a status, a store write or an
    exception. The caller finishes the task.
    """
    running = False
    result: ExecResult | None = None
    error: ExecError | None = None
    task = live or NO_TASK
    checks = task.step(ACTION_CHECKS, "Last checks before the call") if live is not None else NO_STEP
    staged = _staged(task, action, on_stage) if live is not None else on_stage
    extra = {"live": live} if live is not None else {}
    try:
        if action.kind in MAIL_KINDS and not writes_running:
            raise ExecError(HOLD_ONLY_MESSAGE)
        if writes_running:
            if store is None:
                raise ExecError(NOT_SAVED_MESSAGE)
            executor.prepare(action, on_stage=staged)
            checks.note("Account ready (the Google account you confirmed)")
            if proceed is not None and not proceed():
                raise ExecError(CLOSING_MESSAGE)
            if not store.set(action.id, STATUS_RUNNING, kind=action.kind, account=action.account):
                raise ExecError(NOT_SAVED_MESSAGE)
            running = True
            checks.note('Saved "running" before the call (so a crash shows UNKNOWN, never a silent resend)')
            checks.done(STATUS_OK)
            result = executor.execute(action, on_stage=staged, interactive=False, **extra)
        else:
            checks.done(STATUS_OK)
            result = executor.execute(action, on_stage=staged, **extra)
    except ExecError as exc:
        error = exc
    except Exception as exc:  # noqa: BLE001 - a bug must not end the worker
        logger.error("Carrying out action %s failed unexpectedly (%s)", action.id, type(exc).__name__)
        logger.debug("Where:\n%s", "".join(traceback.format_tb(exc.__traceback__)))
        error = ExecError(f"unexpected error ({type(exc).__name__})",
                          outcome=OUTCOME_UNKNOWN if running else OUTCOME_FAILED)
    if error is not None:
        status = STATUS_UNKNOWN if running and error.outcome == OUTCOME_UNKNOWN else STATUS_FAILED
        logger.info("Action %s (%s, %s) not carried out: %s%s", action.id, action.kind,
                    logged_alias(account_of(action)), status,
                    f" (HTTP {error.status})" if error.status else "")
        if live is not None:
            _not_carried_out(task, checks, error, unknown=status == STATUS_UNKNOWN)
    else:
        assert result is not None
        status = result.status
    if writes_running and store is not None:
        if result is not None:
            store.set(action.id, status, link=result.link, message=result.result_text, kind=action.kind,
                      account=action.account)
        else:
            store.set(action.id, status, message=str(error or ""), kind=action.kind, account=action.account)
    return RunOutcome(status, result, error)


def run_undo(executor: Executor, action: ProposedAction, record: UndoRecord, *,
             store: ActionStore) -> RunOutcome:
    """One Undo of the action worker: only ever the owner's click on that card's Undo (never voice,
    never by itself). "running" is saved first (a crash mid-call then shows UNKNOWN at the next
    start, never a silent "added"); then the reversal (Executor.undo). UNDONE: the change was
    reversed and the decision forgotten (the card waits again). UNDO_REFUSED: the event is gone or
    changed since, nothing was changed and the card keeps its result. FAILED / UNKNOWN: Google
    refused or did not answer; the card keeps its result and offers Retry (the reversal again, on
    a click). Logged by action id, kind, alias and status only. Never raises."""
    saved = store.get(action.id) or {}
    result: ExecResult | None = None
    error: ExecError | None = None
    running = False
    # "running" names the forward result it reverses: a crash mid-Undo then keeps that result on the card
    # (check the calendar), never an UNKNOWN whose Retry would apply the change again.
    forward = saved.get("status", "")
    if forward not in (STATUS_CREATED, STATUS_EXISTS, STATUS_SENT):
        forward = STATUS_CREATED if action.kind in (CALENDAR, TODO) else STATUS_SENT
    try:
        if not store.set(action.id, STATUS_RUNNING, link=saved.get("link", ""), message=saved.get("message", ""),
                         kind=action.kind, account=action.account, undo=forward):
            raise ExecError(UNDO_NOT_SAVED_MESSAGE)
        running = True
        result = executor.undo(action, record)
    except UndoRefused as exc:
        error = exc
    except ExecError as exc:
        error = exc
    except Exception as exc:  # noqa: BLE001 - a bug must not end the worker
        logger.error("Undoing action %s failed unexpectedly (%s)", action.id, type(exc).__name__)
        logger.debug("Where:\n%s", "".join(traceback.format_tb(exc.__traceback__)))
        error = ExecError(f"unexpected error ({type(exc).__name__})",
                          outcome=OUTCOME_UNKNOWN if running else OUTCOME_FAILED)
    if error is None:
        store.forget(action.id, (STATUS_RUNNING,))
        status = UNDONE
    else:
        if isinstance(error, UndoRefused):
            status = UNDO_REFUSED
        else:
            status = STATUS_UNKNOWN if running and error.outcome == OUTCOME_UNKNOWN else STATUS_FAILED
        if saved.get("status"):   # the change stands: the card keeps its result
            store.set(action.id, saved["status"], link=saved.get("link", ""), message=saved.get("message", ""),
                      kind=action.kind, account=action.account)
        else:
            store.forget(action.id, (STATUS_RUNNING,))
    logger.info("Undo of action %s (%s, %s): %s", action.id, action.kind, logged_alias(record.alias), status)
    return RunOutcome(status, result, error)


def _created_undo(action: ProposedAction, calendar: Any, created: Any) -> UndoRecord | None:
    """The Undo of an event Jarvis just added (None when nothing was added or its stamp is unknown)."""
    event_id, stamp = _text(getattr(created, "event_id", "")), _text(getattr(created, "stamp", ""))
    if getattr(created, "existed", True) or not event_id or not stamp:
        return None
    return UndoRecord(action.kind, account_of(action), event_id, stamp, send_updates="none",
                      calendar_id=_text(getattr(calendar, "calendar_id", "")))


def _change_undo(action: ProposedAction, change: Any, send_updates: str, calendar_id: str) -> UndoRecord | None:
    """The Undo of an answer or a move Jarvis just made: what Google held right before it (None when
    nothing changed, for a Cancel, or when the stamp or the earlier state is unknown)."""
    before = getattr(change, "before", None)
    stamp = _text(getattr(change, "stamp", ""))
    if action.kind not in (RSVP, MOVE) or getattr(change, "already", True) or before is None or not stamp:
        return None
    event_id = _text(getattr(change, "event_id", "")) or action.field("event")
    if action.kind == RSVP:
        response = _text(getattr(before, "self_response", "")) or "needsAction"
        return UndoRecord(RSVP, account_of(action), event_id, stamp, send_updates=send_updates,
                          calendar_id=calendar_id, response=response,
                          comment=_text(getattr(before, "self_comment", "")))
    start, end = getattr(before, "start", None), getattr(before, "end", None)
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    return UndoRecord(MOVE, account_of(action), event_id, stamp, send_updates=send_updates, calendar_id=calendar_id,
                      start=start, end=end, time_zone=_text(getattr(before, "time_zone", "")))


# --------------------------------------------------------------------------
# The LIVE view's steps of an approved card (on screen only; every helper is quiet)
# --------------------------------------------------------------------------

_ANSWER_WORDS = {"yes": "yes (accepted)", "no": "no (declined)", "maybe": "maybe (tentative)"}
# kind -> (the call step's title, the request)
_CALENDAR_CALLS = {
    CALENDAR: ("Google Calendar: add the event", "events.insert (after looking for the same event)"),
    TODO: ("Google Calendar: add the block", "events.insert (after looking for the same event)"),
    RSVP: ("Google Calendar: answer the invitation", "events.patch (your attendee entry only)"),
    MOVE: ("Google Calendar: move the event", "events.patch (start, end)"),
    CANCEL: ("Google Calendar: cancel the event", "events.delete"),
}
_RSVP_DONE = {"yes": "ACCEPTED", "no": "DECLINED", "maybe": "ANSWERED MAYBE"}
# ExecErrors that are one of Jarvis's own guards refusing (BLOCKED), not a failure.
_GUARD_PROBLEMS = frozenset({PROBLEM_CONFIRM, PROBLEM_IDENTITY})
_GUARD_MESSAGES = (HOLD_ONLY_MESSAGE, NEW_RECIPIENTS_MESSAGE, CLOSING_MESSAGE)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _when_words(item: Any, today: date) -> str:
    """"Fri Oct 9 2:00-3:00 PM" (the card's words without its separator dots)."""
    return when_text(item, today).replace(_SEPARATOR, " ")


def mail_payload(view: MailView, account: str = "") -> Payload:
    """A message as the LIVE view shows what will be / was sent (gmail.mail_view), with its picture
    (the email itself, drawn by Jarvis from the built message; ``account`` the alias it goes from)."""
    return Payload("Will send exactly this", (
        Field("From", view.from_addr or "(Gmail fills it in)", mono=True), Field("To", view.to),
        Field("Cc", view.cc or "none"), Field("Subject", view.subject),
        Field("In-Reply-To", view.in_reply_to or "none", mono=True),
        Field("References", view.references or "none", mono=True),
        Field("Gmail thread", view.thread_id or "a new thread", mono=True)), "Message", view.body,
        picture=_outgoing_picture(view, account))


@quiet(None)
def _outgoing_picture(view: MailView, account: str) -> Any:
    """The LIVE picture of an outgoing message (None when it can't be drawn: the text stays)."""
    from .pictures import outgoing_picture

    return outgoing_picture(view, account=account)


@quiet(NO_STEP)
def _payload_step(task: LiveTask, title: str) -> LiveStep:
    step = task.find(ACTION_PAYLOAD)
    return step if step is not NO_STEP else task.step(ACTION_PAYLOAD, title)


@quiet(NO_STEP)
def _show_payload(task: LiveTask, payload: Payload | None) -> LiveStep:
    """Right before the call: the payload step shows exactly what goes out (a difference from
    what the card showed is named and marks it CHECK)."""
    if not task.enabled or payload is None:
        return NO_STEP
    step = _payload_step(task, payload.title)
    changed = step.show(payload)
    step.update(status_text="SENDING")
    if changed:
        step.note("Not what the card showed: " + ", ".join(changed) + " changed")
        step.update(status_text="CHANGED SINCE")
    return step


@quiet(NO_STEP)
def _show_mail(task: LiveTask, mail: OutgoingMail) -> LiveStep:
    try:
        view = mail_view(mail)
    except GmailError:
        step = _payload_step(task, "Will send exactly this")
        step.done(LIVE_FAILED, status_text="NOT SENT",
                  summary="The message could not be put together, so nothing was sent")
        return step
    return _show_payload(task, mail_payload(view, mail.account))


@quiet(NO_STEP)
def _call_step(task: LiveTask, title: str, request: str) -> LiveStep:
    if not task.enabled:
        return NO_STEP
    return task.step(ACTION_CALL, title, fields=[Field("Request", request, mono=True)])


@quiet()
def _call_failed(call: LiveStep, payload: LiveStep, message: str, *, status: int | None, unknown: bool) -> None:
    http = f" (HTTP {status})" if status else ""
    if unknown:
        call.done(STATUS_WARN, status_text="UNKNOWN", summary="it may have happened - check Sent mail / the calendar "
                                                              f"before retrying{http}")
        call.note(message)
        payload.done(STATUS_WARN, status_text="UNKNOWN", summary="it may have gone out - check before retrying")
    else:
        call.done(LIVE_FAILED, summary=f"{message}{http}")
        payload.done(LIVE_FAILED, status_text="NOT SENT", summary="Not sent")


def _payload_done(payload: LiveStep, word: str) -> None:
    view = payload.view()
    if view is not None and view.status_text == "CHANGED SINCE":
        payload.done(STATUS_WARN, status_text="CHECK",
                     summary="what went out is not what the card showed (see the note)")
    else:
        payload.done(STATUS_OK, status_text=word)


@quiet()
def _mail_sent(call: LiveStep, payload: LiveStep, result: ExecResult, remembered: int) -> None:
    if call is NO_STEP:
        return
    call.field("Gmail message id", result.ref or "(none returned)", mono=True)
    if result.link:
        call.field("Thread", "Open the thread in Gmail", link=result.link)
    call.field("Recipients remembered", str(remembered))
    call.done(STATUS_OK, status_text="SENT")
    _payload_done(payload, "SENT EXACTLY THIS")


# Google already had it, so nothing was written: what the call step says instead of the change.
_ALREADY_WORDS = {CALENDAR: "ALREADY THERE", TODO: "ALREADY THERE", RSVP: "ALREADY ANSWERED",
                  MOVE: "ALREADY AT THAT TIME", CANCEL: "ALREADY GONE"}
_ALREADY_REQUESTS = {CALENDAR: "none - the same event is already there (found with events.list)",
                     TODO: "none - the same event is already there (found with events.list)",
                     RSVP: "none - Google has that answer already (read with events.get)",
                     MOVE: "none - the event is at that time already (read with events.get)",
                     CANCEL: "none - Google has no such event any more (read with events.get)"}


@quiet()
def _calendar_done(call: LiveStep, payload: LiveStep, action: ProposedAction, result: ExecResult, *,
                   already: bool, requested: bool = False) -> None:
    """The call's result. ``already``: Google had it already; ``requested``: a request went out all
    the same (a Cancel whose delete found the event gone) - else nothing was sent or written."""
    if call is NO_STEP:
        return
    if result.ref:
        call.field("Event id", result.ref, mono=True)
    if result.link:
        call.field("Event", "Open the event in Google Calendar", link=result.link)
    if already:
        if not requested:
            call.field("Request", _ALREADY_REQUESTS.get(action.kind, "none"), mono=True)
        call.field("Already that way", "yes - nothing changed")
        call.done(STATUS_OK, status_text=_ALREADY_WORDS.get(action.kind, "ALREADY SO"), summary=result.result_text)
        if requested:
            _payload_done(payload, "SENT - NO CHANGE")
        elif payload is not NO_STEP:
            view = payload.view()
            if view is not None and view.status_text == "CHANGED SINCE":
                payload.done(STATUS_WARN, status_text="CHECK", summary="Nothing was sent: Google already had it "
                                                                       "(and the request differed - see the note)")
            else:
                payload.done(STATUS_OK, status_text="NOTHING SENT", summary="Google already had this, so nothing "
                                                                            "was sent or changed")
        return
    if action.kind in (CALENDAR, TODO):
        word = "ADDED"
    elif action.kind == RSVP:
        word = _RSVP_DONE.get(action.field("answer"), "ANSWERED")
    elif action.kind == MOVE:
        word = "MOVED"
    else:
        word = "CANCELLED EVENT"
    call.done(STATUS_OK, status_text=word, summary=result.result_text)
    _payload_done(payload, "ADDED" if action.kind in (CALENDAR, TODO) else "CHANGED")


REQUEST_BLOCK = "Exact request to Google"


def _request_hook(payload: LiveStep, call: LiveStep, action: ProposedAction,
                  sent: list[str]) -> Callable[[str, dict[str, Any]], None]:
    """gcal's on_request: the exact request, right before it goes (the worker thread)."""

    def heard(method: str, params: dict[str, Any]) -> None:
        sent.append(method)
        _show_request(payload, call, action, method, params)

    return heard


def _request_line(method: str, params: Mapping[str, Any]) -> str:
    """"events.patch calendarId=primary eventId=abc123 sendUpdates=all" (the body is the block)."""
    names = ("calendarId", "eventId", "sendUpdates")
    return " ".join([method, *(f"{name}={params[name]}" for name in names if name in params)])


@quiet()
def _show_request(payload: LiveStep, call: LiveStep, action: ProposedAction, method: str,
                  params: dict[str, Any]) -> None:
    call.field("Request", _request_line(method, params), mono=True)
    body = params.get("body")
    if payload is NO_STEP:
        return
    if isinstance(body, Mapping):
        payload.block(REQUEST_BLOCK, json.dumps(body, indent=2, ensure_ascii=False))
    differs = request_differs(action, method, params)
    if differs:
        verb = "differs" if len(differs) == 1 else "differ"
        payload.note(f"Not what the card showed: {', '.join(differs)} {verb} in the request to Google")
        payload.update(status_text="CHANGED SINCE")


def _wall_text(moment: datetime | None) -> str:
    return moment.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S") if moment is not None else ""


def request_differs(action: ProposedAction, method: str, params: Mapping[str, Any]) -> list[str]:
    """The labels of what the card showed (preview()) that the request to Google does not carry
    as shown: the event's title, time, place, repeat, the event id, the answer, the new time, who
    gets an email, the calendar. [] when the request is what the card said."""
    differs: list[str] = []
    body = params.get("body") if isinstance(params.get("body"), Mapping) else {}
    if action.kind in (CALENDAR, TODO):
        event = action.block_event() if action.kind == TODO else action
        if event is None or method != "events.insert":
            return ["Request"]
        if str(body.get("summary", "")) != event.title.strip():
            differs.append("Title")
        start = body.get("start") if isinstance(body.get("start"), Mapping) else {}
        if event.start is not None:
            if start.get("dateTime") != _wall_text(event.start):
                differs.append("When")
        elif event.all_day_start is not None and start.get("date") != event.all_day_start.isoformat():
            differs.append("When")
        if str(body.get("location", "")) != event.where.strip():
            differs.append("Where")
        if bool(body.get("recurrence")) != bool(event.rrule.strip()):
            differs.append("Repeats")
        try:   # the description (notes and the card's own footer) as the card showed it
            shown = str(build_event_body(event, "", footer=event_footer(action)).get("description", ""))
        except CalendarError:
            shown = ""
        if "description" in body and str(body.get("description", "")) != shown:
            differs.append("Description")
        return differs
    field = action.field
    expected = {RSVP: "events.patch", MOVE: "events.patch", CANCEL: "events.delete"}.get(action.kind)
    if method != expected:
        return ["Request"]
    if params.get("eventId") != field("event"):
        differs.append("Event id")
    if params.get("calendarId") != field("cal", "primary"):
        differs.append("Calendar")
    if params.get("sendUpdates") != _NOTIFY.get(field("notify", "all"), "all"):
        differs.append("Who gets an email")
    if action.kind == RSVP:
        attendees = body.get("attendees") if isinstance(body.get("attendees"), list) else []
        mine = attendees[0] if attendees and isinstance(attendees[0], Mapping) else {}
        if mine.get("responseStatus") != RESPONSES.get(field("answer")):
            differs.append("Answer")
        if str(mine.get("comment", "")) != (action.body or ""):
            differs.append("Note to the organizer")
    elif action.kind == MOVE:
        start = body.get("start") if isinstance(body.get("start"), Mapping) else {}
        end = body.get("end") if isinstance(body.get("end"), Mapping) else {}
        if (start.get("dateTime"), end.get("dateTime")) != (_wall_text(action.start), _wall_text(action.end)):
            differs.append("New time")
    return differs


def _staged(task: LiveTask, action: ProposedAction, on_stage: OnStage) -> OnStage:
    """``on_stage`` that records the browser sign-in in the LIVE view first, then passes it on."""
    signin: list[LiveStep] = []

    def staged(stage: str) -> None:
        _record_stage(task, signin, action, stage)
        on_stage(stage)

    return staged


@quiet()
def _record_stage(task: LiveTask, signin: list[LiveStep], action: ProposedAction, stage: str) -> None:
    if stage == STAGE_SIGNIN:
        signin.append(task.step(ACTION_SIGNIN, "Google sign-in in your browser",
                                fields=[("Account", account_of(action))]))
    elif stage == STAGE_SIGNED_IN and signin:
        signin[-1].done(STATUS_OK, summary="signed in")


@quiet()
def _not_carried_out(task: LiveTask, checks: LiveStep, error: ExecError, *, unknown: bool) -> None:
    """run_action ended with ``error``: the step it ended in says why."""
    message = str(error) or "unknown error"
    http = f" (HTTP {error.status})" if error.status else ""
    for kind in (ACTION_SIGNIN, ACTION_CALL, ACTION_PAYLOAD):
        step = task.find(kind)
        view = step.view()
        if view is None or view.status != LIVE_RUNNING:
            continue
        if kind == ACTION_PAYLOAD:
            if unknown:
                step.done(STATUS_WARN, status_text="UNKNOWN", summary="it may have gone out - check before retrying")
            else:
                step.done(LIVE_FAILED, status_text="NOT SENT", summary="Not sent")
        elif kind == ACTION_CALL and unknown:
            step.done(STATUS_WARN, status_text="UNKNOWN",
                      summary=f"it may have happened - check Sent mail / the calendar before retrying{http}")
        else:
            step.done(LIVE_FAILED, summary=f"{message}{http}")
    if task.find(ACTION_CALL) is not NO_STEP:
        return   # the call was made (or tried): its step says what happened
    guard = error.problem in _GUARD_PROBLEMS or message in _GUARD_MESSAGES or message.startswith(
        OWN_RECIPIENT_MESSAGE.split("{", 1)[0])
    checks.done(STATUS_BLOCKED if guard else LIVE_FAILED, summary=f"{message}{http}")


# --------------------------------------------------------------------------
# Accounts and calendars from config.toml
# --------------------------------------------------------------------------

def account_features(config: Config, alias: str) -> tuple[str, ...]:
    """What ``alias`` signs in for: its configured features; "personal" always has the
    calendar (Calendar proposals, to-do blocks and the agenda use it)."""
    account = config.accounts.get(alias)
    features = [name for name in (account.features if account is not None else ()) if name in FEATURE_SCOPES]
    if alias == DEFAULT_ACCOUNT and CALENDAR_FEATURE not in features:
        features.insert(0, CALENDAR_FEATURE)
    return tuple(features)


def google_aliases(config: Config) -> list[str]:
    """"personal" and every ``[accounts.<alias>]`` with the google backend, in config order."""
    aliases = [DEFAULT_ACCOUNT]
    for alias, account in config.accounts.items():
        if alias != DEFAULT_ACCOUNT and account.backend == BACKEND_GOOGLE and account_features(config, alias):
            aliases.append(alias)
    return aliases


def build_accounts(config: Config, *, flow_factory: Callable[[Any, list[str]], Any] | None = None,
                   refresh: Callable[[Any], None] | None = None) -> dict[str, GoogleAccount]:
    """One GoogleAccount per Google alias, sharing the OAuth client of ``[calendar]
    client_secret``; tokens in ``config.data_dir`` (google_token_<alias>.json).

    The single google_token.json of older versions is moved to the personal account first
    (google_auth.migrate_legacy_token), so nobody has to sign in again. Nothing here opens the
    browser or talks to Google.
    """
    migrate_legacy_token(config.data_dir)
    bindings = AccountBindings(config.data_dir / ACCOUNTS_FILE)   # read on use; nothing is written here
    accounts: dict[str, GoogleAccount] = {}
    for alias in google_aliases(config):
        accounts[alias] = GoogleAccount(
            alias, client_secret_path=config.calendar.client_secret_path, data_dir=config.data_dir,
            features=account_features(config, alias), flow_factory=flow_factory, refresh=refresh,
            bindings=bindings)
    return accounts


def build_calendars(config: Config, accounts: Mapping[str, GoogleAccount] | None = None, *,
                    service_factory: Callable[[Any], Any] | None = None) -> dict[str, GoogleCalendar]:
    """One GoogleCalendar per alias with the calendar feature ("personal" always; it uses
    ``[calendar] calendar_id``, the others "primary"). ``accounts`` defaults to
    build_accounts(config)."""
    if accounts is None:
        accounts = build_accounts(config)
    calendars: dict[str, GoogleCalendar] = {}
    for alias, account in accounts.items():
        if CALENDAR_FEATURE not in account.features:
            continue
        calendar_id = config.calendar.calendar_id if alias == DEFAULT_ACCOUNT else "primary"
        calendars[alias] = GoogleCalendar(account=account, calendar_id=calendar_id,
                                          service_factory=service_factory)
    return calendars


def build_senders(config: Config, accounts: Mapping[str, GoogleAccount] | None = None, *,
                  service_factory: Callable[[Any], Any] | None = None) -> dict[str, GmailSender]:
    """One GmailSender per alias with the gmail_send feature, sharing the account (and token) of
    its calendar. ``accounts`` defaults to build_accounts(config); pass the same mapping as to
    build_calendars so each alias has one sign-in."""
    if accounts is None:
        accounts = build_accounts(config)
    return {alias: GmailSender(account, service_factory=service_factory)
            for alias, account in accounts.items() if GMAIL_FEATURE in account.features}


def build_readers(config: Config, accounts: Mapping[str, GoogleAccount] | None = None, *,
                  service_factory: Callable[[Any], Any] | None = None) -> dict[str, GmailReader]:
    """One GmailReader (Ask Jarvis reading threads; gmail.readonly) per alias with the gmail_read
    feature, sharing the account (and token) of its calendar and sending; never used to send.
    ``accounts`` defaults to build_accounts(config); pass the same mapping as to build_calendars."""
    if accounts is None:
        accounts = build_accounts(config)
    return {alias: GmailReader(account, service_factory=service_factory)
            for alias, account in accounts.items() if GMAIL_READ_FEATURE in account.features}


def recipient_history(config: Config) -> RecipientHistory:
    """The addresses Jarvis sent to before (%LOCALAPPDATA%\\briefing-reader\\recipients.json)."""
    return RecipientHistory(config.data_dir / RECIPIENTS_FILE)


def build_executor(config: Config, calendars: Mapping[str, Any], senders: Mapping[str, Any] | None = None, *,
                   history: RecipientHistory | None = None,
                   now: Callable[[], datetime] | None = None) -> Executor:
    """The Executor of the app: Google Calendar for ``calendars`` and Gmail for ``senders``,
    ``[actions] trusted_domains`` and the recipient history (default: recipient_history(config))."""
    if history is None:
        history = recipient_history(config)
    backends: list[Any] = [CalendarBackend(calendars, now=now)]
    backends.append(GmailBackend(senders or {}, history=history, trusted_domains=config.actions.trusted_domains))
    return Executor(backends, config.accounts)


# --------------------------------------------------------------------------
# What a card says: the check line and sign-in problems
# --------------------------------------------------------------------------

def check_event(action: ProposedAction, details: EventDetails, today: date) -> EventCheck:
    """What the card says Google has for its event, and whether Jarvis may act on it.

    The text leads with Google's own title and time - what shows that the line's event id
    is the event the card means -, then who organizes it and your answer, and ends with the
    address Google answered as: "Google: Project sync \u00b7 Thu Oct 8 \u00b7 12:00-1:00 PM \u00b7
    you organise \u00b7 as ana@example.edu". A long title is shortened (the tooltip has all of
    it). When Google's title or time is not the line's, ``mismatch`` says which and the text
    starts "Google (other time): ..." instead.
    """
    title = _short_title(details.title)
    parts = [when_text(details, today)]
    if details.series:
        parts.append("whole repeating series")
    elif details.recurring_instance:
        parts.append("one occurrence")
    if details.organizer_self:
        parts.append("you organise")
    else:
        parts.append(f"organised by {details.organizer}" if details.organizer else "organised by someone else")
    reason = ""
    if action.kind == RSVP:
        if details.self_email:
            parts.append(_RESPONSE_WORDS.get(details.self_response, "you haven't answered"))
        else:
            reason = "You are not on this event's guest list, so Jarvis can't answer it - open it instead"
    elif action.kind == MOVE:
        if details.series:
            reason = "This is a whole repeating series; Jarvis moves single events only - open it"
        elif not (details.organizer_self or details.guests_can_modify):
            reason = "You don't organise this event, so Jarvis can't move it - open it to answer instead"
        elif details.all_day:
            reason = "All-day events can't be moved from here - open it"
        elif not details.organizer_self:
            parts.append("guests may change it")
    elif action.kind == CANCEL:
        if details.series:
            reason = "This is a whole repeating series; Jarvis cancels single events only - open it"
        elif not details.organizer_self:
            reason = "You don't organise this event, so Jarvis can't cancel it - open it to decline instead"
    identity = details.account_email
    if identity:
        parts.append(f"as {identity}")
    mismatch = event_mismatch(action, details)
    head = f"Google ({_MISMATCH_WORDS[mismatch]})" if mismatch else "Google"
    rest = _SEPARATOR.join(part for part in parts if part)
    text = f"{head}: {title}{_SEPARATOR}{rest}"
    tips = [f"{head}: {details.title}{_SEPARATOR}{rest}"]
    if mismatch:
        stated = stated_when(action)
        said = [action.field("title"), when_text(stated, today) if stated is not None else ""]
        tips.append("The briefing says: " + _SEPARATOR.join(part for part in said if part))
    return EventCheck(text, allowed=not reason, reason=reason, mismatch=mismatch, title=details.title,
                      identity=identity, link=details.link, tooltip="\n".join(tips), start=details.start,
                      end=details.end, all_day_start=details.all_day_start, all_day_end=details.all_day_end)


MISMATCH_TITLE = "title"
MISMATCH_TIME = "time"
MISMATCH_BOTH = "title+time"
_MISMATCH_WORDS = {MISMATCH_TITLE: "other title", MISMATCH_TIME: "other time",
                   MISMATCH_BOTH: "other title and time"}
_CHECK_TITLE_CHARS = 40   # Google's title on the check line; the tooltip has all of it
_WORDS_RE = re.compile(r"[^\W_]+")


def _short_title(title: str) -> str:
    if len(title) <= _CHECK_TITLE_CHARS:
        return title
    cut = title[:_CHECK_TITLE_CHARS]
    if " " in cut[_CHECK_TITLE_CHARS // 2:]:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,;:-") + "\u2026"


def event_mismatch(action: ProposedAction, details: EventDetails) -> str:
    """How Google's event differs from what the line says about it: MISMATCH_TITLE when neither
    title contains the other (case, spacing and punctuation aside), MISMATCH_TIME when its start
    is not the line's at= (as a wall time or as an instant on this PC), MISMATCH_BOTH, or "".
    A line without title= or at= (or an event without a title) is not compared on that part."""
    title = False
    said, google = _words(action.field("title")), _words(details.title)
    if said and google and details.title != NO_TITLE:
        title = f" {said} " not in f" {google} " and f" {google} " not in f" {said} "
    time = False
    stated = stated_when(action)
    if stated is not None:
        if stated.all_day_start is not None:
            day = details.all_day_start or (details.start.date() if details.start is not None else None)
            time = day is not None and day != stated.all_day_start
        elif stated.start is not None:
            start = details.start
            time = start is None or stated.start.replace(second=0, microsecond=0) not in (
                start.replace(tzinfo=None, second=0, microsecond=0),
                start.astimezone().replace(tzinfo=None, second=0, microsecond=0))
    if title and time:
        return MISMATCH_BOTH
    return MISMATCH_TITLE if title else (MISMATCH_TIME if time else "")


def _words(text: str) -> str:
    return " ".join(_WORDS_RE.findall(text.casefold()))


def check_failure(action: ProposedAction, error: BaseException) -> EventCheck:
    """The check line when Google could not show the event (never allowed)."""
    alias = action.account
    problem = getattr(error, "problem", "")
    if isinstance(error, CalendarNotSignedIn) or problem == PROBLEM_SIGNED_OUT:
        return EventCheck(f"Not signed in to the {alias} account - Sign in to check this event",
                          allowed=False)
    if problem in (PROBLEM_EXPIRED, PROBLEM_BLOCKED, PROBLEM_DENIED, PROBLEM_SCOPE, PROBLEM_TIMEOUT):
        return EventCheck(sign_in_note(alias, problem=problem, message=str(error)), allowed=False)
    if isinstance(error, EventGone):
        return EventCheck("Google: event not found (deleted, cancelled or a wrong id)", allowed=False,
                          reason="Google can't find this event, so Jarvis won't act on it")
    text = str(error) or type(error).__name__
    if isinstance(error, NotAllowed):
        return EventCheck(f"Google: {text}", allowed=False, reason=text)
    if problem == PROBLEM_SETUP:
        return EventCheck(text, allowed=False)
    return EventCheck(f"Couldn't check the event with Google: {text}", allowed=False)


def sign_in_note(alias: str, *, signed_in: bool = False, problem: str = "", message: str = "",
                 sends_mail: bool = False, reads_mail: bool = False) -> str:
    """What a card of ``alias`` says while that account cannot be used ("" when it can).

    ``problem`` / ``message`` are the account's last sign-in problem (GoogleCalendar
    .sign_in_problem()); the messages are google_auth's, which never name an address.
    ``sends_mail``: the account also has the gmail_send feature, so every sign-in asks for
    sending email too; a block then says how to keep Calendar actions without it.
    ``reads_mail``: it has gmail_read (Ask Jarvis reading mail, a restricted permission that
    administrators block first), so a block says to remove that first.
    """
    if signed_in:
        return ""
    if problem == PROBLEM_EXPIRED:
        return f"The {alias} account's Google sign-in expired or was revoked - click Sign in to sign in again"
    if problem == PROBLEM_BLOCKED:
        note = message or (f"Google blocked the sign-in: the {alias} account's administrator does not "
                           "allow this app")
        if reads_mail:
            then = f"; if it is still blocked, {keep_calendar_hint(alias)}," if sends_mail else ""
            note += (f". Every {alias} sign-in also asks to read email (Ask Jarvis), which administrators often "
                     f'block: remove "gmail_read" from config.toml [accounts.{alias}] features and sign in again'
                     f"{then} to keep Calendar actions")
        elif sends_mail:
            note += (f". Every {alias} sign-in also asks to send email; if the administrator only blocks "
                     f"that, {keep_calendar_hint(alias)} to keep Calendar actions")
        return note
    if problem == PROBLEM_DENIED:
        return f"Google sign-in for the {alias} account was cancelled or access was denied - click Sign in to try again"
    if problem == PROBLEM_SCOPE:
        return f"Google did not allow Calendar access for the {alias} account - click Sign in and tick every box"
    if problem == PROBLEM_TIMEOUT:
        return message or f"Google sign-in for the {alias} account was not finished in time - click Sign in to try again"
    if problem == PROBLEM_SETUP:
        return message or CALENDAR_SETUP_NOTE
    if problem == PROBLEM_IDENTITY:
        return (f"{message} - click Sign in" if message
                else f"This is not the Google account set up as the {alias} account - click Sign in with that one")
    if problem == PROBLEM_FAILED and message:
        return f"{message} - click Sign in to try again"
    return f"Not signed in to the {alias} account - click Sign in"
