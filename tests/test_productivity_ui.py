"""Tests for briefing_reader.productivity_ui: the productivity bundle's day read and offers on their
own (spec 4, 15; ProductivityController with a fake clock, fake calendars and a real LIVE stream).

The timer runs while the reading screen is visible or minimized; the day is read only while visible,
plus one read of the meeting's hour before a minimized prep offer (R35); the zone comes with the day
read and Google is never called on the GUI thread (R16); prep offers come once, inside the lead
window, never after a sleep past the start; the plan offer's transient reasons are retried (R36) and
its sentence waits until the briefing was heard (R26); an unlock seen by a tick (R18); a stale
cached CapCheck is refreshed first (R13); a failing prod job logs its kind and type only (R8);
back-to-back meetings (R51) and ignored recurring meetings (R38); shutdown stops everything. While
minimized only what the hour read found again is offered, one read per meeting (also when it fails),
never after the reading screen went away; Ctrl+M settles a sentence as text only (R37); a held prep
sentence only up to 3 minutes before the start; the READS lines say only what holds. A sentence not
said yet is never said for a meeting a later read found moved or cancelled, nor from a schedule older
than 30 minutes (back on screen the offers wait for the fresh read); two sentences come 5 minutes
apart, the meeting's first; a queued one is taken back when the session locks or the window is
minimized where it may not be said there; a later group counts the day's earlier offers.
Qt runs offscreen; every name and address is invented.
"""

from __future__ import annotations

import dataclasses
import logging
import tempfile
import threading
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication

from briefing_reader import hud, live
from briefing_reader.ask.modes import ModeInputs, TodoFact
from briefing_reader.ask.offers import QuietFacts, offer_key
from briefing_reader.ask.usage import CapCheck
from briefing_reader.config import ProductivityConfig
from briefing_reader.gcal import EventBrief, Guest
from briefing_reader.prepseries import SeriesStore, series_key
from briefing_reader.productivity_ui import HELD, QUEUED, SPOKEN, TEXT_ONLY, ProductivityController
from tests.ui_fakes import wait_for

_app: QApplication | None = None
PDT = timezone(timedelta(hours=-7))
ZONE = "America/Los_Angeles"
DAY = date(2026, 10, 5)                       # a Monday
AM_KEY = "2026-10-05 AM"
ANA = Guest("zq7ana@example.edu", "Zq7 Ana", "accepted")
BEN = Guest("ben@example.org", "Ben Example", "accepted")
TITLE = "Zq7 Sync"
LOGGER = "briefing_reader"
DOT = chr(0xB7)   # the middle dot of the sub lines


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_productivity_ui", "-platform", "offscreen"])


def at(hour: int, minute: int = 0, *, days: int = 0) -> datetime:
    return datetime.combine(DAY + timedelta(days=days), time(hour, minute), tzinfo=PDT)


def meeting(event_id: str = "evt0001", start: datetime | None = None, minutes: int = 30, *, title: str = TITLE,
            guests: tuple[Guest, ...] = (ANA,), **extra) -> EventBrief:
    start = start or at(14)
    return EventBrief(event_id, "primary", title, start=start, end=start + timedelta(minutes=minutes),
                      organizer_self=True, organizer_email="me@example.edu", guests=guests,
                      self_response="accepted", **extra)


class Clock:
    def __init__(self, start: datetime) -> None:
        self.at = start

    def now(self) -> datetime:
        return self.at

    def advance(self, minutes: float) -> datetime:
        self.at += timedelta(minutes=minutes)
        return self.at


class FakeCal:
    def __init__(self, signed_in: bool = True) -> None:
        self.signed_in = signed_in

    def is_signed_in(self) -> bool:
        return self.signed_in


class FakeSources:
    """GoogleSources' surface for the day read: briefs per alias (only those inside the window), the
    zone (time_zone may refuse the GUI thread), the senders' own addresses; every read recorded.
    ``statuses``: a read's status per alias ("yes" when not given); ``during_read``: called inside
    each read (the window going away while a read is on its way)."""

    def __init__(self, briefs: dict[str, list[EventBrief]], *, gui_zone_refused: bool = False) -> None:
        self.briefs = briefs
        self.calendars = {alias: FakeCal() for alias in briefs}
        self.senders = {"work": SimpleNamespace(from_address=lambda: "Me@Example.edu")}
        self.reads: list[tuple[str, datetime, datetime, str]] = []
        self.zone_threads: list[str] = []
        self.gui_zone_refused = gui_zone_refused
        self.error: Exception | None = None
        self.statuses: dict[str, str] = {}
        self.during_read = None

    def event_briefs(self, alias, start, end, calendars):
        self.reads.append((alias, start, end, threading.current_thread().name))
        if self.during_read is not None:
            self.during_read()
        if self.error is not None:
            raise self.error
        status = self.statuses.get(alias, "yes")
        if status != "yes":
            return [], status
        found = [brief for brief in self.briefs.get(alias, ())
                 if brief.start is None or (brief.start < end and (brief.end or brief.start) > start)]
        return found, "yes"

    def time_zone(self) -> str:
        self.zone_threads.append(threading.current_thread().name)
        if self.gui_zone_refused and threading.current_thread() is threading.main_thread():
            raise AssertionError("time_zone() called on the GUI thread")
        return ZONE

    def cached_time_zone(self) -> str:
        return ZONE


class ControllerCase(unittest.TestCase):
    def make(self, briefs: dict[str, list[EventBrief]] | None = None, *, start: datetime | None = None,
             inline: bool = True, run_state=None, series=None, todos: int = 1, **settings) -> ProductivityController:
        self.clock = Clock(start or at(13, 30))
        self.sources = FakeSources(briefs if briefs is not None else {"personal": [], "work": [meeting()]},
                                   gui_zone_refused=not inline)
        values = dict(enabled=True)
        values.update(settings)
        config = SimpleNamespace(productivity=ProductivityConfig(**values), ask=SimpleNamespace(calendars=("primary",)),
                                 assistant=SimpleNamespace(address="sir"), display=SimpleNamespace(hour24=False),
                                 agenda=SimpleNamespace(evening_from_hour=18))
        self.act = "visible"
        self.facts = QuietFacts()
        self.ask = (True, True, 1.0)
        self.caps = CapCheck(True, 20, 60, left_bundle_day=17)
        self.refreshes = 0
        self.inputs = ModeInputs(todos=tuple(TodoFact("", f"todo{n}", "Read chapter 4", "2026-10-06", "briefing")
                                             for n in range(todos)))
        self.live = live.LiveStream(pictures=False)

        def refresh() -> None:
            self.refreshes += 1

        controller = ProductivityController(
            config, sources=self.sources, live=self.live, now=self.clock.now, activity=lambda: self.act,
            facts=lambda: dataclasses.replace(self.facts, activity=self.act), ask_state=lambda: self.ask,
            inputs=lambda: self.inputs, refresh_caps=refresh, run_state=run_state, series=series,
            caps=lambda: self.caps, inline=inline)
        self.addCleanup(controller.shutdown)
        self.posted, self.spoken, self.retired, self.changed, self.failed = [], [], [], [], []
        controller.offerPosted.connect(self.posted.append)
        controller.offerSpeak.connect(self.spoken.append)
        controller.offerRetired.connect(lambda offer, how: self.retired.append((offer, how)))
        controller.offerChanged.connect(self.changed.append)
        controller._bridge.jobFailed.connect(lambda seq, kind: self.failed.append(kind))
        self.c = controller
        return controller

    def tick(self, minutes: float = 1) -> None:
        self.clock.advance(minutes)
        self.c.tick()

    def own_steps(self) -> list:
        tasks = [task for task in self.live.snapshot() if task.kind == live.TASK_OWN_READS]
        return list(tasks[0].steps) if tasks else []


