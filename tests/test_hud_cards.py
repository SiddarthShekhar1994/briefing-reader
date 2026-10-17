"""Tests for the NEEDS YOUR OK card texts that come from the page.

Tooltips never read page text as HTML (Qt guesses the format of a tooltip, so
'<img src=...>' in a subject would be fetched on a hover): the cards, the speech
line and the agenda, deadline and section rows. Long email addresses wrap
inside the card, a long alias is cut with an ellipsis, a long draft's tooltip
is capped and the BLOCK ADDED link can be named. The RSVP / Move / Cancel card
states (the result with Undo beside it, sent, unknown with Retry, the reserved check line,
Edit / Sign in / Copy links) never move a card's buttons, and the Edit dialog
returns exactly what was typed and reads nothing as HTML. Qt runs offscreen in
this process with the bundled fonts and shows nothing; every text is invented.
"""

from __future__ import annotations

import gc
import unittest
import weakref

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QWidget

from briefing_reader import config, holo, hud

_app: QApplication | None = None
IMG = '<img src="file://203.0.113.9/share/x.png">'
LONG_TITLE = (IMG + " Work on problem set three, chapter four exercises, and the lab write-up for the "
              "chemistry course before the review session")


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_hud_cards", "-platform", "offscreen"])
    hud.load_fonts(config.PROJECT_ROOT / "fonts")


def as_shown(tooltip: str) -> str:
    """What a tooltip shows: Qt reads it as HTML when it looks like HTML."""
    doc = QTextDocument()
    doc.setHtml(tooltip)
    return doc.toPlainText()


def shown(widget, width: int = 300) -> None:
    widget.resize(width, 400)
    widget.show()
    QApplication.processEvents()


class TooltipTests(unittest.TestCase):

    def test_plain_tooltip_shows_markup_as_text(self) -> None:
        tip = hud.plain_tooltip(IMG + " a & b\nnext line")
        self.assertNotIn("<img", tip)
        self.assertEqual(as_shown(tip), IMG + " a & b\nnext line")
        self.assertEqual(hud.plain_tooltip(""), "")

    def test_a_cut_card_title_has_an_escaped_tooltip(self) -> None:
        card = hud.ActionCard("t1", "todo", LONG_TITLE, "Due Wed Oct 7", approve_text="Done", title_lines=2)
        shown(card)
        self.assertNotEqual(card.title_label.text(), LONG_TITLE)   # cut to two lines
        self.assertEqual(as_shown(card.title_label.toolTip()), LONG_TITLE)
        self.assertNotIn("<img", card.title_label.toolTip())
        card.set_status(hud.CARD_FAILED, LONG_TITLE * 2)
        QApplication.processEvents()
        self.assertNotIn("<IMG", card.failure_label.toolTip())
        self.assertEqual(as_shown(card.failure_label.toolTip()), ("FAILED: " + LONG_TITLE * 2).upper())
        card.close()

    def test_a_cut_speech_line_has_an_escaped_tooltip(self) -> None:
        speech = hud.SpeechLabel(max_lines=2)
        shown(speech)
        speech.setText(LONG_TITLE + " " + LONG_TITLE)
        QApplication.processEvents()
        self.assertNotEqual(speech.shown_text(), speech.text())
        self.assertNotIn("<img", speech.toolTip())
        self.assertEqual(as_shown(speech.toolTip()), LONG_TITLE + " " + LONG_TITLE)
        speech.close()

    def test_a_long_draft_tooltip_is_capped(self) -> None:
        body = "\n".join(["This is a fairly long paragraph of drafted reply text that goes on and on."] * 60)
        card = hud.ActionCard("t2", "reply", "Re: Notes", "To: ana@example.edu", body=body,
                              copy_text="Copy reply", approve_text="Done")
        tip = as_shown(card.body_label.toolTip())
        self.assertLess(len(tip), 1300)
        self.assertTrue(tip.endswith("\u2026\n(Copy reply for the whole text)"), tip[-60:])
        self.assertEqual(card.body(), " ".join(body.split()))   # the preview and Copy keep everything
        short = hud.ActionCard("t3", "reply", "Re: Notes", "", body="Hi,\nsee you then.", copy_text="Copy reply")
        self.assertEqual(as_shown(short.body_label.toolTip()), "Hi,\nsee you then.")
        card.close()
        short.close()


class RowTooltipTests(unittest.TestCase):
    """Agenda, deadline and section rows show an event's or a page's title as plain text."""

    def assert_plain(self, widget, text: str) -> None:
        tip = widget.toolTip()
        self.assertNotIn("<img", tip)
        self.assertEqual(as_shown(tip), text)

    def test_an_agenda_row_tooltip_is_never_html(self) -> None:
        row = hud.AgendaRow(hud.AgendaRowInfo("9:00 AM", IMG + " Speaker series", "Room <b>204</b>"))
        self.assert_plain(row, IMG + " Speaker series\nRoom <b>204</b>")
        row.set_info(hud.AgendaRowInfo("ALL DAY", "<i>Holiday</i> & more"))
        self.assert_plain(row, "<i>Holiday</i> & more")
        row.close()

    def test_a_deadline_row_tooltip_is_never_html(self) -> None:
        row = hud.DeadlineRow(hud.DeadlineRowInfo(IMG + " Problem set 3", "Canvas <u>course</u>", "Tomorrow"))
        self.assert_plain(row, IMG + " Problem set 3\nCanvas <u>course</u>\nDue: Tomorrow")
        row.close()

    def test_a_section_row_tooltip_is_never_html(self) -> None:
        row = hud.SectionRow(hud.SectionRowInfo(IMG + " Still needs a reply", "3 items"), "01")
        self.assert_plain(row, IMG + " Still needs a reply")
        row.set_info(hud.SectionRowInfo("<b>Deadlines</b>"))
        self.assert_plain(row, "<b>Deadlines</b>")
        row.close()


