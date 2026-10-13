"""Tests for briefing_reader.prepseries: prep_series.json (recurring meetings whose prep offers were
ignored, R38). The key is a hash, the file holds counts and dates only, entries untouched for 60
days are pruned, a corrupt file counts as empty. Every test writes into a temporary folder; the
real %LOCALAPPDATA%\\briefing-reader\\prep_series.json is never read or written.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from briefing_reader import prepseries
from briefing_reader.prepseries import IGNORED_LIMIT, PRUNE_DAYS, SERIES_FILE, SeriesStore, series_key

PDT = timezone(timedelta(hours=-7))
LOGGER = "briefing_reader.prepseries"
SERIES_ID = "zq7seriesweekly01"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 5, 14, 0, tzinfo=PDT)

    def __call__(self) -> datetime:
        return self.now


class SeriesStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "data"
        self.path = self.dir / SERIES_FILE
        self.clock = Clock()
        self.key = series_key("work", "primary", SERIES_ID)

    def store(self) -> SeriesStore:
        return SeriesStore(self.path, clock=self.clock)

    def saved(self) -> dict:
        return json.loads(self.path.read_text(encoding="ascii"))

    def test_the_key_is_a_hash_never_the_ids(self) -> None:
        self.assertRegex(self.key, r"^[0-9a-f]{32}$")
        self.assertNotEqual(self.key, series_key("personal", "primary", SERIES_ID))
        self.assertNotEqual(self.key, series_key("work", "team", SERIES_ID))
        self.assertEqual(self.key, series_key("work", "primary", SERIES_ID))
        self.assertEqual(IGNORED_LIMIT, 3)

    def test_ignored_counts_reset_and_the_file(self) -> None:
        store = self.store()
        self.assertEqual(store.ignored(self.key), 0)
        self.assertFalse(self.path.exists())
        for count in (1, 2, 3):
            self.assertTrue(store.record_ignored(self.key))
            self.assertEqual(store.ignored(self.key), count)
        self.assertEqual(self.saved(), {"version": 1, "series": {self.key: {"ignored": 3, "day": "2026-10-05"}}})
        self.assertEqual(self.store().ignored(self.key), 3)                 # read back
        raw = self.path.read_bytes()
        self.assertTrue(raw.isascii())
        self.assertNotIn(b"\r", raw)
        for secret in (SERIES_ID.encode(), b"work", b"primary"):
            self.assertNotIn(secret, raw)                                     # no id or alias in clear
        self.clock.now += timedelta(days=1)
        self.assertTrue(store.reset(self.key))
        self.assertEqual(store.ignored(self.key), 0)
        self.assertEqual(self.saved()["series"][self.key], {"ignored": 0, "day": "2026-10-06"})
        self.assertFalse(store.reset(self.key))                              # already 0: nothing written
        self.assertFalse(store.reset(series_key("work", "primary", "other")))   # unknown
        self.assertFalse(store.record_ignored("not a key"))
        self.assertEqual(list(self.dir.glob("*.tmp")), [])

    def test_prune_after_sixty_days(self) -> None:
        store = self.store()
        old = series_key("work", "primary", "old01")
        store.record_ignored(old)
        self.clock.now += timedelta(days=PRUNE_DAYS)
        store.record_ignored(self.key)
        self.assertEqual(set(self.saved()["series"]), {old, self.key})       # 60 days: still kept
        self.clock.now += timedelta(days=1)
        store.record_ignored(self.key)
        self.assertEqual(set(self.saved()["series"]), {self.key})
        self.assertEqual(store.ignored(old), 0)

    def test_corrupt_or_wrong_shapes_count_as_empty(self) -> None:
        self.dir.mkdir(parents=True)
        for text in ("{not json", "[]", json.dumps({"series": []}),
                     json.dumps({"series": {self.key: {"ignored": "3", "day": "2026-10-05"}}}),
                     json.dumps({"series": {"short": {"ignored": 3, "day": "2026-10-05"}}}),
                     json.dumps({"series": {self.key: {"ignored": 3, "day": "yesterday"}}})):
            with self.subTest(text=text[:30]):
                self.path.write_text(text, encoding="ascii")
                with self.assertLogs(LOGGER, level="WARNING") if text in ("{not json", "[]",
                                                                         json.dumps({"series": []})) \
                        else _no_logs():
                    store = self.store()
                self.assertEqual(store.ignored(self.key), 0)
                self.assertEqual(len(store), 0)

    def test_a_file_nested_too_deep_counts_as_empty_and_is_overwritten(self) -> None:
        """Spec 13: a corrupt file never stops anything; JSON nested too deep for the parser
        (RecursionError) is read as empty like any other corrupt file, and the next change replaces it."""
        self.dir.mkdir(parents=True)
        self.path.write_text('{"series": ' + "[" * 200_000 + "]" * 200_000 + "}", encoding="ascii")
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            store = self.store()
        self.assertEqual((store.ignored(self.key), len(store)), (0, 0))
        self.assertIn("RecursionError", "\n".join(logs.output))
        with self.assertLogs(LOGGER, level="WARNING"):
            self.assertTrue(store.record_ignored(self.key))
        self.assertEqual(self.saved()["series"], {self.key: {"ignored": 1, "day": "2026-10-05"}})

    def test_two_processes_merge(self) -> None:
        first, second = self.store(), self.store()
        other = series_key("personal", "primary", "standup01")
        first.record_ignored(self.key)
        second.record_ignored(other)
        self.assertEqual(set(self.saved()["series"]), {self.key, other})

    def test_an_unwritable_file_is_kept_for_this_run(self) -> None:
        self.dir.mkdir(parents=True)
        self.path.mkdir()   # every read and write fails
        with self.assertLogs(LOGGER, level="WARNING"):
            store = self.store()
            self.assertFalse(store.record_ignored(self.key))
        self.assertEqual(store.ignored(self.key), 1)


class _no_logs:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc) -> bool:
        return False


class ModuleTests(unittest.TestCase):
    def test_constants(self) -> None:
        self.assertEqual((prepseries.SERIES_FILE, prepseries.PRUNE_DAYS), ("prep_series.json", 60))


if __name__ == "__main__":
    unittest.main()
