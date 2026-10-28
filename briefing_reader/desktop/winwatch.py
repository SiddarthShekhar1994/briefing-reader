"""Finding and following the window Jarvis opened, for the HUD's bracket (SPEC section 8.4; Phase 1, part 9; no Qt).

After Jarvis opens something (desktop.executor), the HUD points at the window it opened: a cyan bracket round it, the
app's tag and a tether to its result card (desktop_results, hud_overlay.DecorLayer). This module finds that window and
follows it. It never reads a window's title, text or pixels, never moves, raises, activates, hides or closes a window,
and never looks at a window of Jarvis's own process.

Every OS call goes through one seam, ``WinApi``. The app uses ``Win32WinApi`` (user32, dwmapi and kernel32 through
ctypes); every one of its methods raises backend.RealBackendInTests while the desktop test guard is on
(backend.TEST_GUARD_ENV) - ``reaches_os`` marks them - and ``default_api()`` then gives ``NoWinApi``, which finds and
follows nothing (as it does off Windows). Tests and the harness pass tests.desktop_fakes.FakeWinApi.

* Find (``WindowWatch.find``): EnumWindows' top-level windows in z-order (the top first), each kept only when it is
  visible, not minimised, not owned (GetWindow GW_OWNER: a dialog or a pop-up), not a tool window (WS_EX_TOOLWINDOW),
  not cloaked (DwmGetWindowAttribute DWMWA_CLOAKED: on another virtual desktop, or a suspended app; unknown counts as
  cloaked), not Jarvis's own and with a frame of some size - and matched by its process (GetWindowThreadProcessId):
  the pid wanted, or the exe wanted (QueryFullProcessImageNameW on a PROCESS_QUERY_LIMITED_INFORMATION handle; a full
  path is compared whole, a bare file name - as the Start menu gives an app's - by its name, both case-insensitively).
  The window wanted is the first match that was not there before the open (``snapshot()``), else the foreground
  window when it matches (an app that reused a window), else none. Never by a title.
* A folder's window (``find_folder``, P2.3: the cyan bracket round File Explorer's window while a NEEDS YOUR OK card
  waits to move files out of the folder it shows): the Shell's own list of its folder windows (``folder_windows``:
  IShellWindows - each window's handle and the path of the folder it shows, read only) compared whole with the
  card's folder, kept by the same tests as ``find``'s; none open: no bracket.
* Bounds (``bounds``): DwmGetWindowAttribute DWMWA_EXTENDED_FRAME_BOUNDS - the frame as it is drawn, without the
  invisible resize borders GetWindowRect counts - in physical pixels; None for a window that is gone, hidden,
  minimised or cloaked.
* Follow (``follow``): SetWinEventHook(WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS) on the calling thread - the
  GUI thread, whose message loop delivers the events -, filtered to the target's process and thread, one hook per
  event range (``HOOK_RANGES``): EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_MOVESIZESTART..END,
  EVENT_SYSTEM_MINIMIZESTART..END, EVENT_OBJECT_DESTROY and EVENT_OBJECT_LOCATIONCHANGE. Only events of the target
  window itself count (OBJID_WINDOW, CHILDID_SELF for the object events); they come out as ``FRONT``, ``DRAG`` (a move
  or a resize starts: the bracket hides), ``DROP`` (it ended: drawn again where it is), ``MINIMISED``, ``RESTORED``,
  ``MOVED`` (the frame moved or changed size outside a drag: a snap, a maximise) and ``CLOSED`` (its hooks go with
  it). ``Follow.stop()`` unhooks; nothing is ever polled.
* DPI (``map_rect``, pure): Win32 gives physical pixels. Each part of a rect that lies on a monitor (``monitors()``:
  EnumDisplayMonitors + GetMonitorInfoW, the device name and its rect) is mapped onto the Qt screen of the same name:
  logical = the screen's top left + (physical - the monitor's top left) / its device pixel ratio. A window over two
  monitors comes back in two parts (a bracket per screen); a rect that maps onto no screen gives nothing - no bracket.

Logs carry counts, event kinds, exception types and winerrors only - never a title, an exe, a path or a pid.

Public API
==========
``Rect(x, y, width, height)`` (``right``, ``bottom``, ``empty``, ``intersected(other)``), ``Monitor(name, rect)``,
``ScreenArea(name, geometry, dpr)``; ``map_rect(rect, monitors, screens) -> [(screen name, logical Rect)]``;
``same_exe(wanted, actual)``. ``WinApi`` (Protocol): ``windows()``, ``window_process(hwnd)``, ``process_exe(pid)``,
``visible(hwnd)``, ``minimised(hwnd)``, ``owned(hwnd)``, ``tool_window(hwnd)``, ``cloaked(hwnd)``,
``frame_bounds(hwnd)``, ``is_window(hwnd)``, ``foreground()``, ``monitors()``, ``hook(low, high, pid, tid,
callback)``, ``unhook(handle)``, ``own_pid()``, ``folder_windows()`` (P2.3). ``Win32WinApi()``; ``NoWinApi``;
``default_api()``. ``WindowWatch(api=None)``: ``snapshot()``, ``find(exe=, pid=, before=)``, ``find_folder(path)``
(P2.3), ``bounds(hwnd)``, ``follow(hwnd,
on_event)`` -> ``Follow`` (``hwnd``, ``dragging``, ``active()``, ``stop()``) or None, ``monitors()``, ``stop()``.
Event kinds ``FRONT``, ``DRAG``, ``DROP``, ``MINIMISED``, ``RESTORED``, ``MOVED``, ``CLOSED``; the EVENT_* and OBJID_*
values; ``MAX_WINDOWS``.
"""

