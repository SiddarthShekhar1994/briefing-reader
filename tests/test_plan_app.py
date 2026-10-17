"""The productivity bundle's day plan in the app (AppController + ASK cards + JARVIS + LIVE).

A typed "plan my day" runs one planner mode run as a PLAN task; its blocks land under NEEDS YOUR OK
as "Plan - block" cards with Add block; a re-plan replaces only the undecided, unedited, idle plan
cards it overlaps (R39); a block whose time has passed is refused at the click; after a block's
result the focus moves to the next plan card only from the finished card or the list, never out of
a text field or a dialog, and a key press right after that move never approves (R1 / R25); results
are spoken without the title (R27); a created block notes the to-do it serves and the next plan
leaves it out (R28); LIVE opened by the run gives way to JARVIS (R33); a question gets its hint
(R46); the bar is cleared only by a typed run (R23). Qt runs offscreen; fakes only; every title is
invented.
"""

from __future__ import annotations

import logging
import threading
import time
import unittest
from datetime import timedelta
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from briefing_reader import hud, live, persona, ui
from briefing_reader.actions import SOURCE_ASK
from briefing_reader.ask.modes import MODE_PLAN, ORIGIN_OFFER, ModeRequest
from tests.ask_fakes import GatedProcess, plan_stream
from tests.ui_fakes import NOW, AppHarness, fonts, settle, wait_for

_app: QApplication | None = None
PLAN_ON = "\n[productivity]\nenabled = true\n"
MARK = "Zq7 Block"
CHAPTER = "Calendar: Work on: Read chapter 4 | 2026-10-04 15:00-16:00 | | | For: t1"
MARKED = f"Calendar: {MARK} | 2026-10-04 16:30-17:30 | | | For: words"
DOT = chr(0xB7)   # the middle dot of the labels and sub lines
SAY = "I've lined up chapter 4 this afternoon."


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_plan_app", "-platform", "offscreen"])
    fonts()


def plan_run(*lines: str, say: str = SAY, question: str = "") -> list[str]:
    return plan_stream({"say": say, "question": question, "lines": list(lines)})


class PlanAppCase(unittest.TestCase):
    def make(self, *runs, config_extra: str = PLAN_ON) -> AppHarness:
        app = AppHarness(self, runs=runs, config_extra=config_extra)
        app.reading(autoplay=False)
        app.c.ask.start()
        self.assertTrue(wait_for(lambda: app.c.ask.ready is not None), "readiness checked")
        self.app, self.c, self.reading = app, app.c, app.c.window.reading
        return app

    def type(self, text: str) -> None:
        bar = self.reading.command_bar
        bar.set_text(text)
        bar.input.returnPressed.emit()
        settle()

    def plan(self, text: str = "plan my day") -> None:
        self.type(text)
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10), "the plan ran")
        settle()

    def plan_cards(self) -> list:
        return [action for action in self.c._actions if action.mode == MODE_PLAN]

    def card(self, action_id: str) -> hud.ActionCard:
        card = self.reading.action_card(action_id)
        assert card is not None
        return card

    def run_countdown(self, action_id: str) -> None:
        """The countdown of ``action_id`` runs out; waits for its result."""
        self.c._countdown.deadline = time.monotonic() - 0.001
        self.c._on_countdown_tick()
        self.assertTrue(wait_for(lambda: action_id not in self.c._jobs, 10), "the block's result came back")
        settle()

    def last_entry(self) -> hud._ConversationEntry:
        return self.reading.conversation.entry_widgets()[-1]


