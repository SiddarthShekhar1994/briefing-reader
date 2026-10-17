"""Tests for the header's minimize button and what minimizing the window does.

The button sits left of the close button in the header of both views, the
frameless window keeps a taskbar button (a plain top-level window that is
never a tool window), and the prompt's header still fits its width with both
buttons. Minimizing the prompt stops its "Asking again" countdown and asks
again after the short Later (restored on top, without the focus); restoring
it by hand starts the countdown again. The hotkey's activation restores a
minimized window, a reading keeps playing while minimized, and a Send's
hidden hold goes on (the owner approved it and saw Sent; there is no countdown to watch).

Qt runs offscreen in this process with the bundled fonts and shows nothing;
no Notion (fetching is stubbed), no Google (calendar off), no audio (a fake
player), no .env.
"""

from __future__ import annotations

import dataclasses
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from briefing_reader import config, hud, ui
from briefing_reader.actions import parse_action_line
from briefing_reader.config import load_config
from briefing_reader.notion_client import FixtureSession, NotionClient

PDT = timezone(timedelta(hours=-7))
NOW = datetime(2026, 10, 6, 9, 0, tzinfo=PDT)
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fake_page.json"
_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_window_minimize", "-platform", "offscreen"])
    hud.load_fonts(config.PROJECT_ROOT / "fonts")


def settle() -> None:
    for _ in range(3):
        QApplication.processEvents()


def header_with_chips(hour24: bool = False) -> hud.HeaderBar:
    header = hud.HeaderBar()
    header.set_hour24(hour24)
    header.set_clock(lambda: datetime(2026, 10, 4, 13, 52))
    for name in ("notion", "voice", "calendar"):
        header.set_service(name, hud.STATUS_OK)
    return header


class HeaderButtonTests(unittest.TestCase):

    def setUp(self) -> None:
        self.header = header_with_chips()
        self.addCleanup(self.header.deleteLater)

    def test_minimize_button_looks_and_behaves_like_close(self) -> None:
        minimize, close = self.header.minimize_button, self.header.close_button
        self.assertEqual((minimize.accessibleName(), minimize.toolTip()), ("Minimize", "Minimize"))
        self.assertEqual((close.accessibleName(), close.toolTip()), ("Close", "Close"))
        for button in (minimize, close):
            with self.subTest(button=button.accessibleName()):
                self.assertEqual(button.size().toTuple(), (30, 30))
                self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.TabFocus)
                self.assertFalse(button.isDefault() or button.autoDefault())

    def test_a_click_on_minimize_asks_to_minimize_only(self) -> None:
        minimized, closed = [], []
        self.header.minimizeRequested.connect(lambda: minimized.append(1))
        self.header.closeRequested.connect(lambda: closed.append(1))
        self.header.minimize_button.click()
        self.assertEqual((minimized, closed), ([1], []))
        self.header.close_button.click()
        self.assertEqual((minimized, closed), ([1], [1]))

    def test_minimize_sits_just_left_of_close_at_the_right_end(self) -> None:
        self.header.resize(1100, self.header.height())
        self.header.show()
        settle()
        minimize = self.header.minimize_button.geometry()
        close = self.header.close_button.geometry()
        self.assertEqual(close.right() + 1, self.header.width() - self.header._RIGHT_MARGIN)
        self.assertEqual(minimize.right() + 1 + self.header._BUTTON_GAP, close.left())
        self.assertEqual(minimize.center().y(), close.center().y())
        self.assertLessEqual(self.header._clock.geometry().right(), minimize.left())
        self.header.hide()

    def test_the_glyph_is_one_bar_not_an_x(self) -> None:
        image = self.header.minimize_button.grab().toImage()
        scale = image.width() / 30
        muted = QColor(hud.TEXT_MUTED)

        def lit(x: float, y: float) -> bool:
            pixel = image.pixelColor(round(x * scale), round(y * scale))
            return pixel.alpha() > 120 and abs(pixel.green() - muted.green()) < 60

        self.assertTrue(all(lit(x, 15.5) for x in (11, 13, 15, 17, 19)))   # the bar, at mid height
        self.assertFalse(any(lit(x, y) for x, y in ((10, 10), (20, 10), (10, 20), (20, 20), (15, 11))))

    def test_both_clocks_fit_the_prompt_header_with_both_buttons(self) -> None:
        left, _top, right, _bottom = ui._PROMPT_MARGINS
        room = ui.PROMPT_SIZE.width() - left - right
        for hour24 in (False, True):
            with self.subTest(hour24=hour24):
                header = header_with_chips(hour24)
                self.addCleanup(header.deleteLater)
                widest = header._required_width(header._max_level())
                self.assertLessEqual(widest, room)
                self.assertEqual(header.minimumSizeHint().width(), widest)
                self.assertEqual(header._buttons_width(), 30 + 2 + 30)


