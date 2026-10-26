"""The desktop backend: every OS call the desktop feature makes goes through one (SPEC sections 5, 13 and 15; no Qt).

``DesktopBackend`` is what a backend offers. The app uses ``Win32Backend`` (ctypes and pywin32: the Windows shell and
the Windows Search index); tests use tests/desktop_fakes.FakeDesktopBackend, which records every call and works only
inside a temporary folder. While the test guard is on (``TEST_GUARD_ENV`` = "1", set by the tests package before any
test runs) constructing the real backend raises ``RealBackendInTests`` - and so does EVERY method of it that touches
the OS (verified review note, 2026-10-21): a real backend made before the guard went on still reaches nothing. Every
public method of ``Win32Backend`` is marked by ``_real`` (its ``reaches_os`` attribute), which checks the guard first.

The methods:

* Facts for the path check (SPEC 5.2 and 5.3): ``known_folder`` (SHGetKnownFolderPath: follows the OneDrive
  redirection), ``final_path`` (realpath(strict=True), then GetFinalPathNameByHandleW on an opened handle),
  ``file_info`` (lstat: a link is not followed), ``exists``, ``drive_type`` (GetDriveTypeW) and ``file_id`` (the
  file's identity: the volume serial and the 128-bit file id CPython reads from FILE_ID_INFO, its size and mtime).
* Looking (P1.8: desktop.index and desktop.apps decide what may be listed): ``list_folder`` (one folder's entries
  from its directory listing - nothing in it is opened, a link is never followed), ``search_index`` (one Windows
  Search SQL query that desktop.index built and scoped to the roots, with a timeout and a row cap),
  ``recent_items`` (the targets the newest Recent items' shortcuts name, read with IShellLink and never resolved)
  and ``start_menu_apps`` (the Start menu's apps: shell:AppsFolder's names and ids).
* Opening (P1.8; SPEC 2.1, 2.3 and 5.5): ``open_document`` (ShellExecuteExW "open", for a document type of SPEC 2.3
  only), ``show_in_folder`` and ``open_folder`` (SHOpenFolderAndSelectItems: never a command line), ``launch_app``
  (an AppsFolder item, with no arguments) and ``open_url`` (an https address on a public host only). P1.9 adds
  ``open_script_document`` (the same open, for a document that can run macros or scripts only - desktop.executor asks
  for it only after the owner's click on that file's NEEDS YOUR OK card). desktop.executor re-checks every target just
  before; each open is guarded here once more (hard rule 6) by the NEVER list's own rules: a path that is not a plain
  local path on a fixed drive, a type that is not of that open's kind, an app id or an address that is not of its
  shape raises ValueError and nothing is asked of Windows. No open passes arguments, asks for another verb than
  "open" or turns the zone checks off.
* For the result cards (P1.9): ``app_for`` - the app Windows opens a file type or the https protocol with (its
  friendly name and its exe, from the associations only: AssocQueryStringW; nothing is opened or read) - and
  ``link_target`` - the path a shortcut names (IShellLink Load and GetPath, never Resolve: nothing is searched or
  started), so its card can show the real target before anything opens (SPEC 2.4).

No method is ever named for something on the NEVER list (guard.NEVER_CAPABILITIES): there is no delete, no close, no
shell command, no send. Logs carry folder keys, counts and exception types (with a winerror) only, never a path, a
name, a title or an address.

Public API
==========
``DesktopBackend`` (Protocol): ``known_folder(name)``, ``final_path(path)``, ``file_info(path)``, ``exists(path)``,
``drive_type(drive)``, ``file_id(path)``, ``list_folder(path, limit=)``, ``search_index(sql, timeout_s=,
max_rows=)``, ``recent_items(limit=)``, ``start_menu_apps(limit=)``, ``open_document(path)``,
``open_script_document(path)``, ``show_in_folder(path)``, ``open_folder(path)``, ``launch_app(app_id)``,
``open_url(url)``, ``app_for(kind)``, ``link_target(path)``.
``FileInfo(attributes, reparse_tag, is_dir)``, ``FileIdentity(volume, file_id, size, mtime_ns, is_dir)``
(``same_item(other)``), ``DirEntry(name, is_dir, attributes, reparse_tag, size, mtime_ns)``, ``IndexRow(path, name,
size, modified, attributes, is_dir)``, ``AppEntry(app_id, name)``, ``AppInfo(name, exe)``; ``app_kind(kind)`` (what
``app_for`` may be asked); ``KNOWN_FOLDERS`` (name -> KNOWNFOLDERID),
``ROOT_FOLDERS`` (the six folders the owner chose: D2); the FILE_ATTRIBUTE_*, IO_REPARSE_TAG_* and DRIVE_* values the
checks use; ``link_tag(tag)`` (a symbolic link, a junction or an app execution alias), ``cloud_tag(tag)`` (OneDrive).
``Win32Backend()``; ``RealBackendInTests``; ``TEST_GUARD_ENV``, ``real_backend_blocked()``; ``APPS_FOLDER`` (the
AppsFolder's parsing prefix an app id is launched under); ``INDEX_COLUMNS`` (what a Windows Search query selects);
``open_problem(method, target)`` (the opens' last guard, pure); ``MAX_LISTED``.
"""

