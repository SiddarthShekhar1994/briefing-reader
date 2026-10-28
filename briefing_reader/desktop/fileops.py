"""Organising the owner's files: move, rename, copy, make a folder, zip - and putting them back (SPEC sections 2.1,
2.4, 5.2-5.4 and 6; hard rules 5, 6, 12 and 14; no Qt).

Each row of a NEEDS YOUR OK card is one ``FileOp``, carrying the card's OWN targets - the resolved items and the 5.3
identity they were shown with (verified review note: never an id resolved again against a newer index). ``FileOps``
carries a card's rows out one at a time, in order, on the desktop worker's thread; right before each row's OS call
everything is checked again (``check``, the executor's re-check pattern; hard rule 6), source AND destination:

* every source: paths.check_path in the scope as it is now (inside a root, a fixed local drive, no link anywhere, no
  excluded place, code folder or secret file), the same final path and the same identity (volume serial, file id,
  and for a file its size and mtime) - else "Changed since - left as it is." (``R_CHANGED``); a root itself is never
  moved, renamed, copied or zipped (``R_ROOT``);
* before any FOLDER is moved, copied or zipped, a bounded walk of everything below it (``walk_subtree``): it refuses a
  folder holding a scope place (a root or an excluded place), a code marker, a secret folder, an excluded name, a
  secret file, a link, or a folder whose listing is online-only (OneDrive would fetch it) - and one the walk cannot
  finish within its caps (``SUBTREE_CAPS``) or read;
* the destination folder the same way (a folder an earlier row of the run makes is named by that row's ``key``); a
  NEW name passes paths.name_problem (no hidden or control character, no reserved name, never a blocked type, a
  shortcut or an internet shortcut) and never names a code marker, a secret folder or file or an excluded place;
* never over anything: a name that is taken refuses the row (``R_NAME_CLASH``) - the card shows "name (2).ext"
  beforehand when it wants another name (``free_name``; hard rule 5), never a name chosen silently here;
* a move or a rename stays on its own volume (``R_CROSS_VOLUME``: refused in v1); a copy or a zip never makes
  OneDrive download anything (an online-only file: ``R_ONLINE_ONLY``), never zips a blocked type
  (``R_BLOCKED_TYPE``), keeps within 1000 files / 4 GB (``R_CAP``) and needs at most the free space less 10%
  (``R_NO_SPACE``).

Then the row is journalled "doing" (written and flushed to disk first: journal.Journal; nothing is done when that
cannot be saved, ``R_JOURNAL``), its OS call made through the DesktopBackend (MoveFileExW without
MOVEFILE_REPLACE_EXISTING; CopyFileExW fail-if-exists with a cancel callback, so Stop works mid-copy;
CreateDirectoryW; a zip written ZIP_DEFLATED under a temporary name in the destination - Windows removes it if it is
not finished - with the inputs' Zone.Identifier copied onto it, then renamed into place; a folder copy made under a
temporary name the same way), and journalled "done" (with the result's identity, and a copy's or a zip's content
hash) or "failed" with a reason code: a sharing violation (32, 33) is ``R_IN_USE``, a stop ``R_STOPPED``. The card's
row flags come with each result (``FLAG_*``: uploads to / leaves OneDrive, online-only, pinned, hidden characters).

``Undoer`` puts a run's items back on the owner's click (SPEC 6.3), one item at a time, in reverse order, each one
re-checked and journalled the same way: a move or a rename goes back when the same file is still where Jarvis put it
and its old place is free ("Moved since - left where it is.", "Something new is in its old place - left where it
is."); a copy or a zip is recycled - through the ``Recycler`` seam: the Recycle Bin arrives in P2.6, until then
``NoRecycleBin`` refuses cleanly - only when it is Jarvis's own output, unchanged (its hash); a folder Jarvis made is
removed only when it is empty; anything older than [desktop] undo_days is refused. ``UndoReport.stamp()`` is the
card's partial stamp ("PUT BACK 4 OF 5 \u00b7 1 MOVED SINCE").

The raw Windows calls (``raw_*``) are reached only through Win32Backend's guarded methods - and, while the tests run,
through the test fake on a temporary folder: under the test guard every raw call refuses any path outside the
system's temporary folder (backend.RealBackendInTests) before Windows is asked. Names the owner's files carry are
data, never instructions: nothing here reads them for anything but their place. Logs carry run ids, row numbers,
kinds, states, reason codes, counts, exception types and winerrors only - never a name or a path.

Public API
==========
``FileOp(kind, sources, folder, name, key)``, ``Check``, ``OpResult``, ``BatchReport``, ``FileOps(backend, journal,
scope=, cancel=, clock=, caps=)`` (``check(op, pending=)`` - ``pending``: a card's new folders not made yet, P2.2 -,
``run(ops)``); ``Undoer(backend, journal, scope=, recycler=,
undo_days=, now=, cancel=)`` (``undoable()``, ``undo(seqs=None)``), ``UndoResult`` (``words``), ``UndoReport``
(``put_back``, ``total``, ``refused()``, ``stamp()``); ``Recycler``, ``NoRecycleBin``, ``RecycleUnavailable``;
``walk_subtree(folder, scope, backend, content=)``, ``Subtree``, ``SubtreeCaps``, ``SUBTREE_CAPS``,
``tree_digest(folder, backend)``, ``free_name(folder, name, backend, taken=)``, ``fold_name(name)``,
``os_reason(exc)``; reason codes ``R_*``,
``UNDO_WORDS``, ``STAMP_LABELS``; flags ``FLAG_*``; ``Stopped``, ``ChangedSince``; the raw calls ``raw_move``,
``raw_copy``, ``raw_make_folder``, ``raw_remove_folder``, ``raw_write_zip``, ``raw_zone``, ``raw_hash``,
``raw_free``.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import ntpath
import os
import re
import secrets
import tempfile
import time
import unicodedata
import zipfile
from collections import deque
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from . import guard, paths
from .backend import (FILE_ATTRIBUTE_OFFLINE, FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, FILE_ATTRIBUTE_RECALL_ON_OPEN,
                      FILE_ATTRIBUTE_REPARSE_POINT, MAX_LISTED, DesktopBackend, DirEntry, FileIdentity,
                      RealBackendInTests, ZipMember, cloud_tag, real_backend_blocked)
from .context import Place
from .journal import (COPY, DONE, DOING, FAILED, MKDIR, MOVE, PLANNED, RENAME, SCREENSHOT, SKIPPED, UNDO_REFUSED,
                      UNDONE, ZIP, Item, Journal, JournalError, Record, same_place)

logger = logging.getLogger(__name__)

OP_KINDS = (MOVE, RENAME, COPY, MKDIR, ZIP)

# Reason codes (safe to log: a word, never a path). The checks' (each refuses before a card shows, and again right
# before the OS call):
R_CHANGED = "changed"                    # changed or gone since the card was made: "Changed since - left as it is."
R_OUTSIDE = paths.REFUSE_OUTSIDE         # "path_outside": not inside the folders Jarvis may use
R_EXCLUDED = paths.REFUSE_EXCLUDED       # "excluded": a place, code folder or secret file Jarvis never touches
R_BLOCKED_TYPE = paths.REFUSE_TYPE       # "blocked_type": never zipped, never renamed to
R_NAME = paths.REFUSE_NAME               # "name_invalid"
R_NAME_CLASH = "name_clash"              # something is already there under that name: never overwritten
R_CROSS_VOLUME = "cross_volume"          # another drive or volume: refused in v1
R_IN_USE = "in_use"                      # a sharing or lock violation (32, 33): another program has it open
R_ONLINE_ONLY = "online_only"            # OneDrive would download it first: never for a copy or a zip
R_SAME_PLACE = "same_place"              # it is there already
R_INTO_ITSELF = "into_itself"            # a folder into itself
R_ROOT = "root"                          # a root itself (Documents ...) is never moved, renamed, copied or zipped
R_NO_SPACE = "no_space"                  # more than the free space less 10%
R_CAP = "cap"                            # a zip of more than 1000 files or 4 GB
R_SHAPE = "shape"                        # not an operation Jarvis knows, or no item to do it to
R_NO_FOLDER = "no_folder"                # its folder was to come from an earlier row, which did not make it
# The subtree walk's (before any folder is moved, copied or zipped): what the folder holds.
R_HOLDS_PLACE = "holds_place"            # a root or an excluded place (the Jarvis project, its data ...)
R_HOLDS_CODE = "holds_code"              # .git, node_modules, venv ...
R_HOLDS_SECRET_FOLDER = "holds_secret_folder"   # .ssh, .gnupg, .aws
R_HOLDS_EXCLUDED = "holds_excluded"      # AppData, $Recycle.Bin, a shell class name ...
R_HOLDS_SECRET_FILE = "holds_secret_file"       # .env, a key, token or credentials file ...
R_HOLDS_LINK = "holds_link"              # a junction, a symbolic link or an app alias
R_HOLDS_ONLINE_ONLY = "holds_online_only"       # an online-only folder listing (a copy or a zip: any online-only file)
R_WALK_CAP = "walk_cap"                  # the walk did not finish within its caps
R_UNREADABLE = "unreadable"              # a folder in it could not be listed
# The OS call's.
R_STOPPED = "stopped"                    # the owner stopped the run (Esc, Stop)
R_JOURNAL = "journal"                    # the journal could not be saved: nothing was done
R_ACCESS = "access_denied"
R_DISK_FULL = "disk_full"
R_OS = "os_error"
# Undo's.
R_MOVED_SINCE = "moved_since"            # not where Jarvis put it any more
R_OCCUPIED = "occupied"                  # something new is in its old place
R_NOT_EMPTY = "not_empty"                # a folder Jarvis made holds something now
R_EXPIRED = "expired"                    # older than [desktop] undo_days
R_OLD_PLACE_GONE = "old_place_gone"      # the folder it came from is gone
R_NO_BIN = "recycle_unavailable"         # the Recycle Bin can't take it (until P2.6: never)
R_NOT_UNDOABLE = "not_undoable"          # nothing of it was done, or it is put back already
R_NOT_YET = "not_yet"                    # a kind whose Undo comes in a later part
_HOLDS = (R_HOLDS_PLACE, R_HOLDS_CODE, R_HOLDS_SECRET_FOLDER, R_HOLDS_EXCLUDED, R_HOLDS_SECRET_FILE, R_HOLDS_LINK,
          R_WALK_CAP, R_UNREADABLE)

# The card's words for an item Undo left as it is (SPEC 6.3 and hard rule 12 give the first three).
UNDO_WORDS = {
    R_MOVED_SINCE: "Moved since - left where it is.",
    R_OCCUPIED: "Something new is in its old place - left where it is.",
    R_CHANGED: "Changed since - left as it is.",
    R_NOT_EMPTY: "Something is in it now - left as it is.",
    R_EXPIRED: "Too long ago to put back - left as it is.",
    R_CROSS_VOLUME: "Its old place is on another drive now - left where it is.",
    R_IN_USE: "In use - left where it is.",
    R_ONLINE_ONLY: "It would skip your PC's Recycle Bin - left for you.",
    R_OLD_PLACE_GONE: "Its old folder has gone - left where it is.",
    R_NO_BIN: "The Recycle Bin can't take it yet - left as it is.",
    R_OUTSIDE: "Outside the folders I may use now - left where it is.",
    R_EXCLUDED: "Somewhere I never touch now - left where it is.",
    R_ACCESS: "Windows didn't allow it - left where it is.",
    R_NOT_YET: "Can't be put back yet - left as it is.",
    R_NOT_UNDOABLE: "Nothing to put back.",
}
UNDO_WORDS.update({reason: "It holds something I never move - left where it is." for reason in _HOLDS})
DEFAULT_UNDO_WORDS = "Not put back - left where it is."
# The partial stamp's words for each reason ("PUT BACK 4 OF 5 \u00b7 1 MOVED SINCE").
STAMP_LABELS = {R_MOVED_SINCE: "MOVED SINCE", R_OCCUPIED: "SOMETHING NEW IN ITS PLACE", R_CHANGED: "CHANGED SINCE",
                R_NOT_EMPTY: "NOT EMPTY", R_EXPIRED: "TOO OLD", R_CROSS_VOLUME: "ON ANOTHER DRIVE",
                R_IN_USE: "IN USE", R_ONLINE_ONLY: "ONLINE-ONLY", R_OLD_PLACE_GONE: "OLD FOLDER GONE",
                R_NO_BIN: "BIN NOT READY"}
STAMP_OTHER = "NOT PUT BACK"
STAMP_DOT = " \u00b7 "

# The card's row flags (SPEC 5.4; shown as text, never as colour alone).
FLAG_TO_ONEDRIVE = "to_onedrive"         # UPLOADS TO ONEDRIVE: new content under the OneDrive folder
FLAG_LEAVES_ONEDRIVE = "leaves_onedrive"  # LEAVES ONEDRIVE - REMOVED FROM YOUR OTHER DEVICES
FLAG_ONLINE_ONLY = paths.FLAG_ONLINE_ONLY  # ONLINE-ONLY: its content (or some of a folder's) is in the cloud
FLAG_RECALL_ON_OPEN = "recall_on_open"   # a folder whose listing is in the cloud
FLAG_PINNED = "pinned"                   # "Always keep on this device"
FLAG_HIDDEN_CHARS = paths.FLAG_HIDDEN_CHARS
FILE_ATTRIBUTE_PINNED = 0x00080000
_RECALLED = FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_OFFLINE

MAX_ZIP_FILES = 1000                     # SPEC 2.1: zip 1000 files / 4 GB
MAX_ZIP_BYTES = 4 * 1024 ** 3
SPACE_MARGIN = 0.10                      # a copy or a zip needs at most the free space less 10% (SPEC 5.4)
TEMP_SUFFIX = ".jarvis-part"             # a zip or folder copy is made under "<name>.<token>.jarvis-part" first
_DAY_S = 86_400
_CODE_MARKERS = frozenset(marker.casefold() for marker in guard.CODE_MARKERS)
_ZONE_STREAM = ":Zone.Identifier"
_ZONE_RE = re.compile(rb"ZoneId\s*=\s*(\d+)", re.IGNORECASE)
_MAX_ZONE = 64 * 1024
_CHUNK = 1 << 20


class Stopped(Exception):
    """The owner stopped the run while a copy or a zip was being made."""


class ChangedSince(Exception):
    """An item changed while it was being read (a zip member is not the size it was listed with)."""


class RecycleUnavailable(Exception):
    """The Recycle Bin can't take the item (P2.6 brings the real one; until then it never can)."""


