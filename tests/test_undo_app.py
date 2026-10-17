"""The owner's decision of 2026-10-17 in the app: no visible countdown anywhere, an Undo beside the
confirmed state.

A Calendar event, a Todo's block, an RSVP and a Move act at once on the owner's click; the card shows
ADDED / DECLINED / MOVED with the time and Undo, which reverses exactly that change through the same
calendar with the change's own notification policy, is refused when the event changed or vanished
since ("Changed since - left as it is."), and goes after 10 minutes or when the card is cleared. A
Reply, Email or Cancel shows Sent / Cancelled with Undo at once while the call waits a hidden hold:
nothing goes before the hold ends, Undo during it sends nothing, after it exactly one call. The hold goes
on when the window is minimised; Jarvis closing during it sends it at once (or the next start says "Not
sent - Jarvis closed first"). No surface shows a number, voice never approves or undoes, and the tick
box and own-address gates are unchanged. AppHarness: fake calendars and senders that only record, Qt
offscreen; every name, address and id is invented.
"""

from __future__ import annotations

import json
import re
import time
import unittest
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import QApplication

from briefing_reader import actions, dock, hud, ui
from briefing_reader.actions import ActionStore, parse_action_line
from briefing_reader.ask import routes
from tests.ui_fakes import B_CALENDAR, MOVE_LINE, PDT, AppHarness, fonts, settle, wait_for

_app: QApplication | None = None
TODO_BLOCK = "Todo: title=Read chapter 4 | due=2026-10-07 | block=2026-10-06 19:00-21:00 | acct= | link="
RSVP_NO = ("RSVP: acct=work | event=jts0001aa | cal=primary | answer=no | notify=external | title=Jarvis test sync | "
           "at=2026-10-05 14:00-15:00 | due= | link= | body=")
CANCEL_LINE = ("Cancel: acct=work | event=jts0001aa | cal=primary | notify=all | title=Jarvis test sync | "
               "at=2026-10-05 14:00-15:00 | link= | body=")
ANA = "ana@example.edu"            # a trusted domain in AppHarness: no tick needed
DANA = "dana@example.com"          # a NEW RECIPIENT
OWN = "me@example.edu"             # AppHarness's work sender
EMAIL_OK = (f"Email: acct=work | to={ANA} | cc= | subject=Budget numbers | due= | link= | "
            "body=Hi Ana,\\nThe numbers are in.\\nThanks")
EMAIL_NEW = (f"Email: acct=work | to={DANA} | cc= | subject=Study group notes | due= | link= | "
             "body=Hi Dana,\\nHere are my notes.\\nThanks")
EMAIL_SELF = f"Email: acct=work | to={OWN} | cc= | subject=Note to self | due= | link= | body=Remember the slides"
# A countdown's number anywhere: "SENDING IN 7 S", "in 5 s", "Adding the block in 8 s", "7 S".
COUNTDOWN_RE = re.compile(r"\b(?:in|IN)\s+\d+\s*(?:s|S|seconds?)\b|\b\d+\s*S\b")


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_undo_app", "-platform", "offscreen"])
    fonts()


class UndoAppTestCase(unittest.TestCase):
    lines: tuple[str, ...] = (B_CALENDAR, TODO_BLOCK, MOVE_LINE, RSVP_NO, CANCEL_LINE, EMAIL_OK)

    def setUp(self) -> None:
        self.app = AppHarness(self, ask=False, lines=self.lines)
        for calendar in self.app.calendars.values():
            calendar.stamps = True
        self.app.reading(autoplay=False)
        self.c, self.reading = self.app.c, self.app.c.window.reading
        self.reading.set_tab(ui.SCREEN_APPROVALS)
        self.personal, self.work = self.app.calendars["personal"], self.app.calendars["work"]
        self.sender = self.app.senders["work"]
        settle()

    def action(self, kind: str):
        return next(action for action in self.c._actions if action.kind == kind)

    def card(self, action) -> hud.ActionCard:
        card = self.reading.action_card(action.id)
        self.assertIsNotNone(card)
        return card

    def approve(self, action, *, check: bool = False) -> hud.ActionCard:
        if check:
            self.assertTrue(wait_for(lambda: action.id in self.c._checks, 5), "Google's view of the event")
        card = self.card(action)
        card.approve_button.click()
        settle()
        return card

    def wait_done(self, action) -> None:
        self.assertTrue(wait_for(lambda: action.id not in self.c._jobs and action.id not in self.c._running, 10))
        settle()

    def click_undo(self, action) -> None:
        offer = self.c._undos.get(action.id)
        if offer is not None:
            offer.shown -= 5   # past the double-click guard
        if self.c._send_hold is not None:
            self.c._send_hold.started -= 5
        self.card(action).undo_button.click()
        settle()

    def end_hold(self) -> None:
        self.assertIsNotNone(self.c._send_hold)
        self.c._send_hold.deadline = time.monotonic() - 0.001
        self.c._on_send_hold_tick()

    def assert_no_number(self, *texts: str) -> None:
        for text in texts:
            self.assertIsNone(COUNTDOWN_RE.search(text or ""), text)


