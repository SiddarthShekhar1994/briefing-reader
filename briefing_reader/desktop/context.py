"""What the desktop planner may be told about this PC: the context's contents (SPEC sections 3 step 3 and 4.1; no Qt).

A desktop request's planner run is given names only (owner decision D6): the places Jarvis may look in, the files its
own local search found, the Start-menu apps and the saved sites - never a path, a file's contents, a typed value or a
picture. This module holds the shapes of those candidates; the context BUILDER that fills them from this PC (the
search index, the Start menu, the scope) comes with P1.8 (index.py, apps.py). Until then the app hands the planner an
empty context and the tests fill one with invented items. ask.desktop_mode turns a context into the planner's blocks
and issues this run's refs (refs.RefIndex): each ref's target is one of the items below, so a ref resolves to
Jarvis's own entry - its path stays here and never reaches the planner.

Public API
==========
``Place(path, parts)``: a folder Jarvis may look in, its name from the known folder down ("Documents", "Uni",
"LING"; at most ``MAX_PLACE_DEPTH`` folders below the known one). ``FileItem(path, name, place, kind, size, modified,
online_only)``: a file the local search found (``place`` is the path of the deepest listed place it lies in, "" when
none is listed). ``AppItem(app_id, name, exe)``: a Start-menu app (AUMID or exe path). ``SiteItem(url, title)``: a
saved site. ``DesktopContext(places, files, apps, sites, looked_in, found)``: one request's candidates and how much
the search looked through (Live's "Looked in 2 places: 214 files"); ``counts()``.
``file_kind(name)`` ("document", "picture", "music", "video", "other"), ``size_bucket(size)`` ("1-10MB"), the
``KIND_*`` words and the type sets they come from.

Pure and never logged: every repr names kinds and counts only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

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
MAX_PLACE_DEPTH = 3                     # folders below the known folder a place may show (SPEC 4.1)
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