class PlanFlowTests(PlanAppCase):
    def test_a_typed_plan(self) -> None:
        app = self.make(plan_run(CHAPTER, MARKED))
        self.plan("plan my day, Zq7 Block too")
        tasks = [task for task in self.c.live.snapshot() if task.kind == live.TASK_PLAN]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].steps[0].kind, live.MODE_REQUEST)
        self.assertEqual({field.label: field.value for field in tasks[0].steps[0].fields}["You typed"],
                         "plan my day, Zq7 Block too")
        cards = self.plan_cards()
        self.assertEqual([action.title for action in cards], ["Work on: Read chapter 4", MARK])
        self.assertTrue(all(action.source == SOURCE_ASK and not action.error for action in cards))
        labels = [self.card(action.id).kind_label.full_text() for action in cards]
        self.assertEqual(labels, [f"PLAN {DOT} BLOCK"] * 2)
        self.assertEqual(self.card(cards[0].id).approve_text(), "Add block")
        todo = next(action for action in self.c._actions if action.kind == "todo")
        self.assertEqual(cards[0].serves, todo.id)
        text = self.last_entry().entry[2].split("\n")
        self.assertEqual(text[0], "Here's a plan for today:")
        self.assertEqual(text[1:3], ["3:00-4:00 PM  Work on: Read chapter 4", f"4:30-5:30 PM  {MARK}"])
        self.assertEqual(text[-1], persona.PLAN_ANSWER_TAIL)
        self.assertTrue(self.last_entry().entry[3].startswith(f"2 blocks {DOT} 1 planner run {DOT} "))
        self.assertEqual(self.last_entry().link()[0], ui.SEE_STEPS_TEXT)
        spoken = app.voice.texts()[-1]
        self.assertEqual(spoken, "I've lined up two blocks for today, the first at 3 PM. Each one waits for "
                                 "your OK, sir.")
        self.assertFalse(persona.claims_done(spoken))
        self.assertTrue(all(MARK not in said for said in app.voice.texts()))
        self.assertEqual(self.reading.command_bar.text(), "")             # typed: cleared (R23)
        newest = self.reading.activity.entries()[0]
        self.assertEqual((newest[1], newest[2]), (hud.TAG_ASK, "Plan: 2 blocks proposed"))
        stdin = app.factory.runner.started[0].stdin
        self.assertIn("t1 | todo | Read chapter 4 | due 2026-10-06 | from briefing", stdin)
        self.assertIn("The owner's words: Zq7 Block too", stdin)

    def test_no_run_says_why(self) -> None:
        app = self.make()
        self.c._source_lists["briefing"] = [action for action in self.c._source_lists["briefing"]
                                            if action.kind != "todo"]
        self.c._set_actions(self.c._merged_actions())
        self.plan()
        self.assertEqual(app.factory.runner.started, [])
        self.assertEqual(self.last_entry().entry[2], 'Nothing on your list needs time today, sir. Tell me what to '
                                                     'fit in: "plan my day, gym and reading".')
        self.assertEqual(self.last_entry().entry[3], "no planner run")
        self.assertEqual(app.voice.texts()[-1], "Nothing on your list needs time today, sir.")
        newest = self.reading.activity.entries()[0]
        self.assertEqual(newest[2], "Plan: no run (nothing needs time)")   # words, never the reason word
        task = next(task for task in self.c.live.snapshot() if task.kind == live.TASK_PLAN)
        self.assertEqual(task.summary, "no planner run (nothing needs time)")
        self.assertEqual(self.reading.command_bar.text(), "plan my day")   # the draft stays

    def test_a_question_gets_its_hint(self) -> None:
        """R46."""
        app = self.make(plan_run(say="Both need the same hour.", question="Which comes first?"))
        self.plan()
        self.assertEqual(self.last_entry().entry[2].split("\n"),
                         ["Both need the same hour.", "Question: Which comes first?",
                          'Answer with "plan my day, <your answer>".'])
        self.assertEqual(app.voice.texts()[-1], "I have a question before I plan, sir; it's on screen.")
        self.assertEqual(self.plan_cards(), [])

    def test_a_late_plan_says_so_once(self) -> None:
        """R30: from [agenda] evening_from_hour "plan my day" plans tomorrow; the entry starts with the
        late line (once: no second "Here's a plan for tomorrow:") and the speech says tomorrow."""
        app = self.make(plan_run("Calendar: Morning study | 2026-10-05 09:00-10:00 | | | For: t1"))
        app.factory.planner.clock = lambda: NOW.replace(hour=19, minute=0)
        self.plan()
        text = self.last_entry().entry[2].split("\n")
        self.assertEqual(text, ["It's late for today, sir - here's a plan for tomorrow:",
                                "9:00-10:00 AM  Morning study", SAY, persona.PLAN_ANSWER_TAIL])
        self.assertEqual(app.voice.texts()[-1], "I've lined up one block for tomorrow, at 9 AM. It waits for "
                                                "your OK, sir.")

    def test_a_refused_block_reads_refused_everywhere(self) -> None:
        """ACTIVITY, LIVE and the bar's meta line use one word for a refused block."""
        self.make(plan_run(CHAPTER, "Calendar: Clash | 2026-10-04 15:30-16:30 | | | For: t1"))
        self.plan()
        newest = self.reading.activity.entries()[0]
        self.assertEqual(newest[2], "Plan: 1 block proposed (1 refused)")
        meta = self.reading.command_bar.meta()
        self.assertIn("1 REFUSED", meta)
        self.assertNotIn("NOT DOABLE", meta)

    def test_the_planner_say_is_never_spoken(self) -> None:
        app = self.make(plan_run(CHAPTER, say="Zq7say I've added it all."))
        self.plan()
        self.assertIn("Zq7say", self.last_entry().entry[2])
        self.assertTrue(all("Zq7say" not in said for said in app.voice.texts()))


