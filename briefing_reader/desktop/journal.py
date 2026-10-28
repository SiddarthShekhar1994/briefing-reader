"""The desktop journal: what Jarvis did to the owner's files, saved before and after each OS call (SPEC section 6.1;
hard rules 12 and 14; no Qt).

One JSON Lines file per run, ``<run_id>.jsonl``, in the journal folder. The app's folder is
%LOCALAPPDATA%\\briefing-reader\\desktop\\journal (``default_folder()``: on this PC, never OneDrive); the folder is
always a parameter, and while the tests run ``default_folder()`` refuses (backend.RealBackendInTests), so a test can
only ever pass its own temporary folder.

Each line is one ``Record``: an item of the run - ``seq`` is its row on the card - going into a state. The lines are
appended, never rewritten; an item's state is its latest line (``items``). The states:

* ``planned`` - on the card, not started; ``doing`` - its one OS call is in flight; ``done``, ``failed`` (with a
  reason code) or ``skipped`` (never tried: a check refused it, or the owner stopped the run first);
* putting it back (Undo, the owner's click): a ``doing`` line with ``undo`` set, then ``undone`` or ``undo_refused``
  (with the reason the card shows; an item refused once may be tried again).

Write-ahead (hard rule 12): fileops appends "doing" - written, flushed and FlushFileBuffers'd (os.fsync) - before the
OS call, and does nothing when it cannot be saved (``JournalError``); then "done" or "failed". What a record's identity
fields (``volume_serial``, ``file_id``, ``size``, ``mtime_ns``, ``attrs``) describe: the item as it was found before
the call on a "planned"/"doing" line (the source), and the result on a "done" line (a moved file keeps its id; a copy,
a zip or a new folder is a new item, and a copy's or a zip's ``sha256`` is its content's).

Start-up recovery (``recover``): an item left "doing" by a crash is corrected by looking - never redone. A move or a
rename is "done" when its file id is at ``dst``, "failed" (interrupted) when it is still at ``src``, "failed" (lost)
when it is at neither; a file copy is "done" only when its output's content is its (unchanged) source's, else
"failed" (partial, or interrupted); a zip or a folder copy - written under a temporary name, then its file id and
hash saved, then renamed into place - is "done" only when that file id is at ``dst``, "failed" (partial) when it is
still under the temporary name or only part of it was made, else "failed"; a new folder is "done" when it is there.
An Undo left "doing" becomes "undone" when the item is back (or Jarvis's own output is gone), else "undo_refused"
again. A path that is no plain path on a fixed drive now is never touched: that item is "failed" (unchecked).

``purge`` removes the runs older than ``PURGE_DAYS`` (30; at start-up and daily); Undo is offered for [desktop]
undo_days (1-30) only (fileops.Undoer). The journal holds paths - Undo needs them - so it is the owner's own local
file: it is never logged and never sent to Claude. Logs carry run ids, counts, states, reason codes and exception
types only.

Public API
==========
Kinds ``MOVE``, ``RENAME``, ``COPY``, ``MKDIR``, ``ZIP``, ``RECYCLE``, ``SCREENSHOT`` (``KINDS``); states
``PLANNED``, ``DOING``, ``DONE``, ``FAILED``, ``SKIPPED``, ``UNDONE``, ``UNDO_REFUSED`` (``STATES``); recovery's reason
codes ``R_RECOVERED``, ``R_INTERRUPTED``, ``R_LOST``, ``R_PARTIAL``, ``R_UNCHECKED``; ``PURGE_DAYS``.
``Record`` (``to_line()``, ``from_json(value)``, ``identity``, ``with_identity(identity)``), ``Item`` (``state``,
``reason_code``, ``first``, ``last``, ``done``, ``doing``, ``undoing``), ``items(records)``, ``JournalError``,
``Journal(folder, run_id, clock=)`` (``append(record)``, ``extend(records)``, ``read()``, ``records()``,
``items()``, ``next_seq()``), ``run_id_ok(text)``, ``default_folder()``, ``journal_files(folder)``,
``RecoveryReport``, ``recover(folder, backend, clock=)``, ``purge(folder, now=, days=)``, ``same_place(a, b)``.
"""

