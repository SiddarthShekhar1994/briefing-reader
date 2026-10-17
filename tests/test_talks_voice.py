"""Jarvis talks, the voice half in the app (ui.AppController with fakes, offscreen).

* The real voice (voice_ui.AssistantVoice) is built only by start(): never when a voice factory is
  given, never with [assistant] speak = false; its "tts-say" worker uses the module-level
  SpeechSynthesizer (a fake here) with the session's "say" folder, its own player at the
  controller's volume (a fake here), and shutdown waits for it.
* While Jarvis speaks the orb says SPEAKING and the speech line shows his words; then the
  briefing's state and line come back. Play, Skip, a section and Read everything stop him.
* The header's speaker button (assistant screen only, hidden with speak = false), Ctrl+M, and the
  header still fitting at 900 px with both clocks.
* Ask in the conversation: the request as a YOU entry, the JARVIS tab, "One moment, sir." while
  it plans, the whole answer as one JARVIS entry (a 600-character one too), the spoken reply
  (guarded), failures and Cancel.
* After an approved card: nothing at the click or during a Send's hidden hold, exactly one sentence
  after the executor's result (Move, Email, Calendar; failed, unknown, not sent), and none of it
  in the logs.

Nothing reaches Notion, Google, Gmail, edge-tts, Windows SAPI or claude.exe; every name, address
and text is invented.
"""

from __future__ import annotations

import dataclasses
import logging
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from briefing_reader import ask_ui, home, hud, orb, speech, ui, voice_ui
from briefing_reader.actions import SOURCE_ASK, parse_action_line
from briefing_reader.ask import planner as ask_planner
from briefing_reader.ask.planner import AskOutcome
from briefing_reader.config import CalendarConfig, load_config
from briefing_reader.gcal import CalendarUnknownOutcome, NotAllowed
from briefing_reader.gmail import GmailUnknownOutcome
from briefing_reader.models import LAUNCH_OPEN, SectionAudio
from briefing_reader.notion_client import FixtureSession, NotionClient
from tests.ask_fakes import FakeClock, FakeProcess, stream
from tests.ui_fakes import (
    B_CALENDAR,
    COMMAND,
    EMAIL_LINE,
    FIXTURE,
    GUEST,
    MOVE_LINE,
    NOW,
    SAY,
    AppHarness,
    FakePlayer,
    RecordingVoice,
    fonts,
    plan,
    settle,
    wait_for,
)

_app: QApplication | None = None
TRUSTED = "ana@example.edu"                       # a trusted-domain recipient: no Edit needed before Send
MAIL_LINE = EMAIL_LINE.replace(GUEST, TRUSTED)
BODY_MARK = "Moved to Friday at 2 PM"             # a line of EMAIL_LINE's body


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_talks_voice", "-platform", "offscreen"])
    fonts()
    ask_planner.cli.forget_probes()


def ask_card(line: str):
    return dataclasses.replace(parse_action_line(line), source=SOURCE_ASK)


class FakeSynth:
    """ui.SpeechSynthesizer stand-in: records how it was made and what it was asked; never a sound."""

    made: list[dict] = []
    sections: list[str] = []
    threads: set[str] = set()
    gate: threading.Event | None = None     # set: synthesis waits for it (a worker busy at shutdown)

    def __init__(self, **kwargs) -> None:
        FakeSynth.made.append(kwargs)
        FakeSynth.threads.add(threading.current_thread().name)

    def synthesize_section(self, section) -> SectionAudio:
        from briefing_reader.models import AudioSegment

        FakeSynth.sections.append(section.key)
        if FakeSynth.gate is not None:
            FakeSynth.gate.wait(5)
        return SectionAudio(section.key, (AudioSegment(Path(f"{section.key}.wav"), 500, ((0, 0),)),), "edge")

    def close(self) -> None:
        pass


class FakeUtterancePlayer(QObject):
    finished = Signal(int)
    made: list[float] = []

    def __init__(self, parent=None, *, volume: float = 1.0) -> None:
        super().__init__(parent)
        FakeUtterancePlayer.made.append(volume)
        self.seq: int | None = None
        self.played: list[int] = []

    @property
    def playing(self) -> bool:
        return self.seq is not None

    def play(self, seq: int, _audio) -> None:
        self.seq = seq
        self.played.append(seq)

    def stop(self) -> None:
        self.seq = None

    def end(self) -> None:
        seq, self.seq = self.seq, None
        self.finished.emit(seq)


