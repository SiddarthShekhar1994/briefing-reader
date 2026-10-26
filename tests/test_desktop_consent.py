"""THE CONSENT GATE and the first-run "Jarvis on your desktop" card (hard rule 15; SPEC 9, 10.3 and 11; P1.9): until
the owner's Turn on is stored in the app's prefs (fail-closed: unreadable, unsaved or anything but true is a no)
nothing desktop-side works - the Dock's Desktop, the Find list's, the HUD key, a --hud start and a request typed in
the window show the card instead (the window comes forward only on the owner's own key) and the desktop's request
and card clicks refuse; the card's copy (SPEC 10.3's rows live in Phase 1), its keyboard (the focus starts on Not now,
Tab stays on the card, Esc is Not now); Not now leaves nothing on (no prefs entry, no HUD, no desktop thread, no hook);
Turn on stores the OK and goes on to what was asked (the HUD; a typed request waits for Enter); Settings > Desktop
shows "Your OK" and Turn off takes it back.

Qt runs offscreen in this process and shows nothing. No real hotkey, pipe, foreground change, window or OS call: the
HUD's native seam is a recording fake, the desktop's backend a fake over a temporary folder, and the prefs live in the
test's own temporary data folder (AppHarness).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from briefing_reader import desktop_ui, desktop_view, dock, find_view, hud_overlay, prefs, settings_view, ui
from briefing_reader.models import LAUNCH_OPEN
from briefing_reader.prefs import PREFS_FILE, AssistantPrefs
from tests import test_desktop_hud as p14
from tests.desktop_fakes import desktop_disk
from tests.test_desktop_keys import HUD_KEY
from tests.ui_fakes import AppHarness, fonts, plan, settle, wait_for

ESSAY = "open my LING essay"
_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_desktop_consent", "-platform", "offscreen"])
    fonts()


class ConsentApp(p14.HudApp):
    """HudApp (the instant switch, the HUD over a recording seam) with NO stored OK by default (``consent``), the
    desktop's backend a fake over a temporary folder."""

    def make_app(self, extra: str = "", *, consent: bool = False, ask: bool = True, **harness: object) -> AppHarness:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.fake = desktop_disk(tmp.name)
        made = self.make(extra, desktop_consent=consent, desktop_backend=lambda: self.fake, ask=ask, **harness)
        return made

    def prefs_path(self) -> Path:
        return self.harness.config.data_dir / PREFS_FILE

    def stored(self) -> dict:
        path = self.prefs_path()
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def card(self) -> desktop_view.ConsentCard:
        card = self.c._desktop_consent_card
        self.assertIsNotNone(card, "the first-run card was made")
        return card

    def assert_nothing_desktop_side(self) -> None:
        """Not turned on: no HUD, no desktop thread, no window watcher, no HUD window shown, nothing asked of the
        backend; the window as it was."""
        self.assertFalse(self.desktop.is_hud())
        self.assertFalse(self.desktop.consent_given())
        self.assertIsNone(self.desktop._worker)
        self.assertIsNone(self.desktop.results._watch)
        self.assertFalse(any(window.isVisible() for window in self.desktop.overlay.windows()))
        self.assertEqual(self.fake.calls, [])
        self.assertTrue(self.window.isVisible())

    def dock_desktop(self) -> None:
        QTest.mouseClick(self.reading.dock.button(dock.DESKTOP), Qt.MouseButton.LeftButton)
        settle()


