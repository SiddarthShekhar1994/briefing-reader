"""The tidy card: what a NEEDS YOUR OK card of file steps shows and says, through its life (SPEC sections 5.4, 6.3
and 10.3; owner decisions D3 and D9; hard rules 4, 5, 7, 12, 14 and 17; Phase 2, part 3; no Qt).

A request's file steps (move, copy, rename, make a folder, zip) wait for the owner's click on ONE card that shows every
row of their frozen plan (desktop.cardplan.CardPlan, P2.2). ``make_card(plan, scope)`` runs on the desktop thread
right after the run (desktop.results.present) and gives the card's own copy of each row as it shows: the item's name
(hidden characters spelled out), where it goes ("Documents > Uni > LING", from the folder the owner allowed down; a
rename's new name), its flags as plain words (SPEC 5.4: shown as text, never as colour alone - "UPLOADS TO ONEDRIVE",
"LEAVES ONEDRIVE - REMOVED FROM YOUR OTHER DEVICES", "ONLINE-ONLY", "RENAMED - THAT NAME WAS TAKEN", "INSTALLER -
STAYS UNLESS YOU TICK IT", "BIG", "MANY FILES", "TYPE CHANGES" ...), whether it is ticked as the card shows (an
installer is not) and, for a row that can't go, why. The card keeps the plan itself (the click runs exactly it) and
the folder most of its items come from (the File Explorer window showing it gets the cyan bracket).

``TidyState`` is the card's life on the GUI thread (desktop_ui): WAITING (amber: a decision waits - the ticks, Not now
and "Move 5 files", "Nothing ticked" while nothing is), RUNNING ("MOVING 2 OF 5..." in cyan, each row's mark as it
ends, Stop), STOPPING (Stop or Esc pressed: it ends after the row on its way), DONE (the stamp "MOVED 5 FILES
<middle dot> 10:22 AM" in mint, or what was and wasn't done in coral, with Undo while there is something to put back),
REFUSED (nothing ran: the card expired or changed, the desktop can't act, or Jarvis on your desktop was turned off -
in plain words, coral), UNDOING ("PUTTING BACK...") and UNDONE (the stamp: "PUT BACK 5 OF 5" in mint, or the partial
stamp in coral - "PUT BACK 4 OF 5 <middle dot> 1 MOVED SINCE" - each row left as it is saying why: "Moved since - left
where it is."). ``tidy_text(state, hour24=)`` gives each state's copy (British spelling; the caption's "sir" is not
here) and its looks; ``said(state, address=)`` Jarvis's words for the caption once a batch or an Undo ends.

The card's copy holds names and places: it is shown, never logged. Every repr here names states, kinds, counts and
codes only.

Public API
==========
States ``WAITING``, ``RUNNING``, ``STOPPING``, ``DONE``, ``REFUSED``, ``UNDOING``, ``UNDONE``; tones ``AMBER``,
``CYAN``, ``MINT``, ``CORAL``; marks ``MARK_*``; button keys ``GO_KEY``, ``DISMISS_KEY``, ``STOP_KEY``, ``UNDO_KEY``;
codes ``UNAVAILABLE``, ``TURNED_OFF``, ``STOPPED_FIRST``; ``FLAG_WORDS``; ``TidyRow``, ``TidyCard`` (``ok_rows()``,
``counts()``), ``make_card(plan, scope, at=)``; ``RowEnd(number, state, reason, seq)``; ``CardOutcome``,
``UndoOutcome``; ``TidyState(card)`` (``toggle``, ``ticked_numbers``, ``waiting``, ``busy``, ``start``, ``started``,
``row_ended``, ``stop_asked``, ``finish``, ``undo_seqs``, ``undoable``, ``start_undo``, ``finish_undo``);
``RowText``, ``TidyText``, ``tidy_text(state, hour24=)``; ``said(state, address=)``; ``place_words(path, scope)``.
"""

from __future__ import annotations

import ntpath
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..persona import clock_text
from . import cardplan as cp
from . import fileops as fo
from . import guard, paths
from . import validate as dv
from .journal import DONE as DONE_STATE, UNDONE as UNDONE_STATE

WAITING, RUNNING, STOPPING, DONE, REFUSED, UNDOING, UNDONE = (
    "waiting", "running", "stopping", "done", "refused", "undoing", "undone")