# --------------------------------------------------------------------------
# The raw Windows calls (reached through Win32Backend's guarded methods; in the tests, on a temporary folder only)
# --------------------------------------------------------------------------

_ERRORS = {2: R_CHANGED, 3: R_CHANGED, 5: R_ACCESS, 17: R_CROSS_VOLUME, 32: R_IN_USE, 33: R_IN_USE, 39: R_DISK_FULL,
           80: R_NAME_CLASH, 112: R_DISK_FULL, 145: R_NOT_EMPTY, 183: R_NAME_CLASH, 1223: R_STOPPED,
           1235: R_STOPPED}
_PROGRESS_CONTINUE, _PROGRESS_CANCEL = 0, 1
_GENERIC_WRITE, _DELETE = 0x40000000, 0x00010000
_CREATE_NEW, _FILE_ATTRIBUTE_NORMAL = 1, 0x80
_FILE_DISPOSITION_INFO = 4               # FILE_INFO_BY_HANDLE_CLASS FileDispositionInfo
_MOVE_FLAGS = 0                          # no MOVEFILE_REPLACE_EXISTING, COPY_ALLOWED or DELAY_UNTIL_REBOOT
_COPY_FLAGS = guard.COPY_FILE_FAIL_IF_EXISTS


def os_reason(exc: BaseException) -> str:
    """The reason code for an OS call that did not happen (by its winerror; never its text, which names paths)."""
    if isinstance(exc, Stopped):
        return R_STOPPED
    if isinstance(exc, ChangedSince):
        return R_CHANGED
    if isinstance(exc, JournalError):
        return R_JOURNAL
    reason = _ERRORS.get(int(getattr(exc, "winerror", 0) or 0))
    if reason:
        return reason
    if isinstance(exc, FileExistsError):
        return R_NAME_CLASH
    if isinstance(exc, FileNotFoundError):
        return R_CHANGED
    return R_OS


