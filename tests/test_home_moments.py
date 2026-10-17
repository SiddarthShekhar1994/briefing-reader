"""Hologram round 2, part 3a: the Transcript drawer's entries, an offer as Home's ONE amber tile, the day plan
(the timeline with one Add block per block) and the meeting brief (the panel with its reply cards under it).

The widgets alone (transcript_view, offer_view, plan_view, brief_view) and in the app (AppController with the
productivity bundle, a steppable clock and fakes only: nothing is sent, nothing reaches Google, Notion or
Claude). Offers never act before a click; Add block goes through the block's own approval card (at once,
then ADDED with its Undo; no countdown); the brief's reply card is NEEDS YOUR OK's own card (the NEW RECIPIENT
tick, Send, Sent with its Undo); no technical readout shows; with the bundle off none of it appears. Every name
is invented.
"""

from __future__ import annotations

import time
import unittest
from datetime import datetime
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from briefing_reader import brief_view, holo, hud, offer_view, plan_view, transcript_view, ui
from briefing_reader.ask.modes import MODE_PLAN, MODE_PREP
from tests.ask_fakes import FakeReader, plan_stream
from tests.test_offers_app import BLOCK, TITLE, at, meeting
from tests.test_prep_app import ANSWER, sam_thread
from tests.ui_fakes import GUEST, AppHarness, HarnessClock, fonts, settle, wait_for

_app: QApplication | None = None
DOT = chr(0xB7)
READOUTS = ("PLANNER RUN", "RUNS LEFT", "LEFT TODAY", "THREADS READ", " 9.1 S", " 8.2 S")


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_home_moments", "-platform", "offscreen"])
    fonts()


def wall(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 12, hour, minute)


def fake_card(status: str = hud.CARD_PENDING, *, enabled: bool = True, locked: bool = False, result: str = "",
              link: str = "", undo: str = "", approve: str = "Add block") -> SimpleNamespace:
    button = SimpleNamespace(isEnabled=lambda: enabled, toolTip=lambda: "" if enabled else "Finishing...")
    return SimpleNamespace(status=lambda: status, approve_button=button, approve_text=lambda: approve,
                           is_locked=lambda: locked, result_text=lambda: result, link=lambda: link,
                           undo_text=lambda: undo, undo_button=SimpleNamespace(isEnabled=lambda: not locked))


class WindowCase(unittest.TestCase):
    def setUp(self) -> None:
        holo.set_reduced_motion(True)
        self.addCleanup(holo.set_reduced_motion, None)
        self.window = ui.BriefingWindow(10, 30)
        self.addCleanup(self.window.deleteLater)
        self.window.set_clock(lambda: datetime(2026, 10, 12, 21, 51))
        self.reading = self.window.reading
        self.reading.set_ask_available(True)
        self.window.show_reading_view()
        self.window.resize(ui.READING_SIZE)
        self.window.show()
        settle()
        self.addCleanup(self.window.hide)


