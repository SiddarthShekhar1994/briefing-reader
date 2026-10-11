"""Tests for briefing_reader.ask.usage: the hourly and daily caps on a fake clock."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from briefing_reader.ask.usage import BUNDLE_KINDS, KIND_BUNDLE, KIND_NUDGE, KIND_PLAN_DAY, KIND_PREP, UsageLog
from tests.ask_fakes import NOW

LOGGER = "briefing_reader.ask.usage"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class UsageTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "data" / "ask_usage.json"
        self.clock = Clock()

    def log(self, **kwargs: int) -> UsageLog:
        return UsageLog(self.path, clock=self.clock, **kwargs)

    def spend(self, usage: UsageLog, count: int, step: timedelta = timedelta(minutes=1)) -> None:
        for _ in range(count):
            run = usage.start()
            usage.finish(run, outcome="ok", duration_ms=8000, turns=2, input_tokens=5000, output_tokens=400)
            self.clock.now += step

    def test_the_21st_run_in_an_hour_is_refused(self) -> None:
        usage = self.log(max_per_hour=20, max_per_day=60)
        start = self.clock.now
        self.spend(usage, 20)
        check = usage.check()
        self.assertFalse(check.allowed)
        self.assertEqual((check.left_hour, check.left_day), (0, 40))
        self.assertEqual(check.retry_at, start + timedelta(hours=1))
        self.assertEqual(check.message, "Ask limit reached; try again after 10:30 AM "
                                        "(20 planner runs an hour: [ask] max_per_hour)")
        self.clock.now = start + timedelta(hours=1, seconds=1)
        self.assertTrue(usage.check().allowed)

    def test_the_61st_run_in_a_day_is_refused(self) -> None:
        usage = self.log(max_per_hour=20, max_per_day=60)
        self.spend(usage, 60, step=timedelta(minutes=10))
        check = usage.check()
        self.assertFalse(check.allowed)
        self.assertIn("Ask's daily limit reached; try again after 9:30 AM ", check.message)
        self.assertIn("(60 planner runs a day: [ask] max_per_day)", check.message)
        self.assertEqual(check.left_day, 0)

    def test_two_runs_needed_for_mail(self) -> None:
        usage = self.log(max_per_hour=3, max_per_day=60)
        self.spend(usage, 2)
        self.assertTrue(usage.check(runs=1).allowed)
        self.assertFalse(usage.check(runs=2).allowed)

    def test_saved_counts_only_and_reloaded(self) -> None:
        usage = self.log()
        self.spend(usage, 3)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["runs"]), 3)
        self.assertEqual(set(data["runs"][0]), {"id", "at", "outcome", "duration_ms", "turns", "input_tokens",
                                                "output_tokens", "cache_read_tokens", "cache_write_tokens"})
        self.assertRegex(data["runs"][0]["id"], r"^[0-9a-f]{16}$")   # random: never the request
        self.assertNotIn("hold_until", data)
        self.assertEqual(self.path.read_bytes().count(b"\r"), 0)
        self.assertEqual(self.log().counts(), (3, 3))

    def test_a_started_run_counts_even_if_it_never_finishes(self) -> None:
        usage = self.log(max_per_hour=1)
        usage.start()
        self.assertFalse(self.log(max_per_hour=1).check().allowed)

    def test_old_entries_are_pruned(self) -> None:
        usage = self.log()
        self.spend(usage, 2)
        self.clock.now += timedelta(days=3)
        usage.start()
        self.assertEqual(len(json.loads(self.path.read_text(encoding="utf-8"))["runs"]), 1)

    def test_unreadable_file_counts_as_empty(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertLogs(LOGGER, level="WARNING"):
            self.assertTrue(self.log().check().allowed)
        self.path.write_text(json.dumps({"runs": [{"at": "yesterday"}, {"at": NOW.isoformat(), "turns": -3},
                                                  {"at": "2026-10-07T09:00:00"}, "junk"]}), encoding="utf-8")
        self.assertEqual(self.log().counts(), (1, 1))

    def test_failed_write_keeps_counting(self) -> None:
        usage = self.log(max_per_hour=2)
        with mock.patch("briefing_reader.ask.usage.tempfile.mkstemp", side_effect=OSError(13, "denied")):
            with self.assertLogs(LOGGER, level="WARNING"):
                self.spend(usage, 2)
        self.assertFalse(usage.check().allowed)

    def test_runs_from_another_process_count_and_survive(self) -> None:
        """--ask-text from the command line while the app is open: each is a UsageLog of its own."""
        app = self.log(max_per_hour=20)
        self.assertTrue(app.check().allowed)
        for _ in range(20):
            other = self.log(max_per_hour=20)
            other.finish(other.start(), outcome="ok", duration_ms=1000)
        check = app.check()
        self.assertFalse(check.allowed)
        self.assertEqual(check.left_hour, 0)
        self.assertEqual(self.log().counts(), (20, 20))
        self.clock.now += timedelta(hours=1, seconds=1)
        app.finish(app.start(), outcome="ok")
        self.assertEqual(self.log().counts(), (1, 21))   # the app's write kept the other runs
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len({run["id"] for run in data["runs"]}), 21)
        self.assertEqual([run["outcome"] for run in data["runs"]], ["ok"] * 21)

    def test_two_logs_interleaved_never_drop_a_run(self) -> None:
        first, second = self.log(), self.log()
        a = first.start()
        b = second.start()
        first.finish(a, outcome="ok", turns=2)
        second.finish(b, outcome="limit", turns=1)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(sorted((run["outcome"], run["turns"]) for run in data["runs"]), [("limit", 1), ("ok", 2)])

    def test_the_lock_file_is_held_across_processes(self) -> None:
        from briefing_reader.ask.usage import _FileLock

        lock = self.path.with_name("ask_usage.json.lock")
        with _FileLock(lock) as held:
            self.assertTrue(held.locked)
            with _FileLock(lock, timeout=0.1) as other:
                self.assertFalse(other.locked)   # the second waits, then goes on without it
        with _FileLock(lock, timeout=0.1) as again:
            self.assertTrue(again.locked)

    def test_hold_pauses_every_process_until_it_ends(self) -> None:
        usage = self.log()
        until = self.clock.now + timedelta(hours=3)
        self.assertEqual(usage.hold(until), until)
        for log in (usage, self.log()):
            check = log.check()
            self.assertFalse(check.allowed)
            self.assertTrue(check.held)
            self.assertEqual(check.retry_at, until)
            self.assertEqual(check.message, "Ask is paused until 12:30 PM: turn usage credits off at "
                                            "claude.ai/settings/usage (Claude Code said a request would use them)")
        self.assertEqual(self.log().hold_until(), until)
        self.assertIn("hold_until", json.loads(self.path.read_text(encoding="utf-8")))
        self.clock.now = until + timedelta(seconds=1)
        self.assertTrue(usage.check().allowed)
        self.assertIsNone(self.log().hold_until())

    def test_hold_is_capped_and_named_by_weekday_when_far(self) -> None:
        usage = self.log()
        until = usage.hold(self.clock.now + timedelta(days=30))
        self.assertEqual(until, self.clock.now + timedelta(days=8))
        self.assertIn("paused until Thu 9:30 AM", usage.check().message)

    def test_hold_survives_a_failed_write(self) -> None:
        usage = self.log()
        with mock.patch("briefing_reader.ask.usage.tempfile.mkstemp", side_effect=OSError(13, "denied")):
            with self.assertLogs(LOGGER, level="WARNING"):
                usage.hold(self.clock.now + timedelta(hours=1))
        self.assertFalse(usage.check().allowed)


class ResearchUsageTests(UsageTests):
    """Web research runs: "kind": "research" in the file, their own caps, and every run counts for Ask."""

    def research(self, usage: UsageLog, count: int, step: timedelta = timedelta(minutes=1)) -> None:
        for _ in range(count):
            usage.finish(usage.start(kind="research"), outcome="ok", duration_ms=9000, turns=4)
            self.clock.now += step

    def test_research_entries_carry_their_kind_and_plan_entries_keep_todays_keys(self) -> None:
        usage = self.log()
        self.spend(usage, 1)
        self.research(usage, 1)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        plan, research = data["runs"]
        self.assertEqual(set(plan), {"id", "at", "outcome", "duration_ms", "turns", "input_tokens", "output_tokens",
                                     "cache_read_tokens", "cache_write_tokens"})
        self.assertEqual(research["kind"], "research")
        self.assertEqual(set(research) - set(plan), {"kind"})
        self.assertEqual((research["outcome"], research["turns"]), ("ok", 4))
        # Read back by another process: the kinds survive.
        other = self.log()
        self.assertEqual(other.counts(kind="research"), (1, 1))
        self.assertEqual(other.counts(kind="plan"), (1, 1))
        self.assertEqual(other.counts(), (2, 2))

    def test_an_entry_without_a_kind_is_a_plan_run(self) -> None:
        self.path.parent.mkdir(parents=True)
        at = NOW.isoformat()
        self.path.write_text(json.dumps({"runs": [{"id": "a1", "at": at, "outcome": "ok"},
                                                  {"id": "a2", "at": at, "outcome": "ok", "kind": "odd"},
                                                  {"id": "a3", "at": at, "outcome": "ok", "kind": "research"}]}),
                             encoding="utf-8")
        usage = self.log()
        self.assertEqual(usage.counts(kind="plan"), (2, 2))
        self.assertEqual(usage.counts(kind="research"), (1, 1))
        usage.finish(usage.start(), outcome="ok")   # rewritten: the odd kind is dropped, research kept
        kinds = [run.get("kind") for run in json.loads(self.path.read_text(encoding="utf-8"))["runs"]]
        self.assertEqual(sorted(kind or "" for kind in kinds), ["", "", "", "research"])

    def test_the_hourly_research_cap(self) -> None:
        usage = self.log(max_per_hour=20, max_per_day=60, max_research_per_hour=6, max_research_per_day=20)
        start = self.clock.now
        self.research(usage, 6)
        plain = usage.check()
        self.assertTrue(plain.allowed)   # a planner run is still allowed
        self.assertEqual((plain.left_hour, plain.left_research_hour, plain.left_research_day), (14, 0, 14))
        check = usage.check(research=1)
        self.assertFalse(check.allowed)
        self.assertFalse(check.held)
        self.assertEqual(check.retry_at, start + timedelta(hours=1))
        self.assertEqual(check.message, "Web research limit reached; try again after 10:30 AM "
                                        "(6 research runs an hour: [research] max_per_hour)")
        self.assertEqual((check.left_research_hour, check.left_research_day), (0, 14))
        self.clock.now = start + timedelta(hours=1, seconds=1)
        self.assertTrue(usage.check(research=1).allowed)

    def test_the_daily_research_cap(self) -> None:
        usage = self.log(max_per_hour=20, max_per_day=60, max_research_per_hour=6, max_research_per_day=3)
        self.research(usage, 3, step=timedelta(hours=2))
        check = usage.check(research=1)
        self.assertFalse(check.allowed)
        self.assertIn("Web research's daily limit reached; try again after 9:30 AM ", check.message)
        self.assertIn("(3 research runs a day: [research] max_per_day)", check.message)
        self.assertEqual(check.left_research_day, 0)

    def test_research_runs_count_for_the_ask_caps(self) -> None:
        usage = self.log(max_per_hour=3, max_per_day=60, max_research_per_hour=6)
        self.research(usage, 3)
        check = usage.check()
        self.assertFalse(check.allowed)
        self.assertIn("(3 planner runs an hour: [ask] max_per_hour)", check.message)   # the Ask cap says so first
        self.assertFalse(usage.check(research=1).allowed)
        self.assertEqual(usage.counts(), (3, 3))

    def test_a_routed_research_needs_two_runs_and_one_research(self) -> None:
        usage = self.log(max_per_hour=3, max_per_day=60, max_research_per_hour=2)
        self.spend(usage, 1)
        self.assertTrue(usage.check(runs=2, research=1).allowed)
        self.research(usage, 1)
        self.assertFalse(usage.check(runs=2, research=1).allowed)   # one Ask run left
        self.assertTrue(usage.check(runs=1, research=1).allowed)
        self.path = self.path.with_name("other_usage.json")   # a fresh count
        usage = self.log(max_per_hour=20, max_per_day=60, max_research_per_hour=2)
        self.research(usage, 1)
        self.assertTrue(usage.check(runs=2, research=1).allowed)
        self.research(usage, 1)
        check = usage.check(runs=2, research=1)
        self.assertFalse(check.allowed)
        self.assertIn("[research] max_per_hour", check.message)

    def test_the_hold_refuses_research_too(self) -> None:
        usage = self.log()
        until = self.clock.now + timedelta(hours=2)
        usage.hold(until)
        check = usage.check(research=1)
        self.assertFalse(check.allowed)
        self.assertTrue(check.held)
        self.assertIn("Ask is paused until", check.message)
        self.assertEqual((check.left_research_hour, check.left_research_day), (6, 20))

    def test_research_lefts_are_always_filled(self) -> None:
        usage = self.log(max_research_per_hour=6, max_research_per_day=20)
        self.research(usage, 2)
        for check in (usage.check(), usage.check(runs=2), usage.check(research=1)):
            self.assertEqual((check.left_research_hour, check.left_research_day), (4, 18))
        self.assertEqual((usage.max_research_per_hour, usage.max_research_per_day), (6, 20))
        self.assertEqual(self.log(max_research_per_hour=0).max_research_per_hour, 1)


class BundleUsageTests(UsageTests):
    """The productivity bundle's runs: their kinds in the file, the daily bundle cap and the hourly
    reserve for typed Asks; each is an Ask run too (R13: every check fills left_bundle_day)."""

    def bundle(self, usage: UsageLog, count: int, kind: str = KIND_PLAN_DAY,
               step: timedelta = timedelta(minutes=1)) -> None:
        for _ in range(count):
            usage.finish(usage.start(kind=kind), outcome="ok", duration_ms=9000, turns=2)
            self.clock.now += step

    def test_bundle_kinds_are_persisted_and_read(self) -> None:
        usage = self.log()
        self.bundle(usage, 1, KIND_PLAN_DAY)
        self.bundle(usage, 1, KIND_PREP)
        self.bundle(usage, 1, KIND_NUDGE)
        self.spend(usage, 1)
        kinds = [run.get("kind", "") for run in json.loads(self.path.read_text(encoding="utf-8"))["runs"]]
        self.assertEqual(kinds, ["plan_day", "prep", "nudge", ""])
        other = self.log()
        self.assertEqual(other.counts(kind=KIND_PLAN_DAY), (1, 1))
        self.assertEqual(other.counts(kind=KIND_PREP), (1, 1))
        self.assertEqual(other.counts(kind=KIND_BUNDLE), (3, 3))
        self.assertEqual(other.counts(kind="plan"), (4, 4))        # every non-research run, as before
        self.assertEqual(other.counts(), (4, 4))
        self.assertEqual(BUNDLE_KINDS, frozenset({"plan_day", "prep", "nudge"}))

    def test_an_old_file_without_kinds_has_no_bundle_runs(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"runs": [{"id": "a1", "at": NOW.isoformat(), "outcome": "ok"}]}),
                             encoding="utf-8")
        usage = self.log(max_bundle_per_day=20)
        self.assertEqual(usage.counts(kind=KIND_BUNDLE), (0, 0))
        check = usage.check()
        self.assertEqual((check.left_hour, check.left_bundle_day), (19, 20))

    def test_the_daily_bundle_cap(self) -> None:
        usage = self.log(max_per_hour=20, max_per_day=60, max_bundle_per_day=3)
        start = self.clock.now
        self.bundle(usage, 3, step=timedelta(hours=2))
        self.assertTrue(usage.check().allowed)                     # a typed Ask is still allowed
        check = usage.check(runs=1, bundle=1, reserve=3)
        self.assertFalse(check.allowed)
        self.assertTrue(check.bundle_limit)
        self.assertEqual(check.left_bundle_day, 0)
        self.assertEqual(check.retry_at, start + timedelta(days=1))
        self.assertEqual(check.message, "Today's limit for plans, briefs and drafts is reached; try again after "
                                        "9:30 AM ([productivity] max_runs_per_day)")

    def test_the_hourly_reserve_for_typed_asks(self) -> None:
        usage = self.log(max_per_hour=6, max_per_day=60, max_bundle_per_day=20)
        start = self.clock.now
        self.spend(usage, 2)
        self.assertTrue(usage.check(runs=1, bundle=1, reserve=3).allowed)    # 4 left: 3 stay after it
        self.spend(usage, 1)
        check = usage.check(runs=1, bundle=1, reserve=3)
        self.assertFalse(check.allowed)
        self.assertFalse(check.bundle_limit)
        self.assertEqual(check.retry_at, start + timedelta(hours=1))
        self.assertEqual(check.message, "Jarvis keeps 3 Ask runs this hour for your own requests; try again after "
                                        "10:30 AM")
        self.assertTrue(usage.check().allowed)                     # typed Asks never use the reserve
        self.assertTrue(usage.check(runs=1, bundle=1, reserve=2).allowed)
        self.spend(usage, 2)                                       # one Ask run left this hour
        self.assertIn("Jarvis keeps 1 Ask run this hour", usage.check(runs=1, bundle=1, reserve=1).message)
        self.assertTrue(usage.check(runs=1, bundle=1, reserve=0).allowed)

    def test_every_check_fills_the_lefts(self) -> None:
        """R13: the default check() of the app's readiness check and of _refusal gives the offers what
        they need."""
        usage = self.log(max_per_hour=20, max_per_day=60, max_bundle_per_day=20)
        self.bundle(usage, 2)
        self.spend(usage, 1)
        for check in (usage.check(), usage.check(runs=2), usage.check(research=1), usage.check(bundle=1, reserve=3)):
            self.assertEqual((check.left_hour, check.left_bundle_day), (17, 18))
        usage.hold(self.clock.now + timedelta(hours=1))
        held = usage.check()
        self.assertTrue(held.held)
        self.assertEqual(held.left_bundle_day, 18)

    def test_a_kind_that_is_no_word_is_a_planner_run_and_never_raises(self) -> None:
        """A hand-edited or corrupt file: a list or an object as "kind" counts as a planner run (an
        unreadable entry never stops Ask), and the next write heals it."""
        self.path.parent.mkdir(parents=True)
        runs = [{"id": "a1", "at": NOW.isoformat(), "outcome": "ok", "kind": ["plan_day"]},
                {"id": "a2", "at": NOW.isoformat(), "outcome": "ok", "kind": {"plan_day": 1}}]
        self.path.write_text(json.dumps({"runs": runs}), encoding="utf-8")
        usage = self.log(max_per_hour=20, max_bundle_per_day=20)
        check = usage.check(runs=1, bundle=1, reserve=3)
        self.assertTrue(check.allowed)
        self.assertEqual((check.left_hour, check.left_bundle_day), (18, 20))
        self.assertEqual(usage.counts(kind=KIND_BUNDLE), (0, 0))
        self.assertEqual(usage.counts(kind="plan"), (2, 2))
        usage.finish(usage.start(kind=KIND_PLAN_DAY), outcome="ok")
        kinds = [run.get("kind", "") for run in json.loads(self.path.read_text(encoding="utf-8"))["runs"]]
        self.assertEqual(kinds, ["", "", "plan_day"])

    def test_a_reserve_as_large_as_the_hourly_cap_leaves_room_for_the_run(self) -> None:
        """[ask] max_per_hour 3 with the default reserve of 3 would refuse every plan with "try again
        after <now>": the reserve kept is at most the cap minus the run itself (warned once)."""
        usage = self.log(max_per_hour=3, max_per_day=60, max_bundle_per_day=20)
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            self.assertEqual(usage.reserve_kept(3), 2)
            check = usage.check(runs=1, bundle=1, reserve=3)
        self.assertTrue(check.allowed)
        self.assertEqual(logs.output, ["WARNING:briefing_reader.ask.usage:Ask: [productivity] reserve_ask_runs (3) "
                                       "leaves no room in [ask] max_per_hour (3); keeping 2"])
        self.spend(usage, 1)
        check = usage.check(runs=1, bundle=1, reserve=3)
        self.assertFalse(check.allowed)
        self.assertIn("Jarvis keeps 2 Ask runs this hour", check.message)
        with self.assertLogs(LOGGER, level="WARNING"):
            self.assertEqual(self.log(max_per_hour=1).reserve_kept(3), 0)
        with self.assertNoLogs(LOGGER, level="WARNING"):
            self.assertEqual(self.log(max_per_hour=20).reserve_kept(3), 3)

    def test_the_ask_caps_come_first(self) -> None:
        usage = self.log(max_per_hour=2, max_per_day=60, max_bundle_per_day=20)
        self.bundle(usage, 2)
        check = usage.check(runs=1, bundle=1, reserve=0)
        self.assertFalse(check.allowed)
        self.assertIn("[ask] max_per_hour", check.message)
        self.assertFalse(check.bundle_limit)


if __name__ == "__main__":
    unittest.main()
