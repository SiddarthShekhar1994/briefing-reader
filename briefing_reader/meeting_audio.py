"""Meeting recording, part 1: the audio devices and the recorder (no Qt).

Two channels, each its own 16 kHz mono 16-bit WAV file in the meeting's folder: ``mic.wav`` (the microphone,
"You") and ``loopback.wav`` (the call audio from the speakers / headset through WASAPI loopback, "Others").
Both files follow the wall clock: a channel that delivers nothing (WASAPI loopback is silent while nothing
plays) is filled with silence, so the two files stay aligned for the You / Others merge.

Devices (owner decision 4b): the call audio follows the Windows default output device and the microphone the
default input, unless the owner picked one in Settings > Meetings. When a followed default changes mid-meeting
(a headset unplugged), or a stream stops working, the recorder switches to the (new) default and notes it in
the meeting record (``device_changes``: when and which channel; the device names stay in that file only).

Auto-stop (owner decision 4): the meeting's end time + ``END_GRACE_S`` (15 min), ``SILENCE_S`` (10 min) of
silence on both channels, or the ``MAX_S`` (3 h) cap. The owner's Stop and closing Jarvis stop and save too.
Crash-safe: each WAV header is brought up to date every few seconds, and ``repair_wav`` mends a file whose
header a crash left behind (``MeetingStore.recover`` at the next start).

The device libraries are optional add-ons, imported only when used: PyAudioWPatch (preferred) or soundcard.
``load_backend()`` returns (backend, "") or (None, what is missing). Tests hand the recorder fake backends.

Public API
==========
``DeviceInfo(id, name, is_default)``; ``AudioBackend`` (what a backend offers: ``default_input()``,
``default_output()``, ``inputs()``, ``outputs()``, ``open_mic(device)``, ``open_loopback(device)``; a stream
has ``read_available() -> bytes`` (16 kHz mono int16, never blocking) and ``close()``).
``load_backend(prefer=None)``; ``backend_missing()`` -> the add-on words, or "".
``WavWriter(path)``: ``write(bytes)``, ``frames``, ``seconds``, ``sync()``, ``close()``.
``repair_wav(path)``, ``wav_seconds(path)``.
``Recorder(backend, store, record, mic_pick="", call_pick="", clock=, wall=, on_stop=, on_change=)``:
``start()`` (opens both streams and files; raises ``RecorderError`` when the microphone cannot be opened),
``run_thread()``, ``tick()`` (one step: read, write, follow the defaults, check the stop rules), ``stop(reason)``,
``elapsed()``, ``is_recording()``, ``stop_reason``, ``device_names()``.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import queue
import struct
import threading
import time
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from . import meetings as mt

logger = logging.getLogger(__name__)

SAMPLE_RATE = mt.SAMPLE_RATE
BYTES_PER_SAMPLE = 2
TICK_S = 0.1
SYNC_S = 5.0                      # WAV headers brought up to date this often (crash-safe)
DEVICE_CHECK_S = 2.0              # a followed default is checked this often
END_GRACE_S = 15 * 60             # owner decision: auto-stop at the end time + 15 minutes
SILENCE_S = 10 * 60               # ... after 10 minutes of silence on both channels
MAX_S = 3 * 3600                  # ... and at 3 hours
SILENCE_PEAK = 600                # a chunk whose loudest sample stays under this is silence (about -35 dBFS)
LAG_FILL_S = 1.0                  # a channel this far behind the clock is filled with silence ...
LAG_KEEP_S = 0.2                  # ... up to this much behind (a late burst then still fits)
AHEAD_DROP_S = 1.0                # a burst more than this ahead of the clock is cut
PYAUDIO_MODULE = "pyaudiowpatch"
SOUNDCARD_MODULE = "soundcard"
ADDON_AUDIO = "PyAudioWPatch (or soundcard)"


class RecorderError(RuntimeError):
    """The recording could not start; ``code`` is a short word for the record and the log."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DeviceInfo:
    id: str
    name: str
    is_default: bool = False

    def __repr__(self) -> str:                    # a device name is the owner's; never in a log
        return f"DeviceInfo(id={self.id!r}, default={self.is_default})"


