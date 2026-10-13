"""The productivity bundle's offers in the app (AppController + JARVIS + LIVE, a steppable clock and
a recording voice; R24).

After the AM announcement the plan offer is posted with its cost line (R49) and spoken only once
the briefing was heard or viewed, counted as spoken only when the voice starts it (R26); a click
plans as a click-started run (the bar's text stays, R23) and the offer retires; it expires at 13:30.
A prep offer comes 15 minutes before a meeting with guests, with a time-only sentence, a taskbar
flash when minimized (R35), back-to-back meetings together (R51); its click gives the brief and the
entry then reads "Brief ready below" (R50). A click while busy is refused and the link stays. The
own-reads LIVE task shows every read and decision with [live] background off (R6). The Done hint
once, also across restarts, and the Done tooltip (R34). A link clicked after its offer expired (before
the tick) says so at once and reads nothing; Ctrl+M makes a waiting sentence text only for good (R37);
the polled lock holds a sentence until the unlock (R18); a repeating meeting ignored 3 times is not
offered and a click or a typed prep resets it, in prep_series.json (R38); the cost line follows the
limits (R49); AppController.shutdown stops the bundle. A sentence queued but not started is taken from
the voice when the window hides, the prompt replaces the reading screen, or the window is minimized
where it may not be said there (said after the restore). Nothing is read from mail, run or sent before
a click; titles never reach the voice or the log. Qt runs offscreen; fakes only; every name is invented.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from PySide6.QtWidgets import QApplication

from briefing_reader import ask_ui, hud, live, persona, ui
from briefing_reader.ask.modes import MODE_PLAN
from briefing_reader.gcal import EventBrief, Guest
from briefing_reader.player import FINISHED
from briefing_reader.prepseries import SERIES_FILE, series_key
from briefing_reader.runstate import RunState
from tests.ask_fakes import FakeReader, GatedProcess, plan_stream
from tests.ui_fakes import GUEST, PDT, AppHarness, HarnessClock, fonts, settle, wait_for

_app: QApplication | None = None
DOT = chr(0xB7)
AM_KEY = "2026-10-04 AM"                     # the fixture briefing's slot (Updated: 2026-10-04 10:04 PDT)
TITLE = "Zq7 Sync"
SAM = Guest(GUEST, "Sam Example", "accepted")
LEE = Guest("zq7lee@example.org", "Zq7 Lee", "accepted")
BLOCK = "Calendar: Work on: Read chapter 4 | 2026-10-04 11:00-12:00 | | | For: t1"
LIVE_OFF = "\n[live]\nbackground = false\n"


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_offers_app", "-platform", "offscreen"])
    fonts()


def at(hour: int, minute: int = 0, *, days: int = 0) -> datetime:
    return datetime(2026, 10, 4, hour, minute, tzinfo=PDT) + timedelta(days=days)


def meeting(event_id: str = "evt0001", start: datetime | None = None, minutes: int = 30, *, title: str = TITLE,
            guests: tuple[Guest, ...] = (SAM,), series_id: str = "") -> EventBrief:
    start = start or at(14)
    return EventBrief(event_id, "primary", title, start=start, end=start + timedelta(minutes=minutes),
                      organizer_self=True, organizer_email="me@example.edu", guests=guests, self_response="accepted",
                      series_id=series_id)


class OffersAppCase(unittest.TestCase):
    def make(self, start: datetime, *runs, work: list[EventBrief] | None = None, personal: list | None = None,
             config_extra: str = "", run_state=None, prod_extra: str = "") -> AppHarness:
        self.reader = FakeReader("work", {})
        app = AppHarness(self, runs=runs, readers={"work": self.reader}, productivity=True, prod_extra=prod_extra,
                         config_extra=config_extra, clock=HarnessClock(start), run_state=run_state)
        app.calendars["work"].briefs = list(work if work is not None else [])
        app.calendars["personal"].briefs = list(personal or [])
        app.reading(autoplay=False)
        app.c.ask.start()
        self.assertTrue(wait_for(lambda: app.c.ask.ready is not None and app.c.ask.caps is not None),
                        "readiness checked")
        self.app, self.c, self.reading = app, app.c, app.c.window.reading
        self.assertIsNotNone(self.c.productivity)
        return app

    def tick(self, minutes: float = 1) -> None:
        self.app.clock.advance(minutes)
        self.c.productivity.tick()
        settle()

    def offer_entries(self) -> list[hud._ConversationEntry]:
        return [entry for entry in self.reading.conversation.entry_widgets()
                if entry.link()[1].startswith(ui.OFFER_LINK)]

    def notices(self) -> list[tuple[str, str, str]]:
        return [said for said in self.app.voice.said if said[0] == "notice"]

    def own_task(self):
        tasks = [task for task in self.c.live.snapshot() if task.kind == live.TASK_OWN_READS]
        return tasks[0] if tasks else None

    def offer_lines(self) -> list[str]:
        task = self.own_task()
        return [step.title for step in task.steps if step.kind == live.OFFER_MADE] if task is not None else []


class PlanOfferAppTests(OffersAppCase):
    def test_announced_offered_heard_spoken_clicked_planned(self) -> None:
        app = self.make(at(10, 12), plan_stream({"say": "Chapter 4 this morning.", "question": "",
                                                 "lines": [BLOCK]}), config_extra=LIVE_OFF)
        c = self.c
        with self.assertLogs("briefing_reader", level="DEBUG") as logs:
            self.assertTrue(c._announce())                 # "Sir, your AM briefing is ready to view."
            settle()
        entry, = self.offer_entries()
        self.assertEqual(entry.entry[2], persona.plan_offer_text("sir"))
        self.assertEqual(entry.link()[0], "Plan my day")
        self.assertEqual(entry.entry[3], f"1 planner run {DOT} 20 of 20 left today")
        self.assertEqual(self.notices(), [])               # not heard yet: the announcement alone (R26)
        self.tick(5)
        self.assertEqual(self.notices(), [])
        c._mark_viewed(AM_KEY)                             # viewed ...
        self.reading.set_tab(hud.TAB_BRIEFING)
        self.reading.set_tab(hud.TAB_JARVIS)               # ... and then JARVIS: heard
        self.assertEqual(self.notices(), [("notice", "Shall I plan your day, sir?", f"{ui.OFFER_LINK}1")])
        offer = c.productivity.plan_offer_open()
        self.assertEqual(offer.speech_state, "queued")
        c._on_voice_speaking("Shall I plan your day, sir?")   # the voice started it
        self.assertEqual(offer.speech_state, "spoken")
        bar = self.reading.command_bar
        bar.set_text("a draft of mine")
        entry.link_button.click()                          # the click starts the plan (a click-started run)
        self.assertTrue(wait_for(lambda: not c.ask.busy, 10), "the plan ran")
        settle()
        self.assertEqual(bar.text(), "a draft of mine")    # R23
        self.assertEqual(len(app.factory.runner.started), 1)
        cards = [action for action in c._actions if action.mode == MODE_PLAN and not action.error]
        self.assertEqual(len(cards), 1)
        self.assertEqual(entry.entry[2], "You planned your day at 10:17 AM.")
        self.assertEqual(entry.link(), ("", ""))
        self.assertIn("Plan my day (offered after the AM briefing)",
                      [shown.entry[2] for shown in self.reading.conversation.entry_widgets()])
        steps = [step.kind for step in self.own_task().steps]
        self.assertIn(live.CALENDAR_DAY, steps)            # [live] background = false: still shown (R6)
        self.assertIn(live.OFFER_MADE, steps)
        text = "\n".join(logs.output)
        self.assertIn(f"Plan offer: posted for {AM_KEY} (held:not_heard)", text)

    def test_spoken_only_after_finished_and_play_drops_it(self) -> None:
        self.make(at(10, 12))
        c = self.c
        c._announce()
        settle()
        c.player.state = "playing"
        c._on_player_state("playing")
        self.tick(25)
        self.assertEqual(self.notices(), [])               # playing: held
        c.player.state = FINISHED
        c._on_player_state(FINISHED)
        self.assertEqual(len(self.notices()), 1)           # said after FINISHED
        c.toggle_play()                                    # Play while it is queued: dropped, held again
        offer = c.productivity.plan_offer_open()
        self.assertEqual((offer.speech_state, offer.held_reason), ("held", "dropped"))

    def test_expires_at_half_past_one(self) -> None:
        self.make(at(13, 20))
        self.c._announce()
        settle()
        entry, = self.offer_entries()
        self.tick(10)                                      # 1:30 PM
        self.assertEqual(entry.entry[2], 'Offered at 1:20 PM - expired. Type "plan my day" any time.')
        self.assertEqual(entry.link(), ("", ""))

    def test_the_cost_line_follows_the_limits(self) -> None:
        """R49: a bundle run elsewhere (another plan, a brief) is seen at the next re-validating tick:
        the open offer's sub line says what is left now."""
        app = self.make(at(10, 12))
        self.c._announce()
        settle()
        entry, = self.offer_entries()
        self.assertEqual(entry.entry[3], f"1 planner run {DOT} 20 of 20 left today")
        usage = app.factory.planner.usage
        usage.finish(usage.start(kind="plan_day"), outcome="ok", duration_ms=9000)
        self.c.ask.refresh(full=False)
        self.assertTrue(wait_for(lambda: self.c.ask.caps.left_bundle_day == 19), "the limits checked again")
        self.tick(1)
        self.assertEqual(entry.entry[3], f"1 planner run {DOT} 19 of 20 left today")

    def test_a_click_after_half_past_one_says_it_expired_at_once(self) -> None:
        """The link is still shown until the next tick: a click then retires it at once, with no run."""
        app = self.make(at(13, 20))
        self.c._announce()
        settle()
        entry, = self.offer_entries()
        app.clock.set(at(13, 31))                          # no tick since 13:30
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            entry.link_button.click()
            settle()
        self.assertEqual(entry.entry[2], 'Offered at 1:20 PM - expired. Type "plan my day" any time.')
        self.assertEqual(entry.link(), ("", ""))
        self.assertFalse(self.c.ask.busy)
        self.assertEqual(app.factory.runner.started, [])
        self.assertIn("Offer link: expired", "\n".join(logs.output))

    def test_none_after_half_past_one_none_for_pm_none_twice(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        state = RunState(Path(tmp.name) / "runstate.json", clock=lambda: at(10, 12))
        self.make(at(13, 31), run_state=state)
        self.c._set_announced(AM_KEY, True)
        self.c._set_announced("2026-10-04 PM", True)
        settle()
        self.assertEqual(self.offer_entries(), [])
        self.make(at(10, 12), run_state=state)
        self.c._set_announced(AM_KEY, True)
        settle()
        self.assertEqual(len(self.offer_entries()), 1)
        self.assertTrue(state.plan_offered(AM_KEY))
        self.make(at(10, 40), run_state=state)            # a restart: not twice
        self.c._set_announced(AM_KEY, True)
        settle()
        self.assertEqual(self.offer_entries(), [])

    def test_an_evening_plan_for_tomorrow_means_no_offer_next_morning(self) -> None:
        """R29: "plan tomorrow" at 8 PM with a valid block marks tomorrow's AM slot planned."""
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        state = RunState(Path(tmp.name) / "runstate.json", clock=lambda: at(20, 0, days=-1))
        block = "Calendar: Work on: Read chapter 4 | 2026-10-04 09:00-10:00 | | | For: t1"
        self.make(at(20, 0, days=-1), plan_stream({"say": "", "question": "", "lines": [block]}), run_state=state)
        bar = self.reading.command_bar
        bar.set_text("plan tomorrow")
        bar.input.returnPressed.emit()
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10), "the plan ran")
        settle()
        self.assertTrue(state.planned(AM_KEY))
        self.make(at(10, 12), run_state=state)
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            self.c._set_announced(AM_KEY, True)
            settle()
        self.assertEqual(self.offer_entries(), [])
        self.assertIn(f"Plan offer: skipped for {AM_KEY} (planned)", "\n".join(logs.output))

    def test_minimizing_takes_a_queued_plan_sentence_back_until_the_restore(self) -> None:
        """4.2: the plan offer is never said to a minimized window; a sentence queued but not started
        yet when the window is minimized is taken from the voice and said after the restore."""
        app = self.make(at(10, 12))
        c = self.c
        c._announce()
        settle()
        c._mark_viewed(AM_KEY)
        self.reading.set_tab(hud.TAB_BRIEFING)
        self.reading.set_tab(hud.TAB_JARVIS)               # heard: queued, not started yet
        self.assertEqual(len(self.notices()), 1)
        offer = c.productivity.plan_offer_open()
        c.window.showMinimized()
        settle()
        self.assertEqual(c._prod_activity(), "minimized")
        self.assertIn(f"{ui.OFFER_LINK}{offer.id}", app.voice.cancelled)
        self.assertEqual((offer.speech_state, offer.held_reason), ("held", "dropped"))
        self.tick(1)
        self.assertEqual((len(self.notices()), offer.held_reason), (1, "minimized"))
        c.window.showNormal()
        settle()
        self.assertEqual(c._prod_activity(), "visible")
        self.assertEqual(len(self.notices()), 2)           # said after the restore
        self.assertEqual(offer.spoken_how, "restore")

    def test_nothing_while_hidden(self) -> None:
        self.make(at(10, 12))
        self.c.window.hide()
        settle()
        self.c._set_announced(AM_KEY, True)
        self.tick(1)
        self.assertEqual(self.offer_entries(), [])
        self.assertFalse(self.c.productivity.timer_active)


