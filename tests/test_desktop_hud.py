"""Tests for the desktop HUD in the app (P1.4, the instant switch; SPEC 1, 8.3, 9, 12 and 15): the Dock's Desktop and
the Find list's go onto the HUD - the window HIDDEN, never closed - and Bring Jarvis back, a chip, the tray's Bring
Jarvis back, the toast's click or the window key bring it back on the right screen without a greeting or the
start-up; nothing comes forward by itself meanwhile (force_foreground patched and asserted, no synthetic Alt), an
automatic screen change only records the screen, Live never opens by itself, the agenda, the offers and the start-up
count the HUD as present, an open meetings start card closes as Not now, the chips mirror the window's counts live,
a recording started on the HUD gives a REC chip and a held pill, re-entrancy changes nothing, the consent gate is
fail-closed, shutdown on the HUD closes nothing, and with [desktop] off there is no HUD at all.

Qt runs offscreen in this process and shows nothing. No OS call is made: the HUD's native seam is a recording fake
(and the real one refuses while the tests run), and the Win32 calls of the bring-forward are fakes.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from briefing_reader import alive, desktop_ui, dock, find_view, hud_overlay, meetings as mt, meetings_view, ui
from briefing_reader import live as _live
from briefing_reader.config import DesktopConfig
from tests.meeting_fakes import Clock, FakeBackend, tone
from tests.ui_fakes import NOW, AppHarness, fonts, settle

DESKTOP_ON = "\n[desktop]\nenabled = true\n"
MEETINGS_ON = "\n[meetings]\nenabled = true\n"
CLASSIC = "\n[assistant]\nscheduled_prompt = true\n"
OPEN = {"cmd": "activate", "run": None, "now": False, "open": True}
HUD_NAMES = {"JarvisHud", "JarvisResult", "JarvisDecor"}
DOT = "\u00b7"
_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_desktop_hud", "-platform", "offscreen"])
    fonts()


class FakeNative:
    """The HUD's HudNative seam, recorded: what Windows would be asked (nothing is)."""

    def __init__(self) -> None:
        self.excluded: list[int] = []
        self.stacks: list[list[int]] = []

    def exclude_from_capture(self, hwnd: int) -> bool:
        self.excluded.append(int(hwnd))
        return True

    def stack(self, hwnds) -> None:
        self.stacks.append([int(hwnd) for hwnd in hwnds])


class HudApp(unittest.TestCase):
    """An AppHarness with [desktop] on (``enabled``) and the HUD's windows over a fake native seam."""

    def make(self, extra: str = "", *, enabled: bool = True) -> AppHarness:
        self.native = FakeNative()
        harness = AppHarness(self, config_extra=(DESKTOP_ON if enabled else "") + extra,
                             desktop_factory=lambda config: hud_overlay.create(config, native=self.native))
        harness.reading(autoplay=False)
        self.harness, self.c = harness, harness.c
        self.window, self.reading = harness.c.window, harness.c.window.reading
        self.desktop = harness.c.desktop
        self.closes: list[str] = []
        self.window.closeRequested.connect(lambda: self.closes.append("closeRequested"))
        return harness

    def hud(self) -> hud_overlay.HudWindow:
        return self.desktop.overlay.hud

    def enter(self) -> None:
        """The owner's click on the Dock's Desktop."""
        QTest.mouseClick(self.reading.dock.button(dock.DESKTOP), Qt.MouseButton.LeftButton)
        settle()
        self.assertTrue(self.desktop.is_hud())

    def back(self) -> None:
        """The owner's click on Bring Jarvis back."""
        QTest.mouseClick(self.hud().back_button, Qt.MouseButton.LeftButton)
        settle()
        self.assertFalse(self.desktop.is_hud())

    def chip(self, key: str) -> None:
        QTest.mouseClick(self.hud().chips[key], Qt.MouseButton.LeftButton)
        settle()

    def assert_on_hud(self) -> None:
        self.assertTrue(self.desktop.is_hud() and self.c._hud_active())
        self.assertFalse(self.window.isVisible())
        self.assertTrue(self.desktop.overlay.is_shown() and self.hud().isVisible())
        self.assertEqual(self.c.state, ui.STATE_READING)

    def assert_in_window(self, screen: int) -> None:
        self.assertFalse(self.desktop.is_hud() or self.c._hud_active())
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window.isMinimized())
        self.assertFalse(any(window.isVisible() for window in self.desktop.overlay.windows()))
        self.assertEqual(self.reading.current_tab(), screen)