class TranscriptTests(WindowCase):
    def test_entries_are_labelled_and_their_links_are_text_links(self) -> None:
        log = self.reading.conversation
        self.assertIsInstance(log, transcript_view.TranscriptLog)
        clicked: list[str] = []
        log.linkClicked.connect(clicked.append)
        you = log.add(hud.ROLE_YOU, "plan my day", when="9:50 AM")
        jarvis = log.add(hud.ROLE_JARVIS, "Here's a plan for today:\n10:30-12:00 PM  Draft the essay body",
                         when="9:51 AM", sub=f"3 blocks {DOT} 1 planner run {DOT} 9.1 s", link_text="See every step",
                         link_id="live:7")
        self.reading.open_transcript()
        settle()
        first, second = log.entry(you), log.entry(jarvis)
        self.assertIsInstance(second, transcript_view.TranscriptEntry)
        self.assertEqual((first.role_label.text(), first.time_label.isHidden()), ("YOU", True))      # spec 11.4
        self.assertEqual((second.role_label.text(), second.time_label.text()), ("JARVIS", f"{DOT} 9:51 AM"))
        self.assertFalse(second.time_label.isHidden())
        self.assertEqual(second.shown_sub(), "3 blocks")                       # readouts live in LIVE
        self.assertEqual(second.entry[3], f"3 blocks {DOT} 1 planner run {DOT} 9.1 s")
        link = second.link_button
        self.assertIsInstance(link, holo.HoloButton)
        self.assertEqual((link.variant(), link.text()), (holo.LINK_CYAN, "See every step"))
        self.assertTrue(first.link_button.isHidden())
        self.assertEqual(second.text_label.text().split("\n")[1], "10:30-12:00 PM  Draft the essay body")  # in full
        link.click()
        self.assertEqual(clicked, ["live:7"])
        log.update(jarvis, link_text="", link_id="")
        self.assertEqual(second.link(), ("", ""))
        self.assertTrue(second.link_button.isHidden())
        self.assertIn("See every step", second.link_button.accessibleName() or "See every step")

    def test_the_drawer_opens_from_the_stage_and_esc_or_close_give_the_focus_back(self) -> None:
        reading = self.reading
        button = reading.stage.transcript_button
        self.assertEqual(button.accessibleName(), "Transcript")
        self.assertLessEqual(button.width() - 2 * holo.HoloButton.MARGIN, 34)
        self.assertLess(button.x(), reading.stage.width() / 4)                   # bottom-left of the stage
        self.assertGreater(button.y(), reading.stage.height() / 2)
        self.window.activateWindow()
        for close in ("esc", "button"):
            button.setFocus(Qt.FocusReason.TabFocusReason)
            button.click()
            settle()
            drawer = reading.transcript_drawer
            self.assertTrue(drawer.is_open())
            self.assertLessEqual(drawer.width(), 420)
            self.assertEqual(drawer.geometry().right(), drawer.parentWidget().width() - 1)   # from the right
            self.assertTrue(drawer.isAncestorOf(QApplication.focusWidget()))
            if close == "esc":
                QTest.keyClick(drawer.close_button, Qt.Key.Key_Escape)
            else:
                drawer.close_button.click()
            settle()
            self.assertFalse(drawer.isVisible())
            self.assertTrue(button.hasFocus(), close)


class OfferTileTests(unittest.TestCase):
    def test_a_prep_offer_tile_shows_words_never_readouts(self) -> None:
        tile = offer_view.OfferTile()
        self.addCleanup(tile.deleteLater)
        info = offer_view.OfferInfo(3, "prep", "offer:3", f"12:30 PM {DOT} IN 15 MIN", "Project Development check-in",
                                    "Prepare brief", (f"Work {DOT} 3 guests", offer_view.PREP_META),
                                    "Shall I prepare a brief, sir?")
        tile.set_offer(info)
        tile.resize(260, tile.heightForWidth(260))
        kicker, title, meta = tile.texts()
        self.assertEqual(kicker, f"12:30 PM {DOT} IN 15 MIN")
        self.assertEqual(" ".join(title), "Project Development check-in")      # two lines at most
        self.assertEqual(meta, [f"Work {DOT} 3 guests", "Nothing is read until you click."])
        shown = " ".join([kicker, *title, *meta]).upper()
        self.assertFalse([word for word in READOUTS if word in shown])
        buttons = [child for child in tile.findChildren(holo.HoloButton) if not child.isHidden()]
        self.assertEqual(sorted(button.text() for button in buttons), ["Not now", "Prepare brief"])
        self.assertEqual(sum(1 for button in buttons if button.variant() == holo.AMBER_COMMIT), 1)  # one commit
        self.assertGreaterEqual(tile.width(), offer_view.TILE_MIN)
        self.assertTrue(tile.commit_button.geometry().bottom() <= tile.height())
        tile.set_offer(None)
        self.assertTrue(tile.isHidden())


