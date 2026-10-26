"""What the desktop planner may be told about this PC: the context's contents (SPEC sections 3 step 3 and 4.1; no Qt).

A desktop request's planner run is given names only (owner decision D6): the places Jarvis may look in, the files its
own local search found, the Start-menu apps and the saved sites - never a path, a file's contents, a typed value or a
picture. This module holds the shapes of those candidates and ``build_context``, which puts one request's together
within SPEC 4.1's caps: the places and files Jarvis's own search of this PC found (desktop.index: only inside the
approved roots, every item checked by paths.check_path, its identity recorded), the Start-menu apps the owner's words
match (desktop.apps: never one on the never-open list) and the saved sites (none in Phase 1: the approved sites are
Phase 3's). ask.desktop_mode turns a context into the planner's blocks and issues this run's refs (refs.RefIndex):
each ref's target is one of the items below, so a ref resolves to Jarvis's own entry - its path stays here and never
reaches the planner.

Public API
==========
``Place(path, parts, identity)``: a folder Jarvis may look in, its name from the known folder down ("Documents",
"Uni", "LING"; at most ``MAX_PLACE_DEPTH`` folders below the known one). ``FileItem(path, name, place, kind, size,
modified, online_only, identity)``: a file the local search found (``place`` is the path of the deepest listed place
it lies in, "" when none is listed). ``identity``: the backend.FileIdentity recorded as the item was listed (None:
unknown - nothing is opened on it). ``AppItem(app_id, name, exe)``: a Start-menu app (AUMID or exe path).
``SiteItem(url, title)``: a saved site. ``DesktopContext(places, files, apps, sites, looked_in, found)``: one request's
candidates and how much the search looked through (Live's "Looked in 2 places: 214 files"); ``counts()``.
``build_context(places, files, apps, sites, looked_in=, found=)``: the context within the caps (``MAX_PLACES``,
``MAX_FILES``, ``MAX_APPS``, ``MAX_SITES``), each file's place set, nothing secret-shaped, nothing twice.
``file_kind(name)`` ("document", "picture", "music", "video", "other"), ``size_bucket(size)`` ("1-10MB"), the
``KIND_*`` words and the type sets they come from.

Pure and never logged: every repr names kinds and counts only.
"""

from __future__ import annotations

import ntpath
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any

from . import guard

KIND_DOCUMENT, KIND_PICTURE, KIND_MUSIC, KIND_VIDEO, KIND_FOLDER, KIND_OTHER = (
    "document", "picture", "music", "video", "folder", "other")
FILE_KINDS = (KIND_DOCUMENT, KIND_PICTURE, KIND_MUSIC, KIND_VIDEO, KIND_OTHER)
# Each kind's types (SPEC 2.3's openable types, plus the script-capable documents, which are documents too).
DOCUMENT_TYPES = frozenset({"docx", "odt", "rtf", "txt", "md", "csv", "pdf", "xlsx", "ods", "pptx", "odp", "doc",
                            "docm", "dotm", "xls", "xlsm", "xltm", "xlsb", "ppt", "pptm", "potm", "ppsm"})
PICTURE_TYPES = frozenset({"jpg", "jpeg", "png", "gif", "bmp", "webp", "heic", "tif", "tiff", "svg"})
MUSIC_TYPES = frozenset({"mp3", "m4a", "wav", "flac", "ogg"})
VIDEO_TYPES = frozenset({"mp4", "mov", "mkv", "avi", "webm"})
KIND_TYPES = {KIND_DOCUMENT: DOCUMENT_TYPES, KIND_PICTURE: PICTURE_TYPES, KIND_MUSIC: MUSIC_TYPES,
              KIND_VIDEO: VIDEO_TYPES}
MAX_PLACE_DEPTH = 3                     # folders below the known folder a place may show (SPEC 4.1)
# SPEC 4.1's caps of one request's context (ask.desktop_mode shows no more, and trims <files> then <apps> to fit).
MAX_PLACES, MAX_FILES, MAX_APPS, MAX_SITES = 100, 200, 40, 20
_KB, _MB, _GB = 1024, 1024 * 1024, 1024 * 1024 * 1024
_BUCKETS = ((100 * _KB, "0-100KB"), (_MB, "100KB-1MB"), (10 * _MB, "1-10MB"), (100 * _MB, "10-100MB"),
            (_GB, "100MB-1GB"))
BIGGEST_BUCKET = "1GB+"


def file_kind(name: object) -> str:
    """The kind a file's real type makes it (guard.suffix: the last extension Windows sees)."""
    kind = guard.suffix(name)
    if kind in DOCUMENT_TYPES:
        return KIND_DOCUMENT
    if kind in PICTURE_TYPES:
        return KIND_PICTURE
    if kind in MUSIC_TYPES:
        return KIND_MUSIC
    if kind in VIDEO_TYPES:
        return KIND_VIDEO
    return KIND_OTHER


def size_bucket(size: object) -> str:
    """A size as a bucket ("1-10MB"); "" when unknown (a negative or missing size)."""
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return ""
    for limit, words in _BUCKETS:
        if size < limit:
            return words
    return BIGGEST_BUCKET