class ReplanTests(PlanAppCase):
    def test_a_replan_replaces_only_idle_unedited_overlapping_cards(self) -> None:
        """R39: the counting-down card and an edited one stay; the others of those hours go."""
        first = ("Calendar: Block A | 2026-10-04 15:00-16:00 | | | For: t1",
                 "Calendar: Block B | 2026-10-04 16:30-17:00 | | | For: t1",
                 "Calendar: Block C | 2026-10-04 18:00-19:00 | | | For: t1")
        self.make(plan_run(*first), plan_run("Calendar: Block D | 2026-10-04 15:00-15:45 | | | For: t1"))
        self.plan()
        a, b, c = self.plan_cards()
        self.card(a.id).approve_button.click()                              # counting down
        settle()
        self.assertIsNotNone(self.c._countdown)
        edited = c.id
        real = self.c._is_edited
        self.c._is_edited = lambda page: page.id == edited or real(page)   # type: ignore[method-assign]
        with self.assertLogs("briefing_reader.ui", level="INFO") as logs:
            self.plan("plan my day, again")
        titles = [action.title for action in self.plan_cards()]
        self.assertEqual(titles, ["Block D", "Block A", "Block C"])         # B went; A counts down; C edited
        self.assertIn("Plan: replaced 1 earlier plan card(s), kept 2", "\n".join(logs.output))

    def test_a_decided_block_stays(self) -> None:
        """R39 "undecided": a block already added (decided, no countdown, no job) is never replaced."""
        first = ("Calendar: Block A | 2026-10-04 15:00-16:00 | | | For: t1",
                 "Calendar: Block B | 2026-10-04 16:30-17:00 | | | For: t1")
        # t1 is served by A once it is added (R28): the re-plan's block serves the owner's words.
        self.make(plan_run(*first), plan_run("Calendar: Block D | 2026-10-04 18:00-18:45 | | | For: words"))
        self.plan()
        a, _b = self.plan_cards()
        self.card(a.id).approve_button.click()
        settle()
        self.run_countdown(a.id)
        self.assertTrue(self.c._store.is_decided(a.id))
        self.assertNotIn(a.id, self.c._jobs)
        with self.assertLogs("briefing_reader.ui", level="INFO") as logs:
            self.plan("plan my day, again")
        self.assertEqual([(action.title, action.error) for action in self.plan_cards()],
                         [("Block D", ""), ("Block A", "")])                 # B went; A was added
        self.assertIn("Plan: replaced 1 earlier plan card(s), kept 1", "\n".join(logs.output))

    def test_a_question_or_every_block_refused_keeps_the_earlier_plan(self) -> None:
        """Only a plan with a block to approve replaces the earlier blocks of its hours."""
        first = ("Calendar: Block A | 2026-10-04 15:00-16:00 | | | For: t1",
                 "Calendar: Block B | 2026-10-04 16:30-17:00 | | | For: t1")
        app = self.make(plan_run(*first), plan_run(say="Both fit.", question="Which comes first?"),
                        plan_run("Calendar: Clash | 2026-10-04 15:00-16:00 | | | For: t9"))
        self.plan()
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            self.plan("plan my day, keep the evening free")
        self.assertEqual([action.title for action in self.plan_cards()], ["Block A", "Block B"])
        self.assertNotIn("Plan: replaced", "\n".join(logs.output))
        self.assertEqual(self.reading.activity.entries()[0][2], "Plan: a question back")
        self.plan("plan my day, again")
        titles = [(action.title, bool(action.error)) for action in self.plan_cards()]
        self.assertEqual(titles, [("Clash", True), ("Block A", False), ("Block B", False)])
        self.assertEqual(self.reading.activity.entries()[0][2], "Plan: no block proposed (1 refused)")
        self.assertEqual(len(app.factory.runner.started), 3)

    def test_morning_then_afternoon_keeps_both(self) -> None:
        morning = plan_run("Calendar: Morning block | 2026-10-04 10:00-11:00 | | | For: t1")
        afternoon = plan_run("Calendar: Afternoon block | 2026-10-04 13:00-14:00 | | | For: t1")
        app = self.make(morning, afternoon)
        app.factory.planner.clock = lambda: NOW.replace(hour=9, minute=0)
        self.plan("plan my morning")
        self.plan("plan my afternoon")
        self.assertEqual(sorted(action.title for action in self.plan_cards()), ["Afternoon block", "Morning block"])


