"""Jarvis's own search of this PC (SPEC sections 4.1, 5 and 13: index.py; no Qt).

It looks only inside the approved roots (paths.load_scope's Scope), and nothing it finds is listed before the path
check passes (paths.check_path: inside a root, no link anywhere, a fixed local drive, no excluded place, no code
folder, no secret file): excluded places, secret files and code folders are never listed, whatever the index or a
shortcut says. Every listed file and place carries its final path and its identity (backend.file_id, SPEC 5.3),
recorded as it is listed: desktop.executor compares it right before each OS call, so a target that changed between
the listing and the open is refused.

* The Windows Search index first: ``build_query`` writes one SQL query against SystemIndex - SCOPE'd to the roots
  (or to the places the owner's words named), the words matched in the file name, newest first, at most ``top``
  rows - with every quote doubled and LIKE's own wildcards taken literally (pure: the tests read its text);
  ``LocalSearch`` runs it through the backend within ``SEARCH_TIMEOUT_S``.
* When the index cannot answer (the service is off, the query fails) or finds nothing, a bounded walk of the same
  folders (``walk``): at most ``WALK.entries`` (50,000) entries, ``WALK.depth`` (8) levels below a folder,
  ``WALK.seconds`` (2) seconds; it never goes into a link (a junction, a symbolic link, an app execution alias or any
  reparse point that is not OneDrive's), a hidden folder, an excluded or secret folder name, a folder named like a
  code marker or one below a root that holds a code marker, a name ending in a shell class id, and never into an
  online-only (OneDrive) folder: nothing is fetched.
* The Recent items (``recent_files``): the targets of the newest shortcuts in Recent (read by the backend with
  IShellLink, never resolved); the shortcuts themselves are never listed, and each target passes the path check.
* The places (``folder_tree`` + ``pick_places``): the roots and their folders down to context.MAX_PLACE_DEPTH (3)
  levels, the ones the words name and the ones holding a listed file first, for <places>.

``query_words(text)`` are the words a request is searched by (casefolded, without the request's own verbs and
fillers). Logs carry counts and exception types only, never a word, a name or a path.

Public API
==========
``SEARCH_TIMEOUT_S``, ``INDEX_ROWS``, ``MAX_RESULTS``, ``STOP_WORDS``; ``query_words(text)``; ``sql_literal(text)``,
``like_pattern(word)``, ``scope_url(path)``, ``build_query(words, folders, kind=, within_days=, now=, top=)``;
``WalkCaps(entries, depth, seconds)``, ``WALK``, ``PLACE_WALK``; ``WalkResult``; ``walk(folders, backend, accept,
caps=, clock=, max_matches=)``; ``skipped(entry)``; ``SearchResult(files, looked_in, found, source, refused)``;
``LocalSearch(scope, backend, clock=, now=, cache=)`` (``search(words, folders=, kind=, within_days=, limit=)``);
``recent_files(scope, backend, limit=, cache=)``; ``TreeFolder(path, parts, mtime_ns)``; ``folder_tree(scope,
backend, caps=, clock=)``; ``pick_places(scope, backend, tree, words=, files=, cap=, cache=)``; ``file_item(verdict,
backend)``, ``place_item(verdict, parts, backend)``; ``name_score(name, words)``.
"""

from __future__ import annotations

import logging
import ntpath
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import guard, paths
from .backend import (FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_OFFLINE, FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
                      FILE_ATTRIBUTE_RECALL_ON_OPEN, FILE_ATTRIBUTE_REPARSE_POINT, INDEX_COLUMNS, DesktopBackend,
                      DirEntry, FileIdentity, cloud_tag)
from .context import KIND_TYPES, MAX_FILES, MAX_PLACE_DEPTH, MAX_PLACES, FileItem, Place, file_kind

logger = logging.getLogger(__name__)