from __future__ import annotations

import functools
import logging
import ntpath
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, TypeVar

from . import guard

logger = logging.getLogger(__name__)

TEST_GUARD_ENV = "JARVIS_DESKTOP_TEST_GUARD"     # "1": the real backend raises (the tests set it)

# KNOWNFOLDERID values (shlobj.h) by the names config.toml and the checks use.
KNOWN_FOLDERS: dict[str, str] = {
    "desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "music": "4BD8D571-6D19-48D3-BE97-422220080E43",
    "videos": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
    "profile": "5E6C858F-0E22-4760-9AFE-EA3317B67173",
    "onedrive": "A52BBA46-E9E1-435F-B3D9-28DAA648C0F6",
    "local_appdata": "F1B32785-6FBA-4FCF-9D55-7B8E7F157091",
    "roaming_appdata": "3EB685DB-65F9-4CF6-A03A-E3EF65729F3D",
    "windows": "F38BF404-1D43-42F2-9305-67DE0B28FC23",
    "program_files": "905E63B6-C1BF-494E-B29C-65B732D3D21A",
    "program_files_x86": "7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E",
    "program_data": "62AB5D82-FDC1-4DC3-A9DD-070D1D495D97",
    "recent": "AE50C081-EBD2-438A-8655-8A092E34987A",     # the Recent items' shortcuts (never a place)
}
# The folders Jarvis may look in by default (owner decision D2), in [desktop] folders' order.
ROOT_FOLDERS = ("desktop", "documents", "downloads", "pictures", "music", "videos")
APPS_FOLDER = "shell:AppsFolder\\"              # an app is launched as this + its AppsFolder id (SPEC 5.5)

FILE_ATTRIBUTE_READONLY = 0x1
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003         # a junction (or a mounted volume)
IO_REPARSE_TAG_SYMLINK = 0xA000000C
IO_REPARSE_TAG_APPEXECLINK = 0x8000001B         # an app execution alias (WindowsApps)
IO_REPARSE_TAG_CLOUD = 0x9000001A               # OneDrive files: IO_REPARSE_TAG_CLOUD_0 .. _F share this pattern
IO_REPARSE_TAG_CLOUD_MASK = 0xFFFF0FFF
LINK_TAGS = frozenset({IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_APPEXECLINK})
DRIVE_UNKNOWN, DRIVE_NO_ROOT_DIR, DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_REMOTE, DRIVE_CDROM, DRIVE_RAMDISK = range(7)