class BlockClickTests(PlanAppCase):
    def two_blocks(self):  # type: ignore[no-untyped-def]
        self.make(plan_run(CHAPTER, MARKED))
        self.plan("plan my day, the Zq7 Block")
        first, second = self.plan_cards()
        self.c.window.activateWindow()
        settle()
        if not self.c.window.isActiveWindow():   # offscreen platforms differ: the rule is what is tested
            self.c.window.isActiveWindow = lambda: True   # type: ignore[method-assign]
        return first, second

    def test_a_block_whose_time_has_passed_is_refused(self) -> None:
        first, _second = self.two_blocks()
        self.c._now = lambda: NOW.replace(hour=15, minute=30)   # type: ignore[method-assign]
        with self.assertLogs("briefing_reader.ui", level="INFO") as logs:
            self.card(first.id).approve_button.click()
            settle()
        self.assertIsNone(self.c._countdown)
        self.assertEqual(self.card(first.id).note(), ui.PLAN_BLOCK_PASSED_NOTE)
        self.assertIn(f"Approve {first.id}: a plan block whose time has passed; nothing added",
                      "\n".join(logs.output))

    def test_the_result_is_spoken_without_the_title_and_the_footer_goes_with_it(self) -> None:
        """R27 and G24."""
        _first, second = self.two_blocks()
        self.card(second.id).approve_button.click()
        settle()
        self.run_countdown(second.id)
        personal = self.app.calendars["personal"]
        self.assertIn(("create", MARK), personal.calls)
        self.assertEqual(personal.footers[-1], "Added by Jarvis from your day plan.")
        entries = [entry[2] for entry in self.reading.conversation.entries()]
        self.assertTrue(any(MARK in text and text.startswith("Done, sir") for text in entries))   # on screen
        said = self.app.voice.texts()[-1]
        self.assertEqual(said, "Done, sir - I've added the event to your calendar for today at 4:30 PM.")
        self.assertTrue(all(MARK not in text for text in self.app.voice.texts()))

    def test_a_served_todo_is_noted_and_left_out_of_the_next_plan(self) -> None:
        """R28."""
        first, _second = self.two_blocks()
        todo = next(action for action in self.c._actions if action.kind == "todo")
        self.app.factory.runner.runs.append(plan_run("Calendar: Gym | 2026-10-04 19:00-20:00 | | | For: words"))
        self.card(first.id).approve_button.click()
        settle()
        self.run_countdown(first.id)
        self.assertEqual(self.card(todo.id).note(), "Planned 3:00-4:00 PM today (day plan)")
        self.assertFalse(self.c._store.is_decided(todo.id))                 # a note, not a decision
        self.plan("plan my day, gym")
        stdin = self.app.factory.runner.started[-1].stdin
        self.assertIn("<todos>\nnone\n</todos>", stdin)


