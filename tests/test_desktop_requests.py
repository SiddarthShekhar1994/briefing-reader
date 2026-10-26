"""Jarvis on your desktop, P1.7, in the app: a typed desktop request from end to end (SPEC 3; hard rules 1, 2, 9, 13
and 14) and the command bar's rule (R23) along the whole desktop path. ROUTE_DESKTOP only while [desktop] is on; from
the window's bar Jarvis goes onto the HUD and the request continues there; on the HUD's bar a local route is answered
on this PC (no planner run) and anything else is ONE desktop planner run whose answer is Jarvis's own words in the
HUD's caption, nothing opened yet; everything else stays a normal Ask. The bar a request was typed in lets its words
go as soon as it is taken (Up brings them back), a request refused before any run gets its words back only into an
empty bar, an outcome never clears the bar, and with Ask off a local route is still answered on this PC. A desktop
run gets neither the owner's notes nor the conversation (owner decision D6: names only) and never a YOU entry.

Qt runs offscreen in this process and shows nothing. No real hotkey, pipe, foreground change or OS call: the HUD's
native seam is a recording fake, the desktop's candidates are invented (the P1.8 plug, context_source, is filled by
the test) and claude.exe is never started (tests.ask_fakes).
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from briefing_reader import ask_ui, hud, live, ui
from briefing_reader.ask import cli
from briefing_reader.ask import planner as ask_planner
from briefing_reader.ask.modes import MODE_DESKTOP
from briefing_reader.ask.planner import DISABLED_MESSAGE
from briefing_reader.desktop import validate as dv
from briefing_reader.desktop.context import AppItem, DesktopContext, FileItem, Place, SiteItem
from tests import test_desktop_switch as p15
from tests.ask_fakes import GatedProcess, plan_stream
from tests.desktop_fakes import desktop_context
from tests.test_desktop_keys import KeyApp
from tests.ui_fakes import fonts, plan, settle, wait_for

ESSAY = "open my LING essay"
DESKTOP_PLAN = {"say": "I have lined up your LING essay draft.",
                "steps": [{"skill": "open_file", "why": "the essay draft", "files": ["f1"]}]}
UNDERSTOOD = "Understood: open a file. Nothing was opened yet, sir."
DOWNLOADS = "open Downloads"
DOWNLOADS_WORDS = "Understood: open your Downloads folder. Nothing was opened yet, sir."
ON_DESKTOP = "On your desktop, sir."
MEMORY_ON = "\n[productivity]\nenabled = true\n"
NAMES = ("LING", "essay", "Spotify", "Microsoft Word", "PowerShell", "Student portal", "lined up")
_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_desktop_requests", "-platform", "offscreen"])
    fonts()


class DesktopRequestApp(KeyApp):
    """KeyApp (the instant switch, the HUD over a recording seam) with Ask ready and the desktop's candidates set
    (``context_source``: the invented tests.desktop_fakes context, as P1.8's builder will hand it)."""

    def make_ask(self, *runs: object, extra: str = "", **harness: object) -> None:
        self.make(extra, runs=runs, **harness)
        self.desktop.context_source = lambda _words: desktop_context()
        self.c.ask.start()
        self.assertTrue(wait_for(lambda: self.c.ask.ready is not None), "readiness checked")
        settle()

    def type_in_window(self, text: str) -> None:
        bar = self.reading.command_bar
        bar.set_text(text)
        bar.submit()                                          # what Enter in the field does
        settle()

    def type_on_hud(self, text: str) -> None:
        bar = self.hud().bar
        QTest.mouseClick(bar.input, Qt.MouseButton.LeftButton)
        QTest.keyClicks(bar.input, text)
        QTest.keyClick(bar.input, Qt.Key.Key_Return)
        settle()

    def finish(self) -> None:
        self.assertTrue(wait_for(lambda: not self.c.ask.busy), "the request ended")
        settle()

    def runs(self) -> list:
        return self.harness.factory.runner.started

    def prompt_file(self, number: int = 0) -> str:
        argv = self.runs()[number].argv
        return argv[argv.index("--system-prompt-file") + 1]

    def desktop_tasks(self) -> list:
        return [task for task in self.c.live.snapshot() if task.kind == live.TASK_DESKTOP]

    def said(self) -> str:
        return "\n".join(text for _who, _when, text, _sub in self.reading.conversation.entries())

    def up(self, bar: hud.CommandBar) -> str:
        """Up in the empty field: the last request taken from it comes back (then Down clears it again)."""
        QTest.keyClick(bar.input, Qt.Key.Key_Up)
        text = bar.text()
        QTest.keyClick(bar.input, Qt.Key.Key_Down)
        return text


class RoutingTests(DesktopRequestApp):
    def test_desktop_off_it_is_a_normal_ask(self) -> None:
        self.make(enabled=False, runs=(plan(say="Here is what I found.", lines=()),))
        self.c.ask.start()
        self.assertTrue(wait_for(lambda: self.c.ask.ready is not None))
        self.assertIsNone(self.c.desktop)
        self.type_in_window(ESSAY)
        self.finish()
        self.assertEqual(self.prompt_file(), str(cli.PROMPT_FILE))           # Ask's own planner run
        self.assertTrue(self.window.isVisible())                             # no HUD, nothing switched
        self.assertIn(ESSAY, self.said())                                    # a normal Ask's YOU entry
        self.assertEqual(self.desktop_tasks(), [])

    def test_everything_else_typed_in_the_window_stays_a_normal_ask(self) -> None:
        self.make_ask(plan(say="Nothing much today.", lines=()))
        self.type_in_window("what's on today?")
        self.finish()
        self.assertFalse(self.desktop.is_hud())
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.prompt_file(), str(cli.PROMPT_FILE))

    def test_from_the_windows_bar_onto_the_hud_and_one_planner_run(self) -> None:
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.make_ask(GatedProcess(plan_stream(DESKTOP_PLAN), gate))
        ask, window_bar, hud_bar = self.c.ask, self.reading.command_bar, self.hud().bar
        with mock.patch.object(ui, "force_foreground") as forced:
            self.type_in_window(ESSAY)
        forced.assert_not_called()
        self.assert_on_hud()                                                  # the window hidden, never closed
        self.assertEqual(self.closes, [])
        self.assertIs(ask.bar, hud_bar)                                       # the request continues on the HUD
        self.assertTrue(ask.busy and hud_bar.is_running())
        self.assertEqual(ask.running_mode, MODE_DESKTOP)
        self.assertEqual((window_bar.text(), hud_bar.text()), ("", ""))      # taken from both at once (R23)
        self.assertEqual((window_bar.last_request(), hud_bar.last_request()), (ESSAY, ESSAY))
        self.assertTrue(wait_for(lambda: ask.stage() == ask_planner.STAGE_PLANNING))
        settle()
        self.assertEqual(self.hud().caption_text(), "Planning...")          # what Jarvis is doing, no seconds
        gate.set()
        self.finish()
        self.assertEqual(self.hud().caption_text(), UNDERSTOOD)             # Jarvis's own words, nothing opened
        self.assertFalse(hud_bar.is_running())
        self.assertEqual(len(self.runs()), 1)                                # ONE planner run
        self.assertEqual(self.prompt_file(), str(cli.DESKTOP_PROMPT_FILE))
        stdin = self.runs()[0].stdin
        self.assertIn('f1 "LING essay draft.docx" d3', stdin)               # names only
        self.assertIn("The owner's words: " + ESSAY, stdin)
        plan_kept = self.desktop.last_plan                                   # kept for P1.8 to carry out
        self.assertIsInstance(plan_kept, dv.DesktopPlan)
        self.assertEqual([step.code or step.tier for step in plan_kept.steps], [dv.INSTANT])
        self.assertNotIn(ESSAY, self.said())                                 # no YOU entry: the HUD answers
        self.assertNotIn(DESKTOP_PLAN["say"], self.said())
        self.assert_on_hud()
        newest = self.reading.activity.entries()[0]
        self.assertEqual(newest[2], "Desktop: 1 step checked")
        self.assertIn("nothing opened yet", newest[3])
        for name in NAMES:
            self.assertNotIn(name, newest[2] + newest[3])
        task = self.desktop_tasks()[0]
        self.assertEqual([step.kind for step in task.steps],
                         [live.DESKTOP_REQUEST, live.ASK_CHECKS, live.DESKTOP_FIND, live.PLANNER_RUN,
                          live.DESKTOP_VALIDATE, live.DESKTOP_PLAN])
        request = {field.label: field.value for field in task.steps[0].fields}
        self.assertEqual(request, {"You typed": ESSAY, "Route": ask_ui.DESKTOP_ROUTE_TEXT})
        self.assertEqual((task.status, task.summary), (live.STATUS_OK, "plan checked: 1 step OK - nothing opened yet"))
        self.assertEqual(self.reading.current_tab(), ui.SCREEN_HOME)        # LIVE never came up by itself
        self.assertEqual(self.up(window_bar), ESSAY)                          # Up brings it back, in either bar
        self.assertEqual(self.up(hud_bar), ESSAY)

    def test_from_the_windows_bar_a_local_route_with_no_planner_run(self) -> None:
        self.make_ask()
        window_bar, hud_bar = self.reading.command_bar, self.hud().bar
        self.type_in_window(DOWNLOADS)
        self.assert_on_hud()
        self.assertEqual(self.runs(), [])                                    # handled on this PC
        self.assertEqual(self.hud().caption_text(), DOWNLOADS_WORDS)
        self.assertEqual((window_bar.text(), hud_bar.text()), ("", ""))
        self.assertEqual((window_bar.last_request(), hud_bar.last_request()), (DOWNLOADS, DOWNLOADS))
        self.assertEqual(self.desktop.last_route.kind, dv.OPEN_FOLDER)
        task = self.desktop_tasks()[0]
        self.assertEqual([step.kind for step in task.steps], [live.DESKTOP_REQUEST, live.DESKTOP_PLAN])
        fields = {field.label: field.value for field in task.steps[1].fields}
        self.assertEqual(fields, {"Step": dv.OPEN_FOLDER, "Check": dv.INSTANT, "Jarvis says": DOWNLOADS_WORDS})
        self.assertEqual(task.summary, "handled on this PC - nothing opened yet")
        self.assertNotIn(DOWNLOADS, self.said())

    def test_when_the_hud_cannot_open_nothing_runs_and_the_words_stay(self) -> None:
        self.make_ask()
        window_bar = self.reading.command_bar
        with mock.patch.object(self.desktop, "consent_given", return_value=False):
            self.type_in_window(ESSAY)
        self.assertFalse(self.desktop.is_hud())
        self.assertTrue(self.window.isVisible())
        self.assertEqual(window_bar.text(), ESSAY)                           # not taken: the words stay
        self.assertIn(ask_ui.DESKTOP_NOT_NOW_TEXT, window_bar.status())
        self.assertEqual(self.runs(), [])

    def test_a_desktop_request_while_another_runs_is_refused_and_nothing_switches(self) -> None:
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.make_ask(GatedProcess(plan(), gate))
        self.type_in_window("move my Jarvis test sync to Friday")            # a normal Ask, running
        self.assertTrue(self.c.ask.busy)
        self.c.ask.submit(DOWNLOADS)                                         # a desktop request meanwhile
        settle()
        self.assertFalse(self.desktop.is_hud())
        self.assertIsNone(self.desktop.last_route)
        self.assertIn("Jarvis is already planning", self.reading.command_bar.status())
        gate.set()
        self.finish()


