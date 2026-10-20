"""Where the desktop feature may look and act, and the path check (SPEC sections 2.4, 5.1 and 5.2; no Qt).

The scope (``load_scope``): the roots are the known folders named in [desktop] folders (asked of the backend, so the
OneDrive redirection is followed) and the absolute [desktop] extra_folders under the user's own folder; each root
passes the same check as any path, and one that fails (a network drive, a junction, AppData, a code folder ...) is
left out with a log line naming its key and the reason only. The excluded places are the known folders in
guard.EXCLUDED_KNOWN_FOLDERS plus the ones the app adds (the Jarvis project and its data folder).

The check (``check_path(path, scope, backend)``), made before a card shows and again before each OS call:

1. Lexical, before anything touches the disk (a network path is never even opened): text, not too long, no control
   character; no \\\\?\\, \\\\.\\ or \\??\\ prefix, no UNC path (\\\\server\\share), no drive-relative ("C:foo") or
   relative path; no alternate data stream (a ":" after the drive); no "." or ".." component, no empty one; no
   reserved device name (CON, PRN, AUX, NUL, COM0-9, LPT0-9, also with an extension); no component ending in a dot
   or a space; none of < > " | ? *.
2. The final path from the backend (realpath(strict=True), then GetFinalPathNameByHandleW: 8.3 names and junctions
   resolved; its \\\\?\\ prefix is taken off, and \\\\?\\UNC\\ means a network path), checked lexically again, on a
   fixed drive (no network or removable drive in v1).
3. Inside a root (both normcase'd, is-relative-to).
4. Every component lstat'ed without following it: a symbolic link, a junction or an app execution alias anywhere in
   the path as given or in the final path is refused; OneDrive's cloud reparse points are fine; a component that is
   hidden AND system (at or below the root) is refused.
5. Excluded places: the excluded subtrees, guard.EXCLUDED_ROOT_NAMES right below the drive, guard.EXCLUDED_NAMES
   and guard.SECRET_DIR_NAMES at any depth, a component named like a code marker, any folder from the drive down
   holding a code marker (.git, .hg, .svn, .env, venv, .venv, node_modules: this covers the Jarvis project) and a
   secret-shaped file name.

A refusal carries a SPEC 4.4 code (``REFUSE_OUTSIDE`` "path_outside" or ``REFUSE_EXCLUDED`` "excluded") and a finer
reason word for Live and the log (``R_*``; never the path). An accepted path carries its final path, its root and the
card-row flags: ``FLAG_HIDDEN_CHARS`` (a bidi or invisible format character in it: shown as <U+202E>),
``FLAG_ONEDRIVE`` (under the OneDrive folder), ``FLAG_ONLINE_ONLY`` (its content is in the cloud). A name with hidden
characters is not refused here: its real type decides (guard.open_kind), and the card shows the characters.

Public API
==========
``Root(key, label, path, onedrive)``, ``Scope(roots, places, onedrive)`` (``root_of(path)``),
``load_scope(folders, extra_folders, backend, excluded=())``, ``PathVerdict(path, code, reason, root, flags)``
(``ok``), ``check_path(path, scope, backend)``, ``lexical_problem(path)``, ``reserved_name(component)``,
``display_name(text)``, ``name_problem(name)`` (a NEW name: SPEC 4.4 rule 4).
"""

from __future__ import annotations

import logging
import ntpath
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from . import guard
from .backend import DRIVE_FIXED, ROOT_FOLDERS, DesktopBackend, FileInfo, link_tag

logger = logging.getLogger(__name__)

REFUSE_OUTSIDE = "path_outside"
REFUSE_EXCLUDED = "excluded"
REFUSE_NAME = "name_invalid"
REFUSE_TYPE = "blocked_type"
FLAG_HIDDEN_CHARS = "hidden_chars"
FLAG_ONEDRIVE = "onedrive"
FLAG_ONLINE_ONLY = "online_only"
MAX_PATH_CHARS = 32_000
MAX_DEPTH = 64
MAX_NAME_CHARS = 120
ROOT_LABELS = {"desktop": "Desktop", "documents": "Documents", "downloads": "Downloads", "pictures": "Pictures",
               "music": "Music", "videos": "Videos"}

