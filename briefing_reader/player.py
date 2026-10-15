"""Sequential section player for the reading screen (Qt).

The UI registers each section's audio as the TTS worker produces it
(``set_audio``) and hands the player a queue of section indices (``start`` /
``replace_upcoming``). Sections play in queue order; the segments of a section
play back to back with their divider silence in between, and a short silence
separates sections. Silence is a single-shot QTimer rather than audio, so
``pause`` can hold it and ``skip_section`` / ``stop`` can cancel it.

Highlighting: every segment carries ``(start_ms, item_index)`` marks. The
player maps QMediaPlayer's position onto them and emits ``itemChanged`` when
the spoken item changes.

Everything runs on the GUI thread and nothing blocks: QMediaPlayer decodes on
its own threads and all waiting is done with timers.

Signal order when a section begins: ``stateChanged`` (only if the state
changed), ``sectionStarted``, then ``itemChanged`` for its first item. When the
queue runs out: ``stateChanged("finished")`` then ``itemChanged(-1, -1)``.
"""

from __future__ import annotations

import bisect
import logging
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer

from .models import SectionAudio

logger = logging.getLogger(__name__)

IDLE = "idle"
WAITING = "waiting"      # the current section's audio has not arrived yet
PLAYING = "playing"      # includes the silent gaps between segments and sections
PAUSED = "paused"
FINISHED = "finished"

# What the player is doing while a section is current.
_PHASE_NONE = 0          # nothing loaded: waiting for audio, or about to start the section
_PHASE_SEGMENT = 1       # a segment is loaded in the QMediaPlayer
_PHASE_GAP = 2           # silence timer running before the next step

# What happens when the gap timer fires.
_NEXT_SEGMENT = 0
_NEXT_SECTION = 1

_Status = QMediaPlayer.MediaStatus
_READY_STATUSES = (_Status.LoadedMedia, _Status.BufferingMedia, _Status.BufferedMedia)