class Built:
    """A controller started for real (OPEN), with the real voice over fakes when ``speak``."""

    def __init__(self, test: unittest.TestCase, *, speak: bool = True, voice_factory=None) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        test.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        base = load_config(root, environ={"LOCALAPPDATA": str(root / "local")})
        session = FixtureSession(FIXTURE)
        self.config = dataclasses.replace(
            base, page_id=session.page_id, audio_root=root / "audio", calendar=CalendarConfig(enabled=False),
            assistant=dataclasses.replace(base.assistant, speak=speak, greeting_context=False))
        client = NotionClient("", session=session, notion_version=self.config.notion_version)
        FakeSynth.made, FakeSynth.sections, FakeSynth.threads, FakeSynth.gate = [], [], set(), None
        FakeUtterancePlayer.made = []
        patches = [mock.patch.object(ui, "BriefingPlayer", FakePlayer), mock.patch.object(ui, "SpeechSynthesizer", FakeSynth),
                   mock.patch.object(voice_ui, "UtterancePlayer", FakeUtterancePlayer)]
        for patch in patches:
            patch.start()
            test.addCleanup(patch.stop)
        self.c = ui.AppController(self.config, client, expected_run=None, now_mode=False, volume=0.0,
                                  now_func=lambda: NOW, launch=LAUNCH_OPEN, voice_factory=voice_factory,
                                  locked=lambda: False)
        self.c._start_tts = lambda: None                    # the briefing's worker is not under test
        self.c._start_fetch = lambda *args, **kwargs: None   # never Notion
        test.addCleanup(self.close)

    def close(self) -> None:
        c = self.c
        if not c._shut_down:
            with mock.patch.object(ui, "_remove_audio_dir_after"):
                c.shutdown()
        if c._say_worker is not None:
            c._say_worker.join(5)
        c.window.deleteLater()
        settle()


class VoiceBuildTests(unittest.TestCase):
    def test_start_builds_the_real_voice_over_the_module_level_synthesizer(self) -> None:
        built = Built(self)
        c = built.c
        self.assertIsInstance(c.voice, speech.SilentVoice)          # nothing before start()
        c.start()
        self.assertIsInstance(c.voice, voice_ui.AssistantVoice)
        self.assertEqual(c._say_worker.name, "tts-say")
        self.assertTrue(wait_for(lambda: FakeSynth.sections == ["say-1"]), "the greeting was synthesized")
        self.assertEqual(FakeSynth.threads, {"tts-say"})
        made = FakeSynth.made[0]
        self.assertEqual(made["out_dir"], c._audio_dir / "say")
        self.assertEqual((made["voice"], made["rate"], made["offline_voice"]),
                         (c.config.voice.voice, c.config.voice.rate, c.config.voice.offline_voice))
        self.assertEqual(FakeUtterancePlayer.made, [0.0])           # the controller's volume
        player = c.voice._player
        self.assertTrue(wait_for(lambda: player.played == [1]), "the greeting plays")
        reading = c.window.reading
        greeting = reading.conversation.entries()[0][2]
        self.assertEqual(reading.orb.state(), hud.ORB_SPEAKING)
        self.assertEqual(reading.speech.text(), greeting)
        # Home: the hero orb speaks, the header says so and the caption pages what is said.
        self.assertEqual(reading.hero.state(), orb.SPEAKING)
        self.assertEqual(reading.state_word(), "SPEAKING")
        self.assertEqual(reading.captions.state(), home.SPEAKING)
        self.assertEqual(" ".join(reading.captions.pages()), " ".join(greeting.split()))
        self.assertTrue(c.voice.speaking)
        player.end()
        settle()
        self.assertNotEqual(reading.orb.state(), hud.ORB_SPEAKING)
        self.assertNotEqual(reading.speech.text(), greeting)
        self.assertNotEqual(reading.hero.state(), orb.SPEAKING)
        self.assertEqual(reading.captions.state(), home.HOLDING)          # the last page holds, then rests
        self.assertTrue(c.window.header.speaker_visible())

    def test_shutdown_waits_for_tts_say(self) -> None:
        built = Built(self)
        c = built.c
        gate = FakeSynth.gate = threading.Event()   # the greeting's synthesis is still running at shutdown
        c.start()
        worker = c._say_worker
        self.assertTrue(wait_for(lambda: FakeSynth.sections == ["say-1"]))
        with mock.patch.object(ui, "_remove_audio_dir_after") as cleanup:
            c.shutdown()
        self.assertTrue(cleanup.called)
        waited, folder = cleanup.call_args[0]
        self.assertIn(worker, waited)
        self.assertEqual(folder, c._audio_dir)
        gate.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())

    def test_shutdown_hides_the_window_before_stopping_the_voices_player(self) -> None:
        # QMediaPlayer.stop() has (rarely) hung at shutdown: the window is gone before either player
        # stops, and nothing can be said once the app is closing.
        built = Built(self)
        c = built.c
        c.start()
        seen: list[bool] = []
        real = c.voice._player.stop
        c.voice._player.stop = lambda: (seen.append(c.window.isVisible()), real())[-1]
        with mock.patch.object(ui, "_remove_audio_dir_after"):
            c.shutdown()
        self.assertTrue(seen)
        self.assertEqual(set(seen), {False})
        self.assertFalse(c._say(speech.KIND_RESULT, "Done, sir."))

    def test_the_audio_folder_goes_after_both_workers(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name) / "session-1"
        (folder / "say").mkdir(parents=True)
        gates = [threading.Event(), threading.Event()]
        workers = [threading.Thread(target=gate.wait, args=(5,), daemon=True) for gate in gates]
        for worker in workers:
            worker.start()
        ui._remove_audio_dir_after(workers, folder)
        gates[0].set()
        time.sleep(0.2)
        self.assertTrue(folder.exists())                               # "tts-say" still runs
        gates[1].set()
        self.assertTrue(wait_for(lambda: not folder.exists(), 5))

    def test_never_with_an_injected_voice_or_speak_off(self) -> None:
        injected = RecordingVoice()
        built = Built(self, voice_factory=lambda _c: injected)
        with mock.patch.object(ui.AppController, "_create_voice", side_effect=AssertionError("built")):
            built.c.start()
        self.assertIs(built.c.voice, injected)
        self.assertIsNone(built.c._say_worker)
        off = Built(self, speak=False)
        with mock.patch.object(ui.AppController, "_create_voice", side_effect=AssertionError("built")):
            off.c.start()
        self.assertIsInstance(off.c.voice, speech.SilentVoice)
        self.assertFalse(off.c.window.header.speaker_visible())
        self.assertEqual(FakeSynth.made, [])

    def test_a_voice_that_cannot_be_built_leaves_the_words_on_screen(self) -> None:
        built = Built(self)
        with mock.patch.object(voice_ui, "AssistantVoice", side_effect=RuntimeError("no audio")), \
                self.assertLogs("briefing_reader.ui", level="WARNING") as logs:
            built.c.start()
        self.assertIsInstance(built.c.voice, speech.SilentVoice)
        self.assertTrue(any("Could not set up Jarvis's voice (RuntimeError)" in line for line in logs.output))
        self.assertEqual(len(built.c.window.reading.conversation.entries()), 1)   # the greeting, shown


class SpeakingStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = AppHarness(self, ask=False)
        self.app.reading(autoplay=False)
        self.c = self.app.c

    def test_orb_and_speech_line_follow_the_utterance(self) -> None:
        c, reading = self.c, self.c.window.reading
        before_orb, before_line = reading.orb.state(), reading.speech.text()
        c._on_voice_speaking("Good morning, sir.")
        self.assertEqual((reading.orb.state(), reading.speech.text()), (hud.ORB_SPEAKING, "Good morning, sir."))
        c._update_reading_controls()                                   # e.g. a player signal meanwhile
        self.assertEqual(reading.orb.state(), hud.ORB_SPEAKING)
        c._on_item_changed(1, 0)                                       # the briefing's line waits
        self.assertEqual(reading.speech.text(), "Good morning, sir.")
        self.assertFalse(reading.transcript_panel.live.is_live())     # LIVE stays the briefing's
        c._on_voice_speaking("")
        self.assertEqual(reading.orb.state(), before_orb)
        self.assertEqual(reading.speech.text(), c._item_text(1, 0) or before_line)

    def test_the_owners_playback_stops_jarvis(self) -> None:
        c, voice = self.c, self.app.voice
        c.toggle_play()
        self.assertEqual((voice.owner_actions, voice.stops), (1, 1))
        c.skip_section()
        self.assertEqual((voice.owner_actions, voice.stops), (2, 2))
        c.jump_to_section(c._script.section_indices(False)[0])
        self.assertEqual((voice.owner_actions, voice.stops), (3, 3))
        if c._script.has_ignored:
            c.read_everything()
            self.assertEqual((voice.owner_actions, voice.stops), (4, 4))


