"""Tests for briefing_reader.hotkey: combos, the press dispatcher and the agent loop.

The dispatcher is tested with fakes. Nothing here reaches the real user32 or
kernel32 (P1.R, p1r-5): while these tests run, hotkey's one way to them
(``hotkey._api``, and the ``_Win32`` it makes) is shut - it raises
(``setUpModule``) - and every test that needs Win32 brings its own fakes
(``FakeWin32`` and its kinds in place of ``hotkey._api()``). So no global
hotkey is registered, no window's foreground changes (the real
AllowSetForegroundWindow is never called), no mutex of this PC is made and no
pipe but the test's own is written; ``NoRealCase`` fails a test that reached
them anyway. The agent runs its real loop on a thread of its own over
``AgentWin32``, never pressing a key or starting the app; the activation pipe
test talks to the app's own QLocalServer in a separate Python process,
written through Python's own file I/O (``PipeWin32``).

The HUD key (SPEC 9, P1.6) and the foreground hand-over before every pipe
write (SPEC 8.3) are tested the same way.
"""

from __future__ import annotations

import ctypes
import itertools
import json
import logging
import queue
import subprocess
import sys
import threading
import time
import unittest
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from briefing_reader import hotkey
from briefing_reader.hotkey import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    PressDispatcher,
    agent_mutex_name,
    describe_combo,
    normalize_combo,
    parse_combo,
    run_agent,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGGER = "briefing_reader.hotkey"
TEST_COMBO = "ctrl+alt+shift+F24"
WINDOWS = sys.platform == "win32"
# Every reach for the real user32 / kernel32 while these tests run: there must be none.
REACHED: list[str] = []
_SAVED: dict[str, Any] = {}


def _shut(what: str) -> Callable[..., Any]:
    def refuse(*_args: object, **_kwargs: object) -> Any:
        REACHED.append(what)
        raise AssertionError(f"a test reached the real user32 / kernel32 ({what})")
    return refuse


def setUpModule() -> None:
    """hotkey's one way to the real user32 and kernel32 - ``_api`` and the ``_Win32`` it makes - is shut while these
    tests run: a test that needs Win32 patches ``hotkey._api`` with its own fake."""
    _SAVED.update(api=hotkey._api, win32=hotkey._Win32)
    hotkey._api = _shut("hotkey._api")                         # type: ignore[assignment]
    hotkey._Win32 = _shut("hotkey._Win32")                     # type: ignore[assignment,misc]


def tearDownModule() -> None:
    hotkey._api = _SAVED["api"]                                # type: ignore[assignment]
    hotkey._Win32 = _SAVED["win32"]                            # type: ignore[assignment,misc]