class PlanStageTests(unittest.TestCase):
    def info(self) -> plan_view.PlanInfo:
        blocks = (plan_view.PlanBlock("a1", wall(10, 30), wall(12), "10:30-12:00 PM", "Draft the essay body",
                                      "For: LING essay draft"),
                  plan_view.PlanBlock("a2", wall(14), wall(15), "2:00-3:00 PM", "Read chapter 4"),
                  plan_view.PlanBlock("a3", wall(16, 30), wall(17), "4:30-5:00 PM", "Reply to Ben"))
        events = (plan_view.PlanEvent(wall(12, 30), wall(13), "12:30-1:00 PM", "Project Development check-in"),)
        return plan_view.PlanInfo(f"TODAY {DOT} MON 12 OCT", blocks, events, (("DUE TONIGHT", "LING essay draft"),),
                                  wall(10, 12), "Three blocks for today, the first at 10:30 AM. Each one waits for "
                                                "your OK, sir.", "live:5")

    def make(self) -> plan_view.PlanStage:
        stage = plan_view.PlanStage()
        self.addCleanup(stage.deleteLater)
        stage.resize(900, 640)
        stage.set_plan(self.info())
        stage.show()
        settle()
        return stage

    def test_one_add_block_per_block_and_no_add_all(self) -> None:
        stage = self.make()
        cards = {"a1": fake_card(), "a2": fake_card(), "a3": fake_card()}
        stage.sync_cards(cards.get)
        tiles = stage.tiles()
        self.assertEqual([tile.block.action_id for tile in tiles], ["a1", "a2", "a3"])
        buttons = [button for button in stage.findChildren(holo.HoloButton) if button.isVisibleTo(stage)]
        adds = [button for button in buttons if button.text() == plan_view.ADD_TEXT]
        self.assertEqual(len(adds), 3)                                           # one per block
        self.assertFalse([button for button in buttons if "all" in button.text().casefold()])   # never add all
        self.assertTrue(all(tile.add_button.variant() == holo.AMBER_COMMIT for tile in tiles))
        self.assertEqual(stage.due_strip.due(), (("DUE TONIGHT", "LING essay draft"),))
        self.assertEqual(stage.day_label.text(), f"TODAY {DOT} MON 12 OCT")
        self.assertEqual(stage.caption_text(), "Three blocks for today, the first at 10:30 AM.")   # whole sentences
        # L1: never a page cut mid-sentence; whole sentences up to 14 words, the first one always whole.
        self.assertEqual(plan_view.side_caption("I've lined up two blocks for today, the first at 11 AM. Each one "
                                                "waits for your OK, sir."),
                         "I've lined up two blocks for today, the first at 11 AM.")
        self.assertEqual(plan_view.side_caption("Two blocks for today. Each waits for you."),
                         "Two blocks for today. Each waits for you.")
        self.assertEqual(plan_view.side_caption(""), "")
        texts = " ".join([stage.day_label.text(), stage.caption_text(), stage.caption.accessibleName()]).upper()
        self.assertFalse([word for word in READOUTS if word in texts])          # no technical readout
        timeline = stage.timeline
        first = tiles[0]
        self.assertEqual(first.y(), round(timeline.y_of(wall(10, 30))))       # placed by its time
        self.assertEqual(timeline.height_needed(), 12 * plan_view.HOUR_PX + 2 * plan_view.PAD_Y)   # 8 AM - 8 PM
        clicked: list[str] = []
        stage.addBlock.connect(clicked.append)
        first.add_button.click()
        self.assertEqual(clicked, ["a1"])                                        # one block, one click

    def test_each_block_follows_its_card(self) -> None:
        stage = self.make()
        cards = {"a1": fake_card(hud.CARD_WORKING),
                 "a2": fake_card(hud.CARD_ADDED, result=f"ADDED {DOT} 10:32 AM", link="https://calendar.example/e2",
                                 undo="Undo"),
                 "a3": fake_card(enabled=False)}
        stage.sync_cards(cards.get)
        working, added, locked = stage.tiles()
        self.assertEqual(working.mode(), "working")                       # on its way at once: no countdown
        self.assertTrue(working.add_button.isHidden() and working.undo_button.isHidden())
        self.assertFalse(hasattr(working, "phrase"))
        self.assertEqual((added.mode(), added.result(), added.undo_text()), ("added", f"ADDED {DOT} 10:32 AM", "Undo"))
        self.assertTrue(added.open_button.isVisibleTo(stage) and added.undo_button.isVisibleTo(stage))
        self.assertLess(added.open_button.geometry().right(), added.undo_button.geometry().left() + 8)
        self.assertFalse(locked.add_button.isEnabled())
        undone: list[str] = []
        stage.undoBlock.connect(undone.append)
        added.undo_button.click()
        self.assertEqual(undone, ["a2"])
        stage.sync_cards({"a1": fake_card(hud.CARD_ADDED, result="ADDED")}.get)   # a2, a3: their cards went
        self.assertEqual((working.mode(), working.undo_text()), ("added", ""))      # 10 minutes later: no Undo
        self.assertTrue(working.undo_button.isHidden())
        self.assertTrue(added.isHidden() and locked.isHidden())

    def test_esc_and_the_close_button_ask_to_close(self) -> None:
        stage = self.make()
        asked: list[bool] = []
        stage.closeRequested.connect(lambda: asked.append(True))
        stage.close_button.click()
        QTest.keyClick(stage, Qt.Key.Key_Escape)
        self.assertEqual(asked, [True, True])


