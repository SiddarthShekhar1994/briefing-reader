"""Global hotkey for briefing-reader (Ctrl+Alt+J by default), with ctypes only.

``py -m briefing_reader --hotkey-agent`` (started at logon by the "Briefing
hotkey" scheduled task) calls :func:`run_agent`: it takes its own
single-instance mutex, registers the combo with ``RegisterHotKey`` for its
own thread and then sleeps in a blocking ``GetMessage`` loop, so it uses no
CPU until the key is pressed. Each ``WM_HOTKEY`` goes to ``on_press``.

Jarvis on your desktop (SPEC 9, owner decision D1): while [desktop] is on, the
agent is given the HUD key too (``hud_combo``, [desktop] hud_key, default
Ctrl+Alt+H) and registers it as a second hotkey id; its presses go to
``on_hud_press``. A HUD key another program holds is only logged (a status
and the error number): the window key keeps working and the Dock's Desktop
still switches. With [desktop] off nothing new is registered.

:class:`PressDispatcher` is the ``on_press`` the agent uses: it ignores
presses within 2 seconds of the last one, then asks a running app to open
Jarvis (the same one-line JSON activation message a second launch sends,
written to the app's local pipe by :func:`send_activation`) or starts the app;
nothing is read until the owner presses Play. The HUD key has a dispatcher of
its own (its message adds ``"desktop": true``).
The work runs on a short-lived thread, so the message loop never blocks.

The agent received the key press, so Windows lets it set the foreground
window; before every message it writes, it hands that right to the app
(``AllowSetForegroundWindow`` with the app's process id from the pipe -
``GetNamedPipeServerProcessId`` - else to any process), so the app may bring
its window forward, or give the desktop HUD's bar the keyboard, without a
synthetic key press (SPEC 8.3).

Qt-free. The module imports on any platform; the Win32 calls are only made
on Windows.
"""

from __future__ import annotations

import ctypes
import functools
import getpass
import json
import logging
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from . import APP_NAME

logger = logging.getLogger(__name__)

DEFAULT_COMBO = "ctrl+alt+j"
DEBOUNCE_S = 2.0
ACTIVATION_TIMEOUT_S = 5.0
HOTKEY_ID = 0x4A52          # any id in 0x0000..0xBFFF works for a thread hotkey
HUD_HOTKEY_ID = 0x4A48      # the HUD key's ([desktop] hud_key), registered only while [desktop] is on
ASFW_ANY = 0xFFFFFFFF       # AllowSetForegroundWindow: any process (when the app's pid is unknown)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_QUIT = 0x0012
WM_HOTKEY = 0x0312

# Canonical order, flag and display name of each modifier; aliases map to these names.
_MODIFIERS = (("ctrl", MOD_CONTROL, "Ctrl"), ("alt", MOD_ALT, "Alt"),
              ("shift", MOD_SHIFT, "Shift"), ("win", MOD_WIN, "Win"))
_MODIFIER_ALIASES = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
                     "win": "win", "windows": "win"}
_FKEY_RE = re.compile(r"f([1-9]|1[0-9]|2[0-4])")
VK_F1 = 0x70

_ERROR_FILE_NOT_FOUND = 2
_ERROR_ACCESS_DENIED = 5
_ERROR_ALREADY_EXISTS = 183
_ERROR_PIPE_BUSY = 231
_SYNCHRONIZE = 0x00100000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_PIPE_PREFIX = "\\\\.\\pipe\\"


# --------------------------------------------------------------------------
# Combos
# --------------------------------------------------------------------------