def _winerror(exc: BaseException) -> int:
    return int(getattr(exc, "winerror", 0) or 0)


def _long(path: str) -> str:
    """A checked plain path as Windows' long form (\\\\?\\C:\\...): used only here, after every check."""
    text = path.replace("/", "\\")
    return text if text.startswith("\\\\?\\") else "\\\\?\\" + text


def _real_key(path: str) -> str:
    text = path.replace("/", "\\")
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return ntpath.normcase(os.path.realpath(text)).rstrip("\\")


def _temp_only(*targets: str) -> None:
    """While the tests run (the test guard), a raw call touches only the system's temporary folder - where every
    test makes its own folder - and refuses anything else before Windows is asked."""
    if not real_backend_blocked():
        return
    temp = _real_key(tempfile.gettempdir())
    for target in targets:
        if not isinstance(target, str) or not _real_key(target).startswith(temp + "\\"):
            raise RealBackendInTests("a file operation outside the temporary folder while the tests run")


@functools.lru_cache(maxsize=1)
def _win32() -> Any:
    """kernel32's file functions with their prototypes, the copy's progress-routine type and the disposition
    structure (made once)."""
    import ctypes
    from ctypes import wintypes
    from types import SimpleNamespace

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    progress = ctypes.WINFUNCTYPE(wintypes.DWORD, ctypes.c_longlong, ctypes.c_longlong, ctypes.c_longlong,
                                  ctypes.c_longlong, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE, wintypes.HANDLE,
                                  wintypes.LPVOID)

    class Disposition(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

    kernel32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.MoveFileExW.restype = wintypes.BOOL
    kernel32.CopyFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, progress, wintypes.LPVOID,
                                     ctypes.POINTER(wintypes.BOOL), wintypes.DWORD]
    kernel32.CopyFileExW.restype = wintypes.BOOL
    kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, wintypes.LPVOID]
    kernel32.CreateDirectoryW.restype = wintypes.BOOL
    kernel32.RemoveDirectoryW.argtypes = [wintypes.LPCWSTR]
    kernel32.RemoveDirectoryW.restype = wintypes.BOOL
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return SimpleNamespace(ctypes=ctypes, wintypes=wintypes, k=kernel32, progress=progress, Disposition=Disposition,
                           invalid=wintypes.HANDLE(-1).value)


def _last_error() -> OSError:
    import ctypes

    return ctypes.WinError(ctypes.get_last_error())     # its text never names a path


def raw_move(src: str, dst: str) -> None:
    """MoveFileExW(src, dst) with no flags: never over anything (183), never to another volume (17)."""
    _temp_only(src, dst)
    if not guard.move_flags_ok(_MOVE_FLAGS):
        raise ValueError("forbidden move flags")
    if not _win32().k.MoveFileExW(_long(src), _long(dst), _MOVE_FLAGS):
        raise _last_error()


def raw_copy(src: str, dst: str, cancel: Callable[[], bool]) -> None:
    """CopyFileExW(src, dst) fail-if-exists (80); ``cancel`` is asked from the progress routine as the copy goes, and
    True cancels it (1235: Windows removes its part-made copy)."""
    _temp_only(src, dst)
    if not guard.copy_flags_ok(_COPY_FLAGS):
        raise ValueError("copy flags without fail-if-exists")
    win = _win32()

    def progress(*_args: Any) -> int:
        try:
            return _PROGRESS_CANCEL if cancel() else _PROGRESS_CONTINUE
        except Exception:                                  # noqa: BLE001 - a broken cancel stops the copy
            return _PROGRESS_CANCEL

    routine = win.progress(progress)
    cancelled = win.wintypes.BOOL(False)
    if not win.k.CopyFileExW(_long(src), _long(dst), routine, None, win.ctypes.byref(cancelled), _COPY_FLAGS):
        raise _last_error()


def raw_make_folder(path: str) -> None:
    """CreateDirectoryW: a new folder (183 when something is there)."""
    _temp_only(path)
    if not _win32().k.CreateDirectoryW(_long(path), None):
        raise _last_error()


def raw_remove_folder(path: str) -> None:
    """RemoveDirectoryW: an EMPTY folder only (145 when anything is in it)."""
    _temp_only(path)
    if not _win32().k.RemoveDirectoryW(_long(path)):
        raise _last_error()


def _zip_time(mtime_ns: int) -> tuple[int, int, int, int, int, int]:
    try:
        moment = tuple(time.localtime(mtime_ns / 1e9)[:6])
    except (OverflowError, OSError, ValueError):
        moment = (1980, 1, 1, 0, 0, 0)
    return min(max(moment, (1980, 1, 1, 0, 0, 0)), (2107, 12, 31, 23, 59, 58))      # type: ignore[return-value]


def _add_member(archive: zipfile.ZipFile, member: ZipMember, cancel: Callable[[], bool]) -> None:
    if cancel():
        raise Stopped()
    if member.is_dir:
        info = zipfile.ZipInfo(member.arcname.rstrip("\\/") + "/", _zip_time(member.mtime_ns))
        info.external_attr = (0o40775 << 16) | 0x10
        archive.writestr(info, b"")
        return
    info = zipfile.ZipInfo(member.arcname, _zip_time(member.mtime_ns))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.file_size = member.size
    read = 0
    with open(_long(member.path), "rb") as source, archive.open(info, "w") as sink:
        while True:
            if cancel():
                raise Stopped()
            chunk = source.read(_CHUNK)
            if not chunk:
                break
            read += len(chunk)
            if read > member.size:
                raise ChangedSince()
            sink.write(chunk)
    if read != member.size:
        raise ChangedSince()


def raw_write_zip(members: Sequence[ZipMember], temp: str, cancel: Callable[[], bool],
                  zone: bytes | None = None) -> None:
    """A new zip at ``temp`` (CREATE_NEW: it must not exist) from ``members``, ZIP_DEFLATED. Until it is all written
    the file is marked for deletion (FileDispositionInfo), so a failure, a stop or a crash leaves nothing behind -
    Windows removes it as its handle closes; then the mark is cleared and ``zone`` written as its Zone.Identifier.
    The members are only read; one that is not the size it was listed with raises ChangedSince."""
    import msvcrt

    _temp_only(temp, *(member.path for member in members))
    win = _win32()
    handle = win.k.CreateFileW(_long(temp), _GENERIC_WRITE | _DELETE, 0, None, _CREATE_NEW, _FILE_ATTRIBUTE_NORMAL,
                               None)
    if handle is None or handle == win.invalid:
        raise _last_error()

    def dispose(delete: bool) -> None:
        info = win.Disposition(delete)
        if not win.k.SetFileInformationByHandle(handle, _FILE_DISPOSITION_INFO, win.ctypes.byref(info),
                                                win.ctypes.sizeof(info)):
            raise _last_error()

    try:
        dispose(True)
        descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY)
    except BaseException:
        win.k.CloseHandle(handle)
        raise
    with os.fdopen(descriptor, "wb") as out:
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for member in members:
                _add_member(archive, member, cancel)
        out.flush()
        os.fsync(out.fileno())
        if cancel():
            raise Stopped()
        dispose(False)                                     # all written: keep it
    if zone:
        with open(_long(temp) + _ZONE_STREAM, "xb") as stream:
            stream.write(zone)


def raw_zone(path: str) -> bytes | None:
    """The file's Zone.Identifier stream (the mark of the web), or None when it has none."""
    _temp_only(path)
    try:
        with open(_long(path) + _ZONE_STREAM, "rb") as stream:
            return stream.read(_MAX_ZONE)
    except FileNotFoundError:
        return None


def raw_hash(path: str) -> str:
    """The sha256 of the file's content (hex)."""
    _temp_only(path)
    digest = hashlib.sha256()
    with open(_long(path), "rb") as source:
        while chunk := source.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def raw_free(path: str) -> int:
    """The bytes free on the volume of the folder ``path`` (GetDiskFreeSpaceExW, through shutil)."""
    import shutil

    _temp_only(path)
    return int(shutil.disk_usage(_long(path)).free)


