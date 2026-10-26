"""Fakes for the desktop feature's tests: a backend over a temporary folder that records every call, and the
stand-ins Phase 1 needs (an index, the window seam desktop.winwatch uses - ``FakeWinApi`` over pretend windows -, a
clock); ``tags(text)`` spells text in invisible tag characters. None of them reaches the real desktop: the real
backend and the real window seam cannot even be constructed while the tests run (tests/__init__.py sets
backend.TEST_GUARD_ENV).

FakeDesktopBackend keeps a pretend disk inside ``base`` (a TemporaryDirectory): the pretend C: drive is
``base\\C``, D: is ``base\\D`` and so on. Paths going in and coming out are pretend ones
("C:\\Users\\Owner\\OneDrive\\Documents\\Essay.docx"), so the checks see paths shaped like the owner's while every
file operation stays inside the temporary folder. Real junctions can be made in it (``junction``: _winapi), 8.3
names are pretended (``alias``), and attributes, reparse tags and drive types can be added (``set_attributes``,
``set_tag``, ``set_drive``). A path that is not a pretend drive path (a network path, a device path) raises
OSError, and is still recorded in ``calls`` - so a test can show it was never asked for.

P1.8's methods: ``list_folder`` lists the pretend disk (or a ``listings`` entry: a made-up folder of any size, for
the walk's caps), ``search_index`` answers ``index_rows`` (None: there is no index - OSError, so the walk runs; the
SQL asked is kept in ``queries``), ``recent_items`` answers ``recent`` and ``start_menu_apps`` answers ``apps``. The
opens (``open_document``, ``open_script_document``, ``show_in_folder``, ``open_folder``, ``launch_app``,
``open_url``) open nothing: each is recorded in ``opened`` (method, target), or raises ``open_errors[method]``.
P1.9's: ``app_for`` answers ``associations`` (a file type or "https" -> backend.AppInfo; Word for .docx, Excel for
.xlsx and Edge for https by default, invented install paths) and ``link_target`` answers ``links`` (a shortcut's
pretend path -> the pretend path it names; OSError for one that is not there). Every call's thread name is kept in
``threads`` (the desktop worker's "desktop" thread in the app).
"""

from __future__ import annotations

import _winapi
import ntpath
import os
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from briefing_reader.desktop import backend as dbk

OWNER = "C:\\Users\\Owner"
WORD_EXE = "C:\\Program Files\\Microsoft Office\\root\\Office16\\WINWORD.EXE"     # invented install paths
EXCEL_EXE = "C:\\Program Files\\Microsoft Office\\root\\Office16\\EXCEL.EXE"
EDGE_EXE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"
ASSOCIATIONS = {".docx": dbk.AppInfo("Word", WORD_EXE), ".docm": dbk.AppInfo("Word", WORD_EXE),
                ".xlsx": dbk.AppInfo("Excel", EXCEL_EXE), ".xlsm": dbk.AppInfo("Excel", EXCEL_EXE),
                ".pdf": dbk.AppInfo("Microsoft Edge", EDGE_EXE), "https": dbk.AppInfo("Microsoft Edge", EDGE_EXE)}
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


def tags(text: str) -> str:
    """``text`` (ASCII) spelled in invisible Unicode tag characters (U+E0020-E007E), as a hostile name may hide it."""
    return "".join(chr(0xE0000 + ord(char)) for char in text)


