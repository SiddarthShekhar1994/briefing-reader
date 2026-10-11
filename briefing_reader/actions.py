"""Proposed actions: read the briefing's "Proposed actions" section and remember decisions.

The scheduled briefing task (README: "Instructions for the Claude briefing
task") writes one line per proposal under a heading named
"Proposed actions". Two line formats are read:

    Calendar: <title> | <when> | <repeat> | <where> | <notes>
    <Kind>: key=value | key=value | ... | body=<text>

The second one (README: "Proposed actions format") is for the other kinds:
Reply, Email, RSVP, Move, Cancel, Share, Slack, Todo and Open. A line is read
that way only when its label is exactly one of those kinds and its first field
is a key that kind knows; every other line ("Reply: Carol about the draft")
goes through the Calendar / free-text path exactly as before.

    parse_action_line(text)    one line -> ProposedAction (never raises)
    extract_actions(lines)     pull that section out of a page's FlatLines
    link_allowed(url)          the https hosts a card's Open may open
    web_link_allowed(url)      a web research card's link: any public https host (public_host_problem)
    card_view(action, today)   what a card shows (kind label, texts, buttons)
    edit_action(action, ...)   the Edit dialog's changes to an RSVP, Move or Cancel, checked
                               like a line (same id)
    edit_mail(action, ...)     the Edit dialog's changes to a Reply or Email (recipients,
                               subject, body, confirmed new recipients; same id)
    parse_time_range(d, s, e)  the Edit dialog's new time for a Move, read like when=
    email_address(text)        one address as Jarvis sends to it ("" when it is not one)
    due_words(action, today)   "due today" / "due Wed Oct 7, 11:59 PM" (a Reply / Email card's detail)
    ActionStore(path)          persisted decisions (created / exists / denied / done /
                               failed / sent / running / unknown)

Jarvis carries out these kinds after Approve (COUNTDOWN_KINDS): a Calendar line
(create the event), a Todo with a block= time (add that block), an RSVP, Move
or Cancel (answer the invitation, move or cancel the event; EVENT_KINDS), and a
Reply or Email (send it through Gmail; MAIL_KINDS). Each goes through the undo
countdown first and has "running" saved right before its one call
(executor.py, ui.py). The other kinds are hand-offs: their cards open the
source link, copy the drafted text and record Done or Deny, and nothing is
sent. Free-text lines and lines that cannot be read stay informational;
``error`` says why a line could not be read. Nothing here talks to Google:
gcal.py and gmail.py do that, and only after an explicit click on that card.

Qt-free. Briefing content is personal, so only counts, action ids, kinds,
account aliases and statuses are logged, never text, addresses or links.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import unquote, urlsplit

from .models import DIVIDER, HEADING, PARAGRAPH, FlatLine

logger = logging.getLogger(__name__)

CALENDAR = "calendar"
REPLY = "reply"       # answer a message in an existing Gmail thread
EMAIL = "email"       # a new email
RSVP = "rsvp"         # answer a calendar invitation
MOVE = "move"         # move an event you organize
CANCEL = "cancel"     # cancel an event you organize
SHARE = "share"       # a Drive access request
SLACK = "slack"       # answer a Slack message
TODO = "todo"         # something due, optionally with a calendar block to work on it
OPEN = "open"         # a page to look at
UNKNOWN = "unknown"
STRUCTURED_KINDS = frozenset({REPLY, EMAIL, RSVP, MOVE, CANCEL, SHARE, SLACK, TODO, OPEN})
# Kinds Jarvis carries out after Approve: the undo countdown ([actions] undo_seconds) first, then
# "running" is saved right before the one call; never retried by itself (executor.run_action).
COUNTDOWN_KINDS = frozenset({CALENDAR, TODO, RSVP, MOVE, CANCEL, REPLY, EMAIL})
# Change an existing event of the line's account: the card checks Google's own view of it first.
EVENT_KINDS = frozenset({RSVP, MOVE, CANCEL})
# Sent from the line's account through Gmail (gmail.send only); recipients are checked first.
MAIL_KINDS = frozenset({REPLY, EMAIL})
MAX_RECIPIENTS = 5    # to= plus cc= of a Reply or Email (not configurable)
# Body lines a Reply / Email card shows; a longer body is reviewed in the Edit dialog before Send.
MAIL_PREVIEW_LINES = 10
# The https hosts a card's Open may open, besides [actions] link_hosts. Calendar's own
# "https://www.google.com/calendar/..." links are a separate, path-checked rule.
BUILTIN_LINK_HOSTS = ("mail.google.com", "docs.google.com", "drive.google.com",
                      "calendar.google.com", "meet.google.com", "*.slack.com", "*.instructure.com")
DEFAULT_HEADINGS = ("Proposed actions",)
DEFAULT_DURATION = timedelta(minutes=60)
# Who proposed a card (ProposedAction.source): the briefing page, or Ask Jarvis (briefing_reader.ask).
SOURCE_BRIEFING = "briefing"
SOURCE_ASK = "ask"

STATUS_CREATED = "created"   # Approve created the event
STATUS_EXISTS = "exists"     # Approve found it already on the calendar
STATUS_DENIED = "denied"     # Deny (shown as "dismissed" on cards without an Approve)
STATUS_FAILED = "failed"     # not final: the proposal stays pending and can be retried
STATUS_DONE = "done"         # you handled it yourself (a card without an Approve)
STATUS_SENT = "sent"         # Jarvis carried it out (an invitation answered, an event moved or cancelled)
STATUS_RUNNING = "running"   # written right before the call to Google of a countdown kind
STATUS_UNKNOWN = "unknown"   # stopped or timed out mid-call; never retried by itself
STATUSES = (STATUS_CREATED, STATUS_EXISTS, STATUS_DENIED, STATUS_FAILED, STATUS_DONE, STATUS_SENT,
            STATUS_RUNNING, STATUS_UNKNOWN)
# FAILED and UNKNOWN are not decisions: the card keeps its buttons.
DECIDED_STATUSES = frozenset({STATUS_CREATED, STATUS_EXISTS, STATUS_DENIED, STATUS_DONE, STATUS_SENT,
                              STATUS_RUNNING})
INTERRUPTED_MESSAGE = "Jarvis stopped while this was running; check before retrying"

_WEEKDAY_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_WEEKDAY_LONG = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTH_SHORT = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTH_LONG = ("January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December")
_SEPARATOR = " \u00b7 "   # middle dot, as in the HUD artboard
_DAY_MINUTES = 24 * 60
_MAX_COUNT = 999

_FORMAT_HINT = '"Calendar: title | YYYY-MM-DD HH:MM-HH:MM | repeat | where | notes"'
_NO_DATE = "no date (expected YYYY-MM-DD, optionally with HH:MM-HH:MM)"


def _text_prep() -> ModuleType:
    # Imported on use: text_prep imports this module for build_script(actions=...).
    from . import text_prep
    return text_prep


# --------------------------------------------------------------------------
# ProposedAction
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ProposedAction:
    """One line of the "Proposed actions" section.

    Timed events have naive ``start``/``end`` in local wall time (the
    calendar's time zone); all-day events have ``all_day_start`` and an
    inclusive ``all_day_end`` instead. ``repeat`` holds normalized words
    ("weekly until 2026-12-11", "daily x 5") and ``rrule`` the matching RRULE
    ("" = one-off). ``error`` says why the line could not be read.

    Lines in the key=value format (``structured``) keep their normalized
    values in ``fields`` (canonical key order, body excluded), the account
    alias in ``account``, an allowlisted https link in ``link``, the drafted
    text in ``body`` and soft problems in ``warnings``. ``start``/``end`` are
    a Todo's block or a Move's new time; ``title`` is the card headline.
    ``confirmed`` holds the NEW RECIPIENT addresses of a Reply / Email that you
    ticked in the Edit dialog (casefolded; set only by edit_mail, never by a
    line, kept in memory only).

    ``source`` says who proposed it: the briefing page (SOURCE_BRIEFING, every
    line of extract_actions) or Ask Jarvis (SOURCE_ASK). It is not part of the
    id, so the same proposal from both has one id (and one decision).
    ``unverified`` holds the recipients of an Ask Reply / Email that you did not
    type yourself (casefolded; memory only): each counts as NEW, even in a
    trusted domain, unless Jarvis sent to it before (recipients.review).
    ``web`` marks a web research card (an Open card for a source, a Todo with a
    source's link): its link is checked with the web rule (web_link_allowed:
    any public https host) instead of the Open allowlist, when it is read and
    again on the click. Not part of the id.
    ``mode`` marks a card of the productivity bundle's planner modes ("plan":
    a day plan's time block; later "prep" / "nudge"); ``serves`` is a plan
    block's Todo card (that card's id; memory only). Neither is part of the id.
    """

    id: str
    kind: str
    raw: str
    title: str = ""
    start: datetime | None = None
    end: datetime | None = None
    all_day_start: date | None = None
    all_day_end: date | None = None
    repeat: str = ""
    rrule: str = ""
    where: str = ""
    notes: str = ""
    error: str = ""
    account: str = ""                          # alias ("work"), "" when none
    fields: tuple[tuple[str, str], ...] = ()   # normalized values, canonical key order, body excluded
    link: str = ""                             # allowlisted https link, "" when none or rejected
    body: str = ""                             # unescaped body (or Slack reply), "" when none
    warnings: tuple[str, ...] = ()             # soft problems, shown as the card's amber note
    structured: bool = False                   # read by the key=value path (errored lines too)
    confirmed: frozenset[str] = frozenset()    # new recipients ticked in Edit (casefolded; memory only)
    source: str = "briefing"                   # SOURCE_BRIEFING or SOURCE_ASK; never part of the id
    unverified: frozenset[str] = frozenset()   # Ask recipients you did not type (casefolded; memory only)
    web: bool = False                          # a web research card: its link follows the web rule
    mode: str = ""                             # MODE_PLAN ("plan"), ...; "" for any other card; not in the id
    serves: str = ""                           # a plan block: the id of the Todo card it serves (memory only)

    @property
    def decidable(self) -> bool:
        """The card takes a decision: Deny plus Approve, Add block or Done.

        A Calendar line without an error, or a key=value line without one
        (except a Reply the briefing says was already sent). Decidable
        proposals count as pending until decided and are spoken in "Needs
        your OK".
        """
        if self.error:
            return False
        if self.kind == CALENDAR:
            return True
        if not self.structured or self.kind not in STRUCTURED_KINDS:
            return False
        return not (self.kind == REPLY and self.field("replied") == "yes")

    @property
    def actionable(self) -> bool:
        """Approve makes Jarvis carry it out (a Calendar event, a Todo's block, an RSVP, Move or
        Cancel, a Reply or Email)."""
        return self.decidable and bool(approve_label(self))

    @property
    def countdown(self) -> bool:
        """Approve starts the undo countdown first and "running" is saved before the call
        (COUNTDOWN_KINDS: every kind Jarvis carries out)."""
        return self.kind in COUNTDOWN_KINDS and self.actionable

    @property
    def changes_event(self) -> bool:
        """An RSVP, Move or Cancel Jarvis carries out: its card checks Google's own view of the
        event first (the check line), offers Edit (answer, time, notify, note) and Skip."""
        return self.kind in EVENT_KINDS and self.actionable

    @property
    def sends_mail(self) -> bool:
        """A Reply or Email Jarvis sends through Gmail: its card shows From and the recipients,
        offers Edit (recipients, subject, body) and asks to confirm new recipients."""
        return self.kind in MAIL_KINDS and self.actionable

    @property
    def editable(self) -> bool:
        """The tools row has Edit (an RSVP, Move, Cancel, Reply or Email Jarvis carries out)."""
        return self.changes_event or self.sends_mail

    @property
    def all_day(self) -> bool:
        return self.all_day_start is not None

    @property
    def speech_noun(self) -> tuple[str, str]:
        """("a reply", "replies"): how "Needs your OK" counts this kind."""
        return _SPEECH_NOUNS.get(self.kind, ("an item", "items"))

    def approve_label(self) -> str:
        return approve_label(self)

    def field(self, key: str, default: str = "") -> str:
        """The normalized value of ``key`` ("" or ``default`` when the line has none)."""
        for name, value in self.fields:
            if name == key:
                return value
        return default

    def recipients(self) -> tuple[str, ...]:
        """The to= addresses (normalized)."""
        return _split_list(self.field("to"))

    def cc(self) -> tuple[str, ...]:
        return _split_list(self.field("cc"))

    def mail_recipients(self) -> tuple[str, ...]:
        """to= then cc= (normalized, no repeats): everyone a Reply or Email goes to."""
        return self.recipients() + self.cc()

    def due(self) -> date | datetime | None:
        """due= as a date, or a naive datetime when it has a time."""
        return _iso_value(self.field("due"))

    def block_event(self) -> ProposedAction | None:
        """A Todo's block as a Calendar proposal with the Todo's id ("" when there is no block).

        The calendar worker reports its result under that id, so it lands on the Todo's card.
        """
        if self.kind != TODO or not self.decidable or self.start is None or self.end is None:
            return None
        due = self.due()
        notes = f"Due {_due_text(due, show_year=due is not None and due.year != self.start.year)}" if due else ""
        if self.link:
            notes = f"{notes} - {self.link}" if notes else self.link
        return ProposedAction(id=self.id, kind=CALENDAR, raw=self.raw, title=self.title,
                              start=self.start, end=self.end, notes=notes)

    def describe(self, today: date | None = None) -> str:
        """Display detail: "Fri Oct 9 \u00b7 3:00-4:00 PM \u00b7 weekly until Dec 11" ("" if not decidable).

        Years are shown only for dates outside ``today``'s year (default: the real today).
        """
        if not self.decidable:
            return ""
        today = today or date.today()
        if self.kind != CALENDAR:
            return _describe_structured(self, today)
        parts = _describe_when(self, today)
        repeat = _repeat_words(self.repeat, today, spoken=False)
        return _SEPARATOR.join(part for part in (*parts, repeat) if part)

    def spoken(self, today: date | None = None) -> str:
        """"Chess Club Weekly Meeting, Friday October 9, 3 to 4 PM, weekly until December 11".

        No final period (the caller adds one); "" if not decidable.
        """
        if not self.decidable:
            return ""
        today = today or date.today()
        if self.kind != CALENDAR:
            return _spoken_structured(self, today)
        title = _spoken_title(self.title)
        repeat = _repeat_words(self.repeat, today, spoken=True)
        return ", ".join(part for part in (title, *_spoken_when(self, today), repeat) if part)


def approve_label(action: ProposedAction) -> str:
    """The text of the card's Approve button ("" = no Approve in this version: the card gets Done).

    The one place later versions extend: Calendar lines are approved by
    creating the event, a Todo with a block= time by adding that block, an
    RSVP by answering it (Accept / Decline / Maybe), a Move or Cancel by
    moving or cancelling the event, a Reply or Email by sending it (Send).
    """
    if not action.decidable:
        return ""
    if action.kind == CALENDAR:
        return ADD_BLOCK_TEXT if action.mode == MODE_PLAN else "Approve"
    if action.kind == TODO and action.start is not None:
        return "Add block"
    if action.kind == RSVP:
        return _RSVP_BUTTONS.get(action.field("answer"), "")
    if action.kind == MOVE and action.start is not None:
        return "Move"
    if action.kind == CANCEL:
        return "Cancel event"
    if action.kind in MAIL_KINDS:
        return SEND_TEXT
    return ""


SEND_TEXT = "Send"
ADD_BLOCK_TEXT = "Add block"
MODE_PLAN = "plan"            # ProposedAction.mode of a day plan's time block (ask.modes.MODE_PLAN)
_RSVP_BUTTONS = {"yes": "Accept", "no": "Decline", "maybe": "Maybe"}


_SPEECH_NOUNS = {
    CALENDAR: ("a calendar invite", "calendar invites"),
    REPLY: ("a reply", "replies"),
    EMAIL: ("an email", "emails"),
    RSVP: ("an invitation to answer", "invitations to answer"),
    MOVE: ("a meeting to move", "meetings to move"),
    CANCEL: ("a meeting to cancel", "meetings to cancel"),
    SHARE: ("a share request", "share requests"),
    SLACK: ("a Slack reply", "Slack replies"),
    TODO: ("a to-do", "to-dos"),
    OPEN: ("a link to check", "links to check"),
}


def _split_list(value: str) -> tuple[str, ...]:
    return tuple(item for item in value.split(", ") if item) if value else ()


def _iso_value(value: str) -> date | datetime | None:
    """"2026-10-07" -> date, "2026-10-07T23:59" -> datetime, anything else -> None."""
    try:
        return datetime.fromisoformat(value) if "T" in value else date.fromisoformat(value)
    except ValueError:
        return None


def _action_id(action: ProposedAction) -> str:
    """sha1 of (kind, title, start, end, all-day, rrule); where and notes do not count.

    A line that is not actionable has no reliable fields, so its raw text
    stands in for the title. A key=value line without an error is keyed by
    what it acts on (``_structured_target``): rewording the draft, the title
    or the link keeps the id, a new message or a new time changes it.
    """
    if action.structured and not action.error and action.kind in STRUCTURED_KINDS:
        parts: tuple[str, ...] = (action.kind, *_structured_target(action))
        return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
    calendar = action.kind == CALENDAR and not action.error
    if calendar:
        name = action.title
        if action.all_day_start is not None:
            first, last = action.all_day_start.isoformat(), _iso(action.all_day_end)
        else:
            first, last = _iso(action.start), _iso(action.end)
    else:
        name, first, last = action.raw, "", ""
    parts = (action.kind, " ".join(name.casefold().split()), first, last,
             "1" if action.all_day else "0", action.rrule if calendar else "")
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def _structured_target(action: ProposedAction) -> tuple[str, ...]:
    """(account part, *target) for the id of a valid key=value line; never the body, link or warnings."""
    kind, field = action.kind, action.field
    if kind == REPLY:
        target = field("msgid") or "gmid:" + field("gmid")
        return action.account, target
    if kind == EMAIL:
        to = ",".join(sorted(address.casefold() for address in action.recipients()))
        return action.account, to, " ".join(field("subject").casefold().split())
    if kind in (RSVP, CANCEL):
        return action.account, field("cal", "primary"), field("event")
    if kind == MOVE:
        return action.account, field("cal", "primary"), field("event"), _iso(action.start), _iso(action.end)
    if kind == SHARE:
        return action.account, field("file"), field("who").casefold()
    if kind == SLACK:
        return "", field("team"), field("channel"), field("thread"), field("ts")
    if kind == TODO:
        return "", " ".join(action.title.casefold().split()), field("due")
    return "", action.link   # OPEN


def _iso(value: date | datetime | None) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(timespec="minutes")
    return value.isoformat()


def _make_action(kind: str, raw: str, **fields: Any) -> ProposedAction:
    action = ProposedAction(id="", kind=kind, raw=raw, **fields)
    return replace(action, id=_action_id(action))


_KIND_NAMES = {CALENDAR: "Calendar", REPLY: "Reply", EMAIL: "Email", RSVP: "RSVP", MOVE: "Move",
               CANCEL: "Cancel", SHARE: "Share", SLACK: "Slack", TODO: "Todo", OPEN: "Open"}


def with_error(action: ProposedAction, message: str) -> ProposedAction:
    """``action`` as an information-only card that says ``message`` (no decision, nothing to carry
    out). Its id is rebuilt the way an unreadable line's is (from the line's text); for a card that
    another source than the briefing proposed it also depends on that source, so an Ask card never
    shares an id with a briefing card it was refused next to."""
    errored = replace(action, id="", error=message or "can't be carried out")
    action_id = _action_id(errored)
    if errored.source != SOURCE_BRIEFING:
        action_id = hashlib.sha1(f"{errored.source}\x1f{action_id}".encode("utf-8")).hexdigest()[:16]
    return replace(errored, id=action_id)


def restrict(action: ProposedAction, kinds: Collection[str], source: str) -> ProposedAction:
    """``action`` as ``source`` proposes it (``source`` set): a kind ``source`` may not propose
    becomes an information-only card ("Ask can't propose Slack"), and a line that could not be
    read keeps its reason (with_error, so its id is the source's own)."""
    action = replace(action, source=source)
    name = source.capitalize() if source else "This source"
    if action.error:
        return with_error(action, action.error)
    if action.kind == UNKNOWN:
        return with_error(action, f"{name} lines need a kind such as Move: or Email:")
    if action.kind not in kinds:
        return with_error(action, f"{name} can't propose {_KIND_NAMES.get(action.kind, 'this kind')}")
    return action


# --------------------------------------------------------------------------
# parse_action_line
# --------------------------------------------------------------------------

class _LineError(ValueError):
    """Why a proposal line could not be read (shown on its card)."""


# "Calendar:" / "**Calendar**:" (markdown is stripped first). "https://" is not a kind.
_KIND_RE = re.compile(r"(?P<label>[A-Za-z][A-Za-z_-]*(?:\s+[A-Za-z][A-Za-z_-]*){0,2})\s*:(?!//)\s*")
_CALENDAR_LABELS = frozenset({"calendar", "cal", "calendar event", "calendar invite", "event", "invite"})
_KIND_ALIASES = {"to do": "todo", "follow up": "followup"}


def parse_action_line(text: str, *, link_hosts: Sequence[str] = ()) -> ProposedAction:
    """One line of the section -> ProposedAction. Never raises: problems go to ``error``.

    A key=value line (``_structured_line``) is read by its own parser;
    ``link_hosts`` are the extra hosts its link may point to ([actions]
    link_hosts). Every other line is read as before.
    """
    structured = _structured_line(text or "", link_hosts)
    if structured is not None:
        return structured
    raw = _clean_line(text or "")
    if not raw:
        return _make_action(UNKNOWN, raw, error="empty line")
    match = _KIND_RE.match(raw)
    if match is None:
        # Prose ("Nothing to approve today.") is informational; a "|" line lost its kind.
        error = f"no action type (expected {_FORMAT_HINT})" if "|" in raw else ""
        return _make_action(UNKNOWN, raw, title=raw, error=error)
    kind = _kind_word(match.group("label"))
    body = raw[match.end():].strip()
    if kind != CALENDAR:
        return _make_action(kind, raw, title=body)
    return _parse_calendar(raw, body)


def _kind_word(label: str) -> str:
    words = " ".join(re.split(r"[\s_-]+", label.casefold())).strip()
    if words in _CALENDAR_LABELS:
        return CALENDAR
    return _KIND_ALIASES.get(words) or words.split()[0]


def _parse_calendar(raw: str, body: str) -> ProposedAction:
    fields = [field.strip() for field in body.split("|")]
    fields += [""] * (5 - len(fields))
    title_text, when_text, repeat_text, where = fields[:4]
    notes = " | ".join(field for field in fields[4:] if field)
    found: dict[str, Any] = {"title": _title_text(title_text), "where": where, "notes": notes}
    try:
        if not found["title"]:
            raise _LineError("no title")
        if not when_text:
            raise _LineError(_NO_DATE)
        when = _parse_when(when_text)
        found.update(start=when.start, end=when.end, all_day_start=when.all_day_start,
                     all_day_end=when.all_day_end)
        repeat = _read_repeat(repeat_text)
        if repeat is not None:
            if repeat.until is not None and repeat.until < when.first_day:
                raise _LineError("the repeat ends before the event starts")
            found.update(repeat=repeat.words, rrule=repeat.rrule(all_day=when.all_day))
    except _LineError as exc:
        return _make_action(CALENDAR, raw, error=str(exc), **found)
    return _make_action(CALENDAR, raw, **found)


def _title_text(text: str) -> str:
    # The title is spoken and shown as a heading: a link or URL in it is noise.
    return _text_prep().strip_markdown(text) or text


# ---- key=value lines ----------------------------------------------------------
#
# "Reply: acct=work | thread=... | to=... | subject=... | body=..." (README:
# "Proposed actions format"). Values are taken as written: the markdown cleanup
# above would break Message-IDs, ids with "_" and the drafted text.

_LINE_CAP = 12000
_LINK_CAP = 2048
_SUBJECT_CAP = 250
_TITLE_CAP = 200
_WHO_CAP = 80
_SAID_CAP = 400
_LONG_BODY_CAP = 5000    # Reply, Email, Slack
_NOTE_BODY_CAP = 1000    # RSVP, Move, Cancel
_ADDRESS_CAP = 254
_QUOTE_CHARS = 40        # a bad value quoted in an error, middle elided
_MIN_RANGE = timedelta(minutes=5)
_MAX_RANGE = timedelta(hours=12)

_STRUCTURED_LABELS = {
    "reply": REPLY, "email": EMAIL, "e mail": EMAIL, "rsvp": RSVP, "move": MOVE, "cancel": CANCEL,
    "share": SHARE, "slack": SLACK, "todo": TODO, "to do": TODO, "open": OPEN,
}


@dataclass(frozen=True)
class _Key:
    name: str
    type: str
    required: bool = False


# Canonical key order per kind (the order the briefing task writes them in).
_SCHEMAS: dict[str, tuple[_Key, ...]] = {
    REPLY: (_Key("acct", "alias", True), _Key("thread", "gmail_id", True), _Key("msgid", "msgid"),
            _Key("gmid", "gmail_id"), _Key("to", "addresses", True), _Key("cc", "addresses"),
            _Key("subject", "subject", True), _Key("replied", "replied"), _Key("due", "due"),
            _Key("link", "link"), _Key("body", "body", True)),
    EMAIL: (_Key("acct", "alias", True), _Key("to", "addresses", True), _Key("cc", "addresses"),
            _Key("subject", "subject", True), _Key("due", "due"), _Key("link", "link"),
            _Key("body", "body", True)),
    RSVP: (_Key("acct", "alias", True), _Key("event", "event_id", True), _Key("cal", "cal_id"),
           _Key("answer", "answer", True), _Key("notify", "notify"), _Key("title", "title"),
           _Key("at", "at"), _Key("due", "due"), _Key("link", "link"), _Key("body", "body")),
    MOVE: (_Key("acct", "alias", True), _Key("event", "event_id", True), _Key("cal", "cal_id"),
           _Key("when", "range", True), _Key("notify", "notify"), _Key("title", "title"),
           _Key("at", "at"), _Key("link", "link"), _Key("body", "body")),
    CANCEL: (_Key("acct", "alias", True), _Key("event", "event_id", True), _Key("cal", "cal_id"),
             _Key("notify", "notify"), _Key("title", "title"), _Key("at", "at"), _Key("link", "link"),
             _Key("body", "body")),
    SHARE: (_Key("acct", "alias", True), _Key("file", "file_id", True), _Key("who", "address", True),
            _Key("role", "role"), _Key("title", "title"), _Key("link", "link")),
    SLACK: (_Key("team", "slack_team"), _Key("channel", "slack_channel", True), _Key("ts", "slack_ts"),
            _Key("thread", "slack_ts"), _Key("who", "who"), _Key("said", "said"), _Key("link", "link"),
            _Key("body", "body", True)),
    TODO: (_Key("title", "title", True), _Key("due", "due", True), _Key("block", "range"),
           _Key("acct", "alias"), _Key("link", "link")),
    OPEN: (_Key("title", "title", True), _Key("link", "link", True)),
}
_KEYS = {kind: frozenset(key.name for key in keys) for kind, keys in _SCHEMAS.items()}
_BODY_CAPS = {REPLY: _LONG_BODY_CAP, EMAIL: _LONG_BODY_CAP, SLACK: _LONG_BODY_CAP,
              RSVP: _NOTE_BODY_CAP, MOVE: _NOTE_BODY_CAP, CANCEL: _NOTE_BODY_CAP}
_DEFAULTS = {"cal_id": "primary", "notify": "all", "replied": "unknown", "role": "viewer"}
_RANGE_EXAMPLES = {"block": "19:00-21:00", "when": "13:00-14:00"}
# A card without a title= still needs a headline.
_FALLBACK_TITLES = {RSVP: "Calendar invitation", MOVE: "Meeting to move", CANCEL: "Meeting to cancel",
                    SHARE: "File share request"}

_STRUCTURED_SPACES = str.maketrans({"\u00a0": " ", "\u2007": " ", "\u202f": " "})
_EMOJI_PARTS = frozenset({"So", "Sk", "Mn"})   # what a zero-width joiner joins in a combined emoji
_LEADER_RE = re.compile(r"^\s*(?:[-*+\u2022]\s+|\d{1,3}[.)]\s+|\[[ xX]?\]\s+|>\s*)")
_LABEL_EMPHASIS_RE = re.compile(r"^(\*\*|__|\*|_)([A-Za-z][A-Za-z _-]{0,40}?)\s*(?:\1\s*:|:\s*\1)")
_FIRST_KEY_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]{0,23})\s*=")
_FIELD_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]{0,23})\s*=")
# These stay linear on a 12000-character line: a search for "\s*\|\s*" (or a fullmatch of
# "[^<>]*?\s*<") backtracks quadratically over a long run of spaces, and lines are read on the
# GUI thread.
_BODY_START_RE = re.compile(r"(?:^|\|)\s*body\s*=", re.IGNORECASE)
_ESCAPE_RE = re.compile(r"\\([\\n])")
_LINE_BREAK_RE = re.compile("[\n\r\x0b\x0c\x85\u2028\u2029]")
_CONTROL_RE = re.compile("[\x00-\x08\x0e-\x1f\x7f-\x9f]")
_BODY_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")
_SYMBOL_CATEGORIES = frozenset({"So", "Sk", "Cs"})
_SYMBOL_EXTRAS = frozenset({"\ufe0e", "\ufe0f", "\u200d"})

