"""The approved motion prototype, part B: the voice-reactive orb (its spikes and boost follow edge-tts's word
boundaries or the Windows voice's envelope and stop when the speech ends), Home's word-by-word caption, Home's
context tiles with their tethers, and a sent card flying to Approvals.

Offscreen Qt with fakes only: nothing is spoken, sent or read; every name is invented. The tiles show only data
already loaded and never act (a click only opens a screen); speech carries counts and times only; logs carry
kinds and counts; the flight is purely visual (the hold, its Undo and every timing are untouched).
"""

from __future__ import annotations

import logging
import math
import struct
import tempfile
import time
import unittest
import wave
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QEventLoop, QRect, QRectF, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from briefing_reader import alive, context_view, holo, home, hud, offer_view, orb, tts, ui, voice_fx
from briefing_reader.agenda import Deadline
from briefing_reader.gcal import CalendarEvent
from briefing_reader.models import AudioSegment, SectionAudio
from tests.ask_fakes import FakeReader, plan_stream
from tests.test_home_moments import AppCase
from tests.test_offers_app import at, meeting
from tests.test_prep_app import ANSWER, sam_thread
from tests.ui_fakes import GUEST, fonts, settle, wait_for

_app: QApplication | None = None
NOW = datetime(2026, 10, 12, 9, 51)
NAMES = ("Project sync", "Ana Lima", "Budget numbers", "Hackathon", "ana@example.com")


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_alive_voice", "-platform", "offscreen"])
    fonts()