# The shell's open (ShellExecuteExW): synchronous, no error box of its own (the caption says it), the zone checks
# left on (SEE_MASK_NOZONECHECKS is never set), shown normally.
SEE_MASK_NOASYNC = 0x00000100
SEE_MASK_FLAG_NO_UI = 0x00000400
SW_SHOWNORMAL = 1
OPEN_VERB = "open"
_COINIT_APARTMENTTHREADED = 0x2
_COINIT_DISABLE_OLE1DDE = 0x4
# The Windows Search index through its OLE DB provider (read only); the columns a query selects (desktop.index).
_INDEX_PROVIDER = "Provider=Search.CollatorDSO;Extended Properties='Application=Windows';"
INDEX_COLUMNS = ("System.ItemPathDisplay", "System.FileName", "System.Size", "System.DateModified",
                 "System.FileAttributes")
MAX_LISTED = 10_000                             # entries of one folder a listing reads at most
# The associations (AssocQueryStringW): the open verb's app, by its friendly name and its exe.
_ASSOCF_INIT_IGNOREUNKNOWN = 0x00000400
_ASSOCF_IS_PROTOCOL = 0x00001000
_ASSOCSTR_EXECUTABLE = 2
_ASSOCSTR_FRIENDLYAPPNAME = 4
_KIND_RE = re.compile(r"\.[A-Za-z0-9]{1,16}")


class RealBackendInTests(RuntimeError):
    """The real backend was constructed, or one of its methods called, while the test guard is on."""


def real_backend_blocked() -> bool:
    return os.environ.get(TEST_GUARD_ENV, "") == "1"


_F = TypeVar("_F", bound=Callable[..., Any])


def _real(method: _F) -> _F:
    """An OS-touching method of the real backend: it raises RealBackendInTests while the test guard is on, before
    anything else happens (``reaches_os`` marks it; the tests check every public method has it)."""
    @functools.wraps(method)
    def guarded(self: Any, *args: Any, **kwargs: Any) -> Any:
        if real_backend_blocked():
            raise RealBackendInTests(f"Win32Backend.{method.__name__} never runs while the tests run")
        return method(self, *args, **kwargs)

    guarded.reaches_os = True                     # type: ignore[attr-defined]
    return guarded                                # type: ignore[return-value]


# --------------------------------------------------------------------------
# What a backend reports
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class FileInfo:
    """What lstat says about one path component, without following it."""

    attributes: int
    reparse_tag: int = 0
    is_dir: bool = False

    @property
    def hidden_system(self) -> bool:
        both = FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
        return self.attributes & both == both

    @property
    def online_only(self) -> bool:
        """Its content lives in the cloud: opening or reading it downloads it."""
        return bool(self.attributes & (FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | FILE_ATTRIBUTE_RECALL_ON_OPEN))


@dataclass(frozen=True)
class FileIdentity:
    """A file's or folder's identity (SPEC 5.3): its volume's serial number, its 128-bit file id, its size and when it
    last changed. Recorded when Jarvis lists an item and compared right before each OS call on it."""

    volume: int
    file_id: int
    size: int = 0
    mtime_ns: int = 0
    is_dir: bool = False

    def same_item(self, other: Any) -> bool:
        """The same file, unchanged: the same volume and file id and, for a file, the same size and mtime (a folder's
        size and mtime change with what is in it, so only where it is counts)."""
        if not isinstance(other, FileIdentity) or (self.volume, self.file_id) != (other.volume, other.file_id):
            return False
        if self.is_dir or other.is_dir:
            return self.is_dir == other.is_dir
        return (self.size, self.mtime_ns) == (other.size, other.mtime_ns)

    def __repr__(self) -> str:                      # never logged; only whether it is a folder
        return f"FileIdentity(dir={self.is_dir})"


@dataclass(frozen=True)
class DirEntry:
    """One entry of a folder's listing, as the listing reports it (nothing followed or opened)."""

    name: str = field(repr=False)
    is_dir: bool = False
    attributes: int = 0
    reparse_tag: int = 0
    size: int = 0
    mtime_ns: int = 0


@dataclass(frozen=True)
class IndexRow:
    """One row of a Windows Search query: the item's path and name as the index has them (desktop.index checks the
    path before anything is listed), its size, when it last changed and its attributes."""

    path: str = field(repr=False)
    name: str = field(default="", repr=False)
    size: int = -1
    modified: datetime | None = field(default=None, repr=False)
    attributes: int = 0
    is_dir: bool = False