SEARCH_TIMEOUT_S = 3.0                  # the index's answer, at most
INDEX_ROWS = 400                        # rows read from the index at most (the checks then keep MAX_RESULTS)
MAX_RESULTS = MAX_FILES                 # files a search lists at most
MAX_EXAMINED = 600                      # candidates the path check looks at, at most, per search
MAX_WORDS = 8                           # words a request is searched by, at most
RECENT_LINKS = 60                       # Recent items read at most
# The request's own verbs and fillers: never searched for in a name.
STOP_WORDS = frozenset({
    "a", "an", "the", "my", "me", "our", "your", "this", "that", "these", "those", "it", "its", "i", "you", "we",
    "jarvis", "please", "can", "could", "would", "will", "kindly", "just", "now", "hey", "hi", "ok", "okay",
    "open", "show", "find", "where", "is", "are", "was", "were", "whats", "what", "s", "locate", "look", "for",
    "bring", "pull", "up", "launch", "start", "view", "visit", "go", "to", "get", "see", "let", "have", "has", "had",
    "file", "files", "folder", "folders", "document", "documents", "doc", "docs", "thing", "things", "one", "ones",
    "latest", "last", "recent", "recently", "new", "newest", "of", "in", "on", "at", "from", "with", "and", "or",
    "about", "into", "onto", "under", "inside", "within", "called", "named", "did", "do", "i", "put", "save",
    "saved", "leave", "left", "some", "any", "all", "which", "there", "here", "app", "apps", "program"})


# --------------------------------------------------------------------------
# Words
# --------------------------------------------------------------------------

def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", guard.strip_hidden(text)).casefold()


def query_words(text: Any) -> tuple[str, ...]:
    """The words ``text`` is searched by: its words (guard.words, casefolded) without STOP_WORDS, each once, in order,
    at most MAX_WORDS."""
    kept: list[str] = []
    for word in guard.words(text, camel=False):
        if word in STOP_WORDS or word in kept:
            continue
        kept.append(word)
        if len(kept) >= MAX_WORDS:
            break
    return tuple(kept)


def name_score(name: Any, words: Sequence[str]) -> int:
    """How many of ``words`` a name holds (as the index's LIKE does: anywhere in it, case and hidden characters
    aside)."""
    if not isinstance(name, str):
        return 0
    folded = _fold(name)
    return sum(1 for word in words if word and _fold(word) in folded)


# --------------------------------------------------------------------------
# The query (pure: its text is what the tests read)
# --------------------------------------------------------------------------

def sql_literal(text: Any) -> str:
    """``text`` as a SQL string literal: in single quotes, every single quote doubled, control characters dropped."""
    value = "".join(char for char in str(text) if ord(char) >= 0x20 and not 0x7F <= ord(char) <= 0x9F)
    return "'" + value.replace("'", "''") + "'"


def like_pattern(word: str) -> str:
    """'%word%' for LIKE, its own wildcards in ``word`` taken literally: % _ [ become [%] [_] [[]."""
    return "%" + "".join("[" + char + "]" if char in "%_[" else char for char in word) + "%"


def scope_url(path: str) -> str:
    """The SCOPE of a folder: file:C:/Users/... (the index's own form; the folder and everything below it)."""
    return "file:" + path.replace("\\", "/").rstrip("/")


def build_query(words: Sequence[str], folders: Sequence[str], *, kind: str = "", within_days: int | None = None,
                now: datetime | None = None, top: int = INDEX_ROWS) -> str:
    """One Windows Search query: the INDEX_COLUMNS of the items under ``folders`` (SCOPE, each folder and what is
    below it - "" when there is no folder: nothing is asked) whose file name holds any of ``words``, of ``kind``'s
    types, changed within ``within_days`` days of ``now`` (UTC), the newest first, at most ``top`` rows."""
    scopes = [f"SCOPE={sql_literal(scope_url(folder))}" for folder in folders if isinstance(folder, str) and folder]
    if not scopes:
        return ""
    where = ["(" + " OR ".join(scopes) + ")"]
    names = [f"System.FileName LIKE {sql_literal(like_pattern(word))}" for word in words if word]
    if names:
        where.append("(" + " OR ".join(names) + ")")
    types = KIND_TYPES.get(kind)
    if types:
        where.append("(" + " OR ".join(f"System.FileExtension = {sql_literal('.' + ending)}"
                                       for ending in sorted(types)) + ")")
    if within_days:
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        since = moment - timedelta(days=int(within_days))
        where.append(f"System.DateModified >= {sql_literal(since.strftime('%Y-%m-%d %H:%M:%S'))}")
    return (f"SELECT TOP {max(1, int(top))} {', '.join(INDEX_COLUMNS)} FROM SystemIndex WHERE "
            + " AND ".join(where) + " ORDER BY System.DateModified DESC")


