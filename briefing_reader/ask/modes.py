"""The productivity bundle's planner modes: what each run is told and from what (spec 5.2, 7).

A mode is a planner run of its own (ClaudeEngine.run_mode): its own system prompt and schema, no
tools, no mail search, no web research, no second run: the day plan, the meeting brief and the
drafted reply or nudge of a follow-up; also a meeting's summary (MODE_MEETING) and a desktop request
(MODE_DESKTOP, ask.desktop_mode: one JSON plan of refs, no card lines):

    ModeRequest(mode, origin, typed=, part=, ...)   what started it (typed, an offer, a click); the
                                     owner's own words only in ``typed``
    ModeInputs(todos, deadlines, ...)  the GUI thread's snapshot handed to the job (to-dos with the
                                     Todo card ids they come from, the merged deadlines, the owner's
                                     own addresses)
    todo_facts(cards, planned=)      the pending Todo cards as TodoFacts (the card id stays in memory;
                                     the run only ever sees Jarvis's row ids t1, t2, ...)
    command_text(request)            the fixed <command> (titles, names and addresses never enter
                                     it; only the owner's typed words may follow, "The owner's words: ")
    build_plan_prompt(...)           PlanPrompt: the plan's stdin (7.2), the ContextIndex of what it
                                     showed and the <todos> row ids it showed (a block serves only those)
    index_of(ctx, shown)             context's index of an AskContext and the events shown
    build_prep_prompt(...)           a meeting brief's stdin (7.3): the meeting's account, the meeting,
                                     <people> (ask.prep.people_block), the threads Jarvis read with its
                                     guests, the notes, the fixed command (never the owner's words)
    build_nudge_prompt(...)          a drafted reply or nudge's stdin (7.4): the thread's account,
                                     <followup> (kind, who wrote last, days, account, thread), the one
                                     thread read, the notes, the fixed command for its kind and only the
                                     owner's own words from "Tell Jarvis what to say"
    nudge_guard(thread, target, own) "" or why no draft is made: the owner replied since (waiting),
                                     they answered since (owed), or anything else changed (R47)
    owed_target(thread, alias, own)  a brief's "owes you an answer" (R32): the thread as a ThreadTarget
                                     when the owner wrote last (followups' deciding-message rules)
    NudgeCheck(target, thread, notes, owner_words=, own=)   validate.to_cards' ``extra`` for the
                                     draft (8.3): that thread, that message, only its people, never a note

Every data field goes through context.data(), a plan's stdin is at most MAX_PLAN_STDIN characters
(a brief's at most an Ask's, context.MAX_STDIN_CHARS) and <command> is last and once. Pure;
nothing here is logged (the reprs name kinds only).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, NamedTuple

from .. import live
from ..actions import CALENDAR, EMAIL, REPLY, TODO, ProposedAction
from ..gcal import EventBrief
from . import usage
from .context import (
    MAX_STDIN_CHARS,
    AccountInfo,
    AskContext,
    BriefingContext,
    ContextIndex,
    _ADDRESS_RE,
    _event_row,
    _index,
    _mail_block,
    account_row,
    data,
    fit_mail,
    memory_block,
    memory_notes_shown,
    now_line,
)
from .mail import MailThread, trim_threads

MODE_PLAN, MODE_PREP, MODE_NUDGE = "plan", "prep", "nudge"
# Meetings part 2: a recorded meeting's summary (ask.meeting_summary). Not a bundle run: it counts as an Ask run.
MODE_MEETING = "meeting"
# Jarvis on your desktop (P1.7): a desktop request the owner typed (ask.desktop_mode). Claude only plans: its answer
# is one JSON plan of refs, never card lines (MODE_KINDS empty), which desktop.validate checks. Counts as an Ask run.
MODE_DESKTOP = "desktop"
ORIGIN_OFFER, ORIGIN_TYPED, ORIGIN_CHOICE, ORIGIN_LIST, ORIGIN_BRIEF = "offer", "typed", "choice", "list", "brief"
# A summary runs only when the owner asked for it: ORIGIN_AUTO - his own "summarise it" tick on that meeting's
# start card (unticked every time), once the transcript is ready; ORIGIN_SUMMARISE - his click on Summarise.
ORIGIN_AUTO, ORIGIN_SUMMARISE = "auto", "summarise"
MODE_KINDS = {MODE_PLAN: frozenset({CALENDAR}), MODE_PREP: frozenset({REPLY}), MODE_NUDGE: frozenset({REPLY}),
              MODE_MEETING: frozenset({EMAIL}), MODE_DESKTOP: frozenset()}
# A desktop plan has no lines: at most this many steps (desktop_schema.json's steps maxItems).
MODE_MAX_LINES = {MODE_PLAN: 8, MODE_PREP: 2, MODE_NUDGE: 1, MODE_MEETING: 3, MODE_DESKTOP: 12}
MODE_TASKS = {MODE_PLAN: live.TASK_PLAN, MODE_PREP: live.TASK_PREP, MODE_NUDGE: live.TASK_NUDGE,
              MODE_MEETING: live.TASK_MEETING, MODE_DESKTOP: live.TASK_DESKTOP}
MODE_USAGE = {MODE_PLAN: usage.KIND_PLAN_DAY, MODE_PREP: usage.KIND_PREP, MODE_NUDGE: usage.KIND_NUDGE,
              MODE_MEETING: usage.KIND_PLAN, MODE_DESKTOP: usage.KIND_PLAN}
MODE_TITLES = {MODE_PLAN: "Plan", MODE_PREP: "Prep", MODE_NUDGE: "Nudge", MODE_MEETING: "Meeting",
               MODE_DESKTOP: "Desktop"}
FIXED_COMMANDS: dict[Any, str] = {
    MODE_PLAN: "Plan the day in <day> for the owner: propose time blocks inside <free> for what needs time in "
               "<todos> and <briefing>, as Calendar lines, or ask one question.",
    MODE_PREP: "Prepare a short brief for the meeting in <meeting>: where each thread in <mail> stands, what is "
               "still open, and points worth raising.",
    (MODE_NUDGE, "waiting"): "Draft a reply from the owner to the newest message of the thread in <mail>, as "
                             "<followup> says.",
    (MODE_NUDGE, "owed"): "Draft a short, polite follow-up from the owner in the thread in <mail>, about the "
                          "owner's last message, as <followup> says.",
    MODE_MEETING: "Summarise the meeting in <transcript> for the owner: a title, an overview, the key points by "
                  "topic, the decisions, the action items (who and what), the open questions, follow-up drafts to "
                  "guests in <guests> only where the meeting clearly calls for one, and facts worth keeping.",
    # A meeting not linked to a calendar meeting: no guests, so the follow-ups are plain notes, never drafts.
    (MODE_MEETING, "plain"): "Summarise the meeting in <transcript> for the owner: a title, an overview, the key "
                             "points by topic, the decisions, the action items (who and what), the open questions, "
                             "the follow-ups as plain notes (who and what, never an email), and facts worth keeping.",
    # The owner's typed words follow ("The owner's words: ..."): they are the request.
    MODE_DESKTOP: "Plan what the owner asks for on his desktop as one JSON plan: only the ids given in <places>, "
                  "<files>, <apps> and <sites>, only opening, showing and finding, or ask one question.",
}
OWNER_WORDS = "The owner's words: "
OWNER_WORDS_CAP = 300
MAX_PLAN_STDIN = 24_000
MAX_CALENDAR_ROWS = 60
MAX_TODO_ROWS = 40
PLAN_BRIEFING_CAP = 6_000
ADDRESS_MASK = "[address]"
_NONE = "none"
_ONE_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class EventRef:
    """One event by its account, calendar and id (and the start it had when it was offered)."""

    alias: str
    calendar_id: str
    event_id: str
    start: datetime

    def __repr__(self) -> str:
        return f"EventRef({self.alias})"


@dataclass(frozen=True)
class ThreadRef:
    alias: str
    thread_id: str
    last_at: datetime

    def __repr__(self) -> str:
        return f"ThreadRef({self.alias})"


@dataclass(frozen=True)
class ThreadTarget:
    alias: str
    thread_id: str
    kind: str                                        # "waiting" | "owed"
    last_gmail_id: str                               # the deciding message
    auto_ids: frozenset[str] = frozenset()           # automated messages newer than it

    def __repr__(self) -> str:
        return f"ThreadTarget({self.alias}, {self.kind})"


@dataclass(frozen=True)
class ModeRequest:
    """What started a mode run. ``typed`` holds only the owner's own words (a plan's extra words);
    ``typed_route``: it came from the command bar, so only then is the bar cleared (R23)."""

    mode: str
    origin: str
    typed: str = ""
    part: str = ""                   # plan: dayplan.PART_*
    event: EventRef | None = None
    target: str = ""
    thread: ThreadTarget | None = None
    offer_id: int = 0
    typed_route: bool = False
    # A meeting summary's job (ask.meeting_summary.MeetingJob: the meeting's id, its transcript file and the
    # invite's facts), or a desktop request's (ask.desktop_mode.DesktopJob: its run id and the context's candidates);
    # memory only, never logged.
    payload: Any = field(default=None, repr=False, compare=False)

    def __repr__(self) -> str:
        return f"ModeRequest({self.mode}, {self.origin})"


@dataclass(frozen=True)
class TodoFact:
    """A pending Todo card as the plan sees it: Jarvis's row id (t1, t2, ...; dayplan.plan_todos
    numbers them), the card's id (memory only: never in the stdin), its title, due date (as the
    card has it, "2026-10-11" or "2026-10-11T23:59"), source ("briefing" | "ask"), the block it
    proposes itself ("15:00-16:00"; "" for none) and the day a created plan block serves it on
    ("2026-10-10"; "" while none does, R28)."""

    row: str
    action_id: str
    title: str
    due: str
    source: str
    block: str = ""
    planned: str = ""

    def __repr__(self) -> str:
        return f"TodoFact({self.row or '-'}, {len(self.title)} chars)"


@dataclass(frozen=True)
class ModeInputs:
    """The GUI thread's snapshot handed to a mode job (memory only)."""

    todos: tuple[TodoFact, ...] = ()
    deadlines: tuple[str, ...] = ()                          # "title | YYYY-MM-DD[ HH:MM]" (merged)
    followup_counts: tuple[int, int] | None = None           # (waiting, owed): a later build
    participants: Mapping[str, tuple[ThreadRef, ...]] = field(default_factory=dict, hash=False)
    own: frozenset[str] = frozenset()                        # the owner's own addresses known to the app
    evening_from_hour: int = 18                              # [agenda] evening_from_hour (R30)

    def __repr__(self) -> str:
        return f"ModeInputs({len(self.todos)} to-do(s), {len(self.deadlines)} deadline(s))"


