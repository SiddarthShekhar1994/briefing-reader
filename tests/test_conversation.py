"""Tests for briefing_reader.conversation: the last day of the window's conversation kept on this PC.

conversation.json holds the turns of the last 24 hours (at most 100, oldest first), written atomically
(a temporary file, fsync, os.replace) only when they changed; older turns leave it at the start and
whenever it is saved again (prune); Clear conversation deletes it; a missing, corrupt, too deep, too
large or wrong-shaped file is an empty conversation that the next save replaces. Nothing is logged but
counts and exception type names. Every text is invented; files live in a temporary folder.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from briefing_reader import conversation
from briefing_reader.config import PROJECT_ROOT
from briefing_reader.conversation import (CONVERSATION_FILE, KEEP, MAX_TURNS, ROLE_JARVIS, ROLE_YOU, TEXT_CAP,
                                          ConversationStore, Turn, recent)

PDT = timezone(timedelta(hours=-7))
NOW = datetime(2026, 10, 24, 18, 30, tzinfo=PDT)
SECRET = "Zq7conv move the dentist to Friday"


def turn(minutes_ago: float, text: str = "move my 3pm to 4", role: str = ROLE_YOU, tone: str = "") -> Turn:
    return Turn(role, text, NOW - timedelta(minutes=minutes_ago), tone)


class ConversationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "data" / CONVERSATION_FILE
        self.now = NOW

    def store(self) -> ConversationStore:
        return ConversationStore(self.path, clock=lambda: self.now)

    def test_a_missing_file_is_an_empty_conversation_and_nothing_is_made_for_nothing(self) -> None:
        store = self.store()
        self.assertEqual(store.turns(), ())
        self.assertTrue(store.save(()))
        self.assertFalse(self.path.exists())                      # nothing said: no file
        self.assertEqual(store.prune(), 0)
        self.assertFalse(self.path.exists())

    def test_save_and_read_back(self) -> None:
        said = (turn(30), turn(29, "I've lined up the move to 4 PM.", ROLE_JARVIS, "done"),
                turn(5, "and tell Ana", ROLE_YOU))
        store = self.store()
        self.assertTrue(store.save(said))
        again = self.store().turns()
        self.assertEqual(again, said)
        self.assertEqual([item.at for item in again], [item.at for item in said])   # with their offsets
        raw = self.path.read_bytes()
        self.assertTrue(raw.isascii())
        self.assertNotIn(b"\r", raw)
        data = json.loads(raw)
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["turns"][1], {"role": "jarvis", "text": "I've lined up the move to 4 PM.",
                                            "at": "2026-10-24T18:01:00-07:00", "tone": "done"})
        self.assertEqual(set(data), {"version", "turns"})         # the turns only

    def test_a_web_researchs_turns_keep_their_mark(self) -> None:
        store = self.store()
        said = (Turn(ROLE_YOU, "web: museum hours", NOW - timedelta(minutes=3), "", False),
                Turn(ROLE_JARVIS, "It opens at 10 [1].", NOW, "done", False))
        store.save(said)
        rows = json.loads(self.path.read_text(encoding="ascii"))["turns"]
        self.assertEqual([row.get("context") for row in rows], [False, False])
        self.assertEqual([item.context for item in self.store().turns()], [False, False])
        store.save((turn(1, "plain"),))
        self.assertNotIn("context", json.loads(self.path.read_text(encoding="ascii"))["turns"][0])   # the default
        rows = [{"role": "you", "text": "odd", "at": NOW.isoformat(), "context": "no"}]
        self.path.write_text(json.dumps({"version": 1, "turns": rows}), encoding="ascii")
        self.assertEqual(self.store().turns(), ())                   # not a shape Jarvis writes

    def test_non_ascii_text_is_escaped_and_comes_back(self) -> None:
        store = self.store()
        store.save((turn(1, "Caf\u00e9 with Zo\u00eb \u2014 \U0001F600"),))
        self.assertTrue(self.path.read_bytes().isascii())
        self.assertEqual(self.store().turns()[0].text, "Caf\u00e9 with Zo\u00eb \u2014 \U0001F600")

    def test_only_the_last_24_hours(self) -> None:
        old = turn(KEEP.total_seconds() / 60 + 1, "older than a day")
        edge = turn(KEEP.total_seconds() / 60 - 1, "just inside the day")
        store = self.store()
        store.save((old, edge, turn(1)))
        self.assertEqual([item.text for item in self.store().turns()], ["just inside the day", "move my 3pm to 4"])
        on_disk = json.loads(self.path.read_text(encoding="ascii"))["turns"]
        self.assertEqual(len(on_disk), 2)                         # never written

    def test_prune_drops_what_turned_a_day_old_at_the_start_and_later(self) -> None:
        store = self.store()
        store.save((turn(60, "an hour ago"), turn(10, "ten minutes ago")))
        self.now = NOW + KEEP - timedelta(minutes=30)             # the first is now over a day old
        later = self.store()                                      # a start
        self.assertEqual([item.text for item in later.turns()], ["ten minutes ago"])
        self.assertEqual(len(json.loads(self.path.read_text(encoding="ascii"))["turns"]), 2)
        with self.assertLogs("briefing_reader.conversation", level="INFO") as logs:
            self.assertEqual(later.prune(), 1)
        self.assertEqual([item["text"] for item in json.loads(self.path.read_text(encoding="ascii"))["turns"]],
                         ["ten minutes ago"])
        self.assertIn("1 turn(s) older than a day", "\n".join(logs.output))
        self.assertEqual(later.prune(), 0)                        # nothing more to drop: no write
        self.now = NOW + KEEP + timedelta(minutes=30)
        self.assertEqual(later.prune(), 1)
        self.assertEqual(json.loads(self.path.read_text(encoding="ascii"))["turns"], [])

    def test_at_most_100_turns_the_newest_kept(self) -> None:
        said = tuple(turn(500 - number, f"request {number}") for number in range(130))
        self.assertEqual(MAX_TURNS, 100)
        store = self.store()
        store.save(said)
        kept = self.store().turns()
        self.assertEqual(len(kept), 100)
        self.assertEqual((kept[0].text, kept[-1].text), ("request 30", "request 129"))
        self.assertEqual(len(recent(said, NOW)), 100)

    def test_a_long_text_is_cut_to_its_cap(self) -> None:
        self.store().save((turn(1, "x" * (TEXT_CAP + 500)),))
        text = self.store().turns()[0].text
        self.assertEqual(len(text), TEXT_CAP)
        self.assertTrue(text.endswith("..."))

    def test_written_only_when_it_changed(self) -> None:
        store = self.store()
        said = (turn(3), turn(2, "Done.", ROLE_JARVIS))
        store.save(said)
        with mock.patch("briefing_reader.conversation.os.replace") as replaced:
            self.assertTrue(store.save(said))
            replaced.assert_not_called()
            self.assertTrue(store.save(said + (turn(1, "thanks"),)))
            replaced.assert_called_once()

    def test_the_write_is_atomic_and_a_failure_keeps_the_old_file(self) -> None:
        store = self.store()
        store.save((turn(3, "first"),))
        before = self.path.read_bytes()
        with mock.patch("briefing_reader.conversation.os.replace", side_effect=OSError("disk full")), \
                self.assertLogs("briefing_reader.conversation", level="WARNING") as logs:
            self.assertFalse(store.save((turn(3, "first"), turn(1, SECRET))))
        self.assertFalse(store.saved)
        self.assertEqual(self.path.read_bytes(), before)          # the old file whole
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])   # no half-written file left
        self.assertNotIn("Zq7conv", "\n".join(logs.output))
        self.assertIn("OSError", "\n".join(logs.output))
        self.assertTrue(store.save((turn(3, "first"), turn(1, "second"))))
        self.assertTrue(store.saved)
        with mock.patch("briefing_reader.conversation.os.fsync") as synced:
            store.save((turn(1, "third"),))
            synced.assert_called_once()                           # flushed to disk before the replace

    def test_clear_deletes_the_file(self) -> None:
        store = self.store()
        store.save((turn(2), turn(1, "Done.", ROLE_JARVIS)))
        self.assertTrue(store.clear())
        self.assertFalse(self.path.exists())
        self.assertEqual(store.turns(), ())
        self.assertTrue(store.save(()))                           # nothing said since: none made again
        self.assertFalse(self.path.exists())
        self.assertTrue(store.clear())                            # no file: fine
        with mock.patch("briefing_reader.conversation.os.unlink", side_effect=PermissionError("locked")), \
                self.assertLogs("briefing_reader.conversation", level="WARNING"):
            store.save((turn(1, SECRET),))
            self.assertFalse(store.clear())
        self.assertEqual(json.loads(self.path.read_text(encoding="ascii"))["turns"], [])   # emptied instead

    def test_a_bad_file_is_empty_logged_without_its_content_and_replaced(self) -> None:
        cases = {"not json": b"{zq7conv broken",
                 "wrong version": json.dumps({"version": 2, "turns": []}).encode(),
                 "not a list": json.dumps({"version": 1, "turns": {"a": 1}}).encode(),
                 "too deep": b'{"version": 1, "turns": ' + b"[" * 100_000 + b"]" * 100_000 + b"}",
                 "not utf-8": b"\xff\xfe\x00zq7conv"}
        for name, raw in cases.items():
            with self.subTest(name):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_bytes(raw)
                with self.assertLogs("briefing_reader.conversation", level="WARNING") as logs:
                    store = self.store()
                self.assertEqual(store.turns(), ())
                self.assertNotIn("zq7conv", "\n".join(logs.output).casefold())
                self.assertTrue(store.save((turn(1, "a new start"),)))
                self.assertEqual(json.loads(self.path.read_text(encoding="ascii"))["turns"][0]["text"], "a new start")
        with mock.patch.object(conversation, "MAX_FILE_BYTES", 100):
            self.path.write_bytes(json.dumps({"version": 1, "turns": [{"role": "you", "text": "x" * 200,
                                                                        "at": NOW.isoformat()}]}).encode())
            with self.assertLogs("briefing_reader.conversation", level="WARNING"):
                self.assertEqual(self.store().turns(), ())

    def test_turns_in_a_shape_jarvis_never_writes_are_left_out(self) -> None:
        good = {"role": "you", "text": "keep me", "at": (NOW - timedelta(minutes=5)).isoformat(), "tone": ""}
        bad = [{"role": "system", "text": "x", "at": NOW.isoformat()},
               {"role": "you", "text": "", "at": NOW.isoformat()},
               {"role": "you", "text": "naive", "at": "2026-10-24T10:00:00"},
               {"role": "you", "text": "no time"},
               {"role": "jarvis", "text": "bad tone", "at": NOW.isoformat(), "tone": "<b>"},
               "just a string"]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"version": 1, "turns": [*bad, good]}), encoding="utf-8")
        self.assertEqual([item.text for item in self.store().turns()], ["keep me"])

    def test_reprs_and_logs_never_hold_a_text(self) -> None:
        self.assertNotIn("Zq7conv", repr(turn(1, SECRET)))
        with self.assertLogs("briefing_reader", level="DEBUG") as logs:
            store = self.store()
            store.save((turn(60 * 25, SECRET), turn(1, SECRET)))
            self.now = NOW + KEEP
            store.prune()
            store.clear()
            logging_marker()
        self.assertNotIn("Zq7conv", "\n".join(logs.output))


def logging_marker() -> None:
    """One log line, so assertLogs has something to see even when the store logs nothing else."""
    import logging

    logging.getLogger("briefing_reader.conversation").debug("marker")


class HygieneTests(unittest.TestCase):
    def test_qt_free_ascii_lf(self) -> None:
        code = ("import sys, briefing_reader.conversation; "
                "assert not any(m.startswith('PySide6') for m in sys.modules)")
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=PROJECT_ROOT, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        raw = Path(conversation.__file__).read_bytes()
        self.assertTrue(raw.isascii())
        self.assertNotIn(b"\r", raw)

    def test_roles_are_the_windows_own(self) -> None:
        from briefing_reader import hud

        self.assertEqual((ROLE_YOU, ROLE_JARVIS), (hud.ROLE_YOU, hud.ROLE_JARVIS))


if __name__ == "__main__":
    unittest.main()
