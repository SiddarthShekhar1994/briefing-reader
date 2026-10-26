"""Tests for the keys and the HUD bar's keyboard (P1.6; SPEC 1, 3, 8.3, 9 and hard rule 13): every row of SPEC 9's two
tables through AppController.handle_activation, with the messages the hotkey agent sends (the window key's OPEN, the
HUD key's "desktop": true) - the window in any state goes onto the HUD with the bar taking the keyboard, the HUD key on
the HUD gives the bar the keyboard and, when it has it, goes back to the window on the screen it would have had, the
key does nothing while [desktop] is off and brings the window forward while it is not turned on (P1.9's first-run card
plugs in there), both keys are ignored while Jarvis is in control (Phase 3's hook, a fake flag here), a HUD key on the
way in gives the bar the keyboard as the HUD shows; the --hud start (the HUD key started Jarvis) goes onto the HUD with
the window never shown; the bar taking the keyboard and giving it back through the HUD's native seam (the flag dropped
on the NATIVE window - never QWidget.setWindowFlag -, the window that had the keyboard remembered and given it back
after Enter or Esc in an empty bar, Esc with text clearing it, Esc while a request runs cancelling it, a click in the
input taking it, the owner going to another window or the HUD hiding giving it back with nothing activated, the seam's
Win32 side with fakes: attached SetForegroundWindow, never a synthetic key); and Ask bound to one bar at a time
(AskController.set_bar): running, the status and Cancel follow the bound bar, the other bar's Enter and Cancel reach
nothing, a request typed on the HUD runs as the window's would - its cards to Approvals - and the HUD's caption says
only Jarvis's own words ("That's in Approvals, sir."), the window's bar taking over as it is on the way back.

Qt runs offscreen in this process and shows nothing. No real hotkey, pipe or foreground change: the messages are
handed to handle_activation, the HUD's native seam is a recording fake (the real one refuses while the tests run),
force_foreground and the synthetic Alt are patched and asserted, and claude.exe is never started (tests.ask_fakes).
"""

from __future__ import annotations

import contextlib
import ctypes
import tempfile
import threading
import unittest
from ctypes import wintypes
from unittest import mock

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget

from briefing_reader import ask_ui, hud, hud_overlay, ui
from briefing_reader.ask import planner as ask_planner
from briefing_reader.ask.planner import DISABLED_MESSAGE
from briefing_reader.desktop import backend as desktop_backend
from briefing_reader.models import LAUNCH_OPEN
from briefing_reader.persona import with_address
from tests import test_desktop_hud as p14
from tests import test_desktop_switch as p15
from tests.ask_fakes import GatedProcess
from tests.desktop_fakes import desktop_disk
from tests.ui_fakes import COMMAND, SAY, AppHarness, fonts, plan, settle, wait_for

OPEN = dict(p14.OPEN)                                              # the window key (Ctrl+Alt+J)
READ = dict(p14.READ)
HUD_KEY = {"cmd": "activate", "run": None, "now": False, "open": True, "desktop": True}   # the HUD key (Ctrl+Alt+H)
ON_DESKTOP = "On your desktop, sir."
MEMORY_ON = "\n[productivity]\nenabled = true\n"
_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_desktop_keys", "-platform", "offscreen"])
    fonts()


def native_flag(widget: QWidget) -> bool:
    """WindowDoesNotAcceptFocus on the widget's NATIVE window (QWindow's flags, which Windows follows)."""
    handle = widget.windowHandle()
    return handle is not None and bool(handle.flags() & Qt.WindowType.WindowDoesNotAcceptFocus)


