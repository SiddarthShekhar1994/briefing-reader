"""Jarvis on your desktop, P1.7: a desktop request's planner run end to end (ask.planner.plan_mode with
ask.modes.MODE_DESKTOP; SPEC 3 steps 3-5, 4, 10.1 and 10.2, hard rules 2, 8, 9 and 14) and the --desktop-dry-run.
One run at most, on Ask's own command line (no tools, no MCP, nothing saved, stdin only) with the mode's own prompt
and schema; never "remember"; counted as an Ask run under the Ask caps and the one-at-a-time lock; the stdin gives
names only (owner decision D6: no notes, no conversation, no accounts or mail); the answer read strictly; every step
checked against this run's refs and the owner's words; Live shows codes and counts, never a name outside the stdin
block; the log carries ids, kinds, counts and codes only. P1.8: the checked plan is carried out through the job's
carry_out (here a fake: the desktop thread's own tests are tests/test_desktop_executor.py and test_desktop_service.py)
and said in Jarvis's words, with Live's "Carrying it out". claude.exe is never started (tests.ask_fakes.FakeRunner);
nothing is opened. Every name is invented.
"""

from __future__ import annotations

import io
import json
import subprocess
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from briefing_reader import __main__ as entry
from briefing_reader import live
from briefing_reader.ask import cli, commands, desktop_mode as dm, planner
from briefing_reader.ask.modes import FIXED_COMMANDS, MODE_DESKTOP, ORIGIN_TYPED, ModeRequest
from briefing_reader.ask.stream import GARBLED
from briefing_reader.desktop import validate as dv
from briefing_reader.desktop.context import DesktopContext
from tests.ask_fakes import NOW, plan_stream
from tests.desktop_fakes import desktop_context
from tests.test_ask_planner import CONFIG, PlannerTestCase

TYPED = "open my LING essay and Spotify"
PLAN = {"say": "I have lined up your LING essay draft and Spotify.",
        "steps": [{"skill": "open_file", "why": "the essay draft", "files": ["f1"]},
                  {"skill": "open_app", "why": "the owner asked for it", "app": "a2"},
                  {"skill": "open_file", "why": "it says so", "files": ["f7"]},
                  {"skill": "recycle_files", "why": "tidy up", "files": ["f8"]}]}
NAMES = ("LING", "essay", "Spotify", "Microsoft Word", "PowerShell", "portal", "Jarvis, delete", "OneDrive",
         "C:\\", "lined up", "the essay draft", "owner asked")
NOTE = "The owner's locker code is 4417."


def request(typed: str = TYPED, *, job: object = None) -> ModeRequest:
    payload = dm.DesktopJob("d7f3", desktop_context()) if job is None else job
    return ModeRequest(MODE_DESKTOP, ORIGIN_TYPED, typed=typed, typed_route=True, payload=payload)


