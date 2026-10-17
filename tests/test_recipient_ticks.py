"""The NEW RECIPIENT tick on a Reply / Email card (the owner's decision of 2026-10-12; HOLOGRAM_SPEC 0.4, 9).

A new recipient is confirmed with a tick box right on its card: Send stays disabled until it is ticked
("Tick <address> first, then Send."), a programmatic approve sends nothing and opens nothing, a ticked
card shows Sent and sends once after its hidden hold, the address is known afterwards (no tick on the next card), unticking
returns it to NEW, Edit still edits the message and a recipient added there needs its own tick, the
sending account itself is refused, and no address reaches a log. AppHarness: fake senders that only
record, Qt offscreen; every name and address is invented.
"""

from __future__ import annotations

import time
import unittest

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox

from briefing_reader import hud, ui
from tests.ui_fakes import AppHarness, fonts, settle, wait_for

_app: QApplication | None = None
DANA = "dana@example.com"          # not a trusted domain (example.edu), never sent to: NEW
EVE = "eve@example.org"
OWN = "me@example.edu"             # AppHarness's work sender
EMAIL = (f"Email: acct=work | to=Dana Park <{DANA}> | cc= | subject=Study group notes | due= | link= | "
         "body=Hi Dana,\\nHere are my notes.\\nThanks")
EMAIL_TOO = (f"Email: acct=work | to={DANA} | cc= | subject=Room for Tuesday | due= | link= | "
             "body=Hi Dana,\\nRoom 2 is free.\\nThanks")
TO_SELF = f"Email: acct=work | to={OWN} | cc= | subject=Note to self | due= | link= | body=Remember the slides"


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_recipient_ticks", "-platform", "offscreen"])
    fonts()


