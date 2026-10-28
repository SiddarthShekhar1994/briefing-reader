"""A global Esc while a desktop batch acts (SPEC sections 9 and 10.1; hard rules 10, 12 and 13; Phase 2, part 3; no
Qt).

While a NEEDS YOUR OK card's rows are carried out - or put back by its Undo - Esc anywhere stops the batch at the next
row: the owner may be looking at another window. ``held(keys, on_stop)`` arms Esc as a global hot key right as the
batch starts acting (its plan's check passed) and releases it as the batch ends (in a finally): never at idle, never
while a plan is made, never otherwise. ``on_stop`` sets the batch's stop flag (a threading.Event: the batch, on the
desktop thread, asks it before each row and during a copy or a zip).

``Win32StopKeys`` is the real seam: RegisterHotKey(NULL, id, MOD_NOREPEAT, VK_ESCAPE) on a thread of its own with its
own message queue - WM_HOTKEY there calls ``on_stop`` - and ``disarm()`` posts WM_QUIT to that thread, which
unregisters the key (on the thread that registered it) before it ends. While Esc is held other programs don't get it,
for the seconds a batch takes. When another program holds Esc already, RegisterHotKey fails and the batch goes on with
the card's Stop only (logged as a status). It never presses, sends or synthesises a key and never activates a window.

The real seam refuses to be made - and each of its methods refuses - while the desktop test guard is on
(backend.RealBackendInTests); ``default_stopkeys()`` then gives ``NoStopKeys``, which registers nothing (as it does
off Windows). Tests pass tests.desktop_fakes.FakeStopKeys. Logs carry statuses and winerrors only.

Public API
==========
``StopKeys`` (Protocol: ``arm(on_stop) -> bool``, ``disarm()``, ``armed()``); ``Win32StopKeys()``; ``NoStopKeys``;
``default_stopkeys()``; ``held(keys, on_stop)`` (a context manager: whether Esc is held); ``HOTKEY_ID``,
``VK_ESCAPE``, ``MOD_NOREPEAT``, ``WM_HOTKEY``, ``WM_QUIT``.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import threading
from collections.abc import Callable, Iterator
from typing import Any, Protocol, TypeVar

from .backend import RealBackendInTests, real_backend_blocked

logger = logging.getLogger(__name__)

HOTKEY_ID = 0x4A45                      # this thread's own hot key id ("JE")
VK_ESCAPE = 0x1B
MOD_NOREPEAT = 0x4000                   # a held-down Esc is one press
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
_PM_NOREMOVE = 0x0000
_READY_S = 2.0                          # the key's thread says whether it holds Esc within this long
_JOIN_S = 2.0

_F = TypeVar("_F", bound=Callable[..., Any])


class StopKeys(Protocol):
    def arm(self, on_stop: Callable[[], None]) -> bool:
        """Hold Esc as a global hot key until ``disarm``: each press calls ``on_stop`` (on another thread). False
        when it can't be held (another program has it; nothing is registered then)."""
        ...

    def disarm(self) -> None:
        """Let Esc go (nothing when it is not held)."""
        ...

    def armed(self) -> bool:
        ...


def _os_call(method: _F) -> _F:
    """An OS-touching method of the real seam: RealBackendInTests while the test guard is on, before anything else
    happens (``reaches_os`` marks it)."""
    @functools.wraps(method)
    def guarded(self: Any, *args: Any, **kwargs: Any) -> Any:
        if real_backend_blocked():
            raise RealBackendInTests(f"Win32StopKeys.{method.__name__} never runs while the tests run")
        return method(self, *args, **kwargs)

    guarded.reaches_os = True                       # type: ignore[attr-defined]
    return guarded                                  # type: ignore[return-value]


class NoStopKeys:
    """No global Esc (the tests, off Windows, or no seam): the card's Stop is the only way to stop a batch."""

    def arm(self, on_stop: Callable[[], None]) -> bool:
        return False

    def disarm(self) -> None:
        return None

    def armed(self) -> bool:
        return False


