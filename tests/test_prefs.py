"""Tests for briefing_reader.prefs: assistant.json (mute, the recent greetings, answers adopted).

Every test writes into a temporary folder; the real
%LOCALAPPDATA%\\briefing-reader\\assistant.json is never read or written.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from briefing_reader import prefs
from briefing_reader.prefs import PREFS_FILE, RECENT_GREETINGS_MAX, AssistantPrefs

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGGER = "briefing_reader.prefs"


class PrefsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "data"
        self.path = self.dir / PREFS_FILE

    def saved(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write_raw(self, text: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")


class DefaultsTests(PrefsTestCase):
    def test_missing_file_gives_defaults_and_writes_nothing(self) -> None:
        state = AssistantPrefs(self.path)
        self.assertFalse(state.muted)
        self.assertEqual(state.recent_greetings, [])
        self.assertFalse(self.path.exists())

    def test_corrupt_or_wrong_shapes_give_defaults(self) -> None:
        for text in ("{oops", "[]", '"x"', "null", "3"):
            with self.subTest(text=text):
                self.write_raw(text)
                with self.assertLogs(LOGGER, level="WARNING"):
                    state = AssistantPrefs(self.path)
                self.assertFalse(state.muted)
                self.assertEqual(state.recent_greetings, [])

    def test_bad_fields_are_ignored(self) -> None:
        self.write_raw(json.dumps({"muted": "yes", "recent_greetings": ["m1", 3, "", "Hello, sir.", "n2"]}))
        state = AssistantPrefs(self.path)
        self.assertFalse(state.muted)                       # only a real true mutes
        self.assertEqual(state.recent_greetings, ["m1", "n2"])   # never any text, only ids

    def test_unreadable_path_never_crashes(self) -> None:
        self.path.mkdir(parents=True)   # a folder where the file should be
        with self.assertLogs(LOGGER, level="WARNING"):
            state = AssistantPrefs(self.path)
        with self.assertLogs(LOGGER, level="WARNING") as captured:
            self.assertFalse(state.set_muted(True))
        self.assertIn("kept for this run only", "\n".join(captured.output))
        self.assertTrue(state.muted)                        # remembered in memory
        with self.assertLogs(LOGGER, level="WARNING"):
            self.assertFalse(state.remember_greeting("m1"))
        self.assertEqual(state.recent_greetings, ["m1"])


class RoundTripTests(PrefsTestCase):
    def test_muted_round_trip(self) -> None:
        self.assertTrue(AssistantPrefs(self.path).set_muted(True))
        self.assertEqual(self.saved(), {"muted": True, "recent_greetings": []})
        self.assertTrue(AssistantPrefs(self.path).muted)
        AssistantPrefs(self.path).set_muted(False)
        self.assertFalse(AssistantPrefs(self.path).muted)
        self.assertEqual(list(self.dir.glob("*.tmp")), [])

    def test_recent_greetings_are_capped_at_five_oldest_first(self) -> None:
        state = AssistantPrefs(self.path)
        for greeting in ("m1", "m2", "n1", "a1", "e1", "l1", "n2"):
            state.remember_greeting(greeting)
        self.assertEqual(RECENT_GREETINGS_MAX, 5)
        self.assertEqual(state.recent_greetings, ["n1", "a1", "e1", "l1", "n2"])
        self.assertEqual(AssistantPrefs(self.path).recent_greetings, ["n1", "a1", "e1", "l1", "n2"])

    def test_a_repeat_moves_to_the_end(self) -> None:
        state = AssistantPrefs(self.path)
        for greeting in ("m1", "m2", "m1"):
            state.remember_greeting(greeting)
        self.assertEqual(state.recent_greetings, ["m2", "m1"])

    def test_malformed_ids_are_not_stored(self) -> None:
        state = AssistantPrefs(self.path)
        for bad in ("", "Good morning, sir.", "M1", None, 3):
            with self.subTest(bad=bad):
                self.assertFalse(state.remember_greeting(bad))  # type: ignore[arg-type]
        self.assertEqual(state.recent_greetings, [])
        self.assertFalse(self.path.exists())

    def test_mute_and_greetings_keep_each_other(self) -> None:
        AssistantPrefs(self.path).remember_greeting("e2")
        AssistantPrefs(self.path).set_muted(True)
        self.assertEqual(self.saved(), {"muted": True, "recent_greetings": ["e2"]})

    def test_answers_adopted_round_trip(self) -> None:
        state = AssistantPrefs(self.path)
        self.assertFalse(state.answers_adopted)
        state.remember_greeting("m1")
        self.assertEqual(self.saved(), {"muted": False, "recent_greetings": ["m1"]})   # absent until set
        self.assertTrue(state.set_answers_adopted())
        self.assertEqual(self.saved(), {"muted": False, "recent_greetings": ["m1"], "answers_adopted": True})
        again = AssistantPrefs(self.path)
        self.assertTrue(again.answers_adopted)
        again.set_muted(True)
        self.assertTrue(AssistantPrefs(self.path).answers_adopted)                  # kept by other writes
        self.write_raw(json.dumps({"answers_adopted": "yes"}))
        self.assertFalse(AssistantPrefs(self.path).answers_adopted)                 # only a real true

    def test_live_window_round_trip(self) -> None:
        state = AssistantPrefs(self.path)
        self.assertIsNone(state.live_window)
        self.assertFalse(state.live_popped_out)
        state.remember_greeting("m1")
        self.assertNotIn("live_window", self.saved())                               # absent until used
        self.assertTrue(state.set_live_window((1930, -12, 520, 680), True))
        self.assertEqual(self.saved(), {"muted": False, "recent_greetings": ["m1"],
                                        "live_window": {"x": 1930, "y": -12, "w": 520, "h": 680},
                                        "live_popped_out": True})
        again = AssistantPrefs(self.path)
        self.assertEqual((again.live_window, again.live_popped_out), ((1930, -12, 520, 680), True))
        again.set_muted(True)
        self.assertEqual(AssistantPrefs(self.path).live_window, (1930, -12, 520, 680))   # kept by other writes
        again.set_live_window((10, 20, 400, 500), False)
        self.assertEqual(self.saved()["live_popped_out"], False)

    def test_bad_live_window_values_are_ignored(self) -> None:
        for value in ({"x": 1, "y": 2, "w": 0, "h": 5}, {"x": "1", "y": 2, "w": 3, "h": 4}, [1, 2, 3, 4],
                      {"x": True, "y": 2, "w": 3, "h": 4}, {"x": 10 ** 9, "y": 2, "w": 3, "h": 4}, "big"):
            with self.subTest(value=value):
                self.write_raw(json.dumps({"live_window": value, "live_popped_out": "yes"}))
                state = AssistantPrefs(self.path)
                self.assertIsNone(state.live_window)
                self.assertFalse(state.live_popped_out)                           # only a real true

    def test_prod_hint_shown_round_trip(self) -> None:
        """The productivity bundle's one-time "minimize instead of Done" entry (R34): written only once
        it was shown, kept by the other writes; a boolean, never text."""
        state = AssistantPrefs(self.path)
        self.assertFalse(state.prod_hint_shown)
        state.set_muted(True)
        self.assertNotIn("prod_hint_shown", self.saved())                         # absent until shown
        self.assertTrue(state.set_prod_hint_shown())
        self.assertIs(self.saved()["prod_hint_shown"], True)
        again = AssistantPrefs(self.path)
        self.assertTrue(again.prod_hint_shown)
        again.remember_greeting("m2")
        self.assertTrue(AssistantPrefs(self.path).prod_hint_shown)                # kept by other writes
        self.write_raw(json.dumps({"prod_hint_shown": "yes"}))
        self.assertFalse(AssistantPrefs(self.path).prod_hint_shown)               # only a real true
        self.write_raw("{not json")
        with self.assertLogs(LOGGER, level="WARNING"):
            self.assertFalse(AssistantPrefs(self.path).prod_hint_shown)

    def test_file_is_lf_and_ascii(self) -> None:
        AssistantPrefs(self.path).remember_greeting("n3")
        raw = self.path.read_bytes()
        self.assertTrue(raw.isascii())
        self.assertNotIn(b"\r", raw)


class ModuleHygieneTests(unittest.TestCase):
    def test_qt_free(self) -> None:
        code = ("import sys, briefing_reader.prefs; "
                "assert not any(m.startswith('PySide6') for m in sys.modules)")
        result = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])

    def test_source_is_ascii(self) -> None:
        self.assertTrue(Path(prefs.__file__).read_bytes().isascii())


class FollowupsOffTests(unittest.TestCase):
    def test_followups_off_round_trip(self) -> None:
        """B5: "Turn off on this PC" is a boolean written only while it is true; never text."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "assistant.json"
            state = AssistantPrefs(path)
            self.assertFalse(state.followups_off)
            state.set_muted(True)
            self.assertNotIn("followups_off", json.loads(path.read_text(encoding="utf-8")))
            self.assertTrue(state.set_followups_off(True))
            self.assertIs(json.loads(path.read_text(encoding="utf-8"))["followups_off"], True)
            again = AssistantPrefs(path)
            self.assertTrue(again.followups_off)
            again.remember_greeting("m2")
            self.assertTrue(AssistantPrefs(path).followups_off)                 # kept by other writes
            again.set_followups_off(False)
            self.assertNotIn("followups_off", json.loads(path.read_text(encoding="utf-8")))
            path.write_text(json.dumps({"followups_off": "yes"}), encoding="utf-8")
            self.assertFalse(AssistantPrefs(path).followups_off)                 # only a real true


if __name__ == "__main__":
    unittest.main()