@dataclass(frozen=True)
class Place:
    """A folder Jarvis may look in: its final path (Jarvis's own; never sent) and its name from the known folder
    down - ``parts[0]`` is the known folder's label ("Documents"), the rest its subfolders."""

    path: str = field(repr=False)
    parts: tuple[str, ...] = field(default=(), repr=False)
    identity: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.parts, tuple):
            object.__setattr__(self, "parts", tuple(self.parts))

    @property
    def root(self) -> str:
        return self.parts[0] if self.parts else ""

    @property
    def depth(self) -> int:
        """Folders below the known folder (0: the known folder itself)."""
        return max(0, len(self.parts) - 1)

    def __repr__(self) -> str:
        return f"Place(depth {self.depth})"


@dataclass(frozen=True)
class FileItem:
    """A file the local search found: its final path (never sent), its name, the path of the deepest listed place it
    lies in ("" when none is listed), its kind ("" = from the name), size in bytes (-1 unknown), when it last
    changed and whether its content is in the cloud only (OneDrive: opening it downloads it)."""

    path: str = field(repr=False)
    name: str = field(repr=False)
    place: str = field(default="", repr=False)
    kind: str = ""
    size: int = -1
    modified: date | datetime | None = field(default=None, repr=False)
    online_only: bool = False
    identity: Any = field(default=None, repr=False, compare=False)

    @property
    def file_kind(self) -> str:
        return self.kind if self.kind in FILE_KINDS else file_kind(self.name)

    def __repr__(self) -> str:
        return f"FileItem({self.file_kind})"


@dataclass(frozen=True)
class AppItem:
    """A Start-menu app: its id (an AUMID or an exe path; never sent), its name as the Start menu shows it, and its
    exe path when known (the never-open list reads it)."""

    app_id: str = field(repr=False)
    name: str = field(repr=False)
    exe: str = field(default="", repr=False)

    def __repr__(self) -> str:
        return "AppItem()"


@dataclass(frozen=True)
class SiteItem:
    """A saved site: its https address (only its host is ever shown to the planner) and its title."""

    url: str = field(repr=False)
    title: str = field(repr=False)

    def __repr__(self) -> str:
        return "SiteItem()"


@dataclass(frozen=True)
class DesktopContext:
    """One request's candidates (memory only): the places, the files the search found (at most what the planner may
    be shown; ``found`` how many it found in all), the apps and the saved sites; ``looked_in`` how many places the
    search looked in."""

    places: tuple[Place, ...] = field(default=(), repr=False)
    files: tuple[FileItem, ...] = field(default=(), repr=False)
    apps: tuple[AppItem, ...] = field(default=(), repr=False)
    sites: tuple[SiteItem, ...] = field(default=(), repr=False)
    looked_in: int = 0
    found: int = 0

    def __post_init__(self) -> None:
        for name in ("places", "files", "apps", "sites"):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                object.__setattr__(self, name, tuple(value))

    def counts(self) -> dict[str, int]:
        """Sizes only (safe to log)."""
        return {"places": len(self.places), "files": len(self.files), "apps": len(self.apps),
                "sites": len(self.sites)}

    def __repr__(self) -> str:
        return f"DesktopContext({self.counts()})"


def _key(path: str) -> str:
    return ntpath.normcase(path or "").rstrip("\\")


def _inside(path: str, folder: str) -> bool:
    key, root = _key(path), _key(folder)
    return bool(key) and bool(root) and (key == root or key.startswith(root + "\\"))


def build_context(places: Iterable[Place], files: Iterable[FileItem], apps: Iterable[AppItem],
                  sites: Iterable[SiteItem] = (), *, looked_in: int = 0, found: int = 0) -> DesktopContext:
    """One request's context within SPEC 4.1's caps, in the order given (the best first): each place and file once (by
    path), each app once (by id), each site once (by address); no secret-shaped file (guard.secret_file: never
    listed, whatever the search gave); each file's ``place`` the deepest listed place it lies in ("" when none)."""
    kept_places: list[Place] = []
    seen: set[str] = set()
    for place in places:
        key = _key(place.path)
        if isinstance(place, Place) and key and key not in seen and len(kept_places) < MAX_PLACES:
            seen.add(key)
            kept_places.append(place)
    by_depth = sorted(kept_places, key=lambda place: -len(_key(place.path)))     # the deepest first
    kept_files: list[FileItem] = []
    seen = set()
    for item in files:
        key = _key(item.path) if isinstance(item, FileItem) else ""
        if not key or key in seen or guard.secret_file(item.name) or guard.secret_file(item.path):
            continue
        seen.add(key)
        place = next((place.path for place in by_depth if _inside(item.path, place.path)
                      and _key(item.path) != _key(place.path)), "")
        kept_files.append(replace(item, place=place))
        if len(kept_files) >= MAX_FILES:
            break
    kept_apps: list[AppItem] = []
    seen = set()
    for app in apps:
        key = app.app_id.casefold() if isinstance(app, AppItem) else ""
        if key and key not in seen and len(kept_apps) < MAX_APPS:
            seen.add(key)
            kept_apps.append(app)
    kept_sites: list[SiteItem] = []
    seen = set()
    for site in sites:
        key = site.url.casefold() if isinstance(site, SiteItem) else ""
        if key and key not in seen and len(kept_sites) < MAX_SITES:
            seen.add(key)
            kept_sites.append(site)
    return DesktopContext(tuple(kept_places), tuple(kept_files), tuple(kept_apps), tuple(kept_sites),
                          looked_in=max(0, int(looked_in)), found=max(0, int(found)))
