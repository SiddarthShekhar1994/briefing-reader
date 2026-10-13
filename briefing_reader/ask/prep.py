"""Meeting prep (the productivity bundle's "prep me for my 2 PM"): which meeting the owner means, whom
Jarvis looks up and with which Gmail search, the run's <people> rows, the planner's answer read and
checked, and the brief the owner sees.

    resolve_meeting(words, briefs, now, own, zone=)   MeetingMatch: the meeting(s) the typed words
                                     name (a time, "next", or title words: every typed word in the
                                     title (title_all) or only some (title_some, which always asks
                                     "Prep this one" first, R5))
    meeting_of(alias, brief, now, zone)   PrepMeeting: the meeting as read now (wall times)
    prep_guests(brief, own, limit)   a GuestLookup per guest, in the order they are looked up:
                                     the organizer, then accepted, tentative, no answer, declined;
                                     the owner's own mailboxes, group addresses and declined guests
                                     are never searched, and at most ``limit`` are
    prep_query(address, days) / guest_allowed(address, days)   Jarvis's own search for one guest
                                     ('{from:"X" to:"X" cc:"X"} newer_than:14d', the address quoted)
                                     and whether it may run (a plain address with no search syntax,
                                     ask.mail.check_query and is_security_sender, no own domains, R2)
    index_lookup(participants, address, alias, other_accounts=)   the header list's newest thread
                                     with that guest on the meeting's account (R3; a later build
                                     uses it, always after guest_allowed)
    people_block(lookups, days, alias)   the <people> rows
    read_prep_result(event, init)    the run's answer: PrepAnswer, or AskFailure (stream's checks)
    PrepCheck(alias, threads, notes, own=)   validate.to_cards' ``extra`` for the brief's Reply lines
    check_prep(answer, meeting, lookups, threads, memory, zone=)   PrepBrief: Jarvis's own facts (who
                                     is coming, each thread's subject and date in the calendar's
                                     zone) and the planner's items that name a thread Jarvis read
    deterministic_brief(meeting, lookups, pending, memory, why)   PrepBrief without a planner run
    brief_text(brief, hour24)        the JARVIS entry (plain text; never logged or spoken)

The typed words only pick the meeting: they are never sent to Claude. A search names one guest's
address from the calendar and the lookback (3..14 days, R4), nothing else. Pure: no Google, no
Claude, nothing logged; every repr names kinds and counts only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from ..actions import REPLY, ProposedAction, email_address
from ..gcal import EventBrief, wall_in_zone
from ..recipients import mailbox_key, same_mailbox
from .context import data
from .mail import MAX_QUERY_CHARS, MailThread, QueryRefused, check_query, is_security_sender
from .modes import EventRef, ThreadRef
from .stream import GARBLED, AskFailure, InitSummary, failure, result_failure

GROUP_LOCALS = frozenset({"team", "all", "staff", "everyone", "group", "list", "class", "students"})
WHY_PREP = "your latest thread with a guest of this meeting"
LOOKBACK_MIN, LOOKBACK_MAX = 3, 14
MAX_THREADS = 3                     # threads read for one brief (at most one per guest)
MAX_CHOICES = 3                     # meetings offered to pick from
MAX_PEOPLE_ROWS = 20
MATCH_MINUTES = 10                  # a typed time names a meeting starting this close to it
ROLE_ORGANIZER, ROLE_GUEST = "organizer", "guest"

# GuestLookup.status
PENDING = "pending"                 # to be looked up
FOUND = "found"                     # a thread with this guest (alias, thread_id)
NONE = "none"                       # looked up: no email in the lookback
SAME = "same"                       # the newest thread is one another guest's lookup already found
REFUSED = "refused"                 # guest_allowed refused the address: never looked up (R2)
GROUP = "group"                     # a group address (team@, class@ ...): never looked up
DECLINED = "declined"               # declined the meeting: never looked up
NOT_SEARCHED = "not_searched"       # not looked up (``why`` says why)
OWN = "own"                         # one of the owner's own mailboxes: never listed or looked up
STATUSES = (PENDING, FOUND, NONE, SAME, REFUSED, GROUP, DECLINED, NOT_SEARCHED, OWN)

MATCH_TIME, MATCH_NEXT, MATCH_TITLE_ALL, MATCH_TITLE_SOME = "time", "next", "title_all", "title_some"

# Jarvis's own words for a guest who was not looked up (the brief, <people>, LIVE; never logged).
REFUSED_WHY = "Jarvis doesn't search that address"
GROUP_WHY = "a group address"
DECLINED_WHY = "declined"
LIMIT_WHY = "only {limit} guests are looked up"
THREADS_WHY = "only 3 threads are read"
FAILED_WHY = "the Gmail search failed"
UNREAD_WHY = "the thread could not be read"
NOT_READ_WHY = "mail not read for this brief"
CANCELLED_WHY = "cancelled"
NO_MAIL = "no email in the last {days} days"

# PrepCheck's reasons (shown on the information-only card and in LIVE; never logged).
REPLY_ACCOUNT = "A prep reply comes from the meeting's account"
EXTRA_PEOPLE = "This draft adds people who aren't in the thread"
NOTE_LEAK = "This draft repeats one of your private notes, so Jarvis holds it back"
MAIL_LEAK = "This draft repeats mail from another thread, so Jarvis holds it back"
ONLY_REPLIES = "A meeting brief only drafts replies"
# A draft repeats another thread's mail: a whole line of a guest's message at least this long and
# with at least this many words, or this many of its words in a row (longer than memory.note_leak's,
# since mail shares stock phrases; a hold is information only).
LEAK_LINE_CHARS, LEAK_LINE_WORDS, LEAK_RUN_WORDS = 20, 4, 7

RESPONSE_WORDS = {"accepted": "accepted", "tentative": "maybe", "declined": "declined",
                  "needsAction": "no answer yet", "": "no answer yet"}
WAITING_WORDS = {"owner": "Waiting on you.", "them": "Waiting on them.", "nobody": ""}
WAITING_ON = ("owner", "them", "nobody")
_RESPONSE_RANK = {"accepted": 0, "tentative": 1, "needsAction": 2, "": 2, "declined": 3}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# Words a typed target may have around a time or "next" ("my next meeting", "my 2 PM call").
_FILLER = frozenset({"my", "the", "a", "an", "next", "meeting", "call", "at", "today", "tomorrow", "upcoming",
                     "for", "on", "this", "me", "please"})
# Words that never pick a meeting by its title.
_STOPWORDS = frozenset({"the", "and", "for", "with", "about", "from", "into", "our", "your", "this", "that",
                        "today", "tomorrow", "next", "please", "upcoming", "prep", "brief"})
_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?(am|pm)?")
_WORD_RE = re.compile(r"[^\W_]+")
# An address Jarvis puts in a search of his own: a plain mailbox (actions.email_address allows the
# rest of RFC atext, but "{", "}", "(", ")", "*", "=" and the like are Gmail search syntax).
_PLAIN_ADDRESS_RE = re.compile(r"[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9.-]+")
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)
_THREAD_KEY_RE = re.compile(r"^\s*thread\s*=\s*", re.IGNORECASE)
SAY_CAP, SUMMARY_CAP, OPEN_CAP, POINT_CAP, THREAD_CAP, LINE_CAP = 200, 300, 200, 160, 64, 12000
THREADS_MAX, OPEN_MAX, POINTS_MAX, LINES_MAX = 3, 6, 4, 2


# --------------------------------------------------------------------------
# Data (memory only; every repr is kinds and counts)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Guest:
    name: str
    address: str
    role: str                # ROLE_ORGANIZER | ROLE_GUEST
    response: str            # needsAction | accepted | declined | tentative | ""

    def __repr__(self) -> str:
        return f"Guest({self.role})"

    @property
    def display(self) -> str:
        """The name the calendar gives, else the address (on screen only)."""
        return data(self.name, 60) or self.address


@dataclass(frozen=True)
class GuestLookup:
    guest: Guest
    status: str                          # one of STATUSES
    alias: str = ""                      # FOUND / SAME: the account the thread is on
    thread_id: str = ""
    last_at: datetime | None = None      # the thread's newest message (once read)
    why: str = ""                        # NOT_SEARCHED: Jarvis's own words; SAME: whose thread it is

    def __repr__(self) -> str:
        return f"GuestLookup({self.status})"


@dataclass(frozen=True)
class MeetingMatch:
    """What typed words named: up to MAX_CHOICES meetings (by start) and how they were found."""

    refs: tuple[EventRef, ...]
    how: str
    found: tuple[tuple[str, EventBrief], ...] = field(default=(), repr=False, compare=False)

    def __repr__(self) -> str:
        return f"MeetingMatch({len(self.refs)}, {self.how})"


@dataclass(frozen=True)
class PrepMeeting:
    """The meeting as read now: its account, the event, its start and end as naive wall times in the
    personal calendar's zone, and the day word ("today", "tomorrow", or "Fri Oct 9")."""

    alias: str
    brief: EventBrief = field(repr=False)
    start: datetime = field(repr=False)
    end: datetime = field(repr=False)
    day: str = field(default="today", repr=False)

    def __repr__(self) -> str:
        return f"PrepMeeting({self.alias})"

    @property
    def ref(self) -> EventRef:
        start = self.brief.start if self.brief.start is not None else self.start
        return EventRef(self.alias, self.brief.calendar_id, self.brief.event_id, start)