# Reasons (safe to log: a word, never the path).
R_NOT_TEXT = "not_text"
R_TOO_LONG = "too_long"
R_BAD_CHAR = "bad_char"
R_DEVICE_PREFIX = "device_prefix"
R_UNC = "unc"
R_RELATIVE = "relative"
R_DRIVE_RELATIVE = "drive_relative"
R_ADS = "stream"
R_DOT = "dot_segment"
R_TRAILING = "trailing_dot_space"
R_RESERVED = "reserved_name"
R_MISSING = "missing"
R_NOT_FIXED = "not_fixed_drive"
R_OUTSIDE = "outside_roots"
R_LINK = "link"
R_HIDDEN_SYSTEM = "hidden_system"
R_SYSTEM_PLACE = "system_place"
R_JARVIS = "jarvis_place"
R_CODE_FOLDER = "code_folder"
R_SECRET = "secret"
_EXCLUDED_REASONS = frozenset({R_UNC, R_NOT_FIXED, R_LINK, R_HIDDEN_SYSTEM, R_SYSTEM_PLACE, R_JARVIS, R_CODE_FOLDER,
                               R_SECRET})

_HIDDEN_RE = re.compile("[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb]")
_BAD_CHARS = frozenset('<>"|?*')
_RESERVED_RE = re.compile(r"(?:con|prn|aux|nul|conin\$|conout\$|(?:com|lpt)[0-9\u00b9\u00b2\u00b3])", re.IGNORECASE)
_CODE_MARKERS = frozenset(marker.casefold() for marker in guard.CODE_MARKERS)


@dataclass(frozen=True)
class Root:
    key: str                 # "documents" (a known folder) or "extra1"
    label: str               # what the owner calls it: "Documents", or an extra folder's own name
    path: str                # its final path
    onedrive: bool = False   # under the OneDrive folder

    def __repr__(self) -> str:                      # a path is the owner's; never in a log
        return f"Root(key={self.key!r}, onedrive={self.onedrive})"


@dataclass(frozen=True)
class Scope:
    roots: tuple[Root, ...] = ()
    places: tuple[tuple[str, str], ...] = ()       # (final path, reason) of each excluded subtree
    onedrive: str = ""                             # the OneDrive folder's final path ("" none)

    def root_of(self, path: str) -> Root | None:
        """The innermost root ``path`` (a final path) lies in, or None."""
        key = _key(path)
        inside = [root for root in self.roots if _inside(key, _key(root.path))]
        return max(inside, key=lambda root: len(root.path)) if inside else None

    def __repr__(self) -> str:
        return f"Scope(roots={[root.key for root in self.roots]}, places={len(self.places)})"


@dataclass(frozen=True)
class PathVerdict:
    path: str = ""                       # the final path ("" when refused)
    code: str = ""                       # "" ok, else REFUSE_OUTSIDE / REFUSE_EXCLUDED
    reason: str = ""                     # R_*
    root: Root | None = None
    flags: frozenset[str] = field(default_factory=frozenset)

    @property
    def ok(self) -> bool:
        return not self.code

    def __repr__(self) -> str:
        return f"PathVerdict(code={self.code!r}, reason={self.reason!r}, flags={sorted(self.flags)})"


def _refused(reason: str) -> PathVerdict:
    return PathVerdict(code=REFUSE_EXCLUDED if reason in _EXCLUDED_REASONS else REFUSE_OUTSIDE, reason=reason)


# --------------------------------------------------------------------------
# Lexical rules
# --------------------------------------------------------------------------

def reserved_name(component: str) -> bool:
    """CON, PRN, AUX, NUL, CONIN$, CONOUT$, COM0-9 and LPT0-9 (also COM\u00b9 ...), with or without an extension."""
    base = component.split(".", 1)[0].rstrip(" ")
    return bool(_RESERVED_RE.fullmatch(base))


def lexical_problem(path: object) -> str:
    """Why ``path`` is not a plain absolute local path (an R_* word), or ""."""
    if not isinstance(path, str) or not path:
        return R_NOT_TEXT
    if len(path) > MAX_PATH_CHARS:
        return R_TOO_LONG
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in path):
        return R_BAD_CHAR
    text = path.replace("/", "\\")
    if text[:4] in ("\\\\?\\", "\\\\.\\", "\\??\\"):
        return R_DEVICE_PREFIX
    if text.startswith("\\\\"):
        return R_UNC
    if len(text) < 2 or text[1] != ":" or not (text[0].isascii() and text[0].isalpha()):
        return R_RELATIVE
    if len(text) == 2 or text[2] != "\\":
        return R_DRIVE_RELATIVE
    rest = text[3:]
    if ":" in rest:
        return R_ADS
    parts = rest.split("\\")
    if parts[-1] == "":
        parts.pop()                                # one trailing separator
    if len(parts) > MAX_DEPTH:
        return R_TOO_LONG
    for part in parts:
        if not part:
            return R_BAD_CHAR                      # an empty component ("C:\\a\\\\b")
        if part in (".", ".."):
            return R_DOT
        if part[-1] in ". ":
            return R_TRAILING
        if any(char in _BAD_CHARS for char in part):
            return R_BAD_CHAR
        if reserved_name(part):
            return R_RESERVED
    return ""