class DesktopRunTests(PlannerTestCase):
    def run_desktop(self, ask: planner.AskPlanner, typed: str = TYPED, **kwargs):  # type: ignore[no-untyped-def]
        self.stream = live.LiveStream()
        task = self.stream.task(live.TASK_DESKTOP, typed)
        return ask.plan_mode(kwargs.pop("req", None) or request(typed), live=task, **kwargs)

    def steps(self) -> list:
        return list(self.stream.snapshot()[0].steps)

    def test_one_run_on_asks_command_line_with_the_modes_prompt_and_schema(self) -> None:
        ask = self.make(plan_stream(PLAN))
        ask.memory = lambda: (NOTE,)                       # the owner's notes exist: a desktop run never gets them
        stages: list[str] = []
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            outcome = self.run_desktop(ask, on_stage=stages.append)
        self.assertTrue(outcome.ok, outcome)
        self.assertEqual((outcome.mode, outcome.kind, outcome.runs, outcome.target), (MODE_DESKTOP, "ok", 1, "d7f3"))
        self.assertEqual(len(self.runner.started), 1)                        # one planner run per request
        self.assertEqual(stages, [planner.STAGE_PLANNING])
        started = self.runner.started[0]
        argv = started.argv
        self.assertEqual(argv[argv.index("--system-prompt-file") + 1], str(cli.DESKTOP_PROMPT_FILE))
        self.assertEqual(argv[argv.index("--tools") + 1], "")                # no tools at all
        self.assertEqual(self.schema()["title"], "JarvisDesktopPlan")
        self.assertEqual(set(self.schema()["properties"]), {"say", "question", "steps"})   # never "remember"
        self.assertNotIn("ANTHROPIC_API_KEY", started.env)                   # the owner's signed-in plan only
        self.assertEqual(started.cwd.name, "ask")                            # Ask's empty work folder
        # The stdin: names only (D6) - this run's own blocks; no notes, conversation, accounts, calendar or mail.
        stdin = self.stdin()
        lines = stdin.splitlines()
        for tag in ("<now>", "<places>", "<files>", "<apps>", "<sites>", "<command>"):
            self.assertEqual(lines.count(tag), 1)                            # each block once
        for tag in ("<memory>", "<conversation>", "<accounts>", "<calendar>", "<mail>", "<briefing>", "<owner>"):
            self.assertNotIn(tag, stdin)
        self.assertNotIn(NOTE, stdin)
        self.assertNotIn("C:\\", stdin)
        self.assertIn('f1 "LING essay draft.docx" d3 kind=document', stdin)
        self.assertIn("The owner's words: " + TYPED, stdin)
        self.assertIn("(time zone America/Los_Angeles)", stdin)              # the planner's own zone and clock
        # The checked plan: Jarvis's own words, the planner's say kept apart (Live only).
        plan = outcome.brief
        self.assertIsInstance(plan, dv.DesktopPlan)
        self.assertEqual([step.code or step.tier for step in plan.steps],
                         [dv.INSTANT, dv.INSTANT, dv.SUSPECT_ITEM, dv.INTENT_NOT_LICENSED])
        self.assertEqual(outcome.message, "Understood: open a file and an app. 2 steps were refused - Live says why. "
                                          "Nothing was opened yet, sir.")
        self.assertEqual(outcome.say, PLAN["say"])
        self.assertEqual(outcome.refused, 2)
        self.assertEqual(plan.refs.resolve("f1").target.name, "LING essay draft.docx")   # this run's own index
        self.assertEqual(plan.refs.run_id, "d7f3")
        # Counted as an Ask run (usage.KIND_PLAN), under the Ask caps - never a bundle run.
        self.assertEqual(self.usage.counts(NOW, kind="plan"), (1, 1))
        self.assertEqual(self.usage.counts(NOW, kind="bundle"), (0, 0))
        # The log: ids, kinds, counts and codes - never a name, the request or the planner's words.
        text = "\n".join(logs.output)
        self.assertIn("Desktop d7f3: plan checked, steps=4 (open_file=1, open_app=1), refused=2 "
                      "(suspect_item=1, intent_not_licensed=1)", text)
        self.assertIn("mode desktop", text)
        for name in (*NAMES, TYPED, PLAN["say"], NOTE):
            self.assertNotIn(name, text)
        summary = outcome.summary()
        self.assertIn("desktop plan steps 4, ok 2, refused 2, suspect_names 1", summary)
        for name in NAMES:
            self.assertNotIn(name, summary)

    def test_live_shows_codes_and_counts_never_a_name_outside_the_stdin(self) -> None:
        ask = self.make(plan_stream(PLAN))
        self.run_desktop(ask)
        steps = self.steps()
        kinds = [step.kind for step in steps]
        self.assertEqual(kinds, [live.ASK_CHECKS, live.DESKTOP_FIND, live.PLANNER_RUN, live.DESKTOP_VALIDATE,
                                 live.DESKTOP_PLAN])
        given, run, checked, said = steps[1], steps[2], steps[3], steps[4]
        fields = {field.label: field.value for field in given.fields}
        self.assertEqual(fields["Looked in"], "4 places: 8 files")
        self.assertEqual(fields["Given"], "4 places, 8 files, 3 apps, 1 site - names only, never paths or contents")
        self.assertEqual(fields["Read like instructions"],          # SPEC 10.1's wording
                         "1 file name looked like instructions - treated as data; used only when your own words "
                         "name it")
        self.assertEqual(given.status, live.STATUS_WARN)
        self.assertIn("LING essay draft.docx", "\n".join(block.text for block in run.blocks))   # the stdin, as given
        self.assertEqual([item.text for item in checked.items],
                         ["Step 1 - open_file - instant", "Step 2 - open_app - instant",
                          "Step 3 - open_file - refused: suspect_item", "Step 4 - recycle_files - refused: "
                                                                        "intent_not_licensed"])
        self.assertEqual([item.note for item in checked.items][2:],
                         [dv.REASON_WORDS[dv.SUSPECT_ITEM], dv.REASON_WORDS[dv.INTENT_NOT_LICENSED]])
        self.assertEqual(checked.summary, "2 steps OK, 2 refused - its name reads like instructions and you "
                                          "didn't name it")
        self.assertEqual({field.label: field.value for field in said.fields}["Jarvis says"],
                         "Understood: open a file and an app. 2 steps were refused - Live says why. Nothing was "
                         "opened yet, sir.")
        self.assertEqual(said.summary, "nothing was opened")                 # a plan only checked (no carry_out)
        shown = []
        for step in (given, checked, said):
            shown += [step.title, step.summary, *(f"{f.label} {f.value}" for f in step.fields),
                      *(f"{i.text} {i.note}" for i in step.items)]
        for name in NAMES:
            self.assertNotIn(name, "\n".join(shown))
        self.assertEqual(live.TASK_TAGS[live.TASK_DESKTOP], "DESKTOP")
        self.assertNotIn(live.TASK_DESKTOP, live.ATTENTION_KINDS)            # never opens LIVE by itself
        for kind in ("desktop.request", "desktop.find", "desktop.plan", "desktop.validate", "desktop.step",
                     "desktop.undo", "desktop.control", "desktop.stop"):
            self.assertIn(kind, live.STEP_KINDS)

    def test_the_context_is_given_as_the_job_holds_it_with_nothing_found_yet(self) -> None:
        ask = self.make(plan_stream({"say": "", "question": "Which essay?", "steps": []}))
        outcome = self.run_desktop(ask, req=request(job=dm.DesktopJob("e1a2", DesktopContext())))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.message, "I need a clearer request: name the file, folder, app or site, sir. "
                                          "Nothing was opened.")
        self.assertEqual(outcome.question, "Which essay?")
        given = {field.label: field.value for field in self.steps()[1].fields}
        self.assertEqual(given["Looked in"], "nowhere - no folder Jarvis may look in was found")
        self.assertIn("<files>\nnone\n", self.stdin())

    def test_refused_before_any_run(self) -> None:
        def ask_off() -> planner.AskPlanner:
            self.write_config(CONFIG.replace("enabled = true", "enabled = false", 1))
            return self.make()

        def signed_out() -> planner.AskPlanner:
            self.write_config(CONFIG)
            return self.make(auth=json.dumps({"loggedIn": False}))

        def capped() -> planner.AskPlanner:                      # the Ask caps: a desktop run is an Ask run
            self.write_config(CONFIG)
            made = self.make()
            for _ in range(20):
                self.usage.start(NOW - timedelta(minutes=10))
            return made

        for label, make, kind in (("ask off", ask_off, planner.DISABLED), ("signed out", signed_out, "not_signed_in"),
                                  ("caps", capped, planner.CAPS)):
            with self.subTest(label):
                outcome = self.run_desktop(make())
                self.assertEqual((outcome.ok, outcome.kind, outcome.runs, outcome.mode, outcome.target),
                                 (False, kind, 0, MODE_DESKTOP, "d7f3"))
                self.assertEqual(self.runner.started, [])                    # no planner run
                self.assertTrue(outcome.message)

    def test_an_answer_that_does_not_fit_is_refused_whole(self) -> None:
        bad = {"say": "x", "steps": [{"skill": "open_file", "why": "x", "files": ["f1"], "path": "C:\\x.docx"}]}
        ask = self.make(plan_stream(bad))
        outcome = self.run_desktop(ask)
        self.assertEqual((outcome.ok, outcome.kind, outcome.runs), (False, GARBLED, 1))
        self.assertIsNone(outcome.brief)
        self.assertNotIn(live.DESKTOP_VALIDATE, [step.kind for step in self.steps()])

    def test_one_at_a_time_and_only_with_a_job(self) -> None:
        ask = self.make()
        ask._busy.acquire()
        try:
            outcome = self.run_desktop(ask)
        finally:
            ask._busy.release()
        self.assertEqual((outcome.kind, outcome.mode, outcome.target), ("busy", MODE_DESKTOP, "d7f3"))
        no_job = ModeRequest(MODE_DESKTOP, ORIGIN_TYPED, typed=TYPED, typed_route=True)
        outcome = self.run_desktop(ask, req=no_job)
        self.assertEqual((outcome.ok, outcome.kind, outcome.runs), (False, "error", 0))
        self.assertEqual(self.runner.started, [])

    def test_the_engine_never_lets_a_desktop_run_remember(self) -> None:
        self.make(plan_stream(PLAN))
        result = self.engine.run_mode("<command>\nx\n</command>\n", MODE_DESKTOP, allow_remember=True)
        self.assertIsNone(result.failure)
        self.assertNotIn("remember", self.schema()["properties"])
        self.assertIsInstance(result.answer, dv.DesktopAnswer)