class CalendarUndoTests(UndoAppTestCase):
    def test_an_event_is_added_at_once_and_undo_deletes_exactly_it(self) -> None:
        action = self.action("calendar")
        card = self.approve(action)
        self.assertIsNone(self.c._send_hold)              # no countdown, no hold: it went at once
        self.wait_done(action)
        self.assertIn(("create", action.title), self.personal.calls)
        event_id = next(key for key in self.personal.events if key.startswith("evt-new"))
        self.assertEqual(card.status(), hud.CARD_ADDED)
        self.assertEqual(card.result_text(), "ADDED \u00b7 1:52 PM")   # the confirmed state and its time
        self.assertEqual(card.undo_text(), "Undo")
        self.assertTrue(card.undo_button.isVisibleTo(card))
        self.click_undo(action)
        self.wait_done(action)
        self.assertIn(("undo_create", event_id), self.personal.calls)
        self.assertEqual(self.personal.notified[-1], ("undo_create", "none"))   # the insert told nobody
        self.assertNotIn(event_id, self.personal.events)
        self.assertEqual(card.status(), hud.CARD_PENDING)                     # it waits for a decision again
        self.assertEqual(card.note(), ui.UNDONE_NOTES[actions.CALENDAR])
        self.assertIsNone(self.app.store.get(action.id))
        self.assertNotIn(action.id, self.c._undos)

    def test_a_todo_block_is_added_at_once_and_undo_deletes_it(self) -> None:
        action = next(action for action in self.c._actions if action.kind == "todo" and action.actionable)
        card = self.approve(action)
        self.wait_done(action)
        self.assertEqual((card.status(), card.undo_text()), (hud.CARD_ADDED, "Undo"))
        event_id = next(key for key in self.personal.events if key.startswith("evt-new"))
        self.click_undo(action)
        self.wait_done(action)
        self.assertIn(("undo_create", event_id), self.personal.calls)
        self.assertEqual(card.note(), ui.UNDONE_NOTES[actions.TODO])

    def test_an_rsvp_acts_at_once_and_undo_puts_the_earlier_answer_back(self) -> None:
        action = self.action("rsvp")
        before = self.work.events["jts0001aa"]
        card = self.approve(action, check=True)
        self.wait_done(action)
        self.assertIn(("respond", "jts0001aa", "no"), self.work.calls)
        self.assertEqual(self.work.events["jts0001aa"].self_response, "declined")
        self.assertTrue(card.result_text().startswith("DECLINED \u00b7 "), card.result_text())
        self.click_undo(action)
        self.wait_done(action)
        self.assertIn(("undo_respond", "jts0001aa", before.self_response), self.work.calls)
        self.assertEqual(self.work.notified[-2:], [("respond", "externalOnly"), ("undo_respond", "externalOnly")])
        self.assertEqual(self.work.events["jts0001aa"].self_response, before.self_response)
        self.assertEqual(card.note(), ui.UNDONE_NOTES[actions.RSVP])

    def test_a_move_acts_at_once_and_undo_moves_it_back(self) -> None:
        action = self.action("move")
        before = self.work.events["jts0001aa"]
        card = self.approve(action, check=True)
        self.wait_done(action)
        self.assertIn(("move", "jts0001aa"), self.work.calls)
        self.assertEqual(self.work.events["jts0001aa"].start, datetime(2026, 10, 9, 14, tzinfo=PDT))
        self.assertTrue(card.result_text().startswith("MOVED \u00b7 "), card.result_text())
        self.click_undo(action)
        self.wait_done(action)
        self.assertEqual(self.work.notified[-1], ("undo_move", "all"))   # the move's own policy
        self.assertEqual((self.work.events["jts0001aa"].start, self.work.events["jts0001aa"].end),
                         (before.start, before.end))
        self.assertEqual(card.note(), ui.UNDONE_NOTES[actions.MOVE])

    def test_undo_is_refused_when_the_event_changed_or_vanished_since(self) -> None:
        for change in ("changed", "vanished"):
            with self.subTest(change=change):
                self.setUp()
                action = self.action("move")
                card = self.approve(action, check=True)
                self.wait_done(action)
                moved = self.work.events["jts0001aa"]
                if change == "changed":
                    self.work.touch("jts0001aa")    # someone edited it in Google Calendar since
                else:
                    del self.work.events["jts0001aa"]
                self.click_undo(action)
                self.wait_done(action)
                self.assertEqual(card.status(), hud.CARD_SENT)          # left as it is
                self.assertEqual(card.note(), "Changed since - left as it is.")
                self.assertEqual(card.undo_text(), "")
                self.assertNotIn(action.id, self.c._undos)
                if change == "changed":
                    self.assertEqual(self.work.events["jts0001aa"].start, moved.start)
                self.assertEqual(self.app.store.get(action.id)["status"], actions.STATUS_SENT)

    def test_a_failed_undo_says_so_and_offers_retry_which_only_reverses(self) -> None:
        action = self.action("calendar")
        card = self.approve(action)
        self.wait_done(action)
        event_id = next(key for key in self.personal.events if key.startswith("evt-new"))
        real = self.personal.undo_create

        def failing(*args, **kwargs):
            from briefing_reader.gcal import CalendarError
            raise CalendarError("Could not reach Google Calendar while removing the event (OSError)")

        self.personal.undo_create = failing
        with self.assertLogs("briefing_reader", level="INFO"):
            self.click_undo(action)
            self.wait_done(action)
        self.assertEqual((card.status(), card.undo_text()), (hud.CARD_ADDED, "Retry"))
        self.assertIn("Undo didn't go through", card.note())
        creates = [call for call in self.personal.calls if call[0] == "create"]
        self.personal.undo_create = real
        self.click_undo(action)                                     # Retry: the reversal again, nothing else
        self.wait_done(action)
        self.assertNotIn(event_id, self.personal.events)
        self.assertEqual([call for call in self.personal.calls if call[0] == "create"], creates)
        self.assertEqual(card.status(), hud.CARD_PENDING)

    def test_undo_goes_after_ten_minutes_or_when_the_card_is_cleared(self) -> None:
        action = self.action("calendar")
        card = self.approve(action)
        self.wait_done(action)
        self.assertTrue(self.c._undo_timer.isActive())
        self.c._undos[action.id].until = time.monotonic() - 1   # 10 minutes later
        self.c._on_undo_tick()
        settle()
        self.assertEqual((card.status(), card.undo_text(), card.result_text()), (hud.CARD_ADDED, "", "ADDED"))
        self.assertFalse(self.c._undo_timer.isActive())
        self.c.undo_action(action.id)                            # nothing is offered any more
        settle()
        self.assertNotIn("undo_create", [call[0] for call in self.personal.calls])
        self.assertEqual(ui.UNDO_WINDOW_S, 600)
        # Cleared: a newer page without the card takes its Undo with it.
        block = next(action for action in self.c._actions if action.kind == "todo" and action.actionable)
        self.approve(block)
        self.wait_done(block)
        self.assertIn(block.id, self.c._undos)
        self.c._set_source("briefing", [parse_action_line(B_CALENDAR)])
        settle()
        self.assertNotIn(block.id, self.c._undos)

    def test_another_cards_undo_waits_while_an_approval_runs(self) -> None:
        action = self.action("calendar")
        card = self.approve(action)
        self.wait_done(action)
        mail = self.action("email")
        self.approve(mail)                                          # its hold locks the other cards
        self.assertTrue(card.is_locked())
        self.assertFalse(card.undo_button.isEnabled())
        self.c._undos[action.id].shown -= 5
        with self.assertLogs("briefing_reader.ui", level="INFO"):
            self.c.undo_action(action.id)
        self.assertNotIn("undo_create", [call[0] for call in self.personal.calls])