class BriefStageTests(unittest.TestCase):
    def test_the_sections_and_the_people(self) -> None:
        who = (("Ana Lima", "organizer, accepted"), ("Ben Ode", "accepted"), ("Dana Park", "no answer yet"))
        threads = (("Ana Lima", '"Budget numbers", Oct 8', "she asked for the Q3 numbers", "owner"),
                   ("Ben Ode", '"Demo date", Oct 9', "you asked for a date", "them"))
        info = brief_view.BriefInfo("Project Development check-in", "today 12:30-1:00 PM", "work",
                                    brief_view.people_of(who, threads, ()),
                                    ('Ana Lima - "Budget numbers", Oct 8: she asked for the Q3 numbers. Waiting on '
                                     'you.',), ("Send Ana the Q3 numbers before the call.",),
                                    ("Settle the demo date with Ben.",), ("Ana organises the weekly check-in.",),
                                    card_ids=("c1",), steps_link="live:9",
                                    nudges=(('Ben Ode owes you an answer in "Demo date" (Oct 9).', "nudge:4"),))
        stage = brief_view.BriefStage()
        self.addCleanup(stage.deleteLater)
        stage.resize(900, 600)
        stage.set_brief(info)
        stage.show()
        settle()
        self.assertEqual(stage.sections(), ["WHO'S COMING", "LAST THREADS", "STILL OPEN", "POINTS TO RAISE",
                                            "FROM YOUR NOTES", "OWES YOU AN ANSWER"])
        rows = stage.people_rows()
        self.assertEqual([(row.person.name, row.person.badge, row.person.tone) for row in rows],
                         [("Ana Lima", "ORGANISER", holo.TONE_CYAN), ("Ben Ode", "ACCEPTED", holo.TONE_CYAN),
                          ("Dana Park", "NO ANSWER YET", holo.TONE_STEEL)])
        self.assertEqual(rows[0].person.line, '"Budget numbers", Oct 8')     # their subject stays in quotes
        self.assertEqual(rows[2].person.line, "")
        self.assertLessEqual(stage.column.width(), brief_view.COLUMN_MAX)
        self.assertFalse([word for word in READOUTS if word in " ".join(stage.texts()).upper()])
        self.assertEqual(stage.card_ids(), ("c1",))
        clicked: list[str] = []
        stage.linkClicked.connect(clicked.append)
        stage.steps_button.click()
        self.assertEqual(clicked, ["live:9"])
        nudge, = [button for button in stage.findChildren(holo.HoloButton) if button.text() == "Draft a nudge"]
        self.assertEqual(nudge.variant(), holo.LINK_CYAN)                       # cyan: it only drafts a card
        nudge.click()
        self.assertEqual(clicked, ["live:9", "nudge:4"])


class AppCase(unittest.TestCase):
    def make(self, start: datetime, *runs, work=None, reader: FakeReader | None = None) -> AppHarness:
        self.reader = reader or FakeReader("work", {})
        app = AppHarness(self, runs=runs, readers={"work": self.reader}, productivity=True, clock=HarnessClock(start))
        app.calendars["work"].briefs = list(work or [])
        app.calendars["personal"].briefs = []
        app.reading(autoplay=False)
        app.c.ask.start()
        self.assertTrue(wait_for(lambda: app.c.ask.ready is not None and app.c.ask.caps is not None))
        self.app, self.c, self.reading = app, app.c, app.c.window.reading
        return app

    def tick(self, minutes: float = 1) -> None:
        self.app.clock.advance(minutes)
        self.c.productivity.tick()
        settle()

    def calendar_writes(self) -> list:
        return [call for calendar in self.app.calendars.values() for call in calendar.calls
                if call[0] in ("create", "move", "cancel", "respond")]