class FakeDesktopBackend:
    """See the module docstring. ``calls``: (method, argument) of every DesktopBackend call, in order."""

    def __init__(self, base: str | Path, *, known: dict[str, str] | None = None) -> None:
        self.base = os.path.realpath(str(base))
        self.calls: list[tuple[str, str]] = []
        self.threads: list[str] = []               # the thread of every call, in order
        self.known = dict(KNOWN if known is None else known)
        self.aliases: dict[str, str] = {}          # normcased pretend path -> the pretend path it stands for
        self.attributes: dict[str, int] = {}       # normcased pretend path -> attribute bits added
        self.tags: dict[str, int] = {}             # normcased pretend path -> the reparse tag reported
        self.drive_types: dict[str, int] = {}      # "D:\\" -> DRIVE_REMOVABLE ...
        self.listings: dict[str, list[dbk.DirEntry]] = {}   # normcased pretend folder -> a made-up listing
        self.index_rows: list[dbk.IndexRow] | None = None   # what the index answers (None: no index, OSError)
        self.queries: list[str] = []               # the SQL the index was asked
        self.recent: list[str] = []                # the Recent items' targets (pretend paths, newest first)
        self.apps: list[dbk.AppEntry] = []         # the Start menu
        self.opened: list[tuple[str, str]] = []    # (open method, target) of every open asked for
        self.open_errors: dict[str, BaseException] = {}   # an open method -> what it raises
        self.associations: dict[str, dbk.AppInfo] = dict(ASSOCIATIONS)    # a type or "https" -> its app
        self.links: dict[str, str] = {}            # normcased pretend shortcut path -> the pretend path it names
        self.on_call: Callable[[str, str], None] | None = None   # called before each method (a test's hook)
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

    def _record(self, method: str, argument: str) -> None:
        if self.on_call is not None:
            self.on_call(method, argument)
        self.calls.append((method, argument))
        self.threads.append(threading.current_thread().name)

    def set_listing(self, folder: str, entries: list[dbk.DirEntry]) -> None:
        """``folder`` lists as ``entries`` (made up: nothing is on the pretend disk for them)."""
        self.listings[ntpath.normcase(folder)] = list(entries)

    def identity(self, path: str) -> dbk.FileIdentity:
        """What file_id says of ``path`` now - for a test's own setup (not recorded as a call)."""
        info = os.lstat(self.real(path))
        return dbk.FileIdentity(int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns),
                                stat.S_ISDIR(info.st_mode))

    def opened_methods(self) -> list[str]:
        return [method for method, _target in self.opened]

    # ---- DesktopBackend ------------------------------------------------------------------------

    def known_folder(self, name: str) -> str | None:
        self._record("known_folder", name)
        return self.known.get(name)

    def final_path(self, path: str) -> str:
        self._record("final_path", path)
        resolved = os.path.realpath(self.real(path), strict=True)
        return "\\\\?\\" + self.pretend(resolved)     # as GetFinalPathNameByHandleW returns it

    def file_info(self, path: str) -> dbk.FileInfo:
        self._record("file_info", path)
        info = os.lstat(self.real(path))
        key = ntpath.normcase(self._unalias(path))
        attributes = getattr(info, "st_file_attributes", 0) | self.attributes.get(key, 0)
        tag = self.tags.get(key, getattr(info, "st_reparse_tag", 0))
        if tag:
            attributes |= dbk.FILE_ATTRIBUTE_REPARSE_POINT
        is_dir = stat.S_ISDIR(info.st_mode) or bool(attributes & dbk.FILE_ATTRIBUTE_DIRECTORY)
        return dbk.FileInfo(attributes, tag, is_dir)

    def exists(self, path: str) -> bool:
        self._record("exists", path)
        try:
            return os.path.lexists(self.real(path))
        except OSError:
            return False

    def drive_type(self, drive: str) -> int:
        self._record("drive_type", drive)
        return self.drive_types.get(drive.upper(), dbk.DRIVE_FIXED)

    def file_id(self, path: str) -> dbk.FileIdentity:
        self._record("file_id", path)
        info = os.lstat(self.real(path))
        return dbk.FileIdentity(int(info.st_dev), int(info.st_ino), int(info.st_size), int(info.st_mtime_ns),
                                stat.S_ISDIR(info.st_mode))

    def list_folder(self, path: str, *, limit: int = dbk.MAX_LISTED) -> list[dbk.DirEntry]:
        self._record("list_folder", path)
        made_up = self.listings.get(ntpath.normcase(self._unalias(path)))
        if made_up is not None:
            return list(made_up)[:limit]
        entries: list[dbk.DirEntry] = []
        folder = self._unalias(path).rstrip("\\")
        with os.scandir(self.real(path)) as listing:
            for entry in listing:
                info = entry.stat(follow_symlinks=False)
                key = ntpath.normcase(folder + "\\" + entry.name)
                attributes = getattr(info, "st_file_attributes", 0) | self.attributes.get(key, 0)
                tag = self.tags.get(key, getattr(info, "st_reparse_tag", 0))
                if tag:
                    attributes |= dbk.FILE_ATTRIBUTE_REPARSE_POINT
                entries.append(dbk.DirEntry(entry.name, bool(attributes & dbk.FILE_ATTRIBUTE_DIRECTORY), attributes,
                                            tag, int(info.st_size), int(info.st_mtime_ns)))
        return entries[:limit]

    def search_index(self, sql: str, *, timeout_s: float, max_rows: int) -> list[dbk.IndexRow]:
        self._record("search_index", sql)
        self.queries.append(sql)
        if self.index_rows is None:
            raise OSError(2, "no search index here")
        return list(self.index_rows)[:max_rows]

    def recent_items(self, *, limit: int = 50) -> list[str]:
        self._record("recent_items", str(limit))
        return list(self.recent)[:limit]

    def start_menu_apps(self, *, limit: int = 2000) -> list[dbk.AppEntry]:
        self._record("start_menu_apps", str(limit))
        return list(self.apps)[:limit]

    def _open(self, method: str, target: str) -> None:
        self._record(method, target)
        error = self.open_errors.get(method)
        if error is not None:
            raise error
        self.opened.append((method, target))

    def open_document(self, path: str) -> None:
        self._open("open_document", path)

    def open_script_document(self, path: str) -> None:
        self._open("open_script_document", path)

    def show_in_folder(self, path: str) -> None:
        self._open("show_in_folder", path)

    def open_folder(self, path: str) -> None:
        self._open("open_folder", path)

    def launch_app(self, app_id: str) -> None:
        self._open("launch_app", app_id)

    def open_url(self, url: str) -> None:
        self._open("open_url", url)

    def app_for(self, kind: str) -> dbk.AppInfo | None:
        self._record("app_for", kind)
        return self.associations.get(kind) if dbk.app_kind(kind) else None

    def link_target(self, path: str) -> str:
        self._record("link_target", path)
        target = self.links.get(ntpath.normcase(self._unalias(path)))
        if target is None or not os.path.lexists(self.real(path)):
            raise OSError(2, "no such shortcut")
        return target


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


