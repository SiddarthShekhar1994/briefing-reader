"""The LIVE view's pictures, as plain data: what hud draws next to a step's text (Qt-free).

    Picture                           one picture of a LIVE step: its kind (KIND_*), caption, state
                                      (live.PICTURE_*), a one-line note, an accessible description and
                                      the data of its kind (MailPicture / DayPicture / WeekPicture /
                                      PagePicture); ``cost`` (bytes as counted against the LiveLimits
                                      picture caps), ``without_text()``, ``dropped(reason)``
    email_picture(thread, ...)        an email thread Ask read (MAIL_THREAD): the newest message as a mail
                                      client shows it, the older ones as short lines
    outgoing_picture(view, account=)  what a Reply / Email card sends (ACTION_PAYLOAD), exactly as built
    day_change_picture(action, ...)   a calendar card's change on its day (Calendar add, To-do block,
                                      Move, Cancel, RSVP), with the day's other events only when Jarvis
                                      had already read that day (KnownDays) - never a Google call for it
    week_picture(events, start, end, ...)   the 7 days of an Ask's calendar read (CALENDAR_READ)
    plan_picture(events, blocks, day=, ...)  a day plan (PLAN_VALIDATE): the day as Jarvis read it and
                                      the proposed blocks, stamped PROPOSED (nothing added yet)
    search_picture(query, hits, ...)  one web search of a research run (WEB_SEARCH) drawn as a compact
                                      results page: the exact query in a search box, then each result's
                                      title and address (and snippet, when the search gave one)
    page_pending / page_ready / page_unavailable   a page snapshot of a page a web research read
                                      (WEB_FETCH) or cited (WEB_SOURCE)
    stamp(picture, status, status_text)     the stamp on an outgoing / calendar picture ("WILL BE SENT",
                                            "SENT", "UNDONE - NOT SENT" ...) and its tone
    KnownDays                         the days Jarvis already read (today's agenda, an Ask's calendar
                                      read): memory only, GUI thread
    CalendarRead                      what one Ask read from the calendars (AskOutcome.calendar_read)

Rules: every string in a picture went through live.clean_line / live.clean_block (secrets redacted,
control, bidi and invisible characters shown as "<U+202E>"), exactly like the LIVE text. Email,
calendar and search pictures are drawn by Jarvis from the plain text and times he already has: never
email HTML, never a remote image or font, nothing opened for them, nothing clickable. Pictures are
memory only: nothing here writes a file, logs, or touches the network; repr() of every object names
kinds, states and sizes only.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any
from urllib.parse import urlsplit

from . import live
from .actions import (
    CALENDAR,
    CANCEL,
    EVENT_KINDS,
    IDN_WARNING,
    MAIL_KINDS,
    MOVE,
    RSVP,
    TODO,
    idn_host,
    link_host,
    stated_when,
)
from .config import DEFAULT_ACCOUNT
from .text_prep import clock_parts

# --------------------------------------------------------------------------
# Kinds, captions, caps
# --------------------------------------------------------------------------

KIND_EMAIL = "email"        # a thread Ask read (MAIL_THREAD)
KIND_OUTGOING = "outgoing"  # what a Reply / Email card sends (ACTION_PAYLOAD)
KIND_DAY = "day"            # a calendar card's change on its day (ACTION_PAYLOAD)
KIND_WEEK = "week"          # Ask's calendar read (CALENDAR_READ)
KIND_PAGE = "page"          # a page snapshot of a page a web research read (WEB_FETCH) or cited (WEB_SOURCE)
KIND_SEARCH = "search"      # one web search of a research run, drawn as a results page (WEB_SEARCH)
KINDS = (KIND_EMAIL, KIND_OUTGOING, KIND_DAY, KIND_WEEK, KIND_PAGE, KIND_SEARCH)
# Drawn by Jarvis on this PC from what he already has ([live] pictures); a page is a snapshot instead.
MODEL_KINDS = frozenset({KIND_EMAIL, KIND_OUTGOING, KIND_DAY, KIND_WEEK, KIND_SEARCH})

CAPTION_EMAIL = "Email"
CAPTION_DAY = "Calendar change"
CAPTION_WEEK = "Calendar"
CAPTION_SEARCH = "Web search"
CAPTION_PLAN = "Your day plan"
CAPTION_PLAN_DAY = "Your day as read"       # a plan's day with no proposed block on it (the day read)

BODY_CAP = 6_000        # characters of a body drawn; the rest is counted as cut
OLDER_SHOWN = 5         # older messages listed under the newest
SNIPPET_CAP = 140       # one older message's line
HEAD_CAP = 300          # one header line (From / Subject; an email Ask read: To / Cc with "+K more")
EVENT_TITLE_CAP = 120
WEEK_COLUMNS = 7
WEEK_TITLES = 4         # titles kept per day of the week strip (for the enlarged view)
WEEK_TITLE_CAP = 60
DAY_OTHERS_CAP = 40     # other events drawn on one day picture ("+N more" beyond)
ALT_CAP = 300           # accessible description
URL_CAP = 2_048
QUERY_CAP = 500         # a search's query as drawn (WebSearch queries are short)
SEARCH_HITS_SHOWN = 10  # results drawn of one search ("+N more" beyond)
SEARCH_TITLE_CAP = 160  # one result's title
SEARCH_URL_CAP = 300    # one result's address line (plain text, never a link)
SEARCH_SNIPPET_CAP = 300   # one result's snippet (only when the search gave one)
SEARCH_DOMAINS_CAP = 200   # "Only these sites" / "Not these sites"

NOTE_EMAIL = "Drawn by Jarvis from the email's text - no images, no links"
NOTE_OUTGOING = "Exactly what will be sent, drawn by Jarvis"
NOTE_WEEK = "Drawn from the calendar Jarvis just read"
NOTE_DAY = "Drawn by Jarvis from the card and what he already read - no call to Google for it"
NOTE_SEARCH = "Drawn by Jarvis from what the search returned - no page opened, nothing clickable"
NOTE_PLAN = "Drawn by Jarvis from the calendar he just read and the proposed blocks - nothing added yet"
NOTE_PLAN_DAY = "Drawn by Jarvis from the calendar he just read"
SEARCH_EMPTY = "No pages listed"
SEARCH_UNREAD = "The results came back as text Jarvis could not list"
# A page snapshot (Build B's snapshots.py uses these words too).
NOTE_OPENING = "Opening it on this PC in a private window (no sign-ins) to show you..."
NOTE_OPENED = "Opened on this PC in a private window (no sign-ins) to show you"
UNAVAILABLE_PREFIX = "Picture unavailable: "
DROPPED_PREFIX = "Picture not kept: "
NO_TITLE = "(No title)"
NO_SUBJECT = "(no subject)"
GMAIL_FILLS = "(Gmail fills it in)"

CHANGE_ADD = "add"
CHANGE_TODO = "todo"
CHANGE_MOVE = "move"
CHANGE_CANCEL = "cancel"
CHANGE_RSVP = "rsvp"
CHANGE_PLAN = "plan"     # a day plan: the day as read, the proposed blocks (nothing added yet)
CHANGES = (CHANGE_ADD, CHANGE_TODO, CHANGE_MOVE, CHANGE_CANCEL, CHANGE_RSVP, CHANGE_PLAN)
ROLE_OTHER = "other"     # another event that day, as Jarvis last read it
ROLE_BEFORE = "before"   # Move: where it is now (ghost)
ROLE_AFTER = "after"     # Move: the new time
ROLE_NEW = "new"         # Calendar add / To-do block: highlighted
ROLE_CANCEL = "cancel"   # Cancel: struck through
ROLE_RSVP = "rsvp"       # RSVP: the event with the answer chip

# stamp() tones
TONE_WILL = "will"        # amber: what will happen when the countdown runs out
TONE_DOING = "doing"      # cyan: on its way
TONE_DONE = "done"        # green
TONE_FAILED = "failed"    # red
TONE_UNDONE = "undone"    # dim
TONE_CHECK = "check"      # amber: look at it

DAY_START_MIN = 8 * 60    # a day picture shows at least 8:00 ...
DAY_END_MIN = 18 * 60     # ... to 18:00
DAY_MARGIN_MIN = 30       # and every block +- this much
_DAY_MINUTES = 24 * 60

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_ANSWER_CHIPS = {"yes": "YES", "no": "NO", "maybe": "MAYBE"}


def page_caption(host: str) -> str:
    """"Page: example.org"."""
    return f"Page: {_line(host, 120) or 'unknown site'}"


def _line(text: Any, cap: int) -> str:
    return live.clean_line(text, cap)


# --------------------------------------------------------------------------
# Data (frozen; every string cleaned; repr names no content)
# --------------------------------------------------------------------------

@dataclass(frozen=True, repr=False)
class MailHead:
    """One message's header as a mail client shows it (clean lines)."""

    sender: str            # "Ana Lima <ana@example.com>"; outgoing: the From address or "(Gmail fills it in)"
    to: str                # "Sam Ortiz <sam@example.com>, Lee Park <lee@example.com>, +3 more"
    cc: str                # "" = none
    when: str              # "Fri Oct 9 2026, 2:41 PM"; "" = unknown / not sent yet
    subject: str           # "(no subject)" when empty

    def __repr__(self) -> str:
        return "<MailHead>"


