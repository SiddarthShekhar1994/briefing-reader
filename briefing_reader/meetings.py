"""Meeting recording, part 1: where meetings are kept, what a meeting record says, and how long it stays.

Owner decisions (2026-10-18, binding; SPEC "Meeting summaries"):

* Recording starts ONLY from the owner's click on Start in the start card, after he ticked "Everyone in this
  meeting has agreed to be recorded" - every meeting, never remembered. ``StartRequest`` refuses to exist
  without that tick, and the record keeps that he confirmed it and when (``consent_at``).
* Everything stays under ``%LOCALAPPDATA%\\briefing-reader\\meetings\\<id>\\`` (``meetings_root(config)``; a
  root inside a synced folder - OneDrive, Dropbox, Google Drive, iCloud, Downloads - is refused). Audio: two
  16 kHz mono WAV files, ``mic.wav`` ("You") and ``loopback.wav`` ("Others"). Never uploaded anywhere.
* Retention (``MeetingStore.cleanup``, at start-up and daily): audio after ``AUDIO_DAYS`` (7) days, the
  transcript and summary - the whole folder - after ``TEXT_DAYS`` (30) days. ``delete(id)`` removes one
  meeting at once (audio, text and summary).
* Logs carry ids, kinds, counts, durations and statuses only - never a title, a name, a device name or text.

Public API
==========

``meetings_root(config)`` / ``models_root(config)``; ``unsafe_root(path)`` (why a root is refused, or "").
``MeetingInfo(title, start, end, event_id, guests)``: the calendar meeting a recording belongs to (or an
    unscheduled call: ``MeetingInfo()``). ``repr`` carries no title or name.
``StartRequest(info, consent, consent_at, mic, call_audio)``: what the start card hands over; ``consent`` must
    be True (``ConsentMissing`` otherwise).
``MeetingRecord``: one meeting's ``meeting.json`` (``status`` one of STATUS_*; ``status_text()``).
``MeetingStore(root, now=)``: ``create(request, devices)``, ``load(id)``, ``save(record)``, ``list()`` (newest
    first), ``delete(id)``, ``cleanup(now)`` -> (audio removed, meetings removed), ``recover(active=())``
    (meetings left "recording" by a crash become RECORDED, their WAV headers repaired), ``folder(id)``,
    ``audio_path(id, channel)``.
``DevicePrefs(root)``: the devices picked in Settings > Meetings (``microphone``, ``call_audio``; "" follows
    the Windows default), saved in ``meetings\\devices.json``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MEETINGS_DIR = "meetings"
MODELS_DIR = "models"
RECORD_FILE = "meeting.json"
TRANSCRIPT_FILE = "transcript.json"
DEVICES_FILE = "devices.json"
MIC = "mic"                       # the microphone: "You"
LOOPBACK = "loopback"             # the call audio from the speakers / headset: "Others"
CHANNELS = (MIC, LOOPBACK)
AUDIO_FILES = {MIC: "mic.wav", LOOPBACK: "loopback.wav"}
SPEAKERS = {MIC: "You", LOOPBACK: "Others"}
AUDIO_DAYS = 7                    # owner decision: audio is deleted after 7 days
TEXT_DAYS = 30                    # ... the transcript and summary after 30 days
SAMPLE_RATE = 16_000
CONSENT_TEXT = "Everyone in this meeting has agreed to be recorded"
UNSCHEDULED_TITLE = "Unscheduled call"
RECORD_VERSION = 1

STATUS_RECORDING = "recording"
STATUS_RECORDED = "recorded"              # saved; waiting to be written down
STATUS_TRANSCRIBING = "transcribing"      # "Writing it down 40%"
STATUS_TRANSCRIBED = "transcribed"        # the transcript is ready (part 2 adds the summary)
STATUS_SUMMARY_READY = "summary_ready"    # part 2
STATUS_FAILED = "failed"
STATUSES = (STATUS_RECORDING, STATUS_RECORDED, STATUS_TRANSCRIBING, STATUS_TRANSCRIBED, STATUS_SUMMARY_READY,
            STATUS_FAILED)

STOP_BUTTON = "button"            # Stop on the pill, the header, Home or the Meetings screen
STOP_END_TIME = "end_time"        # the meeting's end time + 15 minutes
STOP_SILENCE = "silence"          # 10 minutes of silence on both channels
STOP_CAP = "cap"                  # the 3 hour cap
STOP_CLOSING = "closing"          # Jarvis closed: stopped and saved
STOP_FAILED = "failed"            # the microphone could not be read any more
STOP_INTERRUPTED = "interrupted"  # Jarvis ended without closing (found at the next start)
STOP_REASONS = (STOP_BUTTON, STOP_END_TIME, STOP_SILENCE, STOP_CAP, STOP_CLOSING, STOP_FAILED, STOP_INTERRUPTED)
STOP_WORDS = {STOP_BUTTON: "Stopped", STOP_END_TIME: "Stopped 15 minutes after the meeting's end",
              STOP_SILENCE: "Stopped after 10 minutes of silence", STOP_CAP: "Stopped at the 3 hour limit",
              STOP_CLOSING: "Saved when Jarvis closed", STOP_FAILED: "Stopped: the microphone stopped working",
              STOP_INTERRUPTED: "Saved after Jarvis ended unexpectedly"}

_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
_SYNCED = ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "icloud drive", "iclouddrive",
           "downloads", "box sync")


class ConsentMissing(ValueError):
    """A recording was asked for without the owner's consent tick on the start card."""


