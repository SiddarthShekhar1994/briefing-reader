"""Jarvis's memory: short notes the owner keeps on this PC (``%LOCALAPPDATA%\\briefing-reader\\memory.json``).

    MemoryStore(path, max_entries=100, read_only=False)
        entries()                    the notes, newest first (MemoryEntry)
        add(text, source=)           a new note, or the REFUSE_* word why not (nothing saved)
        edit(entry_id, text)         a changed note, or a REFUSE_* word
        delete(entry_id) / restore(entry)   remove a note (returned for Undo) / put it back
        prompt_notes()               (the notes the planner gets, newest first, at most 4 KB; how many
                                     were left out)
        match(words)                 the notes "forget that <words>" means
        duplicate_of(text, besides=) a kept note (not the one being edited) that says the same
                                     (casefold, punctuation removed, or >= 80 % of the words)
        dismiss(text) / dismissed()  a dismissed suggestion's word hashes (not its sentence; a single
                                     common word or name can be guessed from its unsalted hash)
        refresh()                    take memory.json again if it changed on disk (edited or deleted
                                     by hand while Jarvis runs)
    clean_note(text)               one line: control and format characters removed, spaces collapsed
    memory_refusal(text)             "" or why a note is not kept (empty, long, secret, digits)
    check_suggestion(text, index=, command=, titles=, dismissed=)
                                     "" or why a planner's suggestion is not shown (8 to 160
                                     characters, no link, address, long number or secret, every
                                     capitalized word grounded in the run's names, titles or the
                                     owner's words, not dismissed before)
    note_leak(text, notes, owner_words)   a draft repeats a note (a whole note of >= 12 characters
                                     or 5 words in a row) the owner did not ask for
    note_spoken(text, notes)         what Jarvis would say aloud repeats a note (also its words in
                                     any order): a counts-only sentence is said instead

The file holds only the owner's notes (typed, or suggestions he clicked to keep) and the token
hashes of dismissed suggestions: ``{"version": 1, "entries": [{"id", "text", "source", "created",
"changed"}], "dismissed": [["<8 hex>", ...], ...]}``, ASCII-escaped JSON with LF line ends
(read also with CRLF or a UTF-8 BOM, as an editor may save it), written under a lock through a
temporary file, fsync and ``os.replace``. The file is read again before every change (and on
``refresh``): what the owner changed by hand is taken, and a file he deleted forgets every note and
mark (nothing he removed is written back). A missing file is empty.
An unreadable, corrupt (also nested too deep), wrong-shaped or over 1 MB file makes the store
read-only for this run, with no notes (also when it turns so while Jarvis runs): it is never
overwritten, renamed or deleted (logged without its content).
A failed write keeps the change in memory for this run (``saved`` False).

Nothing here logs a note, a suggestion or a hash: counts, kinds and reason words only. Qt-free;
standard library only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:   # typing only: the index of a planner run (ask.context)
    from .ask.context import ContextIndex

logger = logging.getLogger(__name__)

MEMORY_FILE = "memory.json"
FILE_VERSION = 1
TEXT_CAP = 200
TEXT_MIN = 3
PROMPT_CAP = 4_000                   # characters of notes in <memory>, newest first
SOURCE_TOLD, SOURCE_SUGGESTED = "told", "suggested"
SOURCES = (SOURCE_TOLD, SOURCE_SUGGESTED)
SUGGESTION_CAP = 160
SUGGESTION_MIN = 8
MAX_PENDING_SUGGESTIONS = 5
MAX_DISMISSED = 50
DEFAULT_MAX_ENTRIES = 100
MAX_FILE_BYTES = 1_000_000           # 200 notes take well under 300 KB: a larger file is not one Jarvis wrote
OVERLAP = 0.8                        # this share of the words in common: the same note
LEAK_WORDS = 5                       # a draft with this many words of a note in a row repeats it
LEAK_CHARS = 12                      # ... or with a whole note at least this long
ECHO_SHARE = 0.75                    # speech: this share of a note's own words, in any order, repeats it
REFUSE_EMPTY, REFUSE_LONG, REFUSE_SECRET, REFUSE_DIGITS, REFUSE_FULL, REFUSE_READ_ONLY, REFUSE_DUPLICATE = (
    "empty", "long", "secret", "digits", "full", "read_only", "duplicate")
REFUSE_GONE = "gone"                 # edit: the note is no longer there
STATE_OK, STATE_MISSING, STATE_UNREADABLE = "ok", "missing", "unreadable"

# Narrower than ask.mail's sensitive words on purpose: "Ben is my key contact" or "code review on
# Fridays" are kept; a password, a PIN or an account number is not.
_SECRET_RE = re.compile(
    r"\b(?:passwords?|passcodes?|passwd|pin(?:\s+(?:number|code))?s?|2fa|otp|one[- ]time\s+(?:code|password)s?|"
    r"recovery\s+codes?|api\s?keys?|private\s+keys?|seed\s+phrase|credit\s+cards?|card\s+numbers?|cvv|ssn|"
    r"social\s+security|iban|routing\s+numbers?|account\s+numbers?)\b", re.IGNORECASE)
_LONG_DIGITS_RE = re.compile(r"\d(?:[ -]*\d){11,}")          # 12 or more digits, spaces and dashes ignored
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_SUGGESTION_DIGITS_RE = re.compile(r"\d{6,}")
_URL_RE = re.compile(r"https?|://|www\.", re.IGNORECASE)     # as ask.mail's
_CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_CF_RE = re.compile("[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb]")
_TOKEN_RE = re.compile(r"[^\W_]+")
_WORD_RE = re.compile(r"[^\W\d_]+(?:['\u2019-][^\W\d_]+)*")
_ID_RE = re.compile(r"m[0-9a-f]{11}")
_HASH_RE = re.compile(r"[0-9a-f]{8}")
# Capitalized words a suggestion may use without a source: the calendar's words for days and months.
_FREE_CAPITALS = frozenset(
    "i jarvis am pm monday tuesday wednesday thursday friday saturday sunday mondays tuesdays wednesdays thursdays "
    "fridays saturdays sundays mon tue tues wed thu thur thurs fri sat sun january february march april may june "
    "july august september october november december jan feb mar apr jun jul aug sep sept oct nov dec".split())
# Words that say nothing of a note on their own (note_spoken leaves them out of the note's words).
_COMMON_WORDS = frozenset(
    "the and for with that this are was were has have had his her hers its our ours you your yours their them they "
    "not but from who what when where which will can".split())


@dataclass(frozen=True)
class MemoryEntry:
    id: str          # "m" + 11 random hex
    text: str        # one line, cleaned, TEXT_MIN..TEXT_CAP
    source: str      # SOURCE_TOLD | SOURCE_SUGGESTED
    created: str     # ISO 8601 with offset, seconds
    changed: str = ""

    def __repr__(self) -> str:   # never the text
        return f"MemoryEntry({self.id}, {self.source}, {len(self.text)} chars)"


# --------------------------------------------------------------------------
# Text rules (pure)
# --------------------------------------------------------------------------

def clean_note(text: Any) -> str:
    """One line: control and invisible format characters removed, spaces collapsed."""
    if not isinstance(text, str):
        return ""
    return " ".join(_CONTROL_RE.sub(" ", _CF_RE.sub("", text)).split())


def memory_refusal(text: Any) -> str:
    """"" when ``text`` may be kept as a note, else REFUSE_EMPTY / LONG / SECRET / DIGITS."""
    note = clean_note(text)
    if len(note) < TEXT_MIN:
        return REFUSE_EMPTY
    if len(note) > TEXT_CAP:
        return REFUSE_LONG
    if _SECRET_RE.search(note):
        return REFUSE_SECRET
    if _LONG_DIGITS_RE.search(note) or _SSN_RE.search(note):
        return REFUSE_DIGITS
    return ""


def _tokens(text: str) -> list[str]:
    """Casefolded, punctuation-free tokens of at least 3 characters."""
    return [token for token in _TOKEN_RE.findall(text.casefold()) if len(token) >= 3]


def _plain(text: str) -> str:
    """Casefolded, punctuation removed, spaces collapsed (the duplicate rule's equality)."""
    return " ".join(_TOKEN_RE.findall(text.casefold()))


def fingerprint(text: str) -> frozenset[str]:
    """sha256(token)[:8] of each casefolded, punctuation-free token of >= 3 letters (what a dismissed
    suggestion leaves in the file: not its sentence, though a common word or a name can be guessed
    from its short unsalted hash)."""
    return frozenset(hashlib.sha256(token.encode("utf-8")).hexdigest()[:8] for token in _tokens(clean_note(text)))


def _overlap(first: frozenset[str] | set[str], second: frozenset[str] | set[str]) -> float:
    if not first or not second:
        return 0.0
    return len(first & second) / max(len(first), len(second))


def _same_note(first: str, second: str) -> bool:
    """The duplicate rule: equal after casefold and punctuation removal, or >= 80 % of the words."""
    if _plain(first) == _plain(second):
        return True
    return _overlap(set(_tokens(first)), set(_tokens(second))) >= OVERLAP


def check_suggestion(text: Any, *, index: ContextIndex | None, command: str, titles: Sequence[str],
                     dismissed: Sequence[frozenset[str]] = ()) -> str:
    """"" when a planner's ``remember`` suggestion may be offered to the owner, else the reason word:
    length, url, address, digits, secret, ungrounded or dismissed. Grounded: every capitalized word
    of >= 3 letters but the first (days and months aside) is a word of a name the calendar or the
    briefing gave (``index.names``), of an event title shown in the run, or of the owner's command."""
    note = clean_note(text)
    if not SUGGESTION_MIN <= len(note) <= SUGGESTION_CAP:
        return "length"
    if _URL_RE.search(note):
        return "url"
    if "@" in note:
        return "address"
    if _SUGGESTION_DIGITS_RE.search(note):
        return "digits"
    if _SECRET_RE.search(note) or memory_refusal(note):
        return "secret"
    known: set[str] = set()
    names = getattr(index, "names", None) or {}
    for given in names.values():
        for name in given:
            known.update(word.casefold() for word in _WORD_RE.findall(name))
    for title in titles:
        known.update(word.casefold() for word in _WORD_RE.findall(title or ""))
    known.update(word.casefold() for word in _WORD_RE.findall(command or ""))
    words = _WORD_RE.findall(note)
    for word in words[1:]:
        base = re.split("['\u2019]", word)[0]      # "Ana's" -> "Ana", "I'm" -> "I"
        if len(base) < 3 or not base[0].isupper():
            continue
        for part in base.casefold().split("-"):
            if part and part not in _FREE_CAPITALS and part not in known:
                return "ungrounded"
    mark = fingerprint(note)
    if any(_overlap(mark, old) >= OVERLAP for old in dismissed):
        return "dismissed"
    return ""


def _words(text: str) -> list[str]:
    return _TOKEN_RE.findall(" ".join(text.casefold().split()))


def _runs(words: Sequence[str], size: int) -> set[tuple[str, ...]]:
    return {tuple(words[start:start + size]) for start in range(len(words) - size + 1)}


def note_leak(text: str, notes: Sequence[str], owner_words: str = "") -> bool:
    """``text`` (a mode's draft or block title) repeats a note: a whole note of >= 12 characters or 5
    of a note's words in a row, unless the owner's own words hold the same (he asked for it)."""
    flat = " ".join(str(text or "").casefold().split())
    owner_flat = " ".join(str(owner_words or "").casefold().split())
    text_runs = _runs(_words(flat), LEAK_WORDS)
    owner_runs = _runs(_words(owner_flat), LEAK_WORDS)
    for note in notes:
        whole = " ".join(str(note or "").casefold().split()).rstrip(".!?")
        if len(whole) >= LEAK_CHARS and whole in flat and whole not in owner_flat:
            return True
        if (_runs(_words(whole), LEAK_WORDS) & text_runs) - owner_runs:
            return True
    return False


def note_spoken(text: str, notes: Sequence[str]) -> bool:
    """``text`` (what Jarvis would say aloud) repeats a note: ``note_leak`` without the owner's
    words, or at least ECHO_SHARE of the note's own words (>= 3 letters, a few common ones aside)
    in any order ("Ana is your project lead" for the note "Ana is my project lead"). Stricter than
    ``note_leak`` on purpose: spoken text goes to an online read-aloud service, and the whole
    answer stays on screen. A reworded answer can still slip past it."""
    if note_leak(text, notes):
        return True
    said = set(_tokens(clean_note(text)))
    for note in notes:
        words = set(_tokens(clean_note(note))) - _COMMON_WORDS
        if words and len(words & said) / len(words) >= ECHO_SHARE:
            return True
    return False


# --------------------------------------------------------------------------
# The file
# --------------------------------------------------------------------------

class _Unreadable(Exception):
    """The file is there but not ours to change (corrupt, wrong shape, unreadable)."""


def _entry_from(item: Any) -> MemoryEntry:
    if not isinstance(item, dict):
        raise _Unreadable("entry")
    entry_id, text, source = item.get("id"), item.get("text"), item.get("source")
    created, changed = item.get("created"), item.get("changed", "")
    if not (isinstance(entry_id, str) and _ID_RE.fullmatch(entry_id) and isinstance(text, str)
            and source in SOURCES and isinstance(created, str) and isinstance(changed, str)):
        raise _Unreadable("entry")
    note = clean_note(text)
    if not TEXT_MIN <= len(note) <= TEXT_CAP:
        raise _Unreadable("entry")
    return MemoryEntry(entry_id, note, source, created[:40], changed[:40])


def _parse(raw: bytes) -> tuple[list[MemoryEntry], list[frozenset[str]]]:
    if len(raw) > MAX_FILE_BYTES:
        raise _Unreadable("size")
    try:
        data = json.loads(raw.decode("utf-8-sig"))   # a BOM (Notepad, PowerShell 5.1) is fine; Jarvis writes none
    except (UnicodeDecodeError, ValueError, RecursionError, MemoryError) as exc:   # nested too deep: not ours
        raise _Unreadable("json") from exc
    if not isinstance(data, dict) or data.get("version") != FILE_VERSION or not isinstance(data.get("entries"), list):
        raise _Unreadable("shape")
    entries = [_entry_from(item) for item in data["entries"]]
    if len({entry.id for entry in entries}) != len(entries):
        raise _Unreadable("shape")
    dismissed: list[frozenset[str]] = []
    raw_dismissed = data.get("dismissed", [])
    if not isinstance(raw_dismissed, list):
        raise _Unreadable("shape")
    for item in raw_dismissed[:MAX_DISMISSED]:
        if not isinstance(item, list) or not all(isinstance(mark, str) and _HASH_RE.fullmatch(mark) for mark in item):
            raise _Unreadable("shape")
        dismissed.append(frozenset(item))
    return entries, dismissed


def _read_raw(path: Path) -> bytes | None:
    """The file's bytes (at most one past MAX_FILE_BYTES: _parse refuses a larger file); None when
    there is no file."""
    try:
        with open(path, "rb") as fh:
            return fh.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _Unreadable(type(exc).__name__) from exc


def _serialize(entries: Sequence[MemoryEntry], dismissed: Sequence[frozenset[str]]) -> str:
    data: dict[str, Any] = {"version": FILE_VERSION,
                            "entries": [{"id": entry.id, "text": entry.text, "source": entry.source,
                                         "created": entry.created, "changed": entry.changed} for entry in entries]}
    if dismissed:
        data["dismissed"] = [sorted(marks) for marks in dismissed[:MAX_DISMISSED]]
    return json.dumps(data, ensure_ascii=True, indent=1) + "\n"


def _write_atomic(path: Path, text: str) -> bool:
    part: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, part = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(part, path)
        part = None
        return True
    except OSError as exc:
        logger.warning("Memory: could not save memory.json (%s); kept for this run only",
                       exc.strerror or type(exc).__name__)
        return False
    finally:
        if part is not None:
            try:
                os.unlink(part)
            except OSError:
                pass


class MemoryStore:
    """The owner's notes (memory.json). Thread-safe; every change is written at once."""

    def __init__(self, path: Path, *, max_entries: int = DEFAULT_MAX_ENTRIES, read_only: bool = False,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self.max_entries = max(1, int(max_entries))
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._lock = threading.Lock()
        self.saved = True
        self._read_only = bool(read_only)
        self.state = STATE_MISSING
        self._entries: list[MemoryEntry] = []
        self._dismissed: list[frozenset[str]] = []
        self._disk: bytes | None = None   # the file as Jarvis last read or wrote it (None: no file)
        with self._lock:
            self._sync(first=True)

    # ---- reading --------------------------------------------------------------------------------

    @property
    def read_only(self) -> bool:
        """Nothing may change the file: opened read-only (the command line) or unreadable."""
        return self._read_only or self.state == STATE_UNREADABLE

    def entries(self) -> tuple[MemoryEntry, ...]:
        """The notes, newest first."""
        with self._lock:
            return tuple(self._entries)

    def entry(self, entry_id: str) -> MemoryEntry | None:
        with self._lock:
            return next((entry for entry in self._entries if entry.id == entry_id), None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def refresh(self) -> bool:
        """Take memory.json as it is now: the owner may have changed it by hand, or deleted it to
        forget everything, while Jarvis runs. True when the notes, the dismissed marks or the state
        changed (a file made unreadable meanwhile leaves no notes, makes the store read-only and is
        never written)."""
        with self._lock:
            before = (self.state, tuple(self._entries), tuple(self._dismissed))
            self._sync()
            return (self.state, tuple(self._entries), tuple(self._dismissed)) != before

    def prompt_notes(self) -> tuple[tuple[str, ...], int]:
        """(the notes' texts the planner gets, newest first, at most PROMPT_CAP characters in all;
        how many older ones were left out)."""
        with self._lock:
            texts = [entry.text for entry in self._entries]
        sent: list[str] = []
        used = 0
        for text in texts:
            if used + len(text) > PROMPT_CAP:
                break
            sent.append(text)
            used += len(text)
        return tuple(sent), len(texts) - len(sent)

    def match(self, words: str) -> list[MemoryEntry]:
        """The notes that hold every word of ``words`` of >= 3 letters (casefolded), newest first;
        [] when ``words`` has none."""
        wanted = set(_tokens(clean_note(words)))
        if not wanted:
            return []
        with self._lock:
            return [entry for entry in self._entries if wanted <= set(_tokens(entry.text))]

    def duplicate_of(self, text: str, *, besides: str = "") -> MemoryEntry | None:
        """A kept note (other than ``besides``) that says the same as ``text``."""
        note = clean_note(text)
        with self._lock:
            return next((entry for entry in self._entries if entry.id != besides and _same_note(note, entry.text)),
                        None)

    def dismissed(self) -> tuple[frozenset[str], ...]:
        """The token hashes of dismissed suggestions, newest first."""
        with self._lock:
            return tuple(self._dismissed)

    # ---- changes --------------------------------------------------------------------------------

    def add(self, text: str, *, source: str) -> MemoryEntry | str:
        """A new note (newest), or the REFUSE_* word why not (nothing changed)."""
        note = clean_note(text)
        refusal = memory_refusal(note)
        if refusal:
            return refusal
        if self.read_only:
            return REFUSE_READ_ONLY
        with self._lock:
            if not self._can_write():
                return REFUSE_READ_ONLY
            if len(self._entries) >= self.max_entries:
                return REFUSE_FULL
            if any(_same_note(note, entry.text) for entry in self._entries):
                return REFUSE_DUPLICATE
            entry = MemoryEntry(self._new_id(), note, source if source in SOURCES else SOURCE_TOLD, self._stamp())
            self._entries.insert(0, entry)
            self._save()
        return entry

    def edit(self, entry_id: str, text: str) -> MemoryEntry | str:
        """The note with its new text, or a REFUSE_* word (REFUSE_GONE: no such note)."""
        note = clean_note(text)
        refusal = memory_refusal(note)
        if refusal:
            return refusal
        if self.read_only:
            return REFUSE_READ_ONLY
        with self._lock:
            if not self._can_write():
                return REFUSE_READ_ONLY
            position = next((number for number, entry in enumerate(self._entries) if entry.id == entry_id), None)
            if position is None:
                return REFUSE_GONE
            old = self._entries[position]
            if note == old.text:
                return old
            if any(entry.id != entry_id and _same_note(note, entry.text) for entry in self._entries):
                return REFUSE_DUPLICATE
            changed = replace(old, text=note, changed=self._stamp())
            self._entries[position] = changed
            self._save()
        return changed

    def delete(self, entry_id: str) -> MemoryEntry | None:
        """Remove a note; it is returned for Undo (None: no such note, or read-only)."""
        if self.read_only:
            return None
        with self._lock:
            if not self._can_write():
                return None
            position = next((number for number, entry in enumerate(self._entries) if entry.id == entry_id), None)
            if position is None:
                return None
            entry = self._entries.pop(position)
            self._save()
        return entry

    def position(self, entry_id: str) -> int | None:
        """Where a note is in entries() (0: the newest), None when it is not there."""
        with self._lock:
            return next((number for number, entry in enumerate(self._entries) if entry.id == entry_id), None)

    def restore(self, entry: MemoryEntry, *, at: int | None = None) -> bool:
        """Undo of a delete: the same note (same id) back in its place (``at``: where it was, else by
        the time it was made). False when it is there already, a kept note says the same, the store
        is full or read-only."""
        if self.read_only or not isinstance(entry, MemoryEntry):
            return False
        with self._lock:
            if not self._can_write():
                return False
            if any(kept.id == entry.id or _same_note(entry.text, kept.text) for kept in self._entries):
                return False
            if len(self._entries) >= self.max_entries:
                return False
            if at is None:
                made = _created_at(entry)
                at = next((number for number, kept in enumerate(self._entries) if _created_at(kept) <= made),
                          len(self._entries))
            self._entries.insert(max(0, min(at, len(self._entries))), entry)
            self._save()
        return True

    def dismiss(self, text: str) -> bool:
        """Remember a dismissed suggestion as its word hashes (not its sentence); False when that
        could not be saved (read-only: kept for this run only)."""
        mark = fingerprint(text)
        if not mark:
            return False
        with self._lock:
            writable = not self.read_only and self._can_write()
            self._dismissed = [mark, *[old for old in self._dismissed if old != mark]][:MAX_DISMISSED]
            if not writable:
                return False
            return self._save()

    # ---- inside the lock --------------------------------------------------------------------------

    def _new_id(self) -> str:
        known = {entry.id for entry in self._entries}
        while True:
            entry_id = "m" + secrets.token_hex(6)[:11]
            if entry_id not in known:
                return entry_id

    def _stamp(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def _can_write(self) -> bool:
        """Read the file again right before a change, and take what it holds now: it is ours to
        replace only while it is missing or one this store can read (a file made unreadable
        meanwhile is never overwritten)."""
        return self._sync()

    def _sync(self, *, first: bool = False) -> bool:
        """memory.json as it is now (False: it can't be read; read-only from now on). A file Jarvis
        did not write last (changed by hand) replaces the notes and dismissed marks kept here; a
        file deleted since Jarvis read or wrote it forgets them all. Unchanged bytes (or still no
        file, e.g. after a failed write) keep what is here. ``first``: the store's first read."""
        if self.state == STATE_UNREADABLE:
            return False
        try:
            raw = _read_raw(self.path)
            if raw == self._disk:
                return True
            entries, dismissed = _parse(raw) if raw is not None else ([], [])
        except _Unreadable as exc:
            # As at a start: no notes (what the owner meant the file to hold is not known), read-only,
            # the file never written; dismissed marks stay for this run.
            logger.warning("Memory: could not read memory.json (%s); kept read-only", exc)
            self.state = STATE_UNREADABLE
            self._entries = []
            return False
        if not first:
            logger.info("Memory: memory.json changed outside Jarvis (%s); %d note(s) taken from it",
                        "deleted" if raw is None else "edited", len(entries))
        self._entries, self._dismissed = _newest_first(entries), list(dismissed)
        self.state = STATE_OK if raw is not None else STATE_MISSING
        self._disk = raw
        self.saved = True   # what is kept here is the file again (an earlier unsaved change gave way to it)
        return True

    def _save(self) -> bool:
        text = _serialize(self._entries, self._dismissed)
        self.saved = _write_atomic(self.path, text)
        if self.saved:
            self.state = STATE_OK
            self._disk = text.encode("ascii")
        return self.saved


def _created_at(entry: MemoryEntry) -> float:
    try:
        moment = datetime.fromisoformat(entry.created)
        return (moment if moment.tzinfo is not None else moment.astimezone()).timestamp()
    except (ValueError, OverflowError, OSError):
        return 0.0


def _newest_first(entries: Sequence[MemoryEntry]) -> list[MemoryEntry]:
    """Newest first by the time each note was made (the file keeps that order too); notes made in
    the same second keep their order."""
    return sorted(entries, key=_created_at, reverse=True)