class TimerAndReadTests(ControllerCase):
    def test_the_timer_follows_visible_minimized_and_off(self) -> None:
        c = self.make()
        self.assertFalse(c.timer_active)
        c.sync()
        self.assertTrue(c.timer_active)
        self.act = "minimized"
        c.sync()
        self.assertTrue(c.timer_active)
        self.act = "off"
        c.sync()
        self.assertFalse(c.timer_active)
        self.act = "visible"
        c.sync()
        self.assertTrue(c.timer_active)
        self.act = "off"
        c.tick()                                   # a tick that finds the screen gone stops the timer
        self.assertFalse(c.timer_active)

    def test_the_day_is_read_only_while_visible(self) -> None:
        c = self.make()
        c.sync()                                   # the first visible show reads the day
        self.assertEqual([alias for alias, *_rest in self.sources.reads], ["personal", "work"])
        self.assertEqual(c.day_zone(), ZONE)
        self.assertEqual(c.day_read_at(), at(13, 30))
        self.tick(5)
        self.assertEqual(len(self.sources.reads), 2)          # not again within 10 minutes
        self.tick(5)
        self.assertEqual(len(self.sources.reads), 4)          # every 10 minutes while visible
        self.act = "minimized"
        self.tick(11)
        self.tick(11)
        self.assertEqual(len(self.sources.reads), 4)          # never while minimized (nothing due)
        self.act = "off"
        c.request_day("tick")
        self.tick(11)
        self.assertEqual(len(self.sources.reads), 6)          # asked directly it reads; a tick never does
        steps = [step for step in self.own_steps() if step.kind == live.CALENDAR_DAY]
        self.assertEqual(len(steps), 1)                       # one line, replaced in place
        self.assertIn("personal 0; work 1", steps[0].summary)

    def test_the_zone_comes_from_the_prod_thread_never_the_gui_thread(self) -> None:
        """R16: the real prod thread reads the day and the personal zone; time_zone() refuses the GUI
        thread and is never called there."""
        c = self.make(inline=False)
        changed = []
        c.dayChanged.connect(lambda: changed.append(1))
        c.sync()
        self.assertTrue(wait_for(lambda: changed), "the day read arrived")
        self.assertEqual(self.sources.zone_threads, ["prod"])
        self.assertEqual({thread for *_rest, thread in self.sources.reads}, {"prod"})
        self.assertEqual(c.day_zone(), ZONE)
        self.assertTrue(c.guest_meeting_left_today(at(13, 30)))
        c.shutdown()
        c.join(5)
        self.assertFalse(c._worker.is_alive())

    def test_a_failing_prod_job_logs_its_kind_and_type_only(self) -> None:
        """R8: a reader raising with a subject in its message: one ERROR line with the job kind and the
        exception type; jobFailed; the LIVE step says failed; the message never reaches any record."""
        c = self.make()
        self.sources.error = ValueError("zq7subj Budget numbers")
        with self.assertLogs(LOGGER, level="DEBUG") as logs:
            c.sync()
        self.assertIn("ERROR:briefing_reader.productivity_ui:Productivity: day job failed (ValueError)", logs.output)
        self.assertEqual(self.failed, ["day"])
        for record in logs.records:
            self.assertNotIn("zq7", record.getMessage())
            self.assertIsNone(record.exc_info)
        day, = [step for step in self.own_steps() if step.kind == live.CALENDAR_DAY]
        self.assertEqual(day.status, live.STATUS_FAILED)
        self.sources.error = None
        self.tick(10)
        self.assertEqual(len(self.sources.reads), 3)          # the thread goes on: read again 10 minutes later
        self.assertIsNotNone(c.day_read_at())

    def test_shutdown_stops_everything(self) -> None:
        c = self.make()
        c.sync()
        c.shutdown()
        self.assertFalse(c.timer_active)
        reads = len(self.sources.reads)
        self.clock.advance(15)
        c.tick()
        c.request_day("tick")
        c.on_announced(AM_KEY)
        self.assertEqual(len(self.sources.reads), reads)
        self.assertEqual((self.posted, self.spoken), ([], []))


