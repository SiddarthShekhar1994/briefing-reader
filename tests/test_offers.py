"""Tests for briefing_reader.ask.offers: the productivity bundle's offer rules (spec 4).

Pure functions only: quiet hours, "in a meeting", may_speak's reasons in order, held vs text only
(R37), the AM briefing counting as heard (R26), the cached caps (R13), the plan offer's reasons and
which of them are retried (R36), the prep offer's rules (lead window, guests, skip words, the
ignored series, R38) and back-to-back groups (R51). Every name and address is invented.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from briefing_reader.ask import offers
from briefing_reader.ask.offers import QuietFacts
from briefing_reader.ask.usage import CapCheck
from briefing_reader.config import ProductivityConfig
from briefing_reader.gcal import EventBrief, Guest
from briefing_reader.prepseries import series_key

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PDT = timezone(timedelta(hours=-7))
DAY = date(2026, 10, 5)                       # a Monday
SETTINGS = ProductivityConfig(enabled=True)
OWN = frozenset({"me@example.edu", "me@example.com"})
ANA = Guest("ana@example.edu", "Ana Example", "accepted")
BEN = Guest("ben@example.org", "Ben Example", "needsAction")


def at(hour: int, minute: int = 0, *, days: int = 0) -> datetime:
    return datetime.combine(DAY + timedelta(days=days), time(hour, minute), tzinfo=PDT)


def meeting(event_id: str = "evt0001", title: str = "Project sync", start: datetime | None = None,
            minutes: int = 30, guests: tuple[Guest, ...] = (ANA,), **extra) -> EventBrief:
    start = start or at(14)
    return EventBrief(event_id, "primary", title, start=start, end=start + timedelta(minutes=minutes),
                      organizer_self=True, organizer_email="me@example.edu", guests=guests,
                      self_response=extra.pop("self_response", "accepted"), **extra)


def facts(**changes) -> QuietFacts:
    return dataclasses.replace(QuietFacts(), **changes)


class QuietHoursTests(unittest.TestCase):
    def test_wrapping_midnight(self) -> None:
        for moment, quiet in ((time(23, 0), True), (time(3, 0), True), (time(22, 0), True), (time(7, 59), True),
                              (time(8, 0), False), (time(21, 59), False), (time(13, 0), False)):
            with self.subTest(moment=moment):
                self.assertEqual(offers.in_quiet_hours(moment, "22:00", "08:00"), quiet)

    def test_within_one_day_equal_bounds_and_datetimes(self) -> None:
        self.assertTrue(offers.in_quiet_hours(time(13, 0), "12:00", "14:00"))
        self.assertFalse(offers.in_quiet_hours(time(14, 0), "12:00", "14:00"))
        self.assertFalse(offers.in_quiet_hours(time(3, 0), "22:00", "22:00"))   # no quiet hours
        self.assertTrue(offers.in_quiet_hours(at(23), time(22, 0), time(8, 0)))
        self.assertEqual(offers.clock_time("bad"), time(0, 0))


class InMeetingTests(unittest.TestCase):
    def test_a_meeting_under_way_with_another_guest(self) -> None:
        day = {"work": (meeting(start=at(13, 45), minutes=30),)}
        self.assertTrue(offers.in_meeting(day, at(14), OWN))
        self.assertFalse(offers.in_meeting(day, at(14, 15), OWN))      # it ended
        self.assertFalse(offers.in_meeting(day, at(13, 44), OWN))      # not yet

    def test_not_declined_not_alone_not_all_day(self) -> None:
        under_way = at(13, 45)
        for brief in (meeting(start=under_way, self_response="declined"), meeting(start=under_way, guests=()),
                      meeting(start=under_way, guests=(Guest("me@example.com"),)),
                      EventBrief("evt0009", "primary", "Holiday", all_day_start=DAY, all_day_end=DAY,
                                 guests=(ANA,))):
            with self.subTest(brief=brief.title):
                self.assertFalse(offers.in_meeting({"work": (brief,)}, at(14), OWN))


class MaySpeakTests(unittest.TestCase):
    def may(self, kind: str = offers.KIND_PREP, *, now: datetime | None = None, **kw) -> str:
        quiet = kw.pop("facts", QuietFacts())
        return offers.may_speak(kind, quiet, now=now or at(14), settings=kw.pop("settings", SETTINGS),
                                meeting=kw.pop("meeting", False), last_spoken=kw.pop("last_spoken", None), **kw)

    def test_every_reason_in_order(self) -> None:
        everything = facts(activity="minimized", locked=True, speak_ok=False, playing=True, countdown=True, busy=True)
        late = at(23)
        self.assertEqual(self.may(facts=everything, now=late, meeting=True, stale=True), "muted")
        self.assertEqual(self.may(facts=dataclasses.replace(everything, speak_ok=True), now=late, meeting=True,
                                  stale=True), "quiet_hours")
        rest = dataclasses.replace(everything, speak_ok=True)
        self.assertEqual(self.may(facts=rest, meeting=True, stale=True), "stale")
        self.assertEqual(self.may(offers.KIND_PLAN, facts=rest, meeting=True, stale=True), "meeting")   # plan: no stale
        self.assertEqual(self.may(facts=rest, meeting=True), "meeting")
        self.assertEqual(self.may(facts=rest), "locked")
        rest = dataclasses.replace(rest, locked=False)
        self.assertEqual(self.may(offers.KIND_PLAN, facts=rest), "minimized")          # always for the plan offer
        self.assertEqual(self.may(facts=rest), "playing")                               # prep says so minimized
        quiet_min = dataclasses.replace(SETTINGS, prep_speak_minimized=False)
        self.assertEqual(self.may(facts=rest, settings=quiet_min), "minimized")
        rest = dataclasses.replace(rest, activity="visible")
        self.assertEqual(self.may(offers.KIND_PLAN, facts=rest, heard=False), "not_heard")
        self.assertEqual(self.may(facts=rest, heard=False), "playing")                 # prep: no heard rule
        self.assertEqual(self.may(facts=rest), "playing")
        rest = dataclasses.replace(rest, playing=False)
        self.assertEqual(self.may(facts=rest), "countdown")
        rest = dataclasses.replace(rest, countdown=False)
        self.assertEqual(self.may(facts=rest), "busy")
        rest = dataclasses.replace(rest, busy=False)
        self.assertEqual(self.may(facts=rest, last_spoken=(at(14) - timedelta(minutes=4)).replace(tzinfo=None)),
                         "spacing")
        self.assertEqual(self.may(facts=rest, last_spoken=at(14) - timedelta(minutes=5)), "")
        self.assertEqual(self.may(facts=rest), "")

    def test_never_while_the_reading_screen_is_not_up(self) -> None:
        """Hidden WAITING / the prompt (activity "off"): never said there; held, so it can be said
        once the reading screen is back (within its window)."""
        self.assertEqual(self.may(facts=facts(activity="off")), "hidden")
        self.assertEqual(self.may(offers.KIND_PLAN, facts=facts(activity="off")), "hidden")
        self.assertEqual(self.may(facts=facts(activity="off", speak_ok=False)), "muted")    # muted first
        self.assertEqual(offers.speech_disposition(offers.KIND_PREP, "hidden"), offers.HELD)

    def test_held_or_text_only(self) -> None:
        for reason in ("muted", "quiet_hours", "stale"):
            for kind in (offers.KIND_PLAN, offers.KIND_PREP):
                self.assertEqual(offers.speech_disposition(kind, reason), offers.TEXT_ONLY)
        self.assertEqual(offers.speech_disposition(offers.KIND_PREP, "meeting"), offers.TEXT_ONLY)
        self.assertEqual(offers.speech_disposition(offers.KIND_PLAN, "meeting"), offers.HELD)
        for reason in sorted(offers.HELD_REASONS):
            self.assertEqual(offers.speech_disposition(offers.KIND_PLAN, reason), offers.HELD)
        self.assertEqual(offers.speech_disposition(offers.KIND_PLAN, ""), offers.SPEAK)
        self.assertEqual(offers.TEXT_ONLY_REASONS & offers.HELD_REASONS, frozenset())


class HeardTests(unittest.TestCase):
    def test_finished_viewed_or_twenty_quiet_minutes(self) -> None:
        announced = at(10, 12)
        self.assertTrue(offers.briefing_heard(finished=True, viewed_then_jarvis=False, announced_at=announced,
                                              playback_started=True, now=at(10, 13)))
        self.assertTrue(offers.briefing_heard(finished=False, viewed_then_jarvis=True, announced_at=announced,
                                              playback_started=False, now=at(10, 13)))
        self.assertFalse(offers.briefing_heard(finished=False, viewed_then_jarvis=False, announced_at=announced,
                                               playback_started=False, now=at(10, 31)))
        self.assertTrue(offers.briefing_heard(finished=False, viewed_then_jarvis=False, announced_at=announced,
                                              playback_started=False, now=at(10, 32)))
        self.assertFalse(offers.briefing_heard(finished=False, viewed_then_jarvis=False, announced_at=announced,
                                               playback_started=True, now=at(11, 30)))   # playing: wait for it
        self.assertFalse(offers.briefing_heard(finished=False, viewed_then_jarvis=False, announced_at=None,
                                               playback_started=False, now=at(11, 30)))


class CapsTests(unittest.TestCase):
    def test_caps_from_the_cached_check(self) -> None:
        good = CapCheck(True, 20, 60, left_bundle_day=17)
        self.assertTrue(offers.caps_ok(good, 3))
        self.assertFalse(offers.caps_ok(None, 3))
        self.assertFalse(offers.caps_ok(dataclasses.replace(good, allowed=False), 3))
        self.assertFalse(offers.caps_ok(dataclasses.replace(good, left_hour=3), 3))     # the reserve
        self.assertTrue(offers.caps_ok(dataclasses.replace(good, left_hour=4), 3))
        self.assertFalse(offers.caps_ok(dataclasses.replace(good, left_bundle_day=0), 3))


class PlanOfferTests(unittest.TestCase):
    KEY = "2026-10-05 AM"

    def due(self, **changes) -> str:
        now = changes.pop("now", datetime(2026, 10, 5, 10, 15))
        values = dict(key=self.KEY, now=now, settings=SETTINGS, offered=False, planned=False, ready=True,
                      caps_ok=True, day_read_at=now - timedelta(minutes=2), day_complete=True, free_minutes=200,
                      needs_time=True)
        values.update(changes)
        return offers.plan_offer_due(**values)

    def test_every_reason_word(self) -> None:
        self.assertEqual(self.due(), "")
        self.assertEqual(self.due(settings=dataclasses.replace(SETTINGS, plan_offer=False)), "off")
        self.assertEqual(self.due(key="2026-10-05 PM"), "not_am")
        self.assertEqual(self.due(key="2026-10-04 AM"), "not_am")
        self.assertEqual(self.due(now=datetime(2026, 10, 5, 13, 30)), "late")
        self.assertEqual(self.due(now=datetime(2026, 10, 5, 7, 59)), "early")
        self.assertEqual(self.due(offered=True), "offered")
        self.assertEqual(self.due(planned=True), "planned")
        self.assertEqual(self.due(ready=False), "not_ready")
        self.assertEqual(self.due(caps_ok=False), "caps")
        self.assertEqual(self.due(day_read_at=None), "no_day")
        self.assertEqual(self.due(day_read_at=datetime(2026, 10, 5, 10, 4)), "no_day")   # older than 10 min
        self.assertEqual(self.due(free_minutes=59), "busy_day")
        self.assertEqual(self.due(needs_time=False), "nothing")

    def test_transient_reasons_are_retried(self) -> None:
        self.assertEqual(offers.TRANSIENT_PLAN_REASONS, frozenset({"early", "not_ready", "caps", "no_day"}))
        for reason in ("early", "not_ready", "caps", "no_day"):
            self.assertTrue(offers.plan_reason_transient(reason, True))
        for reason in ("off", "not_am", "offered", "planned", "late"):
            self.assertFalse(offers.plan_reason_transient(reason, False))
        for reason in ("busy_day", "nothing"):
            self.assertFalse(offers.plan_reason_transient(reason, True))      # final after a complete read
            self.assertTrue(offers.plan_reason_transient(reason, False))      # a partial read: again later


class PrepOfferTests(unittest.TestCase):
    def due(self, event: EventBrief | None = None, **changes) -> str:
        values = dict(now=at(13, 45), settings=SETTINGS, own=OWN, offered=(), offered_today=0, last_group_at=None,
                      ready=True, caps_ok=True)
        values.update(changes)
        return offers.prep_offer_due("work", event or meeting(), **values)

    def test_the_lead_window(self) -> None:
        self.assertEqual(self.due(), "")
        self.assertEqual(self.due(now=at(13, 44)), "early")
        self.assertEqual(self.due(now=at(13, 57)), "")
        self.assertEqual(self.due(now=at(13, 58)), "late")                      # less than 3 minutes before
        self.assertEqual(self.due(now=at(14, 5)), "late")                       # a sleep past the start
        self.assertEqual(self.due(now=at(13, 50), in_lead_window=False), "")
        self.assertEqual(self.due(now=at(9), in_lead_window=False), "")        # a back-to-back member

    def test_every_other_rule(self) -> None:
        self.assertEqual(self.due(settings=dataclasses.replace(SETTINGS, prep_offers=False)), "off")
        self.assertEqual(self.due(offered={offers.offer_key("work", meeting())}), "offered")
        self.assertEqual(self.due(ready=False), "not_ready")
        self.assertEqual(self.due(caps_ok=False), "caps")
        holiday = EventBrief("evt0009", "primary", "Holiday", all_day_start=DAY, all_day_end=DAY, guests=(ANA,))
        self.assertEqual(self.due(holiday), "all_day")
        self.assertEqual(self.due(meeting(minutes=241)), "long")
        self.assertEqual(self.due(meeting(minutes=240)), "")
        self.assertEqual(self.due(meeting(guests=())), "guests")
        self.assertEqual(self.due(meeting(guests=(Guest("me@example.com"),))), "guests")   # only his own
        nine = tuple(Guest(f"guest{n}@example.org") for n in range(9))
        self.assertEqual(self.due(meeting(guests=nine)), "guests")
        self.assertEqual(self.due(meeting(guests=nine[:8])), "")
        self.assertEqual(self.due(meeting(self_response="declined")), "declined")
        for title in ("Lunch with Ana", "FOCUS block", "Out of office", "Gym"):
            with self.subTest(title=title):
                self.assertEqual(self.due(meeting(title=title)), "skip_word")
        self.assertEqual(self.due(meeting(title="Breakfast planning")), "")      # whole words only
        self.assertEqual(self.due(offered_today=8), "per_day")
        self.assertEqual(self.due(offered_today=7), "")
        self.assertEqual(self.due(last_group_at=at(13, 40)), "spacing")
        self.assertEqual(self.due(last_group_at=at(13, 35)), "")

    def test_an_ignored_series(self) -> None:
        recurring = meeting(series_id="weekly01")
        key = series_key("work", "primary", "weekly01")
        counts = {key: 3}
        self.assertEqual(self.due(recurring, ignored=lambda found: counts.get(found, 0)), "ignored")
        counts[key] = 2
        self.assertEqual(self.due(recurring, ignored=lambda found: counts.get(found, 0)), "")
        self.assertEqual(self.due(meeting(), ignored=lambda found: 3), "")      # not recurring: never ignored
        self.assertEqual(offers.prep_series_key("work", recurring), key)
        self.assertEqual(offers.prep_series_key("work", meeting()), "")

    def test_offer_key_is_one_per_occurrence(self) -> None:
        self.assertEqual(offers.offer_key("work", meeting()), f"work|primary|evt0001|{at(14).isoformat()}")
        self.assertNotEqual(offers.offer_key("work", meeting(start=at(15))), offers.offer_key("work", meeting()))


class GroupTests(unittest.TestCase):
    def group(self, day, first, now=None) -> list[str]:
        found = offers.prep_offer_group(day, first, now=now or at(12, 45), settings=SETTINGS, own=OWN, offered=())
        return [event.event_id for _alias, event in found]

    def test_back_to_back_meetings_are_one_group(self) -> None:
        one = meeting("evt0001", start=at(13), minutes=60)
        two = meeting("evt0002", start=at(14), minutes=60)
        self.assertEqual(self.group({"work": (one, two)}, ("work", one)), ["evt0001", "evt0002"])

    def test_a_gap_longer_than_the_lead_is_two_groups(self) -> None:
        one = meeting("evt0001", start=at(13), minutes=30)
        two = meeting("evt0002", start=at(14), minutes=30)          # 30 minutes after it ends
        self.assertEqual(self.group({"work": (one, two)}, ("work", one)), ["evt0001"])
        close = meeting("evt0003", start=at(13, 44), minutes=30)    # 14 minutes after: chained
        self.assertEqual(self.group({"work": (one, close)}, ("work", one)), ["evt0001", "evt0003"])

    def test_the_group_never_passes_the_offers_left_today(self) -> None:
        """Each member counts for prep_offers_per_day: the group holds at most the offers left."""
        one = meeting("evt0001", start=at(14), minutes=30)
        two = meeting("evt0002", start=at(14, 30), minutes=30)
        three = meeting("evt0003", start=at(15), minutes=30)
        day = {"work": (one, two, three)}

        def group(per_day: int, offered_today: int = 0) -> list[str]:
            found = offers.prep_offer_group(day, ("work", one), now=at(13, 45), own=OWN, offered=(),
                                            settings=dataclasses.replace(SETTINGS, prep_offers_per_day=per_day),
                                            offered_today=offered_today)
            return [event.event_id for _alias, event in found]

        self.assertEqual(group(1), ["evt0001"])
        self.assertEqual(group(2), ["evt0001", "evt0002"])
        self.assertEqual(group(8), ["evt0001", "evt0002", "evt0003"])
        self.assertEqual(group(3, offered_today=1), ["evt0001", "evt0002"])
        self.assertEqual(group(3, offered_today=3), ["evt0001"])   # the first was checked by the caller

    def test_at_most_three_chained_across_accounts_and_only_qualifying(self) -> None:
        one = meeting("evt0001", start=at(13), minutes=60)
        two = meeting("evt0002", start=at(14), minutes=60)
        three = meeting("evt0003", start=at(15), minutes=60)
        four = meeting("evt0004", start=at(16), minutes=60)
        alone = meeting("evt0005", start=at(13, 30), guests=())
        lunch = meeting("evt0006", start=at(13, 15), title="Lunch")
        day = {"work": (one, three, alone, lunch), "personal": (two, four)}
        self.assertEqual(self.group(day, ("work", one)), ["evt0001", "evt0002", "evt0003"])


class HygieneTests(unittest.TestCase):
    def test_qt_free_ascii_lf(self) -> None:
        code = ("import sys, briefing_reader.ask.offers, briefing_reader.prepseries; "
                "assert not any(m.startswith('PySide6') for m in sys.modules)")
        found = subprocess.run([sys.executable, "-B", "-c", code], cwd=PROJECT_ROOT, capture_output=True, text=True,
                               timeout=60)
        self.assertEqual(found.returncode, 0, found.stderr[-2000:])
        for path in (Path(offers.__file__), PROJECT_ROOT / "briefing_reader" / "prepseries.py",
                     PROJECT_ROOT / "briefing_reader" / "productivity_ui.py"):
            raw = path.read_bytes()
            self.assertTrue(raw.isascii(), path.name)
            self.assertNotIn(b"\r", raw)

    def test_quiet_facts_repr_has_no_text(self) -> None:
        self.assertIn("visible", repr(QuietFacts()))


if __name__ == "__main__":
    unittest.main()