@dataclass(frozen=True)
class AppEntry:
    """One app of the Start menu (shell:AppsFolder): its id (an AUMID, or a known folder's id and the exe's path
    under it) and its name as the Start menu shows it."""

    app_id: str = field(repr=False)
    name: str = field(repr=False)


@dataclass(frozen=True)
class AppInfo:
    """The app Windows opens a file type (or the https protocol) with: its friendly name ("Word"; "" unknown) and its
    exe's path ("" unknown). Shown on a result card and used to find its window - never logged."""

    name: str = field(default="", repr=False)
    exe: str = field(default="", repr=False)


def app_kind(kind: Any) -> bool:
    """What ``app_for`` may be asked: a file type (".docx") or "https" - never a path or a name."""
    return isinstance(kind, str) and (kind == "https" or bool(_KIND_RE.fullmatch(kind)))


def link_tag(tag: int) -> bool:
    """A symbolic link, a junction (mount point) or an app execution alias: never followed or used."""
    return int(tag) in LINK_TAGS


def cloud_tag(tag: int) -> bool:
    """One of OneDrive's cloud file tags (IO_REPARSE_TAG_CLOUD_0 .. _F): allowed."""
    return int(tag) & IO_REPARSE_TAG_CLOUD_MASK == IO_REPARSE_TAG_CLOUD


class DesktopBackend(Protocol):
    def known_folder(self, name: str) -> str | None:
        """The path of a known folder (KNOWN_FOLDERS), as Windows reports it; None when there is none."""
        ...

    def final_path(self, path: str) -> str:
        """The path Windows opens for ``path``: realpath(strict=True), then GetFinalPathNameByHandleW on an opened
        handle (8.3 names and junctions resolved; it may come back as \\\\?\\C:\\...). OSError when it is missing."""
        ...

    def file_info(self, path: str) -> FileInfo:
        """lstat facts of ``path`` itself (a link is not followed). OSError when it is missing."""
        ...

    def exists(self, path: str) -> bool:
        """Whether ``path`` exists (a link counts, even a broken one; it is not followed)."""
        ...

    def drive_type(self, drive: str) -> int:
        """GetDriveTypeW of a drive root ("C:\\"): DRIVE_FIXED, DRIVE_REMOVABLE, DRIVE_REMOTE ..."""
        ...

    def file_id(self, path: str) -> FileIdentity:
        """The identity of ``path`` itself (not followed; SPEC 5.3). OSError when it is missing."""
        ...

    def list_folder(self, path: str, *, limit: int = MAX_LISTED) -> list[DirEntry]:
        """The entries of the folder ``path`` (at most ``limit``), from its listing: no entry is opened or followed.
        OSError when it cannot be listed."""
        ...

    def search_index(self, sql: str, *, timeout_s: float, max_rows: int) -> list[IndexRow]:
        """The rows of one Windows Search SQL query (desktop.index.build_query), at most ``max_rows``, within about
        ``timeout_s`` seconds. OSError (or the provider's own error) when the index cannot answer."""
        ...

    def recent_items(self, *, limit: int = 50) -> list[str]:
        """The paths the newest ``limit`` Recent items' shortcuts point at, newest first (read, never resolved)."""
        ...

    def start_menu_apps(self, *, limit: int = 2000) -> list[AppEntry]:
        """The Start menu's apps (shell:AppsFolder), at most ``limit``."""
        ...

    def open_document(self, path: str) -> None:
        """Open the document ``path`` with its app (ShellExecuteExW "open"; SPEC 2.3's types only)."""
        ...

    def open_script_document(self, path: str) -> None:
        """Open a document that can run macros or scripts (guard.SCRIPT_CAPABLE_TYPES only) with its app - asked for
        only after the owner's click on its NEEDS YOUR OK card."""
        ...

    def show_in_folder(self, path: str) -> None:
        """Show ``path`` selected in its folder (SHOpenFolderAndSelectItems)."""
        ...

    def open_folder(self, path: str) -> None:
        """Open the folder ``path`` itself (SHOpenFolderAndSelectItems)."""
        ...

    def launch_app(self, app_id: str) -> None:
        """Start the Start-menu app ``app_id`` (shell:AppsFolder\\<id>, no arguments)."""
        ...

    def open_url(self, url: str) -> None:
        """Open the https address ``url`` in the default browser."""
        ...

    def app_for(self, kind: str) -> AppInfo | None:
        """The app Windows opens ``kind`` with - a file type (".docx") or "https" (the default browser) -, from the
        associations only (nothing is opened or read); None when Windows names none, or for anything else."""
        ...

    def link_target(self, path: str) -> str:
        """The path the shortcut ``path`` (a .lnk on a fixed drive) names, read without resolving it - nothing is
        searched or started; "" when it names no path. OSError when it can't be read."""
        ...