class PrepOfferAppTests(OffersAppCase):
    def test_offered_spoken_clicked_brief_ready(self) -> None:
        app = self.make(at(13, 30), work=[meeting()])
        with self.assertLogs("briefing_reader", level="DEBUG") as logs:
            self.tick(15)                                  # 1:45 PM
            entry, = self.offer_entries()
            self.assertEqual(entry.entry[2], f"{TITLE} at 2:00 PM (work) starts in 15 minutes, with 1 guest. Shall I "
                                             f"prepare a brief? I'd read your latest thread with each guest and send "
                                             f"them to your Claude.")
            self.assertEqual(entry.link()[0], "Prepare brief")
            self.assertEqual(entry.entry[3], f"1 planner run (none without recent mail) {DOT} 20 of 20 left today "
                                             f"{DOT} as of 1:45 PM")   # read again at 1:45
            self.assertEqual(self.notices(), [("notice", "Your meeting at 2 PM starts in about 15 minutes, sir. "
                                                         "Shall I prepare a brief?", f"{ui.OFFER_LINK}1")])
            self.assertEqual((self.reader.searches, self.reader.reads, app.factory.runner.started), ([], [], []))
            entry.link_button.click()
            self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10), "the brief ran")
            settle()
        self.assertEqual(len(self.reader.searches), 1)     # Jarvis's own search, after the click only
        self.assertEqual(entry.entry[2], "Brief ready below (1:45 PM).")
        self.assertEqual(entry.link(), ("", ""))
        brief = self.reading.conversation.entry_widgets()[-1]
        self.assertTrue(brief.entry[2].startswith(f"Brief: {TITLE} - today 2:00-2:30 PM (work)"))
        self.tick(50)                                      # the meeting ends: the used entry stays as it is
        self.assertEqual(entry.entry[2], "Brief ready below (1:45 PM).")
        for said in app.voice.texts():
            self.assertNotIn("Zq7", said)
            self.assertNotIn("Sam", said)
        record = "\n".join(logs.output)
        for secret in ("Zq7", GUEST, "Sam Example"):
            self.assertNotIn(secret, record)
        self.assertIn("Offer link: used", record)

    def test_an_unclicked_offer_ends_with_the_meeting(self) -> None:
        self.make(at(13, 30), work=[meeting()])
        self.tick(15)
        entry, = self.offer_entries()
        self.tick(46)                                      # 2:31 PM
        self.assertEqual(entry.entry[2], f"{TITLE} at 2:00 PM has ended.")
        self.assertEqual(entry.link(), ("", ""))
        entry.link_button.click()                          # nothing happens on a gone link
        self.assertFalse(self.c.ask.busy)

    def test_a_click_after_the_meeting_ended_before_the_tick(self) -> None:
        """4.4: "That meeting has ended, sir." at once, and no read at all."""
        app = self.make(at(13, 30), work=[meeting()])
        self.tick(15)
        entry, = self.offer_entries()
        app.clock.set(at(14, 31))                          # the meeting ended; the tick has not come yet
        calls = len(app.calendars["work"].calls)
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            entry.link_button.click()
            settle()
        self.assertEqual(entry.entry[2], f"{TITLE} at 2:00 PM has ended.")
        self.assertEqual(entry.link(), ("", ""))
        newest = self.reading.conversation.entry_widgets()[-1]
        self.assertEqual(newest.entry[2], "That meeting has ended, sir.")
        self.assertEqual(app.voice.texts()[-1], "That meeting has ended, sir.")
        self.assertEqual((self.reader.searches, self.reader.reads, app.factory.runner.started), ([], [], []))
        self.assertEqual(len(app.calendars["work"].calls), calls)   # not even the calendar
        self.assertFalse(self.c.ask.busy)
        self.assertIn("Offer link: expired", "\n".join(logs.output))

    def test_mute_while_the_sentence_is_queued_never_brings_it_back(self) -> None:
        """R37: Ctrl+M drops the queued "Shall I ...?" for good; an unmute before the next tick does
        not queue it again."""
        app = self.make(at(13, 30), work=[meeting()])
        self.tick(15)
        self.assertEqual(len(self.notices()), 1)           # queued, not started yet
        self.c._on_mute_toggled(True)
        app.clock.advance(seconds=30)
        self.c._on_mute_toggled(False)
        self.tick(1)
        self.tick(10)
        self.assertEqual(len(self.notices()), 1)
        offer, = self.c.productivity.open_offers()
        self.assertEqual((offer.speech_state, offer.held_reason), ("text_only", "muted"))

    def test_mute_post_unmute_ten_minutes_later_no_speech(self) -> None:
        """Spec 18 (R37): muted when the offer comes, unmuted 10 minutes later: never said."""
        self.make(at(13, 30), work=[meeting()])
        self.c._on_mute_toggled(True)
        self.tick(15)
        self.assertEqual(len(self.offer_entries()), 1)
        self.tick(1)
        self.c._on_mute_toggled(False)
        self.tick(10)
        self.assertEqual(self.notices(), [])

    def test_locked_then_said_after_the_unlock(self) -> None:
        """R18: the tick polls the lock (_locked_now); the held sentence is said after the unlock."""
        self.make(at(13, 30), work=[meeting()])
        with mock.patch.object(self.c, "_locked_now", return_value=True):
            self.tick(15)
        self.assertEqual(len(self.offer_entries()), 1)
        self.assertEqual(self.notices(), [])
        with mock.patch.object(self.c, "_locked_now", return_value=False), \
                self.assertLogs("briefing_reader", level="INFO") as logs:
            self.tick(1)
            said, = self.notices()
            self.c._on_voice_speaking(said[1])             # the voice started it
        self.assertIn("Prep offer: spoken (unlock)", "\n".join(logs.output))

    def test_an_ignored_recurring_meeting_and_a_typed_prep(self) -> None:
        """R38 in the app: prep_series.json under the data folder; three ignored in a row: not offered
        (LIVE says why); a typed "prep me" for it resets the count, in the file too."""
        weekly = meeting(title="Zq7 Weekly", series_id="weekly01")
        app = self.make(at(13, 30), work=[weekly])
        store = self.c.productivity._series
        key = series_key("work", "primary", "weekly01")
        for _ in range(3):
            store.record_ignored(key)
        self.tick(15)
        self.assertEqual(self.offer_entries(), [])
        self.assertIn('Not offered: a prep brief (ignored 3 times - type "prep me" for this one)', self.offer_lines())
        bar = self.reading.command_bar
        bar.set_text("prep me for Zq7 Weekly")
        bar.input.returnPressed.emit()
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10), "the brief ran")
        settle()
        self.assertTrue(self.reading.conversation.entry_widgets()[-1].entry[2].startswith("Brief: Zq7 Weekly"))
        self.assertEqual(store.ignored(key), 0)
        saved = json.loads((app.config.data_dir / SERIES_FILE).read_text(encoding="ascii"))
        self.assertEqual(saved["series"][key]["ignored"], 0)

    def test_a_click_on_a_recurring_offer_resets_its_count(self) -> None:
        """R38: Prepare brief on a repeating meeting ignored twice: the count is 0 again (and in the file)."""
        app = self.make(at(13, 30), work=[meeting(series_id="weekly01")])
        store = self.c.productivity._series
        key = series_key("work", "primary", "weekly01")
        store.record_ignored(key)
        store.record_ignored(key)
        self.tick(15)
        entry, = self.offer_entries()
        entry.link_button.click()
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10), "the brief ran")
        settle()
        self.assertEqual(entry.entry[2], "Brief ready below (1:45 PM).")
        self.assertEqual(store.ignored(key), 0)
        saved = json.loads((app.config.data_dir / SERIES_FILE).read_text(encoding="ascii"))
        self.assertEqual(saved["series"][key]["ignored"], 0)
        self.tick(46)                                      # it ends: a clicked offer never counts as ignored
        self.assertEqual(store.ignored(key), 0)

    def test_an_unclicked_recurring_offer_counts_in_the_file(self) -> None:
        app = self.make(at(13, 30), work=[meeting(series_id="weekly01")])
        self.tick(15)
        self.assertEqual(len(self.offer_entries()), 1)
        self.tick(46)                                      # 2:31 PM: it ended, nobody clicked
        key = series_key("work", "primary", "weekly01")
        saved = json.loads((app.config.data_dir / SERIES_FILE).read_text(encoding="ascii"))
        self.assertEqual(saved["series"][key]["ignored"], 1)

    def test_a_timer_change_never_moves_the_view(self) -> None:
        """An entry changed by a tick (an offer that expired, a sub line) does not scroll the owner who
        is reading further up back to the newest entry."""
        self.make(at(13, 20))
        self.c._announce()
        settle()
        entry, = self.offer_entries()
        conversation = self.reading.conversation
        conversation._follow = False                       # the owner scrolled up a while ago
        conversation._user_scrolled_at = time.monotonic() - 60
        self.c.ask.caps = dataclasses.replace(self.c.ask.caps, left_bundle_day=19)
        self.tick(1)                                       # the sub line changes
        self.assertEqual(entry.entry[3], f"1 planner run {DOT} 19 of 20 left today")
        self.tick(10)                                      # 1:31 PM: expired
        self.assertEqual(entry.entry[2], 'Offered at 1:20 PM - expired. Type "plan my day" any time.')
        self.assertFalse(conversation.following())

    def test_a_small_hourly_limit_still_allows_offers(self) -> None:
        """The offers keep the reserve as a run does (usage.reserve_kept): [ask] max_per_hour 3 with
        reserve_ask_runs 3 keeps 2, so the offer is posted while its click could run."""
        app = AppHarness(self, readers={"work": FakeReader("work", {})}, productivity=True, max_per_hour=3,
                         clock=HarnessClock(at(13, 30)))
        app.calendars["work"].briefs = [meeting()]
        app.calendars["personal"].briefs = []
        with self.assertLogs("briefing_reader", level="WARNING") as logs:
            app.reading(autoplay=False)
            app.c.ask.start()
            self.assertTrue(wait_for(lambda: app.c.ask.ready is not None and app.c.ask.caps is not None))
        self.assertIn("reserve_ask_runs (3) leaves no room in [ask] max_per_hour (3); keeping 2",
                      "\n".join(logs.output))
        self.assertEqual(app.c.ask.caps.left_hour, 3)
        self.assertEqual(app.c._prod_ask_state()[:2], (True, True))
        app.clock.advance(15)
        app.c.productivity.tick()
        settle()
        self.assertEqual(len([entry for entry in app.c.window.reading.conversation.entry_widgets()
                              if entry.link()[1].startswith(ui.OFFER_LINK)]), 1)

    def test_a_queued_sentence_is_taken_back_when_the_window_hides(self) -> None:
        """4.1: hidden WAITING / the prompt: nothing said there. A sentence queued but not started yet
        is cancelled and held; once the reading screen is back it is said at a quiet tick."""
        app = self.make(at(13, 30), work=[meeting()])
        self.tick(15)
        self.assertEqual(len(self.notices()), 1)           # queued, not started yet
        offer, = self.c.productivity.open_offers()
        self.c.window.hide()
        settle()
        self.assertIn(f"{ui.OFFER_LINK}{offer.id}", app.voice.cancelled)
        self.assertEqual((offer.speech_state, offer.held_reason), ("held", "dropped"))
        self.assertFalse(self.c.productivity.timer_active)
        self.c.window.show()
        settle()
        self.tick(1)                                       # 1:46 PM: back and quiet: said now
        self.assertEqual(len(self.notices()), 2)

    def test_a_queued_sentence_is_taken_back_when_the_prompt_replaces_the_reading_screen(self) -> None:
        """4.1: a scheduled launch while the reading screen is up goes back to the prompt with the window
        still visible: a sentence queued but not started yet is never said over the prompt."""
        app = self.make(at(13, 30), work=[meeting()])
        self.tick(15)
        self.assertEqual(len(self.notices()), 1)           # queued, not started yet
        offer, = self.c.productivity.open_offers()
        with mock.patch.object(self.c, "_start_fetch"):    # never Notion
            self.c._back_to_prompt("PM", take_focus=False)
            settle()
        self.assertTrue(self.c.window.isVisible())
        self.assertEqual(self.c._prod_activity(), "off")
        self.assertIn(f"{ui.OFFER_LINK}{offer.id}", app.voice.cancelled)
        self.assertEqual((offer.speech_state, offer.held_reason), ("held", "dropped"))
        self.assertFalse(self.c.productivity.timer_active)

    def test_minimized_with_prep_speak_minimized_off_a_queued_sentence_waits_for_the_restore(self) -> None:
        app = self.make(at(13, 30), work=[meeting()], prod_extra="prep_speak_minimized = false\n")
        self.tick(15)
        self.assertEqual(len(self.notices()), 1)           # queued on screen, not started yet
        offer, = self.c.productivity.open_offers()
        self.c.window.showMinimized()
        settle()
        self.assertIn(f"{ui.OFFER_LINK}{offer.id}", app.voice.cancelled)
        self.assertEqual(offer.speech_state, "held")
        self.c.window.showNormal()
        settle()
        self.assertEqual(len(self.notices()), 2)

    def test_minimized_flash_and_time_only_speech(self) -> None:
        self.make(at(13, 30), work=[meeting()])
        alerts = []
        with mock.patch.object(QApplication, "alert", side_effect=lambda *args: alerts.append(args)):
            self.c.window.showMinimized()
            settle()
            self.assertEqual(self.c._prod_activity(), "minimized")
            self.tick(15)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(self.notices()[-1][1], "Your meeting at 2 PM starts in about 15 minutes, sir. Shall I "
                                                "prepare a brief?")
        self.assertTrue(self.c.window.isMinimized())       # never raised or restored

    def test_back_to_back_meetings_two_entries_one_speech(self) -> None:
        one = meeting("evt0001", start=at(13), minutes=60)
        two = meeting("evt0002", start=at(14), minutes=60, title="Zq7 Review", guests=(LEE,))
        self.make(at(12, 30), work=[one, two])
        self.tick(15)
        first, second = self.offer_entries()
        self.assertTrue(first.entry[2].startswith(f"{TITLE} at 1:00 PM (work) starts in 15 minutes"))
        self.assertTrue(second.entry[2].startswith("Zq7 Review at 2:00 PM (work) starts in 75 minutes"))
        self.assertEqual([said[1] for said in self.notices()],
                         ["You have meetings at 1 PM and 2 PM, sir. Shall I prepare briefs?"])

    def test_in_a_meeting_text_only(self) -> None:
        under_way = meeting("evt0009", start=at(13, 30), minutes=60, title="Zq7 Standup", guests=(LEE,))
        self.make(at(13, 30), work=[under_way, meeting()])
        self.tick(15)
        self.assertEqual(len(self.offer_entries()), 1)
        self.assertEqual(self.notices(), [])

    def test_a_click_while_busy_is_refused_and_the_link_stays(self) -> None:
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.make(at(13, 30), GatedProcess(plan_stream({"say": "x", "question": "", "lines": []}), gate),
                  work=[meeting()])
        self.tick(15)
        entry, = self.offer_entries()
        bar = self.reading.command_bar
        bar.set_text("who is Sam")
        bar.input.returnPressed.emit()
        self.assertTrue(wait_for(lambda: self.c.ask.busy))
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            entry.link_button.click()
        self.assertEqual(bar.status(), ask_ui.CLICK_BUSY_TEXT)
        self.assertEqual(entry.link()[0], "Prepare brief")
        self.assertIn("Offer link: busy", "\n".join(logs.output))
        gate.set()
        self.assertTrue(wait_for(lambda: not self.c.ask.busy, 10))

    def test_offers_never_act_before_a_click(self) -> None:
        app = self.make(at(10, 12), work=[meeting(start=at(10, 30)), meeting("evt0002", start=at(11, 30))])
        self.c._announce()
        for _ in range(90):
            self.tick(1)
        self.assertGreaterEqual(len(self.offer_entries()), 2)
        self.assertEqual((self.reader.searches, self.reader.reads), ([], []))
        self.assertEqual(app.factory.runner.started, [])
        for calendar in app.calendars.values():
            self.assertFalse([call for call in calendar.calls if call[0] in ("create", "move", "sign_in")])
        for sender in app.senders.values():
            self.assertEqual(sender.sent, [])