class PrepOfferTests(ControllerCase):
    def test_offered_once_at_lead_time_with_speech_and_sub_line(self) -> None:
        c = self.make()
        with self.assertLogs(LOGGER, level="INFO") as logs:
            c.sync()                                   # 1:30 PM: too early
            self.tick(10)                              # 1:40 PM
            self.assertEqual(self.posted, [])
            self.tick(5)                               # 1:45 PM: inside the 15 minutes
        offer, = self.posted
        self.assertEqual((offer.kind, offer.guests, offer.alias, offer.event_title), ("prep", 1, "work", TITLE))
        self.assertEqual(offer.sub, f"1 planner run (none without recent mail) {DOT} 17 of 20 left today "
                                    f"{DOT} as of 1:40 PM")
        speech, = self.spoken
        self.assertIs(speech, offer)
        self.assertEqual(offer.speech, "Your meeting at 2 PM starts in about 15 minutes, sir. Shall I prepare "
                                       "a brief?")
        self.assertEqual(offer.speech_state, QUEUED)
        self.assertIn("INFO:briefing_reader.productivity_ui:Prep offer: posted (1 guest(s), lead 15 min, spoken)",
                      logs.output)
        c.on_speaking(offer.speech)
        self.assertEqual(offer.speech_state, SPOKEN)
        for _ in range(5):
            self.tick(1)
        self.assertEqual(len(self.posted), 1)          # once
        text = "\n".join(logs.output)
        for secret in ("Zq7", "zq7", "Sync"):
            self.assertNotIn(secret, text)
        made = [step for step in self.own_steps() if step.kind == live.OFFER_MADE]
        self.assertEqual(made[0].title, "Offered a prep brief: 2:00 PM, 1 guest, from the read at 1:40 PM")
        self.assertEqual({field.label: field.value for field in made[0].fields}["Spoken"], "yes")

    def test_never_after_a_sleep_past_the_start(self) -> None:
        c = self.make()
        c.sync()
        self.tick(10)                                  # 1:40 PM
        self.clock.at = at(14, 1)                      # the laptop slept through 1:45-2:00
        c.tick()
        self.tick(1)
        self.assertEqual(self.posted, [])
        self.assertEqual([step for step in self.own_steps() if step.kind == live.OFFER_MADE], [])   # quiet

    def test_minimized_offers_flash_and_speak_after_the_hour_read(self) -> None:
        """R35: minimized with a schedule older than 30 minutes: one read of the meeting's hour first,
        then the offer with a taskbar flash and its time-only sentence."""
        c = self.make()
        c.sync()                                       # read at 1:30 PM while visible
        self.act = "minimized"
        c.sync()
        self.tick(16)                                  # 1:46 PM: the schedule is 16 minutes old: offered
        offer, = self.posted
        self.assertTrue(offer.flash)
        self.assertEqual(offer.speech_state, QUEUED)
        self.assertEqual(len(self.sources.reads), 2)   # no hour read for a fresh schedule

    def test_minimized_with_a_stale_schedule_reads_the_hour_first(self) -> None:
        c = self.make(briefs={"personal": [], "work": [meeting(start=at(14, 30))]}, start=at(13, 0))
        c.sync()                                       # read at 1:00 PM
        self.act = "minimized"
        c.sync()
        self.tick(15)                                  # 1:15 PM: early
        self.tick(1)                                   # 1:16 PM
        self.assertEqual(self.posted, [])
        self.clock.at = at(14, 15)                     # the window stayed minimized: the read is stale
        c.tick()
        alias, start, end, _thread = self.sources.reads[-1]
        self.assertEqual((alias, start, end), ("work", at(13, 30), at(15, 30)))   # the hour around it
        offer, = self.posted
        self.assertEqual((offer.flash, offer.stale, offer.read_at), (True, False, at(14, 15)))
        self.assertTrue(offer.sub.endswith("as of 2:15 PM"))
        self.assertEqual(offer.speech_state, QUEUED)
        hour = [step for step in self.own_steps() if step.kind == live.CALENDAR_DAY and step.key.startswith("hour:")]
        self.assertEqual(hour[0].title, "Checked the 1:30 PM-3:30 PM hours of work before an offer (minimized)")

    def test_a_meeting_gone_by_the_hour_read_is_not_offered(self) -> None:
        c = self.make(start=at(13, 0))
        c.sync()
        self.act = "minimized"
        self.sources.briefs["work"] = []               # cancelled meanwhile
        self.clock.at = at(13, 46)
        with self.assertLogs(LOGGER, level="INFO") as logs:
            c.tick()
        self.assertEqual(self.posted, [])
        self.assertIn("INFO:briefing_reader.productivity_ui:Prep offer: skipped (gone)", logs.output)

    def test_minimized_check_off_gives_an_entry_only(self) -> None:
        c = self.make(start=at(13, 0), minimized_check=False)
        c.sync()
        self.act = "minimized"
        self.clock.at = at(13, 46)
        c.tick()
        offer, = self.posted
        self.assertEqual((offer.stale, offer.flash, offer.speech_state, offer.held_reason),
                         (True, False, TEXT_ONLY, "stale"))
        self.assertEqual(self.spoken, [])
        self.assertEqual(len(self.sources.reads), 2)   # nothing read while minimized
        self.assertTrue(offer.sub.endswith("as of 1:00 PM"))

    def test_no_speech_with_prep_speak_minimized_off(self) -> None:
        c = self.make(prep_speak_minimized=False)
        c.sync()
        self.act = "minimized"
        self.tick(15)
        offer, = self.posted
        self.assertEqual((offer.flash, offer.speech_state, offer.held_reason), (True, HELD, "minimized"))
        self.act = "visible"
        c.sync()                                       # restored: said now (still >= 3 min before)
        self.assertEqual(self.spoken, [offer])
        self.assertEqual(offer.spoken_how, "restore")

    def test_in_a_meeting_and_quiet_hours_are_text_only(self) -> None:
        busy = meeting("evt0009", start=at(13, 30), minutes=60, guests=(BEN,))
        c = self.make(briefs={"personal": [busy], "work": [meeting()]})
        c.sync()
        self.tick(15)
        offer, = self.posted
        self.assertEqual((offer.speech_state, offer.held_reason, offer.flash), (TEXT_ONLY, "meeting", False))
        late = meeting("evt0002", start=at(22, 30))
        c2 = self.make(briefs={"personal": [], "work": [late]}, start=at(22, 0))
        c2.sync()
        self.tick(15)
        offer, = self.posted
        self.assertEqual((offer.speech_state, offer.held_reason), (TEXT_ONLY, "quiet_hours"))
        self.tick(30)
        self.assertEqual(self.spoken, [])

    def test_locked_then_unlocked_at_a_tick(self) -> None:
        """R18: no lock event: the tick polls; an unlock seen there says the held offer."""
        c = self.make()
        c.sync()
        self.facts = QuietFacts(locked=True)
        self.tick(15)
        offer, = self.posted
        self.assertEqual((offer.speech_state, offer.held_reason), (HELD, "locked"))
        self.facts = QuietFacts()
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.tick(1)
            c.on_speaking(offer.speech)
        self.assertEqual(offer.spoken_how, "unlock")
        self.assertIn("INFO:briefing_reader.productivity_ui:Prep offer: spoken (unlock)", logs.output)

    def test_back_to_back_meetings_get_one_speech(self) -> None:
        one = meeting("evt0001", start=at(13), minutes=60)
        two = meeting("evt0002", start=at(14), minutes=60, guests=(BEN,))
        c = self.make(briefs={"personal": [two], "work": [one]}, start=at(12, 30))
        c.sync()
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.tick(15)                              # 12:45 PM
        first, second = self.posted
        self.assertEqual(first.group, second.group)
        self.assertEqual(self.spoken, [first])
        self.assertEqual(first.speech, "You have meetings at 1 PM and 2 PM, sir. Shall I prepare briefs?")
        self.assertEqual((second.speech_state, second.held_reason), (TEXT_ONLY, "group"))
        self.assertIn("INFO:briefing_reader.productivity_ui:Prep offers: posted together (2)", logs.output)
        self.clock.at = at(13, 45)
        c.tick()
        self.assertEqual(len(self.posted), 2)          # not offered again at its own lead time

    def test_a_later_group_holds_only_the_offers_left_today(self) -> None:
        """R51 with prep_offers_per_day: a group later in the day counts the offers made earlier."""
        early, one = meeting("evt0001", start=at(13)), meeting("evt0002", start=at(14))
        two, three = meeting("evt0003", start=at(14, 30)), meeting("evt0004", start=at(15))
        c = self.make({"personal": [], "work": [early, one, two, three]}, start=at(12, 40), prep_offers_per_day=2)
        c.sync()
        self.tick(5)                                   # 12:45 PM: 1 of 2
        self.clock.at = at(13, 45)
        c.tick()                                       # back to back, a group would be 3: 1 is left
        self.assertEqual([offer.start for offer in self.posted], [at(13), at(14)])
        self.assertEqual(self.posted[1].group, ())

    def test_an_ignored_recurring_meeting_and_its_reset(self) -> None:
        """R38: an offer that ends unclicked counts; after 3 in a row the series is not offered (LIVE
        says why); a click resets the count."""
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        clock = Clock(at(13, 30))
        store = SeriesStore(Path(tmp.name) / "prep_series.json", clock=clock.now)
        key = series_key("work", "primary", "weekly01")
        store.record_ignored(key)
        store.record_ignored(key)
        weekly = meeting(series_id="weekly01")
        c = self.make(briefs={"personal": [], "work": [weekly]}, series=store)
        c.sync()
        self.tick(15)
        offer, = self.posted
        self.assertEqual(offer.series_key, key)
        self.clock.at = at(14, 31)                     # the meeting ended, nobody clicked
        with self.assertLogs(LOGGER, level="INFO") as logs:
            c.tick()
        self.assertEqual(self.retired, [(offer, "ended")])
        self.assertEqual(store.ignored(key), 3)
        self.assertIn("INFO:briefing_reader.productivity_ui:Prep series: an offer was ignored (3 in a row)",
                      logs.output)
        tomorrow = meeting("evt0002", start=at(14, days=1), series_id="weekly01")
        c2 = self.make(briefs={"personal": [], "work": [tomorrow]}, start=at(13, 30, days=1), series=store)
        c2.sync()
        self.tick(15)
        self.assertEqual(self.posted, [])
        made, = [step for step in self.own_steps() if step.kind == live.OFFER_MADE]
        self.assertEqual(made.title, 'Not offered: a prep brief (ignored 3 times - type "prep me" for this one)')
        store.reset(key)
        store.record_ignored(key)
        c3 = self.make(briefs={"personal": [], "work": [tomorrow]}, start=at(13, 30, days=1), series=store)
        c3.sync()
        self.tick(15)
        offer, = self.posted
        with self.assertLogs(LOGGER, level="INFO"):
            c3.use_offer(offer.id)                     # the click resets the count
        self.assertEqual(store.ignored(key), 0)
        self.clock.at = at(14, 31, days=1)
        c3.tick()
        self.assertEqual(store.ignored(key), 0)        # a clicked offer never counts as ignored

    def test_a_brief_ready_and_a_gone_link(self) -> None:
        c = self.make()
        c.sync()
        self.tick(15)
        offer, = self.posted
        self.assertIs(c.offer(offer.id), offer)
        c.use_offer(offer.id)
        c.brief_ready(offer.id, at(13, 47))
        self.assertEqual(self.retired, [(offer, "used")])
        self.assertTrue(offer.used)
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.assertIsNone(c.offer(offer.id))
        self.assertIn("INFO:briefing_reader.productivity_ui:Offer link: gone", logs.output)

    def test_a_stale_cached_check_is_refreshed_before_posting(self) -> None:
        """R13: an old CapCheck is asked for again (on the ask thread) and the offer waits a tick."""
        c = self.make()
        c.sync()
        self.ask = (True, True, 400.0)
        self.tick(15)
        self.assertEqual((self.posted, self.refreshes), ([], 1))
        self.tick(0.5)
        self.assertEqual(self.refreshes, 1)            # asked at most once a minute
        self.ask = (True, True, 1.0)
        self.tick(0.5)
        self.assertEqual(len(self.posted), 1)

    def test_not_ready_or_caps_refused_offers_nothing(self) -> None:
        c = self.make()
        c.sync()
        self.ask = (False, True, 1.0)
        self.tick(15)
        self.ask = (True, False, 1.0)
        self.tick(1)
        self.assertEqual(self.posted, [])
        self.ask = (True, True, 1.0)
        self.tick(1)
        self.assertEqual(len(self.posted), 1)


