"""The LIVE view's event stream: every step Jarvis takes, as it happens, for the LIVE tab and its
pop-out window (ui.py; hud.LiveLog renders it).

    LiveStream(enabled=True, keep_text=True, hour24=False, ...)
                                      one per app (AppController.live): thread-safe, bounded
                                      (LiveLimits), in memory only, this session only
        task(kind, title, ...)        a LiveTask (TASK_*: an Ask, a web research, an approved card, a
                                      briefing fetch, the rolling "Background reads"); NO_TASK when disabled
                                      or closed
        snapshot()                    every TaskView, the pinned task first, then oldest -> newest
        changes(since)                the TaskViews changed since a version, the ids removed since
                                      then (LiveChanges); re-arms the listener
        set_listener(fn)              called once after a mutation (outside the lock, from any
                                      thread), then not again until the next changes()
        running(), clear() (finished tasks; a rolling task's finished steps), close(), when_text(moment)
    LiveTask.step(kind, title, ...)   one thing Jarvis did (the step kinds below) -> LiveStep
    LiveTask.find / update / finish / note
    LiveStep.field / item / note / block / rename_block / update / done
    LiveStep.picture(picture)         a picture of the step (pictures.Picture: an email, a calendar
                                      change, a web search, a page); one per step, a new one replaces
                                      the old
    NO_TASK, NO_STEP                  handles whose every method does nothing
    Payload                           what an approved card will send or change (executor.preview),
                                      with its picture
    PICTURE_PENDING / READY / UNAVAILABLE / DROPPED   a picture's states (pictures.py builds them)
    elapsed_text, status_word, notify_words, command_line_text, open_hint, clean_line, clean_block

Rules (spec "LIVE view" sections 2 and 7):

- Never raises, never blocks: every public method catches its own errors (logged at DEBUG as
  "Live view: <method> failed (<ExcType>)", the type only) and holds the lock only while it
  changes plain Python data. Instrumentation can never change what Jarvis does.
- Only shows: the stream holds what the owner could already know (his own words, what Jarvis read
  from his accounts for the task, what Jarvis computed or sent, Jarvis's own status words). Every
  string goes through config.redact; every one-line string also through a token-shape filter;
  control, bidi and invisible format characters are shown as visible escapes ("<U+202E>").
  Blocks (email text, the planner's stdin and reply, an outgoing body) are kept exactly otherwise.
- On screen only: nothing here writes a file, logs content, touches the network or pickles
  anything; repr() / str() of every object here names ids, kinds and statuses only.
- Bounded: LiveLimits caps the tasks, steps, items, notes, fields, block sizes and the total
  characters; RUNNING tasks are never dropped.
- Pictures (pictures.py: an email drawn by Jarvis, a calendar change on its day, a web search as
  a results page, a page snapshot) are memory only too: never written to a file and never logged.
  Their bytes are counted apart from the characters (LiveLimits picture caps: one picture, one
  task, all); past the total the oldest finished tasks' pictures are dropped first ("Picture not
  kept"); a RUNNING task keeps its pictures. [live] pictures = false refuses the drawn ones;
  [live] text = false keeps no email text or search snippet in a picture and no page picture.
  Clear and close drop them with their tasks.

Qt-free (standard library, config.redact, text_prep.format_time, actions.link_allowed).
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from . import config as _config

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Statuses, task kinds, step kinds
# --------------------------------------------------------------------------

STATUS_RUNNING = "running"        # in progress
STATUS_OK = "ok"                  # done as intended
STATUS_WARN = "warn"              # done, but look at it (nothing found, dropped lines, UNKNOWN)
STATUS_BLOCKED = "blocked"        # Jarvis's own guard refused or stopped it
STATUS_FAILED = "failed"          # it did not work
STATUS_CANCELLED = "cancelled"    # the owner stopped it (Cancel, Undo) or Jarvis closed
STATUSES = (STATUS_RUNNING, STATUS_OK, STATUS_WARN, STATUS_BLOCKED, STATUS_FAILED, STATUS_CANCELLED)
FINAL_STATUSES = frozenset(STATUSES) - {STATUS_RUNNING}
STATUS_WORDS = {STATUS_RUNNING: "RUNNING", STATUS_OK: "DONE", STATUS_WARN: "CHECK", STATUS_BLOCKED: "BLOCKED",
                STATUS_FAILED: "FAILED", STATUS_CANCELLED: "CANCELLED"}

TASK_ASK = "ask"
TASK_ACTION = "action"
TASK_BRIEFING = "briefing"
TASK_BACKGROUND = "background"
TASK_WEB = "web"                  # a web research started with "web:" (ask.research)
TASK_MEMORY = "memory"            # the owner's notes kept on this PC (the productivity bundle's memory)
TASK_PLAN = "plan"                # a day plan (the productivity bundle's "plan my day")
TASK_PREP = "prep"                # a meeting brief (the productivity bundle's "prep me")
TASK_NUDGE = "nudge"              # a drafted reply or nudge (the productivity bundle's follow-ups)
TASK_MEETING = "meeting"          # a recorded meeting's summary (meetings part 2)
TASK_OWN_READS = "own_reads"      # Jarvis's own reads and offers (the day read for prep offers, the header
                                  # scan, a re-fetch, each offer made or not): always shown (R6)
TASK_KINDS = (TASK_ASK, TASK_ACTION, TASK_BRIEFING, TASK_BACKGROUND, TASK_WEB, TASK_MEMORY, TASK_PLAN, TASK_PREP,
              TASK_OWN_READS, TASK_NUDGE, TASK_MEETING)
TASK_TAGS = {TASK_ASK: "ASK", TASK_ACTION: "ACTION", TASK_BRIEFING: "BRIEFING", TASK_BACKGROUND: "BACKGROUND",
             TASK_WEB: "WEB", TASK_MEMORY: "MEMORY", TASK_PLAN: "PLAN", TASK_PREP: "PREP", TASK_OWN_READS: "READS",
             TASK_NUDGE: "NUDGE", TASK_MEETING: "MEETING"}
# Tasks that open the LIVE view by themselves ([live] auto_open) and set the tab's dot.
ATTENTION_KINDS = frozenset({TASK_ASK, TASK_ACTION, TASK_WEB, TASK_PLAN, TASK_PREP, TASK_NUDGE})
COMPACT_KINDS = frozenset({TASK_BRIEFING, TASK_BACKGROUND, TASK_MEMORY, TASK_OWN_READS})
BACKGROUND_KEY = "background"
BACKGROUND_TITLE = "Background reads"
MEMORY_KEY = "memory"             # the rolling MEMORY task (one per session while it is not finished)
MEMORY_TITLE = "Memory (this PC)"
OWN_READS_KEY = "own_reads"       # the rolling READS task: Jarvis's own reads and offers, whatever [live] background
OWN_READS_TITLE = "Jarvis's own reads and offers (this PC)"

ASK_REQUEST = "ask.request"
ASK_CHECKS = "ask.checks"
CALENDAR_READ = "calendar.read"
MAIL_THREAD = "mail.thread"
MAIL_SEARCH = "mail.search"
PLANNER_RUN = "planner.run"
ASK_VALIDATE = "ask.validate"
ASK_CARDS = "ask.cards"
ACTION_APPROVED = "action.approved"
ACTION_PAYLOAD = "action.payload"
ACTION_HOLD = "action.hold"            # a Send's / Cancel's hidden hold (SENDING, never a number)
ACTION_SIGNIN = "action.signin"
ACTION_CHECKS = "action.checks"
ACTION_CALL = "action.call"
BRIEFING_FETCH = "briefing.fetch"
AGENDA_READ = "agenda.read"
EVENT_CHECK = "event.check"
ACCOUNT_SIGNIN = "account.signin"
CLAUDE_CHECK = "claude.check"
WEB_SEARCH = "web.search"         # one web search of a research run (its exact query and results)
WEB_FETCH = "web.fetch"           # one page read of a research run (its URL, HTTP status, excerpt)
WEB_SOURCE = "web.source"         # a page the research's answer cites but did not read: its picture
RESEARCH_INPUT = "research.input"         # what the web research gets (the isolation check)
RESEARCH_RUN = "research.run"             # the web research run (Claude Code with web tools only)
RESEARCH_VALIDATE = "research.validate"   # the research answer checked: sources and suggestions
MEMORY_SAVE = "memory.save"               # a note saved, changed, removed or restored (or not saved, and why)
MODE_REQUEST = "mode.request"             # what started a planner mode run (a typed plan, a click) and what it gets
PLAN_FREE = "plan.free"                   # a day plan's free time (or why it does not run)
PLAN_VALIDATE = "plan.validate"           # a day plan's blocks checked (each card or why not, the row it serves)
PREP_MEETING = "prep.meeting"             # a meeting brief's meeting as read now (time, guests; moved, gone, ended)
PREP_PEOPLE = "prep.people"               # its guests: who is looked up, refused or skipped, and the checks before
PREP_VALIDATE = "prep.validate"           # its answer checked: brief items kept or dropped, replies, note leaks
CALENDAR_DAY = "calendar.day"             # Jarvis's own read of today's events (for prep offers), counts only
OFFER_MADE = "offer.made"                 # an offer posted (or not, and why; spoken, held or text only)
MAIL_SCAN = "mail.scan"                   # Jarvis's own header scan of one account (or one thread again), counts
MEETING_INPUT = "meeting.input"           # a meeting summary's input: the transcript (as data), the guests' names
MEETING_VALIDATE = "meeting.validate"     # its answer checked: items kept or dropped, follow-up drafts, notes
STEP_KINDS = (ASK_REQUEST, ASK_CHECKS, CALENDAR_READ, MAIL_THREAD, MAIL_SEARCH, PLANNER_RUN, ASK_VALIDATE,
              ASK_CARDS, ACTION_APPROVED, ACTION_PAYLOAD, ACTION_HOLD, ACTION_SIGNIN, ACTION_CHECKS,
              ACTION_CALL, BRIEFING_FETCH, AGENDA_READ, EVENT_CHECK, ACCOUNT_SIGNIN, CLAUDE_CHECK, WEB_SEARCH,
              WEB_FETCH, WEB_SOURCE, RESEARCH_INPUT, RESEARCH_RUN, RESEARCH_VALIDATE, MEMORY_SAVE, MODE_REQUEST,
              PLAN_FREE, PLAN_VALIDATE, PREP_MEETING, PREP_PEOPLE, PREP_VALIDATE, CALENDAR_DAY, OFFER_MADE,
              MAIL_SCAN, MEETING_INPUT, MEETING_VALIDATE)

# Internal link ids a Field may carry besides a Jarvis-made Google result link: "tab:jarvis" (a tab),
# "live:3" (a task), "picture:12" (step 12's picture shown larger; the step itself while it has none).
INTERNAL_LINK_RE = re.compile(r"(?:tab:[a-z]{1,16}|live:\d{1,12}|picture:\d{1,12})")
PICTURE_LINK = "picture:"

# --------------------------------------------------------------------------
# Caps and bounds
# --------------------------------------------------------------------------

MAX_BLOCK_CHARS = 131_072
LABEL_CAP = 40
TITLE_CAP = 200
SUMMARY_CAP = 300
FIELD_CAP = 2_000
ITEM_CAP = 500
NOTE_CAP = 300
STATUS_TEXT_CAP = 24
BLOCK_LABEL_CAP = 80
LINK_CAP = 2_000
HIDDEN = "[hidden]"
LINE_BREAK = " \u21b5 "

# A picture's states (pictures.Picture.state).
PICTURE_PENDING = "pending"          # being made (a page snapshot on its way)
PICTURE_READY = "ready"              # there to see
PICTURE_UNAVAILABLE = "unavailable"  # could not be made (its note says why)
PICTURE_DROPPED = "dropped"          # not kept (the memory caps; its note says why)
PICTURE_STATES = (PICTURE_PENDING, PICTURE_READY, PICTURE_UNAVAILABLE, PICTURE_DROPPED)
PICTURE_TOO_BIG = "too large to keep in memory"
PICTURE_TASK_CAP = "this task's pictures already use the memory set aside for one task"
PICTURE_MEMORY = "dropped to save memory (older pictures go first)"


@dataclass(frozen=True)
class LiveLimits:
    """How much the stream keeps (spec 2.8). Past a limit the oldest *finished* task goes (a
    RUNNING task is never dropped), or the step / item / note is counted instead of kept."""

    max_tasks: int = 30
    max_briefing_tasks: int = 3
    max_steps: int = 40
    max_items: int = 200
    max_notes: int = 30
    max_fields: int = 30
    max_block_chars: int = MAX_BLOCK_CHARS
    max_total_chars: int = 2_000_000
    max_removed_log: int = 200
    # Pictures, in bytes (apart from max_total_chars): one picture, one task's, all of them.
    max_picture_bytes: int = 1_500_000
    max_task_picture_bytes: int = 6_000_000
    max_total_picture_bytes: int = 24_000_000


# --------------------------------------------------------------------------
# Data (frozen; every string cleaned)
# --------------------------------------------------------------------------

def _size_repr(name: str, *texts: str) -> str:
    return f"<{name}: {sum(len(text) for text in texts)} chars>"


@dataclass(frozen=True, repr=False)
class Field:
    """One "LABEL  value" row."""

    label: str
    value: str
    mono: bool = False      # ids, queries, addresses, flags
    link: str = ""          # a Jarvis-made result link (rule L) or an internal link id ("tab:jarvis")

    def __repr__(self) -> str:
        return _size_repr("Field", self.label, self.value)


@dataclass(frozen=True, repr=False)
class Item:
    """One row of a list (events, proposals, cards, search results)."""

    text: str
    status: str = ""        # "" or a STATUS_* (the small glyph)
    status_text: str = ""
    note: str = ""          # a second line (reason, NEW recipient, warning)
    mono: bool = False

    def __repr__(self) -> str:
        return f"<Item {self.status or '-'}: {len(self.text) + len(self.note)} chars>"


@dataclass(frozen=True, repr=False)
class Block:
    """Collapsible text: a thread as given to the planner, the planner's stdin and raw reply, an
    outgoing body. ``text`` is exact (line breaks and tabs kept; controls made visible)."""

    label: str
    text: str
    chars: int              # characters of the whole original text
    cut: int = 0            # characters left out at the end ("N more characters not shown")
    kept: bool = True       # False: [live] text = false - only the size is known
    start_open: bool = False
    untrusted: bool = False   # written by other people (email, web): the view labels it

    def __repr__(self) -> str:
        return f"<Block: {self.chars} chars{'' if self.kept else ', not kept'}>"


@dataclass(frozen=True, repr=False)
class Note:
    """A timestamped progress line inside a step."""

    at: float               # monotonic
    text: str

    def __repr__(self) -> str:
        return _size_repr("Note", self.text)


@dataclass(frozen=True, repr=False)
class Payload:
    """What an approved card will send or change, exactly (executor.Executor.preview and the
    backends right before their call). ``fields`` carry raw values: the stream cleans them."""

    title: str                              # "Will send exactly this"
    fields: tuple[Field, ...] = ()
    body_label: str = ""                    # "Message" / "Description"
    body: str = ""
    start_open: bool = True                 # the outgoing body starts open in the view
    picture: Any = None                     # a pictures.Picture of it (the email as it goes out)

    def values(self) -> dict[str, str]:
        """label -> value (for comparing a preview with what is actually sent)."""
        found = {item.label: item.value for item in self.fields}
        if self.body_label:
            found[self.body_label] = self.body
        return found

    def __repr__(self) -> str:
        return f"<Payload: {len(self.fields)} fields, body {len(self.body)} chars>"


_KIND_RE = re.compile(r"[a-z][a-z0-9_.]{0,31}")


def _safe_kind(kind: str) -> str:
    return kind if isinstance(kind, str) and _KIND_RE.fullmatch(kind) else "other"


@dataclass(frozen=True)
class StepView:
    id: int
    task_id: int
    seq: int                                # global order across threads
    kind: str
    key: str
    title: str
    status: str
    status_text: str
    summary: str
    started: float                          # monotonic
    ended: float | None
    at: datetime                            # wall clock of the start
    deadline: float | None                  # a hold's monotonic end (never shown as a number)
    fields: tuple[Field, ...]
    items: tuple[Item, ...]
    items_more: int
    notes: tuple[Note, ...]
    notes_more: int
    blocks: tuple[Block, ...]
    open_hint: bool
    revision: int
    picture: Any = None                     # a pictures.Picture (None: no picture)

    def elapsed(self, now: float) -> float:
        """Seconds from the start to the end (or to ``now`` while it runs)."""
        return max(0.0, (self.ended if self.ended is not None else now) - self.started)

    @property
    def running(self) -> bool:
        return self.status == STATUS_RUNNING

    def __repr__(self) -> str:   # ids, kind and status only - never content (rule P7)
        return (f"StepView(id={self.id}, task={self.task_id}, kind={_safe_kind(self.kind)!r}, "
                f"status={self.status!r}, rev={self.revision})")


@dataclass(frozen=True)
class TaskView:
    id: int
    kind: str
    title: str
    status: str
    status_text: str
    summary: str
    started: float
    ended: float | None
    at: datetime
    compact: bool
    pinned: bool
    steps: tuple[StepView, ...]
    steps_more: int
    revision: int

    def elapsed(self, now: float) -> float:
        return max(0.0, (self.ended if self.ended is not None else now) - self.started)

    @property
    def running(self) -> bool:
        return self.status == STATUS_RUNNING

    @property
    def tag(self) -> str:
        return TASK_TAGS.get(self.kind, "TASK")

    def __repr__(self) -> str:   # ids, kind and status only
        return (f"TaskView(id={self.id}, kind={_safe_kind(self.kind)!r}, status={self.status!r}, "
                f"steps={len(self.steps)}, rev={self.revision})")


@dataclass(frozen=True)
class LiveChanges:
    version: int
    tasks: tuple[TaskView, ...]     # new or changed since ``since``, in display order
    removed: tuple[int, ...]        # task ids dropped since ``since`` (bounds, Clear)
    reset: bool = False             # ``since`` too old: ``tasks`` is the whole snapshot

    def __repr__(self) -> str:
        return (f"LiveChanges(version={self.version}, tasks={len(self.tasks)}, removed={len(self.removed)}, "
                f"reset={self.reset})")


# --------------------------------------------------------------------------
# Cleaning
# --------------------------------------------------------------------------

_FORMAT_CHARS = ("\u00ad\u061c\u180e\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff"
                 "\ufff9-\ufffb")
# One-line text: every C0 / C1 control (tabs and line breaks are handled before), the line
# separators and every bidi / invisible format character.
_LINE_ESCAPE_RE = re.compile(f"[\x00-\x1f\x7f-\x9f{_FORMAT_CHARS}]")
# Blocks keep \n and \t.
_BLOCK_ESCAPE_RE = re.compile(f"[\x00-\x08\x0b-\x1f\x7f-\x9f{_FORMAT_CHARS}]")
# Defence in depth for Jarvis-made strings (an exception message): token shapes are hidden.
TOKEN_SHAPE_RE = re.compile(r"ya29\.[\w.-]{10,}|1//[\w-]{20,}|sk-ant-[\w-]{10,}|(?:secret|ntn)_[A-Za-z0-9]{20,}"
                            r"|Bearer\s+\S{10,}|GOCSPX-[\w-]{10,}")
_STATUS_TEXT_RE = re.compile(r"[^A-Z0-9 .,:;/()'+&-]")


def _escape(match: re.Match[str]) -> str:
    return f"<U+{ord(match.group(0)):04X}>"


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - shown as its type instead
        return f"<{type(value).__name__}>"


def _cut(text: str, cap: int) -> str:
    if cap > 0 and len(text) > cap:
        return text[:max(0, cap - 3)].rstrip() + "..."
    return text


def clean_line(text: Any, cap: int, *, redact: Callable[[str], str] = _config.redact) -> str:
    """One line for a title, summary, field value, item or note: secrets redacted, token shapes
    hidden, controls and bidi / invisible characters shown as "<U+202E>", line breaks as
    " \u21b5 ", spaces collapsed, cut to ``cap`` with "..."."""
    text = redact(_as_text(text))
    text = TOKEN_SHAPE_RE.sub(HIDDEN, text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ").replace("\n", LINE_BREAK)
    text = _LINE_ESCAPE_RE.sub(_escape, text)
    return _cut(" ".join(text.split()), cap)


def clean_block(text: Any, cap: int = MAX_BLOCK_CHARS, *,
                redact: Callable[[str], str] = _config.redact) -> tuple[str, int, int]:
    """(text, characters of the original, characters cut at the end) for a block: secrets
    redacted (no token-shape filter: a block is data, shown exactly), "\\r\\n" / "\\r" as "\\n",
    controls but "\\n" and "\\t" and bidi / invisible characters shown as "<U+202E>"."""
    original = _as_text(text)
    shown = redact(original).replace("\r\n", "\n").replace("\r", "\n")
    shown = _BLOCK_ESCAPE_RE.sub(_escape, shown)
    cut = max(0, len(shown) - cap) if cap > 0 else 0
    if cut:
        shown = shown[:cap]
    return shown, len(original), cut


def clean_status_text(text: Any) -> str:
    """A status word: plain ASCII capitals, at most STATUS_TEXT_CAP characters ("" for none)."""
    word = clean_line(text, 0).upper()
    word = _STATUS_TEXT_RE.sub("", word.encode("ascii", "ignore").decode("ascii"))
    return " ".join(word.split())[:STATUS_TEXT_CAP].strip()


_RESULT_HOSTS = ("calendar.google.com", "mail.google.com")


def result_link(link: Any) -> str:
    """``link`` when it may be a clickable result link (rule L): an internal link id
    ("tab:jarvis", "live:3"), or an https link Jarvis made on calendar.google.com,
    www.google.com/calendar/... or mail.google.com that actions.link_allowed accepts; else ""."""
    if not isinstance(link, str) or not link or len(link) > LINK_CAP:
        return ""
    if INTERNAL_LINK_RE.fullmatch(link):
        return link
    try:
        from .actions import link_allowed

        parts = urlsplit(link)
        host = (parts.hostname or "").rstrip(".").casefold()
    except Exception:  # noqa: BLE001 - not a link then
        return ""
    if host not in _RESULT_HOSTS and not (host == "www.google.com" and parts.path.startswith("/calendar/")):
        return ""
    if not link_allowed(link):
        return ""
    return link


# --------------------------------------------------------------------------
# Words
# --------------------------------------------------------------------------

def elapsed_text(seconds: float) -> str:
    """"0.4 s", "12 s", "1:05", "1:02:03"."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        value = 0.0
    if not math.isfinite(value) or value < 0:
        value = 0.0
    if value < 10:
        return f"{math.floor(value * 10) / 10:.1f} s"
    if value < 60:
        return f"{int(value)} s"
    total = int(value)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def status_word(view: StepView | TaskView, now: float | None = None) -> str:
    """What the status pill says: the view's status_text, else the status's default word. A running
    hold says only its word ("SENDING"): no number, ever (``now`` is accepted and ignored)."""
    return view.status_text or STATUS_WORDS.get(view.status, "")