class GateTests(ConsentApp):
    def test_every_entry_shows_the_card_instead_until_turn_on(self) -> None:
        self.make_app()
        asked: list[str] = []
        self.desktop.consentNeeded.connect(asked.append)
        self.dock_desktop()                                                  # the Dock's Desktop
        self.assertTrue(self.card().is_open())
        self.assertEqual(self.card().entry(), desktop_ui.ENTRY_DOCK)
        self.assert_nothing_desktop_side()
        self.card().close_card()
        self.reading._on_find_pick(find_view.FindItem(find_view.KIND_ACTION, ui.FIND_DESKTOP_KEY, "Desktop"))
        settle()                                                             # the Find list's Desktop
        self.assertTrue(self.card().is_open())
        self.card().close_card()
        self.reading.dock.activate(dock.DESKTOP)                             # Ctrl+8 (the Dock's slot)
        settle()
        self.assertTrue(self.card().is_open())
        self.assertEqual(asked, [desktop_ui.ENTRY_DOCK] * 3)
        self.assertFalse(self.desktop.enter_hud())                           # the gate itself, asked directly
        self.assertFalse(self.desktop.request(ESSAY))
        self.assert_nothing_desktop_side()
        self.assertEqual(self.stored().get("desktop_turned_on"), None)       # nothing stored

    def test_the_hud_key_brings_the_window_forward_on_the_card(self) -> None:
        self.make_app()
        self.window.showMinimized()
        settle()
        with mock.patch.object(ui, "force_foreground") as forced:
            self.c.handle_activation(dict(HUD_KEY))
            settle()
        forced.assert_called_once_with(self.window)                          # the owner's own key: as the window key
        self.assertFalse(self.window.isMinimized())
        self.assertTrue(self.card().is_open())
        self.assertEqual(self.card().entry(), desktop_ui.ENTRY_KEY)
        self.assertEqual(self.native.keys, [])                               # no HUD bar took the keyboard
        self.assert_nothing_desktop_side()

    def test_a_request_typed_in_the_window_shows_the_card_and_the_words_wait(self) -> None:
        self.make_app(runs=(plan(),))
        self.c.ask.start()
        self.assertTrue(wait_for(lambda: self.c.ask.ready is not None))
        bar = self.reading.command_bar
        bar.set_text(ESSAY)
        bar.submit()
        settle()
        self.assertTrue(self.card().is_open())
        self.assertEqual(self.card().entry(), desktop_ui.ENTRY_REQUEST)
        self.assertEqual(bar.text(), ESSAY)                                  # not taken: Enter again after Turn on
        self.assertIn(ui.DESKTOP_OFF_TEXT, bar.status())
        self.assertEqual(self.harness.factory.runner.started, [])            # no planner run
        self.assert_nothing_desktop_side()

    def test_the_hud_key_starting_jarvis_opens_the_window_on_the_card(self) -> None:
        self.native = p14.FakeNative()
        harness = AppHarness(self, config_extra=p14.DESKTOP_ON, desktop_consent=False, launch=LAUNCH_OPEN,
                             desktop_launch=True,
                             desktop_factory=lambda config: hud_overlay.create(config, native=self.native))
        self.harness, self.c = harness, harness.c
        self.window, self.reading, self.desktop = harness.c.window, harness.c.window.reading, harness.c.desktop
        self.c._start_tts = lambda: None
        harness.briefing()
        p14.holo.set_motion_level(p14.holo.MOTION_OFF)
        self.addCleanup(p14.holo.set_motion_level, p14.holo.MOTION_LIVELY)
        self.c.start()
        settle()
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.desktop.is_hud())
        self.assertTrue(self.card().is_open())
        self.assertEqual(self.card().entry(), desktop_ui.ENTRY_START)

    def test_feature_off_no_card_and_no_gate_to_pass(self) -> None:
        self.make_app(enabled=False)
        self.assertIsNone(self.c.desktop)
        self.reading.dock.activate(dock.DESKTOP)
        self.c.handle_activation(dict(HUD_KEY))
        settle()
        self.assertIsNone(self.c._desktop_consent_card)


class CardTests(ConsentApp):
    def test_the_cards_copy_and_look(self) -> None:
        self.make_app()
        self.dock_desktop()
        card = self.card()
        self.assertEqual(card.texts(),
                         ["DESKTOP \u00b7 NEEDS YOUR OK", "Jarvis on your desktop",
                          "I open files, apps, folders and sites you name.",
                          "I only work in the folders and apps you allow.",
                          "To plan, file, folder and app names go to your Claude plan. They're never kept in logs.",
                          "You can turn it off again in Settings \u203a Desktop.", "Not now", "Turn on"])
        self.assertEqual(card.not_now.variant(), desktop_view.holo.AMBER_GHOST)
        self.assertEqual(card.turn_on.variant(), desktop_view.holo.AMBER_COMMIT)
        self.assertLess(card.not_now.geometry().left(), card.turn_on.geometry().left())    # Not now left, Turn on right
        self.assertEqual(card.geometry(), self.reading.rect())               # over the window's screen
        panel = card.panel.geometry()
        self.assertTrue(card.rect().contains(panel))
        for text in card.texts():                                            # British spelling, no technical words
            self.assertNotRegex(text, r"(?i)organiz|color|consent|prefs|config")

    def test_the_keyboard_starts_on_not_now_stays_on_the_card_and_esc_is_not_now(self) -> None:
        self.make_app()
        opener = self.reading.command_bar.input
        opener.setFocus()
        self.dock_desktop()
        card = self.card()
        self.assertIs(QApplication.focusWidget(), card.not_now)              # Enter here turns nothing on
        QTest.keyClick(card.not_now, Qt.Key.Key_Tab)
        self.assertIs(QApplication.focusWidget(), card.turn_on)
        QTest.keyClick(card.turn_on, Qt.Key.Key_Tab)
        self.assertIs(QApplication.focusWidget(), card.not_now)              # trapped between the two
        QTest.keyClick(card.not_now, Qt.Key.Key_Return)                      # Not now
        self.assertFalse(card.is_open())
        self.assertFalse(self.desktop.consent_given())
        self.dock_desktop()
        QTest.keyClick(card.not_now, Qt.Key.Key_Escape)
        self.assertFalse(card.is_open())
        self.dock_desktop()
        QTest.mouseClick(card, Qt.MouseButton.LeftButton, pos=QPoint(4, 4))     # the scrim
        self.assertFalse(card.is_open())
        self.assert_nothing_desktop_side()

    def test_not_now_leaves_nothing_on(self) -> None:
        self.make_app()
        self.dock_desktop()
        QTest.mouseClick(self.card().not_now, Qt.MouseButton.LeftButton)
        settle()
        self.assertFalse(self.card().is_open())
        self.assert_nothing_desktop_side()
        self.assertNotIn("desktop_turned_on", self.stored())
        self.assertFalse(AssistantPrefs(self.prefs_path()).desktop_turned_on)
        self.dock_desktop()                                                  # the next Desktop: the card again
        self.assertTrue(self.card().is_open())

    def test_the_window_hiding_closes_the_card_as_not_now(self) -> None:
        self.make_app()
        self.dock_desktop()
        self.window.hide()
        settle()
        self.assertFalse(self.card().is_open())
        self.assertFalse(self.desktop.consent_given())


