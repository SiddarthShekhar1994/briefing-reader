"""Tests for Ask Jarvis's command bar (hud.CommandBar) and its place in the reading screen.

Enter submits the text with its whitespace collapsed (an empty field or a running request submits
nothing), Esc clears the field or cancels a running request, the field takes at most 500
characters and reads nothing as HTML, the button reads Ask / Cancel, the status line keeps its
height whatever it says, the compact bar (narrow or short screens) keeps the controls' room, and
Space typed in the bar is a space while Space elsewhere still plays and pauses. Qt runs offscreen
with the bundled fonts and shows nothing; every text is invented.
"""

from __future__ import annotations

import unittest

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from briefing_reader import briefing_view, config, holo, hud, ui

_app: QApplication | None = None
IMG = '<img src="file://203.0.113.9/share/x.png">'


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_command_bar", "-platform", "offscreen"])
    hud.load_fonts(config.PROJECT_ROOT / "fonts")


def settle() -> None:
    for _ in range(3):
        QApplication.processEvents()


class CommandBarTests(unittest.TestCase):

    def setUp(self) -> None:
        self.bar = hud.CommandBar()
        self.addCleanup(self.bar.deleteLater)
        self.submitted: list[str] = []
        self.cancelled: list[int] = []
        self.links: list[int] = []
        self.bar.submitted.connect(self.submitted.append)
        self.bar.cancelRequested.connect(lambda: self.cancelled.append(1))
        self.bar.linkClicked.connect(lambda: self.links.append(1))
        self.bar.resize(420, self.bar.sizeHint().height())
        self.bar.show()
        settle()

    def type(self, text: str) -> None:
        self.bar.focus_input()
        QTest.keyClicks(self.bar.input, text)

    def test_enter_submits_the_text_with_its_whitespace_collapsed(self) -> None:
        self.type("  move my   sync to Friday  ")
        QTest.keyClick(self.bar.input, Qt.Key.Key_Return)
        self.assertEqual(self.submitted, ["move my sync to Friday"])
        self.assertEqual(self.bar.input.text(), "  move my   sync to Friday  ")   # the bar's owner clears it

    def test_an_empty_field_submits_nothing(self) -> None:
        self.type("   ")
        QTest.keyClick(self.bar.input, Qt.Key.Key_Return)
        QTest.keyClick(self.bar.input, Qt.Key.Key_Enter)
        self.bar.button.click()
        self.assertEqual(self.submitted, [])

    def test_the_ask_button_submits_like_enter(self) -> None:
        self.type("block two hours on Tuesday")
        self.bar.button.click()
        self.assertEqual(self.submitted, ["block two hours on Tuesday"])

    def test_while_running_the_field_is_read_only_and_the_button_cancels(self) -> None:
        self.type("move my sync")
        self.bar.set_running(True)
        self.assertTrue(self.bar.is_running())
        self.assertTrue(self.bar.input.isReadOnly())
        self.assertEqual(self.bar.button.text(), hud.CANCEL_BUTTON_TEXT)
        self.assertEqual(self.bar.button.variant(), hud.DENY)
        QTest.keyClick(self.bar.input, Qt.Key.Key_Return)
        self.assertEqual(self.submitted, [])           # Enter never starts a second request
        self.bar.button.click()
        self.assertEqual(self.cancelled, [1])
        self.bar.set_cancel_enabled(False)             # on its way: a second click does nothing
        self.assertFalse(self.bar.button.isEnabled())
        self.bar.set_running(False)
        self.assertEqual((self.bar.button.text(), self.bar.button.isEnabled()), (hud.ASK_BUTTON_TEXT, True))
        self.assertFalse(self.bar.input.isReadOnly())
        self.assertEqual(self.bar.input.text(), "move my sync")   # the draft stays

    def test_esc_clears_the_field_or_cancels_a_running_request(self) -> None:
        self.type("never mind")
        QTest.keyClick(self.bar.input, Qt.Key.Key_Escape)
        self.assertEqual(self.bar.input.text(), "")
        self.assertEqual(self.cancelled, [])
        self.type("move it")
        self.bar.set_running(True)
        QTest.keyClick(self.bar.input, Qt.Key.Key_Escape)
        self.assertEqual(self.cancelled, [1])
        self.assertEqual(self.bar.input.text(), "move it")
        self.bar.set_cancel_enabled(False)
        QTest.keyClick(self.bar.input, Qt.Key.Key_Escape)
        self.assertEqual(self.cancelled, [1])

    def test_esc_on_an_empty_idle_field_dismisses_the_answer(self) -> None:
        dismissed: list[int] = []
        self.bar.statusDismissed.connect(lambda: dismissed.append(1))
        self.bar.focus_input()
        QTest.keyClick(self.bar.input, Qt.Key.Key_Escape)
        self.assertEqual(dismissed, [1])

    def test_the_field_takes_at_most_500_characters(self) -> None:
        self.assertEqual(self.bar.input.maxLength(), hud.COMMAND_MAX_LENGTH)
        self.assertEqual(hud.COMMAND_MAX_LENGTH, 500)
        self.bar.set_text("x" * 700)
        self.assertEqual(len(self.bar.input.text()), 500)
        self.assertIn(self.bar.input.placeholderText(), hud.COMMAND_PLACEHOLDERS)   # the longest that fits
        self.assertIn("nothing happens without your OK", hud.COMMAND_PLACEHOLDER)

    def test_markup_is_plain_text_everywhere(self) -> None:
        self.bar.set_status(IMG + " moved", hud.TONE_DONE)
        self.assertEqual(self.bar.status_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(self.bar.status(), IMG + " moved")
        self.bar.set_meta(IMG)
        self.assertEqual(self.bar.meta_label.textFormat(), Qt.TextFormat.PlainText)
        self.bar.set_link("Allow work mail", IMG)
        self.assertNotIn("<img", self.bar.link_button.toolTip())

    def test_the_status_line_never_changes_the_bars_height(self) -> None:
        height = self.bar.sizeHint().height()
        for text, tone in (("", hud.TONE_IDLE), ("Planning... 6 s", hud.TONE_WORKING),
                           ("A long answer " * 40, hud.TONE_DONE), ("Took too long", hud.TONE_ERROR)):
            with self.subTest(tone=tone):
                self.bar.set_status(text, tone)
                settle()
                self.assertEqual(self.bar.sizeHint().height(), height)
                self.assertEqual(self.bar.tone(), tone)
                if text.startswith("A long answer"):          # cut to two lines; the tooltip has it all
                    self.assertTrue(self.bar.status_label.is_cut())
                    self.assertIn("A long answer", self.bar.status_label.toolTip())

    def test_tones_colour_the_status(self) -> None:
        self.bar.set_status("Took too long", hud.TONE_ERROR)
        self.assertIn(hud._qss_color(hud.RED), self.bar.status_label.styleSheet())
        self.bar.set_status("Which Friday?", hud.TONE_WARN)
        self.assertIn(hud._qss_color(hud.AMBER), self.bar.status_label.styleSheet())
        self.bar.set_status("x", "nonsense")
        self.assertEqual(self.bar.tone(), hud.TONE_IDLE)

    def test_the_link_shows_only_with_a_text(self) -> None:
        self.assertFalse(self.bar.link_shown())
        self.bar.set_link("Allow work mail", "Reading email needs one more Google sign-in")
        self.assertTrue(self.bar.link_shown())
        self.assertEqual(self.bar.link(), "Allow work mail")
        self.bar.link_button.click()
        self.assertEqual(self.links, [1])
        self.bar.set_link("")
        self.assertFalse(self.bar.link_shown())

    def test_compact_drops_the_meta_line_and_keeps_the_answer_wide(self) -> None:
        self.bar.set_meta("2 proposals under ASK")
        self.bar.set_link("Allow work mail", "tip")
        full = self.bar.sizeHint().height()
        self.bar.set_compact(True)
        settle()
        self.assertTrue(self.bar.is_compact())
        self.assertLess(self.bar.sizeHint().height(), full)
        self.assertFalse(self.bar.meta_label.isVisible())
        self.bar.set_status("Idle hint", hud.TONE_IDLE)
        self.assertTrue(self.bar.link_shown())               # beside the status while idle
        self.bar.set_status("Moved the sync to Friday", hud.TONE_DONE)
        self.assertFalse(self.bar.link_shown())              # an answer keeps the whole width
        self.assertIn("2 PROPOSALS UNDER ASK", self.bar.status_label.toolTip())
        self.bar.set_compact(False)
        settle()
        self.assertTrue(self.bar.link_shown() and self.bar.meta_label.isVisible())
        self.assertEqual(self.bar.sizeHint().height(), full)

    def test_the_link_hides_while_a_request_runs(self) -> None:
        self.bar.set_link("Allow work mail", "tip")
        self.assertTrue(self.bar.link_shown())
        self.bar.set_running(True)
        self.assertFalse(self.bar.link_shown())             # no browser sign-in during an Ask
        self.bar.set_running(False)
        self.assertTrue(self.bar.link_shown())

    def test_a_status_about_the_link_keeps_it_beside_it_when_compact(self) -> None:
        self.bar.set_link("Allow work mail", "tip")
        self.bar.set_compact(True)
        self.bar.set_status("Jarvis couldn't read your work mail", hud.TONE_DONE, keep_link=True)
        self.assertTrue(self.bar.link_shown())
        self.bar.set_status("Moved the sync to Friday", hud.TONE_DONE)
        self.assertFalse(self.bar.link_shown())

    def test_the_idle_hint_folds_the_status_and_meta_away(self) -> None:
        self.bar.set_meta("20 runs left this hour")
        full = self.bar.sizeHint().height()
        self.bar.set_hint("Type a request")
        settle()
        self.assertTrue(self.bar.is_folded())
        self.assertFalse(self.bar.meta_label.isVisible())
        self.assertLess(self.bar.sizeHint().height(), full - 30)
        self.assertIn("Type a request", self.bar.input.toolTip())
        self.assertEqual(self.bar.status(), "Type a request")
        self.bar.set_link("Allow work mail", "tip")         # a link to offer: the lines come back
        settle()
        self.assertFalse(self.bar.is_folded())
        self.bar.set_link("")
        self.assertTrue(self.bar.is_folded())
        self.bar.set_running(True)                          # anything else the bar says unfolds it
        self.assertFalse(self.bar.is_folded())
        self.bar.set_running(False)
        self.bar.set_status("Moved the sync to Friday", hud.TONE_DONE)
        settle()
        self.assertFalse(self.bar.is_folded())
        self.assertEqual(self.bar.sizeHint().height(), full)

    def test_the_hint_may_have_a_longer_tooltip(self) -> None:
        self.bar.set_hint("Type a request", tooltip="Type a request.\nStart with web: to look something up <b>x</b>")
        self.assertEqual(self.bar.status(), "Type a request")             # the status itself is the hint
        tip = self.bar.input.toolTip()
        self.assertIn("Start with web: to look something up", tip)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", tip)                         # plain text, never markup
        self.bar.set_hint("Type a request")
        self.assertNotIn("web:", self.bar.input.toolTip())

    def test_the_placeholder_keeps_its_promise_short(self) -> None:
        self.assertEqual(hud.COMMAND_PLACEHOLDER, "Ask Jarvis - nothing happens without your OK")
        for width, expected in ((700, hud.COMMAND_PLACEHOLDERS[0]), (300, hud.COMMAND_PLACEHOLDERS[1]),
                                (280, hud.COMMAND_PLACEHOLDERS[2]), (200, hud.COMMAND_PLACEHOLDERS[3])):
            with self.subTest(width=width):
                self.bar.resize(width, self.bar.height())
                settle()
                shown = self.bar.input.placeholderText()
                self.assertEqual(shown, expected)
                room = self.bar.input.width() - 8
                self.assertLessEqual(hud.QFontMetricsF(self.bar.input.font()).horizontalAdvance(shown), room)

    def test_every_button_is_a_tab_stop_never_a_default(self) -> None:
        for button in (self.bar.button, self.bar.link_button):
            self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.TabFocus)
            self.assertFalse(button.isDefault() or button.autoDefault())
        self.assertEqual(self.bar.input.focusPolicy(), Qt.FocusPolicy.StrongFocus)