# --------------------------------------------------------------------------
# Walking a folder before it is moved, copied or zipped
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SubtreeCaps:
    entries: int = 20_000               # entries looked at, at most
    depth: int = 32                     # levels below the folder (its own entries are level 1)
    seconds: float = 5.0                # time, at most


SUBTREE_CAPS = SubtreeCaps()


@dataclass
class Subtree:
    """What a folder holds (paths relative to it), or the reason it may not be moved, copied or zipped."""

    reason: str = ""
    folders: list[tuple[str, DirEntry]] = field(default_factory=list, repr=False)
    files: list[tuple[str, DirEntry]] = field(default_factory=list, repr=False)
    entries: int = 0
    bytes: int = 0
    online: int = 0                     # entries whose content is in the cloud


class _WalkStop(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _fold(name: str) -> str:
    return unicodedata.normalize("NFKC", guard.strip_hidden(name)).casefold().rstrip(". ")


def _key(path: str) -> str:
    return ntpath.normcase(path).rstrip("\\")


def _inside(key: str, folder_key: str) -> bool:
    return key == folder_key or key.startswith(folder_key + "\\")


def _link(entry: DirEntry) -> bool:
    return bool(entry.reparse_tag or entry.attributes & FILE_ATTRIBUTE_REPARSE_POINT) and not cloud_tag(
        entry.reparse_tag)


def _tree(folder: str, backend: DesktopBackend, caps: SubtreeCaps,
          clock: Callable[[], float]) -> Iterator[tuple[str, DirEntry]]:
    """Every entry below ``folder``, breadth-first, as (path relative to it, entry); a link or an online-only folder
    is never gone into. _WalkStop when a cap is reached or a folder can't be listed whole."""
    start = clock()
    seen = 0
    queue: deque[tuple[str, int]] = deque([("", 0)])
    while queue:
        relative, level = queue.popleft()
        if clock() - start > caps.seconds:
            raise _WalkStop(R_WALK_CAP)
        room = caps.entries - seen
        limit = min(MAX_LISTED, room + 1)
        try:
            entries = backend.list_folder(folder + "\\" + relative if relative else folder, limit=limit)
        except OSError:
            raise _WalkStop(R_UNREADABLE) from None
        if len(entries) >= MAX_LISTED:
            raise _WalkStop(R_WALK_CAP)                   # the listing may have been cut short
        for entry in entries:
            seen += 1
            if seen > caps.entries:
                raise _WalkStop(R_WALK_CAP)
            if not isinstance(entry.name, str) or not entry.name:
                raise _WalkStop(R_UNREADABLE)
            child = relative + "\\" + entry.name if relative else entry.name
            yield child, entry
            if entry.is_dir and not _link(entry) and not entry.attributes & _RECALLED:
                if level + 1 >= caps.depth:
                    raise _WalkStop(R_WALK_CAP)
                queue.append((child, level + 1))


def _entry_problem(entry: DirEntry, content: bool) -> str:
    folded = _fold(entry.name)
    if not folded:
        return R_UNREADABLE
    if folded in _CODE_MARKERS:
        return R_HOLDS_CODE
    if folded in guard.SECRET_DIR_NAMES:
        return R_HOLDS_SECRET_FOLDER
    if folded in guard.EXCLUDED_NAMES or paths.shell_class_name(entry.name):
        return R_HOLDS_EXCLUDED
    if not entry.is_dir and guard.secret_file(entry.name):
        return R_HOLDS_SECRET_FILE
    if _link(entry):
        return R_HOLDS_LINK
    if entry.attributes & _RECALLED and (entry.is_dir or content):
        return R_HOLDS_ONLINE_ONLY
    return ""


def walk_subtree(folder: str, scope: paths.Scope, backend: DesktopBackend, *, content: bool,
                 caps: SubtreeCaps = SUBTREE_CAPS, clock: Callable[[], float] = time.monotonic) -> Subtree:
    """Everything below ``folder`` (a checked final path), or why it may not be moved (``content`` False) or copied or
    zipped (``content`` True: no file of it may be online-only either). Refused: a root or an excluded place in it, a
    code marker, a secret folder, an excluded name or shell class name, a secret file, a link, an online-only folder,
    a folder that can't be listed, or a walk that reaches its caps (verified review note)."""
    result = Subtree()
    key = _key(folder)
    if any(_inside(_key(root.path), key) for root in scope.roots) \
            or any(_inside(_key(place), key) for place, _reason in scope.places):
        result.reason = R_HOLDS_PLACE
        return result
    try:
        for relative, entry in _tree(folder, backend, caps, clock):
            reason = _entry_problem(entry, content)
            if reason:
                result.reason = reason
                return result
            result.entries += 1
            if entry.attributes & _RECALLED:
                result.online += 1
            if entry.is_dir:
                result.folders.append((relative, entry))
            else:
                result.files.append((relative, entry))
                result.bytes += max(0, int(entry.size))
    except _WalkStop as stop:
        result.reason = stop.reason
    return result


def tree_digest(folder: str, backend: DesktopBackend, *, caps: SubtreeCaps = SUBTREE_CAPS,
                clock: Callable[[], float] = time.monotonic) -> str:
    """A folder's fingerprint (a folder copy's "hash"): sha256 over every entry below it - its path from the folder,
    whether it is a folder, a file's size and content hash - in name order. "" when it can't all be read (a link, an
    online-only entry, a cap): then Undo never recycles it. The walk's time cap does not apply (reading the content
    of a big copy takes as long as it takes); its entry and depth caps do."""
    lines: list[str] = []
    try:
        for relative, entry in _tree(folder, backend, replace(caps, seconds=float("inf")), clock):
            if _link(entry) or entry.attributes & _RECALLED:
                return ""
            if entry.is_dir:
                lines.append(f"d\0{ntpath.normcase(relative)}")
            else:
                content = backend.content_hash(folder + "\\" + relative)
                lines.append(f"f\0{ntpath.normcase(relative)}\0{int(entry.size)}\0{content}")
    except (_WalkStop, OSError):
        return ""
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8", "surrogatepass")).hexdigest()


def free_name(folder: str, name: str, backend: DesktopBackend, *, limit: int = 99,
              taken: Collection[str] = ()) -> str:
    """The name a card shows for a row whose name is taken in ``folder`` (hard rule 5: nothing is ever overwritten):
    ``name`` when it is free, else "name (2).ext", "name (3).ext" ...; "" when none up to ``limit`` is. ``taken``:
    names an earlier row of the same card already takes there (folded: ``fold_name``), free on the disk as yet."""
    stem, ext = ntpath.splitext(name)
    for number in range(1, max(1, limit) + 1):
        candidate = name if number == 1 else f"{stem} ({number}){ext}"
        if fold_name(candidate) in taken:
            continue
        if not backend.exists(folder.rstrip("\\") + "\\" + candidate):
            return candidate
    return ""


def fold_name(name: str) -> str:
    """A name as Windows compares it in one folder (case and the trailing dots and spaces it drops aside; NFKC)."""
    return _fold(name)


_never_a_name = paths.never_a_name


def _zone_id(data: bytes) -> int:
    found = _ZONE_RE.search(data or b"")
    return int(found.group(1)) if found else 3          # a mark with no zone reads as the internet's


# --------------------------------------------------------------------------
# The Recycle Bin seam (P2.6 brings the real one)
# --------------------------------------------------------------------------

class Recycler(Protocol):
    def recycle(self, path: str) -> None:
        """Send ``path`` to the Recycle Bin (never anywhere else). RecycleUnavailable when it can't."""
        ...


class NoRecycleBin:
    """The Recycle Bin until P2.6: it refuses cleanly, so an Undo that would recycle leaves the item as it is."""

    def recycle(self, path: str) -> None:
        raise RecycleUnavailable("the Recycle Bin arrives in a later part")


# --------------------------------------------------------------------------
# Carrying a card's rows out
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class FileOp:
    """One row of a card. ``sources``: the card's own items (context.FileItem / Place, with the identity they were
    shown with) - one for a move, rename or copy, one or more for a zip, none for a new folder. ``folder``: where the
    result goes (a Place with its identity, or the ``key`` of the row that makes it; None for a rename). ``name``:
    the new name (a rename, a new folder, a zip; for a move or a copy "" keeps the item's own, or the free name the
    card showed)."""

    kind: str
    sources: tuple[Any, ...] = field(default=(), repr=False)
    folder: Any = field(default=None, repr=False)
    name: str = field(default="", repr=False)
    key: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.sources, tuple):
            object.__setattr__(self, "sources", tuple(self.sources))