from __future__ import annotations

import functools
import logging
import math
import ntpath
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from .backend import RealBackendInTests, real_backend_blocked

logger = logging.getLogger(__name__)

# WinEvents (winuser.h).
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MOVESIZESTART = 0x000A
EVENT_SYSTEM_MOVESIZEEND = 0x000B
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
EVENT_OBJECT_DESTROY = 0x8001
EVENT_OBJECT_LOCATIONCHANGE = 0x800B
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
HOOK_FLAGS = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
OBJID_WINDOW = 0
CHILDID_SELF = 0
# One hook per range (the object events' range would bring every object event of the thread in between).
HOOK_RANGES = ((EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND),
               (EVENT_SYSTEM_MOVESIZESTART, EVENT_SYSTEM_MOVESIZEEND),
               (EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND),
               (EVENT_OBJECT_DESTROY, EVENT_OBJECT_DESTROY),
               (EVENT_OBJECT_LOCATIONCHANGE, EVENT_OBJECT_LOCATIONCHANGE))
# What a followed window did.
FRONT, DRAG, DROP, MINIMISED, RESTORED, MOVED, CLOSED = (
    "front", "drag", "drop", "minimised", "restored", "moved", "closed")
EVENT_KINDS = (FRONT, DRAG, DROP, MINIMISED, RESTORED, MOVED, CLOSED)
_KINDS = {EVENT_SYSTEM_FOREGROUND: FRONT, EVENT_SYSTEM_MOVESIZESTART: DRAG, EVENT_SYSTEM_MOVESIZEEND: DROP,
          EVENT_SYSTEM_MINIMIZESTART: MINIMISED, EVENT_SYSTEM_MINIMIZEEND: RESTORED,
          EVENT_OBJECT_DESTROY: CLOSED, EVENT_OBJECT_LOCATIONCHANGE: MOVED}
_OBJECT_EVENTS = frozenset({EVENT_OBJECT_DESTROY, EVENT_OBJECT_LOCATIONCHANGE})
MAX_WINDOWS = 2000                      # top-level windows a search looks at, at most
MIN_SIDE = 40                           # a frame smaller than this (physical px) is no window to bracket

# Win32 values the real seam uses.
_GW_OWNER = 4
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DWMWA_CLOAKED = 14
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MAX_PATH_CHARS = 32768