class FlightRequestTests(p15.FlightApp):
    """With motion on, the request typed in the window's bar flies onto the HUD (SPEC 8.2) and runs there meanwhile:
    Ask is bound to the HUD's bar from the switch's start, the words left both bars, and the answer is in the HUD's
    caption once it has landed."""

    def test_the_request_runs_while_the_switch_flies(self) -> None:
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.make(runs=(GatedProcess(plan_stream(DESKTOP_PLAN), gate),))
        self.desktop.context_source = lambda _words: desktop_context()
        self.c.ask.start()
        self.assertTrue(wait_for(lambda: self.c.ask.ready is not None))
        settle()
        self.now[0] += 10.0
        self.t0 = self.now[0]
        window_bar, hud_bar = self.reading.command_bar, self.hud().bar
        window_bar.set_text(ESSAY)
        window_bar.submit()
        settle()
        self.assertIsNotNone(self.desktop.flight())                          # on its way
        self.assertTrue(self.desktop.is_switching() and self.desktop.is_hud())
        self.assertIs(self.c.ask.bar, hud_bar)
        self.assertTrue(self.c.ask.busy and hud_bar.is_running())
        self.assertEqual((window_bar.text(), hud_bar.text()), ("", ""))
        self.land()
        self.assert_on_hud()
        gate.set()
        self.assertTrue(wait_for(lambda: not self.c.ask.busy))
        settle()
        self.assertEqual(self.hud().caption_text(), UNDERSTOOD)
        self.assertEqual(self.closes, [])
        self.assertEqual(self.changes, [True])