# --------------------------------------------------------------------------
# The walk (when the index can't answer)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class WalkCaps:
    entries: int = 50_000               # entries looked at, at most
    depth: int = 8                      # levels below a starting folder (its own entries are level 1)
    seconds: float = 2.0                # time, at most


WALK = WalkCaps()
PLACE_WALK = WalkCaps(entries=20_000, depth=MAX_PLACE_DEPTH, seconds=0.5)
_LISTED_LIMIT = 10_000
_RECALLED = FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_OFFLINE
_CODE_MARKERS = frozenset(marker.casefold() for marker in guard.CODE_MARKERS)


@dataclass
class WalkResult:
    """What a walk found (``matches``: (path, entry), in the order seen) and how far it went."""

    matches: list[tuple[str, DirEntry]] = field(default_factory=list, repr=False)
    entries: int = 0                    # entries looked at
    folders: int = 0                    # folders listed
    deepest: int = 0                    # the deepest level an entry was seen at
    stopped: str = ""                   # "" (all of it), "entries", "time" or "matches"


def skipped(entry: DirEntry) -> bool:
    """An entry the walk never lists or goes into: hidden (hidden+system too), a reparse point that is not OneDrive's
    (a junction, a symbolic link, an app execution alias ...), an excluded or secret folder name, a code marker's
    name, a name ending in a shell class id (paths.shell_class_name), a secret-shaped file name."""
    if entry.attributes & FILE_ATTRIBUTE_HIDDEN:
        return True
    if (entry.reparse_tag or entry.attributes & FILE_ATTRIBUTE_REPARSE_POINT) and not cloud_tag(entry.reparse_tag):
        return True
    name = entry.name if isinstance(entry.name, str) else ""
    folded = _fold(name).rstrip(". ")
    if not folded or folded in guard.EXCLUDED_NAMES or folded in guard.SECRET_DIR_NAMES or folded in _CODE_MARKERS:
        return True
    if paths.shell_class_name(name):
        return True                                       # "x.{GUID}": the shell's class, not a plain folder
    return not entry.is_dir and guard.secret_file(name)


def walk(folders: Sequence[str], backend: DesktopBackend, accept: Callable[[DirEntry], bool], *,
         caps: WalkCaps = WALK, clock: Callable[[], float] = time.monotonic, max_matches: int = 2000) -> WalkResult:
    """Breadth-first under ``folders`` (all of them level by level), within ``caps``: the entries ``accept`` takes
    (files and folders alike; ``skipped`` ones never). A folder holding a code marker below a starting folder is left
    out whole; an online-only folder is never gone into (OneDrive would fetch it)."""
    result = WalkResult()
    start = clock()
    queue: deque[tuple[str, int]] = deque((folder.rstrip("\\"), 0) for folder in folders if folder)
    while queue:
        if clock() - start >= caps.seconds:
            result.stopped = "time"
            break
        folder, level = queue.popleft()
        room = caps.entries - result.entries
        if room <= 0:
            result.stopped = "entries"
            break
        try:
            entries = backend.list_folder(folder, limit=min(_LISTED_LIMIT, room + 1))
        except OSError:
            continue
        result.folders += 1
        if level > 0 and any(_fold(entry.name) in _CODE_MARKERS for entry in entries
                             if isinstance(entry.name, str)):
            continue                                      # a code folder below where the walk began: none of it
        for entry in entries:
            if result.entries >= caps.entries:
                result.stopped = "entries"
                break
            result.entries += 1
            result.deepest = max(result.deepest, level + 1)
            if skipped(entry):
                continue
            path = folder + "\\" + entry.name
            if accept(entry):
                result.matches.append((path, entry))
                if len(result.matches) >= max_matches:
                    result.stopped = "matches"
                    break
            if entry.is_dir and level + 1 < caps.depth and not entry.attributes & _RECALLED:
                queue.append((path, level + 1))
        if result.stopped:
            break
    return result