def todo_facts(cards: Sequence[ProposedAction], *, planned: Mapping[str, date] | None = None) -> tuple[TodoFact, ...]:
    """The decidable, error-free Todo cards among ``cards`` (the pending ones, briefing and Ask) as
    TodoFacts, without row ids yet; ``planned``: card id -> the day a created plan block serves it."""
    planned = planned or {}
    facts: list[TodoFact] = []
    seen: set[str] = set()
    for card in cards:
        if card.kind != TODO or not card.decidable or card.error or card.id in seen:
            continue
        seen.add(card.id)
        block = ""
        if card.start is not None and card.end is not None:
            block = f"{card.start:%H:%M}-{card.end:%H:%M}"
        day = planned.get(card.id)
        facts.append(TodoFact("", card.id, card.title or card.raw, card.field("due"), card.source or "briefing",
                              block, day.isoformat() if day is not None else ""))
    return tuple(facts)


def command_text(request: ModeRequest, kind: str = "") -> str:
    """The <command> of a mode run: its fixed words (FIXED_COMMANDS; never a title, name or address),
    then "The owner's words: ..." only when the owner typed some."""
    fixed = FIXED_COMMANDS.get((request.mode, kind)) or FIXED_COMMANDS.get(request.mode, "")
    words = data(request.typed, OWNER_WORDS_CAP)
    return f"{fixed}\n{OWNER_WORDS}{words}" if words else fixed


