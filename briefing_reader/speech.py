"""Jarvis's own voice: the utterance kinds, the interface the controller speaks through, the
queue policy and the speech worker.

An *utterance* is one thing Jarvis says on his own (a greeting, "your briefing
is ready to view", an acknowledgement, an Ask reply, the result of an approved
card, a notice); never the briefing itself, which has its own player.

The controller only ever calls the :class:`Voice` protocol. :class:`SilentVoice`
is the default: it never synthesizes or plays anything, so a controller that is
never started (every unit test) cannot make a sound. The real voice
(voice_ui.AssistantVoice: this module's :class:`SpeechLane` and :class:`SayWorker`
plus its own player; edge-tts with the Windows voice as fallback) is built only
by ``AppController.start()`` when ``[assistant] speak = true``.

* :class:`SpeechLane` is the pure queue policy: one utterance at a time, first
  in first out, at most ``QUEUE_CAP`` waiting (the oldest acknowledgement or
  greeting goes first, then the oldest of any kind), each dropped when it is
  too old to start (``STALE_AFTER_S``), a reply drops an acknowledgement that
  has not started, nothing starts while a Send's hidden hold runs (``hold``) and
  nothing is queued while muted.
* :class:`SayWorker` is the daemon thread "tts-say" that owns its own speech
  synthesizer (made by a factory on that thread) and turns ``(seq, text)`` into
  audio. The synthesizer only ever sees the section key "say-<seq>" as a name;
  nothing here logs the text.

Qt-free; standard library only.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .models import ITEM_TEXT, ScriptItem, Section

logger = logging.getLogger(__name__)

KIND_GREETING = "greeting"
KIND_ANNOUNCE = "announce"
KIND_ACK = "ack"
KIND_REPLY = "reply"
KIND_RESULT = "result"
KIND_NOTICE = "notice"
KINDS = (KIND_GREETING, KIND_ANNOUNCE, KIND_ACK, KIND_REPLY, KIND_RESULT, KIND_NOTICE)

QUEUE_CAP = 4                  # utterances waiting to start (the one playing does not count)
# An utterance that has not started this many seconds after it was asked for is dropped (its
# conversation entry is shown already).
STALE_AFTER_S = {KIND_ACK: 8.0, KIND_GREETING: 20.0, KIND_ANNOUNCE: 20.0, KIND_REPLY: 30.0,
                 KIND_RESULT: 60.0, KIND_NOTICE: 60.0}
_DROPPED_FIRST = (KIND_ACK, KIND_GREETING)   # when the queue is full


@runtime_checkable
class Voice(Protocol):
    """What the controller may ask of Jarvis's voice. Every method is called on the GUI thread."""

    @property
    def speaking(self) -> bool:
        """An utterance is playing right now."""
        ...

    @property
    def muted(self) -> bool:
        ...

    def say(self, kind: str, text: str, *, key: str = "") -> bool:
        """Queue ``text`` (one of KINDS); False when it will not be spoken (muted, off, failed)."""
        ...

    def cancel_key(self, key: str) -> None:
        """Drop a queued utterance with this ``key`` that has not started (e.g. "announce:<K>")."""
        ...

    def stop(self) -> None:
        """Stop the current utterance now and drop the queue (the owner pressed Play, mute)."""
        ...

    def pump(self) -> None:
        """Look again whether the next utterance may start (a hold ended, playback changed)."""
        ...

    def owner_acted(self) -> None:
        """The owner touched the briefing's playback: never resume it on his behalf."""
        ...

    def set_muted(self, muted: bool) -> None:
        ...

    def shutdown(self) -> None:
        ...


class SilentVoice:
    """A voice that never speaks: ``say`` returns False and nothing is ever playing.

    ``muted`` follows ``set_muted`` (so a mute switch still shows its state), but
    nothing is synthesized either way.
    """

    def __init__(self) -> None:
        self._muted = False

    @property
    def speaking(self) -> bool:
        return False

    @property
    def muted(self) -> bool:
        return self._muted

    def say(self, kind: str, text: str, *, key: str = "") -> bool:
        return False

    def cancel_key(self, key: str) -> None:
        return None

    def stop(self) -> None:
        return None

    def pump(self) -> None:
        return None

    def owner_acted(self) -> None:
        return None

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)

    def shutdown(self) -> None:
        return None


