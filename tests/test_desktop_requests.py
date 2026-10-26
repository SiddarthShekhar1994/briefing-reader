"""Jarvis on your desktop, P1.7 and P1.8, in the app: a typed desktop request from end to end (SPEC 3 and 6.4; hard
rules 1, 2, 6, 9, 13 and 14) and the command bar's rule (R23) along the whole desktop path. ROUTE_DESKTOP only while
[desktop] is on; from the window's bar Jarvis goes onto the HUD and the request continues there; it goes to the
desktop thread, where a local route is carried out on this PC (no planner run) and anything else becomes ONE desktop
planner run whose checked plan is carried out through the desktop thread - the HUD's caption says Jarvis's own words
for what happened; everything else stays a normal Ask. The bar a request was typed in lets its words go as soon as it
is handled (Up brings them back), a request refused before any run gets its words back only into an empty bar, an
outcome never clears the bar, and with Ask off a local route is still carried out on this PC. A desktop run gets
neither the owner's notes nor the conversation (owner decision D6: names only) and never a YOU entry.

Qt runs offscreen in this process and shows nothing. No real hotkey, pipe, foreground change or OS call: the HUD's
native seam is a recording fake, the desktop's backend is tests.desktop_fakes' fake over a temporary folder (its
opens open nothing: they are recorded), the candidates are invented (context_source) and claude.exe is never started
(tests.ask_fakes).
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from briefing_reader import ask_ui, desktop_ui, hud, live, ui
from briefing_reader.ask import cli
from briefing_reader.ask import planner as ask_planner
from briefing_reader.ask.modes import MODE_DESKTOP
from briefing_reader.ask.planner import DISABLED_MESSAGE
from briefing_reader.desktop import backend as dbk
from briefing_reader.desktop import executor as dx
from briefing_reader.desktop import validate as dv
from briefing_reader.desktop.context import AppItem, DesktopContext, FileItem, Place, SiteItem
from tests import test_desktop_switch as p15
from tests.ask_fakes import GatedProcess, plan_stream
from tests.desktop_fakes import DOWNLOADS, LING, desktop_context, desktop_disk
from tests.test_desktop_keys import KeyApp
from tests.ui_fakes import fonts, plan, settle, wait_for

ESSAY = "open my LING essay"
ESSAY_PATH = LING + "\\LING essay draft.docx"
DESKTOP_PLAN = {"say": "I have lined up your LING essay draft.",
                "steps": [{"skill": "open_file", "why": "the essay draft", "files": ["f1"]}]}
OPENED = "Opened the file, sir."
DOWNLOADS_TYPED = "open Downloads"
DOWNLOADS_WORDS = "Opened your Downloads folder, sir."
ON_DESKTOP = "On your desktop, sir."
MEMORY_ON = "\n[productivity]\nenabled = true\n"
NAMES = ("LING", "essay", "Spotify", "Microsoft Word", "PowerShell", "Student portal", "lined up", "Owner",
         "C:\\")
_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_desktop_requests", "-platform", "offscreen"])
    fonts()


class DesktopRequestApp(KeyApp):
    """KeyApp (the instant switch, the HUD over a recording seam) with Ask ready, the desktop's backend a fake over a
    temporary folder holding the invented files (``fake``: tests.desktop_fakes.desktop_disk) and the invented
    candidates (``context_source``: tests.desktop_fakes.desktop_context, each item's identity as on the fake disk)."""

    def make_ask(self, *runs: object, extra: str = "", invented: bool = True, **harness: object) -> None:
        self.fake = self.disk()
        self.make(extra, runs=runs, desktop_backend=lambda: self.fake, **harness)
        if invented:
            context = desktop_context(backend=self.fake)
            self.desktop.context_source = lambda _words: context
        self.c.ask.start()
        self.assertTrue(wait_for(lambda: self.c.ask.ready is not None), "readiness checked")
        settle()

    def disk(self):  # type: ignore[no-untyped-def]
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        return desktop_disk(tmp.name)

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

    def answered(self) -> None:
        """The desktop thread has answered the request (a local route carried out, or the planner's job handed on)."""
        self.assertTrue(wait_for(lambda: not self.desktop.busy()), "the desktop thread answered")
        settle()

    def started(self) -> None:
        """The request's planner run has started."""
        self.answered()
        self.assertTrue(wait_for(lambda: self.c.ask.busy), "the planner run started")
        settle()

    def finish(self) -> None:
        self.answered()
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

    def assert_on_the_desktop_thread(self) -> None:
        """Every call the backend got came from the desktop thread: never the GUI's or the Ask's."""
        self.assertTrue(self.fake.threads)
        self.assertEqual(set(self.fake.threads), {"desktop"})


class RoutingTests(DesktopRequestApp):
    def test_desktop_off_it_is_a_normal_ask(self) -> None:
        self.make(enabled=False, runs=(plan(say="Here is what I found.", lines=()),))
        self.c.ask.start()
        self.assertTrue(wait_for(lambda: self.c.ask.ready is not None))
        self.assertIsNone(self.c.desktop)
        self.type_in_window(ESSAY)
        self.assertTrue(wait_for(lambda: not self.c.ask.busy))
        settle()
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
        self.assertEqual(self.fake.calls, [])                                # the desktop thread was never asked

    def test_from_the_windows_bar_onto_the_hud_one_planner_run_and_the_file_opens(self) -> None:
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.make_ask(GatedProcess(plan_stream(DESKTOP_PLAN), gate))
        ask, window_bar, hud_bar = self.c.ask, self.reading.command_bar, self.hud().bar
        with mock.patch.object(ui, "force_foreground") as forced:
            self.type_in_window(ESSAY)
            self.started()
        forced.assert_not_called()
        self.assert_on_hud()                                                  # the window hidden, never closed
        self.assertEqual(self.closes, [])
        self.assertIs(ask.bar, hud_bar)                                       # the request continues on the HUD
        self.assertTrue(ask.busy and hud_bar.is_running())
        self.assertEqual(ask.running_mode, MODE_DESKTOP)
        self.assertEqual((window_bar.text(), hud_bar.text()), ("", ""))      # taken from both (R23)
        self.assertEqual((window_bar.last_request(), hud_bar.last_request()), (ESSAY, ESSAY))
        self.assertTrue(wait_for(lambda: ask.stage() == ask_planner.STAGE_PLANNING))
        settle()
        self.assertEqual(self.hud().caption_text(), "Planning...")          # what Jarvis is doing, no seconds
        self.assertEqual(self.fake.opened, [])                                # nothing opens before the plan
        gate.set()
        self.finish()
        self.assertEqual(self.fake.opened, [("open_document", ESSAY_PATH)])  # the one INSTANT step: opened
        self.assertEqual(self.hud().caption_text(), OPENED)                  # Jarvis's own words
        self.assertFalse(hud_bar.is_running())
        self.assertEqual(len(self.runs()), 1)                                # ONE planner run
        self.assertEqual(self.prompt_file(), str(cli.DESKTOP_PROMPT_FILE))
        stdin = self.runs()[0].stdin
        self.assertIn('f1 "LING essay draft.docx" d3', stdin)               # names only
        self.assertIn("The owner's words: " + ESSAY, stdin)
        self.assertNotIn("C:\\", stdin)
        plan_kept = self.desktop.last_plan
        self.assertIsInstance(plan_kept, dv.DesktopPlan)
        self.assertEqual([step.code or step.tier for step in plan_kept.steps], [dv.INSTANT])
        run = self.desktop.last_run
        self.assertIsInstance(run, dx.DesktopRun)
        self.assertEqual([result.status for result in run.results], [dx.DONE])
        others = [item.name for item in run.results[0].others]                # D5: the other matches, kept
        self.assertEqual(others[0], "LING essay draft (old).docx")
        self.assertEqual(self.desktop.pending, ())
        self.assert_on_the_desktop_thread()                                   # every OS call off the GUI thread
        self.assertNotIn(ESSAY, self.said())                                 # no YOU entry: the HUD answers
        self.assertNotIn(DESKTOP_PLAN["say"], self.said())
        self.assert_on_hud()
        newest = self.reading.activity.entries()[0]
        self.assertEqual(newest[2], "Desktop: 1 step checked")
        self.assertIn("1 opened", newest[3])
        for name in NAMES:
            self.assertNotIn(name, newest[2] + newest[3])
        task = self.desktop_tasks()[0]
        self.assertEqual([step.kind for step in task.steps],
                         [live.DESKTOP_REQUEST, live.ASK_CHECKS, live.DESKTOP_FIND, live.PLANNER_RUN,
                          live.DESKTOP_VALIDATE, live.DESKTOP_STEP, live.DESKTOP_PLAN])
        request = {field.label: field.value for field in task.steps[0].fields}
        self.assertEqual(request, {"You typed": ESSAY, "Route": ask_ui.DESKTOP_ROUTE_TEXT})
        carried = task.steps[5]
        self.assertEqual([item.text for item in carried.items], ["Step 1 - open_file - instant - opened"])
        self.assertEqual((task.status, task.summary), (live.STATUS_OK, "plan checked: 1 step OK - 1 opened"))
        self.assertEqual(self.reading.current_tab(), ui.SCREEN_HOME)        # LIVE never came up by itself
        self.assertEqual(self.up(window_bar), ESSAY)                          # Up brings it back, in either bar
        self.assertEqual(self.up(hud_bar), ESSAY)

    def test_from_the_windows_bar_a_local_route_opens_with_no_planner_run(self) -> None:
        self.make_ask()
        window_bar, hud_bar = self.reading.command_bar, self.hud().bar
        self.type_in_window(DOWNLOADS_TYPED)
        self.answered()
        self.assert_on_hud()
        self.assertEqual(self.runs(), [])                                    # handled on this PC
        self.assertEqual(self.fake.opened, [("open_folder", DOWNLOADS)])
        self.assertEqual(self.hud().caption_text(), DOWNLOADS_WORDS)
        self.assertEqual((window_bar.text(), hud_bar.text()), ("", ""))
        self.assertEqual((window_bar.last_request(), hud_bar.last_request()), (DOWNLOADS_TYPED, DOWNLOADS_TYPED))
        self.assertEqual(self.desktop.last_route.kind, dv.OPEN_FOLDER)
        task = self.desktop_tasks()[0]
        self.assertEqual([step.kind for step in task.steps],
                         [live.DESKTOP_REQUEST, live.DESKTOP_STEP, live.DESKTOP_PLAN])
        self.assertEqual([item.text for item in task.steps[1].items],
                         ["On this PC - open_folder - instant - opened"])
        fields = {field.label: field.value for field in task.steps[2].fields}
        self.assertEqual(fields, {"Step": dv.OPEN_FOLDER, "Check": dv.INSTANT, "Jarvis says": DOWNLOADS_WORDS})
        self.assertEqual(task.summary, "handled on this PC - 1 opened")
        self.assertNotIn(DOWNLOADS_TYPED, self.said())
        self.assert_on_the_desktop_thread()

    def test_when_the_hud_cannot_open_nothing_runs_and_the_words_stay(self) -> None:
        self.make_ask()
        window_bar = self.reading.command_bar
        with mock.patch.object(self.desktop, "consent_given", return_value=False):
            self.type_in_window(ESSAY)                       # not turned on yet: the first-run card instead (P1.9)
        self.assertFalse(self.desktop.is_hud())
        self.assertTrue(self.window.isVisible())
        self.assertEqual(window_bar.text(), ESSAY)                           # not taken: the words stay
        self.assertIn(ui.DESKTOP_OFF_TEXT, window_bar.status())
        self.assertTrue(self.c._desktop_consent_card.is_open())
        self.c._desktop_consent_card.close_card()
        with mock.patch.object(QApplication, "activeModalWidget", return_value=self.window):
            self.type_in_window(ESSAY)                       # a dialog waits for an answer: not now
        self.assertFalse(self.desktop.is_hud())
        self.assertEqual(window_bar.text(), ESSAY)
        self.assertIn(ask_ui.DESKTOP_NOT_NOW_TEXT, window_bar.status())
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.fake.calls, [])

    def test_a_desktop_request_while_another_runs_is_refused_and_nothing_switches(self) -> None:
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.make_ask(GatedProcess(plan(), gate))
        self.type_in_window("move my Jarvis test sync to Friday")            # a normal Ask, running
        self.assertTrue(self.c.ask.busy)
        self.c.ask.submit(DOWNLOADS_TYPED)                                   # a desktop request meanwhile
        settle()
        self.assertFalse(self.desktop.is_hud())
        self.assertIsNone(self.desktop.last_route)
        self.assertIn("Jarvis is already planning", self.reading.command_bar.status())
        gate.set()
        self.finish()
        self.assertEqual(self.fake.calls, [])

    def test_without_a_backend_nothing_is_looked_at_and_the_words_stay(self) -> None:
        """The real backend refuses to be made while the tests run: the request says so and nothing happens."""
        self.make(runs=(), desktop_backend=None)
        self.c.ask.start()
        self.assertTrue(wait_for(lambda: self.c.ask.ready is not None))
        self.enter()
        self.type_on_hud(DOWNLOADS_TYPED)
        self.answered()
        self.assertEqual(self.hud().caption_text(), "I can't look on this PC right now, sir - nothing was opened.")
        self.assertEqual(self.hud().bar.text(), DOWNLOADS_TYPED)             # not taken
        self.assertEqual(self.runs(), [])