from __future__ import annotations

import json
import logging
import ntpath
import os
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .backend import DRIVE_FIXED, DesktopBackend, FileIdentity, RealBackendInTests, real_backend_blocked

logger = logging.getLogger(__name__)

MOVE, RENAME, COPY, MKDIR, ZIP, RECYCLE, SCREENSHOT = "move", "rename", "copy", "mkdir", "zip", "recycle", "screenshot"
KINDS = (MOVE, RENAME, COPY, MKDIR, ZIP, RECYCLE, SCREENSHOT)
PLANNED, DOING, DONE, FAILED, SKIPPED, UNDONE, UNDO_REFUSED = (
    "planned", "doing", "done", "failed", "skipped", "undone", "undo_refused")
STATES = (PLANNED, DOING, DONE, FAILED, SKIPPED, UNDONE, UNDO_REFUSED)
# Recovery's reason codes (an item a crash left "doing").
R_RECOVERED = "recovered"          # it had happened: corrected to done / undone
R_INTERRUPTED = "interrupted"      # it had not happened (still at its source; nothing or only a temporary name made)
R_LOST = "lost"                    # it is at neither its source nor its destination
R_PARTIAL = "partial"              # a copy's output is there but not all of it (left for the owner)
R_UNCHECKED = "unchecked"          # its path is no plain path on a fixed drive now: never touched
PURGE_DAYS = 30
JOURNAL_SUFFIX = ".jsonl"
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_DAY_S = 86_400
_MAX_LINE = 1 << 20                # a longer line is not a record
_INT_FIELDS = ("seq", "volume_serial", "file_id", "size", "mtime_ns", "attrs")
_TEXT_FIELDS = ("kind", "state", "src", "dst", "sha256", "reason_code", "tmp")
_SHA256_RE = re.compile(r"(?:[0-9a-f]{64})?")


class JournalError(OSError):
    """A record could not be saved: the OS call it was to come before is not made."""


def run_id_ok(text: Any) -> bool:
    """A run id names a journal file: letters, digits, "_" and "-" only (never a path)."""
    return isinstance(text, str) and bool(_RUN_ID_RE.fullmatch(text))


