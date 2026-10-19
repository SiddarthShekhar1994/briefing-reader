"""The reading window's TODAY / TOMORROW agenda and DEADLINES list (data only).

    agenda_window(now)               which day the agenda shows: (label, start, end)
    events_in_window(events, s, e)   the events that overlap that day
    event_rows(events, now)          CalendarEvents -> EventRow display tuples ("9:00 AM" / "09:00")
    extract_deadlines(lines)         pull the "Deadlines" section out of a page's FlatLines
    parse_deadline_line(text)        one line of that section -> Deadline (None if unreadable)
    deadlines_from_events(events)    calendar events whose title says they are deadlines
    merge_deadlines(page, cal, now)  one list: in the window, deduplicated, sorted, capped
    due_label(due, now)              ("TOMORROW", "high"), ("5D", "low"), ("OCT 20", "low")

The briefing task writes one line per deadline under a "Deadlines" heading:

    Deadline: <title> | <YYYY-MM-DD> or <YYYY-MM-DD HH:MM> | <source>

Page times are local wall times, like actions.py. Calendar times are aware and
are shown in ``now``'s time zone. The section, label and date rules are
actions.py's own helpers, so "Deadlines" behaves like "Proposed actions".
Qt-free and pure: no I/O, no network. Briefing content is personal, so only
counts are logged, never text.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, NamedTuple

from . import actions, text_prep
from .models import HEADING, FlatLine

if TYPE_CHECKING:
    from .gcal import CalendarEvent

logger = logging.getLogger(__name__)

TODAY_LABEL = "TODAY"
TOMORROW_LABEL = "TOMORROW"
DEFAULT_EVENING_FROM_HOUR = 18
DEFAULT_DEADLINE_DAYS = 14
DEFAULT_DEADLINE_LIMIT = 8
DEFAULT_HEADINGS = ("Deadlines",)
DEFAULT_KEYWORDS = ("due", "deadline", "exam", "midterm", "final", "quiz", "submit",
                    "submission", "assignment", "lab report", "application")

ORIGIN_PAGE = "page"
ORIGIN_CALENDAR = "calendar"
CALENDAR_SOURCE = "Calendar"

URGENCY_HIGH = "high"        # due today or tomorrow (or overdue)
URGENCY_MEDIUM = "medium"    # in 2-3 days
URGENCY_LOW = "low"

STATE_PAST = "past"
STATE_NOW = "now"
STATE_UPCOMING = "upcoming"
ALL_DAY_TEXT = "ALL DAY"
OVERDUE_TEXT = "OVERDUE"

_SOON_MINUTES = 60           # "in 20 min" up to this far ahead
_TONIGHT_FROM_HOUR = 18      # "TONIGHT 11:59 PM" vs "TODAY 3:00 PM"
_DAY_COUNT_LIMIT = 14        # "13D" is the last day count; later dates are "OCT 20"
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_SEPARATOR = " \u00b7 "      # middle dot, as in actions.py

_DEADLINE_LABELS = frozenset({"deadline", "deadlines", "due", "due date"})
_DUE_NOISE_RE = re.compile(r"[()\[\],;]")
_SPACES_RE = re.compile(r"\s+")
# Words around the date: "Fri 2026-10-09", "due by 2026-10-09 at 23:59", "2026-10-09T23:59".
_DUE_LEAD_RE = re.compile(
    rf"^(?:(?:due|by|on|at|before)\b|@|t(?=\d)|{actions._WEEKDAY_WORD}(?=\s|$))\s*")
_NON_WORD_RE = re.compile(r"[\W_]+")
# A link without a scheme, as meeting locations often are: "meet.google.com/abc-defg-hij".
_BARE_LINK_RE = re.compile(r"(?<![\w@.])(?:[a-z0-9-]+\.)+[a-z]{2,}/[^\s()<>|]*", re.I)


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------

def _wall(moment: datetime, now: datetime) -> datetime:
    """``moment`` as a naive wall time in ``now``'s time zone (naive times are already local).

    When ``now`` carries the machine's own offset (datetime.now().astimezone()),
    the machine's rules convert ``moment``, so an event after a daylight saving
    change shows its own local time (not one hour off) on the day before.
    """
    if moment.utcoffset() is None:
        return moment
    if now.utcoffset() is None or _has_machine_offset(now):
        return moment.astimezone().replace(tzinfo=None)
    return moment.astimezone(now.tzinfo).replace(tzinfo=None)


def _now_wall(now: datetime) -> datetime:
    return now.replace(tzinfo=None)


def _has_machine_offset(now: datetime) -> bool:
    """True when aware ``now`` has the offset this machine's time zone has at that wall time."""
    try:
        return now.replace(tzinfo=None).astimezone().utcoffset() == now.utcoffset()
    except (OverflowError, OSError, ValueError):
        return False


