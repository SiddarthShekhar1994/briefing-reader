"""Fakes for the desktop feature's tests: a backend over a temporary folder that records every call, and the
stand-ins Phase 1 needs (an index, a window watcher, a clock). None of them reaches the real desktop: the real
backend cannot even be constructed while the tests run (tests/__init__.py sets backend.TEST_GUARD_ENV).

FakeDesktopBackend keeps a pretend disk inside ``base`` (a TemporaryDirectory): the pretend C: drive is
``base\\C``, D: is ``base\\D`` and so on. Paths going in and coming out are pretend ones
("C:\\Users\\Owner\\OneDrive\\Documents\\Essay.docx"), so the checks see paths shaped like the owner's while every
file operation stays inside the temporary folder. Real junctions can be made in it (``junction``: _winapi), 8.3
names are pretended (``alias``), and attributes, reparse tags and drive types can be added (``set_attributes``,
``set_tag``, ``set_drive``). A path that is not a pretend drive path (a network path, a device path) raises
OSError, and is still recorded in ``calls`` - so a test can show it was never asked for.
"""

from __future__ import annotations

import _winapi
import ntpath
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from briefing_reader.desktop import backend as dbk

OWNER = "C:\\Users\\Owner"
KNOWN = {
    "profile": OWNER,
    "desktop": OWNER + "\\OneDrive\\Desktop",
    "documents": OWNER + "\\OneDrive\\Documents",
    "downloads": OWNER + "\\Downloads",
    "pictures": OWNER + "\\OneDrive\\Pictures",
    "music": OWNER + "\\Music",
    "videos": OWNER + "\\Videos",
    "onedrive": OWNER + "\\OneDrive",
    "local_appdata": OWNER + "\\AppData\\Local",
    "roaming_appdata": OWNER + "\\AppData\\Roaming",
    "windows": "C:\\Windows",
    "program_files": "C:\\Program Files",
    "program_files_x86": "C:\\Program Files (x86)",
    "program_data": "C:\\ProgramData",
}


class FakeDesktopBackend:
    """See the module docstring. ``calls``: (method, argument) of every DesktopBackend call, in order."""

    def __init__(self, base: str | Path, *, known: dict[str, str] | None = None) -> None:
        self.base = os.path.realpath(str(base))
        self.calls: list[tuple[str, str]] = []
        self.known = dict(KNOWN if known is None else known)
        self.aliases: dict[str, str] = {}          # normcased pretend path -> the pretend path it stands for
        self.attributes: dict[str, int] = {}       # normcased pretend path -> attribute bits added
        self.tags: dict[str, int] = {}             # normcased pretend path -> the reparse tag reported
        self.drive_types: dict[str, int] = {}      # "D:\\" -> DRIVE_REMOVABLE ...
        for path in self.known.values():
            self.make_dir(path)

    # ---- the pretend disk ----------------------------------------------------------------------

    def _unalias(self, path: str) -> str:
        text = path.replace("/", "\\")
        for _ in range(16):
            key = ntpath.normcase(text)
            for short, long in self.aliases.items():
                if key == short or key.startswith(short + "\\"):
                    text = long + text[len(short):]
                    break
            else:
                return text
        return text

    def real(self, path: str) -> str:
        """The real path of a pretend one (after its 8.3 aliases); OSError for anything else."""
        text = self._unalias(path)
        if len(text) < 3 or text[1:3] != ":\\" or not text[0].isalpha() or text.startswith("\\\\"):
            raise OSError(2, "not a pretend drive path")
        return os.path.join(self.base, text[0].upper(), text[3:])

    def pretend(self, real: str) -> str:
        """The pretend path of a real one inside ``base`` (a path outside it comes back as it is)."""
        key, base = ntpath.normcase(real), ntpath.normcase(self.base)
        if not key.startswith(base + "\\"):
            return real
        rest = real[len(self.base) + 1:]
        drive, _sep, tail = rest.partition("\\")
        return f"{drive}:\\{tail}"

    def make_dir(self, path: str) -> str:
        os.makedirs(self.real(path), exist_ok=True)
        return path

    def make_file(self, path: str, data: bytes = b"x") -> str:
        real = self.real(path)
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, "wb") as handle:
            handle.write(data)
        return path

    def junction(self, link: str, target: str) -> str:
        """A real junction inside the temporary folder (no privilege needed)."""
        os.makedirs(os.path.dirname(self.real(link)), exist_ok=True)
        _winapi.CreateJunction(self.real(target), self.real(link))
        return link

    def alias(self, short: str, long: str) -> str:
        """``short`` (an 8.3 name such as C:\\PROGRA~1) stands for ``long``."""
        self.aliases[ntpath.normcase(short)] = long
        return short

    def set_attributes(self, path: str, bits: int) -> None:
        self.attributes[ntpath.normcase(path)] = bits

    def set_tag(self, path: str, tag: int) -> None:
        self.tags[ntpath.normcase(path)] = tag

    def set_drive(self, drive: str, kind: int) -> None:
        self.drive_types[drive.upper()] = kind

    def asked(self, method: str) -> list[str]:
        return [argument for name, argument in self.calls if name == method]

    # ---- DesktopBackend ------------------------------------------------------------------------

    def known_folder(self, name: str) -> str | None:
        self.calls.append(("known_folder", name))
        return self.known.get(name)

    def final_path(self, path: str) -> str:
        self.calls.append(("final_path", path))
        resolved = os.path.realpath(self.real(path), strict=True)
        return "\\\\?\\" + self.pretend(resolved)     # as GetFinalPathNameByHandleW returns it

    def file_info(self, path: str) -> dbk.FileInfo:
        self.calls.append(("file_info", path))
        info = os.lstat(self.real(path))
        key = ntpath.normcase(self._unalias(path))
        attributes = getattr(info, "st_file_attributes", 0) | self.attributes.get(key, 0)
        tag = self.tags.get(key, getattr(info, "st_reparse_tag", 0))
        if tag:
            attributes |= dbk.FILE_ATTRIBUTE_REPARSE_POINT
        is_dir = stat.S_ISDIR(info.st_mode) or bool(attributes & dbk.FILE_ATTRIBUTE_DIRECTORY)
        return dbk.FileInfo(attributes, tag, is_dir)

    def exists(self, path: str) -> bool:
        self.calls.append(("exists", path))
        try:
            return os.path.lexists(self.real(path))
        except OSError:
            return False

    def drive_type(self, drive: str) -> int:
        self.calls.append(("drive_type", drive))
        return self.drive_types.get(drive.upper(), dbk.DRIVE_FIXED)