# --------------------------------------------------------------------------
# Listing what was found (the path check, then the identity)
# --------------------------------------------------------------------------

def _moment(mtime_ns: int) -> datetime | None:
    try:
        return datetime.fromtimestamp(mtime_ns / 1e9)
    except (OverflowError, OSError, ValueError):
        return None


def file_item(verdict: paths.PathVerdict, backend: DesktopBackend) -> FileItem | None:
    """The FileItem of a path that passed the check (its final path, its own name, its identity read now); None for a
    folder or when it can't be read."""
    if not verdict.ok:
        return None
    try:
        identity = backend.file_id(verdict.path)
    except OSError:
        return None
    if not isinstance(identity, FileIdentity) or identity.is_dir:
        return None
    name = ntpath.basename(verdict.path.rstrip("\\"))
    return FileItem(verdict.path, name, size=identity.size, modified=_moment(identity.mtime_ns),
                    online_only=paths.FLAG_ONLINE_ONLY in verdict.flags, identity=identity)


def place_item(verdict: paths.PathVerdict, parts: Sequence[str], backend: DesktopBackend) -> Place | None:
    """The Place of a folder that passed the check (its final path, its identity read now); None for a file or when
    it can't be read."""
    if not verdict.ok:
        return None
    try:
        identity = backend.file_id(verdict.path)
    except OSError:
        return None
    if not isinstance(identity, FileIdentity) or not identity.is_dir:
        return None
    return Place(verdict.path, tuple(parts), identity)


@dataclass(frozen=True)
class SearchResult:
    """A search's files (checked, the best first), how many folders it looked in, how many matching items it came
    across, where they came from ("index", "walk" or "" for neither) and how many the checks refused."""

    files: tuple[FileItem, ...] = field(default=(), repr=False)
    looked_in: int = 0
    found: int = 0
    source: str = ""
    refused: int = 0

    def __repr__(self) -> str:
        return (f"SearchResult(files={len(self.files)}, looked_in={self.looked_in}, found={self.found}, "
                f"source={self.source!r}, refused={self.refused})")


@dataclass(frozen=True)
class _Candidate:
    path: str
    score: int
    changed: float                      # seconds since the epoch (0: unknown)


