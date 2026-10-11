"""Tests for briefing_reader.ask.dayplan: the day plan's hours, free time, what needs time, the
<todos> rows and the checks of every proposed block (spec 3.2, 8.1; R21, R28, R30).

Every title and address is invented; times are naive wall times in America/Los_Angeles.
"""

from __future__ import annotations

import dataclasses
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from briefing_reader import gcal
from briefing_reader.actions import parse_action_line
from briefing_reader.ask import dayplan
from briefing_reader.ask.dayplan import (
    BUFFER_MIN,
    MIN_SLOT_MIN,
    NO_HOURS,
    PART_AFTERNOON,
    PART_DAY,
    PART_EVENING,
    PART_MORNING,
    PART_OVER,
    PART_TOMORROW,
    DayCheck,
    PlanWindow,
    Slot,
    free_minutes,
    free_slots,
    is_late,
    needs_time,
    plan_todos,
    plan_window,
    row_targets,
    row_title,
    todo_rows,
)
from briefing_reader.ask.modes import ModeInputs, TodoFact
from briefing_reader.config import ProductivityConfig
from briefing_reader.gcal import EventBrief, Guest

PDT = timezone(timedelta(hours=-7), "PDT")
ZONE = "America/Los_Angeles"
SETTINGS = ProductivityConfig(enabled=True)
DAY = date(2026, 10, 7)
LQ, RQ = chr(0x2039), chr(0x203A)   # what context.data() makes of < and >


def at(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute)


def aware(hour: int, minute: int = 0, day: date = DAY, tz: timezone = PDT) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)


def event(title: str, start: datetime, minutes: int = 60, **changes) -> EventBrief:
    return EventBrief(f"e{abs(hash(title)) % 10**8:08d}", "primary", title, start=start,
                      end=start + timedelta(minutes=minutes), **changes)


def window(start: datetime = at(9, 35), end: datetime = at(22), day: date = DAY) -> PlanWindow:
    return PlanWindow(day, start, end, ZONE, "today")


def fixed_offsets(zone: str, moment: datetime) -> timedelta | None:
    """A stand-in for gcal.zone_offset: Los Angeles leaves daylight saving on Nov 1 2026 at 09:00 UTC."""
    if zone != ZONE:
        return None
    switch = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)
    return timedelta(hours=-7) if moment.astimezone(timezone.utc) < switch else timedelta(hours=-8)


class WindowTests(unittest.TestCase):
    def test_the_rest_of_today_starts_5_minutes_after_now_rounded_up(self) -> None:
        for now, start in ((aware(9, 30), at(9, 35)), (aware(9, 31), at(9, 40)),
                           (aware(9, 34).replace(second=20), at(9, 40)), (aware(6, 0), at(8, 0))):
            with self.subTest(now=now):
                found = plan_window(now, PART_DAY, SETTINGS, ZONE)
                self.assertEqual((found.day, found.start, found.end, found.label, found.switched),
                                 (DAY, start, at(22), "today", False))

    def test_parts_are_clipped_to_now_and_the_day(self) -> None:
        cases = {PART_MORNING: (at(9, 35), at(12)), PART_AFTERNOON: (at(12), at(17)), PART_EVENING: (at(17), at(22))}
        for part, (start, end) in cases.items():
            with self.subTest(part=part):
                found = plan_window(aware(9, 30), part, SETTINGS, ZONE)
                self.assertEqual((found.start, found.end, found.part), (start, end, part))
                self.assertEqual(found.label, dayplan.LABELS[part])
        self.assertEqual(plan_window(aware(13, 0), PART_AFTERNOON, SETTINGS, ZONE).start, at(13, 5))

    def test_a_part_already_over(self) -> None:
        self.assertEqual(plan_window(aware(11, 45), PART_MORNING, SETTINGS, ZONE), PART_OVER)
        self.assertEqual(plan_window(aware(18, 0), PART_AFTERNOON, SETTINGS, ZONE), PART_OVER)
        short = dataclasses.replace(SETTINGS, day_end="16:30")
        self.assertEqual(plan_window(aware(9, 0), PART_EVENING, short, ZONE), NO_HOURS)

    def test_tomorrow(self) -> None:
        found = plan_window(aware(9, 30), PART_TOMORROW, SETTINGS, ZONE)
        tomorrow = DAY + timedelta(days=1)
        self.assertEqual((found.day, found.start, found.end, found.label, found.switched),
                         (tomorrow, at(8, day=tomorrow), at(22, day=tomorrow), "tomorrow", False))
        self.assertTrue(found.tomorrow)

    def test_plan_my_day_plans_tomorrow_only_when_it_is_late(self) -> None:
        """R30: 9:00 AM (even on a full day) plans today; 19:00 (evening_from_hour) and day_end - 60
        min plan tomorrow."""
        self.assertEqual(plan_window(aware(9, 0), PART_DAY, SETTINGS, ZONE).label, "today")
        for now, evening in ((aware(19, 0), 18), (aware(21, 0), 24), (aware(21, 30), 24)):
            with self.subTest(now=now, evening=evening):
                found = plan_window(now, PART_DAY, SETTINGS, ZONE, evening_from_hour=evening)
                self.assertEqual((found.day, found.label, found.switched), (DAY + timedelta(days=1), "tomorrow", True))
        self.assertEqual(plan_window(aware(20, 59), PART_DAY, SETTINGS, ZONE, evening_from_hour=24).label, "today")
        self.assertTrue(is_late(at(18), SETTINGS, 18))
        self.assertFalse(is_late(at(17, 59), SETTINGS, 18))
        self.assertTrue(is_late(at(21), SETTINGS, 24))

    def test_now_is_read_in_the_personal_zone(self) -> None:
        with mock.patch.object(gcal, "zone_offset", fixed_offsets):
            utc_now = datetime(2026, 10, 7, 16, 30, tzinfo=timezone.utc)   # 9:30 AM in Los Angeles
            self.assertEqual(plan_window(utc_now, PART_DAY, SETTINGS, ZONE).start, at(9, 35))