_ALIAS_RE = re.compile(r"[a-z][a-z0-9_-]{0,23}")
_GMAIL_ID_RE = re.compile(r"[0-9A-Za-z_-]{6,64}")
# Message-ID halves: printable ASCII without "<", ">", "@" or "|" (phase 3 writes it into In-Reply-To).
_MSGID_PART = r"[\x21-\x3b\x3d\x3f\x41-\x7b\x7d\x7e]{1,250}"
_MSGID_RE = re.compile(rf"<?({_MSGID_PART})@({_MSGID_PART})>?")
_LOCAL_PART_RE = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{}~.-]{1,64}")
_DOMAIN_RE = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}")
_DISPLAY_NAME_RE = re.compile(r"[^<>]*<(?P<address>[^<>]*)>")
_ADDRESS_SPLIT_RE = re.compile(r"[,;]")
_DUE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:(?:\s+|T)(\S.*))?")
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_EVENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{5,1024}")
_CAL_ID_RE = re.compile(r"primary|[\x21-\x3b\x3d\x3f-\x7b\x7d\x7e]{1,256}")   # printable ASCII but <, >, |
_FILE_ID_RE = re.compile(r"[A-Za-z0-9_-]{10,200}")
_SLACK_TEAM_RE = re.compile(r"[TE][A-Z0-9]{2,20}")
_SLACK_CHANNEL_RE = re.compile(r"[CDG][A-Z0-9]{2,20}")
_SLACK_TS_RE = re.compile(r"\d{9,12}\.\d{1,6}")
_ANSWERS = {"yes": "yes", "accept": "yes", "accepted": "yes", "going": "yes", "no": "no",
            "decline": "no", "declined": "no", "maybe": "maybe", "tentative": "maybe"}