@dataclass(frozen=True, repr=False)
class MailLine:
    """One older message of a thread, one line."""

    sender: str            # name, else address
    when: str              # "Oct 8, 9:12 AM"
    snippet: str           # its first SNIPPET_CAP characters, one clean line ("" with [live] text = false)

    def __repr__(self) -> str:
        return "<MailLine>"


@dataclass(frozen=True, repr=False)
class MailPicture:
    outgoing: bool
    account: str           # the alias read from / sent from ("work")
    head: MailHead
    body: str              # live.clean_block text (escapes visible), <= BODY_CAP; "" when not kept
    body_chars: int        # characters of the original body
    body_cut: int          # characters left out at the end (BODY_CAP)
    body_kept: bool = True # False: [live] text = false
    reply_line: str = ""   # outgoing: "Reply in its Gmail thread - In-Reply-To <abc@example.com>" / "New email ..."
    older: tuple[MailLine, ...] = ()   # incoming: older messages, newest first, at most OLDER_SHOWN
    older_more: int = 0                # older messages not listed (beyond OLDER_SHOWN + the thread's left_out)
    shortened: bool = False            # the reader already shortened the text (it ends " [...]")

    def __repr__(self) -> str:
        return (f"<MailPicture {'outgoing' if self.outgoing else 'incoming'}: {self.body_chars} chars, "
                f"{len(self.older)} older>")


@dataclass(frozen=True, repr=False)
class DayBlock:
    column: int            # 0, or 1 for the second day of a Move to another day
    start_min: int         # minutes after its column's midnight (0..1440); 0 for all-day / timeless
    end_min: int
    title: str             # clean line <= EVENT_TITLE_CAP ("(No title)")
    time_text: str         # "2:00-3:00 PM" / "all day" / "time not known"; " - repeats weekly" appended
    role: str              # ROLE_*
    all_day: bool = False
    timeless: bool = False # Jarvis knows no time for it (drawn in the row above the hours)
    chip: str = ""         # RSVP: "YES" / "NO" / "MAYBE"
    lane: int = 0          # overlapping timed blocks side by side (per column)
    lanes: int = 1

    def __repr__(self) -> str:
        return f"<DayBlock {self.role} col={self.column}>"


@dataclass(frozen=True, repr=False)
class DayPicture:
    change: str            # CHANGE_*
    headline: str          # "Move: Project sync" / "Add: Dentist" / "To-do block: Lab report" / "Cancel: ..."
    account: str
    days: tuple[str, ...]  # column headers "Fri Oct 9" (two for a Move to another day)
    first_min: int         # whole hours drawn: 08:00-18:00 at least, widened to every block +- 30 min
    last_min: int
    blocks: tuple[DayBlock, ...]
    others_shown: bool
    others_note: str       # "Other events as Jarvis read them at 2:41 PM (today's agenda)" / "Only this ..."
    others_more: int = 0
    hour24: bool = False
    now_min: int = -1      # the current time on today's column (-1: no column is today)
    now_column: int = -1

    def __repr__(self) -> str:
        return f"<DayPicture {self.change}: {len(self.days)} day(s), {len(self.blocks)} block(s)>"


@dataclass(frozen=True, repr=False)
class WeekColumn:
    label: str             # "Thu 8"
    today: bool
    blocks: tuple[tuple[int, int], ...]   # (start_min, end_min) of its timed events
    all_day: int
    titles: tuple[str, ...]               # up to WEEK_TITLES titles (<= WEEK_TITLE_CAP) for the enlarged view
    count: int

    def __repr__(self) -> str:
        return f"<WeekColumn {self.count} event(s)>"


@dataclass(frozen=True, repr=False)
class WeekPicture:
    columns: tuple[WeekColumn, ...]       # WEEK_COLUMNS days from today (inside the read window)
    first_min: int
    last_min: int
    accounts: tuple[str, ...]
    total: int                            # events read in the whole window
    later: int                            # of them after the last column
    hour24: bool = False

    def __repr__(self) -> str:
        return f"<WeekPicture {len(self.columns)} day(s), {self.total} event(s)>"


@dataclass(frozen=True, repr=False)
class PagePicture:
    url: str               # as asked (clean line, plain text, never a link)
    host: str              # "example.org"
    final_host: str = ""   # "" = the page stayed on its host
    image: bytes = b""     # JPEG (PNG when JPEG cannot be written); READY only
    image_format: str = "JPG"
    width: int = 0         # image pixels (<= 960 wide)
    height: int = 0
    took_ms: int = 0
    idn: bool = False      # an "xn--" host: the note carries actions.IDN_WARNING
    still_loading: bool = False   # taken at the time limit while the page still loaded
    blocked: int = 0       # requests of the page Jarvis blocked (local / not https)

    def __repr__(self) -> str:
        return f"<PagePicture {len(self.image)} bytes, {self.width}x{self.height}>"


@dataclass(frozen=True, repr=False)
class SearchHit:
    """One result of a web search as a results page lists it (clean lines; plain text, never a link)."""

    title: str             # "(no title)" when the search gave none
    url: str               # the address as listed (<= SEARCH_URL_CAP)
    host: str              # "www.example.org" ("" when the address names none)
    snippet: str = ""      # only when the search gave one ("" with [live] text = false)
    site: str = ""         # its address line's first part: the host ("http://" + host for plain http, ":port"
                           # when it names one); "" when the address names no host (the url is drawn instead)
    path: str = ""         # the rest of the address after the site ("/admissions?term=spring"; "" for "/")

    def __repr__(self) -> str:
        return "<SearchHit>"


@dataclass(frozen=True, repr=False)
class SearchPicture:
    """One web search of a research run: the exact query and what it listed, in order."""

    query: str                         # the exact query (clean line <= QUERY_CAP)
    hits: tuple[SearchHit, ...] = ()   # at most SEARCH_HITS_SHOWN, in the search's order
    more: int = 0                      # results listed but not drawn
    only: str = ""                     # "example.org, example.net" (the search's allowed sites)
    never: str = ""                    # its blocked sites
    unread: bool = False               # the results came back as text Jarvis could not list

    def __repr__(self) -> str:
        return f"<SearchPicture {len(self.hits)} result(s), {self.more} more>"


@dataclass(frozen=True, repr=False)
class Picture:
    kind: str              # KIND_*
    caption: str           # "Email" / "Calendar change" / "Calendar" / "Web search" / "Page: example.org"
    state: str = live.PICTURE_READY
    note: str = ""         # one clean line under it (what happened; "Picture unavailable: <reason>")
    alt: str = ""          # accessible description (clean line <= ALT_CAP)
    mail: MailPicture | None = None
    day: DayPicture | None = None
    week: WeekPicture | None = None
    page: PagePicture | None = None
    search: SearchPicture | None = None

    @property
    def cost(self) -> int:
        """Bytes it holds as counted against the LiveLimits picture caps: the image's bytes, two per
        character of its strings, and 256 for the rest."""
        image = len(self.page.image) if self.page is not None else 0
        return image + 2 * _picture_chars(self) + 256

    def without_text(self) -> Picture:
        """[live] text = false: an email keeps its headers, not its text (its size stays known); a
        search keeps its query, titles and addresses (the step lists them too), not its snippets."""
        if self.search is not None:
            hits = tuple(replace(hit, snippet="") for hit in self.search.hits)
            return replace(self, search=replace(self.search, hits=hits))
        if self.mail is None:
            return self
        older = tuple(replace(line, snippet="") for line in self.mail.older)
        return replace(self, mail=replace(self.mail, body="", body_cut=0, body_kept=False, older=older))

    def dropped(self, reason: str) -> Picture:
        """Not kept: state DROPPED, every bit of its data released, the note says why."""
        return Picture(self.kind, self.caption, live.PICTURE_DROPPED, note=_line(DROPPED_PREFIX + reason, 300),
                       alt=self.alt)

    def __repr__(self) -> str:
        return f"Picture(kind={self.kind!r}, state={self.state!r}, cost={self.cost})"


