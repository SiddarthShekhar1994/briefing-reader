"""Which scheduled briefings were answered: ``%LOCALAPPDATA%\\briefing-reader\\runstate.json``.

A *slot* is one occurrence of a scheduled run: with the PM task at 23:42,
"2026-10-04 PM" is the PM briefing of October 4, also when it is answered
after midnight. The scheduled tasks pass the slot times with
``--slots am=10:12,pm=23:42`` (``config.toml [schedule]`` is the fallback).

* :func:`slot_for` is what the catch-up launch (``--catch-up``, at logon and
  unlock) asks: which slot passed in the last few hours?
* :func:`handled_slot_key` is the slot a Read now / Dismiss / Done settles.
* :func:`briefing_key` is the slot a fetched briefing belongs to (by its
  "Updated:" time and run), so "announced" and "viewed" stick to that briefing.
* :class:`RunState` remembers per slot when its prompt was first shown, when
  and how it was answered ("read", "dismissed", "done"), when its
  briefing was first announced ("ready to view") and first viewed, and (the
  productivity bundle) when a plan offer was posted for it and when its day
  was planned, for 14 days (a slot dated in the future, such as tomorrow's
  after an evening "plan tomorrow", is kept too).

The file is written atomically (temporary file + ``os.replace``); a missing,
corrupt or unreadable file counts as empty and never stops the app. Times
are wall-clock times in the time zone of the ``now`` they are compared with.

Qt-free; standard library only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

RUNSTATE_FILE = "runstate.json"
RUNS = ("AM", "PM")
ANSWERS = ("read", "dismissed", "done")
HEARD_ANSWERS = ("read", "done")      # answers that mean the briefing was heard (so viewed)
DEFAULT_SLOTS: Mapping[str, str] = {"AM": "10:12", "PM": "23:42"}
CATCH_UP_MAX_AGE = timedelta(hours=3)   # a slot older than this is not caught up
ANSWER_MAX_AGE = timedelta(hours=18)    # an answer this long after a slot still settles it
BRIEFING_SLOT_WINDOW = timedelta(hours=12)   # a briefing belongs to a slot this close to its update
KEEP_DAYS = 14
ENTRY_FIELDS = ("shown_at", "handled_at", "how", "announced_at", "viewed_at", "plan_offered_at", "planned_at")

_TIME_RE = re.compile(r"([01]?[0-9]|2[0-3]):([0-5][0-9])")
_KEY_RE = re.compile(r"(\d{4}-\d{2}-\d{2}) (AM|PM)")
_SLOTS_ITEM_RE = re.compile(r"\s*([A-Za-z]+)\s*=\s*([0-9:]+)\s*")


# --------------------------------------------------------------------------
# Slot times
# --------------------------------------------------------------------------

def parse_time_of_day(text: object) -> time:
    """"HH:MM" (24-hour, a single-digit hour is fine) -> time; ValueError otherwise."""
    match = _TIME_RE.fullmatch(text.strip()) if isinstance(text, str) else None
    if match is None:
        raise ValueError("not a 24-hour time like 10:12 or 23:42")
    return time(int(match.group(1)), int(match.group(2)))


def normalize_slots(slots: Mapping[str, object]) -> dict[str, str]:
    """{"am": "9:05"} -> {"AM": "09:05"}; ValueError for an unknown run or a bad time."""
    result: dict[str, str] = {}
    for run, value in slots.items():
        name = str(run).strip().upper()
        if name not in RUNS:
            raise ValueError(f"unknown run {str(run)[:20]!r} (use am or pm)")
        result[name] = parse_time_of_day(value).strftime("%H:%M")
    return result


def parse_slots(text: str) -> dict[str, str]:
    """``"am=10:12,pm=23:42"`` -> ``{"AM": "10:12", "PM": "23:42"}``; ValueError when malformed."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("no slots given")
    pairs: dict[str, str] = {}
    for item in text.split(","):
        match = _SLOTS_ITEM_RE.fullmatch(item)
        if match is None:
            raise ValueError("expected run=HH:MM pairs such as am=10:12,pm=23:42")
        run = match.group(1).upper()
        if run in pairs:
            raise ValueError(f"{run} is given twice")
        pairs[run] = match.group(2)
    return normalize_slots(pairs)