class FocusTests(PlanAppCase):
    def ready(self):  # type: ignore[no-untyped-def]
        self.make(plan_run(CHAPTER, MARKED))
        self.plan("plan my day, the Zq7 Block")
        first, second = self.plan_cards()
        self.c.window.activateWindow()
        settle()
        if not self.c.window.isActiveWindow():
            self.c.window.isActiveWindow = lambda: True   # type: ignore[method-assign]
        return first, second

    def approve_from_its_button(self, action_id: str) -> None:
        button = self.card(action_id).approve_button
        button.setFocus(Qt.FocusReason.TabFocusReason)
        QTest.keyClick(button, Qt.Key.Key_Space)
        settle()
        self.assertIsNotNone(self.c._countdown)

    def test_the_focus_moves_to_the_next_block_from_the_finished_card(self) -> None:
        first, second = self.ready()
        self.reading.set_tab(ui.SCREEN_APPROVALS)       # the cards' screen (the hologram's Approvals)
        settle()
        self.c._plan_focus_guard = ("", 0.0)
        self.approve_from_its_button(first.id)
        self.run_countdown(first.id)
        button = self.card(second.id).approve_button
        self.assertIs(QApplication.focusWidget(), button)
        # A keyboard user sees where the next key lands: the ring shows, and the name says the card.
        self.assertTrue(button._focus_visible)
        self.assertTrue(button.accessibleName().startswith("Add block ("), button.accessibleName())
        self.assertIn(MARK, button.accessibleName())
        self.assertIn(MARK, self.card(second.id).deny_button.accessibleName())
        # A Space right after the move never approves (R1 / R25) ...
        with self.assertLogs("briefing_reader.ui", level="INFO") as logs:
            QTest.keyClick(self.card(second.id).approve_button, Qt.Key.Key_Space)
            settle()
        self.assertIsNone(self.c._countdown)
        self.assertNotIn(second.id, self.c._jobs)
        self.assertIn(f"Approve {second.id}: ignored a key press right after the focus moved", "\n".join(logs.output))
        # ... a click does, and so does a key once the second is over.
        self.c._plan_focus_guard = (second.id, time.monotonic() - 0.1)
        QTest.keyClick(self.card(second.id).approve_button, Qt.Key.Key_Space)
        settle()
        self.assertIsNotNone(self.c._countdown)
        self.assertEqual(self.c._countdown.action.id, second.id)

    def test_typing_in_the_bar_keeps_the_focus_and_enter_approves_nothing(self) -> None:
        first, second = self.ready()
        self.card(first.id).approve_button.click()
        settle()
        bar = self.reading.command_bar
        bar.input.setFocus(Qt.FocusReason.MouseFocusReason)
        settle()
        self.run_countdown(first.id)
        self.assertIs(QApplication.focusWidget(), bar.input)
        QTest.keyClick(bar.input, Qt.Key.Key_Return)
        settle()
        self.assertIsNone(self.c._countdown)
        self.assertNotIn(second.id, self.c._jobs)

    def test_a_mouse_click_moves_the_focus_without_a_ring(self) -> None:
        first, second = self.ready()
        self.reading.set_tab(ui.SCREEN_APPROVALS)       # the cards' screen (the hologram's Approvals)
        settle()
        focused = QApplication.focusWidget()
        if focused is not None:
            focused.clearFocus()
        self.card(first.id).approve_button.click()
        settle()
        self.assertIsNone(QApplication.focusWidget())
        self.run_countdown(first.id)
        button = self.card(second.id).approve_button
        self.assertIs(QApplication.focusWidget(), button)                   # nowhere -> the next block
        self.assertFalse(button._focus_visible)                             # no ring for a mouse user

    def test_a_text_field_elsewhere_keeps_the_focus(self) -> None:
        first, second = self.ready()
        field = self.reading.memory_view.input
        self.reading.set_tab(hud.TAB_MEMORY)
        settle()
        field.setFocus(Qt.FocusReason.MouseFocusReason)
        self.card(first.id).approve_button.click()
        settle()
        field.setFocus(Qt.FocusReason.MouseFocusReason)
        self.run_countdown(first.id)
        self.assertIsNot(QApplication.focusWidget(), self.card(second.id).approve_button)

    def test_an_open_dialog_keeps_the_focus(self) -> None:
        """A modal dialog (a NudgeWordsDialog stand-in) open when the countdown ends: nothing moves,
        even from the finished card's own button (which alone would move it)."""
        first, second = self.ready()
        self.c._plan_focus_guard = ("", 0.0)
        self.approve_from_its_button(first.id)
        dialog = QDialog(self.c.window)
        dialog.setModal(True)
        self.addCleanup(dialog.deleteLater)
        with mock.patch.object(QApplication, "activeModalWidget", return_value=dialog):
            self.run_countdown(first.id)
        self.assertIsNot(QApplication.focusWidget(), self.card(second.id).approve_button)
        self.assertEqual(self.c._plan_focus_guard, ("", 0.0))              # Jarvis moved nothing

    def test_a_button_outside_the_list_keeps_the_focus(self) -> None:
        """Not a text field, but outside NEEDS YOUR OK: the focus stays where the owner put it."""
        first, second = self.ready()
        self.c._plan_focus_guard = ("", 0.0)
        self.card(first.id).approve_button.click()
        settle()
        outside = self.reading.play_button
        outside.setEnabled(True)
        outside.setVisible(True)
        outside.setFocus(Qt.FocusReason.TabFocusReason)
        settle()
        self.assertIs(QApplication.focusWidget(), outside)
        self.assertFalse(self.reading.approvals.isAncestorOf(outside))
        self.run_countdown(first.id)
        self.assertIs(QApplication.focusWidget(), outside)
        self.assertEqual(self.c._plan_focus_guard, ("", 0.0))