class CarryOutTests(DesktopRunTests):
    """P1.8: the checked plan carried out through the job's carry_out (the desktop thread, which the Ask thread waits
    for): the accepted steps only, the cancel passed on, the run in the outcome, Jarvis's words for it, and Live's
    "Carrying it out" step between the checks and his words - codes and counts, never a name."""

    def job(self, run: object = None, error: Exception | None = None) -> dm.DesktopJob:
        self.carried: list[tuple] = []

        def carry_out(plan: dv.DesktopPlan, words: str, cancel: object) -> object:
            self.carried.append((plan, words, cancel))
            if error is not None:
                raise error
            return run

        return dm.DesktopJob("d7f3", desktop_context(), carry_out=carry_out)

    def test_the_plan_is_carried_out_and_said(self) -> None:
        from briefing_reader.desktop import executor as dx

        run = dx.DesktopRun("d7f3", (dx.StepResult(1, dv.OPEN_FILE, dv.INSTANT, dx.DONE),
                                     dx.StepResult(2, dv.OPEN_APP, dv.INSTANT, dx.FAILED, dx.OS_FAILED, winerror=5)))
        ask = self.make(plan_stream(PLAN))
        stop = threading.Event()
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            outcome = self.run_desktop(ask, req=request(job=self.job(run)), cancel=stop)
        self.assertEqual(len(self.carried), 1)
        plan, words, cancel = self.carried[0]
        self.assertEqual(words, TYPED)
        self.assertEqual([step.skill for step in plan.accepted()], [dv.OPEN_FILE, dv.OPEN_APP])
        self.assertIs(cancel, stop)                                           # the request's own cancel
        self.assertIs(outcome.desktop_run, run)
        self.assertEqual(outcome.message, "Opened the file. Windows couldn't open the app. 2 steps were refused - "
                                          "Live says why, sir.")
        steps = self.steps()
        self.assertEqual([step.kind for step in steps], [live.ASK_CHECKS, live.DESKTOP_FIND, live.PLANNER_RUN,
                                                         live.DESKTOP_VALIDATE, live.DESKTOP_STEP, live.DESKTOP_PLAN])
        carried = steps[4]
        self.assertEqual([item.text for item in carried.items],
                         ["Step 1 - open_file - instant - opened",
                          "Step 2 - open_app - instant - failed: os_error (Windows error 5)"])
        self.assertEqual(carried.summary, "1 opened, 1 failed")
        self.assertEqual(steps[5].summary, "1 opened, 1 failed")
        for name in NAMES:
            self.assertNotIn(name, "\n".join([carried.title, carried.summary,
                                              *(item.text + item.note for item in carried.items)]))
            self.assertNotIn(name, "\n".join(logs.output))

    def test_nothing_accepted_nothing_carried_out(self) -> None:
        ask = self.make(plan_stream({"say": "", "steps": [{"skill": "recycle_files", "why": "x", "files": ["f1"]}]}))
        outcome = self.run_desktop(ask, req=request(job=self.job(None)))
        self.assertEqual(self.carried, [])
        self.assertIsNone(outcome.desktop_run)
        self.assertEqual(outcome.message, desktop_words_all_refused())

    def test_a_carry_out_that_fails_is_said_and_shown(self) -> None:
        ask = self.make(plan_stream(PLAN))
        with self.assertLogs("briefing_reader", level="WARNING") as logs:
            outcome = self.run_desktop(ask, req=request(job=self.job(error=TimeoutError())))
        self.assertTrue(outcome.ok)
        self.assertIsNone(outcome.desktop_run)
        self.assertIn("Nothing was opened yet", outcome.message)              # the plan's own words
        self.assertIn("could not be carried out (TimeoutError)", "\n".join(logs.output))
        carried = [step for step in self.steps() if step.kind == live.DESKTOP_STEP][0]
        self.assertEqual(carried.summary, "it could not be carried out - nothing was opened")
        self.assertEqual(carried.status, live.STATUS_FAILED)