_NOTIFY_WORDS = {
    "all": "everyone on the event gets Google's email (sendUpdates=all)",
    "external": "only guests outside your organization get an email (sendUpdates=externalOnly)",
    "externalonly": "only guests outside your organization get an email (sendUpdates=externalOnly)",
    "none": "nobody gets an email (sendUpdates=none)",
}


def notify_words(value: str) -> str:
    """A card's notify= (or Google's sendUpdates) in words; anything else counts as "all", as the
    executor sends it."""
    key = value.strip().casefold() if isinstance(value, str) else ""
    return _NOTIFY_WORDS.get(key, _NOTIFY_WORDS["all"])


_PATH_LIKE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]{2}|/|~[\\/])")
_NAME_ONLY_FLAGS = ("--system-prompt-file", "--settings", "--mcp-config-file", "--append-system-prompt-file")


def _quoted(item: str) -> str:
    if item == "":
        return '""'
    if any(char.isspace() for char in item):
        return '"' + item.replace('"', '\\"') + '"'
    return item


def command_line_text(argv: Sequence[str]) -> str:
    """The planner's command line as the LIVE view shows it: "claude " + every argument after the
    executable, the --json-schema value as "<answer schema: N characters>", file arguments as
    their file names only (never argv[0] or any folder: they name the Windows user)."""
    items = [_as_text(item) for item in list(argv)[1:]]
    parts = ["claude"]
    index = 0
    while index < len(items):
        item = items[index]
        has_value = index + 1 < len(items)
        if item == "--json-schema" and has_value:
            parts += [item, f"<answer schema: {len(items[index + 1]):,} characters>"]
            index += 2
            continue
        if item in _NAME_ONLY_FLAGS and has_value:
            parts += [item, _quoted(PureWindowsPath(items[index + 1]).name)]
            index += 2
            continue
        parts.append(_quoted(PureWindowsPath(item).name if _PATH_LIKE_RE.match(item) else item))
        index += 1
    return " ".join(parts)