def _midnight(day: date, now: datetime) -> datetime:
    """00:00 on ``day`` in ``now``'s time zone.

    When ``now`` carries the machine's own offset (datetime.now().astimezone()),
    the machine's rules give the offset of ``day``, so a window across a daylight
    saving change still ends at local midnight.
    """
    naive = datetime.combine(day, time())
    if now.utcoffset() is None:
        return naive
    if _has_machine_offset(now):
        try:
            return naive.astimezone()
        except (OverflowError, OSError, ValueError):
            pass
    return naive.replace(tzinfo=now.tzinfo)


# --------------------------------------------------------------------------
# agenda_window
# --------------------------------------------------------------------------

def agenda_window(now: datetime, evening_from_hour: int = DEFAULT_EVENING_FROM_HOUR,
                  ) -> tuple[str, datetime, datetime]:
    """("TODAY", today 00:00, tomorrow 00:00) before ``evening_from_hour``.

    From that hour on: ("TOMORROW", tomorrow 00:00, the day after 00:00).
    The bounds are in ``now``'s time zone (naive when ``now`` is naive).
    """
    today = now.date()
    if now.hour < evening_from_hour:
        return TODAY_LABEL, _midnight(today, now), _midnight(today + timedelta(days=1), now)
    return (TOMORROW_LABEL, _midnight(today + timedelta(days=1), now),
            _midnight(today + timedelta(days=2), now))


def events_in_window(events: Iterable[CalendarEvent], start: datetime,
                     end: datetime) -> list[CalendarEvent]:
    """The events that overlap [``start``, ``end``), in their original order.

    Meant for agenda_window's bounds, so one list_events call over a longer
    range can feed both the agenda and the deadlines. Timed events are
    compared as wall times in ``start``'s time zone (an event ending exactly
    at ``start`` is left out; one without a duration counts at its start);
    all-day events by their days.
    """
    first_wall, end_wall = _now_wall(start), _now_wall(end)
    first_day = first_wall.date()
    last_day = (end_wall - timedelta(microseconds=1)).date()
    found: list[CalendarEvent] = []
    for event in events:
        if event.start is None:
            first = event.all_day_start
            last = max(event.all_day_end or first, first) if first is not None else None
            if first is not None and first <= last_day and last >= first_day:
                found.append(event)
            continue
        begin = _wall(event.start, start)
        finish = max(_wall(event.end, start), begin) if event.end is not None else begin
        if begin < end_wall and (finish > first_wall or (finish == begin and begin >= first_wall)):
            found.append(event)
    return found


# --------------------------------------------------------------------------
# event_rows
# --------------------------------------------------------------------------

class EventRow(NamedTuple):
    """One agenda row: ("9:00 AM" | "09:00" | "ALL DAY", title, meta, "past" | "now" | "upcoming")."""

    time_text: str
    title: str
    meta: str      # "now", "in 20 min", "ended", a location (never a URL), or ""
    state: str


def event_rows(events: Iterable[CalendarEvent], now: datetime, *,
               hour24: bool = False) -> list[EventRow]:
    """Display rows in time order (all-day events first on their day).

    Times are shown in ``now``'s time zone, as "9:00 AM" (or "09:00" with
    ``hour24``). The meta is "ended" for a past event; "now" or "in 20 min"
    (up to an hour ahead) joined with the location; else the location. A
    location that is only a link is left out.
    """
    return [_event_row(event, now, hour24) for event in ordered_events(events, now)]


def ordered_events(events: Iterable[CalendarEvent], now: datetime) -> list[CalendarEvent]:
    """``events`` in the order event_rows shows them (all-day events first on their day, then by start)."""
    return sorted(events, key=lambda event: _event_key(event, now))


def _event_key(event: CalendarEvent, now: datetime) -> tuple[datetime, int, str]:
    if event.start is not None:
        return _wall(event.start, now), 1, event.title.casefold()
    first = event.all_day_start or _now_wall(now).date()
    return datetime.combine(first, time()), 0, event.title.casefold()