class FlightRequestTests(p15.FlightApp):
    """With motion on, the request typed in the window's bar flies onto the HUD (SPEC 8.2) and is resolved meanwhile:
    Ask is bound to the HUD's bar from the switch's start, the words left both bars once the planner run started, and
    the answer is in the HUD's caption once it has landed."""

    def test_the_request_runs_while_the_switch_flies(self) -> None:
        gate = threading.Event()
        self.addCleanup(gate.set)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        fake = desktop_disk(tmp.name)
        self.make(runs=(GatedProcess(plan_stream(DESKTOP_PLAN), gate),), desktop_backend=lambda: fake)
        context = desktop_context(backend=fake)
        self.desktop.context_source = lambda _words: context
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
        self.assertTrue(wait_for(lambda: self.c.ask.busy))                   # resolved meanwhile: the run started
        self.assertTrue(hud_bar.is_running())
        self.assertEqual((window_bar.text(), hud_bar.text()), ("", ""))
        self.land()
        self.assert_on_hud()
        gate.set()
        self.assertTrue(wait_for(lambda: not self.c.ask.busy))
        settle()
        self.assertEqual(self.hud().caption_text(), OPENED)
        self.assertEqual(fake.opened, [("open_document", ESSAY_PATH)])
        self.assertEqual(self.closes, [])
        self.assertEqual(self.changes, [True])


