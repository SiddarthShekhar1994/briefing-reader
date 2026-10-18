"""Jarvis's own voice in the app (Qt): the player of his utterances and the voice the controller
speaks through (speech.Voice).

    UtterancePlayer(volume=..., parent=...)
        Its own QMediaPlayer and QAudioOutput (never the briefing's), at the controller's volume
        (so the silent harnesses at volume 0 stay silent), following the default output device like
        player.BriefingPlayer. ``play(seq, audio)`` plays one utterance's segments back to back;
        ``finished(seq)`` when it ended or could not be played; ``stop()`` ends it quietly.
        ``position_ms()``: how far into the utterance the audio is (the segments before, their pauses and
        the player's position, carried forward smoothly between its updates; -1 when nothing plays),
        ``timeline()``: its voice_fx.VoiceTimeline (word boundaries or the level envelope).

    AssistantVoice(worker=..., player=..., briefing_player=..., hold_active=..., prefs=...)
        speech.SpeechLane (the queue policy) + speech.SayWorker (the "tts-say" thread; its results
        come back through ``on_audio`` on the GUI thread) + an UtterancePlayer, with these rules:
        * one utterance at a time; synthesis starts at ``say()``, playback when its turn comes;
        * an utterance about to start pauses a PLAYING or WAITING briefing; 400 ms after the queue
          ran empty the voice resumes it, only if it paused it itself and the owner has not
          touched playback since (``owner_acted()``);
        * ``stop()`` (the owner pressed Play, Pause, Skip or a section) ends the utterance now and
          drops the queue;
        * nothing starts while a Send's hidden hold runs (``hold_active()``); ``pump()`` looks
          again (the controller calls it when a hold starts, ends or is undone);
        * ``set_muted(True)`` stops speaking, drops the queue and is remembered in assistant.json
          (prefs.AssistantPrefs); while muted ``say()`` returns False and nothing is synthesized;
        * every text goes through persona.scrub_for_speech; the log names kinds, lengths, the
          engine and timings, never the text.
        ``speakingChanged(text)``: the utterance being said ("" when none), for the orb and the
        speech line. ``timeline()`` / ``position_ms()``: the utterance's voice_fx.VoiceTimeline and how far
        its audio is (the orb's voice spikes and the caption's word reveal follow them; None / -1 when none).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer

from . import voice_fx
from .persona import scrub_for_speech
from .player import PAUSED, PLAYING, WAITING
from .speech import SpeechLane, Utterance

logger = logging.getLogger(__name__)

RESUME_DELAY_MS = 400     # the briefing resumes this long after the last utterance ended
TICK_MS = 500             # while utterances wait: look again (stale ones go, a hold ended)

_Status = QMediaPlayer.MediaStatus
_READY_STATUSES = (_Status.LoadedMedia, _Status.BufferingMedia, _Status.BufferedMedia)


class UtterancePlayer(QObject):
    """Plays one utterance (its segments back to back) on its own media player."""

    finished = Signal(int)   # seq: played to its end, or it could not be played

    def __init__(self, parent: QObject | None = None, *, volume: float = 1.0) -> None:
        super().__init__(parent)
        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(min(max(float(volume), 0.0), 1.0))
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.errorOccurred.connect(self._on_media_error)
        self._devices = QMediaDevices(self)
        self._devices.audioOutputsChanged.connect(self._on_outputs_changed)
        self._gap = QTimer(self)
        self._gap.setSingleShot(True)
        self._gap.timeout.connect(self._next_segment)
        self._seq: int | None = None
        self._segments: tuple[Any, ...] = ()
        self._index = 0
        self._source = QUrl()
        self._media_ready = False
        self._timeline: voice_fx.VoiceTimeline | None = None
        self._offsets: tuple[int, ...] = ()       # where each segment starts in the utterance (ms)
        self._anchor: tuple[float, int] | None = None   # (monotonic, position) of the last position report
        self._gap_from = 0.0
        self._player.positionChanged.connect(self._on_position)

    @property
    def playing(self) -> bool:
        return self._seq is not None

    def volume(self) -> float:
        return self._audio_output.volume()

    def play(self, seq: int, audio: Any) -> None:
        """Play ``audio`` (models.SectionAudio) as utterance ``seq``; ``finished(seq)`` at its end."""
        self.stop()
        self._seq = int(seq)
        self._segments = tuple(getattr(audio, "segments", ()) or ())
        self._index = 0
        offsets, base = [], 0
        for segment in self._segments:
            offsets.append(base)
            base += max(0, int(getattr(segment, "duration_ms", 0) or 0))
            base += max(0, int(getattr(segment, "pause_after_ms", 0) or 0))
        self._offsets = tuple(offsets)
        try:
            self._timeline = voice_fx.timeline_from_audio(audio)
        except Exception:  # noqa: BLE001 - the timing only animates; the audio still plays
            self._timeline = None
        self._play_segment()

    def timeline(self) -> voice_fx.VoiceTimeline | None:
        """The utterance's word boundaries / level envelope (None when nothing plays)."""
        return self._timeline if self._seq is not None else None

    def position_ms(self) -> int:
        """See the module docstring."""
        if self._seq is None or self._index >= len(self._offsets):
            return -1
        base = self._offsets[self._index]
        if self._gap.isActive():
            duration = int(getattr(self._segments[self._index], "duration_ms", 0) or 0)
            return base + duration + int((time.monotonic() - self._gap_from) * 1000)
        anchor = self._anchor
        if anchor is None or not self._media_ready:
            return base
        playing = self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        ahead = min(250, int((time.monotonic() - anchor[0]) * 1000)) if playing else 0
        return base + anchor[1] + max(0, ahead)

    def _on_position(self, position: int) -> None:
        if self._is_current():
            self._anchor = (time.monotonic(), max(0, int(position)))

    def stop(self) -> None:
        """End the utterance now (no ``finished``) and let go of its file."""
        self._gap.stop()
        self._seq = None
        self._segments = ()
        self._timeline = None
        self._release()

    def _play_segment(self) -> None:
        if self._seq is None:
            return
        if self._index >= len(self._segments):
            self._finish()
            return
        path = Path(self._segments[self._index].path)
        if not path.is_file():
            logger.warning("An utterance's audio file is missing")
            self._finish()
            return
        self._release()
        self._media_ready = False
        self._anchor = (time.monotonic(), 0)
        self._source = QUrl.fromLocalFile(str(path))
        self._player.setSource(self._source)
        if self._seq is not None and self._player.source() == self._source:
            self._player.play()

    def _next_segment(self) -> None:
        self._index += 1
        self._play_segment()

    def _segment_done(self) -> None:
        pause_ms = int(getattr(self._segments[self._index], "pause_after_ms", 0) or 0)
        if self._index + 1 < len(self._segments) and pause_ms > 0:
            self._release()
            self._gap_from = time.monotonic()
            self._gap.start(pause_ms)
        else:
            self._next_segment()

    def _finish(self) -> None:
        seq, self._seq = self._seq, None
        self._segments = ()
        self._timeline = None
        self._release()
        if seq is not None:
            self.finished.emit(seq)

    def _release(self) -> None:
        """Unload the file (QMediaPlayer otherwise keeps it open on Windows)."""
        self._source = QUrl()
        self._media_ready = False
        if not self._player.source().isEmpty():
            self._player.stop()
            self._player.setSource(QUrl())

    def _is_current(self) -> bool:
        return self._seq is not None and not self._source.isEmpty() and self._player.source() == self._source

    def _on_media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if not self._is_current():
            return
        if status in _READY_STATUSES:
            self._media_ready = True
        elif status == _Status.EndOfMedia:
            if self._media_ready:   # a stale end-of-media of the previous file comes before Loaded
                self._segment_done()
        elif status == _Status.InvalidMedia:
            logger.warning("An utterance could not be played (invalid media)")
            self._finish()

    def _on_media_error(self, error: QMediaPlayer.Error, _message: str) -> None:
        if error == QMediaPlayer.Error.NoError or not self._is_current():
            return
        logger.warning("An utterance could not be played (%s)", error.name)
        self._finish()

    def _on_outputs_changed(self) -> None:
        device = QMediaDevices.defaultAudioOutput()
        if device.isNull():
            return
        if device != self._audio_output.device():
            logger.info("Jarvis's voice follows the new default audio device")
            self._audio_output.setDevice(device)