def open_hint(kind: str, status: str, items: Iterable[Item] = (), picture: Any = None) -> bool:
    """Whether a step's details start open (spec 2.6): RUNNING, a final WARN / BLOCKED / FAILED,
    what will be / was sent (ACTION_PAYLOAD), a validation with an item that is not ok, the
    Ask's answer and cards, a step with a picture (unless that picture was not kept)."""
    if status == STATUS_RUNNING or status in (STATUS_WARN, STATUS_BLOCKED, STATUS_FAILED):
        return True
    if picture is not None and getattr(picture, "state", PICTURE_DROPPED) != PICTURE_DROPPED:
        return True
    if kind in (ACTION_PAYLOAD, ASK_CARDS):
        return True
    if kind in (ASK_VALIDATE, RESEARCH_VALIDATE):
        return any(item.status and item.status != STATUS_OK for item in items)
    return False


# --------------------------------------------------------------------------
# The stream's records (mutable; only under the stream's lock)
# --------------------------------------------------------------------------

def _field_size(item: Field) -> int:
    return len(item.label) + len(item.value) + len(item.link)


def _item_size(item: Item) -> int:
    return len(item.text) + len(item.note) + len(item.status_text)


def _block_size(item: Block) -> int:
    return len(item.label) + len(item.text)


