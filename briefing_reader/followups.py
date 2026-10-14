"""Follow-ups (the productivity bundle): which mail threads wait on the owner's answer and which wait on
someone else's, from Gmail headers only (spec 3.4, 5.3-5.6).

    facts_from_metadata(data, alias, now=, days=)   ThreadFacts of one users.threads.get answer
                                     (format=metadata): every message older than ``days`` is dropped
                                     here, before anything is kept or classified (R9); a subject that
                                     looks like account security is never kept (sensitive)
    deciding_message(messages, own, known)   the message that decides the thread (from the newest
                                     back, the first that is neither automated mail nor an automated
                                     message of the owner's: an RSVP sent from an invitation, an
                                     auto-reply, R17) and the ids of the automated ones newer than it
    classify(facts, own=, own_domains=, now=, settings=, known=, hidden=)   FollowupItem (WAITING: they
                                     wrote last, to the owner directly, still in the inbox; OWED: the
                                     owner wrote last to someone else directly and nobody answered) or
                                     the reason word it is not listed
    participants_index(facts, own)   mailbox -> the newest 3 threads with that person (prep's shortcut;
                                     never a permission: prep.guest_allowed comes first)
    MailScanner                      the scan of one account (SENT first, then INBOX; the cache by
                                     historyId and account identity; partial on a rate limit or any
                                     other read error; a refusal stops the account until its next
                                     sign-in, R19) and the re-fetch of one thread (R47)
    HiddenStore                      followups.json: hide keys and times only (5.6)
    make_view(...)                   FollowupView: the rows by kind, oldest first

Nothing here reads a body or a snippet: the reader's requests carry field masks without them
(gmail.GmailReader.scan_ids / thread_metadata). Nothing is written but followups.json (hashes and
times). The logs carry aliases (google_auth.logged_alias), counts and reason words only; every repr
names kinds and counts. Qt-free; the reader is injected.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
from pathlib import Path
from typing import Any, NamedTuple

from .actions import email_address
from .ask.mail import MailPerson, auto_submitted, clean_line, is_security_sender, list_mail
from .ask.modes import ThreadRef
from .google_auth import logged_alias
from .recipients import mailbox_key, same_mailbox

logger = logging.getLogger(__name__)

WAITING, OWED = "waiting", "owed"
KINDS = (WAITING, OWED)
FOLLOWUPS_FILE = "followups.json"
# Senders whose mail is never a person waiting on an answer (the local part, before any "+tag").
NOREPLY_LOCALS = frozenset({"noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "do_not_reply",
                            "notification", "notifications", "notify", "mailer-daemon", "postmaster", "bounce",
                            "bounces", "newsletter", "digest", "calendar-notification"})
CALENDAR_SUBJECT_RE = re.compile(r"^(?:invitation|updated invitation(?: with note)?|accepted|declined|"
                                 r"tentatively accepted|cancel+ed event(?: with note)?)\s*:", re.IGNORECASE)
CALENDAR_SENDER = "calendar-notification@google.com"
# A subject that looks like account security or money: the thread is counted, never listed, and its
# subject is not kept (narrower than ask.mail's search filter on purpose: "Code review for lab 3" is
# listed).
_SUBJECT_SKIP_RE = re.compile(r"\b(?:verification code|security code|one[- ]time|password|sign[- ]?in attempt|"
                              r"new sign[- ]?in|login attempt|2fa|otp|reset your|recovery|security alert|"
                              r"account number|statement)", re.IGNORECASE)
GROUP_LOCALS = frozenset({"team", "all", "staff", "everyone", "group", "list", "class", "students"})   # prep's
AUTOMATED_LABELS = frozenset({"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"})
UPDATES_LABEL = "CATEGORY_UPDATES"
DROPPED_LABELS = frozenset({"DRAFT", "SPAM", "TRASH", "CHAT"})
MAX_MESSAGES = 20            # the newest ones of a thread are classified
HEADER_CAP = 20_000          # characters of one address header that are parsed
MAX_ADDRESSES = 50           # addresses kept per header
MAX_DIRECT = 5               # at most this many To addresses for "to you directly" / "to someone directly"
CACHE_MAX = 600              # thread facts kept in memory, all accounts together
SHOWN_MAX = 20               # rows per kind the list holds ("+N more" expands up to this)
INDEX_PER_PERSON = 3
GET_PAUSE_S = 0.1            # between two header reads (the reader's lock is free for an Ask meanwhile)
HIDE_DAYS = 3
PRUNE_DAYS = 30
MAX_HIDDEN = 1000
_KEY_LEN = 32
_GMAIL_ID_RE = re.compile(r"[0-9A-Za-z_-]{6,64}")
_HISTORY_RE = re.compile(r"\d{1,30}")

# Why a thread is not listed (ScanReport.skipped; LIVE and the log say these words, never more).
TOO_OLD, TOO_NEW, AUTOMATED, SENSITIVE, ARCHIVED, CC_ONLY, NOT_DIRECT, GROUP, TO_SELF, HIDDEN = (
    "too_old", "too_new", "automated", "sensitive", "archived", "cc_only", "not_direct", "group", "to_self",
    "hidden")
# ScanReport.problem: why an account was not scanned, or why its scan stopped early.
PROBLEM_UNCONFIRMED, PROBLEM_IDENTITY, PROBLEM_UNREADABLE, PROBLEM_REFUSED, PROBLEM_STOPPED = (
    "unconfirmed", "identity", "unreadable", "refused", "stopped")
PROBLEM_RATE_LIMIT, PROBLEM_ERROR, PROBLEM_CANCELLED = "rate_limit", "error", "cancelled"
NOT_SCANNED = frozenset({PROBLEM_UNCONFIRMED, PROBLEM_IDENTITY, PROBLEM_UNREADABLE, PROBLEM_REFUSED,
                         PROBLEM_STOPPED})


# --------------------------------------------------------------------------
# Data (memory only; every repr is kinds and counts)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MetaMessage:
    """One message's headers as the scan keeps them (no body, no snippet)."""

    gmail_id: str
    at: datetime                                   # aware (internalDate)
    labels: frozenset[str] = frozenset()
    sender: MailPerson | None = None
    to: tuple[MailPerson, ...] = ()
    cc: tuple[MailPerson, ...] = ()
    reply_to: tuple[MailPerson, ...] = ()
    auto: bool = False                             # Auto-Submitted present and not "no"
    listy: bool = False                            # List-Id / List-Unsubscribe / Precedence bulk, list, junk
    calendar: bool = False                         # an invitation or RSVP subject, or Google Calendar's sender
    subject: str = ""                              # "" when it looks like account security (never kept)

    def __repr__(self) -> str:
        return f"MetaMessage({len(self.labels)} label(s), {len(self.to)} to, {len(self.cc)} cc)"

    def people(self) -> tuple[MailPerson, ...]:
        return ((self.sender,) if self.sender else ()) + self.reply_to + self.to + self.cc