@dataclass(frozen=True)
class Record:
    """One line of a run's journal: item ``seq`` going into ``state`` (see the module docstring)."""

    seq: int
    kind: str
    state: str
    src: str = field(default="", repr=False)
    dst: str = field(default="", repr=False)
    volume_serial: int = 0
    file_id: int = field(default=0, repr=False)
    size: int = 0
    mtime_ns: int = field(default=0, repr=False)
    attrs: int = 0
    sha256: str = field(default="", repr=False)
    reason_code: str = ""
    is_dir: bool = False
    undo: bool = False                                            # a step of putting the item back
    at: float = 0.0                                               # when (seconds since the epoch)
    tmp: str = field(default="", repr=False)                      # the temporary name a zip / folder copy is made under
    inputs: tuple[str, ...] = field(default=(), repr=False)      # a zip's inputs (never touched)
    flags: tuple[str, ...] = ()                                   # the card's row flags (fileops.FLAG_*)

    @property
    def identity(self) -> FileIdentity | None:
        """The identity this line records (None when it records none)."""
        if not self.volume_serial and not self.file_id:
            return None
        return FileIdentity(self.volume_serial, self.file_id, self.size, self.mtime_ns, self.is_dir)

    def with_identity(self, identity: FileIdentity | None, attrs: int | None = None) -> Record:
        if identity is None:
            return replace(self, volume_serial=0, file_id=0, size=0, mtime_ns=0,
                           attrs=self.attrs if attrs is None else attrs)
        return replace(self, volume_serial=identity.volume, file_id=identity.file_id, size=identity.size,
                       mtime_ns=identity.mtime_ns, is_dir=identity.is_dir,
                       attrs=self.attrs if attrs is None else attrs)

    def to_line(self) -> bytes:
        value = {"seq": self.seq, "kind": self.kind, "state": self.state, "src": self.src, "dst": self.dst,
                 "volume_serial": self.volume_serial, "file_id": self.file_id, "size": self.size,
                 "mtime_ns": self.mtime_ns, "attrs": self.attrs, "sha256": self.sha256,
                 "reason_code": self.reason_code, "is_dir": self.is_dir, "undo": self.undo, "at": self.at,
                 "tmp": self.tmp, "inputs": list(self.inputs), "flags": list(self.flags)}
        return (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")

    @classmethod
    def from_json(cls, value: Any) -> Record | None:
        """A record read back; None for anything that is not one (a torn line, an unknown kind or state ...)."""
        if not isinstance(value, dict):
            return None
        for name in _INT_FIELDS:
            item = value.get(name, 0)
            if not isinstance(item, int) or isinstance(item, bool) or item < 0:
                return None
        for name in _TEXT_FIELDS:
            if not isinstance(value.get(name, ""), str):
                return None
        inputs, flags = value.get("inputs", []), value.get("flags", [])
        at = value.get("at", 0.0)
        if not isinstance(inputs, list) or not all(isinstance(item, str) for item in inputs) \
                or not isinstance(flags, list) or not all(isinstance(item, str) for item in flags) \
                or not isinstance(at, (int, float)) or isinstance(at, bool) \
                or not isinstance(value.get("is_dir", False), bool) or not isinstance(value.get("undo", False), bool):
            return None
        if value.get("kind") not in KINDS or value.get("state") not in STATES or value.get("seq", 0) < 1 \
                or not _SHA256_RE.fullmatch(value.get("sha256", "")):
            return None
        return cls(value["seq"], value["kind"], value["state"], value.get("src", ""), value.get("dst", ""),
                   value.get("volume_serial", 0), value.get("file_id", 0), value.get("size", 0),
                   value.get("mtime_ns", 0), value.get("attrs", 0), value.get("sha256", ""),
                   value.get("reason_code", ""), value.get("is_dir", False), value.get("undo", False), float(at),
                   value.get("tmp", ""), tuple(inputs), tuple(flags))


@dataclass(frozen=True)
class Item:
    """One item of a run (one row of its card): its records in the order they were saved."""

    seq: int
    kind: str
    records: tuple[Record, ...] = field(repr=False)

    @property
    def first(self) -> Record:
        return self.records[0]

    @property
    def last(self) -> Record:
        return self.records[-1]

    @property
    def state(self) -> str:
        return self.last.state

    @property
    def reason_code(self) -> str:
        return self.last.reason_code

    @property
    def undoing(self) -> bool:
        """Putting it back is in flight (or was, when a crash came)."""
        return self.last.state == DOING and self.last.undo

    @property
    def done(self) -> Record | None:
        """Its latest "done" line: where it is now and the result's identity (a copy's or a zip's hash)."""
        return next((record for record in reversed(self.records) if record.state == DONE), None)

    @property
    def doing(self) -> Record | None:
        """Its latest "doing" line of the operation itself (not of an Undo)."""
        return next((record for record in reversed(self.records) if record.state == DOING and not record.undo), None)


def items(records: Iterable[Record]) -> dict[int, Item]:
    """The run's items by ``seq`` (a line whose kind differs from its item's first line is ignored)."""
    grouped: dict[int, list[Record]] = {}
    for record in records:
        lines = grouped.setdefault(record.seq, [])
        if lines and lines[0].kind != record.kind:
            continue
        lines.append(record)
    return {seq: Item(seq, lines[0].kind, tuple(lines)) for seq, lines in sorted(grouped.items())}


class Journal:
    """One run's journal file, ``<folder>\\<run_id>.jsonl``. ``clock`` gives a record's ``at`` (seconds since the
    epoch) when it has none."""

    def __init__(self, folder: str | Path, run_id: str, *, clock: Callable[[], float] = time.time) -> None:
        if not run_id_ok(run_id):
            raise ValueError("a run id is letters, digits, '_' and '-' only")
        self.folder = Path(folder)
        self.run_id = run_id
        self.path = self.folder / (run_id + JOURNAL_SUFFIX)
        self._clock = clock

    def __repr__(self) -> str:                      # the folder is the owner's; never in a log
        return f"Journal(run_id={self.run_id!r})"

    def append(self, record: Record) -> Record:
        """Save one record (written, flushed and on disk - FlushFileBuffers - before this returns). JournalError when
        it cannot be saved."""
        return self.extend([record])[0]

    def extend(self, records: Sequence[Record]) -> list[Record]:
        """Save several records at once (one flush to disk). JournalError when they cannot be saved."""
        stamped = [record if record.at else replace(record, at=float(self._clock())) for record in records]
        if not stamped:
            return []
        data = b"".join(record.to_line() for record in stamped)
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a+b") as handle:
                if handle.seek(0, os.SEEK_END) > 0:
                    handle.seek(-1, os.SEEK_END)
                    if handle.read(1) != b"\n":
                        data = b"\n" + data                 # a line torn by a crash ends here; ours start afresh
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())                   # FlushFileBuffers: on the disk before the OS call
        except OSError as exc:
            logger.warning("Desktop journal %s: a record was not saved (%s %s)", self.run_id, type(exc).__name__,
                           getattr(exc, "winerror", "") or "")
            raise JournalError(getattr(exc, "errno", 0) or 0, "the journal could not be saved") from None
        return stamped

    def read(self) -> tuple[list[Record], int]:
        """The records saved so far, in order, and how many lines could not be read (a line a crash tore, ...)."""
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return [], 0
        records: list[Record] = []
        bad = 0
        for line in data.split(b"\n"):
            if not line.strip():
                continue
            record = None
            if len(line) <= _MAX_LINE:
                try:
                    record = Record.from_json(json.loads(line.decode("ascii")))
                except (UnicodeDecodeError, ValueError, RecursionError):
                    record = None
            if record is None:
                bad += 1
            else:
                records.append(record)
        return records, bad

    def records(self) -> list[Record]:
        return self.read()[0]

    def items(self) -> dict[int, Item]:
        return items(self.records())

    def next_seq(self) -> int:
        return max((record.seq for record in self.records()), default=0) + 1


