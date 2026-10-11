"""The day plan's arithmetic (the productivity bundle's "plan my day"): which hours are planned, the
free stretches between the events read, what needs time, the <todos> rows, and the checks every
proposed time block must pass.

    plan_window(now, part, settings, zone)   PlanWindow (the day and hours planned) or a reason word
                                             (PART_OVER, NO_HOURS); "plan my day" plans tomorrow only
                                             when it is late (is_late), never because today is full
    free_slots(events, window, own=)         the free stretches of at least MIN_SLOT_MIN minutes
    needs_time(inputs, now, typed_extra)     anything to plan at all?
    plan_todos(inputs, day)                  the pending to-dos not yet served by a block of ``day``,
                                             soonest due first, numbered t1, t2, ...
    todo_rows(inputs) / row_targets(inputs)  the <todos> rows (t1.., d1.., e1) and what each id names
    DayCheck(...)                            validate.to_cards' ``extra``: rules 2-9 of the plan's
                                             checks (spec 8.1); ``served`` maps card id -> row id

Every time here is a naive wall time in the personal calendar's time zone (``PlanWindow.zone``);
an event read from another calendar is converted with gcal.wall_in_zone (Python has no time zone
database on this PC: the offset comes from Windows' ICU). Pure: no Google, no Claude, nothing
logged; texts (titles) only reach the card's reason, never a log.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Any

from ..actions import CALENDAR, ProposedAction
from ..gcal import EventBrief, wall_in_zone
from ..recipients import same_mailbox
from .context import _ADDRESS_RE, data
from .modes import ADDRESS_MASK, ModeInputs, TodoFact, plan_data

PART_DAY, PART_MORNING, PART_AFTERNOON, PART_EVENING, PART_TOMORROW = (
    "day", "morning", "afternoon", "evening", "tomorrow")
PARTS = (PART_DAY, PART_MORNING, PART_AFTERNOON, PART_EVENING, PART_TOMORROW)
PART_OVER, NO_HOURS = "part_over", "no_hours"     # plan_window's reasons for no window
BUFFER_MIN = 10           # minutes kept free before and after a meeting with other guests
MIN_SLOT_MIN = 25         # a free stretch shorter than this is no slot
BLOCK_MIN = 15            # a block lasts 15 minutes ...
BLOCK_MAX = 180           # ... to 3 hours
START_MARGIN_MIN = 2      # a block starts at least this long after now
DEADLINE_DAYS = 7         # a deadline due within this many days needs time
LABELS = {PART_DAY: "today", PART_MORNING: "this morning", PART_AFTERNOON: "this afternoon",
          PART_EVENING: "this evening", PART_TOMORROW: "tomorrow"}
_NOON, _FIVE_PM = time(12, 0), time(17, 0)

# DayCheck's reasons (shown on the information-only card and in LIVE; never logged).
NOT_TIMED = "A plan block needs a start and an end time"
REPEATS = "A plan block doesn't repeat"
OTHER_DAY = "That's not the day being planned"
IN_THE_PAST = "This block starts in the past"
OUTSIDE = "Outside the hours Jarvis plans ({hours})"
LENGTH = "A plan block is 15 minutes to 3 hours"
OVERLAPS = "Overlaps {title} at {at}"
TOO_CLOSE = "Too close to {title} at {at}"
NOT_FREE = f"Not inside a free stretch of at least {MIN_SLOT_MIN} minutes"
OVERLAPS_BLOCK = "Overlaps another block of this plan"
ADDRESS_OR_LINK = "A plan block names no address or link"
NO_PLACE = "A plan block has no place"      # the grammar keeps "where" empty (it would be the event's location)
REPEATS_NOTE = "This block repeats one of your notes"
SERVES_NOTHING = "This block serves nothing on your list"
ROW_WORDS, ROW_BRIEFING = "words", "briefing"   # the two "For:" ids that are no <todos> row
WORDS_TITLE = "your words"
BRIEFING_TITLE = "a deadline in the briefing"
EMAILS_TITLE = "emails waiting on you"
_FOR_RE = re.compile(r"for\s*:\s*([A-Za-z0-9_]{1,24})", re.IGNORECASE)
_LINK_RE = re.compile(r"://|\bwww\.", re.IGNORECASE)


@dataclass(frozen=True)
class PlanWindow:
    """The day and hours a plan covers (naive wall times in ``zone``, the personal calendar's)."""

    day: date
    start: datetime
    end: datetime
    zone: str
    label: str                 # "today" | "tomorrow" | "this morning" | "this afternoon" | "this evening"
    part: str = PART_DAY
    switched: bool = False     # "plan my day" planned tomorrow because it is late (R30)

    @property
    def tomorrow(self) -> bool:
        return self.label == LABELS[PART_TOMORROW]


@dataclass(frozen=True)
class Slot:
    """A free stretch of the plan window (naive wall times)."""

    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------

def _clock_time(text: str) -> time:
    hours, _, minutes = str(text).partition(":")
    return time(int(hours), int(minutes))


def to_wall(moment: datetime, zone: str) -> datetime:
    """``moment`` as a naive wall time in ``zone`` (gcal.wall_in_zone; naive: as it is)."""
    return wall_in_zone(moment, zone)


def _round_up_5(moment: datetime) -> datetime:
    base = moment.replace(second=0, microsecond=0)
    if base != moment:
        base += timedelta(minutes=1)
    return base + timedelta(minutes=(-base.minute) % 5)


def is_late(now: datetime, settings: Any, evening_from_hour: int = 18) -> bool:
    """"plan my day" plans tomorrow from now on (``now`` a naive wall time in the personal zone):
    less than [productivity] plan_min_free_minutes before day_end, or from [agenda]
    evening_from_hour on (R30). A full day earlier is never a reason."""
    day_end = datetime.combine(now.date(), _clock_time(settings.day_end))
    return now >= day_end - timedelta(minutes=int(settings.plan_min_free_minutes)) or now.hour >= evening_from_hour


def plan_window(now: datetime, part: str, settings: Any, zone: str, *, evening_from_hour: int = 18) -> PlanWindow | str:
    """The day and hours ``part`` plans, from ``now`` (aware, or a naive wall time) in the personal
    calendar's ``zone``: PART_DAY from 5 minutes after now (rounded up to 5) to day_end, or tomorrow's
    day_start-day_end when it is late (``switched``); the morning (day_start-12:00), afternoon
    (12:00-17:00) and evening (17:00-day_end) clipped to now and to day_start-day_end; PART_TOMORROW
    tomorrow's day_start-day_end. PART_OVER when less than MIN_SLOT_MIN minutes of the part are
    left, NO_HOURS when the part has no hours between day_start and day_end."""
    wall = to_wall(now, zone)
    today = wall.date()
    start_at, end_at = _clock_time(settings.day_start), _clock_time(settings.day_end)
    if datetime.combine(today, end_at) <= datetime.combine(today, start_at):
        return NO_HOURS
    earliest = _round_up_5(wall) + timedelta(minutes=5)
    if part == PART_TOMORROW or (part == PART_DAY and is_late(wall, settings, evening_from_hour)):
        tomorrow = today + timedelta(days=1)
        return PlanWindow(tomorrow, datetime.combine(tomorrow, start_at), datetime.combine(tomorrow, end_at), zone,
                          LABELS[PART_TOMORROW], PART_DAY if part == PART_DAY else PART_TOMORROW,
                          switched=part == PART_DAY)
    day_start, day_end = datetime.combine(today, start_at), datetime.combine(today, end_at)
    if part == PART_DAY:
        return PlanWindow(today, max(earliest, day_start), day_end, zone, LABELS[PART_DAY], PART_DAY)
    bounds = {PART_MORNING: (start_at, _NOON), PART_AFTERNOON: (_NOON, _FIVE_PM), PART_EVENING: (_FIVE_PM, end_at)}
    if part not in bounds:
        return NO_HOURS
    first = max(datetime.combine(today, bounds[part][0]), day_start)
    last = min(datetime.combine(today, bounds[part][1]), day_end)
    if last - first < timedelta(minutes=MIN_SLOT_MIN):
        return NO_HOURS
    start = max(first, earliest)
    if last - start < timedelta(minutes=MIN_SLOT_MIN):
        return PART_OVER
    return PlanWindow(today, start, last, zone, LABELS[part], part)


# --------------------------------------------------------------------------
# Free time
# --------------------------------------------------------------------------

def _other_guest(brief: EventBrief, own: Collection[str]) -> bool:
    owned = {str(address).casefold() for address in own}
    for guest in brief.guests:
        address = (guest.email or "").strip()
        if address and address.casefold() not in owned and not any(same_mailbox(address, mine) for mine in own):
            return True
    return False


def busy_event(brief: EventBrief) -> bool:
    """A timed event that takes its time: not all-day, not declined, not "show as free" and not a
    working-location entry (R21)."""
    return (brief.start is not None and not brief.all_day and brief.self_response != "declined"
            and not brief.free)


def event_span(brief: EventBrief, zone: str) -> tuple[datetime, datetime] | None:
    """A timed event's (start, end) as naive wall times in ``zone`` (None when it has no time)."""
    if brief.start is None or brief.all_day:
        return None
    start = to_wall(brief.start, zone)
    end = to_wall(brief.end, zone) if brief.end is not None else start
    return start, max(start, end)


def _busy(events: Mapping[str, Sequence[EventBrief]], zone: str,
          own: Collection[str]) -> list[tuple[datetime, datetime, EventBrief, bool]]:
    """(start, end, event, buffered) of every busy event (buffered: it has another guest)."""
    found = []
    for briefs in events.values():
        for brief in briefs or ():
            if not busy_event(brief):
                continue
            span = event_span(brief, zone)
            if span is None or span[1] <= span[0]:
                continue
            found.append((span[0], span[1], brief, _other_guest(brief, own)))
    found.sort(key=lambda item: (item[0], item[1]))
    return found


def free_slots(events: Mapping[str, Sequence[EventBrief]], window: PlanWindow, *,
               own: Collection[str] = ()) -> tuple[Slot, ...]:
    """The free stretches of ``window`` of at least MIN_SLOT_MIN minutes: the window minus every busy
    event of every account (declined, "show as free" and working-location events, and all-day ones,
    leave their time free; R21), with BUFFER_MIN minutes kept before and after an event that has a
    guest besides the owner (``own``: his own addresses). Events are converted to the window's zone."""
    pad = timedelta(minutes=BUFFER_MIN)
    busy = sorted(((start - pad, end + pad) if buffered else (start, end))
                  for start, end, _brief, buffered in _busy(events, window.zone, own))
    slots: list[Slot] = []
    cursor = window.start
    for first, last in busy:
        if cursor >= window.end:
            break
        if first > cursor:
            slots.append(Slot(cursor, min(first, window.end)))
        cursor = max(cursor, last)
    if cursor < window.end:
        slots.append(Slot(cursor, window.end))
    return tuple(slot for slot in slots if slot.minutes >= MIN_SLOT_MIN)


def free_minutes(slots: Sequence[Slot]) -> int:
    return sum(slot.minutes for slot in slots)


# --------------------------------------------------------------------------
# What needs time, and the <todos> rows
# --------------------------------------------------------------------------

def _due_key(due: str) -> str:
    return (due or "9999-12-31").replace("T", " ")


def plan_todos(inputs: ModeInputs, day: date) -> tuple[TodoFact, ...]:
    """The pending to-dos a plan of ``day`` sees: without those a plan block of that day already
    serves (R28), soonest due first (no due date last), numbered t1, t2, ... (Jarvis's own row ids,
    never card ids)."""
    kept = [todo for todo in inputs.todos if todo.planned != day.isoformat()]
    kept.sort(key=lambda todo: (_due_key(todo.due), todo.title.casefold()))
    return tuple(replace(todo, row=f"t{number}") for number, todo in enumerate(kept, 1))


def _deadline_parts(line: str) -> tuple[str, str]:
    title, _, due = str(line).rpartition(" | ")
    return (title, due) if title else (str(line), "")


def _due_date(due: str) -> date | None:
    try:
        return date.fromisoformat(due.strip()[:10])
    except ValueError:
        return None


def needs_time(inputs: ModeInputs, now: datetime, typed_extra: str = "") -> bool:
    """Is there anything to plan? The owner's own extra words always count (R52); a pending to-do
    that does not propose its own block (``inputs.todos`` already leaves out the ones a block of
    the day serves, R28); a deadline due within DEADLINE_DAYS days; follow-ups waiting on him."""
    if " ".join(str(typed_extra or "").split()):
        return True
    if any(not todo.block for todo in inputs.todos):
        return True
    limit = now.date() + timedelta(days=DEADLINE_DAYS)
    for line in inputs.deadlines:
        due = _due_date(_deadline_parts(line)[1])
        if due is not None and due <= limit:
            return True
    counts = inputs.followup_counts
    return bool(counts and counts[0] > 0)


def _todo_ids(inputs: ModeInputs) -> list[tuple[str, TodoFact]]:
    return [(todo.row or f"t{number}", todo) for number, todo in enumerate(inputs.todos, 1)]


def todo_rows(inputs: ModeInputs) -> tuple[tuple[str, str], ...]:
    """(row id, row) of the <todos> block: "t1 | todo | Read chapter 4 | due 2026-10-11 | from ask"
    (" | block proposed 15:00-16:00" when the card proposes its own block), "d1 | deadline | Lab
    report | 2026-10-12", and "e1 | emails | 4 wait on your answer | 2 owed to you" once follow-ups
    were scanned. Every field through context.data() (titles through modes.plan_data: no address)."""
    rows: list[tuple[str, str]] = []
    for row_id, todo in _todo_ids(inputs):
        due = f"due {data(todo.due.replace('T', ' '), 20)}" if todo.due else "no due date"
        text = f"{row_id} | todo | {plan_data(todo.title, 120) or '(untitled)'} | {due} | from {data(todo.source, 20)}"
        if todo.block:
            text += f" | block proposed {data(todo.block, 20)}"
        rows.append((row_id, text))
    for number, line in enumerate(inputs.deadlines, 1):
        title, due = _deadline_parts(line)
        rows.append((f"d{number}", f"d{number} | deadline | {plan_data(title, 120) or '(untitled)'} | {data(due, 20)}"))
    if inputs.followup_counts is not None:
        waiting, owed = inputs.followup_counts
        rows.append(("e1", f"e1 | emails | {int(waiting)} wait on your answer | {int(owed)} owed to you"))
    return tuple(rows)


def row_targets(inputs: ModeInputs) -> dict[str, TodoFact | str]:
    """Each <todos> row id -> its TodoFact (a to-do) or its title (a deadline; "emails waiting on you")."""
    targets: dict[str, TodoFact | str] = {row_id: todo for row_id, todo in _todo_ids(inputs)}
    for number, line in enumerate(inputs.deadlines, 1):
        targets[f"d{number}"] = _deadline_parts(line)[0]
    if inputs.followup_counts is not None:
        targets["e1"] = EMAILS_TITLE
    return targets


def row_title(row_id: str, targets: Mapping[str, TodoFact | str]) -> str:
    """What a block's "For: <row id>" names, in Jarvis's own words (its notes say "For: <this>")."""
    if row_id == ROW_WORDS:
        return WORDS_TITLE
    if row_id == ROW_BRIEFING:
        return BRIEFING_TITLE
    target = targets.get(row_id)
    if isinstance(target, TodoFact):
        return data(target.title, 120)
    return data(target or "", 120)


# --------------------------------------------------------------------------
# The checks of each proposed block (validate.to_cards' extra)
# --------------------------------------------------------------------------

def clock_text(moment: datetime, hour24: bool = False) -> str:
    if hour24:
        return f"{moment.hour:02d}:{moment.minute:02d}"
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def hours_text(start: datetime, end: datetime, hour24: bool = False) -> str:
    """"9:35-11:00 AM", "11:45 AM-1:50 PM", "14:00-15:30" (24-hour)."""
    first, last = clock_text(start, hour24), clock_text(end, hour24)
    if not hour24 and (start.hour < 12) == (end.hour < 12) and end - start < timedelta(hours=12):
        first = first[:-3]   # one AM / PM for both
    return f"{first}-{last}"


class DayCheck:
    """validate.to_cards' ``extra`` for a day plan: rules 2-9 of spec 8.1 (and an empty "where", as
    the plan grammar says), the first failing one wins; "" for a block that may be offered (it is
    recorded: a later block may not overlap it, and ``served`` maps its card id to the <todos> row
    its "For:" names). ``owner_words``: the typed
    extra words ("" = none: "For: words" is then refused); ``briefing``: <briefing> was given
    ("For: briefing" only then); ``notes``: the memory notes the run was given (a title repeating
    one is refused, R7)."""

    def __init__(self, window: PlanWindow, slots: Sequence[Slot], events: Sequence[tuple[str, EventBrief]],
                 now_wall: datetime, rows: Mapping[str, TodoFact | str], *, owner_words: str = "",
                 briefing: bool = False, notes: Sequence[str] = (), own: Collection[str] = (),
                 hour24: bool = False) -> None:
        self.window = window
        self.slots = tuple(slots)
        self.now_wall = now_wall
        self.rows = {str(key).casefold(): value for key, value in rows.items()}
        self.owner_words = " ".join(str(owner_words or "").split())
        self.briefing = bool(briefing)
        self.notes = tuple(notes)
        self.hour24 = hour24
        self.served: dict[str, str] = {}
        self._accepted: list[tuple[datetime, datetime]] = []
        self._events = _busy({"": [brief for _alias, brief in events]}, window.zone, own)

    def __call__(self, card: ProposedAction) -> str:
        reason, row = self._check(card)
        if not reason:
            assert card.start is not None and card.end is not None
            self._accepted.append((card.start, card.end))
            self.served[card.id] = row
        return reason

    def _check(self, card: ProposedAction) -> tuple[str, str]:
        if card.kind != CALENDAR:
            return "A day plan only adds time blocks", ""
        if card.all_day_start is not None or card.start is None:
            return NOT_TIMED, ""
        if card.repeat or card.rrule:
            return REPEATS, ""
        start = card.start.replace(tzinfo=None)
        end = (card.end or card.start).replace(tzinfo=None)
        if start.date() != self.window.day:
            return OTHER_DAY, ""
        if start < self.now_wall + timedelta(minutes=START_MARGIN_MIN):
            return IN_THE_PAST, ""
        if start < self.window.start or end > self.window.end:
            return OUTSIDE.format(hours=hours_text(self.window.start, self.window.end, self.hour24)), ""
        length = end - start
        if length < timedelta(minutes=BLOCK_MIN) or length > timedelta(minutes=BLOCK_MAX):
            return LENGTH, ""
        if not any(slot.start <= start and end <= slot.end for slot in self.slots):
            return self._clash(start, end), ""
        if any(start < last and first < end for first, last in self._accepted):
            return OVERLAPS_BLOCK, ""
        texts = (card.title, card.notes, card.where)
        if any(_names_address_or_link(text or "") for text in texts):
            return ADDRESS_OR_LINK, ""
        if " ".join(str(card.where or "").split()):
            return NO_PLACE, ""   # never a location: a note or briefing text there would reach Google
        if self.notes and _note_leak(card.title, self.notes, self.owner_words):
            return REPEATS_NOTE, ""
        row = self._row(card.notes)
        if not row:
            return SERVES_NOTHING, ""
        return "", row

    def _row(self, notes: str) -> str:
        match = _FOR_RE.fullmatch(" ".join(str(notes or "").split()))
        if match is None:
            return ""
        row = match.group(1).casefold()
        if row == ROW_WORDS:
            return row if self.owner_words else ""
        if row == ROW_BRIEFING:
            return row if self.briefing else ""
        return row if row in self.rows else ""

    def _clash(self, start: datetime, end: datetime) -> str:
        """Why a block is not inside a free slot: the first event it overlaps, else the first whose
        buffer it reaches, else that the free stretch there is too short."""
        pad = timedelta(minutes=BUFFER_MIN)
        for first, last, brief, _buffered in self._events:
            if start < last and first < end:
                return OVERLAPS.format(title=data(brief.title, 80) or "an event", at=clock_text(first, self.hour24))
        for first, last, brief, buffered in self._events:
            if buffered and start < last + pad and first - pad < end:
                return TOO_CLOSE.format(title=data(brief.title, 80) or "an event", at=clock_text(first, self.hour24))
        return NOT_FREE


def _names_address_or_link(text: str) -> bool:
    """8.1 rule 8: an address, a link, or plan_data's "[address]" stand-in copied from a masked title
    (it would put a placeholder in the event's title)."""
    return bool(_ADDRESS_RE.search(text) or _LINK_RE.search(text) or ADDRESS_MASK in text.casefold())


def _note_leak(text: str, notes: Sequence[str], owner_words: str) -> bool:
    from ..memory import note_leak

    return note_leak(text, notes, owner_words)
