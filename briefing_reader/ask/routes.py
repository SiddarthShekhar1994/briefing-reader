"""Typed shortcuts in the command bar that are not an Ask: "remember that ...", "forget that ...",
"what do you remember?" (the productivity bundle's memory; the later builds add plan, prep and
follow-ups).

    route(text, flags) -> Route     ROUTE_NONE (a normal Ask) or the shortcut and its words

Every pattern is a case-insensitive full match of the whitespace-collapsed text, with an optional
leading "jarvis," / "please" and an optional trailing "." or "!". A shortcut whose flag is off is
ROUTE_NONE. "remember to ...", "remember when ...?" and every other question stay normal Asks; the
caller lets "web:" win before routing (ask.research.forced_question).

Pure; nothing is logged (Route's repr names its kind only).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ROUTE_NONE, ROUTE_MEMORY_SAVE, ROUTE_MEMORY_FORGET, ROUTE_MEMORY_SHOW, ROUTE_PLAN, ROUTE_PREP, ROUTE_FOLLOWUPS = (
    "none", "memory_save", "memory_forget", "memory_show", "plan", "prep", "followups")
MEMORY_ROUTES = frozenset({ROUTE_MEMORY_SAVE, ROUTE_MEMORY_FORGET, ROUTE_MEMORY_SHOW})


@dataclass(frozen=True)
class RouteFlags:
    memory: bool = False
    plan: bool = False
    prep: bool = False
    followups: bool = False


@dataclass(frozen=True)
class Route:
    kind: str
    payload: str = ""        # note / forget words / plan extra words / prep target
    part: str = ""           # plan: dayplan.PART_*

    def __repr__(self) -> str:   # never the owner's words
        return f"Route({self.kind})"


NO_ROUTE = Route(ROUTE_NONE)
_PREFIX = r"(?:jarvis[,:]?\s+)?(?:please\s+)?"
_SUFFIX = r"[.!]?"
_NOT_A_NOTE = r"(?:to|when|if|whether|what|who|how|why|where|which)\b"


def _pattern(core: str) -> re.Pattern[str]:
    return re.compile(_PREFIX + "(?:" + core + ")" + _SUFFIX, re.IGNORECASE | re.DOTALL)


_MEMORY_SAVE_RE = _pattern(r"remember(?:\s+that\s+|\s*:\s*|,\s*|\s+(?!" + _NOT_A_NOTE + r"))(?P<note>\S.*)")
_MEMORY_FORGET_RE = _pattern(r"forget(?:\s+that\s+|\s+about\s+|\s*:\s*)(?P<words>\S.*)")
_MEMORY_SHOW_RE = _pattern(r"what\s+do\s+you\s+remember\??|show\s+(?:me\s+)?(?:my\s+)?memory|open\s+memory")


def route(text: str, flags: RouteFlags) -> Route:
    """The shortcut ``text`` asks for (NO_ROUTE: a normal Ask)."""
    text = " ".join(str(text or "").split())
    if not text or not flags.memory:
        return NO_ROUTE
    if _MEMORY_SHOW_RE.fullmatch(text):
        return Route(ROUTE_MEMORY_SHOW)
    match = _MEMORY_SAVE_RE.fullmatch(text)
    if match:
        note = match.group("note").strip()
        # A question is never a note ("remember that meeting we had?"); the "that" form always wins,
        # so a note never starts with "that " (and "remember that" alone is no note).
        if note.endswith("?") or note.casefold() == "that":
            return NO_ROUTE
        return Route(ROUTE_MEMORY_SAVE, note)
    match = _MEMORY_FORGET_RE.fullmatch(text)
    if match:
        return Route(ROUTE_MEMORY_FORGET, match.group("words").strip())
    return NO_ROUTE