class SwitchTests(HudApp):
    def test_the_dock_and_find_go_onto_the_hud_the_window_hidden_never_closed(self) -> None:
        self.make()
        self.reading.set_tab(ui.SCREEN_APPROVALS)
        settle()
        changes: list[bool] = []
        self.desktop.hudChanged.connect(changes.append)
        with mock.patch.object(self.window, "close") as close:
            screen = hud_overlay.screen_for(self.window)
            self.enter()
            self.assert_on_hud()
            self.assertEqual(self.reading.current_tab(), ui.SCREEN_APPROVALS)     # the hidden window keeps its screen
            self.assertIs(self.desktop.overlay.hud_screen(), screen)              # on the window's screen
            self.assertEqual(self.hud().caption_text(), "On your desktop, sir.")
            self.assertEqual(self.native.excluded, [int(self.hud().winId())])     # left out of capture (the fake)
            self.back()
            self.assert_in_window(ui.SCREEN_HOME)                                 # Bring Jarvis back: Home
            self.reading._on_find_pick(find_view.FindItem(find_view.KIND_ACTION, ui.FIND_DESKTOP_KEY, "Desktop"))
            settle()
            self.assert_on_hud()                                                  # Find's Desktop: the Dock's
            self.assertTrue(self.desktop.leave_hud())
            settle()
        close.assert_not_called()
        self.assertEqual(self.closes, [])
        self.assertEqual(changes, [True, False, True, False])
        self.assertIs(self.desktop._bring_forward, ui.bring_forward_on_click)    # back on the click: no Alt

    def test_a_close_of_the_hud_window_brings_the_window_back(self) -> None:
        self.make()
        self.enter()
        self.hud().close()                                    # as Windows could ask: Jarvis never does
        settle()
        self.assert_in_window(ui.SCREEN_HOME)
        self.assertEqual(self.closes, [])
        self.assertEqual(self.c.state, ui.STATE_READING)

    def test_a_close_of_the_hidden_window_from_outside_quits_never_the_prompt(self) -> None:
        self.make(CLASSIC)
        self.enter()
        self.c._pending_run = "PM"                            # the classic prompt would come back on Done
        with mock.patch.object(ui, "_remove_audio_dir_after"), mock.patch.object(ui, "force_foreground") as forced:
            self.window.close()                               # Windows' close (End task): Jarvis never asks for one
            settle()
        forced.assert_not_called()
        self.assertEqual(self.c.state, ui.STATE_QUITTING)
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.desktop.overlay.is_shown())