class FreeSlotTests(unittest.TestCase):
    def test_free_time_between_events(self) -> None:
        events = {"personal": [event("Dentist", aware(11), 45)],
                  "work": [event("Project sync", aware(14), 60, guests=(Guest("ana@example.edu", "Ana Lima"),))]}
        slots = free_slots(events, window(), own=("you@example.edu",))
        self.assertEqual(slots, (Slot(at(9, 35), at(11)), Slot(at(11, 45), at(13, 50)), Slot(at(15, 10), at(22))))
        self.assertEqual(free_minutes(slots), 85 + 125 + 410)

    def test_buffers_only_around_meetings_with_other_guests(self) -> None:
        solo = event("Focus", aware(14), 60)
        own_only = event("Own sync", aware(16), 60, guests=(Guest("You@Example.edu", ""),))
        slots = free_slots({"personal": [solo, own_only]}, window(at(13), at(18)), own=("you@example.edu",))
        self.assertEqual(slots, (Slot(at(13), at(14)), Slot(at(15), at(16)), Slot(at(17), at(18))))
        guest = event("Review", aware(14), 60, guests=(Guest("ben@example.org", "Ben Ode"),))
        slots = free_slots({"work": [guest]}, window(at(13), at(18)))
        self.assertEqual(slots, (Slot(at(13), at(14) - timedelta(minutes=BUFFER_MIN)),
                                 Slot(at(15) + timedelta(minutes=BUFFER_MIN), at(18))))

    def test_declined_free_working_location_and_all_day_leave_the_time_free(self) -> None:
        """R21."""
        events = {"work": [event("Team lunch", aware(12), 60, self_response="declined"),
                           event("Office hours (FYI)", aware(16), 60, transparent=True),
                           event("Home", aware(9), 480, event_type=gcal.WORKING_LOCATION)],
                  "personal": [EventBrief("c1", "primary", "Conference", all_day_start=DAY, all_day_end=DAY)]}
        self.assertEqual(free_slots(events, window()), (Slot(at(9, 35), at(22)),))
        self.assertFalse(dayplan.busy_event(events["work"][0]))
        self.assertTrue(events["work"][1].free and events["work"][2].free)

    def test_short_gaps_are_no_slot(self) -> None:
        events = {"personal": [event("A", aware(10), 60), event("B", aware(11, 20), 60)]}
        slots = free_slots(events, window(at(10), at(13)))
        self.assertEqual(slots, (Slot(at(12, 20), at(13)),))   # 11:00-11:20 is shorter than 25 minutes
        self.assertEqual(MIN_SLOT_MIN, 25)

    def test_events_of_another_zone_are_moved_into_the_personal_one(self) -> None:
        eastern = timezone(timedelta(hours=-4), "EDT")
        events = {"work": [event("East coast sync", aware(14, tz=eastern), 60)]}   # 11:00-12:00 in Los Angeles
        with mock.patch.object(gcal, "zone_offset", fixed_offsets):
            self.assertEqual(free_slots(events, window(at(10), at(13))), (Slot(at(10), at(11)), Slot(at(12), at(13))))

    def test_a_daylight_saving_day(self) -> None:
        """Nov 1 2026: Los Angeles goes back an hour at 2 AM; a UTC event at 17:00 is 9:00 AM PST."""
        day = date(2026, 11, 1)
        events = {"work": [event("Early call", datetime(2026, 11, 1, 17, tzinfo=timezone.utc), 60)]}
        with mock.patch.object(gcal, "zone_offset", fixed_offsets):
            slots = free_slots(events, window(at(8, day=day), at(12, day=day), day=day))
        self.assertEqual(slots, (Slot(at(8, day=day), at(9, day=day)), Slot(at(10, day=day), at(12, day=day))))

    def test_zone_offset_reads_the_real_zone(self) -> None:
        offset = gcal.zone_offset(ZONE, datetime(2026, 10, 7, 16, 30, tzinfo=timezone.utc))
        if offset is None:
            self.skipTest("neither a time zone database nor ICU on this machine")
        self.assertEqual(offset, timedelta(hours=-7))
        self.assertEqual(gcal.zone_offset(ZONE, datetime(2026, 11, 2, 16, tzinfo=timezone.utc)), timedelta(hours=-8))
        self.assertIsNone(gcal.zone_offset("Nowhere/Fake", datetime(2026, 10, 7, tzinfo=timezone.utc)))
        self.assertIsNone(gcal.zone_offset("", datetime(2026, 10, 7, tzinfo=timezone.utc)))
        self.assertEqual(gcal.wall_in_zone(at(9), ZONE), at(9))   # naive: a wall time already


