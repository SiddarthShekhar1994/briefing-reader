"""Typed shortcuts in the command bar that are not an Ask: "remember that ...", "forget that ...",
"what do you remember?" (the productivity bundle's memory), "plan my day" (its day plan) and "prep
me for my 2 PM" (its meeting prep; a later build adds follow-ups).

    route(text, flags) -> Route     ROUTE_NONE (a normal Ask) or the shortcut and its words

Every pattern is a case-insensitive full match of the whitespace-collapsed text, with an optional
leading "jarvis," / "please" and an optional trailing "." or "!". A shortcut whose flag is off is
ROUTE_NONE. "remember to ...", "remember when ...?" and every other question stay normal Asks; so
does any plan request with more than the plan's own words ("plan my day and email Ana"). The
caller lets "web:" win before routing (ask.research.forced_question).

PLAN: "plan my day", "plan today", "plan the rest of my day", "plan tomorrow", "plan my day
tomorrow", "plan my morning|afternoon|evening", with an optional "please" / "for me" and the
owner's own extra words after "," ":" or ";" ("plan my day, keep the afternoon free"): Route.part
is the part of the day (ask.dayplan.PART_*), Route.payload the extra words.

PREP: "prep me", "prepare me", "meeting prep", "prep me for <meeting>" / "prepare me for ..." / "...
on ..." (any words after "for" / "on": a time, "my next meeting", title words), "brief me on my next
meeting" / "brief me for the call" / "brief me on my 2 PM (meeting)": Route.payload is the target
words ("" for the next meeting). They only pick the meeting (ask.prep.resolve_meeting) and are never
sent to Claude. "prepare the report" and "brief me on the news" stay Asks; a prep route never falls
through to an Ask.

Pure; nothing is logged (Route's repr names its kind only).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ROUTE_NONE, ROUTE_MEMORY_SAVE, ROUTE_MEMORY_FORGET, ROUTE_MEMORY_SHOW, ROUTE_PLAN, ROUTE_PREP, ROUTE_FOLLOWUPS = (
    "none", "memory_save", "memory_forget", "memory_show", "plan", "prep", "followups")
MEMORY_ROUTES = frozenset({ROUTE_MEMORY_SAVE, ROUTE_MEMORY_FORGET, ROUTE_MEMORY_SHOW})
# The parts of a day a plan route names (the same words as ask.dayplan.PART_*).
PART_DAY, PART_MORNING, PART_AFTERNOON, PART_EVENING, PART_TOMORROW = (
    "day", "morning", "afternoon", "evening", "tomorrow")


@dataclass(frozen=True)
class RouteFlags:
    memory: bool = False
    plan: bool = False
    prep: bool = False
    followups: bool = False


@dataclass(frozen=True)
class Route:
    kind: str
    payload: str = ""        # note / forget words / plan extra words / prep target words
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
_PLAN_RE = _pattern(r"plan\s+(?P<what>my\s+day\s+tomorrow|my\s+day|today|tomorrow|the\s+rest\s+of\s+"
                    r"(?:my\s+day|today)|my\s+morning|my\s+afternoon|my\s+evening)(?:\s+(?:please|for\s+me))?"
                    r"(?:\s*[,:;]\s*(?P<extra>\S.*))?")
_PREP_RE = _pattern(r"(?:prep|prepare)\s+me(?:\s+(?:for|on)\s+(?P<target>\S.*))?"
                    r"|brief\s+me\s+(?:for|on)\s+(?P<target2>(?:my|the)\s+(?:next\s+)?(?:meeting|call)"
                    r"|my\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?(?:\s+(?:meeting|call))?)"
                    r"|meeting\s+prep")


def _plan_part(what: str) -> str:
    words = " ".join(what.casefold().split())
    if words.endswith("tomorrow"):
        return PART_TOMORROW
    for part in (PART_MORNING, PART_AFTERNOON, PART_EVENING):
        if words == f"my {part}":
            return part
    return PART_DAY


def route(text: str, flags: RouteFlags) -> Route:
    """The shortcut ``text`` asks for (NO_ROUTE: a normal Ask)."""
    text = " ".join(str(text or "").split())
    if not text:
        return NO_ROUTE
    if flags.memory:
        found = _memory_route(text)
        if found is not NO_ROUTE:
            return found
    if flags.plan:
        match = _PLAN_RE.fullmatch(text)
        if match:
            extra = (match.group("extra") or "").strip()
            return Route(ROUTE_PLAN, extra, _plan_part(match.group("what")))
    if flags.prep:
        match = _PREP_RE.fullmatch(text)
        if match:
            target = (match.group("target") or match.group("target2") or "").strip()
            return Route(ROUTE_PREP, target.rstrip(".!?").strip())
    return NO_ROUTE


def _memory_route(text: str) -> Route:
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
