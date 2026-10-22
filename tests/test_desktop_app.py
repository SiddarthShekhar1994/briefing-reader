"""Tests for the desktop feature's switch in the app (P1.1): [desktop] off shows no Desktop surface anywhere; on, the
Dock's Desktop slot (with the HUD key's line), the Find list's Desktop and Settings > Desktop (read only, "Not
turned on yet") appear, and a click on Desktop asks for the HUD (desktopRequested) - the screen and the Dock's
current slot stay as they were, and the window is hidden for the HUD (P1.4; tests/test_desktop_hud.py has the rest).

Qt runs offscreen in this process and shows nothing; the real desktop backend is never constructed.
"""

from __future__ import annotations

import unittest

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from briefing_reader import dock, find_view, settings_view, ui
from tests.ui_fakes import AppHarness, fonts, settle

DESKTOP_ON = "\n[desktop]\nenabled = true\n"
_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_desktop_app", "-platform", "offscreen"])
    fonts()


class DesktopSwitchTests(unittest.TestCase):
    def make(self, extra: str = DESKTOP_ON) -> AppHarness:
        harness = AppHarness(self, ask=True, config_extra=extra)
        harness.reading(autoplay=False)
        self.reading = harness.c.window.reading
        return harness

    def test_off_shows_no_desktop_surface(self) -> None:
        harness = self.make("")
        reading = self.reading
        self.assertFalse(harness.config.desktop.enabled)
        self.assertFalse(reading.desktop_available())
        self.assertNotIn(dock.DESKTOP, reading.dock.visible_slots())
        self.assertFalse(reading.dock.desktop_hint().isVisible())
        self.assertNotIn(settings_view.DESKTOP_GROUP, reading.settings_page.group_buttons)
        self.assertNotIn(ui.FIND_DESKTOP_KEY, [item.key for item in reading.find_items("")])
        self.assertEqual([item.kind for item in reading.find_items("desktop")], [find_view.KIND_ASK])   # only Ask
        asked: list[bool] = []
        reading.desktopRequested.connect(lambda: asked.append(True))
        reading.dock.activate(dock.DESKTOP)
        reading._on_find_pick(find_view.FindItem(find_view.KIND_ACTION, ui.FIND_DESKTOP_KEY, "Desktop"))
        self.assertEqual(asked, [])

    def test_on_shows_the_slot_and_a_click_goes_onto_the_hud(self) -> None:
        harness = self.make()
        reading = self.reading
        window = harness.c.window
        self.assertTrue(reading.desktop_available())
        self.assertEqual(reading.dock.visible_slots()[-2:], [dock.DESKTOP, dock.SETTINGS])
        self.assertEqual(reading.dock.hud_key(), "Ctrl+Alt+H")
        self.assertTrue(reading.dock.is_labelled())                             # the default window size
        self.assertTrue(reading.dock.desktop_hint().isVisible())
        self.assertEqual(reading.dock.desktop_hint().text(), "Ctrl+Alt+H switches")
        asked: list[bool] = []
        screens: list[int] = []
        reading.desktopRequested.connect(lambda: asked.append(True))
        reading.tabChanged.connect(screens.append)
        reading.set_tab(ui.SCREEN_APPROVALS)
        settle()
        screens.clear()
        state = harness.c.state
        QTest.mouseClick(reading.dock.button(dock.DESKTOP), Qt.MouseButton.LeftButton)
        settle()
        self.assertEqual(asked, [True])
        self.assertEqual((reading.current_tab(), reading.dock.current()), (ui.SCREEN_APPROVALS, dock.APPROVALS))
        self.assertEqual(screens, [])
        self.assertFalse(window.isVisible())                                     # hidden for the HUD (P1.4)
        self.assertTrue(harness.c.desktop.is_hud())
        self.assertEqual(harness.c.state, state)

    def test_settings_shows_the_desktop_group_read_only(self) -> None:
        self.make(DESKTOP_ON + "undo_days = 1\nextra_folders = ['C:\\Users\\Owner\\Uni']\napps = true\n")
        settings = self.reading.settings_page
        self.assertIn(settings_view.DESKTOP_GROUP, settings.group_buttons)
        settings.select_group(settings_view.DESKTOP_GROUP)
        rows = dict(settings.rows())
        self.assertEqual(rows["Your OK"], "Not turned on yet")
        self.assertEqual(rows["Folders Jarvis may look in"],
                         "Desktop, Documents, Downloads, Pictures, Music and Videos, plus 1 folder of your own")
        self.assertEqual(rows["HUD key"], "Ctrl+Alt+H - switches between the window and the HUD")
        self.assertEqual(rows["Left out of screenshots and screen sharing"], "Yes")
        self.assertEqual(rows["Screenshots go to"], "Jarvis\\Screenshots in your user folder")
        self.assertEqual(rows["Undo"], "Offered for 1 day")
        self.assertEqual(rows["Work inside apps"], "On - only in the apps you allow")
        texts = " ".join(settings.shown_texts())
        self.assertIn("JARVIS_DESKTOP_ENABLED", texts)
        self.assertNotIn("Owner", texts)                                         # an extra folder is counted only
        self.assertEqual(settings.title_label.text(), "Desktop")

    def test_no_hud_key_when_the_window_key_has_it(self) -> None:
        self.make(DESKTOP_ON + '\n[hotkey]\ncombo = "ctrl+alt+h"\n')
        reading = self.reading
        self.assertTrue(reading.desktop_available())
        self.assertEqual(reading.dock.hud_key(), "")
        self.assertFalse(reading.dock.desktop_hint().isVisible())                # the Dock's Desktop still switches
        self.assertNotIn("Ctrl+Alt+H", reading.dock.button(dock.DESKTOP).accessibleDescription())
        rows = dict(settings_view.desktop_rows(reading.settings_page._config))
        self.assertEqual(rows["HUD key"], "None - the Dock's Desktop switches")

    def test_no_hud_key_while_the_keyboard_shortcut_is_off(self) -> None:
        # [hotkey] enabled = false: the hotkey agent, which registers both keys, does not run, so no key can work.
        harness = self.make(DESKTOP_ON + "\n[hotkey]\nenabled = false\n")
        reading = self.reading
        self.assertEqual(harness.config.desktop.hud_key, "ctrl+alt+h")       # the setting itself is kept
        self.assertTrue(reading.desktop_available())
        self.assertTrue(reading.dock.is_labelled())
        self.assertEqual(reading.dock.hud_key(), "")
        self.assertFalse(reading.dock.desktop_hint().isVisible())                # no "Ctrl+Alt+H switches"
        self.assertNotIn("Ctrl+Alt+H", reading.dock.button(dock.DESKTOP).accessibleDescription())
        settings = reading.settings_page
        settings.select_group(settings_view.DESKTOP_GROUP)
        self.assertEqual(dict(settings.rows())["HUD key"], settings_view.NO_HUD_KEY_SHORTCUT_OFF)
        self.assertTrue(settings_view.NO_HUD_KEY_SHORTCUT_OFF.startswith("No key"))
        self.assertNotIn("Ctrl+Alt+H", " ".join(settings.shown_texts()))
        settings.select_group("ask")
        self.assertEqual(dict(settings.rows())["Keyboard shortcut"], "Off")      # the Ask group agrees