STAGES = (WAITING, RUNNING, STOPPING, DONE, REFUSED, UNDOING, UNDONE)
AMBER, CYAN, MINT, CORAL = "amber", "cyan", "mint", "coral"
# A row's mark at its left: a tick box (WAITING), a box left unticked, done, on its way, not done, nothing yet, a row
# that can't go (no box).
MARK_TICK, MARK_IDLE, MARK_DONE, MARK_NOW, MARK_FAIL, MARK_NONE, MARK_INFO = (
    "tick", "idle", "done", "now", "fail", "none", "info")
GO_KEY, DISMISS_KEY, STOP_KEY, UNDO_KEY = "go", "dismiss", "stop", "undo"
UNAVAILABLE = "unavailable"             # the desktop can't act now (no backend, no journal): nothing ran
TURNED_OFF = "turned_off"               # Jarvis on your desktop was turned off before the batch began
STOPPED_FIRST = "stopped_first"         # Stop or Esc before the first row
SEP = " \u00b7 "                        # a middle dot between spaces
CRUMB = " \u203a "                      # a single right-pointing angle quotation mark between spaces
ELLIPSIS = "\u2026"

FLAG_WORDS = {
    fo.FLAG_TO_ONEDRIVE: "UPLOADS TO ONEDRIVE",
    fo.FLAG_LEAVES_ONEDRIVE: "LEAVES ONEDRIVE - REMOVED FROM YOUR OTHER DEVICES",
    fo.FLAG_ONLINE_ONLY: "ONLINE-ONLY",
    fo.FLAG_RECALL_ON_OPEN: "ONLINE-ONLY FOLDER",
    fo.FLAG_PINNED: "ALWAYS KEPT ON THIS DEVICE",
    fo.FLAG_HIDDEN_CHARS: "NAME HAD HIDDEN CHARACTERS",
    cp.FLAG_RENAMED: "RENAMED - THAT NAME WAS TAKEN",
    dv.FLAG_INSTALLER: "INSTALLER - STAYS UNLESS YOU TICK IT",
    dv.FLAG_BIG: "BIG",
    dv.FLAG_MANY: "MANY FILES",
    dv.FLAG_TYPE_CHANGED: "TYPE CHANGES",
}
_FLAG_ORDER = tuple(FLAG_WORDS)
_MIXED = "mixed"
# Per kind (or mixed): the kicker's words, the button's verb and nouns, the progress verb, the stamp's verb, the title.
_KICKERS = {fo.MOVE: "MOVE FILES", fo.COPY: "COPY FILES", fo.RENAME: "RENAME FILES", fo.ZIP: "ZIP FILES",
            fo.MKDIR: "NEW FOLDERS", _MIXED: "ORGANISE FILES"}
_BUTTONS = {fo.MOVE: ("Move", "file", "files"), fo.COPY: ("Copy", "file", "files"),
            fo.RENAME: ("Rename", "file", "files"), fo.ZIP: ("Make", "zip", "zips"),
            fo.MKDIR: ("Make", "folder", "folders"), _MIXED: ("Make", "change", "changes")}
_DOING = {fo.MOVE: "MOVING", fo.COPY: "COPYING", fo.RENAME: "RENAMING", fo.ZIP: "ZIPPING", fo.MKDIR: "MAKING",
          _MIXED: "WORKING"}
_DONE_VERB = {fo.MOVE: "MOVED", fo.COPY: "COPIED", fo.RENAME: "RENAMED", fo.ZIP: "MADE", fo.MKDIR: "MADE",
              _MIXED: "MADE"}
_PAST = {fo.MOVE: "moved", fo.COPY: "copied", fo.RENAME: "renamed", fo.ZIP: "zipped", fo.MKDIR: "made",
         _MIXED: "changed"}
_TITLES = {fo.MOVE: "Move these files into your folders", fo.COPY: "Copy these files",
           fo.RENAME: "Rename these files", fo.ZIP: "Zip these files", fo.MKDIR: "Make new folders",
           _MIXED: "Organise these files"}