class HoldTests(UndoAppTestCase):
    def test_send_shows_sent_and_undo_at_once_and_nothing_goes_before_the_hold_ends(self) -> None:
        action = self.action("email")
        card = self.approve(action)
        self.assertIsNotNone(self.c._send_hold)
        self.assertEqual((card.status(), card.result_text(), card.undo_text()), (hud.CARD_SENT, "SENT", "Undo"))
        self.assertEqual(self.app.store.get(action.id)["status"], actions.STATUS_HELD)
        self.c._on_send_hold_tick()                                  # the hold goes on
        settle()
        self.assertEqual(self.sender.sent, [])
        self.end_hold()
        self.wait_done(action)
        self.assertEqual(len(self.sender.sent), 1)                   # exactly one send
        self.assertEqual((card.status(), card.undo_text()), (hud.CARD_SENT, ""))   # the Undo went with the hold
        self.assertEqual(self.app.store.get(action.id)["status"], actions.STATUS_SENT)

    def test_undo_during_the_hold_sends_nothing(self) -> None:
        action = self.action("email")
        card = self.approve(action)
        self.click_undo(action)
        self.assertIsNone(self.c._send_hold)
        self.assertEqual(card.status(), hud.CARD_PENDING)
        self.assertEqual(card.note(), "Not sent - undone.")
        self.assertIsNone(self.app.store.get(action.id))
        self.assertTrue(wait_for(lambda: False, 0.3) or True)
        self.assertEqual(self.sender.sent, [])

    def test_cancel_event_is_held_like_a_send(self) -> None:
        action = self.action("cancel")
        card = self.approve(action, check=True)
        self.assertEqual((card.status(), card.result_text(), card.undo_text()), (hud.CARD_SENT, "CANCELLED", "Undo"))
        self.assertNotIn("cancel", [call[0] for call in self.work.calls])
        self.click_undo(action)
        self.assertEqual(card.note(), "Not cancelled - undone.")
        self.approve(action)
        self.end_hold()
        self.wait_done(action)
        self.assertEqual([call for call in self.work.calls if call[0] == "cancel"], [("cancel", "jts0001aa")])
        self.assertEqual(self.work.notified[-1], ("cancel", "all"))

    def test_the_hold_goes_on_when_the_window_is_minimised(self) -> None:
        action = self.action("email")
        self.approve(action)
        self.c._on_minimized()
        settle()
        self.assertIsNotNone(self.c._send_hold)                      # never stopped out of sight
        self.assertTrue(self.c._send_hold_timer.isActive())
        self.end_hold()
        self.wait_done(action)
        self.assertEqual(len(self.sender.sent), 1)

    def test_off_approvals_the_header_says_sent_with_an_undo_that_works(self) -> None:
        action = self.action("email")
        self.approve(action)
        header = self.reading.header
        self.assertEqual(header.hold_text(), "")                    # on Approvals the card has it
        self.reading.set_tab(ui.SCREEN_HOME)
        settle()
        self.assertEqual(header.hold_text(), "SENT")
        self.assertTrue(header.undo_button.isVisibleTo(header))
        self.assertEqual(header.hold.accessibleName(), "Sent. Undo is available.")
        self.c._send_hold.started -= 5
        header.undo_button.click()
        settle()
        self.assertIsNone(self.c._send_hold)
        self.assertEqual(header.hold_text(), "")
        self.assertEqual(self.sender.sent, [])

    def test_closing_during_the_hold_sends_it_now(self) -> None:
        action = self.action("email")
        self.approve(action)
        with self.assertLogs("briefing_reader.ui", level="INFO") as logs:
            self.c.shutdown()
        self.assertEqual(len(self.sender.sent), 1)                   # the owner already saw "Sent"
        self.assertEqual(ActionStore(self.app.store.path).get(action.id)["status"], actions.STATUS_SENT)
        self.assertIn("Jarvis is closing during its hold; sending it now", "\n".join(logs.output))

    def test_closing_without_a_worker_marks_the_card_not_sent(self) -> None:
        action = self.action("email")
        self.approve(action)
        worker, self.c._calendar_worker = self.c._calendar_worker, None
        try:
            self.c.shutdown()
        finally:
            worker.stop()
        self.assertEqual(self.sender.sent, [])
        entry = ActionStore(self.app.store.path).get(action.id)
        self.assertEqual((entry["status"], entry["message"]), (actions.STATUS_FAILED, "Not sent - Jarvis closed first"))


