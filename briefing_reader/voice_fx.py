"""Jarvis's voice, timed (the approved motion prototype, part B): when each word is said and how loud the voice
is, for the hero orb's voice spikes and Home's word-by-word caption. Qt-free; never logs or keeps any text.

``VoiceTimeline(words, duration_ms, envelope)``
    One utterance: ``words`` are (start_ms, end_ms, text) from edge-tts's WordBoundary offsets (a word ends
    where the next one starts, the last one with the audio); ``envelope`` the Windows voice's level 0..1 at
    ``ENVELOPE_HZ`` read from its WAV; ``duration_ms`` the audio's length, pauses between segments included.
    ``timeline_from_audio(audio)`` joins a models.SectionAudio's segments (None without audio).
``level_at(timeline, ms)``
    The voice's level 0..1 at ``ms``: the envelope when there is one, else one syllable pulse per word (a quick
    rise at the word's start, falling over its voiced length, a dip between words); 0 outside the audio.
``word_times(words, timeline, fallback_ms=...)``
    When each caption word (``text.split()``) is said, in ms: matched in order to the word boundaries; a word
    not matched sits between its neighbours; without boundaries an even pace over the audio's length, or
    ``fallback_ms`` a word when the length is unknown too.
``segment_words(text, words, duration_ms)``
    edge-tts's (offset_ms, word) pairs as (start_ms, end_ms, word) spans.
``wav_envelope(path)``
    A PCM WAV's RMS level at ``ENVELOPE_HZ``, normalised to its loudest window (() when it cannot be read).
"""

from __future__ import annotations

import array
import math
import re
import sys
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENVELOPE_HZ = 30
FALLBACK_WORD_MS = 370            # the voice's pace when nothing is known (about 2.7 words a second)
_NORM = re.compile(r"[^0-9a-z]+")


@dataclass(frozen=True)
class VoiceTimeline:
    words: tuple[tuple[int, int, str], ...] = ()
    duration_ms: int = 0
    envelope: tuple[float, ...] = ()

    def __repr__(self) -> str:                    # never the words themselves
        return f"VoiceTimeline({len(self.words)} words, {self.duration_ms} ms, {len(self.envelope)} levels)"

    @property
    def has_words(self) -> bool:
        return bool(self.words)

    @property
    def has_envelope(self) -> bool:
        return bool(self.envelope)


def segment_words(text: str, words: Sequence[tuple[int, str]], duration_ms: int) -> tuple[tuple[int, int, str], ...]:
    """(start_ms, end_ms, word) spans from edge-tts's (offset_ms, word) pairs (``text`` is unused: kept for
    symmetry with tts.align_marks)."""
    del text
    ordered = sorted(((max(0, int(ms)), str(word or "")) for ms, word in words if str(word or "").strip()),
                     key=lambda pair: pair[0])
    spans: list[tuple[int, int, str]] = []
    for i, (start, word) in enumerate(ordered):
        end = ordered[i + 1][0] if i + 1 < len(ordered) else max(start + 1, int(duration_ms))
        spans.append((start, max(start + 1, end), word))
    return tuple(spans)


def timeline_from_audio(audio: Any) -> VoiceTimeline | None:
    """The timeline of a models.SectionAudio (its segments back to back, with their pauses)."""
    segments = tuple(getattr(audio, "segments", ()) or ())
    if not segments:
        return None
    words: list[tuple[int, int, str]] = []
    envelope: list[float] = []
    base = 0
    step = 1000.0 / ENVELOPE_HZ
    for segment in segments:
        duration = max(0, int(getattr(segment, "duration_ms", 0) or 0))
        for start, end, word in getattr(segment, "words", ()) or ():
            words.append((base + int(start), base + int(end), word))
        levels = tuple(getattr(segment, "envelope", ()) or ())
        if levels:
            while len(envelope) * step < base:            # silence up to this segment
                envelope.append(0.0)
            envelope.extend(float(level) for level in levels)
        pause = max(0, int(getattr(segment, "pause_after_ms", 0) or 0))
        base += duration + pause
    total = base - max(0, int(getattr(segments[-1], "pause_after_ms", 0) or 0))
    return VoiceTimeline(words=tuple(words), duration_ms=max(0, total), envelope=tuple(envelope))


def _hash01(index: int) -> float:
    """A fixed pseudo-random 0..1 per index (the same every time: stills and tests repeat)."""
    value = math.sin(index * 12.9898 + 78.233) * 43758.5453
    return value - math.floor(value)