class NoGreetingTests(HudApp):
    def test_coming_back_is_not_a_restore_no_greeting_and_no_start_up(self) -> None:
        self.make()
        voice = self.harness.voice
        clock = [5000.0]
        startup = self.reading.startup
        startup.set_clock(lambda: clock[0])
        self.window.hide()                                    # control: a long absence replays the start-up
        clock[0] += alive.REPLAY_AFTER_S + 60
        self.window.show()
        settle()
        plays = startup.plays()
        self.assertGreaterEqual(plays, 2)
        self.c._last_greeting_at = -1e9                       # a restore would greet now
        voice.said.clear()
        for leave in ("back", "window key"):
            self.enter()
            clock[0] += alive.REPLAY_AFTER_S + 60             # a long HUD session
            with mock.patch.object(ui, "force_foreground") as forced:
                if leave == "back":
                    self.back()
                else:
                    self.c.handle_activation(dict(OPEN))
                    settle()
            forced.assert_not_called()                        # back on the click, never force_foreground
            self.assert_in_window(ui.SCREEN_HOME)
            self.assertNotIn("greeting", voice.kinds(), leave)
            self.assertEqual(startup.plays(), plays, leave)  # the HUD counted as present
        self.window.hide()                                    # control: the window key on a hidden window greets
        settle()
        self.c.handle_activation(dict(OPEN))
        settle()
        self.assertIn("greeting", voice.kinds())

    def test_the_window_key_with_ask_comes_back_on_home_with_the_bar(self) -> None:
        self.make()
        self.reading.set_tab(ui.SCREEN_APPROVALS)
        self.enter()
        with mock.patch.object(ui, "force_foreground") as forced:
            self.c.handle_activation({**OPEN, "ask": True})
            settle()
        forced.assert_not_called()
        self.assert_in_window(ui.SCREEN_HOME)
        self.assertNotIn("greeting", self.harness.voice.kinds())

    def test_a_read_launch_comes_back_on_briefing_and_plays(self) -> None:
        self.make()
        self.enter()
        with mock.patch.object(ui, "force_foreground") as forced:
            self.c.handle_activation({"cmd": "activate", "run": None, "now": False, "read": True})
            settle()
        forced.assert_not_called()
        self.assert_in_window(ui.SCREEN_BRIEFING)
        self.assertEqual(self.c.player.state, "playing")


class NothingForwardTests(HudApp):
    def test_no_force_foreground_or_synthetic_alt_while_on_the_hud(self) -> None:
        self.make()
        self.assertTrue(self.reading.is_new_briefing())
        self.enter()
        with mock.patch.object(ui, "force_foreground") as forced, \
                mock.patch.object(ui, "show_without_activating") as quiet, \
                mock.patch.object(ui, "_restore_without_activating") as restore, \
                mock.patch.object(ui, "_win32_bring_to_front") as alt:
            self.reading.set_tab(ui.SCREEN_LIVE)                          # an automatic screen change
            self.assertFalse(self.c._live_auto_open(_live.TASK_ACTION, 41))   # Live would open by itself
            self.assertTrue(self.c._announce())                         # a NEW briefing: the toast path
            self.c.handle_activation({"cmd": "activate", "run": "PM", "now": False})   # a scheduled launch
            self.c._on_live_link("tab:jarvis")                            # the Live pop-out's JARVIS link
            settle()
            self.assert_on_hud()
        for patched in (forced, quiet, restore, alt):
            patched.assert_not_called()
        toast = self.c._toast_window
        self.assertTrue(toast is not None and toast.isVisible())
        self.assertIs(self.desktop.overlay.neighbours()[0], toast)        # handed to the HUD
        self.assertFalse(toast.geometry().intersects(self.hud().occupied_rect()))
        self.assertFalse(self.window.isActiveWindow())
        self.assertEqual(self.closes, [])

    def test_set_tab_on_the_hud_only_records_the_screen(self) -> None:
        self.make()
        self.enter()
        screens: list[int] = []
        self.reading.tabChanged.connect(screens.append)
        self.reading.set_tab(ui.SCREEN_APPROVALS)
        self.reading.set_tab(ui.SCREEN_MEMORY)               # its slot is hidden (no bundle): not even recorded
        settle()
        self.assertEqual(screens, [])
        self.assertEqual((self.reading.current_tab(), self.reading.dock.current()), (ui.SCREEN_HOME, dock.HOME))
        self.assertEqual(self.desktop.return_screen(), ui.SCREEN_APPROVALS)
        self.assert_on_hud()
        self.back()
        self.assert_in_window(ui.SCREEN_APPROVALS)            # it comes back where it was asked to be
        self.assertEqual(screens, [ui.SCREEN_APPROVALS])
        self.enter()                                          # a new visit starts afresh: Home again
        self.assertEqual(self.desktop.return_screen(), ui.SCREEN_HOME)
        self.back()
        self.assert_in_window(ui.SCREEN_HOME)
        self.reading.set_tab(ui.SCREEN_APPROVALS)             # off the HUD set_tab works as always
        self.assertEqual(self.reading.current_tab(), ui.SCREEN_APPROVALS)

    def test_live_never_opens_by_itself_on_the_hud(self) -> None:
        self.make()
        self.c.pop_out_live(activate=False)                   # the Live pop-out, minimised
        self.c._live_window.showMinimized()
        settle()
        self.enter()
        with mock.patch.object(ui, "_restore_without_activating") as restore:
            self.assertFalse(self.c._live_auto_open(_live.TASK_ACTION, 7))
            self.assertFalse(self.c._live_auto_open(_live.TASK_ACTION, 7))   # once per task: nothing later
        restore.assert_not_called()                           # not even the minimised pop-out comes back
        self.assertEqual(self.desktop.return_screen(), ui.SCREEN_HOME)      # no screen recorded either
        self.back()
        with mock.patch.object(ui, "_restore_without_activating") as restore:
            self.assertFalse(self.c._live_auto_open(_live.TASK_ACTION, 8))
        restore.assert_called_once()                          # control: off the HUD the pop-out comes back
        self.c.dock_live()
        self.reading.set_tab(ui.SCREEN_BRIEFING)
        self.assertTrue(self.c._live_auto_open(_live.TASK_ACTION, 9))       # ... and without it the tab opens
        self.assertEqual(self.reading.current_tab(), ui.SCREEN_LIVE)

    def test_a_classic_launch_on_the_hud_waits_for_the_window(self) -> None:
        self.make(CLASSIC)
        self.assertFalse(self.c._reading_is_outdated("AM"))
        self.assertTrue(self.c._reading_is_outdated("PM"))
        self.enter()
        with mock.patch.object(ui, "force_foreground") as forced:
            self.c.handle_activation({"cmd": "activate", "run": "AM", "now": False})   # the run shown: forward
            settle()
            self.assert_on_hud()
            self.c.handle_activation({"cmd": "activate", "run": "PM", "now": False})
            settle()
            self.c._offer_pending_run()                       # as when a reading ends on the HUD
            settle()
        forced.assert_not_called()
        self.assert_on_hud()
        self.assertEqual(self.c._pending_run, "PM")
        self.back()
        settle()
        self.assertEqual(self.c.state, ui.STATE_PROMPT)       # asked about now that the window is back
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window.is_reading_view())