# --------------------------------------------------------------------------
# Where
# --------------------------------------------------------------------------

def meetings_root(config: Any) -> Path:
    """``<data dir>\\meetings`` (the data dir is %LOCALAPPDATA%\\briefing-reader)."""
    return Path(config.data_dir) / MEETINGS_DIR


def models_root(config: Any) -> Path:
    """``<data dir>\\models`` (the speech model; downloaded only on the owner's click)."""
    return Path(config.data_dir) / MODELS_DIR


def unsafe_root(path: Path) -> str:
    """Why ``path`` may not hold meetings ("" when it may): a folder that syncs (OneDrive, Dropbox, Google
    Drive, iCloud, Downloads) is refused, so a recording never leaves the PC by itself."""
    parts = [part.casefold() for part in Path(path).parts]
    for part in parts:
        if any(part == name or part.startswith(name + " -") or part.startswith(name + "-") for name in _SYNCED):
            return "a folder that syncs to the cloud"
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        base = (os.environ.get(name) or "").strip()
        if base and _inside(Path(path), Path(base)):
            return "a folder that syncs to the cloud"
    return ""


def _inside(path: Path, base: Path) -> bool:
    try:
        Path(os.path.abspath(path)).relative_to(os.path.abspath(base))
    except ValueError:
        return False
    return True


# --------------------------------------------------------------------------
# What
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MeetingInfo:
    """The calendar meeting a recording is for; ``MeetingInfo()`` is an unscheduled call."""

    title: str = field(default="", repr=False)
    start: datetime | None = None
    end: datetime | None = None
    event_id: str = field(default="", repr=False)
    guests: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", " ".join(str(self.title or "").split()))
        object.__setattr__(self, "guests", tuple(str(name) for name in self.guests or () if str(name).strip()))

    @property
    def scheduled(self) -> bool:
        return self.end is not None

    def shown_title(self) -> str:
        return self.title or UNSCHEDULED_TITLE


@dataclass(frozen=True)
class StartRequest:
    """The start card's Start: the meeting, the owner's consent tick (required) and the devices it showed."""

    info: MeetingInfo
    consent: bool
    consent_at: datetime
    mic: str = field(default="", repr=False)          # the device names the card showed
    call_audio: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if self.consent is not True:
            raise ConsentMissing("the consent tick is required to record")


def new_meeting_id(moment: datetime) -> str:
    return f"{moment:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat(timespec="seconds") if moment is not None else None