class DesktopRowsTests(unittest.TestCase):
    def test_the_hud_key_shows_only_while_the_keyboard_shortcut_is_on(self) -> None:
        from briefing_reader.config import DesktopConfig, HotkeyConfig

        class Config:
            desktop = DesktopConfig()
            hotkey = HotkeyConfig()

        self.assertEqual(settings_view.effective_hud_key(Config()), "ctrl+alt+h")
        self.assertEqual(dict(settings_view.desktop_rows(Config()))["HUD key"],
                         "Ctrl+Alt+H - switches between the window and the HUD")
        Config.hotkey = HotkeyConfig(enabled=False)
        self.assertEqual(settings_view.effective_hud_key(Config()), "")
        self.assertEqual(dict(settings_view.desktop_rows(Config()))["HUD key"],
                         "No key while the keyboard shortcut is off - the Dock's Desktop switches")
        Config.desktop = DesktopConfig(hud_key="")                              # the window key took it as well
        self.assertEqual(dict(settings_view.desktop_rows(Config()))["HUD key"], "None - the Dock's Desktop switches")

    def test_rows_without_config_or_folders(self) -> None:
        self.assertEqual(settings_view.desktop_rows(None), [])
        from briefing_reader.config import DesktopConfig

        class Config:
            desktop = DesktopConfig(folders=(), extra_folders=("C:\\Users\\Owner\\A", "C:\\Users\\Owner\\B"))

        rows = dict(settings_view.desktop_rows(Config(), turned_on=True))
        self.assertEqual(rows["Your OK"], settings_view.TURNED_ON)
        self.assertEqual(rows["Folders Jarvis may look in"], "2 folders of your own")
        Config.desktop = DesktopConfig(folders=())
        self.assertEqual(dict(settings_view.desktop_rows(Config()))["Folders Jarvis may look in"], "None")


if __name__ == "__main__":
    unittest.main()
