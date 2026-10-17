"""Round 2, part 2b of the hologram (HOLOGRAM_SPEC 16.6, 16.8): the Memory, Follow-ups and Settings screens.

Memory: the focal tile (a pending suggestion as the amber needs-you tile with Keep / Dismiss; KEPT and the
time after Keep; the Add field without one), Keep and Dismiss in the app (nothing is kept without Keep), the
note rows (readout meta, Edit / Delete shown on hover or keyboard focus, keeping their place). Follow-ups:
the groups and their bars, the oldest WAITING ON YOU row open as the focal row with its actions, the steel
tags, the header readout and the "..." menu (only the actions that were there), and the app's answers to
the row's buttons (a draft only through its card; Hide asks how long; Open in Gmail opens the thread link).
Settings: the six groups, read only, secret-free, config.toml never written. Qt offscreen; every name,
subject and address is invented."""

from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QEnterEvent, QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QLineEdit, QPlainTextEdit,
                               QTextEdit, QVBoxLayout, QWidget)

from briefing_reader import followups_view, holo, hud, memory_view, settings_view, ui
from briefing_reader.memory import SOURCE_SUGGESTED
from tests.test_followups_app import FollowupsAppCase, draft
from tests.ui_fakes import AppHarness, fonts, settle

_app: QApplication | None = None
MEMORY_ON = "\n[productivity]\nenabled = true\n"
SUGGESTION = "Sam Example organises the weekly Jarvis test sync."
DOT = hud.MIDDLE_DOT


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_memory_followups_settings", "-platform", "offscreen"])
    fonts()


def _hover(widget: QWidget, inside: bool) -> None:
    if inside:
        point = QPointF(4, 4)
        QApplication.sendEvent(widget, QEnterEvent(point, point, widget.mapToGlobal(point)))
    else:
        QApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))