def _event_row(event: CalendarEvent, now: datetime, hour24: bool) -> EventRow:
    place = display_location(event.location)
    current = _now_wall(now)
    if event.start is None:
        today = current.date()
        first = event.all_day_start or today
        last = max(event.all_day_end or first, first)
        state = STATE_PAST if last < today else STATE_NOW if first <= today else STATE_UPCOMING
        return EventRow(ALL_DAY_TEXT, event.title, place, state)
    start = _wall(event.start, now)
    end = max(_wall(event.end, now), start) if event.end is not None else start
    shown = text_prep.format_time(start, hour24=hour24)
    if start <= current < end:
        return EventRow(shown, event.title, _joined("now", place), STATE_NOW)
    if current < start:
        minutes = math.ceil((start - current).total_seconds() / 60)
        meta = _joined(f"in {minutes} min", place) if minutes <= _SOON_MINUTES else place
        return EventRow(shown, event.title, meta, STATE_UPCOMING)
    return EventRow(shown, event.title, "ended", STATE_PAST)


def _joined(*parts: str) -> str:
    return _SEPARATOR.join(part for part in parts if part)


def display_location(location: str) -> str:
    """The location without links ("" when it is only a meeting URL).

    "https://meet.google.com/x" -> "", "Room 4 (zoom.us/j/1)" -> "Room 4".
    """
    if not location or not location.strip():
        return ""
    text = text_prep.strip_markdown(location)      # removes scheme and www. links
    text = text_prep.strip_markdown(_BARE_LINK_RE.sub(" ", text))
    return text.rstrip(" :;,|").strip()


# --------------------------------------------------------------------------
# Deadline and extract_deadlines
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Deadline:
    """Something due: ``due`` is a date, or a datetime when a time was given.

    Page deadlines have naive local wall times; calendar ones keep the event's
    aware start. ``origin`` is "page" or "calendar".
    """

    title: str
    due: datetime | date
    source: str = ""
    origin: str = ORIGIN_PAGE

    @property
    def has_time(self) -> bool:
        return isinstance(self.due, datetime)


class _SkipLine(ValueError):
    """Why a Deadlines line was skipped (a fixed phrase: no briefing text)."""


def extract_deadlines(lines: Sequence[FlatLine],
                      heading_names: Sequence[str] | str = DEFAULT_HEADINGS, *,
                      stop_names: Sequence[str] | str = (),
                      ) -> tuple[list[Deadline], list[FlatLine]]:
    """Pull the "Deadlines" section out of a page.

    The section is found and removed exactly like actions.extract_actions
    finds the "Proposed actions" section, so it is never spoken. Returns
    (the readable deadlines in page order, the page without the section).
    Unreadable lines are skipped with a DEBUG log.

    A heading named in ``stop_names`` (the "Proposed actions" headings) ends
    the section at any level and is kept, so a "Proposed actions" heading
    nested inside "Deadlines" is left for extract_actions. Run this before
    extract_actions: then a "Deadlines" heading nested inside "Proposed
    actions" is found here too, instead of turning into proposal cards.
    """
    body, kept, sections = _split_section(lines, heading_names, stop_names)
    deadlines: list[Deadline] = []
    for line in body:
        deadline = parse_deadline_line(line.text)
        if deadline is not None:
            deadlines.append(deadline)
    if sections:
        logger.info("Deadlines: %d line(s), %d read, %d skipped", len(body), len(deadlines),
                    len(body) - len(deadlines))
    return deadlines, kept


def _split_section(lines: Sequence[FlatLine], heading_names: Sequence[str] | str,
                   stop_names: Sequence[str] | str = (),
                   ) -> tuple[list[FlatLine], list[FlatLine], int]:
    """(the section's lines with text, the page without the section, sections found).

    Uses actions.py's heading rules: a heading (any level) or a title-like
    paragraph whose cleaned title matches opens the section, which runs until
    the next heading of the same or a higher level, or until a heading named
    in ``stop_names`` (any level).
    """
    wanted = _heading_keys(heading_names)
    stops = _heading_keys(stop_names) - wanted
    structural = not any(line.kind == HEADING for line in lines)
    plain_level = actions._PARAGRAPH_TITLE_LEVEL if structural else max(
        (line.level or 1 for line in lines if line.kind == HEADING), default=1) + 1

    body: list[FlatLine] = []
    kept: list[FlatLine] = []
    open_level: int | None = None
    sections = 0
    for line in lines:
        title = actions._title_of(line, structural)
        if open_level is not None:
            stop = bool(stops) and actions._section_level(line, title, stops, plain_level) is not None
            if not stop and (title is None or title[1] > open_level):
                if title is None and actions._has_text(line):
                    body.append(line)
                continue
            open_level = None
        open_level = actions._section_level(line, title, wanted, plain_level)
        if open_level is not None:
            sections += 1
            continue
        kept.append(line)
    return body, kept, sections