def _parse(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


@dataclass
class MeetingRecord:
    """One meeting's ``meeting.json``. Device names and the title stay in this file only (never logged)."""

    id: str
    title: str = field(default="", repr=False)
    event_id: str = field(default="", repr=False)
    guests: tuple[str, ...] = field(default=(), repr=False)
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    consent_confirmed: bool = False
    consent_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_s: float = 0.0
    stop_reason: str = ""
    status: str = STATUS_RECORDING
    progress: int = 0                    # 0..100 while STATUS_TRANSCRIBING
    error: str = ""                      # a short code (never text from a library)
    devices: dict[str, Any] = field(default_factory=dict, repr=False)
    device_changes: list[dict[str, Any]] = field(default_factory=list, repr=False)
    audio_deleted_at: datetime | None = None

    def shown_title(self) -> str:
        return self.title or UNSCHEDULED_TITLE

    def status_text(self) -> str:
        """The Meetings screen's status words: Recording / Waiting to be written down / Writing it down 40% /
        Transcript ready / Summary ready / Failed."""
        if self.status == STATUS_RECORDING:
            return "Recording"
        if self.status == STATUS_TRANSCRIBING:
            return f"Writing it down {max(0, min(100, int(self.progress)))}%"
        if self.status == STATUS_TRANSCRIBED:
            return "Transcript ready"
        if self.status == STATUS_SUMMARY_READY:
            return "Summary ready"
        if self.status == STATUS_FAILED:
            return "Failed"
        if self.audio_deleted_at is not None:
            return "Audio deleted after 7 days"
        return "Waiting to be written down"

    def to_json(self) -> dict[str, Any]:
        return {"version": RECORD_VERSION, "id": self.id, "title": self.title, "event_id": self.event_id,
                "guests": list(self.guests), "scheduled_start": _iso(self.scheduled_start),
                "scheduled_end": _iso(self.scheduled_end),
                "consent": {"confirmed": bool(self.consent_confirmed), "at": _iso(self.consent_at)},
                "started_at": _iso(self.started_at), "ended_at": _iso(self.ended_at),
                "duration_s": round(float(self.duration_s), 1), "stop_reason": self.stop_reason,
                "status": self.status, "progress": int(self.progress), "error": self.error,
                "devices": dict(self.devices), "device_changes": list(self.device_changes),
                "audio": dict(AUDIO_FILES), "audio_deleted_at": _iso(self.audio_deleted_at)}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> MeetingRecord:
        consent = data.get("consent") if isinstance(data.get("consent"), Mapping) else {}
        status = data.get("status") if data.get("status") in STATUSES else STATUS_RECORDED
        guests = data.get("guests") if isinstance(data.get("guests"), list) else []
        devices = data.get("devices") if isinstance(data.get("devices"), Mapping) else {}
        changes = data.get("device_changes") if isinstance(data.get("device_changes"), list) else []
        try:
            duration = float(data.get("duration_s") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        try:
            progress = int(data.get("progress") or 0)
        except (TypeError, ValueError):
            progress = 0
        return cls(id=str(data.get("id") or ""), title=str(data.get("title") or ""),
                   event_id=str(data.get("event_id") or ""), guests=tuple(str(g) for g in guests),
                   scheduled_start=_parse(data.get("scheduled_start")),
                   scheduled_end=_parse(data.get("scheduled_end")),
                   consent_confirmed=consent.get("confirmed") is True, consent_at=_parse(consent.get("at")),
                   started_at=_parse(data.get("started_at")), ended_at=_parse(data.get("ended_at")),
                   duration_s=duration, stop_reason=str(data.get("stop_reason") or ""), status=status,
                   progress=progress, error=str(data.get("error") or ""), devices=dict(devices),
                   device_changes=[dict(c) for c in changes if isinstance(c, Mapping)],
                   audio_deleted_at=_parse(data.get("audio_deleted_at")))


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------

def _write_json(path: Path, data: Any) -> None:
    """Atomic: a temporary file next to it, then a replace (a crash leaves the old file or the new one)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=True, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class MeetingStore:
    """``<root>\\<id>\\meeting.json`` plus the two WAV files and (later) the transcript; see the module doc."""

    def __init__(self, root: Path, *, now: Callable[[], datetime] = datetime.now) -> None:
        self.root = Path(root)
        self._now = now

    def check_root(self) -> str:
        return unsafe_root(self.root)

    def folder(self, meeting_id: str) -> Path:
        if not _ID_RE.fullmatch(str(meeting_id or "")):
            raise ValueError("not a meeting id")
        return self.root / meeting_id

    def audio_path(self, meeting_id: str, channel: str) -> Path:
        return self.folder(meeting_id) / AUDIO_FILES[channel]

    def transcript_path(self, meeting_id: str) -> Path:
        return self.folder(meeting_id) / TRANSCRIPT_FILE

    def create(self, request: StartRequest, devices: Mapping[str, Any] | None = None) -> MeetingRecord:
        """A new meeting folder and record (status RECORDING) for the owner's Start."""
        if not isinstance(request, StartRequest) or request.consent is not True:
            raise ConsentMissing("the consent tick is required to record")
        problem = self.check_root()
        if problem:
            raise OSError(f"meetings may not be kept in {problem}")
        now = self._now()
        meeting_id = new_meeting_id(now)
        while (self.root / meeting_id).exists():
            meeting_id = new_meeting_id(now)
        info = request.info
        record = MeetingRecord(id=meeting_id, title=info.title, event_id=info.event_id, guests=info.guests,
                               scheduled_start=info.start, scheduled_end=info.end, consent_confirmed=True,
                               consent_at=request.consent_at, started_at=now, status=STATUS_RECORDING,
                               devices=dict(devices or {}))
        self.folder(meeting_id).mkdir(parents=True, exist_ok=False)
        self.save(record)
        logger.info("Meeting %s: created (scheduled: %s, guests: %d)", meeting_id, info.scheduled, len(info.guests))
        return record

    def save(self, record: MeetingRecord) -> None:
        _write_json(self.folder(record.id) / RECORD_FILE, record.to_json())

    def load(self, meeting_id: str) -> MeetingRecord | None:
        try:
            path = self.folder(meeting_id) / RECORD_FILE
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, Mapping) or data.get("id") != meeting_id:
            return None
        return MeetingRecord.from_json(data)

    def ids(self) -> list[str]:
        try:
            names = [entry.name for entry in self.root.iterdir() if entry.is_dir()]
        except OSError:
            return []
        return sorted((name for name in names if _ID_RE.fullmatch(name)), reverse=True)

    def list(self) -> list[MeetingRecord]:
        """Every readable meeting, newest first."""
        records = [self.load(meeting_id) for meeting_id in self.ids()]
        return [record for record in records if record is not None]

    def update(self, meeting_id: str, **changes: Any) -> MeetingRecord | None:
        record = self.load(meeting_id)
        if record is None:
            return None
        record = replace(record, **changes)
        self.save(record)
        return record

    def delete(self, meeting_id: str) -> bool:
        """The owner's Delete: the meeting's folder - audio, transcript and summary - at once."""
        try:
            folder = self.folder(meeting_id)
        except ValueError:
            return False
        if not folder.exists():
            return False
        shutil.rmtree(folder, ignore_errors=True)
        gone = not folder.exists()
        logger.info("Meeting %s: deleted by the owner (%s)", meeting_id, "done" if gone else "some files stayed")
        return gone

    def cleanup(self, now: datetime | None = None, *, active: Iterable[str] = ()) -> tuple[int, int]:
        """Retention: audio older than AUDIO_DAYS goes, a meeting older than TEXT_DAYS goes entirely. A meeting
        recording now (``active``) is never touched. Returns (audio files removed, meetings removed)."""
        now = now or self._now()
        busy = set(active)
        audio_removed = meetings_removed = 0
        for meeting_id in self.ids():
            if meeting_id in busy:
                continue
            record = self.load(meeting_id)
            started = self._started(meeting_id, record)
            if started is None:
                continue
            age = _age(now, started)
            if age >= timedelta(days=TEXT_DAYS):
                shutil.rmtree(self.root / meeting_id, ignore_errors=True)
                if not (self.root / meeting_id).exists():
                    meetings_removed += 1
                continue
            if age >= timedelta(days=AUDIO_DAYS):
                removed = 0
                for name in (*AUDIO_FILES.values(), *(f"{n}.part" for n in AUDIO_FILES.values())):
                    path = self.root / meeting_id / name
                    if path.exists():
                        try:
                            path.unlink()
                            removed += 1
                        except OSError:
                            logger.warning("Meeting %s: an audio file could not be removed yet", meeting_id)
                if removed:
                    audio_removed += removed
                    if record is not None and record.audio_deleted_at is None:
                        record.audio_deleted_at = now
                        self.save(record)
        if audio_removed or meetings_removed:
            logger.info("Meetings retention: %d audio file(s) and %d meeting(s) removed", audio_removed,
                        meetings_removed)
        return audio_removed, meetings_removed

    def _started(self, meeting_id: str, record: MeetingRecord | None) -> datetime | None:
        if record is not None and record.started_at is not None:
            return record.started_at
        try:                             # an unreadable record: the folder's own time (its id says it too)
            return datetime.strptime(meeting_id[:15], "%Y%m%d-%H%M%S")
        except ValueError:
            return None

    def recover(self, active: Iterable[str] = ()) -> list[str]:
        """Meetings still marked RECORDING (Jarvis ended without closing) that are not ``active``: their WAV
        headers are repaired and they become RECORDED (stop reason STOP_INTERRUPTED). Returns their ids.
        Meetings left TRANSCRIBING go back to RECORDED (their worker is gone)."""
        from .meeting_audio import repair_wav, wav_seconds

        busy = set(active)
        recovered = []
        for record in self.list():
            if record.id in busy:
                continue
            if record.status == STATUS_RECORDING:
                seconds = 0.0
                for channel in CHANNELS:
                    path = self.audio_path(record.id, channel)
                    if path.exists():
                        repair_wav(path)
                        seconds = max(seconds, wav_seconds(path))
                record.status, record.stop_reason = STATUS_RECORDED, STOP_INTERRUPTED
                record.duration_s = seconds
                if record.started_at is not None:
                    record.ended_at = record.started_at + timedelta(seconds=seconds)
                self.save(record)
                recovered.append(record.id)
                logger.info("Meeting %s: saved after an unexpected end (%.0f s)", record.id, seconds)
            elif record.status == STATUS_TRANSCRIBING:
                record.status, record.progress = STATUS_RECORDED, 0
                self.save(record)
        return recovered


def _age(now: datetime, then: datetime) -> timedelta:
    if (now.tzinfo is None) != (then.tzinfo is None):
        now, then = now.replace(tzinfo=None), then.replace(tzinfo=None)
    return now - then


# --------------------------------------------------------------------------
# Devices picked in Settings > Meetings
# --------------------------------------------------------------------------

class DevicePrefs:
    """The microphone and the call-audio device the owner picked ("" follows the Windows default)."""

    def __init__(self, root: Path) -> None:
        self.path = Path(root) / DEVICES_FILE
        self.microphone = ""
        self.call_audio = ""
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, Mapping):
            self.microphone = str(data.get("microphone") or "")[:200]
            self.call_audio = str(data.get("call_audio") or "")[:200]

    def set(self, *, microphone: str | None = None, call_audio: str | None = None) -> None:
        if microphone is not None:
            self.microphone = str(microphone)[:200]
        if call_audio is not None:
            self.call_audio = str(call_audio)[:200]
        if unsafe_root(self.path.parent):
            return
        try:
            _write_json(self.path, {"microphone": self.microphone, "call_audio": self.call_audio})
        except OSError as exc:
            logger.warning("Meetings: the device choice could not be saved (%s)", type(exc).__name__)
        logger.info("Meetings: devices set (microphone picked: %s, call audio picked: %s)",
                    bool(self.microphone), bool(self.call_audio))


def length_text(seconds: float) -> str:
    """"12:04" (minutes:seconds) under an hour, "1:02:03" from an hour on."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def merge_names(names: Sequence[str]) -> str:
    """Guest names in a sentence ("Ana, Ben and Cy"); for the start card only."""
    clean = [" ".join(str(n).split()) for n in names if str(n).strip()]
    if len(clean) <= 1:
        return "".join(clean)
    return ", ".join(clean[:-1]) + " and " + clean[-1]