class HudBarTests(DesktopRequestApp):
    def test_a_local_route_on_the_huds_bar_leaves_it_and_up_brings_it_back(self) -> None:
        self.make_ask()
        self.enter()
        hud_bar = self.hud().bar
        for typed, words, tone in ((DOWNLOADS, DOWNLOADS_WORDS, hud.TONE_DONE),
                                   ("volume 30", "I can't change the volume yet, sir - nothing was changed.",
                                    hud.TONE_WARN)):
            with self.subTest(typed=typed):
                self.type_on_hud(typed)
                self.assertEqual(self.hud().caption_text(), words)
                self.assertEqual(hud_bar.text(), "")                         # taken (R23)
                self.assertEqual(hud_bar.last_request(), typed)
                self.assertEqual(self.up(hud_bar), typed)
                self.assertFalse(self.hud().holds_keyboard())               # Enter gave the keyboard back
        self.assertEqual(self.runs(), [])
        self.assertEqual(len(self.desktop_tasks()), 2)

    def test_the_planner_takes_the_words_and_its_outcome_never_clears_the_bar(self) -> None:
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.make_ask(GatedProcess(plan_stream(DESKTOP_PLAN), gate))
        self.enter()
        hud_bar = self.hud().bar
        self.type_on_hud(ESSAY)
        self.assertTrue(self.c.ask.busy)
        self.assertEqual(hud_bar.text(), "")                                 # taken as it started
        hud_bar.set_text("open my budget")                                   # typed meanwhile
        gate.set()
        self.finish()
        self.assertEqual(hud_bar.text(), "open my budget")                   # the outcome never clears the bar
        self.assertEqual(self.hud().caption_text(), UNDERSTOOD)
        self.assertEqual(self.prompt_file(), str(cli.DESKTOP_PROMPT_FILE))

    def test_refused_before_any_run_the_words_come_back_to_the_empty_bar(self) -> None:
        self.make_ask(auth='{"loggedIn": false}')                            # Claude Code signed out
        self.enter()
        hud_bar = self.hud().bar
        self.type_on_hud(ESSAY)
        self.finish()
        self.assertEqual(self.runs(), [])
        self.assertEqual(hud_bar.text(), ESSAY)                              # given back: nothing was asked
        self.assertNotEqual(self.hud().caption_text(), ON_DESKTOP)          # the caption says why
        hud_bar.clear_input()
        self.back()
        window_bar = self.reading.command_bar
        window_bar.clear_input()
        self.type_in_window(ESSAY)                                           # from the window's bar: the same
        self.finish()
        self.assert_on_hud()
        self.assertEqual(self.hud().bar.text(), ESSAY)                       # back in the bar it continued in
        self.assertEqual(window_bar.text(), "")
        self.assertEqual(self.up(window_bar), ESSAY)

    def test_a_draft_in_the_huds_bar_is_never_replaced(self) -> None:
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.make_ask(GatedProcess(plan_stream(DESKTOP_PLAN), gate))
        self.enter()
        self.hud().bar.set_text("a draft on the HUD")
        self.back()
        self.type_in_window(ESSAY)
        self.assert_on_hud()
        self.assertTrue(self.c.ask.busy)
        self.assertEqual(self.hud().bar.text(), "a draft on the HUD")
        gate.set()
        self.finish()
        self.assertEqual(self.hud().bar.text(), "a draft on the HUD")

    def test_cancel_on_the_hud_says_so_in_the_bar_and_nothing_in_the_conversation(self) -> None:
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.make_ask(GatedProcess(plan_stream(DESKTOP_PLAN), gate))
        self.enter()
        self.type_on_hud(ESSAY)
        self.assertTrue(wait_for(lambda: self.c.ask.stage() == ask_planner.STAGE_PLANNING))
        QTest.keyClick(self.hud().bar.input, Qt.Key.Key_Escape)              # Esc while it runs: Cancel
        settle()
        gate.set()
        self.finish()
        self.assertIn(ask_ui.CANCELLED_TEXT, self.hud().bar.status())
        self.assertEqual(self.hud().caption_text(), ON_DESKTOP)
        self.assertIsNone(self.desktop.last_plan)
        self.assertNotIn(ESSAY, self.said())
        self.assertNotIn("Cancelled", self.said())

    def test_with_ask_off_a_local_route_is_still_answered_and_taken(self) -> None:
        self.make(ask=False)
        self.enter()
        self.hud_key()
        bar = self.hud().bar
        QTest.keyClicks(bar.input, DOWNLOADS)
        QTest.keyClick(bar.input, Qt.Key.Key_Return)
        settle()
        self.assertEqual(self.hud().caption_text(), DOWNLOADS_WORDS)
        self.assertEqual(bar.text(), "")
        self.assertEqual(bar.last_request(), DOWNLOADS)
        self.assertEqual(self.desktop_tasks()[0].summary, "handled on this PC - nothing opened yet")
        self.hud_key()
        QTest.keyClicks(bar.input, ESSAY)                                    # the planner is Ask's: not now
        QTest.keyClick(bar.input, Qt.Key.Key_Return)
        settle()
        self.assertEqual(self.hud().caption_text(), DISABLED_MESSAGE)
        self.assertEqual(bar.text(), ESSAY)                                  # not taken: the words stay