def index_of(ctx: AskContext, shown: Sequence[tuple[str, EventBrief]]) -> ContextIndex:
    """context's ContextIndex of ``ctx`` with ``shown`` the events the text showed (no briefing card
    lines): every address typed in the command is the owner's, a note's is known, never typed."""
    return _index(ctx, shown, ())


# --------------------------------------------------------------------------
# The day plan's stdin (spec 7.2)
# --------------------------------------------------------------------------

def plan_data(text: Any, cap: int = 0) -> str:
    """context.data() for the plan's titles, rows and briefing lines, with every email address as
    "[address]": a plan block may name none (8.1 rule 8), so nobody's address is sent for it. The
    owner's own notes and typed words are not masked."""
    return data(_ADDRESS_RE.sub(ADDRESS_MASK, data(text)), cap)


def _wall_text(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")


def calendar_rows(events: Mapping[str, Sequence[EventBrief]], window: Any, *, redact: bool = False
                  ) -> tuple[list[str], list[tuple[str, EventBrief]]]:
    """The <calendar> rows of the plan's day ("personal | Dentist | 2026-10-10 11:00-11:45 | busy";
    status busy, free ("show as free" or a working location), declined or all day; times in the
    personal zone), timed ones by start then the all-day ones, at most MAX_CALENDAR_ROWS; and the
    (alias, event) shown. No guests, organizers, addresses or event ids. ``redact``: each title as
    its size (--plan-dry-run)."""
    from .dayplan import event_span

    timed: list[tuple[datetime, str, EventBrief, str]] = []
    whole: list[tuple[date, str, EventBrief, str]] = []
    day = window.day
    for alias, briefs in events.items():
        for brief in briefs or ():
            if brief.all_day_start is not None:
                last = brief.all_day_end or brief.all_day_start
                if brief.all_day_start <= day <= last:
                    when = (f"{brief.all_day_start.isoformat()} all day" if last == brief.all_day_start
                            else f"{brief.all_day_start.isoformat()} to {last.isoformat()} all day")
                    whole.append((brief.all_day_start, alias, brief, when))
                continue
            span = event_span(brief, window.zone)
            if span is None:
                continue
            start, end = span
            if not (start.date() <= day <= max(start, end - _ONE_MINUTE).date()):
                continue
            when = (f"{_wall_text(start)}-{end:%H:%M}" if end.date() == start.date()
                    else f"{_wall_text(start)} to {_wall_text(end)}")
            timed.append((start, alias, brief, when))
    timed.sort(key=lambda item: (item[0], item[1]))
    whole.sort(key=lambda item: (item[0], item[1]))
    rows: list[str] = []
    shown: list[tuple[str, EventBrief]] = []
    for _key, alias, brief, when in [*timed, *whole]:
        if len(rows) >= MAX_CALENDAR_ROWS:
            break
        title = (f"[title: {len(brief.title)} characters]" if redact else plan_data(brief.title, 120)) or "(No title)"
        rows.append(" | ".join((data(alias, 24), title, when, _status(brief))))
        shown.append((alias, brief))
    left = len(timed) + len(whole) - len(rows)
    if left > 0:
        rows.append(f"[{left} more left out]")
    return rows, shown


def _status(brief: EventBrief) -> str:
    if brief.all_day_start is not None:
        return "all day"
    if brief.self_response == "declined":
        return "declined"
    if brief.free:
        return "free"
    return "busy"


def _briefing_text(briefing: BriefingContext | None, *, redact: bool = False, cap: int = PLAN_BRIEFING_CAP) -> str:
    """The briefing's readable sections (heading, lines), no card lines and no deadlines (those are
    <todos> rows), at most ``cap`` characters; "none" without one."""
    if briefing is None or not briefing.sections:
        return _NONE
    lines: list[str] = []
    for heading, items in briefing.sections:
        if redact:
            lines.append(f"# [heading: {len(heading)} characters]" if heading else "#")
            lines += [f"- [line: {len(item)} characters]" for item in items if item]
            continue
        lines.append(f"# {plan_data(heading, 120)}" if heading else "#")
        lines += [f"- {plan_data(item, 1000)}" for item in items if plan_data(item, 1000)]
    text = ""
    for line in lines:
        if len(text) + len(line) + 1 > cap:
            text += "[the rest of the briefing is left out]\n"
            break
        text += line + "\n"
    return text.rstrip("\n") or _NONE


def _redacted_rows(rows: Sequence[tuple[str, str]]) -> list[str]:
    """--plan-dry-run: each to-do and deadline title as its size."""
    shown = []
    for _row_id, text in rows:
        parts = text.split(" | ")
        if len(parts) >= 3 and parts[1] in ("todo", "deadline"):
            parts[2] = f"[title: {len(parts[2])} characters]"
        shown.append(" | ".join(parts))
    return shown


class PlanPrompt(NamedTuple):
    """The day plan's stdin, the ContextIndex of what it showed, and the <todos> row ids it showed
    (t1, d1, e1, ...: a row cut to fit MAX_PLAN_STDIN is not among them, so no block may serve it)."""

    text: str
    index: ContextIndex
    rows: frozenset[str]


def day_line(window: Any, max_blocks: int) -> str:
    """"plan for=2026-10-10 (today) | from=09:35 | until=22:00 | time zone=... | at most 5 blocks"."""
    return (f"plan for={window.day.isoformat()} ({window.label}) | from={window.start:%H:%M} | "
            f"until={window.end:%H:%M} | time zone={data(window.zone, 60) or 'unknown'} | "
            f"at most {int(max_blocks)} block{'' if max_blocks == 1 else 's'}")


def build_plan_prompt(now: datetime, zone: str, owner: str, window: Any, events: Mapping[str, Sequence[EventBrief]],
                      slots: Sequence[Any], inputs: ModeInputs, briefing: BriefingContext | None,
                      memory: Sequence[str], request: ModeRequest, *, redact: bool = False, max_blocks: int = 5,
                      accounts: Sequence[AccountInfo] = ()) -> PlanPrompt:
    """The day plan's stdin (spec 7.2): <now> <owner> <day> <calendar> <free> <todos> <briefing>
    [<memory>] <command>, at most MAX_PLAN_STDIN characters (the to-do rows, then the calendar rows,
    then the briefing lines are cut first), the ContextIndex of what it showed (no guest, no name: a
    plan block names nobody) and the <todos> row ids it showed. ``now`` aware or a wall time;
    ``inputs.todos`` already numbered (dayplan.plan_todos). ``redact``: titles, briefing lines and
    notes as their sizes."""
    from .dayplan import to_wall, todo_rows

    wall = to_wall(now, zone)
    command = command_text(request)
    rows, shown = calendar_rows(events, window, redact=redact)
    all_rows = todo_rows(inputs)
    todo_lines = _redacted_rows(all_rows) if redact else [text for _row_id, text in all_rows]
    notes = tuple(memory)
    memory_text = memory_block(notes, redact=redact) if memory_notes_shown(notes) else ""
    free = [f"{slot.start:%Y-%m-%d %H:%M}-{slot.end:%H:%M} ({slot.minutes} min)" for slot in slots]
    briefing_cap = PLAN_BRIEFING_CAP

    def text_of(todos: Sequence[str], calendar: Sequence[str], cap: int) -> str:
        blocks = [f"<now>\n{now_line(wall, zone)}\n</now>", f"<owner>{data(owner, 20)}</owner>",
                  f"<day>{day_line(window, max_blocks)}</day>",
                  "<calendar>\n" + ("\n".join(calendar) if calendar else "no events") + "\n</calendar>",
                  "<free>\n" + ("\n".join(free) if free else _NONE) + "\n</free>",
                  "<todos>\n" + ("\n".join(todos) if todos else _NONE) + "\n</todos>",
                  f"<briefing>\n{_briefing_text(briefing, redact=redact, cap=cap)}\n</briefing>"]
        if memory_text:
            blocks.append(memory_text)
        blocks.append(f"<command>\n{command}\n</command>")   # Jarvis's fixed words; the owner's through data()
        return "\n".join(blocks) + "\n"

    def cut(lines: Sequence[str], keep: int, words: str) -> list[str]:
        return list(lines[:keep]) + ([f"[{len(lines) - keep} more {words} left out]"] if len(lines) > keep else [])

    keep_todos, keep_rows = min(len(todo_lines), MAX_TODO_ROWS), len(rows)
    text = text_of(cut(todo_lines, keep_todos, "to-do rows"), rows, briefing_cap)
    while len(text) > MAX_PLAN_STDIN and keep_todos > 0:
        keep_todos = max(0, keep_todos - 5)
        text = text_of(cut(todo_lines, keep_todos, "to-do rows"), rows, briefing_cap)
    while len(text) > MAX_PLAN_STDIN and keep_rows > 0:
        keep_rows = max(0, keep_rows - 5)
        text = text_of(cut(todo_lines, keep_todos, "to-do rows"), cut(rows, keep_rows, "events"), briefing_cap)
    shown = shown[:keep_rows]
    while len(text) > MAX_PLAN_STDIN and briefing_cap > 200:
        briefing_cap //= 2
        text = text_of(cut(todo_lines, keep_todos, "to-do rows"), cut(rows, keep_rows, "events"), briefing_cap)
    # A plan block names nobody: the events are indexed without their guests and organizers.
    bare = [(alias, replace(brief, guests=(), organizer_email="", organizer_name="")) for alias, brief in shown]
    ctx = AskContext(now=wall, time_zone=zone, command=command, accounts=tuple(accounts),
                     events={alias: tuple(brief for name, brief in bare if name == alias) for alias, _b in bare},
                     memory=notes, owner=owner)
    return PlanPrompt(text, index_of(ctx, bare), frozenset(row_id for row_id, _text in all_rows[:keep_todos]))


# --------------------------------------------------------------------------
# A meeting brief's stdin (spec 7.3)
# --------------------------------------------------------------------------

def build_prep_prompt(now: datetime, owner: str, account: AccountInfo, meeting_alias: str, meeting: EventBrief,
                      people_rows: str, threads: Sequence[MailThread], memory: Sequence[str],
                      request: ModeRequest, *, accounts: Sequence[AccountInfo] = (), zone: str = "",
                      redact: bool = False) -> tuple[str, ContextIndex]:
    """A meeting brief's stdin (spec 7.3): <now> <owner> <accounts> (the meeting's account only)
    <meeting> (context's event row: title, times, organizer, the first 10 guests) <people> (Jarvis's
    lookups, ask.prep.people_block) <mail> (the threads read, ask.mail.trim_threads at most 3, then
    context.fit_mail; only when there are any) [<memory>] <command> (the fixed words: a brief never
    carries the owner's typed words, which only picked the meeting). The ContextIndex: the meeting
    with its guests (SOURCE_GUEST), the threads as shown with their messages' ids and people
    (SOURCE_MAIL), the notes' addresses (SOURCE_MEMORY) and every account's alias and address
    (``accounts``: a Reply from another account is refused by name, not as unknown). ``redact``:
    each email's text and each note as its size."""
    from .dayplan import to_wall

    wall = to_wall(now, zone) if zone else now
    command = command_text(replace(request, typed=""))
    mail = fit_mail(tuple(trim_threads(list(threads), max_threads=3)), redact=redact)
    notes = tuple(memory)
    memory_text = memory_block(notes, redact=redact) if memory_notes_shown(notes) else ""
    blocks = [f"<now>\n{now_line(wall, zone)}\n</now>", f"<owner>{data(owner, 20)}</owner>",
              f"<accounts>\n{account_row(account)}\n</accounts>",
              f"<meeting>\n{_event_row(meeting_alias, meeting)}\n</meeting>",
              f"<people>\n{people_rows or _NONE}\n</people>"]
    if mail:
        blocks.append(f"<mail>\n{_mail_block(mail, redact=redact)}\n</mail>")
    if memory_text:
        blocks.append(memory_text)
    blocks.append(f"<command>\n{command}\n</command>")   # Jarvis's fixed words only
    text = "\n".join(blocks) + "\n"
    if len(text) > MAX_STDIN_CHARS and mail:   # never expected (every part is capped): no mail rather than more
        return build_prep_prompt(now, owner, account, meeting_alias, meeting, people_rows, (), memory, request,
                                 accounts=accounts, zone=zone, redact=redact)
    ctx = AskContext(now=wall, time_zone=zone, command=command, accounts=tuple(accounts) or (account,),
                     events={meeting_alias: (meeting,)}, mail=mail, memory=notes, owner=owner)
    return text, index_of(ctx, [(meeting_alias, meeting)])


# --------------------------------------------------------------------------
# A drafted reply or nudge (spec 3.4, 7.4, 8.3)
# --------------------------------------------------------------------------

KIND_WAITING, KIND_OWED = "waiting", "owed"
FOLLOWUP_WORDS = {KIND_WAITING: "waiting on you", KIND_OWED: "owed to you"}
GUARD_REPLIED, GUARD_ANSWERED, GUARD_CHANGED = "replied_since", "answered_since", "changed"
NUDGE_OTHER_THREAD = "This draft answers another thread"
NUDGE_WRONG_MESSAGE = "This draft answers the wrong message"
NUDGE_EXTRA_PEOPLE = "This draft adds people who aren't in the thread"
NUDGE_OWN_ADDRESS = "This draft would go to your own address"
NUDGE_NOTE_LEAK = "This draft repeats one of your private notes, so Jarvis holds it back"
NUDGE_ONLY_REPLY = "A follow-up draft is one reply"


def _human(_address: str) -> bool:
    """The guard and a brief's "owes you an answer" count mail in Updates as a person's (stopping is
    the safe side)."""
    return True


def deciding_of(thread: MailThread, own: Collection[str]) -> tuple[Any | None, frozenset[str]]:
    """A read thread's deciding message and the automated ones newer than it (followups' rules)."""
    from ..followups import deciding_message

    return deciding_message(thread.messages, own, _human)


def followup_line(target: ThreadTarget, thread: MailThread, now: datetime) -> str:
    """"kind=waiting on you | last from=them | days=3 | acct=work | thread=18c2..." (Jarvis's own words)."""
    message = next((item for item in thread.messages if item.gmail_id == target.last_gmail_id), None)
    days = 0
    if message is not None and message.sent_at is not None:
        reference = now if now.tzinfo is not None else now.astimezone()
        days = max(0, int((reference - message.sent_at).total_seconds() // 86400))
    last = "owner" if target.kind == KIND_OWED else "them"
    return (f"kind={FOLLOWUP_WORDS.get(target.kind, 'waiting on you')} | last from={last} | days={days} | "
            f"acct={data(target.alias, 24)} | thread={data(target.thread_id, 64)}")


def build_nudge_prompt(now: datetime, owner: str, account: AccountInfo, thread: MailThread, target: ThreadTarget,
                       memory: Sequence[str], request: ModeRequest, *, accounts: Sequence[AccountInfo] = (),
                       zone: str = "", redact: bool = False) -> tuple[str, ContextIndex]:
    """A drafted reply or nudge's stdin (spec 7.4): <now> <owner> <accounts> (the thread's account only)
    <followup> <mail> (the one thread read) [<memory>] <command> (the fixed words for its kind, then
    "The owner's words: ..." only when the owner typed some in "Tell Jarvis what to say": an address
    there is the owner's own, SOURCE_OWNER). The ContextIndex: the thread as shown with its messages'
    ids and people (SOURCE_MAIL), the notes' addresses and every account (``accounts``). ``redact``:
    the email text and the notes as their sizes. Follow-up data reaches the run only here, for the
    one thread the owner clicked."""
    from .dayplan import to_wall

    wall = to_wall(now, zone) if zone else now
    command = command_text(request, target.kind)
    mail = fit_mail(tuple(trim_threads([thread], max_threads=1)), redact=redact)
    notes = tuple(memory)
    memory_text = memory_block(notes, redact=redact) if memory_notes_shown(notes) else ""
    blocks = [f"<now>\n{now_line(wall, zone)}\n</now>", f"<owner>{data(owner, 20)}</owner>",
              f"<accounts>\n{account_row(account)}\n</accounts>",
              f"<followup>{followup_line(target, thread, now)}</followup>"]
    if mail:
        blocks.append(f"<mail>\n{_mail_block(mail, redact=redact)}\n</mail>")
    if memory_text:
        blocks.append(memory_text)
    blocks.append(f"<command>\n{command}\n</command>")   # Jarvis's fixed words; the owner's through data()
    text = "\n".join(blocks) + "\n"
    ctx = AskContext(now=wall, time_zone=zone, command=command, accounts=tuple(accounts) or (account,), mail=mail,
                     memory=notes, owner=owner)
    return text, index_of(ctx, [])


def nudge_guard(thread: MailThread, target: ThreadTarget, own: Collection[str]) -> str:
    """Before a draft (3.4 step 3, G7): the deciding message must still be in the thread; then the
    messages newer than it that are not automated (and not among ``target.auto_ids``) decide: one from
    the owner on a waiting thread -> GUARD_REPLIED; one from someone else on an owed thread ->
    GUARD_ANSWERED; any other change, or the deciding message gone -> GUARD_CHANGED; "" when nothing
    changed."""
    from ..followups import automated, from_owner, owner_automated

    messages = list(thread.messages)   # oldest first (ask.mail.thread_from_api)
    ids = [message.gmail_id for message in messages]
    if target.last_gmail_id not in ids:
        return GUARD_CHANGED
    newer = []
    for message in messages[ids.index(target.last_gmail_id) + 1:]:
        if message.gmail_id in target.auto_ids:
            continue
        mine = from_owner(message, own)
        if (mine and owner_automated(message)) or (not mine and automated(message, _human)):
            continue
        newer.append((message, mine))
    if not newer:
        return ""
    if target.kind == KIND_WAITING and any(mine for _message, mine in newer):
        return GUARD_REPLIED
    if target.kind == KIND_OWED and any(not mine for _message, mine in newer):
        return GUARD_ANSWERED
    return GUARD_CHANGED


def owed_target(thread: MailThread, alias: str, own: Collection[str]) -> ThreadTarget | None:
    """A read thread the owner wrote last in (followups' deciding-message rules; R32), to someone else
    directly: a ThreadTarget(kind owed) for "Draft a nudge"; None otherwise."""
    from ..followups import from_owner, is_own

    deciding, auto_ids = deciding_of(thread, own)
    if deciding is None or not from_owner(deciding, own):
        return None
    if not any(person.email and not is_own(person.email, own) for person in deciding.to):
        return None
    return ThreadTarget(alias, thread.thread_id, KIND_OWED, deciding.gmail_id, auto_ids)


class NudgeCheck:
    """validate.to_cards' ``extra`` for the one Reply of a nudge run (spec 8.3): in the clicked thread
    (account and thread exactly), answering its deciding message (waiting: their newest; owed: the
    owner's last), only to that message's people (sender, Reply-To, To, Cc) and never the owner's own
    address, and never repeating one of the owner's notes unless his words in "Tell Jarvis what to
    say" have them (memory.note_leak, R7)."""

    def __init__(self, target: ThreadTarget, thread: MailThread | None, notes: Sequence[str] = (), *,
                 owner_words: str = "", own: Collection[str] = ()) -> None:
        self.target = target
        self.message = None
        if thread is not None:
            self.message = next((item for item in thread.messages if item.gmail_id == target.last_gmail_id), None)
        self.notes = tuple(notes)
        self.owner_words = owner_words
        self.own = tuple(own)

    def __call__(self, card: ProposedAction) -> str:
        from ..followups import is_own

        if card.kind != REPLY:
            return NUDGE_ONLY_REPLY
        if card.account != self.target.alias or card.field("thread") != self.target.thread_id:
            return NUDGE_OTHER_THREAD
        message = self.message
        if message is None:
            return NUDGE_WRONG_MESSAGE
        msgid, gmid = card.field("msgid").strip().strip("<>"), card.field("gmid").strip()
        if (not msgid and not gmid) or (msgid and msgid != message.message_id) or (gmid and gmid != message.gmail_id):
            return NUDGE_WRONG_MESSAGE
        recipients = card.mail_recipients()
        if any(is_own(address, self.own) for address in recipients):
            return NUDGE_OWN_ADDRESS
        people = {person.email.casefold() for person in message.people() if person.email}
        if not recipients or any(address.casefold() not in people for address in recipients):
            return NUDGE_EXTRA_PEOPLE
        if self.notes:
            from ..memory import note_leak

            if any(note_leak(text, self.notes, self.owner_words) for text in (card.field("subject"), card.body)):
                return NUDGE_NOTE_LEAK
        return ""


def check_nudge(card: ProposedAction, target: ThreadTarget, thread: MailThread | None, notes: Sequence[str] = (), *,
                owner_words: str = "", own: Collection[str] = ()) -> str:
    """NudgeCheck for one card: "" or the reason it is information only."""
    return NudgeCheck(target, thread, notes, owner_words=owner_words, own=own)(card)


# --------------------------------------------------------------------------
# Words (shown and spoken; built here from times and counts only)
# --------------------------------------------------------------------------

def block_line(card: ProposedAction, hour24: bool = False) -> str:
    """"10:30-12:00  Work on: Lab report" (the plan answer's list; on screen only)."""
    from .dayplan import hours_text

    if card.start is None or card.end is None:
        return card.title
    return f"{hours_text(card.start, card.end, hour24)}  {card.title}"


def mode_title(mode: str) -> str:
    return MODE_TITLES.get(mode, "")
