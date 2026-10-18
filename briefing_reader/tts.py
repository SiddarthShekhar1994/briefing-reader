"""Text-to-speech: edge-tts synthesis with a Windows SAPI fallback.

A :class:`Section` is split into audio segments at its pause items (dividers);
each segment becomes one audio file plus "marks" that say when each item of
the section starts, so the player can highlight roughly where the audio is.

edge-tts (online, free) is tried first and produces mp3 with word timings.
When it fails, Windows SAPI via pyttsx3 produces a WAV and the marks are
estimated from character positions. After an edge failure the synthesizer
stays on SAPI for a cooldown period before trying edge again.

Microsoft serves the online voice at two addresses: the library's own
("usual") and the newer api.msedgeservices.com one ("newer"); regions move
from one to the other and the old one then refuses the connection. When the
service refuses an attempt, :data:`edge_address` (one selector shared by every
synthesizer in the process) switches edge_tts to the other address and the
same segment is retried there once; only when both refuse does the cooldown
and SAPI fallback apply. The address is not part of the audio cache key.

This module is Qt-free. :class:`TtsWorker` runs synthesis on a background
thread and reports results through a plain callback.
"""

from __future__ import annotations

import asyncio
import bisect
import gc
import hashlib
import logging
import os
import queue
import re
import threading
import time
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import aiohttp
import edge_tts
from edge_tts import communicate as edge_communicate
from edge_tts import constants as edge_constants
from edge_tts.exceptions import NoAudioReceived

from . import voice_fx
from .models import ITEM_PAUSE, AudioSegment, Section, SectionAudio

logger = logging.getLogger(__name__)

EDGE = "edge"
SAPI = "sapi"

USUAL = "usual"                 # edge-tts's own service address, as installed
NEWER = "newer"                 # Microsoft's newer Read Aloud address
_NEWER_BASE_URL = "api.msedgeservices.com/tts/cognitiveservices"

_EDGE_CONNECT_TIMEOUT_S = 6
_EDGE_RECEIVE_TIMEOUT_S = 30
_EDGE_KBITS_PER_S = 48          # edge-tts output is 24 kHz mono 48 kbit/s CBR mp3
_TICKS_PER_MS = 10_000          # WordBoundary offsets are in 100-ns ticks

_SAPI_BASE_WPM = 200
_SAPI_MIN_WPM = 80
_SAPI_MAX_WPM = 400
_WAV_HEADER_BYTES = 44

_PERCENT_RE = re.compile(r"^\s*([+-]?)(\d+)\s*%\s*$")
_URL_QUERY_RE = re.compile(r"((?:https?|wss?)://[^\s?#'\"<>]+)[?#][^\s'\"<>]*", re.IGNORECASE)


class TtsError(Exception):
    """No engine could synthesize the audio. The message is safe to show and log."""


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def parse_percent(value: str) -> int:
    """Parse "+15%" / "-10%" / "15%" into an int; anything else gives 0."""
    if not isinstance(value, str):
        return 0
    match = _PERCENT_RE.match(value)
    if match is None:
        return 0
    sign, digits = match.groups()
    return -int(digits) if sign == "-" else int(digits)


def split_segments(section: Section, divider_pause_ms: int) -> list[tuple[list[tuple[int, str]], int]]:
    """Group a section's spoken items into segments separated by pause items.

    Returns ``[([(item_index, spoken), ...], pause_after_ms), ...]``. Items with
    nothing to say are skipped, empty segments are dropped, and the last
    segment never carries a pause (the player gaps between sections itself).
    """
    segments: list[tuple[list[tuple[int, str]], int]] = []
    current: list[tuple[int, str]] = []
    for index, item in enumerate(section.items):
        if item.kind == ITEM_PAUSE:
            if current:
                segments.append((current, max(0, int(divider_pause_ms))))
                current = []
            continue
        spoken = (item.spoken or "").strip()
        if spoken:
            current.append((index, spoken))
    if current:
        segments.append((current, 0))
    if segments:
        last_items, _ = segments[-1]
        segments[-1] = (last_items, 0)
    return segments