class MemoryStageTests(unittest.TestCase):
    """memory_view.MemoryStage alone."""

    def setUp(self) -> None:
        self.window = QWidget()
        layout = QVBoxLayout(self.window)
        self.stage = memory_view.MemoryStage()
        layout.addWidget(self.stage)
        self.window.resize(900, 700)
        self.window.show()
        settle()
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.window.hide)

    def test_the_question_keeps_the_note_s_words(self) -> None:
        self.assertEqual(memory_view.question_text("Ana organises the weekly check-in."),
                         "Remember that Ana organises the weekly check-in?")
        self.assertEqual(memory_view.question_text("The lab meets on Fridays"),
                         "Remember that the lab meets on Fridays?")
        self.assertEqual(memory_view.question_text("I prefer mornings."), "Remember that I prefer mornings?")

    def test_the_focal_tile_pending_kept_and_the_add_field(self) -> None:
        stage, tile = self.stage, self.stage.suggestion_tile
        self.assertFalse(tile.isVisible())                         # nothing waits: the Add field is first
        self.assertTrue(stage.input.isVisible())
        self.assertEqual(stage.input.placeholderText(), hud.MEMORY_ADD_PLACEHOLDER)
        kept, dismissed = [], []
        stage.suggestionSaved.connect(kept.append)
        stage.suggestionDismissed.connect(dismissed.append)
        stage.set_suggestions([hud.SuggestionRowInfo(7, SUGGESTION), hud.SuggestionRowInfo(8, "Ben runs the lab")])
        settle()
        self.assertTrue(tile.isVisible())
        self.assertEqual((tile.state(), tile.kicker()), ("pending", f"SUGGESTION {DOT} 1 OF 2"))
        self.assertEqual(tile.title_label.text(), "Remember that Sam Example organises the weekly Jarvis test sync?")
        self.assertEqual(tile.keep_button.variant(), holo.AMBER_COMMIT)
        self.assertEqual(tile.dismiss_button.variant(), holo.AMBER_GHOST)
        self.assertTrue(tile.keep_button.isVisible() and tile.dismiss_button.isVisible())
        self.assertLess(tile.geometry().top(), stage.field.geometry().top())     # the focal tile above the field
        tile.dismiss_button.click()
        tile.keep_button.click()
        self.assertEqual((dismissed, kept), ([7], [7]))              # the view only says which; the app decides
        stage.set_read_only(True)
        self.assertFalse(tile.keep_button.isEnabled())               # memory.json can't be read: no Keep
        tile.keep_button.click()
        self.assertEqual(kept, [7])
        stage.set_read_only(False)
        stage.set_suggestions([])                                    # kept by the app: KEPT, the note, its meta
        stage.show_kept(SUGGESTION, f"SUGGESTED {DOT} OCT 12", "10:42 AM")
        settle()
        self.assertEqual(tile.state(), "kept")
        self.assertEqual(tile.kicker(), f"KEPT {DOT} 10:42 AM")
        self.assertEqual(tile.title_label.text(), SUGGESTION)
        self.assertEqual(tile.meta_label.text(), f"SUGGESTED {DOT} OCT 12")
        self.assertTrue(tile.added_label.isVisible())
        self.assertFalse(tile.keep_button.isVisible() or tile.dismiss_button.isVisible())
        self.assertEqual(stage.kept(), (SUGGESTION, f"SUGGESTED {DOT} OCT 12", "10:42 AM"))
        stage.set_suggestions([hud.SuggestionRowInfo(9, "Ben runs the lab")])   # a new one waits: it is focal
        self.assertEqual((tile.state(), tile.kicker()), ("pending", "SUGGESTION"))
        stage.set_suggestions([])
        stage.show_kept(SUGGESTION, "", "10:43 AM")
        self.window.hide()                                           # the screen left: the next visit opens
        settle()                                                     # on the Add field
        self.assertIsNone(stage.kept())
        self.assertEqual(tile.state(), "")

    def test_note_rows_readout_meta_and_actions_on_hover_or_focus(self) -> None:
        stage = self.stage
        stage.set_rows([hud.MemoryRowInfo("a", "Ana prefers mornings", f"TOLD {DOT} OCT 10"),
                        hud.MemoryRowInfo("b", "Ben runs the lab", f"SUGGESTED {DOT} OCT 12 {DOT} EDITED",
                                          sent=False)])
        settle()
        a, b = stage.rows()
        self.assertEqual(a.meta_label.text(), f"TOLD {DOT} OCT 10")
        self.assertEqual(a.meta_label.font().family(), holo.font("readout").family())
        self.assertTrue(b.not_sent_label.isVisible() and not a.not_sent_label.isVisible())
        self.assertEqual((a.edit_button.variant(), a.delete_button.variant()), (holo.GHOST, holo.DANGER_GHOST))
        # At rest Edit and Delete keep their place (and their Tab stop) but are not drawn.
        self.assertTrue(a.edit_button.isVisible() and a.delete_button.isVisible())
        self.assertFalse(a.actions_revealed())
        before = a.text_label.geometry()
        _hover(a, True)
        self.assertTrue(a.actions_revealed())
        self.assertFalse(b.actions_revealed())
        self.assertEqual(a.text_label.geometry(), before)             # nothing moves under the pointer
        _hover(a, False)
        self.assertFalse(a.actions_revealed())
        self.window.activateWindow()
        b.delete_button.setFocus(Qt.FocusReason.TabFocusReason)
        settle()
        if QApplication.focusWidget() is b.delete_button:            # offscreen focus may be unavailable
            self.assertTrue(b.actions_revealed())
            self.assertTrue(b.edit_button.revealed())
        edits: list[tuple[str, str]] = []
        stage.edited.connect(lambda entry_id, text: edits.append((entry_id, text)))
        a.edit_button.click()
        settle()
        self.assertTrue(a.editing() and a.save_button.isVisible() and a.cancel_button.isVisible())
        self.assertEqual(a.save_button.variant(), holo.GO)
        a.edit_field.setText("Ana prefers early mornings")
        a.save_button.click()
        self.assertEqual(edits, [("a", "Ana prefers early mornings")])
        a.cancel_button.click()
        self.assertFalse(a.editing())

    def test_status_and_banner_colours(self) -> None:
        stage = self.stage
        stage.set_status("Kept only on this PC. 2 notes")
        self.assertEqual(stage.status(), "Kept only on this PC. 2 notes")
        stage.set_banner(hud.MEMORY_READ_ONLY_TEXT)
        self.assertEqual(stage.banner(), hud.MEMORY_READ_ONLY_TEXT)
        self.assertIn("rgba(255, 154, 140, 255)", stage.banner_label.styleSheet())   # alert-tx: a failure, not amber