# --------------------------------------------------------------------------
# Rects, monitors and the DPI mapping (pure)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Rect:
    """A rect in whole pixels (physical from Win32, logical once mapped onto a Qt screen)."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:                         # exclusive
        return self.x + self.width

    @property
    def bottom(self) -> int:                        # exclusive
        return self.y + self.height

    @property
    def empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    def intersected(self, other: Rect) -> Rect:
        left, top = max(self.x, other.x), max(self.y, other.y)
        right, bottom = min(self.right, other.right), min(self.bottom, other.bottom)
        return Rect(left, top, max(0, right - left), max(0, bottom - top))


@dataclass(frozen=True)
class Monitor:
    """A monitor as Windows reports it: its device name ("\\\\.\\DISPLAY1", which Qt's screen of it carries too) and its
    rect in physical pixels."""

    name: str
    rect: Rect


@dataclass(frozen=True)
class ScreenArea:
    """A Qt screen as the mapping needs it: its name, its geometry (logical) and its device pixel ratio."""

    name: str
    geometry: Rect
    dpr: float


def map_rect(rect: Rect, monitors: Sequence[Monitor], screens: Sequence[ScreenArea]) -> list[tuple[str, Rect]]:
    """``rect`` (physical) onto the Qt screens: each part of it on a monitor, on the screen of the same name, in that
    screen's logical pixels (rounded outwards); none when no part lies on a monitor with a screen of its own."""
    parts: list[tuple[str, Rect]] = []
    if rect.empty:
        return parts
    for monitor in monitors:
        part = rect.intersected(monitor.rect)
        if part.empty:
            continue
        screen = next((item for item in screens if item.name == monitor.name), None)
        if screen is None or not screen.dpr or screen.dpr <= 0:
            continue
        origin = monitor.rect
        left = screen.geometry.x + (part.x - origin.x) / screen.dpr
        top = screen.geometry.y + (part.y - origin.y) / screen.dpr
        right = screen.geometry.x + (part.right - origin.x) / screen.dpr
        bottom = screen.geometry.y + (part.bottom - origin.y) / screen.dpr
        x, y = math.floor(left + 1e-6), math.floor(top + 1e-6)
        mapped = Rect(x, y, math.ceil(right - 1e-6) - x, math.ceil(bottom - 1e-6) - y)
        if not mapped.empty:
            parts.append((screen.name, mapped))
    return parts


def _folder_key(path: str) -> str:
    """A folder's path as two windows' paths are compared: case-insensitive, one kind of slash, no trailing one."""
    if not isinstance(path, str) or not path.strip():
        return ""
    return ntpath.normcase(ntpath.normpath(path.strip())).rstrip("\\")


def same_exe(wanted: str, actual: str) -> bool:
    """The process's exe (a full path) is the one wanted: the same full path, or - when only a file name is wanted
    ("Spotify.exe") - the same file name; case-insensitive. Nothing wanted or nothing known: no."""
    if not wanted or not actual:
        return False
    wanted, actual = ntpath.normcase(wanted.strip()), ntpath.normcase(actual.strip())
    if "\\" in wanted:
        return wanted == actual
    return ntpath.basename(actual) == wanted


# --------------------------------------------------------------------------
# The OS seam
# --------------------------------------------------------------------------

Callback = Callable[[int, int, int, int], None]      # (event, hwnd, object id, child id)


class WinApi(Protocol):
    def windows(self) -> list[int]:
        """The top-level windows, in z-order (the top first), at most MAX_WINDOWS (EnumWindows)."""
        ...

    def window_process(self, hwnd: int) -> tuple[int, int]:
        """(pid, thread id) of the window's owner (GetWindowThreadProcessId); (0, 0) when unknown."""
        ...

    def process_exe(self, pid: int) -> str:
        """The process's exe as a full path (QueryFullProcessImageNameW); "" when it cannot be read."""
        ...

    def visible(self, hwnd: int) -> bool:
        ...

    def minimised(self, hwnd: int) -> bool:
        ...

    def owned(self, hwnd: int) -> bool:
        """The window has an owner (GetWindow GW_OWNER): a dialog or a pop-up of another window."""
        ...

    def tool_window(self, hwnd: int) -> bool:
        ...

    def cloaked(self, hwnd: int) -> bool:
        """DWM keeps it from showing (another virtual desktop, a suspended app); unknown counts as cloaked."""
        ...

    def frame_bounds(self, hwnd: int) -> Rect | None:
        """DWMWA_EXTENDED_FRAME_BOUNDS in physical pixels; None when it cannot be read."""
        ...

    def is_window(self, hwnd: int) -> bool:
        ...

    def foreground(self) -> int:
        ...

    def monitors(self) -> list[Monitor]:
        ...

    def hook(self, low: int, high: int, pid: int, tid: int, callback: Callback) -> int:
        """SetWinEventHook(low, high, no module, callback, pid, tid, HOOK_FLAGS) on the calling thread; the handle, or
        0 when none could be set."""
        ...

    def unhook(self, handle: int) -> bool:
        ...

    def own_pid(self) -> int:
        ...

    def folder_windows(self) -> list[tuple[int, str]]:
        """The folder windows of File Explorer open now, as (hwnd, the folder each shows: a plain path; "" for a
        place with no path - This PC, a library, a search) - the Shell's own list of its windows (P2.3)."""
        ...


_F = TypeVar("_F", bound=Callable[..., Any])


def _os_call(method: _F) -> _F:
    """An OS-touching method of the real seam: RealBackendInTests while the test guard is on, before anything else
    happens (``reaches_os`` marks it; a test checks every public method has it)."""
    @functools.wraps(method)
    def guarded(self: Any, *args: Any, **kwargs: Any) -> Any:
        if real_backend_blocked():
            raise RealBackendInTests(f"Win32WinApi.{method.__name__} never runs while the tests run")
        return method(self, *args, **kwargs)

    guarded.reaches_os = True                       # type: ignore[attr-defined]
    return guarded                                  # type: ignore[return-value]


class NoWinApi:
    """No window is found or followed (the tests' default, off Windows, and whenever the real seam can't be made)."""

    def windows(self) -> list[int]:
        return []

    def window_process(self, hwnd: int) -> tuple[int, int]:
        return (0, 0)

    def process_exe(self, pid: int) -> str:
        return ""

    def visible(self, hwnd: int) -> bool:
        return False

    def minimised(self, hwnd: int) -> bool:
        return False

    def owned(self, hwnd: int) -> bool:
        return False

    def tool_window(self, hwnd: int) -> bool:
        return False

    def cloaked(self, hwnd: int) -> bool:
        return True

    def frame_bounds(self, hwnd: int) -> Rect | None:
        return None

    def is_window(self, hwnd: int) -> bool:
        return False

    def foreground(self) -> int:
        return 0

    def monitors(self) -> list[Monitor]:
        return []

    def hook(self, low: int, high: int, pid: int, tid: int, callback: Callback) -> int:
        return 0

    def unhook(self, handle: int) -> bool:
        return False

    def own_pid(self) -> int:
        return 0

    def folder_windows(self) -> list[tuple[int, str]]:
        return []


class Win32WinApi:
    """The real seam (ctypes; see the module docstring). Refused, and every method refused, while the tests run."""

    def __init__(self) -> None:
        if real_backend_blocked():
            raise RealBackendInTests("the real window seam is never made while the tests run")
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        dwmapi = ctypes.WinDLL("dwmapi")
        self._enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self._monitor_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                                ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
        self._event_proc = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG,
                                              wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
        user32.EnumWindows.argtypes = [self._enum_proc, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindow.restype = wintypes.HWND
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, self._monitor_proc, wintypes.LPARAM]
        user32.EnumDisplayMonitors.restype = wintypes.BOOL
        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, self._event_proc,
                                           wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
        user32.SetWinEventHook.restype = wintypes.HANDLE
        user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
        user32.UnhookWinEvent.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                        ctypes.POINTER(wintypes.DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.GetCurrentProcessId.argtypes = []
        kernel32.GetCurrentProcessId.restype = wintypes.DWORD
        dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long        # an HRESULT, checked by hand

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

        self._monitor_info = MonitorInfo
        self._user32, self._kernel32, self._dwmapi = user32, kernel32, dwmapi
        self._hooks: dict[int, Any] = {}             # handle -> its callback (kept alive while hooked)

    @_os_call
    def windows(self) -> list[int]:
        found: list[int] = []

        def each(hwnd: Any, _param: Any) -> bool:
            if hwnd:
                found.append(int(hwnd))
            return len(found) < MAX_WINDOWS

        self._user32.EnumWindows(self._enum_proc(each), 0)
        return found

    @_os_call
    def window_process(self, hwnd: int) -> tuple[int, int]:
        pid = self._wintypes.DWORD(0)
        tid = int(self._user32.GetWindowThreadProcessId(hwnd, self._ctypes.byref(pid)) or 0)
        return (int(pid.value), tid) if tid else (0, 0)

    @_os_call
    def process_exe(self, pid: int) -> str:
        if not pid:
            return ""
        handle = self._kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return ""
        try:
            size = self._wintypes.DWORD(_MAX_PATH_CHARS)
            buffer = self._ctypes.create_unicode_buffer(_MAX_PATH_CHARS)
            if not self._kernel32.QueryFullProcessImageNameW(handle, 0, buffer, self._ctypes.byref(size)):
                return ""
            return buffer.value
        finally:
            self._kernel32.CloseHandle(handle)

    @_os_call
    def visible(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindowVisible(hwnd))

    @_os_call
    def minimised(self, hwnd: int) -> bool:
        return bool(self._user32.IsIconic(hwnd))

    @_os_call
    def owned(self, hwnd: int) -> bool:
        return bool(self._user32.GetWindow(hwnd, _GW_OWNER))

    @_os_call
    def tool_window(self, hwnd: int) -> bool:
        return bool(int(self._user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)) & _WS_EX_TOOLWINDOW)

    @_os_call
    def cloaked(self, hwnd: int) -> bool:
        value = self._wintypes.DWORD(0)
        result = self._dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, self._ctypes.byref(value),
                                                    self._ctypes.sizeof(value))
        return result != 0 or bool(value.value)      # a failed read counts as cloaked

    @_os_call
    def frame_bounds(self, hwnd: int) -> Rect | None:
        rect = self._wintypes.RECT()
        result = self._dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, self._ctypes.byref(rect),
                                                    self._ctypes.sizeof(rect))
        if result != 0:
            return None
        return Rect(int(rect.left), int(rect.top), int(rect.right - rect.left), int(rect.bottom - rect.top))

    @_os_call
    def is_window(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindow(hwnd))

    @_os_call
    def foreground(self) -> int:
        return int(self._user32.GetForegroundWindow() or 0)

    @_os_call
    def monitors(self) -> list[Monitor]:
        ctypes = self._ctypes
        found: list[Monitor] = []

        def each(monitor: Any, _hdc: Any, _rect: Any, _param: Any) -> bool:
            info = self._monitor_info()
            info.cbSize = ctypes.sizeof(info)
            if self._user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                box = info.rcMonitor
                found.append(Monitor(str(info.szDevice), Rect(int(box.left), int(box.top),
                                                              int(box.right - box.left), int(box.bottom - box.top))))
            return True

        self._user32.EnumDisplayMonitors(None, None, self._monitor_proc(each), 0)
        return found

    @_os_call
    def hook(self, low: int, high: int, pid: int, tid: int, callback: Callback) -> int:
        def proc(_hook: Any, event: Any, hwnd: Any, object_id: Any, child_id: Any, _thread: Any, _time: Any) -> None:
            try:
                callback(int(event), int(hwnd or 0), int(object_id), int(child_id))
            except Exception as exc:  # noqa: BLE001 - never let an error out into Windows' callback
                logger.debug("Desktop: a window event could not be handled (%s)", type(exc).__name__)

        keep = self._event_proc(proc)
        handle = int(self._user32.SetWinEventHook(low, high, None, keep, int(pid), int(tid), HOOK_FLAGS) or 0)
        if handle:
            self._hooks[handle] = keep
        else:
            logger.info("Desktop: a window event hook could not be set (winerror %d)", self._ctypes.get_last_error())
        return handle

    @_os_call
    def unhook(self, handle: int) -> bool:
        done = bool(self._user32.UnhookWinEvent(handle))
        self._hooks.pop(int(handle), None)
        return done

    @_os_call
    def own_pid(self) -> int:
        return int(self._kernel32.GetCurrentProcessId())

    @_os_call
    def folder_windows(self) -> list[tuple[int, str]]:
        """Shell.Application's Windows() (IShellWindows): each window's HWND and the path of the folder it shows
        (Document.Folder.Self.Path) - read only; a window that can't say (a web view, a place with no path) gives "".
        Never its title, never anything in it."""
        import win32com.client

        found: list[tuple[int, str]] = []
        windows = win32com.client.Dispatch("Shell.Application").Windows()
        for index in range(min(int(windows.Count), MAX_WINDOWS)):
            try:
                item = windows.Item(index)
                if item is None:
                    continue
                hwnd = int(item.HWND)
                try:
                    path = str(item.Document.Folder.Self.Path or "")
                except Exception:  # noqa: BLE001 - not a folder view: no path
                    path = ""
            except Exception as exc:  # noqa: BLE001 - that window is gone or busy: left out
                logger.debug("Desktop: a folder window could not be read (%s)", type(exc).__name__)
                continue
            found.append((hwnd, path))
        return found