def parse_combo(text: str) -> tuple[int, int]:
    """``"ctrl+alt+j"`` -> ``(MOD_CONTROL | MOD_ALT, 0x4A)``; ValueError when invalid.

    Modifiers: ctrl (control), alt, shift, win (windows), any case, each at
    most once. Key: one letter, one digit or F1..F24. A letter or digit needs
    ctrl, alt or win (shift alone would capture ordinary typing); an F key
    may stand alone. ``MOD_NOREPEAT`` is added at registration, not here.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("the hotkey is empty")
    parts = [part.strip().lower() for part in text.split("+")]
    if any(not part for part in parts):
        raise ValueError("the hotkey has an empty part (use e.g. ctrl+alt+j)")
    *modifier_names, key = parts
    modifiers = 0
    for name in modifier_names:
        canonical = _MODIFIER_ALIASES.get(name)
        if canonical is None:
            raise ValueError(f"{name[:20]!r} is not a modifier (use ctrl, alt, shift or win)")
        flag = _modifier_flag(canonical)
        if modifiers & flag:
            raise ValueError(f"{canonical} is given twice")
        modifiers |= flag
    vk, is_function_key = _virtual_key(key)
    if not is_function_key and not modifiers & (MOD_CONTROL | MOD_ALT | MOD_WIN):
        raise ValueError("a letter or digit needs ctrl, alt or win")
    return modifiers, vk


def _modifier_flag(canonical: str) -> int:
    return next(flag for name, flag, _label in _MODIFIERS if name == canonical)


def _virtual_key(key: str) -> tuple[int, bool]:
    if key in _MODIFIER_ALIASES:
        raise ValueError("the hotkey needs a key after the modifiers")
    if len(key) == 1 and ("a" <= key <= "z" or "0" <= key <= "9"):
        return ord(key.upper()), False
    match = _FKEY_RE.fullmatch(key)
    if match is not None:
        return VK_F1 + int(match.group(1)) - 1, True
    raise ValueError(f"{key[:20]!r} is not a letter, digit or F1..F24")


def normalize_combo(text: str) -> str:
    """The canonical spelling: ``" Alt + Ctrl + J "`` -> ``"ctrl+alt+j"``; ValueError when invalid."""
    modifiers, vk = parse_combo(text)
    names = [name for name, flag, _label in _MODIFIERS if modifiers & flag]
    return "+".join([*names, _key_name(vk).lower()])


def describe_combo(text: str) -> str:
    """For people: ``"ctrl+alt+j"`` -> ``"Ctrl+Alt+J"`` (the text itself when invalid)."""
    try:
        modifiers, vk = parse_combo(text)
    except ValueError:
        return str(text)
    labels = [label for _name, flag, label in _MODIFIERS if modifiers & flag]
    return "+".join([*labels, _key_name(vk)])


def _key_name(vk: int) -> str:
    if VK_F1 <= vk < VK_F1 + 24:
        return f"F{vk - VK_F1 + 1}"
    return chr(vk)


def agent_mutex_name(user: str | None = None) -> str:
    """``Local\\briefing-reader-hotkey-<user>``: one hotkey agent per Windows user."""
    if user is None:
        try:
            user = getpass.getuser()
        except Exception:  # noqa: BLE001 - no user name available
            user = ""
    safe = re.sub(r"[^A-Za-z0-9_-]", "", user) or "user"
    return f"Local\\{APP_NAME}-hotkey-{safe}"


# --------------------------------------------------------------------------
# Presses
# --------------------------------------------------------------------------

def _spawn_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="hotkey-press", daemon=True).start()


class PressDispatcher:
    """``on_press`` for the agent: debounce, then activate the running app or start one.

    ``is_app_running()``, ``activate()`` (True when the app got the message)
    and ``start_app()`` (True when it started) are injected; ``spawn`` runs the
    work (a daemon thread by default; tests pass a synchronous one). A press
    within ``debounce_s`` of the last accepted press is ignored. Returns what
    the press did: "ignored" or "dispatched". ``label`` and ``purpose`` only
    word its log lines ("Hotkey" ... "open Jarvis"; the HUD key's "HUD key" ...
    "go onto the desktop").
    """

    def __init__(self, *, is_app_running: Callable[[], bool], activate: Callable[[], bool],
                 start_app: Callable[[], bool], debounce_s: float = DEBOUNCE_S,
                 clock: Callable[[], float] = time.monotonic,
                 spawn: Callable[[Callable[[], None]], None] = _spawn_thread,
                 label: str = "Hotkey", purpose: str = "open Jarvis") -> None:
        self._is_app_running = is_app_running
        self._activate = activate
        self._start_app = start_app
        self._debounce_s = debounce_s
        self._clock = clock
        self._spawn = spawn
        self._label = label
        self._purpose = purpose
        self._last_press: float | None = None
        self.last_outcome = ""      # "activated", "started" or "failed", for logs and tests

    def __call__(self) -> str:
        now = self._clock()
        if self._last_press is not None and now - self._last_press < self._debounce_s:
            logger.debug("%s pressed again within %.0f s; ignored", self._label, self._debounce_s)
            return "ignored"
        self._last_press = now
        self._spawn(self._handle)
        return "dispatched"

    def _handle(self) -> None:
        try:
            self.last_outcome = self._dispatch()
        except Exception as exc:  # noqa: BLE001 - a press must never end the agent
            logger.error("%s: handling the press failed (%s)", self._label, type(exc).__name__)
            self.last_outcome = "failed"

    def _dispatch(self) -> str:
        if self._is_app_running():
            if self._activate():
                logger.info("%s: asked the running app to %s", self._label, self._purpose)
                return "activated"
            logger.warning("%s: the app is running but did not take the request", self._label)
            return "failed"
        if self._start_app():
            logger.info("%s: started the app (it will %s)", self._label, self._purpose)
            return "started"
        logger.warning("%s: could not start the app", self._label)
        return "failed"


# --------------------------------------------------------------------------
# Win32
# --------------------------------------------------------------------------

class _Win32:
    """user32 / kernel32 functions with prototypes (private WinDLL instances)."""

    def __init__(self) -> None:
        from ctypes import wintypes

        self.MSG = wintypes.MSG
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        msg_p = ctypes.POINTER(wintypes.MSG)
        _proto(user32.RegisterHotKey, wintypes.BOOL, wintypes.HWND, ctypes.c_int, wintypes.UINT,
               wintypes.UINT)
        _proto(user32.UnregisterHotKey, wintypes.BOOL, wintypes.HWND, ctypes.c_int)
        _proto(user32.GetMessageW, wintypes.BOOL, msg_p, wintypes.HWND, wintypes.UINT, wintypes.UINT)
        _proto(user32.PeekMessageW, wintypes.BOOL, msg_p, wintypes.HWND, wintypes.UINT, wintypes.UINT,
               wintypes.UINT)
        _proto(user32.PostThreadMessageW, wintypes.BOOL, wintypes.DWORD, wintypes.UINT,
               wintypes.WPARAM, wintypes.LPARAM)
        _proto(user32.AllowSetForegroundWindow, wintypes.BOOL, wintypes.DWORD)
        _proto(kernel32.GetCurrentThreadId, wintypes.DWORD)
        _proto(kernel32.GetNamedPipeServerProcessId, wintypes.BOOL, wintypes.HANDLE,
               ctypes.POINTER(wintypes.ULONG))
        _proto(kernel32.CreateMutexW, wintypes.HANDLE, ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        _proto(kernel32.OpenMutexW, wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
        _proto(kernel32.CloseHandle, wintypes.BOOL, wintypes.HANDLE)
        _proto(kernel32.CreateFileW, wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        _proto(kernel32.WriteFile, wintypes.BOOL, wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD,
               ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
        _proto(kernel32.WaitNamedPipeW, wintypes.BOOL, wintypes.LPCWSTR, wintypes.DWORD)
        self.user32 = user32
        self.kernel32 = kernel32
        self.invalid_handle = ctypes.c_void_p(-1).value

    def valid(self, handle: Any) -> bool:
        return bool(handle) and handle != self.invalid_handle


def _proto(function: Any, restype: Any, *argtypes: Any) -> None:
    function.restype = restype
    function.argtypes = list(argtypes)


@functools.cache
def _api() -> _Win32:
    return _Win32()


def mutex_exists(name: str) -> bool:
    """True when a named mutex (e.g. the app's ``Local\\briefing-reader-<user>``) exists."""
    if sys.platform != "win32":
        return False
    api = _api()
    handle = api.kernel32.OpenMutexW(_SYNCHRONIZE, False, name)
    if handle:
        api.kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == _ERROR_ACCESS_DENIED   # exists, only not ours to open


def send_activation(server_name: str, message: Mapping[str, Any],
                    timeout_s: float = ACTIVATION_TIMEOUT_S, *,
                    clock: Callable[[], float] = time.monotonic,
                    sleep: Callable[[float], None] = time.sleep) -> bool:
    """Write ``message`` as one JSON line to the app's local pipe; False when nobody took it.

    The app listens with a QLocalServer named ``server_name``, which on Windows
    is the pipe ``\\\\.\\pipe\\<server_name>``. A starting app may not listen
    yet, so this retries for up to ``timeout_s``.
    """
    if sys.platform != "win32":
        return False
    payload = (json.dumps(dict(message)) + "\n").encode("utf-8")
    path = _PIPE_PREFIX + server_name
    deadline = clock() + timeout_s
    while True:
        error = _write_pipe(path, payload)
        if error == 0:
            return True
        if clock() >= deadline:
            logger.debug("Activation pipe not available (error %d)", error)
            return False
        if error == _ERROR_PIPE_BUSY:
            _api().kernel32.WaitNamedPipeW(path, 250)
        else:
            sleep(0.25)


def _write_pipe(path: str, payload: bytes) -> int:
    """0 when ``payload`` was written to the pipe, else the Windows error code. Before the write, the app at the
    other end may set the foreground window (``_allow_foreground``)."""
    from ctypes import wintypes

    api = _api()
    handle = api.kernel32.CreateFileW(path, _GENERIC_WRITE, 0, None, _OPEN_EXISTING, 0, None)
    if not api.valid(handle):
        return ctypes.get_last_error() or _ERROR_FILE_NOT_FOUND
    try:
        _allow_foreground(api, handle)
        written = wintypes.DWORD(0)
        ok = api.kernel32.WriteFile(handle, payload, len(payload), ctypes.byref(written), None)
        if not ok:
            return ctypes.get_last_error() or 1
        return 0 if written.value == len(payload) else 1
    finally:
        api.kernel32.CloseHandle(handle)


def _allow_foreground(api: _Win32, handle: Any) -> None:
    """Hand this agent's right to set the foreground window (it received the key press) to the app at the other end
    of the pipe ``handle``: its process id from the pipe, else any process (ASFW_ANY). Best effort, logged at debug
    level with the error number only: the app then brings its window forward, or gives the desktop HUD's bar the
    keyboard, without a synthetic key press (SPEC 8.3)."""
    from ctypes import wintypes

    try:
        pid = wintypes.ULONG(0)
        if api.kernel32.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)) and pid.value:
            target = int(pid.value)
        else:
            target = ASFW_ANY
        if not api.user32.AllowSetForegroundWindow(target):
            logger.debug("AllowSetForegroundWindow was refused (error %d)", ctypes.get_last_error())
    except Exception as exc:  # noqa: BLE001 - the message is written anyway
        logger.debug("Could not let the app set the foreground window (%s)", type(exc).__name__)


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------