class EventCardTests(unittest.TestCase):
    """The RSVP / Move / Cancel card: nothing above the buttons moves when its state changes."""

    def make(self, **options):
        defaults = {"approve_text": "Accept", "open_text": "Open event", "edit_text": "Edit",
                    "sign_in_text": "Sign in", "check_line": True}
        defaults.update(options)
        card = hud.ActionCard("c1", "rsvp \u00b7 work", "Speaker series", "Answer: yes \u00b7 Tue Oct 6",
                              **defaults)
        shown(card)
        return card

    def test_states_keep_the_buttons_in_place(self) -> None:
        card = self.make()
        deny_x = card.deny_button.mapTo(card, card.deny_button.rect().topLeft()).x()
        height = card.height()
        card.set_check_line("Google: " + IMG + " Speaker series \u00b7 Tue Oct 6 \u00b7 5:00-6:00 PM \u00b7 "
                            "organized by someone else \u00b7 you have not answered")
        card.set_sign_in_visible(True)
        card.set_approve_text("Retry")
        QApplication.processEvents()
        self.assertEqual(card.check_line()[:8], "Google: ")
        self.assertNotIn("<img", card.check_label.toolTip())
        self.assertEqual(card.height(), height)   # the check line was reserved from the start
        self.assertEqual(card.deny_button.mapTo(card, card.deny_button.rect().topLeft()).x(), deny_x)
        self.assertEqual(card.approve_text(), "Retry")
        accept_x = card.approve_button.mapTo(card, card.approve_button.rect().topLeft()).x()
        # Accepted at once: the confirmed state with the time and Undo beside it (no countdown, no number).
        card.set_status(hud.CARD_SENT, "Accepted \u00b7 2:41 PM")
        card.set_undo("Undo")
        QApplication.processEvents()
        self.assertTrue(card.undo_button.isVisible())
        self.assertFalse(card.deny_button.isVisible())
        self.assertEqual((card.result_text(), card.undo_text()), ("ACCEPTED \u00b7 2:41 PM", "Undo"))
        self.assertEqual(card.height(), height)
        undo = card.undo_button.mapTo(card, card.undo_button.rect().topRight()).x()
        self.assertLess(undo, accept_x)   # left of where Accept was: a second click there never undoes
        card.set_undo("Retry")
        self.assertEqual((card.undo_button.text(), card.undo_text()), ("Retry", "Retry"))
        card.set_status(hud.CARD_UNKNOWN, "check the calendar before retrying")
        QApplication.processEvents()
        self.assertFalse(card.undo_button.isVisible())
        self.assertTrue(card.deny_button.isVisible() and card.approve_button.isVisible())
        self.assertEqual(card.failure_label.full_text(), "UNKNOWN: CHECK THE CALENDAR BEFORE RETRYING")
        card.set_status(hud.CARD_SENT, "Accepted", "https://www.google.com/calendar/event?eid=x", "Open event")
        QApplication.processEvents()
        self.assertEqual((card.result_text(), card.open_button.text()), ("ACCEPTED", "Open event"))
        self.assertTrue(card.open_button.isVisible())
        card.close()

    def test_undo_edit_and_sign_in_signals_carry_the_id(self) -> None:
        card = self.make()
        got: list[tuple[str, str]] = []
        card.undoClicked.connect(lambda action_id: got.append(("undo", action_id)))
        card.editClicked.connect(lambda action_id: got.append(("edit", action_id)))
        card.signInClicked.connect(lambda action_id: got.append(("sign in", action_id)))
        card.edit_button.click()
        card.set_sign_in_visible(True)
        card.sign_in_button.click()
        card.set_status(hud.CARD_SENT, "Sent")
        card.set_undo("Undo")
        card.undo_button.click()
        self.assertEqual(got, [("edit", "c1"), ("sign in", "c1"), ("undo", "c1")])
        card.set_edit_enabled(False)
        self.assertFalse(card.edit_button.isEnabled())
        card.close()

    def test_a_long_button_fits_a_narrow_card(self) -> None:
        card = self.make(approve_text="Cancel event")
        wide = card.approve_button.width()
        card.resize(212, 400)
        QApplication.processEvents()
        button = card.approve_button
        right = button.mapTo(card, button.rect().topRight()).x()
        self.assertLessEqual(right, card.width() - 1)   # inside the card, the text whole
        self.assertGreaterEqual(button.width(), button.tight_width("Cancel event"))
        self.assertLess(button.width(), wide)
        deny_x = card.deny_button.mapTo(card, card.deny_button.rect().topLeft()).x()
        card.set_approve_text("Done")
        QApplication.processEvents()
        self.assertEqual(card.deny_button.mapTo(card, card.deny_button.rect().topLeft()).x(), deny_x)
        card.close()

    def test_the_tools_slot_switches_without_moving_anything(self) -> None:
        card = self.make(open_text="", copy_text="Copy note", event_text="Open event")
        card.resize(212, 400)   # the narrowest card (760 px window)
        QApplication.processEvents()
        self.assertEqual(card.tool_slot(), hud.TOOL_EDIT)
        self.assertTrue(card.edit_button.isVisible() and card.sign_in_button.isHidden()
                        and card.event_button.isHidden())
        place, height = card.edit_button.geometry(), card.height()
        deny = card.deny_button.mapTo(card, card.deny_button.rect().topLeft())
        got: list[tuple[str, str]] = []
        card.sourceClicked.connect(lambda action_id: got.append(("open", action_id)))
        card.signInClicked.connect(lambda action_id: got.append(("sign in", action_id)))
        for mode, button in ((hud.TOOL_SIGN_IN, card.sign_in_button), (hud.TOOL_OPEN, card.event_button),
                             (hud.TOOL_EDIT, card.edit_button)):
            with self.subTest(mode=mode):
                card.set_tool_slot(mode)
                QApplication.processEvents()
                self.assertEqual(card.tool_slot(), mode)
                self.assertTrue(button.isVisible())
                self.assertEqual(sum(b.isVisible() for b in (card.edit_button, card.sign_in_button,
                                                             card.event_button)), 1)
                self.assertEqual(button.geometry(), place)   # the same place, the same width
                self.assertEqual(card.height(), height)
                self.assertEqual(card.deny_button.mapTo(card, card.deny_button.rect().topLeft()), deny)
                if button is not card.edit_button:
                    button.click()
        self.assertEqual(got, [("sign in", "c1"), ("open", "c1")])
        card.set_edit_enabled(False)   # another card's hold: no dialog, no sign-in beside it
        self.assertFalse(card.edit_button.isEnabled() or card.sign_in_button.isEnabled())
        self.assertTrue(card.event_button.isEnabled())
        plain = self.make()            # built without "Open event": that mode shows Edit
        plain.set_tool_slot(hud.TOOL_OPEN)
        self.assertEqual(plain.tool_slot(), hud.TOOL_EDIT)
        card.close()
        plain.close()

    def test_skip_names_the_left_hand_button(self) -> None:
        card = self.make(deny_text="Skip", approve_text="Decline")
        self.assertEqual((card.deny_button.text(), card.approve_text()), ("Skip", "Decline"))
        card.set_status(hud.CARD_DENIED, "Skipped")
        self.assertEqual(card.result_text(), "SKIPPED")
        card.close()

    def test_three_check_lines_show_googles_title_and_time_at_760(self) -> None:
        card = self.make(approve_text="Cancel event")
        card.resize(212, 400)
        QApplication.processEvents()
        text = ("Google: Quarterly planning review with the\u2026 \u00b7 Thu Oct 8 \u00b7 12:00-1:00 PM \u00b7 "
                "organized by Ana Example \u00b7 you haven't answered \u00b7 as you@example.edu")
        card.set_check_line(text)
        QApplication.processEvents()
        self.assertIn("12:00-1:00 PM", card.check_label.text())   # title and time are never cut
        self.assertEqual(card.check_label.full_text(), text)

    def test_an_edited_kind_label_wraps_instead_of_being_cut(self) -> None:
        card = hud.ActionCard("c2", "cancel \u00b7 personal", "Study group", "Fri Oct 9", approve_text="Cancel event",
                              edit_text="Edit", check_line=True)
        shown(card, 212)
        height = card.height()
        card.set_texts("cancel \u00b7 personal \u00b7 edited", "Study group", "Fri Oct 9 \u00b7 guests notified")
        QApplication.processEvents()
        self.assertNotIn("\u2026", card.kind_label.text())
        self.assertEqual(card.kind_label.text().replace("\n", " "), "CANCEL \u00b7 PERSONAL \u00b7 EDITED")
        card.set_texts("cancel \u00b7 personal", "Study group", "Fri Oct 9")
        QApplication.processEvents()
        self.assertGreaterEqual(card.height(), height)   # it never gets shorter again
        card.close()

    def test_undo_shows_only_beside_a_confirmed_result_and_waits_while_locked(self) -> None:
        card = self.make()
        card.set_undo("Undo")                     # waiting for a decision: no Undo
        self.assertEqual(card.undo_text(), "")
        self.assertFalse(card.undo_button.isVisible())
        card.set_status(hud.CARD_ADDED, "Added \u00b7 2:41 PM")
        card.set_undo("Undo")
        QApplication.processEvents()
        self.assertTrue(card.undo_button.isVisible())
        card.set_locked(True, "Finishing the previous approval...")
        self.assertFalse(card.undo_button.isEnabled())   # another card's approval runs
        card.set_locked(False)
        self.assertTrue(card.undo_button.isEnabled())
        card.set_status(hud.CARD_PENDING)                # undone: the buttons are back, the Undo is gone
        self.assertEqual(card.undo_text(), "")
        self.assertFalse(card.undo_button.isVisible())
        self.assertFalse(hasattr(card, "countdown_label"))
        card.close()

    def test_undo_keeps_the_keyboard_focus_on_its_card(self) -> None:
        host = QWidget()
        first = hud.ActionCard("u1", "rsvp \u00b7 work", "Speaker series", "Answer: yes", approve_text="Accept",
                               edit_text="Edit", check_line=True, parent=host)
        second = hud.ActionCard("u2", "move \u00b7 work", "Project sync", "New time", approve_text="Move",
                                open_text="Open event", edit_text="Edit", check_line=True, parent=host)
        second.move(0, 400)
        shown(host, 300)
        host.activateWindow()
        first.set_status(hud.CARD_SENT, "Sent")
        first.set_undo("Undo")
        QApplication.processEvents()
        first.undo_button.setFocus(Qt.FocusReason.TabFocusReason)
        QApplication.processEvents()
        if QApplication.focusWidget() is first.undo_button:   # offscreen focus may be unavailable
            first.set_status(hud.CARD_PENDING)   # Undo pressed: the buttons are back
            QApplication.processEvents()
            focus = QApplication.focusWidget()
            self.assertFalse(focus is not None and second.isAncestorOf(focus), focus)
        host.close()

    def test_copy_can_be_added_after_an_edit(self) -> None:
        card = self.make(approve_text="Move")
        self.assertFalse(card.copy_button.isVisible())
        edit_x = card.edit_button.x()
        card.set_copy_text("Copy note")
        card.set_body("Moving to 2 PM so everyone can join.")
        QApplication.processEvents()
        self.assertTrue(card.copy_button.isVisible())
        self.assertEqual(card.copy_text(), "Copy note")
        card.show_copied()
        self.assertEqual(card.copy_button.text(), "Copied")
        self.assertGreaterEqual(card.edit_button.x(), edit_x)
        card.set_copy_text("")
        self.assertFalse(card.copy_button.isVisible())
        card.close()