class LocalSearch:
    """One scope's search (see the module docstring). ``cache``: the path check's memory of which folders hold a code
    marker (shared across one request); ``now``: the wall clock (UTC) within_days counts back from."""

    def __init__(self, scope: paths.Scope, backend: DesktopBackend, *, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] | None = None, cache: dict[str, bool] | None = None,
                 walk_caps: WalkCaps = WALK) -> None:
        self.scope = scope
        self.backend = backend
        self.clock = clock
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.cache = {} if cache is None else cache
        self.walk_caps = walk_caps

    def folders_in_scope(self, folders: Iterable[str] | None) -> list[str]:
        """``folders`` that pass the path check as folders (their final paths), or the roots when none is given."""
        if folders is None:
            return [root.path for root in self.scope.roots]
        kept: list[str] = []
        for folder in folders:
            verdict = paths.check_path(folder, self.scope, self.backend, cache=self.cache)
            if verdict.ok and verdict.path not in kept:
                kept.append(verdict.path)
        return kept

    def search(self, words: Sequence[str], *, folders: Iterable[str] | None = None, kind: str = "",
               within_days: int | None = None, limit: int = MAX_RESULTS) -> SearchResult:
        """The files under ``folders`` (the roots when None) whose names hold any of ``words`` (all files when there
        are no words but a kind or within_days), the most words and then the newest first: the index, else the walk;
        every one checked (paths.check_path) and its identity read before it is listed."""
        words = tuple(word for word in words if word)
        kind = kind if kind in KIND_TYPES else ""               # "any" (and "folder": places, not files) filter nothing
        where = self.folders_in_scope(folders)
        if not where or not (words or kind or within_days):
            return SearchResult(looked_in=len(where))
        since = self.now().timestamp() - within_days * 86400 if within_days else None
        candidates, source = self._from_index(words, where, kind, within_days, since)
        if not candidates:
            candidates, source = self._from_walk(words, where, kind, since)
        candidates.sort(key=lambda found: (-found.score, -found.changed))
        files: list[FileItem] = []
        seen: set[str] = set()
        refused = 0
        for found in candidates[:MAX_EXAMINED]:
            if len(files) >= limit:
                break
            verdict = paths.check_path(found.path, self.scope, self.backend, cache=self.cache)
            item = file_item(verdict, self.backend) if verdict.ok else None
            if item is None or ntpath.normcase(item.path) in seen:
                refused += item is None
                continue
            if (kind and item.file_kind != kind) or (since is not None and item.identity.mtime_ns / 1e9 < since):
                continue
            seen.add(ntpath.normcase(item.path))
            files.append(item)
        logger.debug("Desktop: search from the %s, %d candidate(s), %d listed, %d refused", source or "nowhere",
                     len(candidates), len(files), refused)
        return SearchResult(tuple(files), len(where), len(candidates), source, refused)

    def _wanted(self, name: str, words: Sequence[str], kind: str) -> int:
        """The candidate's score (0: not wanted): the words its name holds, or 1 for any name when there are none."""
        if kind and file_kind(name) != kind:
            return 0
        if not words:
            return 1
        return name_score(name, words)

    def _from_index(self, words: Sequence[str], where: Sequence[str], kind: str, within_days: int | None,
                    since: float | None) -> tuple[list[_Candidate], str]:
        sql = build_query(words, where, kind=kind, within_days=within_days, now=self.now())
        if not sql:
            return [], ""
        try:
            rows = self.backend.search_index(sql, timeout_s=SEARCH_TIMEOUT_S, max_rows=INDEX_ROWS)
        except Exception as exc:  # noqa: BLE001 - no index (the service is off, the query failed): the walk then
            logger.info("Desktop: the search index could not answer (%s %s); looking through the folders instead",
                        type(exc).__name__, getattr(exc, "winerror", "") or "")
            return [], ""
        candidates: list[_Candidate] = []
        for row in rows:
            path = getattr(row, "path", "")
            if not isinstance(path, str) or not path or getattr(row, "is_dir", False):
                continue
            if getattr(row, "attributes", 0) & FILE_ATTRIBUTE_HIDDEN:
                continue
            name = row.name if isinstance(getattr(row, "name", ""), str) and row.name else ntpath.basename(path)
            score = self._wanted(name, words, kind)
            if not score:
                continue
            moment = getattr(row, "modified", None)
            modified = moment.timestamp() if isinstance(moment, datetime) else 0.0
            if since is not None and modified and modified < since:
                continue
            candidates.append(_Candidate(path, score, modified))
        return candidates, "index"

    def _from_walk(self, words: Sequence[str], where: Sequence[str], kind: str,
                   since: float | None) -> tuple[list[_Candidate], str]:
        scores: dict[int, int] = {}

        def accept(entry: DirEntry) -> bool:
            if entry.is_dir:
                return False
            score = self._wanted(entry.name, words, kind)
            if not score or (since is not None and entry.mtime_ns / 1e9 < since):
                return False
            scores[id(entry)] = score
            return True

        found = walk(where, self.backend, accept, caps=self.walk_caps, clock=self.clock)
        if found.stopped:
            logger.info("Desktop: the walk stopped at its %s cap (%d entries, %d folders)", found.stopped,
                        found.entries, found.folders)
        return [_Candidate(path, scores.get(id(entry), 1), entry.mtime_ns / 1e9) for path, entry in found.matches], \
            "walk"


# --------------------------------------------------------------------------
# Recent items
# --------------------------------------------------------------------------