class PlanOfferTests(ControllerCase):
    def plan(self, **kw) -> ProductivityController:
        c = self.make({"personal": [meeting("evt0003", start=at(11), guests=(BEN,))], "work": []},
                      start=at(10, 12), **kw)
        c.sync()
        return c

    def test_posted_at_the_announcement_spoken_once_heard_by_view_and_jarvis(self) -> None:
        c = self.plan()
        with self.assertLogs(LOGGER, level="INFO") as logs:
            c.on_announced(AM_KEY)
        offer, = self.posted
        self.assertEqual((offer.kind, offer.slot_key, offer.speech_state, offer.held_reason),
                         ("plan", AM_KEY, HELD, "not_heard"))
        self.assertEqual(offer.sub, f"1 planner run {DOT} 17 of 20 left today")
        self.assertIn(f"INFO:briefing_reader.productivity_ui:Plan offer: posted for {AM_KEY} (held:not_heard)",
                      logs.output)
        self.tick(1)
        self.assertEqual(self.spoken, [])
        c.on_viewed(AM_KEY)
        self.assertEqual(self.spoken, [])
        c.on_tab_changed(hud.TAB_JARVIS)
        self.assertEqual(self.spoken, [offer])
        self.assertEqual((offer.speech, offer.spoken_how), ("Shall I plan your day, sir?", "viewed"))
        c.on_speaking(offer.speech)
        self.assertEqual(offer.speech_state, SPOKEN)

    def test_heard_after_finished_or_twenty_minutes(self) -> None:
        c = self.plan()
        c.on_announced(AM_KEY)
        offer, = self.posted
        c.on_playback_started(AM_KEY)
        self.tick(25)
        self.assertEqual(self.spoken, [])              # playing: wait for FINISHED
        c.on_playback_finished("2026-10-04 PM")        # another briefing: not this one
        self.assertEqual(self.spoken, [])
        c.on_playback_finished(AM_KEY)
        self.assertEqual(self.spoken, [offer])
        c2 = self.plan()
        c2.on_announced(AM_KEY)
        offer2, = self.posted
        self.tick(19)
        self.assertEqual(self.spoken, [])
        self.tick(1)
        self.assertEqual(self.spoken, [offer2])
        self.assertEqual(offer2.spoken_how, "after_20_min")

    def test_dropped_before_it_started_is_held_again(self) -> None:
        """R26: Play while the sentence is queued drops it; held again; said after FINISHED."""
        c = self.plan()
        c.on_announced(AM_KEY)
        offer, = self.posted
        c.on_viewed(AM_KEY)
        c.on_tab_changed(hud.TAB_JARVIS)
        self.assertEqual(offer.speech_state, QUEUED)
        with self.assertLogs(LOGGER, level="INFO") as logs:
            c.on_speech_dropped()
        self.assertEqual((offer.speech_state, offer.held_reason), (HELD, "dropped"))
        self.assertIn("INFO:briefing_reader.productivity_ui:Plan offer: speech dropped; held again", logs.output)
        self.facts = QuietFacts(playing=True)
        self.tick(1)
        self.assertEqual(offer.held_reason, "playing")
        self.facts = QuietFacts()
        c.on_playback_finished(AM_KEY)
        self.assertEqual(offer.speech_state, QUEUED)
        self.clock.advance(2)
        c._requeue_dropped(self.clock.now())           # the voice never started it (stale): held again
        self.assertEqual(offer.speech_state, HELD)

    def test_transient_reasons_are_retried_until_they_clear(self) -> None:
        """R36: not_ready, caps, no_day and early keep the candidate alive; planned is final."""
        c = self.make({"personal": [], "work": []}, start=at(7, 50))
        self.ask = (False, True, 1.0)
        c.sync()
        c.on_announced(AM_KEY)
        self.assertEqual(self.posted, [])              # early
        self.tick(10)                                  # 8:00 AM: not ready
        self.assertEqual(self.posted, [])
        self.ask = (True, False, 1.0)
        self.tick(1)                                   # caps
        self.assertEqual(self.posted, [])
        pending = [step for step in self.own_steps() if step.kind == live.OFFER_MADE]
        self.assertEqual(pending[-1].title, "Not offered yet: plan my day (caps)")
        self.ask = (True, True, 1.0)
        self.tick(1)
        self.assertEqual(len(self.posted), 1)

    def test_no_day_read_then_posted_after_it(self) -> None:
        c = self.make({"personal": [], "work": []}, start=at(10, 12))
        self.act = "off"
        c.on_announced(AM_KEY)                         # not on screen: nothing read, nothing posted
        self.assertEqual((self.posted, self.sources.reads), ([], []))
        self.act = "visible"
        c.sync()                                       # shown: the day is read, then judged again
        self.assertEqual(len(self.posted), 1)
        c2 = self.make({"personal": [], "work": []}, start=at(10, 12))
        c2.on_announced(AM_KEY)                        # on screen, no schedule yet: it asks for the read
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(len(self.sources.reads), 2)

    def test_no_day_read_for_the_plan_offer_while_minimized(self) -> None:
        """4.5: the candidate asks for the day only while the window is visible."""
        c = self.make({"personal": [], "work": []}, start=at(10, 12))
        self.act = "minimized"
        c.on_announced(AM_KEY)
        self.tick(11)
        self.tick(11)
        self.assertEqual((self.posted, self.sources.reads), ([], []))
        self.act = "visible"
        c.sync()
        self.assertEqual(len(self.posted), 1)

    def test_final_reasons_and_runstate(self) -> None:
        class State:
            def __init__(self) -> None:
                self.offered, self.done = set(), set()

            def plan_offered(self, key):
                return key in self.offered

            def planned(self, key):
                return key in self.done

            def record_plan_offered(self, key):
                self.offered.add(key)

            def record_planned(self, key):
                self.done.add(key)

            def is_announced(self, key):
                return True

            def is_viewed(self, key):
                return False

        state = State()
        c = self.plan(run_state=state)
        c.on_announced(AM_KEY)
        self.assertEqual(state.offered, {AM_KEY})
        again = self.plan(run_state=state)             # a restart: not twice
        again.on_announced(AM_KEY)
        self.assertEqual(self.posted, [])
        state.offered.clear()
        state.done.add(AM_KEY)                         # an evening "plan tomorrow" (R29)
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.plan(run_state=state).on_announced(AM_KEY)
        self.assertEqual(self.posted, [])
        self.assertIn(f"INFO:briefing_reader.productivity_ui:Plan offer: skipped for {AM_KEY} (planned)", logs.output)

    def test_nothing_needs_time_or_a_full_day_is_skipped(self) -> None:
        c = self.plan(todos=0)
        c.on_announced(AM_KEY)
        self.assertEqual(self.posted, [])
        full = [meeting(f"evt1{n}", start=at(10, 30) + timedelta(hours=n), minutes=60, guests=(BEN,))
                for n in range(12)]
        c2 = self.make({"personal": full, "work": []}, start=at(10, 12))
        c2.sync()
        with self.assertLogs(LOGGER, level="INFO") as logs:
            c2.on_announced(AM_KEY)
        self.assertEqual(self.posted, [])
        self.assertIn(f"INFO:briefing_reader.productivity_ui:Plan offer: skipped for {AM_KEY} (busy_day)",
                      logs.output)

    def test_planned_retires_and_expiry_retires(self) -> None:
        c = self.plan()
        c.on_announced(AM_KEY)
        offer, = self.posted
        c.planned(AM_KEY)
        self.assertEqual(self.retired, [(offer, "planned")])
        c2 = self.plan()
        c2.on_announced(AM_KEY)
        offer2, = self.posted
        self.clock.at = at(13, 29)
        c2.tick()
        self.assertEqual(self.retired, [])
        self.clock.at = at(13, 30)
        c2.tick()
        self.assertEqual(self.retired, [(offer2, "expired")])
        self.assertIsNone(c2.offer(offer2.id))

    def test_minimized_holds_the_plan_sentence_until_restore(self) -> None:
        c = self.plan()
        self.act = "minimized"
        c.sync()
        c.on_announced(AM_KEY)
        offer, = self.posted
        c.on_playback_finished(AM_KEY)
        self.assertEqual((offer.speech_state, offer.held_reason), (HELD, "minimized"))
        self.act = "visible"
        c.sync()
        self.assertEqual(self.spoken, [offer])

    def test_muted_is_text_only_and_never_retried(self) -> None:
        """R37: mute, post, unmute 10 minutes later: no speech."""
        c = self.plan()
        self.facts = QuietFacts(speak_ok=False)
        c.on_announced(AM_KEY)
        c.on_playback_finished(AM_KEY)
        offer, = self.posted
        self.assertEqual((offer.speech_state, offer.held_reason), (TEXT_ONLY, "muted"))
        self.facts = QuietFacts()
        self.tick(10)
        self.assertEqual(self.spoken, [])

    def test_muting_a_queued_or_held_sentence_makes_it_text_only_for_good(self) -> None:
        """R37: Ctrl+M while the sentence is queued (or still held) settles it as text only: an unmute
        before the next tick never brings a late "Shall I ...?"."""
        c = self.plan()
        c.on_announced(AM_KEY)
        offer, = self.posted
        c.on_viewed(AM_KEY)
        c.on_tab_changed(hud.TAB_JARVIS)
        self.assertEqual((self.spoken, offer.speech_state), ([offer], QUEUED))
        self.facts = QuietFacts(speak_ok=False)
        with self.assertLogs(LOGGER, level="INFO") as logs:
            c.on_muted()                                   # the voice dropped its queue with the mute
        self.assertEqual((offer.speech_state, offer.held_reason), (TEXT_ONLY, "muted"))
        self.assertIn("INFO:briefing_reader.productivity_ui:Plan offer: text only (muted)", logs.output)
        self.clock.advance(0.5)
        self.facts = QuietFacts()                          # unmuted within the minute
        self.tick(1)
        self.tick(10)
        self.assertEqual(self.spoken, [offer])             # said once, never again
        made = [step for step in self.own_steps() if step.kind == live.OFFER_MADE]
        self.assertEqual({field.label: field.value for field in made[-1].fields}["Spoken"], "text only (muted)")
        c2 = self.plan()                                   # held (not heard yet) when muted
        c2.on_announced(AM_KEY)
        held = self.posted[0]
        self.assertEqual(held.speech_state, HELD)
        c2.on_muted()
        c2.on_playback_finished(AM_KEY)
        self.tick(25)
        self.assertEqual((self.spoken, held.speech_state), ([], TEXT_ONLY))

    def test_a_held_sentence_is_said_only_within_two_hours_of_posting(self) -> None:
        """R26: heard at 12:20 PM for an offer posted at 10:12 AM: too late to say it (the entry stays)."""
        c = self.plan()
        c.on_announced(AM_KEY)
        offer, = self.posted
        self.clock.at = at(12, 20)
        c.on_viewed(AM_KEY)
        c.on_tab_changed(hud.TAB_JARVIS)
        self.tick(1)
        self.assertEqual((self.spoken, offer.speech_state), ([], HELD))
        self.assertIs(c.offer(offer.id), offer)            # the link still works until 13:30

    def test_a_stale_cached_check_is_refreshed_before_the_plan_offer(self) -> None:
        """R13: the plan offer, like a prep offer, waits for a fresh CapCheck (one refresh)."""
        c = self.plan()
        self.ask = (True, True, 400.0)
        c.on_announced(AM_KEY)
        self.assertEqual((self.posted, self.refreshes), ([], 1))
        pending = [step for step in self.own_steps() if step.kind == live.OFFER_MADE]
        self.assertEqual(pending[-1].title, "Not offered yet: plan my day (caps checked again)")
        self.ask = (True, True, 1.0)
        self.tick(1)
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(self.refreshes, 1)

    def test_a_partial_day_read_keeps_busy_day_transient(self) -> None:
        """R36: busy_day after a read that missed a calendar is retried; a complete read makes it final."""
        full = [meeting(f"evt1{n}", start=at(10, 30) + timedelta(hours=n), minutes=60, guests=(BEN,))
                for n in range(12)]
        c = self.make({"personal": full, "work": []}, start=at(10, 12))
        self.sources.statuses["work"] = "no"
        c.sync()
        with self.assertLogs(LOGGER, level="INFO") as logs:
            logging.getLogger(LOGGER).info("zq7 mark")     # the block may log nothing else
            c.on_announced(AM_KEY)
            self.tick(1)
        self.assertNotIn("skipped", "\n".join(logs.output))
        pending = [step for step in self.own_steps() if step.kind == live.OFFER_MADE]
        self.assertEqual(pending[-1].title, "Not offered yet: plan my day (busy day)")
        self.assertTrue(c._plan_candidate)
        self.sources.statuses.clear()
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.tick(10)                                  # the next day read reads every calendar
        self.assertIn(f"INFO:briefing_reader.productivity_ui:Plan offer: skipped for {AM_KEY} (busy_day)",
                      logs.output)
        self.assertFalse(c._plan_candidate)
        self.assertEqual([offer for offer in self.posted if offer.kind == "plan"], [])

    def test_the_sub_line_follows_the_cached_caps(self) -> None:
        """R49: each tick re-validates the posted offer's cost line from the cached CapCheck."""
        c = self.plan()
        c.on_announced(AM_KEY)
        offer, = self.posted
        self.tick(1)
        self.assertEqual(self.changed, [])                 # nothing changed: nothing re-shown
        self.caps = CapCheck(True, 20, 60, left_bundle_day=16)
        self.tick(1)
        self.assertEqual(self.changed, [offer])
        self.assertEqual(offer.sub, f"1 planner run {DOT} 16 of 20 left today")
        self.caps = CapCheck(True, 20, 12, left_bundle_day=16)
        self.tick(1)
        self.assertEqual(self.changed, [offer, offer])
        self.assertEqual(offer.sub, f"1 planner run {DOT} 12 Ask runs left today")

    def test_with_prep_offers_off_the_day_is_read_only_while_the_plan_offer_needs_it(self) -> None:
        """Read the least: with prep offers off, the schedule is read for the plan offer candidate
        only; once the plan offer is posted (or settled) nothing more is read."""
        c = self.make({"personal": [meeting("evt0003", start=at(11), guests=(BEN,))], "work": []},
                      start=at(10, 12), prep_offers=False)
        c.sync()
        self.assertEqual(self.sources.reads, [])           # no candidate yet: nothing to read for
        c.on_announced(AM_KEY)                             # the candidate asks for today's schedule
        self.assertEqual(len(self.sources.reads), 2)
        self.assertEqual(len(self.posted), 1)
        for _ in range(30):
            self.tick(1)
        self.assertEqual(len(self.sources.reads), 2)
        c2 = self.make({"personal": [], "work": []}, start=at(10, 12), prep_offers=False, todos=0)
        c2.sync()
        c2.on_announced(AM_KEY)                            # nothing needs time: final after a full read
        self.assertEqual((self.posted, len(self.sources.reads)), ([], 2))
        for _ in range(30):
            self.tick(1)
        self.assertEqual(len(self.sources.reads), 2)