def _picture_chars(picture: Picture) -> int:
    total = len(picture.caption) + len(picture.note) + len(picture.alt)
    mail = picture.mail
    if mail is not None:
        head = mail.head
        total += (len(mail.account) + len(head.sender) + len(head.to) + len(head.cc) + len(head.when)
                  + len(head.subject) + len(mail.body) + len(mail.reply_line)
                  + sum(len(line.sender) + len(line.when) + len(line.snippet) for line in mail.older))
    day = picture.day
    if day is not None:
        total += (len(day.headline) + len(day.account) + sum(len(text) for text in day.days) + len(day.others_note)
                  + sum(len(block.title) + len(block.time_text) + len(block.chip) for block in day.blocks))
    week = picture.week
    if week is not None:
        total += sum(len(column.label) + sum(len(title) for title in column.titles) + 8 * len(column.blocks)
                     for column in week.columns) + sum(len(alias) for alias in week.accounts)
    page = picture.page
    if page is not None:
        total += len(page.url) + len(page.host) + len(page.final_host)
    search = picture.search
    if search is not None:
        total += (len(search.query) + len(search.only) + len(search.never)
                  + sum(len(hit.title) + len(hit.url) + len(hit.host) + len(hit.snippet) + len(hit.site)
                        + len(hit.path) for hit in search.hits))
    return total


@dataclass(frozen=True, repr=False)
class CalendarRead:
    """What one Ask read from the calendars (AskOutcome.calendar_read; memory only, never logged)."""

    start: datetime
    end: datetime
    events: tuple[tuple[str, tuple[Any, ...]], ...]   # (alias, EventBriefs) of the accounts read
    at: datetime

    def __repr__(self) -> str:
        return f"<CalendarRead {len(self.events)} account(s), {sum(len(found) for _a, found in self.events)} event(s)>"


# --------------------------------------------------------------------------
# Times and words
# --------------------------------------------------------------------------

def _has_machine_offset(now: datetime) -> bool:
    """True when aware ``now`` has the offset this PC's time zone has at that wall time
    (datetime.now().astimezone(), as the app's clocks give it)."""
    try:
        return now.replace(tzinfo=None).astimezone().utcoffset() == now.utcoffset()
    except (OverflowError, OSError, ValueError):
        return False


def _zone(now: datetime | None) -> tzinfo | None:
    """The zone to show this PC's times in (the time now, when a read was made): None = this PC's
    own rules, daylight saving changes included, when ``now`` is naive or carries this PC's offset
    (agenda._wall's rule); else ``now``'s own (fixed) zone."""
    if now is None or now.utcoffset() is None or _has_machine_offset(now):
        return None
    return now.tzinfo


def _wall(moment: Any, zone: tzinfo | None) -> datetime | None:
    """``moment`` as a naive wall time on this PC (aware times converted to ``zone``, or with this
    PC's own rules for that moment; naive ones are wall times already). For the time now and read
    times; an event's times from Google go through _event_wall."""
    if not isinstance(moment, datetime):
        return None
    if moment.utcoffset() is None:
        return moment.replace(tzinfo=None)
    converted = moment.astimezone(zone) if zone is not None else moment.astimezone()
    return converted.replace(tzinfo=None)


def _event_wall(moment: Any) -> datetime | None:
    """An event's time as the wall time it carries: Google gives events in the calendar's own time
    zone (and with that day's offset, daylight saving included), the frame of a card's naive times,
    of what the executor sends and of the card's check line."""
    if not isinstance(moment, datetime):
        return None
    return moment.replace(tzinfo=None)


def _foreign(moment: Any, zone: tzinfo | None) -> bool:
    """An aware time whose offset is not the one this PC's frame (``zone``, else this PC's rules)
    has at that moment: the calendar is in another time zone (its "now" would be misplaced)."""
    if not isinstance(moment, datetime) or moment.utcoffset() is None:
        return False
    try:
        local = moment.astimezone(zone) if zone is not None else moment.astimezone()
        return local.utcoffset() != moment.utcoffset()
    except (OverflowError, OSError, ValueError):
        return False


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day)


def day_label(day: date, today: date | None = None) -> str:
    """"Fri Oct 9" ("Fri Jan 8 2027" in another year than ``today``'s)."""
    text = f"{_WEEKDAYS[day.weekday()]} {_MONTHS[day.month - 1]} {day.day}"
    return f"{text} {day.year}" if today is not None and day.year != today.year else text


def _clock(moment: datetime, hour24: bool) -> str:
    digits, meridiem = clock_parts(moment, hour24=hour24)
    return f"{digits} {meridiem}" if meridiem else digits


def time_range(start: datetime, end: datetime | None, hour24: bool = False) -> str:
    """"2:00-3:00 PM", "11:00 AM-1:00 PM", "14:00-15:00"; "2:00 PM" without an end."""
    if end is None or end <= start:
        return _clock(start, hour24)
    first, first_meridiem = clock_parts(start, hour24=hour24)
    last, last_meridiem = clock_parts(end, hour24=hour24)
    if hour24:
        return f"{first}-{last}"
    if first_meridiem == last_meridiem and end - start < timedelta(hours=12):
        return f"{first}-{last} {last_meridiem}"
    return f"{first} {first_meridiem}-{last} {last_meridiem}"


def _person(person: Any) -> str:
    if person is None:
        return ""
    name = " ".join(str(getattr(person, "name", "") or "").split())
    address = str(getattr(person, "email", "") or "")
    return f"{name} <{address}>" if name and address else (name or address)


def _people(people: Sequence[Any], more: int, cap: int = HEAD_CAP) -> str:
    """The people of a To / Cc line, joined, within ``cap`` characters: as many whole people as fit
    with room for ", +K more", where K counts everyone not listed (``more`` too), so the count is
    never cut off. Each person is a clean line already (the line is not cut again)."""
    names = [found for found in (_line(_person(person), cap) for person in people) if found]
    more = max(0, int(more or 0))
    shown: list[str] = []
    for index, name in enumerate(names):
        hidden = len(names) - index - 1 + more
        suffix = f", +{hidden} more" if hidden else ""
        if len(", ".join([*shown, name])) + len(suffix) > cap:
            break
        shown.append(name)
    hidden = len(names) - len(shown) + more
    if not shown and names:   # one person longer than the line: cut, the count after it
        suffix = f", +{hidden - 1} more" if hidden > 1 else ""
        shown, hidden = [_line(names[0], max(8, cap - len(suffix)))], hidden - 1
    text = ", ".join(shown)
    if hidden:
        text = f"{text}, +{hidden} more" if text else f"+{hidden} more"
    return text


def _mail_when(moment: Any, zone: tzinfo | None, hour24: bool) -> str:
    wall = _wall(moment, zone)
    if wall is None:
        return ""
    return f"{_WEEKDAYS[wall.weekday()]} {_MONTHS[wall.month - 1]} {wall.day} {wall.year}, {_clock(wall, hour24)}"


def _short_when(moment: Any, zone: tzinfo | None, hour24: bool) -> str:
    wall = _wall(moment, zone)
    if wall is None:
        return ""
    return f"{_MONTHS[wall.month - 1]} {wall.day}, {_clock(wall, hour24)}"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


# --------------------------------------------------------------------------
# Email pictures
# --------------------------------------------------------------------------