@dataclass(frozen=True)
class ThreadFacts:
    alias: str
    thread_id: str
    history_id: str
    subject: str                                   # the newest subject ("" when sensitive)
    messages: tuple[MetaMessage, ...]              # oldest first, only the last ``days`` days
    sensitive: bool = False                        # a subject looked like account security (never listed)

    def __repr__(self) -> str:
        return f"ThreadFacts({self.alias}, {len(self.messages)} message(s))"


@dataclass(frozen=True)
class FollowupItem:
    row_id: str              # sha1("<alias>\x1f<thread_id>")[:16]: the UI's opaque row id
    key: str                 # the hide key (5.6): changes with a new deciding message
    kind: str                # WAITING | OWED
    alias: str
    thread_id: str = field(repr=False)
    last_gmail_id: str = field(repr=False)
    auto_ids: frozenset[str] = field(default=frozenset(), repr=False)
    subject: str = field(default="", repr=False)
    who: str = field(default="", repr=False)
    who_address: str = field(default="", repr=False)
    who_more: int = 0
    since: datetime | None = field(default=None, repr=False)
    reason: str = field(default="", repr=False)    # the tooltip's words ("They wrote last, to you directly, ...")

    def __repr__(self) -> str:
        return f"FollowupItem({self.kind}, {self.hours} h)"

    @property
    def hours(self) -> int:
        if self.since is None:
            return 0
        return max(0, int((datetime.now(timezone.utc) - self.since).total_seconds() // 3600))


@dataclass(frozen=True)
class ScanReport:
    alias: str
    listed: int = 0
    fetched: int = 0
    cached: int = 0
    waiting: int = 0
    owed: int = 0
    skipped: Mapping[str, int] = field(default_factory=dict, hash=False)
    partial: bool = False
    problem: str = ""
    duration_ms: int = 0
    hidden: int = 0

    def __repr__(self) -> str:
        return (f"ScanReport({self.alias}, listed {self.listed}, fetched {self.fetched}, cached {self.cached}, "
                f"{self.waiting} waiting, {self.owed} owed, {'partial' if self.partial else 'whole'}"
                f"{', ' + self.problem if self.problem else ''})")

    @property
    def scanned(self) -> bool:
        return self.problem not in NOT_SCANNED


class ScanResult(NamedTuple):
    items: list[FollowupItem]
    hidden: list[FollowupItem]
    report: ScanReport
    participants: dict[str, tuple[ThreadRef, ...]]


@dataclass(frozen=True)
class FollowupView:
    waiting: tuple[FollowupItem, ...] = ()
    owed: tuple[FollowupItem, ...] = ()
    hidden: tuple[FollowupItem, ...] = ()
    reports: tuple[ScanReport, ...] = ()
    notes: tuple[tuple[str, str], ...] = ()        # (alias, problem word) for the fix lines
    scanned_at: datetime | None = None
    scanning: bool = False
    off: bool = False                              # turned off on this PC (assistant.json followups_off)

    def __repr__(self) -> str:
        return (f"FollowupView({len(self.waiting)} waiting, {len(self.owed)} owed, {len(self.hidden)} hidden, "
                f"{len(self.notes)} note(s))")

    @property
    def partial(self) -> bool:
        return any(report.partial for report in self.reports)

    @property
    def rows(self) -> tuple[FollowupItem, ...]:
        return self.waiting + self.owed

    def item(self, row_id: str) -> FollowupItem | None:
        return next((item for item in self.rows + self.hidden if item.row_id == row_id), None)


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

def row_id_of(alias: str, thread_id: str) -> str:
    return hashlib.sha1(f"{alias}\x1f{thread_id}".encode("utf-8")).hexdigest()[:16]


def hide_key(alias: str, thread_id: str, gmail_id: str) -> str:
    """followups.json's key of one row: a hash, never the ids in clear; a new deciding message gives
    another key, so a hidden thread comes back when something new arrives."""
    raw = "\x1f".join((str(alias or ""), str(thread_id or ""), str(gmail_id or "")))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_KEY_LEN]


def domains_of(addresses: Iterable[str]) -> frozenset[str]:
    """The domains of the owner's own addresses (a work domain that is a security provider's stays
    listable for follow-ups only; prep never gets them)."""
    return frozenset(address.rpartition("@")[2].casefold() for address in addresses if "@" in address)


# --------------------------------------------------------------------------
# Reading the headers
# --------------------------------------------------------------------------

def _headers(payload: Any) -> dict[str, str]:
    found: dict[str, str] = {}
    for entry in (payload.get("headers") if isinstance(payload, dict) else None) or ():
        if not isinstance(entry, dict):
            continue
        name, value = entry.get("name"), entry.get("value")
        if isinstance(name, str) and isinstance(value, str) and name.casefold() not in found:
            found[name.casefold()] = value
    return found


def _people(value: str, cap: int = MAX_ADDRESSES) -> tuple[MailPerson, ...]:
    if not value:
        return ()
    try:
        pairs = getaddresses([value[:HEADER_CAP]])
    except (TypeError, ValueError, IndexError):
        return ()
    found: list[MailPerson] = []
    seen: set[str] = set()
    for name, address in pairs:
        plain = email_address(address)
        if plain and plain.casefold() not in seen:
            seen.add(plain.casefold())
            found.append(MailPerson(plain, clean_line(name, 60)))
            if len(found) >= cap:
                break
    return tuple(found)


def _at(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def sensitive_subject(subject: str) -> bool:
    return bool(_SUBJECT_SKIP_RE.search(str(subject or "")))


def facts_from_metadata(data: Any, alias: str, *, now: datetime, days: int) -> ThreadFacts | None:
    """One users.threads.get answer (format=metadata) as ThreadFacts; None when it is not one. Every
    message older than ``now - days`` (or without a date) is dropped here, before anything is kept or
    classified (R9): older senders, recipients and subjects never reach the cache or the index."""
    if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not _GMAIL_ID_RE.fullmatch(data["id"]):
        return None
    history = data.get("historyId")
    history = str(history) if isinstance(history, (str, int)) and not isinstance(history, bool) else ""
    cutoff = _aware(now) - timedelta(days=max(1, int(days)))
    messages: list[MetaMessage] = []
    sensitive = False
    for item in data.get("messages") or ():
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not _GMAIL_ID_RE.fullmatch(item["id"]):
            continue
        at = _at(item.get("internalDate"))
        if at is None or at < cutoff:
            continue
        labels = item.get("labelIds") if isinstance(item.get("labelIds"), list) else []
        headers = _headers(item.get("payload"))
        subject = clean_line(headers.get("subject", ""), 200)
        senders = _people(headers.get("from", ""), 1)
        sender = senders[0] if senders else None
        calendar = bool(CALENDAR_SUBJECT_RE.match(subject)) or (
            sender is not None and sender.email.casefold() == CALENDAR_SENDER)
        if sensitive_subject(subject):
            sensitive, subject = True, ""
        messages.append(MetaMessage(
            gmail_id=item["id"], at=at, labels=frozenset(label for label in labels if isinstance(label, str)),
            sender=sender, to=_people(headers.get("to", "")), cc=_people(headers.get("cc", "")),
            reply_to=_people(headers.get("reply-to", ""), 3), auto=auto_submitted(headers.get("auto-submitted", "")),
            listy=list_mail(headers.get("list-id", ""), headers.get("list-unsubscribe", ""),
                            headers.get("precedence", "")),
            calendar=calendar, subject=subject))
    messages.sort(key=lambda message: message.at)
    subject = "" if sensitive else next((message.subject for message in reversed(messages) if message.subject), "")
    return ThreadFacts(alias, data["id"], history if _HISTORY_RE.fullmatch(history) else "", subject,
                       tuple(messages), sensitive)


def within(facts: ThreadFacts, now: datetime, days: int) -> ThreadFacts:
    """``facts`` with only the messages of the last ``days`` days (a cached thread ages too, R9)."""
    cutoff = _aware(now) - timedelta(days=max(1, int(days)))
    kept = tuple(message for message in facts.messages if message.at >= cutoff)
    return facts if len(kept) == len(facts.messages) else replace(facts, messages=kept)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.astimezone()


# --------------------------------------------------------------------------
# Classification (5.4)
# --------------------------------------------------------------------------

def _local(address: str) -> str:
    return mailbox_key(address).rpartition("@")[0]


def is_own(address: str, own: Collection[str]) -> bool:
    return bool(address) and any(same_mailbox(address, mine) for mine in own)


def from_owner(message: Any, own: Collection[str]) -> bool:
    """SENT among its labels, or its sender is one of the owner's own mailboxes."""
    sender = getattr(message, "sender", None)
    return "SENT" in getattr(message, "labels", frozenset()) or (sender is not None and is_own(sender.email, own))


def automated(message: Any, known: Callable[[str], bool]) -> bool:
    """Mail no person waits on: Auto-Submitted, a list header, a no-reply sender, an invitation or
    Google Calendar's own mail, promotions / social / forums, and Updates unless the sender is a known
    correspondent."""
    if getattr(message, "auto", False) or getattr(message, "listy", False) or _calendar(message):
        return True
    sender = getattr(message, "sender", None)
    address = sender.email if sender is not None else ""
    if address and _local(address) in NOREPLY_LOCALS:
        return True
    labels = getattr(message, "labels", frozenset())
    if labels & AUTOMATED_LABELS:
        return True
    if UPDATES_LABEL in labels:
        return not (address and _safe_known(known, address))
    return False


def _calendar(message: Any) -> bool:
    if getattr(message, "calendar", False):
        return True
    sender = getattr(message, "sender", None)
    if sender is not None and sender.email.casefold() == CALENDAR_SENDER:
        return True
    return bool(CALENDAR_SUBJECT_RE.match(str(getattr(message, "subject", "") or "")))


def owner_automated(message: Any) -> bool:
    """An owner's message that never decides a thread (R17): an RSVP from an invitation ("Accepted:
    ...") or an auto-reply."""
    return getattr(message, "auto", False) or _calendar(message)


def _safe_known(known: Callable[[str], bool], address: str) -> bool:
    try:
        return bool(known(address))
    except Exception:  # noqa: BLE001 - unknown
        return False


def _messages_of(messages: Sequence[Any]) -> list[Any]:
    kept = [message for message in messages
            if not (getattr(message, "labels", frozenset()) & DROPPED_LABELS) and _when(message) is not None]
    kept.sort(key=_when)
    return kept[-MAX_MESSAGES:]


def _when(message: Any) -> datetime | None:
    return getattr(message, "at", None) or getattr(message, "sent_at", None)


def deciding_message(messages: Sequence[Any], own: Collection[str], known: Callable[[str], bool]
                     ) -> tuple[Any | None, frozenset[str]]:
    """(the deciding message, the gmail ids of the automated messages newer than it): from the newest
    back, the first message that is neither automated mail from someone else nor an automated message
    of the owner's (R17). (None, ids) when every message is automated. Works on MetaMessage and on a
    read thread's ask.mail.MailMessage alike."""
    skipped: list[str] = []
    for message in reversed(_messages_of(messages)):
        mine = from_owner(message, own)
        if (mine and owner_automated(message)) or (not mine and automated(message, known)):
            skipped.append(message.gmail_id)
            continue
        return message, frozenset(skipped)
    return None, frozenset(skipped)


def age_words(since: datetime, now: datetime) -> str:
    """"20h" (under two days), "3d"."""
    hours = max(0, int((_aware(now) - since).total_seconds() // 3600))
    return f"{hours}h" if hours < 48 else f"{hours // 24}d"


def _age_long(since: datetime, now: datetime) -> str:
    hours = max(0, int((_aware(now) - since).total_seconds() // 3600))
    if hours < 48:
        return f"{hours} hour{'' if hours == 1 else 's'} ago"
    days = hours // 24
    return f"{days} day{'' if days == 1 else 's'} ago"


def _who(person: MailPerson | None) -> str:
    if person is None:
        return ""
    return person.name or person.email.rpartition("@")[0] or person.email


def _classify(facts: ThreadFacts, *, own: Collection[str], own_domains: Collection[str], now: datetime,
              settings: Any, known: Callable[[str], bool]) -> FollowupItem | str:
    days = _setting(settings, "scan_days", 14, 3, 14)
    if not facts.messages:
        return TOO_OLD
    deciding, auto_ids = deciding_message(facts.messages, own, known)
    if deciding is None:
        return AUTOMATED
    mine = from_owner(deciding, own)
    others = ([deciding.sender] if not mine and deciding.sender is not None else
              [person for person in deciding.to if not is_own(person.email, own)])
    if facts.sensitive or any(is_security_sender(person.email, own_domains=own_domains) for person in others):
        return SENSITIVE
    hours = (_aware(now) - deciding.at).total_seconds() / 3600
    if hours > days * 24:
        return TOO_OLD
    if not mine:
        if "INBOX" not in deciding.labels:
            return ARCHIVED
        if not any(is_own(person.email, own) for person in deciding.to):
            return CC_ONLY if any(is_own(person.email, own) for person in deciding.cc) else NOT_DIRECT
        if len(deciding.to) > MAX_DIRECT:
            return GROUP
        if hours < _setting(settings, "waiting_after_hours", 20, 1, 72):
            return TOO_NEW
        kind = WAITING
        who = deciding.sender
        more = {person.email.casefold() for person in deciding.to + deciding.cc
                if not is_own(person.email, own) and who is not None and not same_mailbox(person.email, who.email)}
        reason = f"They wrote last, to you directly, {_age_long(deciding.at, now)}"
    else:
        direct = [person for person in deciding.to if not is_own(person.email, own)
                  and _local(person.email) not in NOREPLY_LOCALS]
        if not direct:
            return TO_SELF
        if len(deciding.to) > MAX_DIRECT or any(_local(person.email) in GROUP_LOCALS for person in deciding.to):
            return GROUP
        if hours < _setting(settings, "owed_after_days", 3, 1, 13) * 24:
            return TOO_NEW
        kind = OWED
        who = direct[0]
        more = {person.email.casefold() for person in direct[1:]}
        reason = f"You wrote last, {_age_long(deciding.at, now)}, and nobody answered"
    return FollowupItem(row_id=row_id_of(facts.alias, facts.thread_id),
                        key=hide_key(facts.alias, facts.thread_id, deciding.gmail_id), kind=kind, alias=facts.alias,
                        thread_id=facts.thread_id, last_gmail_id=deciding.gmail_id, auto_ids=auto_ids,
                        subject=clean_line(facts.subject, 120), who=clean_line(_who(who), 60),
                        who_address=who.email if who is not None else "", who_more=len(more), since=deciding.at,
                        reason=reason)


def classify(facts: ThreadFacts, *, own: Collection[str], own_domains: Collection[str] = (), now: datetime,
             settings: Any, known: Callable[[str], bool] = lambda _address: False,
             hidden: Callable[[str], bool] = lambda _key: False) -> FollowupItem | str:
    """The row of one thread (5.4), or the reason word it is not listed (HIDDEN when its key is hidden)."""
    found = _classify(facts, own=own, own_domains=own_domains, now=now, settings=settings, known=known)
    if isinstance(found, FollowupItem) and _safe_known(hidden, found.key):
        return HIDDEN
    return found


def sent_to(facts: Iterable[ThreadFacts], own: Collection[str]) -> frozenset[str]:
    """The mailboxes the owner wrote to in this scan (a sender in Updates is listed when he is one)."""
    found: set[str] = set()
    for one in facts:
        for message in one.messages:
            if from_owner(message, own):
                found |= {mailbox_key(person.email) for person in message.to + message.cc}
    return frozenset(found)


def participants_index(facts: Iterable[ThreadFacts], own: Collection[str],
                       known: Callable[[str], bool] = lambda _address: False) -> dict[str, tuple[ThreadRef, ...]]:
    """mailbox_key(address) -> the newest INDEX_PER_PERSON ThreadRefs (alias, thread id, newest message)
    of the scan's threads with that person: non-automated, non-sensitive threads only, people of their
    non-automated messages only (in-window messages: the facts are cut already, R9)."""
    refs: dict[str, list[ThreadRef]] = {}
    for one in facts:
        if one.sensitive or not one.messages:
            continue
        deciding, _auto = deciding_message(one.messages, own, known)
        if deciding is None:
            continue
        last_at = max(message.at for message in one.messages)
        people: set[str] = set()
        for message in one.messages:
            if not from_owner(message, own) and automated(message, known):
                continue
            for person in message.people():
                if person.email and not is_own(person.email, own):
                    people.add(mailbox_key(person.email))
        for key in people:
            refs.setdefault(key, []).append(ThreadRef(one.alias, one.thread_id, last_at))
    return {key: tuple(sorted(found, key=lambda ref: ref.last_at, reverse=True)[:INDEX_PER_PERSON])
            for key, found in refs.items()}


def make_view(items: Iterable[FollowupItem], hidden: Iterable[FollowupItem] = (), *,
              reports: Sequence[ScanReport] = (), notes: Sequence[tuple[str, str]] = (),
              scanned_at: datetime | None = None, scanning: bool = False, off: bool = False) -> FollowupView:
    """The rows by kind, each kind oldest first, at most SHOWN_MAX per kind."""
    rows = list(items)

    def kind(name: str) -> tuple[FollowupItem, ...]:
        found = [item for item in rows if item.kind == name]
        found.sort(key=lambda item: (item.since or datetime.max.replace(tzinfo=timezone.utc), item.row_id))
        return tuple(found[:SHOWN_MAX])

    return FollowupView(kind(WAITING), kind(OWED), tuple(hidden), tuple(reports), tuple(notes), scanned_at, scanning,
                        off)


def _setting(settings: Any, name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


# --------------------------------------------------------------------------
# The scan (5.3)
# --------------------------------------------------------------------------

def _read_error(error: Exception) -> str:
    """PROBLEM_RATE_LIMIT, PROBLEM_REFUSED (401, or a 403 that is not a rate limit) or PROBLEM_ERROR
    (5xx, transport, a token refresh that failed: the next interval retries)."""
    from .gmail import is_rate_limited

    if is_rate_limited(error):
        return PROBLEM_RATE_LIMIT
    status = getattr(error, "status", None)
    if status in (401, 403):
        return PROBLEM_REFUSED
    return PROBLEM_ERROR


class MailScanner:
    """The follow-up scan of each account, one at a time, on the prod thread (spec 5.3). Holds the
    header cache (memory only, per account and identity; at most CACHE_MAX threads), the accounts
    stopped by a refusal (until their stop key changes: a new sign-in or another Google account, never
    a token refresh, R19) and the owner's own addresses per account."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic, pause: float = GET_PAUSE_S) -> None:
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._sleep = sleep
        self._monotonic = monotonic
        self._pause = pause
        self._lock = threading.RLock()
        self._cache: dict[str, dict[str, tuple[str, ThreadFacts]]] = {}
        self._identity: dict[str, str] = {}
        self._stopped: dict[str, str] = {}
        self._own: dict[str, frozenset[str]] = {}
        self._sent_to: dict[str, frozenset[str]] = {}
        self.gets = 0                       # header reads since start (tests, the quota probe)

    def stopped(self, alias: str) -> bool:
        with self._lock:
            return alias in self._stopped

    def forget(self, alias: str | None = None) -> None:
        with self._lock:
            for table in (self._cache, self._identity, self._stopped, self._own, self._sent_to):
                if alias is None:
                    table.clear()
                else:
                    table.pop(alias, None)

    def cached(self, alias: str | None = None) -> int:
        with self._lock:
            if alias is not None:
                return len(self._cache.get(alias, {}))
            return sum(len(entries) for entries in self._cache.values())

    def scan(self, alias: str, reader: Any, *, now: datetime, settings: Any, known: Callable[[str], bool],
             hidden: Callable[[str], bool], cancel: threading.Event, step: Any = None,
             stop_key: str = "") -> ScanResult:
        """One account's scan (spec 5.3): its rows, its hidden rows, the report (counts only) and its
        part of the participants index. Gmail errors never raise (a partial or stopped report);
        anything else does (the prod job logs its type only, R8)."""
        from .gmail import SCAN_LABELS, GmailError

        started = self._monotonic()
        days = _setting(settings, "scan_days", 14, 3, 14)
        budget = _setting(settings, "scan_max_threads", 150, 20, 300)
        with self._lock:
            held = self._stopped.get(alias)
            if held is not None and held == stop_key:
                return self._not_scanned(alias, PROBLEM_STOPPED, step, started)
            self._stopped.pop(alias, None)
        problem, _message = reader.available()
        if problem:
            return self._not_scanned(alias, PROBLEM_UNREADABLE, step, started)
        own_address = reader.confirmed_address()
        if not own_address:
            return self._not_scanned(alias, reader.scan_problem() or PROBLEM_UNCONFIRMED, step, started)
        identity = reader.identity_key()
        with self._lock:
            if self._identity.get(alias) != identity:
                self._cache.pop(alias, None)        # another Google account: nothing of the old one is kept
                self._own.pop(alias, None)
                self._sent_to.pop(alias, None)
                self._identity[alias] = identity
            cache = dict(self._cache.get(alias, {}))
        own = frozenset({own_address.casefold()})
        _step_fields(step, days, budget)
        listed: list[tuple[str, str]] = []
        per_label: dict[str, int] = {}
        partial, stop = False, ""
        try:
            # A refusal of the own-address read stops the account like any other refused read (R19).
            own = own | {address.casefold() for address in reader.own_addresses()}
            for label in ("SENT", "INBOX"):
                if cancel.is_set():
                    partial, stop = True, PROBLEM_CANCELLED
                    break
                assert label in SCAN_LABELS
                left = max(1, budget // 3) if label == "SENT" else max(1, budget - per_label.get("SENT", 0))
                ids = reader.scan_ids(label=label, days=days, max_threads=left)
                per_label[label] = len(ids)
                seen = {thread_id for thread_id, _history in listed}
                listed += [(thread_id, history) for thread_id, history in ids if thread_id not in seen]
        except GmailError as exc:
            partial, stop = True, _read_error(exc)
        fetched = reused = 0
        facts: dict[str, ThreadFacts] = {}
        first = True
        for thread_id, history in listed if not stop else ():
            if cancel.is_set():
                partial, stop = True, PROBLEM_CANCELLED
                break
            entry = cache.get(thread_id)
            if entry is not None and history and entry[0] == history:
                facts[thread_id] = entry[1]
                reused += 1
                continue
            if not first:
                self._sleep(self._pause)
                if cancel.is_set():          # a sign-in opened meanwhile: no further read (R19)
                    partial, stop = True, PROBLEM_CANCELLED
                    break
            first = False
            try:
                data = reader.thread_metadata(thread_id)
            except GmailError as exc:
                if getattr(exc, "status", None) == 404:
                    cache.pop(thread_id, None)
                    continue
                partial, stop = True, _read_error(exc)
                break
            fetched += 1
            self.gets += 1
            found = facts_from_metadata(data, alias, now=now, days=days)
            if found is None:
                continue
            cache[thread_id] = (history, found)
            facts[thread_id] = found
        listed_ids = [thread_id for thread_id, _history in listed]
        if partial:
            # Stopped early: the earlier facts stay with this partial set (threads not reached or not
            # listed this time keep their last known headers; nothing is dropped from the cache).
            for thread_id, entry in cache.items():
                facts.setdefault(thread_id, entry[1])
        else:
            cache = {thread_id: cache[thread_id] for thread_id in listed_ids if thread_id in cache}
        facts = {thread_id: within(one, now, days) for thread_id, one in facts.items()}
        with self._lock:
            if stop == PROBLEM_REFUSED:
                self._stopped[alias] = stop_key
            self._cache[alias] = cache
            self._trim_locked(alias)
            self._own[alias] = own
            sent = sent_to(facts.values(), own)
            self._sent_to[alias] = sent
        known_fn = _known_with(known, sent)
        items, hidden_items, skipped = self._classify_all(facts.values(), own, now, settings, known_fn, hidden)
        participants = participants_index(facts.values(), own, known_fn)
        report = ScanReport(alias, listed=len(listed), fetched=fetched, cached=reused,
                            waiting=sum(1 for item in items if item.kind == WAITING),
                            owed=sum(1 for item in items if item.kind == OWED), skipped=skipped, partial=partial,
                            problem=stop, duration_ms=int((self._monotonic() - started) * 1000),
                            hidden=len(hidden_items))
        self._log(reader, report, per_label)
        _step_done(step, report, per_label)
        return ScanResult(items, hidden_items, report, participants)

    def refetch(self, alias: str, thread_id: str, reader: Any, *, now: datetime, settings: Any,
                known: Callable[[str], bool], hidden: Callable[[str], bool], step: Any = None
                ) -> FollowupItem | str | None:
        """One listed thread's headers read again (after a nudge guard stop or a sent reply, R47): its
        row, the reason it is not listed any more ("gone" when Gmail has no such thread), or None when
        it could not be read (the row stays as it was). 10 quota units."""
        from .gmail import GmailError

        days = _setting(settings, "scan_days", 14, 3, 14)
        try:
            data = reader.thread_metadata(thread_id)
        except GmailError as exc:
            if getattr(exc, "status", None) == 404:
                with self._lock:
                    self._cache.get(alias, {}).pop(thread_id, None)
                _step_refetch(step, "gone")
                return "gone"
            _step_refetch(step, "failed")
            return None
        self.gets += 1
        found = facts_from_metadata(data, alias, now=now, days=days)
        if found is None:
            _step_refetch(step, "failed")
            return None
        with self._lock:
            self._cache.setdefault(alias, {})[thread_id] = (found.history_id, found)
            own = self._own.get(alias) or frozenset()
            sent = self._sent_to.get(alias, frozenset())
        if not own:
            confirmed = reader.confirmed_address()
            own = frozenset({confirmed.casefold()}) if confirmed else frozenset()
        known_fn = _known_with(known, sent)
        result = classify(found, own=own, own_domains=domains_of(own), now=now, settings=settings, known=known_fn,
                          hidden=hidden)
        _step_refetch(step, result.kind if isinstance(result, FollowupItem) else result)
        return result

    def _classify_all(self, facts: Iterable[ThreadFacts], own: frozenset[str], now: datetime, settings: Any,
                      known: Callable[[str], bool], hidden: Callable[[str], bool]
                      ) -> tuple[list[FollowupItem], list[FollowupItem], dict[str, int]]:
        own_domains = domains_of(own)
        items: list[FollowupItem] = []
        hidden_items: list[FollowupItem] = []
        skipped: dict[str, int] = {}
        for one in facts:
            found = _classify(one, own=own, own_domains=own_domains, now=now, settings=settings, known=known)
            if isinstance(found, str):
                skipped[found] = skipped.get(found, 0) + 1
            elif _safe_known(hidden, found.key):
                skipped[HIDDEN] = skipped.get(HIDDEN, 0) + 1
                hidden_items.append(found)
            else:
                items.append(found)
        return items, hidden_items, skipped

    def _trim_locked(self, alias: str) -> None:
        total = sum(len(entries) for entries in self._cache.values())
        if total <= CACHE_MAX:
            return
        entries = self._cache.get(alias, {})
        for thread_id in list(entries)[-(total - CACHE_MAX):]:   # listed order: SENT, then the inbox's oldest last
            entries.pop(thread_id, None)

    def _not_scanned(self, alias: str, problem: str, step: Any, started: float) -> ScanResult:
        logger.info("Follow-ups: %s not scanned (%s)", logged_alias(alias), problem)
        report = ScanReport(alias, problem=problem, duration_ms=int((self._monotonic() - started) * 1000))
        _step_done(step, report, {})
        return ScanResult([], [], report, {})

    @staticmethod
    def _log(reader: Any, report: ScanReport, per_label: Mapping[str, int]) -> None:
        name = getattr(reader, "log_name", "Gmail reading")
        logger.info("%s: read %d header set(s)", name, report.fetched)
        skipped = ", ".join(f"{word} {count}" for word, count in sorted(report.skipped.items())) or "none"
        logger.info("Follow-ups: scan of %s in %.1f s (listed %d, fetched %d, cached %d; %d waiting, %d owed; "
                    "skipped %s)", logged_alias(report.alias), report.duration_ms / 1000, report.listed,
                    report.fetched, report.cached, report.waiting, report.owed, skipped)
        if report.partial:
            logger.info("Follow-ups: %s stopped (%s); partial", logged_alias(report.alias), report.problem or "error")


def _known_with(known: Callable[[str], bool], sent: frozenset[str]) -> Callable[[str], bool]:
    def check(address: str) -> bool:
        return mailbox_key(address) in sent or _safe_known(known, address)

    return check


SCAN_WORDS = ("metadata only: sender, recipients, subject, date, labels, list headers - never the text; "
              "last {days} days")
_PROBLEM_WORDS = {PROBLEM_UNCONFIRMED: "not scanned: the Google account is not confirmed yet",
                  PROBLEM_IDENTITY: "not scanned: sign in again so Jarvis knows the Google account",
                  PROBLEM_UNREADABLE: "not scanned: Jarvis can't read this account's mail now",
                  PROBLEM_REFUSED: "Gmail refused reading - stopped until you sign in again",
                  PROBLEM_STOPPED: "not scanned: stopped until you sign in again",
                  PROBLEM_RATE_LIMIT: "partial - Gmail asked Jarvis to slow down",
                  PROBLEM_ERROR: "partial - a read failed; the next scan tries again",
                  PROBLEM_CANCELLED: "partial - stopped for a Google sign-in"}


def problem_words(problem: str) -> str:
    return _PROBLEM_WORDS.get(problem, problem.replace("_", " "))


def _step_fields(step: Any, days: int, budget: int) -> None:
    if step is None:
        return
    try:
        step.field("Reads", SCAN_WORDS.format(days=days))
        step.field("Lists", f"SENT newer_than:{days}d (up to {max(1, budget // 3)}), then INBOX newer_than:{days}d "
                            f"-category:promotions -category:social -category:forums (the rest of {budget})")
    except Exception:  # noqa: BLE001 - LIVE never changes behaviour
        pass


def _step_done(step: Any, report: ScanReport, per_label: Mapping[str, int]) -> None:
    if step is None:
        return
    try:
        from . import live

        if report.problem in NOT_SCANNED:
            step.done(live.STATUS_WARN if report.problem != PROBLEM_REFUSED else live.STATUS_BLOCKED,
                      summary=problem_words(report.problem))
            return
        listed = ", ".join(f"{label} {count}" for label, count in per_label.items()) or "nothing"
        step.field("Listed", listed)
        step.field("Read", f"{report.fetched} header set(s); {report.cached} unchanged (not read again)")
        step.field("Kept", f"{report.waiting} waiting on you, {report.owed} owed to you"
                           + (f", {report.hidden} hidden" if report.hidden else ""))
        skipped = ", ".join(f"{word.replace('_', ' ')} {count}" for word, count in sorted(report.skipped.items()))
        step.field("Skipped", skipped or "none")
        summary = f"{report.waiting} waiting, {report.owed} owed"
        if report.partial:
            step.field("Stopped", problem_words(report.problem or PROBLEM_ERROR))
            step.done(live.STATUS_WARN, summary=f"{summary} (partial)")
        else:
            step.done(live.STATUS_OK, summary=f"{summary}; headers only")
    except Exception:  # noqa: BLE001 - LIVE never changes behaviour
        pass


def _step_refetch(step: Any, result: str) -> None:
    if step is None:
        return
    try:
        from . import live

        words = {WAITING: "still waiting on you", OWED: "still owed to you", "gone": "the thread is gone",
                 "failed": "could not be read now"}.get(result, f"not listed any more ({result.replace('_', ' ')})")
        step.done(live.STATUS_WARN if result == "failed" else live.STATUS_OK, summary=words)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# followups.json (5.6): hide keys and times only
# --------------------------------------------------------------------------

def _local_now() -> datetime:
    return datetime.now().astimezone()


class HiddenStore:
    """followups.json: ``{"version": 1, "hidden": {"<32 hex key>": {"at": iso, "until": iso or ""}}}``.
    ``until`` "" = until something new arrives (a new deciding message gives another key). Entries
    older than 30 days (or whose time passed) are pruned; a missing, corrupt or unreadable file counts
    as empty (hidden rows come back: harmless). Every change re-reads the file first and writes
    atomically (lock, temporary file, fsync, os.replace), LF, ASCII. No text, no id in clear."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self._clock = clock or _local_now
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, str]] = _load(self.path)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def hide(self, key: str, *, until: datetime | None) -> bool:
        """Hide ``key`` until ``until`` (None: until something new arrives); True when written."""
        if not _valid_key(key):
            return False
        now = _aware(self._clock())
        entry = {"at": now.isoformat(timespec="seconds"),
                 "until": _aware(until).isoformat(timespec="seconds") if until is not None else ""}
        with self._lock:
            self._entries = {**_load(self.path), **self._entries}
            self._entries[key] = entry
            self._prune_locked(now)
            return _write(self.path, self._entries)

    def unhide(self, key: str) -> bool:
        if not _valid_key(key):
            return False
        with self._lock:
            self._entries = {**_load(self.path), **self._entries}
            if self._entries.pop(key, None) is None:
                return False
            self._prune_locked(_aware(self._clock()))
            return _write(self.path, self._entries)

    def hidden(self, key: str, now: datetime | None = None) -> bool:
        with self._lock:
            entry = self._entries.get(str(key))
        if entry is None:
            return False
        until = entry.get("until", "")
        if not until:
            return True
        try:
            return _aware(now or self._clock()) < datetime.fromisoformat(until)
        except (TypeError, ValueError):
            return False

    def _prune_locked(self, now: datetime) -> None:
        cutoff = now - timedelta(days=PRUNE_DAYS)
        for key in list(self._entries):
            entry = self._entries[key]
            try:
                at = datetime.fromisoformat(entry.get("at", ""))
                until = datetime.fromisoformat(entry["until"]) if entry.get("until") else None
            except (TypeError, ValueError):
                self._entries.pop(key)
                continue
            if _aware(at) < cutoff or (until is not None and _aware(until) <= now):
                self._entries.pop(key)
        while len(self._entries) > MAX_HIDDEN:
            oldest = min(self._entries, key=lambda item: (self._entries[item].get("at", ""), item))
            self._entries.pop(oldest)


def _valid_key(key: object) -> bool:
    return isinstance(key, str) and len(key) == _KEY_LEN and all(ch in "0123456789abcdef" for ch in key)


def _load(path: Path) -> dict[str, dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        logger.warning("Could not read %s (%s); no follow-up is hidden", path.name, type(exc).__name__)
        return {}
    try:
        data = json.loads(text)
    except (ValueError, RecursionError, MemoryError) as exc:
        logger.warning("%s is not valid JSON (%s); no follow-up is hidden", path.name, type(exc).__name__)
        return {}
    hidden = data.get("hidden") if isinstance(data, dict) else None
    if not isinstance(hidden, dict):
        logger.warning("%s is not a table of hidden follow-ups; no follow-up is hidden", path.name)
        return {}
    kept: dict[str, dict[str, str]] = {}
    for key, value in hidden.items():
        if not _valid_key(key) or not isinstance(value, dict):
            continue
        at, until = value.get("at"), value.get("until", "")
        if not isinstance(at, str) or not isinstance(until, str):
            continue
        try:
            datetime.fromisoformat(at)
            if until:
                datetime.fromisoformat(until)
        except ValueError:
            continue
        kept[key] = {"at": at, "until": until}
    return kept


def _write(path: Path, entries: Mapping[str, Mapping[str, str]]) -> bool:
    data = json.dumps({"version": 1, "hidden": dict(entries)}, ensure_ascii=True, indent=1, sort_keys=True) + "\n"
    part: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, part = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(part, path)
        part = None
        return True
    except OSError as exc:
        logger.warning("Could not save %s (%s); kept for this run only", path.name, type(exc).__name__)
        return False
    finally:
        if part is not None:
            try:
                os.unlink(part)
            except OSError:
                pass