class NextStartTests(unittest.TestCase):
    def test_a_hold_jarvis_could_not_finish_shows_not_sent_at_the_next_start(self) -> None:
        app = AppHarness(self, ask=False, lines=(EMAIL_OK,))
        action = parse_action_line(EMAIL_OK)
        path = Path(app.root) / "actions.json"
        path.write_text(json.dumps({action.id: {"status": "held", "at": "2026-10-04T13:50:00-07:00", "link": "",
                                                "message": "", "kind": "email", "account": "work"}}),
                        encoding="utf-8")
        with self.assertLogs("briefing_reader.actions", level="WARNING"):
            app.c._store = ActionStore(path)
        app.reading(autoplay=False)
        card = app.c.window.reading.action_card(action.id)
        self.assertEqual(card.status(), hud.CARD_FAILED)
        self.assertIn("Not sent - Jarvis closed first", card.note())
        self.assertEqual(app.senders["work"].sent, [])


class SurfaceTests(UndoAppTestCase):
    def test_no_countdown_number_on_any_surface(self) -> None:
        self.assertFalse(hasattr(hud, "CARD_COUNTDOWN"))
        self.assertFalse(hasattr(hud.ActionCard, "set_countdown"))
        self.assertFalse(hasattr(dock, "LIVE_COUNTDOWN"))
        action = self.action("calendar")
        card = self.approve(action)
        self.wait_done(action)
        mail = self.action("email")
        mail_card = self.approve(mail)
        self.reading.set_tab(ui.SCREEN_HOME)
        settle()
        self.c._live_feed.drain()
        settle()
        header = self.reading.header
        texts = [card.result_text(), card.accessibleName(), mail_card.result_text(), mail_card.accessibleName(),
                 header.hold_text(), header.hold.accessibleName(), self.reading.live_status.text(),
                 self.reading.dock.accessible_name(dock.LIVE)]
        texts += [f"{entry[2]} {entry[3]}" for entry in self.reading.activity.entries()]   # the ACTIVITY rows
        self.assert_no_number(*texts)
        self.assertEqual(self.reading.live_status.text(), "SENDING")
        self.assertEqual(self.reading.live_status.state(), hud.LIVE_HOLD)
        self.assertFalse(self.reading.dock.blinking())

    def test_voice_and_typed_words_never_approve_or_undo(self) -> None:
        flags = routes.RouteFlags(memory=True, plan=True, prep=True, followups=True)
        for words in ("undo", "undo that", "Jarvis, undo.", "approve", "send it", "approve it please"):
            self.assertEqual(routes.route(words, flags).kind, routes.ROUTE_NONE, words)
        root = Path(ui.__file__).resolve().parent
        for name in ("voice_ui.py", "speech.py", "hotkey.py", "ask/routes.py", "ask/offers.py", "ask_ui.py"):
            source = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("undo_action", source, name)
            self.assertNotIn("approve_action", source, name)
        action = self.action("calendar")
        self.approve(action)
        self.wait_done(action)
        self.c._say("result", "undo that", key="result")   # Jarvis's own words change nothing
        settle()
        self.assertIn(action.id, self.c._undos)
        self.assertNotIn("undo_create", [call[0] for call in self.personal.calls])


class GateTests(UndoAppTestCase):
    lines = (EMAIL_NEW, EMAIL_SELF)

    def test_the_tick_box_and_the_own_address_still_hold_send(self) -> None:
        new = next(action for action in self.c._actions if action.title == "Study group notes")
        card = self.card(new)
        self.assertFalse(card.approve_button.isEnabled())       # tick DANA first
        self.c.approve_action(new.id)
        settle()
        self.assertIsNone(self.c._send_hold)
        own = next(action for action in self.c._actions if action.title == "Note to self")
        with self.assertLogs("briefing_reader.ui", level="INFO"):
            self.c.approve_action(own.id)
        settle()
        self.assertIsNone(self.c._send_hold)
        self.assertEqual(self.sender.sent, [])
        self.assertEqual(self.card(own).status(), hud.CARD_PENDING)


if __name__ == "__main__":
    unittest.main()
