"""Tests for NEEDS YOUR OK with more than one source: the briefing's cards and Ask Jarvis's.

One list per source, merged ASK then BRIEFING with a group header each (no headers without Ask
cards, so the briefing's cards look exactly as before), a briefing refresh keeps the Ask cards and
an Ask keeps the briefing's, an Ask card the briefing also proposes is information only, a card
counting down keeps its content and its countdown when an Ask arrives, Ask cards are never read
out in the spoken "Needs your OK", and Approve on an Ask card goes through the same countdown to
the (fake) calendar. Qt runs offscreen; nothing reaches Google, Notion or claude.exe.
"""

from __future__ import annotations

import dataclasses
import time
import unittest

from PySide6.QtWidgets import QApplication

from briefing_reader import hud, ui
from briefing_reader.actions import SOURCE_ASK, SOURCE_BRIEFING, parse_action_line
from briefing_reader.ask.validate import ALREADY_LISTED
from tests.ui_fakes import B_CALENDAR, CAL_LINE, MOVE_LINE, AppHarness, fonts, settle, wait_for

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_card_sources", "-platform", "offscreen"])
    fonts()


def ask_card(line: str):
    return dataclasses.replace(parse_action_line(line), source=SOURCE_ASK)


def shown(c: ui.AppController) -> list[tuple[str, str, str, bool]]:
    """(label, title, detail, actionable) of every card, in order."""
    return [(card.kind_label.full_text(), card.title(), card.detail_label.text(), card.actionable)
            for card in c.window.reading.approvals.cards()]