def todo(title: str, due: str = "", block: str = "", planned: str = "", action_id: str = "") -> TodoFact:
    return TodoFact("", action_id or f"id-{title[:6]}", title, due, "ask", block, planned)


class NeedsTimeTests(unittest.TestCase):
    def test_what_needs_time(self) -> None:
        now = at(9, 30)
        self.assertFalse(needs_time(ModeInputs(), now))
        self.assertTrue(needs_time(ModeInputs(), now, "gym and reading"))      # R52: the owner's words
        self.assertTrue(needs_time(ModeInputs(todos=(todo("Read chapter 4"),)), now))
        self.assertFalse(needs_time(ModeInputs(todos=(todo("Email the lab", block="15:00-16:00"),)), now))
        self.assertTrue(needs_time(ModeInputs(deadlines=("Lab report | 2026-10-12",)), now))
        self.assertFalse(needs_time(ModeInputs(deadlines=("Thesis | 2026-11-30",)), now))
        self.assertTrue(needs_time(ModeInputs(followup_counts=(2, 0)), now))
        self.assertFalse(needs_time(ModeInputs(followup_counts=(0, 3)), now))

    def test_a_todo_a_plan_block_serves_that_day_is_left_out(self) -> None:
        """R28: served today, it no longer needs time today (a plan of another day still sees it)."""
        inputs = ModeInputs(todos=(todo("Read chapter 4", planned=DAY.isoformat()),))
        today = dataclasses.replace(inputs, todos=plan_todos(inputs, DAY))
        self.assertEqual(today.todos, ())
        self.assertFalse(needs_time(today, at(9)))
        self.assertEqual(len(plan_todos(inputs, DAY + timedelta(days=1))), 1)

    def test_rows_soonest_due_first_with_ids(self) -> None:
        inputs = ModeInputs(todos=(todo("No due date"), todo("Email the lab group", "2026-10-12", "15:00-16:00"),
                                   todo("Read chapter 4", "2026-10-11T23:59")),
                            deadlines=("Lab report | 2026-10-12",), followup_counts=(4, 2))
        inputs = dataclasses.replace(inputs, todos=plan_todos(inputs, DAY))
        self.assertEqual([item.row for item in inputs.todos], ["t1", "t2", "t3"])
        self.assertEqual(todo_rows(inputs), (
            ("t1", "t1 | todo | Read chapter 4 | due 2026-10-11 23:59 | from ask"),
            ("t2", "t2 | todo | Email the lab group | due 2026-10-12 | from ask | block proposed 15:00-16:00"),
            ("t3", "t3 | todo | No due date | no due date | from ask"),
            ("d1", "d1 | deadline | Lab report | 2026-10-12"),
            ("e1", "e1 | emails | 4 wait on your answer | 2 owed to you")))
        targets = row_targets(inputs)
        self.assertEqual(targets["t1"].title, "Read chapter 4")
        self.assertEqual((targets["d1"], targets["e1"]), ("Lab report", dayplan.EMAILS_TITLE))
        self.assertEqual(row_title("t2", targets), "Email the lab group")
        self.assertEqual((row_title("words", targets), row_title("briefing", targets)),
                         ("your words", "a deadline in the briefing"))

    def test_rows_are_sanitized(self) -> None:
        inputs = ModeInputs(todos=(TodoFact("t1", "x", "Read <ch4> | now\nplease", "", "ask"),),
                            deadlines=("Lab <report> | 2026-10-12",))
        rows = [text for _row, text in todo_rows(inputs)]
        self.assertEqual(rows[0], f"t1 | todo | Read {LQ}ch4{RQ} / now please | no due date | from ask")
        self.assertEqual(rows[1], f"d1 | deadline | Lab {LQ}report{RQ} | 2026-10-12")


