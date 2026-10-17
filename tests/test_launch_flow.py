"""Launch kinds and the "Jarvis talks" flow of the app controller (ui.AppController).

OPEN (the Start menu, the hotkey, --open): the assistant screen on its JARVIS tab with a greeting,
nothing read. READ (--read, an older now:true): reading at once on the BRIEFING tab. SCHEDULED
(--run, the catch-up): waits invisibly, then shows the fresh briefing without the focus and
announces it once. NEW until viewed; announced once (runstate.json, or in memory).

Offscreen, a player that only keeps its state, a recording voice (nothing is ever synthesized),
fetches handed over by the test (never Notion), no Google, a scratch data folder. Every text is
invented.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import random
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from PySide6.QtWidgets import QApplication

from briefing_reader import config as config_module
from briefing_reader import hud, persona, ui
from briefing_reader.config import CalendarConfig, load_config
from briefing_reader.models import LAUNCH_OPEN, LAUNCH_READ, LAUNCH_SCHEDULED, Briefing, BriefingHeader
from briefing_reader.notion_client import FixtureSession, NotionClient, NotionError, PollResult, fetch_briefing
from briefing_reader.prefs import PREFS_FILE, AssistantPrefs
from briefing_reader.runstate import RunState
from tests.ui_fakes import B_CALENDAR, B_TODO, FIXTURE, NOW, PDT, FakePlayer, RecordingVoice, page_fixture, settle

_app: QApplication | None = None
SLOTS = {"AM": "10:12", "PM": "23:42"}
KEY = "2026-10-04 AM"                 # the fixture: "Updated: 2026-10-04 10:04 PDT", "Run: AM"
READY = "Your AM briefing is ready to view."
ALONE = "Sir, your AM briefing is ready to view."


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_launch_flow", "-platform", "offscreen"])
    hud.load_fonts(config_module.PROJECT_ROOT / "fonts")


def variant(page: Briefing, updated: datetime | None, run: str | None) -> Briefing:
    raw = f"{updated:%Y-%m-%d %H:%M} PDT" if updated is not None else "never"
    return dataclasses.replace(page, header=BriefingHeader(raw, updated, run))


class Flow:
    """One controller and the fakes around it; the test hands over fetch answers (attempt / done)."""

    def __init__(self, test: unittest.TestCase, *, launch: str | None = None, expected_run: str | None = "AM",
                 now_mode: bool = False, ask_mode: bool = False, classic: bool = False, greet: bool = True,
                 run_state: bool = True, now: datetime = NOW, greeting_context: bool = True,
                 lines: tuple[str, ...] = ()) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        test.addCleanup(tmp.cleanup)
        self.root = root = Path(tmp.name)
        base = load_config(root, environ={"LOCALAPPDATA": str(root / "local")})
        session = FixtureSession(page_fixture(root, lines) if lines else FIXTURE)
        self.config = dataclasses.replace(
            base, page_id=session.page_id, audio_root=root / "audio", calendar=CalendarConfig(enabled=False),
            assistant=dataclasses.replace(base.assistant, scheduled_prompt=classic, greet=greet,
                                          greeting_context=greeting_context))
        self.client = NotionClient("", session=session, notion_version=self.config.notion_version)
        self.page = fetch_briefing(self.client, self.config.page_id)
        self.now = now
        self.active = True
        self.locked = False
        self.voice = RecordingVoice()
        self.state = RunState(root / "runstate.json", clock=lambda: self.now) if run_state else None
        self.fetches: list[str | None] = []
        players = mock.patch.object(ui, "BriefingPlayer", FakePlayer)   # also for a replaced player
        players.start()
        test.addCleanup(players.stop)
        self.c = ui.AppController(self.config, self.client, expected_run=expected_run, now_mode=now_mode,
                                  ask_mode=ask_mode, volume=0.0, now_func=lambda: self.now, slots=SLOTS,
                                  run_state=self.state, launch=launch,
                                  voice_factory=lambda _controller: self.voice, locked=lambda: self.locked)
        c = self.c
        c._rng = random.Random(4)
        c._start_tts = lambda: None            # no speech worker: nothing is ever synthesized
        c._window_active = lambda: self.active
        c._start_fetch = self._start_fetch     # never Notion: the test hands the answers over
        test.addCleanup(self.close)

    # ---- fetching by hand --------------------------------------------------------------

    def _start_fetch(self, poll_run: str | None) -> None:
        c = self.c
        c._stop_fetch()
        c._fetch_id += 1
        c._fetch_stop = threading.Event()
        c._fetch_poll_run = poll_run
        c._fetch_error = None
        c._fetch_attempts = 0
        self.fetches.append(poll_run)

    def attempt(self, briefing: Briefing | None = None) -> None:
        c = self.c
        c._on_fetch_attempt(c._fetch_id, briefing if briefing is not None else self.page, None,
                            c._fetch_attempts + 1)
        settle()

    def done(self, *, timed_out: bool = True) -> None:
        c = self.c
        c._on_fetch_done(c._fetch_id, PollResult(c._briefing, None, None, max(1, c._fetch_attempts), timed_out,
                                                 False))
        settle()

    # ---- looking -------------------------------------------------------------------------

    @property
    def reading(self) -> ui.ReadingView:
        return self.c.window.reading

    def entries(self) -> list[tuple[str, str, str, str]]:
        return self.reading.conversation.entries()

    def saved(self, key: str = KEY) -> dict:
        return (RunState(self.root / "runstate.json").get(key) or {}) if self.state is not None else {}

    def close(self) -> None:
        c = self.c
        for timer in (c._ignore_timer, c._tick_timer, c._snooze_timer, c._send_hold_timer, c._agenda_timer,
                      c._greeting_timer, c._held_timer, c._note_timer, c._recheck_timer):
            timer.stop()
        c.state = ui.STATE_QUITTING
        c.window.hide()
        c.window.deleteLater()
        settle()


class LaunchKindTests(unittest.TestCase):
    def test_derived_from_the_older_arguments(self) -> None:
        cases = [({"now_mode": True}, LAUNCH_READ), ({"ask_mode": True}, LAUNCH_OPEN),
                 ({"now_mode": False}, LAUNCH_SCHEDULED), ({"expected_run": None}, LAUNCH_OPEN),
                 ({"launch": LAUNCH_OPEN}, LAUNCH_OPEN), ({"launch": LAUNCH_READ, "expected_run": None}, LAUNCH_READ)]
        for kwargs, kind in cases:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(Flow(self, **kwargs).c.launch, kind)
        # The classic mode: a plain constructor (no run) shows the prompt as it used to.
        self.assertEqual(Flow(self, expected_run=None, classic=True).c.launch, LAUNCH_SCHEDULED)
        self.assertEqual(Flow(self, expected_run=None, classic=True, launch=LAUNCH_OPEN).c.launch, LAUNCH_OPEN)

    def test_a_plain_classic_start_shows_the_prompt(self) -> None:
        flow = Flow(self, expected_run=None, classic=True)
        flow.c.start()
        self.assertEqual(flow.c.state, ui.STATE_PROMPT)
        self.assertEqual(flow.fetches, [None])
        self.assertEqual(flow.voice.said, [])

    def test_session_locked_is_false_offscreen(self) -> None:
        self.assertFalse(ui.session_locked())   # never a Win32 desktop check under the test platform

    def test_the_default_voice_is_silent(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        config = load_config(root, environ={"LOCALAPPDATA": str(root / "local")})
        with mock.patch.object(ui, "BriefingPlayer", FakePlayer):
            c = ui.AppController(dataclasses.replace(config, calendar=CalendarConfig(enabled=False)),
                                 NotionClient("", session=FixtureSession(FIXTURE)), expected_run=None,
                                 now_mode=False)
        self.addCleanup(c.window.deleteLater)
        self.assertIsInstance(c.voice, ui.SilentVoice)
        self.assertFalse(c.voice.say("greeting", "Good morning, sir."))


class OpenTests(unittest.TestCase):
    def test_open_greets_and_folds_the_announcement_in(self) -> None:
        flow = Flow(self, launch=LAUNCH_OPEN, expected_run=None)
        c = flow.c
        c.start()
        settle()
        self.assertEqual(c.state, ui.STATE_READING)
        self.assertTrue(c.window.isVisible() and c.window.is_reading_view())
        self.assertEqual(flow.reading.current_tab(), hud.TAB_JARVIS)
        self.assertEqual(flow.fetches, [None])                # one fetch, no run waited for
        first = flow.entries()
        self.assertEqual(len(first), 1)                       # the greeting line at once
        self.assertEqual(first[0][0], hud.ROLE_JARVIS)
        self.assertIn(first[0][2], [line.text("sir") for line in persona.GREETINGS])
        self.assertEqual(flow.voice.said, [])                 # waits for the first fetch answer
        self.assertTrue(c._greeting_timer.isActive())
        flow.attempt()
        self.assertFalse(c._greeting_timer.isActive())
        self.assertEqual(len(flow.voice.said), 1)
        kind, text, key = flow.voice.said[0]
        self.assertEqual((kind, key), ("greeting", f"announce:{KEY}"))
        self.assertTrue(text.endswith(READY), text)
        self.assertTrue(text.startswith(persona.sentences(first[0][2])[0]))
        self.assertLessEqual(len(persona.sentences(text)), 3)
        self.assertEqual(len(flow.entries()), 1)              # the same entry, updated
        self.assertEqual(flow.entries()[0][2], text)
        entry = flow.reading.conversation.entry_widgets()[0]
        self.assertEqual(entry.link(), (ui.VIEW_BRIEFING_TEXT, ui.VIEW_BRIEFING_LINK))
        self.assertEqual(c.player.starts, [])                 # nothing read
        self.assertIn("announced_at", flow.saved())
        self.assertNotIn("how", flow.saved())                 # no slot settled
        self.assertNotIn("viewed_at", flow.saved())
        self.assertTrue(flow.reading.is_new_briefing())             # NEW until viewed
        # A second OPEN within 2 minutes into the visible window: no greeting, no second announcement.
        c.handle_activation({"cmd": "activate", "run": None, "now": False, "open": True})
        settle()
        self.assertEqual(len(flow.voice.said), 1)
        self.assertEqual(len(flow.entries()), 1)
        self.assertEqual(flow.reading.current_tab(), hud.TAB_JARVIS)
        # The recent greeting is remembered (ids only).
        self.assertEqual(AssistantPrefs(flow.config.data_dir / PREFS_FILE).recent_greetings[-1:],
                         [c._greeting_line.id])

    def test_a_question_greeting_gives_way_to_the_salutation_before_the_announcement(self) -> None:
        for line_id in ("n5", "n6", "l1", "n4"):
            with self.subTest(line=line_id):
                flow = Flow(self, launch=LAUNCH_OPEN, expected_run=None, greeting_context=False)
                with mock.patch.object(ui, "pick_greeting", return_value=persona.GREETINGS_BY_ID[line_id]):
                    flow.c.start()
                self.assertEqual(flow.entries()[0][2], persona.GREETINGS_BY_ID[line_id].text())   # at first
                flow.attempt()
                expected = "Good afternoon, sir. " + READY
                self.assertEqual(flow.voice.said, [("greeting", expected, f"announce:{KEY}")])
                self.assertEqual(flow.entries()[0][2], expected)
        flow = Flow(self, launch=LAUNCH_OPEN, expected_run=None)   # nothing follows: said as it is
        with mock.patch.object(ui, "pick_greeting", return_value=persona.GREETINGS_BY_ID["n5"]):
            flow.c.start()
        flow.reading.set_tab(hud.TAB_BRIEFING)
        flow.attempt()
        self.assertEqual(flow.voice.said, [("greeting", "How can I help, sir?", "")])

    def test_open_with_a_viewed_briefing_greets_only(self) -> None:
        flow = Flow(self, launch=LAUNCH_OPEN)
        flow.state.mark_viewed(KEY)
        flow.c.start()
        flow.attempt()
        self.assertEqual(len(flow.voice.said), 1)
        kind, text, key = flow.voice.said[0]
        self.assertEqual((kind, key), ("greeting", ""))
        self.assertNotIn("ready to view", text)
        self.assertFalse(flow.reading.is_new_briefing())
        self.assertNotIn("announced_at", flow.saved())

    def test_a_briefing_heard_through_the_older_prompt_is_not_new(self) -> None:
        # runstate.json from before this version: the briefing was read (or Done after reading).
        for how in ("read", "done"):
            with self.subTest(how=how):
                flow = self.older_runstate(how)
                flow.c.start()
                flow.attempt()
                self.assertEqual(flow.voice.kinds(), ["greeting"])
                self.assertNotIn(READY, flow.voice.said[0][1])
                self.assertFalse(flow.reading.is_new_briefing())
                self.assertNotIn("announced_at", flow.saved())
                self.assertTrue(AssistantPrefs(flow.config.data_dir / PREFS_FILE).answers_adopted)
        flow = self.older_runstate("dismissed")   # dismissed: never heard, so still NEW
        flow.c.start()
        flow.attempt()
        self.assertTrue(flow.voice.said[0][1].endswith(READY))
        self.assertTrue(flow.reading.is_new_briefing())

    def test_older_answers_are_adopted_once_only(self) -> None:
        # After the first start of this version, an answer is only an answer: a "read" this version
        # recorded for a slot whose own briefing was never shown keeps that briefing NEW.
        flow = self.older_runstate("read", adopted=True)
        flow.c.start()
        flow.attempt()
        self.assertTrue(flow.voice.said[0][1].endswith(READY))
        self.assertTrue(flow.reading.is_new_briefing())

    def older_runstate(self, how: str, *, adopted: bool = False) -> Flow:
        flow = Flow(self, launch=LAUNCH_OPEN, expected_run=None)
        old = {KEY: {"shown_at": "2026-10-04T10:12:30-07:00", "handled_at": "2026-10-04T10:13:00-07:00", "how": how}}
        (flow.root / "runstate.json").write_text(json.dumps(old), encoding="utf-8")
        flow.state = flow.c.run_state = RunState(flow.root / "runstate.json", clock=lambda: flow.now)
        if adopted:
            AssistantPrefs(flow.config.data_dir / PREFS_FILE).set_answers_adopted()
        return flow

    def test_an_old_unviewed_briefing_is_not_new(self) -> None:
        flow = Flow(self, launch=LAUNCH_OPEN, now=NOW + timedelta(hours=18, minutes=30))
        flow.c.start()
        flow.attempt()
        self.assertEqual(flow.voice.kinds(), ["greeting"])
        self.assertNotIn("ready to view", flow.voice.texts()[0])
        self.assertFalse(flow.reading.is_new_briefing())
        self.assertIsNone(flow.c._new_key())

    def test_a_slow_fetch_gets_the_greeting_first_then_the_announcement(self) -> None:
        flow = Flow(self, launch=LAUNCH_OPEN)
        c = flow.c
        c.start()
        self.assertEqual(c._greeting_timer.interval(), 3000)
        c._greeting_timer.timeout.emit()   # 3 s passed without an answer
        settle()
        self.assertEqual(flow.voice.said[0][0], "greeting")
        self.assertNotIn("ready to view", flow.voice.said[0][1])
        flow.attempt()
        self.assertEqual(flow.voice.said[1], ("announce", ALONE, f"announce:{KEY}"))
        self.assertEqual([entry[2] for entry in flow.entries()][1], ALONE)
        self.assertIn("announced_at", flow.saved())
        flow.attempt()   # the same page again: never announced twice
        self.assertEqual(len(flow.voice.said), 2)

    def test_greet_false_announces_alone(self) -> None:
        flow = Flow(self, launch=LAUNCH_OPEN, greet=False)
        flow.c.start()
        self.assertEqual(flow.entries(), [])
        flow.attempt()
        self.assertEqual(flow.voice.said, [("announce", ALONE, f"announce:{KEY}")])

    def test_text_only_when_the_voice_does_not_speak(self) -> None:
        flow = Flow(self, launch=LAUNCH_OPEN)
        flow.voice.speaks = False
        with self.assertLogs("briefing_reader", level="INFO") as logs:
            flow.c.start()
            flow.attempt()
        self.assertIn("announced_at", flow.saved())    # shown as text: that is the delivery
        self.assertTrue(any("(text only)" in line for line in logs.output))

    def test_greeting_context_names_waiting_proposals(self) -> None:
        flow = Flow(self, launch=LAUNCH_OPEN, lines=(B_CALENDAR, B_TODO))
        flow.state.mark_viewed(KEY)
        flow.c.start()
        flow.attempt()
        self.assertEqual(len(flow.c._pending_actions()), 2)
        self.assertTrue(flow.voice.texts()[0].endswith("Two proposals are waiting for your OK."))
        self.assertLessEqual(len(persona.sentences(flow.voice.texts()[0])), 3)

    def test_greeting_context_with_the_announcement(self) -> None:
        flow = Flow(self, launch=LAUNCH_OPEN, lines=(B_CALENDAR, B_TODO))
        flow.c.start()
        flow.attempt()
        text = flow.voice.texts()[0]
        self.assertTrue(text.endswith(READY + " Two proposals are waiting for your OK."), text)
        self.assertEqual(len(persona.sentences(text)), 3)

    def test_greeting_context_next_event(self) -> None:
        from briefing_reader.gcal import CalendarEvent

        flow = Flow(self, launch=LAUNCH_OPEN)
        flow.state.mark_viewed(KEY)
        c = flow.c
        c._agenda_status = ui._AGENDA_OK
        c._agenda_events = [CalendarEvent("Lab meeting", start=NOW + timedelta(hours=4)),
                            CalendarEvent("Project sync", start=NOW + timedelta(minutes=68)),
                            CalendarEvent("All day thing", all_day_start=NOW.date())]
        c.start()
        flow.attempt()
        self.assertTrue(flow.voice.texts()[0].endswith("Your next event is Project sync at 3 PM."))

    def test_greeting_context_off(self) -> None:
        flow = Flow(self, launch=LAUNCH_OPEN, greeting_context=False, lines=(B_CALENDAR,))
        flow.state.mark_viewed(KEY)
        flow.c.start()
        flow.attempt()
        self.assertNotIn("proposal", flow.voice.texts()[0])

    def test_open_with_run_waits_for_that_run_in_the_background(self) -> None:
        flow = Flow(self, launch=LAUNCH_OPEN, expected_run="AM")
        c = flow.c
        stale = variant(flow.page, datetime(2026, 10, 3, 23, 50, tzinfo=PDT), "PM")
        c.start()
        self.assertEqual(flow.fetches, ["AM"])
        flow.attempt(stale)
        self.assertEqual(c._script.run_label, "PM")
        self.assertTrue(c.fetching)                              # the AM poll goes on
        said = len(flow.voice.said)
        flow.attempt(flow.page)                                  # the fresh AM briefing
        self.assertEqual(c._briefing.header.run, "AM")
        self.assertEqual(c._script.run_label, "AM")
        self.assertFalse(c.fetching)
        self.assertEqual(flow.voice.said[said:], [("announce", ALONE, f"announce:{KEY}")])


class ViewingTests(unittest.TestCase):
    def opened(self) -> Flow:
        flow = Flow(self, launch=LAUNCH_OPEN)
        flow.c.start()
        flow.c._greeting_timer.timeout.emit()   # the greeting alone, so the announcement is its own
        flow.attempt()
        return flow

    def test_briefing_tab_in_the_active_window_views_it(self) -> None:
        flow = self.opened()
        c, reading = flow.c, flow.reading
        self.assertTrue(reading.is_new_briefing())
        self.assertEqual(reading.speech.text(), "Your AM briefing is ready to view")
        self.assertTrue(reading.updated_bar.value().startswith(f"new {ui.DOT} "))
        self.assertEqual(reading.updated_bar.value_color().name(), hud.AMBER)
        self.assertEqual(c._tray_tooltip(), "Jarvis: AM briefing ready to view")
        reading.set_tab(hud.TAB_BRIEFING)
        settle()
        self.assertIn("viewed_at", flow.saved())
        self.assertIn(f"announce:{KEY}", flow.voice.cancelled)    # a queued announcement is dropped
        self.assertFalse(reading.is_new_briefing())
        self.assertEqual(reading.speech.text(), "AM briefing")
        self.assertFalse(reading.updated_bar.value().startswith("new"))
        self.assertEqual(c._tray_tooltip(), ui.TRAY_TOOLTIP)
        self.assertEqual(c.player.starts, [])                     # viewing is not reading

    def test_briefing_tab_of_an_inactive_window_stays_new_until_activated(self) -> None:
        flow = self.opened()
        flow.active = False
        flow.reading.set_tab(hud.TAB_BRIEFING)
        settle()
        self.assertTrue(flow.reading.is_new_briefing())
        self.assertNotIn("viewed_at", flow.saved())
        flow.active = True
        flow.c.window.activeChanged.emit(True)
        settle()
        self.assertFalse(flow.reading.is_new_briefing())
        self.assertIn("viewed_at", flow.saved())

    def test_play_from_jarvis_switches_to_briefing_and_views(self) -> None:
        flow = self.opened()
        flow.active = False   # playing counts as viewing even in an inactive window
        self.assertEqual(flow.reading.current_tab(), hud.TAB_JARVIS)
        flow.c.toggle_play()
        settle()
        self.assertEqual(flow.reading.current_tab(), hud.TAB_BRIEFING)
        self.assertEqual(len(flow.c.player.starts), 1)
        self.assertIn("viewed_at", flow.saved())
        self.assertEqual(flow.saved().get("how"), "read")         # Play settles the slot, as before
        self.assertFalse(flow.reading.is_new_briefing())

    def test_view_briefing_link(self) -> None:
        flow = self.opened()
        entry = flow.reading.conversation.entry_widgets()[-1]
        self.assertEqual(entry.link()[1], ui.VIEW_BRIEFING_LINK)
        entry.link_button.click()
        settle()
        self.assertEqual(flow.reading.current_tab(), hud.TAB_BRIEFING)
        self.assertIn("viewed_at", flow.saved())

    def test_viewed_before_the_announcement_is_never_said(self) -> None:
        flow = Flow(self, launch=LAUNCH_OPEN, greet=False)
        flow.c.start()
        flow.reading.set_tab(hud.TAB_BRIEFING)   # the owner looks before the page arrives
        flow.attempt()
        self.assertIn("viewed_at", flow.saved())
        self.assertEqual(flow.voice.said, [])
        self.assertNotIn("announced_at", flow.saved())


class ReadTests(unittest.TestCase):
    def test_read_plays_at_once_and_says_nothing(self) -> None:
        flow = Flow(self, launch=LAUNCH_READ)
        flow.c.start()
        flow.attempt()
        self.assertEqual(flow.reading.current_tab(), hud.TAB_BRIEFING)
        self.assertEqual(len(flow.c.player.starts), 1)
        self.assertEqual(flow.voice.said, [])
        self.assertEqual(flow.entries(), [])
        saved = flow.saved()
        self.assertEqual(saved.get("how"), "read")
        self.assertIn("viewed_at", saved)
        self.assertNotIn("announced_at", saved)
        self.assertFalse(flow.reading.is_new_briefing())

    def test_now_mode_is_read(self) -> None:
        flow = Flow(self, now_mode=True)
        flow.c.start()
        flow.attempt()
        self.assertEqual(len(flow.c.player.starts), 1)
        self.assertEqual(flow.voice.said, [])


class ScheduledTests(unittest.TestCase):
    def test_waits_hidden_then_arrives_without_the_focus_and_announces(self) -> None:
        flow = Flow(self, expected_run="AM")
        c = flow.c
        self.assertEqual(c.launch, LAUNCH_SCHEDULED)
        c.start()
        self.assertEqual(c.state, ui.STATE_WAITING)
        self.assertFalse(c.window.isVisible())
        self.assertEqual(flow.fetches, ["AM"])
        self.assertEqual(c._tray_tooltip(), "Jarvis: waiting for the AM briefing")
        flow.attempt(variant(flow.page, datetime(2026, 10, 3, 23, 50, tzinfo=PDT), "PM"))   # not it yet
        self.assertEqual(c.state, ui.STATE_WAITING)
        self.assertFalse(c.window.isVisible())
        with mock.patch.object(ui, "show_without_activating", wraps=ui.show_without_activating) as quiet, \
                mock.patch.object(ui, "force_foreground") as forced:
            flow.attempt(flow.page)
        quiet.assert_called()
        forced.assert_not_called()
        self.assertEqual(c.state, ui.STATE_READING)
        self.assertTrue(c.window.isVisible())
        self.assertEqual(flow.reading.current_tab(), hud.TAB_JARVIS)
        self.assertEqual(c.player.starts, [])
        self.assertEqual(len(flow.voice.said), 1)
        kind, text, key = flow.voice.said[0]
        self.assertEqual((kind, key), ("greeting", f"announce:{KEY}"))
        self.assertTrue(text.endswith(READY))
        self.assertEqual(len(persona.sentences(text)), 2)
        # Not opened by the owner: the hour's plain salutation, never "Welcome back" or a question.
        self.assertEqual(text, "Good afternoon, sir. " + READY)
        self.assertEqual(AssistantPrefs(flow.config.data_dir / PREFS_FILE).recent_greetings[-1:], ["a1"])
        saved = flow.saved()
        self.assertIn("announced_at", saved)
        self.assertIn("shown_at", saved)
        self.assertNotIn("how", saved)
        self.assertTrue(flow.reading.is_new_briefing())

    def test_poll_timeout_shows_nothing_and_quits(self) -> None:
        flow = Flow(self, expected_run="AM")
        c = flow.c
        c.start()
        with mock.patch.object(ui.QCoreApplication, "quit"):
            flow.done(timed_out=True)
        self.assertEqual(c.state, ui.STATE_QUITTING)
        self.assertFalse(c.window.isVisible())
        self.assertEqual(flow.voice.said, [])
        self.assertEqual(flow.saved(), {})

    def test_already_announced_quits_quietly(self) -> None:
        flow = Flow(self, expected_run="AM")
        flow.state.mark_announced(KEY)   # opened at 10:08, heard it, closed before the 10:12 task
        flow.c.start()
        with mock.patch.object(ui.QCoreApplication, "quit"), \
                self.assertLogs("briefing_reader.ui", level="INFO") as logs:
            flow.attempt(flow.page)
        self.assertEqual(flow.c.state, ui.STATE_QUITTING)
        self.assertFalse(flow.c.window.isVisible())
        self.assertEqual(flow.voice.said, [])
        self.assertTrue(any("already announced; nothing to show" in line for line in logs.output))

    def test_locked_session_holds_the_announcement(self) -> None:
        flow = Flow(self, expected_run="AM")
        flow.locked = True
        flow.c.start()
        flow.attempt(flow.page)
        self.assertEqual(flow.voice.said, [])
        self.assertEqual(len(flow.entries()), 1)                 # the entry is there at once
        self.assertNotIn("announced_at", flow.saved())
        self.assertTrue(flow.c._held_timer.isActive())
        flow.c._held_timer.timeout.emit()                        # still locked
        settle()
        self.assertEqual(flow.voice.said, [])
        self.assertTrue(flow.c._held_timer.isActive())
        flow.locked = False
        flow.c._held_timer.timeout.emit()
        settle()
        self.assertEqual(len(flow.voice.said), 1)
        self.assertTrue(flow.voice.said[0][1].endswith(READY))
        self.assertIn("announced_at", flow.saved())

    def test_viewed_while_locked_is_never_said(self) -> None:
        flow = Flow(self, expected_run="AM")
        flow.locked = True
        flow.c.start()
        flow.attempt(flow.page)
        flow.reading.set_tab(hud.TAB_BRIEFING)
        settle()
        flow.locked = False
        flow.c._held_timer.timeout.emit()
        settle()
        self.assertEqual(flow.voice.said, [])

    def test_startup_error_arrives_as_text_only(self) -> None:
        flow = Flow(self, expected_run="AM")
        flow.c.startup_error = "Notion token missing. Add NOTION_TOKEN to the .env file (see README)."
        with mock.patch.object(ui, "force_foreground") as forced:
            flow.c.start()
        forced.assert_not_called()
        self.assertTrue(flow.c.window.isVisible())
        self.assertEqual(flow.entries()[0][2], "I can't reach your briefing: Notion token missing.")
        self.assertEqual(flow.voice.said, [])
        self.assertFalse(flow.reading.is_new_briefing())
        self.assertEqual(flow.fetches, [])

    def test_classic_prompt_mode(self) -> None:
        flow = Flow(self, expected_run="AM", classic=True)
        flow.c.start()
        self.assertEqual(flow.c.state, ui.STATE_PROMPT)
        self.assertTrue(flow.c.window.isVisible())
        self.assertFalse(flow.c.window.is_reading_view())
        self.assertEqual(flow.fetches, ["AM"])
        self.assertEqual(flow.voice.said, [])

    def test_open_while_waiting_greets_and_keeps_polling(self) -> None:
        flow = Flow(self, expected_run="AM")
        c = flow.c
        c.start()
        flow.attempt(variant(flow.page, datetime(2026, 10, 3, 23, 50, tzinfo=PDT), "PM"))
        c.handle_activation({"cmd": "activate", "run": None, "now": False, "open": True})
        settle()
        self.assertEqual(c.state, ui.STATE_READING)
        self.assertEqual(flow.reading.current_tab(), hud.TAB_JARVIS)
        self.assertEqual(flow.voice.kinds(), ["greeting"])
        self.assertTrue(c.fetching)                               # the AM poll goes on
        flow.attempt(flow.page)
        self.assertEqual(c._script.run_label, "AM")
        self.assertEqual(flow.voice.said[-1], ("announce", ALONE, f"announce:{KEY}"))   # no greeting again

    def test_open_while_waiting_after_a_failed_attempt_keeps_waiting_for_the_run(self) -> None:
        for show in ("open", "tray"):
            with self.subTest(show=show):
                flow = Flow(self, expected_run="AM")
                c = flow.c
                c.start()
                c._on_fetch_attempt(c._fetch_id, None, NotionError("Notion could not be reached."), 1)
                settle()
                self.assertEqual(c.state, ui.STATE_WAITING)
                with mock.patch.object(ui, "force_foreground"):
                    if show == "open":
                        c.handle_activation({"cmd": "activate", "run": None, "now": False, "open": True})
                    else:
                        c._on_tray_show()
                settle()
                self.assertEqual(c.state, ui.STATE_READING)
                self.assertEqual(flow.fetches, ["AM", "AM"])         # the AM wait starts over at once
                flow.attempt(variant(flow.page, datetime(2026, 10, 3, 23, 50, tzinfo=PDT), "PM"))
                self.assertTrue(c._polling)                          # last night's page: still waiting
                self.assertEqual(c._script.run_label, "PM")
                flow.attempt(flow.page)
                self.assertEqual(c._script.run_label, "AM")
                self.assertEqual(flow.voice.said[-1], ("announce", ALONE, f"announce:{KEY}"))

    def test_tray_show_from_waiting_never_greets(self) -> None:
        flow = Flow(self, expected_run="AM")
        flow.c.start()
        flow.c._on_tray_show()
        settle()
        self.assertEqual(flow.c.state, ui.STATE_READING)
        self.assertEqual(flow.voice.said, [])
        flow.attempt(flow.page)   # the page arrives: announced on its own, without a greeting
        self.assertEqual(flow.voice.said, [("announce", ALONE, f"announce:{KEY}")])

    def test_scheduled_for_the_same_run_while_waiting_is_ignored(self) -> None:
        flow = Flow(self, expected_run="AM")
        flow.c.start()
        flow.c.handle_activation({"cmd": "activate", "run": "AM", "now": False})
        self.assertEqual(flow.fetches, ["AM"])
        flow.c.handle_activation({"cmd": "activate", "run": "PM", "now": False})
        self.assertEqual(flow.fetches, ["AM", "PM"])
        self.assertEqual(flow.c.state, ui.STATE_WAITING)


class NewerBriefingTests(unittest.TestCase):
    def open_with_stale(self) -> Flow:
        flow = Flow(self, launch=LAUNCH_OPEN, expected_run=None, greet=False)
        flow.c.start()
        flow.attempt(variant(flow.page, datetime(2026, 10, 3, 23, 50, tzinfo=PDT), "PM"))
        flow.voice.said.clear()
        return flow

    def test_replaced_when_idle(self) -> None:
        flow = self.open_with_stale()
        c = flow.c
        with mock.patch.object(ui, "force_foreground") as forced:
            c.handle_activation({"cmd": "activate", "run": "AM", "now": False})
        forced.assert_not_called()                                 # a scheduled launch takes no focus
        self.assertEqual(flow.fetches[-1], "AM")
        flow.attempt(flow.page)
        self.assertEqual(c._script.run_label, "AM")
        self.assertTrue(flow.reading.is_new_briefing())
        self.assertEqual(flow.voice.said, [("announce", ALONE, f"announce:{KEY}")])
        self.assertEqual(c.player.starts, [])

    def test_already_shown_fresh_briefing_needs_nothing(self) -> None:
        flow = Flow(self, launch=LAUNCH_OPEN, expected_run=None, greet=False)
        flow.c.start()
        flow.attempt(flow.page)
        fetches = list(flow.fetches)
        flow.c.handle_activation({"cmd": "activate", "run": "AM", "now": False})
        self.assertEqual(flow.fetches, fetches)

    def test_deferred_while_playing_then_shown_when_finished(self) -> None:
        flow = self.open_with_stale()
        c = flow.c
        c.toggle_play()
        self.assertEqual(c.player.state, "playing")
        c.handle_activation({"cmd": "activate", "run": "AM", "now": False})
        flow.attempt(flow.page)
        self.assertEqual(c._script.run_label, "PM")                # never cut a listen short
        self.assertIn("it shows when this reading ends", c._note)
        self.assertEqual(flow.voice.said, [])
        flow.active = False   # the owner is in another window when the reading ends
        c.player._set("finished")
        settle()
        settle()
        self.assertEqual(c._script.run_label, "AM")
        self.assertEqual(flow.voice.said, [("announce", ALONE, f"announce:{KEY}")])
        self.assertTrue(flow.reading.is_new_briefing())

    def test_replaced_in_front_of_the_owner_counts_as_viewed(self) -> None:
        flow = self.open_with_stale()
        c = flow.c
        c.toggle_play()                    # the BRIEFING tab is current, the window active
        c.handle_activation({"cmd": "activate", "run": "AM", "now": False})
        flow.attempt(flow.page)
        c.player._set("finished")
        settle()
        settle()
        self.assertEqual(c._script.run_label, "AM")
        self.assertEqual(flow.voice.said, [])
        self.assertIn("viewed_at", flow.saved())

    def test_done_while_deferred_quits(self) -> None:
        flow = self.open_with_stale()
        c = flow.c
        c.toggle_play()
        c.handle_activation({"cmd": "activate", "run": "AM", "now": False})
        flow.attempt(flow.page)
        with mock.patch.object(ui.QCoreApplication, "quit"):
            c.done()
        self.assertEqual(c.state, ui.STATE_QUITTING)


class ActivationTests(unittest.TestCase):
    def open_flow(self, **kwargs) -> Flow:
        flow = Flow(self, launch=LAUNCH_OPEN, **kwargs)
        flow.c.start()
        flow.attempt()
        return flow

    def test_read_message_reads(self) -> None:
        flow = self.open_flow()
        flow.c.handle_activation({"cmd": "activate", "run": None, "now": False, "read": True})
        settle()
        self.assertEqual(flow.reading.current_tab(), hud.TAB_BRIEFING)
        self.assertEqual(len(flow.c.player.starts), 1)

    def test_old_now_message_still_reads(self) -> None:
        flow = self.open_flow()
        flow.c.handle_activation({"cmd": "activate", "run": None, "now": True})
        settle()
        self.assertEqual(len(flow.c.player.starts), 1)
        flow.c.handle_activation({"cmd": "activate", "run": None, "now": True})   # already playing
        self.assertEqual(len(flow.c.player.starts), 1)

    def test_empty_message_is_open(self) -> None:
        flow = self.open_flow()
        said = len(flow.voice.said)
        with mock.patch.object(ui, "force_foreground") as forced:
            flow.c.handle_activation({"cmd": "activate"})
        forced.assert_called()
        self.assertEqual(flow.c.player.starts, [])
        self.assertEqual(len(flow.voice.said), said)   # visible window: no greeting

    def test_empty_message_brings_a_snoozed_classic_prompt_back(self) -> None:
        flow = Flow(self, expected_run="AM", classic=True)
        flow.c.start()
        flow.c.later(10)
        self.assertEqual(flow.c.state, ui.STATE_SNOOZED)
        flow.c.handle_activation({"cmd": "activate", "run": None, "now": False})
        settle()
        self.assertEqual(flow.c.state, ui.STATE_PROMPT)
        self.assertTrue(flow.c.window.isVisible())

    def test_read_message_reads_from_the_classic_prompt_and_its_snooze(self) -> None:
        # --read replaces the older --now: on the classic prompt (or its snooze) it reads, as now:true did.
        for snoozed in (False, True):
            with self.subTest(snoozed=snoozed):
                flow = Flow(self, expected_run="AM", classic=True)
                flow.c.start()
                flow.attempt(flow.page)
                if snoozed:
                    flow.c.later(10)
                    self.assertEqual(flow.c.state, ui.STATE_SNOOZED)
                else:
                    self.assertEqual(flow.c.state, ui.STATE_PROMPT)
                with mock.patch.object(ui, "force_foreground"):
                    flow.c.handle_activation({"cmd": "activate", "run": None, "now": False, "read": True})
                settle()
                self.assertEqual(flow.c.state, ui.STATE_READING)
                self.assertEqual(flow.reading.current_tab(), hud.TAB_BRIEFING)
                self.assertEqual(len(flow.c.player.starts), 1)
                self.assertEqual(flow.voice.said, [])

    def test_open_message_leaves_the_classic_prompt_with_a_greeting(self) -> None:
        flow = Flow(self, expected_run="AM", classic=True)
        flow.c.start()
        flow.attempt(flow.page)
        flow.c.handle_activation({"cmd": "activate", "run": None, "now": False, "open": True})
        settle()
        self.assertEqual(flow.c.state, ui.STATE_READING)
        self.assertEqual(flow.reading.current_tab(), hud.TAB_JARVIS)
        self.assertEqual(flow.voice.kinds(), ["greeting"])
        self.assertEqual(flow.c.player.starts, [])

    def test_minimized_window_greets_again_after_two_minutes_only(self) -> None:
        flow = self.open_flow()
        c = flow.c
        c.window.showMinimized()
        settle()
        said = len(flow.voice.said)
        c.handle_activation({"cmd": "activate", "open": True})
        settle()
        self.assertFalse(c.window.isMinimized())
        self.assertEqual(len(flow.voice.said), said)               # greeted less than 2 minutes ago
        c.window.showMinimized()
        settle()
        c._last_greeting_at = time.monotonic() - 121
        c.handle_activation({"cmd": "activate", "open": True})
        settle()
        self.assertEqual(flow.voice.kinds()[-1], "greeting")

    def test_taskbar_restore_never_greets(self) -> None:
        flow = self.open_flow()
        c = flow.c
        c.window.showMinimized()
        settle()
        c._last_greeting_at = time.monotonic() - 500
        said = len(flow.voice.said)
        c.window.showNormal()   # the taskbar button
        settle()
        c._on_tray_show()
        settle()
        self.assertEqual(len(flow.voice.said), said)

    def test_open_into_a_visible_window_announces_a_pending_new_briefing(self) -> None:
        flow = Flow(self, launch=LAUNCH_OPEN, greet=False)
        flow.locked = True
        flow.c.start()
        flow.attempt()
        self.assertEqual(flow.voice.said, [])                       # held: locked
        flow.c._held_key = None                                     # (as if the hold was dropped)
        flow.locked = False
        flow.c.handle_activation({"cmd": "activate", "open": True})
        self.assertEqual(flow.voice.said, [("announce", ALONE, f"announce:{KEY}")])


class WithoutRunStateTests(unittest.TestCase):
    def test_new_and_once_only_in_memory(self) -> None:
        flow = Flow(self, launch=LAUNCH_OPEN, run_state=False)
        c = flow.c
        c.start()
        c._greeting_timer.timeout.emit()
        flow.attempt()
        self.assertEqual(flow.voice.kinds(), ["greeting", "announce"])
        self.assertTrue(flow.reading.is_new_briefing())
        c.handle_activation({"cmd": "activate", "open": True})
        flow.attempt()
        self.assertEqual(flow.voice.kinds(), ["greeting", "announce"])   # once
        flow.reading.set_tab(hud.TAB_BRIEFING)
        settle()
        self.assertFalse(flow.reading.is_new_briefing())
        self.assertIn(KEY, c._viewed_keys)
        self.assertFalse((flow.root / "runstate.json").exists())


class LogPrivacyTests(unittest.TestCase):
    def test_logs_never_hold_what_jarvis_says(self) -> None:
        flow = Flow(self, launch=LAUNCH_OPEN)
        with self.assertLogs("briefing_reader", level="DEBUG") as logs:
            flow.c.start()
            flow.attempt()
            flow.reading.set_tab(hud.TAB_BRIEFING)
            settle()
        text = "\n".join(logs.output)
        self.assertIn("Greeting ", text)                 # the id only
        self.assertIn(f"Announced the {KEY} briefing", text)
        for line in persona.GREETINGS:
            self.assertNotIn(line.text("sir"), text)
        self.assertNotIn("ready to view", text)
        self.assertNotIn(", sir", text)


if __name__ == "__main__":
    logging.disable(logging.NOTSET)
    unittest.main()