class HudBarTests(DesktopRequestApp):
    def test_a_local_route_on_the_huds_bar_leaves_it_and_up_brings_it_back(self) -> None:
        self.make_ask()
        self.enter()
        hud_bar = self.hud().bar
        for typed, words, tone in ((DOWNLOADS_TYPED, DOWNLOADS_WORDS, hud.TONE_DONE),
                                   ("volume 30", "I can't change the volume yet, sir - nothing was changed.",
                                    hud.TONE_WARN)):
            with self.subTest(typed=typed):
                self.type_on_hud(typed)
                self.answered()
                self.assertEqual(self.hud().caption_text(), words)
                self.assertEqual(hud_bar.text(), "")                         # taken (R23)
                self.assertEqual(hud_bar.last_request(), typed)
                self.assertEqual(self.up(hud_bar), typed)
                self.assertFalse(self.hud().holds_keyboard())               # Enter gave the keyboard back
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.fake.opened, [("open_folder", DOWNLOADS)])    # "volume 30" changed nothing
        self.assertEqual(len(self.desktop_tasks()), 2)

    def test_the_caption_says_jarvis_is_looking_meanwhile(self) -> None:
        self.make_ask()
        self.enter()
        release = threading.Event()
        self.addCleanup(release.set)
        self.fake.on_call = lambda method, _argument: release.wait(5) if method == "open_folder" else None
        self.type_on_hud(DOWNLOADS_TYPED)
        bar = self.hud().bar
        self.assertTrue(self.desktop.busy())
        self.assertEqual(self.hud().caption_text(), desktop_ui.LOOKING_TEXT)
        self.assertEqual(bar.text(), DOWNLOADS_TYPED)                        # not handled yet: the words stay
        QTest.keyClick(bar.input, Qt.Key.Key_Return)                          # Enter again meanwhile: refused
        settle()
        self.assertEqual(self.hud().caption_text(), desktop_ui.BUSY_TEXT)
        self.assertEqual(bar.text(), DOWNLOADS_TYPED)
        self.c.ask.submit("what's on today?")                                # an Ask meanwhile waits too
        settle()
        self.assertEqual(self.runs(), [])
        release.set()
        self.answered()
        self.assertEqual(self.hud().caption_text(), DOWNLOADS_WORDS)
        self.assertEqual(bar.text(), "")                                     # handled: taken (R23)
        self.assertEqual(self.fake.opened_methods(), ["open_folder"])        # once

    def test_a_request_that_takes_too_long_opens_nothing_after_the_wait(self) -> None:
        self.make_ask()
        self.enter()
        release, first = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def stuck(method: str, _argument: str) -> None:
            if method == "known_folder" and not first.is_set():
                first.set()
                release.wait(5)                                              # the desktop thread hangs here
        self.fake.on_call = stuck
        with mock.patch.object(desktop_ui, "RESOLVE_TIMEOUT_S", 0.2):
            self.type_on_hud(DOWNLOADS_TYPED)
            self.assertTrue(wait_for(lambda: self.hud().caption_text() == "Looking on this PC took too long, sir - "
                                                                          "I stopped."))
        self.assertFalse(self.desktop.busy())                                # the next request may come
        self.assertEqual(self.hud().bar.text(), DOWNLOADS_TYPED)             # not handled: the words stay
        release.set()
        self.desktop._worker.submit(lambda: None).result(timeout=5)          # noqa: SLF001 - the late job ended
        settle()
        self.assertEqual(self.fake.opened, [])                               # it gave up: nothing opened after
        self.assertEqual(self.hud().caption_text(), "Looking on this PC took too long, sir - I stopped.")

    def test_cancel_while_the_plan_is_carried_out_stops_before_the_next_step(self) -> None:
        steps = [{"skill": "open_file", "why": "x", "files": ["f1"]},
                 {"skill": "open_file", "why": "x", "files": ["f3"]}]
        self.make_ask(plan_stream({"say": "", "steps": steps}))
        self.enter()
        release, opening = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def slow(method: str, _argument: str) -> None:
            if method == "open_document" and not opening.is_set():
                opening.set()
                release.wait(5)                                              # the first open is in flight
        self.fake.on_call = slow
        self.type_on_hud("open my LING essay and the budget")
        self.started()
        self.assertTrue(wait_for(opening.is_set))
        QTest.keyClick(self.hud().bar.input, Qt.Key.Key_Escape)              # Esc while it runs: Cancel
        settle()
        release.set()
        self.finish()
        self.assertEqual(self.fake.opened, [("open_document", ESSAY_PATH)])  # the one in flight completed
        self.assertEqual([result.status for result in self.desktop.last_run.results], [dx.DONE, dx.CANCELLED])
        self.assertEqual(self.hud().caption_text(), "Opened the file. Stopped - nothing more was opened, sir.")

    def test_open_an_app_by_its_name_launches_it(self) -> None:
        self.make_ask(invented=False)
        self.enter()
        self.type_on_hud("open Calculator")
        self.answered()
        self.assertEqual(self.fake.opened, [("launch_app", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App")])
        self.assertEqual(self.hud().caption_text(), "Opened the app, sir.")
        self.assertEqual(self.runs(), [])

    def test_the_planner_takes_the_words_and_its_outcome_never_clears_the_bar(self) -> None:
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.make_ask(GatedProcess(plan_stream(DESKTOP_PLAN), gate))
        self.enter()
        hud_bar = self.hud().bar
        self.type_on_hud(ESSAY)
        self.started()
        self.assertEqual(hud_bar.text(), "")                                 # taken as it started
        hud_bar.set_text("open my budget")                                   # typed meanwhile
        gate.set()
        self.finish()
        self.assertEqual(hud_bar.text(), "open my budget")                   # the outcome never clears the bar
        self.assertEqual(self.hud().caption_text(), OPENED)
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
        self.assertEqual(self.fake.opened, [])
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
        self.started()
        self.assertTrue(self.c.ask.busy)
        self.assertEqual(self.hud().bar.text(), "a draft on the HUD")
        gate.set()
        self.finish()
        self.assertEqual(self.hud().bar.text(), "a draft on the HUD")

    def test_cancel_on_the_hud_says_so_in_the_bar_and_nothing_opens(self) -> None:
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.make_ask(GatedProcess(plan_stream(DESKTOP_PLAN), gate))
        self.enter()
        self.type_on_hud(ESSAY)
        self.started()
        self.assertTrue(wait_for(lambda: self.c.ask.stage() == ask_planner.STAGE_PLANNING))
        QTest.keyClick(self.hud().bar.input, Qt.Key.Key_Escape)              # Esc while it runs: Cancel
        settle()
        gate.set()
        self.finish()
        self.assertIn(ask_ui.CANCELLED_TEXT, self.hud().bar.status())
        self.assertEqual(self.hud().caption_text(), ON_DESKTOP)
        self.assertIsNone(self.desktop.last_plan)
        self.assertEqual(self.fake.opened, [])
        self.assertNotIn(ESSAY, self.said())
        self.assertNotIn("Cancelled", self.said())

    def test_with_ask_off_a_local_route_is_still_carried_out_and_taken(self) -> None:
        self.fake = self.disk()
        self.make(ask=False, desktop_backend=lambda: self.fake)
        self.enter()
        self.hud_key()
        bar = self.hud().bar
        QTest.keyClicks(bar.input, DOWNLOADS_TYPED)
        QTest.keyClick(bar.input, Qt.Key.Key_Return)
        settle()
        self.answered()
        self.assertEqual(self.hud().caption_text(), DOWNLOADS_WORDS)
        self.assertEqual(bar.text(), "")
        self.assertEqual(bar.last_request(), DOWNLOADS_TYPED)
        self.assertEqual(self.desktop_tasks()[0].summary, "handled on this PC - 1 opened")
        self.assertEqual(self.fake.opened, [("open_folder", DOWNLOADS)])
        self.hud_key()
        QTest.keyClicks(bar.input, ESSAY)                                    # the planner is Ask's: not now
        QTest.keyClick(bar.input, Qt.Key.Key_Return)
        settle()
        self.answered()
        self.assertEqual(self.hud().caption_text(), DISABLED_MESSAGE)
        self.assertEqual(bar.text(), ESSAY)                                  # not taken: the words stay
        self.assertEqual(self.fake.opened_methods(), ["open_folder"])


class OneClickTests(DesktopRequestApp):
    """ONE-CLICK and CARD steps wait for the owner's click (P1.9 makes their cards): nothing opens them; the caption
    names what waits, in Jarvis's own words; the controller holds them."""

    def test_a_suggested_address_and_a_macro_document_wait(self) -> None:
        steps = [{"skill": "open_url", "why": "the portal", "url": "https://portal.example.edu/modules"},
                 {"skill": "open_file", "why": "the grades", "files": ["f4"]}]
        self.make_ask(plan_stream({"say": "Here you go.", "steps": steps}))
        self.enter()
        self.type_on_hud("open the student portal and my grades")
        self.finish()
        self.assertEqual(self.fake.opened, [])                                # nothing opened
        self.assertEqual(self.hud().caption_text(),
                         "2 steps wait for your click or OK - nothing was opened, sir.")
        self.assertEqual([(result.skill, result.tier, result.status) for result in self.desktop.pending],
                         [(dv.OPEN_URL, dv.ONE_CLICK, dx.WAITING), (dv.OPEN_FILE, dv.CARD, dx.WAITING)])
        task = self.desktop_tasks()[0]
        carried = next(step for step in task.steps if step.kind == live.DESKTOP_STEP)
        self.assertEqual([item.text for item in carried.items],
                         ["Step 1 - open_url - one-click - waiting for you",
                          "Step 2 - open_file - card - waiting for you"])
        for item in carried.items:
            self.assertNotIn("portal", item.text + item.note)

    def test_one_suggested_address_waits_for_a_click(self) -> None:
        steps = [{"skill": "open_file", "why": "the essay", "files": ["f1"]},
                 {"skill": "open_url", "why": "the portal", "url": "https://portal.example.edu/modules"}]
        self.make_ask(plan_stream({"say": "Here you go.", "steps": steps}))
        self.enter()
        self.type_on_hud("open my LING essay and the portal")
        self.finish()
        self.assertEqual(self.fake.opened, [("open_document", ESSAY_PATH)])
        self.assertEqual(self.hud().caption_text(),
                         "Opened the file. A web page you didn't type waits for your click, sir.")


class CanaryTests(DesktopRequestApp):
    """SPEC 10.2 / hard rule 14 for this part's flow: a canary file name, folder name, app and site title - and the
    owner's words naming them - never reach a log line or a file on disk of Jarvis's; Live (memory only), the
    planner's stdin and the backend's own calls (the pretend disk) are the only places they go."""

    CANARY = "CANARY_9f3"

    def test_the_canaries_stay_out_of_the_logs_and_off_the_disk(self) -> None:
        canary = self.CANARY
        docs = "C:\\Users\\Owner\\OneDrive\\Documents"
        self.make_ask(plan_stream({"say": canary + " lined up.", "question": canary + "?", "steps": [
            {"skill": "open_file", "why": canary, "files": ["f1"]},
            {"skill": "open_app", "why": "x", "app": "a1"}, {"skill": "open_url", "why": "x", "site": "s1"},
            {"skill": "open_folder", "why": "x", "place": "d2"}]}), invented=False)
        self.fake.make_file(docs + "\\" + canary + "\\" + canary + ".txt", b"canary")
        self.fake.apps.append(dbk.AppEntry("Canary.App", canary + " app"))
        context = DesktopContext(
            (Place(docs, ("Documents",), self.fake.identity(docs)),
             Place(docs + "\\" + canary, ("Documents", canary + " folder"), self.fake.identity(docs + "\\" + canary))),
            (FileItem(docs + "\\" + canary + "\\" + canary + ".txt", canary + ".txt", docs + "\\" + canary,
                      identity=self.fake.identity(docs + "\\" + canary + "\\" + canary + ".txt")),),
            (AppItem("Canary.App", canary + " app"),), (SiteItem("https://canary.example.org/", canary + " site"),))
        self.desktop.context_source = lambda _words: context
        typed = f"open {canary}.txt and the {canary} app"
        with self.assertLogs("briefing_reader", level="DEBUG") as logs:
            self.type_in_window(typed)
            self.finish()
            self.type_on_hud(f"open {canary} site")                          # a local route: the saved site
            self.answered()
            self.back()
        self.assertEqual(len(self.runs()), 1)
        self.assertIn(canary, self.runs()[0].stdin)                           # given to the planner, as data
        self.assertEqual(self.fake.opened_methods(), ["open_document", "launch_app", "open_url", "open_folder",
                                                      "open_url"])           # all carried out
        self.assertIn("plan checked", "\n".join(logs.output))
        self.assertIn("carried out", "\n".join(logs.output))
        for line in logs.output:
            self.assertNotIn(canary, line)
            self.assertNotIn(canary.casefold(), line.casefold())
        for path in self.harness.root.rglob("*"):
            if path.is_file():
                with self.subTest(path=path.name):
                    self.assertNotIn(canary.encode("ascii"), path.read_bytes())
        values = "\n".join(field.value for task in self.desktop_tasks() for step in task.steps
                           for field in step.fields)
        self.assertIn(canary, values)                                         # Live: memory only (what was typed)
        for task in self.desktop_tasks():
            for step in task.steps:
                if step.kind == live.DESKTOP_STEP:                            # the steps carried out: no name
                    for item in step.items:
                        self.assertNotIn(canary, item.text + item.note)


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
        self.started()
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