class EditDialogTests(unittest.TestCase):

    def test_values_are_what_was_typed(self) -> None:
        dialog = hud.EditDialog("m1", hud.EDIT_MOVE, "move \u00b7 work", IMG + " Project sync",
                                notify="all", date_text="2026-10-08", start_text="14:00", end_text="15:00",
                                note="Moving to 2 PM.", check_text="Google: <b>Project sync</b>")
        self.assertEqual(dialog.title_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(dialog.check_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(dialog.check_label.text(), "Google: <b>Project sync</b>")
        dialog.end_edit.setText(" 15:30 ")
        dialog.notify_box.setChecked(False)
        got: list = []
        dialog.saved.connect(lambda action_id, values: got.append((action_id, values)))
        dialog.save_button.click()
        self.assertEqual(got, [("m1", {"answer": "yes", "notify": "none", "date": "2026-10-08", "start": "14:00",
                                       "end": "15:30", "note": "Moving to 2 PM."})])
        dialog.show_error("the new time must end 5 minutes to 12 hours after it starts")
        self.assertTrue(dialog.error().startswith("the new time"))
        dialog.close()

    def test_enter_in_a_field_saves_and_times_follow_the_clock(self) -> None:
        dialog = hud.EditDialog("m2", hud.EDIT_MOVE, "move \u00b7 work", "Project sync", date_text="2026-10-08",
                                start_text="2:00 PM", end_text="3:00 PM")
        got: list = []
        dialog.saved.connect(lambda action_id, values: got.append(values))
        self.assertTrue(dialog.save_button.isDefault())
        self.assertEqual(dialog.start_edit.placeholderText(), "2:00 PM")
        dialog.show()
        QApplication.processEvents()
        dialog.end_edit.setFocus()
        QTest.keyClick(dialog.end_edit, Qt.Key.Key_Return)
        self.assertEqual([(v["start"], v["end"]) for v in got], [("2:00 PM", "3:00 PM")])
        dialog.close()
        hour24 = hud.EditDialog("m3", hud.EDIT_MOVE, "move", "Project sync", hour24=True)
        self.assertEqual(hour24.start_edit.placeholderText(), "14:00")
        hour24.close()

    def test_rsvp_notify_says_who_is_emailed(self) -> None:
        dialog = hud.EditDialog("r2", hud.EDIT_RSVP, "rsvp \u00b7 work", "Speaker series", notify="all")
        self.assertEqual(dialog.notify_box.text(), "Email the organizer about my answer")
        outside = hud.EditDialog("r3", hud.EDIT_RSVP, "rsvp \u00b7 work", "Speaker series", notify="external")
        self.assertEqual(outside.notify_box.text(), "Email the organizer only if outside your organization")
        dialog.close()
        outside.close()

    def test_an_rsvp_answer_is_picked_with_one_button(self) -> None:
        dialog = hud.EditDialog("r1", hud.EDIT_RSVP, "rsvp \u00b7 work", "Speaker series", answer="yes",
                                notify="external")
        dialog.answer_buttons["maybe"].click()
        values = dialog.values()
        self.assertEqual((values["answer"], values["notify"]), ("maybe", "external"))
        self.assertTrue(dialog.answer_buttons["maybe"].isChecked())
        self.assertFalse(dialog.answer_buttons["yes"].isChecked())
        self.assertTrue(dialog.date_edit.isHidden())
        with self.assertRaises(ValueError):
            hud.EditDialog("x", "share", "share", "Trip budget")   # Share has no Edit dialog
        dialog.close()


MAIL_BODY = ("Hi both,\nShall we keep it at noon, or move it to 2 PM? The agenda: "
             "https://docs.google.com/document/d/EXAMPLE/edit\n\nThanks")
LONG_ADDRESS = "someone.with.a.really.long.address@subdomain.exampleuniversity.edu"


def mail_links(text: str) -> list[tuple[int, int]]:
    start = text.find("https://")
    return [] if start < 0 else [(start, text.index("edit", start) + 4)]


class MailCardTests(unittest.TestCase):
    """A Reply / Email card: FROM, the recipient chips and the text exactly as it will be sent."""

    def make(self, body: str = MAIL_BODY, width: int = 300):
        card = hud.ActionCard("m1", "reply \u00b7 work", "Re: Thursday noon meeting", "due today",
                              approve_text="Send", body=body, title_lines=2, open_text="Open thread",
                              copy_text="Copy reply", edit_text="Edit", mail=True, body_lines=10,
                              body_links=mail_links(body), approve_alternatives=("Send", "Done"))
        card.set_recipients("work (ana@example.edu)",
                            [hud.RecipientChip("ben@example.edu"), hud.RecipientChip(LONG_ADDRESS, hud.RECIPIENT_NEW)],
                            [hud.RecipientChip("cy@example.com", hud.RECIPIENT_CONFIRMED)])
        shown(card, width)
        card.resize(width, card.heightForWidth(width))   # as tall as the list makes it
        QApplication.processEvents()
        return card

    def test_the_text_keeps_its_line_breaks_and_links(self) -> None:
        card = self.make()
        self.assertTrue(card.body_label.isHidden())
        self.assertFalse(card.mail_body.isHidden())
        self.assertEqual(card.mail_text(), MAIL_BODY)
        lines = card.mail_body.shown_lines()
        self.assertEqual(lines[0], "Hi both,")
        self.assertEqual(lines[-2:], ["", "Thanks"])
        self.assertFalse(card.mail_body.is_cut())
        self.assertEqual(card.mail_body.links(), tuple(mail_links(MAIL_BODY)))
        self.assertEqual(card.body(), " ".join(MAIL_BODY.split()))
        card.close()

    def test_a_long_text_is_cut_at_ten_lines(self) -> None:
        body = "\n".join(f"Line {n} of the drafted reply" for n in range(1, 16))
        card = self.make(body)
        lines = card.mail_body.shown_lines()
        self.assertEqual(len(lines), 10)
        self.assertTrue(lines[-1].endswith("\u2026"), lines[-1])
        self.assertTrue(card.mail_body.is_cut())
        self.assertIn("Line 15", as_shown(card.mail_body.toolTip()))
        card.close()

    def test_the_chips_name_every_recipient_whole(self) -> None:
        card = self.make(width=260)
        chips = card.recipients
        self.assertEqual(chips.text(), "From: work (ana@example.edu)\n"
                                       f"To: ben@example.edu, {LONG_ADDRESS} (new recipient, not confirmed)\n"
                                       "Cc: cy@example.com (new recipient, confirmed)")
        self.assertEqual(as_shown(chips.toolTip()), chips.text())
        self.assertEqual(chips.accessibleName(), chips.text())
        self.assertLessEqual(chips.heightForWidth(chips.width()), chips.height() + 1)
        self.assertLessEqual(chips.minimumSizeHint().width(), chips.width())
        card.close()

    def test_the_sending_account_itself_is_shown_red_with_its_badge(self) -> None:
        card = self.make(width=300)
        card.set_recipients("personal (ana.lima@example.edu)",
                            [hud.RecipientChip("ana.lima@example.edu", hud.RECIPIENT_OWN)])
        QApplication.processEvents()
        chips = card.recipients
        self.assertEqual(chips.text(), "From: personal (ana.lima@example.edu)\n"
                                       "To: ana.lima@example.edu (the sending account itself, never sent to)")
        items = chips._layout(chips.sender_text(), *chips.chips(), chips.width())[1]
        self.assertIn(("badge", hud.OWN_RECIPIENT_TEXT, hud.RECIPIENT_OWN),
                      [(item[0], item[2], item[3]) for item in items if item[0] == "badge"])
        self.assertLessEqual(chips.heightForWidth(chips.width()), chips.height() + 1)
        card.close()

    def test_a_card_knows_when_it_does_not_show_all_of_the_message(self) -> None:
        """Few line breaks but long paragraphs, or a long subject: the card cuts them, and says so
        (Send then opens the Edit dialog to read it all first)."""
        paragraph = "The sampling plan is fine, but the budget table still uses last year's numbers. " * 4
        card = self.make(f"Hi Ana,\n{paragraph}\n{paragraph}\n{paragraph}\nI can't come on Friday.\nThanks")
        self.assertTrue(card.mail_body.is_cut())
        self.assertNotIn("Friday", "\n".join(card.mail_body.shown_lines()))
        self.assertTrue(card.mail_cut())
        short = self.make()
        self.assertFalse(short.mail_cut())
        short.set_texts("reply \u00b7 work", "Re: " + "a very long subject about the lab schedule " * 6, "")
        QApplication.processEvents()
        self.assertTrue(short.title_label.is_cut())
        self.assertTrue(short.mail_cut())
        for each in (card, short):
            each.close()

    def test_a_short_address_never_breaks_inside_a_domain_name(self) -> None:
        font = hud.mono_font(11)
        for address in ("pat.lee@example.net", "jordan.smith@example.org"):
            lines = hud._wrapped_lines(address, font, hud._text_advance(font, address[:-1]))
            self.assertEqual("".join(lines), address)
            self.assertGreater(len(lines), 1)
            for first, second in zip(lines, lines[1:]):   # only after "@" or before ".": never inside a name
                self.assertTrue(first.endswith("@") or second.startswith("."), lines)
        self.assertEqual(hud._with_soft_breaks("ana@example.edu"), "ana@example.edu")   # a card detail: as before
        self.assertEqual(hud._with_soft_breaks("ana@example.edu", 0).split("\u200b"), ["ana@", "example", ".edu"])

    def test_an_emptied_tick_hint_draws_no_glyph(self) -> None:
        # L3: the hint keeps its room once its NEW recipient goes, but never as a bare amber circled-i.
        def amber_pixels(widget: QWidget) -> int:
            image = widget.grab().toImage()
            colours = [image.pixelColor(x, y) for x in range(0, image.width(), 2) for y in range(0, image.height(), 2)]
            return sum(1 for c in colours if c.red() > 200 and 120 < c.green() < 220 and c.blue() < 120)

        hint = hud._TickHint()
        hint.resize(260, 40)
        hint.set_hint("Tick dana@example.com first, then Send.", False)
        self.assertGreater(amber_pixels(hint), 0)
        hint.set_hint("", False)
        hint.show()                       # as _sync_tick_hint keeps it: its room stays
        self.assertEqual(amber_pixels(hint), 0)
        hint.close()

    def test_text_links_are_amber_only_while_a_decision_waits(self) -> None:
        # L2: Open / Copy / Undo are amber on a needs-you tile, holo cyan beside a done (mint) result.
        card = self.make()
        self.assertEqual({card.copy_button.face(), card.source_button.face()}, {holo.LINK})
        card.set_status(hud.CARD_SENT, "Sent")
        card.set_undo("Undo")
        self.assertEqual({card.copy_button.face(), card.source_button.face(), card.undo_button.face()},
                         {holo.LINK_CYAN})
        card.set_status(hud.CARD_PENDING)
        self.assertEqual(card.copy_button.face(), holo.LINK)
        card.close()

    def test_space_on_send_hands_the_focus_to_undo(self) -> None:
        card = self.make()
        card.activateWindow()
        card.approve_button.setFocus(Qt.FocusReason.TabFocusReason)
        QApplication.processEvents()
        if QApplication.focusWidget() is not card.approve_button:   # offscreen focus may be unavailable
            card.close()
            self.skipTest("no keyboard focus offscreen")
        card.set_status(hud.CARD_SENT, "Sent")       # Sent at once, with Undo beside it
        card.set_undo("Undo")
        self.assertIs(QApplication.focusWidget(), card.undo_button)
        card.copy_button.setFocus(Qt.FocusReason.TabFocusReason)   # Tab on during the hold ...
        card.set_status(hud.CARD_SENT, "Sent")
        card.set_undo("Undo")                                      # ... is never pulled back
        self.assertIs(QApplication.focusWidget(), card.copy_button)
        card.undo_button.setFocus(Qt.FocusReason.TabFocusReason)
        card.set_status(hud.CARD_PENDING)   # Undo: the focus stays on the card, never on Send (S3)
        self.assertIs(QApplication.focusWidget(), card)
        card.close()

    def test_the_card_never_shrinks_when_the_recipients_change(self) -> None:
        card = self.make()
        height = card.heightForWidth(card.width())
        card.set_recipients("work (ana@example.edu)", [hud.RecipientChip("ben@example.edu")])
        card.set_body("Thanks")
        QApplication.processEvents()
        self.assertGreaterEqual(card.heightForWidth(card.width()), height)
        self.assertEqual(card.recipients.chips(), ((hud.RecipientChip("ben@example.edu"),), ()))
        self.assertEqual(card.mail_text(), "Thanks")
        card.close()

    def test_send_retry_and_done_never_move_deny(self) -> None:
        card = self.make()
        deny_x = card.deny_button.mapTo(card, card.deny_button.rect().topLeft()).x()
        for text in ("Retry", "Done", "Send"):
            card.set_approve_text(text)
            QApplication.processEvents()
            self.assertEqual(card.deny_button.mapTo(card, card.deny_button.rect().topLeft()).x(), deny_x, text)
        card.close()


class MailEditDialogTests(unittest.TestCase):
    """The Edit dialog of a Reply / Email returns exactly what is shown; adding an address is checked."""

    def make(self, kind: str = hud.EDIT_REPLY, **options):
        defaults = {"sender": "work (ana@example.edu)",
                    "to": [hud.RecipientChip("ben@example.edu"), hud.RecipientChip("dee@example.org", hud.RECIPIENT_NEW)],
                    "cc": [], "body": MAIL_BODY, "find_links": mail_links,
                    "normalize": lambda text: text.strip().lower() if "@" in text and " " not in text.strip() else "",
                    "classify": lambda address: (hud.RECIPIENT_OWN if address == "ana@example.edu"
                                                 else hud.RECIPIENT_NEW if address.endswith(".org")
                                                 else hud.RECIPIENT_KNOWN),
                    "max_recipients": 3}
        defaults.update(options)
        return hud.EditDialog("m1", kind, "reply \u00b7 work", IMG + " Re: Notes", **defaults)

    def test_values_are_what_is_shown(self) -> None:
        dialog = self.make()
        # Round 2 (the owner's decision of 2026-10-12): a new recipient is ticked on the card, never here.
        self.assertEqual(dialog.values(), {"to": ["ben@example.edu", "dee@example.org"], "cc": [],
                                           "subject": IMG + " Re: Notes", "body": MAIL_BODY})
        self.assertTrue(dialog.subject_edit.isReadOnly())        # a reply keeps its thread's subject
        self.assertTrue(dialog.title_label.isHidden())
        self.assertEqual(dialog.from_label.text(), "work (ana@example.edu)")
        new = dialog.to_list.row("dee@example.org")
        self.assertIsNotNone(new.badge)
        self.assertIsNone(dialog.to_list.row("ben@example.edu").badge)
        self.assertEqual(new.focus_chain(), [new.remove_button])   # no tick in the dialog: only Remove
        self.assertEqual([box.text() for box in dialog.findChildren(QCheckBox) if box.text().startswith("Send")],
                         [])
        self.assertEqual(dialog.links_label.text(), "Contains 1 link (highlighted)")
        dialog.close()

    def test_adding_an_address_is_checked(self) -> None:
        dialog = self.make()
        to = dialog.to_list
        for typed, error in (("not an address", '"not an address" is not an email address'),
                             ("BEN@example.edu", "ben@example.edu is already a recipient"),
                             ("ana@example.edu", hud.OWN_ADDRESS_ERROR)):
            to.add_edit.setText(typed)
            to.add_typed()
            self.assertEqual(to.error(), error)
        dialog.cc_list.add_edit.setText("eve@example.org")
        dialog.cc_list.add_typed()
        self.assertEqual(dialog.values()["cc"], ["eve@example.org"])
        added = dialog.cc_list.row("eve@example.org")   # new: coral, its tick is on the card after Save
        self.assertEqual((added.badge.state(), added.badge.text()), (hud.RECIPIENT_NEW, hud.NEW_RECIPIENT_TEXT))
        to.add_edit.setText("fay@example.edu")
        to.add_typed()
        self.assertEqual(to.error(), "At most 3 recipients in To and Cc together")
        to.rows[0].remove_button.click()
        QApplication.processEvents()
        self.assertEqual(dialog.values()["to"], ["dee@example.org"])
        dialog.close()

    def test_enter_in_the_add_field_adds_and_never_saves(self) -> None:
        dialog = self.make()
        got = []
        dialog.saved.connect(lambda _id, values: got.append(values))
        dialog.show()
        QApplication.processEvents()
        dialog.cc_list.add_edit.setFocus()
        dialog.cc_list.add_edit.setText("gil@example.edu")
        QTest.keyClick(dialog.cc_list.add_edit, Qt.Key.Key_Return)
        self.assertEqual(got, [])
        self.assertEqual(dialog.values()["cc"], ["gil@example.edu"])
        dialog.save_button.click()
        self.assertEqual([values["cc"] for values in got], [["gil@example.edu"]])
        dialog.close()

    def test_a_long_new_address_wraps(self) -> None:
        dialog = self.make(cc=[hud.RecipientChip(LONG_ADDRESS + "x", hud.RECIPIENT_NEW)], max_recipients=5)
        dialog.show()
        QApplication.processEvents()
        row = dialog.cc_list.rows[0]
        label = row.address_label
        self.assertEqual(label.text(), LONG_ADDRESS + "x")
        self.assertLessEqual(label.heightForWidth(label.width()), label.height() + 1)   # wrapped, not cut
        self.assertEqual(row.badge.text(), hud.NEW_RECIPIENT_TEXT)
        height = dialog.height()
        dialog.to_list.add_edit.setText("gil@example.org")
        dialog.to_list.add_typed()
        QApplication.processEvents()
        self.assertGreater(dialog.height(), height)   # the dialog grows with its rows, nothing overlaps
        body = dialog.body_edit
        self.assertGreaterEqual(dialog.links_label.y(), body.y() + body.height())
        dialog.close()

    def test_an_address_typed_but_not_added_keeps_the_dialog_open(self) -> None:
        dialog = self.make()
        got = []
        dialog.saved.connect(lambda _id, values: got.append(values))
        dialog.show()
        QApplication.processEvents()
        dialog.cc_list.add_edit.setText("zoe@example.net")
        dialog.save_button.click()
        self.assertEqual(got, [])
        self.assertEqual(dialog.error(), hud.NOT_ADDED_ERROR.format(text="zoe@example.net", name="Cc"))
        dialog.cc_list.add_edit.clear()
        dialog.save_button.click()
        self.assertEqual([values["cc"] for values in got], [[]])
        dialog.close()

    def test_tab_follows_the_dialog_from_top_to_bottom(self) -> None:
        dialog = self.make(cc=[hud.RecipientChip("eve@example.org", hud.RECIPIENT_NEW)], max_recipients=5)
        to, cc = dialog.to_list, dialog.cc_list
        expected = [to.row("ben@example.edu").remove_button, to.row("dee@example.org").remove_button,
                    to.add_edit, to.add_button, cc.row("eve@example.org").remove_button, cc.add_edit,
                    cc.add_button, dialog.subject_edit, dialog.body_edit, dialog.cancel_button, dialog.save_button]
        chain = [expected[0]]
        while len(chain) < len(expected):
            chain.append(chain[-1].nextInFocusChain())
            while chain[-1] not in expected and len(chain) < 200:   # labels and layouts in between
                chain[-1] = chain[-1].nextInFocusChain()
        self.assertEqual(chain, expected)
        to.add_edit.setText("gil@example.org")
        to.add_typed()   # a new row joins the chain where it is shown
        gil = to.row("gil@example.org")
        self.assertIs(to.row("dee@example.org").remove_button.nextInFocusChain(), gil.remove_button)
        dialog.close()

    def test_the_badge_reads_as_on_the_card(self) -> None:
        dialog = self.make(cc=[hud.RecipientChip("eve@example.org", hud.RECIPIENT_CONFIRMED)], max_recipients=5)
        row = dialog.to_list.row("dee@example.org")
        self.assertEqual((row.badge.state(), row.badge.text()), (hud.RECIPIENT_NEW, hud.NEW_RECIPIENT_TEXT))
        ticked = dialog.cc_list.row("eve@example.org")   # ticked on the card: the dialog shows it confirmed
        self.assertEqual((ticked.badge.state(), ticked.badge.text()), (hud.RECIPIENT_CONFIRMED,
                                                                       hud.CONFIRMED_RECIPIENT_TEXT))
        self.assertEqual(ticked.badge.width(), row.badge.width())   # either text: Remove never moves
        dialog.close()

    def test_a_long_message_and_subject_are_shown_whole_and_scroll_on_a_short_screen(self) -> None:
        paragraph = "The sampling plan is fine, but the budget table still uses last year's numbers. " * 5
        body = "\n\n".join([paragraph] * 8) + "\n\nI can't come on Friday."
        subject = "Planning for the shared lab equipment schedule and the booking rules " * 3
        dialog = self.make(hud.EDIT_EMAIL, body=body, max_recipients=5,
                           to=[hud.RecipientChip(f"p{n}@example.org", hud.RECIPIENT_NEW) for n in range(5)])
        dialog.subject_edit.setText(subject.strip())
        dialog.open()
        QApplication.processEvents()
        self.assertTrue(dialog.body_edit.shows_all() and dialog.subject_edit.shows_all())
        self.assertGreater(dialog.subject_edit.lines(), 1)
        room = dialog.screen().availableGeometry()
        self.assertTrue(room.contains(dialog.frameGeometry()), (dialog.frameGeometry(), room))
        save = dialog.save_button.mapToGlobal(dialog.save_button.rect().bottomRight())
        self.assertTrue(room.contains(save))   # Save is always on screen
        bar = dialog.fields_scroll.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)   # taller than the screen: the fields scroll
        self.assertEqual(bar.value(), 0)       # from the top
        self.assertFalse(dialog.whole_seen())
        bar.setValue(bar.maximum() // 2)
        self.assertFalse(dialog.whole_seen())
        bar.setValue(bar.maximum())
        self.assertTrue(dialog.whole_seen())
        dialog.close()

    def test_a_subject_is_one_line_of_text(self) -> None:
        dialog = self.make(hud.EDIT_EMAIL)
        dialog.show()
        QApplication.processEvents()
        dialog.subject_edit.setFocus()
        dialog.subject_edit.setText("Lab")
        cursor = dialog.subject_edit.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        dialog.subject_edit.setTextCursor(cursor)
        got = []
        dialog.saved.connect(lambda _id, values: got.append(values["subject"]))
        QTest.keyClick(dialog.subject_edit, Qt.Key.Key_Return)   # no line break: Enter saves, as in a line edit
        self.assertEqual(got, ["Lab"])
        from PySide6.QtCore import QMimeData
        data = QMimeData()
        data.setText("hours\nBcc: eve@example.com")
        dialog.subject_edit.insertFromMimeData(data)
        self.assertEqual(dialog.subject_edit.text(), "Labhours Bcc: eve@example.com")
        dialog.close()

    def test_the_dialog_is_freed_without_the_garbage_collector(self) -> None:
        """No reference cycle runs through the dialog: freeing it in a garbage-collector pass along
        with its widgets corrupted the heap on Windows."""
        dialog = self.make(cc=[hud.RecipientChip("eve@example.org", hud.RECIPIENT_NEW)], max_recipients=5)
        dialog.to_list.add_edit.setText("gil@example.org")
        dialog.to_list.add_typed()
        dialog.close()
        QApplication.processEvents()   # the dialog's pending re-fit has run
        ref = weakref.ref(dialog)
        gc.disable()
        try:
            del dialog
            self.assertIsNone(ref())
        finally:
            gc.enable()

    def test_the_sending_account_itself_must_be_removed_before_save(self) -> None:
        dialog = self.make(to=[hud.RecipientChip("ana@example.edu", hud.RECIPIENT_OWN),
                               hud.RecipientChip("ben@example.edu")])
        got = []
        dialog.saved.connect(lambda _id, values: got.append(values))
        dialog.show()
        QApplication.processEvents()
        row = dialog.to_list.row("ana@example.edu")
        self.assertEqual((row.badge.text(), row.badge.state()), (hud.OWN_RECIPIENT_TEXT, hud.RECIPIENT_OWN))
        self.assertEqual(row.focus_chain(), [row.remove_button])   # no tick: only Remove
        row.badge.set_state(hud.RECIPIENT_CONFIRMED)   # it has no tick: it stays SENDING ACCOUNT
        self.assertEqual(row.badge.text(), hud.OWN_RECIPIENT_TEXT)
        self.assertEqual(dialog.to_list.own(), ["ana@example.edu"])
        dialog.save_button.click()
        self.assertEqual(got, [])
        self.assertEqual(dialog.error(), hud.OWN_ROW_ERROR.format(address="ana@example.edu"))
        row.remove_button.click()
        QApplication.processEvents()
        self.assertEqual(dialog.error(), "")   # the error goes with the row
        dialog.save_button.click()
        self.assertEqual([values["to"] for values in got], [["ben@example.edu"]])
        dialog.close()

    def test_every_kind_fits_a_small_screen_and_scrolls_inside(self) -> None:
        """At any display scale the dialog stays inside the screen's available area: narrower when
        the area is, and its fields scroll instead of the dialog outgrowing it."""
        from unittest import mock

        from PySide6.QtCore import QRect

        paragraph = "The sampling plan is fine, but the budget table still uses last year's numbers. " * 4
        cases = ((hud.EDIT_REPLY, dict(body="\n\n".join([paragraph] * 6), banner="Read all of it")),
                 (hud.EDIT_RSVP, {}), (hud.EDIT_MOVE, {}), (hud.EDIT_CANCEL, {}))
        for area in (QRect(0, 0, 420, 300), QRect(50, 40, 700, 360), QRect(0, 0, 1280, 672)):
            for kind, options in cases:
                with self.subTest(area=area.getRect(), kind=kind):
                    if kind in hud.EDIT_MAIL_KINDS:
                        dialog = self.make(kind, **options)
                    else:
                        dialog = hud.EditDialog("e1", kind, f"{kind} \u00b7 work", "Speaker series",
                                                date_text="2026-10-08", start_text="2:00 PM", end_text="3:00 PM",
                                                note="Moving to 2 PM so everyone can join.",
                                                check_text="Google: Speaker series \u00b7 Thu Oct 8 \u00b7 2:00-3:00 PM")
                    with mock.patch.object(dialog, "_area", return_value=area):
                        dialog.open()
                        QApplication.processEvents()
                        frame = dialog.frameGeometry()
                        self.assertTrue(area.contains(frame), (frame.getRect(), area.getRect()))
                        save = dialog.save_button
                        corner = save.mapToGlobal(save.rect().bottomRight())
                        self.assertTrue(area.contains(corner))   # Save is always on screen
                        bar = dialog.fields_scroll.verticalScrollBar()
                        content = dialog.fields_scroll.widget()
                        if content.height() > dialog.fields_scroll.viewport().height() + 1:
                            self.assertGreater(bar.maximum(), 0)   # taller than the area: the fields scroll
                        self.assertEqual(dialog.error(), "")
                        dialog.close()

    def test_an_email_subject_can_be_changed(self) -> None:
        dialog = self.make(hud.EDIT_EMAIL, banner="Read all of it")
        self.assertFalse(dialog.subject_edit.isReadOnly())
        self.assertEqual(dialog.banner_label.text(), "Read all of it")
        dialog.subject_edit.setText("Question about the lab")
        dialog.body_edit.setPlainText("Hello,\nIs the lab open?")
        values = dialog.values()
        self.assertEqual((values["subject"], values["body"]), ("Question about the lab", "Hello,\nIs the lab open?"))
        self.assertTrue(dialog.links_label.isHidden())
        dialog.close()


class AccountDialogTests(unittest.TestCase):
    """The first sign-in's question: is it the right Google account? Yes / No, use another account."""

    def make(self, address: str = "ana.lima@example.edu", **options):
        return hud.AccountDialog("personal", address, **options)

    def test_the_question_and_the_answers(self) -> None:
        dialog = self.make()
        self.assertEqual(dialog.question_label.text(),
                         'Signed in as ana.lima@example.edu for "personal" - is that right?')
        self.assertEqual(dialog.question_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn("sends personal email and changes personal calendar events", dialog.detail_label.text())
        self.assertIn("Nothing is sent or changed until you answer", dialog.detail_label.text())
        self.assertEqual((dialog.yes_button.text(), dialog.no_button.text()),
                         (hud.ACCOUNT_YES_TEXT, hud.ACCOUNT_NO_TEXT))
        calendar_only = self.make(sends_mail=False)
        self.assertNotIn("email", calendar_only.detail_label.text())
        got = []
        dialog.answered.connect(lambda alias, address, yes: got.append((alias, address, yes)))
        dialog.open()
        QApplication.processEvents()
        self.assertFalse(dialog.yes_button.isDefault() or dialog.no_button.isDefault())
        QTest.keyClick(dialog, Qt.Key.Key_Return)   # Enter answers nothing
        self.assertEqual(got, [])
        dialog.yes_button.click()
        self.assertEqual(got, [("personal", "ana.lima@example.edu", True)])
        self.assertFalse(dialog.isVisible())
        again = self.make()
        again.answered.connect(lambda alias, address, yes: got.append((alias, address, yes)))
        again.open()
        again.no_button.click()
        self.assertEqual(got[-1], ("personal", "ana.lima@example.edu", False))
        escaped = self.make()
        escaped.answered.connect(lambda alias, address, yes: got.append((alias, address, yes)))
        escaped.open()
        QApplication.processEvents()
        QTest.keyClick(escaped, Qt.Key.Key_Escape)   # closed without an answer: asked again later
        self.assertEqual((len(got), escaped.isVisible()), (2, False))
        calendar_only.close()

    def test_a_long_address_wraps_and_the_dialog_fits_a_small_screen(self) -> None:
        from unittest import mock

        from PySide6.QtCore import QRect

        address = "someone.with.a.really.long.address@subdomain.exampleuniversity.edu"
        for area in (QRect(0, 0, 340, 260), QRect(0, 0, 1280, 672)):
            with self.subTest(area=area.getRect()):
                dialog = self.make(address)
                with mock.patch.object(dialog, "_area", return_value=area):
                    dialog.open()
                    QApplication.processEvents()
                    self.assertTrue(area.contains(dialog.frameGeometry()),
                                    (dialog.frameGeometry().getRect(), area.getRect()))
                    label = dialog.question_label
                    self.assertLessEqual(label.heightForWidth(label.width()), label.height() + 1)   # wrapped, whole
                    for button in (dialog.yes_button, dialog.no_button):
                        self.assertTrue(dialog.rect().contains(button.geometry().translated(
                            button.parentWidget().mapTo(dialog, button.parentWidget().rect().topLeft()))))
                    dialog.close()


class CardTextTests(unittest.TestCase):

    def test_soft_breaks_only_inside_long_words(self) -> None:
        breaks = hud._with_soft_breaks
        self.assertEqual(breaks("To: jordan.smith@example.edu, ana@example.edu"),
                         "To: jordan.smith@example.edu, ana@example.edu")
        self.assertEqual(breaks("alexander.thompson@students.department.example.edu").split("\u200b"),
                         ["alexander", ".thompson@", "students", ".department", ".example", ".edu"])
        self.assertTrue(all(len(piece) <= 24 for piece in breaks("x" * 60).split("\u200b")))
        calendar_detail = "Fri Oct 9 \u00b7 3:00-4:00 PM \u00b7 Room 204, Engineering building"
        self.assertEqual(breaks(calendar_detail), calendar_detail)

    def test_a_long_address_wraps_inside_the_card(self) -> None:
        detail = ("To: alexander.thompson@students.department.example.edu \u00b7 "
                  "Cc: someone.with.a.really.long.address@subdomain.department.example.edu")
        card = hud.ActionCard("t4", "reply \u00b7 work", "Re: Group project", detail, approve_text="Done")
        shown(card, 210)
        label = card.detail_label
        self.assertEqual(label.text(), detail)                 # break chances are display only
        self.assertEqual(label.accessibleName(), detail)
        self.assertLessEqual(label.minimumSizeHint().width(), label.width() + 1)
        self.assertLessEqual(label.heightForWidth(label.width()), label.height() + 1)
        card.close()

    def test_a_long_alias_is_cut_with_an_ellipsis(self) -> None:
        card = hud.ActionCard("t5", "share \u00b7 a-very-long-alias-name1", "Trip budget", "sam@example.com",
                              approve_text="Done")
        shown(card, 210)
        full = "SHARE \u00b7 A-VERY-LONG-ALIAS-NAME1"
        self.assertEqual(card.kind_label.full_text(), full)
        self.assertTrue(card.kind_label.text().endswith("\u2026"), card.kind_label.text())
        self.assertTrue(card.accessibleName().startswith(f"{full}: Trip budget"))
        card.close()

    def test_the_result_link_can_be_named(self) -> None:
        card = hud.ActionCard("t6", "todo", "Read chapter 4", "Due Tue Oct 6", approve_text="Add block")
        card.set_status(hud.CARD_ADDED, "Block added", "https://www.google.com/calendar/event?eid=x", "Open event")
        self.assertEqual((card.open_button.text(), card.result_text()), ("Open event", "BLOCK ADDED"))
        card.set_status(hud.CARD_EXISTS, "", "https://www.google.com/calendar/event?eid=x")
        self.assertEqual(card.open_button.text(), "Open")
        card.close()


if __name__ == "__main__":
    unittest.main()