class FrameTests(unittest.TestCase):

    def test_the_window_taskbar_and_tray_name_jarvis(self) -> None:
        window = ui.BriefingWindow(10, 30)
        self.addCleanup(window.deleteLater)
        assert window.header is not None
        self.assertEqual(window.windowTitle(), "Jarvis")
        self.assertEqual(window.header.subtitle(), f"assistant {hud.MIDDLE_DOT} desktop")
        header = hud.HeaderBar()
        self.addCleanup(header.deleteLater)
        self.assertEqual(header.subtitle(), f"assistant {hud.MIDDLE_DOT} desktop")   # the default too
        self.assertEqual(ui.TRAY_TOOLTIP, "Jarvis")

    def test_a_plain_top_level_window_keeps_its_taskbar_button(self) -> None:
        frame = hud.HudWindowFrame()
        self.addCleanup(frame.deleteLater)
        flags = frame.windowFlags()
        self.assertEqual(flags & Qt.WindowType.WindowType_Mask, Qt.WindowType.Window)   # never Qt.Tool
        self.assertIsNone(frame.parentWidget())                                          # never owned
        for hint in (Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint,
                     Qt.WindowType.WindowMinimizeButtonHint):
            with self.subTest(hint=hint):
                self.assertTrue(flags & hint)

    def test_the_minimize_button_minimizes_and_show_normal_restores(self) -> None:
        frame = hud.HudWindowFrame()
        self.addCleanup(frame.deleteLater)
        frame.resize(600, 300)
        frame.show()
        settle()
        frame.header.minimize_button.click()
        settle()
        self.assertTrue(frame.isMinimized())
        self.assertTrue(frame.isVisible())   # minimized, not hidden: the taskbar button stays
        frame.showNormal()
        settle()
        self.assertFalse(frame.isMinimized())
        frame.close()

    def test_show_without_activating_restores_a_minimized_window(self) -> None:
        frame = hud.HudWindowFrame()
        self.addCleanup(frame.deleteLater)
        frame.resize(600, 300)
        frame.show()
        frame.showMinimized()
        settle()
        ui.show_without_activating(frame)
        settle()
        self.assertTrue(frame.isVisible())
        self.assertFalse(frame.isMinimized())
        frame.close()

    def test_both_views_show_both_buttons_inside_the_header(self) -> None:
        for hour24 in (False, True):
            window = ui.BriefingWindow(10, 30)
            self.addCleanup(window.deleteLater)
            window.set_hour24(hour24)
            window.header.set_clock(lambda: datetime(2026, 10, 4, 13, 52))
            for name in ("notion", "voice", "calendar"):
                window.header.set_service(name, hud.STATUS_WARN)
            for view in ("prompt", "reading"):
                with self.subTest(hour24=hour24, view=view):
                    if view == "prompt":
                        window.show_prompt_view()
                    else:
                        window.show_reading_view()
                        window.resize(ui.READING_MIN_SIZE)
                    window.show()
                    settle()
                    if view == "prompt":
                        self.assertEqual(window.width(), ui.PROMPT_SIZE.width())
                        header = window.header
                        minimize = header.minimize_button.geometry()
                        close = header.close_button.geometry()
                        self.assertTrue(header.minimize_button.isVisible() and header.close_button.isVisible())
                        self.assertLessEqual(close.right(), header.width() - 1)
                        self.assertLessEqual(header._clock.geometry().right(), minimize.left())
                        self.assertLessEqual(header._chips_box.geometry().right(), header._clock.geometry().left())
                    else:
                        # The reading view's title bar is the hologram's header (the frame's is hidden).
                        self.assertFalse(window.header.isVisible())
                        holo_header = window.reading.header
                        minimize = holo_header.minimize_button.geometry()
                        close = holo_header.close_button.geometry()
                        self.assertTrue(holo_header.minimize_button.isVisible()
                                        and holo_header.close_button.isVisible())
                        self.assertLessEqual(close.right(), holo_header.width() - 1)
                        self.assertLessEqual(holo_header.clock.geometry().right(), minimize.left())
                        self.assertLessEqual(holo_header.readout.geometry().right(),
                                             holo_header.clock.geometry().left())
            window.hide()