class _Step:
    __slots__ = ("id", "task", "seq", "kind", "key", "title", "status", "status_text", "summary", "started",
                 "ended", "at", "deadline", "fields", "items", "items_more", "notes", "notes_more", "blocks",
                 "revision", "size", "view", "picture", "picture_cost")

    def __init__(self, step_id: int, task: _Task, seq: int, kind: str, key: str, started: float,
                 at: datetime) -> None:
        self.id = step_id
        self.task = task
        self.seq = seq
        self.kind = kind
        self.key = key
        self.title = ""
        self.status = STATUS_RUNNING
        self.status_text = ""
        self.summary = ""
        self.started = started
        self.ended: float | None = None
        self.at = at
        self.deadline: float | None = None
        self.fields: list[Field] = []
        self.items: list[Item] = []
        self.items_more = 0
        self.notes: list[Note] = []
        self.notes_more = 0
        self.blocks: list[Block] = []
        self.revision = 0
        self.size = 0
        self.view: StepView | None = None
        self.picture: Any = None        # a pictures.Picture
        self.picture_cost = 0           # its bytes as counted (the LiveLimits picture caps)

    def build(self) -> StepView:
        if self.view is None or self.view.revision != self.revision:
            self.view = StepView(
                id=self.id, task_id=self.task.id, seq=self.seq, kind=self.kind, key=self.key, title=self.title,
                status=self.status, status_text=self.status_text, summary=self.summary, started=self.started,
                ended=self.ended, at=self.at, deadline=self.deadline, fields=tuple(self.fields),
                items=tuple(self.items), items_more=self.items_more, notes=tuple(self.notes),
                notes_more=self.notes_more, blocks=tuple(self.blocks),
                open_hint=open_hint(self.kind, self.status, self.items, self.picture), revision=self.revision,
                picture=self.picture)
        return self.view


class _Task:
    __slots__ = ("id", "kind", "title", "status", "status_text", "summary", "started", "ended", "at", "compact",
                 "pinned", "key", "steps", "steps_more", "finished", "removed", "revision", "size", "view",
                 "picture_bytes")

    def __init__(self, task_id: int, kind: str, started: float, at: datetime) -> None:
        self.id = task_id
        self.kind = kind
        self.title = ""
        self.status = STATUS_RUNNING
        self.status_text = ""
        self.summary = ""
        self.started = started
        self.ended: float | None = None
        self.at = at
        self.compact = False
        self.pinned = False
        self.key = ""
        self.steps: list[_Step] = []
        self.steps_more = 0
        self.finished = False
        self.removed = False
        self.revision = 0
        self.size = 0
        self.view: TaskView | None = None
        self.picture_bytes = 0          # its steps' pictures (bytes as counted)

    def live_status(self) -> str:
        """A rolling task is never finished: RUNNING while one of its steps runs, else OK."""
        if self.key and not self.finished:
            return STATUS_RUNNING if any(step.status == STATUS_RUNNING for step in self.steps) else STATUS_OK
        return self.status

    @property
    def attention(self) -> bool:
        return self.kind in ATTENTION_KINDS

    def build(self) -> TaskView:
        if self.view is None or self.view.revision != self.revision:
            self.view = TaskView(
                id=self.id, kind=self.kind, title=self.title, status=self.live_status(),
                status_text=self.status_text, summary=self.summary, started=self.started, ended=self.ended,
                at=self.at, compact=self.compact, pinned=self.pinned,
                steps=tuple(step.build() for step in self.steps), steps_more=self.steps_more,
                revision=self.revision)
        return self.view


UNCHANGED: Any = object()   # LiveStep.update(deadline=UNCHANGED) keeps the deadline