TIDY_TITLE = "Tidy {folder} into your folders"
NOTHING_DELETED = "Nothing is deleted."
INSTALLERS_STAY = "Installers stay where they are."
UNDO_TEXT = "Undo"
UNDO_NOTE = "Puts every file back where it was."
UNDO_NOTE_KEPT = "Puts every file back where it was. Copies and zips stay - Jarvis can't bin them yet."
NO_UNDO_NOTE = "Undo can't take back copies or zips yet - that needs the Recycle Bin, which comes in a later update."
NOT_NOW_TEXT = "Not now"
STOP_TEXT = "Stop"
NOTHING_TICKED = "Nothing ticked"
REFUSED_NOTES = {
    cp.CARD_EXPIRED: "This card is more than 15 minutes old, so nothing was {past}. Ask again for a fresh one.",
    cp.CARD_CHANGED: "This card's plan isn't the one you were shown, so nothing was {past}. Ask again.",
    UNAVAILABLE: "Jarvis can't change files on this PC right now, so nothing was {past}.",
    TURNED_OFF: "Jarvis on your desktop was turned off, so nothing was {past}.",
    STOPPED_FIRST: "Stopped before anything was {past}.",
}
# A row the batch did not do, in Jarvis's words (the Undo's are fileops.UNDO_WORDS).
RUN_WORDS = {
    fo.R_CHANGED: "Changed since - left as it is.",
    fo.R_STOPPED: "Not done - you stopped it.",
    fo.R_IN_USE: "In use - another program has it open. Left where it is.",
    fo.R_NAME_CLASH: "Something already has that name there - left as it is.",
    fo.R_JOURNAL: "Not done - Jarvis couldn't keep its record of it.",
    fo.R_NO_FOLDER: "Not done - its new folder wasn't made.",
    fo.R_ACCESS: "Windows didn't allow it - left where it is.",
    fo.R_DISK_FULL: "Not done - the disk is full.",
}


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _sentence(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    text = text[:1].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


def _shown(name: Any) -> str:
    """A name as the card shows it: its hidden characters spelled out (<U+202E>)."""
    text = str(name or "")
    return "".join(f"<U+{ord(char):04X}>" if guard.hidden_char(char) else char for char in text)


def place_words(path: str, scope: paths.Scope) -> str:
    """A folder as the card says it, from the folder the owner allowed down: "Documents > Uni > LING" (CRUMB
    between); its own name for one outside them."""
    if not isinstance(path, str) or not path:
        return ""
    root = scope.root_of(path)
    if root is None:
        return _shown(ntpath.basename(path.rstrip("\\")))
    rest = path.rstrip("\\")[len(root.path.rstrip("\\")):].strip("\\")
    return CRUMB.join([root.label, *(_shown(part) for part in rest.split("\\") if part)])


# --------------------------------------------------------------------------
# The card (made on the desktop thread)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TidyRow:
    """One row as the card shows it (see the module docstring); ``code``: why it can't go ("" when it may)."""

    number: int
    kind: str
    name: str = field(default="", repr=False)
    to: str = field(default="", repr=False)
    flags: frozenset[str] = frozenset()
    ticked: bool = True
    code: str = ""
    folder: str = field(default="", repr=False)      # where its item is now (the bracket's folder)
    size: int = 0                                   # bytes it carries (BIG's words)

    @property
    def ok(self) -> bool:
        return not self.code

    def flag_words(self) -> str:
        words = []
        for flag in _FLAG_ORDER:
            if flag in self.flags:
                text = FLAG_WORDS[flag]
                if flag == dv.FLAG_BIG and self.size:
                    text = f"{text} - {self.size / 1024 ** 3:.1f} GB"
                words.append(text)
        return SEP.join(words)


@dataclass(frozen=True)
class TidyCard:
    """A card's rows as they show, its own frozen plan (``plan``: the click runs exactly it), its title, and the
    folder most of its items are in (``folder``; ``folder_name`` as the bracket's tag says it)."""

    run_id: str
    plan: Any = field(default=None, repr=False)
    rows: tuple[TidyRow, ...] = field(default=(), repr=False)
    title: str = field(default="", repr=False)
    folder: str = field(default="", repr=False)
    folder_name: str = field(default="", repr=False)
    at: datetime | None = field(default=None, repr=False)
    sha: str = field(default="", repr=False)          # the plan's sha256 as the card shows it (its click carries it)

    def ok_rows(self) -> tuple[TidyRow, ...]:
        return tuple(row for row in self.rows if row.ok)

    def row(self, number: int) -> TidyRow | None:
        return next((row for row in self.rows if row.number == number), None)

    def counts(self) -> dict[str, int]:
        return {"rows": len(self.rows), "ok": len(self.ok_rows()),
                "ticked": sum(row.ok and row.ticked for row in self.rows)}

    def __repr__(self) -> str:
        return f"TidyCard({self.run_id}, {self.counts()})"


def _kind_of(kinds: Collection[str]) -> str:
    found = set(kinds)
    return next(iter(found)) if len(found) == 1 else _MIXED


def _where(folder: Any, scope: paths.Scope, made: dict[str, cp.CardRow]) -> str:
    if isinstance(folder, str):                          # a folder an earlier row of the card makes
        maker = made.get(folder)
        if maker is None:
            return ""
        parent = place_words(getattr(maker.folder, "path", ""), scope)
        return CRUMB.join(part for part in (parent, _shown(maker.name)) if part)
    return place_words(getattr(folder, "path", ""), scope)


def _row(row: cp.CardRow, scope: paths.Scope, made: dict[str, cp.CardRow]) -> TidyRow:
    source = row.sources[0] if row.sources else None
    source_path = str(getattr(source, "path", "") or "")
    source_name = getattr(source, "name", "") or ntpath.basename(source_path.rstrip("\\"))
    place = _where(row.folder, scope, made)
    if row.kind == fo.RENAME:
        name, to = source_name, _shown(row.name)
    elif row.kind == fo.MKDIR:
        name, to = row.name, f"{place} (new folder)" if place else "a new folder"
    elif row.kind == fo.ZIP:
        what = _count(len(row.sources), "file", "files")
        name, to = row.name or "a zip", f"{place} (a zip of {what})" if place else f"a zip of {what}"
    elif row.kind == fo.COPY:
        name, to = row.name or source_name, f"{place} (a copy)" if place else "a copy"
    else:
        name, to = row.name or source_name, place
    size = sum(max(0, int(getattr(getattr(item, "identity", None), "size", 0) or 0)) for item in row.sources)
    return TidyRow(row.number, row.kind, _shown(name), to, frozenset(row.flags), bool(row.ticked and row.ok),
                   row.code, ntpath.dirname(source_path) if source_path else "", size)


def make_card(plan: cp.CardPlan | None, scope: paths.Scope, *, at: datetime | None = None) -> TidyCard | None:
    """The card of a run's frozen file plan (on the desktop thread, right after the run); None when it has no row
    that may go (the caption says why)."""
    if plan is None or not plan.rows or not any(row.ok for row in plan.rows):
        return None
    made = {row.key: row for row in plan.rows if row.kind == fo.MKDIR and row.key}
    rows = tuple(_row(row, scope, made) for row in plan.rows)
    folders = Counter(ntpath.normcase(row.folder) for row in rows if row.folder)
    folder = ""
    if folders:
        wanted = folders.most_common(1)[0][0]
        folder = next(row.folder for row in rows if row.folder and ntpath.normcase(row.folder) == wanted)
    name = place_words(folder, scope).split(CRUMB)[-1] if folder else ""
    kinds = {row.kind for row in rows}
    if kinds == {fo.MOVE} and folder and len(folders) == 1:
        title = TIDY_TITLE.format(folder=name)
    else:
        title = _TITLES[_kind_of(kinds)]
    return TidyCard(plan.run_id, plan, rows, title, folder, name, at, plan.sha256)


# --------------------------------------------------------------------------
# What came of a click (the desktop thread's answers)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RowEnd:
    """How one row ended: ``state`` (journal: done, failed, skipped; undone, undo_refused), its reason code and
    journal seq."""

    number: int
    state: str
    reason: str = ""
    seq: int = 0


@dataclass(frozen=True)
class CardOutcome:
    """A click's batch: refused before anything ran (``code``) or each chosen row's end; ``esc``: Esc was held."""

    run_id: str
    code: str = ""
    rows: tuple[RowEnd, ...] = ()
    seconds: float = 0.0
    esc: bool = False

    def counts(self) -> dict[str, int]:
        return dict(Counter(row.state for row in self.rows))


@dataclass(frozen=True)
class UndoOutcome:
    """An Undo: each row's end (undone or undo_refused with its reason), the partial stamp (fileops.UndoReport)."""

    run_id: str
    rows: tuple[RowEnd, ...] = ()
    stamp: str = ""
    seconds: float = 0.0
    code: str = ""

    @property
    def put_back(self) -> int:
        return sum(1 for row in self.rows if row.state == UNDONE_STATE)


# --------------------------------------------------------------------------
# The card's life (GUI thread)
# --------------------------------------------------------------------------

class TidyState:
    """See the module docstring. Memory only; its repr names its stage and counts."""

    def __init__(self, card: TidyCard) -> None:
        self.card = card
        self.stage = WAITING
        self._ticked = {row.number for row in card.rows if row.ok and row.ticked}
        self.chosen: tuple[int, ...] = ()
        self.ends: dict[int, RowEnd] = {}
        self.ended = 0
        self.esc = False
        self.stop_reason = ""
        self.outcome: CardOutcome | None = None
        self.at: datetime | None = None
        self.undo: UndoOutcome | None = None
        self.undo_ends: dict[int, RowEnd] = {}
        self.undone_at: datetime | None = None
        self.last_code = ""                  # a job that ran nothing and gave the card back (stopped first, ...)
        self.undo_expired = False            # its stamp is older than [desktop] undo_days: Undo is no longer offered

    def __repr__(self) -> str:
        return f"TidyState({self.card.run_id}, {self.stage}, ticked={len(self._ticked)}, ended={self.ended})"

    # ---- the ticks (WAITING) ----------------------------------------------------------------------------

    def toggle(self, number: int, ticked: bool) -> bool:
        """The owner's tick on row ``number`` (only while the card waits, only a row that may go)."""
        row = self.card.row(number)
        if self.stage != WAITING or row is None or not row.ok:
            return False
        if ticked:
            self._ticked.add(number)
        else:
            self._ticked.discard(number)
        return True

    def ticked_numbers(self) -> frozenset[int]:
        return frozenset(self._ticked)

    def is_ticked(self, number: int) -> bool:
        return number in self._ticked

    def waiting(self) -> bool:
        """A decision waits on it (the "N need your OK" chip counts it)."""
        return self.stage == WAITING

    def busy(self) -> bool:
        return self.stage in (RUNNING, STOPPING, UNDOING)

    # ---- the batch -------------------------------------------------------------------------------------

    def start(self) -> None:
        """The owner's click: the ticked rows that may go are carried out, in order."""
        self.chosen = tuple(row.number for row in self.card.rows if row.ok and row.number in self._ticked)
        self.stage = RUNNING
        self.ends, self.ended, self.stop_reason, self.last_code = {}, 0, "", ""

    def started(self, esc: bool) -> None:
        """The batch began acting (its plan's check passed); ``esc``: Esc is held for it."""
        self.esc = bool(esc)

    def row_ended(self, end: RowEnd, ended: int) -> None:
        if self.stage in (RUNNING, STOPPING):
            self.ends[end.number] = end
            self.ended = max(self.ended, int(ended))

    def stop_asked(self, reason: str) -> bool:
        """Stop or Esc (``reason``: "stop", "esc"): it ends after the row on its way."""
        if self.stage not in (RUNNING, UNDOING) or self.stop_reason:
            return False
        self.stop_reason = reason
        if self.stage == RUNNING:
            self.stage = STOPPING
        return True

    def finish(self, outcome: CardOutcome, at: datetime) -> None:
        self.esc = self.esc or outcome.esc
        if outcome.code == STOPPED_FIRST:              # Stop before anything ran: the card waits again
            self.stage, self.chosen, self.ends, self.ended, self.stop_reason = WAITING, (), {}, 0, ""
            self.last_code = STOPPED_FIRST
            return
        self.outcome = outcome
        self.at = at
        if outcome.code:
            self.stage = REFUSED
            return
        for end in outcome.rows:
            self.ends[end.number] = end
        self.ended = len(outcome.rows)
        self.stage = DONE

    def done_numbers(self) -> tuple[int, ...]:
        return tuple(number for number in self.chosen
                     if self.ends.get(number) is not None and self.ends[number].state == DONE_STATE)

    def stopped(self) -> bool:
        return any(end.reason == fo.R_STOPPED for end in self.ends.values())

    # ---- Undo ------------------------------------------------------------------------------------------

    def undo_seqs(self) -> tuple[int, ...]:
        """The journal seqs Undo puts back: every row done (fileops.Undoer goes newest first, item by item)."""
        return tuple(self.ends[number].seq for number in self.done_numbers() if self.ends[number].seq)

    def undoable(self) -> bool:
        """Undo is offered: done, and a row done can be put back now (a move, a rename or a new folder - copies and
        zips wait for the Recycle Bin, P2.6)."""
        return self.stage == DONE and not self.undo_expired and bool(self.undo_seqs()) and any(
            self.card.row(number) is not None and self.card.row(number).kind in (fo.MOVE, fo.RENAME, fo.MKDIR)
            for number in self.done_numbers())

    def start_undo(self) -> None:
        self.stage = UNDOING
        self.stop_reason, self.last_code = "", ""

    def finish_undo(self, outcome: UndoOutcome, at: datetime) -> None:
        if outcome.code:                               # nothing was put back: the stamp keeps its Undo
            self.stage, self.stop_reason, self.last_code = DONE, "", outcome.code
            return
        self.undo = outcome
        self.undone_at = at
        by_seq = {self.ends[number].seq: number for number in self.done_numbers()}
        self.undo_ends = {by_seq[end.seq]: end for end in outcome.rows if end.seq in by_seq}
        self.stage = UNDONE


# --------------------------------------------------------------------------
# Words (pure)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RowText:
    """One row as the window paints it: its mark, name, "to" words, flag words, whether it is struck through
    (unticked) and its line of words under it (why it can't go, wasn't done or was left; ``words_tone``)."""

    number: int
    name: str
    to: str
    flags: str = ""
    mark: str = MARK_NONE
    ticked: bool = False
    struck: bool = False
    words: str = ""
    words_tone: str = CORAL


@dataclass(frozen=True)
class TidyText:
    """The card's copy in a state: ``buttons`` (key, words, role, at the right, enabled), the Undo link and its note."""

    kicker: str
    title: str
    tone: str
    rows: tuple[RowText, ...] = ()
    note: str = ""
    note_tone: str = AMBER
    buttons: tuple[tuple[str, str, str, bool, bool], ...] = ()
    link: tuple[str, str] | None = None
    link_note: str = ""


def _ticked_kind(state: TidyState, numbers: Collection[int]) -> str:
    kinds = [state.card.row(number).kind for number in numbers if state.card.row(number) is not None]
    return _kind_of(kinds) if kinds else _kind_of(row.kind for row in state.card.ok_rows())


def _go_words(state: TidyState) -> tuple[str, bool]:
    ticked = [number for number in state.ticked_numbers() if state.card.row(number) is not None]
    if not ticked:
        return NOTHING_TICKED, False
    verb, one, many = _BUTTONS[_ticked_kind(state, ticked)]
    return f"{verb} {_count(len(ticked), one, many)}", True


def _refused_words(code: str) -> str:
    return _sentence(cp.row_words(code) or dv.reason_words(code) or "it can't go")


def _run_words(reason: str) -> str:
    return RUN_WORDS.get(reason) or (_sentence(cp.row_words(reason)) if cp.row_words(reason) else
                                    "Not done - left as it is.")


def _waiting_note(state: TidyState) -> str:
    rows = state.card.rows
    ticked = state.ticked_numbers()
    parts = [NOTHING_DELETED]
    if any(dv.FLAG_INSTALLER in row.flags and row.number not in ticked for row in rows if row.ok):
        parts.append(INSTALLERS_STAY)
    up = sum(1 for row in rows if row.number in ticked and fo.FLAG_TO_ONEDRIVE in row.flags)
    if up:
        parts.append(f"{up} {'goes' if up == 1 else 'go'} to OneDrive and will upload.")
    out = sum(1 for row in rows if row.number in ticked and fo.FLAG_LEAVES_ONEDRIVE in row.flags)
    if out:
        parts.append(f"{out} {'leaves' if out == 1 else 'leave'} OneDrive - removed from your other devices.")
    return " ".join(parts)


def _row_texts(state: TidyState) -> tuple[RowText, ...]:
    stage = state.stage
    texts = []
    upcoming = next((number for number in state.chosen if number not in state.ends), None)
    for row in state.card.rows:
        flags = row.flag_words()
        if not row.ok:
            texts.append(RowText(row.number, row.name, row.to, flags, MARK_INFO, False, False,
                                 _refused_words(row.code), CORAL))
            continue
        if stage == WAITING:
            ticked = state.is_ticked(row.number)
            texts.append(RowText(row.number, row.name, row.to, flags, MARK_TICK, ticked, not ticked))
            continue
        if row.number not in state.chosen:
            texts.append(RowText(row.number, row.name, row.to, flags, MARK_IDLE, False, True))
            continue
        end = state.ends.get(row.number)
        undo = state.undo_ends.get(row.number)
        if undo is not None:
            if undo.state == UNDONE_STATE:
                texts.append(RowText(row.number, row.name, row.to, flags, MARK_DONE, True, False, "Put back.", MINT))
            else:
                words = fo.UNDO_WORDS.get(undo.reason, fo.DEFAULT_UNDO_WORDS)
                texts.append(RowText(row.number, row.name, row.to, flags, MARK_FAIL, True, False, words, CORAL))
            continue
        if end is None:
            mark = MARK_NOW if stage in (RUNNING, STOPPING) and row.number == upcoming else MARK_NONE
            texts.append(RowText(row.number, row.name, row.to, flags, mark, True, False))
        elif end.state == DONE_STATE:
            texts.append(RowText(row.number, row.name, row.to, flags, MARK_DONE, True, False))
        else:
            texts.append(RowText(row.number, row.name, row.to, flags, MARK_FAIL, True, False,
                                 _run_words(end.reason), CORAL))
    return tuple(texts)


def tidy_text(state: TidyState, hour24: bool = False) -> TidyText:
    """The card's copy and looks in its state (see the module docstring)."""
    card = state.card
    rows = _row_texts(state)
    kind = _ticked_kind(state, state.chosen) if state.chosen else _kind_of(row.kind for row in card.ok_rows())
    stage = state.stage
    if stage == WAITING:
        go, enabled = _go_words(state)
        kicker = f"{_KICKERS[_kind_of(row.kind for row in card.ok_rows())]}{SEP}NEEDS YOUR OK"
        buttons = ((DISMISS_KEY, NOT_NOW_TEXT, "quiet", False, True), (GO_KEY, go, "commit", True, enabled))
        return TidyText(kicker, card.title, AMBER, rows, _waiting_note(state), AMBER, buttons)
    total = len(state.chosen)
    if stage in (RUNNING, STOPPING):
        if stage == STOPPING:
            kicker = f"STOPPING AFTER THIS ONE{ELLIPSIS}"
        else:
            kicker = f"{_DOING[kind]} {min(state.ended + 1, max(total, 1))} OF {total}{ELLIPSIS}"
        note = ("Stop or Esc stops it after this one." if state.esc else "Stop stops it after this one.") \
            + " " + NOTHING_DELETED
        buttons = ((STOP_KEY, STOP_TEXT, "quiet", True, stage == RUNNING),)
        return TidyText(kicker, card.title, CYAN, rows, note, CYAN, buttons)
    when = f"{SEP}{clock_text(state.at, hour24)}" if state.at is not None else ""
    if stage == REFUSED:
        code = state.outcome.code if state.outcome is not None else UNAVAILABLE
        note = REFUSED_NOTES.get(code, REFUSED_NOTES[UNAVAILABLE]).format(past=_PAST[kind])
        return TidyText(f"NOTHING {_DONE_VERB[kind] if kind != _MIXED else 'CHANGED'}{when}", card.title, CORAL,
                        rows, note, CORAL)
    if stage in (DONE, UNDOING):
        done = len(state.done_numbers())
        verb, one, many = _DONE_VERB[kind], *_BUTTONS[kind][1:]
        if stage == UNDOING:
            kicker = f"PUTTING BACK{ELLIPSIS}"
            buttons = ((STOP_KEY, STOP_TEXT, "quiet", True, not state.stop_reason),)
            return TidyText(kicker, card.title, CYAN, rows, "", CYAN, buttons)
        if done == 0:
            kicker = f"NOTHING {verb if kind != _MIXED else 'CHANGED'}{when}"
        elif done == total:
            kicker = f"{verb} {_count(done, one, many).upper()}{when}"
        else:
            kicker = f"{verb} {done} OF {total} {many.upper()}{when}"
        if state.stopped():
            kicker = f"STOPPED BY YOU{SEP}{kicker}"
        tone = MINT if done == total and done else CORAL
        if state.undoable():
            kept = any(card.row(number) is not None and card.row(number).kind in (fo.COPY, fo.ZIP)
                       for number in state.done_numbers())
            return TidyText(kicker, card.title, tone, rows, "", MINT, (), (UNDO_KEY, UNDO_TEXT),
                            UNDO_NOTE_KEPT if kept else UNDO_NOTE)
        note = NO_UNDO_NOTE if done else f"Nothing was {_PAST[kind]}."
        return TidyText(kicker, card.title, tone, rows, note, CYAN if done else CORAL)
    undo = state.undo
    if undo is None or undo.code:
        return TidyText(f"NOT PUT BACK{when}", card.title, CORAL, rows,
                        "Jarvis couldn't put anything back right now - nothing was changed.", CORAL)
    when = f"{SEP}{clock_text(state.undone_at, hour24)}" if state.undone_at is not None else ""
    full = undo.put_back == len(undo.rows) and undo.rows
    left = len(undo.rows) - undo.put_back
    note = "Everything is back as it was." if full else \
        f"{left} {'was' if left == 1 else 'were'} left as {'it is' if left == 1 else 'they are'} - see why above."
    return TidyText(f"{undo.stamp}{when}" if full else undo.stamp, card.title, MINT if full else CORAL, rows, note,
                    MINT if full else CORAL)


def said(state: TidyState, address: str = "sir") -> str:
    """Jarvis's words for the caption once a batch, a refusal or an Undo ended ("" otherwise)."""
    card = state.card
    kind = _ticked_kind(state, state.chosen) if state.chosen else _kind_of(row.kind for row in card.ok_rows())
    past = _PAST[kind]
    if state.stage == WAITING and state.last_code == STOPPED_FIRST:
        return f"Stopped, {address} - nothing was {past}."
    if state.stage == DONE and state.last_code:
        why = "you stopped it" if state.last_code == STOPPED_FIRST else "I can't change files on this PC right now"
        return f"Nothing was put back, {address} - {why}."
    if state.stage == REFUSED:
        code = state.outcome.code if state.outcome is not None else UNAVAILABLE
        why = {cp.CARD_EXPIRED: "the card was more than 15 minutes old", cp.CARD_CHANGED: "the card's plan changed",
               TURNED_OFF: "Jarvis on your desktop was turned off", STOPPED_FIRST: "you stopped it"}.get(
            code, "I can't change files on this PC right now")
        return f"Nothing was {past}, {address} - {why}."
    if state.stage == DONE:
        done, total = len(state.done_numbers()), len(state.chosen)
        _verb, one, many = _BUTTONS[kind]
        if state.stopped():
            return f"Stopped, {address} - {past} {done} of {total}."
        if done == total and done:
            return f"{past.capitalize()} {_count(done, one, many)}, {address}."
        if not done:
            return f"Nothing was {past}, {address} - see the card for why."
        return f"{past.capitalize()} {done} of {total}, {address} - see the card for the rest."
    if state.stage == UNDONE and state.undo is not None:
        undo = state.undo
        if undo.code or not undo.rows:
            return f"I couldn't put anything back, {address}."
        if undo.put_back == len(undo.rows):
            return f"Put back, {address}. Everything is where it was."
        return f"Put back {undo.put_back} of {len(undo.rows)}, {address} - see the card for the rest."
    return ""


def ends_from(numbers: Sequence[int], results: Sequence[Any]) -> tuple[RowEnd, ...]:
    """A batch's results (fileops.OpResult, in the order of the chosen rows ``numbers``) as RowEnds."""
    return tuple(RowEnd(number, str(getattr(result, "state", "")), str(getattr(result, "reason", "") or ""),
                        int(getattr(result, "seq", 0) or 0)) for number, result in zip(numbers, results))
