"""The desktop backend: every OS call the desktop feature makes goes through one (SPEC sections 13 and 15; no Qt).

``DesktopBackend`` is what a backend offers. The app uses ``Win32Backend`` (ctypes, the Windows shell); tests use
tests/desktop_fakes.FakeDesktopBackend, which records every call and works only inside a temporary folder. While
the test guard is on (``TEST_GUARD_ENV`` = "1", set by the tests package before any test runs) constructing the
real backend raises ``RealBackendInTests``, so no test and no harness run can reach the real desktop by mistake.

The backend grows part by part: this part (Phase 1, part 2) has the read-only facts the path check needs. In
``Win32Backend`` only ``known_folder`` works so far (SHGetKnownFolderPath, which follows the OneDrive redirection);
every other method raises NotImplementedError until the part that builds it (P1.8: the final path, lstat facts,
drive types and the open methods). No method is ever named for something on the NEVER list
(guard.NEVER_CAPABILITIES): there is no delete, no close, no shell, no send.

Logs carry folder keys and exception types only, never a path.

Public API
==========
``DesktopBackend`` (Protocol): ``known_folder(name) -> str | None``, ``final_path(path) -> str``,
``file_info(path) -> FileInfo``, ``exists(path) -> bool``, ``drive_type(drive) -> int``.
``FileInfo(attributes, reparse_tag, is_dir)``; ``KNOWN_FOLDERS`` (name -> KNOWNFOLDERID), ``ROOT_FOLDERS`` (the six
folders the owner chose: D2); the FILE_ATTRIBUTE_*, IO_REPARSE_TAG_* and DRIVE_* values the checks use;
``link_tag(tag)`` (a symbolic link, a junction or an app execution alias), ``cloud_tag(tag)`` (OneDrive).
``Win32Backend()``; ``RealBackendInTests``; ``TEST_GUARD_ENV``, ``real_backend_blocked()``.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

TEST_GUARD_ENV = "JARVIS_DESKTOP_TEST_GUARD"     # "1": constructing the real backend raises (the tests set it)

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
}
# The folders Jarvis may look in by default (owner decision D2), in [desktop] folders' order.
ROOT_FOLDERS = ("desktop", "documents", "downloads", "pictures", "music", "videos")

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


class RealBackendInTests(RuntimeError):
    """The real backend was constructed while the test guard is on."""


def real_backend_blocked() -> bool:
    return os.environ.get(TEST_GUARD_ENV, "") == "1"


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


class Win32Backend:
    """The real backend (Windows). See the module docstring: only ``known_folder`` is built so far."""

    def __init__(self) -> None:
        if real_backend_blocked():
            raise RealBackendInTests("the real desktop backend is never constructed while the tests run")

    def known_folder(self, name: str) -> str | None:
        folder_id = KNOWN_FOLDERS.get(name)
        if folder_id is None:
            return None
        try:
            return _shell_known_folder(folder_id)
        except OSError as exc:
            logger.info("Desktop: no %s folder (%s %s)", name, type(exc).__name__, getattr(exc, "winerror", ""))
            return None

    def final_path(self, path: str) -> str:
        raise NotImplementedError("Win32Backend.final_path comes with P1.8")

    def file_info(self, path: str) -> FileInfo:
        raise NotImplementedError("Win32Backend.file_info comes with P1.8")

    def exists(self, path: str) -> bool:
        raise NotImplementedError("Win32Backend.exists comes with P1.8")

    def drive_type(self, drive: str) -> int:
        raise NotImplementedError("Win32Backend.drive_type comes with P1.8")


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