@dataclass(frozen=True)
class PrepChoice:
    """One meeting to pick ("Prep this one"): its ref and what the entry shows (memory only)."""

    ref: EventRef
    title: str = field(repr=False)
    start: datetime = field(repr=False)
    end: datetime = field(repr=False)
    day: str = field(default="today", repr=False)

    def __repr__(self) -> str:
        return f"PrepChoice({self.ref.alias})"


@dataclass(frozen=True)
class PrepAnswer:
    """The run's structured answer, shape-checked (planner text: shown, never logged or spoken)."""

    say: str
    threads: tuple[tuple[str, str, str], ...]   # (thread id, summary, waiting_on)
    open: tuple[tuple[str, str], ...]           # (text, thread id or "")
    points: tuple[str, ...]
    lines: tuple[str, ...]
    via_fallback: bool = False

    def __repr__(self) -> str:
        return (f"PrepAnswer({len(self.threads)} thread(s), {len(self.open)} open, {len(self.points)} point(s), "
                f"{len(self.lines)} line(s))")


@dataclass(frozen=True)
class PrepBrief:
    """The brief (memory only: a conversation entry, never saved, logged or spoken). ``who``,
    ``missing`` and each thread's who / subject / date are Jarvis's own; the summaries, open items
    and points are the planner's, kept only when they name a thread Jarvis read."""

    meeting: EventRef
    title: str = field(repr=False)
    alias: str = ""
    start: datetime | None = field(default=None, repr=False)
    end: datetime | None = field(default=None, repr=False)
    day: str = field(default="today", repr=False)
    who: tuple[tuple[str, str], ...] = field(default=(), repr=False)              # (name, response words)
    # (who, '"subject", Oct 8' from Jarvis's headers, the planner's summary, waiting_on)
    threads: tuple[tuple[str, str, str, str], ...] = field(default=(), repr=False)
    missing: tuple[tuple[str, str], ...] = field(default=(), repr=False)           # (who, why)
    open: tuple[str, ...] = field(default=(), repr=False)
    points: tuple[str, ...] = field(default=(), repr=False)
    notes: tuple[str, ...] = field(default=(), repr=False)                         # memory notes naming a guest
    dropped: int = 0
    ran: bool = False
    note: str = field(default="", repr=False)                                       # why no mail was read
    # The meeting's repeating series (EventBrief.series_id; "" for a single event): a brief starts the
    # series' count of ignored prep offers afresh (R38). Memory only, never shown or logged.
    series_id: str = field(default="", repr=False)

    def __repr__(self) -> str:
        return (f"PrepBrief({len(self.threads)} thread(s), {len(self.open)} open, {len(self.points)} point(s), "
                f"{self.dropped} dropped, {'ran' if self.ran else 'no run'})")

    def when(self, hour24: bool = False) -> str:
        """"today 2:00-2:30 PM", "tomorrow 09:00-09:30" (24-hour)."""
        from .dayplan import hours_text

        if self.start is None:
            return self.day
        return f"{self.day} {hours_text(self.start, self.end or self.start, hour24)}"