class AudioStream(Protocol):
    def read_available(self) -> bytes: ...
    def close(self) -> None: ...


class AudioBackend(Protocol):
    def default_input(self) -> DeviceInfo | None: ...
    def default_output(self) -> DeviceInfo | None: ...
    def inputs(self) -> list[DeviceInfo]: ...
    def outputs(self) -> list[DeviceInfo]: ...
    def open_mic(self, device: DeviceInfo) -> AudioStream: ...
    def open_loopback(self, device: DeviceInfo) -> AudioStream: ...


# --------------------------------------------------------------------------
# WAV files
# --------------------------------------------------------------------------

def _header(data_bytes: int) -> bytes:
    return (b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE" + b"fmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * BYTES_PER_SAMPLE, BYTES_PER_SAMPLE, 16)
            + b"data" + struct.pack("<I", data_bytes))


HEADER_BYTES = len(_header(0))    # 44


class WavWriter:
    """A 16 kHz mono 16-bit PCM WAV file written as it goes; its header is kept current by ``sync()``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh = open(self.path, "wb")
        self._fh.write(_header(0))
        self.frames = 0
        self._synced = 0

    @property
    def seconds(self) -> float:
        return self.frames / SAMPLE_RATE

    def write(self, data: bytes) -> None:
        if self._fh is None or not data:
            return
        if len(data) % BYTES_PER_SAMPLE:
            data = data[:-1]
        self._fh.write(data)
        self.frames += len(data) // BYTES_PER_SAMPLE

    def silence(self, frames: int) -> None:
        if frames > 0:
            self.write(bytes(frames * BYTES_PER_SAMPLE))

    def sync(self) -> None:
        """The header's sizes brought up to date and the data flushed (crash-safe up to here)."""
        if self._fh is None or self._synced == self.frames:
            return
        size = self.frames * BYTES_PER_SAMPLE
        self._fh.flush()
        self._fh.seek(4)
        self._fh.write(struct.pack("<I", 36 + size))
        self._fh.seek(40)
        self._fh.write(struct.pack("<I", size))
        self._fh.seek(0, 2)
        self._fh.flush()
        self._synced = self.frames

    def close(self) -> None:
        if self._fh is None:
            return
        try:
            self.sync()
        finally:
            self._fh.close()
            self._fh = None


def repair_wav(path: Path) -> bool:
    """Mend a header a crash left behind (its sizes from the file's length). True when it was changed."""
    try:
        size = Path(path).stat().st_size
        with open(path, "r+b") as fh:
            head = fh.read(HEADER_BYTES)
            if len(head) < HEADER_BYTES or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return False
            data = max(0, size - HEADER_BYTES)
            data -= data % BYTES_PER_SAMPLE
            if struct.unpack("<I", head[40:44])[0] == data and struct.unpack("<I", head[4:8])[0] == 36 + data:
                return False
            fh.seek(4)
            fh.write(struct.pack("<I", 36 + data))
            fh.seek(40)
            fh.write(struct.pack("<I", data))
            return True
    except OSError:
        return False


def wav_seconds(path: Path) -> float:
    try:
        size = Path(path).stat().st_size
    except OSError:
        return 0.0
    return max(0, size - HEADER_BYTES) / (SAMPLE_RATE * BYTES_PER_SAMPLE)


def peak(data: bytes) -> int:
    """The loudest sample of 16-bit PCM ``data`` (0 for none)."""
    if len(data) < BYTES_PER_SAMPLE:
        return 0
    samples = array("h")
    samples.frombytes(data[:len(data) - len(data) % BYTES_PER_SAMPLE])
    return max(max(samples), -min(samples))