class PlanOfferAppTests(AppCase):
    def test_the_offer_tile_plans_on_its_click_and_each_block_goes_through_its_card(self) -> None:
        app = self.make(at(10, 12), plan_stream({"say": "Chapter 4 this morning.", "question": "", "lines": [BLOCK]}))
        c, reading = self.c, self.reading
        self.assertTrue(c._announce())
        settle()
        tile = reading.offer_tile
        self.assertTrue(tile.isVisible())
        self.assertEqual((tile.info().kicker, tile.info().title), ("PLAN MY DAY", "Blocks around your meetings"))
        self.assertEqual(tile.info().meta, ("Nothing goes on your calendar until you approve each block.",))
        self.assertEqual(reading.state_word(), "NEEDS YOU")
        self.assertEqual(app.factory.runner.started, [])                       # an offer never acts
        tile.commit_button.click()                                             # the owner's click
        self.assertTrue(wait_for(lambda: not c.ask.busy, 10), "the plan ran")
        settle()
        self.assertEqual(len(app.factory.runner.started), 1)
        self.assertFalse(tile.isVisible())                                     # planned: the offer retired
        self.assertEqual(reading.moment(), ui.MOMENT_PLAN)
        stage = reading.plan_stage
        block, = stage.tiles()
        action = next(action for action in c._actions if action.mode == MODE_PLAN and not action.error)
        self.assertEqual(block.block.action_id, action.id)
        self.assertEqual(block.block.title, "Work on: Read chapter 4")
        self.assertTrue(block.block.purpose.startswith("For: "))
        self.assertEqual(block.mode(), "add")
        self.assertEqual(stage.info().label.split(f" {DOT} ")[0], "TODAY")
        self.assertFalse([word for word in READOUTS if word in stage.caption_text().upper()])
        self.assertEqual(self.calendar_writes(), [])
        personal = app.calendars["personal"]
        personal.stamps = True                                                 # Google answers with its version
        block.add_button.click()                                               # through the block's own card
        settle()
        self.assertIsNone(c._send_hold)                                        # no countdown: it goes at once
        self.assertTrue(wait_for(lambda: action.id not in c._jobs, 10))
        settle()
        card = reading.action_card(action.id)
        self.assertEqual((card.status(), card.undo_text()), (hud.CARD_ADDED, "Undo"))
        self.assertEqual((block.mode(), block.undo_text()), ("added", "Undo"))  # the solid cyan event + Undo
        self.assertTrue(block.result().startswith(f"ADDED {DOT} "), block.result())
        self.assertEqual(len(self.calendar_writes()), 1)                      # one block, one add
        c._undos[action.id].shown -= 1.0                                       # past the double-click guard
        block.undo_button.click()                                              # Undo: that event is deleted
        self.assertTrue(wait_for(lambda: action.id not in c._jobs, 10))
        settle()
        self.assertEqual([call[0] for call in personal.calls].count("undo_create"), 1)
        self.assertEqual(block.mode(), "add")                                  # the block waits again
        app.clock.advance(seconds=2)
        block.add_button.click()
        self.assertTrue(wait_for(lambda: action.id not in c._jobs, 10))
        settle()
        self.assertEqual(block.mode(), "added")
        self.assertEqual(len(self.calendar_writes()), 2)                      # added again, once

    def test_not_now_puts_the_offer_away_on_home_only(self) -> None:
        app = self.make(at(10, 12))
        c, reading = self.c, self.reading
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            c._announce()
            settle()
            tile = reading.offer_tile
            offer_id = tile.info().offer_id
            tile.not_now_button.click()
            settle()
        self.assertFalse(tile.isVisible())
        self.assertEqual(reading.offer_question(), "")
        self.assertEqual(app.factory.runner.started, [])
        entry = next(entry for entry in reading.conversation.entry_widgets()
                     if entry.link()[1] == f"{ui.OFFER_LINK}{offer_id}")
        self.assertEqual(entry.link()[0], "Plan my day")                     # the Transcript keeps it
        self.tick(1)
        self.assertFalse(tile.isVisible())                                    # a tick does not bring it back
        c._mark_viewed("2026-10-04 AM")
        reading.set_tab(hud.TAB_BRIEFING)
        reading.set_tab(hud.TAB_JARVIS)
        self.assertEqual([said for said in app.voice.said if said[0] == "notice"], [])   # never said afterwards
        self.assertIn(f"Offer {offer_id}: not now", "\n".join(logs.output))