class GiveWayTests(PlanAppCase):
    def test_live_opened_by_the_run_gives_way_to_jarvis(self) -> None:
        """R33."""
        gate = threading.Event()
        self.make(GatedProcess(plan_run(CHAPTER), gate))
        self.reading.set_tab(hud.TAB_BRIEFING)         # started while another screen is current: LIVE comes up
        self.type("plan my day")
        self.assertTrue(wait_for(lambda: self.reading.current_tab() == hud.TAB_LIVE))
        gate.set()
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10))
        settle()
        self.assertEqual(self.reading.current_tab(), hud.TAB_JARVIS)

    def test_a_plan_from_home_stays_on_home_with_its_chip(self) -> None:
        """Spec 16.0 / 16.4: typed on Home, Home stays and shows the plan (the timeline); the list is in the
        Transcript too. Closed, the cyan "Plan ready" chip opens the plan again (the chip then goes)."""
        gate = threading.Event()
        self.make(GatedProcess(plan_run(CHAPTER), gate))
        self.type("plan my day")
        self.assertEqual(self.reading.current_tab(), hud.TAB_JARVIS)
        gate.set()
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10))
        settle()
        self.assertEqual(self.reading.current_tab(), hud.TAB_JARVIS)
        reading = self.reading
        reading.captions.clear()                       # the spoken answer's caption has rested
        settle()
        self.assertEqual(reading.moment(), ui.MOMENT_PLAN)
        self.assertFalse(reading.transcript_drawer.is_open())
        self.assertTrue(self.last_entry().entry[2].startswith("Here's a plan for today:"))
        reading.close_moment()
        settle()
        self.assertEqual(reading.home_chip(), ui.HOME_PLAN_CHIP)
        reading.answer_chip.click()
        settle()
        self.assertEqual(reading.moment(), ui.MOMENT_PLAN)
        self.assertFalse(reading.transcript_drawer.is_open())
        self.assertNotEqual(reading.home_chip(), ui.HOME_PLAN_CHIP)

    def test_not_when_live_was_already_open(self) -> None:
        """R33: the owner was on LIVE before he typed; the answer leaves him there."""
        gate = threading.Event()
        self.make(GatedProcess(plan_run(CHAPTER), gate))
        self.reading.set_tab(hud.TAB_LIVE)
        settle()
        self.type("plan my day")
        self.assertEqual(self.reading.current_tab(), hud.TAB_LIVE)
        gate.set()
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10))
        settle()
        self.assertEqual(self.reading.current_tab(), hud.TAB_LIVE)

    def test_not_after_the_owner_changed_tabs(self) -> None:
        gate = threading.Event()
        self.make(GatedProcess(plan_run(CHAPTER), gate))
        self.reading.set_tab(hud.TAB_BRIEFING)
        self.type("plan my day")
        self.assertTrue(wait_for(lambda: self.reading.current_tab() == hud.TAB_LIVE))
        self.reading.set_tab(hud.TAB_BRIEFING)
        self.reading.set_tab(hud.TAB_LIVE)
        gate.set()
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10))
        settle()
        self.assertEqual(self.reading.current_tab(), hud.TAB_LIVE)