# --------------------------------------------------------------------------
# Which meeting
# --------------------------------------------------------------------------

def _wall(moment: datetime, zone: str, now: datetime | None = None) -> datetime:
    """``moment`` as a naive wall time in ``zone`` (gcal.wall_in_zone), else in ``now``'s zone."""
    if zone:
        return wall_in_zone(moment, zone)
    if moment.tzinfo is None:
        return moment
    reference = now.tzinfo if now is not None and now.tzinfo is not None else None
    return moment.astimezone(reference).replace(tzinfo=None)


def _span(brief: EventBrief, zone: str, now: datetime | None) -> tuple[datetime, datetime] | None:
    if brief.start is None or brief.all_day:
        return None
    start = _wall(brief.start, zone, now)
    end = _wall(brief.end, zone, now) if brief.end is not None else start
    return start, max(start, end)


def day_word(day: date, today: date) -> str:
    """"today", "tomorrow", else "Fri Oct 9"."""
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    return f"{_WEEKDAYS[day.weekday()]} {_MONTHS[day.month - 1]} {day.day}"


def _is_own(address: str, own: Collection[str]) -> bool:
    folded = (address or "").strip().casefold()
    return any(folded == str(mine).strip().casefold() or same_mailbox(address, str(mine)) for mine in own)


def other_guests(brief: EventBrief, own: Collection[str]) -> int:
    """How many people besides the owner the meeting has (its organizer included when not him)."""
    seen: set[str] = set()
    for address in [*([brief.organizer_email] if not brief.organizer_self else []),
                    *(guest.email for guest in brief.guests)]:
        plain = email_address(address or "")
        if plain and not _is_own(plain, own):
            seen.add(mailbox_key(plain))
    return len(seen)


def qualifies(brief: EventBrief, own: Collection[str]) -> bool:
    """A meeting worth a brief: timed, at least one other guest, not declined by the owner."""
    return (brief.start is not None and not brief.all_day and brief.self_response != "declined"
            and other_guests(brief, own) > 0)


@dataclass(frozen=True)
class _Target:
    kind: str                        # MATCH_NEXT | MATCH_TIME | title
    tomorrow: bool = False
    times: tuple[time, ...] = ()
    words: frozenset[str] = frozenset()

    def __repr__(self) -> str:       # never the typed words (spec 14)
        return f"_Target({self.kind}, {len(self.times)} time(s), {len(self.words)} word(s))"


def _title_words(text: str) -> set[str]:
    return {word for word in _WORD_RE.findall(str(text or "").casefold()) if len(word) >= 3}


def parse_target(words: str) -> _Target:
    """The typed target: "" / "my next meeting" -> next; "my 2 PM", "14:00", "2:30 pm tomorrow" ->
    a time (a bare 1..12 hour may be AM or PM); else title words (>= 3 letters, stopwords out)."""
    text = " ".join(str(words or "").casefold().split()).strip(" .!?,;:")
    text = re.sub(r"'s\b", "", text).replace("o'clock", " ").replace("oclock", " ")
    tokens = text.split()
    tomorrow = "tomorrow" in tokens
    rest = [token for token in tokens if token not in _FILLER]
    if not rest:
        return _Target(MATCH_NEXT, tomorrow)
    joined = "".join(rest).replace(".", "")
    match = _TIME_RE.fullmatch(joined)
    if match:
        hour, minute, meridiem = int(match.group(1)), int(match.group(2) or 0), match.group(3)
        found: list[time] = []
        if minute <= 59:
            if meridiem and 1 <= hour <= 12:
                found = [time(hour % 12 + (12 if meridiem == "pm" else 0), minute)]
            elif not meridiem and 1 <= hour <= 12:
                found = [time(hour % 12, minute), time(hour % 12 + 12, minute)]
            elif not meridiem and hour in (0, *range(13, 24)):
                found = [time(hour, minute)]
        if found:
            return _Target(MATCH_TIME, tomorrow, tuple(found))
    typed = frozenset(word for word in _title_words(text) if word not in _STOPWORDS)
    return _Target("title", tomorrow, words=typed)