def _until(predicate: Callable[[], bool], timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


class NoRealCase(unittest.TestCase):
    """A test that may not reach the real user32 or kernel32: it fails when it did - even where the code it ran
    swallowed the refusal."""

    def setUp(self) -> None:
        super().setUp()
        self._reached = len(REACHED)

    def tearDown(self) -> None:
        reached = REACHED[self._reached:]
        del REACHED[self._reached:]
        super().tearDown()
        self.assertEqual(reached, [], "the real user32 / kernel32 was reached")


# --------------------------------------------------------------------------
# Combos
# --------------------------------------------------------------------------

class ParseComboTests(unittest.TestCase):
    def test_valid_combos(self) -> None:
        cases = {
            "ctrl+alt+j": (MOD_CONTROL | MOD_ALT, 0x4A),
            "Ctrl + Alt + J": (MOD_CONTROL | MOD_ALT, 0x4A),
            "alt+ctrl+j": (MOD_CONTROL | MOD_ALT, 0x4A),
            "win+shift+5": (MOD_WIN | MOD_SHIFT, 0x35),
            "ctrl+0": (MOD_CONTROL, 0x30),
            "control+windows+k": (MOD_CONTROL | MOD_WIN, 0x4B),
            "F24": (0, 0x87),
            "shift+f1": (MOD_SHIFT, 0x70),
            "ctrl+alt+shift+F24": (MOD_CONTROL | MOD_ALT | MOD_SHIFT, 0x87),
            "win+F9": (MOD_WIN, 0x78),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_combo(text), expected)

    def test_invalid_combos(self) -> None:
        bad = ["", "   ", "j", "shift+j", "shift+5", "ctrl+alt", "ctrl", "ctrl+alt+jj", "ctrl++j",
               "+j", "ctrl+alt+", "ctrl+ctrl+j", "control+ctrl+j", "ctrl+alt+F25", "ctrl+alt+F0",
               "hyper+j", "ctrl+alt+-", "ctrl+alt+\u00e9", "ctrl+alt+space", None, 5]
        for text in bad:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_combo(text)  # type: ignore[arg-type]

    def test_normalize_and_describe(self) -> None:
        self.assertEqual(normalize_combo(" Alt + Ctrl + J "), "ctrl+alt+j")
        self.assertEqual(normalize_combo("WIN+shift+f9"), "shift+win+f9")
        self.assertEqual(describe_combo("ctrl+alt+j"), "Ctrl+Alt+J")
        self.assertEqual(describe_combo("shift+ctrl+f12"), "Ctrl+Shift+F12")
        self.assertEqual(describe_combo("nonsense"), "nonsense")
        with self.assertRaises(ValueError):
            normalize_combo("j")

    def test_agent_mutex_name(self) -> None:
        self.assertEqual(agent_mutex_name("john.doe"), "Local\\briefing-reader-hotkey-johndoe")
        self.assertEqual(agent_mutex_name(""), "Local\\briefing-reader-hotkey-user")
        self.assertTrue(agent_mutex_name().startswith("Local\\briefing-reader-hotkey-"))


# --------------------------------------------------------------------------
# Dispatcher
# --------------------------------------------------------------------------

class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class DispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.calls: list[str] = []
        self.running = False
        self.activate_ok = True
        self.start_ok = True

    def make(self) -> PressDispatcher:
        def is_app_running() -> bool:
            self.calls.append("running?")
            return self.running

        def activate() -> bool:
            self.calls.append("activate")
            return self.activate_ok

        def start_app() -> bool:
            self.calls.append("start")
            return self.start_ok

        return PressDispatcher(is_app_running=is_app_running, activate=activate, start_app=start_app,
                               clock=self.clock, spawn=lambda work: work())

    def test_running_app_is_activated(self) -> None:
        dispatcher = self.make()
        self.running = True
        with self.assertLogs(LOGGER, level="INFO") as captured:
            self.assertEqual(dispatcher(), "dispatched")
        self.assertEqual(self.calls, ["running?", "activate"])
        self.assertEqual(dispatcher.last_outcome, "activated")
        self.assertIn("asked the running app", "\n".join(captured.output))

    def test_app_is_started_when_not_running(self) -> None:
        dispatcher = self.make()
        with self.assertLogs(LOGGER, level="INFO"):
            dispatcher()
        self.assertEqual(self.calls, ["running?", "start"])
        self.assertEqual(dispatcher.last_outcome, "started")

    def test_presses_within_two_seconds_are_ignored(self) -> None:
        dispatcher = self.make()
        with self.assertLogs(LOGGER, level="INFO"):
            self.assertEqual(dispatcher(), "dispatched")
        self.clock.now += 1.0
        self.assertEqual(dispatcher(), "ignored")
        self.clock.now += 0.99
        self.assertEqual(dispatcher(), "ignored")
        self.clock.now += 0.01      # 2.0 s after the accepted press
        with self.assertLogs(LOGGER, level="INFO"):
            self.assertEqual(dispatcher(), "dispatched")
        self.assertEqual(self.calls.count("start"), 2)

    def test_a_running_app_that_does_not_answer_is_not_started_twice(self) -> None:
        dispatcher = self.make()
        self.running, self.activate_ok = True, False
        with self.assertLogs(LOGGER, level="WARNING"):
            dispatcher()
        self.assertEqual(self.calls, ["running?", "activate"])
        self.assertEqual(dispatcher.last_outcome, "failed")

    def test_start_failure_is_logged(self) -> None:
        dispatcher = self.make()
        self.start_ok = False
        with self.assertLogs(LOGGER, level="WARNING"):
            dispatcher()
        self.assertEqual(dispatcher.last_outcome, "failed")

    def test_errors_never_escape(self) -> None:
        def broken() -> bool:
            raise OSError("secret-looking detail")

        dispatcher = PressDispatcher(is_app_running=broken, activate=broken, start_app=broken,
                                     clock=self.clock, spawn=lambda work: work())
        with self.assertLogs(LOGGER, level="ERROR") as captured:
            dispatcher()
        self.assertEqual(dispatcher.last_outcome, "failed")
        self.assertNotIn("secret-looking detail", "\n".join(captured.output))

    def test_default_spawn_runs_the_work_on_a_thread(self) -> None:
        done = threading.Event()
        names: list[str] = []

        def start_app() -> bool:
            names.append(threading.current_thread().name)
            done.set()
            return True

        dispatcher = PressDispatcher(is_app_running=lambda: False, activate=lambda: True,
                                     start_app=start_app)
        with self.assertLogs(LOGGER, level="INFO"):
            dispatcher()
            self.assertTrue(done.wait(5))
            time.sleep(0.05)
        self.assertEqual(names, ["hotkey-press"])


# --------------------------------------------------------------------------
# Windows: the agent loop, the HUD key, mutexes and the activation pipe - with fakes only
# --------------------------------------------------------------------------

ERROR_HOTKEY_ALREADY_REGISTERED = 1409
HUD_MESSAGE = {"cmd": "activate", "run": None, "now": False, "open": True, "desktop": True}


class FakeWin32:
    """``hotkey._api()`` for the tests: user32 and kernel32 fakes that record every call in order (``calls``).
    GetMessageW plays ``messages`` - the wParam of one WM_HOTKEY each - then WM_QUIT; RegisterHotKey refuses the ids
    in ``refuse`` (as when another program holds the keys); the pipe's server is process ``pid`` (None: unknown) and
    ``pipe`` False means nobody listens."""

    def __init__(self, messages: tuple[int, ...] = (), refuse: tuple[int, ...] = (), pid: int | None = 4321,
                 pipe: bool = True, allow: bool = True) -> None:
        from types import SimpleNamespace
        from ctypes import wintypes

        self.MSG = wintypes.MSG
        self.calls: list[tuple] = []
        self.messages = list(messages)
        self.refuse = set(refuse)
        self.pid, self.pipe, self.allow = pid, pipe, allow
        self.invalid_handle = ctypes.c_void_p(-1).value
        self.user32 = SimpleNamespace(RegisterHotKey=self._register, UnregisterHotKey=self._unregister,
                                      PeekMessageW=lambda *_args: 0, GetMessageW=self._get_message,
                                      PostThreadMessageW=lambda *_args: 1,
                                      AllowSetForegroundWindow=self._allow)
        self.kernel32 = SimpleNamespace(CreateMutexW=self._mutex, CloseHandle=self._close,
                                        GetCurrentThreadId=lambda: 7, CreateFileW=self._create_file,
                                        WriteFile=self._write, WaitNamedPipeW=lambda *_args: 0,
                                        GetNamedPipeServerProcessId=self._server_pid)

    def valid(self, handle: object) -> bool:
        return bool(handle) and handle != self.invalid_handle

    def named(self, name: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == name]

    def _register(self, _hwnd: object, ident: int, modifiers: int, vk: int) -> int:
        self.calls.append(("RegisterHotKey", ident, modifiers, vk))
        if ident in self.refuse:
            ctypes.set_last_error(ERROR_HOTKEY_ALREADY_REGISTERED)
            return 0
        return 1

    def _unregister(self, _hwnd: object, ident: int) -> int:
        self.calls.append(("UnregisterHotKey", ident))
        return 1

    def _get_message(self, pointer: object, *_args: object) -> int:
        if not self.messages:
            return 0                                                   # WM_QUIT
        msg = pointer._obj                                            # ctypes.byref(MSG)
        msg.message = hotkey.WM_HOTKEY
        msg.wParam = self.messages.pop(0)
        return 1

    def _mutex(self, *_args: object) -> int:
        ctypes.set_last_error(0)
        return 11

    def _close(self, handle: object) -> int:
        self.calls.append(("CloseHandle", handle))
        return 1

    def _create_file(self, path: str, *_args: object) -> object:
        self.calls.append(("CreateFileW", path))
        if not self.pipe:
            ctypes.set_last_error(2)
            return self.invalid_handle
        return 99

    def _server_pid(self, handle: object, pid: object) -> int:
        self.calls.append(("GetNamedPipeServerProcessId", handle))
        if self.pid is None:
            return 0
        pid._obj.value = self.pid
        return 1

    def _allow(self, pid: int) -> int:
        self.calls.append(("AllowSetForegroundWindow", pid))
        if not self.allow:
            ctypes.set_last_error(5)
        return int(self.allow)

    def _write(self, handle: object, payload: bytes, size: int, written: object, _overlapped: object) -> int:
        self.calls.append(("WriteFile", handle, bytes(payload)[:size]))
        written._obj.value = size
        return 1


ERROR_FILE_NOT_FOUND = 2
ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183


class AgentWin32(FakeWin32):
    """FakeWin32 for the agent's real loop on a thread of its own, and for named mutexes (nothing real: no hotkey is
    registered, no mutex of this PC is made). GetMessageW waits for what PostThreadMessageW posts - WM_QUIT ends the
    loop as Windows' does (and so do 10 s with nothing, so a broken test never hangs); RegisterHotKey also refuses keys
    another id holds, as another program's would (``keys``: what each id holds); a named mutex lives while a handle to
    it is open (``mutexes``: CreateMutexW of a live name sets ERROR_ALREADY_EXISTS, OpenMutexW opens it; a name in
    ``foreign`` is another user's - OpenMutexW is refused with ERROR_ACCESS_DENIED)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.posted: queue.SimpleQueue[tuple[int, int]] = queue.SimpleQueue()
        self.keys: dict[int, tuple[int, int]] = {}
        self.mutexes: dict[str, int] = {}
        self.handles: dict[int, str] = {}
        self.foreign: set[str] = set()
        self._ids = itertools.count(200)
        self.user32.PostThreadMessageW = self._post
        self.kernel32.OpenMutexW = self._open_mutex

    def _register(self, _hwnd: object, ident: int, modifiers: int, vk: int) -> int:
        keys = (modifiers & ~hotkey.MOD_NOREPEAT, vk)
        if any(held == keys for other, held in self.keys.items() if other != ident):
            self.calls.append(("RegisterHotKey", ident, modifiers, vk))
            ctypes.set_last_error(ERROR_HOTKEY_ALREADY_REGISTERED)
            return 0
        done = super()._register(_hwnd, ident, modifiers, vk)
        if done:
            self.keys[ident] = keys
        return done

    def _unregister(self, _hwnd: object, ident: int) -> int:
        self.calls.append(("UnregisterHotKey", ident))
        return int(self.keys.pop(ident, None) is not None)

    def _post(self, thread_id: int, message: int, wparam: int, _lparam: int) -> int:
        self.calls.append(("PostThreadMessageW", thread_id, message, wparam))
        self.posted.put((message, wparam))
        return 1

    def _get_message(self, pointer: Any, *_args: object) -> int:
        try:
            message, wparam = self.posted.get(timeout=10)
        except queue.Empty:
            return 0
        if message == hotkey.WM_QUIT:
            return 0
        msg = pointer._obj                                            # ctypes.byref(MSG)
        msg.message = message
        msg.wParam = wparam
        return 1

    def _mutex(self, _security: object = None, _initial: object = False, name: str = "") -> int:
        ctypes.set_last_error(ERROR_ALREADY_EXISTS if name in self.mutexes else 0)
        handle = next(self._ids)
        self.mutexes[name] = self.mutexes.get(name, 0) + 1
        self.handles[handle] = name
        return handle

    def _open_mutex(self, _access: int, _inherit: bool, name: str) -> int:
        self.calls.append(("OpenMutexW", name))
        if name in self.foreign:
            ctypes.set_last_error(ERROR_ACCESS_DENIED)
            return 0
        if name not in self.mutexes:
            ctypes.set_last_error(ERROR_FILE_NOT_FOUND)
            return 0
        return self._mutex(None, False, name)

    def _close(self, handle: object) -> int:
        self.calls.append(("CloseHandle", handle))
        name = self.handles.pop(handle, None) if isinstance(handle, int) else None
        if name is not None:
            self.mutexes[name] -= 1
            if not self.mutexes[name]:
                del self.mutexes[name]
        return 1


class PipeWin32(FakeWin32):
    """FakeWin32 whose pipe goes through to one the test's own child process listens on - through Python's own file
    I/O, never a Win32 call of the tests' - while the hand-over stays a fake: the foreground never changes hands (the
    app at the other end is process ``pid``)."""

    def __init__(self, pid: int) -> None:
        super().__init__(pid=pid)
        self.files: dict[int, Any] = {}
        self._ids = itertools.count(300)
        self.kernel32.WaitNamedPipeW = self._wait_pipe

    def _create_file(self, path: str, *_args: object) -> object:
        self.calls.append(("CreateFileW", path))
        try:
            pipe = open(path, "wb", buffering=0)                      # noqa: SIM115 - CloseHandle closes it
        except OSError as exc:
            ctypes.set_last_error(int(getattr(exc, "winerror", 0) or ERROR_FILE_NOT_FOUND))
            return self.invalid_handle
        handle = next(self._ids)
        self.files[handle] = pipe
        return handle

    def _write(self, handle: object, payload: bytes, size: int, written: Any, _overlapped: object) -> int:
        data = bytes(payload)[:size]
        self.calls.append(("WriteFile", handle, data))
        written._obj.value = self.files[handle].write(data) or 0
        return 1

    def _close(self, handle: object) -> int:
        self.calls.append(("CloseHandle", handle))
        pipe = self.files.pop(handle, None) if isinstance(handle, int) else None
        if pipe is not None:
            pipe.close()
        return 1

    @staticmethod
    def _wait_pipe(_path: str, timeout_ms: int) -> int:
        time.sleep(min(int(timeout_ms), 250) / 1000)
        return 1


class AgentThread:
    """run_agent on its own thread over ``api`` (an AgentWin32 - the test patches ``hotkey._api`` with it), with a
    private mutex name; always stopped by the test. ``hud``: the HUD key ("ctrl+alt+h") too."""

    def __init__(self, api: AgentWin32, combo: str = TEST_COMBO, *, hud: bool = False) -> None:
        self.api = api
        self.mutex_name = f"Local\\briefing-reader-hotkey-test-{uuid.uuid4().hex}"
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.pressed = threading.Event()
        self.presses = 0
        self.hud_presses = 0
        self.thread_id = 0
        self.rc: int | None = None
        extra: dict[str, Any] = {"hud_combo": "ctrl+alt+h", "on_hud_press": self._on_hud_press} if hud else {}
        self.thread = threading.Thread(target=self._run, args=(combo, extra), name="test-hotkey-agent", daemon=True)

    def _run(self, combo: str, extra: dict[str, Any]) -> None:
        self.rc = run_agent(combo, self._on_press, self.stop, mutex_name=self.mutex_name, on_ready=self._on_ready,
                            **extra)
        self.ready.set()   # also when registration failed

    def _on_ready(self) -> None:
        self.thread_id = self.api.kernel32.GetCurrentThreadId()
        self.ready.set()

    def _on_press(self) -> None:
        self.presses += 1
        self.pressed.set()

    def _on_hud_press(self) -> None:
        self.hud_presses += 1
        self.pressed.set()

    def start(self) -> AgentThread:
        self.thread.start()
        if not self.ready.wait(5):
            self.close()
            raise AssertionError("the hotkey agent did not start")
        return self

    def close(self) -> None:
        self.stop.set()
        self.thread.join(5)


@unittest.skipUnless(WINDOWS, "the agent's Win32 path runs only on Windows (fakes here: nothing real is touched)")
class AgentTests(NoRealCase):
    """The agent's real loop on a thread of its own, its mutex and the activation pipe - over fakes only (P1.R,
    p1r-5: no real global hotkey, mutex, foreground or pipe of this PC)."""

    def test_registers_dispatches_and_releases_the_hotkey(self) -> None:
        api = AgentWin32()
        modifiers, vk = parse_combo(TEST_COMBO)
        with mock.patch.object(hotkey, "_api", return_value=api), \
                self.assertLogs(LOGGER, level="INFO") as captured:
            agent = AgentThread(api).start()
            try:
                self.assertIsNone(agent.rc)
                self.assertTrue(agent.thread_id)
                self.assertEqual(api.keys, {hotkey.HOTKEY_ID: (modifiers, vk)})
                # A WM_HOTKEY as Windows would post it, without pressing anything.
                self.assertTrue(api.user32.PostThreadMessageW(agent.thread_id, hotkey.WM_HOTKEY, hotkey.HOTKEY_ID,
                                                              0))
                self.assertTrue(agent.pressed.wait(5))
                # Another id is not ours.
                api.user32.PostThreadMessageW(agent.thread_id, hotkey.WM_HOTKEY, hotkey.HOTKEY_ID + 1, 0)
                # A second agent of the same user exits at once.
                self.assertEqual(run_agent(TEST_COMBO, lambda: None, mutex_name=agent.mutex_name), 0)
            finally:
                started = time.monotonic()
                agent.close()
            self.assertFalse(agent.thread.is_alive())
            self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(agent.rc, 0)
        self.assertEqual(agent.presses, 1)
        text = "\n".join(captured.output)
        self.assertIn("Listening for the hotkey Ctrl+Alt+Shift+F24", text)
        self.assertIn("Another hotkey agent is already running", text)
        self.assertIn("released", text)
        # Released: the keys and the mutex are let go, and registering the same combo again works.
        self.assertEqual((api.keys, api.mutexes), ({}, {}))
        self.assertTrue(api.user32.RegisterHotKey(None, 0x4A53, modifiers | hotkey.MOD_NOREPEAT, vk))
        self.assertTrue(api.user32.UnregisterHotKey(None, 0x4A53))

    def test_combo_in_use_is_a_warning_and_exit_code_1(self) -> None:
        api = AgentWin32()
        modifiers, vk = parse_combo(TEST_COMBO)
        self.assertTrue(api.user32.RegisterHotKey(None, 0x4A54, modifiers, vk))    # another program holds the keys
        with mock.patch.object(hotkey, "_api", return_value=api), \
                self.assertLogs(LOGGER, level="WARNING") as captured:
            agent = AgentThread(api).start()
            agent.close()
        self.assertEqual(agent.rc, 1)
        self.assertIn("another program probably uses it", "\n".join(captured.output))
        self.assertEqual(api.keys, {0x4A54: (modifiers, vk)})                      # theirs, untouched

    def test_stop_before_the_loop_starts(self) -> None:
        api = AgentWin32()
        agent = AgentThread(api)
        agent.stop.set()
        with mock.patch.object(hotkey, "_api", return_value=api), self.assertLogs(LOGGER, level="INFO"):
            agent.thread.start()
            agent.thread.join(5)
        self.assertFalse(agent.thread.is_alive())
        self.assertEqual(agent.rc, 0)
        self.assertEqual((api.keys, api.mutexes), ({}, {}))                        # let go again

    def test_invalid_combo_exits_with_2(self) -> None:
        with self.assertLogs(LOGGER, level="WARNING"):
            self.assertEqual(run_agent("shift+j", lambda: None, mutex_name="Local\\unused-test"), 2)

    def test_mutex_exists(self) -> None:
        api = AgentWin32()
        name = f"Local\\briefing-reader-test-{uuid.uuid4().hex}"
        with mock.patch.object(hotkey, "_api", return_value=api):
            self.assertFalse(hotkey.mutex_exists(name))
            handle = api.kernel32.CreateMutexW(None, False, name)
            self.assertTrue(hotkey.mutex_exists(name))
            api.kernel32.CloseHandle(handle)
            self.assertFalse(hotkey.mutex_exists(name))
            api.foreign.add(name)                                                  # another user's: not ours to open
            self.assertTrue(hotkey.mutex_exists(name))
        self.assertEqual(api.mutexes, {})

    def test_send_activation_without_a_listener(self) -> None:
        api = FakeWin32(pipe=False)
        name = f"briefing-reader-test-{uuid.uuid4().hex}"
        started = time.monotonic()
        with mock.patch.object(hotkey, "_api", return_value=api):
            self.assertFalse(hotkey.send_activation(name, {"cmd": "activate"}, timeout_s=0.3))
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(api.named("AllowSetForegroundWindow"), [])                # nobody to hand it to

    def test_send_activation_reaches_the_apps_activation_server(self) -> None:
        # The receiving side is the app's own QLocalServer, in a separate process (Qt); the pipe is written through
        # Python's own file I/O (PipeWin32) - no real user32 or kernel32 call: the foreground never changes hands.
        name = f"briefing-reader-test-{uuid.uuid4().hex}"
        server_code = f"""
