"""Tests for voice_ui.AssistantVoice: Jarvis's own voice with a fake worker, a fake utterance player
and a fake briefing player (offscreen Qt; nothing is synthesized or played, no QtMultimedia).

The voice pauses a playing briefing for an utterance and resumes it 400 ms after the queue ran
empty, but only if it paused it itself and the owner has not touched playback since; Play (stop)
ends an utterance at once; no utterance starts while a Send's hidden hold runs and a held one plays
after the hold ends; mute stops speaking, drops the queue, refuses new utterances and is
remembered in assistant.json; and the log names kinds and lengths, never the text.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from briefing_reader import speech
from briefing_reader.models import AudioSegment, SectionAudio
from briefing_reader.prefs import PREFS_FILE, AssistantPrefs
from briefing_reader.speech import KIND_ACK, KIND_ANNOUNCE, KIND_GREETING, KIND_REPLY, KIND_RESULT
from briefing_reader.voice_ui import AssistantVoice

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(["test_voice_ui", "-platform", "offscreen"])


def settle(rounds: int = 3) -> None:
    for _ in range(rounds):
        QApplication.processEvents()


def wait_for(predicate, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    QApplication.processEvents()
    return bool(predicate())


def audio(seq: int) -> SectionAudio:
    return SectionAudio(section_key=f"say-{seq}", segments=(AudioSegment(Path(f"say-{seq}.wav"), 800, ((0, 0),)),),
                        engine="edge")


class FakeWorker:
    def __init__(self) -> None:
        self.submitted: list[tuple[int, str]] = []
        self.forgotten: list[int] = []
        self.stopped = False

    def submit(self, seq: int, text: str) -> None:
        self.submitted.append((seq, text))

    def forget(self, seq: int) -> None:
        self.forgotten.append(seq)

    def stop(self) -> None:
        self.stopped = True


class FakeUtterancePlayer(QObject):
    finished = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.seq: int | None = None
        self.played: list[int] = []
        self.stops = 0

    @property
    def playing(self) -> bool:
        return self.seq is not None

    def play(self, seq: int, _audio) -> None:
        self.seq = seq
        self.played.append(seq)

    def stop(self) -> None:
        self.stops += 1
        self.seq = None

    def end(self) -> None:
        """The utterance played to its end."""
        seq, self.seq = self.seq, None
        self.finished.emit(seq)


class FakeBriefing:
    def __init__(self, state: str = "idle") -> None:
        self.state = state
        self.pauses = 0
        self.resumes = 0

    def pause(self) -> None:
        if self.state in ("playing", "waiting"):
            self.pauses += 1
            self.state = "paused"

    def resume(self) -> None:
        if self.state == "paused":
            self.resumes += 1
            self.state = "playing"


class VoiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.prefs_path = Path(tmp.name) / PREFS_FILE
        self.prefs = AssistantPrefs(self.prefs_path)
        self.worker = FakeWorker()
        self.player = FakeUtterancePlayer()
        self.briefing = FakeBriefing()
        self.holding = False
        self.spoken: list[str] = []
        self.voice = self.make()

    def make(self, **kwargs) -> AssistantVoice:
        voice = AssistantVoice(worker=self.worker, player=self.player, briefing_player=lambda: self.briefing,
                               hold_active=lambda: self.holding, prefs=self.prefs, resume_delay_ms=40,
                               tick_ms=20, **kwargs)
        voice.speakingChanged.connect(self.spoken.append)
        self.addCleanup(voice.shutdown)
        return voice

    def say(self, kind: str, text: str, key: str = "", *, synthesized: bool = True) -> int:
        self.assertTrue(self.voice.say(kind, text, key=key))
        seq = self.worker.submitted[-1][0]
        if synthesized:
            self.voice.on_audio(seq, audio(seq), None)
        return seq


class SpeakingTests(VoiceTestCase):
    def test_say_synthesizes_then_plays_and_reports_the_text(self) -> None:
        seq = self.say(KIND_GREETING, "Good morning, sir.", synthesized=False)
        self.assertEqual(self.worker.submitted, [(seq, "Good morning, sir.")])
        self.assertFalse(self.voice.speaking)                        # its audio has not arrived
        self.voice.on_audio(seq, audio(seq), None)
        self.assertTrue(self.voice.speaking)
        self.assertEqual(self.player.played, [seq])
        self.assertEqual(self.spoken, ["Good morning, sir."])
        self.player.end()
        self.assertFalse(self.voice.speaking)
        self.assertEqual(self.spoken, ["Good morning, sir.", ""])

    def test_a_player_that_cannot_play_ends_after_the_text(self) -> None:
        """UtterancePlayer reports ``finished`` inside play() when the file is gone: the "" must come
        last, or the orb would stay on SPEAKING."""
        def fail_at_once(seq: int, _audio) -> None:
            self.player.played.append(seq)
            self.player.finished.emit(seq)

        self.player.play = fail_at_once
        self.say(KIND_RESULT, "Done, sir.")
        self.assertEqual(self.spoken, ["Done, sir.", ""])
        self.assertFalse(self.voice.speaking)
        self.assertIsNone(self.voice.current())
        second = self.say(KIND_RESULT, "Next.")
        self.assertEqual(self.player.played[-1], second)               # the lane went on

    def test_one_at_a_time_in_order(self) -> None:
        first = self.say(KIND_GREETING, "One.")
        second = self.say(KIND_RESULT, "Two.")
        self.assertEqual(self.player.played, [first])
        self.player.end()
        self.assertEqual(self.player.played, [first, second])

    def test_the_text_is_scrubbed_before_synthesis(self) -> None:
        self.voice.say(KIND_REPLY, "Write to sam.example@example.com at https://example.com/x.")
        self.assertEqual(self.worker.submitted[-1][1], "Write to that address at the link.")

    def test_a_failed_synthesis_is_skipped(self) -> None:
        broken = self.say(KIND_REPLY, "Broken.", synthesized=False)
        after = self.say(KIND_RESULT, "Next.")
        self.assertEqual(self.player.played, [])
        with self.assertLogs("briefing_reader.voice_ui", level="INFO") as logs:
            self.voice.on_audio(broken, None, "Speech synthesis failed (TtsError)")
        self.assertEqual(self.player.played, [after])
        self.assertIn("Say failed (reply)", "\n".join(logs.output))

    def test_cancel_key_drops_a_waiting_announcement(self) -> None:
        self.say(KIND_GREETING, "Good morning, sir.")
        announce = self.say(KIND_ANNOUNCE, "Sir, your AM briefing is ready to view.", key="announce:K")
        self.voice.cancel_key("announce:K")
        self.assertIn(announce, self.worker.forgotten)
        self.player.end()
        self.assertEqual(len(self.player.played), 1)

    def test_a_reply_drops_the_waiting_ack(self) -> None:
        self.say(KIND_RESULT, "Something first.")
        ack = self.say(KIND_ACK, "One moment, sir.", synthesized=False)
        reply = self.say(KIND_REPLY, "Right, sir.")
        self.assertIn(ack, self.worker.forgotten)
        self.player.end()
        self.assertEqual(self.player.played[-1], reply)

    def test_audio_for_a_dropped_utterance_is_ignored(self) -> None:
        seq = self.say(KIND_ANNOUNCE, "Sir, your AM briefing is ready to view.", key="k", synthesized=False)
        self.voice.cancel_key("k")
        self.voice.on_audio(seq, audio(seq), None)
        self.assertEqual(self.player.played, [])

    def test_stale_utterances_are_dropped_while_waiting(self) -> None:
        clock = [100.0]
        self.voice = self.make(clock=lambda: clock[0])
        self.voice.say(KIND_ACK, "On it, sir.")
        clock[0] += 9
        self.assertTrue(wait_for(lambda: not self.voice.pending()))
        self.assertEqual(self.player.played, [])

    def test_the_log_never_holds_the_text(self) -> None:
        with self.assertLogs("briefing_reader", level="DEBUG") as logs:
            seq = self.say(KIND_REPLY, "MARKER-REPLY to sam.example@example.com.")
            self.player.end()
            self.voice.set_muted(True)
            self.voice.say(KIND_RESULT, "MARKER-MUTED")
        text = "\n".join(logs.output)
        self.assertIn("Say: kind=reply chars=", text)
        self.assertIn("Say skipped: muted (result)", text)
        self.assertNotIn("MARKER", text)
        self.assertNotIn("example.com", text)
        self.assertTrue(seq)


class BriefingTests(VoiceTestCase):
    def test_pauses_a_playing_briefing_and_resumes_it_after(self) -> None:
        self.briefing.state = "playing"
        self.say(KIND_RESULT, "Done, sir.")
        self.assertEqual((self.briefing.state, self.briefing.pauses), ("paused", 1))
        self.say(KIND_RESULT, "Also done, sir.")
        self.player.end()
        settle()
        self.assertEqual(self.briefing.state, "paused")            # still more to say
        self.player.end()
        self.assertEqual(self.briefing.state, "paused")            # not at once: 400 ms (40 here) later
        self.assertTrue(wait_for(lambda: self.briefing.state == "playing"))
        self.assertEqual((self.briefing.pauses, self.briefing.resumes), (1, 1))

    def test_a_waiting_briefing_is_paused_too(self) -> None:
        self.briefing.state = "waiting"
        self.say(KIND_RESULT, "Done, sir.")
        self.assertEqual(self.briefing.state, "paused")

    def test_never_resumes_after_the_owner_acted(self) -> None:
        self.briefing.state = "playing"
        self.say(KIND_RESULT, "Done, sir.")
        self.voice.owner_acted()     # e.g. the owner pressed Pause meanwhile
        self.player.end()
        settle()
        time.sleep(0.1)
        settle()
        self.assertEqual((self.briefing.state, self.briefing.resumes), ("paused", 0))

    def test_never_resumes_a_briefing_it_did_not_pause(self) -> None:
        self.briefing.state = "paused"    # paused by the owner (or by Ask planning)
        self.say(KIND_ACK, "One moment, sir.")
        self.player.end()
        time.sleep(0.1)
        settle()
        self.assertEqual((self.briefing.pauses, self.briefing.resumes), (0, 0))

    def test_never_resumes_a_replaced_briefing_player(self) -> None:
        self.briefing.state = "playing"
        self.say(KIND_RESULT, "Done, sir.")
        old, self.briefing = self.briefing, FakeBriefing("paused")
        self.player.end()
        time.sleep(0.1)
        settle()
        self.assertEqual((old.resumes, self.briefing.resumes), (0, 0))

    def test_play_stops_an_utterance_at_once(self) -> None:
        self.briefing.state = "paused"
        self.say(KIND_REPLY, "Right, sir.")
        self.say(KIND_RESULT, "Done, sir.")
        self.voice.owner_acted()
        self.voice.stop()
        self.assertFalse(self.voice.speaking)
        self.assertEqual(self.player.stops, 1)
        self.assertEqual(self.voice.pending(), [])
        self.assertEqual(self.spoken[-1], "")
        self.assertEqual(len(self.player.played), 1)

    def test_stop_without_the_owner_acting_resumes_what_it_paused(self) -> None:
        self.briefing.state = "playing"
        self.say(KIND_RESULT, "Done, sir.")
        self.voice.stop()
        self.assertTrue(wait_for(lambda: self.briefing.state == "playing"))


class CountdownTests(VoiceTestCase):
    def test_nothing_starts_during_a_hold_and_the_held_one_plays_after(self) -> None:
        self.holding = True
        seq = self.say(KIND_RESULT, "Done, sir.")
        self.voice.pump()
        time.sleep(0.05)
        settle()
        self.assertEqual(self.player.played, [])
        self.assertFalse(self.voice.speaking)
        self.holding = False
        self.voice.pump()                 # the controller's _cancel_send_hold / _finish_send_hold
        self.assertEqual(self.player.played, [seq])

    def test_one_already_playing_finishes(self) -> None:
        first = self.say(KIND_RESULT, "Done, sir.")
        self.holding = True
        second = self.say(KIND_RESULT, "Also done, sir.")
        self.assertEqual(self.player.stops, 0)
        self.player.end()
        self.assertEqual(self.player.played, [first])
        self.holding = False
        self.assertTrue(wait_for(lambda: self.player.played == [first, second]))   # the tick looks again

    def test_an_earlier_result_dropped_at_the_next_approval_is_never_said(self) -> None:
        # The controller's _start_send_hold drops the waiting "result" sentences: nothing is said when
        # that hold ends (or is undone) before its own result.
        seq = self.say(KIND_RESULT, "Sent, sir - your email is on its way.", "result", synthesized=False)
        self.voice.cancel_key("result")
        self.holding = True
        self.voice.pump()
        self.voice.on_audio(seq, audio(seq), None)   # the synthesis ends after the drop
        self.holding = False
        self.voice.pump()
        time.sleep(0.05)
        settle()
        self.assertEqual(self.player.played, [])
        self.assertIn(seq, self.worker.forgotten)
        self.assertEqual(self.spoken, [])

    def test_a_failing_hold_check_holds(self) -> None:
        def broken() -> bool:
            raise RuntimeError("no controller")

        self.voice = self.make()
        self.voice._hold_active = broken
        self.say(KIND_RESULT, "Done, sir.")
        self.assertFalse(self.voice.speaking)


class MuteTests(VoiceTestCase):
    def test_mute_stops_drops_and_refuses_then_unmute(self) -> None:
        self.say(KIND_REPLY, "Right, sir.")
        self.say(KIND_RESULT, "Done, sir.", synthesized=False)
        self.voice.set_muted(True)
        self.assertTrue(self.voice.muted)
        self.assertFalse(self.voice.speaking)
        self.assertEqual(self.voice.pending(), [])
        submitted = len(self.worker.submitted)
        self.assertFalse(self.voice.say(KIND_RESULT, "Done, sir."))
        self.assertEqual(len(self.worker.submitted), submitted)    # nothing synthesized while muted
        self.voice.set_muted(False)
        self.assertTrue(self.voice.say(KIND_RESULT, "Done, sir."))

    def test_mute_is_remembered(self) -> None:
        self.voice.set_muted(True)
        self.assertTrue(AssistantPrefs(self.prefs_path).muted)
        again = AssistantVoice(worker=FakeWorker(), player=FakeUtterancePlayer(), briefing_player=lambda: None,
                               prefs=AssistantPrefs(self.prefs_path))
        self.addCleanup(again.shutdown)
        self.assertTrue(again.muted)
        self.assertFalse(again.say(KIND_GREETING, "Good morning, sir."))
        self.voice.set_muted(False)
        self.assertFalse(AssistantPrefs(self.prefs_path).muted)

    def test_without_prefs_the_muted_argument_counts(self) -> None:
        voice = AssistantVoice(worker=FakeWorker(), player=FakeUtterancePlayer(), briefing_player=lambda: None,
                               muted=True)
        self.addCleanup(voice.shutdown)
        self.assertTrue(voice.muted)

    def test_muting_a_paused_briefing_brings_it_back(self) -> None:
        self.briefing.state = "playing"
        self.say(KIND_RESULT, "Done, sir.")
        self.voice.set_muted(True)
        self.assertTrue(wait_for(lambda: self.briefing.state == "playing"))


class ShutdownTests(VoiceTestCase):
    def test_shutdown_stops_everything(self) -> None:
        self.say(KIND_RESULT, "Done, sir.")
        self.voice.shutdown()
        self.assertTrue(self.worker.stopped)
        self.assertFalse(self.voice.speaking)
        self.assertFalse(self.voice.say(KIND_RESULT, "Done, sir."))
        self.voice.shutdown()   # twice is fine

    def test_it_is_a_voice(self) -> None:
        self.assertIsInstance(self.voice, speech.Voice)


if __name__ == "__main__":
    unittest.main()