class HeaderSpeakerTests(unittest.TestCase):
    def header(self, hour24: bool = False) -> hud.HeaderBar:
        header = hud.HeaderBar()
        self.addCleanup(header.deleteLater)
        header.set_hour24(hour24)
        header.set_clock(lambda: datetime(2026, 10, 4, 13, 52))
        for name in ("notion", "voice", "calendar", "claude"):
            header.set_service(name, hud.STATUS_OK)
        return header

    def test_hidden_until_shown_and_counted_while_shown(self) -> None:
        header = self.header()
        self.assertFalse(header.speaker_visible())
        widths = [header._required_width(level) for level in range(header._max_level() + 1)]
        header.set_speaker_visible(True)
        self.assertTrue(header.speaker_visible())
        self.assertEqual([header._required_width(level) for level in range(header._max_level() + 1)],
                         [width + 30 + 6 for width in widths])
        self.assertEqual(header._buttons_width(), 30 + 2 + 30)       # minimize and close as before
        header.set_speaker_visible(False)
        self.assertEqual([header._required_width(level) for level in range(header._max_level() + 1)], widths)

    def test_six_px_left_of_minimize_after_the_clock(self) -> None:
        header = self.header()
        header.set_speaker_visible(True)
        header.resize(1100, header.height())
        header.show()
        settle()
        speaker, minimize = header.speaker_button.geometry(), header.minimize_button.geometry()
        self.assertEqual(speaker.right() + 1 + header._SPEAKER_GAP, minimize.left())
        self.assertEqual(speaker.center().y(), minimize.center().y())
        self.assertLessEqual(header._clock.geometry().right(), speaker.left())
        self.assertEqual(speaker.size().toTuple(), (30, 30))
        header.hide()

    def test_click_toggles_and_says_so(self) -> None:
        header = self.header()
        header.set_speaker_visible(True)
        toggled = []
        header.muteToggled.connect(toggled.append)
        button = header.speaker_button
        self.assertEqual((button.toolTip(), button.accessibleName()), (hud.SPEAKER_ON_TIP, hud.SPEAKER_ON_TIP))
        button.click()
        self.assertEqual(toggled, [True])
        self.assertTrue(header.muted())
        self.assertEqual(button.toolTip(), "Jarvis is muted - click to let him speak")
        button.click()
        self.assertEqual(toggled, [True, False])
        header.set_muted(True)                                        # set from outside: no signal
        self.assertEqual(toggled, [True, False])
        self.assertTrue(header.muted())

    def test_glyphs_differ_on_and_muted(self) -> None:
        button = self.header().speaker_button
        on = button.grab().toImage()
        button.set_muted(True)
        muted = button.grab().toImage()
        self.assertNotEqual(on, muted)

    def test_the_header_fits_at_900_px_with_both_clocks(self) -> None:
        for hour24 in (False, True):
            with self.subTest(hour24=hour24):
                window = ui.BriefingWindow(10, 30)
                self.addCleanup(window.deleteLater)
                window.set_hour24(hour24)
                window.header.set_clock(lambda: datetime(2026, 10, 4, 13, 52))
                for name in ("notion", "voice", "calendar", "claude"):
                    window.header.set_service(name, hud.STATUS_WARN)
                window.set_speaker_available(True)
                window.show_reading_view()
                window.resize(ui.READING_MIN_SIZE)
                window.show()
                settle()
                header = window.header
                self.assertTrue(header.speaker_visible())              # hidden bar, its button still works (Ctrl+M)
                # The reading view's title bar is the hologram's: clock, divider, mute, minimize, close.
                holo_header = window.reading.header
                self.assertTrue(holo_header.mute_visible() and holo_header.mute_button.isVisible())
                mute = holo_header.mute_button.geometry()
                self.assertLessEqual(holo_header.readout.geometry().right(), holo_header.clock.geometry().left())
                self.assertLessEqual(holo_header.clock.geometry().right(), mute.left())
                self.assertLessEqual(mute.right(), holo_header.minimize_button.geometry().left())
                self.assertLessEqual(holo_header.close_button.geometry().right(), holo_header.width() - 1)
                window.show_prompt_view()                              # the legacy prompt: no speaker
                settle()
                self.assertFalse(header.speaker_visible())
                window.hide()

    def test_ctrl_m_only_while_the_button_shows(self) -> None:
        window = ui.BriefingWindow(10, 30)
        self.addCleanup(window.deleteLater)
        toggled = []
        window.header.muteToggled.connect(toggled.append)
        window.show_reading_view()
        window._on_mute_shortcut()
        self.assertEqual(toggled, [])                                  # not available ([assistant] speak off)
        window.set_speaker_available(True)
        window._on_mute_shortcut()
        self.assertEqual(toggled, [True])

    def test_the_controller_mutes_its_voice_from_the_button(self) -> None:
        app = AppHarness(self, ask=False)
        app.reading(autoplay=False)
        header = app.c.window.header
        self.assertTrue(header.speaker_visible())
        header.speaker_button.click()
        self.assertTrue(app.voice.muted and header.muted())
        app.c.window._on_mute_shortcut()
        self.assertFalse(app.voice.muted or header.muted())