def build_segment_text(items: Sequence[tuple[int, str]]) -> tuple[str, list[tuple[int, int]]]:
    """Join spoken texts with newlines; return (text, [(char_start, item_index), ...])."""
    parts: list[str] = []
    starts: list[tuple[int, int]] = []
    position = 0
    for item_index, spoken in items:
        if parts:
            position += 1  # the "\n" separator
        starts.append((position, item_index))
        parts.append(spoken)
        position += len(spoken)
    return "\n".join(parts), starts


def align_marks(text: str, starts: Sequence[tuple[int, int]], words: Sequence[tuple[int, str]],
                duration_ms: int) -> tuple[tuple[int, int], ...]:
    """Turn word timings into one (start_ms, item_index) mark per item.

    ``words`` are (offset_ms, word_text) pairs from edge-tts WordBoundary events,
    in spoken order. Items whose words could not be located are interpolated by
    character position between their neighbours.
    """
    ordered = sorted(starts)
    if not ordered:
        return ()
    located = _locate_words(text, words)
    positions = [pos for pos, _ in located]
    known: list[int | None] = []
    for i, (char_start, _item) in enumerate(ordered):
        char_end = ordered[i + 1][0] if i + 1 < len(ordered) else len(text) + 1
        j = bisect.bisect_left(positions, char_start)
        known.append(located[j][1] if j < len(located) and positions[j] < char_end else None)

    end_ms = max([max(0, int(duration_ms))] + [ms for ms in known if ms is not None])
    end_point = (max(len(text), ordered[-1][0]), end_ms)
    marks: list[int] = []
    for i, (char_start, _item) in enumerate(ordered):
        ms = known[i]
        if ms is None:
            before = next(((ordered[k][0], known[k]) for k in range(i - 1, -1, -1)
                           if known[k] is not None), (0, 0))
            after = next(((ordered[k][0], known[k]) for k in range(i + 1, len(ordered))
                          if known[k] is not None), end_point)
            ms = _lerp(char_start, before, after)  # type: ignore[arg-type]
        marks.append(ms)
    return _finalize_marks(ordered, marks)


def proportional_marks(text_len: int, starts: Sequence[tuple[int, int]],
                       duration_ms: int) -> tuple[tuple[int, int], ...]:
    """Estimate item marks from character position alone (used for SAPI audio)."""
    ordered = sorted(starts)
    duration = max(0, int(duration_ms))
    marks: list[int] = []
    for char_start, _item in ordered:
        if text_len <= 0:
            marks.append(0)
        else:
            clamped = min(max(char_start, 0), text_len)
            marks.append(round(duration * clamped / text_len))
    return _finalize_marks(ordered, marks)


def _locate_words(text: str, words: Sequence[tuple[int, str]]) -> list[tuple[int, int]]:
    """Find each word in ``text`` scanning forward; returns [(char_pos, offset_ms)]."""
    located: list[tuple[int, int]] = []
    cursor = 0
    for offset_ms, word in words:
        word = (word or "").strip()
        if not word:
            continue
        pos = text.find(word, cursor)
        if pos >= 0:
            end = pos + len(word)
        else:
            # A regex keeps positions exact where str.lower() could change lengths.
            match = re.compile(re.escape(word), re.IGNORECASE).search(text, cursor)
            if match is None:
                continue
            pos, end = match.start(), match.end()
        located.append((pos, max(0, int(offset_ms))))
        cursor = end
    return located


def _lerp(x: int, start: tuple[int, int], end: tuple[int, int]) -> int:
    (x0, y0), (x1, y1) = start, end
    if x1 <= x0:
        return y0
    fraction = min(max((x - x0) / (x1 - x0), 0.0), 1.0)
    return round(y0 + (y1 - y0) * fraction)


def _finalize_marks(ordered: Sequence[tuple[int, int]], marks: Sequence[int]) -> tuple[tuple[int, int], ...]:
    """First mark at 0 ms, never decreasing, paired with item indices in text order."""
    result: list[tuple[int, int]] = []
    running = 0
    for i, ((_char, item_index), ms) in enumerate(zip(ordered, marks)):
        running = 0 if i == 0 else max(running, int(ms))
        result.append((running, item_index))
    return tuple(result)