import json, sys
from PySide6.QtCore import QCoreApplication, QTimer
from briefing_reader.activation import ActivationServer
app = QCoreApplication([])
server = ActivationServer({name!r})
got = []
def handle(message):
    got.append(message)
    QTimer.singleShot(200, app.quit)
server.set_handler(handle)
assert server.listen()
print("listening", flush=True)
QTimer.singleShot(15000, app.quit)
app.exec()
server.close()
print(json.dumps(got), flush=True)
"""
        proc = subprocess.Popen([sys.executable, "-c", server_code], cwd=PROJECT_ROOT, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        api = PipeWin32(pid=proc.pid)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "listening")
            message = {"cmd": "activate", "run": None, "now": False, "open": True}   # OPEN_ACTIVATION
            with mock.patch.object(hotkey, "_api", return_value=api):
                self.assertTrue(hotkey.send_activation(name, message, timeout_s=5))
            out, err = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
            for pipe in api.files.values():
                pipe.close()
        self.assertEqual(proc.returncode, 0, err[-2000:])
        self.assertEqual(json.loads(out.strip().splitlines()[-1]), [message])
        self.assertEqual(api.named("AllowSetForegroundWindow"), [("AllowSetForegroundWindow", proc.pid)])   # a fake


class NoRealWin32Tests(NoRealCase):
    """P1.R (p1r-5): hotkey's one way to the real user32 and kernel32 is shut while these tests run, and the paths they
    take - the agent with both keys and their presses, a mutex check, a pipe write with its hand-over - reach only the
    fakes: no real RegisterHotKey, no real AllowSetForegroundWindow."""

    def test_the_way_to_the_real_user32_and_kernel32_is_shut(self) -> None:
        before = len(REACHED)
        for shut in (hotkey._api, hotkey._Win32):
            with self.assertRaises(AssertionError):
                shut()
        self.assertEqual(REACHED[before:], ["hotkey._api", "hotkey._Win32"])
        del REACHED[before:]

    @unittest.skipUnless(WINDOWS, "the agent's Win32 path runs only on Windows")
    def test_every_path_here_reaches_only_the_fakes(self) -> None:
        api = AgentWin32()
        handed: list[object] = []
        hand_over = hotkey._allow_foreground

        def recording(given: Any, handle: Any) -> None:
            handed.append(given)
            hand_over(given, handle)

        with mock.patch.object(hotkey, "_api", return_value=api), \
                mock.patch.object(hotkey, "_allow_foreground", recording), self.assertLogs(LOGGER, level="INFO"):
            agent = AgentThread(api, hud=True).start()
            try:
                for ident in (hotkey.HOTKEY_ID, hotkey.HUD_HOTKEY_ID):
                    api.user32.PostThreadMessageW(agent.thread_id, hotkey.WM_HOTKEY, ident, 0)
                self.assertTrue(_until(lambda: (agent.presses, agent.hud_presses) == (1, 1)))
                self.assertTrue(hotkey.mutex_exists(agent.mutex_name))
                self.assertTrue(hotkey.send_activation("briefing-reader-fake", HUD_MESSAGE))
            finally:
                agent.close()
        self.assertEqual(handed, [api])                                            # the fake, never the real _Win32
        self.assertEqual(api.named("AllowSetForegroundWindow"), [("AllowSetForegroundWindow", 4321)])
        self.assertEqual(sorted(call[1] for call in api.named("RegisterHotKey")),
                         sorted((hotkey.HOTKEY_ID, hotkey.HUD_HOTKEY_ID)))
        self.assertEqual((api.keys, api.mutexes), ({}, {}))


@unittest.skipUnless(WINDOWS, "the agent's Win32 path runs only on Windows (fakes here: nothing real is touched)")
class HudKeyAgentTests(NoRealCase):
    def agent(self, api: FakeWin32, **kwargs: object) -> tuple[int, list[str]]:
        presses: list[str] = []
        with mock.patch.object(hotkey, "_api", return_value=api):
            rc = run_agent("ctrl+alt+j", lambda: presses.append("window"), mutex_name="Local\\briefing-reader-fake",
                           **kwargs)
        return rc, presses

    def hud(self, presses: list[str]) -> dict:
        return {"hud_combo": "ctrl+alt+h", "on_hud_press": lambda: presses.append("hud")}

    def test_the_hud_key_is_a_second_id_only_when_it_is_given(self) -> None:
        api = FakeWin32(messages=(hotkey.HOTKEY_ID, hotkey.HUD_HOTKEY_ID))
        with self.assertLogs(LOGGER, level="INFO") as captured:
            rc, presses = self.agent(api)                     # [desktop] off: as before
        self.assertEqual((rc, presses), (0, ["window"]))      # a HUD id that is not ours is not a press
        self.assertEqual(api.named("RegisterHotKey"),
                         [("RegisterHotKey", hotkey.HOTKEY_ID, MOD_CONTROL | MOD_ALT | hotkey.MOD_NOREPEAT, 0x4A)])
        self.assertEqual(api.named("UnregisterHotKey"), [("UnregisterHotKey", hotkey.HOTKEY_ID)])
        self.assertNotIn("HUD key", "\n".join(captured.output))
        for kwargs in ({"hud_combo": "ctrl+alt+h"}, {"on_hud_press": lambda: None}, {"hud_combo": "", "on_hud_press":
                                                                                     lambda: None}):
            with self.subTest(kwargs=sorted(kwargs)):
                api = FakeWin32()
                with self.assertLogs(LOGGER, level="INFO"):
                    self.agent(api, **kwargs)
                self.assertEqual([call[1] for call in api.named("RegisterHotKey")], [hotkey.HOTKEY_ID])
        api = FakeWin32(messages=(hotkey.HUD_HOTKEY_ID, hotkey.HOTKEY_ID, hotkey.HUD_HOTKEY_ID))
        hud_presses: list[str] = []
        with self.assertLogs(LOGGER, level="INFO") as captured:
            rc, presses = self.agent(api, **self.hud(hud_presses))
        self.assertEqual((rc, presses, hud_presses), (0, ["window"], ["hud", "hud"]))
        self.assertEqual(api.named("RegisterHotKey"),
                         [("RegisterHotKey", hotkey.HOTKEY_ID, MOD_CONTROL | MOD_ALT | hotkey.MOD_NOREPEAT, 0x4A),
                          ("RegisterHotKey", hotkey.HUD_HOTKEY_ID, MOD_CONTROL | MOD_ALT | hotkey.MOD_NOREPEAT, 0x48)])
        self.assertEqual(sorted(call[1] for call in api.named("UnregisterHotKey")),
                         sorted([hotkey.HOTKEY_ID, hotkey.HUD_HOTKEY_ID]))
        text = "\n".join(captured.output)
        self.assertIn("Listening for the HUD key Ctrl+Alt+H", text)
        self.assertIn("HUD key Ctrl+Alt+H released", text)

    def test_a_hud_key_another_program_holds_is_a_status_and_the_window_key_works(self) -> None:
        api = FakeWin32(messages=(hotkey.HOTKEY_ID, hotkey.HUD_HOTKEY_ID, hotkey.HOTKEY_ID),
                        refuse=(hotkey.HUD_HOTKEY_ID,))
        hud_presses: list[str] = []
        with self.assertLogs(LOGGER, level="INFO") as captured:
            rc, presses = self.agent(api, **self.hud(hud_presses))
        self.assertEqual((rc, presses, hud_presses), (0, ["window", "window"], []))
        self.assertEqual(api.named("UnregisterHotKey"), [("UnregisterHotKey", hotkey.HOTKEY_ID)])
        warnings = [record.getMessage() for record in captured.records if record.levelname == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertIn(f"Could not register the HUD key Ctrl+Alt+H (error {ERROR_HOTKEY_ALREADY_REGISTERED})",
                      warnings[0])
        self.assertIn("The hotkey Ctrl+Alt+J works as before", warnings[0])

    def test_a_window_key_another_program_holds_ends_the_agent_as_before(self) -> None:
        api = FakeWin32(refuse=(hotkey.HOTKEY_ID,))
        with self.assertLogs(LOGGER, level="WARNING"):
            rc, _presses = self.agent(api, **self.hud([]))
        self.assertEqual(rc, 1)
        self.assertEqual([call[1] for call in api.named("RegisterHotKey")], [hotkey.HOTKEY_ID])   # no HUD key either

    def test_a_hud_key_that_is_not_valid_or_is_the_window_key_is_not_registered(self) -> None:
        for combo, words in (("shift+h", "is not valid"), ("alt+ctrl+J", "is the window key")):
            with self.subTest(combo=combo):
                api = FakeWin32(messages=(hotkey.HOTKEY_ID,))
                with self.assertLogs(LOGGER, level="INFO") as captured:
                    rc, presses = self.agent(api, hud_combo=combo, on_hud_press=lambda: None)
                self.assertEqual((rc, presses), (0, ["window"]))
                self.assertEqual([call[1] for call in api.named("RegisterHotKey")], [hotkey.HOTKEY_ID])
                self.assertIn(words, "\n".join(captured.output))

    def test_every_pipe_write_hands_the_foreground_to_the_app_first(self) -> None:
        api = FakeWin32(pid=4321)
        with mock.patch.object(hotkey, "_api", return_value=api):
            self.assertTrue(hotkey.send_activation("briefing-reader-fake", {"cmd": "activate", "open": True}))
            self.assertTrue(hotkey.send_activation("briefing-reader-fake", HUD_MESSAGE))
        names = [call[0] for call in api.calls]
        once = ["CreateFileW", "GetNamedPipeServerProcessId", "AllowSetForegroundWindow", "WriteFile", "CloseHandle"]
        self.assertEqual(names, once * 2)                     # before EVERY write: the app's pid, then the write
        self.assertEqual(api.named("AllowSetForegroundWindow"), [("AllowSetForegroundWindow", 4321)] * 2)
        self.assertEqual(api.named("CreateFileW")[0][1], "\\\\.\\pipe\\briefing-reader-fake")
        written = [json.loads(call[2]) for call in api.named("WriteFile")]
        self.assertEqual(written, [{"cmd": "activate", "open": True}, HUD_MESSAGE])

    def test_without_the_apps_pid_any_process_may_and_a_refusal_still_writes(self) -> None:
        for api, wanted in ((FakeWin32(pid=None), hotkey.ASFW_ANY), (FakeWin32(allow=False), 4321)):
            with self.subTest(pid=api.pid, allow=api.allow):
                with mock.patch.object(hotkey, "_api", return_value=api):
                    self.assertTrue(hotkey.send_activation("briefing-reader-fake", HUD_MESSAGE))
                self.assertEqual(api.named("AllowSetForegroundWindow"), [("AllowSetForegroundWindow", wanted)])
                self.assertEqual(len(api.named("WriteFile")), 1)
                names = [call[0] for call in api.calls]
                self.assertLess(names.index("AllowSetForegroundWindow"), names.index("WriteFile"))
        self.assertEqual(hotkey.ASFW_ANY, 0xFFFFFFFF)

    def test_nobody_listening_hands_nothing_over(self) -> None:
        api = FakeWin32(pipe=False)
        with mock.patch.object(hotkey, "_api", return_value=api):
            self.assertFalse(hotkey.send_activation("briefing-reader-fake", HUD_MESSAGE, timeout_s=0,
                                                    sleep=lambda _s: None))
        self.assertEqual([call[0] for call in api.calls], ["CreateFileW"])


class HudKeyDispatcherTests(unittest.TestCase):
    def test_the_hud_keys_dispatcher_words_its_logs_as_its_own(self) -> None:
        clock = FakeClock()
        dispatcher = PressDispatcher(is_app_running=lambda: True, activate=lambda: True, start_app=lambda: True,
                                     clock=clock, spawn=lambda work: work(), label="HUD key",
                                     purpose="go onto your desktop")
        with self.assertLogs(LOGGER, level="INFO") as captured:
            dispatcher()
        self.assertIn("HUD key: asked the running app to go onto your desktop", "\n".join(captured.output))
        self.assertEqual(dispatcher(), "ignored")             # its own 2 s debounce, as the window key's


class ModuleHygieneTests(unittest.TestCase):
    def test_source_is_ascii(self) -> None:
        self.assertTrue(Path(hotkey.__file__).read_bytes().isascii())

    def test_non_windows_calls_are_harmless(self) -> None:
        if WINDOWS:
            self.skipTest("only meaningful elsewhere")
        self.assertFalse(hotkey.mutex_exists("x"))
        self.assertFalse(hotkey.send_activation("x", {}))
        self.assertEqual(run_agent("ctrl+alt+j", lambda: None), 1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
