"""Tests for briefing_reader.actions: proposal lines, section extraction and the decision store.

Everything is offline and pure: no Notion, no Google. Dates are fixed (today
is taken as 2026-10-04, a Sunday) so wording that depends on the year does
not depend on the machine clock. ActionStore tests use temporary folders.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from briefing_reader import actions, gcal
from briefing_reader.actions import (
    BLOCK_WARNING,
    BUILTIN_LINK_HOSTS,
    CALENDAR,
    CANCEL,
    EMAIL,
    MAX_RECIPIENTS,
    MOVE,
    OPEN,
    REPLIED_WARNING,
    REPLY,
    RSVP,
    SHARE,
    SLACK,
    STRUCTURED_KINDS,
    TODO,
    UNKNOWN,
    ActionStore,
    CardView,
    ProposedAction,
    approve_label,
    card_view,
    extract_actions,
    link_allowed,
    parse_action_line,
    result_text,
)
from briefing_reader.models import (
    BULLETED,
    DIVIDER,
    HEADING,
    NUMBERED,
    PARAGRAPH,
    TO_DO,
    FlatLine,
)
from briefing_reader.notion_client import FixtureSession, NotionClient, fetch_briefing

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FAKE_PAGE = Path(__file__).resolve().parent / "fixtures" / "fake_page.json"
ACTIONS_LOGGER = "briefing_reader.actions"

TODAY = date(2026, 10, 4)
PDT = timezone(timedelta(hours=-7), "PDT")
NOW = datetime(2026, 10, 4, 13, 52, tzinfo=PDT)

# The examples from HUD_SPEC.md section 1.
CHESS = ("Calendar: Chess Club Weekly Meeting | 2026-10-09 15:00-16:00 | weekly until 2026-12-11 | "
       "https://meet.google.com/aaa-bbbb-ccc | Carol's team sync")
FILM = ("Calendar: Film Club October General Meeting | 2026-10-06 17:00-18:00 | | "
       "Central Library, Media Lab |")
ESSAY = "Calendar: HIST Essay #1 draft due | 2026-10-05 |  |  | first draft + peer review"
SPRING = "Calendar: Spring break | 2027-03-22 to 2027-03-26"


def dt(day: int, hour: int, minute: int = 0, month: int = 10, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute)


def cal(when: str, repeat: str = "", title: str = "Sync") -> ProposedAction:
    return parse_action_line(f"Calendar: {title} | {when} | {repeat}")


def heading(text: str, level: int = 2) -> FlatLine:
    return FlatLine(HEADING, text, level=level)


def bullet(text: str, depth: int = 0) -> FlatLine:
    return FlatLine(BULLETED, text, depth)


def para(text: str, depth: int = 0) -> FlatLine:
    return FlatLine(PARAGRAPH, text, depth)


# --------------------------------------------------------------------------
# The spec's examples
# --------------------------------------------------------------------------

class SpecExampleTests(unittest.TestCase):

    def test_weekly_meeting_fields(self) -> None:
        action = parse_action_line(CHESS)
        self.assertTrue(action.actionable)
        self.assertEqual(action.kind, CALENDAR)
        self.assertEqual(action.error, "")
        self.assertEqual(action.title, "Chess Club Weekly Meeting")
        self.assertEqual(action.start, dt(9, 15))
        self.assertEqual(action.end, dt(9, 16))
        self.assertIsNone(action.all_day_start)
        self.assertFalse(action.all_day)
        self.assertEqual(action.repeat, "weekly until 2026-12-11")
        self.assertEqual(action.rrule, "RRULE:FREQ=WEEKLY;UNTIL=20261211T235959Z")
        self.assertEqual(action.where, "https://meet.google.com/aaa-bbbb-ccc")
        self.assertEqual(action.notes, "Carol's team sync")
        self.assertEqual(action.raw, CHESS)

    def test_weekly_meeting_wording(self) -> None:
        action = parse_action_line(CHESS)
        self.assertEqual(action.describe(TODAY), "Fri Oct 9 \u00b7 3:00-4:00 PM \u00b7 weekly until Dec 11")
        self.assertEqual(action.spoken(TODAY),
                         "Chess Club Weekly Meeting, Friday October 9, 3 to 4 PM, weekly until December 11")

    def test_meeting_with_empty_repeat_and_trailing_pipe(self) -> None:
        action = parse_action_line(FILM)
        self.assertTrue(action.actionable)
        self.assertEqual(action.title, "Film Club October General Meeting")
        self.assertEqual((action.start, action.end), (dt(6, 17), dt(6, 18)))
        self.assertEqual((action.repeat, action.rrule), ("", ""))
        self.assertEqual(action.where, "Central Library, Media Lab")
        self.assertEqual(action.notes, "")
        self.assertEqual(action.describe(TODAY), "Tue Oct 6 \u00b7 5:00-6:00 PM")
        self.assertEqual(action.spoken(TODAY), "Film Club October General Meeting, Tuesday October 6, 5 to 6 PM")

    def test_all_day_deadline(self) -> None:
        action = parse_action_line(ESSAY)
        self.assertTrue(action.actionable)
        self.assertEqual(action.title, "HIST Essay #1 draft due")
        self.assertIsNone(action.start)
        self.assertIsNone(action.end)
        self.assertEqual((action.all_day_start, action.all_day_end), (date(2026, 10, 5), date(2026, 10, 5)))
        self.assertTrue(action.all_day)
        self.assertEqual(action.where, "")
        self.assertEqual(action.notes, "first draft + peer review")
        self.assertEqual(action.describe(TODAY), "Mon Oct 5 \u00b7 all day")
        self.assertEqual(action.spoken(TODAY), "HIST Essay number 1 draft due, Monday October 5, all day")

    def test_multi_day_all_day(self) -> None:
        action = parse_action_line(SPRING)
        self.assertTrue(action.actionable)
        self.assertEqual((action.all_day_start, action.all_day_end), (date(2027, 3, 22), date(2027, 3, 26)))
        self.assertEqual(action.describe(TODAY), "Mon Mar 22 - Fri Mar 26, 2027 \u00b7 all day")
        self.assertEqual(action.spoken(TODAY),
                         "Spring break, Monday March 22 to Friday March 26, 2027, all day")
        self.assertEqual(action.describe(date(2027, 1, 1)), "Mon Mar 22 - Fri Mar 26 \u00b7 all day")


# --------------------------------------------------------------------------
# <when>
# --------------------------------------------------------------------------

class WhenTimedTests(unittest.TestCase):

    def assertTimed(self, when: str, start: datetime, end: datetime) -> None:
        action = cal(when)
        self.assertEqual(action.error, "", when)
        self.assertEqual((action.start, action.end), (start, end), when)
        self.assertIsNone(action.all_day_start, when)

    def test_24_hour_ranges(self) -> None:
        for when in ("2026-10-09 15:00-16:00", "2026-10-09 15:00 - 16:00", "2026-10-09 15:00\u201316:00",
                     "2026-10-09 15:00 \u2013 16:00", "2026-10-09 15:00 \u2014 16:00",
                     "2026-10-09 15:00 to 16:00", "2026/10/09 15:00-16:00", "2026-10-09T15:00-16:00",
                     "2026-10-09, 15:00-16:00", "2026-10-09 from 15:00 to 16:00"):
            with self.subTest(when=when):
                self.assertTimed(when, dt(9, 15), dt(9, 16))

    def test_12_hour_forms(self) -> None:
        for when in ("2026-10-09 3:00 PM-4:00 PM", "2026-10-09 3:00PM-4:00PM", "2026-10-09 3pm-4pm",
                     "2026-10-09 3 PM - 4 PM", "2026-10-09 3 p.m. - 4 p.m.", "2026-10-09 3pm\u20134pm",
                     "2026-10-09 3-4pm", "2026-10-09 3 - 4 PM", "2026-10-09 3pm-4",
                     "2026-10-09 at 3pm to 4pm"):
            with self.subTest(when=when):
                self.assertTimed(when, dt(9, 15), dt(9, 16))

    def test_12_hour_minutes(self) -> None:
        cases = {
            "2026-10-09 3 PM - 4:30 PM": (dt(9, 15), dt(9, 16, 30)),
            "2026-10-09 3PM-4:30PM": (dt(9, 15), dt(9, 16, 30)),
            "2026-10-09 3:30-4pm": (dt(9, 15, 30), dt(9, 16)),
            "2026-10-09 10:30-11:00 AM": (dt(9, 10, 30), dt(9, 11)),
            "2026-10-09 9:15 am - 9:45 am": (dt(9, 9, 15), dt(9, 9, 45)),
        }
        for when, (start, end) in cases.items():
            with self.subTest(when=when):
                self.assertTimed(when, start, end)

    def test_missing_meridiem_takes_the_shorter_event(self) -> None:
        cases = {
            "2026-10-09 11:30-12:30 PM": (dt(9, 11, 30), dt(9, 12, 30)),
            "2026-10-09 9-5pm": (dt(9, 9), dt(9, 17)),
            "2026-10-09 11am-1": (dt(9, 11), dt(9, 13)),
            "2026-10-09 9-17": (dt(9, 9), dt(9, 17)),
        }
        for when, (start, end) in cases.items():
            with self.subTest(when=when):
                self.assertTimed(when, start, end)

    def test_noon_and_midnight(self) -> None:
        cases = {
            "2026-10-09 12pm-1pm": (dt(9, 12), dt(9, 13)),
            "2026-10-09 12am-1am": (dt(9, 0), dt(9, 1)),
            "2026-10-09 noon-1:30pm": (dt(9, 12), dt(9, 13, 30)),
            "2026-10-09 noon-1": (dt(9, 12), dt(9, 13)),
            "2026-10-09 11am-noon": (dt(9, 11), dt(9, 12)),
            "2026-10-09 22:00-midnight": (dt(9, 22), dt(10, 0)),
            "2026-10-09 22:00-24:00": (dt(9, 22), dt(10, 0)),
        }
        for when, (start, end) in cases.items():
            with self.subTest(when=when):
                self.assertTimed(when, start, end)

    def test_end_before_start_is_next_day(self) -> None:
        for when in ("2026-10-09 23:00-01:00", "2026-10-09 11pm-1am", "2026-10-09 11-1am",
                     "2026-10-09 11 PM - 1 AM"):
            with self.subTest(when=when):
                self.assertTimed(when, dt(9, 23), dt(10, 1))

    def test_single_time_lasts_an_hour(self) -> None:
        cases = {
            "2026-10-09 15:00": (dt(9, 15), dt(9, 16)),
            "2026-10-09 3pm": (dt(9, 15), dt(9, 16)),
            "2026-10-09 at 3:30 PM": (dt(9, 15, 30), dt(9, 16, 30)),
            "2026-10-09 9:30": (dt(9, 9, 30), dt(9, 10, 30)),   # HH:MM without AM/PM is 24-hour
            "2026-10-09 23:30": (dt(9, 23, 30), dt(10, 0, 30)),
        }
        for when, (start, end) in cases.items():
            with self.subTest(when=when):
                self.assertTimed(when, start, end)

    def test_weekday_prefix_is_checked_and_dropped(self) -> None:
        for when in ("Fri 2026-10-09 3pm-4pm", "Friday, 2026-10-09, 15:00-16:00",
                     "2026-10-09 (Fri) 15:00-16:00", "on Fri. 2026-10-09 3-4pm"):
            with self.subTest(when=when):
                self.assertTimed(when, dt(9, 15), dt(9, 16))

    def test_timed_span_over_two_dates(self) -> None:
        self.assertTimed("2026-10-09 15:00 to 2026-10-11 11:00", dt(9, 15), dt(11, 11))
        self.assertTimed("2026-10-09 3pm - 2026-10-10 9am", dt(9, 15), dt(10, 9))
        self.assertTimed("Fri 2026-10-09 3pm to Sat 2026-10-10 9am", dt(9, 15), dt(10, 9))

    def test_times_are_naive_wall_times(self) -> None:
        action = cal("2026-10-09 15:00-16:00")
        self.assertIsNone(action.start.tzinfo)
        self.assertIsNone(action.end.tzinfo)


class WhenAllDayTests(unittest.TestCase):

    def assertAllDay(self, when: str, first: date, last: date) -> None:
        action = cal(when)
        self.assertEqual(action.error, "", when)
        self.assertEqual((action.all_day_start, action.all_day_end), (first, last), when)
        self.assertIsNone(action.start, when)
        self.assertTrue(action.all_day, when)

    def test_single_day(self) -> None:
        for when in ("2026-10-05", "2026-10-05 all day", "2026-10-05 (all-day)", "Mon 2026-10-05",
                     "2026-10-05 All Day", "2026/10/5"):
            with self.subTest(when=when):
                self.assertAllDay(when, date(2026, 10, 5), date(2026, 10, 5))

    def test_multi_day_range_is_inclusive(self) -> None:
        for when in ("2027-03-22 to 2027-03-26", "2027-03-22 - 2027-03-26", "2027-03-22\u20132027-03-26",
                     "2027-03-22 through 2027-03-26", "Mon 2027-03-22 to Fri 2027-03-26",
                     "2027-03-22 to 2027-03-26 all day"):
            with self.subTest(when=when):
                self.assertAllDay(when, date(2027, 3, 22), date(2027, 3, 26))

    def test_same_start_and_end_day(self) -> None:
        self.assertAllDay("2026-10-05 to 2026-10-05", date(2026, 10, 5), date(2026, 10, 5))


class WhenErrorTests(unittest.TestCase):

    def assertError(self, when: str, fragment: str) -> None:
        action = cal(when)
        self.assertFalse(action.actionable, when)
        self.assertEqual(action.kind, CALENDAR, when)
        self.assertIn(fragment, action.error, when)
        self.assertEqual(action.describe(TODAY), "")
        self.assertEqual(action.spoken(TODAY), "")

    def test_missing_date(self) -> None:
        for line in ("Calendar: Sync", "Calendar: Sync |", "Calendar: Sync |  | weekly",
                     "Calendar: Sync | Friday 3pm", "Calendar: Sync | next week"):
            with self.subTest(line=line):
                action = parse_action_line(line)
                self.assertFalse(action.actionable)
                self.assertIn("no date", action.error)
                self.assertEqual(action.title, "Sync")

    def test_bad_times(self) -> None:
        cases = {
            "2026-10-09 25:00": 'could not read the time "25:00"',
            "2026-10-09 15:75": 'could not read the time "15:75"',
            "2026-10-09 13pm": 'could not read the time "13pm"',
            "2026-10-09 0am": 'could not read the time "0am"',
            "2026-10-09 15:00 PT": 'could not read the time "15:00 pt"',
            "2026-10-09 15:00-16:00-17:00": "could not read the time",
            "2026-10-09 soonish": "could not read the time",
        }
        for when, fragment in cases.items():
            with self.subTest(when=when):
                self.assertError(when, fragment)

    def test_ambiguous_hours_need_am_pm(self) -> None:
        for when in ("2026-10-09 3-4", "2026-10-09 9", "2026-10-09 3:00-4"):
            with self.subTest(when=when):
                self.assertError(when, "needs AM/PM or HH:MM")

    def test_zero_length(self) -> None:
        self.assertError("2026-10-09 3pm-3pm", "ends when it starts")
        self.assertError("2026-10-09 15:00-15:00", "ends when it starts")

    def test_impossible_dates(self) -> None:
        for when in ("2026-02-30", "2026-13-01", "2026-10-09 to 2026-10-32"):
            with self.subTest(when=when):
                self.assertError(when, "is not a real date")

    def test_weekday_mismatch(self) -> None:
        self.assertError("Thu 2026-10-09 3pm", "2026-10-09 is a Friday, not Thu")
        self.assertError("2026-10-05 to Sun 2026-10-09", "is a Friday, not Sun")

    def test_bad_ranges(self) -> None:
        self.assertError("2027-03-26 to 2027-03-22", "the end date is before the start date")
        self.assertError("2026-10-09 15:00 to 2026-10-09 14:00", "ends before it starts")
        self.assertError("2026-10-09 15:00 to 2026-10-10", "both dates or for neither")
        self.assertError("2026-10-05 and 2026-10-06", "could not read the dates")
        self.assertError("2026-10-05 2026-10-06 2026-10-07", "could not read the date")
        self.assertError("soon 2026-10-05", "could not read the date")

    def test_time_and_all_day_conflict(self) -> None:
        self.assertError("2026-10-05 all day 3pm", 'both a time and "all day"')


# --------------------------------------------------------------------------
# <repeat>
# --------------------------------------------------------------------------

class RepeatTests(unittest.TestCase):

    def test_words_map_to_rrules(self) -> None:
        cases = {
            "daily": ("daily", "RRULE:FREQ=DAILY"),
            "Every day": ("daily", "RRULE:FREQ=DAILY"),
            "weekdays": ("weekdays", "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"),
            "every weekday": ("weekdays", "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"),
            "weekly": ("weekly", "RRULE:FREQ=WEEKLY"),
            "Weekly.": ("weekly", "RRULE:FREQ=WEEKLY"),
            "biweekly": ("biweekly", "RRULE:FREQ=WEEKLY;INTERVAL=2"),
            "bi-weekly": ("biweekly", "RRULE:FREQ=WEEKLY;INTERVAL=2"),
            "every other week": ("biweekly", "RRULE:FREQ=WEEKLY;INTERVAL=2"),
            "monthly": ("monthly", "RRULE:FREQ=MONTHLY"),
            "yearly": ("yearly", "RRULE:FREQ=YEARLY"),
            "annually": ("yearly", "RRULE:FREQ=YEARLY"),
        }
        for words, (repeat, rrule) in cases.items():
            with self.subTest(repeat=words):
                action = cal("2026-10-09 15:00-16:00", words)
                self.assertTrue(action.actionable, action.error)
                self.assertEqual((action.repeat, action.rrule), (repeat, rrule))

    def test_one_off_words(self) -> None:
        for words in ("", "once", "Once", "none", "one-off", "does not repeat", "-"):
            with self.subTest(repeat=words):
                action = cal("2026-10-09 15:00-16:00", words)
                self.assertTrue(action.actionable, action.error)
                self.assertEqual((action.repeat, action.rrule), ("", ""))

    def test_until(self) -> None:
        for words in ("weekly until 2026-12-11", "Weekly, until 2026-12-11.", "weekly till 2026-12-11",
                      "weekly through 2026-12-11", "weekly ending 2026-12-11"):
            with self.subTest(repeat=words):
                action = cal("2026-10-09 15:00-16:00", words)
                self.assertEqual(action.repeat, "weekly until 2026-12-11")
                self.assertEqual(action.rrule, "RRULE:FREQ=WEEKLY;UNTIL=20261211T235959Z")

    def test_until_for_all_day_is_a_date(self) -> None:
        action = cal("2026-10-05", "weekly until 2026-12-07")
        self.assertEqual(action.rrule, "RRULE:FREQ=WEEKLY;UNTIL=20261207")
        action = cal("2026-10-05", "weekdays until 2026-10-30")
        self.assertEqual(action.rrule, "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20261030")

    def test_counts(self) -> None:
        for words in ("weekly x 10", "weekly x10", "Weekly X 10", "weekly \u00d7 10", "weekly 10 times",
                      "weekly, 10 times", "weekly for 10 times"):
            with self.subTest(repeat=words):
                action = cal("2026-10-09 15:00-16:00", words)
                self.assertTrue(action.actionable, action.error)
                self.assertEqual(action.repeat, "weekly x 10")
                self.assertEqual(action.rrule, "RRULE:FREQ=WEEKLY;COUNT=10")
        action = cal("2026-10-09", "biweekly x 6")
        self.assertEqual(action.rrule, "RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=6")

    def test_a_count_of_one_is_a_one_off(self) -> None:
        action = cal("2026-10-09 15:00-16:00", "daily x 1")
        self.assertTrue(action.actionable)
        self.assertEqual((action.repeat, action.rrule), ("", ""))

    def test_bad_repeats(self) -> None:
        cases = {
            "every other blue moon": 'could not read the repeat "every other blue moon"',
            "fortnight-ish": "could not read the repeat",
            "until 2026-12-11": "has an end but no repeat",
            "once x 3": "has an end but no repeat",
            "weekly until Dec 11": 'could not read the end date in "weekly until Dec 11"',
            "weekly until 2026-12-32": "is not a real date",
            "weekly x 0": "the repeat count must be 1 to 999",
            "weekly x 1000": "the repeat count must be 1 to 999",
            "weekly until 2026-10-01": "the repeat ends before the event starts",
        }
        for words, fragment in cases.items():
            with self.subTest(repeat=words):
                action = cal("2026-10-09 15:00-16:00", words)
                self.assertFalse(action.actionable)
                self.assertIn(fragment, action.error)
                self.assertEqual(action.rrule, "")

    def test_until_on_the_start_day_is_allowed(self) -> None:
        action = cal("2026-10-09 15:00-16:00", "daily until 2026-10-09")
        self.assertTrue(action.actionable, action.error)


# --------------------------------------------------------------------------
# describe() and spoken()
# --------------------------------------------------------------------------

class WordingTests(unittest.TestCase):

    def assertWording(self, when: str, repeat: str, describe: str, spoken: str) -> None:
        action = cal(when, repeat)
        self.assertEqual(action.error, "")
        self.assertEqual(action.describe(TODAY), describe)
        self.assertEqual(action.spoken(TODAY), spoken)

    def test_time_ranges(self) -> None:
        dot = " \u00b7 "
        self.assertWording("2026-10-09 15:30-16:00", "", f"Fri Oct 9{dot}3:30-4:00 PM",
                           "Sync, Friday October 9, 3:30 to 4 PM")
        self.assertWording("2026-10-09 3pm-4:30pm", "", f"Fri Oct 9{dot}3:00-4:30 PM",
                           "Sync, Friday October 9, 3 to 4:30 PM")
        self.assertWording("2026-10-09 11:30-12:30 PM", "", f"Fri Oct 9{dot}11:30 AM-12:30 PM",
                           "Sync, Friday October 9, 11:30 AM to 12:30 PM")
        self.assertWording("2026-10-09 9-5pm", "", f"Fri Oct 9{dot}9:00 AM-5:00 PM",
                           "Sync, Friday October 9, 9 AM to 5 PM")
        self.assertWording("2026-10-09 9:00-9:15", "", f"Fri Oct 9{dot}9:00-9:15 AM",
                           "Sync, Friday October 9, 9 to 9:15 AM")

    def test_noon_midnight_and_overnight(self) -> None:
        dot = " \u00b7 "
        self.assertWording("2026-10-09 noon-1pm", "", f"Fri Oct 9{dot}12:00-1:00 PM",
                           "Sync, Friday October 9, noon to 1 PM")
        self.assertWording("2026-10-09 11am-noon", "", f"Fri Oct 9{dot}11:00 AM-12:00 PM",
                           "Sync, Friday October 9, 11 AM to noon")
        self.assertWording("2026-10-09 22:00-00:00", "", f"Fri Oct 9{dot}10:00 PM-12:00 AM",
                           "Sync, Friday October 9, 10 PM to midnight")
        self.assertWording("2026-10-09 23:00-01:00", "", f"Fri Oct 9{dot}11:00 PM-1:00 AM (next day)",
                           "Sync, Friday October 9, 11 PM to 1 AM")

    def test_span_over_dates(self) -> None:
        self.assertWording("2026-10-09 15:00 to 2026-10-11 11:00", "",
                           "Fri Oct 9 3:00 PM - Sun Oct 11 11:00 AM",
                           "Sync, from Friday October 9 at 3 PM to Sunday October 11 at 11 AM")

    def test_repeats_in_words(self) -> None:
        dot = " \u00b7 "
        self.assertWording("2026-10-09 9:00-9:15", "weekdays until 2027-01-15",
                           f"Fri Oct 9{dot}9:00-9:15 AM{dot}weekdays until Jan 15, 2027",
                           "Sync, Friday October 9, 9 to 9:15 AM, every weekday until January 15, 2027")
        self.assertWording("2026-10-09", "biweekly x 6",
                           f"Fri Oct 9{dot}all day{dot}every 2 weeks, 6 times",
                           "Sync, Friday October 9, all day, every two weeks, 6 times")
        self.assertWording("2026-10-09 3pm", "daily", f"Fri Oct 9{dot}3:00-4:00 PM{dot}daily",
                           "Sync, Friday October 9, 3 to 4 PM, daily")
        self.assertWording("2026-10-09 3pm", "monthly until 2026-12-31",
                           f"Fri Oct 9{dot}3:00-4:00 PM{dot}monthly until Dec 31",
                           "Sync, Friday October 9, 3 to 4 PM, monthly until December 31")

    def test_years_outside_this_year(self) -> None:
        dot = " \u00b7 "
        self.assertWording("2027-01-01", "", f"Fri Jan 1, 2027{dot}all day", "Sync, Friday January 1, 2027, all day")
        self.assertWording("2026-12-28 to 2027-01-02", "", f"Mon Dec 28 - Sat Jan 2, 2027{dot}all day",
                           "Sync, Monday December 28 to Saturday January 2, 2027, all day")
        self.assertWording("2027-01-04 10:00-11:00", "", f"Mon Jan 4, 2027{dot}10:00-11:00 AM",
                           "Sync, Monday January 4, 2027, 10 to 11 AM")

    def test_default_today_is_the_real_today(self) -> None:
        action = parse_action_line(CHESS)
        self.assertEqual(action.describe(), action.describe(date.today()))
        self.assertEqual(action.spoken(), action.spoken(date.today()))

    def test_spoken_title_reads_symbols(self) -> None:
        action = parse_action_line("Calendar: Q3 review & planning w/ Bob | 2026-10-09 3pm")
        self.assertEqual(action.title, "Q3 review & planning w/ Bob")
        self.assertTrue(action.spoken(TODAY).startswith("Q3 review and planning with Bob, Friday"))

    def test_spoken_is_plain_ascii(self) -> None:
        for line in (CHESS, FILM, ESSAY, SPRING):
            with self.subTest(line=line):
                spoken = parse_action_line(line).spoken(TODAY)
                self.assertTrue(spoken.isascii())
                self.assertNotIn("#", spoken)
                self.assertFalse(spoken.endswith("."))

    def test_informational_lines_have_no_wording(self) -> None:
        for line in ("Reply: Carol about the draft", "Calendar: Sync | 2026-10-09 25:00", "Just a note"):
            with self.subTest(line=line):
                action = parse_action_line(line)
                self.assertEqual(action.describe(TODAY), "")
                self.assertEqual(action.spoken(TODAY), "")

    def test_hand_built_action(self) -> None:
        action = ProposedAction(id="x", kind=CALENDAR, raw="", title="Dentist",
                                start=dt(7, 8, 30), end=dt(7, 9, 15), repeat="yearly")
        self.assertEqual(action.describe(TODAY), "Wed Oct 7 \u00b7 8:30-9:15 AM \u00b7 yearly")
        self.assertEqual(action.spoken(TODAY), "Dentist, Wednesday October 7, 8:30 to 9:15 AM, yearly")


# --------------------------------------------------------------------------
# Kinds and malformed lines
# --------------------------------------------------------------------------

class KindTests(unittest.TestCase):

    def test_other_kinds_are_informational(self) -> None:
        cases = {
            "Reply: Carol about the draft": ("reply", "Carol about the draft"),
            "Todo: renew parking permit": ("todo", "renew parking permit"),
            "To-do: renew parking permit": ("todo", "renew parking permit"),
            "TODO: renew parking permit": ("todo", "renew parking permit"),
            "Frobnicate: the widgets | 2026-10-09": ("frobnicate", "the widgets | 2026-10-09"),
            "Reply to Bob: yes": ("reply", "yes"),
        }
        for line, (kind, title) in cases.items():
            with self.subTest(line=line):
                action = parse_action_line(line)
                self.assertEqual(action.kind, kind)
                self.assertEqual(action.title, title)
                self.assertEqual(action.error, "")
                self.assertFalse(action.actionable)
                self.assertEqual(action.raw, line)

    def test_calendar_kind_is_case_insensitive_with_aliases(self) -> None:
        for line in ("calendar: Sync | 2026-10-09 3pm", "CALENDAR : Sync | 2026-10-09 3pm",
                     "Calendar event: Sync | 2026-10-09 3pm", "Event: Sync | 2026-10-09 3pm"):
            with self.subTest(line=line):
                action = parse_action_line(line)
                self.assertEqual(action.kind, CALENDAR)
                self.assertTrue(action.actionable, action.error)
                self.assertEqual(action.title, "Sync")

    def test_prose_without_a_kind(self) -> None:
        action = parse_action_line("Nothing needs your OK today.")
        self.assertEqual(action.kind, UNKNOWN)
        self.assertEqual(action.error, "")
        self.assertFalse(action.actionable)
        self.assertEqual(action.raw, "Nothing needs your OK today.")

    def test_pipe_line_without_a_kind_is_an_error(self) -> None:
        action = parse_action_line("Chess Club Weekly Meeting | 2026-10-09 15:00-16:00")
        self.assertEqual(action.kind, UNKNOWN)
        self.assertIn("no action type", action.error)
        self.assertFalse(action.actionable)

    def test_a_url_is_not_a_kind(self) -> None:
        action = parse_action_line("https://example.com/agenda")
        self.assertEqual(action.kind, UNKNOWN)
        self.assertEqual(action.raw, "https://example.com/agenda")

    def test_empty_lines(self) -> None:
        for text in ("", "   ", "** **", "\U0001F4C5"):
            with self.subTest(text=text):
                action = parse_action_line(text)
                self.assertEqual(action.kind, UNKNOWN)
                self.assertEqual(action.error, "empty line")
                self.assertFalse(action.actionable)

    def test_missing_title(self) -> None:
        action = parse_action_line("Calendar: | 2026-10-09 3pm")
        self.assertEqual(action.error, "no title")
        self.assertFalse(action.actionable)

    def test_list_markers_and_quotes_are_ignored(self) -> None:
        for line in ("- Calendar: Sync | 2026-10-09 3pm", "* Calendar: Sync | 2026-10-09 3pm",
                     "1. Calendar: Sync | 2026-10-09 3pm", "[ ] Calendar: Sync | 2026-10-09 3pm",
                     "> Calendar: Sync | 2026-10-09 3pm"):
            with self.subTest(line=line):
                action = parse_action_line(line)
                self.assertTrue(action.actionable, action.error)
                self.assertEqual(action.raw, "Calendar: Sync | 2026-10-09 3pm")

    def test_errored_line_keeps_what_it_could_read(self) -> None:
        action = parse_action_line("Calendar: Sync | 2026-10-09 | sometimes | Room 12 | bring laptop")
        self.assertIn("could not read the repeat", action.error)
        self.assertEqual((action.title, action.where, action.notes), ("Sync", "Room 12", "bring laptop"))


# --------------------------------------------------------------------------
# Markdown inside fields
# --------------------------------------------------------------------------

class MarkdownFieldTests(unittest.TestCase):

    def test_emphasis_code_and_emoji_are_stripped(self) -> None:
        action = parse_action_line(
            "- **Calendar:** *Team sync* | `2026-10-09` 3pm | | __Room 12__ | _bring_ ~~old~~ notes \U0001F4C5")
        self.assertTrue(action.actionable, action.error)
        self.assertEqual(action.title, "Team sync")
        self.assertEqual(action.where, "Room 12")
        self.assertEqual(action.notes, "bring old notes")
        self.assertEqual(action.raw, "Calendar: Team sync | 2026-10-09 3pm | | Room 12 | bring old notes")

    def test_links_keep_their_url_in_where_and_notes(self) -> None:
        action = parse_action_line(
            "Calendar: Sync | 2026-10-09 3pm | | [Zoom](https://zoom.us/j/123) | see [doc](https://x.io/d).")
        self.assertEqual(action.where, "Zoom (https://zoom.us/j/123)")
        self.assertEqual(action.notes, "see doc (https://x.io/d).")

    def test_bare_and_angle_urls_survive(self) -> None:
        action = parse_action_line(
            "Calendar: Sync | 2026-10-09 3pm | | <https://meet.google.com/abc-defg-hij> | "
            "agenda at https://example.com/a_b_c.")
        self.assertEqual(action.where, "https://meet.google.com/abc-defg-hij")
        self.assertEqual(action.notes, "agenda at https://example.com/a_b_c.")

    def test_link_label_that_is_a_url(self) -> None:
        action = parse_action_line(
            "Calendar: Sync | 2026-10-09 3pm | | [https://zoom.us/j/1](https://zoom.us/j/1) |")
        self.assertEqual(action.where, "https://zoom.us/j/1")

    def test_title_drops_links_and_urls(self) -> None:
        action = parse_action_line("Calendar: [Team sync](https://notion.so/abc) | 2026-10-09 3pm")
        self.assertEqual(action.title, "Team sync")
        self.assertEqual(action.raw, "Calendar: Team sync (https://notion.so/abc) | 2026-10-09 3pm")

    def test_html_is_removed(self) -> None:
        action = parse_action_line("Calendar: <b>Sync</b> | 2026-10-09 3pm | | Room&nbsp;5 |")
        self.assertEqual(action.title, "Sync")
        self.assertEqual(action.where, "Room 5")

    def test_extra_fields_join_the_notes(self) -> None:
        action = parse_action_line("Calendar: Sync | 2026-10-09 3pm | | Room 5 | bring laptop | and charger")
        self.assertEqual(action.notes, "bring laptop | and charger")

    def test_escaped_markdown_stays_literal(self) -> None:
        action = parse_action_line(r"Calendar: 5 \* 3 review | 2026-10-09 3pm")
        self.assertEqual(action.title, "5 * 3 review")


# --------------------------------------------------------------------------
# Ids
# --------------------------------------------------------------------------

class IdTests(unittest.TestCase):

    def test_id_formula(self) -> None:
        key = "\x1f".join(("calendar", "chess club weekly meeting", "2026-10-09T15:00", "2026-10-09T16:00", "0",
                           "RRULE:FREQ=WEEKLY;UNTIL=20261211T235959Z"))
        self.assertEqual(parse_action_line(CHESS).id, hashlib.sha1(key.encode("utf-8")).hexdigest()[:16])
        key = "\x1f".join(("calendar", "hist essay #1 draft due", "2026-10-05", "2026-10-05", "1", ""))
        self.assertEqual(parse_action_line(ESSAY).id, hashlib.sha1(key.encode("utf-8")).hexdigest()[:16])

    def test_ids_are_16_hex(self) -> None:
        for line in (CHESS, FILM, ESSAY, SPRING, "Reply: x", ""):
            with self.subTest(line=line):
                action_id = parse_action_line(line).id
                self.assertRegex(action_id, r"^[0-9a-f]{16}$")

    def test_stable_across_spelling_where_and_notes(self) -> None:
        base = parse_action_line(CHESS).id
        variants = (
            "Calendar: chess club  weekly MEETING | 2026-10-09 3pm-4pm | Weekly until 2026-12-11",
            "- **Calendar:** Chess Club Weekly Meeting | Fri 2026-10-09 15:00\u201316:00 | weekly till 2026-12-11 "
            "| Room 5 | different notes",
        )
        for line in variants:
            with self.subTest(line=line):
                self.assertEqual(parse_action_line(line).id, base)

    def test_changes_with_time_repeat_or_kind_of_day(self) -> None:
        ids = {
            cal("2026-10-09 15:00-16:00").id,
            cal("2026-10-09 15:00-16:30").id,
            cal("2026-10-09 16:00-17:00").id,
            cal("2026-10-09 15:00-16:00", "weekly").id,
            cal("2026-10-09").id,
            cal("2026-10-09 to 2026-10-10").id,
            cal("2026-10-09 15:00-16:00", title="Other").id,
        }
        self.assertEqual(len(ids), 7)

    def test_informational_lines_differ_by_text(self) -> None:
        first = parse_action_line("Reply: Carol about the draft")
        second = parse_action_line("Reply: Bob about the invoice")
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.id, parse_action_line("Reply: Carol about the draft").id)
        broken_a = parse_action_line("Calendar: Sync | 2026-10-09 25:00")
        broken_b = parse_action_line("Calendar: Sync | 2026-10-09 26:00")
        self.assertNotEqual(broken_a.id, broken_b.id)


# --------------------------------------------------------------------------
# extract_actions
# --------------------------------------------------------------------------

class ExtractActionsTests(unittest.TestCase):

    def page(self) -> list[FlatLine]:
        return [
            heading("Daily Briefing", 1),
            heading("Work (2)"),
            bullet("Email from **Bob** about the budget"),
            bullet("Invoice 4411 overdue"),
            heading("Proposed actions (3)"),
            bullet(CHESS),
            bullet(CHESS.replace("Carol's team sync", "duplicate with other notes")),
            FlatLine(NUMBERED, "Reply: Carol about the draft", number=1),
            heading("Later this term", 3),
            bullet(SPRING),
            bullet("Calendar: Sync | 2026-10-09 25:00", depth=1),
            FlatLine(DIVIDER, ""),
            para("   "),
            FlatLine(TO_DO, ESSAY, checked=False),
            heading("Ignore"),
            bullet("Promo: 20% off"),
        ]

    def test_section_between_other_headings(self) -> None:
        lines = self.page()
        found, kept = extract_actions(lines)
        self.assertEqual([a.title for a in found], ["Chess Club Weekly Meeting", "Carol about the draft",
                                                    "Spring break", "Sync", "HIST Essay #1 draft due"])
        self.assertEqual([a.kind for a in found], ["calendar", "reply", "calendar", "calendar", "calendar"])
        self.assertEqual([a.actionable for a in found], [True, False, True, False, True])
        self.assertEqual(found[0].notes, "Carol's team sync")   # the duplicate kept the first
        self.assertEqual(kept, lines[:4] + lines[14:])
        self.assertIsInstance(kept, list)

    def test_no_section_returns_the_lines_unchanged(self) -> None:
        lines = (heading("Work"), bullet("Calendar: Sync | 2026-10-09 3pm"), heading("Ignore"))
        found, kept = extract_actions(lines)
        self.assertEqual(found, [])
        self.assertEqual(kept, list(lines))

    def test_nested_section_ends_at_its_own_level(self) -> None:
        lines = [
            heading("Work"),
            heading("Inbox", 3),
            bullet("Email from Bob"),
            heading("Proposed actions", 3),
            bullet(FILM),
            heading("Calendar", 3),
            bullet("Dentist at 4"),
            heading("Personal"),
            bullet("Call mom"),
        ]
        found, kept = extract_actions(lines)
        self.assertEqual([a.title for a in found], ["Film Club October General Meeting"])
        self.assertEqual(kept, lines[:3] + lines[5:])

    def test_section_ends_at_a_higher_level(self) -> None:
        lines = [heading("Proposed actions", 3), bullet(FILM), heading("Next", 1), bullet("z")]
        found, kept = extract_actions(lines)
        self.assertEqual(len(found), 1)
        self.assertEqual(kept, lines[2:])

    def test_section_at_the_end_of_the_page(self) -> None:
        lines = [heading("Work"), bullet("x"), heading("Proposed actions"), bullet(ESSAY), bullet(FILM)]
        found, kept = extract_actions(lines)
        self.assertEqual(len(found), 2)
        self.assertEqual(kept, lines[:2])

    def test_heading_matching_is_lenient_about_case_counts_and_markup(self) -> None:
        for text in ("PROPOSED ACTIONS", "Proposed actions (2)", "\U0001F4C5 Proposed actions",
                     "**Proposed actions:**", "Proposed  Actions - 2"):
            with self.subTest(heading=text):
                found, kept = extract_actions([heading(text), bullet(ESSAY), heading("Ignore")])
                self.assertEqual(len(found), 1)
                self.assertEqual(kept, [heading("Ignore")])

    def test_only_the_whole_name_matches(self) -> None:
        lines = [heading("Proposed actions for next week"), bullet(ESSAY)]
        self.assertEqual(extract_actions(lines), ([], lines))

    def test_custom_heading_names(self) -> None:
        lines = [heading("Needs approval"), bullet(ESSAY), heading("Work"), bullet("x")]
        found, kept = extract_actions(lines, ("Proposed actions", "Needs approval"))
        self.assertEqual(len(found), 1)
        self.assertEqual(kept, lines[2:])
        found, _ = extract_actions(lines, "needs approval")   # a single string works too
        self.assertEqual(len(found), 1)
        self.assertEqual(extract_actions(lines), ([], lines))

    def test_two_sections_are_both_removed(self) -> None:
        lines = [heading("Proposed actions"), bullet(ESSAY), heading("Work"), bullet("x"),
                 heading("Proposed actions"), bullet(FILM), bullet(ESSAY)]
        found, kept = extract_actions(lines)
        self.assertEqual([a.title for a in found], ["HIST Essay #1 draft due", "Film Club October General Meeting"])
        self.assertEqual(kept, lines[2:4])

    def test_markdown_paragraph_headings(self) -> None:
        lines = [para("## Work"), para("Email from Bob"), para("## Proposed actions"),
                 para("- " + ESSAY), para("### Notes"), para("- Reply: Bob"), para("## Ignore"), para("promo")]
        found, kept = extract_actions(lines)
        self.assertEqual([a.kind for a in found], ["calendar", "reply"])
        self.assertEqual(kept, lines[:2] + lines[6:])

    def test_bold_paragraph_titles_on_a_page_without_heading_blocks(self) -> None:
        lines = [para("**Work**"), bullet("x"), para("**Proposed actions**"), bullet(ESSAY),
                 para("**Calendar: Sync | 2026-10-09 3pm**"), para("**Ignore**"), bullet("promo")]
        found, kept = extract_actions(lines)
        self.assertEqual([a.title for a in found], ["HIST Essay #1 draft due", "Sync"])
        self.assertEqual(kept, lines[:2] + lines[5:])

    def test_plain_label_paragraph_on_a_page_with_headings(self) -> None:
        lines = [heading("Work"), bullet("x"), para("Proposed actions:"), bullet(ESSAY),
                 para("**Not a title here**"), heading("Ignore"), bullet("promo")]
        found, kept = extract_actions(lines)
        self.assertEqual([a.title for a in found], ["HIST Essay #1 draft due", "Not a title here"])
        self.assertEqual(kept, lines[:2] + lines[5:])

    def test_nested_lines_are_parsed_too(self) -> None:
        lines = [heading("Proposed actions"), bullet(ESSAY), bullet(FILM, depth=1), para("a note", depth=2)]
        found, kept = extract_actions(lines)
        self.assertEqual([a.title for a in found], ["HIST Essay #1 draft due", "Film Club October General Meeting",
                                                    "a note"])
        self.assertEqual(kept, [])

    def test_logs_counts_never_text(self) -> None:
        with self.assertLogs(ACTIONS_LOGGER, level="INFO") as captured:
            extract_actions(self.page())
        output = "\n".join(captured.output)
        self.assertIn("5 line(s), 3 actionable, 1 duplicate(s)", output)
        for secret in ("Chess Club", "Carol", "Spring", "meet.google"):
            self.assertNotIn(secret, output)

    def test_fake_notion_page_has_no_section(self) -> None:
        session = FixtureSession(FAKE_PAGE)
        client = NotionClient(token="test-token", session=session, sleep=lambda s: None)
        briefing = fetch_briefing(client, session.page_id, now=NOW)
        found, kept = extract_actions(briefing.lines)
        self.assertEqual(found, [])
        self.assertEqual(kept, list(briefing.lines))

    # ---- key=value lines in the section ----

    def mixed_page(self) -> list[FlatLine]:
        return [
            heading("Work"),
            bullet("x"),
            heading("Proposed actions"),
            bullet(CHESS),
            bullet(kv_line("Reply", REPLY_PAIRS)),
            bullet(kv_line("Todo", TODO_PAIRS)),
            bullet("Open: title=Lab form | link=https://forms.example.net/lab"),
            bullet("Reply: Carol about the draft"),
            bullet("Frobnicate: the widgets"),
            bullet("Nothing else today."),
            heading("Ignore"),
        ]

    def test_link_hosts_reach_the_parser(self) -> None:
        found, _ = extract_actions(self.mixed_page())
        form = found[3]
        self.assertEqual((form.kind, form.decidable), (OPEN, False))
        self.assertIn("forms.example.net is not on the list", form.error)
        found, _ = extract_actions(self.mixed_page(), link_hosts=("forms.example.net",))
        self.assertEqual((found[3].error, found[3].link), ("", "https://forms.example.net/lab"))
        found, _ = extract_actions(self.mixed_page(), actions.DEFAULT_HEADINGS, link_hosts=["*.example.net"])
        self.assertTrue(found[3].decidable)

    def test_log_counts_kinds_without_any_text(self) -> None:
        with self.assertLogs(ACTIONS_LOGGER, level="INFO") as captured:
            found, _ = extract_actions(self.mixed_page())
        output = "\n".join(captured.output)
        self.assertEqual(len(found), 7)
        self.assertIn("Proposed actions: 7 line(s), 3 actionable, 0 duplicate(s) dropped; "
                      "3 to decide (calendar 1, reply 2, todo 1, open 1, other 1, note 1)", output)
        for secret in ("Chess", "Thursday", "ana@example.edu", "18c0ffee", "CAExample", "Problem set",
                       "instructure", "forms.example.net", "Carol", "Frobnicate", "widgets", "Shall we"):
            self.assertNotIn(secret, output)

    def test_old_log_prefix_is_kept(self) -> None:
        with self.assertLogs(ACTIONS_LOGGER, level="INFO") as captured:
            extract_actions(self.page())
        output = "\n".join(captured.output)
        self.assertIn("5 line(s), 3 actionable, 1 duplicate(s) dropped; 3 to decide (calendar 4, reply 1)", output)


# --------------------------------------------------------------------------
# ActionStore
# --------------------------------------------------------------------------

class FixedClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class ActionStoreTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "briefing-reader" / "actions.json"
        self.clock = FixedClock(NOW)

    def store(self) -> ActionStore:
        return ActionStore(self.path, clock=self.clock)

    def write_raw(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.path.parent.iterdir() if p.name != self.path.name)

    def test_round_trip(self) -> None:
        store = self.store()
        store.set("abc", "created", link="https://calendar.google.com/event?eid=1")
        store.set("def", "denied")
        reopened = self.store()
        self.assertEqual(reopened.get("abc"), {"status": "created", "at": "2026-10-04T13:52:00-07:00",
                                               "link": "https://calendar.google.com/event?eid=1",
                                               "message": ""})
        self.assertEqual(reopened.get("def")["status"], "denied")
        self.assertIsNone(reopened.get("missing"))
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(on_disk), {"abc", "def"})
        self.assertEqual(self.leftovers(), [])

    def test_missing_file_is_empty_and_not_created(self) -> None:
        store = self.store()
        self.assertIsNone(store.get("abc"))
        self.assertFalse(store.is_decided("abc"))
        self.assertFalse(self.path.exists())

    def test_decided_statuses(self) -> None:
        store = self.store()
        for status, decided in (("created", True), ("exists", True), ("denied", True), ("failed", False)):
            with self.subTest(status=status):
                store.set(f"id-{status}", status, message="boom" if status == "failed" else "")
                self.assertEqual(store.is_decided(f"id-{status}"), decided)
        self.assertEqual(self.store().get("id-failed")["message"], "boom")

    def test_failed_then_created(self) -> None:
        store = self.store()
        store.set("abc", "failed", message="Google sign-in timed out")
        self.assertFalse(store.is_decided("abc"))
        store.set("abc", "created", link="https://x")
        self.assertTrue(self.store().is_decided("abc"))
        self.assertEqual(self.store().get("abc")["message"], "")

    def test_unknown_status_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store().set("abc", "approved")
        self.assertFalse(self.path.exists())

    def test_get_returns_a_copy(self) -> None:
        store = self.store()
        store.set("abc", "denied")
        store.get("abc")["status"] = "created"
        self.assertEqual(store.get("abc")["status"], "denied")

    def test_corrupt_file_is_logged_and_treated_as_empty(self) -> None:
        self.write_raw('{"abc": {"status": "created", ')
        with self.assertLogs(ACTIONS_LOGGER, level="WARNING") as captured:
            store = self.store()
        self.assertIn("not valid JSON", "\n".join(captured.output))
        self.assertIsNone(store.get("abc"))
        store.set("def", "denied")
        self.assertEqual(set(json.loads(self.path.read_text(encoding="utf-8"))), {"def"})

    def test_wrong_shapes_are_ignored(self) -> None:
        self.write_raw("[1, 2, 3]")
        with self.assertLogs(ACTIONS_LOGGER, level="WARNING"):
            self.assertIsNone(self.store().get("1"))
        self.write_raw(json.dumps({
            "good": {"status": "denied", "at": "2026-10-04T10:00:00-07:00", "link": 5},
            "bad-status": {"status": "approved"},
            "not-a-dict": "created",
        }))
        with self.assertLogs(ACTIONS_LOGGER, level="WARNING") as captured:
            store = self.store()
        self.assertIn("Ignored 2 malformed", "\n".join(captured.output))
        self.assertEqual(store.get("good"), {"status": "denied", "at": "2026-10-04T10:00:00-07:00",
                                             "link": "", "message": ""})
        self.assertIsNone(store.get("bad-status"))

    def test_byte_order_mark_is_accepted(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"abc": {"status": "exists"}}).encode("utf-8"))
        self.assertTrue(self.store().is_decided("abc"))

    def test_unreadable_path_never_crashes(self) -> None:
        self.path.mkdir(parents=True)   # a folder where the file should be
        with self.assertLogs(ACTIONS_LOGGER, level="WARNING"):
            store = self.store()
        with self.assertLogs(ACTIONS_LOGGER, level="WARNING") as captured:
            store.set("abc", "denied")
        self.assertIn("kept for this run only", "\n".join(captured.output))
        self.assertTrue(store.is_decided("abc"))

    def test_write_is_atomic_via_replace(self) -> None:
        store = self.store()
        with mock.patch.object(actions.os, "replace", wraps=os.replace) as replace:
            store.set("abc", "created")
        replace.assert_called_once()
        source, target = replace.call_args.args
        self.assertEqual(Path(target), self.path)
        self.assertEqual(Path(source).parent, self.path.parent)
        self.assertNotEqual(Path(source), self.path)
        self.assertFalse(Path(source).exists())
        self.assertEqual(self.leftovers(), [])

    def test_failed_replace_keeps_the_old_file(self) -> None:
        store = self.store()
        store.set("abc", "created")
        before = self.path.read_bytes()
        failure = PermissionError(13, "Access is denied")
        with mock.patch.object(actions.os, "replace", side_effect=failure):
            with self.assertLogs(ACTIONS_LOGGER, level="WARNING"):
                store.set("def", "denied")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.leftovers(), [])
        self.assertTrue(store.is_decided("def"))       # kept in memory for this run
        self.assertFalse(self.store().is_decided("def"))

    def test_prune_forgets_old_and_undated_decisions(self) -> None:
        old = (NOW - timedelta(days=61)).isoformat()
        recent = (NOW - timedelta(days=59)).isoformat()
        self.write_raw(json.dumps({
            "old": {"status": "denied", "at": old},
            "recent": {"status": "created", "at": recent},
            "undated": {"status": "created", "at": "yesterday-ish"},
            "naive": {"status": "exists", "at": "2026-10-01T09:00:00"},
        }))
        store = self.store()
        with self.assertLogs(ACTIONS_LOGGER, level="INFO") as captured:
            store.prune(60)
        self.assertIn("Forgot 2 action decision(s)", "\n".join(captured.output))
        self.assertEqual(set(json.loads(self.path.read_text(encoding="utf-8"))), {"recent", "naive"})
        self.assertIsNone(store.get("old"))
        self.assertTrue(store.is_decided("recent"))

    def test_prune_default_age_and_naive_clock(self) -> None:
        self.clock.now = datetime(2026, 10, 4, 12, 0)   # naive clock values are local time
        store = self.store()
        store.set("today", "denied")
        self.clock.now = datetime(2026, 12, 4, 12, 1)   # 61 days later
        store.prune()
        self.assertIsNone(store.get("today"))

    def test_prune_without_old_entries_does_not_write(self) -> None:
        store = self.store()
        store.set("abc", "denied")
        with mock.patch.object(actions.os, "replace") as replace:
            store.prune(60)
        replace.assert_not_called()
        self.assertTrue(store.is_decided("abc"))

    # ---- the statuses and keys of the other proposal kinds ----

    def test_new_statuses_and_which_are_decisions(self) -> None:
        store = self.store()
        cases = (("done", True), ("sent", True), ("running", True), ("unknown", False), ("failed", False))
        for status, decided in cases:
            with self.subTest(status=status):
                store.set(f"id-{status}", status)
                self.assertEqual(store.is_decided(f"id-{status}"), decided)
        self.assertEqual(actions.STATUSES, ("created", "exists", "denied", "failed", "done", "sent", "running",
                                            "unknown", "held"))
        self.assertEqual(actions.DECIDED_STATUSES,
                         frozenset({"created", "exists", "denied", "done", "sent", "running", "held"}))

    def test_kind_and_account_round_trip(self) -> None:
        store = self.store()
        store.set("abc", "done", kind="reply", account="work")
        store.set("def", "denied", kind="todo")
        reopened = self.store()
        self.assertEqual(reopened.get("abc"), {"status": "done", "at": "2026-10-04T13:52:00-07:00", "link": "",
                                               "message": "", "kind": "reply", "account": "work"})
        self.assertEqual(reopened.get("def")["kind"], "todo")
        self.assertNotIn("account", reopened.get("def"))
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["abc"]["account"], "work")

    def test_an_old_file_without_kind_or_account_loads(self) -> None:
        self.write_raw(json.dumps({"abc": {"status": "created", "at": "2026-10-04T10:00:00-07:00",
                                           "link": "https://x", "message": ""},
                                   "def": {"status": "denied", "kind": 5, "account": ""}}))
        store = self.store()
        self.assertEqual(store.get("abc"), {"status": "created", "at": "2026-10-04T10:00:00-07:00",
                                            "link": "https://x", "message": ""})
        self.assertEqual(store.get("def"), {"status": "denied", "at": "", "link": "", "message": ""})

    def test_running_becomes_unknown_and_prune_saves_it(self) -> None:
        self.write_raw(json.dumps({
            "sending": {"status": "running", "at": NOW.isoformat(), "kind": "reply", "account": "work"},
            "done": {"status": "done", "at": NOW.isoformat()},
        }))
        with self.assertLogs(ACTIONS_LOGGER, level="WARNING") as captured:
            store = self.store()
        self.assertEqual("\n".join(captured.output).count("1 action(s) were interrupted; marked unknown"), 1)
        entry = store.get("sending")
        self.assertEqual((entry["status"], entry["message"], entry["kind"], entry["account"]),
                         ("unknown", actions.INTERRUPTED_MESSAGE, "reply", "work"))
        self.assertEqual(actions.INTERRUPTED_MESSAGE,
                         "Jarvis stopped while this was running; check before retrying")
        self.assertFalse(store.is_decided("sending"))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["sending"]["status"], "running")
        store.prune(60)   # nothing is old enough, but the change is saved
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["sending"]["status"], "unknown")
        with mock.patch.object(actions.os, "replace") as replace:
            store.prune(60)   # saved once is enough
        replace.assert_not_called()

    def test_a_held_send_jarvis_closed_on_is_not_sent_at_the_next_start(self) -> None:
        """A Reply / Email / Cancel still "held" (Jarvis closed before its hidden hold could go out) is
        "failed" with "Not sent - Jarvis closed first" at the next start: the card never says Sent."""
        self.write_raw(json.dumps({
            "mail": {"status": "held", "at": NOW.isoformat(), "kind": "email", "account": "work"},
            "cancel": {"status": "held", "at": NOW.isoformat(), "kind": "cancel", "account": "work"},
        }))
        with self.assertLogs(ACTIONS_LOGGER, level="WARNING") as captured:
            store = self.store()
        self.assertIn("2 held action(s) were not sent before Jarvis closed; marked failed",
                      " ".join(captured.output))
        self.assertEqual((store.get("mail")["status"], store.get("mail")["message"]),
                         ("failed", "Not sent - Jarvis closed first"))
        self.assertEqual(store.get("cancel")["message"], "Not cancelled - Jarvis closed first")
        self.assertFalse(store.is_decided("mail"))   # it waits for a decision again
        store.prune(60)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["mail"]["status"], "failed")

    def test_held_is_a_decision_and_forget_only_removes_the_named_statuses(self) -> None:
        store = self.store()
        store.set("mail", "held", kind="reply", account="work")
        self.assertTrue(store.is_decided("mail"))
        self.assertFalse(store.forget("mail", ("running",)))
        self.assertEqual(store.get("mail")["status"], "held")
        self.assertTrue(store.forget("mail", ("held",)))
        self.assertIsNone(store.get("mail"))
        self.assertNotIn("mail", json.loads(self.path.read_text(encoding="utf-8")))
        self.assertFalse(store.forget("missing", ("held",)))

    def test_log_line_names_id_kind_account_and_status(self) -> None:
        store = self.store()
        with self.assertLogs(ACTIONS_LOGGER, level="INFO") as captured:
            store.set("abc", "done", kind="reply", account="work")
            store.set("def", "denied")
        output = "\n".join(captured.output)
        self.assertIn("Action abc (reply, work): done", output)
        self.assertIn("Action def (-, -): denied", output)

    def test_an_alias_outside_the_known_ones_is_logged_as_other(self) -> None:
        # acct= is page text: it could carry a name or a phone number.
        store = self.store()
        with self.assertLogs(ACTIONS_LOGGER, level="INFO") as captured:
            store.set("abc", "done", kind="reply", account="ana-garcia-0100")
            store.set("def", "denied", kind="email", account="personal")
        output = "\n".join(captured.output)
        self.assertIn("Action abc (reply, other): done", output)
        self.assertIn("Action def (email, personal): denied", output)
        self.assertNotIn("ana-garcia", output)
        self.assertEqual(store.get("abc")["account"], "ana-garcia-0100")   # kept in actions.json as written


# --------------------------------------------------------------------------
# key=value lines: the other proposal kinds (all examples invented)
# --------------------------------------------------------------------------

DOT = " \u00b7 "
REPLY_BODY = r"Hi both,\nShall we keep it at noon with the two of us, or move it to 2 PM?\nThanks"
REPLY_PAIRS = (("acct", "work"), ("thread", "18c0ffee00000001"), ("msgid", "CAExample0001@mail.example.com"),
               ("gmid", ""), ("to", "ana@example.edu, ben@example.edu"), ("cc", ""),
               ("subject", "Re: Thursday noon meeting"), ("replied", "no"), ("due", ""),
               ("link", "https://mail.google.com/mail/#all/18c0ffee00000001"), ("body", REPLY_BODY))
EMAIL_PAIRS = (("acct", "personal"), ("to", "office@example.edu"), ("cc", ""),
               ("subject", "Question about the lab schedule"), ("due", ""), ("link", ""),
               ("body", r"Hello,\nIs the lab open on Saturday?\nThanks"))
RSVP_PAIRS = (("acct", "work"), ("event", "abc123def456ghi789"), ("cal", "primary"), ("answer", "yes"),
              ("notify", "all"), ("title", "Speaker series"), ("at", "2026-10-06 17:00-18:00"),
              ("due", "2026-10-06"), ("link", "https://calendar.google.com/calendar/event?eid=ZXhhbXBsZQ"),
              ("body", ""))
MOVE_PAIRS = (("acct", "work"), ("event", "abc123def456ghi789_20261008T190000Z"), ("cal", "primary"),
              ("when", "2026-10-08 14:00-15:00"), ("notify", "all"), ("title", "Project sync"),
              ("at", "2026-10-08 12:00-13:00"), ("link", ""), ("body", "Moving to 2 PM so everyone can join."))
CANCEL_PAIRS = (("acct", "personal"), ("event", "zyx987wvu654tsr321"), ("cal", "primary"), ("notify", "all"),
                ("title", "Study group"), ("at", "2026-10-09 18:00-19:00"), ("link", ""), ("body", ""))
SHARE_PAIRS = (("acct", "personal"), ("file", "1AbCdEfGhIjKlMnOpQrStUvWxYz0123"), ("who", "sam@example.com"),
               ("role", "viewer"), ("title", "Trip budget"),
               ("link", "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123/edit"))
SLACK_PAIRS = (("team", "T00000000"), ("channel", "D00000000"), ("ts", "1700000000.000100"), ("thread", ""),
               ("who", "Sam"), ("said", "are you free friday?"),
               ("link", "https://example.slack.com/archives/D00000000/p1700000000000100"),
               ("body", "Yes! Friday after 4 works."))
TODO_PAIRS = (("title", "Work on Problem set 3"), ("due", "2026-10-07 23:59"), ("block", "2026-10-06 19:00-21:00"),
              ("acct", ""), ("link", "https://example.instructure.com/courses/1/assignments/2"))
OPEN_PAIRS = (("title", "Lab safety form"), ("link", "https://docs.google.com/forms/d/e/EXAMPLE/viewform"))
EXAMPLES = {"Reply": REPLY_PAIRS, "Email": EMAIL_PAIRS, "RSVP": RSVP_PAIRS, "Move": MOVE_PAIRS,
            "Cancel": CANCEL_PAIRS, "Share": SHARE_PAIRS, "Slack": SLACK_PAIRS, "Todo": TODO_PAIRS,
            "Open": OPEN_PAIRS}
LABELS = {REPLY: "Reply", EMAIL: "Email", RSVP: "RSVP", MOVE: "Move", CANCEL: "Cancel", SHARE: "Share",
          SLACK: "Slack", TODO: "Todo", OPEN: "Open"}


def kv_line(label: str, pairs: tuple[tuple[str, str], ...], **changes: str | None) -> str:
    """"<label>: key=value | ..." from ``pairs``; ``changes`` replace values (None drops the key)."""
    items = []
    for key, value in pairs:
        if key in changes:
            if changes[key] is None:
                continue
            value = changes[key]
        items.append(f"{key}={value}")
    return f"{label}: " + " | ".join(items)


def example(label: str, **changes: str | None) -> ProposedAction:
    return parse_action_line(kv_line(label, EXAMPLES[label], **changes))


def sha16(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


class StructuredDetectionTests(unittest.TestCase):

    def test_every_label_and_spelling(self) -> None:
        cases = {"Reply": REPLY, "reply": REPLY, "REPLY": REPLY, "Email": EMAIL, "E-mail": EMAIL,
                 "e-mail": EMAIL, "RSVP": RSVP, "Rsvp": RSVP, "Move": MOVE, "Cancel": CANCEL, "Share": SHARE,
                 "Slack": SLACK, "Todo": TODO, "To-do": TODO, "To do": TODO, "TODO": TODO, "Open": OPEN}
        for label, kind in cases.items():
            with self.subTest(label=label):
                line = kv_line(label, EXAMPLES[LABELS[kind]])
                action = parse_action_line(line)
                self.assertTrue(action.structured)
                self.assertEqual(action.kind, kind)
                self.assertEqual(action.error, "")
                self.assertTrue(action.decidable)
                self.assertEqual(action.raw, line)
        self.assertEqual(STRUCTURED_KINDS, frozenset(LABELS))

    def test_free_text_lines_stay_as_before(self) -> None:
        for line, kind in (("Reply: Carol about the draft", REPLY), ("Reply: x=1 is wrong", REPLY),
                           ("Todo: renew parking permit", TODO), ("Move: the dentist to Friday", MOVE),
                           ("Reply to Bob: yes", REPLY), ("Frobnicate: the widgets | 2026-10-09", "frobnicate"),
                           ("Reply to Bob: " + kv_line("Reply", REPLY_PAIRS)[len("Reply: "):], REPLY),
                           ("Open: color=blue | link=https://docs.google.com/x", OPEN),
                           ("Calendar: title=Sync | 2026-10-09 3pm", CALENDAR)):
            with self.subTest(line=line):
                action = parse_action_line(line)
                self.assertFalse(action.structured)
                self.assertEqual(action.kind, kind)
                self.assertEqual((action.fields, action.account, action.link, action.body), ((), "", "", ""))
        self.assertEqual(parse_action_line("Reply: x=1 is wrong").title, "x=1 is wrong")
        self.assertFalse(parse_action_line("Reply: Carol about the draft").decidable)
        self.assertTrue(parse_action_line("Calendar: title=Sync | 2026-10-09 3pm").actionable)

    def test_markdown_wrappers_leaders_and_emoji_before_the_label(self) -> None:
        base = kv_line("Reply", REPLY_PAIRS)
        rest = base[len("Reply: "):]
        prefixes = ("**Reply:** ", "**Reply**: ", "__Reply__: ", "*Reply:* ", "*Reply*: ", "_Reply_: ",
                    "- Reply: ", "* Reply: ", "+ Reply: ", "\u2022 Reply: ", "1. Reply: ", "2) Reply: ",
                    "[ ] Reply: ", "[x] Reply: ", "> Reply: ", "- [ ] **Reply:** ", "\U0001F4E7 Reply: ",
                    "\u2709\ufe0f Reply: ", "- \U0001F4E7 **Reply**: ", "  Reply: ")
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                action = parse_action_line(prefix + rest)
                self.assertTrue(action.structured)
                self.assertEqual(action.error, "")
                self.assertEqual(action.raw, base)
                self.assertEqual(action.id, parse_action_line(base).id)

    def test_no_break_spaces_count_as_spaces(self) -> None:
        base = kv_line("Open", OPEN_PAIRS)
        for space in ("\u00a0", "\u2007", "\u202f"):
            with self.subTest(space=hex(ord(space))):
                action = parse_action_line(base.replace(" ", space))
                self.assertEqual((action.error, action.title, action.raw), ("", "Lab safety form", base))

    def test_never_raises(self) -> None:
        lines = ("Reply:", "Reply: acct=", "Reply: =x", "Email: acct=work | to=" + "a" * 13000,
                 "Open: title=\x00 | link=https://docs.google.com/x", "Todo: title=x | due=9999-99-99",
                 "Move: acct=work | event=abcdef | when=2026-10-08 23:00-02:00 to 2026-10-09",
                 "Slack: channel=D00000 | body=" + "\\" * 50, "Share: acct=a | who=<<>> | file=x")
        for line in lines:
            with self.subTest(line=line[:40]):
                action = parse_action_line(line)
                self.assertIsInstance(action, ProposedAction)
                self.assertRegex(action.id, r"^[0-9a-f]{16}$")

    def test_a_bug_in_the_reader_is_an_error_not_an_exception(self) -> None:
        line = kv_line("Reply", REPLY_PAIRS)
        with mock.patch.object(actions._StructuredReader, "read", side_effect=RuntimeError("boom ana@example.edu")):
            with self.assertLogs(ACTIONS_LOGGER, level="WARNING") as captured:
                action = parse_action_line(line)
        self.assertEqual((action.kind, action.structured, action.error), (REPLY, True, "could not read this line"))
        self.assertFalse(action.decidable)
        output = "\n".join(captured.output)
        self.assertIn("Could not parse a reply line (RuntimeError)", output)
        self.assertNotIn("ana@example.edu", output)


class SplitTests(unittest.TestCase):

    def test_body_takes_the_rest_of_the_line(self) -> None:
        action = parse_action_line("Email: acct=work | to=a@example.com | subject=Hi | body=one | to=b@example.com"
                                   " | body=two | bcc=c@example.com")
        self.assertEqual(action.error, "")
        self.assertEqual(action.recipients(), ("a@example.com",))
        self.assertEqual(action.body, "one | to=b@example.com | body=two | bcc=c@example.com")
        self.assertEqual(parse_action_line("Reply: body=hi").error, "missing acct=")

    def test_a_value_with_a_pipe_is_joined_back(self) -> None:
        action = parse_action_line("Email: acct=work | to=a@example.com | subject=Q3 | planning | review | body=x")
        self.assertEqual(action.error, "")
        self.assertEqual(action.title, "Q3 | planning | review")
        self.assertEqual(action.field("subject"), "Q3 | planning | review")

    def test_a_key_twice_is_an_error(self) -> None:
        cases = {
            "Email: acct=work | to=a@example.com | to=b@example.com | subject=x | body=y": "to= appears twice",
            "Email: acct=work | to=a@example.com | cc= | CC= | subject=x | body=y": "cc= appears twice",
            "Email: acct=work | to=a@example.com | subject=x | Acct=home | body=y": "acct= appears twice",
            # An address slipped into a subject is caught: every key is always written.
            "Email: acct=work | to=a@example.com | cc= | subject=Hi | to=evil@example.net | due= | link= | body=y":
                "to= appears twice",
            "Email: acct=work | to=a@example.com | bcc=b@example.com | subject=x | body=y": "bcc= is not supported",
        }
        for line, error in cases.items():
            with self.subTest(line=line):
                action = parse_action_line(line)
                self.assertEqual(action.error, error)
                self.assertTrue(action.structured)
                self.assertFalse(action.decidable)

    def test_a_second_body_is_part_of_the_body(self) -> None:
        action = parse_action_line("Email: acct=work | to=a@example.com | subject=x | body=first | body=second")
        self.assertEqual((action.error, action.body), ("", "first | body=second"))

    def test_unknown_keys_are_ignored_and_only_counted(self) -> None:
        with self.assertLogs(ACTIONS_LOGGER, level="DEBUG") as captured:
            action = parse_action_line(
                "Open: title=Form | colour=blue | size=3 | link=https://docs.google.com/forms/x")
        self.assertEqual(action.error, "")
        self.assertEqual(action.fields, (("title", "Form"), ("link", "https://docs.google.com/forms/x")))
        output = "\n".join(captured.output)
        self.assertIn("Ignored 2 unknown key(s) (open line)", output)
        self.assertNotIn("blue", output)
        self.assertNotIn("colour", output)

    def test_empty_optional_and_empty_required_values(self) -> None:
        action = parse_action_line("Email: acct=work | to=a@example.com | cc= | subject=Hi | due= | link= | body=x")
        self.assertEqual(action.error, "")
        self.assertEqual((action.cc(), action.field("due"), action.link), ((), "", ""))
        self.assertIsNone(action.due())
        for line, error in (("Email: acct=work | to=a@example.com | subject= | body=x", "missing subject="),
                            ("Email: acct=work | to=a@example.com | subject=Hi | body=", "missing body="),
                            ("Email: acct=work | to=a@example.com | subject=Hi | body= \\n ", "missing body="),
                            ("Email: acct= | to=a@example.com | subject=Hi | body=x", "missing acct=")):
            with self.subTest(line=line):
                self.assertEqual(parse_action_line(line).error, error)

    def test_escapes_in_body_and_said_only(self) -> None:
        action = parse_action_line(r"Slack: channel=D00000000 | said=line one\nline two | body=a\nb\\nc\td\\")
        self.assertEqual(action.error, "")
        self.assertEqual(action.body, "a\nb\\nc\\td\\")
        self.assertEqual(action.field("said"), "line one\nline two")
        other = parse_action_line(r"Open: title=C:\new folder | link=https://docs.google.com/x")
        self.assertEqual(other.title, r"C:\new folder")

    def test_values_keep_markdown_characters(self) -> None:
        action = parse_action_line("Email: acct=work | to=a@example.com | subject=*Q3* _plan_ <draft> `x` | "
                                   "body=**Bold** <b>hi</b> `code` snake_case_id")
        self.assertEqual(action.error, "")
        self.assertEqual(action.title, "*Q3* _plan_ <draft> `x`")
        self.assertEqual(action.body, "**Bold** <b>hi</b> `code` snake_case_id")
        self.assertEqual(example("Move").field("event"), "abc123def456ghi789_20261008T190000Z")

    def test_line_cap(self) -> None:
        fixed = "acct=work | to=a@example.com | subject=Hi | body="
        fits = parse_action_line("Email: " + fixed + "x" * 5000)
        self.assertEqual(fits.error, "")
        rest = fixed + "x" * (12001 - len(fixed))
        action = parse_action_line("Email: " + rest)
        self.assertEqual(action.error, "the line is too long (12001 characters, at most 12000)")
        self.assertEqual(parse_action_line("Email: " + rest[:-1]).error,
                         f"body= is too long ({12000 - len(fixed)} characters, at most 5000)")

    def test_long_runs_of_spaces_parse_in_linear_time(self) -> None:
        # Lines are read on the GUI thread; a quadratic pattern took about 0.6 s per line like these.
        spaces = " " * 11900
        lines = ("Email: acct=work | to=a" + spaces + "b | subject=s | body=x",
                 "Email: acct=work | to=Ana" + spaces + "<a@example.com> | subject=s | body=x",
                 "Email: acct=work | to=a@example.com | subject=a" + spaces + "b | body=x",
                 "Email: acct=work" + spaces + "x | to=a@example.com | subject=s | body=x",
                 "Share: acct=work | file=1AbCdEfGhIjKlMnOpQrStUvWxYz0123 | who=a" + spaces + "b",
                 "Todo: title=a" + spaces + "b | due=2026-10-07")
        for line in lines:
            with self.subTest(line=line[:30]):
                started = time.perf_counter()
                action = parse_action_line(line)
                self.assertLess(time.perf_counter() - started, 0.15)
                self.assertTrue(action.structured)
        self.assertEqual(parse_action_line(lines[1]).recipients(), ("a@example.com",))


class FieldValidationTests(unittest.TestCase):

    def test_account_alias(self) -> None:
        self.assertEqual(example("Email", acct="Work").account, "work")
        self.assertEqual(example("Email", acct="school_mail-2").account, "school_mail-2")
        self.assertEqual(example("Email", acct="Work Mail").error,
                         'acct=: "Work Mail" is not an account name (letters, digits, - or _)')
        for bad in ("9lives", "a" * 25, "w@rk"):
            with self.subTest(bad=bad):
                self.assertIn("is not an account name", example("Email", acct=bad).error)

    def test_bad_values_are_quoted_and_cut_to_40_characters(self) -> None:
        error = example("Email", acct="x" * 30 + " " + "y" * 30).error
        quoted = error.split(": ", 1)[1].split(" is not")[0]
        self.assertEqual(quoted, '"' + "x" * 19 + "..." + "y" * 18 + '"')

    def test_gmail_ids_and_message_ids(self) -> None:
        self.assertEqual(example("Reply", thread="x").error, 'thread=: "x" is not a Gmail id')
        for written in ("CAExample0001@mail.example.com", "<CAExample0001@mail.example.com>"):
            with self.subTest(msgid=written):
                self.assertEqual(example("Reply", msgid=written).field("msgid"), "<CAExample0001@mail.example.com>")
        for bad in ("abc", "a@b@c", "@example.com", "a@"):
            with self.subTest(bad=bad):
                self.assertEqual(example("Reply", msgid=bad).error, f'msgid=: "{bad}" is not a Message-ID')
        only_gmid = example("Reply", msgid="", gmid="18c0ffee00000002")
        self.assertEqual((only_gmid.error, only_gmid.field("gmid"), only_gmid.field("msgid")),
                         ("", "18c0ffee00000002", ""))
        self.assertEqual(example("Reply", msgid=None, gmid="bad id!").error, 'gmid=: "bad id!" is not a Gmail id')
        self.assertEqual(example("Reply", msgid="", gmid="").error, "missing msgid= (or gmid=)")

    def test_addresses_are_normalized(self) -> None:
        action = example("Reply", to="Ana Lima <Ana@Example.EDU>; ben@example.edu, ana@example.edu",
                         cc="ben@example.edu, Cy <cy@EXAMPLE.edu>;")
        self.assertEqual(action.error, "")
        self.assertEqual(action.recipients(), ("Ana@example.edu", "ben@example.edu"))
        self.assertEqual(action.cc(), ("cy@example.edu",))
        self.assertEqual(action.field("to"), "Ana@example.edu, ben@example.edu")
        self.assertEqual(action.field("cc"), "cy@example.edu")
        self.assertEqual(example("Reply", cc="ana@example.edu").field("cc"), "")

    def test_bad_addresses(self) -> None:
        for bad in ("bob@", "@example.edu", "a..b@example.edu", ".a@example.edu", "a.@example.edu",
                    "a b@example.edu", "a@example", "a@-example.edu", "a@example.e", 'a"b@example.edu',
                    "Ana <ana@example.edu", "x" * 65 + "@example.edu", "a@" + ("b" * 60 + ".") * 5 + "edu"):
            with self.subTest(bad=bad[:30]):
                error = example("Email", to=bad).error
                self.assertTrue(error.startswith("to=: ") and error.endswith(" is not an email address"), error)
        self.assertEqual(example("Email", to="bob@").error, 'to=: "bob@" is not an email address')
        self.assertEqual(example("Email", cc="x@").error, 'cc=: "x@" is not an email address')

    def test_recipient_cap(self) -> None:
        five = ", ".join(f"p{n}@example.edu" for n in range(5))
        self.assertEqual(example("Email", to=five).error, "")
        self.assertEqual(example("Email", to=five, cc="P0@example.edu").error, "")   # counted once
        self.assertEqual(example("Email", to="p0@example.edu, p1@example.edu, p2@example.edu",
                                 cc="p3@example.edu, p4@example.edu, p5@example.edu").error,
                         "to= and cc= name 6 addresses (at most 5)")
        self.assertEqual(MAX_RECIPIENTS, 5)
        self.assertEqual(example("Email", to=",;").error, "missing to=")

    def test_subject(self) -> None:
        self.assertEqual(example("Email", subject="Line one\nline two").error, "subject= has a line break")
        self.assertEqual(example("Email", subject="Line one\rline two").error, "subject= has a line break")
        self.assertEqual(example("Email", subject="Bell\x07").error, "subject= has a control character")
        self.assertEqual(example("Email", subject="  Lots   of\tspace ").title, "Lots of space")
        self.assertEqual(example("Email", subject="s" * 250).error, "")
        self.assertEqual(example("Email", subject="s" * 251).error,
                         "subject= is too long (251 characters, at most 250)")
        self.assertEqual(example("Reply", subject="Thursday noon meeting").title, "Re: Thursday noon meeting")
        self.assertEqual(example("Reply", subject="RE: Thursday").title, "RE: Thursday")
        self.assertEqual(example("Email", subject="Re thursday").title, "Re thursday")

    def test_invisible_format_characters_are_removed(self) -> None:
        # Direction overrides and zero-width characters could make a value show as something else.
        self.assertEqual(example("Email", subject="Invoice \u202egnp.exe").title, "Invoice gnp.exe")
        self.assertEqual(example("Open", title="Lab\u200b form\u2066\u2069").title, "Lab form")
        self.assertEqual(example("Slack", said="are you \u202efree\u200d?").field("said"), "are you free?")
        self.assertEqual(example("Email", body="Hi\u200b there\ufeff").body, "Hi there")
        self.assertEqual(example("Reply", msgid="CAEx\u202eample@mail.example.com").field("msgid"),
                         "<CAExample@mail.example.com>")
        # A key written with a zero-width space inside is a key again, so a slipped-in "to=" is caught.
        hidden = kv_line("Email", EMAIL_PAIRS, subject="Hi | t\u200bo=evil@example.net")
        self.assertEqual(parse_action_line(hidden).error, "to= appears twice")
        # A combined emoji keeps its zero-width joiner.
        emoji = "\U0001f469\U0001f3fd\u200d\U0001f4bb"
        self.assertEqual(example("Todo", title=f"{emoji} Code review").title, f"{emoji} Code review")

    def test_ids_are_printable_ascii(self) -> None:
        for bad in ("abc\x01def@mail.example.com", "abc\x00@mail.example.com", "caf\u00e9@mail.example.com"):
            with self.subTest(msgid=ascii(bad)):
                self.assertIn("is not a Message-ID", example("Reply", msgid=bad).error)
        for bad in ("a\x00b", "team\x7f@group.calendar.google.com", "caf\u00e9@group.calendar.google.com",
                    "a<b>"):
            with self.subTest(cal=ascii(bad)):
                self.assertIn("is not a calendar id", example("RSVP", cal=bad).error)
        self.assertEqual(example("Reply", msgid="a.b+c_d=e/f?g{h}~i@[mail.example.com]").error, "")

    def test_title_and_who_caps(self) -> None:
        self.assertEqual(example("Open", title="t" * 200).error, "")
        self.assertEqual(example("Open", title="t" * 201).error, "title= is too long (201 characters, at most 200)")
        self.assertEqual(example("Slack", who="w" * 80).title, "Slack message from " + "w" * 80)
        self.assertEqual(example("Slack", who="w" * 81).error, "who= is too long (81 characters, at most 80)")
        self.assertEqual(example("Todo", title="Line\nbreak").error, "title= has a line break")

    def test_due(self) -> None:
        cases = {"2026-10-07": date(2026, 10, 7), "2026-10-07 23:59": dt(7, 23, 59),
                 "2026-10-07 11:59 PM": dt(7, 23, 59), "2026-10-07 11:59pm": dt(7, 23, 59),
                 "2026-10-07T09:30": dt(7, 9, 30), "2026-10-07 noon": dt(7, 12)}
        for written, due in cases.items():
            with self.subTest(due=written):
                action = example("Todo", due=written)
                self.assertEqual(action.error, "")
                self.assertEqual(action.due(), due)
        self.assertEqual(example("Todo").field("due"), "2026-10-07T23:59")
        self.assertEqual(example("Todo", due="2026-10-07").field("due"), "2026-10-07")
        errors = {"2026-02-30": 'due=: "2026-02-30" is not a real date',
                  "2026-10-07 11": 'due=: the time "11" needs AM/PM or HH:MM (24-hour)',
                  "2026-10-07 25:00": 'due=: could not read the time "25:00"',
                  "tomorrow": 'due=: "tomorrow" is not a date (YYYY-MM-DD, optionally with a time)',
                  "10/07/2026": 'due=: "10/07/2026" is not a date (YYYY-MM-DD, optionally with a time)'}
        for written, error in errors.items():
            with self.subTest(due=written):
                self.assertEqual(example("Todo", due=written).error, error)

    def test_time_ranges(self) -> None:
        action = example("Todo")
        self.assertEqual((action.start, action.end), (dt(6, 19), dt(6, 21)))
        self.assertEqual(action.field("block"), "2026-10-06T19:00/2026-10-06T21:00")
        self.assertEqual(example("Todo", block="2026-10-06 7pm-9pm").start, dt(6, 19))
        self.assertEqual(example("Todo", block="2026-10-06 19:00-19:05").error, "")
        self.assertEqual(example("Todo", block="2026-10-06 07:00-19:00").error, "")
        errors = {"2026-10-06": "block=: give a time range like 2026-10-06 19:00-21:00",
                  "2026-10-06 19:00-19:04": "block=: the time range must be 5 minutes to 12 hours long",
                  "2026-10-06 07:00-19:01": "block=: the time range must be 5 minutes to 12 hours long",
                  "2026-10-06 25:00": 'block=: could not read the time "25:00"',
                  "next week": "block=: no date (expected YYYY-MM-DD, optionally with HH:MM-HH:MM)"}
        for written, error in errors.items():
            with self.subTest(block=written):
                self.assertEqual(example("Todo", block=written).error, error)
        self.assertEqual(example("Move", when="2026-10-08").error,
                         "when=: give a time range like 2026-10-08 13:00-14:00")
        move = example("Move")
        self.assertEqual((move.start, move.end), (dt(8, 14), dt(8, 15)))

    def test_at_is_display_only(self) -> None:
        action = example("RSVP", at="sometime soon")
        self.assertEqual(action.error, "")
        self.assertEqual(action.warnings, ("at= could not be read; not shown",))
        self.assertEqual(action.field("at"), "")
        self.assertTrue(action.decidable)
        self.assertEqual(example("RSVP", at="2026-10-06").field("at"), "2026-10-06/2026-10-06")
        self.assertEqual(example("RSVP").field("at"), "2026-10-06T17:00/2026-10-06T18:00")

    def test_word_choices_and_defaults(self) -> None:
        answers = (("yes", "yes"), ("Accept", "yes"), ("accepted", "yes"), ("going", "yes"), ("NO", "no"),
                   ("decline", "no"), ("Declined", "no"), ("maybe", "maybe"), ("tentative", "maybe"))
        for written, answer in answers:
            with self.subTest(answer=written):
                self.assertEqual(example("RSVP", answer=written).field("answer"), answer)
        for written, notify in (("all", "all"), ("external", "external"), ("externalOnly", "external"),
                                ("NONE", "none"), ("", "all"), (None, "all")):
            with self.subTest(notify=written):
                self.assertEqual(example("Cancel", notify=written).field("notify"), notify)
        self.assertEqual(example("Reply", replied=None).field("replied"), "unknown")
        self.assertEqual(example("Share", role="").field("role"), "viewer")
        self.assertEqual(example("Share", role="Editor").field("role"), "editor")
        self.assertEqual(example("RSVP", cal=None).field("cal"), "primary")
        self.assertEqual(example("RSVP", cal="team@group.calendar.google.com").field("cal"),
                         "team@group.calendar.google.com")
        errors = {("RSVP", "answer", "perhaps"): "answer=: use yes, no or maybe",
                  ("Cancel", "notify", "some"): "notify=: use all, external or none",
                  ("Reply", "replied", "maybe"): "replied=: use yes, no or unknown",
                  ("Share", "role", "owner"): "role=: use viewer, commenter or editor",
                  ("RSVP", "cal", "my cal"): 'cal=: "my cal" is not a calendar id'}
        for (label, key, value), error in errors.items():
            with self.subTest(key=key):
                self.assertEqual(example(label, **{key: value}).error, error)

    def test_ids_of_events_files_and_slack(self) -> None:
        errors = {("RSVP", "event", "abc"): 'event=: "abc" is not a calendar event id',
                  ("Share", "file", "short"): 'file=: "short" is not a Drive file id',
                  ("Slack", "team", "X0000"): 'team=: "X0000" is not a Slack workspace id',
                  ("Slack", "channel", "Z0000"): 'channel=: "Z0000" is not a Slack channel id',
                  ("Slack", "ts", "12345"): 'ts=: "12345" is not a Slack message ts',
                  ("Slack", "thread", "1.2"): 'thread=: "1.2" is not a Slack message ts',
                  ("Share", "who", "sam@example.com, kim@example.com"): "who=: give exactly one email address"}
        for (label, key, value), error in errors.items():
            with self.subTest(key=key):
                self.assertEqual(example(label, **{key: value}).error, error)
        self.assertEqual(example("Slack", thread="1700000000.000001").field("thread"), "1700000000.000001")
        self.assertEqual(example("Share", who="Sam <Sam@Example.com>").field("who"), "Sam@example.com")

    def test_body_caps_and_control_characters(self) -> None:
        for label, cap in (("Reply", 5000), ("Email", 5000), ("Slack", 5000), ("RSVP", 1000), ("Move", 1000),
                           ("Cancel", 1000)):
            with self.subTest(label=label):
                self.assertEqual(example(label, body="b" * cap).body, "b" * cap)
                self.assertEqual(example(label, body="b" * (cap + 1)).error,
                                 f"body= is too long ({cap + 1} characters, at most {cap})")
        self.assertEqual(example("Slack", said="s" * 400).field("said"), "s" * 400)
        self.assertEqual(example("Slack", said="s" * 401).error, "said= is too long (401 characters, at most 400)")
        self.assertEqual(example("Email", body="a\x07b\tc\r\nd").body, "ab\tc\nd")


class LinkAllowlistTests(unittest.TestCase):

    def test_built_in_hosts(self) -> None:
        self.assertEqual(BUILTIN_LINK_HOSTS, ("mail.google.com", "docs.google.com", "drive.google.com",
                                              "calendar.google.com", "meet.google.com", "*.slack.com",
                                              "*.instructure.com"))
        for url in ("https://mail.google.com/mail/#all/1", "https://docs.google.com/document/d/x/edit",
                    "https://drive.google.com/file/d/x", "https://calendar.google.com/calendar/event?eid=1",
                    "https://meet.google.com/aaa-bbbb-ccc", "https://example.slack.com/archives/D0/p1",
                    "https://a.b.slack.com/x", "https://example.instructure.com/courses/1",
                    "https://www.google.com/calendar/event?eid=1", "HTTPS://DOCS.GOOGLE.COM/x",
                    "https://docs.google.com:443/x", "https://docs.google.com./x"):
            with self.subTest(url=url):
                self.assertTrue(link_allowed(url))

    def test_refused_links(self) -> None:
        for url in ("https://slack.com/x", "https://instructure.com/x", "https://www.google.com/search?q=x",
                    "https://www.google.com/calendarx", "http://docs.google.com/x", "ftp://docs.google.com/x",
                    "https://user@docs.google.com/x", "https://user:pw@docs.google.com/x",
                    "https://docs.google.com:8443/x", "https://docs.google.com:x/", "https://docs.google.com.evil.example/",
                    "https://evil.example/docs.google.com", "https://d\u00f6cs.google.com/x",
                    "https://xn--dcs-xoa.google.com/x", "https://docs.google.com/a b", "https://docs.google.com/\tx",
                    "https://docs.google.com/\u200bx", "https://docs.google.com\\@evil.example/",
                    "https://docs.google.com/" + "a" * 2030, "javascript:alert(1)", "", "docs.google.com/x",
                    "https:///x", "https://docs%2egoogle.com/x"):
            with self.subTest(url=url[:50]):
                self.assertFalse(link_allowed(url))
        self.assertTrue(link_allowed("https://docs.google.com/" + "a" * (2048 - 24)))

    def test_dot_segments_are_refused(self) -> None:
        # A browser removes "/../" (also written %2e%2e) before it asks the host, so
        # "www.google.com/calendar/../url?q=..." would open Google's redirector.
        for url in ("https://www.google.com/calendar/../url?q=https://evil.example",
                    "https://www.google.com/calendar/%2e%2e/url?q=https://evil.example",
                    "https://www.google.com/calendar/.%2E/amp/s/evil.example/p",
                    "https://www.google.com/calendar/%2E./search?q=x",
                    "https://www.google.com/calendar/./x", "https://www.google.com/calendar/%2e/x",
                    "https://www.google.com/calendar/x/..", "https://docs.google.com/document/../x"):
            with self.subTest(url=url):
                self.assertFalse(link_allowed(url))
        self.assertEqual(example("Open", link="https://www.google.com/calendar/../url?q=x").error,
                         "not an https link")
        for url in ("https://www.google.com/calendar/event?eid=1", "https://docs.google.com/document/d/a..b/edit",
                    "https://docs.google.com/x/.../y", "https://docs.google.com/x?next=../y"):
            with self.subTest(url=url):
                self.assertTrue(link_allowed(url))

    def test_extra_hosts(self) -> None:
        self.assertTrue(link_allowed("https://forms.example.edu/x", ("forms.example.edu",)))
        self.assertTrue(link_allowed("https://forms.example.edu/x", ["FORMS.Example.EDU"]))
        self.assertFalse(link_allowed("https://x.forms.example.edu/x", ("forms.example.edu",)))
        self.assertTrue(link_allowed("https://a.example.edu/x", ("*.example.edu",)))
        self.assertTrue(link_allowed("https://a.b.example.edu/x", ("*.example.edu",)))
        self.assertFalse(link_allowed("https://example.edu/x", ("*.example.edu",)))
        self.assertFalse(link_allowed("https://badexample.edu/x", ("*.example.edu",)))
        self.assertFalse(link_allowed("http://forms.example.edu/x", ("forms.example.edu",)))
        self.assertTrue(link_allowed("https://forms.example.edu/x", "forms.example.edu"))   # one string works
        # A punycode (IDN) host only when listed by its exact name, never by a wildcard.
        self.assertFalse(link_allowed("https://xn--bcher-kva.example.edu/", ("*.example.edu",)))
        self.assertTrue(link_allowed("https://xn--bcher-kva.example.edu/", ("xn--bcher-kva.example.edu",)))

    def test_an_optional_link_that_is_refused_is_hidden_with_a_warning(self) -> None:
        action = example("Todo", link="http://lms.example.edu/a")
        self.assertEqual((action.error, action.link), ("", ""))
        self.assertEqual(action.warnings, ("Link hidden: not an https link",))
        self.assertNotIn("link", dict(action.fields))
        action = example("Reply", link="https://forms.example.net/x")
        self.assertEqual(action.link, "")
        self.assertEqual(action.warnings, ("Link hidden: forms.example.net is not on the list of hosts Open may "
                                           "open ([actions] link_hosts)",))
        listed = parse_action_line(kv_line("Reply", REPLY_PAIRS, link="https://forms.example.net/x"),
                                   link_hosts=("forms.example.net",))
        self.assertEqual((listed.link, listed.warnings), ("https://forms.example.net/x", ()))
        self.assertEqual(listed.id, action.id)   # the link never counts for the id

    def test_an_open_line_needs_a_link_it_may_open(self) -> None:
        self.assertEqual(example("Open", link="http://forms.example.net/x").error, "not an https link")
        self.assertEqual(example("Open", link="https://forms.example.net/x").error,
                         "forms.example.net is not on the list of hosts Open may open ([actions] link_hosts)")
        action = parse_action_line("Open: title=Form | link=https://forms.example.net/x",
                                   link_hosts=["forms.example.net"])
        self.assertEqual((action.error, action.link), ("", "https://forms.example.net/x"))

    def test_parsed_links_pass_the_click_time_check(self) -> None:
        for label in EXAMPLES:
            with self.subTest(label=label):
                action = example(label)
                self.assertTrue(not action.link or link_allowed(action.link))


class KindSchemaTests(unittest.TestCase):

    def test_reply(self) -> None:
        action = example("Reply")
        self.assertEqual(action.fields, (
            ("acct", "work"), ("thread", "18c0ffee00000001"), ("msgid", "<CAExample0001@mail.example.com>"),
            ("to", "ana@example.edu, ben@example.edu"), ("subject", "Re: Thursday noon meeting"), ("replied", "no"),
            ("link", "https://mail.google.com/mail/#all/18c0ffee00000001")))
        self.assertEqual((action.kind, action.account, action.title), (REPLY, "work", "Re: Thursday noon meeting"))
        self.assertEqual(action.body,
                         "Hi both,\nShall we keep it at noon with the two of us, or move it to 2 PM?\nThanks")
        self.assertEqual(action.link, "https://mail.google.com/mail/#all/18c0ffee00000001")
        self.assertEqual((action.warnings, action.start, action.end), ((), None, None))
        self.assertEqual((action.recipients(), action.cc(), action.due()),
                         (("ana@example.edu", "ben@example.edu"), (), None))

    def test_email(self) -> None:
        action = example("Email")
        self.assertEqual(action.fields, (("acct", "personal"), ("to", "office@example.edu"),
                                         ("subject", "Question about the lab schedule")))
        self.assertEqual((action.account, action.title, action.link), ("personal", "Question about the lab schedule", ""))
        self.assertEqual(action.body, "Hello,\nIs the lab open on Saturday?\nThanks")

    def test_rsvp(self) -> None:
        action = example("RSVP")
        self.assertEqual(action.fields, (
            ("acct", "work"), ("event", "abc123def456ghi789"), ("cal", "primary"), ("answer", "yes"),
            ("notify", "all"), ("title", "Speaker series"), ("at", "2026-10-06T17:00/2026-10-06T18:00"),
            ("due", "2026-10-06"), ("link", "https://calendar.google.com/calendar/event?eid=ZXhhbXBsZQ")))
        self.assertEqual((action.title, action.body, action.due()), ("Speaker series", "", date(2026, 10, 6)))
        self.assertEqual(example("RSVP", title="").title, "Calendar invitation")

    def test_move(self) -> None:
        action = example("Move")
        self.assertEqual(action.fields, (
            ("acct", "work"), ("event", "abc123def456ghi789_20261008T190000Z"), ("cal", "primary"),
            ("when", "2026-10-08T14:00/2026-10-08T15:00"), ("notify", "all"), ("title", "Project sync"),
            ("at", "2026-10-08T12:00/2026-10-08T13:00")))
        self.assertEqual((action.start, action.end, action.body),
                         (dt(8, 14), dt(8, 15), "Moving to 2 PM so everyone can join."))
        self.assertEqual(example("Move", title=None).title, "Meeting to move")

    def test_cancel(self) -> None:
        action = example("Cancel")
        self.assertEqual(action.fields, (
            ("acct", "personal"), ("event", "zyx987wvu654tsr321"), ("cal", "primary"), ("notify", "all"),
            ("title", "Study group"), ("at", "2026-10-09T18:00/2026-10-09T19:00")))
        self.assertEqual((action.account, action.title, action.start), ("personal", "Study group", None))
        self.assertEqual(example("Cancel", title="").title, "Meeting to cancel")

    def test_share(self) -> None:
        action = example("Share")
        self.assertEqual(action.fields, (
            ("acct", "personal"), ("file", "1AbCdEfGhIjKlMnOpQrStUvWxYz0123"), ("who", "sam@example.com"),
            ("role", "viewer"), ("title", "Trip budget"),
            ("link", "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123/edit")))
        self.assertEqual(example("Share", title="").title, "File share request")

    def test_slack(self) -> None:
        action = example("Slack")
        self.assertEqual(action.fields, (
            ("team", "T00000000"), ("channel", "D00000000"), ("ts", "1700000000.000100"), ("who", "Sam"),
            ("said", "are you free friday?"),
            ("link", "https://example.slack.com/archives/D00000000/p1700000000000100")))
        self.assertEqual((action.account, action.title, action.body),
                         ("", "Slack message from Sam", "Yes! Friday after 4 works."))
        self.assertEqual(example("Slack", who="").title, "Slack message")

    def test_todo(self) -> None:
        action = example("Todo")
        self.assertEqual(action.fields, (
            ("title", "Work on Problem set 3"), ("due", "2026-10-07T23:59"),
            ("block", "2026-10-06T19:00/2026-10-06T21:00"),
            ("link", "https://example.instructure.com/courses/1/assignments/2")))
        self.assertEqual((action.title, action.due(), action.start, action.end, action.warnings),
                         ("Work on Problem set 3", dt(7, 23, 59), dt(6, 19), dt(6, 21), ()))
        self.assertEqual(example("Todo", acct="Work").account, "work")

    def test_open(self) -> None:
        action = example("Open")
        self.assertEqual(action.fields, (("title", "Lab safety form"),
                                         ("link", "https://docs.google.com/forms/d/e/EXAMPLE/viewform")))
        self.assertEqual((action.title, action.link),
                         ("Lab safety form", "https://docs.google.com/forms/d/e/EXAMPLE/viewform"))

    def test_missing_required_keys(self) -> None:
        required = {"Reply": ("acct", "thread", "to", "subject", "body"), "Email": ("acct", "to", "subject", "body"),
                    "RSVP": ("acct", "event", "answer"), "Move": ("acct", "event", "when"),
                    "Cancel": ("acct", "event"), "Share": ("acct", "file", "who"), "Slack": ("channel", "body"),
                    "Todo": ("title", "due"), "Open": ("title", "link")}
        for label, keys in required.items():
            for key in keys:
                for change in (None, ""):
                    with self.subTest(label=label, key=key, change=change):
                        action = example(label, **{key: change})
                        self.assertEqual(action.error, f"missing {key}=")
                        self.assertTrue(action.structured)
                        self.assertFalse(action.decidable)

    def test_the_first_error_in_key_order_wins(self) -> None:
        action = example("Reply", thread="x", to="bob@")
        self.assertEqual(action.error, 'thread=: "x" is not a Gmail id')
        self.assertEqual(action.account, "work")   # what was read before the error is kept

    def test_block_after_the_due_time_warns(self) -> None:
        self.assertEqual(example("Todo", due="2026-10-06 20:00").warnings, (BLOCK_WARNING,))
        self.assertEqual(example("Todo", due="2026-10-06", block="2026-10-06 22:00-23:30").warnings, ())
        self.assertEqual(example("Todo", due="2026-10-06", block="2026-10-06 23:00-01:00").warnings,
                         (BLOCK_WARNING,))
        self.assertEqual(example("Todo", block="").warnings, ())
        self.assertEqual(BLOCK_WARNING, "The block ends after the due time")

    def test_replied(self) -> None:
        done = example("Reply", replied="yes")
        self.assertEqual(done.error, "")
        self.assertFalse(done.decidable)
        self.assertFalse(done.actionable)
        self.assertEqual((done.describe(TODAY), done.spoken(TODAY)), ("", ""))
        for replied in ("unknown", "", None):
            with self.subTest(replied=replied):
                action = example("Reply", replied=replied)
                self.assertEqual(action.warnings, (REPLIED_WARNING,))
                self.assertTrue(action.decidable)
        self.assertEqual(example("Reply").warnings, ())
        self.assertEqual(REPLIED_WARNING, "Couldn't tell if you already replied - check the thread first")


class StructuredIdTests(unittest.TestCase):

    def test_exact_ids(self) -> None:
        expected = {
            "Reply": sha16("reply", "work", "<CAExample0001@mail.example.com>"),
            "Email": sha16("email", "personal", "office@example.edu", "question about the lab schedule"),
            "RSVP": sha16("rsvp", "work", "primary", "abc123def456ghi789"),
            "Move": sha16("move", "work", "primary", "abc123def456ghi789_20261008T190000Z", "2026-10-08T14:00",
                          "2026-10-08T15:00"),
            "Cancel": sha16("cancel", "personal", "primary", "zyx987wvu654tsr321"),
            "Share": sha16("share", "personal", "1AbCdEfGhIjKlMnOpQrStUvWxYz0123", "sam@example.com"),
            "Slack": sha16("slack", "", "T00000000", "D00000000", "", "1700000000.000100"),
            "Todo": sha16("todo", "", "work on problem set 3", "2026-10-07T23:59"),
            "Open": sha16("open", "", "https://docs.google.com/forms/d/e/EXAMPLE/viewform"),
        }
        for label, action_id in expected.items():
            with self.subTest(label=label):
                self.assertEqual(example(label).id, action_id)
        self.assertEqual(example("Reply", msgid="", gmid="18c0ffee00000002").id,
                         sha16("reply", "work", "gmid:18c0ffee00000002"))

    def test_rewording_keeps_the_id(self) -> None:
        same = {
            "Reply": dict(body="Other words", link="", subject="Re: Thursday meeting", due="2026-10-09",
                          cc="cy@example.edu", replied="unknown", gmid="18c0ffee00000002"),
            "Email": dict(body="Other words", link="https://mail.google.com/mail/#all/x", due="2026-10-09",
                          cc="cy@example.edu", to="Office <OFFICE@example.edu>", subject="question  about the LAB schedule"),
            "RSVP": dict(title="Talk", at="", answer="no", notify="none", due="", body="See you", link=""),
            "Move": dict(title="Sync", at="", notify="external", body="", link="https://calendar.google.com/x"),
            "Cancel": dict(title="", at="", notify="none", body="Sorry", cal="primary"),
            "Share": dict(title="Budget", role="editor", link="", who="SAM@example.com"),
            "Slack": dict(who="", said="", body="Sure", link=""),
            "Todo": dict(acct="work", link="", block="2026-10-05 18:00-19:00", title="work on  problem set 3"),
            "Open": dict(title="Another name"),
        }
        for label, changes in same.items():
            base = example(label).id
            for key, value in changes.items():
                with self.subTest(label=label, key=key):
                    self.assertEqual(example(label, **{key: value}).id, base)

    def test_a_new_target_gets_a_new_id(self) -> None:
        other = {
            "Reply": dict(msgid="CAExample0002@mail.example.com", acct="personal"),
            "Email": dict(subject="Another question", to="lab@example.edu", acct="work"),
            "RSVP": dict(event="zzz123def456ghi789", cal="team@group.calendar.google.com", acct="personal"),
            "Move": dict(when="2026-10-08 15:00-16:00", event="abc123def456ghi789"),
            "Cancel": dict(event="abc123def456ghi789"),
            "Share": dict(who="kim@example.com", file="1AbCdEfGhIjKlMnOpQrStUvWxYz0124"),
            "Slack": dict(ts="1700000000.000200", thread="1700000000.000100", channel="C00000000"),
            "Todo": dict(due="2026-10-08 23:59", title="Work on Problem set 4"),
            "Open": dict(link="https://docs.google.com/forms/d/e/OTHER/viewform"),
        }
        for label, changes in other.items():
            base = example(label).id
            for key, value in changes.items():
                with self.subTest(label=label, key=key):
                    self.assertNotEqual(example(label, **{key: value}).id, base)

    def test_errored_lines_use_the_old_formula(self) -> None:
        line = "Reply: acct=work | thread=18c0ffee00000001 | to=ana@example.edu | subject=Re: x | body=hi"
        action = parse_action_line(line)
        self.assertEqual(action.id, sha16("reply", " ".join(line.casefold().split()), "", "", "0", ""))
        self.assertNotEqual(action.id, parse_action_line(line.replace("Re: x", "Re: y")).id)

    def test_all_ids_are_16_hex(self) -> None:
        for label in EXAMPLES:
            with self.subTest(label=label):
                self.assertRegex(example(label).id, r"^[0-9a-f]{16}$")
                self.assertRegex(example(label, **{EXAMPLES[label][0][0]: "!"}).id, r"^[0-9a-f]{16}$")


class DecidableTests(unittest.TestCase):

    def test_matrix(self) -> None:
        rows = [
            (example("Reply"), True, True, "Send"),
            (example("Email"), True, True, "Send"),
            (example("RSVP"), True, True, "Accept"),
            (example("RSVP", answer="no"), True, True, "Decline"),
            (example("RSVP", answer="maybe"), True, True, "Maybe"),
            (example("Move"), True, True, "Move"),
            (example("Cancel"), True, True, "Cancel event"),
            (example("Share"), True, False, ""),
            (example("Slack"), True, False, ""),
            (example("Todo"), True, True, "Add block"),
            (example("Todo", block=""), True, False, ""),
            (example("Open"), True, False, ""),
            (parse_action_line(CHESS), True, True, "Approve"),
            (parse_action_line("Calendar: Sync | 2026-10-09 25:00"), False, False, ""),
            (parse_action_line("Reply: Carol about the draft"), False, False, ""),
            (parse_action_line("Todo: renew parking permit"), False, False, ""),
            (parse_action_line("Nothing needs your OK today."), False, False, ""),
            (example("Reply", replied="yes"), False, False, ""),
            (example("Reply", to="bob@"), False, False, ""),
            (example("Todo", block="2026-10-06"), False, False, ""),
            (ProposedAction(id="x", kind=TODO, raw="", title="t", start=dt(6, 19), end=dt(6, 21)), False, False, ""),
            (ProposedAction(id="x", kind=CALENDAR, raw="", title="t", start=dt(6, 19)), True, True, "Approve"),
        ]
        for action, decidable, actionable, label in rows:
            with self.subTest(raw=action.raw[:40], kind=action.kind):
                self.assertEqual(action.decidable, decidable)
                self.assertEqual(action.actionable, actionable)
                self.assertEqual(approve_label(action), label)
                self.assertEqual(action.approve_label(), label)

    def test_speech_nouns(self) -> None:
        nouns = {CALENDAR: ("a calendar invite", "calendar invites"), REPLY: ("a reply", "replies"),
                 EMAIL: ("an email", "emails"), RSVP: ("an invitation to answer", "invitations to answer"),
                 MOVE: ("a meeting to move", "meetings to move"), CANCEL: ("a meeting to cancel", "meetings to cancel"),
                 SHARE: ("a share request", "share requests"), SLACK: ("a Slack reply", "Slack replies"),
                 TODO: ("a to-do", "to-dos"), OPEN: ("a link to check", "links to check")}
        for kind, noun in nouns.items():
            with self.subTest(kind=kind):
                self.assertEqual(ProposedAction(id="x", kind=kind, raw="").speech_noun, noun)

    def test_still_frozen_and_hashable(self) -> None:
        action = example("Reply")
        self.assertEqual(hash(action), hash(example("Reply")))
        with self.assertRaises(AttributeError):
            action.body = "x"   # type: ignore[misc]


class StructuredWordingTests(unittest.TestCase):

    def test_describe_and_spoken_per_kind(self) -> None:
        cases = {
            "Reply": ("To: ana@example.edu, ben@example.edu", "Reply about Thursday noon meeting, work account"),
            "Email": ("To: office@example.edu", "Email about Question about the lab schedule, personal account"),
            "RSVP": (f"Answer: yes{DOT}Tue Oct 6{DOT}5:00-6:00 PM{DOT}organizer emailed{DOT}due Tue Oct 6",
                     "Accept Speaker series, work account, Tuesday October 6, 5 to 6 PM"),
            "Move": (f"New time: Thu Oct 8{DOT}2:00-3:00 PM{DOT}guests notified{DOT}was 12:00-1:00 PM",
                     "Move Project sync to Thursday October 8, 2 to 3 PM, work account"),
            "Cancel": (f"Fri Oct 9{DOT}6:00-7:00 PM{DOT}guests notified",
                       "Cancel Study group, personal account, Friday October 9, 6 to 7 PM"),
            "Share": ("sam@example.com asks for viewer access", "Share request for Trip budget, personal account"),
            "Slack": ('"are you free friday?"', "Slack reply to Sam"),
            "Todo": (f"Due Wed Oct 7, 11:59 PM{DOT}block Tue Oct 6{DOT}7:00-9:00 PM",
                     "Work on Problem set 3, due Wednesday October 7 at 11:59 PM, with a block Tuesday October 6, "
                     "7 to 9 PM"),
            "Open": ("docs.google.com", "Lab safety form"),
        }
        for label, (describe, spoken) in cases.items():
            with self.subTest(label=label):
                action = example(label)
                self.assertEqual(action.describe(TODAY), describe)
                self.assertEqual(action.spoken(TODAY), spoken)

    def test_due_words(self) -> None:
        self.assertEqual(example("Reply", due="2026-10-04").describe(TODAY),
                         f"To: ana@example.edu, ben@example.edu{DOT}due today")
        self.assertEqual(example("Reply", due="2026-10-05").spoken(TODAY),
                         "Reply about Thursday noon meeting, work account, due tomorrow")
        action = example("Email", cc="cy@example.edu", due="2027-01-04 9:00 AM")
        self.assertEqual(action.describe(TODAY),
                         f"To: office@example.edu{DOT}Cc: cy@example.edu{DOT}due Mon Jan 4, 2027, 9:00 AM")
        self.assertEqual(action.spoken(TODAY), "Email about Question about the lab schedule, personal account, "
                                               "due Monday January 4, 2027 at 9 AM")
        self.assertEqual(example("Todo", due="2026-10-05 12:00", block="").spoken(TODAY),
                         "Work on Problem set 3, due tomorrow at noon")
        self.assertEqual(example("Todo", due="2026-10-04", block="").describe(TODAY), "Due today")

    def test_rsvp_move_cancel_variants(self) -> None:
        self.assertTrue(example("RSVP", answer="no").spoken(TODAY).startswith("Decline Speaker series, work account"))
        self.assertTrue(example("RSVP", answer="maybe").spoken(TODAY).startswith("Answer maybe to Speaker series"))
        self.assertEqual(example("RSVP", title="", at="", due="").spoken(TODAY), "Accept an invitation, work account")
        self.assertEqual(example("RSVP", at="", due="").describe(TODAY), f"Answer: yes{DOT}organizer emailed")
        # Who Google emails about the answer is on the card, as it will be sent (sendUpdates).
        self.assertEqual(example("RSVP", at="", due="", notify="none").describe(TODAY),
                         f"Answer: yes{DOT}organizer not emailed")
        self.assertEqual(example("RSVP", at="", due="", notify="external").describe(TODAY),
                         f"Answer: yes{DOT}organizer emailed only if external")
        self.assertEqual(example("Move", title="").spoken(TODAY),
                         "Move a meeting to Thursday October 8, 2 to 3 PM, work account")
        self.assertEqual(example("Move", at="2026-10-07 12:00-13:00", notify="external").describe(TODAY),
                         f"New time: Thu Oct 8{DOT}2:00-3:00 PM{DOT}only outside guests notified{DOT}"
                         "was Wed Oct 7, 12:00-1:00 PM")
        self.assertEqual(example("Move", at="", notify="none").describe(TODAY),
                         f"New time: Thu Oct 8{DOT}2:00-3:00 PM{DOT}guests not notified")
        self.assertEqual(example("Cancel", at="").describe(TODAY), "guests notified")
        self.assertEqual(example("Cancel", at="", title="").spoken(TODAY), "Cancel a meeting, personal account")

    def test_share_slack_and_accounts(self) -> None:
        self.assertEqual(example("Share", title="", role="editor").spoken(TODAY),
                         "Share request for a file, personal account")
        self.assertEqual(example("Share", role="editor").describe(TODAY), "sam@example.com asks for editor access")
        self.assertEqual(example("Slack", who="").spoken(TODAY), "Slack reply")
        self.assertEqual(example("Slack", said="").describe(TODAY), "From Sam")
        self.assertEqual(example("Slack", said="w" * 200).describe(TODAY), '"' + "w" * 137 + '..."')
        self.assertEqual(example("Slack", said=r"two\nlines").describe(TODAY), '"two lines"')
        self.assertEqual(example("Email", acct="school_mail").spoken(TODAY),
                         "Email about Question about the lab schedule, school mail account")
        self.assertEqual(example("Reply", subject="Fwd: RE: Budget").spoken(TODAY), "Reply about Budget, work account")

    def test_spoken_is_plain_ascii_without_a_final_period(self) -> None:
        for label in EXAMPLES:
            with self.subTest(label=label):
                spoken = example(label).spoken(TODAY)
                self.assertTrue(spoken.isascii())
                self.assertFalse(spoken.endswith("."))
                self.assertNotIn("\u00b7", example(label).spoken(TODAY))

    def test_default_today_is_the_real_today(self) -> None:
        action = example("Todo")
        self.assertEqual(action.describe(), action.describe(date.today()))
        self.assertEqual(action.spoken(), action.spoken(date.today()))


class BlockEventTests(unittest.TestCase):

    def test_a_todo_block_is_a_calendar_proposal_with_the_todo_id(self) -> None:
        todo = example("Todo")
        block = todo.block_event()
        self.assertIsNotNone(block)
        self.assertEqual((block.id, block.kind, block.raw, block.title), (todo.id, CALENDAR, todo.raw, todo.title))
        self.assertEqual((block.start, block.end, block.all_day, block.repeat), (dt(6, 19), dt(6, 21), False, ""))
        self.assertEqual(block.notes,
                         "Due Wed Oct 7, 11:59 PM - https://example.instructure.com/courses/1/assignments/2")
        self.assertTrue(block.actionable)
        self.assertEqual(block.describe(TODAY), f"Tue Oct 6{DOT}7:00-9:00 PM")
        body = gcal.build_event_body(block, "America/Los_Angeles")
        self.assertEqual(body["summary"], "Work on Problem set 3")
        self.assertTrue(body["start"]["dateTime"].startswith("2026-10-06T19:00"))
        self.assertTrue(body["end"]["dateTime"].startswith("2026-10-06T21:00"))
        self.assertTrue(body["description"].startswith("Due Wed Oct 7, 11:59 PM - https://example.instructure.com"))

    def test_notes_variants(self) -> None:
        self.assertEqual(example("Todo", due="2026-10-07", link="").block_event().notes, "Due Wed Oct 7")
        self.assertEqual(example("Todo", due="2027-01-05", block="2026-12-30 10:00-11:00", link="")
                         .block_event().notes, "Due Tue Jan 5, 2027")

    def test_only_a_valid_todo_with_a_block_has_one(self) -> None:
        for action in (example("Todo", block=""), example("Reply"), example("Move"), parse_action_line(CHESS),
                       example("Todo", block="2026-10-06"), parse_action_line("Todo: renew parking permit")):
            with self.subTest(kind=action.kind, raw=action.raw[:30]):
                self.assertIsNone(action.block_event())


class CardViewTests(unittest.TestCase):

    def test_per_kind(self) -> None:
        reply_body = "Hi both,\nShall we keep it at noon with the two of us, or move it to 2 PM?\nThanks"
        expected = {
            "Reply": CardView("reply \u00b7 work", "Re: Thursday noon meeting", "To: ana@example.edu, ben@example.edu",
                              body=reply_body, open_text="Open thread", copy_text="Copy reply", approve_text="Send",
                              decidable=True, editable=True, body_lines=10),
            "Email": CardView("email \u00b7 personal", "Question about the lab schedule", "To: office@example.edu",
                              body="Hello,\nIs the lab open on Saturday?\nThanks", copy_text="Copy email",
                              approve_text="Send", decidable=True, editable=True, body_lines=10),
            "RSVP": CardView("rsvp \u00b7 work", "Speaker series", example("RSVP").describe(TODAY),
                             open_text="Open event", approve_text="Accept", decidable=True, editable=True,
                             check=True, deny_text="Skip"),
            "Move": CardView("move \u00b7 work", "Project sync", example("Move").describe(TODAY),
                             body="Moving to 2 PM so everyone can join.", copy_text="Copy note", approve_text="Move",
                             decidable=True, note=actions.NOTE_NOT_SENT, editable=True, check=True,
                             deny_text="Skip"),
            "Cancel": CardView("cancel \u00b7 personal", "Study group", example("Cancel").describe(TODAY),
                               approve_text="Cancel event", decidable=True, editable=True, check=True,
                               deny_text="Skip"),
            "Share":CardView("share \u00b7 personal", "Trip budget", "sam@example.com asks for viewer access",
                              open_text="Open request", approve_text="Done", decidable=True),
            "Slack": CardView("slack", "Slack message from Sam", '"are you free friday?"',
                              body="Yes! Friday after 4 works.", open_text="Open in Slack", copy_text="Copy reply",
                              approve_text="Done", decidable=True),
            "Todo": CardView("todo", "Work on Problem set 3", example("Todo").describe(TODAY),
                             open_text="Open in Canvas", approve_text="Add block", decidable=True),
            "Open": CardView("open", "Lab safety form", "docs.google.com", open_text="Open", approve_text="Done",
                             decidable=True),
        }
        for label, view in expected.items():
            with self.subTest(label=label):
                self.assertEqual(card_view(example(label), TODAY), view)

    def test_variants(self) -> None:
        self.assertEqual(card_view(example("Todo", link="https://docs.google.com/x"), TODAY).open_text, "Open")
        self.assertEqual(card_view(example("Todo", block=""), TODAY).approve_text, "Done")
        # An RSVP's note goes to the organizer with the answer: no Copy. A Move's or Cancel's note is
        # not sent (Google Calendar has no message): Copy, and the card says so.
        rsvp = card_view(example("RSVP", body="Running late"), TODAY)
        self.assertEqual((rsvp.copy_text, rsvp.body, rsvp.note), ("", "Note to the organizer: Running late", ""))
        cancel = card_view(example("Cancel", body="Sorry, something came up"), TODAY)
        self.assertEqual((cancel.copy_text, cancel.note), ("Copy note", actions.NOTE_NOT_SENT))
        self.assertEqual(card_view(example("Move", body=""), TODAY).note, "")
        self.assertEqual(card_view(example("Email", link="https://mail.google.com/mail/#all/1"), TODAY).open_text,
                         "Open")
        replied = card_view(example("Reply", replied="yes"), TODAY)
        self.assertEqual(replied, CardView("reply \u00b7 work", "Re: Thursday noon meeting",
                                           "The briefing says you already replied", open_text="Open thread"))

    def test_calendar_and_legacy_cards(self) -> None:
        self.assertEqual(card_view(parse_action_line(CHESS), TODAY),
                         CardView("calendar", "Chess Club Weekly Meeting",
                                  f"Fri Oct 9{DOT}3:00-4:00 PM{DOT}weekly until Dec 11", approve_text="Approve",
                                  decidable=True))
        self.assertEqual(card_view(parse_action_line(FILM), TODAY).detail,
                         f"Tue Oct 6{DOT}5:00-6:00 PM{DOT}Central Library, Media Lab")
        self.assertEqual(card_view(parse_action_line("Reply: Carol about the draft"), TODAY),
                         CardView("reply", "Carol about the draft", "Information only"))
        self.assertEqual(card_view(parse_action_line("Nothing needs your OK today."), TODAY),
                         CardView("note", "Nothing needs your OK today.", "Information only"))
        broken = "Calendar: Dentist checkup | sometime next week"
        self.assertEqual(card_view(parse_action_line(broken), TODAY),
                         CardView("calendar", broken, "Can't read this line: no date (expected YYYY-MM-DD, "
                                                     "optionally with HH:MM-HH:MM)"))

    def test_errored_key_value_line(self) -> None:
        line = kv_line("Reply", REPLY_PAIRS, to="bob@")
        view = card_view(parse_action_line(line), TODAY)
        self.assertEqual(view, CardView("reply \u00b7 work", line[:157].rstrip() + "...",
                                        'Can\'t read this line: to=: "bob@" is not an email address'))
        self.assertLessEqual(len(view.title), 160)
        short = "Open: title=Form | link=http://x.example/a"
        self.assertEqual(card_view(parse_action_line(short), TODAY).title, short)

    def test_note_shows_at_most_two_warnings(self) -> None:
        one = card_view(example("Reply", replied=None), TODAY)
        self.assertEqual(one.note, REPLIED_WARNING)
        two = card_view(example("Reply", replied=None, link="http://x.example/a", due="2026-10-04"), TODAY)
        self.assertEqual(two.note, f"Link hidden: not an https link{DOT}{REPLIED_WARNING}")
        # No kind produces three warnings today, so the cap is checked on a hand-made proposal.
        three = dataclasses.replace(example("Reply"), warnings=("first", "second", "third"))
        self.assertTrue(three.decidable)
        self.assertEqual(card_view(three, TODAY).note, f"first{DOT}second")
        both = example("Todo", due="2026-10-06 20:00", link="https://lms.example.edu/a", block="2026-10-06 19:00-21:00")
        self.assertEqual(len(both.warnings), 2)
        rsvp = example("RSVP", at="soon", link="http://x.example/a")
        self.assertEqual(rsvp.warnings, ("at= could not be read; not shown", "Link hidden: not an https link"))
        self.assertEqual(card_view(rsvp, TODAY).note,
                         f"at= could not be read; not shown{DOT}Link hidden: not an https link")

    def test_result_text(self) -> None:
        self.assertEqual(result_text(example("Todo"), "created"), "Block added")
        self.assertEqual(result_text(example("Todo"), "denied"), "")
        self.assertEqual(result_text(example("Todo", block=""), "denied"), "Dismissed")
        self.assertEqual(result_text(example("Reply"), "denied"), "")   # DENIED: Jarvis would have sent it
        self.assertEqual(result_text(example("Reply", replied="yes"), "denied"), "")
        self.assertEqual(result_text(example("Share"), "denied"), "Dismissed")
        self.assertEqual(result_text(example("Reply"), "done"), "")
        self.assertEqual(result_text(parse_action_line(CHESS), "denied"), "")
        self.assertEqual(result_text(parse_action_line(CHESS), "created"), "")
        self.assertEqual(result_text(example("Todo"), "exists"), "")


class CarriedOutKindsTests(unittest.TestCase):
    """Calendar, Todo blocks, RSVP, Move, Cancel, Reply and Email: carried out by Jarvis after the click,
    with no countdown: the calendar kinds at once (Undo reverses them), the others after a hidden hold."""

    def test_carried_kinds(self) -> None:
        self.assertEqual(actions.CARRIED_KINDS, frozenset({CALENDAR, TODO, RSVP, MOVE, CANCEL, REPLY, EMAIL}))
        self.assertEqual(actions.UNDO_KINDS, frozenset({CALENDAR, TODO, RSVP, MOVE}))
        self.assertEqual(actions.HOLD_KINDS, frozenset({CANCEL, REPLY, EMAIL}))
        self.assertEqual(actions.UNDO_KINDS | actions.HOLD_KINDS, actions.CARRIED_KINDS)
        self.assertFalse(actions.UNDO_KINDS & actions.HOLD_KINDS)
        for action in (example("RSVP"), example("Move"), example("Todo"), parse_action_line(CHESS)):
            self.assertEqual((action.undoable, action.held), (True, False), action.kind)
        for action in (example("Cancel"), example("Reply"), example("Email")):
            self.assertEqual((action.undoable, action.held), (False, True), action.kind)
        for action in (example("Share"), example("Todo", block="")):
            self.assertEqual((action.undoable, action.held), (False, False), action.kind)
        self.assertEqual(actions.EVENT_KINDS, frozenset({RSVP, MOVE, CANCEL}))
        self.assertEqual(actions.MAIL_KINDS, frozenset({REPLY, EMAIL}))
        rows = (  # carried, changes_event, sends_mail, editable
            (example("RSVP"), True, True, False, True), (example("Move"), True, True, False, True),
            (example("Cancel"), True, True, False, True), (example("Reply"), True, False, True, True),
            (example("Email"), True, False, True, True), (example("Todo"), True, False, False, False),
            (parse_action_line(CHESS), True, False, False, False),
            (example("Todo", block=""), False, False, False, False),
            (example("Reply", replied="yes"), False, False, False, False),
            (example("Share"), False, False, False, False), (example("Slack"), False, False, False, False),
            (parse_action_line("Move: the dentist to Friday"), False, False, False, False),
            (parse_action_line("Reply: Carol about the draft"), False, False, False, False),
            (example("Move", when="2026-10-08"), False, False, False, False),
            (example("Reply", to="bob@"), False, False, False, False))
        for action, carried, event, mail, editable in rows:
            with self.subTest(kind=action.kind, raw=action.raw[:30]):
                self.assertEqual((action.carried, action.changes_event, action.sends_mail, action.editable),
                                 (carried, event, mail, editable))
                self.assertEqual(action.carried, action.actionable)   # every kind Jarvis carries out

    def test_sent_and_unknown_texts(self) -> None:
        self.assertEqual([result_text(example("RSVP", answer=a), "sent") for a in ("yes", "no", "maybe")],
                         ["Accepted", "Declined", "Answered maybe"])
        self.assertEqual(result_text(example("Move"), "sent"), "Moved")
        self.assertEqual(result_text(example("Cancel"), "sent"), "Cancelled")
        self.assertEqual([actions.sent_text(example("RSVP", answer=a), already=True) for a in ("yes", "no", "maybe")],
                         ["Already accepted", "Already declined", "Already answered maybe"])
        self.assertEqual(actions.sent_text(example("Move"), already=True), "Already at that time")
        self.assertEqual(actions.sent_text(example("Cancel"), already=True), "Already cancelled")
        self.assertEqual(actions.sent_text(example("Reply")), "Sent")
        self.assertEqual(result_text(example("Email"), "sent"), "Sent")
        self.assertEqual(actions.sent_text(example("Share")), "")
        for label in ("Reply", "Email"):
            with self.subTest(label=label):
                self.assertEqual(result_text(example(label), "unknown"), actions.UNKNOWN_MAIL)
        self.assertEqual(actions.UNKNOWN_MAIL, "Unknown: check Sent mail before retrying")
        for label in ("RSVP", "Move", "Cancel"):
            with self.subTest(label=label):
                self.assertEqual(result_text(example(label), "unknown"), actions.UNKNOWN_CALENDAR)
                # Skip: the card reads SKIPPED (nothing was sent), not DENIED next to a "Decline".
                self.assertEqual(result_text(example(label), "denied"), "Skipped")
        self.assertEqual(result_text(parse_action_line(CHESS), "denied"), "")   # DENIED, as before
        self.assertEqual(actions.UNKNOWN_CALENDAR, "Unknown: check the calendar before retrying")
        # Calendar events and to-do blocks have the running / unknown model too now.
        self.assertEqual(result_text(parse_action_line(CHESS), "unknown"), actions.UNKNOWN_CALENDAR)
        self.assertEqual(result_text(example("Todo"), "unknown"), actions.UNKNOWN_CALENDAR)
        self.assertEqual(result_text(example("Todo", block=""), "unknown"), "")

    def test_when_text_shows_the_wall_time_an_event_carries(self) -> None:
        item = dataclasses.make_dataclass("Item", ["start", "end", "all_day_start", "all_day_end"])
        self.assertEqual(actions.when_text(item(datetime(2026, 10, 8, 12, 0, tzinfo=PDT),
                                                datetime(2026, 10, 8, 13, 0, tzinfo=PDT), None, None), TODAY),
                         f"Thu Oct 8{DOT}12:00-1:00 PM")
        self.assertEqual(actions.when_text(item(None, None, date(2026, 10, 9), date(2026, 10, 10)), TODAY),
                         f"Fri Oct 9 - Sat Oct 10{DOT}all day")

    def test_edit_is_checked_and_keeps_the_id(self) -> None:
        move = example("Move")
        edited = actions.edit_action(move, start=dt(9, 9), end=dt(9, 10), notify="none", body="New note")
        self.assertEqual((edited.id, edited.start, edited.end, edited.body, edited.field("notify")),
                         (move.id, dt(9, 9), dt(9, 10), "New note", "none"))
        self.assertTrue(edited.carried)
        with self.assertRaises(actions.EditInvalid):
            actions.edit_action(move, notify="loud")
        with self.assertRaises(actions.EditInvalid):
            actions.edit_action(example("Open"), body="x")

    def test_store_set_says_whether_it_was_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ActionStore(Path(tmp) / "actions.json")
            self.assertTrue(store.set("a" * 16, "running", kind=RSVP, account="work"))
            blocked = ActionStore(Path(tmp) / "actions.json" / "not-a-folder" / "actions.json")
            with self.assertLogs(ACTIONS_LOGGER, level="WARNING"):
                self.assertFalse(blocked.set("b" * 16, "sent", kind=RSVP, account="work"))
            self.assertEqual(blocked.get("b" * 16)["status"], "sent")   # kept for this run


class EmailAddressTests(unittest.TestCase):
    """email_address: one address as Jarvis sends to it; display names never count."""

    def test_valid_and_normalized(self) -> None:
        cases = {"ana@example.edu": "ana@example.edu", " Ana@Example.EDU ": "Ana@example.edu",
                 "Ana Example <ana@example.edu>": "ana@example.edu", "<ana@example.edu>": "ana@example.edu",
                 '"Ben, from the lab" <ben@lab.example.com>': "ben@lab.example.com",
                 "o'neil+lists@example.co.uk": "o'neil+lists@example.co.uk"}
        for text, address in cases.items():
            with self.subTest(text=text):
                self.assertEqual(actions.email_address(text), address)

    def test_refused(self) -> None:
        for text in ("", "ana", "ana@", "@example.edu", "ana@example", "ana@example.e", "a b@example.edu",
                     "ana@exa mple.edu", "ana@example.edu, ben@example.edu", "ana@example.edu;x@example.edu",
                     "ana..x@example.edu", ".ana@example.edu", "ana.@example.edu", '"ana"@example.edu',
                     "ana@example.edu\nBcc: x@example.com", "ana@exa\rmple.edu", "ana\u2028@example.edu",
                     "ana@example.edu\u2028Bcc: x@example.com", "ana@example.edu\x00",
                     "ana|x@example.edu", "an\u00e1@example.edu", "ana@b\u00fccher.example",
                     "Bob <ana@example.edu> <eve@example.com>", "x" * 65 + "@example.edu",
                     "a@" + "b" * 250 + ".edu", None, 5):
            with self.subTest(text=text):
                self.assertEqual(actions.email_address(text), "")   # type: ignore[arg-type]


class MailCardTests(unittest.TestCase):
    """Reply / Email cards Jarvis sends: Send, Edit, a longer preview, link and threading notes."""

    def test_notes(self) -> None:
        linked = example("Reply", body=r"See https://docs.google.com/x and www.example.com/a.\nThanks")
        self.assertEqual(card_view(linked, TODAY).note, "Contains 2 links")
        one = example("Email", body="Form: https://forms.example.edu/a, thanks")
        self.assertEqual(card_view(one, TODAY).note, "Contains 1 link")
        gmid = example("Reply", msgid="", gmid="18c0ffee00000002")
        self.assertEqual(card_view(gmid, TODAY).note, actions.NO_MSGID_NOTE)
        unknown = example("Reply", replied="unknown", msgid="", gmid="18c0ffee00000002")
        self.assertEqual(card_view(unknown, TODAY).note, f"{REPLIED_WARNING}{DOT}{actions.NO_MSGID_NOTE}")

    def test_encoded_word_subject_is_not_sent(self) -> None:
        tricky = example("Email", subject="=?utf-8?b?SGVsbG8=?=")
        self.assertTrue(tricky.sends_mail)
        self.assertEqual(actions.mail_problem(tricky), actions.ENCODED_WORD_PROBLEM)
        self.assertIn(actions.ENCODED_WORD_PROBLEM, card_view(tricky, TODAY).note)
        self.assertEqual(actions.mail_problem(example("Email", subject="Is 2 =? 3")), "")   # no closing ?=
        self.assertEqual(actions.mail_problem(example("Email")), "")
        self.assertEqual(actions.mail_problem(example("RSVP")), "")
        empty = dataclasses.replace(example("Email"), body="  ")
        self.assertEqual(actions.mail_problem(empty), actions.EMPTY_MAIL_PROBLEM)

    def test_body_links_and_review(self) -> None:
        text = "a https://x.example.edu/p?q=1). b http://y.example.com c www.z.example.org"
        spans = actions.body_links(text)
        self.assertEqual([text[s:e] for s, e in spans],
                         ["https://x.example.edu/p?q=1", "http://y.example.com", "www.z.example.org"])
        self.assertEqual(actions.body_links("(see https://x.example.edu/a_(b))"),
                         ((5, 32),))
        self.assertEqual(actions.links_note(""), "")
        self.assertFalse(actions.body_needs_review(example("Reply")))
        long_body = r"\n".join(f"line {n}" for n in range(11))
        self.assertTrue(actions.body_needs_review(example("Reply", body=long_body)))
        self.assertFalse(actions.body_needs_review(example("Slack", body=long_body)))
        self.assertEqual(card_view(example("Reply", body=long_body), TODAY).body_lines, actions.MAIL_PREVIEW_LINES)
        self.assertEqual(card_view(example("Slack"), TODAY).body_lines, 3)

    def test_due_words_are_the_mail_cards_detail(self) -> None:
        self.assertEqual(actions.due_words(example("Reply", due="2026-10-04"), TODAY), "due today")
        self.assertEqual(actions.due_words(example("Reply", due=str(TODAY + timedelta(days=1))), TODAY),
                         "due tomorrow")
        self.assertEqual(actions.due_words(example("Email", due="2026-10-09 15:30"), TODAY), "due Fri Oct 9, 3:30 PM")
        self.assertEqual(actions.due_words(example("Email", due=""), TODAY), "")

    def test_mail_recipients_and_confirmed_default(self) -> None:
        action = example("Email", to="b@example.edu, a@example.edu", cc="c@example.edu")
        self.assertEqual(action.mail_recipients(), ("b@example.edu", "a@example.edu", "c@example.edu"))
        self.assertEqual(action.confirmed, frozenset())
        # A line can never confirm anyone: an unknown key is ignored.
        self.assertEqual(parse_action_line(kv_line("Email", EMAIL_PAIRS) .replace(" | body=",
                         " | confirmed=office@example.edu | body=")).confirmed, frozenset())


class EditMailTests(unittest.TestCase):
    """edit_mail: what the Edit dialog changes on a Reply or Email is checked like a line."""

    def test_recipients_subject_body_and_the_id(self) -> None:
        email = example("Email")
        edited = actions.edit_mail(email, to=["Office <OFFICE@Example.EDU>", "dean@example.edu"],
                                   cc=("office@example.edu", "cy@example.edu", "CY@example.edu"),
                                   subject="  Lab   schedule\tquestion ", body="Hi,\r\nIs it open?\rThanks\n\n")
        self.assertEqual(edited.id, email.id)
        self.assertEqual(edited.recipients(), ("OFFICE@example.edu", "dean@example.edu"))
        self.assertEqual(edited.cc(), ("cy@example.edu",))   # office@ stays in To only
        self.assertEqual((edited.title, edited.field("subject")), ("Lab schedule question",) * 2)
        self.assertEqual(edited.body, "Hi,\nIs it open?\nThanks")
        self.assertEqual([name for name, _ in edited.fields], ["acct", "to", "cc", "subject"])
        self.assertTrue(edited.describe(TODAY).startswith("To: OFFICE@example.edu, dean@example.edu" + DOT +
                                                          "Cc: cy@example.edu"))
        dropped = actions.edit_mail(edited, cc=[])
        self.assertEqual((dropped.cc(), [n for n, _ in dropped.fields]), ((), ["acct", "to", "subject"]))
        self.assertEqual(actions.edit_mail(email), dataclasses.replace(email, confirmed=frozenset()))
        self.assertEqual(actions.edit_mail(email, to="a@example.edu; b@example.edu").recipients(),
                         ("a@example.edu", "b@example.edu"))

    def test_reply_keeps_its_subject_and_thread(self) -> None:
        reply = example("Reply")
        same = actions.edit_mail(reply, subject="re: thursday  NOON meeting", body="Works for me")
        self.assertEqual((same.title, same.body, same.field("thread"), same.field("msgid")),
                         ("Re: Thursday noon meeting", "Works for me", "18c0ffee00000001",
                          "<CAExample0001@mail.example.com>"))
        with self.assertRaises(actions.EditInvalid) as ctx:
            actions.edit_mail(reply, subject="Something else")
        self.assertIn("keeps the subject", str(ctx.exception))

    def test_confirmations_follow_the_recipients(self) -> None:
        email = example("Email", cc="cy@example.edu")
        ticked = actions.edit_mail(email, confirmed=["Office <Office@Example.edu>", "nobody@example.com", "bad@"])
        self.assertEqual(ticked.confirmed, frozenset({"office@example.edu"}))
        self.assertEqual(ticked.id, email.id)
        kept = actions.edit_mail(ticked, body="New text")
        self.assertEqual(kept.confirmed, frozenset({"office@example.edu"}))
        removed = actions.edit_mail(ticked, to=["cy@example.edu"], cc=[])
        self.assertEqual(removed.confirmed, frozenset())   # office@ is no longer a recipient
        again = actions.edit_mail(removed, to=["office@example.edu"])
        self.assertEqual(again.confirmed, frozenset())    # a removed tick does not come back by itself
        self.assertNotEqual(hash(ticked), hash(email))     # still frozen and hashable
        with self.assertRaises(actions.EditInvalid):
            actions.edit_mail(email, confirmed="office@example.edu")   # type: ignore[arg-type]

    def test_bad_edits(self) -> None:
        email = example("Email")
        six = [f"p{n}@example.edu" for n in range(6)]
        cases = (
            (dict(to=[]), "To needs at least one address"),
            (dict(to=["bob@"]), 'To: "bob@" is not an email address'),
            (dict(cc=["ana@example.edu, eve@example.com"]), "Cc: "),
            (dict(to=["ana@example.edu\r\nBcc: eve@example.com"]), "is not an email address"),
            (dict(to=[5]), "an address must be text"),
            (dict(to=six), "To and Cc name 6 addresses (at most 5)"),
            (dict(to=six[:3], cc=six[3:]), "name 6 addresses"),
            (dict(subject="Hi\r\nBcc: eve@example.com"), "the subject has a line break"),
            (dict(subject="Hi\nthere"), "line break"),
            (dict(subject="Hi\u2028there"), "line break"),
            (dict(subject="Hi\u0085there"), "line break"),
            (dict(subject="Hi\x0bthere"), "line break"),
            (dict(subject="Hi\x00there"), "control character"),
            (dict(subject="Hi\x1bthere"), "control character"),
            (dict(subject="=?utf-8?b?SGksDQpCY2M6IGV2ZUBleGFtcGxlLmNvbQ==?="), "encoded word"),
            (dict(subject="Re: price =?ISO-8859-1?Q?a?= now"), "encoded word"),
            (dict(subject="x" * 251), "the subject is too long (251 characters, at most 250)"),
            (dict(subject="  "), "give a subject"),
            (dict(subject=3), "the subject must be text"),
            (dict(body=" \n\t "), "the message is empty"),
            (dict(body="y" * 5001), "the message is too long (5001 characters, at most 5000)"),
            (dict(body=b"bytes"), "the message must be text"),
        )
        for kwargs, words in cases:
            with self.subTest(kwargs=str(kwargs)[:60]), self.assertRaises(actions.EditInvalid) as ctx:
                actions.edit_mail(email, **kwargs)   # type: ignore[arg-type]
            self.assertIn(words, str(ctx.exception))
        self.assertEqual(len(actions.edit_mail(email, subject="x" * 250).title), 250)
        self.assertEqual(len(actions.edit_mail(email, body="y" * 5000).body), 5000)
        self.assertEqual(len(actions.edit_mail(email, to=six[:5]).mail_recipients()), 5)
        for action in (example("RSVP"), example("Slack"), parse_action_line(CHESS), example("Reply", replied="yes")):
            with self.subTest(kind=action.kind), self.assertRaises(actions.EditInvalid):
                actions.edit_mail(action, body="x")
        with self.assertRaises(actions.EditInvalid):
            actions.edit_action(example("Reply"), body="x")   # the event edit is for RSVP / Move / Cancel

    def test_invisible_and_control_characters_are_removed(self) -> None:
        edited = actions.edit_mail(example("Email"), subject="Lab\u202e schedule\u200b",
                                   body="a\u202eb\x00c\u2028d\te\x07")
        self.assertEqual(edited.title, "Lab schedule")
        self.assertEqual(edited.body, "abcd\te")


# --------------------------------------------------------------------------
# Module hygiene
# --------------------------------------------------------------------------

class ImportTests(unittest.TestCase):

    def run_python(self, code: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, capture_output=True,
                              text=True, timeout=60)

    def test_qt_free_and_no_import_cycle_in_either_order(self) -> None:
        for first, second in (("actions", "text_prep"), ("text_prep", "actions")):
            with self.subTest(first=first):
                code = (f"import sys, briefing_reader.{first}, briefing_reader.{second}; "
                        "from briefing_reader.actions import parse_action_line; "
                        "assert parse_action_line('Calendar: A | 2026-10-09 3pm').actionable; "
                        "assert not any(m.startswith(('PySide6', 'googleapiclient')) for m in sys.modules)")
                result = self.run_python(code)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])

    def test_source_is_ascii(self) -> None:
        source = Path(actions.__file__).read_bytes()
        self.assertTrue(source.isascii())



class SourceTests(unittest.TestCase):
    """ProposedAction.source (briefing / ask), restrict() and with_error()."""

    MOVE_LINE = ("Move: acct=work | event=abc123def456 | cal=primary | when=2026-10-09 14:00-15:00 | "
                 "notify=all | title=Project sync | at=2026-10-08 14:00-15:00 | link= | body=")

    def test_source_is_not_part_of_the_id(self) -> None:
        line = parse_action_line(self.MOVE_LINE)
        self.assertEqual(line.source, actions.SOURCE_BRIEFING)
        self.assertEqual(line.unverified, frozenset())
        ask = actions.restrict(line, {MOVE}, actions.SOURCE_ASK)
        self.assertEqual(ask.source, "ask")
        self.assertEqual(ask.id, line.id)
        self.assertTrue(ask.actionable)
        self.assertEqual(dataclasses.replace(ask, source="briefing"), line)

    def test_a_kind_the_source_may_not_propose_is_information_only(self) -> None:
        slack = parse_action_line("Slack: channel=C0123ABCD | ts=1696000000.000100 | who=Ana | body=Sure")
        refused = actions.restrict(slack, {MOVE, EMAIL}, actions.SOURCE_ASK)
        self.assertEqual(refused.error, "Ask can't propose Slack")
        self.assertFalse(refused.decidable)
        self.assertNotEqual(refused.id, slack.id)
        share = actions.restrict(parse_action_line("Share: acct=work | file=1AbCdEfGhIjKlMnOp | who=a@example.edu"),
                                 {MOVE}, actions.SOURCE_ASK)
        self.assertEqual(share.error, "Ask can't propose Share")
        prose = actions.restrict(parse_action_line("Nothing to do today."), {MOVE}, actions.SOURCE_ASK)
        self.assertIn("need a kind", prose.error)

    def test_with_error_rebuilds_the_id_per_source(self) -> None:
        line = parse_action_line(self.MOVE_LINE)
        briefing_error = actions.with_error(line, "nope")
        ask_error = actions.with_error(dataclasses.replace(line, source="ask"), "nope")
        # A briefing card's id is the one an unreadable line with the same text gets.
        self.assertEqual(briefing_error.id, actions._action_id(dataclasses.replace(line, error="x")))
        self.assertNotEqual(briefing_error.id, line.id)
        self.assertNotEqual(ask_error.id, briefing_error.id)
        self.assertEqual(ask_error.id, actions.with_error(dataclasses.replace(line, source="ask"), "other").id)
        self.assertFalse(ask_error.decidable)
        # An unreadable Ask line keeps its reason, under an id of its own.
        bad = parse_action_line("Move: acct=work | event=! | when=2026-10-09 14:00-15:00")
        ask_bad = actions.restrict(bad, {MOVE}, actions.SOURCE_ASK)
        self.assertEqual(ask_bad.error, bad.error)
        self.assertNotEqual(ask_bad.id, bad.id)

    def test_card_of_a_refused_ask_line_says_why_plainly(self) -> None:
        line = parse_action_line(self.MOVE_LINE)
        ask = actions.with_error(dataclasses.replace(line, source="ask"), "Jarvis doesn't know this event - ask again")
        view = card_view(ask, date(2026, 10, 4))
        self.assertEqual(view.title, "Project sync")
        self.assertEqual(view.detail, "Jarvis doesn't know this event - ask again")
        self.assertFalse(view.decidable)
        # A briefing line keeps the old wording.
        briefing = card_view(actions.with_error(line, "bad"), date(2026, 10, 4))
        self.assertEqual(briefing.detail, "Can't read this line: bad")

    def test_unverified_recipients_survive_an_edit(self) -> None:
        line = parse_action_line("Email: acct=work | to=Ana <ana@example.edu> | subject=Hi | body=Hello")
        ask = dataclasses.replace(line, source="ask", unverified=frozenset({"ana@example.edu"}))
        edited = actions.edit_mail(ask, body="Hello again", confirmed=["ana@example.edu"])
        self.assertEqual(edited.unverified, frozenset({"ana@example.edu"}))
        self.assertEqual(edited.source, "ask")
        self.assertEqual(edited.id, line.id)


class WebCardTests(unittest.TestCase):
    """Web research cards: the web link rule (any public https host) and how such a card reads."""

    SOURCE = "Open: title=[1] Visit - Example Museum | link=https://www.example.org/visit"

    def web_open(self, line: str = SOURCE, host: str = "www.example.org") -> ProposedAction:
        return dataclasses.replace(parse_action_line(line, link_hosts=(host,)), source=actions.SOURCE_ASK, web=True)

    def test_web_link_rule(self) -> None:
        good = ("https://www.example.org/visit", "https://example.org", "https://EXAMPLE.org./a?b=c#d",
                "https://museum.example.net:443/hours", "https://xn--bcher-kva.example/page",
                "https://a.b.example.com/" + "x" * 2000)
        for url in good:
            with self.subTest(url=url):
                self.assertEqual(actions.web_link_problem(url), "")
                self.assertTrue(actions.web_link_allowed(url))
        bad = {
            "not an https link": ("http://example.org", "javascript:alert(1)", "data:text/html,x", "ftp://example.org",
                                  "file:///C:/x", "https://user:pw@example.org/", "https://example.org:8443/",
                                  "https://example.org/a b", "https://example.org/\u202ex", "https://example.org/a\\b",
                                  "https://example.org/a/../b", "https://example.org/%2e%2e/b", "https://example.org/a|b",
                                  'https://example.org/"x', "https://example.org/<x>", "https://example.org/`x`",
                                  "https://example.org/" + "x" * 2048, "", "example.org"),
            actions.LOCAL_ADDRESS: ("https://localhost/x", "https://LOCALHOST./x", "https://127.0.0.1/",
                                    "https://10.0.0.1/", "https://192.168.1.1/admin", "https://169.254.1.1/",
                                    "https://[::1]/", "https://[fe80::1]/", "https://router.local/",
                                    "https://printer.lan/", "https://a.internal/", "https://intranet/",
                                    "https://box.home.arpa/", "https://x.test/", "https://x.invalid/",
                                    "https://abc.onion/", "https://web.localhost/", "https://127.1/"),
            actions.NOT_WEB_ADDRESS: ("https://8.8.8.8/", "https://0x7f.0.0.1/", "https://example.123/",
                                      "https://ex_ample.org/", "https://b\u00fccher.example/"),
        }
        for reason, urls in bad.items():
            for url in urls:
                with self.subTest(url=url):
                    self.assertEqual(actions.web_link_problem(url), reason)
                    self.assertFalse(actions.web_link_allowed(url))
        for value in (None, 3, b"https://example.org"):
            self.assertFalse(actions.web_link_allowed(value))

    def test_public_host_problem(self) -> None:
        for host in ("example.org", "www.example.org", "EXAMPLE.ORG", "example.org.", "xn--bcher-kva.example",
                     "a-b.example.co.uk", "site.xn--p1ai"):
            with self.subTest(host=host):
                self.assertEqual(actions.public_host_problem(host), "")
        for host in ("localhost", "127.0.0.1", "10.1.2.3", "192.168.1.1", "::1", "[::1]", "router.local",
                     "intranet", "printer.lan", "a.internal", "nas.home", "x.corp"):
            with self.subTest(host=host):
                self.assertEqual(actions.public_host_problem(host), actions.LOCAL_ADDRESS)
        for host in ("", "8.8.8.8", "-bad.example", "example.c", "exa mple.org", None):
            with self.subTest(host=host):
                self.assertEqual(actions.public_host_problem(host), actions.NOT_WEB_ADDRESS)

    def test_public_names_that_lead_to_this_pc_or_its_network(self) -> None:
        # Wildcard DNS, names fixed to 127.0.0.1 and router setup pages: Jarvis checks the name only.
        for host in ("192.168.1.1.nip.io", "app.10.0.0.5.nip.io", "127-0-0-1.sslip.io", "x.sslip.io", "localtest.me",
                     "a.lvh.me", "routerlogin.net", "www.routerlogin.com", "tplinkwifi.net", "router.asus.com",
                     "fritz.box", "10.0.0.1.example.net", "x.192-168-0-10.example.org", "host-127-0-0-1.example.com"):
            with self.subTest(host=host):
                self.assertEqual(actions.public_host_problem(host), actions.LOCAL_ADDRESS)
                self.assertEqual(actions.web_link_problem(f"https://{host}/admin"), actions.LOCAL_ADDRESS)
        for host in ("8.8.8.8.example.net", "www.2-1-3-4.example.com", "asus.com", "www.nip.com",
                     "2026-10-09.example.org", "notrouterlogin.net"):
            with self.subTest(host=host):
                self.assertEqual(actions.public_host_problem(host), "")

    def test_the_label_shows_the_end_of_the_host(self) -> None:
        for host, shown in (("www.example.org", "www.example.org"), ("example.org", "example.org"),
                            ("accounts.google.com.sign-in-check.example.net", "\u2026example.net"),
                            ("evil.github.io", "evil.github.io"), ("shop.example.co.uk", "\u2026example.co.uk"),
                            ("www.shop.example.co.uk", "\u2026example.co.uk"),
                            ("www.the-very-long-name-of-a-museum-example.com", "\u2026seum-example.com"),
                            ("xn--bcher-kva.example", "\u2026cher-kva.example"),
                            ("museum.example.net", "\u2026example.net"), ("a.b.example.org", "a.b.example.org")):
            with self.subTest(host=host):
                label = actions.label_host(host)
                self.assertEqual(label, shown)
                self.assertLessEqual(len(label), actions.LABEL_HOST_CHARS)
                self.assertTrue(host.endswith(label.lstrip("\u2026")))
        card = self.web_open("Open: title=[1] Sign in | link=https://accounts.google.com.sign-in-check.example.net/x",
                             "accounts.google.com.sign-in-check.example.net")
        view = card_view(card, TODAY)
        self.assertEqual(view.kind_label, "web \u00b7 \u2026example.net")
        self.assertTrue(view.detail.startswith("accounts.google.com.sign-in-check.example.net \u00b7 web source"))

    def test_the_open_allowlist_is_unchanged(self) -> None:
        # The web rule is separate: an Open card from the briefing or the planner still needs a listed host.
        self.assertFalse(link_allowed("https://www.example.org/visit"))
        self.assertTrue(parse_action_line(self.SOURCE).error)
        self.assertEqual(parse_action_line(self.SOURCE, link_hosts=("www.example.org",)).error, "")

    def test_web_is_not_part_of_the_id_and_is_kept(self) -> None:
        plain = parse_action_line(self.SOURCE, link_hosts=("www.example.org",))
        self.assertFalse(plain.web)
        web = self.web_open()
        self.assertEqual(web.id, plain.id)
        self.assertTrue(actions.with_error(web, "refused").web)
        self.assertTrue(actions.restrict(web, {OPEN}, actions.SOURCE_ASK).web)
        self.assertTrue(actions.restrict(web, {TODO}, actions.SOURCE_ASK).web)   # refused, still web
        self.assertTrue(dataclasses.replace(web, title="x").web)
        # web, then the planner modes' mode and serves (memory only, never in the id) come last.
        self.assertEqual([field.name for field in dataclasses.fields(ProposedAction)][-3:], ["web", "mode", "serves"])

    def test_a_web_open_card(self) -> None:
        card = self.web_open()
        self.assertEqual(actions.kind_label(card), "web \u00b7 www.example.org")
        self.assertEqual(actions.open_text(card), "Open page")
        view = card_view(card, TODAY)
        self.assertEqual(view.kind_label, "web \u00b7 www.example.org")
        self.assertEqual(view.title, "[1] Visit - Example Museum")
        self.assertEqual(view.detail, "www.example.org \u00b7 web source \u00b7 opens in your browser")
        self.assertEqual((view.open_text, view.approve_text, view.decidable, view.note), ("Open page", "Done", True, ""))
        # Not web: the same card reads like any Open card.
        plain = parse_action_line(self.SOURCE, link_hosts=("www.example.org",))
        self.assertEqual((actions.kind_label(plain), actions.open_text(plain)), (OPEN, "Open"))
        self.assertEqual(card_view(plain, TODAY).detail, "www.example.org")

    def test_an_international_domain_gets_a_warning(self) -> None:
        card = self.web_open("Open: title=[2] Books | link=https://xn--bcher-kva.example/page", "xn--bcher-kva.example")
        self.assertEqual(card.error, "")
        self.assertTrue(actions.idn_host(card.link))
        view = card_view(card, TODAY)
        # In the detail, above Open page (the note is drawn below Deny / Done).
        self.assertEqual(view.note, "")
        self.assertEqual(view.detail, "xn--bcher-kva.example \u00b7 web source \u00b7 opens in your browser \u00b7 "
                                      + actions.IDN_WARNING)
        self.assertEqual(view.kind_label, "web \u00b7 \u2026cher-kva.example")
        warned = dataclasses.replace(card, warnings=(actions.IDN_WARNING, "other"))
        self.assertEqual(card_view(warned, TODAY).detail.count(actions.IDN_WARNING), 1)   # said once
        self.assertEqual(card_view(warned, TODAY).note, "other")
        self.assertFalse(actions.idn_host("https://www.example.org/"))

    def test_a_web_todo_shows_the_site_it_opens(self) -> None:
        # Its link may be any public site (a source's page): the detail names the domain and the
        # tool reads Open page, like a source's card.
        line = "Todo: title=Buy tickets | due=2026-10-09 18:00 | block= | acct= | link=https://www.example.org/visit"
        todo = dataclasses.replace(parse_action_line(line, link_hosts=("www.example.org",)), web=True)
        self.assertEqual((actions.kind_label(todo), actions.open_text(todo)), (TODO, "Open page"))
        view = card_view(todo, TODAY)
        self.assertTrue(view.detail.endswith(" \u00b7 opens www.example.org"), view.detail)
        plain = parse_action_line(line, link_hosts=("www.example.org",))
        self.assertEqual(actions.open_text(plain), "Open")
        self.assertNotIn("opens", card_view(plain, TODAY).detail)


class PlanModeCardTests(unittest.TestCase):
    """A day plan's time block (the productivity bundle): ``mode`` and ``serves`` live in memory only,
    never in the id; the card reads "block" and its button "Add block"."""

    LINE = "Calendar: Work on: Lab report | 2026-10-07 10:00-11:00 | | | For: d1"

    def test_mode_and_serves_are_not_part_of_the_id(self) -> None:
        plain = parse_action_line(self.LINE)
        block = dataclasses.replace(plain, mode="plan", serves="todo-card-1", notes="For: Lab report")
        self.assertEqual(actions._action_id(block), plain.id)
        self.assertEqual(actions._action_id(dataclasses.replace(plain, mode="nudge")), plain.id)
        self.assertEqual((plain.mode, plain.serves), ("", ""))
        refused = actions.with_error(block, "Too close")
        self.assertEqual((refused.mode, refused.serves), ("plan", "todo-card-1"))
        self.assertEqual(refused.id, actions.with_error(plain, "Too close").id)

    def test_labels(self) -> None:
        block = dataclasses.replace(parse_action_line(self.LINE), mode="plan")
        self.assertEqual((approve_label(block), actions.kind_label(block)), ("Add block", "block"))
        view = actions.card_view(block, date(2026, 10, 7))
        self.assertEqual((view.kind_label, view.approve_text), ("block", "Add block"))
        plain = parse_action_line(self.LINE)
        self.assertEqual((approve_label(plain), actions.kind_label(plain)), ("Approve", "calendar"))


class ReopenTests(unittest.TestCase):
    def test_reopen_removes_only_a_denied_decision(self) -> None:
        """B5, R15: a follow-up draft the owner clicked again reopens its denied id; sent, unknown,
        failed, done or created decisions are never reopened."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "actions.json"
            store = ActionStore(path, clock=FixedClock(NOW))
            for action_id, status in (("denied1", "denied"), ("sent1", "sent"), ("unknown1", "unknown"),
                                      ("failed1", "failed"), ("done1", "done"), ("created1", "created")):
                store.set(action_id, status)
            self.assertTrue(store.reopen("denied1"))
            self.assertIsNone(store.get("denied1"))
            self.assertFalse(store.is_decided("denied1"))
            self.assertNotIn("denied1", json.loads(path.read_text(encoding="utf-8")))   # saved
            for action_id in ("sent1", "unknown1", "failed1", "done1", "created1", "nothing"):
                with self.subTest(action_id=action_id):
                    self.assertFalse(store.reopen(action_id))
            self.assertEqual(ActionStore(path).get("sent1")["status"], "sent")


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