def email_picture(thread: Any, *, hour24: bool = False, tz: tzinfo | None = None) -> Picture | None:
    """The thread Ask read: its newest message as a mail client shows it (From, To / Cc, Date,
    Subject, the text), the older ones as short lines, newest first. None for an empty thread."""
    messages = tuple(getattr(thread, "messages", ()) or ())
    if not messages:
        return None
    newest = messages[-1]
    sender = _line(_person(getattr(newest, "sender", None)) or "(sender not known)", HEAD_CAP)
    subject = _line(getattr(newest, "subject", "") or getattr(thread, "subject", "") or NO_SUBJECT, HEAD_CAP)
    head = MailHead(
        sender=sender,
        to=_line(_people(getattr(newest, "to", ()), int(getattr(newest, "to_more", 0) or 0)), HEAD_CAP),
        cc=_line(_people(getattr(newest, "cc", ()), int(getattr(newest, "cc_more", 0) or 0)), HEAD_CAP),
        when=_line(_mail_when(getattr(newest, "sent_at", None), tz, hour24), 80),
        subject=subject)
    body, chars, cut = live.clean_block(getattr(newest, "text", "") or "", BODY_CAP)
    older: list[MailLine] = []
    for message in reversed(messages[:-1]):
        if len(older) >= OLDER_SHOWN:
            break
        person = getattr(message, "sender", None)
        who = " ".join(str(getattr(person, "name", "") or "").split()) or str(getattr(person, "email", "") or "")
        snippet = " ".join(str(getattr(message, "text", "") or "").split())   # one line: breaks as spaces
        older.append(MailLine(_line(who or "(sender not known)", 80),
                              _line(_short_when(getattr(message, "sent_at", None), tz, hour24), 40),
                              _line(snippet, SNIPPET_CAP)))
    older_more = max(0, len(messages) - 1 - OLDER_SHOWN) + max(0, int(getattr(thread, "left_out", 0) or 0))
    earlier = len(messages) - 1 + max(0, int(getattr(thread, "left_out", 0) or 0))
    mail = MailPicture(outgoing=False, account=_line(getattr(thread, "account", ""), 40), head=head, body=body,
                       body_chars=chars, body_cut=cut, older=tuple(older), older_more=older_more,
                       shortened=bool(getattr(newest, "cut", False)))
    alt = f"Email from {sender}, subject {subject}, {_plural(earlier, 'earlier message')}"
    return Picture(KIND_EMAIL, CAPTION_EMAIL, live.PICTURE_READY, note=NOTE_EMAIL, alt=_line(alt, ALT_CAP), mail=mail)


def outgoing_picture(view: Any, *, account: str) -> Picture:
    """What a Reply / Email card sends, read back from the built message (gmail.mail_view): From,
    To, Cc, Subject exactly as they go out, the reply line and the text. To and Cc keep every
    recipient (at most MAX_RECIPIENTS addresses of up to 254 characters each: the LIVE text field's
    cap, live.FIELD_CAP, holds them all), so the picture never drops one the text shows."""
    from_addr = str(getattr(view, "from_addr", "") or "")
    to = _line(getattr(view, "to", ""), live.FIELD_CAP)
    subject = _line(getattr(view, "subject", "") or NO_SUBJECT, HEAD_CAP)
    head = MailHead(sender=_line(from_addr or GMAIL_FILLS, HEAD_CAP), to=to,
                    cc=_line(getattr(view, "cc", ""), live.FIELD_CAP), when="", subject=subject)
    in_reply_to = str(getattr(view, "in_reply_to", "") or "")
    if in_reply_to:
        reply_line = f"Reply in its Gmail thread - In-Reply-To {in_reply_to}"
    elif getattr(view, "thread_id", ""):
        reply_line = "Reply in its Gmail thread"
    else:
        reply_line = "New email (a new thread)"
    body, chars, cut = live.clean_block(getattr(view, "body", "") or "", BODY_CAP)
    alias = _line(account, 40)
    mail = MailPicture(outgoing=True, account=alias, head=head, body=body, body_chars=chars, body_cut=cut,
                       reply_line=_line(reply_line, HEAD_CAP))
    alt = f"Email from {alias or 'Gmail'} ({from_addr or 'address filled in by Gmail'}) to {to}, subject {subject}"
    return Picture(KIND_OUTGOING, CAPTION_EMAIL, live.PICTURE_READY, note=NOTE_OUTGOING, alt=_line(alt, ALT_CAP),
                   mail=mail)


# --------------------------------------------------------------------------
# The days Jarvis already read
# --------------------------------------------------------------------------

SOURCE_AGENDA = "today's agenda"
SOURCE_ASK = "Ask's calendar read"
_SOURCES_PER_ALIAS = 2


@dataclass(frozen=True, repr=False)
class KnownEvent:
    title: str
    start: datetime | None        # naive wall time on this PC (None for all-day)
    end: datetime | None
    all_day_start: date | None = None
    all_day_end: date | None = None   # inclusive
    event_id: str = ""

    def __repr__(self) -> str:
        return "<KnownEvent>"


@dataclass(frozen=True, repr=False)
class KnownDay:
    events: tuple[KnownEvent, ...]
    source: str
    at: datetime
    foreign: bool = False         # the calendar's times are in another zone than this PC's

    def __repr__(self) -> str:
        return f"<KnownDay {len(self.events)} event(s)>"


@dataclass(frozen=True, repr=False)
class _Read:
    start: datetime               # naive wall times (this PC's)
    end: datetime
    events: tuple[KnownEvent, ...]   # naive wall times as Google gave them (the calendar's zone)
    source: str
    at: datetime
    foreign: bool = False


class KnownDays:
    """The calendar days Jarvis already read this session (memory only; GUI thread): today's agenda
    and each Ask's calendar read, per account alias, the newest read per (alias, source). A day
    picture draws the day's other events only from a read that covers that whole day."""

    def __init__(self) -> None:
        self._reads: dict[str, dict[str, _Read]] = {}

    def __repr__(self) -> str:
        return f"KnownDays(accounts={len(self._reads)})"

    def remember(self, alias: str, source: str, start: datetime, end: datetime, events: Iterable[Any], *,
                 at: datetime) -> None:
        """A read of ``alias``'s calendar from ``start`` to ``end`` (events duck-typed: CalendarEvent or
        EventBrief); it replaces the older read of the same source."""
        try:
            zone = _zone(at)
            first, last = _wall(start, zone), _wall(end, zone)
            if first is None or last is None or not isinstance(alias, str):
                return
            items = list(events)
            frame = _read_frame(items, zone)
            kept = tuple(found for found in (_known_event(item, frame(item)) for item in items) if found is not None)
            foreign = any(_foreign(_in_frame(getattr(item, "start", None), frame(item)), zone) for item in items)
            reads = self._reads.setdefault(alias, {})
            reads.pop(source, None)
            reads[source] = _Read(first, last, kept, source, at, foreign)
            while len(reads) > _SOURCES_PER_ALIAS:
                reads.pop(next(iter(reads)))
        except Exception:  # noqa: BLE001 - only what a picture shows
            return

    def lookup(self, alias: str, day: date) -> KnownDay | None:
        """The newest read of ``alias`` that covers the whole of ``day`` (None when none does)."""
        begin, end = _midnight(day), _midnight(day) + timedelta(days=1)
        found: _Read | None = None
        for read in (self._reads.get(alias) or {}).values():
            if read.start <= begin and read.end >= end and (found is None or _later(read.at, found.at)):
                found = read
        return KnownDay(found.events, found.source, found.at, found.foreign) if found is not None else None

    def clear(self) -> None:
        self._reads.clear()


def _later(first: datetime, second: datetime) -> bool:
    try:
        return first >= second
    except TypeError:   # naive and aware mixed
        return True


_MAIN_CALENDAR = "primary"


def _calendar_of(item: Any) -> str:
    return str(getattr(item, "calendar_id", "") or "")


def _in_pc_frame(anchors: Sequence[tuple[datetime, timedelta]], zone: tzinfo | None) -> bool:
    """Every (utc moment, offset) of one calendar carries this PC's offset at that moment (``zone``:
    _zone of the read; None = this PC's own rules)."""
    try:
        return all((moment.astimezone(zone) if zone is not None else moment.astimezone()).utcoffset() == offset
                   for moment, offset in anchors)
    except (OverflowError, OSError, ValueError):
        return False


def _read_frame(items: Sequence[Any], zone: tzinfo | None = None) -> Any:
    """How one read's timed events are put in ONE frame, the card's: ``frame(item)`` is the fixed
    zone to re-express that event in, or None to keep the wall time it carries. Google gives an
    agenda read's calendars each in its OWN time zone (no timeZone is asked for: gcal.list_events),
    an Ask's read all in the account's. The main calendar is the frame: "primary"; else, of the
    calendars whose times carry this PC's offset (``zone``: _zone of the read), the one with the most
    timed events (the owner's own calendar named by its address, not a busier team calendar kept in
    another zone); else the one with the most timed events. An event of another calendar whose
    offset differs from the main calendar's nearest event takes that event's offset (a team calendar
    kept in another time zone is drawn at its time in the account's frame, and the card's own event
    is matched)."""
    anchors_by: dict[str, list[tuple[datetime, timedelta]]] = {}
    for item in items:
        start = getattr(item, "start", None)
        try:
            if isinstance(start, datetime) and start.utcoffset() is not None:
                anchors_by.setdefault(_calendar_of(item), []).append((start.astimezone(timezone.utc),
                                                                      start.utcoffset()))
        except (OverflowError, OSError, ValueError):   # an odd time: kept as it carries it
            continue
    if len(anchors_by) < 2:
        return lambda _item: None   # one calendar (or none timed): one frame already
    if _MAIN_CALENDAR in anchors_by:
        main = _MAIN_CALENDAR
    else:
        pool = [key for key, found in anchors_by.items() if _in_pc_frame(found, zone)] or list(anchors_by)
        main = max(pool, key=lambda key: len(anchors_by[key]))
    anchors = anchors_by[main]

    def frame(item: Any) -> Any:
        start = getattr(item, "start", None)
        if _calendar_of(item) == main or not isinstance(start, datetime) or start.utcoffset() is None:
            return None
        try:
            moment = start.astimezone(timezone.utc)
            _when, offset = min(anchors, key=lambda pair: abs(pair[0] - moment))
            return None if offset == start.utcoffset() else timezone(offset)
        except (OverflowError, OSError, ValueError):
            return None

    return frame