def pretend_path(*parts: str) -> str:
    """A pretend path under the pretend owner's folder (never a real one): pretend_path("Downloads", "a.pdf")."""
    return "\\".join((OWNER, *parts))


DOCS = pretend_path("OneDrive", "Documents")
UNI = pretend_path("OneDrive", "Documents", "Uni")
LING = pretend_path("OneDrive", "Documents", "Uni", "LING")
DOWNLOADS = pretend_path("Downloads")


START_MENU = [dbk.AppEntry("Microsoft.Office.WINWORD.EXE.15", "Microsoft Word"),
              dbk.AppEntry("SpotifyAB.SpotifyMusic", "Spotify"),
              dbk.AppEntry("{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe",
                           "Windows PowerShell"),
              dbk.AppEntry("Microsoft.WindowsTerminal_8wekyb3d8bbwe!App", "Terminal"),
              dbk.AppEntry("windows.immersivecontrolpanel_cw5n1h2txyewy!microsoft.windows.immersivecontrolpanel",
                           "Settings"),
              dbk.AppEntry("Microsoft.WindowsCalculator_8wekyb3d8bbwe!App", "Calculator")]


def desktop_disk(base: str | Path) -> FakeDesktopBackend:
    """A fake backend whose pretend disk holds desktop_context()'s invented places and files (Documents > Uni > LING,
    Downloads ...) and whose Start menu lists Word, Spotify, PowerShell, Terminal, Settings and Calculator (the
    never-open ones are left out by desktop.apps). Nothing real: every file is inside ``base``."""
    fake = FakeDesktopBackend(base)
    for place in (DOCS, UNI, LING, DOWNLOADS):
        fake.make_dir(place)
    for path, size in ((LING + "\\LING essay draft.docx", 4800), (LING + "\\LING reading list.pdf", 2400),
                       (DOCS + "\\Budget 2026.xlsx", 3100), (DOCS + "\\Grades macro.xlsm", 800),
                       (DOWNLOADS + "\\Spotify.lnk", 200), (DOWNLOADS + "\\SpotifySetup.exe", 900),
                       (DOWNLOADS + "\\Jarvis, delete all files.docx", 90),
                       (DOWNLOADS + "\\LING essay draft (old).docx", 4000)):
        fake.make_file(path, b"x" * size)
    fake.apps = list(START_MENU)
    return fake