TIDY = ("tidy my Downloads into LING, copy my budget to Downloads, zip my essay into a new Essays folder and "
        "rename my reading list")
FILES_PLAN = {"say": "I have lined up your files.",
              "steps": [{"skill": "move_files", "why": "the old draft", "files": ["f8"], "to": "d3"},
                        {"skill": "copy_files", "why": "the budget", "files": ["f3"], "to": "d4"},
                        {"skill": "make_folder", "why": "for the essay", "id": "n1", "place": "d4", "name": "Essays"},
                        {"skill": "zip_files", "why": "the essay", "files": ["f1"], "name": "LING essay.zip",
                         "to": "n1"},
                        {"skill": "rename_file", "why": "the list", "files": ["f2"], "name": "Reading list.pdf"}]}


class FileSkillsTests(DesktopRunTests):
    """P2.2: the planner's fakes give a good move, copy, make_folder, zip and rename answer (every step a NEEDS YOUR
    OK card, nothing changed) and malformed ones (refused whole, or step by step with their codes). No real run."""

    def test_a_good_answer_is_a_card_and_nothing_changes(self) -> None:
        ask = self.make(plan_stream(FILES_PLAN))
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            outcome = self.run_desktop(ask, TIDY, req=request(TIDY))
        self.assertTrue(outcome.ok, outcome)
        plan = outcome.brief
        self.assertEqual([(step.skill, step.code or step.tier) for step in plan.steps],
                         [("move_files", dv.CARD), ("copy_files", dv.CARD), ("make_folder", dv.CARD),
                          ("zip_files", dv.CARD), ("rename_file", dv.CARD)])
        self.assertEqual(outcome.message, "Understood: move files, copy files, rename a file, make a folder and make "
                                          "a zip. Nothing was changed yet, sir.")
        checked = [step for step in self.steps() if step.kind == live.DESKTOP_VALIDATE][0]
        self.assertEqual([item.text for item in checked.items],
                         ["Step 1 - move_files - card", "Step 2 - copy_files - card", "Step 3 - make_folder - card",
                          "Step 4 - zip_files - card", "Step 5 - rename_file - card (online_only)"])
        text = "\n".join(logs.output)
        self.assertIn("plan checked, steps=5 (move_files=1, copy_files=1, make_folder=1, zip_files=1, rename_file=1), "
                      "refused=0", text)
        for name in (*NAMES, TIDY, "Essays", "Reading list"):
            self.assertNotIn(name, text)

    def test_the_card_is_made_on_the_desktop_thread_and_nothing_is_moved(self) -> None:
        import tempfile

        from briefing_reader.desktop import executor as dx
        from briefing_reader.desktop import paths
        from tests.desktop_fakes import desktop_disk

        with tempfile.TemporaryDirectory() as tmp:
            fake = desktop_disk(tmp)
            scope = paths.load_scope(["documents", "downloads"], [], fake)
            executor = dx.Executor(fake, scope=lambda: scope, apps=lambda: ())

            def carry_out(plan: dv.DesktopPlan, words: str, cancel: object) -> object:
                return executor.run_plan(plan, words=words, cancel=cancel)

            job = dm.DesktopJob("d7f3", desktop_context(backend=fake), carry_out=carry_out)
            ask = self.make(plan_stream(FILES_PLAN))
            outcome = self.run_desktop(ask, TIDY, req=request(TIDY, job=job))
            run = outcome.desktop_run
            self.assertEqual([(result.skill, result.tier, result.status) for result in run.results],
                             [(step["skill"], dv.CARD, dx.WAITING) for step in FILES_PLAN["steps"]])
            card = run.card
            self.assertIsNotNone(card)
            self.assertEqual(card.verify(card.made_at), "")
            self.assertEqual([(row.kind, row.code, row.ticked) for row in card.rows],
                             [("move", "", True), ("copy", "", True), ("mkdir", "", True), ("zip", "", True),
                              ("rename", "", True)])
            self.assertIn("to_onedrive", card.rows[0].flags)                   # Downloads into LING (OneDrive)
            self.assertEqual(outcome.message, "5 steps need your OK first - nothing has changed yet, sir.")
            changed = [call for call in fake.calls if call[0] in ("move_item", "copy_file", "make_folder",
                                                                  "remove_folder", "write_zip")]
            self.assertEqual(changed, [])                                    # nothing until the owner's click
            self.assertFalse(fake.opened)

    def test_malformed_answers_are_refused_whole(self) -> None:
        good = FILES_PLAN["steps"][0]
        bad = [
            {"say": "x", "steps": [dict(good, to="C:\\Users\\Owner\\Documents")]},          # a path, not a ref
            {"say": "x", "steps": [dict(good, dest="d3")]},                                  # a key outside 4.3
            {"say": "x", "steps": [dict(good, files="f8")]},                                 # not a list
            {"say": "x", "steps": [dict(FILES_PLAN["steps"][4], name="x" * 121)]},           # over 120
            {"say": "x", "steps": [dict(good, to=3)]},                                       # not a string
            {"say": "x", "steps": [good] * 13},                                              # 13 steps
        ]
        for value in bad:
            with self.subTest(value=str(value)[:70]):
                ask = self.make(plan_stream(value))
                outcome = self.run_desktop(ask, TIDY, req=request(TIDY))
                self.assertEqual((outcome.ok, outcome.kind, outcome.runs), (False, GARBLED, 1))
                self.assertIsNone(outcome.brief)

    def test_malformed_steps_are_refused_one_by_one(self) -> None:
        steps = [
            ({"skill": "move_files", "why": "x", "files": ["f8"]}, dv.SHAPE),                  # no "to"
            ({"skill": "move_files", "why": "x", "files": ["f8"], "to": "n4"}, dv.UNKNOWN_REF),  # never made
            ({"skill": "copy_files", "why": "x", "files": ["f3"], "to": "d9"}, dv.UNKNOWN_REF),
            ({"skill": "rename_file", "why": "x", "files": ["f1"], "name": "..\\Essay.docx"}, dv.NAME_INVALID),
            ({"skill": "rename_file", "why": "x", "files": ["f1"], "name": "Essay.exe"}, dv.BLOCKED_TYPE),
            ({"skill": "make_folder", "why": "x", "id": "d1", "place": "d4", "name": "Old"}, dv.UNKNOWN_REF),
            ({"skill": "make_folder", "why": "x", "id": "n1", "place": "d4", "name": "node_modules"}, dv.EXCLUDED),
            ({"skill": "zip_files", "why": "x", "files": ["f1"], "name": "Essay.rar"}, dv.NAME_INVALID),
            ({"skill": "zip_files", "why": "x", "files": ["f6"], "name": "Setup.zip"}, dv.BLOCKED_TYPE),
            ({"skill": "copy_files", "why": "x", "files": ["f5"], "to": "d1"}, dv.BLOCKED_TYPE),
            ({"skill": "move_files", "why": "x", "files": ["f7"], "to": "d3"}, dv.SUSPECT_ITEM),
            ({"skill": "recycle_files", "why": "x", "files": ["f8"]}, dv.INTENT_NOT_LICENSED),
        ]
        ask = self.make(plan_stream({"say": "x", "steps": [step for step, _code in steps]}))
        outcome = self.run_desktop(ask, TIDY, req=request(TIDY))
        self.assertTrue(outcome.ok, outcome)
        self.assertEqual([step.code for step in outcome.brief.steps], [code for _step, code in steps])
        self.assertEqual(outcome.message, desktop_words_all_refused())