class PresenceTests(HudApp):
    def test_the_agenda_the_offers_and_the_minimise_count_the_hud_as_present(self) -> None:
        self.make()
        self.assertTrue(self.c._agenda_active())
        self.assertEqual(self.c._prod_activity(), "visible")
        self.enter()
        self.assertTrue(self.c._agenda_active())             # the agenda still refreshes by itself
        self.assertTrue(self.c._agenda_timer.isActive())
        self.assertEqual(self.c._prod_activity(), "minimized")   # offers take the toast path
        self.assertFalse(self.c._minimized)                  # the hide was not a minimise
        self.c._on_window_state()
        self.assertFalse(self.c._minimized)
        self.back()
        self.assertEqual(self.c._prod_activity(), "visible")
        self.assertFalse(self.c._minimized)
        self.window.hide()                                    # control: a hidden window without the HUD is away
        settle()
        self.assertFalse(self.c._agenda_active())
        self.assertEqual(self.c._prod_activity(), "off")


class ChipTests(HudApp):
    def test_the_chips_mirror_the_counts_live_and_go_to_their_screens(self) -> None:
        self.make()
        reading = self.reading
        reading.set_meetings_available(True)                  # the Meetings screen (no recorder here)
        self.assertTrue(reading.is_new_briefing())
        self.assertEqual(reading.pending_count(), 2)
        self.enter()
        win = self.hud()
        self.assertEqual(win.chip_texts(), [ui.HOME_BRIEFING_CHIP, ui.needs_ok_chip(2)])
        self.assertEqual(win.state_word(), "NEEDS YOU")       # the orb takes the window's look
        first = self.c._actions[0].id
        self.c.deny_action(first)                             # one decided while on the HUD: live
        settle()
        self.assertEqual(reading.pending_count(), 1)
        self.assertEqual(win.chip_texts(), [ui.HOME_BRIEFING_CHIP, ui.needs_ok_chip(1)])
        reading.set_recording("0:42")                         # the meetings tick, each second
        self.assertEqual(win.chip_texts()[-1], "REC 0:42")
        reading.set_recording("0:43", warning=meetings_view.CALL_MISSING_TEXT)
        self.assertEqual(win.chip_texts()[-1], f"REC 0:43 {DOT} {meetings_view.CALL_MISSING_TEXT}")
        self.assertEqual(win.cut_texts(), [])
        self.chip(hud_overlay.CHIP_APPROVALS)
        self.assert_in_window(ui.SCREEN_APPROVALS)
        self.enter()
        self.chip(hud_overlay.CHIP_REC)
        self.assert_in_window(ui.SCREEN_MEETINGS)
        reading.set_recording("")
        self.enter()
        self.assertEqual(self.hud().chip_texts(), [ui.HOME_BRIEFING_CHIP, ui.needs_ok_chip(1)])
        self.chip(hud_overlay.CHIP_BRIEFING)
        self.assert_in_window(ui.SCREEN_BRIEFING)
        self.c._mark_viewed_if_seen()                         # seen in the active window: no longer NEW
        self.assertFalse(reading.is_new_briefing())
        self.enter()
        self.assertEqual(self.hud().chip_texts(), [ui.needs_ok_chip(1)])
        self.c.deny_action(self.c._actions[1].id)
        settle()
        self.assertEqual(self.hud().chip_texts(), [])         # nothing waits: no chip
        self.assertEqual(self.closes, [])

    def test_the_toast_click_on_the_hud_brings_the_window_back_on_its_screen(self) -> None:
        self.make()
        self.enter()
        with mock.patch.object(ui, "force_foreground") as forced:
            self.c._post_toast("Your AM briefing is ready, sir.", "View", ui.TOAST_VIEW)
            toast = self.c._toast_window
            self.assertTrue(toast.isVisible())
            self.assert_on_hud()
            toast.action_button.click()
            settle()
        forced.assert_not_called()
        self.assert_in_window(ui.SCREEN_BRIEFING)