class RecipientTickTests(unittest.TestCase):
    def make(self, *lines: str) -> AppHarness:
        app = AppHarness(self, ask=False, lines=lines)
        app.reading(autoplay=False)
        self.app, self.c, self.reading = app, app.c, app.c.window.reading
        self.sender = app.senders["work"]
        self.reading.set_tab(ui.SCREEN_APPROVALS)
        settle()
        return app

    def card(self, subject: str):
        action = next(action for action in self.c._actions if action.title == subject)
        card = self.reading.action_card(action.id)
        self.assertIsNotNone(card)
        return action, card

    @staticmethod
    def states(card) -> dict[str, str]:
        to, cc = card.recipients.chips()
        return {chip.address: chip.state for chip in (*to, *cc)}

    def send_now(self, action_id: str) -> None:
        self.assertIsNotNone(self.c._send_hold)
        self.c._send_hold.deadline = time.monotonic() - 0.001
        self.c._on_send_hold_tick()
        self.assertTrue(wait_for(lambda: action_id not in self.c._jobs and bool(self.sender.sent), 10))
        settle()

    def test_an_unticked_new_recipient_keeps_send_disabled_and_sends_nothing(self) -> None:
        self.make(EMAIL)
        action, card = self.card("Study group notes")
        self.assertEqual(self.states(card), {DANA: hud.RECIPIENT_NEW})
        self.assertEqual(card.ticks.addresses(), [DANA])
        self.assertEqual(card.needs_tick(), [DANA])
        self.assertFalse(card.approve_button.isEnabled())
        self.assertEqual(card.approve_button.toolTip(), f"Tick {DANA} first")
        self.assertEqual(card.tick_hint.text(), f"Tick {DANA} first, then Send.")
        self.assertTrue(card.deny_button.isEnabled() and card.edit_button.isEnabled())
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            self.c.approve_action(action.id)   # a key or a program: refused, nothing opens
            settle()
        self.assertIsNone(self.c._send_hold)
        self.assertIsNone(self.c._dialog)
        self.assertEqual(self.sender.sent, [])
        self.assertTrue(card.ticks.tick(DANA).nudging())   # the empty box rings
        self.assertTrue(any("not ticked on the card" in line for line in logs.output))
        self.assertFalse(any(DANA in line or "Dana" in line for line in logs.output))   # ids and counts only
        QTest.mouseClick(card.approve_button, Qt.MouseButton.LeftButton)   # disabled: still nothing
        settle()
        self.assertIsNone(self.c._send_hold)
        self.assertEqual(self.sender.sent, [])

    def test_a_ticked_card_shows_sent_sends_once_and_the_address_is_known_afterwards(self) -> None:
        self.make(EMAIL, EMAIL_TOO)
        action, card = self.card("Study group notes")
        other, other_card = self.card("Room for Tuesday")
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            card.ticks.tick(DANA).click()
            settle()
        self.assertFalse(any(DANA in line for line in logs.output))
        self.assertEqual(self.c._effective(action).confirmed, frozenset({DANA}))
        self.assertEqual(self.states(card), {DANA: hud.RECIPIENT_CONFIRMED})
        self.assertEqual(card.tick_hint.text(), f"{DANA} confirmed.")
        self.assertTrue(card.approve_button.isEnabled())
        # A tick is the card's own: the other card to the same address still waits for its tick.
        self.assertEqual(other_card.needs_tick(), [DANA])
        self.assertFalse(other_card.approve_button.isEnabled())
        self.assertEqual(self.sender.sent, [])
        card.approve_button.click()
        settle()
        self.assertIsNone(self.c._dialog)
        self.assertEqual((card.status(), card.result_text(), card.undo_text()), (hud.CARD_SENT, "SENT", "Undo"))
        self.assertEqual(self.sender.sent, [])   # nothing during the hidden hold
        self.send_now(action.id)
        self.assertEqual(len(self.sender.sent), 1)
        self.assertEqual(card.status(), hud.CARD_SENT)
        self.assertTrue(self.c._history.knows(DANA))
        # Known now: the other card has no tick box and its Send is enabled.
        self.assertEqual(other_card.ticks.addresses(), [])
        self.assertEqual(self.states(other_card), {DANA: hud.RECIPIENT_KNOWN})
        self.assertTrue(other_card.approve_button.isEnabled())
        self.assertEqual(other_card.tick_hint.text(), "")

    def test_unticking_returns_the_recipient_to_new(self) -> None:
        self.make(EMAIL)
        action, card = self.card("Study group notes")
        tick = card.ticks.tick(DANA)
        tick.click()
        settle()
        self.assertTrue(card.approve_button.isEnabled())
        tick = card.ticks.tick(DANA)
        tick.click()
        settle()
        self.assertEqual(self.c._effective(action).confirmed, frozenset())
        self.assertEqual(self.states(card), {DANA: hud.RECIPIENT_NEW})
        self.assertFalse(card.approve_button.isEnabled())
        self.assertEqual(card.tick_hint.text(), f"Tick {DANA} first, then Send.")
        self.c.approve_action(action.id)
        settle()
        self.assertIsNone(self.c._send_hold)
        self.assertEqual(self.sender.sent, [])

    def test_space_ticks_the_box_from_the_keyboard(self) -> None:
        self.make(EMAIL)
        _action, card = self.card("Study group notes")
        self.c.window.activateWindow()
        tick = card.ticks.tick(DANA)
        tick.setFocus(Qt.FocusReason.TabFocusReason)
        QTest.keyClick(tick, Qt.Key.Key_Space)
        settle()
        self.assertTrue(card.ticks.tick(DANA).isChecked())
        self.assertTrue(card.approve_button.isEnabled())

    def test_edit_edits_the_message_and_a_recipient_added_there_needs_its_own_tick(self) -> None:
        self.make(EMAIL)
        action, card = self.card("Study group notes")
        card.ticks.tick(DANA).click()
        settle()
        self.c.edit_action(action.id)
        settle()
        dialog = self.c._dialog
        self.assertIsNotNone(dialog)
        # No tick in the dialog: the address shows as the card has it (ticked: NEW \u00b7 CONFIRMED).
        row = dialog.to_list.row(DANA)
        self.assertEqual(row.badge.state(), hud.RECIPIENT_CONFIRMED)
        self.assertEqual([box for box in dialog.findChildren(QCheckBox) if box.text().startswith("Send")], [])
        dialog.cc_list.add_edit.setText(EVE)
        dialog.cc_list.add_typed()
        self.assertEqual(dialog.cc_list.row(EVE).badge.state(), hud.RECIPIENT_NEW)
        dialog.body_edit.setPlainText("Hi Dana,\nHere are my notes from Sunday.\nThanks")
        dialog.save_button.click()
        settle()
        self.assertIsNone(self.c._dialog)
        edited = self.c._effective(action)
        self.assertEqual(edited.body, "Hi Dana,\nHere are my notes from Sunday.\nThanks")
        self.assertEqual(edited.confirmed, frozenset({DANA}))           # DANA's tick stays
        self.assertEqual(card.ticks.addresses(), [DANA, EVE])
        self.assertEqual(card.needs_tick(), [EVE])                       # EVE needs its own
        self.assertEqual(self.states(card), {DANA: hud.RECIPIENT_CONFIRMED, EVE: hud.RECIPIENT_NEW})
        self.assertFalse(card.approve_button.isEnabled())
        self.assertEqual(card.tick_hint.text(), f"Tick {EVE} first, then Send.")
        self.c.approve_action(action.id)
        settle()
        self.assertIsNone(self.c._send_hold)
        self.assertEqual(self.sender.sent, [])
        card.ticks.tick(EVE).click()
        settle()
        self.assertTrue(card.approve_button.isEnabled())
        self.assertEqual(card.tick_hint.text(), f"{DANA} and {EVE} confirmed.")
        # Removing a ticked address in Edit drops its tick: added again, it is NEW again.
        self.c.edit_action(action.id)
        settle()
        dialog = self.c._dialog
        dialog.cc_list.row(EVE).remove_button.click()
        settle()
        dialog.save_button.click()
        settle()
        self.assertEqual(self.c._effective(action).confirmed, frozenset({DANA}))
        self.assertTrue(card.approve_button.isEnabled())

    def test_the_sending_account_itself_is_refused(self) -> None:
        self.make(TO_SELF)
        action, card = self.card("Note to self")
        self.assertEqual(self.states(card), {OWN: hud.RECIPIENT_OWN})
        self.assertEqual(card.ticks.addresses(), [])   # no tick can make it sendable
        self.c.approve_action(action.id)
        settle()
        self.assertIsNone(self.c._send_hold)
        self.assertEqual(self.sender.sent, [])
        self.c.tick_recipient(action.id, OWN, True)    # even a forged tick changes nothing
        settle()
        self.c.approve_action(action.id)
        settle()
        self.assertIsNone(self.c._send_hold)
        self.assertEqual(self.sender.sent, [])

    def test_a_tick_is_ignored_while_another_approval_counts_down(self) -> None:
        self.make(EMAIL, EMAIL_TOO)
        first, first_card = self.card("Study group notes")
        second, second_card = self.card("Room for Tuesday")
        first_card.ticks.tick(DANA).click()
        settle()
        first_card.approve_button.click()
        settle()
        self.assertIsNotNone(self.c._send_hold)
        self.assertFalse(second_card.ticks.tick(DANA).isEnabled())   # locked with its card
        self.c.tick_recipient(second.id, DANA, True)                  # a program: ignored
        settle()
        self.assertEqual(self.c._effective(second).confirmed, frozenset())
        self.assertFalse(second_card.ticks.tick(DANA).isChecked())
        self.c._undo_send_hold()
        settle()


if __name__ == "__main__":
    unittest.main()