def wait(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def motion(test: unittest.TestCase, level: str = holo.MOTION_LIVELY) -> None:
    holo.set_reduced_motion(False)
    holo.set_motion_level(level)
    test.addCleanup(holo.set_reduced_motion, None)
    test.addCleanup(holo.set_motion_level, holo.MOTION_LIVELY)


FACTS = context_view.ContextFacts(meeting=(datetime(2026, 10, 12, 14, 0), "Project sync with Ana"),
                                  thread=("Ana Lima", "Budget numbers"), deadline=("TOMORROW", "Hackathon entry"),
                                  approvals=2, replies=2)

# --------------------------------------------------------------------------
# The voice's timing (voice_fx, tts)
# --------------------------------------------------------------------------


class VoiceTimingTests(unittest.TestCase):
    def test_segments_join_with_their_pauses(self) -> None:
        audio = SectionAudio("say-1", (
            AudioSegment(Path("a.mp3"), 1000, (), pause_after_ms=500, words=((0, 400, "Good"), (400, 1000, "morning"))),
            AudioSegment(Path("b.mp3"), 800, (), words=((0, 800, "sir"),)),
        ))
        timeline = voice_fx.timeline_from_audio(audio)
        assert timeline is not None
        self.assertEqual(timeline.words, ((0, 400, "Good"), (400, 1000, "morning"), (1500, 2300, "sir")))
        self.assertEqual(timeline.duration_ms, 2300)
        self.assertNotIn("Good", repr(timeline))                      # never the words in a log line
        self.assertIsNone(voice_fx.timeline_from_audio(SectionAudio("say-2", ())))

    def test_the_level_follows_the_word_boundaries(self) -> None:
        timeline = voice_fx.VoiceTimeline(words=((0, 600, "Hello"), (1000, 1600, "there")), duration_ms=1600)
        self.assertGreater(voice_fx.level_at(timeline, 150), 0.3)      # inside a word
        self.assertLess(voice_fx.level_at(timeline, 900), 0.05)        # the pause between words
        self.assertGreater(voice_fx.level_at(timeline, 1150), 0.3)
        self.assertEqual(voice_fx.level_at(timeline, 5000), 0.0)       # after the speech
        self.assertEqual(voice_fx.level_at(timeline, -1), 0.0)
        self.assertEqual(voice_fx.level_at(None, 100), 0.0)

    def test_the_windows_voice_envelope_drives_the_level(self) -> None:
        timeline = voice_fx.VoiceTimeline(duration_ms=100, envelope=(0.0, 1.0, 0.5))
        self.assertAlmostEqual(voice_fx.level_at(timeline, 0), 0.0)
        self.assertAlmostEqual(voice_fx.level_at(timeline, 1000 / 30), 1.0, places=3)
        self.assertAlmostEqual(voice_fx.level_at(timeline, 500 / 30), 0.5, places=3)

    def test_word_times_follow_the_boundaries_else_an_even_pace(self) -> None:
        timeline = voice_fx.VoiceTimeline(words=((0, 300, "Good"), (300, 700, "morning"), (700, 900, "sir"),
                                                 (1200, 1500, "Your"), (1500, 1800, "meeting")), duration_ms=2000)
        words = "Good morning, sir. Your 2:00 meeting".split()
        times = voice_fx.word_times(words, timeline)
        self.assertEqual(times[:4], [0, 300, 700, 1200])
        self.assertEqual(times[5], 1500)
        self.assertTrue(1200 <= times[4] <= 1500)                      # not said as written: between its neighbours
        even = voice_fx.word_times(["one", "two", "three"], voice_fx.VoiceTimeline(duration_ms=900))
        self.assertEqual(even, [0, 300, 600])                          # no boundaries: even over the audio
        self.assertEqual(voice_fx.word_times(["a", "b"], None), [0, voice_fx.FALLBACK_WORD_MS])

    def test_a_wav_gives_its_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "voice.wav"
            rate = 8000
            frames = [0] * (rate // 2) + [int(12000 * math.sin(2 * math.pi * 220 * i / rate)) for i in range(rate // 2)]
            with wave.open(str(path), "wb") as fh:
                fh.setnchannels(1)
                fh.setsampwidth(2)
                fh.setframerate(rate)
                fh.writeframes(struct.pack(f"<{len(frames)}h", *frames))
            levels = voice_fx.wav_envelope(path)
            self.assertEqual(voice_fx.wav_envelope(Path(folder) / "missing.wav"), ())
        self.assertAlmostEqual(len(levels), 30, delta=2)
        self.assertLess(max(levels[:12]), 0.05)                        # the silence
        self.assertGreater(min(levels[18:28]), 0.8)                    # the tone

    def test_a_segment_carries_its_word_spans(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            synth = tts.SpeechSynthesizer(voice="v", rate="+0%", volume="+0%", offline_voice="", out_dir=Path(folder))
            clip = tts._Clip(Path(folder) / "x.mp3", 1000, ((0, "Hi"), (500, "there")), tts.EDGE)
            segment = synth._make_segment(clip, "Hi there", [(0, 0)], 0)
            self.assertEqual(segment.words, ((0, 500, "Hi"), (500, 1000, "there")))
            sapi = tts._Clip(Path(folder) / "y.wav", 900, (), tts.SAPI, envelope=(0.1, 0.9))
            self.assertEqual(synth._make_segment(sapi, "Hi there", [(0, 0)], 0).envelope, (0.1, 0.9))
            synth.close()

class VoiceTimelineTests(unittest.TestCase):
    def test_the_voice_hands_out_the_utterance_timeline_as_it_starts(self) -> None:
        from tests.test_voice_ui import FakeBriefing, FakeUtterancePlayer, FakeWorker
        from briefing_reader.voice_ui import AssistantVoice

        player = FakeUtterancePlayer()
        player.position_ms = lambda: 420                               # type: ignore[attr-defined]
        worker = FakeWorker()
        voice = AssistantVoice(worker=worker, player=player, briefing_player=FakeBriefing, resume_delay_ms=10,
                               tick_ms=20)
        self.addCleanup(voice.shutdown)
        seen: list = []
        voice.speakingChanged.connect(lambda text: seen.append((text, voice.timeline(), voice.position_ms())))
        self.assertEqual((voice.timeline(), voice.position_ms()), (None, -1))      # nothing said
        self.assertTrue(voice.say("ack", "Done, sir."))
        seq = worker.submitted[-1][0]
        words = ((0, 300, "Done"), (300, 700, "sir"))
        voice.on_audio(seq, SectionAudio(f"say-{seq}", (AudioSegment(Path("x.mp3"), 700, (), words=words),)))
        self.assertTrue(wait_for(lambda: bool(seen), 2))
        text, timeline, position = seen[0]
        self.assertEqual(text, "Done, sir.")
        self.assertIsNotNone(timeline)                                 # known before the player has it
        self.assertEqual(timeline.words, words)
        self.assertEqual(position, 420)
        player.end()
        self.assertTrue(wait_for(lambda: voice.timeline() is None, 2))


# --------------------------------------------------------------------------
# The orb's voice spikes and boost
# --------------------------------------------------------------------------


class OrbVoiceTests(unittest.TestCase):
    def make(self, px: int = orb.HERO) -> tuple[orb.OrbWidget, list[float], list[float]]:
        widget = orb.OrbWidget(px)
        self.addCleanup(widget.deleteLater)
        widget.set_voice_room(orb.voice_side(px))
        clock = [20.0]
        level = [0.0]
        widget._now = lambda: clock[0]
        widget.set_voice(lambda: level[0])
        return widget, clock, level

    def run_for(self, widget: orb.OrbWidget, clock: list[float], seconds: float) -> orb.OrbFrame:
        frame = widget.sample()
        for _ in range(max(1, round(seconds / 0.05))):
            clock[0] += 0.05
            frame = widget.sample()
        return frame

    def test_the_spikes_follow_the_voice_and_stop_when_it_ends(self) -> None:
        motion(self)
        widget, clock, level = self.make()
        widget.set_look(orb.SPEAKING)
        frame = self.run_for(widget, clock, 1.0)
        self.assertAlmostEqual(frame.spikes, 1.0, places=3)
        self.assertEqual(len(frame.spike_lens), orb.SPIKES)
        self.assertLess(max(frame.spike_lens), orb.SPIKE_MIN + 0.5)    # silent: the bars rest
        level[0] = 1.0
        loud = self.run_for(widget, clock, 0.4)
        self.assertGreater(max(loud.spike_lens), 20.0)                 # the voice lengthens them
        self.assertLessEqual(max(loud.spike_lens), orb.SPIKE_MAX + 1e-6)
        self.assertAlmostEqual(widget._tw["boost"].value(clock[0]), orb.SPEAK_BOOST)   # the rings boost
        self.assertFalse(widget.grab().isNull())
        widget.set_look(orb.IDLE)                                      # the speech ended
        clock[0] += 0.1
        easing = widget._tw["boost"].value(clock[0])
        self.assertTrue(0.0 < easing < orb.SPEAK_BOOST)                # settles back, never a jump
        ended = self.run_for(widget, clock, 1.0)
        self.assertEqual((ended.spikes, ended.spike_lens), (0.0, ()))
        self.assertEqual(widget._tw["boost"].value(clock[0]), 0.0)

    def test_calm_is_gentler_and_off_has_no_spikes(self) -> None:
        motion(self, holo.MOTION_CALM)
        widget, clock, level = self.make()
        level[0] = 1.0
        widget.set_look(orb.SPEAKING)
        frame = self.run_for(widget, clock, 1.0)
        self.assertLessEqual(max(frame.spike_lens), orb.SPIKE_CALM_MAX + 1e-6)
        self.assertAlmostEqual(widget._tw["boost"].value(clock[0]), orb.SPEAK_BOOST_CALM)
        holo.set_motion_level(holo.MOTION_OFF)
        still, clock2, level2 = self.make()
        level2[0] = 1.0
        still.set_look(orb.SPEAKING)
        frame = self.run_for(still, clock2, 1.0)
        self.assertEqual((frame.spikes, frame.spike_lens), (0.0, ()))
        self.assertEqual(still._tw["boost"].value(clock2[0]), 0.0)     # the orb as it was

    def test_only_the_hero_and_only_with_room(self) -> None:
        motion(self)
        compact, clock, level = self.make(orb.COMPACT)
        level[0] = 1.0
        compact.set_look(orb.SPEAKING)
        self.assertEqual(self.run_for(compact, clock, 1.0).spikes, 0.0)
        hero, clock, _level = self.make()
        hero.set_voice_room(0)
        self.assertEqual(hero.width(), orb.widget_side(orb.HERO))     # no room given: the old widget
        self.assertLess(hero.spike_room(), orb.SPIKE_MAX)
        hero.set_voice_room(orb.voice_side(orb.HERO))
        self.assertEqual(hero.width(), orb.widget_side(orb.HERO))     # idle: the room is not taken
        self.assertEqual((orb.voice_side(orb.HERO) - orb.widget_side(orb.HERO)) % 8, 0)
        self.assertAlmostEqual(hero.spike_room(), orb.SPIKE_MAX)
        hero.set_look(orb.SPEAKING)
        self.assertEqual(hero.width(), orb.voice_side(orb.HERO))      # speaking: it grows for the spikes
        hero.set_look(orb.IDLE)
        self.run_for(hero, clock, 1.0)
        hero.grab()
        settle()
        self.assertEqual(hero.width(), orb.widget_side(orb.HERO))     # faded: back to the halo's room

    def test_without_voice_data_the_synthetic_curve_runs(self) -> None:
        motion(self)
        widget, clock, _level = self.make()
        widget.set_voice(lambda: None)
        widget.set_look(orb.SPEAKING)
        frames = [self.run_for(widget, clock, 0.2) for _ in range(6)]
        self.assertGreater(max(max(frame.spike_lens) for frame in frames), orb.SPIKE_MIN + 2.0)

# --------------------------------------------------------------------------
# The word-by-word caption
# --------------------------------------------------------------------------


class CaptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.view = home.CaptionView()
        self.view.resize(760, 80)
        self.addCleanup(self.view.deleteLater)
        self.view.show()

    def test_each_word_shows_as_the_voice_says_it(self) -> None:
        motion(self)
        position = [-1]
        timeline = voice_fx.VoiceTimeline(words=((0, 300, "Good"), (300, 700, "morning"), (700, 1000, "sir")),
                                          duration_ms=1000)
        view = self.view
        view.say("Good morning, sir.", timeline=timeline, position=lambda: position[0])
        self.assertTrue(view.is_timed())
        self.assertEqual(view.word_times(), [0, 300, 700])
        self.assertEqual(view.accessibleDescription(), "Good morning, sir.")   # the whole line from the start
        self.assertEqual(view.revealed_words(), 0)                    # the audio has not started
        position[0] = 0
        self.assertTrue(wait_for(lambda: view.revealed_words() == 1, 1))
        wait(120)
        self.assertEqual(view.revealed_words(), 1)                    # "morning" waits for its boundary
        position[0] = 350
        self.assertTrue(wait_for(lambda: view.revealed_words() == 2, 1))
        position[0] = 750
        self.assertTrue(wait_for(lambda: view.revealed_words() == 3, 1))
        view.finish()
        self.assertEqual(view.state(), home.HOLDING)

    def test_pages_follow_the_voice_too(self) -> None:
        motion(self)
        text = ("Three things tomorrow, sir. Your AI-Trader check-in at 10, the Project Development check-in at "
                "12:30 on Google Meet, and your LING essay draft is due by evening.")
        words = text.split()
        timeline = voice_fx.VoiceTimeline(words=tuple((i * 100, i * 100 + 100, w) for i, w in enumerate(words)),
                                          duration_ms=100 * len(words))
        position = [0]
        view = self.view
        view.say(text, timeline=timeline, position=lambda: position[0])
        self.assertGreater(len(view.pages()), 1)
        position[0] = 100 * len(words)
        self.assertTrue(wait_for(lambda: view.page_index() == len(view.pages()) - 1
                                 and view.revealed_words() == len(view.page_text().split()), 3))
        self.assertEqual(view.accessibleDescription(), " ".join(view.pages()))

    def test_no_boundaries_an_even_pace_over_the_line(self) -> None:
        motion(self)
        view = self.view
        view.say("One two three.", timeline=voice_fx.VoiceTimeline(duration_ms=900), position=lambda: 0)
        self.assertEqual(view.word_times(), [0, 300, 600])
        view.say("One two three.")                                    # no timeline at all: the estimate
        self.assertFalse(view.is_timed())
        self.assertEqual(view._interval_ms(), round(1000 / home.SPOKEN_WPS))

    def test_motion_off_shows_the_whole_line_at_once(self) -> None:
        motion(self, holo.MOTION_OFF)
        view = self.view
        view.say("One two three four.", timeline=voice_fx.VoiceTimeline(duration_ms=900), position=lambda: 0)
        self.assertEqual(view.revealed_words(), 4)
        self.assertFalse(view._frame.isActive())
        self.assertEqual(view.accessibleDescription(), "One two three four.")

# --------------------------------------------------------------------------
# Home's context tiles: the data
# --------------------------------------------------------------------------


def fake_controller(*, bundle: bool, pending: int = 0, events: list | None = None,
                    deadlines: list | None = None) -> SimpleNamespace:
    waiting = (SimpleNamespace(who="Ana Lima", subject="Budget numbers"),
               SimpleNamespace(who="Sam Example", subject="Newer thread"))
    fake = SimpleNamespace(
        _now=lambda: NOW, _agenda_status=ui._AGENDA_OK, _agenda_events=events if events is not None else [
            CalendarEvent("Lab meeting", start=NOW + timedelta(hours=6)),
            CalendarEvent("Project sync", start=NOW + timedelta(minutes=69)),
            CalendarEvent("Yesterday's thing", start=NOW - timedelta(hours=20)),
            CalendarEvent("All day thing", all_day_start=NOW.date())],
        productivity=SimpleNamespace(followups=lambda: SimpleNamespace(off=False, waiting=waiting)) if bundle else None,
        _followups_section_on=lambda: bundle,
        config=SimpleNamespace(agenda=SimpleNamespace(deadline_days=14, deadline_keywords=("due",))),
        _page_deadlines=deadlines if deadlines is not None else [
            Deadline("Hackathon entry", NOW + timedelta(days=1)), Deadline("Old one", NOW - timedelta(days=2))],
        _pending_actions=lambda: [object()] * pending, state=ui.STATE_READING, _context_tiles=[],
        _context_timer=QTimer())
    fake._context_facts = lambda: ui.AppController._context_facts(fake)
    return fake


class ContextDataTests(unittest.TestCase):
    def test_tiles_come_in_the_order_they_are_named_and_skip_empty_sources(self) -> None:
        tiles = context_view.context_tiles(FACTS)
        self.assertEqual([tile.kind for tile in tiles], [context_view.KIND_MEETING, context_view.KIND_THREAD,
                                                         context_view.KIND_DEADLINE, context_view.KIND_APPROVALS])
        self.assertEqual([tile.amber for tile in tiles], [False, False, False, True])   # amber: a decision waits
        self.assertEqual(tiles[3].title, "2 waiting for your OK")
        self.assertEqual([tile.target for tile in tiles], [context_view.TARGET_DAY, context_view.TARGET_FOLLOWUPS,
                                                           context_view.TARGET_DAY, context_view.TARGET_APPROVALS])
        only = context_view.context_tiles(context_view.ContextFacts(deadline=("3D", "Essay")))
        self.assertEqual([tile.kind for tile in only], [context_view.KIND_DEADLINE])
        self.assertEqual(only[0].meta, "Due in 3 days")
        self.assertEqual(context_view.context_tiles(context_view.ContextFacts()), [])
        self.assertNotIn("Ana", repr(FACTS) + repr(tiles))             # reprs (logs) carry no names

    def test_the_spoken_sentence_has_counts_and_times_only(self) -> None:
        facts = context_view.ContextFacts(meeting=FACTS.meeting, thread=FACTS.thread, replies=2)
        self.assertEqual(context_view.spoken_context(facts, "sir"),
                         "Your next meeting is at 2 PM, and two replies are waiting, sir.")
        self.assertEqual(context_view.spoken_context(FACTS), "Your next meeting is at 2 PM, and two proposals are "
                                                             "waiting for your OK.")
        self.assertEqual(context_view.spoken_context(context_view.ContextFacts(approvals=2)),
                         "Two proposals are waiting for your OK.")
        self.assertEqual(context_view.spoken_context(context_view.ContextFacts()), "")
        for spoken in (context_view.spoken_context(facts, "sir"), context_view.spoken_context(FACTS)):
            self.assertFalse([name for name in NAMES + ("Hackathon entry", "Sam") if name in spoken], spoken)

    def test_the_controller_uses_only_loaded_data_and_the_bundle(self) -> None:
        on = ui.AppController._context_facts(fake_controller(bundle=True, pending=1))
        self.assertEqual(on.meeting, (NOW + timedelta(minutes=69), "Project sync"))   # the next one today
        self.assertEqual(on.thread, ("Ana Lima", "Budget numbers"))                   # the oldest waiting
        self.assertEqual((on.replies, on.approvals), (2, 1))
        self.assertEqual(on.deadline, ("TOMORROW", "Hackathon entry"))                # the nearest, not overdue
        off = ui.AppController._context_facts(fake_controller(bundle=False))
        self.assertIsNone(off.thread)                                                  # bundle off: no thread
        self.assertNotIn(context_view.KIND_THREAD, [tile.kind for tile in context_view.context_tiles(off)])
        self.assertNotIn(context_view.KIND_APPROVALS, [tile.kind for tile in context_view.context_tiles(off)])
        empty = ui.AppController._context_facts(fake_controller(bundle=False, events=[], deadlines=[]))
        self.assertEqual(context_view.context_tiles(empty), [])
        fake = fake_controller(bundle=True, pending=2)
        spoken = ui.AppController._greeting_context(fake)
        self.assertEqual(spoken, "Your next meeting is at 11 AM, and two proposals are waiting for your OK.")
        self.assertFalse([name for name in NAMES if name in spoken])

    def test_the_log_names_kinds_and_counts_only(self) -> None:
        fake = fake_controller(bundle=True, pending=2)
        with self.assertLogs("briefing_reader.ui", level=logging.INFO) as logs:
            ui.AppController._queue_context_tiles(fake, True)
        text = "\n".join(logs.output)
        self.assertIn("Home context: 4 tile(s) (meeting, thread, deadline, approvals)", text)
        self.assertFalse([name for name in NAMES + ("Hackathon entry",) if name in text])
        self.assertEqual(len(fake._context_tiles), 4)
        self.assertTrue(fake._context_timer.isActive())               # they wait for the line to start
        fake._context_timer.stop()

# --------------------------------------------------------------------------
# Home's context tiles: on screen
# --------------------------------------------------------------------------


class ContextViewTests(unittest.TestCase):
    def window(self, size: tuple[int, int] = (1120, 700)) -> ui.BriefingWindow:
        window = ui.BriefingWindow(10, 30)
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.hide)
        window.set_clock(lambda: NOW)
        window.reading.set_ask_available(True)
        window.reading.set_greeting("Good morning, sir.")
        window.show_reading_view()
        window.resize(*size)
        window.show()
        settle()
        wait(60)
        return window

    def landed(self, reading) -> None:
        context = reading.stage.context
        context.set_clock(lambda: time.monotonic() + 30.0)          # every entrance over (the clock still runs)
        context._tick()
        settle()

    def rect_in(self, widget: QWidget, window: QWidget) -> QRectF:
        return QRectF(QRect(widget.mapTo(window, widget.rect().topLeft()), widget.size()))

    def test_tiles_keep_clear_of_the_ask_bar_the_caption_and_the_side_tile(self) -> None:
        motion(self)
        for size in ((1120, 700), (900, 600)):
            for offer in (False, True):
                window = self.window(size)
                reading = window.reading
                if offer:
                    entry = reading.conversation.add(hud.ROLE_JARVIS, "Shall I plan your day, sir?",
                                                     link_text="Plan my day", link_id="offer:1")
                    reading.set_offers([offer_view.OfferInfo(1, "plan", "offer:1", offer_view.PLAN_KICKER,
                                                             offer_view.PLAN_TITLE, "Plan my day",
                                                             (offer_view.PLAN_META,), "Shall I plan your day, sir?",
                                                             entry)])
                    settle()
                    wait(30)
                shown = reading.show_context(context_view.context_tiles(FACTS))
                self.landed(reading)
                tiles = reading.stage.context.tiles()
                tag = f"{size} offer={offer}"
                self.assertEqual(len(tiles), shown, tag)
                self.assertGreaterEqual(shown, 2 if offer else 4, tag)       # an offer keeps its place
                kinds = [tile.info.kind for tile in tiles]
                self.assertIn(context_view.KIND_APPROVALS, kinds, tag)        # the amber one always stays
                clear = [reading.command_bar, reading.captions, reading.stage.transcript_button]
                if offer:
                    self.assertTrue(reading.offer_tile.isVisible(), tag)
                    clear.append(reading.offer_tile)
                win = QRectF(window.rect())
                for tile in tiles:
                    self.assertTrue(tile.isVisible(), tag)
                    rect = self.rect_in(tile, window)
                    self.assertTrue(win.contains(rect), tag)
                    for other in clear:
                        self.assertFalse(rect.intersects(self.rect_in(other, window)), f"{tag} {other}")
                    center = QRectF(reading.hero.geometry()).center()
                    radius = reading.hero.orb_size() / 2
                    local = QRectF(tile.geometry())
                    nearest_x = max(local.left(), min(center.x(), local.right()))
                    nearest_y = max(local.top(), min(center.y(), local.bottom()))
                    self.assertGreater(math.hypot(nearest_x - center.x(), nearest_y - center.y()), radius, tag)
                for rim, anchor, amber in reading.stage.context.tether_lines():
                    self.assertGreater(math.hypot(anchor.x() - rim.x(), anchor.y() - rim.y()), 10.0, tag)
                window.hide()
                settle()

    def test_a_click_only_opens_its_screen(self) -> None:
        motion(self)
        window = self.window()
        reading = window.reading
        acted: list = []
        for signal in (reading.approveClicked, reading.denyClicked, reading.homeLink, reading.undoAction):
            signal.connect(lambda *args, store=acted: store.append(args))
        reading.show_context(context_view.context_tiles(FACTS))
        self.landed(reading)
        by_kind = {tile.info.kind: tile for tile in reading.stage.context.tiles()}
        by_kind[context_view.KIND_MEETING].click()
        settle()
        self.assertTrue(reading.day_drawer.is_open() or reading.day_drawer.isVisible())   # the Day view
        self.assertEqual(reading.current_tab(), ui.SCREEN_HOME)
        reading.day_drawer.close_drawer()
        reading.show_context(context_view.context_tiles(FACTS))
        self.landed(reading)
        approvals = next(tile for tile in reading.stage.context.tiles() if tile.info.amber)
        approvals.click()
        settle()
        self.assertEqual(reading.current_tab(), ui.SCREEN_APPROVALS)
        self.assertEqual(acted, [])                                   # nothing approved, denied or opened

    def test_they_fade_after_their_time_on_typing_and_off_home(self) -> None:
        motion(self)
        window = self.window()
        reading = window.reading
        context = reading.stage.context
        infos = context_view.context_tiles(FACTS)
        self.assertEqual(context_view.HOLD_S, 60.0)
        context.show_tiles(infos, hold_s=0.05)                        # ~60 s in the app
        self.assertTrue(wait_for(lambda: not context.is_showing(), 3))
        reading.show_context(infos)
        self.landed(reading)
        reading._on_typing(True)                                      # the owner types
        self.assertIn(context.phase(), ("leaving", ""))
        self.assertTrue(wait_for(lambda: not context.is_showing(), 2))
        reading._on_typing(False)
        reading.show_context(infos)
        self.landed(reading)
        reading.show_request("what's on tomorrow?")                   # the owner asks
        self.assertTrue(wait_for(lambda: not context.is_showing(), 2))
        reading.show_context(infos)
        self.landed(reading)
        reading.set_tab(ui.SCREEN_APPROVALS)                          # the owner leaves Home
        self.assertTrue(wait_for(lambda: not context.is_showing(), 2))
        self.assertEqual(reading.show_context(infos), 0)              # never off Home

    def test_they_fly_and_tether_lively_and_appear_still_without_motion(self) -> None:
        motion(self)
        window = self.window()
        reading = window.reading
        context = reading.stage.context
        start = time.monotonic()
        context.set_clock(lambda: start)
        reading.show_context(context_view.context_tiles(FACTS))
        self.assertEqual(context.phase(), "entering")
        self.assertEqual(context.flight(0, start), 0.0)
        self.assertFalse(context.tiles()[0].isVisible())              # still in flight
        later = start + 0.3 + context_view.FLY_MS / 1000.0 + 0.01
        self.assertEqual(context.flight(0, later), 1.0)
        self.assertEqual(context.tether_progress(0, later), 1.0)
        self.assertFalse(window.grab().isNull())
        context.dismiss()
        holo.set_motion_level(holo.MOTION_OFF)
        reading.show_context(context_view.context_tiles(FACTS))
        settle()
        self.assertEqual(context.phase(), "steady")
        self.assertTrue(all(tile.isVisible() for tile in context.tiles()))
        self.assertFalse(context._timer.isActive())                  # static: nothing repaints

# --------------------------------------------------------------------------
# A card flying to Approvals
# --------------------------------------------------------------------------


class CardFlightTests(unittest.TestCase):
    def test_the_flight_is_visual_only(self) -> None:
        motion(self)
        parent = QWidget()
        self.addCleanup(parent.deleteLater)
        parent.resize(800, 600)
        card = QLabel("Re: Budget numbers", parent)
        card.setGeometry(300, 300, 240, 80)
        parent.show()
        settle()
        flight = alive.CardFlight(parent)
        target = QRectF(10, 120, 48, 48)
        self.assertTrue(flight.fly(card, target))
        self.assertTrue(flight.is_running())
        self.assertEqual((card.geometry(), card.isVisible()), (QRect(300, 300, 240, 80), True))   # untouched
        start = flight._flights[0][3]
        rect, opacity = flight.frame(0, start)
        self.assertEqual((rect, opacity), (QRectF(300, 300, 240, 80), 1.0))
        rect, opacity = flight.frame(0, start + alive.FLIGHT_MS / 1000.0)
        self.assertAlmostEqual(rect.center().x(), target.center().x(), delta=1.0)
        self.assertAlmostEqual(rect.width(), 240 * 0.15, delta=1.0)
        self.assertAlmostEqual(opacity, 0.0)
        self.assertTrue(wait_for(lambda: not flight.is_running(), 2))
        self.assertTrue(card.isVisible())
        holo.set_motion_level(holo.MOTION_OFF)
        self.assertFalse(flight.fly(card, target))                   # off: nothing flies

    def test_file_card_needs_the_brief_on_home(self) -> None:
        window = ui.BriefingWindow(10, 30)
        self.addCleanup(window.deleteLater)
        window.show_reading_view()
        window.show()
        settle()
        self.assertFalse(window.reading.file_card("a1"))              # no brief: nothing moves


class SentCardFlightAppTests(AppCase):
    """The brief's reply card on Home: Send, the hidden hold with its Undo exactly as before, then the card flies
    to Approvals as the hold ends; nothing is sent before that and the timing is the owner's setting."""

    def test_the_hold_and_its_undo_are_unchanged_and_the_card_flies_after(self) -> None:
        motion(self)
        reader = FakeReader("work", {"thr0000777": sam_thread()}, found_for={GUEST: ["thr0000777"]})
        app = self.make(at(13, 30), plan_stream(ANSWER), work=[meeting()], reader=reader)
        c, reading = self.c, self.reading
        self.tick(15)
        reading.offer_tile.commit_button.click()
        self.assertTrue(wait_for(lambda: not c.ask.busy, 10), "the brief ran")
        settle()
        stage = reading.brief_stage
        held = stage.held()
        self.assertEqual(len(held), 1)
        card = held[0]
        waiting = card.needs_tick()
        if waiting:
            card.ticks.tick(waiting[0]).click()
            settle()
        card.approve_button.click()
        settle()
        hold = c._send_hold
        self.assertIsNotNone(hold)
        self.assertEqual((card.status(), card.undo_text()), (hud.CARD_SENT, "Undo"))   # Sent + Undo, as always
        self.assertAlmostEqual(hold.deadline - hold.started, c.config.actions.undo_seconds, places=3)
        self.assertEqual(reading.card_flight.flights(), 0)            # nothing flies during the hold
        self.assertEqual(stage.held(), [card])
        self.assertEqual([sender.sent for sender in app.senders.values()], [[] for _ in app.senders])
        hold.deadline = time.monotonic() - 0.01                       # the owner's hold time is over
        c._on_send_hold_tick()
        settle()
        self.assertIsNone(c._send_hold)
        self.assertEqual(stage.held(), [])                            # back on Approvals ...
        self.assertTrue(reading.approvals.widget().isAncestorOf(card))
        self.assertEqual(reading.card_flight.flights(), 1)            # ... flying there
        self.assertTrue(wait_for(lambda: any(sender.sent for sender in app.senders.values()), 5))   # sent once
        self.assertEqual(sum(len(sender.sent) for sender in app.senders.values()), 1)


if __name__ == "__main__":
    unittest.main()
