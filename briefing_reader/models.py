"""Plain data types shared between modules.

Everything here is immutable and Qt-free so it can cross thread boundaries and
be used in unit tests without a display.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# Notion side (produced by notion_client)
# --------------------------------------------------------------------------

# FlatLine.kind values
HEADING = "heading"
PARAGRAPH = "paragraph"
BULLETED = "bulleted"
NUMBERED = "numbered"
TO_DO = "to_do"
QUOTE = "quote"
CALLOUT = "callout"
TOGGLE = "toggle"
CODE = "code"
TABLE_ROW = "table_row"
DIVIDER = "divider"
OTHER = "other"

LINE_KINDS = (
    HEADING, PARAGRAPH, BULLETED, NUMBERED, TO_DO, QUOTE,
    CALLOUT, TOGGLE, CODE, TABLE_ROW, DIVIDER, OTHER,
)


@dataclass(frozen=True)
class FlatLine:
    """One line of a flattened Notion page.

    ``text`` is the block's plain text with any inline markdown the author
    typed left untouched; text_prep strips it later. ``depth`` is the nesting
    level (0 = top level). ``level`` is 1-3 for headings, ``number`` is the
    1-based position of a numbered item among its consecutive siblings, and
    ``checked`` is set for to-dos only.
    """

    kind: str
    text: str
    depth: int = 0
    level: int | None = None
    number: int | None = None
    checked: bool | None = None
    block_id: str = ""


@dataclass(frozen=True)
class BriefingHeader:
    """The "Updated: ..." / "Run: AM" lines at the top of the page."""

    updated_raw: str | None = None      # text after "Updated:", e.g. "2026-10-04 10:04 PDT" or "never"
    updated_at: datetime | None = None  # timezone-aware, in local time; None if missing or unparseable
    run: str | None = None              # "AM", "PM" or None


@dataclass(frozen=True)
class Briefing:
    """A fetched page: parsed header plus the body lines (header lines removed)."""

    page_id: str
    header: BriefingHeader
    lines: tuple[FlatLine, ...]
    fetched_at: datetime                # timezone-aware local time

    @property
    def content_key(self) -> str:
        """Stable hash of the content, used to notice when the page changed."""
        h = hashlib.sha1()
        h.update(repr((self.header.updated_raw, self.header.run)).encode("utf-8"))
        for line in self.lines:
            h.update(repr((line.kind, line.text, line.depth, line.level,
                           line.number, line.checked)).encode("utf-8"))
        return h.hexdigest()


@dataclass(frozen=True)
class Freshness:
    """Whether a briefing is the one we were waiting for."""

    fresh: bool
    updated_today: bool
    run_matches: bool                   # True when no run was expected


# --------------------------------------------------------------------------
# Speech script (produced by text_prep)
# --------------------------------------------------------------------------

# ScriptItem.kind values
ITEM_NOTE = "note"              # stale warning
ITEM_INTRO = "intro"            # "Here's your AM briefing, ..."
ITEM_HEADING = "heading"        # section announcement
ITEM_SUBHEADING = "subheading"  # heading nested inside a section
ITEM_ENTRY = "item"             # list item / to-do / table row
ITEM_TEXT = "text"              # paragraph, quote, callout, toggle, code, other
ITEM_PAUSE = "pause"            # divider: silence, nothing spoken
ITEM_OUTRO = "outro"            # "That's the end of your briefing."

ITEM_KINDS = (
    ITEM_NOTE, ITEM_INTRO, ITEM_HEADING, ITEM_SUBHEADING,
    ITEM_ENTRY, ITEM_TEXT, ITEM_PAUSE, ITEM_OUTRO,
)


@dataclass(frozen=True)
class ScriptItem:
    """One displayable line and what to say for it.

    ``spoken`` is "" when nothing should be said (pauses, code). ``display``
    is the text shown in the reading panel ("" for a pause), without
    indentation; the UI indents by ``depth``.
    """

    kind: str
    spoken: str
    display: str
    depth: int = 0


@dataclass(frozen=True)
class Section:
    """A unit for "Skip section": a heading and everything under it.

    ``key`` is unique within a Script ("intro", "s1", "s2", ..., "outro").
    ``title`` is the display title ("" for intro/outro). ``ignored`` sections
    are only read after the user clicks "Read everything".
    """

    key: str
    title: str
    items: tuple[ScriptItem, ...]
    ignored: bool = False

    @property
    def has_speech(self) -> bool:
        return any(item.spoken for item in self.items)


@dataclass(frozen=True)
class Script:
    """The whole briefing, prepared for listening and display."""

    sections: tuple[Section, ...]
    run_label: str | None = None        # "AM", "PM" or None
    updated_label: str = ""             # e.g. "Updated today at 10:04 AM" (for the UI)
    stale: bool = False                 # True when a stale note was prepended

    def section_indices(self, include_ignored: bool = False) -> list[int]:
        """Indices of the sections to play, in document order."""
        return [i for i, s in enumerate(self.sections)
                if include_ignored or not s.ignored]

    @property
    def has_ignored(self) -> bool:
        return any(s.ignored for s in self.sections)


# --------------------------------------------------------------------------
# Audio (produced by tts, consumed by player)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AudioSegment:
    """One audio file covering consecutive items of a section.

    ``marks`` are (start_ms, item_index) pairs sorted by start_ms, where
    item_index indexes ``Section.items``. ``pause_after_ms`` is silence the
    player inserts after this segment (dividers). ``words``: (start_ms,
    end_ms, word) from edge-tts's word boundaries; ``envelope``: the Windows
    voice's level 0..1 at voice_fx.ENVELOPE_HZ (the orb's voice spikes and
    Home's word-by-word caption; neither is ever logged).
    """

    path: Path
    duration_ms: int
    marks: tuple[tuple[int, int], ...]
    pause_after_ms: int = 0
    words: tuple[tuple[int, int, str], ...] = field(default=(), repr=False)
    envelope: tuple[float, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class SectionAudio:
    """All audio for one section. ``segments`` may be empty (nothing to say)."""

    section_key: str
    segments: tuple[AudioSegment, ...] = field(default_factory=tuple)
    engine: str = "edge"                # "edge" or "sapi"


# --------------------------------------------------------------------------
# Launch kinds (how a start or an activation asked for Jarvis)
# --------------------------------------------------------------------------

LAUNCH_OPEN = "open"            # the assistant screen with a greeting; nothing is read until Play
LAUNCH_READ = "read"            # the assistant screen on its BRIEFING tab, reading at once (--read)
LAUNCH_SCHEDULED = "scheduled"  # a scheduled run or the catch-up: wait for the run's briefing
LAUNCH_KINDS = (LAUNCH_OPEN, LAUNCH_READ, LAUNCH_SCHEDULED)