def resolve_meeting(words: str, briefs: Mapping[str, Sequence[EventBrief]], now: datetime, own: Collection[str],
                    *, zone: str = "") -> MeetingMatch:
    """The meeting(s) typed ``words`` name among ``briefs`` (today and tomorrow as read now), at most
    MAX_CHOICES by start. Only qualifying meetings (timed, another guest, not declined), today or
    tomorrow in ``zone``'s wall time (``now``'s zone when ""), each event once. A time ("2", "2 pm",
    "14:00", "2:30") names a meeting starting within MATCH_MINUTES of it today (tomorrow when the
    words say "tomorrow") [time]; nothing but filler ("", "my next meeting") the next one starting
    from now on [next]; else title words of meetings not yet over: those whose title has every typed
    word [title_all], else those with at least one [title_some]. The caller goes straight on only
    for one time / next / title_all match; title_some always asks first (R5)."""
    wall_now = _wall(now, zone, now)
    today = wall_now.date()
    tomorrow = today + timedelta(days=1)
    candidates: list[tuple[datetime, datetime, str, EventBrief]] = []
    seen: set[str] = set()
    for alias, items in briefs.items():
        for brief in items or ():
            if not qualifies(brief, own) or brief.event_id in seen:
                continue
            span = _span(brief, zone, now)
            if span is None or span[0].date() not in (today, tomorrow):
                continue
            seen.add(brief.event_id)
            candidates.append((span[0], span[1], alias, brief))
    candidates.sort(key=lambda item: (item[0], item[2]))
    target = parse_target(words)
    if target.kind == MATCH_NEXT:
        hits = [item for item in candidates if item[0] >= wall_now and (not target.tomorrow
                                                                         or item[0].date() == tomorrow)][:1]
        how = MATCH_NEXT
    elif target.kind == MATCH_TIME:
        day = tomorrow if target.tomorrow else today
        window = timedelta(minutes=MATCH_MINUTES)
        hits = [item for item in candidates if item[0].date() == day
                and any(abs(item[0] - datetime.combine(day, at)) <= window for at in target.times)]
        how = MATCH_TIME
    else:
        typed = target.words
        open_ones = [item for item in candidates
                     if item[1] > wall_now and (not target.tomorrow or item[0].date() == tomorrow)]
        every: list[tuple[datetime, datetime, str, EventBrief]] = []
        some: list[tuple[datetime, datetime, str, EventBrief]] = []
        for item in open_ones:
            title = _title_words(item[3].title)
            if not typed or not typed & title:
                continue
            (every if typed <= title else some).append(item)   # "meeting" / "call" count like any word (R5)
        hits, how = (every, MATCH_TITLE_ALL) if every else (some, MATCH_TITLE_SOME)
    hits = hits[:MAX_CHOICES]
    refs = tuple(EventRef(alias, brief.calendar_id, brief.event_id, brief.start or start)
                 for start, _end, alias, brief in hits)
    return MeetingMatch(refs, how, tuple((alias, brief) for _start, _end, alias, brief in hits))


def meeting_of(alias: str, brief: EventBrief, now: datetime, zone: str = "") -> PrepMeeting:
    """The meeting as read now, with its wall times in ``zone`` and its day word."""
    span = _span(brief, zone, now)
    wall_now = _wall(now, zone, now)
    if span is None:
        start = end = wall_now
    else:
        start, end = span
    return PrepMeeting(alias, brief, start, end, day_word(start.date(), wall_now.date()))


def choice_of(alias: str, brief: EventBrief, now: datetime, zone: str = "") -> PrepChoice:
    meeting = meeting_of(alias, brief, now, zone)
    return PrepChoice(meeting.ref, brief.title, meeting.start, meeting.end, meeting.day)


def find_event(briefs: Sequence[EventBrief], calendar_id: str, event_id: str) -> EventBrief | None:
    """The event read now with this calendar and id (None: gone or cancelled; cancelled events are
    never in a read)."""
    return next((brief for brief in briefs if brief.event_id == event_id and brief.calendar_id == calendar_id),
                None)


# --------------------------------------------------------------------------
# Whom to look up
# --------------------------------------------------------------------------

def prep_guests(brief: EventBrief, own: Collection[str], limit: int) -> list[GuestLookup]:
    """The meeting's guests in the order they are looked up: the organizer first (when it is not the
    owner), then accepted, tentative, no answer and declined (each in the calendar's order). The
    owner's own mailboxes (OWN), group addresses (GROUP) and declined guests (DECLINED) are never
    looked up; past ``limit`` the rest are NOT_SEARCHED; the others PENDING. Each mailbox once."""
    guests: list[Guest] = []
    seen: set[str] = set()

    def add(name: str, address: str, role: str, response: str) -> None:
        plain = email_address(address or "")
        if not plain or mailbox_key(plain) in seen:
            return
        seen.add(mailbox_key(plain))
        guests.append(Guest(" ".join(str(name or "").split()), plain, role, response or ""))

    organizer = "" if brief.organizer_self else brief.organizer_email
    if not organizer and not brief.organizer_self:
        organizer = next((guest.email for guest in brief.guests if guest.organizer), "")
    if organizer:
        entry = next((guest for guest in brief.guests if same_mailbox(guest.email, organizer)), None)
        add(brief.organizer_name or (entry.name if entry is not None else ""), organizer, ROLE_ORGANIZER,
            entry.response if entry is not None else "")
    for guest in sorted(brief.guests, key=lambda item: _RESPONSE_RANK.get(item.response or "", 2)):
        add(guest.name, guest.email, ROLE_GUEST, guest.response)
    lookups: list[GuestLookup] = []
    searched = 0
    for guest in guests:
        if _is_own(guest.address, own):
            lookups.append(GuestLookup(guest, OWN))
        elif guest.response == "declined":
            lookups.append(GuestLookup(guest, DECLINED, why=DECLINED_WHY))
        elif guest.address.rpartition("@")[0].casefold() in GROUP_LOCALS:
            lookups.append(GuestLookup(guest, GROUP, why=GROUP_WHY))
        elif searched >= max(0, int(limit)):
            lookups.append(GuestLookup(guest, NOT_SEARCHED, why=LIMIT_WHY.format(limit=int(limit))))
        else:
            lookups.append(GuestLookup(guest, PENDING))
            searched += 1
    return lookups