class TurnOnTests(ConsentApp):
    def test_turn_on_is_stored_and_goes_onto_the_hud(self) -> None:
        self.make_app()
        changes: list[bool] = []
        self.desktop.consentChanged.connect(changes.append)
        self.dock_desktop()
        QTest.mouseClick(self.card().turn_on, Qt.MouseButton.LeftButton)
        settle()
        self.assertFalse(self.card().is_open())
        self.assertEqual(self.stored().get("desktop_turned_on"), True)       # stored in the app's prefs
        self.assertTrue(AssistantPrefs(self.prefs_path()).desktop_turned_on)
        self.assertTrue(self.desktop.consent_given())
        self.assertEqual(changes, [True])
        self.assertTrue(self.desktop.is_hud())                               # on to what the Dock's Desktop asked
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.desktop.has_keyboard())
        self.back()
        self.dock_desktop()                                                  # from now on: straight onto the HUD
        self.assertTrue(self.desktop.is_hud())
        self.assertFalse(self.card().is_open())

    def test_after_the_hud_key_turn_on_goes_onto_the_hud_without_taking_the_keyboard(self) -> None:
        self.make_app()
        self.c.handle_activation(dict(HUD_KEY))
        settle()
        QTest.mouseClick(self.card().turn_on, Qt.MouseButton.LeftButton)
        settle()
        self.assertTrue(self.desktop.is_hud())
        self.assertFalse(self.desktop.has_keyboard())                        # the HUD key or a click gives it that
        self.assertEqual(self.native.keys, [])

    def test_after_a_typed_request_the_words_wait_for_enter(self) -> None:
        self.make_app(runs=(plan(),))
        self.c.ask.start()
        self.assertTrue(wait_for(lambda: self.c.ask.ready is not None))
        bar = self.reading.command_bar
        bar.set_text("open Downloads")
        bar.submit()
        settle()
        QTest.mouseClick(self.card().turn_on, Qt.MouseButton.LeftButton)
        settle()
        self.assertTrue(self.desktop.consent_given())
        self.assertFalse(self.desktop.is_hud())                              # nothing carried out on that click
        self.assertEqual(bar.text(), "open Downloads")
        self.assertIn("Press Enter to ask again", bar.status())
        self.assertEqual(self.fake.opened, [])
        bar.submit()                                                         # his Enter: now it goes
        self.assertTrue(wait_for(lambda: not self.desktop.busy()))
        settle()
        self.assertTrue(self.desktop.is_hud())
        self.assertEqual([method for method, _target in self.fake.opened], ["open_folder"])

    def test_a_turn_on_that_cannot_be_saved_turns_nothing_on(self) -> None:
        self.make_app()
        self.dock_desktop()
        with mock.patch.object(prefs, "_write_atomic", return_value=False):
            QTest.mouseClick(self.card().turn_on, Qt.MouseButton.LeftButton)
            settle()
        self.assertTrue(self.card().is_open())
        self.assertIn(desktop_view.NOT_SAVED_TEXT, self.card().texts())
        self.assertFalse(self.desktop.consent_given())                       # fail-closed: never on unsaved
        self.assert_nothing_desktop_side()


