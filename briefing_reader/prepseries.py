"""Recurring meetings whose prep offers the owner ignored: ``%LOCALAPPDATA%\\briefing-reader\\prep_series.json``.

A prep offer that ends unclicked counts as ignored for its recurring series; after three in a row
the series is not offered any more (a typed "prep me" still works and resets the count; R38). The
file holds no title, id or address: ``{"version": 1, "series": {"<32 hex key>": {"ignored": 2,
"day": "2026-10-08"}}}``, the key being ``sha256(alias, calendar id, recurringEventId)`` cut to 32
hex characters. Entries untouched for 60 days are pruned; a missing, corrupt or unreadable file
counts as empty (offers simply come back) and never stops the app.

Written atomically (lock, temporary file, fsync, ``os.replace``), LF, ASCII. Qt-free.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

SERIES_FILE = "prep_series.json"
IGNORED_LIMIT = 3
PRUNE_DAYS = 60
MAX_SERIES = 500
_KEY_LEN = 32


def series_key(alias: str, calendar_id: str, series_id: str) -> str:
    """The file's key of one recurring series: a hash, never the ids in clear."""
    raw = "\x1f".join((str(alias or ""), str(calendar_id or ""), str(series_id or "")))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_KEY_LEN]


def _local_today() -> datetime:
    return datetime.now().astimezone()


class SeriesStore:
    """prep_series.json: how often each recurring series was ignored in a row, and the day of the
    last change. Every change re-reads the file first, prunes and writes atomically; a failed write
    is kept in memory for this run."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self._clock = clock or _local_today
        self._lock = threading.Lock()
        self._series: dict[str, dict[str, object]] = _load(self.path)

    def __len__(self) -> int:
        with self._lock:
            return len(self._series)

    def ignored(self, key: str) -> int:
        """How many prep offers of that series ended unclicked in a row (0 when unknown)."""
        with self._lock:
            entry = self._series.get(str(key))
            return int(entry["ignored"]) if entry is not None else 0

    def record_ignored(self, key: str) -> bool:
        """One more offer ended unclicked; True when the change was written."""
        if not _valid_key(key):
            return False
        with self._lock:
            self._series = _merge(_load(self.path), self._series)
            entry = self._series.setdefault(key, {"ignored": 0, "day": ""})
            entry["ignored"] = int(entry.get("ignored", 0)) + 1
            entry["day"] = self._today().isoformat()
            self._prune_locked()
            return _write(self.path, self._series)

    def reset(self, key: str) -> bool:
        """A click on Prepare brief or a typed prep of that series: the count goes back to 0; True
        when something changed and was written (an unknown key changes nothing)."""
        if not _valid_key(key):
            return False
        with self._lock:
            self._series = _merge(_load(self.path), self._series)
            entry = self._series.get(key)
            if entry is None or int(entry.get("ignored", 0)) == 0:
                return False
            entry["ignored"] = 0
            entry["day"] = self._today().isoformat()
            self._prune_locked()
            return _write(self.path, self._series)

    def _today(self) -> date:
        now = self._clock()
        return now.date() if isinstance(now, datetime) else now

    def _prune_locked(self) -> int:
        cutoff = self._today() - timedelta(days=PRUNE_DAYS)
        gone = [key for key, entry in self._series.items() if _entry_day(entry) < cutoff]
        for key in gone:
            del self._series[key]
        while len(self._series) > MAX_SERIES:   # the oldest day first
            oldest = min(self._series, key=lambda item: (_entry_day(self._series[item]), item))
            del self._series[oldest]
        return len(gone)


def _valid_key(key: object) -> bool:
    return isinstance(key, str) and len(key) == _KEY_LEN and all(ch in "0123456789abcdef" for ch in key)


def _entry_day(entry: dict[str, object]) -> date:
    try:
        return date.fromisoformat(str(entry.get("day", "")))
    except ValueError:
        return date.min


_Series = dict[str, dict[str, object]]


def _merge(on_disk: _Series, in_memory: _Series) -> _Series:
    """The file's entries, with this process's newer ones on top (the later day wins)."""
    merged = {key: dict(entry) for key, entry in on_disk.items()}
    for key, entry in in_memory.items():
        kept = merged.get(key)
        if kept is None or _entry_day(entry) >= _entry_day(kept):
            merged[key] = dict(entry)
    return merged


def _load(path: Path) -> dict[str, dict[str, object]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        logger.warning("Could not read %s (%s); starting without ignored series", path.name, type(exc).__name__)
        return {}
    try:
        data = json.loads(text)
    except (ValueError, RecursionError, MemoryError) as exc:   # not JSON, or nested too deep: not ours
        logger.warning("%s is not valid JSON (%s); starting without ignored series", path.name, type(exc).__name__)
        return {}
    series = data.get("series") if isinstance(data, dict) else None
    if not isinstance(series, dict):
        logger.warning("%s is not a table of series; starting without ignored series", path.name)
        return {}
    kept: dict[str, dict[str, object]] = {}
    for key, value in series.items():
        if not _valid_key(key) or not isinstance(value, dict):
            continue
        count = value.get("ignored")
        day = value.get("day")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0 or not isinstance(day, str):
            continue
        try:
            date.fromisoformat(day)
        except ValueError:
            continue
        kept[key] = {"ignored": count, "day": day}
    return kept


def _write(path: Path, series: dict[str, dict[str, object]]) -> bool:
    data = json.dumps({"version": 1, "series": series}, ensure_ascii=True, indent=1, sort_keys=True) + "\n"
    part: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, part = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(part, path)
        part = None
        return True
    except OSError as exc:
        logger.warning("Could not save %s (%s); kept for this run only", path.name, type(exc).__name__)
        return False
    finally:
        if part is not None:
            try:
                os.unlink(part)
            except OSError:
                pass