def format_slots(slots: Mapping[str, str]) -> str:
    """The ``--slots`` argument for ``slots``: ``"am=10:12,pm=23:42"``."""
    clean = normalize_slots(slots)
    return ",".join(f"{run.lower()}={clean[run]}" for run in RUNS if run in clean)


# --------------------------------------------------------------------------
# Slots
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Slot:
    """One occurrence of a scheduled run; ``at`` carries the tzinfo of the ``now`` it came from."""

    run: str        # "AM" or "PM"
    at: datetime

    @property
    def key(self) -> str:
        """"YYYY-MM-DD AM": the date of the occurrence, not of the answer."""
        return f"{self.at:%Y-%m-%d} {self.run}"


def slot_for(now: datetime, slots: Mapping[str, str],
             max_age: timedelta = CATCH_UP_MAX_AGE) -> Slot | None:
    """The most recent slot occurrence at or before ``now``, if it is at most ``max_age`` old.

    Only the latest occurrence counts: once the PM slot has passed, a missed
    AM slot of the same day is not offered any more. An unusable ``slots``
    mapping yields None (logged at DEBUG).
    """
    try:
        times = {run: parse_time_of_day(value) for run, value in normalize_slots(slots).items()}
    except ValueError as exc:
        logger.debug("No usable slot times (%s)", exc)
        return None
    wall = now.replace(tzinfo=None)
    latest: Slot | None = None
    for run, at_time in times.items():
        for days_back in range(max(0, max_age.days) + 2):
            at = datetime.combine(wall.date() - timedelta(days=days_back), at_time)
            if at > wall or wall - at > max_age:
                continue
            if latest is None or at > latest.at.replace(tzinfo=None):
                latest = Slot(run=run, at=at.replace(tzinfo=now.tzinfo))
    return latest


def handled_slot_key(now: datetime, slots: Mapping[str, str],
                     max_age: timedelta = ANSWER_MAX_AGE) -> str | None:
    """The slot an answer at ``now`` settles: the latest occurrence within ``max_age`` (18 h)."""
    slot = slot_for(now, slots, max_age=max_age)
    return slot.key if slot is not None else None


def briefing_key(header: Any, slots: Mapping[str, str] | None = None) -> str | None:
    """The slot key ("2026-10-08 AM") a fetched briefing belongs to; None without an update time.

    ``header`` is a :class:`briefing_reader.models.BriefingHeader` (``updated_at``, ``run``).
    The candidates are the header's run ("AM"/"PM"), or both runs when it names none; for
    each, the slot time (``slots``, else :data:`DEFAULT_SLOTS`) on the day before, the day of
    and the day after the update (wall time in the update's own zone). The occurrence closest
    to the update within 12 hours wins (ties: the earlier one). A PM briefing written at 00:10
    therefore belongs to the 23:42 slot of the evening before. With no occurrence that close,
    the update's own date and the header's run, or None when the header names no run.
    """
    updated = getattr(header, "updated_at", None)
    if not isinstance(updated, datetime):
        return None
    run = getattr(header, "run", None)
    run = run if run in RUNS else None
    times = _slot_times(slots)
    wall = updated.replace(tzinfo=None)
    best: tuple[timedelta, datetime, str] | None = None
    for candidate in ([run] if run else list(RUNS)):
        at_time = times.get(candidate)
        if at_time is None:
            continue
        for days in (-1, 0, 1):
            at = datetime.combine(wall.date() + timedelta(days=days), at_time)
            distance = abs(at - wall)
            if distance > BRIEFING_SLOT_WINDOW:
                continue
            if best is None or (distance, at) < (best[0], best[1]):
                best = (distance, at, candidate)
    if best is not None:
        return f"{best[1]:%Y-%m-%d} {best[2]}"
    return f"{wall:%Y-%m-%d} {run}" if run else None