def _heading_keys(names: Sequence[str] | str) -> set[str]:
    names = (names,) if isinstance(names, str) else tuple(names or ())
    return {key for key in map(actions._heading_key, names) if key}


def parse_deadline_line(text: str) -> Deadline | None:
    """"Deadline: Essay | 2026-10-09 23:59 | Canvas" -> Deadline; None when unreadable.

    Lenient like actions.py: markdown and list markers are ignored, the
    "Deadline:" label is optional, the source may be missing, the date may
    carry a weekday ("Fri 2026-10-09"), and a time that cannot be read
    ("11:59 PM ET", "EOD") leaves the date alone.
    """
    try:
        return _parse_deadline(text)
    except _SkipLine as exc:
        logger.debug("Deadlines: skipped a line (%s)", exc)
        return None


def _parse_deadline(text: str) -> Deadline:
    raw = actions._clean_line(text or "")
    if not raw:
        raise _SkipLine("empty line")
    body = raw
    label = actions._KIND_RE.match(raw)     # "Deadline:" like "Calendar:"
    if label is not None and _label_key(label.group("label")) in _DEADLINE_LABELS:
        body = raw[label.end():]
    if "|" not in body:
        raise _SkipLine("no fields (expected title | date | source)")
    fields = [field.strip() for field in body.split("|")]
    title = text_prep.strip_markdown(fields[0])     # a link in the title is noise
    if not title:
        raise _SkipLine("no title")
    due = _parse_due(fields[1])
    sources = (text_prep.strip_markdown(field) for field in fields[2:])
    source = " | ".join(part for part in sources if part)
    return Deadline(title=title, due=due, source=source, origin=ORIGIN_PAGE)


def _label_key(label: str) -> str:
    return " ".join(re.split(r"[\s_-]+", label.casefold())).strip()


def _parse_due(text: str) -> datetime | date:
    """"2026-10-09" -> date; "2026-10-09 23:59" / "2026-10-09 11:59 PM" -> datetime."""
    s = _DUE_NOISE_RE.sub(" ", actions._DASH_RE.sub("-", text).casefold())
    s = _SPACES_RE.sub(" ", s).strip().rstrip(".").strip()
    dates = list(actions._DATE_RE.finditer(s))
    if not dates:
        raise _SkipLine("no date (expected YYYY-MM-DD)")
    if len(dates) > 1:
        raise _SkipLine("more than one date")
    try:
        day = actions._make_date(dates[0])
    except actions._LineError:
        raise _SkipLine("not a real date") from None
    if _strip_due_words(s[:dates[0].start()]):
        raise _SkipLine("unreadable text before the date")
    rest = _strip_due_words(s[dates[0].end():])
    if not rest:
        return day
    try:
        minutes = actions._lone_minutes(actions._read_clock(rest))
    except actions._LineError:
        minutes = None
    if minutes is None or minutes >= 24 * 60:
        logger.debug("Deadlines: kept the date of a line whose time could not be read")
        return day
    return datetime.combine(day, time()) + timedelta(minutes=minutes)


def _strip_due_words(text: str) -> str:
    text = text.strip()
    while True:
        stripped = _DUE_LEAD_RE.sub("", text, count=1).strip()
        if stripped == text:
            return text
        text = stripped


# --------------------------------------------------------------------------
# Deadlines from the calendar, merging, labels
# --------------------------------------------------------------------------

def deadlines_from_events(events: Iterable[CalendarEvent], now: datetime,
                          days: int = DEFAULT_DEADLINE_DAYS,
                          keywords: Sequence[str] | str = DEFAULT_KEYWORDS) -> list[Deadline]:
    """Calendar events due within ``days`` whose title has one of ``keywords``.

    Keywords match whole words, case-insensitively ("Final exam" matches
    "final"; "Finals Week" and "Duet" match nothing). A timed event is due at
    its start; an all-day one on its first day, or on its last day while it is
    running. Like merge_deadlines, today's items count from 00:00.
    """
    pattern = _keyword_pattern(keywords)
    if pattern is None:
        return []
    found: list[Deadline] = []
    for event in events:
        if not pattern.search(event.title.casefold()):
            continue
        due = _event_due(event, now)
        if due is not None and _in_window(due, now, days):
            found.append(Deadline(title=event.title, due=due, source=CALENDAR_SOURCE,
                                  origin=ORIGIN_CALENDAR))
    return found