class ShowCounter(QObject):
    """Counts the Show events of a window (the window must never show on the way onto the HUD)."""

    def __init__(self, widget: QWidget) -> None:
        super().__init__()
        self.shows = 0
        widget.installEventFilter(self)

    def eventFilter(self, _watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if event.type() == QEvent.Type.Show:
            self.shows += 1
        return False


class KeyApp(p14.HudApp):
    """HudApp (the instant switch, the HUD over a recording seam) with the keys as the hotkey agent sends them."""

    def make_bare(self, extra: str = "", **harness: object) -> AppHarness:
        """As make(), but the window never shown and no state entered: the test puts the app where it wants it."""
        self.native = p14.FakeNative()
        made = AppHarness(self, config_extra=p14.DESKTOP_ON + extra,
                          desktop_factory=lambda config: hud_overlay.create(config, native=self.native), **harness)
        made.briefing()
        p14.holo.set_motion_level(self.MOTION)
        self.addCleanup(p14.holo.set_motion_level, p14.holo.MOTION_LIVELY)
        self.harness, self.c = made, made.c
        self.window, self.reading = made.c.window, made.c.window.reading
        self.desktop = made.c.desktop
        self.closes: list[str] = []
        self.window.closeRequested.connect(lambda: self.closes.append("closeRequested"))
        return made

    def window_key(self, extra: dict | None = None) -> None:
        self.c.handle_activation({**OPEN, **(extra or {})})
        settle()

    def hud_key(self) -> None:
        self.c.handle_activation(dict(HUD_KEY))
        settle()

    def assert_bar_has_the_keyboard(self) -> None:
        win = self.hud()
        self.assertTrue(self.desktop.has_keyboard() and win.holds_keyboard() and win.is_lit())
        self.assertFalse(native_flag(win))                                    # the native window takes the focus now
        self.assertTrue(win.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)   # the widget's own: untouched
        self.assertIs(QApplication.focusWidget(), win.bar.input)

    def assert_bar_idle(self) -> None:
        win = self.hud()
        self.assertFalse(self.desktop.has_keyboard() or win.holds_keyboard() or win.is_lit())
        if win.windowHandle() is not None:
            self.assertTrue(native_flag(win))
        self.assertFalse(win.bar.input.hasFocus())

    def took(self) -> list[tuple]:
        """What the bar asked of Windows to take the keyboard: the window that had it, then the HUD activated."""
        return [("foreground",), ("activate", int(self.hud().winId()))]


# --------------------------------------------------------------------------
# SPEC 9: the HUD key (Ctrl+Alt+H)
# --------------------------------------------------------------------------


class HudKeyTableTests(KeyApp):
    def test_the_window_in_front_goes_onto_the_hud_and_the_bar_takes_the_keyboard(self) -> None:
        self.make()
        self.reading.set_tab(ui.SCREEN_APPROVALS)
        settle()
        changes: list[bool] = []
        self.desktop.hudChanged.connect(changes.append)
        with mock.patch.object(ui, "force_foreground") as forced, mock.patch.object(ui, "_win32_bring_to_front") as alt:
            self.hud_key()
        forced.assert_not_called()
        alt.assert_not_called()
        self.assert_on_hud()
        self.assert_bar_has_the_keyboard()
        self.assertEqual(self.native.keys, self.took())                      # remembered, then activated
        self.assertEqual(self.reading.current_tab(), ui.SCREEN_APPROVALS)    # the hidden window keeps its screen
        self.assertEqual(changes, [True])
        self.assertEqual(self.closes, [])

    def test_the_window_in_any_other_state_goes_onto_the_hud_too(self) -> None:
        """Minimised, hidden, behind another window, the classic prompt, a hidden wait for a run: the assistant screen
        comes up without showing the window, then the HUD with the bar taking the keyboard."""
        def minimised() -> None:
            self.make()
            self.window.showMinimized()

        def hidden() -> None:
            self.make()
            self.window.hide()

        def behind() -> None:
            self.make()
            other = QLineEdit()
            self.addCleanup(other.deleteLater)
            other.show()
            other.activateWindow()
            other.setFocus()

        def prompt() -> None:
            self.make_bare(p14.CLASSIC)
            self.c.show_prompt(take_focus=False)
            self.assertEqual(self.c.state, ui.STATE_PROMPT)

        def waiting() -> None:
            self.make_bare()
            self.c._start_waiting()
            self.assertEqual(self.c.state, ui.STATE_WAITING)

        for name, setup in (("minimised", minimised), ("hidden", hidden), ("behind", behind), ("prompt", prompt),
                            ("waiting", waiting)):
            with self.subTest(name):
                setup()
                settle()
                visible = self.window.isVisible() and not self.window.isMinimized()
                shows = ShowCounter(self.window)
                with mock.patch.object(ui, "force_foreground") as forced, \
                        mock.patch.object(ui, "_win32_bring_to_front") as alt:
                    self.hud_key()
                forced.assert_not_called()
                alt.assert_not_called()
                self.assert_on_hud()
                self.assert_bar_has_the_keyboard()
                self.assertTrue(self.window.is_reading_view())               # the assistant screen, behind the HUD
                if not visible:
                    self.assertEqual(shows.shows, 0, name)                   # the window never showed on the way
                self.assertFalse(self.c._ignore_timer.isActive() or self.c._snooze_timer.isActive())
                self.assertEqual(self.closes, [])
                self.assertTrue(self.desktop.leave_hud())                    # and back as from any visit
                settle()
                self.assert_in_window(ui.SCREEN_HOME)

    def test_on_the_hud_the_key_gives_the_bar_the_keyboard_and_then_goes_back(self) -> None:
        self.make()
        self.enter()                                          # the Dock's Desktop: the bar does not take the keyboard
        self.assert_bar_idle()
        self.assertEqual(self.native.keys, [])
        self.reading.set_tab(ui.SCREEN_APPROVALS)             # an automatic change on the HUD: only recorded
        with mock.patch.object(ui, "force_foreground") as forced, mock.patch.object(ui, "_win32_bring_to_front") as alt:
            self.hud_key()                                    # the bar does not have it: it takes it
            self.assert_on_hud()
            self.assert_bar_has_the_keyboard()
            self.assertEqual(self.native.keys, self.took())
            self.hud_key()                                    # the bar has it: back to the window
            self.assert_in_window(ui.SCREEN_APPROVALS)        # on the screen it would have had
            self.assertFalse(self.hud().is_lit() or self.hud().holds_keyboard())
            self.assertTrue(native_flag(self.hud()))          # the native window never takes the focus again
            self.assertEqual(self.native.keys, self.took())   # let go: the window comes forward, nothing restored
            self.hud_key()                                    # from the window: onto the HUD, the bar with the keyboard
            self.assert_bar_has_the_keyboard()
            self.hud_key()
            self.assert_in_window(ui.SCREEN_HOME)             # nothing recorded this time: Home
        forced.assert_not_called()
        alt.assert_not_called()
        self.assertNotIn("greeting", self.harness.voice.kinds())       # leaving the HUD is no restore
        self.assertEqual(self.closes, [])

    def test_desktop_off_the_key_does_nothing(self) -> None:
        for state in ("shown", "minimised"):
            with self.subTest(state):
                self.make(enabled=False)
                self.reading.set_tab(ui.SCREEN_APPROVALS)
                if state == "minimised":
                    self.window.showMinimized()
                settle()
                before = (self.window.isVisible(), self.window.isMinimized(), self.reading.current_tab(), self.c.state)
                with mock.patch.object(ui, "force_foreground") as forced, \
                        self.assertLogs("briefing_reader.ui", level="INFO") as logs:
                    self.hud_key()
                forced.assert_not_called()
                self.assertIsNone(self.c.desktop)
                after = (self.window.isVisible(), self.window.isMinimized(), self.reading.current_tab(), self.c.state)
                self.assertEqual(after, before)
                self.assertIn("[desktop] is off; nothing happens", "\n".join(logs.output))

    def test_not_turned_on_yet_the_window_comes_forward(self) -> None:
        """THE CONSENT GATE's no (P1.9 shows its first-run card here): the window comes forward as for the window key."""
        self.make()
        self.window.showMinimized()
        settle()
        with mock.patch.object(self.desktop, "consent_given", return_value=False), \
                mock.patch.object(ui, "force_foreground") as forced:
            self.hud_key()
        forced.assert_called_once_with(self.window)
        self.assertFalse(self.desktop.is_hud() or self.window.isMinimized())
        self.assertEqual(self.native.keys, [])

    def test_when_the_hud_cannot_open_the_window_comes_forward(self) -> None:
        self.make()
        with mock.patch.object(QApplication, "activeModalWidget", return_value=QWidget()), \
                mock.patch.object(ui, "force_foreground") as forced:
            self.hud_key()                                    # a dialog waits for an answer: no HUD over it
        self.assertFalse(self.desktop.is_hud())
        forced.assert_called_once_with(self.window)
        self.assertEqual(self.native.keys, [])


class InControlTests(KeyApp):
    def test_in_control_both_keys_are_ignored_and_nothing_leaves_the_hud(self) -> None:
        """Phase 3's hook: ``in_control()`` (a no-op predicate until then; a fake flag here)."""
        self.make()
        self.assertFalse(self.desktop.in_control())
        self.hud_key()
        self.assert_bar_has_the_keyboard()
        with mock.patch.object(self.desktop, "in_control", return_value=True), \
                mock.patch.object(ui, "force_foreground") as forced:
            for message in (HUD_KEY, OPEN, READ, {**OPEN, "ask": True}):
                with self.subTest(message=message), self.assertLogs("briefing_reader.ui", level="INFO") as logs:
                    self.c.handle_activation(dict(message))
                    settle()
                self.assert_on_hud()
                self.assertIn("in control", "\n".join(logs.output))
            self.assertFalse(self.desktop.leave_hud())       # nothing leaves the HUD: Esc or Stop first
            self.hud().bringBackClicked.emit()
            self.hud().chipClicked.emit(hud_overlay.CHIP_BRIEFING)
            settle()
            self.assert_on_hud()
            self.assertFalse(self.desktop.take_keyboard())
            self.assert_bar_has_the_keyboard()               # untouched
        forced.assert_not_called()
        self.window_key()                                     # control: no longer in control, the window key works
        self.assert_in_window(ui.SCREEN_HOME)
        with mock.patch.object(self.desktop, "in_control", return_value=True):
            self.hud_key()                                    # ... and from the window the HUD key is ignored too
        self.assertFalse(self.desktop.is_hud())


class HudKeyFlightTests(p15.FlightApp):
    def test_on_the_way_in_the_bar_takes_the_keyboard_as_the_hud_shows(self) -> None:
        self.make()
        self.now[0] += 10.0
        self.t0 = self.now[0]
        with mock.patch.object(ui, "force_foreground") as forced:
            self.c.handle_activation(dict(HUD_KEY))
            flight = self.desktop.flight()
            self.assertIsNotNone(flight)
            settle()
            self.assertTrue(self.desktop.is_hud() and self.desktop.is_switching())
            self.assertFalse(self.desktop.has_keyboard())
            self.assertEqual(self.native.keys, [])            # nothing asked of Windows while it flies
            self.c.handle_activation(dict(HUD_KEY))           # again on the way in: still the bar, as it shows
            settle()
            self.assertTrue(self.desktop.is_hud() and self.desktop.flight() is flight)
            self.at(1499)
            self.assertFalse(self.hud().isVisible() or self.desktop.has_keyboard())
            self.at(1500)                                     # everything has landed: the HUD's windows show
            self.assertTrue(self.desktop.has_keyboard())
            self.assertEqual(self.native.keys, [("foreground",), ("activate", int(self.hud().winId()))])
            self.land()
            self.assertTrue(self.desktop.has_keyboard())
            self.now[0] += 10.0
            self.t0 = self.now[0]
            self.c.handle_activation(dict(HUD_KEY))           # the bar has it: back to the window, in flight
            back = self.desktop.flight()
            self.assertIsNotNone(back)
            settle()
            self.assertFalse(self.hud().is_lit() or self.hud().holds_keyboard())   # let go as it leaves
            self.c.handle_activation(dict(HUD_KEY))           # on the way back: nothing more
            settle()
            self.assertIs(self.desktop.flight(), back)
            self.land()
        forced.assert_not_called()
        self.assertFalse(self.desktop.is_hud())
        self.assertTrue(self.window.isVisible())
        self.assertNotIn(("restore", self.native.other), self.native.keys)


# --------------------------------------------------------------------------
# SPEC 9: the window key (Ctrl+Alt+J)
# --------------------------------------------------------------------------


class WindowKeyTableTests(KeyApp):
    """Not running: the agent starts the app with --open as today (tests/test_main.py HotkeyAgentTests). In control:
    InControlTests."""

    def test_minimised_hidden_or_behind_it_comes_forward_as_today(self) -> None:
        for state in ("minimised", "hidden", "behind"):
            with self.subTest(state):
                self.make()
                if state == "minimised":
                    self.window.showMinimized()
                elif state == "hidden":
                    self.window.hide()
                else:
                    other = QLineEdit()
                    self.addCleanup(other.deleteLater)
                    other.show()
                    other.activateWindow()
                settle()
                with mock.patch.object(ui, "force_foreground", wraps=ui.force_foreground) as forced:
                    self.window_key()                         # (offscreen force_foreground makes no Win32 call)
                forced.assert_called_once_with(self.window)
                self.assertTrue(self.window.isVisible() and not self.window.isMinimized())
                self.assertFalse(self.desktop.is_hud())
                self.assertEqual(self.native.keys, [])

    def test_in_front_it_is_unchanged(self) -> None:
        self.make()
        self.reading.set_tab(ui.SCREEN_APPROVALS)
        settle()
        with mock.patch.object(ui, "force_foreground") as forced:
            self.window_key()
        forced.assert_called_once_with(self.window)           # today's behaviour: it comes forward, its tab unchanged
        self.assertEqual(self.reading.current_tab(), ui.SCREEN_APPROVALS)
        self.assertFalse(self.desktop.is_hud())

    def test_on_the_hud_back_on_the_remembered_screen_else_home(self) -> None:
        self.make()
        self.enter()
        self.reading.set_tab(ui.SCREEN_APPROVALS)             # recorded on the HUD
        self.hud_key()                                        # the bar with the keyboard: let go on the way back
        self.assert_bar_has_the_keyboard()
        with mock.patch.object(ui, "force_foreground") as forced, mock.patch.object(ui, "_win32_bring_to_front") as alt:
            self.window_key()
            self.assert_in_window(ui.SCREEN_APPROVALS)
            self.assertFalse(self.hud().is_lit())
            self.assertNotIn(("restore", self.native.other), self.native.keys)
            self.enter()
            self.window_key()
            self.assert_in_window(ui.SCREEN_HOME)
        forced.assert_not_called()
        alt.assert_not_called()


# --------------------------------------------------------------------------
# SPEC 9: the HUD key starting Jarvis (--hud)
# --------------------------------------------------------------------------


class HudStartTests(KeyApp):
    def start(self, *, enabled: bool = True, consent: bool = True, error: str = "") -> tuple[mock.Mock, ShowCounter]:
        self.native = p14.FakeNative()
        harness = AppHarness(self, config_extra=p14.DESKTOP_ON if enabled else "",
                             desktop_factory=lambda config: hud_overlay.create(config, native=self.native),
                             launch=LAUNCH_OPEN, desktop_launch=True)
        p14.holo.set_motion_level(p14.holo.MOTION_OFF)
        self.addCleanup(p14.holo.set_motion_level, p14.holo.MOTION_LIVELY)
        self.harness, self.c = harness, harness.c
        self.window, self.reading, self.desktop = harness.c.window, harness.c.window.reading, harness.c.desktop
        self.c._start_tts = lambda: None                      # no speech worker: nothing is ever synthesized
        self.c.startup_error = error or None
        harness.briefing()
        shows = ShowCounter(self.window)
        gate = (mock.patch.object(self.desktop, "consent_given", return_value=consent) if self.desktop is not None
                else contextlib.nullcontext())
        with gate, mock.patch.object(ui, "force_foreground", wraps=ui.force_foreground) as forced:
            self.c.start()                                    # (offscreen force_foreground makes no Win32 call)
            settle()
        return forced, shows

    def test_the_hud_key_starting_jarvis_goes_onto_the_hud_without_the_window(self) -> None:
        forced, shows = self.start()
        forced.assert_not_called()
        self.assertEqual(shows.shows, 0)                      # the window never showed
        self.assert_on_hud()
        self.assertEqual(self.hud().caption_text(), ON_DESKTOP)
        self.assertFalse(self.desktop.has_keyboard())         # a start only goes onto the HUD
        self.assertIn("greeting", self.harness.voice.kinds()) # Jarvis greets as at any start
        self.assertTrue(self.desktop.leave_hud())
        settle()
        self.assert_in_window(ui.SCREEN_HOME)

    def test_off_not_turned_on_or_a_setup_error_the_window_opens_as_for_open(self) -> None:
        for enabled, consent, error in ((False, True, ""), (True, False, ""),
                                        (True, True, "Notion token missing. Add NOTION_TOKEN to the .env file.")):
            with self.subTest(enabled=enabled, consent=consent, error=bool(error)):
                forced, _shows = self.start(enabled=enabled, consent=consent, error=error)
                forced.assert_called_once_with(self.window)   # as --open (P1.9: on its first-run card)
                self.assertTrue(self.window.isVisible())
                self.assertFalse(self.c._hud_active())
                self.assertEqual(self.c.state, ui.STATE_READING)


# --------------------------------------------------------------------------
# SPEC 8.3: the bar's keyboard, through the HUD's native seam
# --------------------------------------------------------------------------


class BarKeyboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.other = QLineEdit()                              # another app's window with the keyboard
        self.addCleanup(self.other.deleteLater)
        self.other.show()
        self.other.activateWindow()
        self.other.setFocus()
        settle()
        self.native = p14.FakeNative()
        self.overlay = hud_overlay.HudOverlay(hud_key="ctrl+alt+h", native=self.native)
        self.addCleanup(self.overlay.shutdown)
        self.win = self.overlay.hud
        self.win.set_caption(ON_DESKTOP)
        self.overlay.show_on(None)
        settle()
        self.changes: list[bool] = []
        self.win.keyboardChanged.connect(self.changes.append)
        self.flag_calls: list[str] = []
        for name in ("setWindowFlag", "setWindowFlags"):           # QWidget's: they recreate (and hide) the window
            setattr(self.win, name, lambda *_args, n=name: self.flag_calls.append(n))

    def took(self) -> list[tuple]:
        return [("foreground",), ("activate", int(self.win.winId()))]

    def outline(self) -> QColor:
        """The bar's top edge in the window's own paint (lit while it has the keyboard)."""
        image = self.win.background().toImage()
        bar = self.win.bar_rect_local()
        dpr = image.devicePixelRatio()
        return image.pixelColor(round(bar.center().x() * dpr), round((bar.top() + 0.5) * dpr))

    def test_idle_the_native_window_never_takes_the_focus(self) -> None:
        self.assertTrue(native_flag(self.win))
        self.assertFalse(self.win.holds_keyboard() or self.win.is_lit())
        self.assertFalse(self.win.give_back_keyboard())       # nothing to give back
        hidden = hud_overlay.HudOverlay(hud_key="ctrl+alt+h", native=p14.FakeNative())
        self.addCleanup(hidden.shutdown)
        self.assertFalse(hidden.hud.take_keyboard())          # only while it shows
        self.assertEqual(self.native.keys, [])

    def test_the_key_takes_it_on_the_native_window_and_enter_gives_it_back(self) -> None:
        idle = self.outline()
        self.assertTrue(self.win.take_keyboard())
        settle()
        self.assertTrue(self.win.holds_keyboard() and self.win.is_lit())
        self.assertFalse(native_flag(self.win))               # dropped on the native window (QWindow.setFlag) ...
        self.assertEqual(self.flag_calls, [])                 # ... never through QWidget.setWindowFlag(s)
        self.assertTrue(self.win.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertEqual(self.native.keys, self.took())       # the window that had it remembered, the HUD activated
        self.assertIs(QApplication.focusWidget(), self.win.bar.input)
        self.assertNotEqual(self.outline(), idle)             # the outline lit
        self.assertEqual(self.outline(), QColor(hud_overlay.holo.HOLO))
        sent: list[str] = []
        self.win.bar.submitted.connect(sent.append)
        QTest.keyClicks(self.win.bar.input, "open my essay")
        QTest.keyClick(self.win.bar.input, Qt.Key.Key_Return)
        settle()
        self.assertEqual(sent, ["open my essay"])
        self.assertEqual(self.native.keys, [*self.took(), ("restore", self.native.other)])   # given back
        self.assertFalse(self.win.holds_keyboard() or self.win.is_lit() or self.win.bar.input.hasFocus())
        self.assertTrue(native_flag(self.win))
        self.assertEqual(self.outline(), idle)
        self.assertEqual(self.changes, [True, False])
        self.assertEqual(self.flag_calls, [])

    def test_esc_clears_the_text_cancels_a_request_and_in_an_empty_bar_gives_it_back(self) -> None:
        self.win.take_keyboard()
        settle()
        bar = self.win.bar
        cancelled: list[int] = []
        bar.cancelRequested.connect(lambda: cancelled.append(1))
        QTest.keyClicks(bar.input, "open my essay")
        QTest.keyClick(bar.input, Qt.Key.Key_Escape)          # Esc with text: cleared, the keyboard kept
        settle()
        self.assertEqual(bar.text(), "")
        self.assertTrue(self.win.holds_keyboard())
        bar.set_running(True)
        QTest.keyClick(bar.input, Qt.Key.Key_Escape)          # Esc while a request runs: Cancel, the keyboard kept
        settle()
        self.assertEqual(cancelled, [1])
        self.assertTrue(self.win.holds_keyboard())
        bar.set_running(False)
        QTest.keyClick(bar.input, Qt.Key.Key_Escape)          # Esc in an empty bar: given back
        settle()
        self.assertFalse(self.win.holds_keyboard())
        self.assertTrue(native_flag(self.win))
        self.assertEqual(self.native.keys[-1], ("restore", self.native.other))
        self.assertEqual(self.changes, [True, False])

    def test_a_click_in_the_input_takes_it_and_a_chip_or_a_button_does_not(self) -> None:
        self.win.set_briefing_ready(True)
        settle()
        QTest.mouseClick(self.win.chips[hud_overlay.CHIP_BRIEFING], Qt.MouseButton.LeftButton)
        QTest.mouseClick(self.win.back_button, Qt.MouseButton.LeftButton)
        settle()
        self.assertEqual(self.native.keys, [])
        self.assertFalse(self.win.holds_keyboard())
        with self.assertLogs("briefing_reader.hud_overlay", level="INFO") as logs:
            QTest.mouseClick(self.win.bar.input, Qt.MouseButton.LeftButton)
            settle()
        self.assertTrue(self.win.holds_keyboard())
        self.assertEqual(self.native.keys, self.took())
        self.assertIn("takes the keyboard (a click)", "\n".join(logs.output))
        QTest.mouseClick(self.win.bar.input, Qt.MouseButton.LeftButton)     # a click while it has it: nothing new
        settle()
        self.assertEqual(self.native.keys, self.took())
        self.assertEqual(self.changes, [True])

    def test_the_owner_going_elsewhere_gives_it_back_without_activating_anything(self) -> None:
        self.win.take_keyboard()
        settle()
        self.other.activateWindow()                           # the owner clicked into another window
        self.other.setFocus()
        settle()
        self.assertFalse(self.win.holds_keyboard() or self.win.is_lit())
        self.assertTrue(native_flag(self.win))
        self.assertEqual(self.native.keys, self.took())       # nothing restored: he chose where to type
        self.assertEqual(self.changes, [True, False])

    def test_the_hud_hiding_gives_it_back_without_activating_anything(self) -> None:
        self.win.take_keyboard()
        settle()
        self.overlay.hide()
        settle()
        self.assertFalse(self.win._holding or self.win.is_lit())
        self.assertTrue(native_flag(self.win))
        self.assertEqual(self.native.keys, self.took())
        self.assertEqual(self.changes, [True, False])

    def test_a_refused_activation_leaves_the_bar_without_it_and_hiding_activates_nothing(self) -> None:
        """Windows refused the activation (the owner's input went elsewhere first): the bar was given the keyboard but
        does not hold it - the next HUD key asks again instead of going back - and the HUD hiding gives nothing back
        to anyone."""
        with mock.patch.object(self.win, "isActiveWindow", return_value=False):
            self.assertTrue(self.win.take_keyboard())
            settle()
            self.assertFalse(self.win.holds_keyboard())
            self.assertTrue(self.win.is_lit())
            self.overlay.hide()
            settle()
        self.assertFalse(self.win.is_lit() or self.win._holding)
        self.assertTrue(native_flag(self.win))
        self.assertEqual(self.native.keys, self.took())        # nothing restored
        self.assertEqual(self.changes, [True, False])

    def test_the_first_remembered_window_is_the_one_given_back(self) -> None:
        self.win.take_keyboard()
        self.native.other = 999                               # the key again before the HUD was active
        self.win.take_keyboard()
        settle()
        self.assertEqual(self.native.keys.count(("foreground",)), 1)
        self.win.give_back_keyboard(True, "Enter")
        self.assertEqual(self.native.keys[-1], ("restore", 4242))

    def test_a_seam_that_fails_leaves_the_keyboard_where_windows_put_it(self) -> None:
        class Broken(p14.FakeNative):
            def foreground(self) -> int:
                raise OSError("no")

            def activate(self, hwnd: int) -> bool:
                raise OSError("no")

        broken = hud_overlay.HudOverlay(hud_key="ctrl+alt+h", native=Broken())
        self.addCleanup(broken.shutdown)
        broken.show_on(None)
        settle()
        self.assertTrue(broken.hud.take_keyboard())           # Qt's own activation still asked; nothing raised
        self.assertTrue(broken.hud.give_back_keyboard())

    def test_the_caption_says_what_jarvis_is_doing_without_its_seconds(self) -> None:
        bar = self.win.bar
        bar.set_status("Planning... 12 s", hud.TONE_WORKING)
        self.assertEqual(self.win.caption_text(), "Planning...")
        bar.set_status("Searching the web... 3 s", hud.TONE_WORKING)
        self.assertEqual(self.win.caption_text(), "Searching the web...")
        bar.set_status("Ask's daily limit reached; try again after 9 AM (20 planner runs a day).", hud.TONE_WARN)
        self.assertEqual(self.win.caption_text(), "Ask's daily limit reached; try again after 9 AM.")
        bar.set_hint(ask_ui.IDLE_HINT)
        self.assertEqual(self.win.caption_text(), ON_DESKTOP)


class _FakeUser32:
    """user32 as Win32Native's keyboard calls use it, recorded: no window is touched."""

    def __init__(self, *, foreground: int = 77, theirs_pid: int = 1234, agree: bool = True,
                 visible: bool = True) -> None:
        self.foreground, self.theirs_pid, self.agree, self.visible = foreground, theirs_pid, agree, visible
        self.calls: list[tuple] = []

    def GetForegroundWindow(self) -> int:  # noqa: N802 - the Win32 name
        return self.foreground

    def GetWindowThreadProcessId(self, hwnd: int, pid: object) -> int:  # noqa: N802
        if pid is not None:
            pid._obj.value = self.theirs_pid                 # ctypes.byref(DWORD): the process of the window
        return 55                                             # its thread

    def AttachThreadInput(self, theirs: int, ours: int, attach: bool) -> bool:  # noqa: N802
        self.calls.append(("AttachThreadInput", theirs, ours, bool(attach)))
        return True

    def BringWindowToTop(self, hwnd: int) -> bool:  # noqa: N802
        self.calls.append(("BringWindowToTop", hwnd))
        return True

    def SetForegroundWindow(self, hwnd: int) -> bool:  # noqa: N802
        self.calls.append(("SetForegroundWindow", hwnd))
        if self.agree:
            self.foreground = hwnd
        return self.agree

    def IsWindow(self, hwnd: int) -> bool:  # noqa: N802
        return hwnd in (77, 88)

    def IsWindowVisible(self, hwnd: int) -> bool:  # noqa: N802
        return self.visible


class NativeSeamTests(unittest.TestCase):
    def seam(self, user32: _FakeUser32, *, our_pid: int = 4321) -> hud_overlay.Win32Native:
        """A Win32Native over fakes (its constructor refuses while the tests run: the fakes go in by hand)."""
        seam = object.__new__(hud_overlay.Win32Native)
        seam._user32 = user32
        seam._kernel32 = mock.Mock(GetCurrentThreadId=mock.Mock(return_value=66),
                                   GetCurrentProcessId=mock.Mock(return_value=our_pid))
        seam._dword = wintypes.DWORD
        seam._byref = ctypes.byref
        seam._last_error = lambda: 0
        return seam

    def test_offscreen_and_in_the_tests_no_os_call_at_all(self) -> None:
        none = hud_overlay.NoNative()
        self.assertEqual((none.foreground(), none.activate(5), none.restore_foreground(5)), (0, False, False))
        self.assertTrue(desktop_backend.real_backend_blocked())
        self.assertIsInstance(hud_overlay.default_native(), hud_overlay.NoNative)
        with self.assertRaises(RuntimeError):
            hud_overlay.Win32Native()

    def test_the_foreground_is_another_apps_never_jarviss_own(self) -> None:
        self.assertEqual(self.seam(_FakeUser32()).foreground(), 77)
        self.assertEqual(self.seam(_FakeUser32(theirs_pid=4321)).foreground(), 0)    # one of Jarvis's own windows
        self.assertEqual(self.seam(_FakeUser32(foreground=0)).foreground(), 0)

    def test_activate_and_restore_are_attached_and_never_a_synthetic_key(self) -> None:
        user32 = _FakeUser32()
        self.assertTrue(self.seam(user32).activate(5))
        self.assertEqual(user32.calls, [("AttachThreadInput", 55, 66, True), ("BringWindowToTop", 5),
                                        ("SetForegroundWindow", 5), ("AttachThreadInput", 55, 66, False)])
        self.assertFalse(hasattr(user32, "keybd_event"))      # the fake has none: a synthetic key would have raised
        user32.calls.clear()
        self.assertTrue(self.seam(user32).restore_foreground(88))
        self.assertIn(("SetForegroundWindow", 88), user32.calls)
        refused = _FakeUser32(agree=False)
        self.assertFalse(self.seam(refused).activate(5))      # refused: False, and still no synthetic key
        for gone in (_FakeUser32(visible=False), _FakeUser32()):
            with self.subTest(visible=gone.visible):
                self.assertFalse(self.seam(gone).restore_foreground(66 if gone.visible else 88))   # gone or hidden
                self.assertEqual(gone.calls, [])


# --------------------------------------------------------------------------
# SPEC 8.3 / 3: Ask bound to one bar at a time
# --------------------------------------------------------------------------


class AskBarTests(KeyApp):
    def make_ask(self, *runs: object, extra: str = "") -> None:
        self.make(extra, runs=runs)
        self.c.ask.start()
        self.assertTrue(wait_for(lambda: self.c.ask.ready is not None), "readiness checked")
        settle()

    def type_on_hud(self, text: str = COMMAND) -> None:
        bar = self.hud().bar
        QTest.mouseClick(bar.input, Qt.MouseButton.LeftButton)       # a click in the bar: it takes the keyboard
        QTest.keyClicks(bar.input, text)
        QTest.keyClick(bar.input, Qt.Key.Key_Return)
        settle()

    def test_running_the_status_and_cancel_follow_the_bound_bar(self) -> None:
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.make_ask(GatedProcess(plan(), gate))
        ask, window_bar, hud_bar = self.c.ask, self.reading.command_bar, self.hud().bar
        self.assertIs(ask.bar, window_bar)
        self.enter()
        self.assertIs(ask.bar, hud_bar)                       # bound to the HUD's bar while it shows
        self.assertEqual(self.hud().caption_text(), ON_DESKTOP)
        window_bar.set_text(COMMAND)
        window_bar.submit()                                   # the window's bar is not bound: nothing starts
        settle()
        self.assertFalse(ask.busy)
        self.type_on_hud()
        self.assertTrue(ask.busy and hud_bar.is_running())
        self.assertFalse(window_bar.is_running())
        self.assertFalse(self.hud().holds_keyboard())         # Enter gave the keyboard back ...
        self.assertEqual(self.native.keys[-1], ("restore", self.native.other))   # ... to the window that had it
        self.assertTrue(wait_for(lambda: ask.stage() == ask_planner.STAGE_PLANNING))
        settle()
        self.assertEqual(self.hud().caption_text(), "Planning...")   # what Jarvis is doing, no seconds
        window_bar.cancelRequested.emit()                     # the window's Cancel is not bound either
        settle()
        self.assertFalse(ask._running.cancel_requested)
        QTest.keyClick(hud_bar.input, Qt.Key.Key_Escape)      # Esc in the HUD's bar while it runs: Cancel
        settle()
        self.assertTrue(ask._running.cancel_requested)
        self.assertEqual(self.hud().caption_text(), "Cancelling...")
        gate.set()
        self.assertTrue(wait_for(lambda: not ask.busy))
        settle()
        self.assertFalse(hud_bar.is_running())
        self.assertEqual(self.hud().caption_text(), ON_DESKTOP)      # cancelled: nothing to say on the HUD
        self.assertIn(ask_ui.CANCELLED_TEXT, hud_bar.status())

    def test_back_in_the_window_its_bar_takes_over_as_it_is(self) -> None:
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.make_ask(GatedProcess(plan(), gate))
        ask, window_bar, hud_bar = self.c.ask, self.reading.command_bar, self.hud().bar
        pending = self.reading.pending_count()
        self.enter()
        self.type_on_hud()
        self.assertTrue(wait_for(lambda: ask.stage() == ask_planner.STAGE_PLANNING))
        self.back()
        self.assertIs(ask.bar, window_bar)
        self.assertTrue(window_bar.is_running())              # it takes over the run, Cancel with it
        self.assertTrue(window_bar.status().startswith("Planning..."))
        window_bar.set_text("a draft of the next one")        # typed meanwhile: never cleared by the run
        hud_bar.cancelRequested.emit()                        # the HUD's bar is let go: its Cancel reaches nothing
        settle()
        self.assertFalse(ask._running.cancel_requested)
        gate.set()
        self.assertTrue(wait_for(lambda: not ask.busy))
        settle()
        self.assertFalse(window_bar.is_running())
        self.assertIn(SAY, window_bar.status())               # the window's bar: the planner's answer
        self.assertEqual(window_bar.text(), "a draft of the next one")
        self.assertEqual(hud_bar.text(), "")                  # the bar it was typed in is cleared (R23)
        self.assertEqual(self.reading.pending_count(), pending + 2)   # its two cards wait in Approvals
        self.enter()
        self.assertEqual(self.hud().caption_text(), with_address(ask_ui.HUD_IN_APPROVALS, "sir"))
        self.assertIn(ui.needs_ok_chip(pending + 2), self.hud().chip_texts())   # ... and the HUD's chip counts them
        self.back()

    def test_the_huds_caption_says_only_jarviss_own_words(self) -> None:
        question = "Which Friday did you mean?"
        cases = ((plan(), ask_ui.HUD_IN_APPROVALS, SAY),
                 (plan(say="", question=question, lines=()), ask_ui.HUD_IN_WINDOW, question))
        for run, words, planners in cases:
            with self.subTest(words):
                self.make_ask(run)
                self.enter()
                self.type_on_hud()
                self.assertTrue(wait_for(lambda: not self.c.ask.busy))
                settle()
                caption = self.hud().caption_text()
                self.assertEqual(caption, with_address(words, "sir"))
                self.assertNotIn(planners, caption)
                self.assertNotIn(COMMAND, caption)            # nor the owner's request
                self.assertEqual(self.reading.current_tab(), ui.SCREEN_HOME)    # nothing came forward
                self.assertFalse(self.window.isVisible())
                said = "\n".join(text for _who, _when, text, _sub in self.reading.conversation.entries())
                self.assertIn(planners, said)                 # the answer is in the window's conversation
                self.back()
                self.assertIn(planners, self.reading.command_bar.status())      # ... and on the window's bar

    def test_a_memory_shortcut_typed_on_the_hud_clears_the_huds_bar(self) -> None:
        self.make_ask(extra=MEMORY_ON)
        window_bar = self.reading.command_bar
        window_bar.set_text("a draft in the window")
        self.enter()
        self.type_on_hud("what do you remember?")
        self.assertEqual(self.hud().bar.text(), "")           # the bar it was typed in
        self.assertEqual(window_bar.text(), "a draft in the window")
        self.assertEqual(self.desktop.return_screen(), ui.SCREEN_MEMORY)   # MEMORY waits for the window
        self.assert_on_hud()
        self.back()
        self.assertEqual(self.reading.current_tab(), ui.SCREEN_MEMORY)

    def test_a_memory_note_typed_on_the_hud_is_never_quoted_in_its_caption(self) -> None:
        self.make_ask(extra=MEMORY_ON)
        self.enter()
        for typed, said in (("remember that the lab opens at 9", "Noted, sir."),
                            ("remember that the lab opens at 9", "I already have that, sir."),
                            ("forget about the lab", "Forgotten, sir.")):
            with self.subTest(typed=typed):
                self.type_on_hud(typed)
                self.assertEqual(self.hud().caption_text(), said)          # Jarvis's own words: never the note
                self.assertEqual(self.hud().bar.text(), "")    # taken: the words left the bar (Up brings them back)
        self.back()
        self.assertIn("lab opens at 9", self.reading.command_bar.status())  # the window's bar quotes it, as before
        said = "\n".join(text for _who, _when, text, _sub in self.reading.conversation.entries())
        self.assertIn('Forgotten, sir: "the lab opens at 9."', said)      # ... and so does the conversation

    def test_closing_on_the_hud_gives_ask_the_windows_bar_back(self) -> None:
        self.make_ask()
        self.enter()
        self.assertIs(self.c.ask.bar, self.hud().bar)
        with mock.patch.object(ui, "_remove_audio_dir_after"), mock.patch.object(self.window, "close") as close:
            self.c.shutdown()
            settle()
        close.assert_not_called()
        self.assertIs(self.c.ask.bar, self.reading.command_bar)   # never bound to the HUD's deleted bar

    def test_set_bar_twice_or_none_changes_nothing(self) -> None:
        self.make_ask(plan())
        ask, window_bar = self.c.ask, self.reading.command_bar
        ask.set_bar(window_bar)
        ask.set_bar(None)
        self.assertIs(ask.bar, window_bar)
        window_bar.set_text(COMMAND)
        window_bar.submit()                                   # still bound once: one Ask, not two
        settle()
        self.assertTrue(wait_for(lambda: not self.c.ask.busy))
        self.assertEqual(len(self.harness.factory.runner.started), 1)

    def test_with_ask_off_the_huds_enter_says_so(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        fake = desktop_disk(tmp.name)
        self.make(ask=False, desktop_backend=lambda: fake)
        self.enter()
        self.hud_key()
        bar = self.hud().bar
        QTest.keyClicks(bar.input, "open my essay")
        QTest.keyClick(bar.input, Qt.Key.Key_Return)
        settle()
        self.assertTrue(wait_for(lambda: not self.desktop.busy()))     # the desktop thread: no local route
        settle()
        self.assertEqual(self.hud().caption_text(), DISABLED_MESSAGE)
        self.assertEqual(bar.text(), "open my essay")                  # not taken: the words stay
        self.assertEqual(fake.opened, [])
        bar.clear_input()
        self.assertFalse(self.hud().holds_keyboard())         # Enter gave the keyboard back
        QTest.keyClick(bar.input, Qt.Key.Key_Escape)          # Esc in the empty bar: the note goes
        settle()
        self.assertEqual(self.hud().caption_text(), ON_DESKTOP)


if __name__ == "__main__":
    unittest.main()