def _slot_times(slots: Mapping[str, str] | None) -> dict[str, time]:
    """Slot times per run: the usable ones in ``slots``, :data:`DEFAULT_SLOTS` for the rest."""
    times = {run: parse_time_of_day(value) for run, value in DEFAULT_SLOTS.items()}
    for name, value in (slots or {}).items():
        try:
            for run, text in normalize_slots({name: value}).items():
                times[run] = parse_time_of_day(text)
        except ValueError:
            continue
    return times


def slot_key_date(key: str) -> date | None:
    """The date of a slot key, or None when ``key`` is not "YYYY-MM-DD AM|PM"."""
    match = _KEY_RE.fullmatch(key) if isinstance(key, str) else None
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Persisted state
# --------------------------------------------------------------------------

def _local_now() -> datetime:
    return datetime.now().astimezone()


class RunState:
    """runstate.json: ``{"2026-10-04 PM": {"shown_at": iso, "handled_at": iso, "how": "read",
    "announced_at": iso, "viewed_at": iso}}``.

    Every change re-reads the file first (the catch-up launch and the app are
    separate processes), prunes slots older than ``KEEP_DAYS`` and writes the
    result atomically. A failed write is logged and kept in memory for this
    run. The first show, the first answer, the first announcement and the
    first view of a slot are kept; later ones change nothing. A slot counts as
    handled (the catch-up leaves it alone) once it was answered, announced or
    viewed.
    """

    def __init__(self, path: Path, clock: Callable[[], datetime] = _local_now) -> None:
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, str]] = _load_entries(self.path)

    def get(self, slot_key: str) -> dict[str, str] | None:
        with self._lock:
            entry = self._entries.get(slot_key)
            return dict(entry) if entry is not None else None

    def is_handled(self, slot_key: str) -> bool:
        """Answered ("read", "dismissed", "done"), or its briefing was announced or viewed."""
        return bool(self.handled_how(slot_key))

    def handled_how(self, slot_key: str) -> str:
        """How ``slot_key`` was handled: its answer, else "viewed", else "announced"; "" when not."""
        entry = self.get(slot_key) or {}
        if entry.get("how", "") in ANSWERS:
            return entry["how"]
        if entry.get("viewed_at"):
            return "viewed"
        return "announced" if entry.get("announced_at") else ""

    def is_announced(self, slot_key: str) -> bool:
        return bool((self.get(slot_key) or {}).get("announced_at"))

    def is_viewed(self, slot_key: str) -> bool:
        return bool((self.get(slot_key) or {}).get("viewed_at"))

    def adopt_heard_answers(self) -> int:
        """Once, at the first start of a version that knows "viewed": a slot that an older version
        settled with "read" (Read now) or "done" (Done after reading) had its briefing heard, so it
        counts as viewed from then on (``viewed_at`` = its ``handled_at``) and is never announced
        as "ready to view". Returns how many slots changed. Never raises."""
        try:
            with self._lock:
                self._entries = _merge(_load_entries(self.path), self._entries)
                changed = 0
                for entry in self._entries.values():
                    if entry.get("how") in HEARD_ANSWERS and entry.get("handled_at") and not entry.get("viewed_at"):
                        entry["viewed_at"] = entry["handled_at"]
                        changed += 1
                if changed:
                    _write_entries(self.path, self._entries)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never stop the app
            logger.warning("Could not adopt the older answers (%s)", type(exc).__name__)
            return 0
        if changed:
            logger.info("%d slot(s) answered by an older version count as viewed", changed)
        return changed

    def mark_shown(self, slot_key: str) -> bool:
        """Remember when the prompt for ``slot_key`` was first shown; True when that was now."""
        _check_key(slot_key)
        stamp = self._stamp()
        return self._update(slot_key, lambda entry: _set_once(entry, {"shown_at": stamp}))

    def mark_handled(self, slot_key: str, how: str) -> bool:
        """Remember the first answer ("read", "dismissed" or "done"); True when it was recorded now."""
        _check_key(slot_key)
        if how not in ANSWERS:
            raise ValueError(f"unknown answer {how!r}")
        stamp = self._stamp()
        return self._update(slot_key, lambda entry: _set_once(entry, {"handled_at": stamp, "how": how}))

    def mark_announced(self, slot_key: str) -> bool:
        """Remember when the briefing of ``slot_key`` was first announced; True when that was now."""
        _check_key(slot_key)
        stamp = self._stamp()
        return self._update(slot_key, lambda entry: _set_once(entry, {"announced_at": stamp}))

    def mark_viewed(self, slot_key: str) -> bool:
        """Remember when the briefing of ``slot_key`` was first viewed; True when that was now."""
        _check_key(slot_key)
        stamp = self._stamp()
        return self._update(slot_key, lambda entry: _set_once(entry, {"viewed_at": stamp}))

    def record_announced(self, slot_key: str, how: str = "spoken") -> bool:
        """``mark_announced`` that never raises (a failure is logged); True when recorded now.

        ``how`` ("spoken", "text only") only goes into the log line."""
        return self._record(slot_key, self.mark_announced, f"announced ({how})")

    def record_viewed(self, slot_key: str) -> bool:
        """``mark_viewed`` that never raises (a failure is logged); True when recorded now."""
        return self._record(slot_key, self.mark_viewed, "viewed")

    # ---- the productivity bundle's plan offer (spec 4.3, 3.2 step 9) ----------------------------

    def plan_offered(self, slot_key: str) -> bool:
        """A plan offer was posted for ``slot_key`` (also by another process, or before a restart)."""
        return bool((self.get(slot_key) or {}).get("plan_offered_at"))

    def planned(self, slot_key: str) -> bool:
        """The day of ``slot_key`` was planned (a plan with at least one block; an evening "plan
        tomorrow" marks tomorrow's AM slot, R29)."""
        return bool((self.get(slot_key) or {}).get("planned_at"))

    def mark_plan_offered(self, slot_key: str) -> bool:
        """Remember when the plan offer for ``slot_key`` was first posted; True when that was now."""
        _check_key(slot_key)
        stamp = self._stamp()
        return self._update(slot_key, lambda entry: _set_once(entry, {"plan_offered_at": stamp}))

    def mark_planned(self, slot_key: str) -> bool:
        """Remember when the day of ``slot_key`` was first planned; True when that was now. Any
        valid slot key is accepted, tomorrow's too."""
        _check_key(slot_key)
        stamp = self._stamp()
        return self._update(slot_key, lambda entry: _set_once(entry, {"planned_at": stamp}))

    def record_plan_offered(self, slot_key: str) -> bool:
        """``mark_plan_offered`` that never raises (a failure is logged); True when recorded now."""
        return self._record(slot_key, self.mark_plan_offered, "followed by a plan offer")

    def record_planned(self, slot_key: str) -> bool:
        """``mark_planned`` that never raises (a failure is logged); True when recorded now."""
        return self._record(slot_key, self.mark_planned, "planned")

    def _record(self, slot_key: str, mark: Callable[[str], bool], what: str) -> bool:
        try:
            changed = mark(slot_key)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never break the screen
            logger.warning("Could not record that the %s briefing was %s (%s)",
                           str(slot_key)[:40], what.split(" ")[0], type(exc).__name__)
            return False
        if changed:
            logger.info("The %s briefing was %s", slot_key, what)
        return changed

    def prune(self, keep_days: int = KEEP_DAYS) -> int:
        """Forget slots dated more than ``keep_days`` days ago; returns how many."""
        with self._lock:
            self._entries = _merge(_load_entries(self.path), self._entries)
            removed = self._prune_locked(keep_days)
            if removed:
                _write_entries(self.path, self._entries)
        return removed

    def record_shown(self, now: datetime, slots: Mapping[str, str]) -> str | None:
        """Mark the slot ``now`` belongs to as shown; the slot key, or None. Never raises."""
        key = handled_slot_key(now, slots)
        if key is None:
            return None
        try:
            if self.mark_shown(key):
                logger.info("Prompt for the %s slot shown", key)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never break the prompt
            logger.warning("Could not record the prompt for %s (%s)", key, type(exc).__name__)
        return key

    def record_answer(self, now: datetime, slots: Mapping[str, str], how: str) -> str | None:
        """Mark the slot an answer at ``now`` settles; the slot key, or None. Never raises."""
        if how not in ANSWERS:
            logger.warning("Ignoring an unknown answer %r for the run state", how)
            return None
        key = handled_slot_key(now, slots)
        if key is None:
            logger.debug("No slot in the last %s to settle with %s", ANSWER_MAX_AGE, how)
            return None
        try:
            if self.mark_handled(key, how):
                logger.info("The %s slot is answered (%s)", key, how)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never break an answer
            logger.warning("Could not record the answer for %s (%s)", key, type(exc).__name__)
        return key

    def _stamp(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def _update(self, slot_key: str, change: Callable[[dict[str, str]], bool]) -> bool:
        with self._lock:
            self._entries = _merge(_load_entries(self.path), self._entries)
            entry = dict(self._entries.get(slot_key, {}))
            changed = change(entry)
            if changed:
                self._entries[slot_key] = entry
            removed = self._prune_locked(KEEP_DAYS)
            if changed or removed:
                _write_entries(self.path, self._entries)
        return changed

    def _prune_locked(self, keep_days: int) -> int:
        cutoff = self._clock().date() - timedelta(days=keep_days)
        old = [key for key in self._entries if (slot_key_date(key) or date.min) < cutoff]
        for key in old:
            del self._entries[key]
        if old:
            logger.debug("Forgot %d slot(s) older than %d days", len(old), keep_days)
        return len(old)


def _check_key(slot_key: str) -> None:
    if slot_key_date(slot_key) is None:
        raise ValueError(f"not a slot key like '2026-10-04 PM': {str(slot_key)[:40]!r}")


def _set_once(entry: dict[str, str], values: dict[str, str]) -> bool:
    """Set ``values`` unless the first of them is already set (the first event wins)."""
    first = next(iter(values))
    if entry.get(first):
        return False
    entry.update(values)
    return True


def _merge(on_disk: dict[str, dict[str, str]],
           in_memory: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """The file's entries plus what this process knows (fills fields the file lacks)."""
    merged = {key: dict(entry) for key, entry in on_disk.items()}
    for key, entry in in_memory.items():
        target = merged.setdefault(key, {})
        for name, value in entry.items():
            if value and not target.get(name):
                target[name] = value
        if target.get("how") not in ANSWERS:
            target.pop("how", None)
            target.pop("handled_at", None)
    return merged


def _load_entries(path: Path) -> dict[str, dict[str, str]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        logger.warning("Could not read %s (%s); starting with an empty run state",
                       path, type(exc).__name__)
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("%s is not valid JSON; starting with an empty run state", path)
        return {}
    if not isinstance(data, dict):
        logger.warning("%s is not a table of slots; starting with an empty run state", path)
        return {}
    entries: dict[str, dict[str, str]] = {}
    for key, value in data.items():
        entry = _clean_entry(key, value)
        if entry is not None:
            entries[key] = entry
    if len(entries) != len(data):
        logger.warning("Ignored %d malformed slot(s) in %s", len(data) - len(entries), path)
    return entries


def _clean_entry(key: object, value: object) -> dict[str, str] | None:
    if not isinstance(key, str) or slot_key_date(key) is None or not isinstance(value, dict):
        return None
    entry = {name: value[name] for name in ENTRY_FIELDS
             if isinstance(value.get(name), str) and value[name]}
    if entry.get("how") not in ANSWERS:
        entry.pop("how", None)
        entry.pop("handled_at", None)
    return entry


def _write_entries(path: Path, entries: dict[str, dict[str, str]]) -> bool:
    """Atomically replace ``path`` with ``entries`` as JSON; False (logged) on failure."""
    data = json.dumps(entries, indent=2, sort_keys=True) + "\n"
    part: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, part = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(part, path)
        part = None
        return True
    except OSError as exc:
        logger.warning("Could not save %s (%s); the run state is kept for this run only",
                       path, exc.strerror or type(exc).__name__)
        return False
    finally:
        if part is not None:
            try:
                os.unlink(part)
            except OSError:
                pass