class ClickStartedTests(PlanAppCase):
    def test_a_click_started_plan_never_clears_the_bar(self) -> None:
        """R23: an offer-style run keeps what the owner was typing."""
        self.make(plan_run(CHAPTER))
        bar = self.reading.command_bar
        bar.set_text("half-typed request")
        self.assertTrue(self.c.ask.submit_mode(ModeRequest(MODE_PLAN, ORIGIN_OFFER), "Plan my day"))
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10))
        settle()
        self.assertEqual(len(self.plan_cards()), 1)
        self.assertEqual(bar.text(), "half-typed request")

    def test_a_click_while_busy_is_refused_and_nothing_is_queued(self) -> None:
        gate = threading.Event()
        app = self.make(GatedProcess(plan_run(CHAPTER), gate))
        self.type("what is on today?")
        self.assertTrue(self.c.ask.busy)
        self.assertFalse(self.c.ask.submit_mode(ModeRequest(MODE_PLAN, ORIGIN_OFFER), "Plan my day"))
        self.assertIn("click the offer again when it's done", self.reading.command_bar.status())
        gate.set()
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10))
        self.assertEqual(len(app.factory.runner.started), 1)


class PlanOffTests(PlanAppCase):
    def test_with_the_bundle_off_plan_my_day_is_an_ask(self) -> None:
        app = self.make(plan_run(), config_extra="")
        self.plan()
        self.assertEqual(len(app.factory.runner.started), 1)
        self.assertIn("<accounts>", app.factory.runner.started[0].stdin)   # an ordinary Ask
        self.assertEqual([task.kind for task in self.c.live.snapshot() if task.kind == live.TASK_PLAN], [])

    def test_logs_and_activity_never_hold_a_title_or_the_words(self) -> None:
        """Spec 14: sentinel titles and typed words appear in no log record and no ACTIVITY row."""
        self.make(plan_run(CHAPTER, MARKED, "Calendar: Zq7 Refused | 2026-10-04 15:30-16:00 | | | For: t1"))
        with self.assertLogs("briefing_reader", level="DEBUG") as logs:
            self.plan("plan my day, zq7req words")
        text = "\n".join(logs.output)
        rows = "\n".join(" ".join(row) for row in self.reading.activity.entries())
        self.assertIn("Plan: 2 blocks proposed (1 refused)", rows)
        for secret in ("Zq7", "zq7", "Read chapter 4", "Jarvis test sync", "Club general meeting", SAY[:20]):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, rows)


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