class PrepAppTests(AppCase):
    def test_the_prep_tile_then_the_brief_with_its_reply_card(self) -> None:
        reader = FakeReader("work", {"thr0000777": sam_thread()}, found_for={GUEST: ["thr0000777"]})
        app = self.make(at(13, 30), plan_stream(ANSWER), work=[meeting()], reader=reader)
        c, reading = self.c, self.reading
        self.tick(15)                                                          # 1:45 PM: 15 minutes before
        tile = reading.offer_tile
        self.assertTrue(tile.isVisible())
        info = tile.info()
        self.assertEqual((info.kicker, info.title, info.commit), (f"2:00 PM {DOT} IN 15 MIN", TITLE, "Prepare brief"))
        self.assertEqual(info.meta, (f"Work {DOT} 1 guest", "Nothing is read until you click."))
        self.assertEqual((reader.searches, reader.reads, app.factory.runner.started), ([], [], []))
        tile.commit_button.click()
        self.assertTrue(wait_for(lambda: not c.ask.busy, 10), "the brief ran")
        settle()
        self.assertEqual(reading.moment(), ui.MOMENT_BRIEF)
        stage = reading.brief_stage
        self.assertEqual(stage.sections()[:2], ["WHO'S COMING", "LAST THREADS"])
        self.assertIn("STILL OPEN", stage.sections())
        self.assertIn("POINTS TO RAISE", stage.sections())
        person, = stage.people_rows()
        self.assertEqual((person.person.name, person.person.badge), ("Sam Example", "ACCEPTED"))
        self.assertTrue(person.person.line.startswith('"Zq7subj Budget numbers"'))
        self.assertFalse([word for word in READOUTS if word in " ".join(stage.texts()).upper()])
        reply = next(action for action in c._actions if action.mode == MODE_PREP and not action.error)
        card = reading.action_card(reply.id)
        self.assertEqual(stage.held(), [card])                                  # the very approval card
        self.assertTrue(stage.isAncestorOf(card))
        self.assertEqual(card.approve_text(), "Send")
        waiting = card.needs_tick()
        if waiting:                                                             # a NEW RECIPIENT: tick first
            self.assertFalse(card.approve_button.isEnabled())
            card.ticks.tick(waiting[0]).click()
            settle()
        self.assertTrue(card.approve_button.isEnabled())
        card.approve_button.click()
        settle()
        self.assertEqual((card.status(), card.undo_text()), (hud.CARD_SENT, "Undo"))   # Sent + Undo, as always
        c._send_hold.started -= 1.0                                            # past the double-click guard
        card.undo_button.click()
        settle()
        self.assertIsNone(c._send_hold)
        self.assertEqual([sender.sent for sender in app.senders.values()], [[] for _ in app.senders])   # nothing
        reading.set_tab(ui.SCREEN_APPROVALS)                                    # back in NEEDS YOUR OK
        settle()
        self.assertEqual(stage.held(), [])
        self.assertEqual(reading.approvals.lent(), [])
        self.assertTrue(reading.approvals.widget().isAncestorOf(card))
        reading.set_tab(ui.SCREEN_HOME)
        settle()
        self.assertEqual(stage.held(), [card])


class BundleOffTests(unittest.TestCase):
    def test_no_offer_tile_and_no_moments_without_the_bundle(self) -> None:
        app = AppHarness(self)
        app.reading(autoplay=False)
        c, reading = app.c, app.c.window.reading
        self.assertIsNone(c.productivity)
        c._announce()
        settle()
        self.assertTrue(reading.offer_tile.isHidden())
        self.assertEqual(reading.moment(), "")
        self.assertIsNone(reading.plan_stage.info())
        self.assertIsNone(reading.brief_stage.info())
        self.assertFalse(c._plan_on())
        self.assertFalse(any(entry.link()[1].startswith(ui.OFFER_LINK)
                             for entry in reading.conversation.entry_widgets()))


if __name__ == "__main__":
    unittest.main()