@dataclass(frozen=True)
class _Source:
    path: str = field(repr=False)                 # its final path now
    identity: FileIdentity = field(repr=False)
    attributes: int = 0
    verdict: paths.PathVerdict | None = field(default=None, repr=False)
    subtree: Subtree | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Check:
    """A row checked as it is now: ``reason`` "" when it may go ahead; where it goes, its sources and the card's
    flags."""

    reason: str = ""
    flags: frozenset[str] = frozenset()
    dst: str = field(default="", repr=False)
    folder: str = field(default="", repr=False)
    sources: tuple[_Source, ...] = field(default=(), repr=False)
    members: tuple[ZipMember, ...] = field(default=(), repr=False)
    bytes: int = 0

    @property
    def ok(self) -> bool:
        return not self.reason


@dataclass(frozen=True)
class OpResult:
    """What became of one row: its journal state (done, failed or skipped) and reason code, the card's flags, where
    the result is (``path``, ``identity``) and a part-made copy or zip left under its temporary name (``leftover``,
    for the owner to see; never removed here)."""

    seq: int
    kind: str
    state: str
    reason: str = ""
    winerror: int = 0
    flags: frozenset[str] = frozenset()
    path: str = field(default="", repr=False)
    identity: FileIdentity | None = field(default=None, repr=False)
    leftover: str = field(default="", repr=False)


@dataclass(frozen=True)
class BatchReport:
    run_id: str
    results: tuple[OpResult, ...] = ()

    def counts(self) -> dict[str, int]:
        found: dict[str, int] = {}
        for result in self.results:
            found[result.state] = found.get(result.state, 0) + 1
        return found

    def reasons(self) -> dict[str, int]:
        found: dict[str, int] = {}
        for result in self.results:
            if result.reason:
                found[result.reason] = found.get(result.reason, 0) + 1
        return found


def _count_text(counts: dict[str, int]) -> str:
    return ", ".join(f"{name}={number}" for name, number in sorted(counts.items())) or "-"