class FakePlayer(QObject):
    """Stands in for BriefingPlayer (no QtMultimedia): it only keeps its state."""

    itemChanged = Signal(int, int)
    sectionStarted = Signal(int)
    stateChanged = Signal(str)
    errorOccurred = Signal(str)

    def __init__(self, parent: QObject | None = None, **_kwargs) -> None:
        super().__init__(parent)
        self.state = "idle"
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1
        self.state = "idle"


class ControllerTests(unittest.TestCase):

    def make(self, *, startup_error: str | None = None) -> ui.AppController:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        base = load_config(root, environ={"LOCALAPPDATA": str(root / "appdata")})   # no config.toml: defaults
        cfg = dataclasses.replace(base, page_id="0" * 32, audio_root=root / "audio",
                                  calendar=dataclasses.replace(base.calendar, enabled=False))
        client = NotionClient("", session=FixtureSession(FIXTURE), notion_version=cfg.notion_version)
        with mock.patch.object(ui, "BriefingPlayer", FakePlayer):
            c = ui.AppController(cfg, client, expected_run="AM", now_mode=False, startup_error=startup_error,
                                 volume=0.0, now_func=lambda: NOW)
        c._start_fetch = lambda *args, **kwargs: None   # never Notion
        self.addCleanup(self.close, c)
        return c

    @staticmethod
    def close(c: ui.AppController) -> None:
        for timer in (c._ignore_timer, c._tick_timer, c._snooze_timer, c._send_hold_timer, c._agenda_timer):
            timer.stop()
        c.state = ui.STATE_QUITTING
        c.window.hide()
        c.window.deleteLater()
        settle()

    def prompt(self, **kwargs) -> ui.AppController:
        c = self.make(**kwargs)
        c.show_prompt(take_focus=False)
        settle()
        self.assertEqual(c.state, ui.STATE_PROMPT)
        return c

    def minimize(self, c: ui.AppController) -> None:
        c.window.header.minimize_button.click()
        settle()
        self.assertTrue(c.window.isMinimized())

    def test_minimizing_the_prompt_asks_again_after_the_short_later(self) -> None:
        c = self.prompt()
        self.assertTrue(c._ignore_timer.isActive())
        self.assertTrue(c.window.prompt.countdown.text().startswith("Asking again in 10 min"))
        self.minimize(c)
        self.assertEqual(c.state, ui.STATE_PROMPT)       # still answerable from the taskbar
        self.assertTrue(c.window.isVisible())
        self.assertFalse(c._ignore_timer.isActive() or c._tick_timer.isActive())
        self.assertEqual(c.window.prompt.countdown.text(), "")
        self.assertTrue(c._snooze_timer.isActive())
        self.assertEqual(c._snooze_timer.interval(), c.config.prompt.later_short_minutes * 60_000)

    def test_a_prompt_restored_by_hand_starts_its_countdown_again(self) -> None:
        c = self.prompt()
        self.minimize(c)
        c.window.showNormal()   # its taskbar button
        settle()
        self.assertFalse(c._snooze_timer.isActive())
        self.assertTrue(c._ignore_timer.isActive())
        self.assertEqual(c._ignore_timer.interval(), c.config.prompt.ignore_after_seconds * 1000)
        self.assertGreater(c._ignore_timer.remainingTime(), (c.config.prompt.ignore_after_seconds - 5) * 1000)
        self.assertTrue(c.window.prompt.countdown.isVisibleTo(c.window.prompt))

    def test_the_minimized_prompt_comes_back_by_itself_without_the_focus(self) -> None:
        c = self.prompt()
        self.minimize(c)
        with mock.patch.object(ui, "force_foreground") as forced:
            c._snooze_timer.timeout.emit()
            settle()
        forced.assert_not_called()   # refocus_on_reprompt = false: on top, the focus stays put
        self.assertEqual(c.state, ui.STATE_PROMPT)
        self.assertTrue(c.window.isVisible())
        self.assertFalse(c.window.isMinimized())
        self.assertFalse(c._snooze_timer.isActive())
        self.assertTrue(c._ignore_timer.isActive())

    def test_without_later_a_minimized_prompt_just_waits(self) -> None:
        c = self.prompt(startup_error="NOTION_TOKEN is not set")
        self.minimize(c)
        self.assertFalse(c._snooze_timer.isActive() or c._ignore_timer.isActive())

    def test_close_from_the_taskbar_while_minimized_counts_as_later(self) -> None:
        c = self.prompt()
        self.minimize(c)
        c.window.close()
        settle()
        self.assertEqual(c.state, ui.STATE_SNOOZED)
        self.assertFalse(c.window.isVisible())
        self.assertFalse(c._minimized)
        c._snooze_timer.timeout.emit()   # comes back restored, not minimized
        settle()
        self.assertEqual(c.state, ui.STATE_PROMPT)
        self.assertTrue(c.window.isVisible() and not c.window.isMinimized())

    def test_the_hotkey_restores_a_minimized_prompt_and_reads(self) -> None:
        c = self.prompt()
        self.minimize(c)
        c.handle_activation({"cmd": "activate", "run": None, "now": True})
        settle()
        self.assertEqual(c.state, ui.STATE_READING)
        self.assertTrue(c.window.is_reading_view())
        self.assertTrue(c.window.isVisible())
        self.assertFalse(c.window.isMinimized())
        self.assertFalse(c._snooze_timer.isActive() or c._ignore_timer.isActive())

    def test_the_hotkey_restores_a_minimized_reading_screen(self) -> None:
        c = self.prompt()
        c.enter_reading()
        c.window.show()
        settle()
        self.minimize(c)
        c.handle_activation({"cmd": "activate", "run": None, "now": True})
        settle()
        self.assertEqual(c.state, ui.STATE_READING)
        self.assertFalse(c.window.isMinimized())

    def test_a_reading_keeps_playing_while_minimized(self) -> None:
        c = self.prompt()
        c.enter_reading()
        c.window.show()
        settle()
        c.player.state = "playing"
        self.minimize(c)
        self.assertEqual((c.player.state, c.player.stops), ("playing", 0))
        self.assertFalse(c._agenda_timer.isActive())   # the agenda waits until it is visible again
        c.window.showNormal()
        settle()
        self.assertEqual((c.player.state, c.player.stops), ("playing", 0))

    def test_minimizing_keeps_a_sends_hidden_hold(self) -> None:
        c = self.prompt()
        c.enter_reading()
        c.window.show()
        settle()
        action = parse_action_line("Email: acct=work | to=ana@example.edu | cc= | subject=Dentist moved | due= | link= | "
                                   "body=Hi Ana")
        now = time.monotonic()
        c._send_hold = ui._SendHold(action, deadline=now + 30, started=now)
        c._jobs[action.id] = ui._STAGE_HOLD
        c._send_hold_timer.start()
        with self.assertLogs("briefing_reader.ui", level="INFO") as logs:
            self.minimize(c)
        self.assertIsNotNone(c._send_hold)                        # it goes on out of sight
        self.assertEqual(c._jobs[action.id], ui._STAGE_HOLD)
        self.assertTrue(c._send_hold_timer.isActive())
        self.assertFalse(any("undone" in line for line in logs.output))
        self.assertFalse(any("Dentist" in line for line in logs.output))   # ids only, never titles
        c._send_hold = None
        c._jobs.pop(action.id, None)
        c._send_hold_timer.stop()

    def test_off_approvals_the_header_shows_sent_and_its_undo(self) -> None:
        """The card's Undo is on Approvals; on any other screen the header shows "SENT" with an Undo that
        works (never a number), so the Undo is never out of sight."""
        c = self.prompt()
        c.enter_reading()
        c.window.show()
        settle()
        reading = c.window.reading
        reading.set_tab(ui.SCREEN_APPROVALS)
        action = parse_action_line("Email: acct=work | to=ana@example.edu | cc= | subject=Dentist moved | due= | link= | "
                                   "body=Hi Ana")
        now = time.monotonic()
        c._send_hold = ui._SendHold(action, deadline=now + 30, started=now)
        c._jobs[action.id] = ui._STAGE_HOLD
        c._send_hold_timer.start()
        reading.set_hold("Sent")
        header = reading.header
        self.assertEqual(header.hold_text(), "")                  # on Approvals the card has it
        for screen in (ui.SCREEN_HOME, ui.SCREEN_LIVE, ui.SCREEN_BRIEFING):
            reading.set_tab(screen)
            settle()
            self.assertIsNotNone(c._send_hold, screen)            # leaving the card never stops it
            self.assertEqual(header.hold_text(), "SENT")
            self.assertTrue(header.undo_button.isVisible())
        self.assertIn("Undo is available", header.hold.accessibleName())
        c._send_hold.started -= 5                                 # past the double-click guard
        with self.assertLogs("briefing_reader.ui", level="INFO") as logs:
            header.undo_button.click()
        self.assertIsNone(c._send_hold)
        self.assertNotIn(action.id, c._jobs)
        self.assertTrue(any("undone (undone); nothing was sent" in line for line in logs.output))
        self.assertEqual(reading.activity.entries()[0][2], "Undone: Dentist moved")
        self.assertEqual(header.hold_text(), "")                   # it went with the hold


if __name__ == "__main__":
    unittest.main()
