"""Fakes for the meeting recorder's tests: audio devices that never touch a real device, a clock the test moves,
a transcriber that never loads a model and an HTTP session that never reaches the network."""

from __future__ import annotations

import hashlib
import threading
from array import array
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from briefing_reader import meetings as mt
from briefing_reader.meeting_audio import SAMPLE_RATE, DeviceInfo
from briefing_reader.meeting_speech import Segment

LOUD = 4000
MIC_A = DeviceInfo("in-1", "Owner Laptop Microphone", True)
MIC_B = DeviceInfo("in-2", "Owner Headset Microphone")
OUT_A = DeviceInfo("out-1", "Owner Laptop Speakers", True)
OUT_B = DeviceInfo("out-2", "Owner Headset Earphones")


def tone(seconds: float, level: int = LOUD) -> bytes:
    """``seconds`` of 16 kHz mono int16 audio whose samples alternate +-``level`` (0: silence)."""
    count = int(seconds * SAMPLE_RATE)
    pair = array("h", (-level, level)).tobytes()
    return pair * (count // 2) + pair[:2] * (count % 2)


class FakeStream:
    """Hands out what the test fed it; ``fail()`` makes the next read raise (a device that went away)."""

    def __init__(self, device: DeviceInfo, kind: str) -> None:
        self.device, self.kind = device, kind
        self.pending = bytearray()
        self.closed = False
        self._fail = False

    def feed(self, data: bytes) -> None:
        self.pending += data

    def fail(self) -> None:
        self._fail = True

    def read_available(self) -> bytes:
        if self._fail:
            raise OSError("gone")
        data, self.pending = bytes(self.pending), bytearray()
        return data

    def close(self) -> None:
        self.closed = True


class FakeBackend:
    """Two inputs and two outputs; the defaults can change; every opened stream is kept in ``streams``."""

    def __init__(self, *, mic_fails: bool = False) -> None:
        self.ins = [MIC_A, MIC_B]
        self.outs = [OUT_A, OUT_B]
        self.default_in: DeviceInfo | None = MIC_A
        self.default_out: DeviceInfo | None = OUT_A
        self.mic_fails = mic_fails
        self.streams: list[FakeStream] = []
        self.opened: list[tuple[str, str]] = []

    def default_input(self) -> DeviceInfo | None:
        return self.default_in

    def default_output(self) -> DeviceInfo | None:
        return self.default_out

    def inputs(self) -> list[DeviceInfo]:
        return list(self.ins)

    def outputs(self) -> list[DeviceInfo]:
        return list(self.outs)

    def _open(self, device: DeviceInfo, kind: str) -> FakeStream:
        stream = FakeStream(device, kind)
        self.streams.append(stream)
        self.opened.append((kind, device.id))
        return stream

    def open_mic(self, device: DeviceInfo) -> FakeStream:
        if self.mic_fails:
            raise OSError("no microphone")
        return self._open(device, mt.MIC)

    def open_loopback(self, device: DeviceInfo) -> FakeStream:
        return self._open(device, mt.LOOPBACK)

    def live(self, kind: str) -> FakeStream:
        """The newest open stream of ``kind``."""
        return [s for s in self.streams if s.kind == kind and not s.closed][-1]


class Clock:
    """Monotonic seconds and a wall clock that move together."""

    def __init__(self, start: datetime = datetime(2026, 10, 19, 14, 0)) -> None:
        self.t = 1000.0
        self.start = start

    def mono(self) -> float:
        return self.t

    def wall(self) -> datetime:
        return self.start + timedelta(seconds=self.t - 1000.0)

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeTranscriber:
    """Segments per file name; ``fail`` raises part-way (a model that broke)."""

    def __init__(self, segments: dict[str, list[Segment]] | None = None, *, fail: bool = False) -> None:
        self.segments = segments or {}
        self.fail = fail
        self.calls: list[str] = []

    def transcribe(self, path: Path) -> Iterable[Segment]:
        self.calls.append(Path(path).name)
        for index, seg in enumerate(self.segments.get(Path(path).name, [])):
            if self.fail and index == 1:
                raise RuntimeError("model broke")
            yield seg


class FakeResponse:
    def __init__(self, status: int, headers: dict | None = None, body: bytes = b"") -> None:
        self.status_code = status
        self.headers = headers or {}
        self._body = body

    def iter_content(self, size: int) -> Iterable[bytes]:
        for i in range(0, len(self._body), max(1, size // 4 or 1)):
            yield self._body[i:i + max(1, size // 4 or 1)]


class FakeSession:
    """The model's files served from memory: HEAD gives the sizes (and SHA-256 for model.bin, like Hugging
    Face's X-Linked-Etag); GET honours Range. ``cut_after`` bytes into model.bin the first GET breaks off."""

    def __init__(self, files: dict[str, bytes], *, cut_after: int = 0, bad_hash: bool = False) -> None:
        self.files = files
        self.cut_after = cut_after
        self.bad_hash = bad_hash
        self.requests: list[tuple[str, str, str]] = []
        self.lock = threading.Lock()

    @staticmethod
    def _name(url: str) -> str:
        return url.rsplit("/", 1)[-1]

    def head(self, url: str, allow_redirects: bool = False, timeout: float = 0) -> FakeResponse:
        name = self._name(url)
        self.requests.append(("HEAD", name, ""))
        body = self.files[name]
        if name == "model.bin":
            sha = hashlib.sha256(body + (b"x" if self.bad_hash else b"")).hexdigest()
            return FakeResponse(302, {"X-Linked-Size": str(len(body)), "X-Linked-Etag": f'"{sha}"'})
        return FakeResponse(200, {"Content-Length": str(len(body))})

    def get(self, url: str, headers: dict | None = None, stream: bool = True, timeout: float = 0) -> FakeResponse:
        name = self._name(url)
        span = (headers or {}).get("Range", "")
        self.requests.append(("GET", name, span))
        body = self.files[name]
        start = int(span.split("=")[1].rstrip("-")) if span else 0
        part = body[start:]
        if name == "model.bin" and self.cut_after and not span:
            part = part[:self.cut_after]
            self.cut_after = 0
            return _Broken(200, {}, part)
        return FakeResponse(206 if span else 200, {}, part)


class _Broken(FakeResponse):
    def iter_content(self, size: int) -> Iterable[bytes]:
        yield from super().iter_content(size)
        raise ConnectionError("connection reset")
