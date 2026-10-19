"""Meeting recording, part 1: the speech model (downloaded only on the owner's click) and the transcription.

Speech to text runs on this PC with faster-whisper (owner decision: model medium.en, CPU, int8). The model is
``Systran/faster-whisper-medium.en`` from Hugging Face (about 1.5 GB), stored in
``%LOCALAPPDATA%\\briefing-reader\\models\\faster-whisper-medium.en``. ``ModelDownloader`` fetches it only when
the owner clicks Download (Settings > Meetings or the Meetings screen): with progress, resumable (each file
grows in ``<name>.part`` and continues with an HTTP Range request), file checks (the size Hugging Face lists and
the SHA-256 it lists for its large files), then ``manifest.json``. The app never runs pip: a missing add-on is
named on the Meetings screen (``missing_pieces``).

The transcription is a separate background process at below-normal priority (``TranscriptionProcess``:
``python -m briefing_reader.meeting_speech --meeting DIR --model DIR``) that reports progress as JSON lines on
its stdout (never the text) and writes ``transcript.json`` into the meeting's folder: each channel transcribed on
its own, then merged by time into turns - the microphone as "You", the call audio as "Others".
``transcribe_meeting(folder, transcriber, progress)`` is that work, with any transcriber (tests: a fake one).

Public API
==========
``MODEL_REPO``, ``MODEL_FILES``, ``MODEL_SIZE_TEXT``, ``model_dir(models_root)``, ``model_ready(dir)``,
``speech_missing()`` (the faster-whisper add-on, or ""), ``missing_pieces(models_root)`` -> list of
``Missing(key, text)`` (add-ons and the model, in the words the Meetings screen shows).
``ModelDownloader(target, session=None, progress=None, cancel=None)``: ``run()`` -> "" or a short error code.
``Segment(start, end, text)``, ``merge_turns(mic, loopback, gap=)``, ``transcribe_meeting(...)``.
``FasterWhisperTranscriber(model_dir)`` (the add-on behind a small adapter, imported only when used).
``TranscriptionProcess(meeting_dir, model_dir, on_progress, on_done)``: ``start()``, ``cancel()`` (kills the worker
and waits until it ended), ``running()``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import threading
import wave
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import meetings as mt

logger = logging.getLogger(__name__)

MODEL_NAME = "medium.en"
MODEL_REPO = "Systran/faster-whisper-medium.en"
MODEL_FOLDER = "faster-whisper-medium.en"
MODEL_REVISION = "main"
MODEL_FILES = ("config.json", "tokenizer.json", "vocabulary.txt", "model.bin")
MODEL_SIZE_TEXT = "about 1.5 GB"
MODEL_URL = "https://huggingface.co/{repo}/resolve/{revision}/{name}"
MANIFEST = "manifest.json"
DOWNLOAD_TEXT = f"Download the speech model - {MODEL_SIZE_TEXT} from Hugging Face ({MODEL_REPO})"
SPEECH_MODULE = "faster_whisper"
ADDON_SPEECH = "faster-whisper"
CHUNK = 1 << 20
TIMEOUT_S = 30
MERGE_GAP_S = 1.5
CANCEL_WAIT_S = 5.0               # a stopped worker is waited for this long (its files are free after)
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
CREATE_NO_WINDOW = 0x08000000
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_GIT_SHA1_RE = re.compile(r"[0-9a-f]{40}")
GIT_PREFIX = "git:"                # a small file's check: its git blob SHA-1 (Hugging Face's X-Linked-Etag)
_IDENTITY = {"Accept-Encoding": "identity"}   # the files as stored: a gzip answer has another size

ERR_NETWORK = "network"
ERR_CHECK = "check_failed"
ERR_DISK = "disk"
ERR_CANCELLED = "cancelled"
ERR_ADDON = "addon_missing"
ERR_MODEL = "model_missing"
ERR_AUDIO = "audio_missing"
ERR_WORKER = "worker_failed"


# --------------------------------------------------------------------------
# What is missing
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Missing:
    key: str           # "audio_addon", "speech_addon", "model"
    text: str


def model_dir(models_root: Path) -> Path:
    return Path(models_root) / MODEL_FOLDER


def model_ready(folder: Path) -> bool:
    """Every model file there with the size its manifest recorded after the checks."""
    try:
        manifest = json.loads((Path(folder) / MANIFEST).read_text(encoding="utf-8"))
        files = manifest.get("files", {})
        return all((Path(folder) / name).is_file() and (Path(folder) / name).stat().st_size == int(files[name])
                   for name in MODEL_FILES)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def speech_missing() -> str:
    try:
        return "" if importlib.util.find_spec(SPEECH_MODULE) is not None else ADDON_SPEECH
    except (ImportError, ValueError):
        return ADDON_SPEECH


def missing_pieces(models_root: Path, *, audio_missing: Callable[[], str] | None = None,
                   speech: Callable[[], str] | None = None) -> list[Missing]:
    """What the Meetings screen says is missing, exactly (empty: everything is there)."""
    if audio_missing is None:
        from .meeting_audio import backend_missing as audio_missing
    pieces = []
    audio = audio_missing()
    if audio:
        pieces.append(Missing("audio_addon", f"The Python add-on {audio}, to record the microphone and the call "
                                             "audio. It is installed with your OK; Jarvis never installs it."))
    words = (speech or speech_missing)()
    if words:
        pieces.append(Missing("speech_addon", f"The Python add-on {words}, to write the meeting down on this PC. "
                                              "It is installed with your OK; Jarvis never installs it."))
    if not model_ready(model_dir(models_root)):
        pieces.append(Missing("model", f"The speech model {MODEL_NAME} ({MODEL_SIZE_TEXT}). It is downloaded only "
                                       "when you click Download."))
    return pieces


# --------------------------------------------------------------------------
# The model download (only on the owner's click)
# --------------------------------------------------------------------------

class ModelDownloader:
    """See the module docstring. ``progress(done_bytes, total_bytes)`` is called on the downloading thread;
    ``cancel()`` (an Event-like ``is_set``) stops between chunks. ``session`` is a requests-like session
    (``head(url, allow_redirects=, timeout=)`` and ``get(url, headers=, stream=, timeout=)``)."""

    def __init__(self, target: Path, *, session: Any = None, progress: Callable[[int, int], Any] | None = None,
                 cancel: Any = None, repo: str = MODEL_REPO, revision: str = MODEL_REVISION,
                 files: Sequence[str] = MODEL_FILES) -> None:
        self.target = Path(target)
        self._session = session
        self._progress = progress
        self._cancel = cancel or threading.Event()
        self.repo, self.revision, self.files = repo, revision, tuple(files)

    def cancel(self) -> None:
        self._cancel.set()

    def _http(self) -> Any:
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _url(self, name: str) -> str:
        return MODEL_URL.format(repo=self.repo, revision=self.revision, name=name)

    def _expected(self, name: str) -> tuple[int, str]:
        """(size, check) from the listing's headers. Hugging Face: a large file answers with X-Linked-Size and
        its SHA-256 as X-Linked-Etag; a small one redirects (its Content-Length is the redirect's own body), so
        the size comes from following it uncompressed and the check is its git blob SHA-1 ("git:<sha1>")."""
        response = self._http().head(self._url(name), allow_redirects=False, timeout=TIMEOUT_S)
        status = int(getattr(response, "status_code", 0))
        if status >= 400:
            raise ConnectionError(f"listing answered {status}")
        headers = {str(k).lower(): str(v) for k, v in dict(response.headers).items()}
        size = headers.get("x-linked-size") or headers.get("content-length") or "0"
        etag = (headers.get("x-linked-etag") or "").strip().strip('"').lower()
        if status in (301, 302, 303, 307, 308) and not headers.get("x-linked-size"):
            follow = self._http().head(self._url(name), allow_redirects=True, timeout=TIMEOUT_S,
                                       headers=dict(_IDENTITY))
            size = str(dict(follow.headers).get("Content-Length") or dict(follow.headers).get("content-length")
                       or size)
        if _SHA256_RE.fullmatch(etag):
            return int(size), etag
        return int(size), GIT_PREFIX + etag if _GIT_SHA1_RE.fullmatch(etag) else ""

    def run(self) -> str:
        """Fetch every file (continuing the ``.part`` files), check them, write the manifest. "" when the model
        is ready, else a short error code (ERR_*). Never raises: a file error (a scanner holding the new file,
        say) is ERR_DISK, so the app always hears that the download ended."""
        try:
            return self._run()
        except OSError as exc:
            logger.info("Speech model: a file could not be written (%s)", type(exc).__name__)
            return ERR_DISK
        except Exception as exc:  # noqa: BLE001 - a download never dies silently
            logger.info("Speech model: the download stopped (%s)", type(exc).__name__)
            return ERR_NETWORK

    def _run(self) -> str:
        try:
            self.target.mkdir(parents=True, exist_ok=True)
            expected = {name: self._expected(name) for name in self.files}
        except OSError:
            return ERR_DISK
        except Exception as exc:  # noqa: BLE001 - network problems are a code, never a crash
            logger.info("Speech model: the file list could not be read (%s)", type(exc).__name__)
            return ERR_NETWORK
        total = sum(size for size, _sha in expected.values())
        done_before = 0
        for name in self.files:
            size, sha = expected[name]
            final = self.target / name
            if final.is_file() and final.stat().st_size == size and not sha:
                done_before += size
                continue
            code = self._fetch(name, size, sha, done_before, total)
            if code:
                logger.info("Speech model: %s stopped (%s)", name, code)
                return code
            done_before += size
        manifest = {"repo": self.repo, "revision": self.revision,
                    "files": {name: expected[name][0] for name in self.files},
                    "sha256": {name: expected[name][1] for name in self.files
                               if expected[name][1] and not expected[name][1].startswith(GIT_PREFIX)}}
        try:
            mt._write_json(self.target / MANIFEST, manifest)
        except OSError:
            return ERR_DISK
        logger.info("Speech model: ready (%d files, %d MB)", len(self.files), total // (1 << 20))
        return ""

    def _fetch(self, name: str, size: int, sha: str, done_before: int, total: int) -> str:
        final = self.target / name
        part = self.target / f"{name}.part"
        if final.is_file() and final.stat().st_size == size:
            final.replace(part)                      # checked again below (its hash, when Hugging Face gave one)
        have = part.stat().st_size if part.exists() else 0
        if have > size:
            part.unlink()
            have = 0
        if have < size:
            headers = dict(_IDENTITY)
            if have:
                headers["Range"] = f"bytes={have}-"
            try:
                response = self._http().get(self._url(name), headers=headers, stream=True, timeout=TIMEOUT_S)
                status = int(getattr(response, "status_code", 0))
                if status >= 400:
                    return ERR_NETWORK
                if have and status != 206:            # the server ignored the Range: start this file again
                    have = 0
                fh = open(part, "ab" if have else "wb")
            except Exception as exc:  # noqa: BLE001 - network problems are a code; the .part file stays
                logger.info("Speech model: the download could not start (%s)", type(exc).__name__)
                return ERR_DISK if isinstance(exc, PermissionError) else ERR_NETWORK
            with fh:
                chunks = iter(response.iter_content(CHUNK))
                while True:
                    if self._cancel.is_set():
                        return ERR_CANCELLED
                    try:
                        chunk = next(chunks, None)
                    except Exception as exc:  # noqa: BLE001 - the connection dropped; the .part file stays
                        logger.info("Speech model: download interrupted (%s)", type(exc).__name__)
                        return ERR_NETWORK
                    if chunk is None:
                        break
                    if not chunk:
                        continue
                    try:
                        fh.write(chunk)
                    except OSError:
                        return ERR_DISK
                    have += len(chunk)
                    if self._progress is not None:
                        self._progress(done_before + have, total)
        if part.stat().st_size != size:
            return ERR_NETWORK if part.stat().st_size < size else ERR_CHECK
        if sha:
            digest = (GIT_PREFIX + _git_sha1(part, self._cancel) if sha.startswith(GIT_PREFIX)
                      else _sha256(part, self._cancel))
            if self._cancel.is_set():
                return ERR_CANCELLED             # stopped while checking: the whole .part stays (checked next time)
            if digest != sha:
                part.unlink(missing_ok=True)
                return ERR_CHECK
        part.replace(final)
        return ""


def _git_sha1(path: Path, cancel: Any) -> str:
    """The file's git blob SHA-1 (what Hugging Face gives as the X-Linked-Etag of a small file)."""
    digest = hashlib.sha1(b"blob %d\x00" % path.stat().st_size, usedforsecurity=False)
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            if cancel.is_set():
                return ""
            digest.update(chunk)
    return digest.hexdigest()


def _sha256(path: Path, cancel: Any) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            if cancel.is_set():
                return ""
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# The transcription
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str = field(repr=False)


def merge_turns(mic: Iterable[Segment], loopback: Iterable[Segment], gap: float = MERGE_GAP_S) -> list[dict]:
    """Both channels' segments in time order as turns ({speaker, start, end, text}): "You" for the microphone,
    "Others" for the call audio; a speaker's segments closer than ``gap`` seconds join one turn."""
    tagged = [(seg, mt.SPEAKERS[mt.MIC]) for seg in mic] + [(seg, mt.SPEAKERS[mt.LOOPBACK]) for seg in loopback]
    tagged.sort(key=lambda item: (item[0].start, item[0].end))
    turns: list[dict] = []
    for seg, speaker in tagged:
        text = " ".join(seg.text.split())
        if not text:
            continue
        last = turns[-1] if turns else None
        if last is not None and last["speaker"] == speaker and seg.start - last["end"] <= gap:
            last["text"] = f"{last['text']} {text}"
            last["end"] = max(last["end"], round(seg.end, 2))
        else:
            turns.append({"speaker": speaker, "start": round(seg.start, 2), "end": round(seg.end, 2), "text": text})
    return turns


def wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as wav:
            return wav.getnframes() / float(wav.getframerate() or mt.SAMPLE_RATE)
    except (OSError, wave.Error, EOFError):
        return 0.0


class FasterWhisperTranscriber:
    """faster-whisper behind a small adapter: ``WhisperModel(model_dir, device="cpu", compute_type="int8")``;
    ``transcribe(path)`` yields Segments (``vad_filter`` skips the silence, English only)."""

    def __init__(self, folder: Path, *, threads: int = 0) -> None:
        module = importlib.import_module(SPEECH_MODULE)
        self._model = module.WhisperModel(str(folder), device="cpu", compute_type="int8",
                                          cpu_threads=threads or max(1, (os.cpu_count() or 4) - 2))

    def transcribe(self, path: Path) -> Iterable[Segment]:
        segments, _info = self._model.transcribe(str(path), language="en", beam_size=5, vad_filter=True)
        for seg in segments:
            yield Segment(float(seg.start), float(seg.end), str(seg.text))


def transcribe_meeting(folder: Path, transcriber: Any, progress: Callable[[int], Any] | None = None) -> dict:
    """Transcribe ``mic.wav`` and ``loopback.wav`` in ``folder``, merge them and write ``transcript.json``.
    ``progress(percent)`` as the audio is worked through (both files together). Returns the transcript."""
    folder = Path(folder)
    paths = {kind: folder / mt.AUDIO_FILES[kind] for kind in mt.CHANNELS}
    present = {kind: path for kind, path in paths.items() if path.is_file()}
    if not present:
        raise FileNotFoundError("no audio")
    durations = {kind: wav_duration(path) for kind, path in present.items()}
    total = sum(durations.values()) or 1.0
    done_before = 0.0
    last = -1
    found: dict[str, list[Segment]] = {kind: [] for kind in mt.CHANNELS}
    for kind in mt.CHANNELS:
        path = present.get(kind)
        if path is None:
            continue
        for seg in transcriber.transcribe(path):
            found[kind].append(seg)
            percent = int(min(99, 100 * (done_before + min(seg.end, durations[kind])) / total))
            if progress is not None and percent > last:
                last = percent
                progress(percent)
        done_before += durations[kind]
        percent = int(min(99, 100 * done_before / total))
        if progress is not None and percent > last:
            last = percent
            progress(percent)
    transcript = {"version": 1, "speakers": dict(mt.SPEAKERS),
                  "durations": {kind: round(seconds, 1) for kind, seconds in durations.items()},
                  "turns": merge_turns(found[mt.MIC], found[mt.LOOPBACK])}
    mt._write_json(folder / mt.TRANSCRIPT_FILE, transcript)
    if progress is not None:
        progress(100)
    return transcript


class TranscriptionProcess:
    """The background process; ``on_progress(percent)`` and ``on_done(error_code)`` ("" = done) are called on
    its reader thread. ``python`` defaults to this interpreter."""

    def __init__(self, meeting_dir: Path, model: Path, *, on_progress: Callable[[int], Any],
                 on_done: Callable[[str], Any], python: str | None = None) -> None:
        self.meeting_dir, self.model = Path(meeting_dir), Path(model)
        self._on_progress, self._on_done = on_progress, on_done
        self._python = python or sys.executable
        self._proc: subprocess.Popen | None = None
        self._cancelled = False

    def start(self) -> None:
        flags = BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW if os.name == "nt" else 0
        root = str(Path(__file__).resolve().parent.parent)
        self._proc = subprocess.Popen(
            [self._python, "-B", "-m", "briefing_reader.meeting_speech", "--meeting", str(self.meeting_dir),
             "--model", str(self.model)], cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=flags)
        threading.Thread(target=self._read, name="meeting-transcribe", daemon=True).start()

    def _read(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        error = ERR_WORKER
        for raw in proc.stdout:
            try:
                message = json.loads(raw.decode("ascii", "replace"))
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if isinstance(message.get("progress"), int):
                self._on_progress(max(0, min(100, message["progress"])))
            if message.get("done") is True:
                error = ""
            elif isinstance(message.get("error"), str):
                error = message["error"][:40]
        proc.stdout.close()
        proc.wait()
        self._on_done(ERR_CANCELLED if self._cancelled else error)

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def cancel(self, wait_s: float = CANCEL_WAIT_S) -> bool:
        """Stop the worker and wait (up to ``wait_s``) until it has ended, so the files it held are free (a
        Delete right after removes them all). True when no worker is left running."""
        self._cancelled = True
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return True
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=wait_s)
        except subprocess.TimeoutExpired:
            logger.info("Meetings: the writing down did not end within %.0f s", wait_s)
            return False
        return True


def _emit(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    """The worker process: progress and the outcome as JSON lines; never the text, never a library message."""
    parser = argparse.ArgumentParser(prog="briefing_reader.meeting_speech")
    parser.add_argument("--meeting", required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args(argv)
    if speech_missing():
        _emit({"error": ERR_ADDON})
        return 2
    if not model_ready(Path(args.model)):
        _emit({"error": ERR_MODEL})
        return 2
    try:
        transcriber = FasterWhisperTranscriber(Path(args.model))
        transcribe_meeting(Path(args.meeting), transcriber, lambda percent: _emit({"progress": percent}))
    except FileNotFoundError:
        _emit({"error": ERR_AUDIO})
        return 1
    except Exception as exc:  # noqa: BLE001 - only the kind of failure goes out
        _emit({"error": ERR_WORKER, "kind": type(exc).__name__})
        return 1
    _emit({"done": True})
    return 0


if __name__ == "__main__":
    sys.exit(main())