class HintAndLifecycleTests(OffersAppCase):
    def test_the_done_hint_once_and_the_done_tooltip(self) -> None:
        self.make(at(13, 30), work=[meeting()])
        hint = persona.prod_hint_text("sir")
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            self.tick(1)
            self.tick(1)
        texts = [entry.entry[2] for entry in self.reading.conversation.entry_widgets()]
        self.assertEqual(texts.count(hint), 1)
        self.assertIn("Productivity: the Done hint was shown", "\n".join(logs.output))
        self.assertTrue(self.c._prefs_store().prod_hint_shown)
        self.assertEqual(self.app.voice.texts().count(hint), 0)      # never spoken
        self.assertEqual(self.reading.done_tooltip(), persona.DONE_PREP_TOOLTIP)
        self.tick(61)                                                # 2:33 PM: no guest meeting left
        self.assertEqual(self.reading.done_tooltip(), ui.DONE_TOOLTIP)

    def test_the_done_hint_is_not_shown_again_after_a_restart(self) -> None:
        """R34: assistant.json prod_hint_shown from an earlier run: no hint this time."""
        self.make(at(13, 30), work=[meeting()])
        self.c._prefs_store().set_prod_hint_shown()
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            self.tick(1)
            self.tick(15)                                            # an offer too: still no hint
        texts = [entry.entry[2] for entry in self.reading.conversation.entry_widgets()]
        self.assertNotIn(persona.prod_hint_text("sir"), texts)
        self.assertEqual(len(self.offer_entries()), 1)
        self.assertNotIn("the Done hint was shown", "\n".join(logs.output))

    def test_app_shutdown_stops_the_offers(self) -> None:
        """AppController.shutdown (Done, X, tray Quit) stops the bundle's timer and thread."""
        self.make(at(13, 30), work=[meeting()])
        controller = self.c.productivity
        self.assertTrue(controller.timer_active)
        with mock.patch.object(ui, "_remove_audio_dir_after"):
            self.c.shutdown()
        self.assertTrue(controller.closed)
        self.assertFalse(controller.timer_active)
        self.app.clock.advance(15)
        controller.tick()
        self.c._set_announced(AM_KEY, True)
        settle()
        self.assertEqual((self.offer_entries(), self.notices()), ([], []))

    def test_a_broken_series_store_keeps_the_offers(self) -> None:
        """prep_series.json is only a count: a store that cannot be set up never turns the offers off."""
        with mock.patch("briefing_reader.prepseries.SeriesStore", side_effect=RecursionError("deep")), \
                self.assertLogs("briefing_reader", level="WARNING") as logs:
            self.make(at(13, 30), work=[meeting()])
        self.assertIsNone(self.c.productivity._series)
        self.assertIn("Could not read the ignored meeting series (RecursionError)", "\n".join(logs.output))
        self.tick(15)
        self.assertEqual(len(self.offer_entries()), 1)

    def test_close_stops_the_timer(self) -> None:
        self.make(at(13, 30))
        controller = self.c.productivity
        self.assertTrue(controller.timer_active)
        with mock.patch.object(controller, "shutdown", wraps=controller.shutdown) as shutdown:
            self.app.close()
        shutdown.assert_called_once_with()
        self.assertTrue(controller._closed)
        self.app.close()                                   # a second close (the cleanup's) does nothing

    def test_bundle_off_has_no_controller(self) -> None:
        app = AppHarness(self)
        self.assertIsNone(app.c.productivity)


if __name__ == "__main__":
    unittest.main()