def _normalize_percent(value: str) -> str:
    return f"{parse_percent(value):+d}%"


def _sapi_rate_wpm(rate: str) -> int:
    wpm = round(_SAPI_BASE_WPM * (1 + parse_percent(rate) / 100))
    return min(max(wpm, _SAPI_MIN_WPM), _SAPI_MAX_WPM)


def _sapi_volume_level(volume: str) -> float:
    return min(max(1 + parse_percent(volume) / 100, 0.0), 1.0)


def _describe_error(exc: BaseException) -> str:
    """Exception type plus a short message, with URL query strings removed."""
    try:
        text = str(exc)
    except Exception:  # noqa: BLE001 - a broken __str__ must not hide the error itself
        text = ""
    message = _URL_QUERY_RE.sub(r"\1", text).strip()
    if len(message) > 200:
        message = message[:200] + "..."
    name = type(exc).__name__
    return f"{name}: {message}" if message else name


def _refusal(exc: BaseException) -> aiohttp.ClientResponseError | None:
    """The aiohttp response error behind ``exc`` (itself or a cause), or None.

    A refused address answers the WebSocket handshake with an HTTP status
    (WSServerHandshakeError, a ClientResponseError); edge-tts may wrap a 403
    in its own error with the response as the cause.
    """
    current: BaseException | None = exc
    for _ in range(4):
        if current is None:
            break
        if isinstance(current, (aiohttp.WSServerHandshakeError, aiohttp.ClientResponseError)):
            return current
        current = current.__cause__
    return None


def _part_path(path: Path) -> Path:
    return path.with_name(path.name + ".part")


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Could not remove %s: %s", path.name, exc)


def _wav_duration_ms(path: Path) -> int:
    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        frames = wav.getnframes()
    return round(frames * 1000 / rate) if rate > 0 else 0


# --------------------------------------------------------------------------
# Online voice address
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _EdgeAddress:
    """One set of edge-tts service URLs, known by a short name ("usual" or "newer")."""

    name: str
    base_url: str
    wss_url: str
    voice_list: str


def _usual_address() -> _EdgeAddress:
    """The library's own address, read from edge_tts.constants once at import (never hard-coded)."""
    return _EdgeAddress(USUAL, edge_constants.BASE_URL, edge_constants.WSS_URL, edge_constants.VOICE_LIST)


def _newer_address() -> _EdgeAddress:
    """Microsoft's newer Read Aloud address, with the library's own client token."""
    token = edge_constants.TRUSTED_CLIENT_TOKEN
    return _EdgeAddress(
        NEWER, _NEWER_BASE_URL,
        f"wss://{_NEWER_BASE_URL}/websocket/v1?Ocp-Apim-Subscription-Key={token}",
        f"https://{_NEWER_BASE_URL}/voices/list?Ocp-Apim-Subscription-Key={token}")