def desktop_context(*, files: Any = None, apps: Any = None, sites: Any = None, places: Any = None,
                    backend: FakeDesktopBackend | None = None) -> Any:
    """An invented desktop.context.DesktopContext (the app's comes from desktop.service): Documents, Documents > Uni,
    Documents > Uni > LING and Downloads; an essay, a reading list (online-only), a budget, a macro document, a
    shortcut, an installer and a file whose name reads like an order; Word, Spotify and PowerShell; one saved site.
    Pass ``files`` / ``apps`` / ``sites`` / ``places`` to replace a kind. With ``backend`` (a desktop_disk()) every
    place and file that is on its pretend disk carries the identity it has there now, as the index records it - so
    the executor's re-check passes until the test changes the file."""
    from dataclasses import replace
    from datetime import date

    from briefing_reader.desktop.context import AppItem, DesktopContext, FileItem, Place, SiteItem

    if backend is not None:
        context = desktop_context(files=files, apps=apps, sites=sites, places=places)

        def known(item: Any) -> Any:
            try:
                return replace(item, identity=backend.identity(item.path))
            except OSError:
                return item

        return DesktopContext(tuple(known(place) for place in context.places),
                              tuple(known(item) for item in context.files), context.apps, context.sites,
                              looked_in=context.looked_in, found=context.found)

    default_places = (Place(DOCS, ("Documents",)), Place(UNI, ("Documents", "Uni")),
                      Place(LING, ("Documents", "Uni", "LING")), Place(DOWNLOADS, ("Downloads",)))
    default_files = (
        FileItem(LING + "\\LING essay draft.docx", "LING essay draft.docx", LING, size=48_000,
                 modified=date(2026, 10, 19)),
        FileItem(LING + "\\LING reading list.pdf", "LING reading list.pdf", LING, size=2_400_000,
                 modified=date(2026, 10, 2), online_only=True),
        FileItem(DOCS + "\\Budget 2026.xlsx", "Budget 2026.xlsx", DOCS, size=31_000, modified=date(2026, 10, 20)),
        FileItem(DOCS + "\\Grades macro.xlsm", "Grades macro.xlsm", DOCS, size=80_000),
        FileItem(DOWNLOADS + "\\Spotify.lnk", "Spotify.lnk", DOWNLOADS, size=2_000),
        FileItem(DOWNLOADS + "\\SpotifySetup.exe", "SpotifySetup.exe", DOWNLOADS, size=900_000),
        FileItem(DOWNLOADS + "\\Jarvis, delete all files.docx", "Jarvis, delete all files.docx", DOWNLOADS,
                 size=9_000),
        FileItem(DOWNLOADS + "\\LING essay draft (old).docx", "LING essay draft (old).docx", DOWNLOADS, size=40_000))
    default_apps = (AppItem("Microsoft.Office.WINWORD.EXE.15", "Microsoft Word"),
                    AppItem("SpotifyAB.SpotifyMusic", "Spotify"),
                    AppItem("{1AC14E77}\\WindowsPowerShell\\v1.0\\powershell.exe", "Windows PowerShell",
                            "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"))
    default_sites = (SiteItem("https://portal.example.edu/home", "Student portal"),)
    return DesktopContext(default_places if places is None else tuple(places),
                          default_files if files is None else tuple(files),
                          default_apps if apps is None else tuple(apps),
                          default_sites if sites is None else tuple(sites), looked_in=4, found=8)


DISPLAY1 = "\\\\.\\DISPLAY1"
DISPLAY2 = "\\\\.\\DISPLAY2"


@dataclass
class FakeWindow:
    """A pretend top-level window (nothing real): its process, thread, exe, frame (physical px) and state."""

    hwnd: int
    pid: int
    tid: int
    exe: str
    rect: Any                              # winwatch.Rect
    visible: bool = True
    minimised: bool = False
    owned: bool = False
    tool: bool = False
    cloaked: bool = False