class FileOps:
    """Carries a card's rows out (see the module docstring). ``scope`` gives the scope as it is now (paths.Scope);
    ``cancel`` is the run's stop flag (Esc, Stop): asked before each row and during a copy or a zip."""

    def __init__(self, backend: DesktopBackend, journal: Journal, *, scope: Callable[[], paths.Scope],
                 cancel: Callable[[], bool] | None = None, clock: Callable[[], float] = time.monotonic,
                 caps: SubtreeCaps = SUBTREE_CAPS) -> None:
        self.backend = backend
        self.journal = journal
        self._scope = scope
        self._cancel = cancel or (lambda: False)
        self._clock = clock
        self._caps = caps
        self._made: dict[str, Place] = {}          # a mkdir row's key -> the folder it made

    # ---- checking a row ------------------------------------------------------------------------------

    def check(self, op: FileOp, *, pending: Mapping[str, tuple[Any, str]] | None = None) -> Check:
        """Every check of the row as things are now (nothing is changed): see the module docstring. ``pending`` (a
        card's check before anything is done, desktop.cardplan): the new folders earlier rows of the card will make,
        by their key -> (the place each is made in, its name); a row into one is checked against that place (the
        scope, the drive, OneDrive, the space) and nothing can be in it yet."""
        if not isinstance(op, FileOp) or op.kind not in OP_KINDS:
            return Check(R_SHAPE)
        wanted = {MKDIR: 0, ZIP: len(op.sources) or -1}.get(op.kind, 1)
        if len(op.sources) != wanted or (op.kind in (RENAME, MKDIR, ZIP) and not op.name) \
                or (op.kind == RENAME) != (op.folder is None):
            return Check(R_SHAPE)
        scope = self._scope()
        content = op.kind in (COPY, ZIP)
        sources: list[_Source] = []
        for item in op.sources:
            reason, source = self._source(item, scope, content=content, zipping=op.kind == ZIP)
            if reason:
                return Check(reason)
            sources.append(source)
        fresh = ""                                       # the place a new folder (not made yet) will be made in
        if op.kind == RENAME:
            reason, folder, folder_id, folder_flags = self._parent(sources[0].path, scope)
        elif isinstance(op.folder, str) and op.folder not in self._made and pending and op.folder in pending:
            parent, made_name = pending[op.folder]
            reason, fresh, folder_id, folder_flags = self._folder(parent, scope)
            folder = fresh.rstrip("\\") + "\\" + made_name if not reason else ""
        else:
            reason, folder, folder_id, folder_flags = self._folder(op.folder, scope)
        if reason:
            return Check(reason)
        name = op.name or ntpath.basename(sources[0].path)
        if op.name:
            reason = paths.name_problem(op.name) or (R_EXCLUDED if _never_a_name(op.name) else "")
            if reason:
                return Check(reason)
        if op.kind == ZIP and guard.suffix(name) != "zip":
            return Check(R_NAME)
        dst = folder.rstrip("\\") + "\\" + name
        if paths.lexical_problem(dst):
            return Check(R_NAME)
        case_only = False
        if op.kind in (MOVE, RENAME) and same_place(dst, sources[0].path):
            if op.kind != RENAME or dst == sources[0].path:
                return Check(R_SAME_PLACE)
            case_only = True                             # "notes.txt" to "Notes.txt": the same file, a new spelling
        if op.kind in (MOVE, COPY) and sources[0].identity.is_dir and _inside(_key(folder), _key(sources[0].path)):
            return Check(R_INTO_ITSELF)
        if not case_only and not fresh and self.backend.exists(dst):
            return Check(R_NAME_CLASH)
        if op.kind in (MOVE, RENAME) and sources[0].identity.volume != folder_id.volume:
            return Check(R_CROSS_VOLUME)
        members: tuple[ZipMember, ...] = ()
        size = 0
        if op.kind == ZIP:
            reason, members, size = _zip_members(sources)
            if reason:
                return Check(reason)
        elif op.kind == COPY:
            subtree = sources[0].subtree
            size = subtree.bytes if subtree is not None else sources[0].identity.size
        if content and size:
            try:
                free = self.backend.free_space(fresh or folder)
            except OSError:
                free = -1                                  # unknown: Windows itself refuses a copy that can't fit
            if free >= 0 and size > free * (1 - SPACE_MARGIN):
                return Check(R_NO_SPACE)
        flags = _flags(op.kind, sources, folder_flags)
        return Check("", frozenset(flags), dst, folder, tuple(sources), members, size)

    def _source(self, item: Any, scope: paths.Scope, *, content: bool, zipping: bool) -> tuple[str, Any]:
        """A card's source re-checked: the path check now, the same final path, the same identity; never a secret
        file, never a root; an online-only file never copied or zipped, a blocked type never zipped; a folder walked."""
        recorded, path = getattr(item, "identity", None), getattr(item, "path", None)
        if not isinstance(recorded, FileIdentity) or not isinstance(path, str):
            return R_CHANGED, None
        verdict = paths.check_path(path, scope, self.backend)
        if verdict.reason == paths.R_MISSING:
            return R_CHANGED, None
        if not verdict.ok:
            return verdict.code or R_OUTSIDE, None
        if not same_place(verdict.path, path):
            return R_CHANGED, None                         # it resolves somewhere else now
        if verdict.root is not None and same_place(verdict.path, verdict.root.path):
            return R_ROOT, None
        if not recorded.is_dir and guard.secret_file(verdict.path):
            return R_EXCLUDED, None
        try:
            now = self.backend.file_id(verdict.path)
            attributes = int(self.backend.file_info(verdict.path).attributes)
        except OSError:
            return R_CHANGED, None
        if not isinstance(now, FileIdentity) or now.is_dir != recorded.is_dir or not recorded.same_item(now):
            return R_CHANGED, None
        subtree = None
        if now.is_dir:
            if attributes & FILE_ATTRIBUTE_RECALL_ON_OPEN:
                return R_HOLDS_ONLINE_ONLY, None           # its listing is in the cloud: never looked into
            subtree = walk_subtree(verdict.path, scope, self.backend, content=content, caps=self._caps,
                                   clock=self._clock)
            if subtree.reason:
                return subtree.reason, None
        else:
            if content and attributes & _RECALLED:
                return R_ONLINE_ONLY, None
            if zipping and guard.blocked_type(verdict.path):
                return R_BLOCKED_TYPE, None
        return "", _Source(verdict.path, now, attributes, verdict, subtree)

    def _folder(self, target: Any, scope: paths.Scope) -> tuple[str, str, Any, frozenset[str]]:
        """The destination folder re-checked (a Place with its identity, or the key of the row that made it)."""
        if isinstance(target, str):
            made = self._made.get(target)
            if made is None:
                return R_NO_FOLDER, "", None, frozenset()
            target = made
        recorded, path = getattr(target, "identity", None), getattr(target, "path", None)
        if not isinstance(recorded, FileIdentity) or not recorded.is_dir or not isinstance(path, str):
            return R_CHANGED, "", None, frozenset()
        verdict = paths.check_path(path, scope, self.backend)
        if verdict.reason == paths.R_MISSING:
            return R_CHANGED, "", None, frozenset()
        if not verdict.ok:
            return verdict.code or R_OUTSIDE, "", None, frozenset()
        if not same_place(verdict.path, path):
            return R_CHANGED, "", None, frozenset()
        try:
            now = self.backend.file_id(verdict.path)
        except OSError:
            return R_CHANGED, "", None, frozenset()
        if not isinstance(now, FileIdentity) or not now.is_dir or not recorded.same_item(now):
            return R_CHANGED, "", None, frozenset()
        return "", verdict.path, now, verdict.flags

    def _parent(self, path: str, scope: paths.Scope) -> tuple[str, str, Any, frozenset[str]]:
        """A renamed item's own folder, checked as a place."""
        verdict = paths.check_path(ntpath.dirname(path), scope, self.backend)
        if not verdict.ok:
            return (R_CHANGED if verdict.reason == paths.R_MISSING else verdict.code or R_OUTSIDE), "", None, \
                frozenset()
        try:
            now = self.backend.file_id(verdict.path)
        except OSError:
            return R_CHANGED, "", None, frozenset()
        return "", verdict.path, now, verdict.flags

    # ---- carrying rows out ----------------------------------------------------------------------------

    def run(self, ops: Sequence[FileOp]) -> BatchReport:
        """The rows in order: each re-checked, journalled and done (or refused); after a stop none starts."""
        first = self.journal.next_seq()
        planned = [self._planned(first + number, op) for number, op in enumerate(ops)]
        try:
            self.journal.extend(planned)
        except JournalError:
            return BatchReport(self.journal.run_id, tuple(OpResult(record.seq, record.kind, SKIPPED, R_JOURNAL)
                                                          for record in planned))
        results: list[OpResult] = []
        halted = ""
        for number, op in enumerate(ops):
            seq = first + number
            if not halted and self._cancel():
                halted = R_STOPPED
            if halted:
                results.append(self._skip(seq, op, halted))
                continue
            result = self._carry_out(seq, op)
            if result.reason == R_JOURNAL:
                halted = R_JOURNAL                         # nothing more is done while the journal can't be saved
            results.append(result)
        report = BatchReport(self.journal.run_id, tuple(results))
        logger.info("Desktop files %s: %d row(s) (%s), reasons (%s)", self.journal.run_id, len(results),
                    _count_text(report.counts()), _count_text(report.reasons()))
        return report

    def _planned(self, seq: int, op: FileOp) -> Record:
        kind = op.kind if op.kind in OP_KINDS else MOVE
        source = op.sources[0] if len(op.sources) == 1 else None
        src = getattr(source, "path", "") if source is not None else ""
        folder = getattr(op.folder, "path", "") if op.folder is not None and not isinstance(op.folder, str) else ""
        if op.kind == RENAME and isinstance(src, str) and src:
            folder = ntpath.dirname(src)
        name = op.name or (ntpath.basename(src) if isinstance(src, str) else "")
        dst = folder.rstrip("\\") + "\\" + name if isinstance(folder, str) and folder and name else ""
        inputs = tuple(getattr(item, "path", "") for item in op.sources) if op.kind == ZIP else ()
        identity = getattr(source, "identity", None)
        record = Record(seq, kind, PLANNED, src=src if isinstance(src, str) and op.kind != ZIP else "", dst=dst,
                        inputs=tuple(path for path in inputs if isinstance(path, str)))
        return record.with_identity(identity if isinstance(identity, FileIdentity) else None)

    def _skip(self, seq: int, op: FileOp, reason: str, check: Check | None = None) -> OpResult:
        kind = op.kind if op.kind in OP_KINDS else MOVE
        flags = check.flags if check is not None else frozenset()
        self._save(Record(seq, kind, SKIPPED, dst=check.dst if check is not None else "", reason_code=reason,
                          flags=tuple(sorted(flags))))
        return OpResult(seq, kind, SKIPPED, reason, flags=flags)

    def _save(self, record: Record) -> bool:
        try:
            self.journal.append(record)
        except JournalError:
            return False                                   # a "doing" line stays: start-up recovery corrects it
        return True

    def _temp_name(self, folder: str, name: str) -> str:
        for _attempt in range(5):
            candidate = folder.rstrip("\\") + "\\" + f"{name}.{secrets.token_hex(4)}{TEMP_SUFFIX}"
            if not paths.lexical_problem(candidate) and not self.backend.exists(candidate):
                return candidate
        return ""

    def _carry_out(self, seq: int, op: FileOp) -> OpResult:
        check = self.check(op)
        if not check.ok:
            return self._skip(seq, op, check.reason, check)
        if self._cancel():
            return self._skip(seq, op, R_STOPPED, check)
        source = check.sources[0] if check.sources else None
        is_dir = op.kind == MKDIR or (source is not None and source.identity.is_dir and op.kind != ZIP)
        tmp = ""
        if op.kind == ZIP or (op.kind == COPY and is_dir):
            tmp = self._temp_name(check.folder, ntpath.basename(check.dst))
            if not tmp:
                return self._skip(seq, op, R_NAME, check)
        doing = Record(seq, op.kind, DOING, src=source.path if source is not None and op.kind != ZIP else "",
                       dst=check.dst, tmp=tmp, is_dir=is_dir, flags=tuple(sorted(check.flags)),
                       inputs=tuple(item.path for item in check.sources) if op.kind == ZIP else ())
        if source is not None and op.kind != ZIP:
            doing = doing.with_identity(source.identity, source.attributes)
        try:
            self.journal.append(doing)
        except JournalError:
            return OpResult(seq, op.kind, SKIPPED, R_JOURNAL, flags=check.flags)     # nothing done
        latest = [doing]
        try:
            digest = self._call(op, check, tmp, latest)
        except (OSError, Stopped, ChangedSince) as exc:
            reason = os_reason(exc)
            doing = latest[0]
            self._save(replace(doing, state=FAILED, reason_code=reason, at=0.0))
            logger.info("Desktop files %s: row %d %s failed: %s (%s %s)", self.journal.run_id, seq, op.kind, reason,
                        type(exc).__name__, _winerror(exc) or "")
            leftover = tmp if tmp and self._there(tmp) else ""
            return OpResult(seq, op.kind, FAILED, reason, _winerror(exc), check.flags, leftover=leftover)
        try:
            made = self.backend.file_id(check.dst)
        except OSError:
            made = None                                    # done, but not where Undo could find it: never put back
        doing = latest[0]
        self._save(replace(doing, state=DONE, sha256=digest, reason_code="", at=0.0).with_identity(made))
        if op.kind == MKDIR and op.key:
            self._made[op.key] = Place(check.dst, (), made)
        return OpResult(seq, op.kind, DONE, "", 0, check.flags, check.dst, made)

    def _there(self, path: str) -> bool:
        try:
            return bool(self.backend.exists(path))
        except OSError:
            return False

    def _written(self, latest: list[Record], tmp: str, digest: str) -> None:
        """A zip or a folder copy all written under its temporary name: its file id and hash are saved (write-ahead)
        before it is renamed into place, so start-up recovery finds it by its id - at its name or still at the
        temporary one - and never takes someone else's file for it."""
        if not digest:
            raise ChangedSince()                           # it can't be fingerprinted whole: never put in place
        record = replace(latest[0], sha256=digest, at=0.0).with_identity(self.backend.file_id(tmp))
        self.journal.append(record)                        # JournalError: not renamed into place
        latest[0] = record

    def _call(self, op: FileOp, check: Check, tmp: str, latest: list[Record]) -> str:
        """The row's OS call(s); the result's content hash for a copy or a zip ("" otherwise). ``latest`` holds the
        row's latest "doing" record."""
        backend, source = self.backend, (check.sources[0] if check.sources else None)
        if op.kind in (MOVE, RENAME):
            assert source is not None
            backend.move_item(source.path, check.dst)
            return ""
        if op.kind == MKDIR:
            backend.make_folder(check.dst)
            return ""
        if op.kind == ZIP:
            backend.write_zip(check.members, tmp, cancel=self._cancel, zone=self._zone(check.members))
            self._written(latest, tmp, backend.content_hash(tmp))
            backend.move_item(tmp, check.dst)
            return latest[0].sha256
        assert source is not None
        if not source.identity.is_dir:
            backend.copy_file(source.path, check.dst, cancel=self._cancel)
            try:
                return backend.content_hash(check.dst)
            except OSError:
                return ""                                  # copied; with no hash Undo leaves it be ("changed")
        subtree = source.subtree or Subtree()
        backend.make_folder(tmp)
        for relative, _entry in subtree.folders:
            if self._cancel():
                raise Stopped()
            backend.make_folder(tmp + "\\" + relative)
        for relative, _entry in subtree.files:
            if self._cancel():
                raise Stopped()
            backend.copy_file(source.path + "\\" + relative, tmp + "\\" + relative, cancel=self._cancel)
        if self._cancel():
            raise Stopped()
        self._written(latest, tmp, tree_digest(tmp, backend, caps=self._caps, clock=self._clock))
        backend.move_item(tmp, check.dst)
        return latest[0].sha256

    def _zone(self, members: Sequence[ZipMember]) -> bytes | None:
        """The Zone.Identifier the zip gets: the inputs' most restrictive one (the highest ZoneId), copied as it is."""
        chosen: bytes | None = None
        for member in members:
            if member.is_dir:
                continue
            data = self.backend.zone_identifier(member.path)
            if data and (chosen is None or _zone_id(data) > _zone_id(chosen)):
                chosen = data
        return chosen