# --------------------------------------------------------------------------
# Conversion to 16 kHz mono int16 (the device's own rate and channels in)
# --------------------------------------------------------------------------

def to_mono16k(data: bytes, rate: int, channels: int, *, fmt: str = "int16") -> bytes:
    """``data`` (interleaved int16 or float32 frames at ``rate``) as 16 kHz mono int16. numpy when it is there
    (the add-ons bring it), else plain Python (decimation without a filter: fine for speech)."""
    if not data:
        return b""
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is not None:
        dtype = np.float32 if fmt == "float32" else np.int16
        frames = np.frombuffer(data[:len(data) - len(data) % (np.dtype(dtype).itemsize * channels)], dtype=dtype)
        frames = frames.reshape(-1, channels).astype(np.float32)
        if fmt == "float32":
            frames *= 32767.0
        mono = frames.mean(axis=1)
        if rate != SAMPLE_RATE and len(mono):
            count = int(len(mono) * SAMPLE_RATE / rate)
            if rate % SAMPLE_RATE == 0:
                step = rate // SAMPLE_RATE
                mono = mono[:count * step].reshape(-1, step).mean(axis=1)
            else:
                mono = np.interp(np.arange(count) * (rate / SAMPLE_RATE), np.arange(len(mono)), mono)
        return np.clip(mono, -32768, 32767).astype("<i2").tobytes()
    samples = array("f" if fmt == "float32" else "h")
    samples.frombytes(data[:len(data) - len(data) % (samples.itemsize * channels)])
    scale = 32767.0 if fmt == "float32" else 1.0
    mono = [sum(samples[i:i + channels]) * scale / channels for i in range(0, len(samples), channels)]
    step = rate / SAMPLE_RATE
    out = array("h", (max(-32768, min(32767, int(mono[int(i * step)])))
                      for i in range(int(len(mono) / step))))
    return out.tobytes()


# --------------------------------------------------------------------------
# The add-ons (imported only when used)
# --------------------------------------------------------------------------

def backend_missing() -> str:
    """What the recording needs that is not installed ("" when PyAudioWPatch or soundcard is there)."""
    for name in (PYAUDIO_MODULE, SOUNDCARD_MODULE):
        try:
            if importlib.util.find_spec(name) is not None:
                return ""
        except (ImportError, ValueError):
            continue
    return ADDON_AUDIO


def load_backend(prefer: str | None = None) -> tuple[AudioBackend | None, str]:
    """The installed device library behind its adapter, or (None, what is missing / failed)."""
    order = (prefer,) if prefer else (PYAUDIO_MODULE, SOUNDCARD_MODULE)
    for name in order:
        try:
            if importlib.util.find_spec(name) is None:
                continue
            module = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - a broken add-on is reported, never raised
            logger.warning("Meetings: the %s add-on could not be loaded (%s)", name, type(exc).__name__)
            continue
        if name == PYAUDIO_MODULE:
            return PyAudioWPatchBackend(module), ""
        return SoundcardBackend(module), ""
    return None, ADDON_AUDIO


class _QueueStream:
    """A stream fed by the device library's callback or reader thread; ``read_available`` never blocks."""

    def __init__(self, rate: int, channels: int, fmt: str) -> None:
        self.rate, self.channels, self.fmt = rate, channels, fmt
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=600)
        self.failed = False

    def push(self, data: bytes) -> None:
        try:
            self._queue.put_nowait(data)
        except queue.Full:                         # the recorder stalled: the oldest chunk goes
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass

    def read_available(self) -> bytes:
        if self.failed:
            raise OSError("the device stopped")
        chunks = []
        while True:
            try:
                chunks.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return to_mono16k(b"".join(chunks), self.rate, self.channels, fmt=self.fmt) if chunks else b""

    def close(self) -> None:
        pass