def _keyword_pattern(keywords: Sequence[str] | str) -> re.Pattern[str] | None:
    words = (keywords,) if isinstance(keywords, str) else tuple(keywords or ())
    parts = {r"[\s_-]+".join(map(re.escape, str(word).casefold().split()))
             for word in words if str(word or "").strip()}
    if not parts:
        return None
    alternatives = "|".join(sorted(parts, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)


def _event_due(event: CalendarEvent, now: datetime) -> datetime | date | None:
    if event.start is not None:
        return event.start
    first = event.all_day_start
    if first is None:
        return None
    last = max(event.all_day_end or first, first)
    return first if first >= _now_wall(now).date() else last


def _due_point(due: datetime | date, now: datetime) -> tuple[date, datetime]:
    """(the due day, the moment for sorting); a date without a time sorts at its end."""
    if isinstance(due, datetime):
        wall = _wall(due, now)
        return wall.date(), wall
    return due, datetime.combine(due, time.max)


def _in_window(due: datetime | date, now: datetime, days: int) -> bool:
    """Due from today 00:00 (today's items stay until the day ends) to ``now`` + ``days``."""
    current = _now_wall(now)
    day, point = _due_point(due, now)
    limit = current + timedelta(days=max(0, days))
    if day < current.date():
        return False
    return point <= limit if isinstance(due, datetime) else day <= limit.date()


def merge_deadlines(page: Iterable[Deadline], calendar: Iterable[Deadline], now: datetime,
                    days: int = DEFAULT_DEADLINE_DAYS,
                    limit: int = DEFAULT_DEADLINE_LIMIT) -> list[Deadline]:
    """Page and calendar deadlines as one list for the panel.

    Drops items already past (today's stay until the end of the day) and
    items beyond ``days``; drops duplicates (same due day and one casefolded
    title contained in the other, page items winning); sorts by due (a
    date without a time sorts at the end of its day); keeps ``limit``.
    """
    kept: list[Deadline] = []
    for item in (*page, *calendar):
        if not _in_window(item.due, now, days):
            continue
        if any(_same_deadline(item, other, now) for other in kept):
            continue
        kept.append(item)
    kept.sort(key=lambda item: (_due_point(item.due, now)[1], item.title.casefold()))
    return kept[:max(0, limit)]


def _same_deadline(a: Deadline, b: Deadline, now: datetime) -> bool:
    if _due_point(a.due, now)[0] != _due_point(b.due, now)[0]:
        return False
    first, second = _title_key(a.title), _title_key(b.title)
    if not first or not second:
        return first == second
    return f" {first} " in f" {second} " or f" {second} " in f" {first} "


def _title_key(title: str) -> str:
    return " ".join(_NON_WORD_RE.sub(" ", title.casefold()).split())


def due_label(due: datetime | date, now: datetime) -> tuple[str, str]:
    """(text, urgency) for the right edge of a deadline row.

    Today: "TODAY" for a date, "TODAY 3:00 PM" / "TONIGHT 11:59 PM" for a
    time (always 12-hour, like the proposal cards); then "TOMORROW", "2D" ..
    "13D", and "OCT 20" further out; a past day is "OVERDUE". Urgency:
    "high" up to tomorrow, "medium" for 2-3 days, else "low".
    """
    day, _ = _due_point(due, now)
    days = (day - _now_wall(now).date()).days
    if days < 0:
        text = OVERDUE_TEXT
    elif days == 0:
        text = _today_text(due, now)
    elif days == 1:
        text = TOMORROW_LABEL
    elif days < _DAY_COUNT_LIMIT:
        text = f"{days}D"
    else:
        text = f"{_MONTHS[day.month - 1]} {day.day}"
    urgency = URGENCY_HIGH if days <= 1 else URGENCY_MEDIUM if days <= 3 else URGENCY_LOW
    return text, urgency


def _today_text(due: datetime | date, now: datetime) -> str:
    if not isinstance(due, datetime):
        return TODAY_LABEL
    wall = _wall(due, now)
    word = "TONIGHT" if wall.hour >= _TONIGHT_FROM_HOUR else TODAY_LABEL
    return f"{word} {text_prep.format_time(wall)}"