class ReentryTests(HudApp):
    def test_double_clicks_and_strays_change_nothing(self) -> None:
        self.make()
        changes: list[bool] = []
        self.desktop.hudChanged.connect(changes.append)
        self.reading.desktopRequested.emit()
        self.reading.desktopRequested.emit()                  # a double click
        settle()
        self.assertEqual(changes, [True])
        self.assertFalse(self.desktop.enter_hud())            # enter while on the HUD
        self.assertEqual(changes, [True])
        self.assert_on_hud()
        win = self.hud()
        win.bringBackClicked.emit()
        win.bringBackClicked.emit()                           # a double click on Bring Jarvis back
        win.chipClicked.emit(hud_overlay.CHIP_APPROVALS)      # ... and a chip after it
        settle()
        self.assertEqual(changes, [True, False])
        self.assert_in_window(ui.SCREEN_HOME)
        self.assertFalse(self.desktop.leave_hud())            # leave while not on the HUD
        self.assertFalse(self.desktop.leave_hud(ui.SCREEN_APPROVALS))
        self.assertEqual(self.reading.current_tab(), ui.SCREEN_HOME)
        self.desktop.record_screen(ui.SCREEN_APPROVALS)       # a stray record off the HUD: nothing
        self.assertEqual(self.desktop.return_screen(), ui.SCREEN_HOME)
        nested: list[tuple[bool, bool]] = []

        def during() -> None:                                 # an enter or leave asked for during a switch
            nested.append((self.desktop.enter_hud(), self.desktop.leave_hud()))

        self.window.visibilityChanged.connect(during)
        try:
            self.assertTrue(self.desktop.enter_hud())
            self.assertTrue(self.desktop.leave_hud())
        finally:
            self.window.visibilityChanged.disconnect(during)
        settle()
        self.assertTrue(nested)
        self.assertEqual(set(nested), {(False, False)})
        self.assertEqual(changes, [True, False, True, False])
        self.assert_in_window(ui.SCREEN_HOME)
        self.assertEqual(self.closes, [])

    def test_the_app_side_and_the_consent_gate_refuse_closed(self) -> None:
        self.make()
        desktop = self.desktop
        self.assertTrue(desktop.consent_given())

        class Broken:
            @property
            def desktop(self) -> object:
                raise RuntimeError("unreadable")

        for config in (SimpleNamespace(), SimpleNamespace(desktop=None), SimpleNamespace(desktop=DesktopConfig()),
                       SimpleNamespace(desktop=SimpleNamespace(enabled="yes")), Broken()):
            desktop.config = config
            self.assertFalse(desktop.consent_given(), config)
            self.assertFalse(desktop.enter_hud(), config)
            self.assertTrue(self.window.isVisible())
        desktop.config = self.c.config
        with mock.patch.object(QApplication, "activeModalWidget", return_value=QWidget()):
            self.assertFalse(desktop.enter_hud())             # a dialog waits for an answer
        desktop._may_enter = mock.Mock(side_effect=RuntimeError("broken"))
        with self.assertLogs("briefing_reader.desktop_ui", level="WARNING") as logs:
            self.assertFalse(desktop.enter_hud())             # unknown: not now
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNotIn("broken", "\n".join(logs.output))   # the type only, never the text
        desktop._may_enter = self.c._may_enter_hud
        self.c.state = ui.STATE_PROMPT
        self.assertFalse(desktop.enter_hud())                 # only from the assistant screen
        self.c.state = ui.STATE_READING
        self.assertTrue(desktop.enter_hud())
        self.assertTrue(desktop.leave_hud())