def _in_frame(moment: Any, zone: Any) -> Any:
    """``moment`` re-expressed in ``zone`` (a read's frame: _read_frame), else as it is."""
    if zone is None or not isinstance(moment, datetime) or moment.utcoffset() is None:
        return moment
    try:
        return moment.astimezone(zone)
    except (OverflowError, OSError, ValueError):
        return moment


def _known_event(item: Any, zone: Any = None) -> KnownEvent | None:
    title = _line(getattr(item, "title", "") or NO_TITLE, EVENT_TITLE_CAP)
    first = getattr(item, "all_day_start", None)
    if isinstance(first, date) and not isinstance(first, datetime):
        last = getattr(item, "all_day_end", None)
        last = last if isinstance(last, date) and not isinstance(last, datetime) and last >= first else first
        return KnownEvent(title, None, None, first, last, str(getattr(item, "event_id", "") or ""))
    start = _event_wall(_in_frame(getattr(item, "start", None), zone))
    end = _event_wall(_in_frame(getattr(item, "end", None), zone))
    if start is None:
        return None
    if end is None or end < start:
        end = start
    return KnownEvent(title, start, end, None, None, str(getattr(item, "event_id", "") or ""))


# --------------------------------------------------------------------------
# Day pictures
# --------------------------------------------------------------------------

@dataclass
class _Spot:
    """Where one event of a day picture is (before columns are known)."""

    start: datetime | None = None
    end: datetime | None = None
    all_day_start: date | None = None
    all_day_end: date | None = None

    @property
    def timeless(self) -> bool:
        return self.start is None and self.all_day_start is None

    @property
    def day(self) -> date | None:
        if self.all_day_start is not None:
            return self.all_day_start
        return self.start.date() if self.start is not None else None


def _account_of(action: Any) -> str:
    """The alias that acts for the card (executor.account_of: a Calendar event and a To-do's block
    go to the personal calendar)."""
    if action.kind in EVENT_KINDS | MAIL_KINDS:
        return action.account or DEFAULT_ACCOUNT
    return DEFAULT_ACCOUNT


def _spot_of(item: Any) -> _Spot:
    """Where an event is, in the calendar's frame: a card's naive wall times as they are, Google's
    aware ones as the wall time they carry (_event_wall)."""
    if item is None:
        return _Spot()
    first = getattr(item, "all_day_start", None)
    if isinstance(first, date) and not isinstance(first, datetime):
        last = getattr(item, "all_day_end", None)
        return _Spot(all_day_start=first, all_day_end=last if isinstance(last, date) and last >= first else first)
    start = _event_wall(getattr(item, "start", None))
    if start is None:
        return _Spot()
    end = _event_wall(getattr(item, "end", None))
    return _Spot(start=start, end=end if end is not None and end >= start else start + timedelta(minutes=30))


def _current_spot(action: Any, check: Any) -> _Spot:
    """The affected event's time now: Google's (the card's check) else the line's at=, else none."""
    if check is not None and (getattr(check, "start", None) is not None
                              or getattr(check, "all_day_start", None) is not None):
        return _spot_of(check)
    return _spot_of(stated_when(action))


def _spot_text(spot: _Spot, hour24: bool, *, unknown: str = "time not known") -> str:
    if spot.all_day_start is not None:
        if spot.all_day_end is not None and spot.all_day_end != spot.all_day_start:
            return f"all day to {day_label(spot.all_day_end)}"
        return "all day"
    if spot.start is None:
        return unknown
    return time_range(spot.start, spot.end, hour24)