class MemoryKeepAppTests(unittest.TestCase):
    """Keep and Dismiss on the focal tile in the app: nothing is kept without Keep."""

    def make(self) -> None:
        app = AppHarness(self, config_extra=MEMORY_ON)
        app.reading(autoplay=False)
        self.app, self.c, self.reading = app, app.c, app.c.window.reading
        self.view = self.reading.memory_view
        self.reading.set_tab(ui.SCREEN_MEMORY)
        settle()

    def test_keep_stamps_kept_and_the_note_its_suggested_date(self) -> None:
        self.make()
        self.c._offer_memory_suggestion(SUGGESTION)          # as an Ask that suggested it (never spoken)
        settle()
        tile = self.view.suggestion_tile
        self.assertEqual(tile.state(), "pending")
        self.assertEqual(list(self.c.memory_store.entries()), [])  # offered only
        tile.keep_button.click()
        settle()
        entries = self.c.memory_store.entries()
        self.assertEqual([(entry.text, entry.source) for entry in entries], [(SUGGESTION, SOURCE_SUGGESTED)])
        self.assertEqual(tile.state(), "kept")
        self.assertTrue(tile.kicker().startswith(f"KEPT {DOT} "))
        self.assertEqual(tile.title_label.text(), SUGGESTION)
        self.assertTrue(tile.meta_label.text().startswith(f"SUGGESTED {DOT} "))
        self.assertEqual(self.view.rows()[0].info().text, SUGGESTION)
        self.assertTrue(self.view.rows()[0].info().meta.startswith("SUGGESTED"))

    def test_dismiss_keeps_nothing(self) -> None:
        self.make()
        self.c._offer_memory_suggestion(SUGGESTION)
        settle()
        self.view.suggestion_tile.dismiss_button.click()
        settle()
        self.assertEqual(list(self.c.memory_store.entries()), [])
        self.assertEqual(self.view.suggestion_tile.state(), "")
        self.assertTrue(self.view.input.isVisible())


def _row(row_id: str, kind: str, who: str, age: str, subject: str = "Zq7 Lab budget", state: str = "",
         words: str = "") -> hud.FollowupRowInfo:
    return hud.FollowupRowInfo(row_id, kind, who, age, subject, "", state, words)


class FollowupsStageTests(unittest.TestCase):
    """followups_view.FollowupsStage alone."""

    def setUp(self) -> None:
        self.window = QWidget()
        layout = QVBoxLayout(self.window)
        self.stage = followups_view.FollowupsStage()
        layout.addWidget(self.stage)
        self.window.resize(900, 700)
        self.window.show()
        settle()
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.window.hide)

    def info(self, *, shown: int = 6, checking: bool = False) -> hud.FollowupsInfo:
        waiting = (_row("w1", "waiting", "Ana Lima", "4d", state=hud.FOLLOWUP_CHECKING if checking else "",
                        words="replied - checking" if checking else ""),
                   _row("w2", "waiting", "Dana Park", "20h", state=hud.FOLLOWUP_DRAFT_WAITING, words="draft waiting"))
        owed = (_row("o1", "owed", "Ben Ode", "3d"),)
        return hud.FollowupsInfo(meta=f"2 waiting {DOT} 1 owed {DOT} updated 10:41 AM", waiting=waiting, owed=owed,
                                 shown=shown, empty_text="Nothing waits on you")

    def test_groups_focal_row_and_tags(self) -> None:
        stage = self.stage
        self.assertFalse(stage.followups_visible())
        stage.set_followups(self.info())
        settle()
        self.assertEqual(stage.followups_meta.text(), f"2 waiting {DOT} 1 owed {DOT} updated 10:41 AM")
        self.assertEqual(stage.followups_meta.font().capitalization(), QFont.Capitalization.AllUppercase)
        self.assertEqual([row.info().row_id for row in stage.followup_rows()], ["w1", "w2", "o1"])
        w1, w2, o1 = stage.followup_rows()
        self.assertEqual(stage.expanded_id(), "w1")                 # the oldest WAITING ON YOU row is focal
        self.assertTrue(w1.expanded() and not w2.expanded() and not o1.expanded())
        for button in (w1.draft_button, w1.words_button, w1.hide_button, w1.open_button):
            self.assertTrue(button.isVisible())
        self.assertFalse(w2.draft_button.isVisible())
        self.assertEqual((w1.draft_button.text(), w1.draft_button.variant()), ("Draft a reply", holo.GO))
        self.assertEqual(w1.hide_button.variant(), holo.GHOST)
        self.assertEqual(w1.open_button.variant(), holo.ICON)
        self.assertEqual(w1.open_button.accessibleName(), "Open in Gmail: Ana Lima")
        self.assertEqual((w2.draft_button.text(), w2.draft_button.variant()), ("Show the draft", holo.GHOST))
        self.assertEqual(o1.draft_button.text(), "Draft a nudge")
        self.assertEqual(stage._labels["waiting"].text(), "WAITING ON YOU")
        self.assertEqual(stage._labels["owed"].text(), "OWED TO YOU")
        o1.head.click()                                             # the owner opens another row
        self.assertEqual(stage.expanded_id(), "o1")
        self.assertTrue(o1.expanded() and not w1.expanded())
        o1.head.click()                                             # and closes it: none is open
        self.assertEqual(stage.expanded_id(), "")
        stage.set_followups(self.info(checking=True))               # a row being checked never opens
        w1 = stage.followup_rows()[0]
        w1.head.click()
        self.assertFalse(w1.expanded())
        self.assertEqual(w1.info().state_text, "replied - checking")

    def test_buttons_say_which_row_and_the_menu(self) -> None:
        stage = self.stage
        stage.set_followups(self.info())
        seen: list[tuple[str, str]] = []
        for name in ("draftRequested", "wordsRequested", "hideRequested", "openRequested", "followupActivated"):
            getattr(stage, name).connect(lambda row_id, which=name: seen.append((which, row_id)))
        menus: list[bool] = []
        stage.followupsMenuRequested.connect(lambda: menus.append(True))
        w1 = stage.followup_rows()[0]
        for button in (w1.draft_button, w1.words_button, w1.hide_button, w1.open_button):
            button.click()
        w1.head.customContextMenuRequested.emit(w1.head.rect().center())
        stage.followups_menu_button.click()
        self.assertEqual(seen, [("draftRequested", "w1"), ("wordsRequested", "w1"), ("hideRequested", "w1"),
                                ("openRequested", "w1"), ("followupActivated", "w1")])
        self.assertEqual(menus, [True])
        self.assertEqual(stage.followups_menu_button.accessibleName(), "Follow-ups menu")

    def test_more_and_empty(self) -> None:
        stage = self.stage
        stage.set_followups(self.info(shown=1))
        more = stage.followup_more("waiting")
        self.assertTrue(more.isVisibleTo(stage) and more.text() == "+1 more")
        self.assertEqual(len(stage.followup_rows()), 2)
        more.click()
        self.assertEqual((more.text(), len(stage.followup_rows())), ("Show fewer", 3))
        stage.set_followups(hud.FollowupsInfo(meta="updated 10:41 AM", empty_text="Nothing waits on you"))
        self.assertEqual(stage.followup_rows(), [])
        self.assertTrue(stage.followups_empty.isVisibleTo(stage))
        self.assertEqual(stage.expanded_id(), "")