class TrayTests(HudApp):
    def test_bring_jarvis_back_shows_in_the_tray_only_on_the_hud(self) -> None:
        self.make()
        menu = self.c._make_tray_menu()
        self.addCleanup(menu.deleteLater)
        self.assertEqual([action.text() for action in menu.actions()],
                         [desktop_ui.BRING_BACK_TEXT, "Show briefing", "Dismiss"])
        back = self.c._tray_back_action
        self.assertFalse(back.isVisible())
        self.reading.set_tab(ui.SCREEN_APPROVALS)
        self.enter()
        self.assertTrue(back.isVisible())
        with mock.patch.object(ui, "force_foreground") as forced:
            back.trigger()
            settle()
            forced.assert_not_called()
            self.assert_in_window(ui.SCREEN_HOME)
            self.assertFalse(back.isVisible())
            self.enter()
            self.c._on_tray_show()                            # the tray icon's click on the HUD: the same
            settle()
            forced.assert_not_called()
        self.assert_in_window(ui.SCREEN_HOME)
        back.trigger()                                        # off the HUD it does nothing
        settle()
        self.assertTrue(self.window.isVisible())
        self.assertNotIn("greeting", self.harness.voice.kinds())

    def test_feature_off_no_hud_no_dock_item_no_tray_item(self) -> None:
        before = {widget.objectName() for widget in QApplication.topLevelWidgets()}
        self.make(enabled=False)
        reading = self.reading
        self.assertIsNone(self.c.desktop)
        self.assertFalse(self.c._hud_active())
        self.assertNotIn(dock.DESKTOP, reading.dock.visible_slots())
        self.assertFalse(reading.dock.button(dock.DESKTOP).isVisible())
        reading.desktopRequested.emit()
        reading.dock.activate(dock.DESKTOP)
        settle()
        self.assertTrue(self.window.isVisible())
        made = {widget.objectName() for widget in QApplication.topLevelWidgets()} - before
        self.assertFalse(made & HUD_NAMES, made)
        menu = self.c._make_tray_menu()
        self.addCleanup(menu.deleteLater)
        self.assertEqual([action.text() for action in menu.actions()], ["Show briefing", "Dismiss"])
        self.assertIsNone(self.c._tray_back_action)
        reading.set_tab(ui.SCREEN_APPROVALS)                  # set_tab and the presence as always
        self.assertEqual(reading.current_tab(), ui.SCREEN_APPROVALS)
        self.assertIsNone(reading._screen_hold)
        self.assertFalse(reading._hud_present)