def lookback(days: int) -> int:
    """The prep lookback in days, 3..14 (R4)."""
    try:
        value = int(days)
    except (TypeError, ValueError):
        value = LOOKBACK_MAX
    return max(LOOKBACK_MIN, min(LOOKBACK_MAX, value))


def plain_address(address: str) -> str:
    """``address`` as a plain mailbox Jarvis may put in a search of his own, else "": one address
    (actions.email_address) whose local part has no Gmail search syntax ("{", "}", "(", ")", '"',
    "*", "=", ...), so it can never close or open a search group or name an operator."""
    plain = email_address(address or "")
    return plain if plain and _PLAIN_ADDRESS_RE.fullmatch(plain) else ""


def prep_query(address: str, days: int) -> str:
    """Jarvis's own search for one guest: '{from:"X" to:"X" cc:"X"} newer_than:14d' (any mail with
    them in the lookback), or 'from:"X" newer_than:14d' when that is over MAX_QUERY_CHARS (200). The
    address is quoted, so Gmail reads it as one value; an address that is not a plain mailbox
    (plain_address) raises QueryRefused: it never reaches a search."""
    plain = plain_address(address)
    if not plain:
        raise QueryRefused("The guest's address is not a plain mailbox")
    span = f"newer_than:{lookback(days)}d"
    full = f'{{from:"{plain}" to:"{plain}" cc:"{plain}"}} {span}'
    return full if len(full) <= MAX_QUERY_CHARS else f'from:"{plain}" {span}'


def guest_allowed(address: str, days: int) -> bool:
    """May Jarvis look this guest up at all (the index too, R2)? The address is a plain mailbox
    (plain_address: no search syntax that could widen the search), its search passes
    ask.mail.check_query (no sensitive word, security sender or link; plain characters) and the
    address is no account-security or payment sender, with **no** own domains: the follow-up list's
    exemption for the owner's own work domain never reaches prep."""
    if not plain_address(address):
        return False
    try:
        check_query(prep_query(address, days), None)
    except QueryRefused:
        return False
    return not is_security_sender(address)


def index_lookup(participants: Mapping[str, Sequence[ThreadRef]], address: str, alias: str, *,
                 other_accounts: bool) -> ThreadRef | None:
    """The header list's newest thread with this guest's mailbox on the meeting's account ``alias``;
    with ``other_accounts`` the newest on any account when there is none there (R3). Call it only
    after guest_allowed: the index is a shortcut, never a permission."""
    refs = list(participants.get(mailbox_key(address), ()) or ())
    mine = [ref for ref in refs if ref.alias == alias]
    pool = mine or (refs if other_accounts else [])
    if not pool:
        return None
    return max(pool, key=lambda ref: ref.last_at)


# --------------------------------------------------------------------------
# <people>
# --------------------------------------------------------------------------

def response_words(guest: Guest) -> str:
    """"organizer, accepted" / "accepted" / "maybe" / "no answer yet" / "declined"."""
    words = RESPONSE_WORDS.get(guest.response or "", "no answer yet")
    if guest.role == ROLE_ORGANIZER:
        return f"organizer, {words}" if guest.response else "organizer"
    return words


def lookup_words(lookup: GuestLookup, days: int, alias: str = "") -> str:
    """What became of one guest's lookup, in Jarvis's words ("no email in the last 14 days", "not
    searched (only 3 threads are read)", "the same thread as Ana Lima")."""
    status = lookup.status
    if status == FOUND:
        return "" if not lookup.alias or lookup.alias == alias else f"on {lookup.alias}"
    if status == NONE:
        return NO_MAIL.format(days=lookback(days))
    if status == SAME:
        return f"the same thread as {lookup.why}" if lookup.why else "the same thread"
    if status == REFUSED:
        return f"not searched ({REFUSED_WHY})"
    if status == PENDING:
        return f"not searched ({NOT_READ_WHY})"
    return f"not searched ({lookup.why or NOT_READ_WHY})"