def card(line: str):
    return parse_action_line(line)


class DayCheckTests(unittest.TestCase):
    def make(self, *, owner_words: str = "", briefing: bool = False, notes=(), now: datetime = at(9, 30),
             events=None) -> DayCheck:
        events = events if events is not None else [
            ("personal", event("Dentist", aware(11), 45)),
            ("work", event("Project sync", aware(14), 60, guests=(Guest("ana@example.edu", "Ana Lima"),))),
            ("work", event("Team lunch", aware(12), 60, self_response="declined"))]
        self.window = window()
        slots = free_slots({"all": [brief for _alias, brief in events]}, self.window)
        inputs = ModeInputs(todos=(TodoFact("t1", "todo-card-1", "Read chapter 4", "2026-10-08", "ask"),),
                            deadlines=("Lab report | 2026-10-09",))
        return DayCheck(self.window, slots, events, now, row_targets(inputs), owner_words=owner_words,
                        briefing=briefing, notes=notes)

    def check(self, line: str, **kwargs) -> str:
        return self.make(**kwargs)(card(line))

    def test_a_good_block_is_recorded_with_its_row(self) -> None:
        check = self.make()
        block = card("Calendar: Work on: Lab report | 2026-10-07 09:45-11:00 | | | For: d1")
        self.assertEqual(check(block), "")
        self.assertEqual(check.served, {block.id: "d1"})
        declined_time = card("Calendar: Read chapter 4 | 2026-10-07 12:00-12:45 | | | For: T1")
        self.assertEqual(check(declined_time), "")   # a declined meeting leaves its time free
        self.assertEqual(check.served[declined_time.id], "t1")

    def test_every_rule(self) -> None:
        cases = [
            ("Calendar: Lab report | 2026-10-07 | | | For: d1", dayplan.NOT_TIMED),
            ("Calendar: Lab report | 2026-10-07 10:00-11:00 | weekly | | For: d1", dayplan.REPEATS),
            ("Calendar: Lab report | 2026-10-08 10:00-11:00 | | | For: d1", dayplan.OTHER_DAY),
            ("Calendar: Lab report | 2026-10-07 09:00-10:00 | | | For: d1", dayplan.IN_THE_PAST),
            ("Calendar: Lab report | 2026-10-07 09:31-10:00 | | | For: d1", dayplan.IN_THE_PAST),
            ("Calendar: Lab report | 2026-10-07 21:30-22:30 | | | For: d1",
             "Outside the hours Jarvis plans (9:35 AM-10:00 PM)"),
            ("Calendar: Lab report | 2026-10-07 10:00-10:10 | | | For: d1", dayplan.LENGTH),
            ("Calendar: Lab report | 2026-10-07 15:15-18:30 | | | For: d1", dayplan.LENGTH),
            ("Calendar: Lab report | 2026-10-07 14:15-14:45 | | | For: d1", "Overlaps Project sync at 2:00 PM"),
            ("Calendar: Lab report | 2026-10-07 13:15-14:00 | | | For: d1", "Too close to Project sync at 2:00 PM"),
            ("Calendar: Lab report | 2026-10-07 10:30-11:15 | | | For: d1", "Overlaps Dentist at 11:00 AM"),
            ("Calendar: Email ana@example.edu | 2026-10-07 15:30-16:00 | | | For: d1", dayplan.ADDRESS_OR_LINK),
            ("Calendar: Lab report | 2026-10-07 15:30-16:00 | | https://example.org | For: d1",
             dayplan.ADDRESS_OR_LINK),
            ("Calendar: Lab report | 2026-10-07 15:30-16:00 | | | see www.example.org", dayplan.ADDRESS_OR_LINK),
            # plan_data's stand-in for a masked address, copied from a to-do title (any case)
            ("Calendar: Reply to [address] about the lab | 2026-10-07 15:30-16:00 | | | For: t1",
             dayplan.ADDRESS_OR_LINK),
            ("Calendar: Lab report | 2026-10-07 15:30-16:00 | | | For: d1 [Address]", dayplan.ADDRESS_OR_LINK),
            ("Calendar: Lab report | 2026-10-07 15:30-16:00 | | Room 4 | For: d1", dayplan.NO_PLACE),
            ("Calendar: Lab report | 2026-10-07 15:30-16:00 | | | For: t9", dayplan.SERVES_NOTHING),
            ("Calendar: Lab report | 2026-10-07 15:30-16:00 | | |", dayplan.SERVES_NOTHING),
            ("Calendar: Lab report | 2026-10-07 15:30-16:00 | | | For: d1 and t1", dayplan.SERVES_NOTHING),
            ("Calendar: Gym | 2026-10-07 15:30-16:00 | | | For: words", dayplan.SERVES_NOTHING),
            ("Calendar: Lab report | 2026-10-07 15:30-16:00 | | | For: briefing", dayplan.SERVES_NOTHING),
        ]
        for line, reason in cases:
            with self.subTest(line=line):
                self.assertEqual(self.check(line), reason)

    def test_words_and_briefing_rows_only_when_given(self) -> None:
        self.assertEqual(self.check("Calendar: Gym | 2026-10-07 15:30-16:30 | | | For: words", owner_words="gym"), "")
        self.assertEqual(self.check("Calendar: Thesis | 2026-10-07 15:30-16:30 | | | For: briefing", briefing=True),
                         "")

    def test_no_overlap_with_another_block_of_the_plan(self) -> None:
        check = self.make()
        self.assertEqual(check(card("Calendar: Lab report | 2026-10-07 15:30-16:30 | | | For: d1")), "")
        self.assertEqual(check(card("Calendar: Chapter 4 | 2026-10-07 16:00-17:00 | | | For: t1")),
                         dayplan.OVERLAPS_BLOCK)
        self.assertEqual(check(card("Calendar: Chapter 4 | 2026-10-07 16:30-17:00 | | | For: t1")), "")

    def test_a_title_repeating_a_note_is_refused_unless_the_owner_asked(self) -> None:
        """R7."""
        note = "Prepare the grant budget with the whole lab team"
        line = "Calendar: Prepare the grant budget with the whole lab | 2026-10-07 15:30-16:30 | | | For: d1"
        self.assertEqual(self.check(line, notes=(note,)), dayplan.REPEATS_NOTE)
        self.assertEqual(self.check(line, notes=(note,), owner_words="prepare the grant budget with the whole lab"),
                         "")
        self.assertEqual(self.check(line, notes=("Short note",)), "")

    def test_a_note_in_the_place_never_reaches_the_event(self) -> None:
        """The plan grammar keeps "where" empty: a note (or briefing text) there would go to Google as
        the event's location, past the title's note check."""
        note = "My therapist appointments are on Tuesdays with Dr Vale"
        line = f"Calendar: Work on: Lab report | 2026-10-07 15:30-16:30 | | {note} | For: d1"
        self.assertEqual(self.check(line, notes=(note,)), dayplan.NO_PLACE)
        self.assertEqual(self.check(line), dayplan.NO_PLACE)
        self.assertEqual(self.check("Calendar: Work on: Lab report | 2026-10-07 15:30-16:30 | |   | For: d1"), "")

    def test_a_replan_with_new_times_gives_new_ids(self) -> None:
        first = card("Calendar: Work on: Lab report | 2026-10-07 10:00-11:00 | | | For: d1")
        again = card("Calendar: Work on: Lab report | 2026-10-07 15:30-16:30 | | | For: d1")
        notes_only = card("Calendar: Work on: Lab report | 2026-10-07 10:00-11:00 | | | For: Lab report")
        self.assertNotEqual(first.id, again.id)
        self.assertEqual(first.id, notes_only.id)   # notes are not part of a Calendar id


class ReprTests(unittest.TestCase):
    def test_reprs_hold_no_text(self) -> None:
        fact = TodoFact("t1", "card-id", "Zq7 secret title", "2026-10-08", "ask")
        self.assertNotIn("Zq7", repr(fact))
        self.assertNotIn("Zq7", repr(ModeInputs(todos=(fact,), deadlines=("Zq7 | 2026-10-08",))))


if __name__ == "__main__":
    unittest.main()