def run_agent(combo: str, on_press: Callable[[], Any], stop_event: threading.Event | None = None,
              *, mutex_name: str | None = None, on_ready: Callable[[], None] | None = None,
              hud_combo: str = "", on_hud_press: Callable[[], Any] | None = None) -> int:
    """Listen for ``combo`` until ``stop_event`` is set (or forever); the exit code.

    0: stopped, or another agent of this user already listens; 1: the combo
    could not be registered (another program uses it; logged as a WARNING)
    or this is not Windows; 2: the combo is not valid. ``on_ready`` is called
    once the hotkey is registered. Must run on the thread that should own
    the hotkey; ``stop_event`` may be set from any thread.

    ``hud_combo`` with ``on_hud_press`` (the HUD key, given only while
    [desktop] is on): registered as a second hotkey id once ``combo`` is; its
    presses go to ``on_hud_press``. Not valid, the same keys as ``combo``, or
    held by another program: a WARNING status only - ``combo`` keeps working
    and the exit code is as without it.
    """
    if sys.platform != "win32":
        logger.warning("The global hotkey needs Windows")
        return 1
    try:
        modifiers, vk = parse_combo(combo)
    except ValueError as exc:
        logger.warning("The hotkey %r is not valid (%s); not listening", combo, exc)
        return 2
    hud = _hud_key(hud_combo, on_hud_press, (modifiers, vk))
    api = _api()
    name = mutex_name or agent_mutex_name()
    mutex = api.kernel32.CreateMutexW(None, False, name)
    already = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
    try:
        if already:
            logger.info("Another hotkey agent is already running; this one exits")
            return 0
        return _listen(api, combo, modifiers, vk, on_press, stop_event, on_ready, hud)
    finally:
        if mutex:
            api.kernel32.CloseHandle(mutex)