def people_block(lookups: Sequence[GuestLookup], days: int, alias: str) -> str:
    """The <people> rows: "Ana Lima (ana@example.edu) | organizer | accepted | thread found: yes
    (work, last mail 2026-10-08)", "... | thread found: no (no email in the last 14 days)", "... |
    not searched (only 3 threads are read)". Never the owner's own mailboxes; at most
    MAX_PEOPLE_ROWS rows."""
    rows: list[str] = []
    shown = [lookup for lookup in lookups if lookup.status != OWN]
    for lookup in shown[:MAX_PEOPLE_ROWS]:
        guest = lookup.guest
        name = data(guest.name, 60)
        who = f"{name} ({guest.address})" if name else guest.address
        response = RESPONSE_WORDS.get(guest.response or "", "no answer yet")
        if guest.role == ROLE_ORGANIZER and not guest.response:
            response = "no answer listed"          # the calendar gives the organizer no guest entry
        if lookup.status == FOUND:
            when = f", last mail {lookup.last_at.date().isoformat()}" if lookup.last_at is not None else ""
            found = f"thread found: yes ({data(lookup.alias, 24)}{when})"
        elif lookup.status == SAME:
            found = f"thread found: yes ({data(lookup_words(lookup, days), 120)})"
        elif lookup.status == NONE:
            found = f"thread found: no ({NO_MAIL.format(days=lookback(days))})"
        else:
            found = data(lookup_words(lookup, days), 120)
        rows.append(" | ".join((who, guest.role, response, found)))
    if len(shown) > MAX_PEOPLE_ROWS:
        rows.append(f"[{len(shown) - MAX_PEOPLE_ROWS} more left out]")
    return "\n".join(rows) if rows else "none"


# --------------------------------------------------------------------------
# The run's answer
# --------------------------------------------------------------------------

def _one_line(text: str) -> str:
    return " ".join(data(text).split())


def _text(value: Any, cap: int) -> str | None:
    """A string of at most ``cap`` characters as one line; None when it is not one."""
    if not isinstance(value, str) or len(value) > cap:
        return None
    return _one_line(value)


def prep_answer_from(value: Any, *, fallback: bool = False) -> PrepAnswer | None:
    """structured_output (or the fallback's JSON) checked against prep_schema.json locally: the
    types, the caps and the waiting_on words; None when anything does not fit."""
    if not isinstance(value, dict):
        return None
    say = _text(value.get("say"), SAY_CAP)
    threads, open_items = value.get("threads"), value.get("open")
    points, lines = value.get("points"), value.get("lines")
    if say is None or not all(isinstance(item, list) for item in (threads, open_items, points, lines)):
        return None
    if len(threads) > THREADS_MAX or len(open_items) > OPEN_MAX or len(points) > POINTS_MAX \
            or len(lines) > LINES_MAX:
        return None
    kept_threads: list[tuple[str, str, str]] = []
    for item in threads:
        if not isinstance(item, dict):
            return None
        thread, summary, waiting = _text(item.get("thread"), THREAD_CAP), _text(item.get("summary"), SUMMARY_CAP), \
            item.get("waiting_on")
        if thread is None or summary is None or waiting not in WAITING_ON:
            return None
        kept_threads.append((thread, summary, waiting))
    kept_open: list[tuple[str, str]] = []
    for item in open_items:
        if not isinstance(item, dict):
            return None
        text, thread = _text(item.get("text"), OPEN_CAP), _text(item.get("thread"), THREAD_CAP)
        if text is None or thread is None:
            return None
        kept_open.append((text, thread))
    kept_points: list[str] = []
    for item in points:
        text = _text(item, POINT_CAP)
        if text is None:
            return None
        kept_points.append(text)
    if not all(isinstance(line, str) and len(line) <= LINE_CAP for line in lines):
        return None
    kept_lines = tuple(line.strip() for line in lines if line.strip())
    return PrepAnswer(say, tuple(kept_threads), tuple(kept_open), tuple(kept_points), kept_lines, fallback)


def read_prep_result(event: Mapping[str, Any], init: InitSummary | None, *,
                     now: Callable[[], datetime] = lambda: datetime.now().astimezone()) -> PrepAnswer | AskFailure:
    """The result event of a prep run -> PrepAnswer, or AskFailure: stream.result_failure first (a
    limit, a sign-in, schema retries, no checked init ... as for an Ask), then the structured
    output checked locally (GARBLED "shape" when it does not fit)."""
    found = result_failure(event, init, now=now)
    if found is not None:
        return found
    assert init is not None
    value = event.get("structured_output")
    if value is not None:
        answer = prep_answer_from(value)
        return answer if answer is not None else failure(GARBLED, "shape")
    if init.structured_tool:
        return failure(GARBLED, "no_structured_output")
    text = event.get("result") if isinstance(event.get("result"), str) else ""
    fenced = _FENCE_RE.match(text or "")
    try:
        parsed = json.loads(fenced.group(1) if fenced else text)
    except ValueError:
        return failure(GARBLED, "fallback")
    answer = prep_answer_from(parsed, fallback=True)
    return answer if answer is not None else failure(GARBLED, "fallback")


# --------------------------------------------------------------------------
# Checks: the reply lines and the brief's items
# --------------------------------------------------------------------------

def thread_people(thread: MailThread) -> frozenset[str]:
    """Everyone a thread's messages show (sender, Reply-To, To, Cc), casefolded."""
    return frozenset(person.email.casefold() for message in thread.messages for person in message.people()
                     if person.email)


def mail_lines(thread: MailThread, own: Collection[str] = ()) -> tuple[str, ...]:
    """The lines of a thread's messages that a draft to another thread must not repeat: each
    message's subject and text lines, but not the owner's own messages (``own`` addresses: his
    sign-off is his to reuse)."""
    lines: list[str] = []
    for message in thread.messages:
        sender = message.sender.email if message.sender is not None else ""
        if sender and _is_own(sender, own):
            continue
        lines += [line for line in (message.subject, *message.text.splitlines()) if line.strip()]
    return tuple(lines)