# --------------------------------------------------------------------------
# The real backend
# --------------------------------------------------------------------------

class Win32Backend:
    """The real backend (Windows); see the module docstring. Every public method raises RealBackendInTests while the
    test guard is on."""

    def __init__(self) -> None:
        if real_backend_blocked():
            raise RealBackendInTests("the real desktop backend is never constructed while the tests run")

    # ---- facts ----------------------------------------------------------------------------------------

    @_real
    def known_folder(self, name: str) -> str | None:
        folder_id = KNOWN_FOLDERS.get(name)
        if folder_id is None:
            return None
        try:
            return _shell_known_folder(folder_id)
        except OSError as exc:
            logger.info("Desktop: no %s folder (%s %s)", name, type(exc).__name__, getattr(exc, "winerror", ""))
            return None

    @_real
    def final_path(self, path: str) -> str:
        resolved = os.path.realpath(path, strict=True)
        return _final_path_name(resolved)

    @_real
    def file_info(self, path: str) -> FileInfo:
        info = os.lstat(path)
        attributes = int(getattr(info, "st_file_attributes", 0))
        tag = int(getattr(info, "st_reparse_tag", 0)) if attributes & FILE_ATTRIBUTE_REPARSE_POINT else 0
        is_dir = stat.S_ISDIR(info.st_mode) or bool(attributes & FILE_ATTRIBUTE_DIRECTORY)
        return FileInfo(attributes, tag, is_dir)

    @_real
    def exists(self, path: str) -> bool:
        return os.path.lexists(path)

    @_real
    def drive_type(self, drive: str) -> int:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_type = kernel32.GetDriveTypeW
        get_type.argtypes = [wintypes.LPCWSTR]
        get_type.restype = wintypes.UINT
        return int(get_type(drive))

    @_real
    def file_id(self, path: str) -> FileIdentity:
        # CPython 3.12+ reads FILE_ID_INFO here: st_ino is the 128-bit file id, st_dev the volume's serial number.
        info = os.lstat(path)
        return FileIdentity(int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns),
                            stat.S_ISDIR(info.st_mode))

    # ---- looking ----------------------------------------------------------------------------------------

    @_real
    def list_folder(self, path: str, *, limit: int = MAX_LISTED) -> list[DirEntry]:
        entries: list[DirEntry] = []
        with os.scandir(path) as listing:
            for entry in listing:
                if len(entries) >= limit:
                    break
                try:
                    info = entry.stat(follow_symlinks=False)    # the listing's own facts: nothing is opened
                except OSError:
                    continue
                attributes = int(getattr(info, "st_file_attributes", 0))
                tag = int(getattr(info, "st_reparse_tag", 0)) if attributes & FILE_ATTRIBUTE_REPARSE_POINT else 0
                entries.append(DirEntry(entry.name, bool(attributes & FILE_ATTRIBUTE_DIRECTORY), attributes, tag,
                                        int(info.st_size), int(info.st_mtime_ns)))
        return entries

    @_real
    def search_index(self, sql: str, *, timeout_s: float, max_rows: int) -> list[IndexRow]:
        _com_ready()
        import win32com.client

        seconds = max(1, int(timeout_s + 0.999))
        deadline = time.monotonic() + max(0.1, float(timeout_s))
        connection = win32com.client.Dispatch("ADODB.Connection")
        connection.ConnectionTimeout = seconds
        connection.CommandTimeout = seconds
        connection.Open(_INDEX_PROVIDER)
        rows: list[IndexRow] = []
        try:
            found = connection.Execute(sql)
            records = found[0] if isinstance(found, tuple) else found
            try:
                while not records.EOF and len(rows) < max_rows and time.monotonic() < deadline:
                    fields = records.Fields
                    values = {name: fields.Item(name).Value for name in INDEX_COLUMNS}
                    rows.append(_index_row(values))
                    records.MoveNext()
            finally:
                records.Close()
        finally:
            connection.Close()
        return rows

    @_real
    def recent_items(self, *, limit: int = 50) -> list[str]:
        folder = _shell_known_folder(KNOWN_FOLDERS["recent"])
        if not folder:
            return []
        links: list[tuple[int, str]] = []
        with os.scandir(folder) as listing:
            for entry in listing:
                if entry.name.casefold().endswith(".lnk") and entry.is_file(follow_symlinks=False):
                    links.append((int(entry.stat(follow_symlinks=False).st_mtime_ns), entry.path))
        links.sort(reverse=True)
        _com_ready()
        import pythoncom
        from win32com.shell import shell

        targets: list[str] = []
        for _changed, link_path in links[:max(0, int(limit))]:
            try:
                link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER,
                                                  shell.IID_IShellLink)
                link.QueryInterface(pythoncom.IID_IPersistFile).Load(link_path, 0)   # STGM_READ; never Resolve()
                target, _found = link.GetPath(0)
            except Exception as exc:  # noqa: BLE001 - one unreadable shortcut is skipped
                logger.debug("Desktop: a Recent item could not be read (%s)", type(exc).__name__)
                continue
            if isinstance(target, str) and target:
                targets.append(target)
        return targets

    @_real
    def start_menu_apps(self, *, limit: int = 2000) -> list[AppEntry]:
        _com_ready()
        import win32com.client

        folder = win32com.client.Dispatch("Shell.Application").NameSpace(APPS_FOLDER.rstrip("\\"))
        if folder is None:
            return []
        items = folder.Items()
        entries: list[AppEntry] = []
        for index in range(min(int(items.Count), max(0, int(limit)))):
            item = items.Item(index)
            name, app_id = str(item.Name or ""), str(item.Path or "")
            if name and app_id:
                entries.append(AppEntry(app_id, name))
        return entries

    # ---- opening ----------------------------------------------------------------------------------------

    @_real
    def open_document(self, path: str) -> None:
        self._last_guard("open_document", path)
        _shell_open(path)

    @_real
    def open_script_document(self, path: str) -> None:
        self._last_guard("open_script_document", path)
        _shell_open(path)

    @_real
    def show_in_folder(self, path: str) -> None:
        self._last_guard("show_in_folder", path)
        _com_ready()
        from win32com.shell import shell

        folder, _attributes = shell.SHParseDisplayName(ntpath.dirname(path.rstrip("\\")), 0)
        item, _attributes = shell.SHParseDisplayName(path, 0)
        shell.SHOpenFolderAndSelectItems(folder, (item,), 0)    # its folder opens with it selected

    @_real
    def open_folder(self, path: str) -> None:
        self._last_guard("open_folder", path)
        _com_ready()
        from win32com.shell import shell

        folder, _attributes = shell.SHParseDisplayName(path, 0)
        # The folder with itself as the one item: the folder itself opens and nothing in it is selected (an id that
        # is not one of its children selects nothing). [UNCERTAIN: checked with the owner present.]
        shell.SHOpenFolderAndSelectItems(folder, (folder,), 0)

    @_real
    def launch_app(self, app_id: str) -> None:
        self._last_guard("launch_app", app_id)
        _shell_open(APPS_FOLDER + app_id)

    @_real
    def open_url(self, url: str) -> None:
        self._last_guard("open_url", url)
        _shell_open(url)

    # ---- for the result cards ----------------------------------------------------------------------------

    @_real
    def app_for(self, kind: str) -> AppInfo | None:
        if not app_kind(kind):
            return None
        name = _assoc_string(kind, _ASSOCSTR_FRIENDLYAPPNAME)
        exe = _assoc_string(kind, _ASSOCSTR_EXECUTABLE)
        if exe and _exe_problem(exe):
            exe = ""                                 # not a plain exe path (a packaged app's alias ...): no window match
        return AppInfo(name, exe) if name or exe else None

    @_real
    def link_target(self, path: str) -> str:
        self._last_guard("link_target", path)
        return _link_path(path)

    def _last_guard(self, method: str, target: str) -> None:
        """The last guard before the shell is asked (``open_problem``, and a path's drive a fixed one): ValueError
        and nothing is asked of Windows."""
        if open_problem(method, target):
            raise ValueError("refused by the last guard")
        if method in _PATH_OPENS and self.drive_type(target.replace("/", "\\")[:3]) != DRIVE_FIXED:
            raise ValueError("refused by the last guard")


