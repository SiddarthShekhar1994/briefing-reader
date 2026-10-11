"""Tests for briefing_reader.__main__: --slots, --catch-up, the hand-off and the hotkey agent.

Nothing here shows a window, plays sound, claims the app's real lock or
touches the real profile: the catch-up decision is tested with the lock
check replaced, and the end-to-end catch-up runs in a separate Python with
LOCALAPPDATA pointing into a temporary folder and .env loading switched off
(only the "nothing is due" paths, which end before any window could open).
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import briefing_reader
import briefing_reader.__main__ as entry
from briefing_reader import hotkey
from briefing_reader.config import load_config
from briefing_reader.runstate import RUNSTATE_FILE, RunState, slot_for

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGGER = "briefing_reader.main"
SLOTS = {"AM": "10:12", "PM": "23:42"}


class ArgumentTests(unittest.TestCase):
    def test_modes_are_mutually_exclusive(self) -> None:
        parser = entry.build_parser()
        args = parser.parse_args(["--catch-up", "--slots", "am=10:12,pm=23:42"])
        self.assertTrue(args.catch_up)
        self.assertEqual(args.slots, "am=10:12,pm=23:42")
        self.assertTrue(parser.parse_args(["--hotkey-agent"]).hotkey_agent)
        self.assertEqual(parser.parse_args(["--run", "PM"]).run, "pm")
        for argv in (["--run", "am", "--catch-up"], ["--catch-up", "--hotkey-agent"]):
            with self.subTest(argv=argv):
                with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                    parser.parse_args(argv)

    def test_open_now_and_read(self) -> None:
        parser = entry.build_parser()
        self.assertTrue(parser.parse_args(["--open"]).open)
        self.assertTrue(parser.parse_args(["--read"]).read)
        self.assertTrue(parser.parse_args(["--now"]).now)
        args = parser.parse_args(["--run", "am", "--open", "--slots", "am=10:12"])
        self.assertEqual((args.run, args.open), ("am", True))
        self.assertTrue(parser.parse_args(["--ask", "--open"]).open)
        for argv in (["--open", "--read"], ["--now", "--read"], ["--open", "--now"]):
            with self.subTest(argv=argv):
                with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                    parser.parse_args(argv)
        for argv in (["--open", "--catch-up"], ["--read", "--hotkey-agent"], ["--now", "--ask-check"],
                     ["--open", "--ask-text", "x"], ["--read", "--ask"]):
            with self.subTest(argv=argv):
                with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                    entry.main(argv)
        help_text = " ".join(parser.format_help().split())
        self.assertIn("Without options Jarvis opens with a greeting; the briefing waits in its BRIEFING tab.",
                      help_text)
        self.assertIn("older name of --open", help_text)
        self.assertIn("read the briefing aloud at once (what --now did before)", help_text)
        self.assertTrue(parser.format_help().isascii())

    def test_product_name_and_unchanged_command(self) -> None:
        # The visible name is Jarvis Assistant; the command and the internal names stay briefing_reader.
        parser = entry.build_parser()
        help_text = " ".join(parser.format_help().split())
        self.assertTrue(help_text.startswith("usage: py -3.13 -m briefing_reader "))
        self.assertIn("Jarvis Assistant: a personal AI desktop assistant for Windows", help_text)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out, self.assertRaises(SystemExit):
            parser.parse_args(["--version"])
        self.assertEqual(out.getvalue().strip(), f"Jarvis Assistant {briefing_reader.__version__}")
        self.assertEqual(briefing_reader.APP_NAME, "briefing-reader")   # the data folder's name never changes

    def test_launch_kind(self) -> None:
        parser = entry.build_parser()
        cases = {(): "open", ("--open",): "open", ("--now",): "open", ("--ask",): "open", ("--read",): "read",
                 ("--run", "am"): "scheduled", ("--catch-up",): "scheduled", ("--run", "pm", "--open"): "open",
                 ("--run", "pm", "--read"): "read", ("--run", "pm", "--now"): "open"}
        for argv, kind in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(entry.launch_kind(parser.parse_args(list(argv))), kind)

    def test_ask_arguments(self) -> None:
        parser = entry.build_parser()
        self.assertTrue(parser.parse_args(["--ask-check"]).ask_check)
        args = parser.parse_args(["--ask-text", "move my sync", "--ask-dry-run"])
        self.assertEqual((args.ask_text, args.ask_dry_run), ("move my sync", True))
        for argv in (["--ask-check", "--run", "am"], ["--ask-text", "x", "--hotkey-agent"],
                     ["--ask-check", "--ask-text", "x"]):
            with self.subTest(argv=argv):
                with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                    parser.parse_args(argv)
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            entry.main(["--ask-dry-run"])   # needs --ask-text

    def test_ask_modes_run_without_qt_or_the_lock_and_never_log_the_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(Path(tmp), environ={"LOCALAPPDATA": tmp})
        for argv in (["--ask-check"], ["--ask-text", "SECRET-REQUEST-TEXT", "--ask-dry-run"],
                     ["--ask-text", "SECRET-REQUEST-TEXT"]):
            with self.subTest(argv=argv):
                with mock.patch("briefing_reader.config.setup_logging", return_value=Path("x.log")), \
                        mock.patch("briefing_reader.config.load_config", return_value=config), \
                        mock.patch("briefing_reader.ask.commands.run", return_value=0) as run, \
                        mock.patch.object(entry, "_claim_single_instance") as claim, \
                        mock.patch.object(entry, "_run_app") as app, \
                        self.assertLogs("briefing_reader", level="INFO") as logs:
                    self.assertEqual(entry.main(argv), 0)
                (args, passed_config, _out), _ = run.call_args
                self.assertIs(passed_config, config)
                self.assertEqual(args.ask_check, argv == ["--ask-check"])
                claim.assert_not_called()
                app.assert_not_called()
                self.assertNotIn("SECRET-REQUEST-TEXT", "\n".join(logs.output))

    def test_plan_dry_run_argument(self) -> None:
        """The day plan's dry run: an optional request, its own mode, no Qt, no lock, never the text in the log."""
        parser = entry.build_parser()
        self.assertEqual(parser.parse_args(["--plan-dry-run"]).plan_dry_run, "")
        self.assertEqual(parser.parse_args(["--plan-dry-run", "plan tomorrow"]).plan_dry_run, "plan tomorrow")
        self.assertIsNone(parser.parse_args([]).plan_dry_run)
        for argv in (["--plan-dry-run", "--ask-check"], ["--plan-dry-run", "x", "--run", "am"]):
            with self.subTest(argv=argv):
                with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                    parser.parse_args(argv)
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            entry.main(["--plan-dry-run", "--open"])
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(Path(tmp), environ={"LOCALAPPDATA": tmp})
        with mock.patch("briefing_reader.config.setup_logging", return_value=Path("x.log")), \
                mock.patch("briefing_reader.config.load_config", return_value=config), \
                mock.patch("briefing_reader.ask.commands.run", return_value=0) as run, \
                mock.patch.object(entry, "_claim_single_instance") as claim, \
                self.assertLogs("briefing_reader", level="INFO") as logs:
            self.assertEqual(entry.main(["--plan-dry-run", "plan my day, SECRET-PLAN-WORDS"]), 0)
        (args, _config, _out), _ = run.call_args
        self.assertEqual(args.plan_dry_run, "plan my day, SECRET-PLAN-WORDS")
        claim.assert_not_called()
        self.assertNotIn("SECRET-PLAN-WORDS", "\n".join(logs.output))

    def test_ask_start_mode(self) -> None:
        parser = entry.build_parser()
        args = parser.parse_args(["--ask"])
        self.assertTrue(args.ask)
        self.assertFalse(args.ask_check or args.ask_text)
        self.assertFalse(parser.parse_args([]).ask)
        for argv in (["--ask", "--run", "am"], ["--ask", "--catch-up"], ["--ask", "--hotkey-agent"],
                     ["--ask", "--ask-check"], ["--ask", "--ask-text", "x"]):
            with self.subTest(argv=argv):
                with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                    parser.parse_args(argv)

    def test_ask_with_the_app_running_asks_it_to_open_the_bar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(Path(tmp), environ={"LOCALAPPDATA": tmp})
        with mock.patch("briefing_reader.config.setup_logging", return_value=Path("x.log")), \
                mock.patch("briefing_reader.config.load_config", return_value=config), \
                mock.patch.object(entry, "_claim_single_instance", return_value=False), \
                mock.patch("briefing_reader.activation.forward_to_running_instance", return_value=0) as forward, \
                mock.patch.object(entry, "_run_app") as app, \
                self.assertLogs("briefing_reader", level="INFO"):
            self.assertEqual(entry.main(["--ask"]), 0)
        app.assert_not_called()
        forward.assert_called_once_with(entry._instance_name(), None, launch="open", ask=True)

    def test_a_launch_forwards_its_kind(self) -> None:
        with mock.patch("briefing_reader.activation.forward_to_running_instance", return_value=0) as forward:
            self.assertEqual(entry._forward_to_running("name", "AM", launch="scheduled"), 0)
            self.assertEqual(entry._forward_to_running("name", None, launch="read"), 0)
        self.assertEqual(forward.call_args_list, [mock.call("name", "AM", launch="scheduled"),
                                                  mock.call("name", None, launch="read")])

    def test_each_launch_forwards_its_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(Path(tmp), environ={"LOCALAPPDATA": tmp})
        cases = [([], "open", False), (["--open"], "open", False), (["--now"], "open", False),
                 (["--read"], "read", False), (["--ask"], "open", True), (["--run", "pm"], "scheduled", False),
                 (["--run", "am", "--open"], "open", False), (["--run", "am", "--read"], "read", False)]
        for argv, launch, ask in cases:
            with self.subTest(argv=argv):
                with mock.patch("briefing_reader.config.setup_logging", return_value=Path("x.log")), \
                        mock.patch("briefing_reader.config.load_config", return_value=config), \
                        mock.patch.object(entry, "_claim_single_instance", return_value=False), \
                        mock.patch.object(entry, "_should_detach", return_value=False), \
                        mock.patch("briefing_reader.activation.forward_to_running_instance",
                                   return_value=0) as forward, \
                        mock.patch.object(entry, "_run_app") as app, \
                        self.assertLogs("briefing_reader", level="INFO") as logs:
                    self.assertEqual(entry.main(argv), 0)
                app.assert_not_called()
                run = "PM" if "pm" in argv else "AM" if "am" in argv else None
                expected = {"launch": launch, "ask": True} if ask else {"launch": launch}
                forward.assert_called_once_with(entry._instance_name(), run, **expected)
                self.assertTrue(any(f"launch={launch}" in line for line in logs.output))

    def test_the_activation_message_carries_ask_only_when_asked(self) -> None:
        from briefing_reader import activation

        sent: list[bytes] = []

        def fake_send(_name: str, payload: bytes) -> bool:
            sent.append(payload)
            return True

        with mock.patch.object(activation, "try_send", side_effect=fake_send), \
                mock.patch.object(activation, "QCoreApplication"):   # no Qt application for a message
            self.assertEqual(activation.forward_to_running_instance("name", None, False, ask=True), 0)
            self.assertEqual(activation.forward_to_running_instance("name", "PM", True), 0)   # the older form
            self.assertEqual(activation.forward_to_running_instance("name", None, launch="open"), 0)
            self.assertEqual(activation.forward_to_running_instance("name", None, launch="open", ask=True), 0)
            self.assertEqual(activation.forward_to_running_instance("name", "AM", launch="read"), 0)
            self.assertEqual(activation.forward_to_running_instance("name", "PM", launch="scheduled"), 0)
        self.assertEqual(json.loads(sent[0]), {"cmd": "activate", "run": None, "now": False, "ask": True})
        self.assertEqual(json.loads(sent[1]), {"cmd": "activate", "run": "PM", "now": True})
        self.assertEqual(json.loads(sent[2]), {"cmd": "activate", "run": None, "now": False, "open": True})
        self.assertEqual(json.loads(sent[3]), {"cmd": "activate", "run": None, "now": False, "open": True,
                                               "ask": True})
        self.assertEqual(json.loads(sent[4]), {"cmd": "activate", "run": "AM", "now": False, "read": True})
        self.assertEqual(json.loads(sent[5]), {"cmd": "activate", "run": "PM", "now": False})
        self.assertEqual(activation.parse_activation(sent[0])["ask"], True)
        self.assertEqual(activation.parse_activation(sent[2])["open"], True)

    def test_resolve_slots(self) -> None:
        self.assertEqual(entry._resolve_slots(None, SLOTS), SLOTS)
        self.assertEqual(entry._resolve_slots("am=7:30", SLOTS), {"AM": "07:30", "PM": "23:42"})
        self.assertEqual(entry._resolve_slots("am=07:30,pm=18:05", SLOTS), {"AM": "07:30", "PM": "18:05"})
        with self.assertLogs(LOGGER, level="WARNING") as captured:
            self.assertEqual(entry._resolve_slots("am=noon", SLOTS), SLOTS)
        self.assertIn("--slots", "\n".join(captured.output))

    def test_detached_arguments_carry_open_or_read(self) -> None:
        base = {"run": "am", "from_file": None, "debug": False}
        for flags, extra in (({"open": True}, ["--open"]), ({"now": True}, ["--open"]),
                             ({"read": True}, ["--read"]), ({}, [])):
            with self.subTest(flags=flags):
                args = argparse.Namespace(**{"open": False, "now": False, "read": False, **base, **flags})
                self.assertEqual(entry._detached_arguments(args), ["--run", "am", "--detached", *extra])

    def test_detached_arguments_carry_the_slots(self) -> None:
        args = argparse.Namespace(run="pm", now=False, from_file=None, debug=True)
        self.assertEqual(entry._detached_arguments(args, SLOTS),
                         ["--run", "pm", "--detached", "--slots", "am=10:12,pm=23:42", "--debug"])
        self.assertEqual(entry._detached_arguments(args), ["--run", "pm", "--detached", "--debug"])


class CatchUpDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        self.claim = mock.Mock(return_value=True)
        for patcher in (mock.patch.object(entry, "_claim_single_instance", self.claim),
                        mock.patch.object(entry, "_local_now", lambda: self.now)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.now = datetime(2026, 10, 5, 0, 30).astimezone()

    def decide(self) -> tuple[str | None, str]:
        with self.assertLogs(LOGGER, level="INFO") as captured:
            run = entry._catch_up_run(SLOTS, self.data_dir, "briefing-reader-test")
        lines = [line for line in captured.output if "Catch-up:" in line]
        self.assertEqual(len(lines), 1, captured.output)   # exactly one line with the decision
        return run, lines[0]

    def test_unanswered_slot_is_shown_like_run(self) -> None:
        run, line = self.decide()
        self.assertEqual(run, "PM")
        self.assertIn("2026-10-04 PM", line)
        self.assertIn("like --run pm", line)
        self.claim.assert_called_once_with("briefing-reader-test")

    def test_nothing_due(self) -> None:
        self.now = datetime(2026, 10, 5, 9, 0).astimezone()
        run, line = self.decide()
        self.assertIsNone(run)
        self.assertIn("nothing to do", line)
        self.claim.assert_not_called()

    def test_answered_slot_is_not_asked_again(self) -> None:
        RunState(self.data_dir / RUNSTATE_FILE).mark_handled("2026-10-04 PM", "dismissed")
        run, line = self.decide()
        self.assertIsNone(run)
        self.assertIn("already answered (dismissed)", line)
        self.claim.assert_not_called()

    def test_announced_or_viewed_slot_is_left_alone(self) -> None:
        state = RunState(self.data_dir / RUNSTATE_FILE)
        state.mark_announced("2026-10-04 PM")
        run, line = self.decide()
        self.assertIsNone(run)
        self.assertIn("2026-10-04 PM briefing was already announced", line)
        state.mark_viewed("2026-10-04 PM")
        run, line = self.decide()
        self.assertIsNone(run)
        self.assertIn("already viewed", line)
        self.claim.assert_not_called()

    def test_shown_but_unanswered_slot_is_asked_again(self) -> None:
        RunState(self.data_dir / RUNSTATE_FILE).mark_shown("2026-10-04 PM")
        self.assertEqual(self.decide()[0], "PM")

    def test_running_app_is_left_alone(self) -> None:
        self.claim.return_value = False
        run, line = self.decide()
        self.assertIsNone(run)
        self.assertIn("already running", line)

    def test_corrupt_run_state_counts_as_unanswered(self) -> None:
        (self.data_dir / RUNSTATE_FILE).write_text("{oops", encoding="utf-8")
        with self.assertLogs("briefing_reader.runstate", level="WARNING"):
            self.assertEqual(self.decide()[0], "PM")


class DetachTests(unittest.TestCase):
    def test_start_detached_flags_and_fallback(self) -> None:
        child = SimpleNamespace(pid=4242)
        popen = mock.Mock(side_effect=[OSError("no breakaway"), child])
        with mock.patch.object(entry.subprocess, "Popen", popen), \
                self.assertLogs(LOGGER, level="INFO") as captured:
            self.assertTrue(entry._start_detached(["--now"], executable="C:\\py\\pythonw.exe"))
        first, second = (call.kwargs["creationflags"] for call in popen.call_args_list)
        base = (entry.subprocess.DETACHED_PROCESS | entry.subprocess.CREATE_NEW_PROCESS_GROUP
                | entry.subprocess.NORMAL_PRIORITY_CLASS)
        self.assertEqual(first, base | entry.subprocess.CREATE_BREAKAWAY_FROM_JOB)
        self.assertEqual(second, base)
        self.assertEqual(popen.call_args.args[0], ["C:\\py\\pythonw.exe", "-m", "briefing_reader", "--now"])
        self.assertIn("pid 4242", "\n".join(captured.output))

    def test_start_detached_reports_failure(self) -> None:
        with mock.patch.object(entry.subprocess, "Popen", side_effect=OSError("nope")):
            self.assertFalse(entry._start_detached(["--now"]))

    def test_windowless_python_prefers_pythonw(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            python = Path(tmp) / "python.exe"
            python.write_bytes(b"")
            with mock.patch.object(entry.sys, "executable", str(python)):
                self.assertEqual(entry._windowless_python(), str(python))   # no pythonw.exe yet
                (Path(tmp) / "pythonw.exe").write_bytes(b"")
                self.assertEqual(entry._windowless_python(), str(Path(tmp) / "pythonw.exe"))
            other = str(Path(tmp) / "pythonw.exe")
            with mock.patch.object(entry.sys, "executable", other):
                self.assertEqual(entry._windowless_python(), other)


class HotkeyAgentTests(unittest.TestCase):
    def config(self, enabled: bool = True) -> SimpleNamespace:
        return SimpleNamespace(hotkey=SimpleNamespace(enabled=enabled, combo="ctrl+alt+j"))

    def test_disabled_agent_exits_without_listening(self) -> None:
        with mock.patch.object(hotkey, "run_agent") as run_agent, \
                self.assertLogs(LOGGER, level="INFO") as captured:
            self.assertEqual(entry._run_hotkey_agent(self.config(enabled=False), SLOTS), 0)
        run_agent.assert_not_called()
        self.assertIn("enabled = false", "\n".join(captured.output))

    def test_agent_wires_the_dispatcher(self) -> None:
        with mock.patch.object(hotkey, "run_agent", return_value=0) as run_agent:
            self.assertEqual(entry._run_hotkey_agent(self.config(), SLOTS), 0)
        combo, dispatcher = run_agent.call_args.args
        self.assertEqual(combo, "ctrl+alt+j")
        self.assertIsInstance(dispatcher, hotkey.PressDispatcher)
        name = entry._instance_name()
        with mock.patch.object(hotkey, "mutex_exists", return_value=True) as exists, \
                mock.patch.object(hotkey, "send_activation", return_value=True) as send, \
                mock.patch.object(entry, "_start_detached") as start:
            dispatcher._spawn = lambda work: work()
            with self.assertLogs("briefing_reader.hotkey", level="INFO"):
                dispatcher()
        exists.assert_called_once_with(f"Local\\{name}")
        send.assert_called_once_with(name, {"cmd": "activate", "run": None, "now": False, "open": True})
        self.assertEqual(entry.OPEN_ACTIVATION, {"cmd": "activate", "run": None, "now": False, "open": True})
        start.assert_not_called()

    def test_agent_starts_the_app_with_open_and_slots(self) -> None:
        with mock.patch.object(hotkey, "run_agent", return_value=0) as run_agent:
            entry._run_hotkey_agent(self.config(), SLOTS)
        dispatcher = run_agent.call_args.args[1]
        dispatcher._spawn = lambda work: work()
        with mock.patch.object(hotkey, "mutex_exists", return_value=False), \
                mock.patch.object(entry, "_start_detached", return_value=True) as start, \
                mock.patch.object(entry, "_windowless_python", return_value="pythonw.exe"), \
                self.assertLogs("briefing_reader.hotkey", level="INFO"):
            dispatcher()
        start.assert_called_once_with(["--open", "--slots", "am=10:12,pm=23:42"], executable="pythonw.exe")


class SetupErrorTests(unittest.TestCase):
    """A missing NOTION_TOKEN or BRIEFING_PAGE_ID becomes the startup error; nothing is fetched."""

    PAGE = "00112233445566778899aabbccddeeff"   # the fake page of tests/fixtures/fake_page.json
    TOKEN = "ntn_FakeTokenForMainTests0123456789"

    def config(self, token: str = TOKEN, page_id: str = PAGE) -> SimpleNamespace:
        return SimpleNamespace(notion_token=token, page_id=page_id, notion_version="2022-06-28")

    def test_messages(self) -> None:
        self.assertEqual(entry.PAGE_ID_MISSING, "Notion page ID missing. Add BRIEFING_PAGE_ID to the .env file "
                                                "in the project folder (see README).")
        self.assertTrue(entry.TOKEN_MISSING.startswith("Notion token missing. Add NOTION_TOKEN "))
        self.assertTrue(entry.TOKEN_AND_PAGE_ID_MISSING.startswith("Notion token and page ID missing. "))
        for message in (entry.TOKEN_MISSING, entry.PAGE_ID_MISSING, entry.TOKEN_AND_PAGE_ID_MISSING):
            with self.subTest(message=message):
                self.assertIn(". Add ", message)   # headline, then what to do
                self.assertTrue(message.endswith(".env file in the project folder (see README)."))

    def test_setup_error(self) -> None:
        cases = {
            (self.TOKEN, self.PAGE): None,
            (self.TOKEN, ""): entry.PAGE_ID_MISSING,
            (self.TOKEN, "not-a-page-id"): entry.PAGE_ID_MISSING,
            ("", self.PAGE): entry.TOKEN_MISSING,
            ("", ""): entry.TOKEN_AND_PAGE_ID_MISSING,
        }
        for (token, page_id), expected in cases.items():
            with self.subTest(token=token, page_id=page_id):
                self.assertEqual(entry._setup_error(self.config(token, page_id)), expected)

    def test_build_client_reports_a_missing_page_id_and_logs_it(self) -> None:
        args = argparse.Namespace(from_file=None)
        with self.assertLogs(LOGGER, level="ERROR") as captured:
            client, config, error = entry._build_client(args, self.config(page_id=""))
        self.assertEqual(error, entry.PAGE_ID_MISSING)
        self.assertEqual(config.page_id, "")
        self.assertIsNotNone(client)
        self.assertIn("Notion page ID missing", "\n".join(captured.output))

    def test_build_client_without_problems_logs_nothing(self) -> None:
        with self.assertNoLogs(LOGGER, level="ERROR"):
            _client, config, error = entry._build_client(argparse.Namespace(from_file=None), self.config())
        self.assertIsNone(error)
        self.assertEqual(config.page_id, self.PAGE)

    def test_from_file_needs_neither_token_nor_page_id(self) -> None:
        fixture = PROJECT_ROOT / "tests" / "fixtures" / "fake_page.json"
        args = argparse.Namespace(from_file=str(fixture))
        with tempfile.TemporaryDirectory() as tmp:   # a real Config without .env: no token, no page id
            base = load_config(Path(tmp), environ={"LOCALAPPDATA": tmp})
        self.assertEqual((base.notion_token, base.page_id), ("", ""))
        with self.assertLogs(LOGGER, level="INFO") as captured:
            _client, config, error = entry._build_client(args, base)
        self.assertIsNone(error)
        self.assertEqual([r.getMessage() for r in captured.records if r.levelno >= logging.ERROR], [])
        self.assertEqual(config.page_id, self.PAGE)   # the fixture's (fake) page


def _run_isolated(code: str, localappdata: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh Python: LOCALAPPDATA in a temp folder, no .env, no token, no page id."""
    env = {key: value for key, value in os.environ.items() if key not in ("NOTION_TOKEN", "BRIEFING_PAGE_ID")}
    env["LOCALAPPDATA"] = str(localappdata)
    prelude = ("import briefing_reader.config as _config\n"
               "_config._load_dotenv = lambda path: None   # never read the real .env\n")
    return subprocess.run([sys.executable, "-c", prelude + textwrap.dedent(code), *argv],
                          cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60, env=env)


class CatchUpProcessTests(unittest.TestCase):
    """``main(["--catch-up", ...])`` end to end, only where nothing is due."""

    CODE = """
        import json, sys, time
        started = time.monotonic()
        import briefing_reader.__main__ as entry
        rc = entry.main(["--catch-up", "--slots", sys.argv[1]])
        print(json.dumps({"rc": rc, "seconds": time.monotonic() - started,
                          "qt": sorted(m for m in sys.modules if m.startswith("PySide6"))}))
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.localappdata = Path(tmp.name)
        self.data_dir = self.localappdata / "briefing-reader"

    def run_catch_up(self, slots: str) -> dict:
        result = _run_isolated(self.CODE, self.localappdata, slots)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return json.loads(result.stdout.strip().splitlines()[-1])

    def log_text(self) -> str:
        return (self.data_dir / "logs" / "briefing-reader.log").read_text(encoding="utf-8")

    def test_nothing_due_exits_fast_without_qt(self) -> None:
        # Both slots later today: their latest occurrences were about a day ago.
        now = datetime.now()
        slots = (f"am={(now + timedelta(hours=1)).strftime('%H:%M')},"
                 f"pm={(now + timedelta(hours=2)).strftime('%H:%M')}")
        report = self.run_catch_up(slots)
        self.assertEqual(report["rc"], 0)
        self.assertEqual(report["qt"], [])
        self.assertLess(report["seconds"], 2.0)
        log = self.log_text()
        self.assertIn("Catch-up: no scheduled briefing in the last 3 hours", log)
        # No token and no page id: noted in the log, and the catch-up still ends normally.
        self.assertIn("BRIEFING_PAGE_ID is not set", log)
        self.assertIn("Page (not set)", log)
        self.assertNotIn("Traceback", log)

    def test_answered_slot_exits_fast_without_qt(self) -> None:
        now = datetime.now()
        am = (now - timedelta(minutes=30)).strftime("%H:%M")
        pm = (now + timedelta(hours=6)).strftime("%H:%M")
        slot = slot_for(now, {"AM": am, "PM": pm})
        self.assertIsNotNone(slot)
        RunState(self.data_dir / RUNSTATE_FILE).mark_handled(slot.key, "read")
        report = self.run_catch_up(f"am={am},pm={pm}")
        self.assertEqual(report["rc"], 0)
        self.assertEqual(report["qt"], [])
        self.assertLess(report["seconds"], 2.0)
        self.assertIn(f"Catch-up: the {slot.key} briefing was already answered (read)", self.log_text())


class ModuleHygieneTests(unittest.TestCase):
    def test_importing_main_does_not_import_qt_and_old_names_still_resolve(self) -> None:
        code = """
            import sys
            import briefing_reader.__main__ as entry
            assert not any(m.startswith("PySide6") for m in sys.modules), "Qt imported early"
            from briefing_reader import activation
            assert entry.ActivationServer is activation.ActivationServer
            assert entry._forward_to_running_instance is activation.forward_to_running_instance
            assert entry._forward_qt_message is activation.forward_qt_message
            assert entry._try_send is activation.try_send
            try:
                entry.no_such_name
            except AttributeError:
                pass
            else:
                raise AssertionError("missing names must raise AttributeError")
            print("ok")
        """
        with tempfile.TemporaryDirectory() as tmp:
            result = _run_isolated(code, Path(tmp))
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(result.stdout.strip(), "ok")

    def test_sources_are_ascii(self) -> None:
        for name in ("__main__.py", "activation.py"):
            with self.subTest(name=name):
                self.assertTrue((PROJECT_ROOT / "briefing_reader" / name).read_bytes().isascii())


class ExitWatchdogTests(unittest.TestCase):
    """A hung shutdown must not leave a process holding the single-instance lock."""

    def test_fires_after_the_grace_period(self) -> None:
        exits: list[int] = []
        with self.assertLogs("briefing_reader.main", level="WARNING") as logs:
            timer = entry.start_exit_watchdog(0.05, exits.append)
            timer.join(2)
        self.assertEqual(exits, [0])
        self.assertTrue(timer.daemon)
        self.assertIn("did not finish", logs.output[0])

    def test_cancelled_watchdog_does_nothing(self) -> None:
        exits: list[int] = []
        timer = entry.start_exit_watchdog(0.2, exits.append)
        timer.cancel()
        timer.join(1)
        self.assertEqual(exits, [])

    def test_default_grace_outlasts_the_audio_cleanup(self) -> None:
        self.assertGreater(entry.SHUTDOWN_GRACE_S, 15.0)


class RunAppTests(unittest.TestCase):
    """_run_app sets the web engine's Qt attributes before it creates the QApplication."""

    def test_prepare_application_comes_before_the_qapplication(self) -> None:
        import PySide6.QtCore as qt_core
        import PySide6.QtWidgets as qt_widgets

        from briefing_reader import hud, snapshot_engine, ui

        order: list[str] = []
        app = mock.MagicMock(name="app")
        app_class = mock.MagicMock(name="QApplication")
        app_class.instance.side_effect = lambda: order.append("instance")
        app_class.side_effect = lambda argv: order.append("QApplication") or app
        with mock.patch.object(snapshot_engine, "prepare_application", side_effect=lambda: order.append("prepare")), \
                mock.patch.object(qt_widgets, "QApplication", app_class), \
                mock.patch.object(qt_core, "qInstallMessageHandler"), \
                mock.patch.object(entry, "_set_app_user_model_id"), \
                mock.patch.object(entry, "_run_controller", return_value=0) as controller, \
                mock.patch.object(hud, "load_fonts"), \
                mock.patch.object(ui, "apply_dark_theme"), \
                mock.patch.object(ui, "app_icon", return_value=None):
            rc = entry._run_app(argparse.Namespace(), SimpleNamespace(project_root=PROJECT_ROOT), "test", None)
        self.assertEqual(rc, 0)
        self.assertEqual(order, ["prepare", "instance", "QApplication"])
        controller.assert_called_once()
        self.assertIs(controller.call_args.args[4], app)

    def test_prepare_application_never_raises_and_waits_for_no_app(self) -> None:
        from briefing_reader import snapshot_engine

        with mock.patch.object(snapshot_engine, "QCoreApplication") as core:
            core.instance.return_value = None
            core.setAttribute.side_effect = RuntimeError("boom")
            snapshot_engine.prepare_application()
            core.instance.side_effect = RuntimeError("boom")
            snapshot_engine.prepare_application()
        with mock.patch.object(snapshot_engine, "QCoreApplication") as core:
            core.instance.return_value = object()   # too late: Qt would only warn
            snapshot_engine.prepare_application()
            core.setAttribute.assert_not_called()
        with mock.patch.object(snapshot_engine, "QCoreApplication") as core:
            core.instance.return_value = None
            snapshot_engine.prepare_application()
            attributes = [call.args[0] for call in core.setAttribute.call_args_list]
        from PySide6.QtCore import Qt

        self.assertEqual(attributes, [Qt.ApplicationAttribute.AA_ShareOpenGLContexts,
                                      Qt.ApplicationAttribute.AA_DisableShaderDiskCache])


class QtMessageTests(unittest.TestCase):
    """activation.forward_qt_message: a web page's messages never reach the log with their text."""

    MESSAGE = "Uncaught page-marker-41 at https://www.example.org/private?id=7"

    def test_web_page_messages_are_logged_as_their_category_only(self) -> None:
        from PySide6.QtCore import QtMsgType

        from briefing_reader import activation

        for category in ("js", "qt.webenginecontext", "qt.webengine.compositor"):
            for mode in (QtMsgType.QtDebugMsg, QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg):
                with self.subTest(category=category, mode=mode):
                    with self.assertLogs("qt", level="DEBUG") as logs:
                        activation.forward_qt_message(mode, SimpleNamespace(category=category), self.MESSAGE)
                    self.assertEqual([record.levelno for record in logs.records], [logging.DEBUG])
                    self.assertIn(f"[{category}] a web page message (not logged)", logs.output[0])
                    self.assertNotIn("page-marker-41", logs.output[0])
                    self.assertNotIn("example.org", logs.output[0])

    def test_other_qt_messages_are_still_logged(self) -> None:
        from PySide6.QtCore import QtMsgType

        from briefing_reader import activation

        with self.assertLogs("qt", level="WARNING") as logs:
            activation.forward_qt_message(QtMsgType.QtWarningMsg, SimpleNamespace(category="qt.qpa.fonts"),
                                          "a font is missing")
            activation.forward_qt_message(QtMsgType.QtWarningMsg, SimpleNamespace(category="default"),
                                          "Release of profile requested")
        self.assertIn("[qt.qpa.fonts] a font is missing", logs.output[0])
        self.assertIn("Release of profile requested", logs.output[1])
        for category in ("javascript", "qt.web", "jsx"):
            with self.subTest(category=category), self.assertLogs("qt", level="WARNING") as logs:
                activation.forward_qt_message(QtMsgType.QtWarningMsg, SimpleNamespace(category=category), "kept")
            self.assertIn("kept", logs.output[0])


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