class AskConversationTests(unittest.TestCase):
    def make(self, *runs, **kwargs) -> AppHarness:
        app = AppHarness(self, runs=runs, **kwargs)
        app.reading(autoplay=False)
        app.c.ask.start()
        self.assertTrue(wait_for(lambda: app.c.ask.ready is not None), "readiness checked")
        return app

    @staticmethod
    def submit(c: ui.AppController, text: str = COMMAND) -> None:
        c.window.reading.command_bar.set_text(text)
        c.window.reading.command_bar.input.returnPressed.emit()
        settle()

    def test_request_answer_and_speech(self) -> None:
        app = self.make(plan())
        c, reading = app.c, app.c.window.reading
        reading.set_tab(hud.TAB_BRIEFING)
        with self.assertLogs("briefing_reader", level="DEBUG") as logs:
            self.submit(c)
            self.assertEqual(reading.current_tab(), hud.TAB_LIVE)   # [live] auto_open = "tab" (the default)
            self.assertEqual(reading.conversation.entries()[-1][0::2], ("you", COMMAND))
            self.assertTrue(wait_for(lambda: not c.ask.busy, 10))
            settle()
        role, _stamp, text, sub = reading.conversation.entries()[-1]
        self.assertEqual((role, text), ("jarvis", SAY))
        self.assertTrue(sub.startswith(f"2 proposals under NEEDS YOUR OK {hud.MIDDLE_DOT} "), sub)
        self.assertEqual(reading.conversation.entry_widgets()[-1].tone(), hud.TONE_DONE)
        kinds = app.voice.kinds()
        self.assertEqual(kinds, ["ack", "reply"])
        self.assertEqual(app.voice.said[0][2], ui.ASK_ACK_KEY)
        self.assertIn(app.voice.texts()[0], ("Right away, sir.", "One moment, sir.", "On it, sir.", "Let me see, sir."))
        # SAY claims the move was done ("Moved ..."): it is never spoken as written.
        self.assertEqual(app.voice.texts()[1], "I've lined up two proposals for your OK, sir.")
        bar = reading.command_bar
        self.assertEqual((bar.status(), bar.tone()), (SAY, hud.TONE_DONE))   # the bar as before
        text = "\n".join(logs.output)
        self.assertIn("Ask reply not spoken as written (it claimed something was done)", text)
        for secret in (COMMAND, SAY, GUEST, "One moment", "lined up"):
            self.assertNotIn(secret, text)

    def test_request_brings_up_jarvis_with_auto_open_off(self) -> None:
        app = self.make(plan(), config_extra='\n[live]\nauto_open = "off"\n')
        c, reading = app.c, app.c.window.reading
        reading.set_tab(hud.TAB_BRIEFING)
        self.submit(c)
        self.assertEqual(reading.current_tab(), hud.TAB_JARVIS)
        self.assertEqual(reading.conversation.entries()[-1][0::2], ("you", COMMAND))
        self.assertTrue(wait_for(lambda: not c.ask.busy, 10))
        settle()
        self.assertEqual(reading.current_tab(), hud.TAB_JARVIS)
        self.assertEqual(reading.conversation.entries()[-1][0::2], ("jarvis", SAY))

    def test_a_plain_answer_is_spoken_as_written(self) -> None:
        say = "Right, sir. I've lined up moving Jarvis test sync to Friday at two. It is waiting for your OK."
        app = self.make(plan(say=say, lines=(MOVE_LINE,)))
        self.submit(app.c)
        self.assertTrue(wait_for(lambda: not app.c.ask.busy, 10))
        settle()
        self.assertEqual(app.voice.said[-1], ("reply", say, ""))

    def test_a_question_back_is_amber_and_spoken(self) -> None:
        app = self.make(plan(say="", question="Which Friday do you mean, sir?", lines=()))
        self.submit(app.c)
        self.assertTrue(wait_for(lambda: not app.c.ask.busy, 10))
        settle()
        entry = app.c.window.reading.conversation.entry_widgets()[-1]
        self.assertEqual(entry.entry[2], "Which Friday do you mean, sir?")
        self.assertEqual(entry.tone(), hud.TONE_WARN)
        self.assertTrue(entry.entry[3].startswith("no proposals"), entry.entry[3])
        self.assertEqual(app.voice.texts()[-1], "Which Friday do you mean, sir?")

    def test_a_600_character_answer_is_shown_whole(self) -> None:
        app = AppHarness(self)
        app.reading(autoplay=False)
        c = app.c
        say = ("Right, sir. I've lined up moving Project sync from Thursday to Friday, because it clashes with your "
               "Studio Weekly Meeting and the Northwind review on Thursday afternoon, and a short note to Ana. ") * 2
        question = ("Should the note also mention that the slides will be shared on Friday morning, or would you "
                    "rather tell her yourself when you see her at the review tomorrow?")
        note = "Mail of the work account could not be read; one more Google sign-in allows it (Allow work mail)."
        outcome = AskOutcome(True, "ok", message=note, say=say.strip()[:300], question=question,
                             cards=(ask_card(MOVE_LINE),), runs=1, duration_ms=6900)
        full = ask_ui.AskController._answer(outcome)
        self.assertGreaterEqual(len(full), 450)
        c._on_ask_outcome(outcome)
        settle()
        entry = c.window.reading.conversation.entry_widgets()[-1]
        self.assertEqual(entry.text_label.text(), full)              # never elided or cut
        self.assertEqual(entry.entry[3], f"1 proposal under NEEDS YOUR OK {hud.MIDDLE_DOT} 6.9 s")
        self.assertEqual(entry.shown_sub(), "1 proposal under NEEDS YOUR OK")   # Home's Transcript: no seconds
        spoken = app.voice.texts()[-1]
        self.assertLessEqual(len(spoken), 400)
        self.assertNotIn("Mail of the work account", spoken)          # Jarvis's note is shown, not said

    def test_a_timeout_is_shown_whole_and_said_briefly(self) -> None:
        clock = FakeClock()
        app = self.make(FakeProcess(stream("success")[:1], clock=clock, hang=True), engine_clock=clock)
        self.submit(app.c)
        self.assertTrue(wait_for(lambda: not app.c.ask.busy, 10))
        settle()
        entry = app.c.window.reading.conversation.entry_widgets()[-1]
        self.assertEqual(entry.entry[2], "Took too long; nothing was proposed")
        self.assertEqual(entry.tone(), hud.TONE_ERROR)
        self.assertEqual(app.voice.texts()[-1], "Sorry, sir - that took too long. Nothing was proposed.")

    def test_cancel_adds_an_entry_and_drops_the_ack(self) -> None:
        from tests.test_ask_controller import GatedProcess

        gate = threading.Event()
        process = GatedProcess(plan(), gate)
        app = self.make(process)
        c = app.c
        self.submit(c)
        self.assertTrue(wait_for(lambda: c.ask.stage() == ask_planner.STAGE_PLANNING))
        self.assertTrue(wait_for(lambda: process.given >= 1))
        c.window.reading.command_bar.button.click()
        self.assertTrue(wait_for(lambda: not c.ask.busy, 3))
        settle()
        entry = c.window.reading.conversation.entry_widgets()[-1]
        self.assertEqual((entry.entry[2], entry.tone()), (ui.ASK_CANCELLED_ENTRY, hud.TONE_IDLE))
        self.assertIn(ui.ASK_ACK_KEY, app.voice.cancelled)
        self.assertNotIn("reply", app.voice.kinds())

    def test_speak_replies_off_keeps_the_entries_and_says_nothing(self) -> None:
        app = AppHarness(self, runs=(plan(),))
        app.c.config = dataclasses.replace(app.c.config, assistant=dataclasses.replace(app.c.config.assistant,
                                                                                      speak_replies=False))
        app.reading(autoplay=False)
        app.c.ask.start()
        self.assertTrue(wait_for(lambda: app.c.ask.ready is not None))
        self.submit(app.c)
        self.assertTrue(wait_for(lambda: not app.c.ask.busy, 10))
        settle()
        self.assertEqual(app.voice.said, [])
        self.assertEqual([entry[0] for entry in app.c.window.reading.conversation.entries()[-2:]], ["you", "jarvis"])

    def test_a_refused_enter_adds_nothing(self) -> None:
        app = self.make(plan())
        c = app.c
        before = len(c.window.reading.conversation.entries())
        self.submit(c, "   ")
        self.assertEqual(len(c.window.reading.conversation.entries()), before)
        self.assertEqual(app.voice.said, [])


class ResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = AppHarness(self)
        self.app.reading(autoplay=False)
        self.c = self.app.c
        self.voice = self.app.voice

    def approve(self, action, *, check: bool = False) -> None:
        if check:
            self.assertTrue(wait_for(lambda: action.id in self.c._checks, 5), "Google's view of the event")
        said = len(self.voice.said)
        card = self.c.window.reading.action_card(action.id)
        if not action.held:   # a calendar change goes at once: its sentence comes with Google's answer
            card.approve_button.click()
            self.assertEqual(len(self.voice.said), said, "nothing is said at the click")
            settle()
            self.assertIsNone(self.c._send_hold)
            return
        card.approve_button.click()
        settle()
        self.assertIsNotNone(self.c._send_hold, self.c._hints.get(action.id))
        self.assertEqual(len(self.voice.said), said, "nothing is said at the click")
        pumps = self.voice.pumps
        self.c._on_send_hold_tick()                                    # the hold goes on
        settle()
        self.assertEqual(len(self.voice.said), said, "nothing is said during the hold")
        self.c._send_hold.deadline = time.monotonic() - 0.001
        self.c._on_send_hold_tick()
        self.assertGreater(self.voice.pumps, pumps)                    # the voice looked again at the end

    def wait_result(self, action) -> None:
        self.assertTrue(wait_for(lambda: action.id not in self.c._jobs and action.id not in self.c._running, 5))
        settle()

    def results(self) -> list[str]:
        return [text for kind, text, _key in self.voice.said if kind == "result"]

    def last_entry(self) -> tuple[str, str]:
        widget = self.c.window.reading.conversation.entry_widgets()[-1]
        return widget.entry[2], widget.tone()

    def test_a_move_is_confirmed_once_after_the_result(self) -> None:
        move = ask_card(MOVE_LINE)
        self.c._take_ask_cards([move])
        with self.assertLogs("briefing_reader", level="DEBUG") as logs:
            self.approve(move, check=True)
            self.wait_result(move)
        expected = "Done, sir - I've moved 'Jarvis test sync' to Friday at 2 PM."
        self.assertEqual(self.results(), [expected])
        self.assertEqual(self.last_entry(), (expected, hud.TONE_GOOD))
        self.assertIn(("move", "jts0001aa"), self.app.calendars["work"].calls)
        text = "\n".join(logs.output)
        self.assertNotIn("Jarvis test sync", text)
        self.assertNotIn("Done, sir", text)

    def test_an_email_is_confirmed_without_its_body_or_address(self) -> None:
        mail = parse_action_line(MAIL_LINE)
        self.c._set_source("briefing", list(self.c._source_lists["briefing"]) + [mail])
        settle()
        with self.assertLogs("briefing_reader", level="DEBUG") as logs:
            self.approve(mail)
            self.wait_result(mail)
        self.assertEqual(len(self.app.senders["work"].sent), 1)
        expected = "Sent, sir - your email 'Jarvis test sync moved' is on its way."
        self.assertEqual(self.results(), [expected])
        self.assertEqual(self.last_entry(), (expected, hud.TONE_GOOD))
        shown = " ".join(entry[2] for entry in self.c.window.reading.conversation.entries())
        for secret in (BODY_MARK, TRUSTED, "@"):
            self.assertNotIn(secret, " ".join(self.voice.texts()))
            self.assertNotIn(secret, shown)
        text = "\n".join(logs.output)
        for secret in ("Jarvis test sync moved", BODY_MARK, TRUSTED, "Sent, sir"):
            self.assertNotIn(secret, text)

    def test_a_calendar_event_names_its_time(self) -> None:
        calendar = next(action for action in self.c._actions if action.kind == "calendar")
        self.approve(calendar)
        self.wait_result(calendar)
        self.assertEqual(self.results(), ["Done, sir - I've added 'Club general meeting' to your calendar for "
                                          "Tuesday at 5 PM."])

    def test_a_failed_move_says_so_without_the_error(self) -> None:
        move = ask_card(MOVE_LINE)
        work = self.app.calendars["work"]

        def refuse(*_args, **_kwargs):
            raise NotAllowed("MARKER-ERROR only the organizer ana@example.edu may move it")

        work.move = refuse
        self.c._take_ask_cards([move])
        self.approve(move, check=True)
        self.wait_result(move)
        expected = "Sorry, sir - I couldn't move 'Jarvis test sync'. The card says why."
        self.assertEqual(self.results(), [expected])
        self.assertEqual(self.last_entry(), (expected, hud.TONE_ERROR))

    def test_an_unknown_move_asks_to_check(self) -> None:
        move = ask_card(MOVE_LINE)

        def lost(*_args, **_kwargs):
            raise CalendarUnknownOutcome("connection lost")

        self.app.calendars["work"].move = lost
        self.c._take_ask_cards([move])
        self.approve(move, check=True)
        self.wait_result(move)
        expected = ("Sir, I can't tell whether 'Jarvis test sync' was moved. Please check your calendar before "
                    "trying again.")
        self.assertEqual(self.results(), [expected])
        self.assertEqual(self.last_entry(), (expected, hud.TONE_WARN))

    def test_an_unknown_email(self) -> None:
        mail = parse_action_line(MAIL_LINE)
        sender = self.app.senders["work"]

        def lost(_mail):
            raise GmailUnknownOutcome("timed out")

        sender.send = lost
        self.c._set_source("briefing", list(self.c._source_lists["briefing"]) + [mail])
        settle()
        self.approve(mail)
        self.wait_result(mail)
        self.assertEqual(self.results(), ["Sir, I can't tell whether 'Jarvis test sync moved' was sent. Please "
                                          "check your Sent mail before trying again."])

    def test_not_sent_when_the_account_changed(self) -> None:
        mail = parse_action_line(MAIL_LINE)
        self.c._set_source("briefing", list(self.c._source_lists["briefing"]) + [mail])
        settle()
        with mock.patch.object(ui, "_mail_from_problem", return_value=ui.FROM_CHANGED_NOTE):
            self.approve(mail)
        settle()
        self.assertEqual(self.app.senders["work"].sent, [])
        expected = ("Sir, I didn't send 'Jarvis test sync moved': the sending account changed. Please check From "
                    "and try again.")
        self.assertEqual(self.results(), [expected])
        self.assertEqual(self.last_entry(), (expected, hud.TONE_WARN))

    def test_an_earlier_done_never_waits_into_the_next_approval(self) -> None:
        # A's "Done, sir" not yet said when B is approved is dropped (its entry stays): said when B's
        # hold ends or is undone, it would sound like B's result, which only comes later.
        calendar = next(action for action in self.c._actions if action.kind == "calendar")
        self.approve(calendar)
        self.wait_result(calendar)
        self.assertEqual([(kind, key) for kind, _text, key in self.voice.said], [("result", "result")])
        entries = len(self.c.window.reading.conversation.entries())
        cancelled = len(self.voice.cancelled)
        mail = parse_action_line(MAIL_LINE)
        self.c._set_source("briefing", list(self.c._source_lists["briefing"]) + [mail])
        settle()
        self.c.window.reading.action_card(mail.id).approve_button.click()
        settle()
        self.assertIsNotNone(self.c._send_hold)
        self.assertEqual(self.voice.cancelled[cancelled:], ["result"])
        self.assertEqual(len(self.voice.said), 1)          # nothing new at the click
        self.c._send_hold.deadline = time.monotonic() - 0.001
        self.c._on_send_hold_tick()
        self.wait_result(mail)
        self.assertEqual(self.results()[-1], "Sent, sir - your email 'Jarvis test sync moved' is on its way.")
        self.assertEqual(len(self.voice.said), 2)
        self.assertEqual(len(self.c.window.reading.conversation.entries()), entries + 1)

    def test_undo_says_nothing(self) -> None:
        mail = parse_action_line(MAIL_LINE)
        self.c._set_source("briefing", list(self.c._source_lists["briefing"]) + [mail])
        settle()
        self.c.window.reading.action_card(mail.id).approve_button.click()
        settle()
        self.c._send_hold.started -= 5
        pumps = self.voice.pumps
        self.c.undo_action(mail.id)
        settle()
        self.assertIsNone(self.c._send_hold)
        self.assertGreater(self.voice.pumps, pumps)
        self.assertEqual(self.voice.said, [])
        # A calendar change's Undo after its result: nothing more is said either.
        self.app.calendars["personal"].stamps = True
        calendar = next(action for action in self.c._actions if action.kind == "calendar")
        self.approve(calendar)
        self.wait_result(calendar)
        said = list(self.voice.said)
        self.c._undos[calendar.id].shown -= 5
        self.c.undo_action(calendar.id)
        self.wait_result(calendar)
        self.assertNotIn(calendar.id, self.c._undos)
        self.assertEqual(self.voice.said, said)

    def test_speak_results_off_shows_only(self) -> None:
        self.c.config = dataclasses.replace(self.c.config, assistant=dataclasses.replace(
            self.c.config.assistant, speak_results=False))
        calendar = next(action for action in self.c._actions if action.kind == "calendar")
        self.approve(calendar)
        self.wait_result(calendar)
        self.assertEqual(self.voice.said, [])
        self.assertTrue(self.last_entry()[0].startswith("Done, sir - I've added 'Club general meeting'"))


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