class PyAudioWPatchBackend:
    """PyAudioWPatch (a PyAudio fork with WASAPI loopback): ``get_host_api_info_by_type(paWASAPI)`` for the
    defaults, ``get_loopback_device_info_generator()`` for the speakers' loopback twins, callback streams."""

    def __init__(self, module: Any) -> None:
        self.pa = module
        self._audio = module.PyAudio()

    def _wasapi(self) -> dict:
        return self._audio.get_host_api_info_by_type(self.pa.paWASAPI)

    def _info(self, index: int) -> dict | None:
        try:
            return self._audio.get_device_info_by_index(int(index))
        except Exception:  # noqa: BLE001 - a device that went away
            return None

    def _devices(self, *, inputs: bool) -> list[dict]:
        api = self._wasapi()["index"]
        found = []
        for index in range(self._audio.get_device_count()):
            info = self._info(index)
            if info is None or info.get("hostApi") != api or info.get("isLoopbackDevice"):
                continue
            if (info.get("maxInputChannels", 0) if inputs else info.get("maxOutputChannels", 0)) > 0:
                found.append(info)
        return found

    def default_input(self) -> DeviceInfo | None:
        info = self._info(self._wasapi().get("defaultInputDevice", -1))
        return DeviceInfo(str(info["index"]), str(info["name"]), True) if info else None

    def default_output(self) -> DeviceInfo | None:
        info = self._info(self._wasapi().get("defaultOutputDevice", -1))
        return DeviceInfo(str(info["index"]), str(info["name"]), True) if info else None

    def inputs(self) -> list[DeviceInfo]:
        return [DeviceInfo(str(i["index"]), str(i["name"])) for i in self._devices(inputs=True)]

    def outputs(self) -> list[DeviceInfo]:
        return [DeviceInfo(str(i["index"]), str(i["name"])) for i in self._devices(inputs=False)]

    def _open(self, info: dict) -> AudioStream:
        channels = max(1, min(2, int(info.get("maxInputChannels") or 1)))
        rate = int(info.get("defaultSampleRate") or 48_000)
        stream = _QueueStream(rate, channels, "int16")

        def callback(data: bytes, _frames: int, _time: Any, _status: int) -> tuple[None, int]:
            stream.push(data)
            return None, self.pa.paContinue

        handle = self._audio.open(format=self.pa.paInt16, channels=channels, rate=rate, input=True,
                                  frames_per_buffer=rate // 10, input_device_index=int(info["index"]),
                                  stream_callback=callback)
        handle.start_stream()

        def close() -> None:
            try:
                handle.stop_stream()
                handle.close()
            except Exception:  # noqa: BLE001 - closing a device that went away
                pass

        stream.close = close  # type: ignore[method-assign]
        return stream

    def open_mic(self, device: DeviceInfo) -> AudioStream:
        info = self._info(int(device.id))
        if info is None:
            raise OSError("no such microphone")
        return self._open(info)

    def open_loopback(self, device: DeviceInfo) -> AudioStream:
        speakers = self._info(int(device.id))
        if speakers is None:
            raise OSError("no such output device")
        if not speakers.get("isLoopbackDevice"):
            for loopback in self._audio.get_loopback_device_info_generator():
                if str(speakers["name"]) in str(loopback["name"]):
                    speakers = loopback
                    break
            else:
                raise OSError("no loopback for that output device")
        return self._open(speakers)