def display_name(text: str) -> str:
    """``text`` with its bidi and invisible format characters shown as <U+202E> (the card's row flag)."""
    return _HIDDEN_RE.sub(lambda match: f"<U+{ord(match.group(0)):04X}>", text)


def name_problem(name: object) -> str:
    """Why a NEW name (a rename, a new folder, a zip) may not be used: REFUSE_NAME, REFUSE_TYPE or "". It must be
    NFC-normalised already, 1-120 characters, with no control, format or bidi character, none of < > : " / \\ | ? *,
    no reserved device name, no trailing dot or space, and never a blocked type, a shortcut or an internet shortcut.
    (Whether the type may change at all is validate.py's question: only when the owner's words ask.)"""
    if not isinstance(name, str) or not 1 <= len(name) <= MAX_NAME_CHARS:
        return REFUSE_NAME
    if unicodedata.normalize("NFC", name) != name or name in (".", ".."):
        return REFUSE_NAME
    if any(unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp") for char in name) or _HIDDEN_RE.search(name):
        return REFUSE_NAME
    if any(char in _BAD_CHARS or char in ':/\\' for char in name):
        return REFUSE_NAME
    if name[-1] in ". " or reserved_name(name):
        return REFUSE_NAME
    return "" if guard.may_create_type(name) else REFUSE_TYPE


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _key(path: str) -> str:
    return ntpath.normcase(path).rstrip("\\")


def _inside(key: str, root_key: str) -> bool:
    return key == root_key or key.startswith(root_key + "\\")


def _fold(component: str) -> str:
    return unicodedata.normalize("NFKC", _HIDDEN_RE.sub("", component)).casefold().rstrip(". ")


def _strip_final(path: str) -> str:
    """GetFinalPathNameByHandleW's form back to a plain one: \\\\?\\C:\\x -> C:\\x, \\\\?\\UNC\\s\\x -> \\\\s\\x."""
    text = path.replace("/", "\\")
    if text[:8].casefold() == "\\\\?\\unc\\":
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\") and text[5:7] == ":\\":
        return text[4:]
    return text


def _prefixes(path: str) -> list[str]:
    """Every path from the first folder below the drive down to ``path`` itself (C:\\a, C:\\a\\b, ...)."""
    text = path.replace("/", "\\")
    drive, rest = text[:3], text[3:].rstrip("\\")
    if not rest:
        return []
    parts = rest.split("\\")
    return [drive + "\\".join(parts[:i]) for i in range(1, len(parts) + 1)]


def _final(path: str, backend: DesktopBackend) -> str:
    """The checked final path of ``path``, or "" (missing, not plain)."""
    if lexical_problem(path):
        return ""
    try:
        final = _strip_final(backend.final_path(path))
    except OSError:
        return ""
    return "" if lexical_problem(final) else final


# --------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------

def _infos(paths: Iterable[str], backend: DesktopBackend) -> list[FileInfo]:
    return [backend.file_info(path) for path in paths]


def _place_problem(final: str, scope: Scope, backend: DesktopBackend, item_is_dir: bool,
                   cache: dict[str, bool] | None) -> str:
    key = _key(final)
    for place, reason in scope.places:
        if _inside(key, _key(place)):
            return reason
    parts = final[3:].rstrip("\\").split("\\") if final[3:].strip("\\") else []
    folded = [_fold(part) for part in parts]
    if folded and folded[0] in guard.EXCLUDED_ROOT_NAMES:
        return R_SYSTEM_PLACE
    if any(part in guard.EXCLUDED_NAMES for part in folded):
        return R_SYSTEM_PLACE
    if any(part in guard.SECRET_DIR_NAMES for part in folded) or (parts and guard.secret_file(parts[-1])):
        return R_SECRET
    if any(part in _CODE_MARKERS for part in folded):
        return R_CODE_FOLDER
    folders = [final[:3]] + _prefixes(final)
    if not item_is_dir:
        folders = folders[:-1]
    for folder in folders:
        if _holds_marker(folder, backend, cache):
            return R_CODE_FOLDER
    return ""


def _holds_marker(folder: str, backend: DesktopBackend, cache: dict[str, bool] | None) -> bool:
    key = _key(folder)
    if cache is not None and key in cache:
        return cache[key]
    found = any(backend.exists(ntpath.join(folder, marker)) for marker in guard.CODE_MARKERS)
    if cache is not None:
        cache[key] = found
    return found


def check_path(path: object, scope: Scope, backend: DesktopBackend, *,
               cache: dict[str, bool] | None = None) -> PathVerdict:
    """The check described in the module docstring. ``cache`` (optional) remembers which folders hold a code
    marker during one walk."""
    reason = lexical_problem(path)
    if reason:
        return _refused(reason)
    assert isinstance(path, str)
    try:
        final = _strip_final(backend.final_path(path))
    except OSError:
        return _refused(R_MISSING)
    reason = lexical_problem(final)
    if reason:
        return _refused(reason)
    if backend.drive_type(final[:3]) != DRIVE_FIXED:
        return _refused(R_NOT_FIXED)
    root = scope.root_of(final)
    if root is None:
        return _refused(R_OUTSIDE)
    below = _prefixes(final)[max(0, len(_prefixes(root.path)) - 1):]     # the root and everything under it
    try:
        given = _infos(_prefixes(path), backend)
        infos = _infos(below, backend)
    except OSError:
        return _refused(R_MISSING)
    if any(link_tag(info.reparse_tag) for info in (*given, *infos)):
        return _refused(R_LINK)
    if any(info.hidden_system for info in infos):
        return _refused(R_HIDDEN_SYSTEM)
    reason = _place_problem(final, scope, backend, bool(infos and infos[-1].is_dir), cache)
    if reason:
        return _refused(reason)
    flags = set()
    if _HIDDEN_RE.search(final):
        flags.add(FLAG_HIDDEN_CHARS)
    if root.onedrive or (scope.onedrive and _inside(_key(final), _key(scope.onedrive))):
        flags.add(FLAG_ONEDRIVE)
    if infos and infos[-1].online_only:
        flags.add(FLAG_ONLINE_ONLY)
    return PathVerdict(final, root=root, flags=frozenset(flags))


# --------------------------------------------------------------------------
# The scope
# --------------------------------------------------------------------------

def _root_problem(path: str, final: str, base: Scope, backend: DesktopBackend) -> str:
    """Why ``final`` (the final path of ``path``) may not be a root ("" when it may)."""
    if backend.drive_type(final[:3]) != DRIVE_FIXED:
        return R_NOT_FIXED
    try:
        infos = _infos([*_prefixes(path), *_prefixes(final)], backend)
    except OSError:
        return R_MISSING
    if any(link_tag(info.reparse_tag) for info in infos):
        return R_LINK
    if any(info.hidden_system for info in infos):
        return R_HIDDEN_SYSTEM
    return _place_problem(final, base, backend, True, None)


def load_scope(folders: Sequence[str], extra_folders: Sequence[str], backend: DesktopBackend, *,
               excluded: Sequence[str] = ()) -> Scope:
    """The roots and the excluded places (see the module docstring). ``excluded`` are the app's own places (the
    Jarvis project, its data folder); they are excluded whether or not they exist."""
    places: list[tuple[str, str]] = []
    for name in guard.EXCLUDED_KNOWN_FOLDERS:
        path = backend.known_folder(name)
        if path:
            places.append((_final(path, backend) or path, R_SYSTEM_PLACE))
    for path in excluded:
        if isinstance(path, str) and path:
            places.append((_final(path, backend) or path, R_JARVIS))
    onedrive_path = backend.known_folder("onedrive") or ""
    onedrive = _final(onedrive_path, backend) if onedrive_path else ""
    base = Scope((), tuple(places), onedrive)
    roots: list[Root] = []
    for name in folders:
        if name not in ROOT_FOLDERS:
            logger.warning("Desktop: %r is not a folder Jarvis knows; left out", name[:20])
            continue
        path = backend.known_folder(name) or ""
        _add_root(roots, name, ROOT_LABELS[name], path, base, backend)
    profile_path = backend.known_folder("profile") or ""
    profile = _final(profile_path, backend) if profile_path else ""
    for index, path in enumerate(extra_folders, 1):
        key = f"extra{index}"
        final = _final(path, backend) if isinstance(path, str) else ""
        if not profile or not final or _key(final) == _key(profile) or not _inside(_key(final), _key(profile)):
            logger.warning("Desktop: extra folder %d is not a folder under your user folder; left out", index)
            continue
        _add_root(roots, key, ntpath.basename(final.rstrip("\\")), path, base, backend)
    return Scope(tuple(roots), tuple(places), onedrive)


def _add_root(roots: list[Root], key: str, label: str, path: str, base: Scope, backend: DesktopBackend) -> None:
    final = _final(path, backend) if path else ""
    if not final:
        logger.warning("Desktop: the %s folder is not there; left out", key)
        return
    problem = _root_problem(path, final, base, backend)
    if problem:
        logger.warning("Desktop: the %s folder is left out (%s)", key, problem)
        return
    if any(_key(root.path) == _key(final) for root in roots):
        return
    onedrive = bool(base.onedrive) and _inside(_key(final), _key(base.onedrive))
    roots.append(Root(key, label, final, onedrive))