_NOTIFY = {"all": "all", "external": "external", "externalonly": "external", "none": "none"}
_REPLIED = ("yes", "no", "unknown")
_ROLES = ("viewer", "commenter", "editor")
REPLIED_WARNING = "Couldn't tell if you already replied - check the thread first"
BLOCK_WARNING = "The block ends after the due time"


def _structured_line(text: str, link_hosts: Sequence[str]) -> ProposedAction | None:
    """The line as a key=value proposal, or None when it is not one (the old path reads it)."""
    try:
        raw = _clean_structured(text)
        found = _structured_kind(raw)
    except Exception as exc:  # noqa: BLE001 - never lose a line: the old path reads it instead
        logger.debug("Could not check a proposal line (%s)", type(exc).__name__)
        return None
    if found is None:
        return None
    kind, rest = found
    try:
        return _StructuredReader(kind, link_hosts).read(raw, rest)
    except Exception as exc:  # noqa: BLE001 - parse_action_line never raises
        logger.warning("Could not parse a %s line (%s)", kind, type(exc).__name__)
        return _make_action(kind, raw, error="could not read this line", structured=True)


def _clean_structured(text: str) -> str:
    """Spaces normalized, invisible format characters, list / quote / checkbox markers, leading
    emoji and an emphasized label ("**Reply:**") removed; otherwise the values stay as written."""
    text = _drop_format_characters(text.translate(_STRUCTURED_SPACES))
    for _ in range(4):
        stripped = _LEADER_RE.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    start = 0
    while start < len(text) and (text[start].isspace() or text[start] in _SYMBOL_EXTRAS
                                 or unicodedata.category(text[start]) in _SYMBOL_CATEGORIES):
        start += 1
    text = text[start:]
    text = _LABEL_EMPHASIS_RE.sub(lambda m: m.group(2).rstrip() + ":", text, count=1)
    return text.strip()


def _drop_format_characters(text: str) -> str:
    """``text`` without invisible format characters (Unicode Cf: zero-width spaces, direction
    overrides such as U+202E, tag characters), so a value reads exactly as it shows: a "to=" written
    with a zero-width space inside would show as a key in a subject without being one.

    A zero-width joiner between two emoji parts (one combined emoji) stays.
    """
    if text.isascii():
        return text
    kept: list[str] = []
    for index, char in enumerate(text):
        if unicodedata.category(char) != "Cf":
            kept.append(char)
        elif (char == "\u200d" and kept and index + 1 < len(text)
              and unicodedata.category(kept[-1]) in _EMOJI_PARTS
              and unicodedata.category(text[index + 1]) in _EMOJI_PARTS):
            kept.append(char)
    return "".join(kept)


def _structured_kind(line: str) -> tuple[str, str] | None:
    """(kind, the text after "<Kind>:") when the label is a key=value kind and a known key comes first."""
    match = _KIND_RE.match(line)
    if match is None:
        return None
    label = " ".join(part for part in re.split(r"[\s_-]+", match.group("label").casefold()) if part)
    kind = _STRUCTURED_LABELS.get(label)
    if kind is None:
        return None
    rest = line[match.end():]
    key = _FIRST_KEY_RE.match(rest)
    if key is None or key.group(1).casefold() not in _KEYS[kind]:
        return None
    return kind, rest


def _split_fields(kind: str, rest: str) -> dict[str, str]:
    """key -> value as written (body= swallows the rest of the line; see README)."""
    if len(rest) > _LINE_CAP:
        raise _LineError(f"the line is too long ({len(rest)} characters, at most {_LINE_CAP})")
    head, body = rest, None
    if "body" in _KEYS[kind]:
        match = _BODY_START_RE.search(rest)
        if match is not None:
            head, body = rest[:match.start()], rest[match.end():].strip()
    values: dict[str, str] = {}
    last = ""
    for fragment in head.split("|"):
        fragment = fragment.strip()
        if not fragment:
            continue
        key_match = _FIELD_KEY_RE.match(fragment)
        if key_match is None:
            # A value that contained " | " (a subject, say): the next part still belongs to it.
            if last:
                values[last] += " | " + fragment
            continue
        key = key_match.group(1).casefold()
        if key in values:
            raise _LineError(f"{key}= appears twice")
        if key == "bcc":
            raise _LineError("bcc= is not supported")
        values[key] = fragment[key_match.end():].strip()
        last = key
    unknown = sum(1 for key in values if key not in _KEYS[kind])
    if unknown:
        logger.debug("Ignored %d unknown key(s) (%s line)", unknown, kind)
    if body is not None:
        values["body"] = body
    return values


def _quoted(text: str) -> str:
    """A bad value for an error message: in quotes, cut to 40 characters (middle elided)."""
    text = " ".join(text.split())
    if len(text) > _QUOTE_CHARS:
        head = (_QUOTE_CHARS - 3 + 1) // 2
        tail = _QUOTE_CHARS - 3 - head
        text = f"{text[:head]}...{text[-tail:]}"
    return f'"{text}"'


def _unescape(text: str) -> str:
    """body= / said=: "\\n" is a line break and "\\\\" a backslash; other backslashes stay."""
    return _ESCAPE_RE.sub(lambda m: "\n" if m.group(1) == "n" else "\\", text)


class _StructuredReader:
    """Reads one key=value line of ``kind`` into ProposedAction fields (first error wins)."""

    def __init__(self, kind: str, link_hosts: Sequence[str]) -> None:
        self.kind = kind
        self.link_hosts = (link_hosts,) if isinstance(link_hosts, str) else tuple(link_hosts or ())
        self.attrs: dict[str, Any] = {"structured": True}
        self.pairs: list[tuple[str, str]] = []
        self.warnings: list[str] = []
        self.to: tuple[str, ...] = ()
        self.cc: tuple[str, ...] = ()
        self.due: date | datetime | None = None
        self.block: tuple[datetime, datetime] | None = None
        self.title = ""
        self.who = ""

    def read(self, raw: str, rest: str) -> ProposedAction:
        try:
            values = _split_fields(self.kind, rest)
            for key in _SCHEMAS[self.kind]:
                self._read_key(key, values.get(key.name, ""))
            self._cross_checks()
        except _LineError as exc:
            return self._action(raw, error=str(exc))
        self.attrs["title"] = self._headline()
        return self._action(raw)

    def _action(self, raw: str, error: str = "") -> ProposedAction:
        attrs = dict(self.attrs, fields=tuple(self.pairs), warnings=tuple(self.warnings))
        if error:
            attrs["error"] = error
        return _make_action(self.kind, raw, **attrs)

    def _read_key(self, key: _Key, text: str) -> None:
        text = text.strip()
        if key.type in ("body", "said") and text:
            text = _BODY_CONTROL_RE.sub("", _unescape(text)).strip()
        if not text:
            if key.required:
                raise _LineError(f"missing {key.name}=")
            default = _DEFAULTS.get(key.type)
            if default:
                self.pairs.append((key.name, default))
            if self.kind == REPLY and key.name == "gmid" and not self._has("msgid"):
                raise _LineError("missing msgid= (or gmid=)")
            return
        value = getattr(self, "_read_" + key.type)(key.name, text)
        if value is not None:
            self.pairs.append((key.name, value))

    def _has(self, name: str) -> bool:
        return any(key == name for key, _ in self.pairs)

    # ---- one reader per value type; each returns the normalized text (None = not kept) ----

    def _read_alias(self, name: str, text: str) -> str:
        value = text.casefold()
        if not _ALIAS_RE.fullmatch(value):
            raise _LineError(f"{name}=: {_quoted(text)} is not an account name (letters, digits, - or _)")
        self.attrs["account"] = value
        return value

    def _read_gmail_id(self, name: str, text: str) -> str:
        if not _GMAIL_ID_RE.fullmatch(text):
            raise _LineError(f"{name}=: {_quoted(text)} is not a Gmail id")
        return text

    def _read_msgid(self, name: str, text: str) -> str:
        match = _MSGID_RE.fullmatch(text)
        if match is None:
            raise _LineError(f"{name}=: {_quoted(text)} is not a Message-ID")
        return f"<{match.group(1)}@{match.group(2)}>"

    def _read_addresses(self, name: str, text: str) -> str | None:
        addresses = _address_list(name, text)
        if name == "to":
            self.to = addresses
        else:   # an address in both to= and cc= stays in to= only
            seen = {address.casefold() for address in self.to}
            addresses = tuple(address for address in addresses if address.casefold() not in seen)
            self.cc = addresses
        return ", ".join(addresses) or None

    def _read_address(self, name: str, text: str) -> str:
        addresses = _address_list(name, text)
        if len(addresses) != 1:
            raise _LineError(f"{name}=: give exactly one email address")
        return addresses[0]

    def _read_subject(self, name: str, text: str) -> str:
        value = _plain_text(name, text, _SUBJECT_CAP)
        if self.kind == REPLY and not value.casefold().startswith("re:"):
            value = "Re: " + value
        self.title = value
        return value

    def _read_title(self, name: str, text: str) -> str:
        self.title = _plain_text(name, text, _TITLE_CAP)
        return self.title

    def _read_who(self, name: str, text: str) -> str:
        self.who = _plain_text(name, text, _WHO_CAP)
        return self.who

    def _read_due(self, name: str, text: str) -> str:
        self.due = _read_due(name, text)
        return _iso(self.due)

    def _read_range(self, name: str, text: str) -> str:
        start, end = _read_range(name, text)
        self.block = (start, end)
        self.attrs.update(start=start, end=end)
        return f"{_iso(start)}/{_iso(end)}"

    def _read_at(self, name: str, text: str) -> str | None:
        try:
            when = _parse_when(text)
        except _LineError:
            self.warnings.append(f"{name}= could not be read; not shown")
            return None
        if when.all_day_start is not None:
            return f"{_iso(when.all_day_start)}/{_iso(when.all_day_end)}"
        return f"{_iso(when.start)}/{_iso(when.end)}"

    def _read_answer(self, name: str, text: str) -> str:
        return _choice(name, text, _ANSWERS, "use yes, no or maybe")

    def _read_notify(self, name: str, text: str) -> str:
        return _choice(name, text, _NOTIFY, "use all, external or none")

    def _read_replied(self, name: str, text: str) -> str:
        return _choice(name, text, {word: word for word in _REPLIED}, "use yes, no or unknown")

    def _read_role(self, name: str, text: str) -> str:
        return _choice(name, text, {word: word for word in _ROLES}, "use viewer, commenter or editor")

    def _read_event_id(self, name: str, text: str) -> str:
        return _matching(name, text, _EVENT_ID_RE, "is not a calendar event id")

    def _read_cal_id(self, name: str, text: str) -> str:
        return _matching(name, text, _CAL_ID_RE, "is not a calendar id")

    def _read_file_id(self, name: str, text: str) -> str:
        return _matching(name, text, _FILE_ID_RE, "is not a Drive file id")

    def _read_slack_team(self, name: str, text: str) -> str:
        return _matching(name, text, _SLACK_TEAM_RE, "is not a Slack workspace id")

    def _read_slack_channel(self, name: str, text: str) -> str:
        return _matching(name, text, _SLACK_CHANNEL_RE, "is not a Slack channel id")

    def _read_slack_ts(self, name: str, text: str) -> str:
        return _matching(name, text, _SLACK_TS_RE, "is not a Slack message ts")

    def _read_link(self, name: str, text: str) -> str | None:
        problem = _link_problem(text, self.link_hosts)
        if problem:
            if self.kind == OPEN:
                raise _LineError(problem)
            self.warnings.append(f"Link hidden: {problem}")
            return None
        self.attrs["link"] = text
        return text

    def _read_body(self, name: str, text: str) -> None:
        cap = _BODY_CAPS.get(self.kind, _LONG_BODY_CAP)
        if len(text) > cap:
            raise _LineError(f"{name}= is too long ({len(text)} characters, at most {cap})")
        self.attrs["body"] = text
        return None

    def _read_said(self, name: str, text: str) -> str:
        if len(text) > _SAID_CAP:
            raise _LineError(f"{name}= is too long ({len(text)} characters, at most {_SAID_CAP})")
        return text

    # ---- after every key -------------------------------------------------------------------

    def _cross_checks(self) -> None:
        if self.kind in (REPLY, EMAIL):
            count = len(self.to) + len(self.cc)
            if count > MAX_RECIPIENTS:
                raise _LineError(f"to= and cc= name {count} addresses (at most {MAX_RECIPIENTS})")
            if not self.to:
                raise _LineError("missing to=")
        if self.kind == TODO and self.block is not None and self.due is not None:
            due = self.due if isinstance(self.due, datetime) else \
                datetime.combine(self.due + timedelta(days=1), time())
            if self.block[1] > due:
                self.warnings.append(BLOCK_WARNING)
        if self.kind == REPLY and dict(self.pairs).get("replied") == "unknown":
            self.warnings.append(REPLIED_WARNING)

    def _headline(self) -> str:
        if self.kind == SLACK:
            return f"Slack message from {self.who}" if self.who else "Slack message"
        return self.title or _FALLBACK_TITLES.get(self.kind, "")


def _plain_text(name: str, text: str, cap: int) -> str:
    """A one-line text field: no line breaks or control characters, spaces collapsed."""
    if _LINE_BREAK_RE.search(text):
        raise _LineError(f"{name}= has a line break")
    if _CONTROL_RE.search(text):
        raise _LineError(f"{name}= has a control character")
    value = " ".join(text.split())
    if len(value) > cap:
        raise _LineError(f"{name}= is too long ({len(value)} characters, at most {cap})")
    return value


def _choice(name: str, text: str, choices: dict[str, str], hint: str) -> str:
    value = choices.get(text.casefold())
    if value is None:
        raise _LineError(f"{name}=: {hint}")
    return value