def _safe(default: Any = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Never raise: a failure is logged at DEBUG (the type only) and ``default`` returned."""

    def wrap(function: Callable[..., Any]) -> Callable[..., Any]:
        name = function.__name__

        def safe(*args: Any, **kwargs: Any) -> Any:
            try:
                return function(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - instrumentation never changes what Jarvis does
                try:
                    logger.debug("Live view: %s failed (%s)", name, type(exc).__name__)
                except Exception:  # noqa: BLE001
                    pass
                return default() if callable(default) else default

        safe.__name__ = name
        safe.__doc__ = function.__doc__
        return safe

    return wrap


def quiet(default: Any = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """For the instrumentation's own helpers (the code that turns what Jarvis did into steps):
    whatever goes wrong in one is logged at DEBUG as "Live view: <helper> failed (<ExcType>)" and
    ``default`` is returned, so the LIVE view can never change what Jarvis does."""
    return _safe(default)


# --------------------------------------------------------------------------
# The stream
# --------------------------------------------------------------------------

class LiveStream:
    """One in-process, thread-safe, bounded, memory-only event stream (see the module docs)."""

    def __init__(self, *, enabled: bool = True, keep_text: bool = True, hour24: bool = False,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], datetime] = lambda: datetime.now().astimezone(),
                 redact: Callable[[str], str] = _config.redact, limits: LiveLimits | None = None,
                 pictures: bool = True) -> None:
        self._enabled = bool(enabled)
        self._keep_text = bool(keep_text)
        self._pictures = bool(pictures)
        self._picture_total = 0
        self.hour24 = bool(hour24)
        self._clock = clock
        self._wall = wall
        self._redact = redact
        self.limits = limits or LiveLimits()
        self._lock = threading.Lock()
        self._tasks: dict[int, _Task] = {}        # creation order (oldest first)
        self._next_task = 0
        self._next_step = 0
        self._seq = 0
        self._version = 0
        self._total = 0
        self._removed: deque[tuple[int, int]] = deque()   # (version, task id), the newest max_removed_log
        self._removed_floor = 0                           # removals at or before this version were forgotten
        self._listener: Callable[[], None] | None = None
        self._armed = True
        self._closed = False

    # ---- properties --------------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled and not self._closed

    @property
    def keep_text(self) -> bool:
        return self._keep_text

    @property
    def pictures(self) -> bool:
        """[live] pictures: the email and calendar pictures Jarvis draws are kept."""
        return self._pictures

    @property
    def version(self) -> int:
        return self._version

    @_safe(0)
    def picture_bytes(self) -> int:
        """The bytes of every picture kept, as counted against the LiveLimits picture caps."""
        with self._lock:
            return self._picture_total

    def __repr__(self) -> str:
        return f"LiveStream(enabled={self.enabled}, tasks={len(self._tasks)}, version={self._version})"

    # ---- cleaning (outside the lock) ---------------------------------------------------------

    def _line(self, text: Any, cap: int) -> str:
        return clean_line(text, cap, redact=self._redact)

    def _field(self, item: Any) -> Field | None:
        if isinstance(item, Field):
            return Field(self._line(item.label, LABEL_CAP), self._line(item.value, FIELD_CAP), bool(item.mono),
                         result_link(item.link))
        if isinstance(item, tuple) and len(item) >= 2:
            return Field(self._line(item[0], LABEL_CAP), self._line(item[1], FIELD_CAP),
                         bool(item[2]) if len(item) > 2 else False)
        return None

    def _item(self, item: Any) -> Item | None:
        if isinstance(item, Item):
            return Item(self._line(item.text, ITEM_CAP), _status_or_blank(item.status),
                        clean_status_text(item.status_text), self._line(item.note, ITEM_CAP), bool(item.mono))
        if isinstance(item, str):
            return Item(self._line(item, ITEM_CAP))
        return None

    def _block(self, label: Any, text: Any, start_open: bool, untrusted: bool) -> Block:
        label = self._line(label, BLOCK_LABEL_CAP)
        if not self._keep_text:
            return Block(label, "", len(_as_text(text)), 0, kept=False, start_open=bool(start_open),
                         untrusted=bool(untrusted))
        shown, chars, cut = clean_block(text, self.limits.max_block_chars, redact=self._redact)
        return Block(label, shown, chars, cut, True, bool(start_open), bool(untrusted))

    # ---- bookkeeping (under the lock) ---------------------------------------------------------

    def _touch(self, task: _Task, step: _Step | None = None) -> None:
        self._version += 1
        task.revision = self._version
        if step is not None:
            step.revision = self._version

    def _resize(self, task: _Task, step: _Step | None, delta: int) -> None:
        if step is not None:
            step.size += delta
        task.size += delta
        self._total += delta

    def _drop(self, task: _Task) -> None:
        if task.removed:
            return
        task.removed = True
        self._tasks.pop(task.id, None)
        self._total -= task.size
        self._picture_total -= task.picture_bytes
        task.picture_bytes = 0
        for step in task.steps:   # the pictures go with the task (nothing keeps them alive)
            step.picture, step.picture_cost = None, 0
        self._version += 1
        self._removed.append((self._version, task.id))
        while len(self._removed) > self.limits.max_removed_log:
            self._removed_floor = self._removed.popleft()[0]

    def _trim(self) -> None:
        """The bounds of LiveLimits: the oldest finished tasks go (briefings keep their newest few)."""
        limits = self.limits
        briefings = [task for task in self._tasks.values() if task.kind == TASK_BRIEFING]
        excess = len(briefings) - limits.max_briefing_tasks
        for task in briefings:
            if excess <= 0:
                break
            if task.finished:
                self._drop(task)
                excess -= 1
        for over in (lambda: len(self._tasks) > limits.max_tasks, lambda: self._total > limits.max_total_chars):
            while over():
                oldest = next((task for task in self._tasks.values() if task.finished and not task.pinned), None)
                if oldest is None:
                    break
                self._drop(oldest)

    def _notify(self) -> None:
        """After a mutation, outside the lock: the listener once until the next changes()."""
        with self._lock:
            listener = self._listener if self._armed else None
            if listener is not None:
                self._armed = False
        if listener is not None:
            try:
                listener()
            except Exception as exc:  # noqa: BLE001 - a broken listener never stops Jarvis
                logger.debug("Live view: listener failed (%s)", type(exc).__name__)

    def _alive(self, task: _Task) -> bool:
        return not self._closed and not task.removed and not task.finished

    # ---- public API --------------------------------------------------------------------------

    @_safe(lambda: NO_TASK)
    def task(self, kind: str, title: str, *, summary: str = "", compact: bool | None = None,
             key: str = "") -> LiveTask:
        """A new task (``key``: the rolling task of that key, the same one every time while it is
        not finished; it is pinned first). NO_TASK while disabled or closed."""
        if not self.enabled:
            return NO_TASK
        kind = kind if kind in TASK_KINDS else _safe_kind(kind)
        title_text = self._line(" ".join(_as_text(title).split()), TITLE_CAP)
        summary_text = self._line(summary, SUMMARY_CAP)
        key = self._line(key, 80)
        now, wall = self._clock(), self._wall()
        with self._lock:
            if self._closed:
                return NO_TASK
            if key:
                for existing in self._tasks.values():
                    if existing.key == key and not existing.finished:
                        return LiveTask(self, existing)
            self._next_task += 1
            record = _Task(self._next_task, kind, now, wall)
            record.title, record.summary = title_text, summary_text
            record.compact = (kind in COMPACT_KINDS) if compact is None else bool(compact)
            record.key = key
            record.pinned = bool(key)
            self._tasks[record.id] = record
            self._resize(record, None, len(title_text) + len(summary_text))
            self._touch(record)
            self._trim()
        self._notify()
        return LiveTask(self, record)

    @_safe(tuple)
    def snapshot(self) -> tuple[TaskView, ...]:
        """Every task: the pinned ones first, then oldest -> newest."""
        with self._lock:
            return tuple(task.build() for task in self._ordered())

    def _ordered(self) -> list[_Task]:
        tasks = list(self._tasks.values())
        return [task for task in tasks if task.pinned] + [task for task in tasks if not task.pinned]

    @_safe(lambda: LiveChanges(0, (), (), True))
    def changes(self, since: int) -> LiveChanges:
        """What changed after version ``since`` (and re-arm the listener)."""
        since = int(since) if isinstance(since, (int, float)) and not isinstance(since, bool) else 0
        with self._lock:
            self._armed = True
            if since < self._removed_floor or since > self._version:
                return LiveChanges(self._version, tuple(task.build() for task in self._ordered()), (), True)
            tasks = tuple(task.build() for task in self._ordered() if task.revision > since)
            removed = tuple(task_id for version, task_id in self._removed if version > since)
            return LiveChanges(self._version, tasks, removed, False)

    @_safe(0)
    def running(self) -> int:
        """Tasks that ask for attention (an Ask, an approved card, web research) still RUNNING."""
        with self._lock:
            return sum(1 for task in self._tasks.values()
                       if task.attention and not task.finished and task.live_status() == STATUS_RUNNING)

    @_safe()
    def task_view(self, task_id: int) -> TaskView | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.build() if task is not None else None

    @_safe()
    def clear(self) -> None:
        """Every finished task goes; a running one stays whole (the owner never loses sight of
        something in flight). A rolling task (never finished) keeps only its steps still running."""
        with self._lock:
            for task in [task for task in self._tasks.values() if task.finished]:
                self._drop(task)
            for task in [task for task in self._tasks.values() if task.key and not task.finished]:
                kept = [step for step in task.steps if step.status == STATUS_RUNNING]
                if len(kept) == len(task.steps) and not task.steps_more:
                    continue
                gone = sum(step.size for step in task.steps if step.status != STATUS_RUNNING)
                for step in task.steps:
                    if step.status != STATUS_RUNNING:
                        self._release_picture(task, step)
                task.steps = kept
                task.steps_more = 0
                self._resize(task, None, -gone)
                self._touch(task)
        self._notify()

    @_safe()
    def set_listener(self, listener: Callable[[], None] | None) -> None:
        with self._lock:
            self._listener = listener if not self._closed else None
            self._armed = True

    @_safe()
    def close(self) -> None:
        """Shutdown: no listener, everything dropped; every later call is a no-op."""
        with self._lock:
            self._listener = None
            self._closed = True
            for task in list(self._tasks.values()):
                task.removed = True
                task.picture_bytes = 0
                for step in task.steps:
                    step.picture, step.picture_cost = None, 0
            self._tasks.clear()
            self._total = 0
            self._picture_total = 0

    @_safe("")
    def when_text(self, moment: datetime, *, seconds: bool = False) -> str:
        """"2:41 PM" / "14:41" ([display] clock); ``seconds``: "2:41:07 PM" / "14:41:07"."""
        from .text_prep import format_time

        text = format_time(moment, hour24=self.hour24)
        if not seconds:
            return text
        digits, _space, meridiem = text.partition(" ")
        digits = f"{digits}:{moment.second:02d}"
        return f"{digits} {meridiem}" if meridiem else digits

    # ---- used by the handles -----------------------------------------------------------------

    def _new_step(self, task: _Task, kind: str, title: str, status: str, status_text: str, summary: str,
                  fields: list[Field], items: list[Item], deadline: float | None, key: str) -> _Step | None:
        now, wall = self._clock(), self._wall()
        with self._lock:
            if not self._alive(task):
                return None
            existing = next((step for step in task.steps if key and step.key == key), None)
            if existing is None and len(task.steps) >= self.limits.max_steps:
                task.steps_more += 1
                self._touch(task)
                record = None
            else:
                self._seq += 1
                if existing is not None:   # a keyed step is replaced in place (same id, same row)
                    self._resize(task, existing, -existing.size)
                    self._release_picture(task, existing)
                    record = _Step(existing.id, task, self._seq, kind, key, now, wall)
                    task.steps[task.steps.index(existing)] = record
                else:
                    self._next_step += 1
                    record = _Step(self._next_step, task, self._seq, kind, key, now, wall)
                    task.steps.append(record)
                record.title, record.status_text, record.summary = title, status_text, summary
                record.status = status
                record.deadline = deadline
                if status != STATUS_RUNNING:
                    record.ended = now
                record.fields = fields[:self.limits.max_fields]
                record.items = items[:self.limits.max_items]
                record.items_more = max(0, len(items) - self.limits.max_items)
                size = (len(title) + len(status_text) + len(summary) + sum(_field_size(f) for f in record.fields)
                        + sum(_item_size(i) for i in record.items))
                self._resize(task, record, size)
                self._touch(task, record)
                self._trim()
        self._notify()
        return record

    def _mutate(self, task: _Task, step: _Step | None, change: Callable[[], int | None]) -> None:
        """Run ``change`` under the lock unless the task is finished, removed or the stream closed;
        it returns the size delta (None: nothing changed)."""
        with self._lock:
            if not self._alive(task) or (step is not None and step not in task.steps):
                return
            delta = change()
            if delta is None:
                return
            self._resize(task, step, delta)
            self._touch(task, step)
            if delta > 0:
                self._trim()
        self._notify()

    # ---- pictures (under the lock) ------------------------------------------------------------

    def _release_picture(self, task: _Task, step: _Step) -> None:
        """``step``'s picture goes (its bytes are no longer counted)."""
        if step.picture_cost:
            task.picture_bytes -= step.picture_cost
            self._picture_total -= step.picture_cost
        step.picture, step.picture_cost = None, 0

    def _set_picture(self, task: _Task, step: _Step, picture: Any) -> None:
        cost = _picture_cost(picture)
        self._release_picture(task, step)
        step.picture, step.picture_cost = picture, cost
        task.picture_bytes += cost
        self._picture_total += cost

    def _admit_picture(self, task: _Task, step: _Step, picture: Any) -> Any:
        """``picture`` as ``step`` may keep it (the LiveLimits picture caps): too big for one picture or
        for this task's share -> not kept; over the total -> the READY pictures of the oldest finished,
        unpinned other tasks go first, then this one if it still does not fit. A RUNNING task never
        loses a picture."""
        if picture.state == PICTURE_DROPPED:
            return picture
        limits = self.limits
        cost = _picture_cost(picture)
        if cost > limits.max_picture_bytes:
            return picture.dropped(PICTURE_TOO_BIG)
        if task.picture_bytes - step.picture_cost + cost > limits.max_task_picture_bytes:
            return picture.dropped(PICTURE_TASK_CAP)

        def over() -> bool:
            return self._picture_total - step.picture_cost + cost > limits.max_total_picture_bytes

        for other in list(self._tasks.values()):   # oldest first
            if not over():
                break
            if other is task or not other.finished or other.pinned:
                continue
            touched = False
            for record in other.steps:
                held = record.picture
                if held is None or held.state != PICTURE_READY:
                    continue
                self._set_picture(other, record, held.dropped(PICTURE_MEMORY))
                self._touch(other, record)
                touched = True
                if not over():
                    break
            if touched:
                self._touch(other)
        return picture.dropped(PICTURE_MEMORY) if over() else picture

    def _store_picture(self, task: _Task, step: _Step, picture: Any) -> None:
        """Keep ``picture`` on ``step`` (the rules of LiveStep.picture)."""
        with self._lock:
            if self._closed or task.removed or step not in task.steps:
                return
            if task.finished:   # only a late picture for a step that waits for one (a page snapshot)
                held = step.picture
                if held is None or held.state != PICTURE_PENDING or held.kind != picture.kind:
                    return
            kept = self._admit_picture(task, step, picture)
            self._set_picture(task, step, kept)
            self._touch(task, step)
        self._notify()

    def _finish(self, task: _Task, status: str, status_text: str, summary: str | None) -> None:
        now = self._clock()
        with self._lock:
            if not self._alive(task) or task.key:
                return   # a rolling task is never finished
            delta = 0
            task.status = status
            task.status_text = status_text
            if summary is not None:
                delta += len(summary) - len(task.summary)
                task.summary = summary
            task.ended = now
            task.finished = True
            for step in task.steps:
                if step.status == STATUS_RUNNING:
                    step.status = status
                    step.ended = now
                    step.deadline = None
                    if status == STATUS_CANCELLED and summary and not step.summary:
                        delta += len(summary)
                        step.summary = summary
                    self._touch(task, step)
            self._resize(task, None, delta)
            self._touch(task)
            self._trim()
        self._notify()


def _status_or_blank(status: Any) -> str:
    return status if status in STATUSES else ""


def _picture_cost(picture: Any) -> int:
    """The bytes a picture is counted as (one that can't say counts as too big to keep)."""
    try:
        return max(0, int(picture.cost))
    except Exception:  # noqa: BLE001
        return 1 << 40


def _status(status: Any, default: str = STATUS_RUNNING) -> str:
    return status if status in STATUSES else default


# --------------------------------------------------------------------------
# Handles
# --------------------------------------------------------------------------

class LiveTask:
    """A handle on one task. NO_TASK (no stream) accepts every call and does nothing."""

    __slots__ = ("_stream", "_task")

    def __init__(self, stream: LiveStream | None, task: _Task | None) -> None:
        self._stream = stream
        self._task = task

    @property
    def id(self) -> int:
        return self._task.id if self._task is not None else 0

    @property
    def kind(self) -> str:
        return self._task.kind if self._task is not None else ""

    @property
    def finished(self) -> bool:
        task, stream = self._task, self._stream
        return task is None or stream is None or task.finished or task.removed or stream._closed

    @property
    def enabled(self) -> bool:
        """Something is recorded (a real task that is not finished)."""
        return not self.finished

    @property
    def keep_text(self) -> bool:
        return self._stream is not None and self._stream.keep_text

    @property
    def pictures(self) -> bool:
        """[live] pictures (the email and calendar pictures Jarvis draws are kept)."""
        return self._stream is not None and self._stream.pictures

    def __repr__(self) -> str:
        return f"LiveTask(id={self.id}, kind={_safe_kind(self.kind)!r}, finished={self.finished})"

    @_safe(lambda: NO_STEP)
    def step(self, kind: str, title: str, *, status: str = STATUS_RUNNING, status_text: str = "",
             summary: str = "", fields: Iterable[Field | tuple[str, str]] = (), items: Iterable[Item] = (),
             deadline: float | None = None, key: str = "") -> LiveStep:
        """A new step at the end (``key``: replaces the task's step of that key, same place)."""
        stream, task = self._stream, self._task
        if stream is None or task is None or self.finished:
            return NO_STEP
        clean_fields = [found for found in (stream._field(item) for item in fields) if found is not None]
        merged: dict[str, Field] = {}
        for item in clean_fields:   # the same label once (the last value)
            merged[item.label] = item
        clean_items = [found for found in (stream._item(item) for item in items) if found is not None]
        record = stream._new_step(task, _safe_kind(kind), stream._line(title, TITLE_CAP), _status(status),
                                  clean_status_text(status_text), stream._line(summary, SUMMARY_CAP),
                                  list(merged.values()), clean_items, _deadline(deadline),
                                  stream._line(key, 120))
        return LiveStep(stream, record) if record is not None else NO_STEP

    @_safe(lambda: NO_STEP)
    def find(self, kind: str, key: str = "") -> LiveStep:
        """The newest step of ``kind`` (and of ``key`` when given); NO_STEP when there is none."""
        stream, task = self._stream, self._task
        if stream is None or task is None:
            return NO_STEP
        key = stream._line(key, 120) if key else ""
        with stream._lock:
            for record in reversed(task.steps):
                if record.kind == kind and (not key or record.key == key):
                    return LiveStep(stream, record)
        return NO_STEP

    @_safe(lambda: NO_STEP)
    def current(self) -> LiveStep:
        """The newest step still RUNNING (NO_STEP when none is)."""
        stream, task = self._stream, self._task
        if stream is None or task is None:
            return NO_STEP
        with stream._lock:
            for record in reversed(task.steps):
                if record.status == STATUS_RUNNING:
                    return LiveStep(stream, record)
        return NO_STEP

    @_safe()
    def note(self, text: str) -> None:
        """A note on the newest RUNNING step (the last step when none runs)."""
        step = self.current()
        if step is NO_STEP and self._stream is not None and self._task is not None:
            with self._stream._lock:
                last = self._task.steps[-1] if self._task.steps else None
            step = LiveStep(self._stream, last) if last is not None else NO_STEP
        step.note(text)

    @_safe()
    def update(self, *, title: str | None = None, summary: str | None = None) -> None:
        stream, task = self._stream, self._task
        if stream is None or task is None:
            return
        new_title = stream._line(" ".join(_as_text(title).split()), TITLE_CAP) if title is not None else None
        new_summary = stream._line(summary, SUMMARY_CAP) if summary is not None else None

        def change() -> int:
            delta = 0
            if new_title is not None:
                delta += len(new_title) - len(task.title)
                task.title = new_title
            if new_summary is not None:
                delta += len(new_summary) - len(task.summary)
                task.summary = new_summary
            return delta

        stream._mutate(task, None, change)

    @_safe()
    def finish(self, status: str, *, status_text: str = "", summary: str | None = None) -> None:
        """The end: every step still RUNNING gets ``status`` too; later calls change nothing."""
        stream, task = self._stream, self._task
        if stream is None or task is None:
            return
        final = status if status in FINAL_STATUSES else STATUS_OK
        stream._finish(task, final, clean_status_text(status_text),
                       stream._line(summary, SUMMARY_CAP) if summary is not None else None)


class LiveStep:
    """A handle on one step. NO_STEP accepts every call and does nothing."""

    __slots__ = ("_stream", "_step")

    def __init__(self, stream: LiveStream | None, step: _Step | None) -> None:
        self._stream = stream
        self._step = step

    @property
    def id(self) -> int:
        return self._step.id if self._step is not None else 0

    @property
    def active(self) -> bool:
        """A real step whose task still records."""
        step, stream = self._step, self._stream
        return step is not None and stream is not None and stream._alive(step.task) and step in step.task.steps

    def __repr__(self) -> str:
        return f"LiveStep(id={self.id})"

    def _apply(self, change: Callable[[_Step], int | None]) -> None:
        stream, step = self._stream, self._step
        if stream is None or step is None:
            return
        stream._mutate(step.task, step, lambda: change(step))

    @_safe()
    def update(self, *, title: str | None = None, status: str | None = None, status_text: str | None = None,
               summary: str | None = None, deadline: Any = UNCHANGED) -> None:
        stream = self._stream
        if stream is None or self._step is None:
            return
        new_title = stream._line(title, TITLE_CAP) if title is not None else None
        new_text = clean_status_text(status_text) if status_text is not None else None
        new_summary = stream._line(summary, SUMMARY_CAP) if summary is not None else None
        new_status = status if status in STATUSES else None
        new_deadline = _deadline(deadline) if deadline is not UNCHANGED else UNCHANGED
        now = stream._clock()

        def change(step: _Step) -> int:
            delta = 0
            if new_title is not None:
                delta += len(new_title) - len(step.title)
                step.title = new_title
            if new_text is not None:
                delta += len(new_text) - len(step.status_text)
                step.status_text = new_text
            if new_summary is not None:
                delta += len(new_summary) - len(step.summary)
                step.summary = new_summary
            if new_status is not None and not (new_status == STATUS_RUNNING and step.ended is not None):
                step.status = new_status
                if new_status != STATUS_RUNNING and step.ended is None:
                    step.ended = now
            if new_deadline is not UNCHANGED:
                step.deadline = new_deadline
            return delta

        self._apply(change)

    @_safe()
    def field(self, label: str, value: str, *, mono: bool = False, link: str = "") -> None:
        """A "LABEL  value" row; the same label again replaces its value (same place)."""
        stream = self._stream
        if stream is None or self._step is None:
            return
        item = Field(stream._line(label, LABEL_CAP), stream._line(value, FIELD_CAP), bool(mono), result_link(link))
        limit = stream.limits.max_fields

        def change(step: _Step) -> int | None:
            for index, old in enumerate(step.fields):
                if old.label == item.label:
                    step.fields[index] = item
                    return _field_size(item) - _field_size(old)
            if len(step.fields) >= limit:
                return None
            step.fields.append(item)
            return _field_size(item)

        self._apply(change)

    @_safe()
    def item(self, text: str, *, status: str = "", status_text: str = "", note: str = "",
             mono: bool = False) -> None:
        stream = self._stream
        if stream is None or self._step is None:
            return
        entry = Item(stream._line(text, ITEM_CAP), _status_or_blank(status), clean_status_text(status_text),
                     stream._line(note, ITEM_CAP), bool(mono))
        limit = stream.limits.max_items

        def change(step: _Step) -> int:
            if len(step.items) >= limit:
                step.items_more += 1
                return 0
            step.items.append(entry)
            return _item_size(entry)

        self._apply(change)

    @_safe()
    def note(self, text: str) -> None:
        """A timestamped progress line; past the cap the oldest go (the first one is kept)."""
        stream = self._stream
        if stream is None or self._step is None:
            return
        entry = Note(stream._clock(), stream._line(text, NOTE_CAP))
        limit = max(1, stream.limits.max_notes)

        def change(step: _Step) -> int:
            delta = len(entry.text)
            if len(step.notes) >= limit:
                gone = step.notes.pop(1 if limit > 1 else 0)
                step.notes_more += 1
                delta -= len(gone.text)
            step.notes.append(entry)
            return delta

        self._apply(change)

    @_safe()
    def block(self, label: str, text: str, *, start_open: bool = False, untrusted: bool = False) -> None:
        """Collapsible text, kept exactly (cut at MAX_BLOCK_CHARS; only its size with [live]
        text = false); the same label again replaces it."""
        stream = self._stream
        if stream is None or self._step is None or not self.active:
            return
        entry = stream._block(label, text, start_open, untrusted)

        def change(step: _Step) -> int:
            for index, old in enumerate(step.blocks):
                if old.label == entry.label:
                    step.blocks[index] = entry
                    return _block_size(entry) - _block_size(old)
            step.blocks.append(entry)
            return _block_size(entry)

        self._apply(change)

    @_safe()
    def rename_block(self, label: str, new_label: str) -> None:
        """The block called ``label`` is called ``new_label`` from now on (its text unchanged);
        nothing when there is none (or one is called ``new_label`` already)."""
        stream = self._stream
        if stream is None or self._step is None:
            return
        old_name = stream._line(label, BLOCK_LABEL_CAP)
        new_name = stream._line(new_label, BLOCK_LABEL_CAP)

        def change(step: _Step) -> int | None:
            if old_name == new_name or any(item.label == new_name for item in step.blocks):
                return None
            for index, old in enumerate(step.blocks):
                if old.label == old_name:
                    step.blocks[index] = replace(old, label=new_name)
                    return len(new_name) - len(old_name)
            return None

        self._apply(change)

    @_safe()
    def done(self, status: str = STATUS_OK, *, status_text: str = "", summary: str | None = None) -> None:
        """The step's end (its end time is set once; a later done may change status and summary)."""
        final = status if status in FINAL_STATUSES else STATUS_OK
        self.update(status=final, status_text=status_text, summary=summary, deadline=None)

    @_safe()
    def picture(self, picture: Any) -> None:
        """A picture of this step (a pictures.Picture), shown first in its details; a new one replaces
        the old. Nothing for NO_STEP, anything but a Picture, a picture Jarvis draws (an email, a
        calendar, a web search) while [live] pictures is off, a page picture while [live] text is
        off, or a step whose task finished (except a late picture of the same kind for a step that
        waits for one: a page snapshot). With [live] text = false an email picture keeps its
        headers only, a search picture no snippet. Past the memory caps it is kept as "Picture not
        kept: <why>"."""
        stream, step = self._stream, self._step
        if stream is None or step is None or picture is None:
            return
        from . import pictures as _pictures

        if not isinstance(picture, _pictures.Picture) or picture.kind not in _pictures.KINDS:
            return
        if picture.kind in _pictures.MODEL_KINDS and not stream.pictures:
            return
        if picture.kind == _pictures.KIND_PAGE and not stream.keep_text:
            return
        if not stream.keep_text:
            picture = picture.without_text()
        stream._store_picture(step.task, step, picture)

    @_safe()
    def view(self) -> StepView | None:
        """The step as the view shows it now (None for NO_STEP or a step that is gone)."""
        stream, step = self._stream, self._step
        if stream is None or step is None:
            return None
        with stream._lock:
            if step.task.removed or step not in step.task.steps:
                return None
            return step.build()

    @_safe(tuple)
    def show(self, payload: Payload | None) -> tuple[str, ...]:
        """Show ``payload`` (what is sent or changed, exactly): its title, fields and body replace
        what the step showed. Returns the labels whose value differs from what it showed before
        (a preview), () when it showed none."""
        stream, step = self._stream, self._step
        if stream is None or step is None or payload is None:
            return ()
        title = stream._line(payload.title, TITLE_CAP)
        fields = [found for found in (stream._field(item) for item in payload.fields) if found is not None]
        fields = list({item.label: item for item in fields}.values())[:stream.limits.max_fields]
        body = stream._block(payload.body_label, payload.body, payload.start_open, False) if payload.body_label \
            else None
        changed: list[str] = []

        def change(record: _Step) -> int:
            before = {item.label: item.value for item in record.fields}
            old_blocks = {item.label: item for item in record.blocks}
            if before:
                for item in fields:
                    if before.get(item.label, "") != item.value:
                        changed.append(item.label)
                changed.extend(label for label in before if label not in {item.label for item in fields}
                               and before[label])
                if body is not None:
                    old = old_blocks.get(body.label)
                    if old is None or (old.text, old.chars) != (body.text, body.chars):
                        changed.append(body.label)
            delta = len(title) - len(record.title) - sum(_field_size(item) for item in record.fields)
            record.title = title
            record.fields = fields
            delta += sum(_field_size(item) for item in fields)
            if body is not None:
                for index, old in enumerate(record.blocks):
                    if old.label == body.label:
                        record.blocks[index] = body
                        delta += _block_size(body) - _block_size(old)
                        break
                else:
                    record.blocks.append(body)
                    delta += _block_size(body)
            return delta

        self._apply(change)
        if payload.picture is not None:   # not part of "changed": the fields and the body say that
            self.picture(payload.picture)
        return tuple(changed)


def _deadline(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


NO_TASK = LiveTask(None, None)
NO_STEP = LiveStep(None, None)
