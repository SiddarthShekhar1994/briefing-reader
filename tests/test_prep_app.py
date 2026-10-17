"""The productivity bundle's meeting brief in the app (AppController + ASK cards + JARVIS + LIVE).

A typed "prep me for ..." runs one planner mode run as a PREP task: the brief in JARVIS (never
spoken, never logged), its Reply card under NEEDS YOUR OK as "Prep - reply", and a spoken sentence
with the meeting's time only ("... is ready" with JARVIS current, "... is in the JARVIS tab" when
the owner changed tabs; LIVE opened by the run gives way to JARVIS, R33); an approved prep reply's
result is said without its subject (R27); several meetings give choices with "Prep this one"; no
such meeting answers with no read at all. Qt runs offscreen; fakes only; every name is invented.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from PySide6.QtWidgets import QApplication

from briefing_reader import hud, live, persona, ui
from briefing_reader.actions import REPLY, SOURCE_ASK
from briefing_reader.ask.modes import MODE_PREP
from briefing_reader.gcal import Guest
from tests.ask_fakes import FakeReader, GatedProcess, gmail_message, gmail_thread, plan_stream
from tests.ui_fakes import GUEST, NOW, AppHarness, event, fonts, settle, wait_for

_app: QApplication | None = None
PREP_ON = "\n[productivity]\nenabled = true\n"
DOT = chr(0xB7)
SUBJECT = "Zq7subj Budget numbers"
REPLY_LINE = ("Reply: acct=work | thread=thr0000777 | msgid=<CAB7@mail.example.com> | gmid= | to=" + GUEST + " | cc= | "
              "subject=Re: " + SUBJECT + " | replied=no | due= | link= | body=Hi Sam,\\nI'll send them today.\\nThanks")
ANSWER = {"say": "Sam waits on the numbers.",
          "threads": [{"thread": "thr0000777", "summary": "Zq7brief Sam asked for the Q3 numbers.",
                       "waiting_on": "owner"}],
          "open": [{"text": "Send Sam the Q3 numbers.", "thread": "thr0000777"}],
          "points": ["Agree the demo date."], "lines": [REPLY_LINE]}
TYPED = "prep me for Jarvis test sync"


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_prep_app", "-platform", "offscreen"])
    fonts()


def sam_thread() -> dict:
    return gmail_thread("thr0000777", [gmail_message(
        "msg0000777", sender=f"Sam Example <{GUEST}>", to="me@example.edu", subject=SUBJECT,
        body="Could you send me the Q3 numbers?", msgid="<CAB7@mail.example.com>", at_ms=1791100000000)])


class PrepAppCase(unittest.TestCase):
    def make(self, *runs, found: bool = True) -> AppHarness:
        self.reader = FakeReader("work", {"thr0000777": sam_thread()},
                                 found_for={GUEST: ["thr0000777"]} if found else None)
        app = AppHarness(self, runs=runs, readers={"work": self.reader}, config_extra=PREP_ON)
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

    def prep(self, text: str = TYPED) -> None:
        self.type(text)
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10), "the brief ran")
        settle()

    def prep_cards(self) -> list:
        return [action for action in self.c._actions if action.mode == MODE_PREP]

    def entries(self) -> list[hud._ConversationEntry]:
        return self.reading.conversation.entry_widgets()


class PrepFlowTests(PrepAppCase):
    def test_a_typed_brief(self) -> None:
        app = self.make(plan_stream(ANSWER))
        with self.assertLogs("briefing_reader", level="DEBUG") as logs:
            self.prep()
        self.assertEqual(self.reader.reads, ["thr0000777"])
        text = self.entries()[-1].entry[2].split("\n")
        self.assertEqual(text[0], "Brief: Jarvis test sync - tomorrow 2:00-3:00 PM (work)")
        self.assertEqual(text[1], "Who: Sam Example (accepted)")
        self.assertEqual(text[2:4], ["Last threads:", '- Sam Example - "Zq7subj Budget numbers", Oct 4: Zq7brief '
                                                      'Sam asked for the Q3 numbers. Waiting on you.'])
        self.assertIn("Still open:", text)
        self.assertTrue(self.entries()[-1].entry[3].startswith(f"prep brief {DOT} 1 thread read {DOT} 1 planner run"))
        self.assertEqual(self.entries()[-1].link()[0], ui.SEE_STEPS_TEXT)
        card, = self.prep_cards()
        self.assertEqual((card.kind, card.source, card.error), (REPLY, SOURCE_ASK, ""))
        widget = self.reading.action_card(card.id)
        self.assertEqual(widget.kind_label.full_text(), f"PREP {DOT} REPLY {DOT} WORK")
        self.assertEqual(widget.approve_text(), "Send")
        spoken = app.voice.texts()
        self.assertEqual(spoken[-1], "Your brief for tomorrow's 2 PM meeting is ready, sir.")
        self.assertFalse(persona.claims_done(spoken[-1]))
        for said in spoken:
            for secret in ("Zq7", "Sam", "Budget", "Jarvis test sync"):
                self.assertNotIn(secret, said)
        self.assertEqual(self.reading.command_bar.text(), "")              # typed: cleared (R23)
        self.assertEqual(self.reading.current_tab(), hud.TAB_JARVIS)        # LIVE gave way (R33)
        newest = self.reading.activity.entries()[0]
        self.assertEqual((newest[1], newest[2]), (hud.TAG_ASK, "Prep: brief ready (1 thread read), 1 reply"))
        task = next(task for task in self.c.live.snapshot() if task.kind == live.TASK_PREP)
        kinds = [step.kind for step in task.steps]
        self.assertEqual(kinds[0], live.MODE_REQUEST)
        self.assertIn(live.PREP_MEETING, kinds)
        self.assertIn(live.PREP_VALIDATE, kinds)
        self.assertEqual(task.summary, "brief ready (1 thread read), 1 reply, 1 recipient you didn't type")
        stdin = app.factory.runner.started[0].stdin
        self.assertNotIn(TYPED, stdin)                                      # the words only picked the meeting
        record = "\n".join(logs.output)
        for secret in ("Zq7", "Sam", GUEST, "Jarvis test sync", "Q3", "thr0000777"):
            self.assertNotIn(secret, record)
        for row in self.reading.activity.entries():
            self.assertNotIn("Zq7", " ".join(row))

    def test_the_brief_says_where_it_is_when_another_tab_is_current(self) -> None:
        """R33: LIVE opened by the run gives way to JARVIS, but not after the owner changed tabs; the
        sentence then says the brief is in the JARVIS tab."""
        gate = threading.Event()
        app = self.make(GatedProcess(plan_stream(ANSWER), gate))
        self.reading.set_tab(hud.TAB_MEMORY)            # started while another screen is current
        self.type(TYPED)
        self.assertTrue(wait_for(lambda: self.reading.current_tab() == hud.TAB_LIVE))
        self.reading.set_tab(hud.TAB_BRIEFING)
        gate.set()
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10))
        settle()
        self.assertEqual(self.reading.current_tab(), hud.TAB_BRIEFING)
        self.assertEqual(app.voice.texts()[-1], "Your brief for tomorrow's 2 PM meeting is in the Transcript, sir.")

    def test_the_brief_says_ready_when_the_window_is_minimized(self) -> None:
        """R33: minimized, the owner is not looking at another tab: "... is ready" even after he
        switched to BRIEFING before minimizing."""
        gate = threading.Event()
        app = self.make(GatedProcess(plan_stream(ANSWER), gate))
        self.reading.set_tab(hud.TAB_MEMORY)
        self.type(TYPED)
        self.assertTrue(wait_for(lambda: self.reading.current_tab() == hud.TAB_LIVE))
        self.reading.set_tab(hud.TAB_BRIEFING)
        with mock.patch.object(self.c.window, "isMinimized", return_value=True):
            gate.set()
            self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10))
            settle()
        self.assertEqual(self.reading.current_tab(), hud.TAB_BRIEFING)
        self.assertEqual(app.voice.texts()[-1], "Your brief for tomorrow's 2 PM meeting is ready, sir.")

    def test_live_auto_opened_by_the_run_gives_way(self) -> None:
        gate = threading.Event()
        app = self.make(GatedProcess(plan_stream(ANSWER), gate))
        self.reading.set_tab(hud.TAB_MEMORY)
        self.type(TYPED)
        self.assertTrue(wait_for(lambda: self.reading.current_tab() == hud.TAB_LIVE))
        gate.set()
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10))
        settle()
        self.assertEqual(self.reading.current_tab(), hud.TAB_JARVIS)
        self.assertEqual(app.voice.texts()[-1], "Your brief for tomorrow's 2 PM meeting is ready, sir.")

    def test_a_brief_typed_on_home_stays_on_home_with_its_chip(self) -> None:
        """Spec 16.0 / 16.5: Home stays and shows the brief (the drawer stays shut: never opened by itself);
        the brief is in the Transcript too. Closed, the cyan "Brief ready" chip opens the brief again."""
        self.make(plan_stream(ANSWER))
        self.prep()
        reading = self.reading
        self.assertEqual(reading.current_tab(), hud.TAB_JARVIS)
        self.assertFalse(reading.transcript_drawer.is_open())
        self.assertEqual(reading.moment(), ui.MOMENT_BRIEF)
        self.assertTrue(self.entries()[-1].entry[2].startswith("Brief: Jarvis test sync"))
        reading.close_moment()
        reading.captions.clear()
        settle()
        self.assertEqual(reading.home_chip(), ui.HOME_BRIEF_CHIP)
        reading.answer_chip.click()
        settle()
        self.assertEqual(reading.moment(), ui.MOMENT_BRIEF)
        self.assertFalse(reading.transcript_drawer.is_open())
        self.assertEqual(reading.current_tab(), hud.TAB_JARVIS)
        self.assertNotEqual(reading.home_chip(), ui.HOME_BRIEF_CHIP)

    def test_an_approved_prep_reply_is_said_without_its_subject(self) -> None:
        """R27: the entry keeps the subject on screen; the spoken result has none."""
        app = self.make(plan_stream(ANSWER))
        self.prep()
        card, = self.prep_cards()
        self.c._history.add([GUEST])                                        # Jarvis wrote to Sam before
        self.c._refresh_actions_ui()
        settle()
        self.reading.action_card(card.id).approve_button.click()
        settle()
        self.assertIsNotNone(self.c._countdown)
        self.c._countdown.deadline = time.monotonic() - 0.001
        self.c._on_countdown_tick()
        self.assertTrue(wait_for(lambda: card.id not in self.c._jobs and app.senders["work"].sent, 10))
        settle()
        self.assertEqual(app.voice.texts()[-1], "Sent, sir - your reply is on its way.")
        self.assertIn("Zq7subj", self.entries()[-1].entry[2])                # on screen only
        self.assertTrue(all("Zq7subj" not in said for said in app.voice.texts()))

    def test_no_thread_gives_the_brief_without_a_run(self) -> None:
        app = self.make(found=False)
        self.prep()
        self.assertEqual(app.factory.runner.started, [])
        text = self.entries()[-1].entry[2]
        self.assertIn("- Sam Example - no email in the last 14 days.", text)
        self.assertEqual(self.entries()[-1].entry[3], f"prep brief {DOT} no planner run")
        self.assertEqual(app.voice.texts()[-1], "Your brief for tomorrow's 2 PM meeting is ready, sir.")
        self.assertEqual(self.reading.activity.entries()[0][2], "Prep: brief without a planner run")

    def test_no_such_meeting_reads_nothing(self) -> None:
        app = self.make()
        self.prep("prep me for the Zebra review")
        self.assertEqual((self.reader.searches, app.factory.runner.started), ([], []))
        self.assertEqual(self.entries()[-1].entry[2], "I don't see a meeting like that today or tomorrow, sir.")
        self.assertEqual(app.voice.texts()[-1], "I don't see a meeting like that today or tomorrow, sir.")
        self.assertEqual(self.reading.command_bar.text(), "prep me for the Zebra review")   # kept to edit
        self.assertEqual(self.reading.activity.entries()[0][2], "Prep: no run (no such meeting)")

    def test_a_failed_run_keeps_the_words(self) -> None:
        """A run whose answer can't be read: the failure entry (red), one STOP row of counts, no card,
        nothing spoken about the meeting, and the typed words stay in the bar to edit (R23)."""
        app = self.make(plan_stream({"say": "Sam waits.", "threads": "not a list", "open": [], "points": [],
                                     "lines": []}))
        self.prep()
        self.assertEqual((self.reader.reads, len(app.factory.runner.started)), (["thr0000777"], 1))
        last = self.entries()[-1]
        self.assertEqual((last.entry[2], last.tone()),
                         ("The planner's answer could not be read; nothing was proposed", hud.TONE_ERROR))
        self.assertEqual(self.prep_cards(), [])
        newest = self.reading.activity.entries()[0]
        self.assertEqual((newest[1], newest[2]), (hud.TAG_STOP, "Prep: nothing was proposed"))
        self.assertEqual(self.reading.command_bar.text(), TYPED)              # kept to edit
        for said in app.voice.texts():
            for secret in ("Zq7", "Sam", "Jarvis test sync"):
                self.assertNotIn(secret, said)


class PrepChoiceTests(PrepAppCase):
    def test_choices_then_prep_this_one(self) -> None:
        app = self.make(plan_stream(ANSWER))
        personal = app.calendars["personal"]
        personal.events["jts0002bb"] = event("jts0002bb", "Jarvis test sync", NOW.replace(hour=16, minute=0))
        personal.guests["jts0002bb"] = (Guest("lee@example.com", "Lee Example", "accepted"),)
        self.prep()
        self.assertEqual((self.reader.searches, app.factory.runner.started), ([], []))
        lead, today, tomorrow = self.entries()[-3:]
        self.assertEqual(lead.entry[2], "Which meeting shall I prepare, sir? Nothing is read until you pick one.")
        self.assertEqual(today.entry[2], "Jarvis test sync at 4:00 PM (personal)")
        self.assertEqual(tomorrow.entry[2], "Jarvis test sync at 2:00 PM (work), tomorrow")
        self.assertEqual([today.link()[0], tomorrow.link()[0]], [ui.PREP_THIS_ONE_TEXT] * 2)
        # Each link says which meeting it acts on (a screen reader hears more than "Prep this one").
        self.assertEqual(today.link_button.accessibleName(), "Prep this one: Jarvis test sync at 4:00 PM (personal)")
        self.assertEqual(tomorrow.link_button.accessibleName(),
                         "Prep this one: Jarvis test sync at 2:00 PM (work), tomorrow")
        self.assertEqual(app.voice.texts()[-1], "Which meeting, sir? The choices are on screen.")
        # The conversation shows the group from its question (never a bare link at the top).
        conversation = self.reading.conversation
        viewport = conversation.viewport()
        self.assertGreaterEqual(lead.mapTo(viewport, lead.rect().topLeft()).y(), 0)
        self.reading.command_bar.set_text("half typed")
        tomorrow.link_button.click()
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10))
        settle()
        self.assertEqual(self.reader.reads, ["thr0000777"])
        self.assertTrue(self.entries()[-1].entry[2].startswith("Brief: Jarvis test sync - tomorrow 2:00-3:00 PM"))
        self.assertEqual((today.link()[1], tomorrow.link()[1]), ("", ""))   # one click, one brief
        self.assertEqual((today.link_button.accessibleName(), tomorrow.link_button.accessibleName()), ("", ""))
        self.assertEqual(self.reading.command_bar.text(), "half typed")      # never what he typed since (R23)
        you = [entry.entry[2] for entry in self.entries() if entry.entry[0] == hud.ROLE_YOU]
        self.assertEqual(you[-1], "Prep this one: Jarvis test sync at 2:00 PM (work), tomorrow")

    def test_the_click_clears_the_typed_words_still_in_the_bar(self) -> None:
        """R23: the words that led to the choices are done with once the brief is in, but only while
        the bar still holds exactly them (anything the owner typed since stays)."""
        app = self.make(plan_stream(ANSWER))
        personal = app.calendars["personal"]
        personal.events["jts0002bb"] = event("jts0002bb", "Jarvis test sync", NOW.replace(hour=16, minute=0))
        personal.guests["jts0002bb"] = (Guest("lee@example.com", "Lee Example", "accepted"),)
        self.prep()
        self.assertEqual(self.reading.command_bar.text(), TYPED)              # kept with the choices
        tomorrow = self.entries()[-1]
        tomorrow.link_button.click()
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10))
        settle()
        self.assertTrue(self.entries()[-1].entry[2].startswith("Brief: Jarvis test sync - tomorrow"))
        self.assertEqual(self.reading.command_bar.text(), "")

    def test_a_choice_click_while_busy_keeps_the_link(self) -> None:
        """4.1: a click while another request runs is refused (nothing queued); the link stays, and
        the refusal stays on the status line through the running request's 1 s ticks."""
        from briefing_reader.ask_ui import CHOICE_BUSY_TEXT, CLICK_BUSY_TEXT

        gate = threading.Event()
        app = self.make(GatedProcess(plan_stream({"say": "Nothing today.", "question": "", "lines": []}), gate))
        personal = app.calendars["personal"]
        personal.events["jts0002bb"] = event("jts0002bb", "Jarvis test sync", NOW.replace(hour=16, minute=0))
        personal.guests["jts0002bb"] = (Guest("lee@example.com", "Lee Example", "accepted"),)
        self.prep()
        today, tomorrow = self.entries()[-2:]
        self.type("what's on today?")                                        # an Ask, held at its run
        self.assertTrue(wait_for(lambda: self.c.ask.busy))
        tomorrow.link_button.click()
        settle()
        status = self.reading.command_bar.status
        self.assertIn(CHOICE_BUSY_TEXT, status())
        self.assertIn("click Prep this one again", CHOICE_BUSY_TEXT)         # a choice link, not an offer
        self.assertNotEqual(CHOICE_BUSY_TEXT, CLICK_BUSY_TEXT)
        self.assertEqual((today.link()[0], tomorrow.link()[0]), (ui.PREP_THIS_ONE_TEXT, ui.PREP_THIS_ONE_TEXT))
        self.c.ask._show_stage()                                              # what the 1 s tick does
        self.assertIn(CHOICE_BUSY_TEXT, status())
        wait_for(lambda: False, 1.2)                                          # a real tick
        self.assertIn(CHOICE_BUSY_TEXT, status())
        self.c.ask._notice_until = 0.0                                        # a few seconds later
        self.c.ask._show_stage()
        self.assertNotIn(CHOICE_BUSY_TEXT, status())
        self.assertIn("Planning", status())                                   # the Ask's stage is back
        gate.set()
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10))
        self.assertEqual(self.reader.reads, [])
        self.assertEqual(len(app.factory.runner.started), 1)

    def test_old_choice_groups_lose_their_links(self) -> None:
        """Only the newest 30 links are kept (memory only); a group that goes loses its links on
        screen too, so no dead "Prep this one" stays."""
        from briefing_reader.ask.modes import EventRef
        from briefing_reader.ask.prep import PrepChoice

        self.make()
        start = NOW.replace(hour=14, minute=0, tzinfo=None)
        groups: list[list[hud._ConversationEntry]] = []
        for number in range(16):
            choices = tuple(PrepChoice(EventRef("work", "primary", f"evt{number}{letter}", start),
                                       f"Sync {number}{letter}", start, start, "today") for letter in "ab")
            self.c._prep_choice_entries(SimpleNamespace(message="", choices=choices))
            groups.append(self.entries()[-2:])
        settle()
        self.assertEqual(len(self.c._prep_choices), 30)
        self.assertEqual([entry.link() for entry in groups[0]], [("", ""), ("", "")])
        self.assertEqual([entry.link()[0] for entry in groups[1]], [ui.PREP_THIS_ONE_TEXT] * 2)
        self.assertEqual([entry.link()[0] for entry in groups[-1]], [ui.PREP_THIS_ONE_TEXT] * 2)
        self.assertEqual(sorted(self.c._prep_choices), list(range(3, 33)))


class PrepRouteGatingTests(PrepAppCase):
    def test_off_without_the_bundle(self) -> None:
        app = AppHarness(self, runs=(), readers={})
        app.reading(autoplay=False)
        self.assertFalse(app.c._route_flags().prep)

    def test_on_with_the_bundle_and_the_calendar(self) -> None:
        self.make()
        flags = self.c._route_flags()
        self.assertTrue(flags.prep and flags.plan)

    def test_off_without_the_calendar(self) -> None:
        """Spec 2: "prep me" needs [calendar] enabled; "plan my day" does not."""
        app = self.make(plan_stream({"say": "Nothing today.", "question": "", "lines": []}))
        self.c.config = dataclasses.replace(self.c.config, calendar=dataclasses.replace(self.c.config.calendar,
                                                                                        enabled=False))
        flags = self.c._route_flags()
        self.assertFalse(flags.prep)
        self.assertTrue(flags.plan)
        self.type("prep me for Jarvis test sync")                            # an ordinary Ask now
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10))
        self.assertEqual((self.reader.searches, len(app.factory.runner.started)), ([], 1))
        self.assertNotIn("Prep this one", [entry.link()[0] for entry in self.entries()])
        self.assertEqual(self.entries()[-1].entry[2], "Nothing today.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