class CanaryTests(DesktopRequestApp):
    """SPEC 10.2 / hard rule 14 for this part's flow: a canary file name, folder name, app and site title - and the
    owner's words naming it - never reach a log line or a file on disk; Live (memory only) and the planner's stdin are
    the only places they go."""

    CANARY = "CANARY_9f3"

    def test_the_canaries_stay_out_of_the_logs_and_off_the_disk(self) -> None:
        canary = self.CANARY
        context = DesktopContext(
            (Place("C:\\Users\\Owner\\Documents", ("Documents",)),
             Place("C:\\Users\\Owner\\Documents\\" + canary, ("Documents", canary + " folder"))),
            (FileItem("C:\\Users\\Owner\\Documents\\" + canary + "\\" + canary + ".txt", canary + ".txt",
                      "C:\\Users\\Owner\\Documents\\" + canary),),
            (AppItem("Canary.App", canary + " app"),), (SiteItem("https://canary.example.org/", canary + " site"),))
        steps = [{"skill": "open_file", "why": canary, "files": ["f1"]},
                 {"skill": "open_app", "why": "x", "app": "a1"}, {"skill": "open_url", "why": "x", "site": "s1"},
                 {"skill": "open_folder", "why": "x", "place": "d2"}]
        self.make_ask(plan_stream({"say": canary + " lined up.", "question": canary + "?", "steps": steps}))
        self.desktop.context_source = lambda _words: context
        typed = f"open {canary}.txt and the {canary} app"
        with self.assertLogs("briefing_reader", level="DEBUG") as logs:
            self.type_in_window(typed)
            self.finish()
            self.type_on_hud(f"open {canary} site")                          # a local route: the saved site
            self.back()
        self.assertEqual(len(self.runs()), 1)
        self.assertIn(canary, self.runs()[0].stdin)                           # given to the planner, as data
        self.assertIn("plan checked", "\n".join(logs.output))
        for line in logs.output:
            self.assertNotIn(canary, line)
            self.assertNotIn(canary.casefold(), line.casefold())
        for path in self.harness.root.rglob("*"):
            if path.is_file():
                with self.subTest(path=path.name):
                    self.assertNotIn(canary.encode("ascii"), path.read_bytes())
        self.assertIn(canary, "\n".join(field.value for task in self.desktop_tasks() for step in task.steps
                                        for field in step.fields))           # Live: memory only