def _hud_key(hud_combo: str, on_hud_press: Callable[[], Any] | None,
             window_key: tuple[int, int]) -> tuple[str, int, int, Callable[[], Any]] | None:
    """(combo, modifiers, vk, on_press) of the HUD key to register, or None: none given, not valid (a WARNING), or
    the window key's own keys (a WARNING; config.py refuses that already)."""
    if not hud_combo or on_hud_press is None:
        return None
    try:
        modifiers, vk = parse_combo(hud_combo)
    except ValueError as exc:
        logger.warning("The HUD key %r is not valid (%s); not registered", str(hud_combo)[:40], exc)
        return None
    if (modifiers, vk) == window_key:
        logger.warning("The HUD key %s is the window key; not registered", describe_combo(hud_combo))
        return None
    return hud_combo, modifiers, vk, on_hud_press


def _listen(api: _Win32, combo: str, modifiers: int, vk: int, on_press: Callable[[], Any],
            stop_event: threading.Event | None, on_ready: Callable[[], None] | None,
            hud: tuple[str, int, int, Callable[[], Any]] | None = None) -> int:
    msg = api.MSG()
    api.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)   # creates this thread's queue
    if not api.user32.RegisterHotKey(None, HOTKEY_ID, modifiers | MOD_NOREPEAT, vk):
        error = ctypes.get_last_error()
        logger.warning("Could not register the hotkey %s (error %d): another program probably uses it. "
                       "Choose another [hotkey] combo in config.toml.", describe_combo(combo), error)
        return 1
    on_hud_press: Callable[[], Any] | None = None
    try:
        logger.info("Listening for the hotkey %s", describe_combo(combo))
        on_hud_press = _register_hud_key(api, hud, combo)
        if stop_event is not None:
            _watch_stop(api, stop_event, api.kernel32.GetCurrentThreadId())
        if on_ready is not None:
            on_ready()
        return _message_loop(api, msg, on_press, on_hud_press)
    finally:
        if on_hud_press is not None and hud is not None:
            api.user32.UnregisterHotKey(None, HUD_HOTKEY_ID)
            logger.info("HUD key %s released", describe_combo(hud[0]))
        api.user32.UnregisterHotKey(None, HOTKEY_ID)
        logger.info("Hotkey %s released", describe_combo(combo))