def _word_level(start: int, end: int, word: str, index: int, ms: float) -> float:
    letters = len(_NORM.sub("", word.lower())) or 1
    voiced = max(80.0, min(float(end - start), 90.0 + 55.0 * letters))
    amp = 0.62 + 0.38 * _hash01(index)
    x = (ms - start) / voiced
    if x < 0.0:
        return 0.0
    if x <= 1.0:
        syllables = max(1, round(letters / 3.2))
        shape = math.sin(math.pi * x) ** 0.6
        ripple = 0.78 + 0.22 * abs(math.sin(math.pi * x * syllables))
        return amp * shape * ripple
    return amp * 0.12 * math.exp(-(ms - start - voiced) / 80.0)


def level_at(timeline: VoiceTimeline | None, ms: float) -> float:
    """See the module docstring."""
    if timeline is None or ms < 0.0 or (timeline.duration_ms and ms > timeline.duration_ms + 120):
        return 0.0
    if timeline.envelope:
        position = ms * ENVELOPE_HZ / 1000.0
        i = int(position)
        if i >= len(timeline.envelope):
            return 0.0
        a = timeline.envelope[i]
        b = timeline.envelope[i + 1] if i + 1 < len(timeline.envelope) else 0.0
        return max(0.0, min(1.0, a + (b - a) * (position - i)))
    if timeline.words:
        # the word now and the one before (its tail), found by bisection on the starts
        lo, hi = 0, len(timeline.words)
        while lo < hi:
            mid = (lo + hi) // 2
            if timeline.words[mid][0] <= ms:
                lo = mid + 1
            else:
                hi = mid
        level = 0.0
        for i in (lo - 1, lo - 2):
            if 0 <= i < len(timeline.words):
                start, end, word = timeline.words[i]
                level = max(level, _word_level(start, end, word, i, ms))
        return max(0.0, min(1.0, level))
    return 0.0


def _norm(word: str) -> str:
    return _NORM.sub("", str(word or "").lower())


def word_times(words: Sequence[str], timeline: VoiceTimeline | None,
               fallback_ms: int = FALLBACK_WORD_MS) -> list[int]:
    """See the module docstring."""
    count = len(words)
    if count == 0:
        return []
    duration = timeline.duration_ms if timeline is not None else 0
    if timeline is None or not timeline.words:
        if duration > 0:
            pace = duration / count
            return [round(i * pace) for i in range(count)]
        return [i * int(fallback_ms) for i in range(count)]
    found: list[int | None] = [None] * count
    bounds = timeline.words
    cursor = 0
    for i, word in enumerate(words):
        key = _norm(word)
        if not key:
            continue
        for j in range(cursor, min(len(bounds), cursor + 5)):
            other = _norm(bounds[j][2])
            if other and (other == key or key.startswith(other) or other.startswith(key)):
                found[i] = bounds[j][0]
                cursor = j + 1
                break
    end = max(duration, bounds[-1][0] + 1)
    times: list[int] = []
    for i in range(count):
        if found[i] is not None:
            times.append(int(found[i]))       # type: ignore[arg-type]
            continue
        # between the known neighbours (the first word is said at 0, the audio's end closes the line)
        k0, t0 = next(((k, int(found[k])) for k in range(i - 1, -1, -1)      # type: ignore[arg-type]
                       if found[k] is not None), (0, 0))
        k1, t1 = next(((k, int(found[k])) for k in range(i + 1, count)       # type: ignore[arg-type]
                       if found[k] is not None), (count, end))
        times.append(round(t0 + (t1 - t0) * (i - k0) / max(1, k1 - k0)))
    for i in range(1, count):                 # never earlier than the word before
        times[i] = max(times[i], times[i - 1])
    return times


def wav_envelope(path: str | Path, hz: int = ENVELOPE_HZ) -> tuple[float, ...]:
    """See the module docstring (8-, 16- or 32-bit PCM, any channel count)."""
    try:
        with wave.open(str(path), "rb") as fh:
            channels, width, rate = fh.getnchannels(), fh.getsampwidth(), fh.getframerate()
            frames = fh.readframes(fh.getnframes())
    except (OSError, EOFError, wave.Error):
        return ()
    typecode = {1: "b", 2: "h", 4: "i"}.get(width)
    if typecode is None or rate <= 0 or not frames:
        return ()
    samples = array.array(typecode)
    usable = len(frames) - len(frames) % width
    if width == 1:   # 8-bit WAV is unsigned
        samples.frombytes(bytes((b - 128) & 0xFF for b in frames[:usable]))
    else:
        samples.frombytes(frames[:usable])
        if sys.byteorder == "big":
            samples.byteswap()
    window = max(1, rate // hz) * channels
    stride = max(1, window // 64)             # 64 samples a window are plenty for a level
    levels: list[float] = []
    for start in range(0, len(samples), window):
        chunk = samples[start:start + window:stride]
        if not chunk:
            break
        levels.append(math.sqrt(sum(value * value for value in chunk) / len(chunk)))
    loudest = max(levels, default=0.0)
    if loudest <= 0.0:
        return tuple(0.0 for _ in levels)
    return tuple(round(min(1.0, level / loudest), 3) for level in levels)