def mail_leak(text: str, lines: Collection[str]) -> bool:
    """``text`` (a draft's subject or body) repeats one of ``lines`` (another thread's mail): a whole
    line of at least LEAK_LINE_CHARS characters and LEAK_LINE_WORDS words, or LEAK_RUN_WORDS of a
    line's words in a row. Stricter than memory.note_leak on purpose: mail shares stock phrases
    ("best regards", "thanks for your help"), and a hold is information only."""
    flat = " ".join(str(text or "").casefold().split())
    words = _WORD_RE.findall(flat)
    runs = {tuple(words[start:start + LEAK_RUN_WORDS]) for start in range(len(words) - LEAK_RUN_WORDS + 1)}
    for line in lines:
        whole = " ".join(str(line or "").casefold().split()).rstrip(".!?")
        line_words = _WORD_RE.findall(whole)
        if len(whole) >= LEAK_LINE_CHARS and len(line_words) >= LEAK_LINE_WORDS and whole in flat:
            return True
        if any(tuple(line_words[start:start + LEAK_RUN_WORDS]) in runs
               for start in range(len(line_words) - LEAK_RUN_WORDS + 1)):
            return True
    return False


class PrepCheck:
    """validate.to_cards' ``extra`` for a brief's Reply lines (spec 8.2): from the meeting's account
    (REPLY_ACCOUNT), only to people already in that thread (EXTRA_PEOPLE), never repeating one of
    the owner's notes in its subject or body (NOTE_LEAK, R7; prep has no owner's words), and never
    repeating mail from another thread read for the brief (MAIL_LEAK: the other guests' threads are
    not the owner's to share, mail_leak). The thread itself must be one Jarvis read (validate's
    UNKNOWN_THREAD comes first)."""

    def __init__(self, alias: str, threads: Sequence[MailThread], notes: Sequence[str] = (),
                 own: Collection[str] = ()) -> None:
        self.alias = alias
        self.people = {(thread.account, thread.thread_id): thread_people(thread) for thread in threads}
        self.lines = {(thread.account, thread.thread_id): mail_lines(thread, own) for thread in threads}
        self.notes = tuple(notes)

    def __call__(self, card: ProposedAction) -> str:
        if card.kind != REPLY:
            return ONLY_REPLIES
        if card.account != self.alias:
            return REPLY_ACCOUNT
        key = (card.account, card.field("thread"))
        people = self.people.get(key)
        if people is None:
            return EXTRA_PEOPLE
        if any(address.casefold() not in people for address in card.mail_recipients()):
            return EXTRA_PEOPLE
        texts = (card.field("subject"), card.body)
        if self.notes:
            from ..memory import note_leak

            if any(note_leak(text, self.notes) for text in texts):
                return NOTE_LEAK
        # Only lines the card's own thread does not hold too: repeating those reveals nothing new.
        mine = {" ".join(line.casefold().split()) for line in self.lines.get(key, ())}
        others = [line for other, lines in self.lines.items() if other != key
                  for line in lines if " ".join(line.casefold().split()) not in mine]
        if others and any(mail_leak(text, others) for text in texts):
            return MAIL_LEAK
        return ""


def _thread_key(value: str) -> str:
    return _THREAD_KEY_RE.sub("", str(value or "")).strip()


def _short_date(moment: datetime | None, zone: str = "") -> str:
    """"Oct 8" in ``zone`` (the calendar's, as the rest of the brief), else in this PC's zone."""
    if moment is None:
        return ""
    local = _wall(moment, zone) if zone else (moment.astimezone() if moment.tzinfo is not None else moment)
    return f"{_MONTHS[local.month - 1]} {local.day}"


def thread_label(thread: MailThread, zone: str = "") -> str:
    """'"Budget numbers", Oct 8' from Jarvis's own headers (the newest message's date, in ``zone``)."""
    subject = data(thread.subject, 120) or "(no subject)"
    newest = thread.messages[-1].sent_at if thread.messages else None
    when = _short_date(newest, zone)
    return f'"{subject}", {when}' if when else f'"{subject}"'


def _who(lookups: Sequence[GuestLookup]) -> tuple[tuple[str, str], ...]:
    return tuple((lookup.guest.display, response_words(lookup.guest)) for lookup in lookups
                 if lookup.status != OWN)


def _missing(lookups: Sequence[GuestLookup], days: int, alias: str,
             found: Collection[str] = ()) -> tuple[tuple[str, str], ...]:
    rows = []
    for lookup in lookups:
        if lookup.status == OWN or (lookup.status == FOUND and lookup.thread_id in found):
            continue
        words = lookup_words(lookup, days, alias)
        if lookup.status == FOUND:
            words = f"not searched ({UNREAD_WHY})"
        rows.append((lookup.guest.display, words))
    return tuple(rows)


def guest_notes(notes: Sequence[str], lookups: Sequence[GuestLookup]) -> tuple[str, ...]:
    """The owner's notes that name a guest (a word of the guest's name of >= 3 letters, or the
    address), matched here on this PC."""
    names: set[str] = set()
    addresses: set[str] = set()
    for lookup in lookups:
        if lookup.status == OWN:
            continue
        names |= _title_words(lookup.guest.name)
        addresses.add(lookup.guest.address.casefold())
    kept = []
    for note in notes:
        text = str(note or "")
        words = set(_WORD_RE.findall(text.casefold()))
        if words & names or any(address in text.casefold() for address in addresses):
            kept.append(data(text, 200))
    return tuple(note for note in kept if note)