class FollowupsAppButtonTests(FollowupsAppCase):
    """The focal row's buttons in the app do what the row menu did."""

    def test_draft_hide_open_and_the_header_menu(self) -> None:
        app = self.make(draft())
        self.scan()
        stage = self.panel
        self.assertIs(stage, self.reading.followups_stage)
        focal = stage.followup_row(stage.expanded_id())
        self.assertIsNotNone(focal)
        self.assertEqual(focal.info().kind, "waiting")
        opened: list[str] = []
        with mock.patch.object(self.c, "open_action_link", side_effect=opened.append):
            focal.open_button.click()
        self.assertEqual(len(opened), 1)
        self.assertIn("thrwait001", opened[0])                       # the thread link the app already builds
        focal.hide_button.click()
        menu = self.c._followup_menu
        self.assertEqual([action.text() for action in menu.actions() if action.text()],
                         [ui.FOLLOWUP_MENU_HIDE_DAYS, ui.FOLLOWUP_MENU_HIDE_NEW])
        menu.close()
        stage.followups_menu_button.click()                          # only the actions that were there
        menu = self.c._followup_menu
        self.assertEqual([action.text() for action in menu.actions() if action.text()],
                         ["Rescan", "Show hidden (0)", "Turn off on this PC"])
        menu.close()
        focal.draft_button.click()                                   # a draft: only a card that waits for OK
        self.wait_run()
        card, = self.nudge_cards()
        self.assertFalse(app.senders["work"].sent)
        self.assertIsNotNone(self.reading.action_card(card.id))
        focal = stage.followup_row(self.row().row_id)
        self.assertEqual(focal.info().state, hud.FOLLOWUP_DRAFT_WAITING)
        self.assertEqual(focal.draft_button.text(), followups_view.SHOW_DRAFT_TEXT)
        with mock.patch.object(self.reading.approvals, "reveal") as reveal:
            focal.draft_button.click()                               # Show the draft: no second run
        reveal.assert_called_once_with(card.id)
        self.assertEqual(len(self.nudge_cards()), 1)


_SECRET = "Zq7secret"