class AssistantVoice(QObject):
    """speech.Voice for the app (see the module docs). Every method runs on the GUI thread."""

    speakingChanged = Signal(str)   # the utterance being said, "" when none

    def __init__(self, *, worker: Any, player: Any, briefing_player: Callable[[], Any],
                 hold_active: Callable[[], bool] = lambda: False, prefs: Any = None,
                 muted: bool = False, clock: Callable[[], float] = time.monotonic,
                 resume_delay_ms: int = RESUME_DELAY_MS, tick_ms: int = TICK_MS,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._worker = worker
        self._player = player
        self._briefing_player = briefing_player
        self._hold_active = hold_active
        self._prefs = prefs
        self._clock = clock
        self._lane = SpeechLane(clock=clock)
        self._lane.set_muted(bool(prefs.muted) if prefs is not None else bool(muted))
        self._paused: Any = None         # the briefing player this voice paused (owner untouched since)
        self._synth_s: dict[int, float] = {}   # seq -> seconds from say() to its audio
        self._closed = False
        player.finished.connect(self._on_finished)
        self._resume_timer = QTimer(self)
        self._resume_timer.setSingleShot(True)
        self._resume_timer.setInterval(max(0, int(resume_delay_ms)))
        self._resume_timer.timeout.connect(self._resume_briefing)
        self._tick = QTimer(self)
        self._tick.setInterval(max(10, int(tick_ms)))
        self._tick.timeout.connect(self.pump)

    # ---- speech.Voice ------------------------------------------------------------------

    @property
    def speaking(self) -> bool:
        return self._lane.current is not None and bool(self._player.playing)

    @property
    def muted(self) -> bool:
        return self._lane.muted

    def say(self, kind: str, text: str, *, key: str = "") -> bool:
        if self._closed:
            return False
        if self._lane.muted:
            logger.info("Say skipped: muted (%s)", kind)
            return False
        spoken = scrub_for_speech(text)
        utterance, dropped = self._lane.push(kind, spoken, key)
        self._forget(dropped)
        if utterance is None:
            return False
        self._worker.submit(utterance.seq, utterance.text)
        if not self._tick.isActive():
            self._tick.start()
        self.pump()
        return True

    def timeline(self) -> voice_fx.VoiceTimeline | None:
        """The utterance being said: its word boundaries or level envelope (None when none is said)."""
        current = self._lane.current
        if current is None or current.audio is None:
            return None
        try:   # from the utterance's own audio: known as soon as it starts (before the player has it)
            return voice_fx.timeline_from_audio(current.audio)
        except Exception:  # noqa: BLE001 - the timing only animates
            return None

    def position_ms(self) -> int:
        """How far the utterance being said is (ms); -1 when none plays."""
        if self._lane.current is None:
            return -1
        getter = getattr(self._player, "position_ms", None)
        return int(getter()) if callable(getter) else -1

    def cancel_key(self, key: str) -> None:
        self._forget(self._lane.cancel_key(key))

    def stop(self) -> None:
        """Stop the utterance now and drop the queue; the briefing this voice paused comes back
        unless the owner acted (``owner_acted`` first)."""
        speaking = self._lane.current is not None
        self._forget(self._lane.clear())
        self._player.stop()
        self._tick.stop()
        if speaking:
            self.speakingChanged.emit("")
        self._schedule_resume()

    def pump(self) -> None:
        if self._closed or self._lane.current is not None:
            return
        utterance = self._lane.start_next(hold=self._hold())
        if utterance is None:
            if self._lane.idle:
                self._tick.stop()
                self._schedule_resume()
            return
        self._resume_timer.stop()
        self._pause_briefing()
        logger.info("Say: kind=%s chars=%d engine=%s %.1f s", utterance.kind, len(utterance.text),
                    getattr(utterance.audio, "engine", "?"), self._synth_s.pop(utterance.seq, 0.0))
        # Said before play(): a player that cannot play the file reports ``finished`` at once, and its
        # "" must come after the text, or the orb would stay on SPEAKING.
        self.speakingChanged.emit(utterance.text)
        self._player.play(utterance.seq, utterance.audio)

    def owner_acted(self) -> None:
        self._paused = None
        self._resume_timer.stop()

    def set_muted(self, muted: bool) -> None:
        muted = bool(muted)
        if muted == self._lane.muted:
            return
        if muted:
            self.stop()
        self._lane.set_muted(muted)
        if self._prefs is not None:
            try:
                self._prefs.set_muted(muted)
            except Exception as exc:  # noqa: BLE001 - the switch still works for this run
                logger.warning("Could not remember the mute switch (%s)", type(exc).__name__)
        logger.info("Jarvis's voice is %s", "muted" if muted else "on")

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._tick.stop()
        self._resume_timer.stop()
        self._lane.clear()
        self._player.stop()
        try:
            self._worker.stop()
        except Exception as exc:  # noqa: BLE001 - shutting down goes on
            logger.debug("Jarvis's speech worker did not stop cleanly (%s)", type(exc).__name__)

    # ---- the worker's results (GUI thread, through the controller's bridge) ---------------

    def on_audio(self, seq: int, audio: Any, error: Any = None) -> None:
        """The audio of utterance ``seq`` (models.SectionAudio), or None with ``error``."""
        if self._closed:
            return
        item = self._lane.find(seq)
        if item is None or item is self._lane.current:
            return   # dropped meanwhile (stale, cancelled, muted, stopped)
        if audio is None or not getattr(audio, "segments", ()):
            self._lane.fail(seq)
            logger.info("Say failed (%s)", item.kind)
        else:
            self._synth_s[seq] = max(0.0, self._clock() - item.created)
            self._lane.set_audio(seq, audio)
        self.pump()

    # ---- internals -------------------------------------------------------------------------

    def pending(self) -> list[Utterance]:
        """Tests: the utterances waiting to start."""
        return self._lane.pending

    def current(self) -> Utterance | None:
        return self._lane.current

    def _hold(self) -> bool:
        try:
            return bool(self._hold_active())
        except Exception:  # noqa: BLE001 - unknown: hold (never speak over a hold by mistake)
            return True

    def _forget(self, dropped: list[Utterance]) -> None:
        for item in dropped:
            self._synth_s.pop(item.seq, None)
            try:
                self._worker.forget(item.seq)
            except Exception:  # noqa: BLE001 - only saves a synthesis
                pass

    def _briefing(self) -> Any:
        try:
            return self._briefing_player()
        except Exception:  # noqa: BLE001 - no briefing player: nothing to pause
            return None

    def _pause_briefing(self) -> None:
        player = self._briefing()
        if player is not None and getattr(player, "state", None) in (PLAYING, WAITING):
            player.pause()
            self._paused = player
            logger.info("The briefing pauses while Jarvis speaks")

    def _schedule_resume(self) -> None:
        if self._paused is not None and not self._closed:
            self._resume_timer.start()

    def _resume_briefing(self) -> None:
        paused, self._paused = self._paused, None
        if paused is None or self._closed:
            return
        if not self._lane.idle:
            self._paused = paused   # more to say first
            return
        if paused is self._briefing() and getattr(paused, "state", None) == PAUSED:
            logger.info("The briefing resumes after Jarvis spoke")
            paused.resume()

    def _on_finished(self, seq: int) -> None:
        current = self._lane.current
        if current is None or current.seq != seq:
            return
        self._lane.finish(seq)
        self.speakingChanged.emit("")
        self.pump()