_PATH_OPENS = frozenset({"open_document", "open_script_document", "show_in_folder", "open_folder", "link_target"})
# Each path method's own kind of file (guard.open_kind): anything else is refused before Windows is asked.
_PATH_KINDS = {"open_document": guard.OPEN_DOCUMENT, "open_script_document": guard.OPEN_SCRIPT,
               "link_target": guard.OPEN_SHORTCUT}


def _exe_problem(exe: str) -> str:
    """Why an association's exe is no plain exe path to find a window by ("" when it is one)."""
    from .paths import lexical_problem                # late: paths imports this module

    return lexical_problem(exe) or ("type" if guard.suffix(exe) != "exe" else "")


def open_problem(method: str, target: Any) -> str:
    """Why an open may not ask the shell for ``target`` ("" when it may; pure - the open methods' last guard, made
    again after desktop.executor's re-check): a path that is not a plain absolute local one (paths.lexical_problem),
    a file that is not of the method's kind (guard.open_kind: open_document a document that opens at once,
    open_script_document one that can run macros or scripts, link_target a shortcut), an app id not of an AppsFolder
    id's shape (desktop.apps.app_id_problem), an address that is not https on a public host (guard.url_problem), or
    an unknown method."""
    from .apps import app_id_problem
    from .paths import lexical_problem

    if not isinstance(target, str) or not target:
        return "not_text"
    if method in _PATH_OPENS:
        problem = lexical_problem(target)
        if problem:
            return problem
        kind = _PATH_KINDS.get(method)
        if kind is not None and guard.open_kind(target) != kind:
            return "type"
        return ""
    if method == "launch_app":
        return app_id_problem(target)
    if method == "open_url":
        return guard.url_problem(target)
    return "method"