def _matching(name: str, text: str, pattern: re.Pattern[str], problem: str) -> str:
    if not pattern.fullmatch(text):
        raise _LineError(f"{name}=: {_quoted(text)} {problem}")
    return text


def _address_list(name: str, text: str) -> tuple[str, ...]:
    """"Ana <ana@example.edu>; ben@Example.EDU" -> ("ana@example.edu", "ben@example.edu")."""
    addresses: list[str] = []
    seen: set[str] = set()
    for item in _ADDRESS_SPLIT_RE.split(text):
        if not item.strip():
            continue
        address = _address(name, item.strip())
        if address.casefold() not in seen:
            seen.add(address.casefold())
            addresses.append(address)
    return tuple(addresses)


def _address(name: str, item: str) -> str:
    """One "local@domain" or "Display Name <local@domain>"; the domain is casefolded."""
    address = email_address(item)
    if not address:
        raise _LineError(f"{name}=: {_quoted(item)} is not an email address")
    return address


def email_address(text: str) -> str:
    """``text`` as the address Jarvis sends to, or "" when it is not exactly one address.

    "local@domain" or "Display Name <local@domain>": the display name is dropped (it never
    decides who an address is) and the domain is casefolded. The local part is 1-64 RFC 5322
    atext characters (ASCII letters, digits and !#$%&'*+/=?^_`{}~.- ; no quotes, spaces, "|"
    or line breaks) without leading, trailing or double dots; the domain is ASCII labels ending
    in a 2-63 letter top-level domain; 254 characters at most.
    """
    if not isinstance(text, str):
        return ""
    item = text.strip()
    match = _DISPLAY_NAME_RE.fullmatch(item)
    address = match.group("address").strip() if match is not None else item
    local, at, domain = address.rpartition("@")
    if (not at or not _LOCAL_PART_RE.fullmatch(local) or local.startswith(".") or local.endswith(".")
            or ".." in local or not _DOMAIN_RE.fullmatch(domain) or len(address) > _ADDRESS_CAP):
        return ""
    return f"{local}@{domain.casefold()}"


def _read_due(name: str, text: str) -> date | datetime:
    """"2026-10-07", "2026-10-07 23:59" or "2026-10-07 11:59 PM"."""
    match = _DUE_RE.fullmatch(text)
    if match is None:
        raise _LineError(f"{name}=: {_quoted(text)} is not a date (YYYY-MM-DD, optionally with a time)")
    year, month, day_number = (int(part) for part in _ISO_DATE_RE.fullmatch(match.group(1)).groups())
    try:
        day = date(year, month, day_number)
    except ValueError:
        raise _LineError(f'{name}=: "{match.group(1)}" is not a real date') from None
    clock = match.group(2)
    if clock is None:
        return day
    try:
        minutes = _lone_minutes(_read_clock(" ".join(clock.casefold().split())))
    except _LineError as exc:
        raise _LineError(f"{name}=: {exc}") from None
    return datetime.combine(day, time()) + timedelta(minutes=minutes)


def _read_range(name: str, text: str) -> tuple[datetime, datetime]:
    """A timed range in the Calendar grammar ("2026-10-06 19:00-21:00"), 5 minutes to 12 hours."""
    try:
        when = _parse_when(text)
    except _LineError as exc:
        raise _LineError(f"{name}=: {exc}") from None
    if when.start is None or when.end is None:
        day = when.all_day_start.isoformat() if when.all_day_start is not None else "YYYY-MM-DD"
        raise _LineError(f"{name}=: give a time range like {day} {_RANGE_EXAMPLES.get(name, '13:00-14:00')}")
    if not _MIN_RANGE <= when.end - when.start <= _MAX_RANGE:
        raise _LineError(f"{name}=: the time range must be 5 minutes to 12 hours long")
    return when.start, when.end


# ---- links -----------------------------------------------------------------------

_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")
_NOT_HTTPS = "not an https link"


def link_allowed(url: str, extra_hosts: Sequence[str] = ()) -> bool:
    """True for an https link a card's Open may open.

    https only, no user name or port, an ASCII host that is one of
    BUILTIN_LINK_HOSTS (www.google.com only under /calendar/) or matches an
    ``extra_hosts`` pattern ("name.tld" exactly, "*.name.tld" any
    subdomain). Checked when the line is read and again on the click.
    """
    return not _link_problem(url, extra_hosts)


def _https_parts(url: str) -> tuple[str, Any, str]:
    """(why ``url`` is not a plain https link ("" when it is), its urlsplit parts, its host).
    https only, at most 2,048 characters, no whitespace / control / format character or
    backslash, no user name or password, port none or 443, no dot segments, an ASCII host."""
    if not isinstance(url, str) or not url or len(url) > _LINK_CAP or "\\" in url:
        return _NOT_HTTPS, None, ""
    if any(ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf") for ch in url):
        return _NOT_HTTPS, None, ""
    if not url[:8].casefold() == "https://":
        return _NOT_HTTPS, None, ""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return _NOT_HTTPS, None, ""
    if parts.scheme != "https" or parts.username is not None or parts.password is not None:
        return _NOT_HTTPS, None, ""
    if port not in (None, 443):
        return _NOT_HTTPS, None, ""
    # "/calendar/../url" (also as "%2e%2e" or ".%2E") passes a path check, but the browser removes
    # the dot segments and opens "/url": refused on every host.
    if any(unquote(segment) in (".", "..") for segment in parts.path.split("/")):
        return _NOT_HTTPS, None, ""
    host = (parts.hostname or "").rstrip(".")
    return "", parts, host


def _link_problem(url: str, extra_hosts: Sequence[str] = ()) -> str:
    """Why Open may not open ``url`` ("" when it may)."""
    problem, parts, host = _https_parts(url)
    if problem:
        return problem
    if not host or not host.isascii() or not _HOST_RE.fullmatch(host):
        return _NOT_HTTPS
    extras = (extra_hosts,) if isinstance(extra_hosts, str) else tuple(extra_hosts or ())
    patterns = [pattern.strip().casefold().rstrip(".") for pattern in extras if isinstance(pattern, str)]
    if any(label.startswith("xn--") for label in host.split(".")):
        allowed = host in patterns   # an IDN host only when listed by its exact name
    else:
        allowed = _host_listed(host, BUILTIN_LINK_HOSTS) or _host_listed(host, patterns) or (
            host == "www.google.com" and parts.path.startswith("/calendar/"))
    return "" if allowed else f"{host} is not on the list of hosts Open may open ([actions] link_hosts)"


def _host_listed(host: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]) and len(host) > len(pattern) - 1:
                return True
        elif host == pattern:
            return True
    return False


LOCAL_ADDRESS = "a local or private address"
NOT_WEB_ADDRESS = "not a web address"
IDN_WARNING = "International domain name - check the address before you open it"
# Hosts that name this PC, its network or no public place (a name under these is never on the web).
_PRIVATE_HOSTS = ("localhost",)
_PRIVATE_SUFFIXES = (".localhost", ".local", ".lan", ".home", ".internal", ".intranet", ".corp", ".home.arpa",
                     ".test", ".invalid", ".onion")
_TLD_RE = re.compile(r"[a-z]{2,63}|xn--[a-z0-9-]{1,59}")
_WEB_LINK_BANNED = frozenset('|<>"`')
# Public names known to lead to this PC or its network (Jarvis checks a name, never where it
# resolves): wildcard DNS that answers any IP address written in the name ("192.168.1.1.nip.io"),
# names fixed to 127.0.0.1, and the setup pages of home routers. A name under one is local too.
LOCAL_DOMAINS = frozenset({
    "nip.io", "sslip.io", "xip.io", "nip.direct", "localtest.me", "lvh.me", "vcap.me", "lacolhost.com",
    "localhost.direct", "traefik.me", "fuf.me", "localho.st",
    "routerlogin.net", "routerlogin.com", "orbilogin.net", "orbilogin.com", "mywifiext.net", "mywifiext.com",
    "tplinkwifi.net", "tplinklogin.net", "tplinkmodem.net", "tplinkrepeater.net", "tplinkap.net",
    "router.asus.com", "asusrouter.com", "repeater.asus.com", "linksyssmartwifi.com", "miwifi.com",
    "fritz.box", "speedport.ip", "dlinkrouter.com", "myrouter.com"})


def _names_local_ip(labels: Sequence[str]) -> bool:
    """Four labels in a row ("10.0.0.1.example.net") or four parts of one label
    ("127-0-0-1.example.net") that read as a loopback, private or other non-public IPv4 address:
    wildcard DNS answers with that address."""
    runs = [list(labels)] + [label.split("-") for label in labels if label.count("-") >= 3]
    for parts in runs:
        for start in range(len(parts) - 3):
            quad = parts[start:start + 4]
            if all(part.isdigit() and len(part) <= 3 and int(part) <= 255 for part in quad) \
                    and not _public_ip(".".join(str(int(part)) for part in quad)):
                return True
    return False


def public_host_problem(host: Any) -> str:
    """Why ``host`` is not a public web host ("" when it is): an ASCII host name (IDN as "xn--"
    labels) with a dot, whose last label is letters or "xn--...", that is not an IP address (no
    label-only-digits IPv4; IPv6 literals fail the name rule) and not localhost or a name under
    .localhost, .local, .lan, .home, .internal, .intranet, .corp, .home.arpa, .test, .invalid or
    .onion, nor a public name known to lead to this PC or its network (LOCAL_DOMAINS; a name that
    spells a non-public IPv4 address, such as "192.168.1.1.example.net"). LOCAL_ADDRESS or
    NOT_WEB_ADDRESS. Only the name is checked: where it resolves is not (README: Web research)."""
    if not isinstance(host, str):
        return NOT_WEB_ADDRESS
    name = host.strip().casefold().rstrip(".")
    if ":" in name or name.startswith("["):   # an IPv6 literal: never a web host
        return NOT_WEB_ADDRESS if _public_ip(name.strip("[]")) else LOCAL_ADDRESS
    if not name or not name.isascii() or not _HOST_RE.fullmatch(name):
        return NOT_WEB_ADDRESS
    labels = name.split(".")
    if all(label.isdigit() for label in labels):   # an IPv4 literal (or a short form such as 127.1)
        return NOT_WEB_ADDRESS if _public_ip(name) else LOCAL_ADDRESS
    if name in _PRIVATE_HOSTS or name.endswith(_PRIVATE_SUFFIXES) or len(labels) < 2:
        return LOCAL_ADDRESS   # a name without a dot is looked up on the local network
    if any(".".join(labels[start:]) in LOCAL_DOMAINS for start in range(len(labels) - 1)) \
            or _names_local_ip(labels):
        return LOCAL_ADDRESS
    if not _TLD_RE.fullmatch(labels[-1]):
        return NOT_WEB_ADDRESS
    return ""


def _public_ip(text: str) -> bool:
    """True for a public IP address (still refused, as "not a web address"); False for a loopback,
    private, link-local, reserved one and for anything Python cannot read as one (a short form the
    browser would still read, such as 127.1)."""
    import ipaddress

    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return address.is_global


def web_link_problem(url: Any) -> str:
    """Why a web research card may not open ``url`` ("" when it may): everything an Open link must
    be (https, at most 2,048 characters, no whitespace / control / format character or backslash,
    no user name or password, port none or 443, no dot segments), none of | < > " ` (a source
    becomes a key=value line and is shown as text), and a public host (public_host_problem) on any
    site. Checked when the research's answer is read and again on the card's click."""
    if not isinstance(url, str) or any(char in _WEB_LINK_BANNED for char in url):
        return _NOT_HTTPS
    problem, _parts, host = _https_parts(url)
    if problem:
        return problem
    return public_host_problem(host)


def web_link_allowed(url: Any) -> bool:
    """True for a link a web research card may open (web_link_problem)."""
    return not web_link_problem(url)


def idn_host(url: str) -> bool:
    """The host of ``url`` has an internationalized ("xn--") label: shown with IDN_WARNING."""
    return any(label.startswith("xn--") for label in link_host(url).casefold().split("."))


def link_host(url: str) -> str:
    """The host of ``url`` ("docs.google.com"), "" when it has none."""
    try:
        return (urlsplit(url).hostname or "").rstrip(".")
    except ValueError:
        return ""


def body_links(text: str) -> tuple[tuple[int, int], ...]:
    """(start, end) of every web link in a drafted text, so the card and the Edit dialog can
    highlight them (trailing punctuation is not part of a link)."""
    spans: list[tuple[int, int]] = []
    for match in _URL_RE.finditer(text or ""):
        core = match.group(0).rstrip(_URL_TRAILING)
        if core:
            spans.append((match.start(), match.start() + len(core)))
    return tuple(spans)


def due_words(action: ProposedAction, today: date) -> str:
    """"due today", "due tomorrow" or "due Wed Oct 7" (+ ", 11:59 PM" when timed); "" without
    due=. A Reply / Email card shows this as its detail: its recipients are chips."""
    return _due_words(action.due(), today)


def links_note(text: str) -> str:
    """"Contains 1 link" / "Contains 3 links" ("" without links)."""
    count = len(body_links(text))
    if not count:
        return ""
    return f"Contains {count} link" + ("" if count == 1 else "s")


# ---- markdown cleanup that keeps URLs ------------------------------------

_URL_RE = re.compile(r"(?:\b(?:https?|ftp)://|\bwww\.)(?:\([^\s()<>|]*\)|[^\s()<>|])+", re.I)
_URL_TRAILING = ".,;:!?'\"*_~"
_MD_LINK_RE = re.compile(
    r"(?<!!)\[([^\[\]]*)\]\(\s*<?((?:[^()\s<>]|\([^()\s]*\))+)>?(?:\s+\"[^\"]*\")?\s*\)")
_ANGLE_URL_RE = re.compile(r"<((?:https?|ftp)://[^\s<>]+)>", re.I)
# URLs are parked on private-use characters while strip_markdown runs (it removes URLs).
_PARK_BASE = 0xF000
_PARK_LIMIT = 0x400
_PARKED_RE = re.compile("[\uf000-\uf3ff]")


def _clean_line(text: str) -> str:
    """Markdown, HTML and emoji removed like text_prep.strip_markdown, but URLs kept.

    A meeting link is the most useful "where", so "[Zoom](https://...)"
    becomes "Zoom (https://...)" and bare URLs stay.
    """
    text = _PARKED_RE.sub("", text)
    text = _MD_LINK_RE.sub(_link_text, text)
    text = _ANGLE_URL_RE.sub(r"\1", text)
    urls: list[str] = []

    def park(match: re.Match[str]) -> str:
        url = match.group(0)
        core = url.rstrip(_URL_TRAILING)
        if len(urls) >= _PARK_LIMIT:
            return url
        urls.append(core)
        return chr(_PARK_BASE + len(urls) - 1) + url[len(core):]

    text = _URL_RE.sub(park, text)
    text = _text_prep().strip_markdown(text)
    return _PARKED_RE.sub(lambda m: urls[ord(m.group()) - _PARK_BASE], text)


def _link_text(match: re.Match[str]) -> str:
    label, url = match.group(1).strip(), match.group(2)
    if not label or label == url or _URL_RE.fullmatch(label):
        return url
    if _URL_RE.match(url):
        return f"{label} ({url})"
    return label   # a relative or in-app link has nothing worth keeping