class ShutdownTests(HudApp):
    def test_shutdown_on_the_hud_closes_nothing_and_the_hud_goes(self) -> None:
        self.make()
        self.enter()
        overlay = self.desktop.overlay
        windows = overlay.windows()
        with mock.patch.object(ui, "_remove_audio_dir_after"), mock.patch.object(self.window, "close") as close:
            self.c.shutdown()
            settle()
        close.assert_not_called()
        self.assertEqual(self.closes, [])
        self.assertEqual(self.c.state, ui.STATE_QUITTING)
        self.assertFalse(self.desktop.is_hud())
        self.assertFalse(overlay.is_shown())
        self.assertFalse(self.window.isVisible())
        self.assertIsNone(self.reading._screen_hold)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(any(shiboken6.isValid(window) for window in windows))
        self.assertFalse(self.desktop.enter_hud())            # shut: nothing more
        self.assertFalse(self.desktop.leave_hud())
        self.reading.set_pending(3, 3)                        # a late change reaches no deleted window
        self.desktop.shutdown()                               # twice is fine


class MeetingsOnHudTests(HudApp):
    def make_meetings(self) -> None:
        self.make(MEETINGS_ON)
        self.clock = Clock(NOW.replace(tzinfo=None))
        self.backend = FakeBackend()
        m = self.m = self.c.meetings
        m.threaded = False
        m._mono, m._now = self.clock.mono, self.clock.wall
        m.store._now = self.clock.wall
        m.stage.card.set_clock(self.clock.wall)
        m.backend_factory = lambda: (self.backend, "")
        m.audio_missing = lambda: ""
        m.speech_missing = lambda: "faster-whisper"           # nothing is written down here
        m.summary_runner = None
        m.start()

    def tick(self, seconds: int) -> None:
        for _ in range(seconds):
            self.clock.advance(1.0)
            self.backend.live(mt.MIC).feed(tone(1.0, 3000))
            self.m.recorder.tick()
        self.m._sync_indicators()
        settle()

    def test_going_onto_the_hud_closes_the_start_card_as_not_now(self) -> None:
        self.make_meetings()
        self.m.open_start_card()
        settle()
        card = self.m.stage.card
        card.tick.click()
        card.summary_tick.click()
        self.assertTrue(card.isVisible() and card.tick.isChecked() and card.summary_tick.isChecked())
        self.enter()
        self.assertFalse(card.isVisible())
        self.assertFalse(card.tick.isChecked() or card.summary_tick.isChecked())
        self.assertFalse(self.m.is_recording())
        self.assertEqual(self.m.store.list(), [])             # nothing recorded, nothing kept
        self.back()
        self.assertFalse(card.isVisible())                    # it does not come back by itself
        self.assertEqual(self.m.store.list(), [])

    def test_a_recording_started_on_the_hud_gives_a_rec_chip_and_a_held_pill(self) -> None:
        self.make_meetings()
        self.enter()
        self.assertIsNone(self.m.pill)
        request = mt.StartRequest(mt.MeetingInfo(), consent=True, consent_at=self.clock.wall())
        self.assertTrue(self.m.start_recording(request))
        settle()
        pill = self.m.pill
        self.assertIsNotNone(pill)                            # made now, handed to the HUD at once
        self.assertIs(self.desktop.overlay.neighbours()[1], pill)
        self.assertTrue(pill.is_held())
        self.assertFalse(pill.isVisible())
        self.assertEqual(self.hud().chip_texts()[-1], "REC 0:00")
        self.tick(3)
        self.assertEqual(self.hud().chip_texts()[-1], "REC 0:03")     # live, each second
        self.assertEqual(pill.text(), "Recording 0:03")               # it keeps its time, hidden
        self.assertFalse(pill.isVisible())
        self.chip(hud_overlay.CHIP_REC)
        self.assert_in_window(ui.SCREEN_MEETINGS)
        self.assertFalse(pill.is_held())
        self.tick(1)
        self.assertTrue(pill.isVisible())                     # back at its next tick
        self.enter()
        self.assertTrue(pill.is_held() and not pill.isVisible())
        self.assertTrue(self.m.stop())
        settle()
        self.assertNotIn("REC", " ".join(self.hud().chip_texts()))
        self.back()