class FailClosedTests(ConsentApp):
    def write_prefs(self, text: str):  # type: ignore[no-untyped-def]
        def prepare(config) -> None:  # type: ignore[no-untyped-def]
            path = config.data_dir / PREFS_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return prepare

    def test_anything_but_a_stored_true_is_no(self) -> None:
        for text in ('{"desktop_turned_on": "true"}', '{"desktop_turned_on": 1}', "{not json", "[true]",
                     '{"desktop_turned_on": null}'):
            with self.subTest(text=text):
                self.make_app(prepare=self.write_prefs(text))
                self.assertFalse(self.desktop.consent_given())
                self.dock_desktop()
                self.assertTrue(self.card().is_open())
                self.assertFalse(self.desktop.is_hud())
                self.harness.close()

    def test_an_unreadable_store_or_config_is_no_and_logs_the_type_only(self) -> None:
        self.make_app(consent=True)
        self.assertTrue(self.desktop.consent_given())
        self.desktop._consent_store = mock.Mock(side_effect=OSError(5, "C:\\Users\\Owner\\secret"))
        with self.assertLogs("briefing_reader.desktop_ui", level="WARNING") as logs:
            self.assertFalse(self.desktop.consent_given())
        self.assertIn("OSError", "\n".join(logs.output))
        self.assertNotIn("secret", "\n".join(logs.output))
        self.desktop._consent_store = None                                   # no store at all
        self.assertFalse(self.desktop.consent_given())
        self.desktop._consent_store = lambda: AssistantPrefs(self.prefs_path())
        self.assertTrue(self.desktop.consent_given())
        self.desktop.config = mock.Mock(desktop=mock.Mock(enabled="yes"))    # not exactly on
        self.assertFalse(self.desktop.consent_given())


class SettingsTests(ConsentApp):
    def test_your_ok_and_turn_off(self) -> None:
        self.make_app(consent=True)
        settings = self.reading.settings_page
        settings.select_group(settings_view.DESKTOP_GROUP)
        panel = self.c._desktop_settings
        self.assertEqual(dict(settings.rows())["Your OK"], "Turned on")
        self.assertTrue(panel.turn_off.isVisibleTo(panel))
        self.assertEqual(panel.line.text(), desktop_view.SETTINGS_ON_TEXT)
        self.assertIn("Turn off takes it back", settings.text_label.text())
        QTest.mouseClick(panel.turn_off, Qt.MouseButton.LeftButton)
        settle()
        self.assertFalse(self.desktop.consent_given())
        self.assertNotIn("desktop_turned_on", self.stored())
        self.assertEqual(dict(settings.rows())["Your OK"], "Not turned on yet")
        self.assertFalse(panel.turn_off.isVisibleTo(panel))
        self.assertEqual(panel.line.text(), desktop_view.SETTINGS_OFF_TEXT)
        self.dock_desktop()                                                  # the card again
        self.assertTrue(self.card().is_open())
        self.assertFalse(self.desktop.is_hud())
        QTest.mouseClick(self.card().turn_on, Qt.MouseButton.LeftButton)     # and on again
        settle()
        self.assertTrue(self.desktop.is_hud())
        self.back()
        settings.select_group(settings_view.DESKTOP_GROUP)
        self.assertEqual(dict(settings.rows())["Your OK"], "Turned on")

    def test_a_turn_off_that_cannot_be_saved_is_off_for_now_and_says_so(self) -> None:
        self.make_app(consent=True)
        panel = self.c._desktop_settings
        with mock.patch.object(prefs, "_write_atomic", return_value=False):
            QTest.mouseClick(panel.turn_off, Qt.MouseButton.LeftButton)
            settle()
        self.assertFalse(self.desktop.consent_given())
        self.assertTrue(panel.problem.isVisibleTo(panel))
        self.assertEqual(panel.problem.text(), desktop_view.TURN_OFF_NOT_SAVED_TEXT)


class PrefsTests(unittest.TestCase):
    def test_the_desktop_ok_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / PREFS_FILE
            store = AssistantPrefs(path)
            self.assertFalse(store.desktop_turned_on)
            self.assertTrue(store.set_desktop_turned_on(True))
            self.assertTrue(AssistantPrefs(path).desktop_turned_on)
            self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["desktop_turned_on"])
            self.assertTrue(store.set_desktop_turned_on(False))
            self.assertNotIn("desktop_turned_on", json.loads(path.read_text(encoding="utf-8")))
            with mock.patch.object(prefs, "_write_atomic", return_value=False):
                self.assertFalse(store.set_desktop_turned_on(True))
            self.assertFalse(store.desktop_turned_on)                        # never on unsaved
            store.set_desktop_turned_on(True)
            with mock.patch.object(prefs, "_write_atomic", return_value=False):
                self.assertFalse(store.set_desktop_turned_on(False))
            self.assertFalse(store.desktop_turned_on)                        # off for this run, even unsaved


if __name__ == "__main__":
    unittest.main()