class MinimizedCheckTests(ControllerCase):
    """R35 with R51: what the hour read re-checked, and only that, is offered while minimized."""

    def hour_reads(self) -> list:
        return [read for read in self.sources.reads if read[2] - read[1] == timedelta(hours=2)]

    def minimized_from(self, start: datetime, briefs: dict) -> ProductivityController:
        c = self.make(briefs, start=start)
        c.sync()                                           # the last visible read
        self.act = "minimized"
        c.sync()
        return c

    def test_a_back_to_back_member_cancelled_meanwhile_is_not_offered(self) -> None:
        a = meeting("evt0001", start=at(14))
        b = meeting("evt0002", start=at(14, 30), guests=(BEN,))
        other = meeting("evt0003", start=at(14, 35), guests=(BEN,))   # another calendar: not in that read
        c = self.minimized_from(at(13), {"personal": [other], "work": [a, b]})
        self.sources.briefs["work"] = [a]                  # B cancelled after the last visible read
        self.clock.at = at(13, 46)
        c.tick()
        self.assertEqual(self.hour_reads()[-1][:3], ("work", at(13), at(15)))
        offer, = self.posted
        self.assertEqual((offer.start, offer.group, offer.read_at), (at(14), (), at(13, 46)))
        self.assertEqual([said.speech for said in self.spoken],
                         ["Your meeting at 2 PM starts in about 14 minutes, sir. Shall I prepare a brief?"])
        self.assertEqual([event.event_id for event in c.day_briefs()["work"]], ["evt0001"])
        self.clock.at = at(14, 20)                         # the other calendar's meeting: its own check
        c.tick()
        self.assertEqual(self.hour_reads()[-1][0], "personal")
        self.assertEqual([offer.start for offer in self.posted], [at(14), at(14, 35)])

    def test_an_unreadable_hour_is_read_once_per_meeting(self) -> None:
        later = meeting("evt0002", start=at(16), guests=(BEN,))
        c = self.minimized_from(at(13), {"personal": [], "work": [meeting(), later]})
        self.sources.statuses["work"] = "error"
        self.clock.at = at(13, 44)
        with self.assertLogs(LOGGER, level="INFO") as logs:
            for _ in range(10):
                self.tick(1)                               # 1:45 to 1:54 PM
        self.assertEqual(len(self.hour_reads()), 1)
        self.assertEqual(logs.output.count("INFO:briefing_reader.productivity_ui:Prep offer: skipped (unreadable)"),
                         1)
        self.assertEqual(self.posted, [])
        made = [step for step in self.own_steps() if step.kind == live.OFFER_MADE]
        self.assertEqual(len(made), 1)
        self.sources.statuses.clear()
        self.clock.at = at(15, 46)
        c.tick()
        self.assertEqual(len(self.hour_reads()), 2)        # the later meeting gets its own check
        self.assertEqual([offer.start for offer in self.posted], [at(16)])

    def test_a_failed_hour_read_is_not_retried_while_minimized(self) -> None:
        c = self.minimized_from(at(13), {"personal": [], "work": [meeting()]})
        self.sources.error = ValueError("zq7subj Budget")
        self.clock.at = at(13, 44)
        with self.assertLogs(LOGGER, level="INFO") as logs:
            for _ in range(5):
                self.tick(1)
        self.assertEqual(len(self.hour_reads()), 1)
        self.assertEqual(self.failed, ["day"])
        self.assertIn("INFO:briefing_reader.productivity_ui:Prep offer: skipped (unreadable)", logs.output)
        self.sources.error = None
        self.act = "visible"
        c.sync()                                           # back on screen: a fresh day read, then offered
        offer, = self.posted
        self.assertEqual(offer.read_at, at(13, 49))

    def test_an_hour_read_that_lands_after_the_window_left_posts_nothing(self) -> None:
        """4.1: hidden WAITING / the prompt: nothing posted or said, even from a read on its way."""
        c = self.minimized_from(at(13), {"personal": [], "work": [meeting()]})

        def gone() -> None:
            self.act = "off"                               # e.g. a scheduled launch went back to the prompt

        self.sources.during_read = gone
        self.clock.at = at(13, 46)
        c.tick()
        self.assertEqual(len(self.hour_reads()), 1)
        self.assertEqual((self.posted, self.spoken), ([], []))
        self.sources.during_read = None
        self.act = "visible"
        c.sync()                                           # the reading screen is back: judged again
        offer, = self.posted
        self.assertEqual(self.spoken, [offer])

    def test_a_checked_hour_is_not_read_again_while_the_offer_waits(self) -> None:
        """One hour read per candidate: a transient reason after the read (Claude Code reported not
        ready while the read was on its way) waits on the re-checked event, which is posted later as
        of that read."""
        c = self.minimized_from(at(13), {"personal": [], "work": [meeting()]})

        def not_ready() -> None:
            self.ask = (False, True, 1.0)

        self.sources.during_read = not_ready
        self.clock.at = at(13, 45)
        c.tick()                                           # the hour read; then not ready: not posted
        self.sources.during_read = None
        self.tick(1)
        self.assertEqual(self.posted, [])
        self.ask = (True, True, 1.0)
        self.tick(1)                                       # 1:47 PM: ready again
        self.assertEqual(len(self.hour_reads()), 1)
        offer, = self.posted
        self.assertEqual((offer.read_at, offer.stale, offer.flash), (at(13, 45), False, True))
        self.assertTrue(offer.sub.endswith("as of 1:45 PM"))

    def test_a_waiting_rechecked_offer_groups_only_rechecked_members(self) -> None:
        """R35 with R51: posted at a later tick, the group takes only meetings an hour read re-checked;
        a back-to-back meeting on another calendar nothing read again is left out."""
        a = meeting("evt0001", start=at(14))
        other = meeting("evt0002", start=at(14, 10), guests=(BEN,))   # another calendar, back to back
        c = self.minimized_from(at(13), {"personal": [other], "work": [a]})

        def not_ready() -> None:
            self.ask = (False, True, 1.0)

        self.sources.during_read = not_ready
        self.clock.at = at(13, 45)
        c.tick()                                           # the 2 PM hour read; then not ready
        self.sources.during_read = None
        self.ask = (True, True, 1.0)
        self.tick(1)                                       # 1:46 PM
        self.assertEqual([offer.start for offer in self.posted], [at(14)])
        self.assertEqual(len(self.hour_reads()), 1)

    def test_each_member_of_a_rechecked_group_is_as_of_its_own_read(self) -> None:
        """Back-to-back meetings on two calendars, each re-checked by its own hour read: each entry's sub
        line says when its own meeting was read."""
        first = meeting("evt0001", start=at(13, 35), minutes=10)   # its hour read covers the 2:10 PM one
        a = meeting("evt0002", start=at(14))
        other = meeting("evt0003", start=at(14, 10), guests=(BEN,))   # back to back with 2 PM
        c = self.minimized_from(at(12, 40), {"personal": [first, other], "work": [a]})
        self.clock.at = at(13, 20)
        c.tick()                                           # the personal hour read (12:35-2:35 PM)

        def not_ready() -> None:
            self.ask = (False, True, 1.0)

        self.sources.during_read = not_ready
        self.clock.at = at(13, 45)
        c.tick()                                           # the work hour read; then not ready
        self.sources.during_read = None
        self.ask = (True, True, 1.0)
        self.tick(1)                                       # 1:46 PM: 2 PM and 2:10 PM together
        _first, two, ten = self.posted
        self.assertEqual((two.start, ten.start, two.group), (at(14), at(14, 10), (two.id, ten.id)))
        self.assertEqual((two.read_at, ten.read_at), (at(13, 45), at(13, 20)))
        self.assertTrue(two.sub.endswith("as of 1:45 PM"))
        self.assertTrue(ten.sub.endswith("as of 1:20 PM"))
        self.assertEqual(len(self.hour_reads()), 2)

    def test_a_group_after_an_hour_read_holds_only_the_offers_left_today(self) -> None:
        """R35 with R51 and prep_offers_per_day: the group the hour read verified counts the earlier offer."""
        early = meeting("evt0001", start=at(12))
        one, two = meeting("evt0002", start=at(14)), meeting("evt0003", start=at(14, 30), guests=(BEN,))
        c = self.make({"personal": [], "work": [early, one, two]}, start=at(11, 40), prep_offers_per_day=2)
        c.sync()
        self.tick(5)                                       # 11:45 AM: 1 of 2, on screen
        self.act = "minimized"
        c.sync()
        self.clock.at = at(13, 45)
        c.tick()                                           # an old schedule: the hour read, then 1 is left
        self.assertEqual(len(self.hour_reads()), 1)
        self.assertEqual([offer.start for offer in self.posted], [at(12), at(14)])
        self.assertEqual(self.posted[1].group, ())

    def test_an_hour_read_without_a_meeting_takes_back_its_queued_sentence(self) -> None:
        """The hour read replaces its hours of the schedule (R35): an open offer whose meeting it no longer
        has is not said, and a sentence still queued is taken back. (Driven directly: the 30 minutes an
        hour read counts as fresh keep a second one away from a meeting whose sentence still waits.)"""
        a = meeting("evt0001", start=at(14))
        later = meeting("evt0002", start=at(14, 50), guests=(BEN,))   # not back to back with 2 PM
        c = self.minimized_from(at(13, 40), {"personal": [], "work": [a, later]})
        unsaid = []
        c.offerUnsaid.connect(unsaid.append)
        self.tick(5)                                       # 1:45 PM: from the 1:40 PM read, queued
        offer, = self.posted
        self.assertEqual(offer.speech_state, QUEUED)
        self.sources.briefs["work"] = [later]              # 2 PM cancelled
        c._request_hour("work", later, offer_key("work", later))   # 1:50-3:50 PM: has no 2 PM meeting
        self.assertEqual((unsaid, offer.speech_state, offer.held_reason), ([offer], TEXT_ONLY, "gone"))
        self.tick(1)
        self.assertEqual(self.spoken, [offer])             # never again

    def test_the_hour_reads_stop_at_the_days_offers(self) -> None:
        """R35: at most prep_offers_per_day minimized checks a day ("checks used")."""
        gone = meeting("evt0001", start=at(14))
        later = meeting("evt0002", start=at(15, 30), guests=(BEN,))
        c = self.make({"personal": [], "work": [gone, later]}, start=at(13), prep_offers_per_day=1)
        c.sync()
        self.act = "minimized"
        c.sync()
        self.sources.briefs["work"] = [later]              # cancelled: its check finds it gone
        self.clock.at = at(13, 46)
        c.tick()
        self.clock.at = at(15, 16)
        c.tick()
        self.assertEqual(len(self.hour_reads()), 1)
        self.assertEqual(self.posted, [])
        made = [step for step in self.own_steps() if step.kind == live.OFFER_MADE]
        self.assertEqual(made[-1].title, "Not offered yet: a prep brief (checks used)")