class FakeUser32:
    """user32 as the bring-forward uses it, recorded: no window is touched (``agree``: SetForegroundWindow works)."""

    def __init__(self, *, agree: bool = True) -> None:
        self.agree = agree
        self.foreground = 77                                  # another app's window
        self.calls: list[str] = []

    def GetForegroundWindow(self) -> int:  # noqa: N802 - the Win32 name
        self.calls.append("GetForegroundWindow")
        return self.foreground

    def GetWindowThreadProcessId(self, hwnd: int, _pid: object) -> int:  # noqa: N802
        self.calls.append("GetWindowThreadProcessId")
        return 4242

    def AttachThreadInput(self, *_args: object) -> bool:  # noqa: N802
        self.calls.append("AttachThreadInput")
        return True

    def BringWindowToTop(self, _hwnd: int) -> bool:  # noqa: N802
        self.calls.append("BringWindowToTop")
        return True

    def SetForegroundWindow(self, hwnd: int) -> bool:  # noqa: N802
        self.calls.append("SetForegroundWindow")
        if self.agree:
            self.foreground = int(hwnd)
        return self.agree

    def keybd_event(self, *_args: object) -> None:  # noqa: N802
        self.calls.append("keybd_event")


class BringForwardTests(unittest.TestCase):
    def test_the_owners_click_brings_the_window_forward_without_a_synthetic_alt(self) -> None:
        widget = QWidget()
        self.addCleanup(widget.deleteLater)
        widget.resize(300, 200)
        kernel32 = SimpleNamespace(GetCurrentThreadId=lambda: 1)
        for agree in (True, False):
            user32 = FakeUser32(agree=agree)
            widget.hide()
            with mock.patch.object(ui, "_native_windows", return_value=True), \
                    mock.patch.object(ui, "_user32", return_value=user32), \
                    mock.patch.object(ui.ctypes, "WinDLL", return_value=kernel32):
                ui.bring_forward_on_click(widget)
            self.assertTrue(widget.isVisible())
            self.assertIn("SetForegroundWindow", user32.calls)
            self.assertNotIn("keybd_event", user32.calls, agree)   # refused or not: never the Alt key
        user32 = FakeUser32(agree=False)                      # control: force_foreground's last resort is Alt
        with mock.patch.object(ui, "_native_windows", return_value=True), \
                mock.patch.object(ui, "_user32", return_value=user32), \
                mock.patch.object(ui.ctypes, "WinDLL", return_value=kernel32):
            ui.force_foreground(widget)
        self.assertIn("keybd_event", user32.calls)

    def test_a_minimised_window_is_restored(self) -> None:
        widget = QWidget()
        self.addCleanup(widget.deleteLater)
        widget.resize(300, 200)
        widget.showMinimized()
        settle()
        ui.bring_forward_on_click(widget)
        settle()
        self.assertTrue(widget.isVisible())
        self.assertFalse(widget.isMinimized())
        widget.hide()


if __name__ == "__main__":
    unittest.main()