def default_api() -> WinApi:
    """``Win32WinApi`` on Windows outside the tests; ``NoWinApi`` while the test guard is on, elsewhere, or when the
    real seam can't be made."""
    if real_backend_blocked():
        return NoWinApi()
    import sys

    if sys.platform != "win32":
        return NoWinApi()
    try:
        return Win32WinApi()
    except (OSError, AttributeError, RuntimeError) as exc:
        logger.info("Desktop: no window seam (%s)", type(exc).__name__)
        return NoWinApi()


# --------------------------------------------------------------------------
# Finding and following
# --------------------------------------------------------------------------

class Follow:
    """One followed window: its events come to ``on_event(kind)`` (on the thread that set the hooks) until it closes
    or ``stop()``. ``dragging``: a move or resize is on its way (MOVED is not reported meanwhile)."""

    def __init__(self, watch: WindowWatch, hwnd: int, on_event: Callable[[str], None]) -> None:
        self.hwnd = int(hwnd)
        self.dragging = False
        self._watch = watch
        self._on_event = on_event
        self._handles: list[int] = []

    def active(self) -> bool:
        return bool(self._handles)

    def stop(self) -> None:
        handles, self._handles = self._handles, []
        for handle in handles:
            try:
                self._watch.api.unhook(handle)
            except Exception as exc:  # noqa: BLE001 - it goes with the process at the latest
                logger.debug("Desktop: a window event hook could not be removed (%s)", type(exc).__name__)
        self._watch._forget(self)

    def _on_raw(self, event: int, hwnd: int, object_id: int, child_id: int) -> None:
        """A WinEvent of the target's thread: only the target window's own count."""
        if not self._handles or int(hwnd) != self.hwnd:
            return
        if event in _OBJECT_EVENTS and (object_id != OBJID_WINDOW or child_id != CHILDID_SELF):
            return
        kind = _KINDS.get(event)
        if kind is None:
            return
        if kind == DRAG:
            self.dragging = True
        elif kind in (DROP, MINIMISED, CLOSED):
            self.dragging = False
        elif kind == MOVED and self.dragging:
            return                                   # drawn again where the drag ends
        if kind == CLOSED:
            self.stop()
        try:
            self._on_event(kind)
        except Exception as exc:  # noqa: BLE001 - a follower's error never reaches Windows
            logger.warning("Desktop: a followed window's %s could not be shown (%s)", kind, type(exc).__name__)