class EdgeAddressSelector:
    """Which address edge-tts connects to: one choice for the whole process.

    Every SpeechSynthesizer (the app runs two, on the "tts-worker" and "tts-say"
    threads) shares it, so a refusal seen by one moves both. ``apply`` writes
    edge_tts.constants.BASE_URL / WSS_URL / VOICE_LIST and
    edge_tts.communicate.WSS_URL together under a lock (communicate.py imports
    the name at load and reads its own module global at connect time) and is
    idempotent. Nothing is written at import: "usual" is what the library has.
    """

    def __init__(self, addresses: Sequence[_EdgeAddress], start: str = USUAL) -> None:
        self._addresses = {address.name: address for address in addresses}
        self._lock = threading.Lock()
        self._current = start
        self._confirmed: str | None = start   # the address whose success was last announced

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._addresses)

    @property
    def current(self) -> str:
        """Name of the address edge-tts is pointed at."""
        with self._lock:
            return self._current

    def urls(self, name: str) -> tuple[str, str, str]:
        """(BASE_URL, WSS_URL, VOICE_LIST) of the address called ``name``."""
        address = self._addresses[name]
        return address.base_url, address.wss_url, address.voice_list

    def apply(self, name: str) -> None:
        """Point edge_tts at the address called ``name`` (idempotent; safe from any thread)."""
        with self._lock:
            self._apply_locked(name)

    def reset(self) -> None:
        """Back to the library's own address with nothing left to announce (tests)."""
        with self._lock:
            self._apply_locked(USUAL)
            self._confirmed = USUAL

    def switch_from(self, seen: str) -> str:
        """After a refusal at ``seen``: move to the other address, unless another thread already did.

        Returns the name to retry at. Never moves twice for one refusal, so two
        synthesizers refused at the same time cannot swap the address back and forth.
        """
        with self._lock:
            if self._current == seen:
                self._apply_locked(self._other(seen))
            return self._current

    def confirm(self) -> str | None:
        """Record a success at the current address; its name the first time since the address moved, else None.

        The selector names the address, not the caller: edge-tts reads the URL at connect time, so
        a thread that looked before another thread's switch still connected at the new address.
        """
        with self._lock:
            if self._confirmed == self._current:
                return None
            self._confirmed = self._current
            return self._current

    def snapshot(self) -> tuple[str, str, str, str, str]:
        """(current, BASE_URL, WSS_URL, VOICE_LIST, communicate.WSS_URL), read together under the lock."""
        with self._lock:
            return (self._current, edge_constants.BASE_URL, edge_constants.WSS_URL,
                    edge_constants.VOICE_LIST, edge_communicate.WSS_URL)

    def _other(self, name: str) -> str:
        return next(other for other in self._addresses if other != name)

    def _apply_locked(self, name: str) -> None:
        address = self._addresses[name]
        if name != self._current:
            self._confirmed = None
        edge_constants.BASE_URL = address.base_url
        edge_constants.WSS_URL = address.wss_url
        edge_constants.VOICE_LIST = address.voice_list
        edge_communicate.WSS_URL = address.wss_url
        self._current = name


edge_address = EdgeAddressSelector((_usual_address(), _newer_address()))


# --------------------------------------------------------------------------
# Synthesizer
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _Clip:
    """A synthesized audio file and what is needed to compute its marks."""

    path: Path
    duration_ms: int
    words: tuple[tuple[int, str], ...]
    engine: str
    envelope: tuple[float, ...] = ()     # the Windows voice's level (voice_fx.wav_envelope)