def _minutes(moment: datetime, day: date) -> int:
    delta = moment - _midnight(day)
    return int(max(0, min(_DAY_MINUTES, delta.total_seconds() // 60)))


def _block(spot: _Spot, column: int, day: date | None, title: str, time_text: str, role: str,
           chip: str = "") -> DayBlock:
    if spot.all_day_start is not None or day is None or spot.start is None:
        return DayBlock(column, 0, 0, title, time_text, role, all_day=spot.all_day_start is not None,
                        timeless=spot.timeless or day is None, chip=chip)
    start = _minutes(spot.start, day)
    end = _minutes(spot.end or spot.start, day)
    if end <= start:
        end = min(_DAY_MINUTES, start + 15)
        start = min(start, end - 15)
    return DayBlock(column, start, end, title, time_text, role, chip=chip)


_LANE_MIN = 30   # a timed block is drawn at least this many minutes tall (hud, at its natural hour size)


def with_lanes(blocks: Sequence[DayBlock], min_minutes: int = _LANE_MIN) -> tuple[DayBlock, ...]:
    """``blocks`` with lanes for a drawing whose shortest block stands for ``min_minutes`` (hud
    re-lanes a squeezed thumbnail, where its 14 px minimum is 30 to 105 minutes): _with_lanes."""
    return _with_lanes(blocks, min_minutes)


def _with_lanes(blocks: Sequence[DayBlock], min_minutes: int = _LANE_MIN) -> tuple[DayBlock, ...]:
    """Overlapping timed blocks of a column side by side: greedy lanes per overlapping cluster. A
    block counts as at least ``min_minutes`` long (as it is drawn: _LANE_MIN at the natural hour
    size), so short events one after the other (9:00-9:15, 9:15-9:30) sit side by side instead of
    printing over each other."""
    min_minutes = max(1, int(min_minutes))
    timed = sorted((index for index, block in enumerate(blocks) if not block.all_day and not block.timeless),
                   key=lambda index: (blocks[index].column, blocks[index].start_min, blocks[index].end_min))
    result = list(blocks)
    cluster: list[int] = []
    lane_ends: list[int] = []
    cluster_end = -1
    column = -1

    def close() -> None:
        for index in cluster:
            result[index] = replace(result[index], lanes=max(1, len(lane_ends)))

    for index in timed:
        block = blocks[index]
        if block.column != column or block.start_min >= cluster_end:
            close()
            cluster, lane_ends, cluster_end, column = [], [], -1, block.column
        drawn_end = max(block.end_min, block.start_min + min_minutes)
        lane = next((number for number, end in enumerate(lane_ends) if end <= block.start_min), len(lane_ends))
        if lane == len(lane_ends):
            lane_ends.append(drawn_end)
        else:
            lane_ends[lane] = drawn_end
        result[index] = replace(block, lane=lane)
        cluster.append(index)
        cluster_end = max(cluster_end, drawn_end)
    close()
    return tuple(result)


def _hours(blocks: Sequence[DayBlock]) -> tuple[int, int]:
    timed = [block for block in blocks if not block.all_day and not block.timeless]
    first = min([DAY_START_MIN] + [block.start_min - DAY_MARGIN_MIN for block in timed])
    last = max([DAY_END_MIN] + [block.end_min + DAY_MARGIN_MIN for block in timed])
    first = max(0, (first // 60) * 60)
    last = min(_DAY_MINUTES, -(-last // 60) * 60)
    return first, last


def _same_event(known: KnownEvent, event_id: str, title: str, starts: Sequence[datetime | None]) -> bool:
    if known.event_id and event_id:
        return known.event_id == event_id
    if known.title.casefold() != title.casefold():
        return False
    begin = known.start.replace(second=0, microsecond=0) if known.start is not None else None
    for start in starts:
        moment = start.replace(second=0, microsecond=0) if start is not None else None
        if begin == moment:
            return True
    return False


def _others_on(known: KnownDay, day: date, column: int, hour24: bool, event_id: str, title: str,
               starts: Sequence[datetime | None]) -> list[DayBlock]:
    begin, end = _midnight(day), _midnight(day) + timedelta(days=1)
    found: list[tuple[tuple, DayBlock]] = []
    for event in known.events:
        if _same_event(event, event_id, title, starts):
            continue
        if event.all_day_start is not None:
            last = event.all_day_end or event.all_day_start
            if event.all_day_start <= day <= last:
                spot = _Spot(all_day_start=event.all_day_start, all_day_end=last)
                found.append(((0, begin), _block(spot, column, day, event.title, _spot_text(spot, hour24),
                                                  ROLE_OTHER)))
            continue
        if event.start is None:
            continue
        stop = event.end or event.start
        if not (event.start < end and (stop > begin or (stop == event.start and event.start >= begin))):
            continue
        spot = _Spot(start=event.start, end=event.end)
        found.append(((1, event.start), _block(spot, column, day, event.title, _spot_text(spot, hour24), ROLE_OTHER)))
    found.sort(key=lambda pair: pair[0])
    return [block for _key, block in found]


def day_change_picture(action: Any, *, now: datetime, hour24: bool = False, check: Any = None,
                       known: KnownDays | None = None) -> Picture | None:
    """A calendar card's change drawn on its day: Calendar add / To-do block (the new block
    highlighted), Move (where it is now as a ghost, an arrow, the new time; two columns for another
    day), Cancel (struck through), RSVP (the event with the answer chip). The day's other events
    only from a read that covers that whole day (``known``); else only the affected blocks. None
    for other kinds (or a To-do without a block)."""
    kind = getattr(action, "kind", "")
    if kind not in (CALENDAR, TODO, MOVE, CANCEL, RSVP):
        return None
    zone = _zone(now)
    today = (_wall(now, zone) or datetime.now()).date()
    account = _line(_account_of(action), 40)
    foreign = _foreign(getattr(check, "start", None), zone) if check is not None else False
    chip = ""
    affected: list[tuple[_Spot, str, str]] = []   # (where, role, time words)
    event_id = ""
    if kind in (CALENDAR, TODO):
        event = action.block_event() if kind == TODO else action
        if event is None:
            return None
        title = _line(event.title or NO_TITLE, EVENT_TITLE_CAP)
        spot = _spot_of(event)
        if spot.timeless:
            return None
        words = _spot_text(spot, hour24)
        repeat = " ".join(str(getattr(event, "repeat", "") or "").split())
        if repeat:
            words = f"{words} - repeats {repeat}"
        change = CHANGE_TODO if kind == TODO else CHANGE_ADD
        headline = f"{'To-do block' if kind == TODO else 'Add'}: {title}"
        affected.append((spot, ROLE_NEW, _line(words, 160)))
    else:
        event_id = action.field("event")
        google_title = str(getattr(check, "title", "") or "") if check is not None else ""
        title = _line(google_title or action.field("title") or action.title or NO_TITLE, EVENT_TITLE_CAP)
        current = _current_spot(action, check)
        if kind == MOVE:
            change, headline = CHANGE_MOVE, f"Move: {title}"
            new = _spot_of(action)
            affected.append((current, ROLE_BEFORE, _spot_text(current, hour24, unknown="old time not known")))
            if not new.timeless:
                affected.append((new, ROLE_AFTER, _spot_text(new, hour24)))
        elif kind == CANCEL:
            change, headline = CHANGE_CANCEL, f"Cancel: {title}"
            affected.append((current, ROLE_CANCEL, _spot_text(current, hour24)))
        else:
            change = CHANGE_RSVP
            chip = _ANSWER_CHIPS.get(action.field("answer").casefold(), "")
            headline = f"Answer {chip}: {title}" if chip else f"Answer: {title}"
            affected.append((current, ROLE_RSVP, _spot_text(current, hour24)))
    days = sorted({spot.day for spot, _role, _words in affected if spot.day is not None})
    columns = {day: index for index, day in enumerate(days)}
    blocks: list[DayBlock] = []
    for spot, role, words in affected:
        day = spot.day
        blocks.append(_block(spot, columns.get(day, 0) if day is not None else 0, day, title, words, role,
                             chip if role == ROLE_RSVP else ""))
    starts = [spot.start for spot, _role, _words in affected]
    others: list[DayBlock] = []
    covered: list[tuple[date, KnownDay]] = []
    missing: list[date] = []
    for day, column in columns.items():
        found = known.lookup(_account_of(action), day) if known is not None else None
        if found is None:
            missing.append(day)
            continue
        covered.append((day, found))
        foreign |= found.foreign
        others.extend(_others_on(found, day, column, hour24, event_id, title, starts))
    others_more = max(0, len(others) - DAY_OTHERS_CAP)
    blocks = list(others[:DAY_OTHERS_CAP]) + blocks
    blocks_t = _with_lanes(blocks)
    first_min, last_min = _hours(blocks_t)
    if covered:
        reads: list[tuple[str, datetime]] = []   # each read the columns came from, once (column order)
        for _day, found in covered:
            if (found.source, found.at) not in reads:
                reads.append((found.source, found.at))

        def read_at(source: str, when: datetime) -> str:
            """"2:41 PM (today's agenda)"; a read of an earlier day says which (a read is kept for
            the whole session: "12:30 PM (Ask's calendar read yesterday)", "2:41 PM (the agenda of
            Wed Oct 7)")."""
            wall = _wall(when, zone) or when
            text = _clock(wall, hour24)
            age = (today - wall.date()).days if isinstance(wall, datetime) else 0
            if age <= 0:
                return f"{text} ({source})"
            if source == SOURCE_AGENDA:
                label = "yesterday's agenda" if age == 1 else f"the agenda of {day_label(wall.date(), today)}"
            else:
                label = f"{source} yesterday" if age == 1 else f"{source} on {day_label(wall.date(), today)}"
            return f"{text} ({label})"

        if len(reads) == 1:
            source, when = reads[0]
            note = f"Other events as Jarvis read them at {read_at(source, when)}"
        else:   # the columns came from different reads: each day says which
            note = "Other events as Jarvis read them: " + "; ".join(
                f"{day_label(day, today)} at {read_at(found.source, found.at)}" for day, found in covered)
        if missing:
            note += f"; Jarvis has not read the rest of {' or '.join(day_label(day, today) for day in missing)}"
    elif missing:
        note = f"Only this event is drawn: Jarvis has not read the rest of " \
               f"{' or '.join(day_label(day, today) for day in missing)}"
    else:
        note = "Only this event is drawn: Jarvis does not know its day"
    now_wall = _wall(now, zone)
    # The time now is this PC's: on a calendar in another time zone it would sit at the wrong hour.
    now_column = columns.get(today, -1) if not foreign else -1
    now_min = _minutes(now_wall, today) if now_column >= 0 and now_wall is not None else -1
    picture = DayPicture(change=change, headline=_line(headline, 200), account=account,
                         days=tuple(day_label(day, today) for day in days) or ("Day not known",),
                         first_min=first_min, last_min=last_min, blocks=blocks_t, others_shown=bool(covered),
                         others_note=_line(note, 300), others_more=others_more, hour24=bool(hour24),
                         now_min=now_min, now_column=now_column)
    # The note is the coverage line (SPEC 3.3): what the day's other events are drawn from, or that
    # only this event is drawn - under the thumbnail, in the viewer and to a screen reader.
    return Picture(KIND_DAY, CAPTION_DAY, live.PICTURE_READY, note=picture.others_note or NOTE_DAY,
                   alt=_line(_day_alt(picture, affected, days, others, today), ALT_CAP), day=picture)


def _day_alt(picture: DayPicture, affected: Sequence[tuple[_Spot, str, str]], days: Sequence[date],
             others: Sequence[DayBlock], today: date) -> str:
    def when(spot: _Spot, words: str) -> str:
        day = spot.day
        return f"{day_label(day, today)} {words}" if day is not None else words

    parts = [when(spot, words) for spot, _role, words in affected]
    if picture.change == CHANGE_MOVE and len(parts) == 2:
        text = f"{picture.headline} from {parts[0]} to {parts[1]}"
    else:
        text = f"{picture.headline}, {', '.join(parts)}"
    if picture.others_shown:
        count = len(others)
        text += f"; {_plural(count, 'other event')} {'that day' if len(days) <= 1 else 'on those days'}"
    return text


# --------------------------------------------------------------------------
# A day plan (the productivity bundle): the day as read, and the proposed blocks
# --------------------------------------------------------------------------

def plan_picture(events: Mapping[str, Sequence[Any]], blocks: Sequence[Any], *, day: date, now: datetime,
                 hour24: bool = False, zone: str = "") -> Picture:
    """A day plan drawn on its day: the events Jarvis just read for it (every account, as
    ROLE_OTHER; a declined one titled "... (declined)", a "show as free" or working-location one
    "... (free)", R21) and the proposed blocks (ROLE_NEW, stamped PROPOSED: nothing is added before
    each block's own click). ``blocks=()`` (or none on that day) draws the day alone, captioned as
    read and never stamped. ``zone``: the personal calendar's (events of another calendar, and now,
    are moved into it; gcal.wall_in_zone): the frame of the blocks' times. Kind KIND_DAY, no new role."""
    from .gcal import wall_in_zone

    # "now" (and which day is today) in the blocks' frame: the personal calendar's zone, else this PC's.
    now_wall = wall_in_zone(now, zone) if zone and isinstance(now, datetime) else _wall(now, _zone(now))
    today = (now_wall or datetime.now()).date()

    def wall(moment: Any) -> datetime | None:
        if not isinstance(moment, datetime):
            return None
        return wall_in_zone(moment, zone) if zone else _event_wall(moment)

    others: list[tuple[tuple, DayBlock]] = []
    for briefs in events.values():
        for brief in briefs or ():
            title = _line(getattr(brief, "title", "") or NO_TITLE, EVENT_TITLE_CAP - 12)
            if getattr(brief, "self_response", "") == "declined":
                title += " (declined)"
            elif getattr(brief, "transparent", False) or getattr(brief, "event_type", "") == "workingLocation":
                title += " (free)"
            first = getattr(brief, "all_day_start", None)
            if isinstance(first, date) and not isinstance(first, datetime):
                last = getattr(brief, "all_day_end", None) or first
                if first <= day <= last:
                    spot = _Spot(all_day_start=first, all_day_end=last)
                    others.append(((0, _midnight(day)), _block(spot, 0, day, title, _spot_text(spot, hour24),
                                                               ROLE_OTHER)))
                continue
            start, end = wall(getattr(brief, "start", None)), wall(getattr(brief, "end", None))
            if start is None:
                continue
            end = end if end is not None and end >= start else start + timedelta(minutes=30)
            begin, finish = _midnight(day), _midnight(day) + timedelta(days=1)
            if not (start < finish and (end > begin or (end == start and start >= begin))):
                continue
            spot = _Spot(start=start, end=end)
            others.append(((1, start), _block(spot, 0, day, title, _spot_text(spot, hour24), ROLE_OTHER)))
    others.sort(key=lambda pair: pair[0])
    drawn = [block for _key, block in others]
    more = max(0, len(drawn) - DAY_OTHERS_CAP)
    proposed: list[DayBlock] = []
    for card in blocks:
        start, end = getattr(card, "start", None), getattr(card, "end", None)
        if not isinstance(start, datetime) or start.date() != day:
            continue
        spot = _Spot(start=start.replace(tzinfo=None),
                     end=(end.replace(tzinfo=None) if isinstance(end, datetime) else start + timedelta(minutes=30)))
        proposed.append(_block(spot, 0, day, _line(getattr(card, "title", "") or NO_TITLE, EVENT_TITLE_CAP),
                               _spot_text(spot, hour24), ROLE_NEW))
    all_blocks = _with_lanes(drawn[:DAY_OTHERS_CAP] + proposed)
    first_min, last_min = _hours(all_blocks)
    now_column = 0 if day == today and now_wall is not None else -1
    count = len(proposed)
    headline = (f"Day plan for {day_label(day, today)}: {_plural(count, 'block')} proposed" if blocks
                else f"{day_label(day, today)} as Jarvis read it")
    note = NOTE_PLAN if proposed else NOTE_PLAN_DAY
    picture = DayPicture(change=CHANGE_PLAN, headline=_line(headline, 200), account="personal",
                         days=(day_label(day, today),), first_min=first_min, last_min=last_min, blocks=all_blocks,
                         others_shown=True, others_note=note, others_more=more, hour24=bool(hour24),
                         now_min=_minutes(now_wall, today) if now_column == 0 and now_wall is not None else -1,
                         now_column=now_column)
    alt = f"{headline}; {_plural(len(drawn), 'event')} that day"
    return Picture(KIND_DAY, CAPTION_PLAN if proposed else CAPTION_PLAN_DAY, live.PICTURE_READY, note=note,
                   alt=_line(alt, ALT_CAP), day=picture)


# --------------------------------------------------------------------------
# The week strip of an Ask's calendar read
# --------------------------------------------------------------------------

def week_picture(events: Mapping[str, Sequence[Any]], start: datetime, end: datetime, *, now: datetime,
                 hour24: bool = False) -> Picture | None:
    """The 7 days from today (inside the read window) of what Ask read, every account together:
    timed blocks per day, all-day counts, up to 4 titles a day; None when no calendar was read."""
    if not events:
        return None
    zone = _zone(now)
    now_wall = _wall(now, zone) or datetime.now()
    first_wall, last_wall = _wall(start, zone), _wall(end, zone)
    if first_wall is None or last_wall is None:
        return None
    first_day = max(now_wall.date(), first_wall.date())
    days = [first_day + timedelta(days=offset) for offset in range(WEEK_COLUMNS)
            if _midnight(first_day + timedelta(days=offset)) < last_wall]
    if not days:
        return None
    timed: dict[date, list[tuple[int, int]]] = {day: [] for day in days}
    all_day: dict[date, int] = {day: 0 for day in days}
    titles: dict[date, list[tuple[tuple, str]]] = {day: [] for day in days}
    counts: dict[date, int] = {day: 0 for day in days}
    total = later = 0
    last_day = days[-1]
    for briefs in events.values():
        for brief in briefs or ():
            total += 1
            spot = _spot_of(brief)
            title = _line(getattr(brief, "title", "") or NO_TITLE, WEEK_TITLE_CAP)
            if spot.all_day_start is not None:
                last = spot.all_day_end or spot.all_day_start
                if spot.all_day_start > last_day:
                    later += 1
                for day in days:
                    if spot.all_day_start <= day <= last:
                        all_day[day] += 1
                        counts[day] += 1
                        titles[day].append(((0, _midnight(day)), title))
                continue
            if spot.start is None:
                continue
            if spot.start.date() > last_day:
                later += 1
            stop = spot.end or spot.start
            for day in days:
                begin, finish = _midnight(day), _midnight(day) + timedelta(days=1)
                if spot.start < finish and (stop > begin or (stop == spot.start and spot.start >= begin)):
                    a, b = _minutes(spot.start, day), _minutes(stop, day)
                    timed[day].append((a, max(b, min(_DAY_MINUTES, a + 15))))
                    counts[day] += 1
                    titles[day].append(((1, spot.start), title))
    shown = [block for day in days for block in timed[day]]
    first_min = max(0, (min([DAY_START_MIN] + [a - DAY_MARGIN_MIN for a, _b in shown]) // 60) * 60)
    last_min = min(_DAY_MINUTES, -(-max([DAY_END_MIN] + [b + DAY_MARGIN_MIN for _a, b in shown]) // 60) * 60)
    columns = tuple(WeekColumn(label=f"{_WEEKDAYS[day.weekday()]} {day.day}", today=day == now_wall.date(),
                               blocks=tuple(sorted(timed[day])), all_day=all_day[day],
                               titles=tuple(title for _key, title in sorted(titles[day], key=lambda pair: pair[0])
                                            [:WEEK_TITLES]),
                               count=counts[day]) for day in days)
    accounts = tuple(_line(alias, 40) for alias in events)
    week = WeekPicture(columns=columns, first_min=first_min, last_min=last_min, accounts=accounts, total=total,
                       later=later, hour24=bool(hour24))
    busy = ", ".join(f"{column.label}: {column.count}" for column in columns)
    alt = f"Calendar for the next {len(columns)} days: {_plural(total, 'event')} read ({busy})"
    if later:
        alt += f", {later} later"
    return Picture(KIND_WEEK, CAPTION_WEEK, live.PICTURE_READY, note=NOTE_WEEK, alt=_line(alt, ALT_CAP), week=week)


# --------------------------------------------------------------------------
# A web search of a research run, drawn as a compact results page
# --------------------------------------------------------------------------

def _address(url: str) -> tuple[str, str]:
    """A result's address as a results page shows it: the site (its host, the part that says whose
    site it is; "http://" first for any scheme but https, ":port" when it names one; never a user
    name) and the rest (path, query, fragment; "" for the site's root). ("", "") without a host."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".")
        port = parts.port
    except ValueError:
        return "", ""
    if not host:
        return "", ""
    site = host if port is None else f"{host}:{port}"
    scheme = parts.scheme.lower()
    if scheme and scheme != "https":
        site = f"{scheme}://{site}"
    rest = parts.path + (f"?{parts.query}" if parts.query else "") + (f"#{parts.fragment}" if parts.fragment else "")
    return site, ("" if rest == "/" else rest)


def _search_hit(item: Any) -> SearchHit | None:
    """A result as the research stream lists it: (title, url), or (title, url, snippet)."""
    if not isinstance(item, (tuple, list)) or len(item) < 2:
        return None
    title, url = item[0], item[1]
    if not isinstance(url, str) or not url.strip():
        return None
    snippet = item[2] if len(item) > 2 and isinstance(item[2], str) else ""
    site, path = _address(url.strip())
    return SearchHit(title=_line(title if isinstance(title, str) else "", SEARCH_TITLE_CAP) or "(no title)",
                     url=_line(url.strip(), SEARCH_URL_CAP), host=_line(link_host(url.strip()), 255),
                     snippet=_line(snippet, SEARCH_SNIPPET_CAP), site=_line(site, 300),
                     path=_line(path, SEARCH_URL_CAP))


def search_picture(query: Any, hits: Iterable[Any] = (), *, only: Sequence[str] = (), never: Sequence[str] = (),
                   unread: bool = False) -> Picture | None:
    """One web search drawn as a compact results page: the exact ``query`` in a search box, then
    each result's title, address and snippet (only when the search gave one) in the order the
    search listed them, at most SEARCH_HITS_SHOWN ("+N more" beyond). ``hits``: the research
    stream's (title, url) pairs (a third item: a snippet). ``unread``: the results came back as
    text Jarvis could not list. Drawn by Jarvis from that text alone: nothing is opened for it and
    nothing in it can be clicked. None without a query."""
    shown_query = _line(query, QUERY_CAP)
    if not shown_query:
        return None
    found = [hit for hit in (_search_hit(item) for item in (hits or ())) if hit is not None]
    drawn = tuple(found[:SEARCH_HITS_SHOWN])
    search = SearchPicture(query=shown_query, hits=drawn, more=max(0, len(found) - len(drawn)),
                           only=_line(", ".join(str(site) for site in only or ()), SEARCH_DOMAINS_CAP),
                           never=_line(", ".join(str(site) for site in never or ()), SEARCH_DOMAINS_CAP),
                           unread=bool(unread) and not found)
    if search.unread:
        listed = "results Jarvis could not list"
    elif not found:
        listed = "no pages listed"
    else:
        sites = ", ".join(dict.fromkeys(hit.host or hit.url for hit in drawn))
        listed = f"{_plural(len(found), 'result')}: {sites}"
    alt = _line(f"Web search for {shown_query}: {listed}", ALT_CAP)
    return Picture(KIND_SEARCH, CAPTION_SEARCH, live.PICTURE_READY, note=NOTE_SEARCH, alt=alt, search=search)


# --------------------------------------------------------------------------
# Page snapshots (made by Build B's snapshot engine; the data and words are here)
# --------------------------------------------------------------------------

def _page_base(url: str) -> PagePicture:
    text = url if isinstance(url, str) else ""
    host = link_host(text)
    return PagePicture(url=_line(text, URL_CAP), host=_line(host, 255), idn=bool(host) and idn_host(text))


def page_pending(url: str) -> Picture:
    """A page picture on its way ("Opening it on this PC ...")."""
    page = _page_base(url)
    return Picture(KIND_PAGE, page_caption(page.host), live.PICTURE_PENDING, note=NOTE_OPENING,
                   alt=_line(f"Picture of the page {page.host or 'unknown site'} - being taken", ALT_CAP), page=page)


def page_ready(url: str, *, image: bytes, image_format: str = "JPG", width: int, height: int, final_url: str = "",
               took_ms: int = 0, still_loading: bool = False, blocked: int = 0) -> Picture:
    """The first screen of the page, as taken on this PC; the note names a redirect to another host,
    a page still loading at the time limit and an international domain name."""
    page = _page_base(url)
    final_host = link_host(final_url) if isinstance(final_url, str) and final_url else ""
    moved = bool(final_host) and final_host.casefold() != page.host.casefold()
    page = replace(page, final_host=_line(final_host, 255) if moved else "", image=bytes(image or b""),
                   image_format=str(image_format or "JPG")[:8], width=max(0, int(width)), height=max(0, int(height)),
                   took_ms=max(0, int(took_ms)), still_loading=bool(still_loading), blocked=max(0, int(blocked)))
    note = NOTE_OPENED
    if page.final_host:
        note += f" - redirected to {page.final_host}"
    if page.still_loading:
        note += " - still loading after 15 s"
    if page.idn:
        note += f" - {IDN_WARNING}"
    alt = f"Picture of the page {page.host or 'unknown site'}"
    if page.final_host:
        alt += f", redirected to {page.final_host}"
    return Picture(KIND_PAGE, page_caption(page.host), live.PICTURE_READY, note=_line(note, 300),
                   alt=_line(alt, ALT_CAP), page=page)


def page_unavailable(url: str, reason: str) -> Picture:
    """No picture of the page ("Picture unavailable: <reason>")."""
    page = _page_base(url)
    return Picture(KIND_PAGE, page_caption(page.host), live.PICTURE_UNAVAILABLE,
                   note=_line(UNAVAILABLE_PREFIX + (reason or "it could not be taken"), 300),
                   alt=_line(f"No picture of the page {page.host or 'unknown site'}", ALT_CAP), page=page)


# --------------------------------------------------------------------------
# Stamps
# --------------------------------------------------------------------------

_WILL = {CHANGE_ADD: "WILL BE ADDED", CHANGE_TODO: "WILL BE ADDED", CHANGE_MOVE: "WILL MOVE",
         CHANGE_CANCEL: "WILL BE CANCELLED"}
_DONE = {CHANGE_ADD: "ADDED", CHANGE_TODO: "ADDED", CHANGE_MOVE: "MOVED", CHANGE_CANCEL: "CANCELLED"}
PLAN_STAMP = "PROPOSED - NOT ADDED YET"


def stamp(picture: Any, status: str, status_text: str) -> tuple[str, str]:
    """(words, tone) of the stamp on an outgoing email or a calendar change picture, from its payload
    step's status and status text: "WILL BE SENT" (will) during the countdown, "SENDING NOW"
    (doing), "SENT" / "MOVED" / "ANSWERED YES" (done), "MAY HAVE GONE OUT - CHECK" (check), "NOT
    SENT" (failed), "UNDONE - NOT SENT" (undone) ... ("", "") for other pictures."""
    kind = getattr(picture, "kind", "")
    if kind not in (KIND_OUTGOING, KIND_DAY) or getattr(picture, "state", "") != live.PICTURE_READY:
        return "", ""
    mail = kind == KIND_OUTGOING
    day = getattr(picture, "day", None)
    change = getattr(day, "change", "")
    if change == CHANGE_PLAN:   # proposed blocks: each is added only after its own click
        proposed = any(block.role == ROLE_NEW for block in getattr(day, "blocks", ()))
        return (PLAN_STAMP, TONE_WILL) if proposed else ("", "")   # the day as read proposes nothing
    chip = next((block.chip for block in getattr(day, "blocks", ()) if block.chip), "")
    text = (status_text or "").upper()
    if status == live.STATUS_RUNNING:
        if text == "CHANGED SINCE":
            return "NOT WHAT THE COUNTDOWN SHOWED", TONE_CHECK
        if text == "SENDING":
            if mail:
                return "SENDING NOW", TONE_DOING
            return ("ADDING NOW" if change in (CHANGE_ADD, CHANGE_TODO) else "CHANGING NOW"), TONE_DOING
        if mail:
            return "WILL BE SENT", TONE_WILL
        if change == CHANGE_RSVP:
            return (f"WILL ANSWER {chip}" if chip else "WILL ANSWER"), TONE_WILL
        return _WILL.get(change, "WILL CHANGE"), TONE_WILL
    if status == live.STATUS_OK:
        if text in ("NOTHING SENT", "SENT - NO CHANGE"):
            return "NOTHING CHANGED", TONE_DONE
        if mail:
            return "SENT", TONE_DONE
        if change == CHANGE_RSVP:
            return (f"ANSWERED {chip}" if chip else "ANSWERED"), TONE_DONE
        return _DONE.get(change, "CHANGED"), TONE_DONE
    if status == live.STATUS_WARN:
        if text == "UNKNOWN":
            return "MAY HAVE GONE OUT - CHECK", TONE_CHECK
        return "CHECK THE NOTE", TONE_CHECK
    if status in (live.STATUS_FAILED, live.STATUS_BLOCKED):
        return ("NOT SENT" if mail else "NOT CHANGED"), TONE_FAILED
    if status == live.STATUS_CANCELLED:
        return ("UNDONE - NOT SENT" if mail else "UNDONE - NOT CHANGED"), TONE_UNDONE
    return "", ""