def _zip_members(sources: Sequence[_Source]) -> tuple[str, tuple[ZipMember, ...], int]:
    """What a zip holds: each file under its own name, each folder with everything below it under the folder's name.
    Refused: a blocked type in it, two entries with one name, more than 1000 files or 4 GB."""
    members: list[ZipMember] = []
    for source in sources:
        top = ntpath.basename(source.path)
        if not source.identity.is_dir:
            members.append(ZipMember(source.path, top, False, source.identity.size, source.identity.mtime_ns))
            continue
        members.append(ZipMember(source.path, top, True, 0, source.identity.mtime_ns))
        subtree = source.subtree or Subtree()
        for relative, entry in subtree.folders:
            members.append(ZipMember(source.path + "\\" + relative, top + "\\" + relative, True, 0, entry.mtime_ns))
        for relative, entry in subtree.files:
            if guard.blocked_type(entry.name):
                return R_BLOCKED_TYPE, (), 0
            members.append(ZipMember(source.path + "\\" + relative, top + "\\" + relative, False, int(entry.size),
                                     entry.mtime_ns))
    names = [_fold(member.arcname.replace("/", "\\")) for member in members]
    if len(set(names)) != len(names):
        return R_NAME_CLASH, (), 0
    files = [member for member in members if not member.is_dir]
    size = sum(max(0, member.size) for member in files)
    if len(files) > MAX_ZIP_FILES or size > MAX_ZIP_BYTES:
        return R_CAP, (), 0
    return "", tuple(members), size


def _flags(kind: str, sources: Sequence[_Source], folder_flags: frozenset[str]) -> set[str]:
    """The card's row flags (SPEC 5.4)."""
    flags: set[str] = set()
    from_onedrive = any(source.verdict is not None and paths.FLAG_ONEDRIVE in source.verdict.flags
                        for source in sources)
    to_onedrive = paths.FLAG_ONEDRIVE in folder_flags
    if to_onedrive and (kind in (COPY, ZIP, MKDIR) or (kind == MOVE and not from_onedrive)):
        flags.add(FLAG_TO_ONEDRIVE)
    if kind == MOVE and from_onedrive and not to_onedrive:
        flags.add(FLAG_LEAVES_ONEDRIVE)
    for source in sources:
        if source.verdict is not None and FLAG_HIDDEN_CHARS in source.verdict.flags:
            flags.add(FLAG_HIDDEN_CHARS)
        if source.attributes & _RECALLED or (source.subtree is not None and source.subtree.online):
            flags.add(FLAG_ONLINE_ONLY)
        if source.attributes & FILE_ATTRIBUTE_RECALL_ON_OPEN:
            flags.add(FLAG_RECALL_ON_OPEN)
        if source.attributes & FILE_ATTRIBUTE_PINNED:
            flags.add(FLAG_PINNED)
    return flags


# --------------------------------------------------------------------------
# Undo (the owner's click; reverse order; per item)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class UndoResult:
    seq: int
    kind: str
    state: str                     # UNDONE or UNDO_REFUSED
    reason: str = ""
    winerror: int = 0

    @property
    def words(self) -> str:
        """The card's line for an item left as it is ("" when it was put back)."""
        return "" if self.state == UNDONE else UNDO_WORDS.get(self.reason, DEFAULT_UNDO_WORDS)


@dataclass(frozen=True)
class UndoReport:
    run_id: str
    results: tuple[UndoResult, ...] = ()

    @property
    def put_back(self) -> int:
        return sum(1 for result in self.results if result.state == UNDONE)

    @property
    def total(self) -> int:
        return len(self.results)

    def refused(self) -> dict[str, int]:
        found: dict[str, int] = {}
        for result in self.results:
            if result.state != UNDONE:
                found[result.reason] = found.get(result.reason, 0) + 1
        return found

    def stamp(self) -> str:
        """The card's stamp: "PUT BACK 4 OF 5 \u00b7 1 MOVED SINCE" (the reasons by count, most first)."""
        labels: dict[str, int] = {}
        for reason, count in self.refused().items():
            label = STAMP_LABELS.get(reason, STAMP_OTHER)
            labels[label] = labels.get(label, 0) + count
        parts = [f"PUT BACK {self.put_back} OF {self.total}"]
        parts += [f"{count} {label}" for label, count in sorted(labels.items(), key=lambda pair: (-pair[1], pair[0]))]
        return STAMP_DOT.join(parts)


_UNDOABLE = (DONE, UNDO_REFUSED)
_UNDO_ERRORS = {R_NAME_CLASH: R_OCCUPIED, R_CHANGED: R_MOVED_SINCE, R_STOPPED: R_OS}