def _base(meeting: PrepMeeting, lookups: Sequence[GuestLookup], memory: Sequence[str]) -> dict[str, Any]:
    return {"meeting": meeting.ref, "title": data(meeting.brief.title, 120) or "(No title)", "alias": meeting.alias,
            "start": meeting.start, "end": meeting.end, "day": meeting.day, "who": _who(lookups),
            "notes": guest_notes(memory, lookups), "series_id": getattr(meeting.brief, "series_id", "")}


def check_prep(answer: PrepAnswer, meeting: PrepMeeting, lookups: Sequence[GuestLookup],
               threads: Sequence[MailThread], memory: Sequence[str], *, days: int = LOOKBACK_MAX,
               zone: str = "") -> PrepBrief:
    """The brief of a run (spec 8.2): a thread summary only for a thread Jarvis read for this brief
    (once; its subject and date from Jarvis's headers, the date in ``zone`` like the meeting's times,
    its who the guest whose lookup found it); an open item only with "" or such a thread; points as
    text. Every text one line through context.data() and capped; what does not fit is dropped and
    counted."""
    read = {thread.thread_id: thread for thread in threads}
    finder = {lookup.thread_id: lookup.guest.display for lookup in lookups if lookup.status == FOUND}
    summaries: dict[str, tuple[str, str]] = {}
    dropped = 0
    for thread_id, summary, waiting in answer.threads:
        key = _thread_key(thread_id)
        text = data(summary, SUMMARY_CAP)
        if key not in read or key in summaries or not text:
            dropped += 1
            continue
        summaries[key] = (text, waiting)
    rows = []
    for thread in threads:
        summary, waiting = summaries.get(thread.thread_id, ("", "nobody"))
        rows.append((finder.get(thread.thread_id, ""), thread_label(thread, zone), summary, waiting))
    open_items: list[str] = []
    for text, thread_id in answer.open:
        key = _thread_key(thread_id)
        shown = data(text, OPEN_CAP)
        if (key and key not in read) or not shown:
            dropped += 1
            continue
        open_items.append(shown)
    points = [shown for shown in (data(point, POINT_CAP) for point in answer.points) if shown]
    dropped += len(answer.points) - len(points)
    return PrepBrief(**_base(meeting, lookups, memory), threads=tuple(rows),
                     missing=_missing(lookups, days, meeting.alias, read), open=tuple(open_items),
                     points=tuple(points), dropped=dropped, ran=True)


def deterministic_brief(meeting: PrepMeeting, lookups: Sequence[GuestLookup],
                        briefing_pending: Sequence[ProposedAction], memory: Sequence[str], why: str = "", *,
                        days: int = LOOKBACK_MAX) -> PrepBrief:
    """A brief without a planner run (G4): who is coming and their answers, what became of each
    guest's lookup ("no email in the last 14 days", "not searched (...)"), the briefing's cards still
    waiting for a decision whose recipients include a guest (their titles), the owner's notes that
    name a guest, and ``why`` (why no mail was read, "" when none was found)."""
    guests = [lookup.guest.address for lookup in lookups if lookup.status != OWN]
    open_items = []
    for card in briefing_pending:
        recipients = card.mail_recipients() if hasattr(card, "mail_recipients") else ()
        if any(same_mailbox(address, guest) for address in recipients for guest in guests):
            title = data(card.title or card.raw, 120)
            if title:
                open_items.append(f"{title} (waiting under NEEDS YOUR OK)")
    return PrepBrief(**_base(meeting, lookups, memory), missing=_missing(lookups, days, meeting.alias),
                     open=tuple(open_items), ran=False, note=data(why, 300))


# --------------------------------------------------------------------------
# The entry
# --------------------------------------------------------------------------

DOT = "\u00b7"


def brief_text(brief: PrepBrief, hour24: bool = False) -> str:
    """The brief as the JARVIS tab shows it (plain text, never cut, never spoken or logged):

        Brief: Project sync - today 2:00-2:30 PM (work)
        Who: Ana Lima (organizer, accepted) . Ben Ode (accepted)
        Last threads:
        - Ana Lima - "Budget numbers", Oct 8: she asked for the Q3 numbers. Waiting on you.
        - Ben Ode - no email in the last 14 days.
        Still open: / Points to raise: / From your notes: (each "- ..." line)
    """
    lines = [f"Brief: {brief.title} - {brief.when(hour24)} ({brief.alias})"]
    if brief.who:
        lines.append("Who: " + f" {DOT} ".join(f"{name} ({words})" for name, words in brief.who))
    if brief.note:
        note = brief.note[:1].upper() + brief.note[1:]
        lines.append(note if note.endswith((".", "!", "?", ")")) else note + ".")
    if brief.threads or brief.missing:
        lines.append("Last threads:")
        for who, label, summary, waiting in brief.threads:
            text = f"- {who} - {label}" if who else f"- {label}"
            if summary:
                text += f": {summary.rstrip('.')}."
                tail = WAITING_WORDS.get(waiting, "")
                if tail:
                    text += f" {tail}"
            else:
                text += "."
            lines.append(text)
        for who, why in brief.missing:
            lines.append(f"- {who} - {why}.")
    for heading, items in (("Still open:", brief.open), ("Points to raise:", brief.points),
                           ("From your notes:", brief.notes)):
        if items:
            lines.append(heading)
            lines += [f"- {item}" for item in items]
    return "\n".join(lines)