def _register_hud_key(api: _Win32, hud: tuple[str, int, int, Callable[[], Any]] | None,
                      combo: str) -> Callable[[], Any] | None:
    """The HUD key as the second hotkey id; its ``on_press`` once registered, else None (a WARNING with the error
    number: the window key ``combo`` works as before and the Dock's Desktop still switches)."""
    if hud is None:
        return None
    hud_combo, modifiers, vk, on_hud_press = hud
    if api.user32.RegisterHotKey(None, HUD_HOTKEY_ID, modifiers | MOD_NOREPEAT, vk):
        logger.info("Listening for the HUD key %s", describe_combo(hud_combo))
        return on_hud_press
    logger.warning("Could not register the HUD key %s (error %d): another program probably uses it. The hotkey %s "
                   "works as before, and the Dock's Desktop still switches; choose another [desktop] hud_key in "
                   "config.toml.", describe_combo(hud_combo), ctypes.get_last_error(), describe_combo(combo))
    return None


def _watch_stop(api: _Win32, stop_event: threading.Event, thread_id: int) -> None:
    """Wake the blocking GetMessage with WM_QUIT once ``stop_event`` is set."""
    def watch() -> None:
        stop_event.wait()
        api.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)

    threading.Thread(target=watch, name="hotkey-stop", daemon=True).start()


def _message_loop(api: _Win32, msg: Any, on_press: Callable[[], Any],
                  on_hud_press: Callable[[], Any] | None = None) -> int:
    while True:
        result = api.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if result == 0:          # WM_QUIT
            return 0
        if result == -1:
            logger.error("Waiting for the hotkey failed (error %d)", ctypes.get_last_error())
            return 1
        if msg.message != WM_HOTKEY:
            continue
        if msg.wParam == HOTKEY_ID:
            _press(on_press)
        elif msg.wParam == HUD_HOTKEY_ID and on_hud_press is not None:
            _press(on_hud_press)


def _press(on_press: Callable[[], Any]) -> None:
    try:
        on_press()
    except Exception as exc:  # noqa: BLE001 - keep listening whatever a press does
        logger.error("The hotkey handler failed (%s)", type(exc).__name__)