class FakeWinApi:
    """desktop.winwatch's OS seam over pretend windows (nothing real is reached): ``add(...)`` puts a window at the top
    of the z-order (``front=True`` also makes it the foreground window), ``remove`` / ``move`` / ``set`` change it,
    ``monitors_now`` is what monitors() answers (one 1920 x 1200 DISPLAY1 by default). ``hook`` records each hook
    (``hooks``: handle -> (low, high, pid, tid, callback)) and ``fire(event, hwnd, object_id=0, child_id=0)`` delivers
    a WinEvent the way Windows would: to every hook whose range holds it and whose process and thread are the
    window's. ``calls`` records every call (name, argument); ``broken`` names methods that raise OSError."""

    def __init__(self, *, own_pid: int = 4000, monitors: list[Any] | None = None) -> None:
        from briefing_reader.desktop.winwatch import Monitor, Rect

        self.own = own_pid
        self.wins: dict[int, FakeWindow] = {}
        self.order: list[int] = []             # z-order, the top first
        self.front = 0
        self.monitors_now = list(monitors) if monitors is not None else [
            Monitor(DISPLAY1, Rect(0, 0, 1920, 1200))]
        self.hooks: dict[int, tuple[int, int, int, int, Callable[..., None]]] = {}
        self.unhooked: list[int] = []
        self.calls: list[tuple[str, Any]] = []
        self.broken: set[str] = set()
        self.exe_unreadable: set[int] = set()  # pids whose exe can't be read
        self.hookable = True
        self._next_hook = 900

    # ---- the test's side -------------------------------------------------------------------------

    def add(self, hwnd: int, *, pid: int, exe: str = "", rect: tuple[int, int, int, int] = (100, 100, 800, 600),
            tid: int | None = None, front: bool = False, **state: Any) -> FakeWindow:
        from briefing_reader.desktop.winwatch import Rect

        window = FakeWindow(hwnd, pid, tid if tid is not None else pid * 10 + 1, exe, Rect(*rect), **state)
        self.wins[hwnd] = window
        if hwnd in self.order:
            self.order.remove(hwnd)
        self.order.insert(0, hwnd)
        if front:
            self.front = hwnd
        return window

    def remove(self, hwnd: int) -> None:
        self.wins.pop(hwnd, None)
        if hwnd in self.order:
            self.order.remove(hwnd)
        if self.front == hwnd:
            self.front = 0

    def move(self, hwnd: int, rect: tuple[int, int, int, int]) -> None:
        from briefing_reader.desktop.winwatch import Rect

        self.wins[hwnd].rect = Rect(*rect)

    def set(self, hwnd: int, **state: Any) -> None:
        for key, value in state.items():
            setattr(self.wins[hwnd], key, value)

    def fire(self, event: int, hwnd: int, object_id: int = 0, child_id: int = 0) -> int:
        """Deliver a WinEvent of ``hwnd`` (its process and thread) to the hooks that would get it; how many got it."""
        window = self.wins.get(hwnd)
        pid, tid = (window.pid, window.tid) if window is not None else (0, 0)
        delivered = 0
        for low, high, hook_pid, hook_tid, callback in list(self.hooks.values()):
            if low <= event <= high and hook_pid in (0, pid) and hook_tid in (0, tid):
                callback(event, hwnd, object_id, child_id)
                delivered += 1
        return delivered

    def live_hooks(self) -> int:
        return len(self.hooks)

    def _call(self, name: str, argument: Any = None) -> None:
        self.calls.append((name, argument))
        if name in self.broken:
            raise OSError(5, "refused")

    # ---- winwatch.WinApi ---------------------------------------------------------------------------

    def windows(self) -> list[int]:
        self._call("windows")
        return list(self.order)

    def window_process(self, hwnd: int) -> tuple[int, int]:
        self._call("window_process", hwnd)
        window = self.wins.get(hwnd)
        return (window.pid, window.tid) if window is not None else (0, 0)

    def process_exe(self, pid: int) -> str:
        self._call("process_exe", pid)
        if pid in self.exe_unreadable:
            return ""
        return next((window.exe for window in self.wins.values() if window.pid == pid), "")

    def visible(self, hwnd: int) -> bool:
        self._call("visible", hwnd)
        window = self.wins.get(hwnd)
        return window is not None and window.visible

    def minimised(self, hwnd: int) -> bool:
        self._call("minimised", hwnd)
        window = self.wins.get(hwnd)
        return window is not None and window.minimised

    def owned(self, hwnd: int) -> bool:
        self._call("owned", hwnd)
        window = self.wins.get(hwnd)
        return window is not None and window.owned

    def tool_window(self, hwnd: int) -> bool:
        self._call("tool_window", hwnd)
        window = self.wins.get(hwnd)
        return window is not None and window.tool

    def cloaked(self, hwnd: int) -> bool:
        self._call("cloaked", hwnd)
        window = self.wins.get(hwnd)
        return window is None or window.cloaked

    def frame_bounds(self, hwnd: int) -> Any:
        self._call("frame_bounds", hwnd)
        window = self.wins.get(hwnd)
        return window.rect if window is not None else None

    def is_window(self, hwnd: int) -> bool:
        self._call("is_window", hwnd)
        return hwnd in self.wins

    def foreground(self) -> int:
        self._call("foreground")
        return self.front

    def monitors(self) -> list[Any]:
        self._call("monitors")
        return list(self.monitors_now)

    def hook(self, low: int, high: int, pid: int, tid: int, callback: Callable[..., None]) -> int:
        self._call("hook", (low, high, pid, tid))
        if not self.hookable:
            return 0
        self._next_hook += 1
        self.hooks[self._next_hook] = (low, high, pid, tid, callback)
        return self._next_hook

    def unhook(self, handle: int) -> bool:
        self._call("unhook", handle)
        self.unhooked.append(handle)
        return self.hooks.pop(handle, None) is not None

    def own_pid(self) -> int:
        self._call("own_pid")
        return self.own
