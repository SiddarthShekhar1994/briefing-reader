"""The Briefing and Approvals screens of the hologram look (round 2; HOLOGRAM_SPEC 6, 8, 14, 16.1, 16.2).

Briefing: the phases follow the transport (Play / Pause / Replay / Retry), the ready readout, the
reading lens's words lit one by one while it plays (and held while paused), the quiet line (status and
stale warning, never "Playing"), the section track's clicks. Approvals: the screen opens on the first
undecided card, the queue's tiles and count, a tile's click makes its card the focal one, Decided today,
and the queue gives way on a narrow window. Qt offscreen; every text is invented.
"""

from __future__ import annotations

import time
import unittest
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from briefing_reader import approvals_view, briefing_view, config, holo, hud, orb, ui

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_briefing_screen", "-platform", "offscreen"])
    hud.load_fonts(config.PROJECT_ROOT / "fonts")


def settle(rounds: int = 3) -> None:
    for _ in range(rounds):
        QApplication.processEvents()


def rows(current: int = 1, count: int = 4) -> list[hud.SectionRowInfo]:
    names = ["Intro", "Urgent", "Replies", "Calendar", "Deadlines", "Ignore"][:count]
    out = []
    for index, name in enumerate(names):
        state = hud.ROW_PLAYED if index < current else (hud.ROW_CURRENT if index == current else hud.ROW_UPCOMING)
        out.append(hud.SectionRowInfo(name, state=state))
    return out


class HelperTests(unittest.TestCase):
    def test_the_ready_texts(self) -> None:
        self.assertEqual(briefing_view.greeting_for("AM briefing"), "Your AM briefing is ready, sir.")
        self.assertEqual(briefing_view.greeting_for(""), "Your briefing is ready, sir.")
        self.assertEqual(briefing_view.summary_text(6, 4), "6 SECTIONS \u00b7 ABOUT 4 MIN")
        self.assertEqual(briefing_view.summary_text(1, 0), "1 SECTION")
        self.assertEqual(briefing_view.summary_text(0, 3), "")
        self.assertEqual(briefing_view.estimate_minutes(0), 0)
        self.assertEqual(briefing_view.estimate_minutes(151), 2)
        self.assertEqual(briefing_view.lens_label(2, 6, "Urgent"), ("02 / 06", "URGENT"))

    def test_the_quiet_line_leaves_out_what_the_transport_says(self) -> None:
        self.assertEqual(ui._briefing_notice("Playing"), "")
        self.assertEqual(ui._briefing_notice("Paused"), "")
        self.assertEqual(ui._briefing_notice("Playing (offline voice)"), "Offline voice")
        self.assertEqual(ui._briefing_notice("Paused - Your PM briefing is due"), "Your PM briefing is due")
        self.assertEqual(ui._briefing_notice("Preparing audio..."), "Preparing audio...")


class BriefingScreenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = ui.BriefingWindow(10, 30)
        self.addCleanup(self.window.deleteLater)
        self.window.set_clock(lambda: datetime(2026, 10, 12, 10, 6))
        self.window.show_reading_view()
        self.window.resize(1120, 700)
        self.window.show()
        self.reading = self.window.reading
        self.stage = self.reading.briefing_stage
        self.reading.set_header("AM briefing", "Updated 10:04 AM")
        self.reading.set_tab(hud.TAB_BRIEFING)
        settle()
        self.addCleanup(self.window.hide)

    def test_the_phases_follow_the_transport(self) -> None:
        reading, stage = self.reading, self.stage
        reading.set_controls("Play", False, False)
        self.assertEqual(stage.phase(), briefing_view.LOADING)   # no briefing yet
        reading.set_controls("Retry", True, False)
        self.assertEqual(stage.phase(), briefing_view.FAILED)
        reading._has_script = True                               # a briefing is loaded
        for primary, can_skip, phase in (("Play", False, briefing_view.READY),
                                         ("Pause", True, briefing_view.PLAYING),
                                         ("Play", True, briefing_view.PAUSED),
                                         ("Replay", False, briefing_view.ENDED)):
            reading.set_controls(primary, True, can_skip)
            self.assertEqual(stage.phase(), phase, primary)
            self.assertEqual(reading.play_button.accessibleName(), primary)
        self.assertEqual(stage.label(), "END \u00b7 BRIEFING READ")

    def test_the_ready_view_is_cyan_and_its_link_amber(self) -> None:
        reading, stage = self.reading, self.stage
        reading._has_script = True
        reading.set_controls("Play", True, False)
        reading.set_new_briefing(True, "Your AM briefing is ready to view")
        reading.set_activity_state(hud.ORB_STANDBY, False)
        reading.set_pending(2, 3)
        settle()
        self.assertEqual(stage.kicker(), "AM BRIEFING \u00b7 UPDATED 10:04 AM")
        self.assertTrue(stage.is_new())
        self.assertEqual(stage.greeting(), "Your AM briefing is ready, sir.")
        self.assertTrue(stage.approvals_link.isVisible())
        self.assertEqual(stage.approvals_link.text(), "2 proposals need your OK")
        self.assertEqual(stage.approvals_link.variant(), holo.LINK)     # amber: a decision waits
        self.assertEqual(self.reading.hero.look()[0], orb.NEEDS)         # Home's orb: a decision waits
        self.assertEqual(stage.orb.look()[0], orb.IDLE)                 # the compact orb is never amber
        for hidden in (reading.skip_button, reading.everything_button, reading.notion_button, stage.track):
            self.assertFalse(hidden.isVisible())
        stage.approvalsClicked.connect(lambda: None)
        stage.approvals_link.click()
        settle()
        self.assertEqual(reading.current_tab(), ui.SCREEN_APPROVALS)

    def test_the_lens_lights_its_words_while_it_plays(self) -> None:
        reading, stage = self.reading, self.stage
        reading._has_script = True
        reading.set_controls("Pause", True, True)
        reading.set_sections(rows(), "4 sections", 1)
        reading.set_speech("Ana wants the Q3 numbers before the check-in")
        self.assertEqual(stage.word_head(), -1)                         # one colour until it is paced
        reading.pace_line(900)                                          # 9 words over 0.9 s: 150 ms each
        self.assertEqual(stage.word_head(), 0)
        self.assertEqual(stage._word_timer.interval(), 150)
        self.assertEqual(stage.label(), "02 / 04 \u00b7 URGENT")
        stage._next_word()
        stage._next_word()
        self.assertEqual(stage.word_head(), 2)
        reading.set_controls("Play", True, True)                         # paused: the words hold
        self.assertEqual(stage.phase(), briefing_view.PAUSED)
        self.assertFalse(stage._word_timer.isActive())
        self.assertEqual(stage.word_head(), 2)
        reading.set_controls("Pause", True, True)                        # playing again: they go on
        self.assertTrue(stage._word_timer.isActive())
        for _ in range(20):
            stage._next_word()
        self.assertEqual(stage.word_head(), reading.speech.word_count())  # all said
        self.assertFalse(stage._word_timer.isActive())
        reading.set_speech("Next line")                                   # a new line: one colour again
        self.assertEqual(stage.word_head(), -1)
        reading.pace_line(0)                                              # unknown length: the voice's pace
        self.assertEqual(stage._word_timer.interval(), round(1000 / 2.7))
        reading.set_controls("Replay", True, False)
        self.assertEqual(stage.word_head(), -1)

    def test_the_lens_keeps_the_previous_line_of_its_section(self) -> None:
        reading, stage = self.reading, self.stage
        reading._has_script = True
        reading.set_controls("Pause", True, True)
        reading._current = (1, 0)
        reading.set_speech("First line")
        reading.set_speech("Second line")
        self.assertEqual(stage.previous_line(), "First line")
        reading._current = (2, 0)                                         # a new section: none above
        reading.set_speech("Third line")
        self.assertEqual(stage.previous_line(), "")

    def test_a_track_segment_plays_its_section(self) -> None:
        reading, stage = self.reading, self.stage
        reading._has_script = True
        reading.set_controls("Pause", True, True)
        reading.set_sections(rows(), "4 sections", 1)
        settle()
        clicked: list[int] = []
        reading.sectionClicked.connect(clicked.append)
        track = stage.track
        self.assertEqual(len(track.rows()), 4)
        center = track.segment_rect(3).center().toPoint()
        QTest.mouseClick(track, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, center)
        self.assertEqual(clicked, [3])

    def test_the_header_close_says_what_done_says(self) -> None:
        before = self.reading.header.close_button.toolTip()
        self.reading.set_done_tooltip("Done - prep offers stop for today")
        self.assertEqual(self.reading.header.close_button.toolTip(), "Done - prep offers stop for today")
        self.reading.set_done_tooltip("")
        self.assertEqual(self.reading.header.close_button.toolTip(), before)


class ApprovalsScreenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = ui.BriefingWindow(10, 30)
        self.addCleanup(self.window.deleteLater)
        self.window.set_clock(lambda: datetime(2026, 10, 12, 10, 41))
        self.window.show_reading_view()
        self.window.resize(1120, 700)
        self.window.show()
        self.reading = self.window.reading
        self.addCleanup(self.window.hide)
        self.cards = [self.reading.add_action_card(f"a{n}", "Calendar", f"Event {n}", "Tue 13 Oct", True,
                                                   approve_text="Add to calendar") for n in range(3)]
        self.reading.set_pending(3, 3)

    def test_it_opens_on_the_first_undecided_card(self) -> None:
        reading = self.reading
        self.cards[0].set_status(hud.CARD_ADDED, "ADDED")
        reading.set_tab(ui.SCREEN_APPROVALS)
        settle()
        reading.approvals_stage.sync_now()
        self.assertEqual(reading.approvals.focal_id(), "a1")
        queue = reading.approvals_queue
        self.assertEqual([tile.action_id for tile in queue.tiles()], ["a1", "a2"])
        self.assertEqual(queue.selected(), "a1")
        self.assertEqual(queue.count(), 3)
        self.assertEqual(queue.decided_toggle.count(), 1)
        self.assertFalse(queue.decided_open())                          # Decided today starts closed
        queue.toggle_decided()
        settle()
        self.assertTrue(queue.decided_open())
        self.assertTrue(all(row.isVisible() for row in queue.decided_rows()))

    def test_a_queue_tile_makes_its_card_the_focal_one(self) -> None:
        reading = self.reading
        reading.set_tab(ui.SCREEN_APPROVALS)
        settle()
        reading.approvals_stage.sync_now()
        tile = reading.approvals_queue.tile("a2")
        tile.click()
        settle()
        self.assertEqual(reading.approvals.focal_id(), "a2")
        self.assertTrue(self.cards[2].is_focal() and not self.cards[0].is_focal())
        self.assertEqual(reading.approvals_queue.selected(), "a2")

    def test_the_queue_gives_way_on_a_narrow_window(self) -> None:
        reading = self.reading
        reading.set_tab(ui.SCREEN_APPROVALS)
        settle()
        stage = reading.approvals_stage
        self.assertTrue(stage.queue_shown() and reading.approvals_queue.isVisible())
        stage.resize(approvals_view.QUEUE_MIN_STAGE - 1, stage.height())
        settle()
        self.assertFalse(reading.approvals_queue.isVisible())
        self.assertLessEqual(reading.approvals.width(), stage.width())

    def test_many_waiting_cards_scroll_in_the_queue_and_never_overlap(self) -> None:
        reading = self.reading
        self.window.resize(900, 600)
        for n in range(3, 9):
            reading.add_action_card(f"a{n}", "Calendar", f"A longer event title number {n}", "Wed 14 Oct", True,
                                    approve_text="Add to calendar")
        reading.set_tab(ui.SCREEN_APPROVALS)
        settle(6)
        reading.approvals_stage.sync_now()
        settle(6)
        tiles = reading.approvals_queue.tiles()
        self.assertEqual(len(tiles), 9)
        for first, second in zip(tiles, tiles[1:]):
            self.assertLessEqual(first.geometry().bottom(), second.geometry().top(), first.action_id)
        self.assertGreater(reading.approvals_queue.scroll.verticalScrollBar().maximum(), 0)   # it scrolls
        reading.approvals_stage.select("a8")                                                # the last one
        settle()
        tile = reading.approvals_queue.tile("a8")
        view = reading.approvals_queue.scroll.viewport()
        self.assertTrue(view.rect().contains(tile.mapTo(view, tile.rect().center())))       # brought into sight

    def test_a_locked_queue_tile_is_dimmed(self) -> None:
        reading = self.reading
        reading.set_tab(ui.SCREEN_APPROVALS)
        settle()
        self.cards[1].set_locked(True, "Finishing the previous approval...")
        reading.approvals_stage.sync_now()
        self.assertTrue(reading.approvals_queue.tile("a1")._dimmed)
        self.assertFalse(reading.approvals_queue.tile("a0")._dimmed)

    def test_the_confirmed_stamp_and_its_undo_replace_the_action_row(self) -> None:
        card = self.cards[0]
        self.reading.set_tab(ui.SCREEN_APPROVALS)
        settle()
        card.set_status(hud.CARD_ADDED, "Added \u00b7 2:41 PM")   # at once: no strip, no drain bar, no number
        card.set_undo("Undo")
        settle()
        slot = card._result
        self.assertEqual(slot.mode(), slot.STAMP)
        self.assertFalse(hasattr(slot, "drain_fraction"))
        self.assertFalse(card.approve_button.isVisible() or card.deny_button.isVisible())
        self.assertTrue(card.undo_button.isVisible())
        card.set_undo("")                                            # 10 minutes later
        settle()
        self.assertEqual(slot.mode(), slot.STAMP)
        self.assertFalse(card.undo_button.isVisible())