# --------------------------------------------------------------------------
# The queue policy
# --------------------------------------------------------------------------

@dataclass
class Utterance:
    """One thing to say: ``seq`` (unique in the lane), its kind, the text (already scrubbed for
    speech), an optional ``key`` (``cancel_key``), when it was asked for (the lane's clock) and,
    once synthesized, its audio (models.SectionAudio)."""

    seq: int
    kind: str
    text: str
    key: str = ""
    created: float = 0.0
    audio: Any = None

    @property
    def ready(self) -> bool:
        return self.audio is not None


class SpeechLane:
    """Which utterance plays next (pure policy; the caller plays it and calls ``finish``).

    ``push`` queues (nothing while muted); ``set_audio`` / ``fail`` report the synthesis;
    ``start_next(hold)`` returns the utterance to play now, or None (one is playing, the head is
    not synthesized yet, ``hold`` - a Send's hidden hold - is on, or nothing is left). Every method
    returns or logs what it dropped (kinds only, never the text).
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, cap: int = QUEUE_CAP) -> None:
        self._clock = clock
        self._cap = max(1, int(cap))
        self._queue: list[Utterance] = []
        self._current: Utterance | None = None
        self._seq = 0
        self._muted = False

    # ---- state ----------------------------------------------------------------------

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def current(self) -> Utterance | None:
        """The utterance playing now (between ``start_next`` and ``finish``)."""
        return self._current

    @property
    def pending(self) -> list[Utterance]:
        """The utterances waiting to start, oldest first."""
        return list(self._queue)

    @property
    def idle(self) -> bool:
        """Nothing playing and nothing waiting."""
        return self._current is None and not self._queue

    def find(self, seq: int) -> Utterance | None:
        if self._current is not None and self._current.seq == seq:
            return self._current
        return next((item for item in self._queue if item.seq == seq), None)

    # ---- changes ----------------------------------------------------------------------

    def set_muted(self, muted: bool) -> list[Utterance]:
        """Muted: everything waiting and playing is dropped and nothing is queued until unmuted."""
        self._muted = bool(muted)
        return self.clear() if self._muted else []

    def push(self, kind: str, text: str, key: str = "") -> tuple[Utterance | None, list[Utterance]]:
        """Queue ``text``; (the utterance or None while muted or empty, what was dropped for it)."""
        if self._muted or not text:
            return None, []
        dropped: list[Utterance] = []
        if kind == KIND_REPLY:   # the answer is here: an acknowledgement still waiting is pointless
            dropped += self._drop([item for item in self._queue if item.kind == KIND_ACK], "superseded")
        while len(self._queue) >= self._cap:
            oldest = next((item for item in self._queue if item.kind in _DROPPED_FIRST), self._queue[0])
            dropped += self._drop([oldest], "queue full")
        self._seq += 1
        utterance = Utterance(self._seq, kind if kind in KINDS else KIND_NOTICE, text, key or "", self._clock())
        self._queue.append(utterance)
        return utterance, dropped

    def set_audio(self, seq: int, audio: Any) -> bool:
        """The audio of a waiting utterance arrived; False when it is no longer waiting."""
        item = next((item for item in self._queue if item.seq == seq), None)
        if item is None:
            return False
        item.audio = audio
        return True

    def fail(self, seq: int) -> Utterance | None:
        """Its synthesis failed: it is dropped (returned, or None when it was gone already)."""
        item = next((item for item in self._queue if item.seq == seq), None)
        if item is not None:
            self._queue.remove(item)
        return item

    def cancel_key(self, key: str) -> list[Utterance]:
        """Drop the waiting utterances with ``key`` (never the one playing; "" matches nothing)."""
        if not key:
            return []
        return self._drop([item for item in self._queue if item.key == key], "cancelled")

    def clear(self) -> list[Utterance]:
        """Drop everything, the one playing included (it is the caller's to stop)."""
        dropped = list(self._queue)
        self._queue.clear()
        if self._current is not None:
            dropped.insert(0, self._current)
            self._current = None
        return dropped

    def drop_stale(self) -> list[Utterance]:
        """Drop the waiting utterances that are too old to start now."""
        now = self._clock()
        stale = [item for item in self._queue
                 if now - item.created > STALE_AFTER_S.get(item.kind, STALE_AFTER_S[KIND_NOTICE])]
        return self._drop(stale, "stale")

    def start_next(self, *, hold: bool = False) -> Utterance | None:
        """The utterance to play now (it becomes ``current``), or None. Stale ones are dropped first;
        ``hold`` (a Send's hidden hold) starts nothing; the head waits for its audio (first in, first
        out)."""
        if self._current is not None:
            return None
        self.drop_stale()
        if hold or not self._queue or not self._queue[0].ready:
            return None
        self._current = self._queue.pop(0)
        return self._current

    def finish(self, seq: int | None = None) -> None:
        """The current utterance ended (``seq``: only if it is that one)."""
        if self._current is not None and (seq is None or self._current.seq == seq):
            self._current = None

    def _drop(self, items: list[Utterance], why: str) -> list[Utterance]:
        for item in items:
            if item in self._queue:
                self._queue.remove(item)
                logger.info("Say dropped: %s (%s)", why, item.kind)
        return items


# --------------------------------------------------------------------------
# The speech worker
# --------------------------------------------------------------------------

def say_section(seq: int, text: str) -> Section:
    """The one-item section an utterance is synthesized as; its key "say-<seq>" is all the
    synthesizer logs."""
    return Section(key=f"say-{seq}", title="", items=(ScriptItem(ITEM_TEXT, text, text),))


class SayWorker(threading.Thread):
    """Daemon thread "tts-say" that owns its own speech synthesizer and processes ``(seq, text)``
    jobs first in, first out.

    ``synth_factory()`` runs on this thread (SAPI/COM is thread-bound). ``on_result(seq, audio,
    error)`` runs on this thread too: ``audio`` is a SectionAudio, or None with ``error`` (a short
    reason that never holds the text). A job ``forget``-ten before it starts is skipped. A failing
    synthesizer or callback never ends the thread.
    """

    def __init__(self, synth_factory: Callable[[], Any],
                 on_result: Callable[[int, Any, str | None], None]) -> None:
        super().__init__(name="tts-say", daemon=True)
        self._synth_factory = synth_factory
        self._on_result = on_result
        self._jobs: queue.Queue[tuple[int, str] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._forgotten: set[int] = set()
        self._stop_requested = threading.Event()

    def submit(self, seq: int, text: str) -> None:
        self._jobs.put((int(seq), str(text)))

    def forget(self, seq: int) -> None:
        """Skip this job if it has not started (its utterance was dropped)."""
        with self._lock:
            self._forgotten.add(int(seq))

    def stop(self) -> None:
        """Finish the current job, then exit. Never blocks the caller."""
        self._stop_requested.set()
        self._jobs.put(None)

    def run(self) -> None:
        synth, startup_error = self._create_synth()
        try:
            while not self._stop_requested.is_set():
                job = self._jobs.get()
                if job is None or self._stop_requested.is_set():
                    break
                seq, text = job
                with self._lock:
                    if seq in self._forgotten:
                        self._forgotten.discard(seq)
                        continue
                audio, error = self._process(synth, startup_error, seq, text)
                if self._stop_requested.is_set():
                    break
                self._deliver(seq, audio, error)
        finally:
            if synth is not None:
                try:
                    synth.close()
                except Exception:  # noqa: BLE001 - shutdown must not fail
                    logger.warning("Closing Jarvis's speech synthesizer failed")

    def _create_synth(self) -> tuple[Any, str | None]:
        try:
            return self._synth_factory(), None
        except Exception as exc:  # noqa: BLE001 - reported to every job instead
            logger.warning("Could not start Jarvis's speech engine (%s)", type(exc).__name__)
            return None, f"Could not start the speech engine ({type(exc).__name__})"

    @staticmethod
    def _process(synth: Any, startup_error: str | None, seq: int, text: str) -> tuple[Any, str | None]:
        if synth is None:
            return None, startup_error or "The speech engine is not available"
        try:
            return synth.synthesize_section(say_section(seq, text)), None
        except Exception as exc:  # noqa: BLE001 - keep the worker alive; never the text in the log
            logger.warning("Say synthesis failed for say-%d (%s)", seq, type(exc).__name__)
            return None, f"Speech synthesis failed ({type(exc).__name__})"

    def _deliver(self, seq: int, audio: Any, error: str | None) -> None:
        try:
            self._on_result(seq, audio, error)
        except Exception:  # noqa: BLE001 - a broken callback must not kill the worker
            logger.warning("The result of say-%d could not be delivered", seq)