class ReadsLineTests(ControllerCase):
    """The READS task says only what holds: a "not ready" line only for a meeting that would be offered
    now, settled when it passes, and each member of a group says where it went."""

    def titles(self) -> list[str]:
        return [step.title for step in self.own_steps() if step.kind == live.OFFER_MADE]

    def test_not_ready_lines_and_a_group_member(self) -> None:
        first = meeting("evt0001", start=at(14))
        member = meeting("evt0002", start=at(14, 10), guests=(BEN,))
        past = meeting("evt0007", start=at(9))
        holiday = EventBrief("evt0009", "primary", "Zq7 Holiday", all_day_start=DAY, all_day_end=DAY, guests=(ANA,))
        c = self.make({"personal": [first], "work": [member, past, holiday]}, start=at(13, 30))
        self.ask = (False, True, 1.0)
        c.sync()
        self.assertEqual(self.titles(), [])                # nothing due: no "not ready" for any of them
        self.tick(25)                                      # 1:55 PM: both due, Claude Code not ready
        self.assertEqual(self.titles(), ["Not offered yet: a prep brief (not ready)"] * 2)
        self.ask = (True, True, 1.0)
        self.tick(1)
        self.assertEqual([offer.start for offer in self.posted], [at(14), at(14, 10)])
        self.assertEqual(self.titles(), ["Offered together: 2:00 PM and 2:10 PM (from the read at 1:55 PM)",
                                          "Offered together with 2:00 PM: 2:10 PM"])

    def test_a_not_ready_line_is_settled_when_the_meeting_passes(self) -> None:
        c = self.make(start=at(13, 30))
        self.ask = (False, True, 1.0)
        c.sync()
        self.tick(15)
        self.assertEqual(self.titles(), ["Not offered yet: a prep brief (not ready)"])
        self.tick(13)                                      # 1:58 PM: too close to its start
        self.assertEqual(self.titles(), ["Not offered: a prep brief (too close to its start)"])
        self.tick(1)
        self.assertEqual(len(self.titles()), 1)