# ---- <when> ---------------------------------------------------------------

@dataclass(frozen=True)
class _When:
    start: datetime | None = None
    end: datetime | None = None
    all_day_start: date | None = None
    all_day_end: date | None = None

    @property
    def all_day(self) -> bool:
        return self.all_day_start is not None

    @property
    def first_day(self) -> date:
        if self.all_day_start is not None:
            return self.all_day_start
        assert self.start is not None
        return self.start.date()


_WEEKDAY_INDEX = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "weds": 2,
    "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}
_WEEKDAY_WORD = "(?:" + "|".join(sorted(_WEEKDAY_INDEX, key=len, reverse=True)) + r")\.?"
_DASH_RE = re.compile("[\u2010-\u2015\u2212]")      # hyphens, en/em dash, minus sign
_WHEN_NOISE_RE = re.compile(r"[()\[\],;]")
_SPACES_RE = re.compile(r"\s+")
_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)")
_ALL_DAY_RE = re.compile(r"\ball[\s-]?day\b")
_RANGE_WORDS = r"(?:to|through|thru|until|till)"
_RANGE_SEP_RE = re.compile(rf"\s*(?:-|\b{_RANGE_WORDS}\b)\s*")
_LEAD_RE = re.compile(r"^(?:(?:on|from|at)\b|@|t(?=\d))\s*")
_WEEKDAY_LEAD_RE = re.compile(rf"^({_WEEKDAY_WORD})(?:\s+|$)")
# Between two dates: an optional time for the first one, the range word, an optional weekday.
_MIDDLE_RE = re.compile(
    rf"(?:(?:at\s+)?(?P<time>\S.*?)\s*)?(?:-|\b{_RANGE_WORDS}\b)\s*(?P<weekday>{_WEEKDAY_WORD})?")


def _parse_when(text: str) -> _When:
    """"2026-10-09 15:00-16:00", "2026-10-09 3pm", "2026-10-05", "2027-03-22 to 2027-03-26"."""
    shown = " ".join(text.split())
    s = _SPACES_RE.sub(" ", _WHEN_NOISE_RE.sub(" ", _DASH_RE.sub("-", text).casefold()))
    s, all_day_marks = _ALL_DAY_RE.subn(" ", s)
    s = _SPACES_RE.sub(" ", s).strip().rstrip(".").strip()
    dates = list(_DATE_RE.finditer(s))
    if not dates:
        raise _LineError(_NO_DATE)
    if len(dates) > 2:
        raise _LineError(f'could not read the date "{shown}"')
    first = _make_date(dates[0])
    _check_prefix(s[:dates[0].start()], first, shown)
    if len(dates) == 1:
        rest = _take_weekday(_strip_lead(s[dates[0].end():]), first)
        if not rest:
            return _When(all_day_start=first, all_day_end=first)
        if all_day_marks:
            raise _LineError(f'both a time and "all day" in "{shown}"')
        start_minutes, end_minutes = _parse_time_range(rest)
        start = datetime.combine(first, time()) + timedelta(minutes=start_minutes)
        return _When(start=start, end=start + timedelta(minutes=end_minutes - start_minutes))
    return _parse_two_dates(s, dates, first, shown, bool(all_day_marks))


def _parse_two_dates(s: str, dates: list[re.Match[str]], first: date, shown: str,
                     all_day: bool) -> _When:
    second = _make_date(dates[1])
    middle = _MIDDLE_RE.fullmatch(s[dates[0].end():dates[1].start()].strip())
    if middle is None:
        raise _LineError(f'could not read the dates "{shown}" (expected "YYYY-MM-DD to YYYY-MM-DD")')
    if middle.group("weekday"):
        _check_weekday(middle.group("weekday"), second)
    first_time = (middle.group("time") or "").strip()
    second_time = _strip_lead(s[dates[1].end():])
    if not first_time and not second_time:
        if second < first:
            raise _LineError("the end date is before the start date")
        return _When(all_day_start=first, all_day_end=second)
    if not (first_time and second_time):
        raise _LineError(f'give a time for both dates or for neither in "{shown}"')
    if all_day:
        raise _LineError(f'both a time and "all day" in "{shown}"')
    start = datetime.combine(first, time()) + timedelta(minutes=_lone_minutes(_read_clock(first_time)))
    end = datetime.combine(second, time()) + timedelta(minutes=_lone_minutes(_read_clock(second_time)))
    if end <= start:
        raise _LineError("the event ends before it starts")
    return _When(start=start, end=end)


def _make_date(match: re.Match[str]) -> date:
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        raise _LineError(f'"{match.group(0)}" is not a real date') from None


def _strip_lead(text: str) -> str:
    text = text.strip()
    while True:
        stripped = _LEAD_RE.sub("", text, count=1).strip()
        if stripped == text:
            return text
        text = stripped


def _check_prefix(prefix: str, day: date, shown: str) -> None:
    """Only an optional weekday ("Fri", "on Friday") may come before the date."""
    prefix = _strip_lead(prefix)
    if not prefix:
        return
    if _take_weekday(prefix, day):
        raise _LineError(f'could not read the date "{shown}"')


def _take_weekday(text: str, day: date) -> str:
    """Drop a leading weekday after checking it matches ``day``; return the rest."""
    match = _WEEKDAY_LEAD_RE.match(text)
    if match is None:
        return text
    _check_weekday(match.group(1), day)
    return _strip_lead(text[match.end():])


def _check_weekday(word: str, day: date) -> None:
    if _WEEKDAY_INDEX[word.rstrip(".")] != day.weekday():
        raise _LineError(f"{day.isoformat()} is a {_WEEKDAY_LONG[day.weekday()]}, "
                         f"not {word.rstrip('.').capitalize()}")


# ---- times ------------------------------------------------------------------

_TIME_TOKEN_RE = re.compile(r"(?P<hour>\d{1,2})(?:[:.](?P<minute>\d{2}))?\s*(?:(?P<mer>[ap])\.?(?:m\.?)?)?")
_TWELVE_HOURS = 12 * 60


@dataclass(frozen=True)
class _Clock:
    """One written time. ``style``: "24h", "12h" (AM/PM given), "word" (noon/midnight)
    or "open" (1-12 without AM/PM: either half of the day)."""

    text: str
    minutes: int          # after midnight, reading an "open" time as AM
    style: str
    has_minutes: bool


def _read_clock(token: str) -> _Clock:
    token = token.strip()
    if token in ("noon", "12 noon"):
        return _Clock(token, _TWELVE_HOURS, "word", True)
    if token in ("midnight", "12 midnight"):
        return _Clock(token, 0, "word", True)
    match = _TIME_TOKEN_RE.fullmatch(token)
    if match is None:
        raise _LineError(f'could not read the time "{token}"')
    hour, minute = int(match.group("hour")), int(match.group("minute") or 0)
    has_minutes = match.group("minute") is not None
    meridiem = match.group("mer")
    if minute > 59:
        raise _LineError(f'could not read the time "{token}"')
    if meridiem:
        if not 1 <= hour <= 12:
            raise _LineError(f'could not read the time "{token}"')
        offset = _TWELVE_HOURS if meridiem == "p" else 0
        return _Clock(token, (hour % 12) * 60 + offset + minute, "12h", has_minutes)
    if hour > 24 or (hour == 24 and minute):
        raise _LineError(f'could not read the time "{token}"')
    style = "24h" if hour == 0 or hour >= 13 else "open"
    return _Clock(token, hour * 60 + minute, style, has_minutes)


def _lone_minutes(clock: _Clock) -> int:
    """A time on its own: "open" ones are 24-hour when written as HH:MM."""
    if clock.style == "open" and not clock.has_minutes:
        raise _LineError(f'the time "{clock.text}" needs AM/PM or HH:MM (24-hour)')
    return clock.minutes


