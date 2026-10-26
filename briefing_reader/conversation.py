"""The conversation Jarvis keeps on this PC for a day: ``%LOCALAPPDATA%\\briefing-reader\\conversation.json``.

    Turn(role, text, at, tone="", context=True)   one entry as the window showed it: who said it (ROLE_YOU,
                                    the owner's request as typed; ROLE_JARVIS, Jarvis's words), when (aware),
                                    its text, the tone it was shown in and whether Ask may be given it
                                    (False: a web research's request or answer, kept on screen only)
    recent(turns, now)              the turns of the last KEEP (24 hours), the newest MAX_TURNS, in order
    ConversationStore(path, *, clock=None)
        turns()                     what the file held when it was read: the last 24 hours, at most
                                    MAX_TURNS, oldest first (the window shows them again at a start)
        save(turns)                 the window's conversation as it is now (older than 24 hours left out,
                                    the newest MAX_TURNS kept), written only when it changed; False when it
                                    could not be written (kept for this run only)
        prune()                     the file without its turns older than 24 hours (at the start and
                                    daily); how many left it
        clear()                     the file deleted (the Transcript's Clear conversation); False when it
                                    could not be

The window's conversation (hud.ConversationLog.kept_entries: the owner's requests, Jarvis's answers,
announcements and results, oldest first; never a memory shortcut's lines, which stay on screen only) is
what is kept, so a restart does not wipe it, and what Ask is given as <conversation> (ask.context). The
app saves it a moment after it changes, hourly (so a turn leaves the file within the hour after it turns
a day old) and at the quit. The file holds the turns only: ``{"version": 1, "turns": [{"role": "you",
"text": "...", "at": "2026-10-24T09:41:00-07:00", "tone": "done"}, ...]}`` (``"context": false`` on a web
research's turns), ASCII-escaped JSON with LF line
ends (read also with a UTF-8 BOM), written through a temporary file, fsync and ``os.replace``; each text at
most TEXT_CAP characters. A missing file is an empty conversation; so is an unreadable, corrupt, too deep
or over MAX_FILE_BYTES one (logged with the exception's type only), and the next save replaces it: it only
ever holds what the window showed. A turn the file holds in a shape Jarvis never writes is left out.

Nothing here logs a text: counts and exception type names only. Qt-free; standard library only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONVERSATION_FILE = "conversation.json"
FILE_VERSION = 1
ROLE_YOU, ROLE_JARVIS = "you", "jarvis"          # hud.ROLE_YOU / hud.ROLE_JARVIS
ROLES = (ROLE_YOU, ROLE_JARVIS)
KEEP = timedelta(hours=24)
MAX_TURNS = 100                                  # hud.ConversationLog.MAX_ENTRIES
TEXT_CAP = 20_000                                # one turn's text in the file
MAX_FILE_BYTES = 16_000_000                      # 100 turns of 20,000 escaped characters stay well under it
_TONE_RE = re.compile(r"[a-z]{1,16}")


@dataclass(frozen=True)
class Turn:
    """One entry of the conversation as the window showed it (never logged: the repr has no text)."""

    role: str           # ROLE_YOU | ROLE_JARVIS
    text: str
    at: datetime        # aware
    tone: str = ""      # hud.TONE_* ("" for the window's default)
    context: bool = True    # given to Ask as <conversation> (False: a web research's exchange, never)

    def __repr__(self) -> str:   # never the text
        return f"Turn({self.role}, {len(self.text)} chars)"


def _aware(moment: datetime) -> datetime:
    return moment if moment.utcoffset() is not None else moment.astimezone()


def recent(turns: Sequence[Turn], now: datetime) -> tuple[Turn, ...]:
    """The turns said in the last KEEP before ``now`` (a turn stamped later than ``now``, after a clock
    change, stays), the newest MAX_TURNS, in their order."""
    since = _aware(now) - KEEP
    kept = [turn for turn in turns if _aware(turn.at) >= since]
    return tuple(kept[-MAX_TURNS:])


def _cut(text: str) -> str:
    return text if len(text) <= TEXT_CAP else text[:TEXT_CAP - 3].rstrip() + "..."


def _turn_from(item: Any) -> Turn | None:
    """One turn of the file, or None when it is not in the shape Jarvis writes."""
    if not isinstance(item, dict):
        return None
    role, text, at, tone = item.get("role"), item.get("text"), item.get("at"), item.get("tone", "")
    context = item.get("context", True)
    if role not in ROLES or not isinstance(text, str) or not text.strip() or not isinstance(at, str):
        return None
    if not isinstance(context, bool):
        return None
    if not isinstance(tone, str) or (tone and not _TONE_RE.fullmatch(tone)):
        return None
    try:
        moment = datetime.fromisoformat(at[:40])
    except ValueError:
        return None
    if moment.utcoffset() is None:
        return None
    return Turn(role, _cut(text), moment, tone, context)


def _parse(raw: bytes) -> list[Turn]:
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("size")
    data = json.loads(raw.decode("utf-8-sig"))   # a BOM (an editor) is fine; Jarvis writes none
    if not isinstance(data, dict) or data.get("version") != FILE_VERSION or not isinstance(data.get("turns"), list):
        raise ValueError("shape")
    return [turn for turn in (_turn_from(item) for item in data["turns"]) if turn is not None]


def _serialize(turns: Sequence[Turn]) -> str:
    rows = []
    for turn in turns:
        row: dict[str, Any] = {"role": turn.role, "text": _cut(turn.text),
                               "at": turn.at.isoformat(timespec="seconds"), "tone": turn.tone}
        if not turn.context:
            row["context"] = False        # a web research's exchange: shown again, never given to Ask
        rows.append(row)
    return json.dumps({"version": FILE_VERSION, "turns": rows}, ensure_ascii=True, indent=1) + "\n"


class ConversationStore:
    """conversation.json: the last day of the window's conversation. Thread-safe; see the module docs."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._lock = threading.Lock()
        self.saved = True
        self._disk: str | None = None     # the text Jarvis last read or wrote (None: no file, or not ours)
        self._held: list[Turn] = []       # the turns the file holds, as last read or written
        with self._lock:
            self._held = self._read()

    def turns(self) -> tuple[Turn, ...]:
        """What the file held (the last 24 hours, the newest MAX_TURNS), oldest first."""
        with self._lock:
            return recent(self._held, self._clock())

    def save(self, turns: Sequence[Turn]) -> bool:
        """The window's conversation as it is now: the turns of the last 24 hours, the newest MAX_TURNS,
        written atomically when they changed (nothing said and no file: none is made). True when the
        file holds them (also when nothing changed)."""
        with self._lock:
            kept = recent(turns, self._clock())
            if not kept and not self._held and not self.path.exists():
                return True
            return self._write(kept)

    def prune(self) -> int:
        """The file without its turns older than 24 hours (the start and daily): how many left it (0: the
        file stays as it is)."""
        with self._lock:
            kept = recent(self._held, self._clock())
            gone = len(self._held) - len(kept)
            if gone > 0:
                self._write(kept)
                logger.info("Conversation: %d turn(s) older than a day left conversation.json", gone)
            return max(0, gone)

    def clear(self) -> bool:
        """Clear conversation: the file is deleted (False when it could not be: it is emptied instead, if
        that can be done)."""
        with self._lock:
            self._held = []
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("Conversation: could not delete conversation.json (%s)", type(exc).__name__)
                self._write([], force=True)
                return False
            self._disk = None
            self.saved = True
            return True

    # ---- inside the lock --------------------------------------------------------------------------

    def _read(self) -> list[Turn]:
        try:
            with open(self.path, "rb") as fh:
                raw = fh.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("Conversation: could not read conversation.json (%s); starting without it",
                           type(exc).__name__)
            return []
        try:
            turns = _parse(raw)
        except (UnicodeDecodeError, ValueError, RecursionError, MemoryError) as exc:   # not ours: replaced later
            logger.warning("Conversation: conversation.json can't be read (%s); starting without it",
                           type(exc).__name__)
            return []
        self._disk = raw.decode("utf-8-sig") if raw.isascii() else None
        return turns

    def _write(self, turns: Sequence[Turn], *, force: bool = False) -> bool:
        text = _serialize(turns)
        if text == self._disk and not force:
            self._held = list(turns)
            return True
        part: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, part = tempfile.mkstemp(prefix=f"{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="ascii", newline="\n") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(part, self.path)
            part = None
        except OSError as exc:
            logger.warning("Conversation: could not save conversation.json (%s); kept for this run only",
                           type(exc).__name__)
            self.saved = False
            return False
        finally:
            if part is not None:
                try:
                    os.unlink(part)
                except OSError:
                    pass
        self._disk = text
        self._held = list(turns)
        self.saved = True
        return True