class SettingsStageTests(unittest.TestCase):
    def make(self) -> AppHarness:
        app = AppHarness(self, config_extra=MEMORY_ON)
        app.reading(autoplay=False)
        self.app, self.c, self.reading = app, app.c, app.c.window.reading
        return app

    def test_six_groups_read_only(self) -> None:
        app = self.make()
        stage = self.reading.settings_page
        self.assertIsInstance(stage, settings_view.SettingsStage)
        self.assertEqual([key for key, _text in settings_view.GROUPS],
                         ["voice", "times", "ask", "accounts", "quiet", "live"])
        self.assertEqual([button.text() for button in stage.group_buttons.values()],
                         ["Voice", "Briefing times", "Ask", "Accounts", "Quiet hours", "LIVE and privacy"])
        config_path = Path(app.root) / "config.toml"
        before = config_path.read_bytes()
        self.reading.set_tab(ui.SCREEN_SETTINGS)
        settle()
        for key, title in settings_view.GROUPS:
            stage.group_buttons[key].click()
            settle()
            self.assertEqual((stage.current_group(), stage.title_label.text()), (key, title))
            self.assertTrue(stage.rows(), key)
            self.assertIn("config.toml, then restart Jarvis", stage.text_label.text())
            for kind in (QLineEdit, QTextEdit, QPlainTextEdit, QCheckBox, QComboBox, QAbstractSpinBox):
                self.assertEqual(stage.findChildren(kind), [], f"{key}: no editing control ({kind.__name__})")
        self.assertEqual(config_path.read_bytes(), before)            # never written
        self.assertTrue(stage.config_button.isVisible() or stage.config_button.isVisibleTo(stage))
        stage.select_group("voice")
        rows = dict(stage.rows())
        self.assertEqual(rows["Speed"], "Normal")
        self.assertIn(rows["Speaks on his own"], ("Yes", "No - text only"))
        stage.select_group("quiet")
        self.assertEqual(stage.rows()[0], ("Quiet hours", "10:00 PM to 8:00 AM"))
        QTest.keyClick(stage.group_buttons["quiet"], Qt.Key.Key_Down)     # Up / Down move through the groups
        self.assertEqual(stage.current_group(), "live")
        QTest.keyClick(stage.group_buttons["live"], Qt.Key.Key_Up)
        self.assertEqual(stage.current_group(), "quiet")

    def test_accounts_show_only_names_and_connected(self) -> None:
        self.make()
        stage = self.reading.settings_page
        stage.select_group("accounts")
        rows = dict(stage.rows())
        self.assertEqual(set(rows), set(self.c.config.accounts))
        self.c._accounts_state["work"] = (True, "", "")
        self.c._accounts_state["personal"] = (False, "signed_out", "")
        self.c._update_service_chips()
        self.assertEqual(dict(stage.rows()), {"work": "Connected", "personal": "Not connected"}
                         | {alias: "Not checked yet" for alias in rows if alias not in ("work", "personal")})

    def test_nothing_secret_is_shown(self) -> None:
        self.make()
        config = self.c.config
        secret = dataclasses.replace(
            config, notion_token=f"ntn_{_SECRET}", page_id=f"{_SECRET}page",
            calendar=dataclasses.replace(config.calendar, client_secret_path=Path(f"C:/{_SECRET}/client.json")))
        stage = settings_view.SettingsStage()
        self.addCleanup(stage.deleteLater)
        stage.set_config(secret)
        stage.set_connected({"work": True})
        for key, _title in settings_view.GROUPS:
            stage.select_group(key)
            for text in stage.shown_texts():
                self.assertNotIn(_SECRET.lower(), text.lower(), key)
                self.assertNotIn("@", text, key)
                self.assertNotIn("token", text.lower(), key)
                self.assertNotIn("client", text.lower(), key)

    def test_rows_in_plain_words(self) -> None:
        config = self.make().config
        rows = dict(settings_view.settings_rows(config, "times"))
        self.assertEqual(rows["Clock on screen"], "12-hour (1:05 PM)")
        self.assertIn("minutes, checking every", rows["Waits for a late briefing"])
        self.assertEqual(settings_view._percent("+10%", "faster", "slower"), "10% faster")
        self.assertEqual(settings_view._percent("-5%", "louder", "quieter"), "5% quieter")
        self.assertEqual(settings_view._clock("22:00", True), "22:00")
        self.assertEqual(settings_view._combo("ctrl+alt+j"), "Ctrl+Alt+J")
        self.assertEqual(settings_view.settings_rows(None, "voice"), [])


if __name__ == "__main__":
    unittest.main()