def _parse_time_range(text: str) -> tuple[int, int]:
    """"15:00-16:00", "3pm - 4:30 PM", "3-4pm", "11:30-12:30 PM" -> (start, end) minutes.

    The end may pass midnight (end before start = next day). A single time lasts 60 minutes.
    """
    parts = _RANGE_SEP_RE.split(text)
    if len(parts) > 2 or not all(part.strip() for part in parts):
        raise _LineError(f'could not read the time "{text}"')
    clocks = [_read_clock(part) for part in parts]
    if len(clocks) == 1:
        start = _lone_minutes(clocks[0])
        if start >= _DAY_MINUTES:
            raise _LineError(f'could not read the time "{text}"')
        return start, start + int(DEFAULT_DURATION.total_seconds() // 60)
    start, duration = _resolve_pair(clocks[0], clocks[1], text)
    return start, start + duration


def _resolve_pair(first: _Clock, second: _Clock, text: str) -> tuple[int, int]:
    """(start minutes, duration) for a range, filling in a missing AM/PM.

    An "open" side takes whichever half of the day gives the shorter event:
    "3-4pm" is 3 to 4 PM, "9-5pm" 9 AM to 5 PM, "11-1am" 11 PM to 1 AM.
    """
    if first.style == "open" and second.style == "open":
        if not (first.has_minutes and second.has_minutes):
            raise _LineError(f'the time "{text}" needs AM/PM or HH:MM (24-hour)')
        candidates = [(first.minutes, second.minutes)]
    else:
        candidates = [(start, end) for start in _readings(first, second)
                      for end in _readings(second, first)]
    best: tuple[int, int] | None = None
    for start, end in candidates:
        duration = _duration(start, end)
        if start < _DAY_MINUTES and duration and (best is None or duration < best[1]):
            best = (start, duration)
    if best is None:
        raise _LineError(f'the time "{text}" ends when it starts')
    return best


def _readings(clock: _Clock, other: _Clock) -> list[int]:
    if clock.style != "open" or (clock.has_minutes and other.style == "24h"):
        return [clock.minutes]
    return [clock.minutes, (clock.minutes + _TWELVE_HOURS) % _DAY_MINUTES]


def _duration(start: int, end: int) -> int:
    """Minutes from start to end, wrapping past midnight; 0 when they are the same time."""
    if end % _DAY_MINUTES == start:
        return 0
    return end - start if end > start else end - start + _DAY_MINUTES


# ---- <repeat> -------------------------------------------------------------

_REPEAT_ALIASES = {
    "daily": "daily", "every day": "daily", "each day": "daily", "everyday": "daily",
    "weekdays": "weekdays", "every weekday": "weekdays", "each weekday": "weekdays",
    "on weekdays": "weekdays", "weekdays only": "weekdays", "mon-fri": "weekdays",
    "monday-friday": "weekdays",
    "weekly": "weekly", "every week": "weekly", "each week": "weekly",
    "biweekly": "biweekly", "bi-weekly": "biweekly", "fortnightly": "biweekly",
    "every 2 weeks": "biweekly", "every two weeks": "biweekly", "every other week": "biweekly",
    "monthly": "monthly", "every month": "monthly", "each month": "monthly",
    "yearly": "yearly", "annually": "yearly", "annual": "yearly", "every year": "yearly",
    "each year": "yearly",
}
_NO_REPEAT = frozenset({
    "", "once", "one-off", "one off", "one time", "one-time", "single", "none", "no", "never",
    "no repeat", "does not repeat", "doesn't repeat", "-", "n/a", "na",
})
_RRULE_FREQ = {
    "daily": "FREQ=DAILY",
    "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
    "weekly": "FREQ=WEEKLY",
    "biweekly": "FREQ=WEEKLY;INTERVAL=2",
    "monthly": "FREQ=MONTHLY",
    "yearly": "FREQ=YEARLY",
}
_REPEAT_DISPLAY = {"weekdays": "weekdays", "biweekly": "every 2 weeks"}
_REPEAT_SPOKEN = {"weekdays": "every weekday", "biweekly": "every two weeks"}
_REPEAT_RE = re.compile(
    r"(?P<freq>.*?)\s*(?:"
    r"\b(?:until|till|through|thru|ending(?:\s+on)?|ends?(?:\s+on)?)\s+(?P<until>\S.*)"
    r"|(?:x|\u00d7)\s*(?P<count>\d+)"
    r"|(?:for\s+)?(?P<times>\d+)\s*(?:times|occurrences|x)"
    r")?"
)
_REPEAT_HINT = "use daily, weekdays, weekly, biweekly, monthly or yearly"


@dataclass(frozen=True)
class _Repeat:
    freq: str
    until: date | None = None
    count: int | None = None

    @property
    def words(self) -> str:
        if self.until is not None:
            return f"{self.freq} until {self.until.isoformat()}"
        if self.count is not None:
            return f"{self.freq} x {self.count}"
        return self.freq

    def rrule(self, *, all_day: bool) -> str:
        rule = "RRULE:" + _RRULE_FREQ[self.freq]
        if self.until is not None:
            # gcal sends the start with the calendar's time zone, so a timed UNTIL is UTC.
            stamp = self.until.strftime("%Y%m%d")
            rule += f";UNTIL={stamp}" if all_day else f";UNTIL={stamp}T235959Z"
        elif self.count is not None:
            rule += f";COUNT={self.count}"
        return rule


def _read_repeat(text: str) -> _Repeat | None:
    """"weekly until 2026-12-11", "daily x 5", "monthly 3 times" -> _Repeat; "" / "once" -> None."""
    shown = " ".join(text.split())
    s = _SPACES_RE.sub(" ", _WHEN_NOISE_RE.sub(" ", _DASH_RE.sub("-", text).casefold()))
    s = s.strip().rstrip(".").strip()
    match = _REPEAT_RE.fullmatch(s)
    if match is None:   # unreachable: the pattern accepts any text
        raise _LineError(f'could not read the repeat "{shown}" ({_REPEAT_HINT})')
    freq = match.group("freq").strip()
    until_text = match.group("until")
    count_text = match.group("count") or match.group("times")
    if freq in _NO_REPEAT:
        if until_text or count_text:
            raise _LineError(f'"{shown}" has an end but no repeat ({_REPEAT_HINT})')
        return None
    if freq not in _REPEAT_ALIASES:
        raise _LineError(f'could not read the repeat "{shown}" ({_REPEAT_HINT})')
    freq = _REPEAT_ALIASES[freq]
    if until_text:
        date_match = _DATE_RE.fullmatch(until_text.rstrip("."))
        if date_match is None:
            raise _LineError(f'could not read the end date in "{shown}" (expected YYYY-MM-DD)')
        return _Repeat(freq, until=_make_date(date_match))
    if count_text:
        count = int(count_text)
        if not 1 <= count <= _MAX_COUNT:
            raise _LineError(f"the repeat count must be 1 to {_MAX_COUNT}")
        return None if count == 1 else _Repeat(freq, count=count)
    return _Repeat(freq)


# --------------------------------------------------------------------------
# describe() / spoken() wording
# --------------------------------------------------------------------------

def _short_day(day: date, show_year: bool) -> str:
    text = f"{_WEEKDAY_SHORT[day.weekday()]} {_MONTH_SHORT[day.month - 1]} {day.day}"
    return f"{text}, {day.year}" if show_year else text


def _long_day(day: date, show_year: bool) -> str:
    text = f"{_WEEKDAY_LONG[day.weekday()]} {_MONTH_LONG[day.month - 1]} {day.day}"
    return f"{text}, {day.year}" if show_year else text


def _years_shown(first: date, last: date, today: date) -> tuple[bool, bool]:
    """A range names its year once, at the end, unless it spans two years."""
    show_last = last.year != today.year
    show_first = first.year != today.year and first.year != last.year
    return show_first, show_last


def _meridiem(moment: datetime) -> str:
    return _text_prep().clock_parts(moment)[1]


def _clock_text(moment: datetime) -> str:
    """"3:00" (12-hour digits; cards always use the 12-hour clock)."""
    return _text_prep().clock_parts(moment)[0]


def _is_short(start: datetime, end: datetime) -> bool:
    """Same day, or past midnight but under 24 hours: one date and a time range."""
    return end - start < timedelta(days=1) and (end.date() - start.date()).days <= 1


def _describe_when(action: ProposedAction, today: date) -> list[str]:
    if action.all_day_start is not None:
        first = action.all_day_start
        last = action.all_day_end or first
        if last == first:
            return [_short_day(first, first.year != today.year), "all day"]
        show_first, show_last = _years_shown(first, last, today)
        return [f"{_short_day(first, show_first)} - {_short_day(last, show_last)}", "all day"]
    start, end = action.start, action.end
    if start is None:
        return []
    end = end or start + DEFAULT_DURATION
    if _is_short(start, end):
        return [_short_day(start.date(), start.year != today.year), _display_times(start, end)]
    show_first, show_last = _years_shown(start.date(), end.date(), today)
    return [f"{_short_day(start.date(), show_first)} {_clock_text(start)} {_meridiem(start)} - "
            f"{_short_day(end.date(), show_last)} {_clock_text(end)} {_meridiem(end)}"]


def _display_times(start: datetime, end: datetime) -> str:
    """"3:00-4:00 PM", "11:30 AM-12:30 PM", "11:00 PM-1:00 AM (next day)"."""
    if start.date() == end.date() and _meridiem(start) == _meridiem(end):
        return f"{_clock_text(start)}-{_clock_text(end)} {_meridiem(end)}"
    text = f"{_clock_text(start)} {_meridiem(start)}-{_clock_text(end)} {_meridiem(end)}"
    if end.date() > start.date() and end.time() != time():
        text += " (next day)"
    return text


def _spoken_title(title: str) -> str:
    spoken = _text_prep().to_spoken(title) or title
    return spoken.rstrip(".!?").strip() or title


def _spoken_when(action: ProposedAction, today: date) -> list[str]:
    if action.all_day_start is not None:
        first = action.all_day_start
        last = action.all_day_end or first
        if last == first:
            return [_long_day(first, first.year != today.year), "all day"]
        show_first, show_last = _years_shown(first, last, today)
        return [f"{_long_day(first, show_first)} to {_long_day(last, show_last)}", "all day"]
    start, end = action.start, action.end
    if start is None:
        return []
    end = end or start + DEFAULT_DURATION
    if _is_short(start, end):
        return [_long_day(start.date(), start.year != today.year), _spoken_times(start, end)]
    show_first, show_last = _years_shown(start.date(), end.date(), today)
    return [f"from {_long_day(start.date(), show_first)} at {_spoken_clock(start, True)} "
            f"to {_long_day(end.date(), show_last)} at {_spoken_clock(end, True)}"]


def _spoken_clock(moment: datetime, with_meridiem: bool) -> str:
    """"3", "3:30", "noon", "midnight" (plus " PM" when asked, never after noon/midnight)."""
    if moment.minute == 0 and moment.hour in (0, 12):
        return "midnight" if moment.hour == 0 else "noon"
    digits = _clock_text(moment)
    text = digits.partition(":")[0] if moment.minute == 0 else digits
    return f"{text} {_meridiem(moment)}" if with_meridiem else text


def _spoken_times(start: datetime, end: datetime) -> str:
    """"3 to 4 PM", "3:30 to 4 PM", "11 AM to 1 PM", "noon to 1 PM"."""
    words = {"noon", "midnight"}
    first, last = _spoken_clock(start, False), _spoken_clock(end, False)
    same_half = start.date() == end.date() and _meridiem(start) == _meridiem(end)
    if same_half and first not in words and last not in words:
        return f"{first} to {last} {_meridiem(end)}"
    return f"{_spoken_clock(start, True)} to {_spoken_clock(end, True)}"


_NOTIFY_WORDS = {"all": "guests notified", "external": "only outside guests notified",
                 "none": "guests not notified"}
# An RSVP's notify= decides whether Google emails the organizer about the answer (sendUpdates);
# the organizer sees the answer and its note on the event either way.
_RSVP_NOTIFY_WORDS = {"all": "organizer emailed", "external": "organizer emailed only if external",
                      "none": "organizer not emailed"}
_RSVP_VERBS = {"yes": "Accept", "no": "Decline", "maybe": "Answer maybe to"}
_REPLY_PREFIX_RE = re.compile(r"^(?:(?:re|fwd?|aw)\s*:\s*)+", re.IGNORECASE)
_SAID_CHARS = 140


def _due_text(due: date | datetime, *, show_year: bool) -> str:
    """"Wed Oct 7" or "Wed Oct 7, 11:59 PM"."""
    text = _short_day(due if not isinstance(due, datetime) else due.date(), show_year)
    if isinstance(due, datetime):
        text += f", {_clock_text(due)} {_meridiem(due)}"
    return text


def _due_words(due: date | datetime | None, today: date) -> str:
    """"due today", "due tomorrow" or "due Wed Oct 7" (+ ", 11:59 PM" when timed); "" without a due."""
    if due is None:
        return ""
    day = due.date() if isinstance(due, datetime) else due
    if day == today:
        words = "due today"
    elif day == today + timedelta(days=1):
        words = "due tomorrow"
    else:
        words = "due " + _short_day(day, day.year != today.year)
    if isinstance(due, datetime):
        words += f", {_clock_text(due)} {_meridiem(due)}"
    return words


def _spoken_due(due: date | datetime | None, today: date) -> str:
    """"due today", "due Wednesday October 7 at 11:59 PM"."""
    if due is None:
        return ""
    day = due.date() if isinstance(due, datetime) else due
    if day == today:
        words = "due today"
    elif day == today + timedelta(days=1):
        words = "due tomorrow"
    else:
        words = "due " + _long_day(day, day.year != today.year)
    if isinstance(due, datetime):
        words += f" at {_spoken_clock(due, True)}"
    return words


def _field_when(action: ProposedAction, key: str) -> _When | None:
    """A "<start>/<end>" range field (at=, when=, block=) as a _When; None when absent."""
    first, _, last = action.field(key).partition("/")
    start, end = _iso_value(first), _iso_value(last)
    if isinstance(start, datetime) and isinstance(end, datetime):
        return _When(start=start, end=end)
    if isinstance(start, date) and not isinstance(start, datetime):
        end_day = end if isinstance(end, date) and not isinstance(end, datetime) else start
        return _When(all_day_start=start, all_day_end=end_day)
    return None


def _account_words(action: ProposedAction) -> str:
    return f"{' '.join(re.split(r'[_-]+', action.account))} account" if action.account else ""


def _flat(text: str) -> str:
    return " ".join(text.split())


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def _describe_structured(action: ProposedAction, today: date) -> str:
    """The card detail of a key=value proposal (README: "Other proposals")."""
    kind, field = action.kind, action.field
    due = _due_words(action.due(), today)
    parts: list[str] = []
    if kind in (REPLY, EMAIL):
        parts.append("To: " + ", ".join(action.recipients()))
        if action.cc():
            parts.append("Cc: " + ", ".join(action.cc()))
        parts.append(due)
    elif kind == RSVP:
        at = _field_when(action, "at")
        parts += [f"Answer: {field('answer')}", *(_describe_when(at, today) if at else []),
                  _RSVP_NOTIFY_WORDS.get(field("notify"), ""), due]
    elif kind == MOVE:
        new = _describe_when(action, today)
        if new:
            parts += [f"New time: {new[0]}", *new[1:]]
        parts.append(_NOTIFY_WORDS.get(field("notify"), ""))
        at = _field_when(action, "at")
        if at is not None:
            same_day = (at.start is not None and action.start is not None
                        and at.start.date() == action.start.date() and at.end is not None
                        and _is_short(at.start, at.end))
            was = _display_times(at.start, at.end) if same_day else ", ".join(_describe_when(at, today))
            parts.append(f"was {was}")
    elif kind == CANCEL:
        at = _field_when(action, "at")
        parts += [*(_describe_when(at, today) if at else []), _NOTIFY_WORDS.get(field("notify"), "")]
    elif kind == SHARE:
        parts.append(f"{field('who')} asks for {field('role', 'viewer')} access")
    elif kind == SLACK:
        said = _flat(field("said"))
        parts.append(f'"{_cut(said, _SAID_CHARS)}"' if said else (f"From {field('who')}" if field("who") else ""))
    elif kind == TODO:
        parts.append(due[:1].upper() + due[1:])
        block = _describe_when(action, today)
        if block:
            parts += [f"block {block[0]}", *block[1:]]
        if action.web and action.link:   # a research Todo's page (any public site): its domain shows
            parts.append(f"{WEB_TODO_WORDS} {link_host(action.link)}")
    elif kind == OPEN:
        parts.append(link_host(action.link))
        if action.web:
            parts += list(WEB_SOURCE_WORDS)
    return _SEPARATOR.join(part for part in parts if part)


def _spoken_structured(action: ProposedAction, today: date) -> str:
    """What "Needs your OK" says about a key=value proposal (no final period)."""
    kind, field = action.kind, action.field
    account = _account_words(action)
    named = field("title")
    name = _spoken_title(named) if named else ""
    parts: list[str]
    if kind in (REPLY, EMAIL):
        subject = _REPLY_PREFIX_RE.sub("", action.title).strip() or action.title
        verb = "Reply about" if kind == REPLY else "Email about"
        parts = [f"{verb} {_spoken_title(subject)}", account, _spoken_due(action.due(), today)]
    elif kind == RSVP:
        at = _field_when(action, "at")
        verb = _RSVP_VERBS.get(field("answer"), "Answer")
        parts = [f"{verb} {name or 'an invitation'}", account, *(_spoken_when(at, today) if at else [])]
    elif kind == MOVE:
        new = _spoken_when(action, today)
        target = f" to {new[0]}" if new else ""
        parts = [f"Move {name or 'a meeting'}{target}", *new[1:], account]
    elif kind == CANCEL:
        at = _field_when(action, "at")
        parts = [f"Cancel {name or 'a meeting'}", account, *(_spoken_when(at, today) if at else [])]
    elif kind == SHARE:
        parts = [f"Share request for {name or 'a file'}", account]
    elif kind == SLACK:
        who = field("who")
        parts = [f"Slack reply to {_spoken_title(who)}" if who else "Slack reply"]
    elif kind == TODO:
        block = _spoken_when(action, today)
        parts = [_spoken_title(action.title), _spoken_due(action.due(), today),
                 "with a block " + ", ".join(block) if block else ""]
    else:   # OPEN
        parts = [_spoken_title(action.title)]
    return ", ".join(part for part in parts if part)


def _repeat_words(repeat: str, today: date, *, spoken: bool) -> str:
    """"weekly until Dec 11" / "every two weeks, 6 times"; unknown text is shown as written."""
    if not repeat:
        return ""
    try:
        parsed = _read_repeat(repeat)
    except _LineError:
        return repeat
    if parsed is None:
        return ""
    names = _REPEAT_SPOKEN if spoken else _REPEAT_DISPLAY
    word = names.get(parsed.freq, parsed.freq)
    if parsed.until is not None:
        until = parsed.until
        show_year = until.year != today.year
        if spoken:
            day = f"{_MONTH_LONG[until.month - 1]} {until.day}"
        else:
            day = f"{_MONTH_SHORT[until.month - 1]} {until.day}"
        return f"{word} until {day}, {until.year}" if show_year else f"{word} until {day}"
    if parsed.count is not None:
        return f"{word}, {parsed.count} times"
    return word


# --------------------------------------------------------------------------
# extract_actions
# --------------------------------------------------------------------------

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(?=\S)")
_WHOLE_BOLD_RE = re.compile(r"^(\*\*|__)(?=\S)((?:(?!\1).)+?)(?<=\S)\1$", re.S)
_NON_WORD_RE = re.compile(r"[\W_]+")
_PARAGRAPH_TITLE_LEVEL = 2   # text_prep reads titles on a page without heading blocks like a heading_2
_TITLE_MAX_CHARS = 60
_TITLE_MAX_WORDS = 6


def extract_actions(lines: Sequence[FlatLine],
                    heading_names: Sequence[str] = DEFAULT_HEADINGS, *,
                    link_hosts: Sequence[str] = (),
                    ) -> tuple[list[ProposedAction], list[FlatLine]]:
    """Pull the "Proposed actions" section out of a page.

    The section starts at a heading (any level) whose cleaned title matches
    one of ``heading_names`` (case-insensitive, counts and emoji ignored) and
    runs until the next heading of the same or a higher level; deeper
    subheadings belong to it. Returns (the parsed non-empty lines in page
    order, the page without the section). Duplicate ids keep the first.
    ``link_hosts`` go to parse_action_line ([actions] link_hosts).
    """
    names = (heading_names,) if isinstance(heading_names, str) else tuple(heading_names)
    wanted = {key for key in map(_heading_key, names) if key}
    structural = not any(line.kind == HEADING for line in lines)
    plain_level = _PARAGRAPH_TITLE_LEVEL if structural else max(
        (line.level or 1 for line in lines if line.kind == HEADING), default=1) + 1

    actions: list[ProposedAction] = []
    kept: list[FlatLine] = []
    seen: set[str] = set()
    open_level: int | None = None
    sections = duplicates = 0
    for line in lines:
        title = _title_of(line, structural)
        if open_level is not None:
            if title is None or title[1] > open_level:
                if title is None and _has_text(line):
                    action = parse_action_line(line.text, link_hosts=link_hosts)
                    if action.id in seen:
                        duplicates += 1
                    else:
                        seen.add(action.id)
                        actions.append(action)
                continue
            open_level = None
        open_level = _section_level(line, title, wanted, plain_level)
        if open_level is not None:
            sections += 1
            continue
        kept.append(line)

    if sections:
        logger.info("Proposed actions: %d line(s), %d actionable, %d duplicate(s) dropped; "
                    "%d to decide (%s)", len(actions), sum(1 for a in actions if a.actionable),
                    duplicates, sum(1 for a in actions if a.decidable), _kind_counts(actions))
    return actions, kept


def _kind_counts(actions: Sequence[ProposedAction]) -> str:
    """"calendar 2, reply 1, note 1" in order of first appearance; a kind word the
    briefing made up is counted as "other", so no page text reaches the log."""
    counts: dict[str, int] = {}
    for action in actions:
        if action.kind == UNKNOWN:
            name = "note"
        elif action.kind == CALENDAR or action.kind in STRUCTURED_KINDS:
            name = action.kind
        else:
            name = "other"
        counts[name] = counts.get(name, 0) + 1
    return ", ".join(f"{name} {count}" for name, count in counts.items()) or "none"


def _heading_key(text: str) -> str:
    title = _text_prep().clean_heading(text)[0]
    return _NON_WORD_RE.sub(" ", title.casefold()).strip()


def _has_text(line: FlatLine) -> bool:
    return line.kind != DIVIDER and bool(_text_prep().strip_markdown(line.text))


def _title_of(line: FlatLine, structural: bool) -> tuple[str, int] | None:
    """(text, level) when the line is a heading.

    Heading blocks always are. A top-level paragraph is one when written as a
    markdown heading ("## Work") or, on a page without heading blocks, bold as
    a whole - the same titles text_prep reads as sections. A line with "|" is
    a proposal, never a title.
    """
    if line.kind == HEADING:
        return (line.text, line.level or 1) if _heading_key(line.text) else None
    if line.kind != PARAGRAPH or line.depth != 0 or "|" in line.text:
        return None
    text = line.text.strip()
    markdown = _MD_HEADING_RE.match(text)
    if markdown is not None:
        body = text[markdown.end():]
        return (body, len(markdown.group(1))) if _is_title_like(body) else None
    if structural and _WHOLE_BOLD_RE.match(text) and _is_title_like(text):
        return text, _PARAGRAPH_TITLE_LEVEL
    return None


def _is_title_like(text: str) -> bool:
    prep = _text_prep()
    title = prep.clean_heading(text)[0]
    return (bool(title) and len(title) <= _TITLE_MAX_CHARS and len(title.split()) <= _TITLE_MAX_WORDS
            and not prep.strip_markdown(text).endswith((".", "!", "?")))


def _section_level(line: FlatLine, title: tuple[str, int] | None, wanted: set[str],
                   plain_level: int) -> int | None:
    """The level of a "Proposed actions" heading, or None for any other line.

    A plain top-level paragraph that says just "Proposed actions:" also opens
    the section, ranked below every heading block.
    """
    if title is not None:
        return title[1] if _heading_key(title[0]) in wanted else None
    if line.kind == PARAGRAPH and line.depth == 0 and "|" not in line.text:
        if _heading_key(line.text) in wanted:
            return plain_level
    return None


# --------------------------------------------------------------------------
# Cards (Qt-free: ui.py turns a CardView into a hud.ActionCard)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CardView:
    """What one NEEDS YOUR OK card shows."""

    kind_label: str          # "reply \u00b7 work" (the HUD upper-cases it), "calendar", "note"
    title: str
    detail: str
    body: str = ""           # body preview (unescaped); "" = no body line
    open_text: str = ""      # the tools row's Open button ("" = none)
    copy_text: str = ""      # the tools row's Copy button ("" = none)
    approve_text: str = ""   # the right-hand decision button: approve_label or "Done"; "" when not decidable
    decidable: bool = False
    note: str = ""           # amber note: warnings joined by " \u00b7 " (at most two)
    editable: bool = False   # the tools row has Edit (an RSVP, Move, Cancel, Reply or Email)
    check: bool = False      # a reserved line for Google's own view of the event (RSVP, Move, Cancel)
    deny_text: str = "Deny"  # the left-hand decision button ("Skip" on an RSVP, Move or Cancel)
    body_lines: int = 3      # body preview lines (MAIL_PREVIEW_LINES on a Reply / Email Jarvis sends)


INFO_DETAIL = "Information only"
ALREADY_REPLIED = "The briefing says you already replied"
DONE_TEXT = "Done"
RETRY_TEXT = "Retry"
# The left-hand button of an RSVP, Move or Cancel: "Deny" next to "Decline" read like the same
# thing, but it only drops the card (nothing is sent); the card then reads SKIPPED.
SKIP_TEXT = "Skip"
SKIPPED_TEXT = "Skipped"
# An RSVP's note goes to the organizer with the answer: its preview says so.
RSVP_NOTE_PREFIX = "Note to the organizer: "
NOTE_NOT_SENT = "The note is not sent with the change (Google Calendar has no message) - Copy it"
UNKNOWN_CALENDAR = "Unknown: check the calendar before retrying"
UNKNOWN_MAIL = "Unknown: check Sent mail before retrying"
SENT_TEXT = "Sent"
# A Reply with gmid= but no msgid=: without the Message-ID there is no In-Reply-To header.
NO_MSGID_NOTE = "No Message-ID on this line: the reply joins the thread in Gmail only"
ENCODED_WORD_PROBLEM = ('The subject contains "=?...?=" (an encoded word that mail apps would show '
                        "differently), so Jarvis won't send it - Copy it instead")
EMPTY_MAIL_PROBLEM = "The message is empty - Edit it first"
_SENT_TEXTS = {(RSVP, "yes"): "Accepted", (RSVP, "no"): "Declined", (RSVP, "maybe"): "Answered maybe",
               (MOVE, ""): "Moved", (CANCEL, ""): "Cancelled", (REPLY, ""): SENT_TEXT,
               (EMAIL, ""): SENT_TEXT}
_ALREADY_TEXTS = {(RSVP, "yes"): "Already accepted", (RSVP, "no"): "Already declined",
                  (RSVP, "maybe"): "Already answered maybe", (MOVE, ""): "Already at that time",
                  (CANCEL, ""): "Already cancelled"}
_RAW_SHOWN = 160
_MAX_WARNINGS_SHOWN = 2
_ACCOUNT_KINDS = frozenset({REPLY, EMAIL, RSVP, MOVE, CANCEL, SHARE})
_OPEN_TEXTS = {REPLY: "Open thread", EMAIL: "Open", RSVP: "Open event", MOVE: "Open event",
               CANCEL: "Open event", SHARE: "Open request", SLACK: "Open in Slack", TODO: "Open",
               OPEN: "Open"}
# An RSVP's note goes to the organizer with the answer, so it has no Copy; a Move's or Cancel's
# note is not sent (the Calendar API has no message), so it does.
_COPY_TEXTS = {REPLY: "Copy reply", SLACK: "Copy reply", EMAIL: "Copy email", MOVE: "Copy note",
               CANCEL: "Copy note"}
_COPIED_TEXTS = {REPLY: "Copied the reply", SLACK: "Copied the reply", EMAIL: "Copied the email",
                 RSVP: "Copied the note", MOVE: "Copied the note", CANCEL: "Copied the note"}


WEB_OPEN_TEXT = "Open page"
WEB_SOURCE_WORDS = ("web source", "opens in your browser")
WEB_TODO_WORDS = "opens"


def is_web_open(action: ProposedAction) -> bool:
    """A web research source card (an Open card whose link follows the web rule)."""
    return action.web and action.kind == OPEN


LABEL_HOST_CHARS = 17   # what a card's one-line kind label shows of a web host at the narrowest (900 px)
# Second levels under a two-letter country code ("example.co.uk") and shared hosts whose names are
# anyone's ("someone.github.io"): the site is one label more.
_SECOND_LEVELS = frozenset("co com net org gov edu ac or ne go gob mil nic ltd plc sch".split())
_SHARED_HOSTS = frozenset({
    "github.io", "gitlab.io", "blogspot.com", "wordpress.com", "herokuapp.com", "netlify.app", "vercel.app",
    "pages.dev", "workers.dev", "web.app", "firebaseapp.com", "appspot.com", "azurewebsites.net",
    "cloudfront.net", "amazonaws.com", "glitch.me", "onrender.com", "fly.dev", "wixsite.com", "weebly.com",
    "webflow.io", "notion.site", "substack.com", "medium.com", "tumblr.com", "translate.goog", "ngrok.io",
    "ngrok-free.app", "trycloudflare.com", "github.dev", "repl.co", "replit.app", "carrd.co", "square.site"})


def label_host(host: str) -> str:
    """The host as a web card's kind label shows it: always its right end, which names the site
    ("example.net", "someone.github.io", "example.co.uk"), with more labels to the left while it
    stays within LABEL_HOST_CHARS characters, and "\u2026" where something was left out
    ("\u2026example.net" for "accounts.google.com.check.example.net": the label never shows a
    leading part that could pass for another site; the card's detail shows the whole host)."""
    labels = (host or "").rstrip(".").split(".")
    keep = len(labels)
    if len(labels) > 2:
        site = 3 if (len(labels[-1]) == 2 and labels[-2].casefold() in _SECOND_LEVELS) \
            or ".".join(labels[-2:]).casefold() in _SHARED_HOSTS else 2
        keep = site
        while keep < len(labels) and len(".".join(labels[-(keep + 1):])) <= LABEL_HOST_CHARS:
            keep += 1
    shown = ".".join(labels[-keep:])
    if len(shown) > LABEL_HOST_CHARS:
        return "\u2026" + shown[-(LABEL_HOST_CHARS - 1):]
    return "\u2026" + shown if keep < len(labels) else shown


def kind_label(action: ProposedAction) -> str:
    """"reply \u00b7 work", "todo", "calendar"; "note" for a line without a kind; "web \u00b7
    example.org" for a web research source (label_host: the end of the host, which names the site;
    the whole host is in the detail)."""
    if action.kind == UNKNOWN:
        return "note"
    if action.kind == CALENDAR and action.mode == MODE_PLAN:
        return "block"   # a day plan's time block ("Plan - block")
    if is_web_open(action):
        return f"web{_SEPARATOR}{label_host(link_host(action.link))}"
    if action.structured and action.account and action.kind in _ACCOUNT_KINDS:
        return f"{action.kind}{_SEPARATOR}{action.account}"
    return action.kind


def open_text(action: ProposedAction) -> str:
    """The tools row's Open label ("" when the card has no usable link)."""
    if action.error or not action.structured or not action.link:
        return ""
    if action.web:   # a source's card, or a research Todo carrying a source's link
        return WEB_OPEN_TEXT
    if action.kind == TODO and link_host(action.link).endswith(".instructure.com"):
        return "Open in Canvas"
    return _OPEN_TEXTS.get(action.kind, "")


def copy_text(action: ProposedAction) -> str:
    """The tools row's Copy label ("" when there is nothing to copy)."""
    if not action.decidable or not action.body:
        return ""
    return _COPY_TEXTS.get(action.kind, "")


def copied_text(action: ProposedAction) -> str:
    """The activity line after a Copy ("Copied the reply")."""
    return _COPIED_TEXTS.get(action.kind, "Copied the text")


def card_view(action: ProposedAction, today: date) -> CardView:
    """Kind label, texts and buttons of ``action``'s card (README: "Other proposals")."""
    label = kind_label(action)
    if action.error and action.source != SOURCE_BRIEFING:
        # An Ask card Jarvis refused (or could not read): its headline when it had one, and the
        # reason as written ("Jarvis doesn't know this event - ask again").
        return CardView(label, action.title or _cut(action.raw, _RAW_SHOWN), action.error)
    if action.error:
        title = _cut(action.raw, _RAW_SHOWN) if action.structured else action.raw
        return CardView(label, title, f"Can't read this line: {action.error}")
    title = action.title or action.raw
    if action.kind == CALENDAR:
        parts = [action.describe(today)]
        if action.where and "://" not in action.where:
            parts.append(action.where)
        return CardView(label, title, _SEPARATOR.join(part for part in parts if part),
                        approve_text=approve_label(action), decidable=True)
    if not action.structured:
        return CardView(label, title, INFO_DETAIL)
    if not action.decidable:   # a Reply the briefing says was sent already
        return CardView(label, title, ALREADY_REPLIED, open_text=open_text(action))
    detail = action.describe(today)
    notes = list(action.warnings[:_MAX_WARNINGS_SHOWN])
    if action.web and action.link and idn_host(action.link):
        # In the detail, above Open page (the note is drawn below Deny / Done).
        detail = _SEPARATOR.join(part for part in (detail, IDN_WARNING) if part)
        notes = [note for note in action.warnings if note != IDN_WARNING][:_MAX_WARNINGS_SHOWN]
    if action.kind in (MOVE, CANCEL) and action.body and action.changes_event:
        notes.append(NOTE_NOT_SENT)
    if action.sends_mail:
        notes += [mail_problem(action), NO_MSGID_NOTE if action.kind == REPLY and not action.field("msgid") else "",
                  links_note(action.body)]
    body = action.body
    if action.kind == RSVP and body and action.changes_event:
        body = RSVP_NOTE_PREFIX + body
    return CardView(label, title, detail, body=body, open_text=open_text(action),
                    copy_text=copy_text(action), approve_text=approve_label(action) or DONE_TEXT,
                    decidable=True, note=_SEPARATOR.join(note for note in notes if note),
                    editable=action.editable, check=action.changes_event,
                    deny_text=SKIP_TEXT if action.changes_event else "Deny",
                    body_lines=MAIL_PREVIEW_LINES if action.sends_mail else 3)


def result_text(action: ProposedAction, status: str) -> str:
    """A card's result text when it differs from the HUD's default ("" = the default)."""
    if action.kind == TODO and status == STATUS_CREATED:
        return "Block added"
    if action.decidable and not action.actionable and status == STATUS_DENIED:
        return "Dismissed"
    if action.changes_event and status == STATUS_DENIED:
        return SKIPPED_TEXT
    if status == STATUS_SENT:
        return sent_text(action)
    if status == STATUS_UNKNOWN and action.countdown:
        return UNKNOWN_MAIL if action.kind in MAIL_KINDS else UNKNOWN_CALENDAR
    return ""


def mail_problem(action: ProposedAction) -> str:
    """Why Jarvis would not send this Reply / Email as it stands ("" when it would, or when it is
    not one): a subject with an encoded word ("=?utf-8?b?...?=") would reach the recipients as
    other text than the card shows; an empty message."""
    if not action.sends_mail:
        return ""
    if _has_encoded_word(action.title):
        return ENCODED_WORD_PROBLEM
    if not action.body.strip():
        return EMPTY_MAIL_PROBLEM
    return ""


def body_needs_review(action: ProposedAction) -> bool:
    """A Reply / Email whose body has more lines than its card shows (MAIL_PREVIEW_LINES): Send
    opens the Edit dialog to read all of it first."""
    return action.sends_mail and len(action.body.split("\n")) > MAIL_PREVIEW_LINES


def _has_encoded_word(text: str) -> bool:
    """True when ``text`` has "=?" followed later by "?=" (what an RFC 2047 encoded word needs)."""
    start = text.find("=?")
    return start >= 0 and text.find("?=", start + 2) >= 0


def sent_text(action: ProposedAction, *, already: bool = False) -> str:
    """"Accepted", "Moved", "Cancelled", "Sent" ("Already accepted" ... with ``already``); "" for
    other kinds."""
    key = (action.kind, action.field("answer") if action.kind == RSVP else "")
    return (_ALREADY_TEXTS if already else _SENT_TEXTS).get(key, "")


def stated_when(action: ProposedAction) -> Any | None:
    """The event's time as the line states it (an RSVP's, Move's or Cancel's at=), with start /
    end or all_day_start / all_day_end like a ProposedAction; None when the line gives none."""
    return _field_when(action, "at")


def when_text(item: Any, today: date) -> str:
    """"Thu Oct 8 \u00b7 12:00-1:00 PM" for anything with start / end / all_day_start / all_day_end
    (an aware start is shown as the wall time it carries)."""
    start, end = getattr(item, "start", None), getattr(item, "end", None)
    when = _When(start=start.replace(tzinfo=None) if start is not None else None,
                 end=end.replace(tzinfo=None) if end is not None else None,
                 all_day_start=getattr(item, "all_day_start", None),
                 all_day_end=getattr(item, "all_day_end", None))
    return _SEPARATOR.join(_describe_when(when, today))


class EditInvalid(ValueError):
    """Why an edit cannot be used (shown in the Edit dialog)."""


def edit_action(action: ProposedAction, *, answer: str | None = None, notify: str | None = None,
                start: datetime | None = None, end: datetime | None = None,
                body: str | None = None) -> ProposedAction:
    """``action`` with the Edit dialog's changes, checked like a line (same id; README: "Edit").

    RSVP: ``answer`` (yes / no / maybe), ``notify``, ``body`` (the note sent with the answer).
    Move: ``start`` / ``end`` (5 minutes to 12 hours), ``notify``, ``body`` (Copy only).
    Cancel: ``notify``, ``body`` (Copy only). Raises EditInvalid with a short reason. The id
    never changes: the card and the saved decision stay the same proposal.
    """
    if not action.changes_event:
        raise EditInvalid("This card cannot be edited")
    pairs = dict(action.fields)
    changes: dict[str, Any] = {}
    try:
        if answer is not None:
            if action.kind != RSVP:
                raise _LineError("only an invitation has an answer")
            pairs["answer"] = _choice("answer", _edit_text(answer), _ANSWERS, "use yes, no or maybe")
        if notify is not None:
            pairs["notify"] = _choice("notify", _edit_text(notify), _NOTIFY, "use all, external or none")
        if start is not None or end is not None:
            if action.kind != MOVE or start is None or end is None:
                raise _LineError("only a move has a new time (start and end)")
            if not isinstance(start, datetime) or not isinstance(end, datetime):
                raise _LineError("the new time needs a start and an end")
            start = start.replace(tzinfo=None, second=0, microsecond=0)
            end = end.replace(tzinfo=None, second=0, microsecond=0)
            if not _MIN_RANGE <= end - start <= _MAX_RANGE:
                raise _LineError("the new time must end 5 minutes to 12 hours after it starts")
            pairs["when"] = f"{_iso(start)}/{_iso(end)}"
            changes.update(start=start, end=end)
        if body is not None:
            if not isinstance(body, str):
                raise _LineError("the note must be text")
            text = _BODY_CONTROL_RE.sub("", body.replace("\r\n", "\n").replace("\r", "\n")).strip()
            cap = _BODY_CAPS.get(action.kind, _LONG_BODY_CAP)
            if len(text) > cap:
                raise _LineError(f"the note is too long ({len(text)} characters, at most {cap})")
            changes["body"] = text
    except _LineError as exc:
        raise EditInvalid(str(exc)) from None
    known = [name for name, _ in action.fields]
    order = known + [name for name in pairs if name not in known]
    return replace(action, fields=tuple((name, pairs[name]) for name in order), **changes)


def _edit_text(value: Any) -> str:
    if not isinstance(value, str):
        raise _LineError("the value must be text")
    return value.strip()


def edit_mail(action: ProposedAction, *, to: Sequence[str] | str | None = None,
              cc: Sequence[str] | str | None = None, subject: str | None = None, body: str | None = None,
              confirmed: Sequence[str] | None = None) -> ProposedAction:
    """``action`` (a Reply or Email) with the Edit dialog's changes, checked like a line (same id).

    ``to`` / ``cc``: the chips, each an address or "Name <address>" (the name is dropped); an
    address in both stays in To only; at least one in To and at most MAX_RECIPIENTS together.
    ``subject``: an Email's new subject (one line, 250 characters at most, no encoded word); a
    Reply keeps the thread's subject. ``body``: the message exactly as it will be sent (line
    breaks kept; control and invisible format characters removed; not empty). ``confirmed``: the
    NEW RECIPIENT addresses ticked ("Send to <address>"); only the ones still among the
    recipients are kept, and without ``confirmed`` the earlier ticks stay for those that remain.
    ``None`` = unchanged. Raises EditInvalid with a short reason. The id never changes.
    """
    if not action.sends_mail:
        raise EditInvalid("This card cannot be edited")
    pairs = dict(action.fields)
    changes: dict[str, Any] = {}
    try:
        new_to = action.recipients() if to is None else _edit_addresses("To", to)
        new_cc = action.cc() if cc is None else _edit_addresses("Cc", cc)
        in_to = {address.casefold() for address in new_to}
        new_cc = tuple(address for address in new_cc if address.casefold() not in in_to)
        if not new_to:
            raise _LineError("To needs at least one address")
        count = len(new_to) + len(new_cc)
        if count > MAX_RECIPIENTS:
            raise _LineError(f"To and Cc name {count} addresses (at most {MAX_RECIPIENTS})")
        pairs["to"] = ", ".join(new_to)
        if new_cc:
            pairs["cc"] = ", ".join(new_cc)
        else:
            pairs.pop("cc", None)
        if subject is not None:
            # A reply's subject may be the briefing's whole subject= with "Re: " on top.
            text = _edit_subject(subject, _SUBJECT_CAP + len("Re: ") if action.kind == REPLY else _SUBJECT_CAP)
            if action.kind == REPLY:
                if text.casefold() != action.title.casefold():
                    raise _LineError("a reply keeps the subject of its thread")
            else:
                pairs["subject"] = text
                changes["title"] = text
        if body is not None:
            changes["body"] = _edit_body(body, _BODY_CAPS.get(action.kind, _LONG_BODY_CAP))
        recipients = {address.casefold() for address in new_to + new_cc}
        if confirmed is None:
            ticked = set(action.confirmed)
        else:
            if isinstance(confirmed, str):
                raise _LineError("the confirmed recipients must be a list of addresses")
            ticked = {email_address(item).casefold() for item in confirmed} - {""}
        changes["confirmed"] = frozenset(ticked & recipients)
    except _LineError as exc:
        raise EditInvalid(str(exc)) from None
    order = [key.name for key in _SCHEMAS[action.kind] if key.name in pairs]
    order += [name for name in pairs if name not in order]
    return replace(action, fields=tuple((name, pairs[name]) for name in order), **changes)


def _edit_addresses(name: str, items: Sequence[str] | str) -> tuple[str, ...]:
    """The chips of To or Cc as normalized addresses (repeats dropped); a text is split at , and ;."""
    if isinstance(items, str):
        items = [part for part in _ADDRESS_SPLIT_RE.split(items)]
    addresses: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            raise _LineError(f"{name}: an address must be text")
        if not item.strip():
            continue
        address = email_address(item)
        if not address:
            raise _LineError(f"{name}: {_quoted(item)} is not an email address")
        if address.casefold() not in seen:
            seen.add(address.casefold())
            addresses.append(address)
    return tuple(addresses)


def _edit_subject(value: Any, cap: int = _SUBJECT_CAP) -> str:
    if not isinstance(value, str):
        raise _LineError("the subject must be text")
    text = _drop_format_characters(value)
    if _LINE_BREAK_RE.search(text):
        raise _LineError("the subject has a line break")
    if _CONTROL_RE.search(text):
        raise _LineError("the subject has a control character")
    text = " ".join(text.split())
    if not text:
        raise _LineError("give a subject")
    if len(text) > cap:
        raise _LineError(f"the subject is too long ({len(text)} characters, at most {cap})")
    if _has_encoded_word(text):
        raise _LineError('the subject can\'t contain "=?...?=" (an encoded word)')
    return text


def _edit_body(value: Any, cap: int) -> str:
    if not isinstance(value, str):
        raise _LineError("the message must be text")
    text = _drop_format_characters(value.replace("\r\n", "\n").replace("\r", "\n"))
    text = _BODY_CONTROL_RE.sub("", text).strip()
    if not text:
        raise _LineError("the message is empty")
    if len(text) > cap:
        raise _LineError(f"the message is too long ({len(text)} characters, at most {cap})")
    return text


_EDIT_FIELD_CAP = 40   # one field of the Edit dialog's new time ("2026-10-08", "2:00 PM")


def parse_time_range(date_text: str, start_text: str, end_text: str) -> tuple[datetime, datetime]:
    """The Edit dialog's new time for a Move ("2026-10-08", "14:00", "15:00"), read like a when=
    value: naive local wall times, 5 minutes to 12 hours (an end before the start is the next
    day). Raises EditInvalid with a short reason."""
    parts = []
    for name, value in (("date", date_text), ("start", start_text), ("end", end_text)):
        if not isinstance(value, str) or not value.strip():
            raise EditInvalid(f"give the new {name}")
        value = " ".join(value.split())
        if len(value) > _EDIT_FIELD_CAP or "|" in value or _CONTROL_RE.search(value):
            raise EditInvalid(f"the new {name} can't be read")
        parts.append(value)
    if not _ISO_DATE_RE.fullmatch(parts[0]):
        raise EditInvalid("give the date as YYYY-MM-DD")
    if "-" in parts[1] or "-" in parts[2]:
        raise EditInvalid("give the start and the end as times, like 14:00 or 2:00 PM")
    try:
        return _read_range("when", f"{parts[0]} {parts[1]}-{parts[2]}")
    except _LineError as exc:
        raise EditInvalid(str(exc).removeprefix("when=: ")) from None


# --------------------------------------------------------------------------
# ActionStore
# --------------------------------------------------------------------------

def _local_now() -> datetime:
    return datetime.now().astimezone()


def _aware(moment: datetime) -> datetime:
    return moment if moment.utcoffset() is not None else moment.astimezone()


# acct= is page text, so only these aliases are logged by name; any other is logged as "other"
# (it could be a name or a number). Phase 2 checks aliases against [accounts].
_LOGGED_ALIASES = frozenset({"personal", "work"})


def _logged_alias(account: str) -> str:
    """``account`` as written to the log: "-" for none, "other" for an alias outside _LOGGED_ALIASES."""
    if not account:
        return "-"
    return account if account in _LOGGED_ALIASES else "other"


class ActionStore:
    """Persisted decisions: %LOCALAPPDATA%\\briefing-reader\\actions.json
    {id: {status, at, link, message, kind, account}} (``kind`` and ``account`` only when known).

    "failed" is remembered (for the card's message) but is not a decision: the
    proposal stays pending and can be approved again; so is "unknown". An
    entry still "running" when the file is read (Jarvis stopped mid-call)
    becomes "unknown", and the next ``prune()`` saves that. A corrupt or
    unreadable file is logged and treated as empty; a failed write is logged
    and the decision is kept in memory for this run. Writes are atomic
    (temporary file + os.replace), so a crash never leaves half a file.
    """

    def __init__(self, path: Path, clock: Callable[[], datetime] = _local_now) -> None:
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, str]] = _load_entries(self.path)
        self._dirty = _mark_interrupted(self._entries)   # changed since read: prune() saves it

    def get(self, action_id: str) -> dict | None:
        with self._lock:
            entry = self._entries.get(action_id)
            return dict(entry) if entry is not None else None

    def set(self, action_id: str, status: str, *, link: str = "", message: str = "",
            kind: str = "", account: str = "") -> bool:
        """Remember ``status`` for ``action_id``; True when it was written to the file (False:
        logged, and kept in memory for this run only)."""
        if status not in STATUSES:
            raise ValueError(f"unknown action status {status!r}")
        entry = {"status": status, "at": self._clock().isoformat(timespec="seconds"),
                 "link": link or "", "message": message or ""}
        if kind:
            entry["kind"] = kind
        if account:
            entry["account"] = account
        with self._lock:
            self._entries[action_id] = entry
            saved = _write_entries(self.path, self._entries)
            if saved:
                self._dirty = False
        logger.info("Action %s (%s, %s): %s", action_id, kind or "-", _logged_alias(account), status)
        return saved

    def is_decided(self, action_id: str) -> bool:
        entry = self.get(action_id)
        return entry is not None and entry["status"] in DECIDED_STATUSES

    def prune(self, max_age_days: int = 60) -> None:
        """Forget decisions older than ``max_age_days`` (and ones without a readable time).

        Also saves what changed when the file was read (interrupted calls marked unknown).
        """
        cutoff = _aware(self._clock()) - timedelta(days=max_age_days)
        with self._lock:
            old = [key for key, entry in self._entries.items() if not _newer_than(entry, cutoff)]
            for key in old:
                del self._entries[key]
            if (old or self._dirty) and _write_entries(self.path, self._entries):
                self._dirty = False
        if old:
            logger.info("Forgot %d action decision(s) older than %d days", len(old), max_age_days)