class SoundcardBackend:
    """soundcard: ``default_microphone()``, ``default_speaker()``, ``get_microphone(name,
    include_loopback=True)`` for the speakers' loopback; each stream records on a reader thread."""

    def __init__(self, module: Any) -> None:
        self.sc = module

    def default_input(self) -> DeviceInfo | None:
        mic = self.sc.default_microphone()
        return DeviceInfo(str(mic.id), str(mic.name), True) if mic is not None else None

    def default_output(self) -> DeviceInfo | None:
        speaker = self.sc.default_speaker()
        return DeviceInfo(str(speaker.id), str(speaker.name), True) if speaker is not None else None

    def inputs(self) -> list[DeviceInfo]:
        return [DeviceInfo(str(m.id), str(m.name)) for m in self.sc.all_microphones()]

    def outputs(self) -> list[DeviceInfo]:
        return [DeviceInfo(str(s.id), str(s.name)) for s in self.sc.all_speakers()]

    def _open(self, source: Any) -> AudioStream:
        stream = _QueueStream(SAMPLE_RATE, 1, "float32")
        stop = threading.Event()

        def read() -> None:
            try:
                with source.recorder(samplerate=SAMPLE_RATE, channels=1) as recorder:
                    while not stop.is_set():
                        frames = recorder.record(numframes=SAMPLE_RATE // 10)
                        stream.push(frames.astype("<f4").tobytes())
            except Exception:  # noqa: BLE001 - the recorder notices on its next read
                stream.failed = not stop.is_set()

        thread = threading.Thread(target=read, name="meeting-audio", daemon=True)
        thread.start()
        stream.close = lambda: (stop.set(), thread.join(1.0))  # type: ignore[method-assign]
        return stream

    def open_mic(self, device: DeviceInfo) -> AudioStream:
        return self._open(self.sc.get_microphone(device.id))

    def open_loopback(self, device: DeviceInfo) -> AudioStream:
        return self._open(self.sc.get_microphone(device.id, include_loopback=True))


# --------------------------------------------------------------------------
# The recorder
# --------------------------------------------------------------------------

def pick_device(choices: list[DeviceInfo], default: DeviceInfo | None, picked: str) -> tuple[DeviceInfo | None, bool]:
    """The device to use: the one named ``picked`` while it is there, else the default. Returns (device,
    follows the default)."""
    if picked:
        for device in choices:
            if device.name == picked:
                return device, False
    return default, True


class _Channel:
    def __init__(self, kind: str, writer: WavWriter) -> None:
        self.kind = kind
        self.writer = writer
        self.stream: AudioStream | None = None
        self.device: DeviceInfo | None = None
        self.follows_default = True
        self.picked = ""


class Recorder:
    """One meeting's recording; see the module docstring. ``on_stop(reason)`` and ``on_change()`` are called
    on the recorder's thread (the Qt side hands them over to the GUI thread)."""

    def __init__(self, backend: AudioBackend, store: mt.MeetingStore, record: mt.MeetingRecord, *,
                 mic_pick: str = "", call_pick: str = "", clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], datetime] = datetime.now,
                 on_stop: Callable[[str], Any] | None = None, on_change: Callable[[], Any] | None = None) -> None:
        self.backend = backend
        self.store = store
        self.record = record
        self._clock = clock
        self._wall = wall
        self._on_stop = on_stop
        self._on_change = on_change
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._started = 0.0
        self._last_sound = 0.0
        self._last_sync = 0.0
        self._last_check = 0.0
        self._recording = False
        self.stop_reason = ""
        self._picks = {mt.MIC: mic_pick, mt.LOOPBACK: call_pick}
        self._channels: dict[str, _Channel] = {}

    # ---- start ----------------------------------------------------------------------------

    def start(self) -> None:
        """Open both devices and both files. A microphone that cannot be opened stops the start
        (``RecorderError``); call audio that cannot be opened is recorded as silence and tried again."""
        meeting_id = self.record.id
        for kind in mt.CHANNELS:
            self._channels[kind] = _Channel(kind, WavWriter(self.store.audio_path(meeting_id, kind)))
            self._channels[kind].picked = self._picks[kind]
        try:
            self._open(self._channels[mt.MIC], first=True)
        except Exception as exc:
            for channel in self._channels.values():
                channel.writer.close()
            raise RecorderError("mic_unavailable", "the microphone could not be opened") from exc
        try:
            self._open(self._channels[mt.LOOPBACK], first=True)
        except Exception as exc:  # noqa: BLE001 - the call audio is tried again every few seconds
            logger.warning("Meeting %s: the call audio could not be opened yet (%s)", meeting_id, type(exc).__name__)
            self._note_change(mt.LOOPBACK, "unavailable")
        now = self._clock()
        self._started = self._last_sound = self._last_sync = self._last_check = now
        self._recording = True
        self.record.devices = self.device_record()
        self.store.save(self.record)
        logger.info("Meeting %s: recording (microphone follows the default: %s, call audio follows the default: "
                    "%s)", meeting_id, self._channels[mt.MIC].follows_default,
                    self._channels[mt.LOOPBACK].follows_default)

    def _choose(self, channel: _Channel) -> tuple[DeviceInfo | None, bool]:
        if channel.kind == mt.MIC:
            return pick_device(self.backend.inputs() if channel.picked else [], self.backend.default_input(),
                               channel.picked)
        return pick_device(self.backend.outputs() if channel.picked else [], self.backend.default_output(),
                           channel.picked)

    def _open(self, channel: _Channel, *, first: bool = False) -> None:
        device, follows = self._choose(channel)
        if device is None:
            raise OSError("no device")
        opener = self.backend.open_mic if channel.kind == mt.MIC else self.backend.open_loopback
        stream = opener(device)
        if channel.stream is not None:
            try:
                channel.stream.close()
            except Exception:  # noqa: BLE001 - the old device may be gone
                pass
        channel.stream, channel.device, channel.follows_default = stream, device, follows
        if not first:
            self._note_change(channel.kind, "switched")

    def _note_change(self, kind: str, what: str) -> None:
        channel = self._channels.get(kind)
        device = channel.device if channel is not None else None
        entry = {"at": self._wall().isoformat(timespec="seconds"),
                 "channel": "microphone" if kind == mt.MIC else "call audio", "change": what,
                 "to": device.name if device is not None and what == "switched" else ""}
        self.record.device_changes.append(entry)
        logger.info("Meeting %s: %s %s", self.record.id, entry["channel"], what)

    def device_record(self) -> dict[str, Any]:
        mic, loop = self._channels.get(mt.MIC), self._channels.get(mt.LOOPBACK)
        return {"microphone": mic.device.name if mic and mic.device else "",
                "microphone_follows_default": bool(mic.follows_default) if mic else True,
                "call_audio": loop.device.name if loop and loop.device else "",
                "call_audio_follows_default": bool(loop.follows_default) if loop else True}

    def device_names(self) -> tuple[str, str]:
        record = self.device_record()
        return record["microphone"], record["call_audio"]

    # ---- running --------------------------------------------------------------------------------

    def run_thread(self) -> None:
        """Tick every TICK_S on a thread of its own until a stop."""
        self._thread = threading.Thread(target=self._loop, name="meeting-recorder", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop_event.wait(TICK_S):
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - a recording never dies silently: it stops and saves
                logger.warning("Meeting %s: the recorder failed (%s); stopping and saving", self.record.id,
                               type(exc).__name__)
                self.stop(mt.STOP_FAILED)
            if not self._recording:
                break

    def elapsed(self) -> float:
        if not self._started:
            return 0.0
        return max(0.0, self._clock() - self._started)

    def is_recording(self) -> bool:
        return self._recording

    def tick(self) -> None:
        """One step: each channel's new audio is written (silence fills a channel behind the clock), the
        followed defaults are checked, the WAV headers synced, then the stop rules."""
        with self._lock:
            if not self._recording:
                return
            now = self._clock()
            elapsed = now - self._started
            target = int(elapsed * SAMPLE_RATE)
            loud = False
            for channel in self._channels.values():
                data = b""
                if channel.stream is not None:
                    try:
                        data = channel.stream.read_available()
                    except Exception as exc:  # noqa: BLE001 - a device that went away
                        logger.info("Meeting %s: a stream stopped (%s)", self.record.id, type(exc).__name__)
                        channel.stream = None
                        if channel.kind == mt.MIC:
                            self._retry(channel)
                if data:
                    room = target + int(AHEAD_DROP_S * SAMPLE_RATE) - channel.writer.frames
                    if room <= 0:
                        data = b""
                    elif len(data) // BYTES_PER_SAMPLE > room:
                        data = data[:room * BYTES_PER_SAMPLE]
                    if peak(data) >= SILENCE_PEAK:
                        loud = True
                    channel.writer.write(data)
                behind = target - channel.writer.frames
                if behind > LAG_FILL_S * SAMPLE_RATE:
                    channel.writer.silence(behind - int(LAG_KEEP_S * SAMPLE_RATE))
            if loud:
                self._last_sound = now
            if now - self._last_check >= DEVICE_CHECK_S:
                self._last_check = now
                self._follow_defaults()
            if now - self._last_sync >= SYNC_S:
                self._last_sync = now
                for channel in self._channels.values():
                    channel.writer.sync()
            reason = self._stop_rule(now, elapsed)
        if reason:
            self.stop(reason)

    def _retry(self, channel: _Channel) -> None:
        try:
            self._open(channel)
        except Exception:  # noqa: BLE001 - tried again at the next device check
            channel.stream = None

    def _follow_defaults(self) -> None:
        for channel in self._channels.values():
            if channel.stream is None:
                self._retry(channel)
                continue
            if not channel.follows_default and not channel.picked:
                continue
            try:
                device, _follows = self._choose(channel)
            except Exception:  # noqa: BLE001 - checked again in a moment
                continue
            if device is not None and channel.device is not None and device.id != channel.device.id:
                self._retry(channel)
        devices = self.device_record()
        if devices != self.record.devices:
            self.record.devices = devices
            self.store.save(self.record)
            if self._on_change is not None:
                self._on_change()

    def _stop_rule(self, now: float, elapsed: float) -> str:
        if elapsed >= MAX_S:
            return mt.STOP_CAP
        if now - self._last_sound >= SILENCE_S:
            return mt.STOP_SILENCE
        end = self.record.scheduled_end
        if end is not None:
            wall = self._wall()
            if (wall.tzinfo is None) != (end.tzinfo is None):
                wall, end = wall.replace(tzinfo=None), end.replace(tzinfo=None)
            if wall >= end + timedelta(seconds=END_GRACE_S):
                return mt.STOP_END_TIME
        return ""

    # ---- stop -----------------------------------------------------------------------------------

    def stop(self, reason: str = mt.STOP_BUTTON) -> bool:
        """Stop and save (both files closed, the record RECORDED). False when it had stopped already."""
        with self._lock:
            if not self._recording:
                return False
            self._recording = False
            self.stop_reason = reason if reason in mt.STOP_REASONS else mt.STOP_BUTTON
            self._stop_event.set()
            elapsed = self.elapsed()
            for channel in self._channels.values():
                if channel.stream is not None:
                    try:
                        channel.stream.close()
                    except Exception:  # noqa: BLE001 - the device may be gone
                        pass
                    channel.stream = None
                try:
                    channel.writer.close()
                except OSError as exc:
                    logger.warning("Meeting %s: a file did not close cleanly (%s)", self.record.id,
                                   type(exc).__name__)
            record = self.record
            record.status = mt.STATUS_RECORDED
            record.stop_reason = self.stop_reason
            record.duration_s = elapsed
            record.ended_at = self._wall()
            record.devices = self.device_record()
            try:
                self.store.save(record)
            except OSError as exc:
                logger.warning("Meeting %s: the record could not be saved (%s)", record.id, type(exc).__name__)
            logger.info("Meeting %s: stopped (%s) after %.0f s; %d device change(s)", record.id, self.stop_reason,
                        elapsed, len(record.device_changes))
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)
        if self._on_stop is not None:
            self._on_stop(self.stop_reason)
        return True