# --------------------------------------------------------------------------
# Windows helpers (only ever reached through Win32Backend's guarded methods)
# --------------------------------------------------------------------------

_com = threading.local()


def _com_ready() -> None:
    """COM on this thread (the desktop worker's), apartment-threaded as the shell wants it; once per thread."""
    if getattr(_com, "ready", False):
        return
    import pythoncom

    try:
        pythoncom.CoInitializeEx(_COINIT_APARTMENTTHREADED | _COINIT_DISABLE_OLE1DDE)
    except pythoncom.com_error as exc:     # already set up on this thread in another model: it is usable as it is
        logger.debug("Desktop: COM was set up on this thread already (%s)", type(exc).__name__)
    _com.ready = True


def _shell_open(target: str) -> None:
    """ShellExecuteExW with the "open" verb, no arguments, no working folder, synchronous and with no error box; its
    failure as OSError with the winerror only (the message never names the target)."""
    _com_ready()
    import pywintypes
    from win32com.shell import shell

    try:
        shell.ShellExecuteEx(fMask=SEE_MASK_NOASYNC | SEE_MASK_FLAG_NO_UI, lpVerb=OPEN_VERB, lpFile=target,
                             nShow=SW_SHOWNORMAL)
    except pywintypes.error as exc:
        raise OSError(0, "the shell could not open it", None, int(getattr(exc, "winerror", 0) or 0)) from None