def default_folder() -> Path:
    """The app's journal folder, %LOCALAPPDATA%\\briefing-reader\\desktop\\journal (local: never OneDrive). Never while
    the tests run (backend.RealBackendInTests): a test passes its own temporary folder."""
    if real_backend_blocked():
        raise RealBackendInTests("the real journal folder is never used while the tests run")
    from ..config import default_data_dir         # late: config imports the desktop backend

    return default_data_dir() / "desktop" / "journal"


def journal_files(folder: str | Path) -> list[Path]:
    """The run journals right in ``folder`` (plain files named ``<run_id>.jsonl``; links and anything else are left
    alone), oldest name first."""
    found: list[Path] = []
    try:
        with os.scandir(folder) as listing:
            for entry in listing:
                name = entry.name
                if not name.endswith(JOURNAL_SUFFIX) or not run_id_ok(name[:-len(JOURNAL_SUFFIX)]):
                    continue
                try:
                    if entry.is_symlink() or entry.is_junction() or not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                found.append(Path(entry.path))
    except OSError:
        return []
    return sorted(found, key=lambda path: path.name)


# --------------------------------------------------------------------------
# Start-up recovery
# --------------------------------------------------------------------------

@dataclass
class RecoveryReport:
    runs: int = 0                                               # journals looked at
    corrected: dict[str, int] = field(default_factory=dict)     # the state each "doing" item was corrected to
    unreadable: int = 0                                         # lines that could not be read

    @property
    def items(self) -> int:
        return sum(self.corrected.values())