class WindowWatch:
    """See the module docstring. ``api``: the seam (``default_api()`` when None)."""

    def __init__(self, api: WinApi | None = None) -> None:
        self.api: WinApi = api if api is not None else default_api()
        self._follows: list[Follow] = []

    def snapshot(self) -> frozenset[int]:
        """The top-level windows now (before an open: the window it brings is the one not among them)."""
        try:
            return frozenset(self.api.windows())
        except Exception as exc:  # noqa: BLE001 - nothing known before: any match counts as new
            logger.debug("Desktop: the windows could not be listed (%s)", type(exc).__name__)
            return frozenset()

    def find(self, *, exe: str = "", pid: int = 0, before: Collection[int] = frozenset()) -> int:
        """The window an open brought (see the module docstring); 0 for none."""
        if not exe and not pid:
            return 0
        api = self.api
        try:
            own = api.own_pid()
            exes: dict[int, str] = {}
            matches: list[int] = []
            for hwnd in api.windows()[:MAX_WINDOWS]:
                if not api.visible(hwnd):
                    continue
                owner_pid, _tid = api.window_process(hwnd)
                if not owner_pid or owner_pid == own:
                    continue
                if pid and owner_pid == pid:
                    wanted = True
                else:
                    if owner_pid not in exes:
                        exes[owner_pid] = api.process_exe(owner_pid)
                    wanted = bool(exe) and same_exe(exe, exes[owner_pid])
                if wanted and self._shows(hwnd):
                    matches.append(hwnd)
            fresh = [hwnd for hwnd in matches if hwnd not in before]
            if fresh:
                return fresh[0]
            front = api.foreground()
            if front and front in matches:
                return front
        except Exception as exc:  # noqa: BLE001 - not found: no bracket
            logger.debug("Desktop: the window could not be looked for (%s)", type(exc).__name__)
        return 0

    def find_folder(self, path: str) -> int:
        """The File Explorer window showing the folder ``path`` (the Shell's own list of its windows, the path
        compared whole and case-insensitively; P2.3: a NEEDS YOUR OK card's folder) - one a bracket may go round (as
        ``find``'s); the one in front when there are several; 0 for none. Never by a title."""
        wanted = _folder_key(path)
        if not wanted:
            return 0
        api = self.api
        try:
            own = api.own_pid()
            matches = []
            for hwnd, shown in api.folder_windows()[:MAX_WINDOWS]:
                if not hwnd or _folder_key(shown) != wanted or not api.visible(hwnd):
                    continue
                owner_pid, _tid = api.window_process(hwnd)
                if owner_pid and owner_pid != own and self._shows(hwnd):
                    matches.append(int(hwnd))
            if not matches:
                return 0
            front = api.foreground()
            return front if front in matches else matches[0]
        except Exception as exc:  # noqa: BLE001 - not found: no bracket
            logger.debug("Desktop: the folder's window could not be looked for (%s)", type(exc).__name__)
        return 0

    def _shows(self, hwnd: int) -> bool:
        """A window a bracket may go round: not minimised, owned, a tool window or cloaked, with a frame of size."""
        api = self.api
        if api.minimised(hwnd) or api.owned(hwnd) or api.tool_window(hwnd) or api.cloaked(hwnd):
            return False
        bounds = api.frame_bounds(hwnd)
        return bounds is not None and bounds.width >= MIN_SIDE and bounds.height >= MIN_SIDE

    def bounds(self, hwnd: int) -> Rect | None:
        """The window's frame now (physical), or None when it is gone, hidden, minimised or cloaked."""
        api = self.api
        try:
            if not hwnd or not api.is_window(hwnd) or not api.visible(hwnd) or api.minimised(hwnd) \
                    or api.cloaked(hwnd):
                return None
            rect = api.frame_bounds(hwnd)
        except Exception as exc:  # noqa: BLE001 - unknown: no bracket
            logger.debug("Desktop: a window's frame could not be read (%s)", type(exc).__name__)
            return None
        return rect if rect is not None and not rect.empty else None

    def monitors(self) -> list[Monitor]:
        try:
            return list(self.api.monitors())
        except Exception as exc:  # noqa: BLE001 - no monitor known: nothing maps, no bracket
            logger.debug("Desktop: the monitors could not be listed (%s)", type(exc).__name__)
            return []

    def follow(self, hwnd: int, on_event: Callable[[str], None]) -> Follow | None:
        """Follow ``hwnd`` (see the module docstring) on the calling thread - the GUI thread; None when it can't be
        followed (gone, or no hook could be set: the bracket then stays where it was drawn)."""
        api = self.api
        try:
            pid, tid = api.window_process(hwnd)
            if not pid or not tid or pid == api.own_pid():
                return None
        except Exception as exc:  # noqa: BLE001 - not followed: a still bracket
            logger.info("Desktop: a window could not be followed (%s)", type(exc).__name__)
            return None
        follow = Follow(self, hwnd, on_event)
        try:
            for low, high in HOOK_RANGES:
                handle = api.hook(low, high, pid, tid, follow._on_raw)
                if handle:
                    follow._handles.append(handle)
        except Exception as exc:  # noqa: BLE001 - not followed: a still bracket, no hook left behind
            logger.info("Desktop: a window could not be followed (%s)", type(exc).__name__)
            follow.stop()
            return None
        if not follow._handles:
            return None
        self._follows.append(follow)
        logger.info("Desktop: following a window (%d hooks)", len(follow._handles))
        return follow

    def _forget(self, follow: Follow) -> None:
        if follow in self._follows:
            self._follows.remove(follow)

    def following(self) -> list[Follow]:
        return list(self._follows)

    def stop(self) -> None:
        """Every hook goes (the results went, the HUD hid, Jarvis closes)."""
        for follow in list(self._follows):
            follow.stop()