def _assoc_string(kind: str, what: int) -> str:
    """AssocQueryStringW for the "open" verb of a file type (".docx") or of the https protocol: the app's friendly
    name or its exe; "" when Windows has none (unknown types are not answered with "Unknown application")."""
    import ctypes
    from ctypes import wintypes

    shlwapi = ctypes.WinDLL("shlwapi")
    query = shlwapi.AssocQueryStringW
    query.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPWSTR,
                      ctypes.POINTER(wintypes.DWORD)]
    query.restype = ctypes.c_long                    # an HRESULT, checked by hand
    flags = _ASSOCF_INIT_IGNOREUNKNOWN | (_ASSOCF_IS_PROTOCOL if kind == "https" else 0)
    size = wintypes.DWORD(1024)
    buffer = ctypes.create_unicode_buffer(size.value)
    if query(flags, what, kind, OPEN_VERB, buffer, ctypes.byref(size)) != 0:
        return ""
    return buffer.value.strip()


def _link_path(path: str) -> str:
    """The path a shortcut names: IShellLink's Load (read only) and GetPath - never Resolve(), which could search the
    disk or the network for a moved target."""
    _com_ready()
    import pythoncom
    from win32com.shell import shell

    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    try:
        link.QueryInterface(pythoncom.IID_IPersistFile).Load(path, 0)      # STGM_READ
        target, _found = link.GetPath(0)
    except pythoncom.com_error as exc:
        raise OSError(0, "the shortcut could not be read", None, int(getattr(exc, "hresult", 0) or 0)) from None
    return target if isinstance(target, str) else ""


def _index_row(values: dict[str, Any]) -> IndexRow:
    path = values.get("System.ItemPathDisplay")
    name = values.get("System.FileName")
    size = values.get("System.Size")
    modified = values.get("System.DateModified")
    attributes = values.get("System.FileAttributes")
    attributes = int(attributes) if isinstance(attributes, int) else 0
    return IndexRow(path if isinstance(path, str) else "", name if isinstance(name, str) else "",
                    int(size) if isinstance(size, int) else -1,
                    modified if isinstance(modified, datetime) else None, attributes,
                    bool(attributes & FILE_ATTRIBUTE_DIRECTORY))


def _final_path_name(path: str) -> str:
    """GetFinalPathNameByHandleW on a handle opened for its attributes only (no content is read or recalled)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                       wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    final = kernel32.GetFinalPathNameByHandleW
    final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    final.restype = wintypes.DWORD
    close = kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    file_read_attributes, share_all, open_existing, backup_semantics = 0x80, 0x7, 3, 0x02000000
    handle = create(path, file_read_attributes, share_all, None, open_existing, backup_semantics, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = 512
        while True:
            buffer = ctypes.create_unicode_buffer(size)
            length = final(handle, buffer, size, 0)
            if not length:
                raise ctypes.WinError(ctypes.get_last_error())
            if length < size:
                return buffer.value
            size = length + 1
    finally:
        close(handle)


def _shell_known_folder(folder_id: str) -> str | None:
    """SHGetKnownFolderPath(folder_id, KF_FLAG_DEFAULT): the path, or None when Windows gives an empty one."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    value = uuid.UUID(folder_id)
    guid = GUID(value.fields[0], value.fields[1], value.fields[2], (ctypes.c_ubyte * 8)(*value.bytes[8:]))
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    get_path = shell32.SHGetKnownFolderPath
    get_path.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
    get_path.restype = ctypes.HRESULT                # a failure HRESULT raises OSError
    free = ole32.CoTaskMemFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    result = ctypes.c_wchar_p()
    try:
        get_path(ctypes.byref(guid), 0, None, ctypes.byref(result))
        return result.value or None
    finally:
        free(ctypes.cast(result, ctypes.c_void_p))