class HeldPrepSpeechTests(ControllerCase):
    """4.2: a held prep offer is said only while at least 3 minutes remain before its start."""

    def test_an_unlock_two_minutes_before_the_start_says_nothing(self) -> None:
        c = self.make()
        c.sync()
        self.facts = QuietFacts(locked=True)
        self.tick(15)
        offer, = self.posted
        self.assertEqual((offer.speech_state, offer.held_reason), (HELD, "locked"))
        self.clock.at = at(13, 58)
        self.facts = QuietFacts()
        c.tick()                                           # the unlock is seen here
        self.assertEqual((self.spoken, offer.speech_state), ([], HELD))
        self.tick(1)
        self.assertEqual(self.spoken, [])

    def test_a_restore_two_minutes_before_the_start_says_nothing(self) -> None:
        c = self.make(prep_speak_minimized=False)
        c.sync()
        self.act = "minimized"
        self.tick(15)
        offer, = self.posted
        self.assertEqual((offer.speech_state, offer.held_reason), (HELD, "minimized"))
        self.clock.at = at(13, 58)
        self.act = "visible"
        c.sync()
        self.assertEqual((self.spoken, offer.speech_state), ([], HELD))


class RecheckTests(ControllerCase):
    """R35 and 4.4: a sentence not said yet is never said for a meeting a later read no longer has at
    that time, nor from a schedule older than 30 minutes; nothing is offered from such a schedule."""

    def spoken_field(self) -> str:
        made, = [step for step in self.own_steps() if step.kind == live.OFFER_MADE]
        return {field.label: field.value for field in made.fields}["Spoken"]

    def test_a_held_sentence_for_a_meeting_cancelled_since_is_never_said(self) -> None:
        c = self.make(start=at(13, 40))
        c.sync()                                           # read at 1:40 PM
        self.facts = QuietFacts(playing=True)
        self.tick(5)                                       # 1:45 PM: offered, held (the briefing plays)
        offer, = self.posted
        self.assertEqual((offer.speech_state, offer.held_reason), (HELD, "playing"))
        self.sources.briefs["work"] = []                   # cancelled
        with self.assertLogs(LOGGER, level="INFO") as logs:
            self.tick(5)                                   # 1:50 PM: the day read no longer has it
        self.assertEqual((offer.speech_state, offer.held_reason), (TEXT_ONLY, "gone"))
        self.assertIn("INFO:briefing_reader.productivity_ui:Prep offer: text only (gone)", logs.output)
        self.assertEqual(self.spoken_field(), "text only (moved or cancelled)")
        self.facts = QuietFacts()
        self.tick(2)                                       # 1:52 PM: quiet
        self.assertEqual(self.spoken, [])
        self.assertIs(c.offer(offer.id), offer)            # the entry keeps its link: a click reads again

    def test_a_queued_sentence_for_a_moved_meeting_is_taken_back_and_the_new_time_offered(self) -> None:
        c = self.make(start=at(13, 30))
        unsaid = []
        c.offerUnsaid.connect(unsaid.append)
        c.sync()
        self.tick(15)                                      # 1:45 PM: queued; the voice has not started it
        offer, = self.posted
        self.assertEqual(offer.speech_state, QUEUED)
        self.sources.briefs["work"] = [meeting(start=at(14, 30))]   # the same meeting, moved to 2:30 PM
        self.clock.advance(0.5)
        c.request_day("tick")
        self.assertEqual((unsaid, offer.speech_state, offer.held_reason), ([offer], TEXT_ONLY, "moved"))
        self.clock.at = at(14, 15)
        c.tick()                                           # offered again at the new time's lead
        self.assertEqual([posted.start for posted in self.posted], [at(14), at(14, 30)])
        self.assertEqual(self.spoken, [offer, self.posted[1]])

    def test_a_groups_one_sentence_goes_with_a_cancelled_member(self) -> None:
        one = meeting("evt0001", start=at(13), minutes=60)
        two = meeting("evt0002", start=at(14), minutes=60, guests=(BEN,))
        c = self.make(briefs={"personal": [two], "work": [one]}, start=at(12, 30))
        c.sync()
        self.facts = QuietFacts(playing=True)
        self.tick(15)                                      # 12:45 PM: both offered, their one sentence held
        first, second = self.posted
        self.assertEqual((first.speech_state, first.held_reason), (HELD, "playing"))
        self.sources.briefs["personal"] = []               # the 2 PM meeting cancelled
        self.tick(10)                                      # 12:55 PM: the day read no longer has it
        self.assertEqual((first.speech_state, first.held_reason), (TEXT_ONLY, "gone"))
        self.facts = QuietFacts()
        self.tick(1)
        self.assertEqual(self.spoken, [])

    def test_minimized_a_held_sentence_from_a_schedule_older_than_30_minutes_is_text_only(self) -> None:
        c = self.make(start=at(12, 59), prep_lead_minutes=60)
        c.sync()                                           # read at 12:59 PM
        self.facts = QuietFacts(busy=True)
        self.tick(1)                                       # 1:00 PM: offered on screen, held (busy)
        offer, = self.posted
        self.act = "minimized"
        c.sync()
        self.sources.briefs["work"] = []                   # cancelled; nothing reads it again while minimized
        for _ in range(49):
            self.tick(1)                                   # busy until 1:49 PM
        self.facts = QuietFacts()
        self.tick(1)                                       # 1:50 PM: quiet, still minimized
        self.assertEqual((self.spoken, offer.speech_state, offer.held_reason), ([], TEXT_ONLY, "stale"))
        self.assertEqual(len(self.sources.reads), 2)       # no read while minimized for an offer made already

    def restored(self, *, cancelled: bool) -> tuple[ProductivityController, list]:
        """Offered minimized at 1:00 PM from a 10-minute-old schedule (said only on screen), restored at
        1:40 PM; the read the restore asks for is on its way (the prod thread)."""
        c = self.make(start=at(12, 50), prep_lead_minutes=60, prep_speak_minimized=False)
        c.sync()                                           # read at 12:50 PM
        self.act = "minimized"
        c.sync()
        self.tick(10)                                      # 1:00 PM
        offer, = self.posted
        self.assertEqual((offer.speech_state, offer.held_reason), (HELD, "minimized"))
        self.clock.at = at(13, 40)
        c.tick()
        self.assertEqual(offer.speech_state, HELD)         # never stale while it could not be said minimized
        if cancelled:
            self.sources.briefs["work"] = []
        pending = []
        c._worker.put = pending.append
        self.act = "visible"
        c.sync()                                           # restored: the day is asked for
        self.tick(0.5)                                     # a tick before the read is back
        self.assertEqual((len(pending), self.spoken), (1, []))
        return c, pending

    def test_after_a_restore_a_held_sentence_waits_for_the_fresh_read(self) -> None:
        c, pending = self.restored(cancelled=False)
        offer, = self.posted
        c._worker._run_job(pending[0])                     # the read lands: the meeting is still there
        self.assertEqual(self.spoken, [])
        self.tick(0.5)
        self.assertEqual(self.spoken, [offer])

    def test_after_a_restore_a_held_sentence_for_a_cancelled_meeting_is_never_said(self) -> None:
        c, pending = self.restored(cancelled=True)
        offer, = self.posted
        c._worker._run_job(pending[0])                     # the read lands: no such meeting
        self.tick(1)
        self.assertEqual((self.spoken, offer.speech_state, offer.held_reason), ([], TEXT_ONLY, "gone"))

    def test_after_a_restore_nothing_is_offered_from_the_old_schedule_before_the_read(self) -> None:
        for cancelled in (True, False):
            c = self.make(start=at(10, 30))
            c.sync()                                       # 10:30 AM: the 2 PM meeting
            self.act = "minimized"
            c.sync()
            if cancelled:
                self.sources.briefs["work"] = []           # cancelled while minimized
            self.clock.at = at(13, 44)
            pending = []
            c._worker.put = pending.append                 # the read runs on the prod thread: not back yet
            self.act = "visible"
            c.sync()                                       # restored at 1:44 PM: the read is asked for
            self.tick(1)                                   # 1:45 PM, before the read is back
            self.assertEqual((len(pending), self.posted, self.spoken), (1, [], []))
            c._worker._run_job(pending[0])                 # the read lands
            self.tick(1)
            self.assertEqual([offer.read_at for offer in self.posted], [] if cancelled else [at(13, 45)])