def recover(folder: str | Path, backend: DesktopBackend, *, clock: Callable[[], float] = time.time) -> RecoveryReport:
    """Correct every item a crash left "doing", in every journal in ``folder``, by looking where it is (see the module
    docstring). Nothing is ever redone, moved or removed here; only records are added."""
    report = RecoveryReport()
    for path in journal_files(folder):
        journal = Journal(folder, path.name[:-len(JOURNAL_SUFFIX)], clock=clock)
        records, bad = journal.read()
        report.runs += 1
        report.unreadable += bad
        for item in items(records).values():
            if item.state != DOING:
                continue
            try:
                fixed = _corrected(item, backend)
            except OSError as exc:                    # a look that failed: it is checked again at the next start
                logger.info("Desktop journal %s: item %d not checked (%s %s)", journal.run_id, item.seq,
                            type(exc).__name__, getattr(exc, "winerror", "") or "")
                continue
            try:
                journal.append(fixed)
            except JournalError:
                continue
            report.corrected[fixed.state] = report.corrected.get(fixed.state, 0) + 1
    if report.items or report.unreadable:
        logger.info("Desktop journal: recovered %d item(s) in %d run(s) (%s), %d unreadable line(s)", report.items,
                    report.runs, ", ".join(f"{state}={n}" for state, n in sorted(report.corrected.items())) or "-",
                    report.unreadable)
    return report


def _plain_on_fixed(path: str, backend: DesktopBackend) -> bool:
    """A plain absolute local path on a fixed drive (asked before anything on it is touched: a network drive's letter
    is never looked at)."""
    from .paths import lexical_problem             # late: paths imports the backend, as this module does

    return not lexical_problem(path) and backend.drive_type(path.replace("/", "\\")[:3]) == DRIVE_FIXED


def _identity_at(path: str, backend: DesktopBackend) -> FileIdentity | None:
    try:
        identity = backend.file_id(path)
    except OSError:
        return None
    return identity if isinstance(identity, FileIdentity) else None


def _same_id(identity: FileIdentity | None, record: Record) -> bool:
    return identity is not None and (identity.volume, identity.file_id) == (record.volume_serial, record.file_id)


def _corrected(item: Item, backend: DesktopBackend) -> Record:
    """The record that corrects ``item`` (left "doing"); its time is the "doing" line's, when it was done."""
    last = item.last
    paths = [path for path in (last.src, last.dst, last.tmp) if path]
    if not paths or not all(_plain_on_fixed(path, backend) for path in paths):
        return replace(last, state=UNDO_REFUSED if last.undo else FAILED, reason_code=R_UNCHECKED)
    if last.undo:
        return _corrected_undo(item, backend)
    kind = item.kind
    if kind in (MOVE, RENAME):
        here = _identity_at(last.dst, backend)
        if _same_id(here, last):
            return replace(last, state=DONE, reason_code=R_RECOVERED).with_identity(here)
        if last.src and _same_id(_identity_at(last.src, backend), last):
            return replace(last, state=FAILED, reason_code=R_INTERRUPTED)
        return replace(last, state=FAILED, reason_code=R_LOST)
    if kind == MKDIR:
        made = _identity_at(last.dst, backend)
        if made is not None and made.is_dir:
            return replace(last, state=DONE, reason_code=R_RECOVERED).with_identity(made)
        return replace(last, state=FAILED, reason_code=R_INTERRUPTED)
    if kind == COPY and not last.is_dir:
        return _corrected_file_copy(last, backend)
    if kind in (COPY, ZIP, SCREENSHOT):
        # A zip or a folder copy is written under a temporary name; once it is whole its file id and hash are saved
        # (a "doing" line with a hash), then it is renamed into place. Only that id is ever taken for it.
        if last.sha256 and last.identity is not None:
            here = _identity_at(last.dst, backend)
            if _same_id(here, last):
                return replace(last, state=DONE, reason_code=R_RECOVERED).with_identity(here)
            if last.tmp and _same_id(_identity_at(last.tmp, backend), last):
                return replace(last, state=FAILED, reason_code=R_PARTIAL)
            return replace(last, state=FAILED, reason_code=R_LOST)
        if last.tmp and backend.exists(last.tmp):
            return replace(last, state=FAILED, reason_code=R_PARTIAL)
        return replace(last, state=FAILED, reason_code=R_INTERRUPTED)
    return replace(last, state=FAILED, reason_code=R_UNCHECKED)       # recycling: P2.6 checks its own