class ReadingScreenTests(unittest.TestCase):

    def setUp(self) -> None:
        self.window = ui.BriefingWindow(10, 30)
        self.addCleanup(self.window.deleteLater)
        self.reading = self.window.reading
        self.toggles: list[int] = []
        self.reading.playPause.connect(lambda: self.toggles.append(1))

    def show(self, *, ask: bool, size: tuple[int, int] = (1120, 700)) -> None:
        self.window.show_reading_view()
        self.reading.set_ask_available(ask)
        self.window.setMinimumSize(600, 400)
        self.window.resize(*size)
        self.window.show()
        settle()

    def tearDown(self) -> None:
        self.window.hide()
        settle()

    def test_the_bar_is_hidden_while_ask_is_off(self) -> None:
        self.show(ask=False)
        self.assertFalse(self.reading.ask_available())
        self.assertFalse(self.reading.command_bar.isVisible())

    def test_the_bar_sits_at_the_bottom_of_home_under_the_caption(self) -> None:
        # HOLOGRAM_SPEC 11.3 / 16.0 (it sat between the orb row and the controls before the hologram):
        # Home only, under the orb and the caption band, centred, at most 620 wide plus its ring.
        self.show(ask=True)
        bar = self.reading.command_bar
        self.assertEqual(self.reading.current_tab(), ui.SCREEN_HOME)
        self.assertTrue(bar.isVisible())
        y = {name: widget.mapTo(self.reading, widget.rect().topLeft()).y()
             for name, widget in (("orb", self.reading.hero), ("caption", self.reading.captions), ("bar", bar))}
        self.assertLess(y["orb"], y["caption"])
        self.assertLess(y["caption"], y["bar"])
        self.assertLessEqual(bar.width(), holo.COMMAND_MAX_WIDTH + 8)
        stage = self.reading.stage
        centre = stage.mapTo(self.reading, stage.rect().center()).x()
        self.assertLessEqual(abs(bar.mapTo(self.reading, bar.rect().center()).x() - centre), 2)
        corner = bar.mapTo(self.window, QPoint(bar.width(), bar.height()))
        self.assertLessEqual(corner.y(), self.window.height())
        self.reading.set_tab(hud.TAB_BRIEFING)                  # the transport controls never share a screen
        settle()
        self.assertFalse(bar.isVisible())
        self.assertTrue(self.reading.play_button.isVisible())

    def test_space_in_the_bar_is_a_space_elsewhere_it_plays_and_pauses(self) -> None:
        self.show(ask=True)
        bar = self.reading.command_bar
        bar.focus_input()
        settle()
        QTest.keyClicks(bar.input, "move it")
        QTest.keyClick(bar.input, Qt.Key.Key_Space)
        settle()
        self.assertEqual(bar.input.text(), "move it ")
        self.assertEqual(self.toggles, [])
        bar.set_running(True)                         # read only: Space types nothing and plays nothing
        QTest.keyClick(bar.input, Qt.Key.Key_Space)
        settle()
        self.assertEqual((bar.input.text(), self.toggles), ("move it ", []))
        self.reading.setFocus(Qt.FocusReason.OtherFocusReason)
        settle()
        QTest.keyClick(self.reading, Qt.Key.Key_Space)
        settle()
        self.assertEqual(self.toggles, [1])
        self.reading.text.setFocus(Qt.FocusReason.OtherFocusReason)   # the read-only transcript
        settle()
        QTest.keyClick(self.reading.text, Qt.Key.Key_Space)
        settle()
        self.assertEqual(self.toggles, [1, 1])

    def test_keeps_space_covers_buttons_and_editable_text_only(self) -> None:
        self.assertTrue(ui._keeps_space(self.reading.play_button))
        self.assertTrue(ui._keeps_space(self.reading.command_bar.input))
        self.assertFalse(ui._keeps_space(self.reading.text))   # QTextBrowser, read only
        self.assertFalse(ui._keeps_space(self.reading))
        self.assertFalse(ui._keeps_space(None))

    def test_home_has_the_hologram_pill_at_every_size(self) -> None:
        # The compact bar used to depend on the window's size; Home's pill is always one line under the field
        # and never shows the meta line (its counts and seconds are LIVE's).
        for size in ((1120, 700), (900, 640), (900, 600), (760, 652)):
            with self.subTest(size=size):
                self.show(ask=True, size=size)
                bar = self.reading.command_bar
                self.assertTrue(bar.is_holo() and bar.is_compact())
                bar.set_meta(f"1.2 s {hud.MIDDLE_DOT} 3 runs left this hour")
                self.assertFalse(bar.meta_label.isVisible())
                self.assertTrue(bar.send_button.isVisible())
                self.assertFalse(bar.button.isVisible())

    def test_the_briefing_transport_keeps_its_room_with_the_bar(self) -> None:
        # Round 2: the Briefing screen's transport is one centred row inside its stage (no wrapping flow).
        for size in ((760, 652), (900, 600), (1120, 700)):
            with self.subTest(size=size):
                self.show(ask=True, size=size)
                self.reading.set_tab(hud.TAB_BRIEFING)
                settle()
                stage = self.reading.briefing_stage
                for phase in (briefing_view.READY, briefing_view.PLAYING):
                    stage.set_phase(phase)
                    settle()
                    buttons = [self.reading.play_button, self.reading.skip_button, self.reading.everything_button,
                               self.reading.notion_button]
                    shown = [button for button in buttons if button.isVisible()]
                    self.assertEqual(len(shown), 4 if phase == briefing_view.PLAYING else 1)
                    for button in shown:
                        self.assertIs(button.parentWidget(), stage)
                        self.assertTrue(stage.rect().contains(button.geometry()), (size, phase, button.text()))

    def test_the_claude_chip_is_for_the_reading_screen_only(self) -> None:
        header = self.window.header
        header.set_service(ui.ASK_CHIP, hud.STATUS_OK)
        self.window.show_prompt_view()
        self.assertTrue(header.service_chip(ui.ASK_CHIP).isHidden())
        left, _top, right, _bottom = ui._PROMPT_MARGINS
        for name in ("notion", "voice", "calendar"):
            header.set_service(name, hud.STATUS_OK)
        self.assertLessEqual(header.minimumSizeHint().width(), ui.PROMPT_SIZE.width() - left - right)
        self.window.show_reading_view()
        self.assertFalse(header.service_chip(ui.ASK_CHIP).isHidden())


class GroupHeaderTests(unittest.TestCase):

    def test_headers_sit_between_the_cards_and_go_with_clear(self) -> None:
        cards = hud.ActionList()
        self.addCleanup(cards.deleteLater)
        cards.add_group_header("Ask", 1)
        cards.add_card(hud.ActionCard("a1", "ask \u00b7 move", "Project sync"))
        cards.add_group_header("Briefing", 1)
        cards.add_card(hud.ActionCard("b1", "calendar", "Club meeting"))
        self.assertEqual(cards.group_headers(), [("ASK", 1), ("BRIEFING", 1)])
        self.assertEqual([card.action_id for card in cards.cards()], ["a1", "b1"])
        layout = cards.widget().layout()
        order = [type(layout.itemAt(i).widget()).__name__ for i in range(4)]
        self.assertEqual(order, ["GroupHeader", "ActionCard", "GroupHeader", "ActionCard"])
        cards.clear()
        self.assertEqual((cards.group_headers(), cards.cards()), ([], []))
        self.assertFalse(cards.empty_label.isHidden())


if __name__ == "__main__":
    unittest.main()