class SpeechSynthesizer:
    """Blocking synthesis. Create and use it on ONE thread (SAPI/COM is thread-bound)."""

    def __init__(self, *, voice: str, rate: str, volume: str, offline_voice: str, out_dir: Path,
                 divider_pause_ms: int = 900, edge_retry_after_s: float = 120.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._voice = voice
        self._rate = _normalize_percent(rate)
        self._volume = _normalize_percent(volume)
        self._offline_voice = (offline_voice or "").strip()
        self._out_dir = Path(out_dir)
        self._divider_pause_ms = divider_pause_ms
        self._edge_retry_after_s = float(edge_retry_after_s)
        self._clock = clock
        self._edge_failed_at: float | None = None
        self._cache: dict[tuple[str, str, str, str, str], _Clip] = {}
        self._last_engine = EDGE
        self._sapi_engine: Any = None
        self._com_initialized = False

    @property
    def last_engine(self) -> str:
        """Engine used for the most recently synthesized segment."""
        return self._last_engine

    def synthesize_section(self, section: Section) -> SectionAudio:
        """Synthesize every segment of ``section``; raises TtsError if both engines fail."""
        segments: list[AudioSegment] = []
        engines: list[str] = []
        for items, pause_after_ms in split_segments(section, self._divider_pause_ms):
            text, starts = build_segment_text(items)
            segment, engine = self._synthesize_segment(text, starts, pause_after_ms)
            segments.append(segment)
            engines.append(engine)
            self._last_engine = engine
        engine = SAPI if SAPI in engines else (EDGE if engines else self._last_engine)
        total_ms = sum(segment.duration_ms for segment in segments)
        logger.info("Section %s: %d segment(s), %.1f s of audio, engine %s",
                    section.key, len(segments), total_ms / 1000, engine)
        return SectionAudio(section_key=section.key, segments=tuple(segments), engine=engine)

    def close(self) -> None:
        """Release the SAPI engine and COM if this synthesizer initialised them."""
        engine, self._sapi_engine = self._sapi_engine, None
        if engine is not None:
            del engine
            gc.collect()  # release the COM objects before COM itself goes away
        if self._com_initialized:
            self._com_initialized = False
            try:
                import comtypes
                comtypes.CoUninitialize()
            except Exception as exc:  # noqa: BLE001 - shutdown must not fail
                logger.debug("CoUninitialize failed: %s", _describe_error(exc))

    # ---- segment-level engine choice -------------------------------------

    def _synthesize_segment(self, text: str, starts: list[tuple[int, int]],
                            pause_after_ms: int) -> tuple[AudioSegment, str]:
        clip = self._cached(self._cache_key(EDGE, text))
        if clip is None and self._edge_allowed():
            clip = self._try_edge(text)
        if clip is None:
            clip = self._cached(self._cache_key(SAPI, text))
            if clip is not None:
                logger.debug("Reusing cached SAPI audio %s", clip.path.name)
            else:
                clip = self._run_sapi(text)
        return self._make_segment(clip, text, starts, pause_after_ms), clip.engine

    def _edge_allowed(self) -> bool:
        if self._edge_failed_at is None:
            return True
        remaining = self._edge_retry_after_s - (self._clock() - self._edge_failed_at)
        if remaining <= 0:
            logger.info("Retrying edge-tts after the cooldown")
            return True
        logger.debug("edge-tts in cooldown for another %.0f s; using SAPI", remaining)
        return False

    def _try_edge(self, text: str) -> _Clip | None:
        key = self._cache_key(EDGE, text)
        path = self._path_for(key, ".mp3")
        address = edge_address.current
        try:
            duration_ms, words = self._edge_attempt(text, path)
        except Exception as exc:  # noqa: BLE001 - any edge failure means "fall back"
            retry_at = self._address_after_refusal(address, exc)
            if retry_at is None:
                self._on_edge_failure(exc)
                return None
            try:   # the same segment once more, at the other address; never a second switch
                duration_ms, words = self._edge_attempt(text, path)
            except Exception as retry_exc:  # noqa: BLE001 - both addresses failed: cooldown + SAPI
                self._on_edge_failure(retry_exc)
                return None
        announced = edge_address.confirm()
        if announced:
            logger.info("edge-tts: using the %s address", announced)
        if self._edge_failed_at is not None:
            logger.info("edge-tts is working again; leaving the offline voice")
            self._edge_failed_at = None
        clip = _Clip(path, max(0, int(duration_ms)), tuple(words), EDGE)
        self._cache[key] = clip
        return clip

    def _edge_attempt(self, text: str, path: Path) -> tuple[int, tuple[tuple[int, str], ...]]:
        duration_ms, words = self._edge_to_file(text, path)
        if not path.is_file():
            raise TtsError("edge-tts did not produce a file")
        return duration_ms, words

    @staticmethod
    def _address_after_refusal(address: str, exc: BaseException) -> str | None:
        """The address to retry at when the service refused ``address``; None for any other failure."""
        refusal = _refusal(exc)
        if refusal is None:
            return None
        retry_at = edge_address.switch_from(address)
        status = refusal.status if refusal.status is not None else "?"
        logger.warning("edge-tts refused at the %s address (%s: %s); trying the %s address",
                       address, type(refusal).__name__, status, retry_at)
        return retry_at

    def _on_edge_failure(self, exc: BaseException) -> None:
        if isinstance(exc, NoAudioReceived):
            # The service answered, so the network is fine: no cooldown for this.
            logger.warning("edge-tts returned no audio for a segment; using Windows SAPI for it")
            return
        self._edge_failed_at = self._clock()
        logger.warning("edge-tts failed (%s); using Windows SAPI and retrying edge-tts in %.0f s",
                       _describe_error(exc), self._edge_retry_after_s)

    def _run_sapi(self, text: str) -> _Clip:
        key = self._cache_key(SAPI, text)
        path = self._path_for(key, ".wav")
        try:
            duration_ms = self._sapi_to_file(text, path)
            if not path.is_file():
                raise TtsError("Windows SAPI did not produce a file")
        except Exception as exc:  # noqa: BLE001 - reported as TtsError below
            raise TtsError("Speech synthesis failed: edge-tts is unavailable and Windows SAPI failed "
                           f"({_describe_error(exc)})") from exc
        clip = _Clip(path, max(0, int(duration_ms)), (), SAPI, envelope=voice_fx.wav_envelope(path))
        self._cache[key] = clip
        return clip

    def _make_segment(self, clip: _Clip, text: str, starts: list[tuple[int, int]],
                      pause_after_ms: int) -> AudioSegment:
        if clip.engine == EDGE:
            marks = align_marks(text, starts, clip.words, clip.duration_ms)
        else:
            marks = proportional_marks(len(text), starts, clip.duration_ms)
        return AudioSegment(path=clip.path, duration_ms=clip.duration_ms, marks=marks,
                            pause_after_ms=pause_after_ms,
                            words=voice_fx.segment_words(text, clip.words, clip.duration_ms),
                            envelope=clip.envelope)

    # ---- cache ------------------------------------------------------------

    def _cache_key(self, engine: str, text: str) -> tuple[str, str, str, str, str]:
        voice = self._voice if engine == EDGE else self._offline_voice
        return (engine, voice, self._rate, self._volume, text)

    def _cached(self, key: tuple[str, str, str, str, str]) -> _Clip | None:
        clip = self._cache.get(key)
        if clip is None:
            return None
        if clip.path.is_file():
            return clip
        del self._cache[key]
        return None

    def _path_for(self, key: tuple[str, str, str, str, str], suffix: str) -> Path:
        self._out_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(repr(key).encode("utf-8")).hexdigest()[:16]
        return self._out_dir / f"{digest}{suffix}"

    # ---- edge-tts (overridable in tests) -----------------------------------

    def _edge_to_file(self, text: str, path: Path) -> tuple[int, tuple[tuple[int, str], ...]]:
        """Write edge-tts mp3 for ``text`` to ``path``; return (duration_ms, words)."""
        part = _part_path(path)
        timeout_s = 60.0 + len(text) / 50

        async def run() -> tuple[int, list[tuple[int, str]], int]:
            return await asyncio.wait_for(self._edge_stream(text, part), timeout=timeout_s)

        try:
            nbytes, words, last_word_end_ms = asyncio.run(run())
            if nbytes <= 0:
                raise TtsError("edge-tts returned no audio data")
            os.replace(part, path)
        finally:
            _unlink_quietly(part)
        duration_ms = max(nbytes * 8 // _EDGE_KBITS_PER_S, last_word_end_ms)
        return duration_ms, tuple(words)

    async def _edge_stream(self, text: str, part: Path) -> tuple[int, list[tuple[int, str]], int]:
        communicate = edge_tts.Communicate(
            text, self._voice, rate=self._rate, volume=self._volume, boundary="WordBoundary",
            connect_timeout=_EDGE_CONNECT_TIMEOUT_S, receive_timeout=_EDGE_RECEIVE_TIMEOUT_S)
        words: list[tuple[int, str]] = []
        nbytes = 0
        last_word_end_ms = 0
        with open(part, "wb") as fh:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    fh.write(chunk["data"])
                    nbytes += len(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    words.append((chunk["offset"] // _TICKS_PER_MS, chunk["text"]))
                    end_ms = (chunk["offset"] + chunk["duration"]) // _TICKS_PER_MS
                    last_word_end_ms = max(last_word_end_ms, end_ms)
        return nbytes, words, last_word_end_ms

    # ---- Windows SAPI via pyttsx3 (overridable in tests) -------------------

    def _sapi_to_file(self, text: str, path: Path) -> int:
        """Write a SAPI WAV for ``text`` to ``path``; return its duration in ms."""
        part = _part_path(path)
        try:
            try:
                self._sapi_render(text, part)
            except Exception as exc:  # noqa: BLE001 - pyttsx3 sometimes needs a fresh engine
                logger.warning("Windows SAPI failed (%s); re-initialising it and retrying once",
                               _describe_error(exc))
                self._drop_sapi_engine()
                self._sapi_render(text, part)
            duration_ms = _wav_duration_ms(part)
            if duration_ms <= 0:
                raise TtsError("Windows SAPI produced an empty WAV file")
            os.replace(part, path)
        finally:
            _unlink_quietly(part)
        return duration_ms

    def _sapi_render(self, text: str, target: Path) -> None:
        engine = self._get_sapi_engine()
        _unlink_quietly(target)
        errors: list[BaseException] = []

        def on_error(name: object = None, exception: BaseException | None = None, **_kw: object) -> None:
            errors.append(exception or TtsError("unknown SAPI error"))

        token = engine.connect("error", on_error)
        try:
            # pyttsx3 quirk: an idle engine (any use after the first runAndWait)
            # writes the file synchronously inside save_to_file, and a following
            # runAndWait then never returns. Only a fresh engine, whose commands
            # are still queued, needs runAndWait.
            idle = not engine.isBusy()
            engine.save_to_file(text, str(target))
            if idle:
                _pump_com_messages()
            elif not errors:
                _run_and_wait(engine, timeout_s=30.0 + len(text) / 20)
        finally:
            engine.disconnect(token)
        if errors:
            raise TtsError(f"Windows SAPI could not write audio ({_describe_error(errors[0])})")
        if not target.is_file() or target.stat().st_size <= _WAV_HEADER_BYTES:
            raise TtsError("Windows SAPI produced no audio")

    def _get_sapi_engine(self) -> Any:
        if self._sapi_engine is not None:
            return self._sapi_engine
        self._ensure_com()
        import pyttsx3

        # pyttsx3.init() shares one engine per driver name across threads; a
        # private Engine keeps it on this thread and lets a re-init really start fresh.
        engine_cls = getattr(pyttsx3, "Engine", None)
        engine = engine_cls("sapi5") if engine_cls is not None else pyttsx3.init("sapi5")
        self._configure_sapi(engine)
        self._sapi_engine = engine
        return engine

    def _configure_sapi(self, engine: Any) -> None:
        if self._offline_voice:
            needle = self._offline_voice.casefold()
            voices = engine.getProperty("voices") or []
            match = next((v for v in voices if needle in str(getattr(v, "name", "") or "").casefold()), None)
            if match is None:
                logger.warning("No Windows voice name contains %r; using the default voice",
                               self._offline_voice)
            else:
                engine.setProperty("voice", match.id)
                logger.info("Windows SAPI voice: %s", match.name)
        rate_wpm = _sapi_rate_wpm(self._rate)
        volume = _sapi_volume_level(self._volume)
        engine.setProperty("rate", rate_wpm)
        engine.setProperty("volume", volume)
        logger.info("Windows SAPI engine ready (%d wpm, volume %.2f)", rate_wpm, volume)

    def _ensure_com(self) -> None:
        if self._com_initialized:
            return
        import comtypes
        try:
            comtypes.CoInitialize()
        except Exception as exc:  # noqa: BLE001 - e.g. already initialised in another mode
            logger.debug("CoInitialize skipped: %s", _describe_error(exc))
            return
        self._com_initialized = True

    def _drop_sapi_engine(self) -> None:
        engine, self._sapi_engine = self._sapi_engine, None
        if engine is not None:
            del engine
            gc.collect()


def _pump_com_messages() -> None:
    """Deliver pending SAPI events so they do not pile up in this thread's queue."""
    try:
        import pythoncom
        pythoncom.PumpWaitingMessages()
    except Exception as exc:  # noqa: BLE001 - only housekeeping
        logger.debug("PumpWaitingMessages failed: %s", _describe_error(exc))


def _run_and_wait(engine: Any, *, timeout_s: float) -> None:
    """engine.runAndWait() with a watchdog that ends pyttsx3's SAPI loop if it never finishes."""
    timed_out = threading.Event()

    def abort() -> None:
        timed_out.set()
        driver = getattr(getattr(engine, "proxy", None), "_driver", None)
        if driver is not None and hasattr(driver, "_looping"):
            driver._looping = False

    timer = threading.Timer(timeout_s, abort)
    timer.daemon = True
    timer.start()
    try:
        engine.runAndWait()
    finally:
        timer.cancel()
    if timed_out.is_set():
        raise TtsError("Windows SAPI did not finish in time")


# --------------------------------------------------------------------------
# Background worker
# --------------------------------------------------------------------------

class _Job(NamedTuple):
    generation: int
    section_index: int
    section: Section


class TtsWorker(threading.Thread):
    """Daemon thread that owns a SpeechSynthesizer and processes jobs FIFO.

    Jobs from a generation older than the current one are dropped, and a job
    that becomes stale while it runs has its result discarded. ``on_result``
    runs on this worker thread.
    """

    def __init__(self, synth_factory: Callable[[], SpeechSynthesizer],
                 on_result: Callable[[int, int, SectionAudio | None, str | None], None]) -> None:
        super().__init__(name="tts-worker", daemon=True)
        self._synth_factory = synth_factory
        self._on_result = on_result
        self._jobs: queue.Queue[_Job | None] = queue.Queue()
        self._lock = threading.Lock()
        self._generation = 0
        self._stop_requested = threading.Event()

    def submit(self, generation: int, section_index: int, section: Section) -> None:
        """Queue a section; a newer generation than the current one becomes current."""
        with self._lock:
            if generation > self._generation:
                self._generation = generation
        self._jobs.put(_Job(generation, section_index, section))

    def set_generation(self, generation: int) -> None:
        """Make ``generation`` current; queued jobs from other generations are dropped."""
        with self._lock:
            self._generation = generation

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
                if self._is_stale(job.generation):
                    logger.debug("Dropping stale TTS job (generation %d, section %d)",
                                 job.generation, job.section_index)
                    continue
                audio, error = self._process(synth, startup_error, job)
                if self._is_stale(job.generation):
                    logger.debug("Discarding stale TTS result (generation %d, section %d)",
                                 job.generation, job.section_index)
                    continue
                self._deliver(job, audio, error)
        finally:
            if synth is not None:
                try:
                    synth.close()
                except Exception:  # noqa: BLE001 - shutdown must not fail
                    logger.exception("Closing the speech synthesizer failed")

    def _create_synth(self) -> tuple[SpeechSynthesizer | None, str | None]:
        try:
            return self._synth_factory(), None
        except Exception as exc:  # noqa: BLE001 - reported to every job instead
            logger.exception("Could not create the speech synthesizer")
            return None, f"Could not start the speech engine ({_describe_error(exc)})"

    def _is_stale(self, generation: int) -> bool:
        with self._lock:
            return generation < self._generation

    def _process(self, synth: SpeechSynthesizer | None, startup_error: str | None,
                 job: _Job) -> tuple[SectionAudio | None, str | None]:
        if synth is None:
            return None, startup_error or "The speech engine is not available"
        try:
            return synth.synthesize_section(job.section), None
        except TtsError as exc:
            logger.warning("Speech synthesis failed for section %d: %s", job.section_index, exc)
            return None, str(exc)
        except Exception as exc:  # noqa: BLE001 - keep the worker alive
            logger.exception("Unexpected error while synthesizing section %d", job.section_index)
            return None, f"Speech synthesis failed ({_describe_error(exc)})"

    def _deliver(self, job: _Job, audio: SectionAudio | None, error: str | None) -> None:
        try:
            self._on_result(job.generation, job.section_index, audio, error)
        except Exception:  # noqa: BLE001 - a broken callback must not kill the worker
            logger.exception("TTS result callback failed for section %d", job.section_index)