class FakeClock:
    """A wall clock and a monotonic clock the test moves (``advance``; ``sleep`` advances too)."""

    def __init__(self, start: datetime | None = None) -> None:
        self.wall = start or datetime(2026, 10, 20, 10, 21)
        self.mono = 1000.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


@dataclass(frozen=True)
class FakeEntry:
    """One file or folder the fake index knows (P1.8's index entry takes its place)."""

    path: str
    name: str
    kind: str = "document"
    size: int = 0
    modified: datetime | None = None


class FakeIndex:
    """A search index stand-in (until P1.8 builds desktop/index.py): ``search(words)`` returns the entries whose
    name holds every word (casefolded); every query is recorded in ``queries``."""

    def __init__(self, entries: list[FakeEntry] | None = None) -> None:
        self.entries = list(entries or [])
        self.queries: list[tuple[str, ...]] = []

    def search(self, words: list[str] | tuple[str, ...], limit: int = 200) -> list[FakeEntry]:
        self.queries.append(tuple(words))
        folded = [word.casefold() for word in words]
        return [entry for entry in self.entries if all(word in entry.name.casefold() for word in folded)][:limit]


class FakeWinWatch:
    """A window watcher stand-in (until P1.9 builds desktop/winwatch.py): windows are (hwnd, pid, exe, rect) the test
    sets in ``windows``; ``follow(hwnd, callback)`` registers a callback the test fires with ``emit(hwnd, event)``.
    Nothing reaches a real window."""

    def __init__(self) -> None:
        self.windows: dict[int, dict[str, Any]] = {}
        self.followed: dict[int, Callable[[str], None]] = {}
        self.calls: list[tuple[str, Any]] = []

    def add(self, hwnd: int, pid: int, exe: str, rect: tuple[int, int, int, int]) -> None:
        self.windows[hwnd] = {"pid": pid, "exe": exe, "rect": rect}

    def find(self, *, pid: int | None = None, exe: str | None = None) -> list[int]:
        self.calls.append(("find", (pid, exe)))
        return [hwnd for hwnd, info in self.windows.items()
                if (pid is None or info["pid"] == pid) and (exe is None or info["exe"].casefold() == exe.casefold())]

    def rect(self, hwnd: int) -> tuple[int, int, int, int] | None:
        self.calls.append(("rect", hwnd))
        info = self.windows.get(hwnd)
        return info["rect"] if info else None

    def follow(self, hwnd: int, callback: Callable[[str], None]) -> None:
        self.calls.append(("follow", hwnd))
        self.followed[hwnd] = callback

    def unfollow(self, hwnd: int) -> None:
        self.calls.append(("unfollow", hwnd))
        self.followed.pop(hwnd, None)

    def emit(self, hwnd: int, event: str) -> None:
        callback = self.followed.get(hwnd)
        if callback is not None:
            callback(event)