def _newer_than(entry: dict[str, str], cutoff: datetime) -> bool:
    try:
        at = datetime.fromisoformat(entry.get("at", ""))
    except (TypeError, ValueError):
        return False
    return _aware(at) >= cutoff


def _load_entries(path: Path) -> dict[str, dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        logger.warning("Could not read %s (%s); starting with no saved decisions",
                       path, type(exc).__name__)
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("%s is not valid JSON; starting with no saved decisions", path)
        return {}
    if not isinstance(data, dict):
        logger.warning("%s is not a table of decisions; starting with no saved decisions", path)
        return {}
    entries: dict[str, dict[str, str]] = {}
    for key, value in data.items():
        entry = _clean_entry(value)
        if entry is not None:
            entries[str(key)] = entry
    if len(entries) != len(data):
        logger.warning("Ignored %d malformed decision(s) in %s", len(data) - len(entries), path)
    return entries


def _clean_entry(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict) or value.get("status") not in STATUSES:
        return None
    entry = {name: value[name] if isinstance(value.get(name), str) else ""
             for name in ("status", "at", "link", "message")}
    for name in ("kind", "account"):   # optional; files from before they existed have neither
        if isinstance(value.get(name), str) and value[name]:
            entry[name] = value[name]
    return entry


def _mark_interrupted(entries: dict[str, dict[str, str]]) -> bool:
    """"running" entries (Jarvis stopped mid-call) become "unknown"; True when any did."""
    count = 0
    for entry in entries.values():
        if entry["status"] == STATUS_RUNNING:
            entry["status"] = STATUS_UNKNOWN
            entry["message"] = INTERRUPTED_MESSAGE
            count += 1
    if count:
        logger.warning("%d action(s) were interrupted; marked unknown", count)
    return bool(count)


def _write_entries(path: Path, entries: dict[str, dict[str, str]]) -> bool:
    """Atomically replace ``path`` with ``entries`` as JSON; False (logged) on failure."""
    data = json.dumps(entries, indent=2, sort_keys=True) + "\n"
    part: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, part = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(part, path)
        part = None
        return True
    except OSError as exc:
        logger.warning("Could not save %s (%s); the decision is kept for this run only",
                       path, exc.strerror or type(exc).__name__)
        return False
    finally:
        if part is not None:
            try:
                os.unlink(part)
            except OSError:
                pass