class NamesOnlyTests(DesktopRequestApp):
    """Owner decision D6: the desktop run's context blocks are its own - no notes, no conversation - while an Ask
    at the same moment gets both."""

    def test_no_notes_and_no_conversation_for_a_desktop_run(self) -> None:
        self.make_ask(plan(say="Nothing much today.", lines=()), plan_stream(DESKTOP_PLAN),
                      plan(say="Still nothing.", lines=()), extra=MEMORY_ON)
        self.type_in_window("remember that the lab opens at 9")
        self.type_in_window("what's on today?")
        self.finish()
        self.type_in_window(ESSAY)
        self.finish()
        self.assert_on_hud()
        self.type_on_hud("what's on tomorrow?")
        self.finish()
        first, desktop, last = (started.stdin for started in self.runs())
        self.assertEqual(self.prompt_file(1), str(cli.DESKTOP_PROMPT_FILE))
        self.assertIn("<memory>", first)
        self.assertIn("lab opens at 9", first)
        self.assertIn("<conversation>", last)                                # an Ask now: what was said before
        for tag in ("<memory>", "<conversation>"):
            self.assertNotIn(tag, desktop)
        self.assertNotIn("lab opens at 9", desktop)
        self.assertNotIn("what's on today", desktop)
        self.assertNotIn(ESSAY, last)                                        # the desktop request joined no conversation
        task = self.desktop_tasks()[0]
        labels = [field.label for field in task.steps[0].fields]
        self.assertNotIn("Memory given", labels)
        self.assertEqual(labels, ["You typed", "Route"])


if __name__ == "__main__":
    unittest.main()