def _corrected_file_copy(last: Record, backend: DesktopBackend) -> Record:
    """A file copy (CopyFileExW straight to its name): "done" only when the output's content is the source's and the
    source is unchanged since; an output that is there but not the same is left for the owner (partial)."""
    made = _identity_at(last.dst, backend)
    if made is None:
        return replace(last, state=FAILED, reason_code=R_INTERRUPTED)
    source = _identity_at(last.src, backend)
    recorded = last.identity
    if source is None or recorded is None or not recorded.same_item(source):
        return replace(last, state=FAILED, reason_code=R_PARTIAL)
    digest = backend.content_hash(last.dst)
    if digest != backend.content_hash(last.src):
        return replace(last, state=FAILED, reason_code=R_PARTIAL)
    return replace(last, state=DONE, reason_code=R_RECOVERED, sha256=digest).with_identity(made)


def _corrected_undo(item: Item, backend: DesktopBackend) -> Record:
    last = item.last
    done = item.done or last
    if item.kind in (MOVE, RENAME):
        if last.src and _same_id(_identity_at(last.src, backend), done):
            return replace(last, state=UNDONE, reason_code=R_RECOVERED, undo=True).with_identity(
                _identity_at(last.src, backend))
        if _same_id(_identity_at(last.dst, backend), done):
            return replace(last, state=UNDO_REFUSED, reason_code=R_INTERRUPTED)
        return replace(last, state=UNDO_REFUSED, reason_code=R_LOST)
    if item.kind in (COPY, ZIP, SCREENSHOT, MKDIR):
        if _same_id(_identity_at(last.dst, backend), done):
            return replace(last, state=UNDO_REFUSED, reason_code=R_INTERRUPTED)
        return replace(last, state=UNDONE, reason_code=R_RECOVERED)   # Jarvis's own output is gone from its place
    return replace(last, state=UNDO_REFUSED, reason_code=R_UNCHECKED)


# --------------------------------------------------------------------------
# Purging old runs
# --------------------------------------------------------------------------

def _last_activity(path: Path) -> float:
    """When a run's journal last changed: its newest record's time, else the file's own."""
    folder, name = path.parent, path.name[:-len(JOURNAL_SUFFIX)]
    newest = max((record.at for record in Journal(folder, name).records()), default=0.0)
    return newest or path.stat().st_mtime


def purge(folder: str | Path, *, now: float | None = None, days: int = PURGE_DAYS) -> int:
    """Remove the run journals in ``folder`` whose last record is more than ``days`` old (Jarvis's own records; the
    owner's files are never touched). The number removed."""
    cutoff = (time.time() if now is None else now) - max(1, int(days)) * _DAY_S
    removed = 0
    for path in journal_files(folder):
        try:
            if _last_activity(path) >= cutoff:
                continue
            path.unlink()
            removed += 1
        except OSError as exc:
            logger.info("Desktop journal: an old run was not purged (%s %s)", type(exc).__name__,
                        getattr(exc, "winerror", "") or "")
    if removed:
        logger.info("Desktop journal: purged %d run(s) older than %d days", removed, days)
    return removed


def same_place(first: str, second: str) -> bool:
    """Two paths name the same place (Windows compares names without case)."""
    return ntpath.normcase(first).rstrip("\\") == ntpath.normcase(second).rstrip("\\")