def desktop_words_all_refused() -> str:
    return "I couldn't check any of that - Live says why, sir. Nothing was opened."


class DryRunTests(PlannerTestCase):
    """--desktop-dry-run: what a typed desktop request would do - the local route, or the stdin its one planner run
    would get (an invented sample context) and its command line; nothing is run, started, opened or counted."""

    def dry(self, text: str, **kwargs) -> tuple[int, str]:  # type: ignore[no-untyped-def]
        out = io.StringIO()
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("started a process")), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("ran a process")), \
                mock.patch.object(cli, "locate_claude", side_effect=AssertionError("looked Claude Code up")), \
                mock.patch.object(cli, "probe", side_effect=AssertionError("asked Claude Code for --version")), \
                mock.patch.object(cli, "auth_status", side_effect=AssertionError("asked for the sign-in")):
            code = commands.desktop_dry_run(text, self.config, out, now=NOW, zone="Europe/London", **kwargs)
        return code, out.getvalue()

    def test_the_stdin_and_the_command_line_and_nothing_run(self) -> None:
        code, text = self.dry("open my LING essay")
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith(commands.DESKTOP_DRY_RUN_HEADER))
        stdin = text.split("---- stdin ----\n", 1)[1].split("---- end of stdin ----\n", 1)[0]
        made = dm.build_prompt(NOW, ModeRequest(MODE_DESKTOP, ORIGIN_TYPED, typed="open my LING essay",
                                                typed_route=True), commands.sample_context(), zone="Europe/London",
                               run_id=commands.DESKTOP_RUN_ID)
        self.assertEqual(stdin, made.text)                                # exactly what the run would get
        self.assertIn("<command>\n" + FIXED_COMMANDS[MODE_DESKTOP] + "\n", stdin)
        self.assertIn("The owner's words: open my LING essay\n</command>", stdin)
        self.assertIn("claude.exe", text)
        self.assertIn(str(cli.DESKTOP_PROMPT_FILE), text)
        self.assertIn("--tools", text)
        self.assertIn("Context: places 4, files 4, apps 2, sites 1, files_left_out 0, apps_left_out 0, suspect names 1",
                      text)
        self.assertNotIn("C:\\", stdin)                                   # names only, even in the sample
        self.assertFalse((self.config.data_dir / "ask_usage.json").exists())   # nothing counted

    def test_a_local_route_and_not_a_desktop_request(self) -> None:
        code, text = self.dry("open Downloads")
        self.assertEqual(code, 0)
        self.assertIn("Handled on this PC (no planner run): open_folder, instant\n", text)
        self.assertIn("Jarvis would say: Understood: open your Downloads folder. Nothing was opened yet, sir.", text)
        self.assertNotIn("---- stdin ----", text)
        code, text = self.dry("volume 30")
        self.assertIn("Handled on this PC (no planner run): volume, refused: not_yet\n", text)
        code, text = self.dry("what's on today?")
        self.assertEqual((code, text), (2, commands.NOT_DESKTOP + "\n"))

    def test_the_notes_when_desktop_or_ask_is_off(self) -> None:
        self.write_config(CONFIG.replace("enabled = true", "enabled = false", 1))
        code, text = self.dry("open my LING essay")
        self.assertEqual(code, 0)
        self.assertIn("Note: Jarvis on your desktop is off in the app", text)
        self.assertIn("Note: Ask is off ([ask] enabled = false): the app would not run the planner", text)
        self.write_config(CONFIG + "\n[desktop]\nenabled = true\n")
        code, text = self.dry("open my LING essay")
        self.assertNotIn("Jarvis on your desktop is off", text)

    def test_the_command_line_entry(self) -> None:
        parser = entry.build_parser()
        self.assertEqual(parser.parse_args(["--desktop-dry-run", "open x"]).desktop_dry_run, "open x")
        self.assertIsNone(parser.parse_args([]).desktop_dry_run)
        for argv in (["--desktop-dry-run", "x", "--ask-check"], ["--desktop-dry-run", "x", "--plan-dry-run", "y"],
                     ["--desktop-dry-run"]):
            with self.subTest(argv=argv):
                with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                    parser.parse_args(argv)
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            entry.main(["--desktop-dry-run", "x", "--open"])
        with mock.patch("briefing_reader.config.setup_logging", return_value=Path("x.log")), \
                mock.patch("briefing_reader.config.load_config", return_value=self.config), \
                mock.patch("briefing_reader.ask.commands.run", return_value=0) as run, \
                mock.patch.object(entry, "_claim_single_instance") as claim, \
                mock.patch.object(entry, "_run_app") as app, \
                self.assertLogs("briefing_reader", level="INFO") as logs:
            self.assertEqual(entry.main(["--desktop-dry-run", "open SECRET-DESKTOP-WORDS"]), 0)
        (args, _config, _out), _ = run.call_args
        self.assertEqual(args.desktop_dry_run, "open SECRET-DESKTOP-WORDS")
        claim.assert_not_called()
        app.assert_not_called()
        self.assertIn("desktop_dry_run=True", "\n".join(logs.output))
        self.assertNotIn("SECRET-DESKTOP-WORDS", "\n".join(logs.output))
        out = io.StringIO()
        with mock.patch.object(commands, "desktop_dry_run", return_value=0) as dry:
            self.assertEqual(commands.run(SimpleNamespace(ask_check=False, followups_check=False,
                                                          desktop_dry_run="open x", plan_dry_run=None,
                                                          ask_text=None), self.config, out), 0)
        dry.assert_called_once()


if __name__ == "__main__":
    unittest.main()