def recent_files(scope: paths.Scope, backend: DesktopBackend, *, limit: int = 30,
                 cache: dict[str, bool] | None = None) -> list[FileItem]:
    """The files the newest Recent items point at that pass the path check (files only, each once, the newest first,
    at most ``limit``). The shortcuts themselves are never listed."""
    try:
        targets = backend.recent_items(limit=min(RECENT_LINKS, max(1, limit * 2)))
    except Exception as exc:  # noqa: BLE001 - no Recent items then
        logger.info("Desktop: the Recent items could not be read (%s)", type(exc).__name__)
        return []
    files: list[FileItem] = []
    seen: set[str] = set()
    for target in targets:
        if len(files) >= limit:
            break
        verdict = paths.check_path(target, scope, backend, cache=cache)
        item = file_item(verdict, backend) if verdict.ok else None
        if item is None or ntpath.normcase(item.path) in seen:
            continue
        seen.add(ntpath.normcase(item.path))
        files.append(item)
    return files


# --------------------------------------------------------------------------
# Places
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TreeFolder:
    """A folder below a root, as a walk saw it: its path, its name from the known folder down and when it changed."""

    path: str = field(repr=False)
    parts: tuple[str, ...] = field(repr=False)
    mtime_ns: int = 0


def folder_tree(scope: paths.Scope, backend: DesktopBackend, *, caps: WalkCaps = PLACE_WALK,
                clock: Callable[[], float] = time.monotonic) -> list[TreeFolder]:
    """The folders below the roots down to ``caps.depth`` levels (the walk's rules: no link, hidden, excluded, secret
    or code folder, nothing online-only gone into), within its caps. Not yet checked: pick_places checks the ones it
    lists."""
    labels = {ntpath.normcase(root.path.rstrip("\\")): root.label for root in scope.roots}
    found = walk([root.path for root in scope.roots], backend, lambda entry: entry.is_dir, caps=caps, clock=clock,
                 max_matches=caps.entries)
    tree: list[TreeFolder] = []
    for path, entry in found.matches:
        root = scope.root_of(path)
        if root is None:
            continue
        rest = path[len(root.path.rstrip("\\")) + 1:]
        parts = tuple(part for part in rest.split("\\") if part)
        if not parts or len(parts) > MAX_PLACE_DEPTH:
            continue
        tree.append(TreeFolder(path, (labels.get(ntpath.normcase(root.path.rstrip("\\")), root.label), *parts),
                               entry.mtime_ns))
    return tree


def pick_places(scope: paths.Scope, backend: DesktopBackend, tree: Sequence[TreeFolder], *,
                words: Sequence[str] = (), files: Sequence[FileItem] = (), named: Sequence[str] = (),
                cap: int = MAX_PLACES, cache: dict[str, bool] | None = None) -> list[Place]:
    """The places for <places>: the roots, then the folders the owner's words name (``named``: their paths), the
    folders whose names hold the words, the folders the listed files are in (down to 3 levels), then the newest -
    each one only once it passes the path check, at most ``cap``."""
    places: list[Place] = []
    seen: set[str] = set()

    def add(path: str, parts: Sequence[str]) -> None:
        key = ntpath.normcase(path.rstrip("\\"))
        if key in seen or len(places) >= cap:
            return
        seen.add(key)
        verdict = paths.check_path(path, scope, backend, cache=cache)
        place = place_item(verdict, parts, backend) if verdict.ok else None
        if place is not None:
            seen.add(ntpath.normcase(place.path.rstrip("\\")))
            places.append(place)

    for root in scope.roots:
        add(root.path, (root.label,))
    by_key = {ntpath.normcase(folder.path.rstrip("\\")): folder for folder in tree}
    for path in named:
        folder = by_key.get(ntpath.normcase(path.rstrip("\\")))
        if folder is not None:
            add(folder.path, folder.parts)
    if words:
        for folder in sorted(tree, key=lambda folder: (len(folder.parts), -folder.mtime_ns)):
            if name_score(folder.parts[-1], words):
                add(folder.path, folder.parts)
    for item in files:
        holder = ntpath.dirname(item.path)
        while holder and ntpath.normcase(holder.rstrip("\\")) not in by_key:
            parent = ntpath.dirname(holder)
            if parent == holder:
                holder = ""
                break
            holder = parent
        if holder:
            folder = by_key[ntpath.normcase(holder.rstrip("\\"))]
            add(folder.path, folder.parts)
    for folder in sorted(tree, key=lambda folder: -folder.mtime_ns):
        if len(places) >= cap:
            break
        add(folder.path, folder.parts)
    return places