class Win32StopKeys:
    """The real seam (ctypes; see the module docstring). Refused, and every method refused, while the tests run."""

    def __init__(self) -> None:
        if real_backend_blocked():
            raise RealBackendInTests("the real Esc key is never held while the tests run")
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._msg_type = wintypes.MSG
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                        wintypes.UINT]
        user32.PeekMessageW.restype = wintypes.BOOL
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostThreadMessageW.restype = wintypes.BOOL
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._user32 = user32
        self._kernel32 = kernel32
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._tid = 0

    @_os_call
    def arm(self, on_stop: Callable[[], None]) -> bool:
        with self._lock:
            if self._thread is not None:
                return True
            ready = threading.Event()
            held: list[bool] = []
            thread = threading.Thread(target=self._loop, args=(on_stop, ready, held), name="desktop-esc", daemon=True)
            thread.start()
            ready.wait(_READY_S)
            if not held:
                thread.join(_JOIN_S)                 # it ended by itself: nothing is registered
                return False
            self._thread = thread
            return True

    def _loop(self, on_stop: Callable[[], None], ready: threading.Event, held: list[bool]) -> None:
        ctypes, user32 = self._ctypes, self._user32
        msg = self._msg_type()
        try:
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, _PM_NOREMOVE)    # this thread's queue, made now
            self._tid = int(self._kernel32.GetCurrentThreadId())
            if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_NOREPEAT, VK_ESCAPE):
                logger.info("Desktop: Esc could not be held for the batch (winerror %d); its Stop only",
                            ctypes.get_last_error())
                return
            held.append(True)
        finally:
            ready.set()
        try:
            while True:
                got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if got in (0, -1):                       # WM_QUIT (disarm), or the queue failed
                    break
                if msg.message == WM_HOTKEY and int(msg.wParam) == HOTKEY_ID:
                    try:
                        on_stop()
                    except Exception as exc:  # noqa: BLE001 - the key stays held until disarm
                        logger.warning("Desktop: Esc was heard but not passed on (%s)", type(exc).__name__)
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)

    @_os_call
    def disarm(self) -> None:
        with self._lock:
            thread, self._thread = self._thread, None
            tid = self._tid
        if thread is None:
            return
        if not self._user32.PostThreadMessageW(tid, WM_QUIT, 0, 0):
            logger.warning("Desktop: Esc could not be let go at once (winerror %d)", self._ctypes.get_last_error())
        thread.join(_JOIN_S)

    def armed(self) -> bool:
        return self._thread is not None


def default_stopkeys() -> StopKeys:
    """``Win32StopKeys`` on Windows outside the tests; ``NoStopKeys`` while the test guard is on, elsewhere, or when
    the real seam can't be made."""
    if real_backend_blocked():
        return NoStopKeys()
    import sys

    if sys.platform != "win32":
        return NoStopKeys()
    try:
        return Win32StopKeys()
    except (OSError, AttributeError, RuntimeError) as exc:
        logger.info("Desktop: no global Esc (%s)", type(exc).__name__)
        return NoStopKeys()


@contextlib.contextmanager
def held(keys: StopKeys | None, on_stop: Callable[[], None]) -> Iterator[bool]:
    """Esc held while the ``with`` block runs (a batch acting): whether it is; let go at the end in any case."""
    armed = False
    if keys is not None:
        try:
            armed = bool(keys.arm(on_stop))
        except Exception as exc:  # noqa: BLE001 - no Esc: the card's Stop still stops the batch
            logger.info("Desktop: Esc could not be held for the batch (%s); its Stop only", type(exc).__name__)
    try:
        yield armed
    finally:
        if keys is not None:
            try:
                keys.disarm()
            except Exception as exc:  # noqa: BLE001 - logged; the key's thread ends with Jarvis
                logger.warning("Desktop: Esc could not be let go (%s)", type(exc).__name__)