class Undoer:
    """Puts a run's items back (see the module docstring). ``recycler``: the Recycle Bin seam (NoRecycleBin until
    P2.6); ``undo_days``: [desktop] undo_days (1-30); ``now``: seconds since the epoch; ``cancel``: the stop flag."""

    def __init__(self, backend: DesktopBackend, journal: Journal, *, scope: Callable[[], paths.Scope],
                 recycler: Recycler | None = None, undo_days: int = 7, now: Callable[[], float] = time.time,
                 cancel: Callable[[], bool] | None = None, caps: SubtreeCaps = SUBTREE_CAPS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.backend = backend
        self.journal = journal
        self._scope = scope
        self.recycler: Recycler = recycler if recycler is not None else NoRecycleBin()
        self.undo_days = min(30, max(1, int(undo_days)))
        self._now = now
        self._cancel = cancel or (lambda: False)
        self._caps = caps
        self._clock = clock

    def undoable(self) -> tuple[int, ...]:
        """The items Undo is offered for: done (or refused once) and within undo_days, newest row first."""
        return tuple(seq for seq, item in sorted(self.journal.items().items(), reverse=True)
                     if item.state in _UNDOABLE and item.done is not None and not self._expired(item.done))

    def undo(self, seqs: Iterable[int] | None = None) -> UndoReport:
        """Put the items back - all that may be (``seqs`` None) or those - in reverse order, each on its own."""
        known = self.journal.items()
        chosen = sorted(set(seqs) if seqs is not None else
                        {seq for seq, item in known.items() if item.state in _UNDOABLE}, reverse=True)
        results: list[UndoResult] = []
        for seq in chosen:
            item = known.get(seq)
            if item is None or item.state not in _UNDOABLE or item.done is None:
                results.append(UndoResult(seq, item.kind if item else "", UNDO_REFUSED, R_NOT_UNDOABLE))
                continue
            if self._cancel():
                results.append(UndoResult(seq, item.kind, UNDO_REFUSED, R_STOPPED))
                continue
            results.append(self._undo_item(item))
        report = UndoReport(self.journal.run_id, tuple(results))
        logger.info("Desktop undo %s: put back %d of %d (%s)", self.journal.run_id, report.put_back, report.total,
                    _count_text(report.refused()))
        return report

    def _expired(self, done: Record) -> bool:
        return self._now() - done.at > self.undo_days * _DAY_S

    def _undo_item(self, item: Item) -> UndoResult:
        done = item.done
        assert done is not None
        if self._expired(done):
            return self._refuse(item, done, R_EXPIRED)
        if item.kind in (MOVE, RENAME):
            return self._put_back(item, done)
        if item.kind == MKDIR:
            return self._remove_made_folder(item, done)
        if item.kind in (COPY, ZIP, SCREENSHOT):
            return self._recycle_output(item, done)
        return self._refuse(item, done, R_NOT_YET)

    def _refuse(self, item: Item, done: Record, reason: str, winerror: int = 0) -> UndoResult:
        try:
            self.journal.append(replace(done, state=UNDO_REFUSED, undo=True, reason_code=reason, at=0.0))
        except JournalError:
            pass
        return UndoResult(item.seq, item.kind, UNDO_REFUSED, reason, winerror)

    def _where(self, done: Record, scope: paths.Scope) -> tuple[str, paths.PathVerdict | None, Any]:
        """Jarvis's result, re-checked where the journal says it is: the same file (volume and file id)."""
        verdict = paths.check_path(done.dst, scope, self.backend)
        if verdict.reason == paths.R_MISSING:
            return R_MOVED_SINCE, None, None
        if not verdict.ok:
            return verdict.code or R_OUTSIDE, None, None
        if not same_place(verdict.path, done.dst):
            return R_MOVED_SINCE, None, None
        try:
            now = self.backend.file_id(verdict.path)
        except OSError:
            return R_MOVED_SINCE, None, None
        if not isinstance(now, FileIdentity) or (now.volume, now.file_id) != (done.volume_serial, done.file_id):
            return R_MOVED_SINCE, None, None
        return "", verdict, now

    def _act(self, item: Item, done: Record, call: Callable[[], None]) -> UndoResult:
        """Write-ahead, then the one OS call, then the result (hard rule 12)."""
        doing = replace(done, state=DOING, undo=True, reason_code="", at=0.0)
        try:
            self.journal.append(doing)
        except JournalError:
            return UndoResult(item.seq, item.kind, UNDO_REFUSED, R_JOURNAL)        # nothing done
        try:
            call()
        except RecycleUnavailable:
            reason, winerror = R_NO_BIN, 0
        except OSError as exc:
            reason = os_reason(exc)
            reason, winerror = _UNDO_ERRORS.get(reason, reason), _winerror(exc)
            logger.info("Desktop undo %s: row %d %s not put back: %s (%s %s)", self.journal.run_id, item.seq,
                        item.kind, reason, type(exc).__name__, winerror or "")
        else:
            self._save(replace(doing, state=UNDONE, at=0.0))
            return UndoResult(item.seq, item.kind, UNDONE)
        self._save(replace(doing, state=UNDO_REFUSED, reason_code=reason, at=0.0))
        return UndoResult(item.seq, item.kind, UNDO_REFUSED, reason, winerror)

    def _save(self, record: Record) -> None:
        try:
            self.journal.append(record)
        except JournalError:
            pass                                              # recovery corrects the "doing" line at the next start

    def _put_back(self, item: Item, done: Record) -> UndoResult:
        """A move or a rename: the same file still where Jarvis put it, its old place free, on the same volume."""
        scope = self._scope()
        reason, verdict, now = self._where(done, scope)
        if reason:
            return self._refuse(item, done, reason)
        here, old = verdict.path, done.src
        if not old or paths.lexical_problem(old):
            return self._refuse(item, done, R_OLD_PLACE_GONE)
        parent = paths.check_path(ntpath.dirname(old), scope, self.backend)
        if parent.reason == paths.R_MISSING:
            return self._refuse(item, done, R_OLD_PLACE_GONE)
        if not parent.ok:
            return self._refuse(item, done, parent.code or R_OUTSIDE)
        back = parent.path.rstrip("\\") + "\\" + ntpath.basename(old)
        if not same_place(back, here) and self.backend.exists(back):
            return self._refuse(item, done, R_OCCUPIED)
        try:
            folder = self.backend.file_id(parent.path)
        except OSError:
            return self._refuse(item, done, R_OLD_PLACE_GONE)
        if folder.volume != now.volume:
            return self._refuse(item, done, R_CROSS_VOLUME)
        if now.is_dir:
            walk = walk_subtree(here, scope, self.backend, content=False, caps=self._caps, clock=self._clock)
            if walk.reason:
                return self._refuse(item, done, walk.reason)
        return self._act(item, done, lambda: self.backend.move_item(here, back))

    def _remove_made_folder(self, item: Item, done: Record) -> UndoResult:
        """A folder Jarvis made: removed only while it is the same folder and empty."""
        reason, verdict, _now = self._where(done, self._scope())
        if reason:
            return self._refuse(item, done, reason)
        try:
            inside = self.backend.list_folder(verdict.path, limit=1)
        except OSError as exc:
            return self._refuse(item, done, os_reason(exc), _winerror(exc))
        if inside:
            return self._refuse(item, done, R_NOT_EMPTY)
        return self._act(item, done, lambda: self.backend.remove_folder(verdict.path))

    def _recycle_output(self, item: Item, done: Record) -> UndoResult:
        """A copy, a zip or a screenshot: recycled only while it is Jarvis's own output, unchanged (its hash), and
        on this PC (an online-only file would skip the Recycle Bin)."""
        scope = self._scope()
        reason, verdict, now = self._where(done, scope)
        if reason:
            return self._refuse(item, done, reason)
        if FLAG_ONLINE_ONLY in verdict.flags:
            return self._refuse(item, done, R_ONLINE_ONLY)
        try:
            if now.is_dir:
                walk = walk_subtree(verdict.path, scope, self.backend, content=True, caps=self._caps,
                                    clock=self._clock)
                if walk.reason:
                    return self._refuse(item, done, R_ONLINE_ONLY if walk.reason == R_HOLDS_ONLINE_ONLY
                                        else walk.reason)
                digest = tree_digest(verdict.path, self.backend, caps=self._caps, clock=self._clock)
            else:
                digest = self.backend.content_hash(verdict.path)
        except OSError as exc:
            return self._refuse(item, done, os_reason(exc), _winerror(exc))
        if not done.sha256 or digest != done.sha256:
            return self._refuse(item, done, R_CHANGED)
        return self._act(item, done, lambda: self.recycler.recycle(verdict.path))