class SpacingTests(ControllerCase):
    """4.2: never two offers' sentences within 5 minutes. A sentence queued but not started yet counts as
    said (the voice starts it a little later); the meeting's sentence goes first (it cannot wait)."""

    def due_together(self) -> tuple[ProductivityController, object, object]:
        c = self.make({"personal": [meeting(start=at(10))], "work": []}, start=at(9, 30))
        c.sync()
        c.on_announced(AM_KEY)
        c.on_playback_started(AM_KEY)
        self.facts = QuietFacts(playing=True)
        self.tick(15)                                      # 9:45 AM: the prep offer, held (the briefing plays)
        plan, prep = self.posted
        self.assertEqual([(offer.kind, offer.held_reason) for offer in (plan, prep)],
                         [("plan", "not_heard"), ("prep", "playing")])
        self.facts = QuietFacts()
        self.clock.advance(5)                              # 9:50 AM
        c.on_playback_finished(AM_KEY)                     # both may be said now
        self.assertEqual(self.spoken, [prep])
        self.assertEqual((plan.speech_state, plan.held_reason), (HELD, "spacing"))
        return c, plan, prep

    def test_two_sentences_due_together_are_said_five_minutes_apart(self) -> None:
        c, plan, prep = self.due_together()
        c.on_speaking(prep.speech)                         # the voice starts it at 9:50 AM
        for _ in range(4):
            self.tick(1)                                   # 9:51-9:54 AM
        self.assertEqual(self.spoken, [prep])
        self.tick(1)                                       # 9:55 AM
        self.assertEqual(self.spoken, [prep, plan])

    def test_a_sentence_taken_back_before_it_started_does_not_hold_the_other(self) -> None:
        c, plan, prep = self.due_together()
        c.use_offer(prep.id)                               # its link clicked before the voice started it
        self.tick(1)                                       # 9:51 AM
        self.assertEqual(self.spoken, [prep, plan])


class TakeBackTests(ControllerCase):
    """4.2: a sentence queued but not started is taken from the voice where it may not be said."""

    def test_a_queued_sentence_is_taken_back_when_the_session_locks(self) -> None:
        c = self.make()
        unsaid = []
        c.offerUnsaid.connect(unsaid.append)
        c.sync()
        self.tick(15)                                      # 1:45 PM: queued, not started yet
        offer, = self.posted
        self.facts = QuietFacts(locked=True)
        self.tick(0.5)                                     # the lock is seen at a tick (R18)
        self.assertEqual((unsaid, offer.speech_state, offer.held_reason), ([offer], HELD, "locked"))
        self.facts = QuietFacts()
        self.tick(1)                                       # unlocked: said now
        self.assertEqual((self.spoken, offer.spoken_how), ([offer, offer], "unlock"))

    def test_minimized_only_what_may_be_said_minimized_stays_queued(self) -> None:
        for speak_minimized in (True, False):
            c = self.make(prep_speak_minimized=speak_minimized)
            unsaid = []
            c.offerUnsaid.connect(unsaid.append)
            c.sync()
            self.tick(15)                                  # 1:45 PM: queued on screen
            offer, = self.posted
            self.act = "minimized"
            c.sync()
            self.assertEqual(unsaid, [] if speak_minimized else [offer])
            self.assertEqual(offer.speech_state, QUEUED if speak_minimized else HELD)


class ProdThreadOfferTests(ControllerCase):
    def test_offers_from_the_prod_thread_never_call_google_on_the_gui_thread(self) -> None:
        """R16 with the real prod thread: the plan offer and a prep offer are judged and posted on the
        GUI thread from the zone the day read brought; time_zone() refuses the GUI thread."""
        c = self.make(inline=False, start=at(10, 12))
        changed = []
        c.dayChanged.connect(lambda: changed.append(1))
        c.sync()
        self.assertTrue(wait_for(lambda: changed), "the day read arrived")
        c.on_announced(AM_KEY)
        self.assertTrue(wait_for(lambda: self.posted), "the plan offer")
        self.clock.at = at(13, 45)
        c.tick()
        self.assertTrue(wait_for(lambda: len(self.posted) == 2 and len(changed) == 2), "the prep offer")
        plan, prep = self.posted
        self.assertEqual((plan.kind, prep.kind), ("plan", "prep"))
        self.assertEqual(set(self.sources.zone_threads), {"prod"})
        self.assertEqual({thread for *_rest, thread in self.sources.reads}, {"prod"})


if __name__ == "__main__":
    unittest.main()