class BriefingPlayer(QObject):
    """Plays queued briefing sections one after another (see module docstring).

    ``played`` lists the sections reached since the last ``start()``, in order,
    including the current one and any skipped because their audio failed or was
    empty. ``replace_upcoming`` on an idle/finished player starts the new queue
    but keeps that history, so "Read everything" after the end only needs the
    sections not yet in ``played``.
    """

    itemChanged = Signal(int, int)
    sectionStarted = Signal(int)
    stateChanged = Signal(str)
    errorOccurred = Signal(str)

    def __init__(self, parent: QObject | None = None, *, section_gap_ms: int = 600,
                 volume: float = 1.0) -> None:
        super().__init__(parent)
        self._section_gap_ms = max(0, int(section_gap_ms))

        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(min(max(float(volume), 0.0), 1.0))
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.errorOccurred.connect(self._on_media_error)
        self._player.positionChanged.connect(self._on_position)

        self._devices = QMediaDevices(self)
        self._devices.audioOutputsChanged.connect(self._on_outputs_changed)

        self._gap_timer = QTimer(self)
        self._gap_timer.setSingleShot(True)
        self._gap_timer.timeout.connect(self._on_gap_timeout)

        self._audio: dict[int, SectionAudio] = {}
        self._failed: set[int] = set()
        self._upcoming: list[int] = []
        self._played: list[int] = []
        self._current: int | None = None
        self._current_audio: SectionAudio | None = None
        self._segment_index = 0
        self._mark_starts: list[int] = []
        self._mark_items: list[int] = []
        self._phase = _PHASE_NONE
        self._gap_action = _NEXT_SEGMENT
        self._gap_remaining_ms = 0
        self._source = QUrl()
        self._media_ready = False
        self._state = IDLE
        self._last_item = (-1, -1)
        # Bumped by every command and transition. A multi-signal sequence stops
        # early if a connected slot re-entered the player while it was emitting.
        self._epoch = 0

    # ------------------------------------------------------------------ properties

    @property
    def state(self) -> str:
        return self._state

    @property
    def current_section(self) -> int | None:
        return self._current

    @property
    def played(self) -> list[int]:
        return list(self._played)

    def current_item_ms(self) -> int:
        """How long the audio says the item now highlighted: from its mark to the next one (or to its
        segment's end); 0 when unknown. Home's caption paces its words by it."""
        audio, (section, item) = self._current_audio, self._last_item
        if audio is None or section != self._current or item not in self._mark_items:
            return 0
        if not 0 <= self._segment_index < len(audio.segments):
            return 0
        i = self._mark_items.index(item)
        end = (self._mark_starts[i + 1] if i + 1 < len(self._mark_starts)
               else audio.segments[self._segment_index].duration_ms)
        return max(0, int(end) - int(self._mark_starts[i]))

    # ------------------------------------------------------------------ public API

    def set_audio(self, section_index: int, audio: SectionAudio) -> None:
        """Register a section's audio; start it now if playback is waiting on it."""
        self._audio[section_index] = audio
        self._failed.discard(section_index)
        if section_index == self._current and self._state == WAITING:
            logger.debug("Audio for section %d arrived", section_index)
            self._epoch += 1
            self._enter_current_or_advance()

    def mark_failed(self, section_index: int) -> None:
        """No audio will come for this section; skip it if waiting on it or when reached.

        A section that is already playing keeps playing the audio it has.
        """
        self._failed.add(section_index)
        self._audio.pop(section_index, None)
        if section_index == self._current and self._state == WAITING:
            logger.info("Section %d has no audio; skipping it", section_index)
            self._epoch += 1
            self._advance()

    def start(self, queue: Sequence[int]) -> None:
        """Play these sections in order from the beginning of the first one."""
        logger.info("Starting playback of %d section(s)", len(queue))
        self._played = []
        self._restart(queue)

    def replace_upcoming(self, queue: Sequence[int]) -> None:
        """Replace everything after the current section; start ``queue`` if idle/finished.

        If ``queue`` contains the current section, only what follows it is used,
        so passing the full desired order also works.
        """
        new = [int(i) for i in queue]
        if self._state in (IDLE, FINISHED):
            logger.info("Starting playback of %d more section(s)", len(new))
            self._restart(new)
            return
        if self._current in new:
            new = new[new.index(self._current) + 1:]
        logger.debug("Upcoming sections replaced: %s", new)
        self._upcoming = new

    def pause(self) -> None:
        """Pause speech or hold the current silence. No-op unless playing/waiting."""
        if self._state not in (PLAYING, WAITING):
            return
        self._epoch += 1
        if self._phase == _PHASE_SEGMENT:
            self._player.pause()
        elif self._phase == _PHASE_GAP:
            self._gap_remaining_ms = max(0, self._gap_timer.remainingTime())
            self._gap_timer.stop()
        logger.info("Paused")
        self._set_state(PAUSED)

    def resume(self) -> None:
        """Continue where pause() left off. No-op unless paused."""
        if self._state != PAUSED:
            return
        self._epoch += 1
        logger.info("Resumed")
        # Act first, announce second, so a slot that pauses again sees a consistent player.
        if self._phase == _PHASE_SEGMENT:
            self._player.play()
            self._set_state(PLAYING)
        elif self._phase == _PHASE_GAP:
            self._gap_timer.start(self._gap_remaining_ms)
            self._set_state(PLAYING)
        else:
            self._enter_current_or_advance()

    def toggle(self) -> None:
        """Pause when playing or waiting, resume when paused; otherwise do nothing."""
        if self._state in (PLAYING, WAITING):
            self.pause()
        elif self._state == PAUSED:
            self.resume()

    def skip_section(self) -> None:
        """Abandon the current section and go straight to the next one.

        Skipping while paused starts the next section playing.
        """
        if self._state not in (PLAYING, PAUSED, WAITING):
            return
        logger.info("Skipping section %s", self._current)
        self._epoch += 1
        self._advance()

    def stop(self) -> None:
        """Stop, forget the queue and release the audio file so it can be deleted.

        Registered audio and failed marks are kept for a later start().
        """
        self._epoch += 1
        epoch = self._epoch
        self._cancel_gap()
        self._release_media()
        self._upcoming = []
        self._current = None
        self._current_audio = None
        if self._state != IDLE:
            logger.info("Stopped")
        self._set_state(IDLE)
        if epoch == self._epoch:
            self._emit_item(-1, -1)

    # ------------------------------------------------------------------ sequencing

    def _restart(self, queue: Sequence[int]) -> None:
        self._epoch += 1
        self._upcoming = [int(i) for i in queue]
        self._current = None
        self._advance()

    def _advance(self) -> None:
        """Leave the current section and enter the next playable one, or finish."""
        self._cancel_gap()
        self._release_media()
        self._current_audio = None
        while self._upcoming:
            self._current = self._upcoming.pop(0)
            self._played.append(self._current)
            if self._enter_current():
                return
        self._finish()

    def _enter_current_or_advance(self) -> None:
        if not self._enter_current():
            self._advance()

    def _enter_current(self) -> bool:
        """Begin the current section or wait for its audio. False means skip it."""
        index = self._current
        if index is None:
            return False
        if index in self._failed:
            logger.info("Skipping section %d: its audio could not be generated", index)
            return False
        audio = self._audio.get(index)
        if audio is None:
            logger.info("Waiting for audio of section %d", index)
            self._phase = _PHASE_NONE
            self._set_state(WAITING)
            return True
        if not audio.segments:
            logger.info("Skipping section %d: nothing to say", index)
            return False
        self._begin_section(index, audio)
        return True

    def _begin_section(self, index: int, audio: SectionAudio) -> None:
        logger.info("Playing section %d (%d segment(s), %s)", index, len(audio.segments), audio.engine)
        self._current_audio = audio
        self._phase = _PHASE_NONE
        epoch = self._epoch
        self._set_state(PLAYING)
        if epoch != self._epoch:
            return
        self.sectionStarted.emit(index)
        if epoch != self._epoch:
            return
        self._play_segment(0)

    def _play_segment(self, number: int) -> None:
        self._epoch += 1
        self._release_media()
        audio = self._current_audio
        assert audio is not None
        segment = audio.segments[number]
        self._segment_index = number
        self._mark_starts = [start for start, _ in segment.marks]
        self._mark_items = [item for _, item in segment.marks]
        path = Path(segment.path)
        if not path.is_file():
            self._segment_failed(f"Audio file is missing: {path.name}")
            return
        url = QUrl.fromLocalFile(str(path))
        logger.debug("Section %s segment %d: %s", self._current, number, path.name)
        # Phase and expected source are set before setSource so that the
        # synchronous status signals it emits are attributed to this segment.
        self._phase = _PHASE_SEGMENT
        self._media_ready = False
        self._source = url
        self._player.setSource(url)
        if self._is_current_media():
            self._player.play()
            self._emit_item_at(0)

    def _segment_done(self) -> None:
        """The current segment ended (or failed): schedule what comes after it."""
        self._epoch += 1
        audio = self._current_audio
        assert audio is not None
        if self._segment_index + 1 < len(audio.segments):
            pause_ms = audio.segments[self._segment_index].pause_after_ms
            self._start_gap(pause_ms, _NEXT_SEGMENT)
        else:
            self._start_gap(self._section_gap_ms if self._upcoming else 0, _NEXT_SECTION)

    def _segment_failed(self, message: str) -> None:
        logger.warning("Section %s segment %d: %s", self._current, self._segment_index, message)
        self._segment_done()
        self.errorOccurred.emit(message)

    def _finish(self) -> None:
        self._epoch += 1
        epoch = self._epoch
        self._cancel_gap()
        self._release_media()
        self._upcoming = []
        self._current = None
        self._current_audio = None
        logger.info("Finished playback")
        self._set_state(FINISHED)
        if epoch == self._epoch:
            self._emit_item(-1, -1)

    # ------------------------------------------------------------------ gaps

    def _start_gap(self, duration_ms: int, action: int) -> None:
        self._phase = _PHASE_GAP
        self._gap_action = action
        duration_ms = max(0, int(duration_ms))
        if self._state == PLAYING:
            self._gap_timer.start(duration_ms)
        else:
            # The segment ended just as the user paused: hold the whole gap.
            self._gap_remaining_ms = duration_ms

    def _cancel_gap(self) -> None:
        self._gap_timer.stop()
        self._gap_remaining_ms = 0

    def _on_gap_timeout(self) -> None:
        if self._phase != _PHASE_GAP or self._state != PLAYING:
            return
        self._epoch += 1
        if self._gap_action == _NEXT_SEGMENT:
            self._play_segment(self._segment_index + 1)
        else:
            self._advance()

    # ------------------------------------------------------------------ media plumbing

    def _release_media(self) -> None:
        """Unload the current file (QMediaPlayer otherwise keeps it open on Windows)."""
        self._phase = _PHASE_NONE
        self._media_ready = False
        self._source = QUrl()
        if not self._player.source().isEmpty():
            self._player.stop()
            self._player.setSource(QUrl())

    def _is_current_media(self) -> bool:
        return self._phase == _PHASE_SEGMENT and self._player.source() == self._source

    def _on_media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if not self._is_current_media():
            return
        if status in _READY_STATUSES:
            self._media_ready = True
        elif status == _Status.EndOfMedia:
            # A queued end-of-media from the previous file can arrive right after
            # a switch; the new file has not reported Loaded yet at that point.
            if self._media_ready:
                self._segment_done()
            else:
                logger.debug("Ignoring a stale end-of-media signal")
        elif status == _Status.InvalidMedia:
            name = Path(self._source.toLocalFile()).name
            detail = self._player.errorString() or "unsupported or corrupt audio"
            self._segment_failed(f"Could not play {name}: {detail}")

    def _on_media_error(self, error: QMediaPlayer.Error, message: str) -> None:
        if error == QMediaPlayer.Error.NoError:
            return
        if not self._is_current_media():
            logger.debug("Ignoring a media error for audio that is no longer playing: %s", message)
            return
        self._segment_failed(f"Audio playback error: {message or error.name}")

    def _on_position(self, position_ms: int) -> None:
        if self._media_ready and self._is_current_media():
            self._emit_item_at(position_ms)

    def _on_outputs_changed(self) -> None:
        device = QMediaDevices.defaultAudioOutput()
        if device.isNull():
            logger.warning("No audio output device is available")
            return
        if device != self._audio_output.device():
            logger.info("Switching audio output to the new default device: %s", device.description())
            self._audio_output.setDevice(device)

    # ------------------------------------------------------------------ signals

    def _emit_item_at(self, position_ms: int) -> None:
        if self._current is None or not self._mark_starts:
            return
        i = bisect.bisect_right(self._mark_starts, position_ms) - 1
        self._emit_item(self._current, self._mark_items[max(i, 0)])

    def _emit_item(self, section_index: int, item_index: int) -> None:
        item = (section_index, item_index)
        if item == self._last_item:
            return
        self._last_item = item
        self.itemChanged.emit(section_index, item_index)

    def _set_state(self, state: str) -> None:
        if state == self._state:
            return
        logger.debug("Player state %s -> %s", self._state, state)
        self._state = state
        self.stateChanged.emit(state)
