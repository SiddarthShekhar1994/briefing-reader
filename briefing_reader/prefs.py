"""Jarvis's own small preferences: ``%LOCALAPPDATA%\\briefing-reader\\assistant.json``.

``{"muted": false, "recent_greetings": ["m3", "n1"]}``: whether the owner muted
Jarvis's voice (the header's speaker button) and the ids of the last few
greetings, so the next one differs; ``"answers_adopted": true`` once the
answers of an older version in runstate.json were taken as viewed (once, at
the first start after the update); ``"live_window": {"x", "y", "w", "h"}`` and
``"live_popped_out"``: where the LIVE pop-out window was and whether it was open
when Jarvis quit (written once the pop-out has been used); ``"prod_hint_shown":
true`` once the productivity bundle's one-time "minimize instead of Done" entry
was shown (R34); ``"followups_off": true`` while the follow-up list is turned off on
this PC (its section's "Turn off on this PC"). Ids, numbers and booleans only: never any
text (the LIVE view's steps are never saved).

The file is written atomically (temporary file + ``os.replace``); a missing,
corrupt or unwritable file counts as the defaults and never stops the app
(a failed write is logged and kept in memory for this run).

Qt-free; standard library only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

PREFS_FILE = "assistant.json"
RECENT_GREETINGS_MAX = 5
_GREETING_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,15}")


class AssistantPrefs:
    """``muted`` and ``recent_greetings`` (oldest first, at most 5 ids), kept in ``path``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        (self._muted, self._recent, self._adopted, self._live_window, self._live_popped,
         self._prod_hint, self._followups_off) = _load(self.path)

    @property
    def muted(self) -> bool:
        with self._lock:
            return self._muted

    @property
    def recent_greetings(self) -> list[str]:
        with self._lock:
            return list(self._recent)

    def set_muted(self, muted: bool) -> bool:
        """Remember the mute switch; False when the file could not be written (kept in memory)."""
        with self._lock:
            self._muted = bool(muted)
            return self._save_locked()

    @property
    def answers_adopted(self) -> bool:
        """The older version's answers in runstate.json were taken as viewed already."""
        with self._lock:
            return self._adopted

    def set_answers_adopted(self) -> bool:
        """Remember that the older answers were taken as viewed (it is done once)."""
        with self._lock:
            self._adopted = True
            return self._save_locked()

    @property
    def live_window(self) -> tuple[int, int, int, int] | None:
        """The LIVE pop-out's last (x, y, width, height), or None."""
        with self._lock:
            return self._live_window

    @property
    def live_popped_out(self) -> bool:
        """The LIVE pop-out was open when Jarvis last quit (it comes back at the next start)."""
        with self._lock:
            return self._live_popped

    def set_live_window(self, rect: tuple[int, int, int, int] | None, popped_out: bool) -> bool:
        """Remember the LIVE pop-out's place and size (ints) and whether it is open."""
        with self._lock:
            self._live_window = _live_rect(rect)
            self._live_popped = bool(popped_out)
            return self._save_locked()

    @property
    def prod_hint_shown(self) -> bool:
        """The productivity bundle's one-time "minimize instead of Done" entry was shown (R34)."""
        with self._lock:
            return self._prod_hint

    def set_prod_hint_shown(self) -> bool:
        """Remember that the hint was shown (it is shown once)."""
        with self._lock:
            self._prod_hint = True
            return self._save_locked()

    @property
    def followups_off(self) -> bool:
        """The follow-up list is turned off on this PC (nothing is scanned; the section says so)."""
        with self._lock:
            return self._followups_off

    def set_followups_off(self, off: bool) -> bool:
        """Turn the follow-up list off (True) or on again on this PC."""
        with self._lock:
            self._followups_off = bool(off)
            return self._save_locked()

    def remember_greeting(self, greeting_id: str) -> bool:
        """Add ``greeting_id`` as the most recent greeting (the oldest beyond 5 is forgotten)."""
        if not isinstance(greeting_id, str) or not _GREETING_ID_RE.fullmatch(greeting_id):
            logger.debug("Ignoring a malformed greeting id")
            return False
        with self._lock:
            recent = [item for item in self._recent if item != greeting_id] + [greeting_id]
            self._recent = recent[-RECENT_GREETINGS_MAX:]
            return self._save_locked()

    def _save_locked(self) -> bool:
        values: dict[str, object] = {"muted": self._muted, "recent_greetings": self._recent}
        if self._adopted:
            values["answers_adopted"] = True
        if self._live_window is not None:
            x, y, width, height = self._live_window
            values["live_window"] = {"x": x, "y": y, "w": width, "h": height}
        if self._live_window is not None or self._live_popped:
            values["live_popped_out"] = self._live_popped
        if self._prod_hint:
            values["prod_hint_shown"] = True
        if self._followups_off:
            values["followups_off"] = True
        return _write_atomic(self.path, json.dumps(values, indent=2) + "\n")


_LIVE_RECT_LIMIT = 100_000   # any sane screen coordinate or size is far below this


def _live_rect(value: object) -> tuple[int, int, int, int] | None:
    """(x, y, w, h) as ints from a tuple or a {"x", "y", "w", "h"} table; None when malformed."""
    if isinstance(value, dict):
        value = tuple(value.get(key) for key in ("x", "y", "w", "h"))
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        return None
    if not all(isinstance(item, int) and not isinstance(item, bool) and abs(item) < _LIVE_RECT_LIMIT
               for item in value):
        return None
    x, y, width, height = value
    return (x, y, width, height) if width > 0 and height > 0 else None



_Loaded = tuple[bool, list[str], bool, tuple[int, int, int, int] | None, bool, bool, bool]
_DEFAULTS: _Loaded = (False, [], False, None, False, False, False)


def _load(path: Path) -> _Loaded:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return _DEFAULTS
    except (OSError, UnicodeError) as exc:
        logger.warning("Could not read %s (%s); using the defaults", path, type(exc).__name__)
        return _DEFAULTS
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("%s is not valid JSON; using the defaults", path)
        return _DEFAULTS
    if not isinstance(data, dict):
        logger.warning("%s is not a table of settings; using the defaults", path)
        return _DEFAULTS
    muted = data.get("muted") is True
    raw = data.get("recent_greetings")
    recent = [item for item in raw if isinstance(item, str) and _GREETING_ID_RE.fullmatch(item)] \
        if isinstance(raw, list) else []
    return (muted, recent[-RECENT_GREETINGS_MAX:], data.get("answers_adopted") is True,
            _live_rect(window) if isinstance(window := data.get("live_window"), dict) else None,
            data.get("live_popped_out") is True, data.get("prod_hint_shown") is True,
            data.get("followups_off") is True)


def _write_atomic(path: Path, data: str) -> bool:
    part: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, part = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(part, path)
        part = None
        return True
    except OSError as exc:
        logger.warning("Could not save %s (%s); kept for this run only", path,
                       exc.strerror or type(exc).__name__)
        return False
    finally:
        if part is not None:
            try:
                os.unlink(part)
            except OSError:
                pass