class CardSourceTests(unittest.TestCase):

    def setUp(self) -> None:
        self.app = AppHarness(self)
        self.app.reading()
        self.c = self.app.c

    def test_the_briefing_cards_look_exactly_as_with_ask_off(self) -> None:
        off = AppHarness(self, ask=False)
        off.reading()
        self.assertEqual(shown(self.c), shown(off.c))
        self.assertEqual(self.c.window.reading.approvals.group_headers(), [])
        self.assertEqual([a.id for a in self.c._actions], [a.id for a in off.c._actions])

    def test_ask_cards_come_first_each_source_with_its_header(self) -> None:
        self.c._take_ask_cards([ask_card(CAL_LINE)])
        settle()
        reading = self.c.window.reading
        self.assertEqual(reading.approvals.group_headers(), [("ASK", 1), ("BRIEFING", 2)])
        self.assertEqual([a.source for a in self.c._actions], [SOURCE_ASK, SOURCE_BRIEFING, SOURCE_BRIEFING])
        labels = [label for label, *_rest in shown(self.c)]
        self.assertEqual(labels, ["ASK \u00b7 CALENDAR", "CALENDAR", "TODO"])
        self.assertEqual(reading.approvals_panel.meta_label.text(), "3 pending")

    def test_a_briefing_refresh_keeps_the_ask_cards_and_an_ask_keeps_the_briefings(self) -> None:
        self.c._take_ask_cards([ask_card(CAL_LINE)])
        ask_ids = [a.id for a in self.c._actions if a.source == SOURCE_ASK]
        page = [parse_action_line(B_CALENDAR)]
        self.c._set_source(SOURCE_BRIEFING, page)
        self.assertEqual([a.id for a in self.c._actions if a.source == SOURCE_ASK], ask_ids)
        self.assertEqual([a.id for a in self.c._actions if a.source == SOURCE_BRIEFING], [page[0].id])
        self.c._take_ask_cards([ask_card(MOVE_LINE)])
        self.assertEqual([a.id for a in self.c._actions if a.source == SOURCE_BRIEFING], [page[0].id])
        self.assertEqual(len([a for a in self.c._actions if a.source == SOURCE_ASK]), 2)   # newest first

    def test_an_ask_card_the_briefing_also_has_is_information_only(self) -> None:
        duplicate = ask_card(B_CALENDAR)
        self.c._take_ask_cards([duplicate])
        asked = [a for a in self.c._actions if a.source == SOURCE_ASK]
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0].error, ALREADY_LISTED)
        self.assertNotEqual(asked[0].id, duplicate.id)
        self.assertEqual(sum(1 for a in self.c._actions if a.id == duplicate.id), 1)   # the briefing's card
        label, _title, detail, actionable = shown(self.c)[0]
        self.assertEqual((label, actionable), ("ASK \u00b7 CALENDAR", False))
        self.assertIn("Already in your list under BRIEFING", detail)

    def test_the_spoken_needs_your_ok_leaves_out_ask_cards(self) -> None:
        before = self.c._build_script(include_note=False)
        self.c._take_ask_cards([ask_card(CAL_LINE), ask_card(MOVE_LINE)])
        after = self.c._build_script(include_note=False)
        self.assertEqual(before.sections, after.sections)
        self.assertEqual({a.source for a in self.c._pending_page_actions()}, {SOURCE_BRIEFING})
        self.assertEqual(len(self.c._pending_actions()), 4)            # they still need your OK

    def test_an_ask_during_a_countdown_keeps_it(self) -> None:
        reading = self.c.window.reading
        page_card = next(a for a in self.c._actions if a.kind == "calendar")
        reading.action_card(page_card.id).approve_button.click()
        settle()
        countdown = self.c._countdown
        self.assertIsNotNone(countdown)
        self.c._take_ask_cards([ask_card(CAL_LINE)])
        settle()
        self.assertIs(self.c._countdown, countdown)
        self.assertEqual(reading.action_card(page_card.id).status(), hud.CARD_COUNTDOWN)
        reading.set_tab(ui.SCREEN_APPROVALS)                     # the cards are on the Approvals screen
        settle()
        self.assertTrue(reading.action_card(page_card.id).undo_button.isVisible())

    def test_an_ask_card_counting_down_keeps_its_content(self) -> None:
        first = ask_card(CAL_LINE)
        self.c._take_ask_cards([first])
        self.c.window.reading.action_card(first.id).approve_button.click()
        settle()
        self.assertIsNotNone(self.c._countdown)
        changed = dataclasses.replace(first, title="Something else")   # same id from a newer answer
        self.c._take_ask_cards([changed])
        self.assertIsNotNone(self.c._countdown)
        self.assertEqual(self.c._action(first.id).title, "Study block")

    def test_approve_on_an_ask_card_goes_through_the_countdown_to_the_calendar(self) -> None:
        card = ask_card(CAL_LINE)
        self.c._take_ask_cards([card])
        widget = self.c.window.reading.action_card(card.id)
        widget.approve_button.click()
        settle()
        personal = self.app.calendars["personal"]
        self.assertEqual(widget.status(), hud.CARD_COUNTDOWN)
        self.assertNotIn(("create", "Study block"), personal.calls)   # nothing before the countdown ends
        self.c._countdown.deadline = time.monotonic() - 0.001
        self.c._on_countdown_tick()
        self.assertTrue(wait_for(lambda: ("create", "Study block") in personal.calls))
        self.assertTrue(wait_for(lambda: self.c.window.reading.action_card(card.id).status() == hud.CARD_ADDED))
        self.assertEqual(personal.calls.count(("create", "Study block")), 1)
        self.assertEqual(self.c._store.get(card.id)["status"], "created")
        self.assertEqual(personal.threads - {"ask"}, {"calendar"})

    def test_at_most_sixteen_ask_cards_stay(self) -> None:
        for n in range(20):
            self.c._take_ask_cards([ask_card(f"Calendar: Block {n} | 2026-10-06 19:00-20:00")])
        asked = [a for a in self.c._actions if a.source == SOURCE_ASK]
        self.assertEqual(len(asked), ui._ASK_CARDS_MAX)
        self.assertEqual(asked[0].title, "Block 19")                   # the newest first, the oldest gone

    def test_deny_on_an_ask_card_is_saved_by_id(self) -> None:
        card = ask_card(CAL_LINE)
        self.c._take_ask_cards([card])
        self.c.window.reading.action_card(card.id).deny_button.click()
        settle()
        self.assertEqual(self.c._store.get(card.id)["status"], "denied")
        self.assertEqual(self.c.window.reading.action_card(card.id).result_text(), "DENIED")
        self.assertNotIn(card.id, [a.id for a in self.c._pending_actions()])


if __name__ == "__main__":
    unittest.main()
