"""The productivity bundle's offers, as pure rules (spec 4): when a plan offer or a prep offer is
due, when its one sentence may be said, and which reasons are held (said later) or text only.

An offer never acts: it is one JARVIS entry with one link and at most one locally built sentence
(times and counts only). Nothing is read from mail and nothing runs until the owner clicks.

    QuietFacts                         what the app knows at a tick (visible / minimized, locked, the
                                       voice, the briefing playing, a countdown, a run or Jarvis speaking)
    in_quiet_hours(now, from, until)   22:00-08:00 by default, wrapping midnight
    in_meeting(day, now, own)          a timed event under way with another guest, not declined
    may_speak(kind, facts, ...)        "" (say it now) or the first reason word, in the spec's order
    speech_disposition(kind, reason)   "speak" | "held" (said at a later quiet tick) | "text_only" (never)
    briefing_heard(...)                the AM briefing counts as heard: FINISHED, viewed then JARVIS, or
                                       20 minutes after the announcement with no playback (R26)
    caps_ok(caps, reserve)             the cached CapCheck allows a bundle run (R13)
    plan_offer_due(...)                "" or why not (R36: the transient ones are retried)
    prep_offer_due(alias, event, ...)  "" or why not (lead window, guests, skip words, ignored series)
    prep_offer_group(day, first, ...)  back-to-back meetings offered together with the first (R51)
    offer_key(alias, event)            one offer per event occurrence (memory only)

Every clock here is a naive wall time in the personal calendar's zone (the controller converts);
an aware ``now`` is compared with aware event times as it is. Qt-free; nothing is logged.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from ..gcal import EventBrief
from ..prepseries import IGNORED_LIMIT, series_key
from .prep import other_guests

KIND_PLAN, KIND_PREP = "plan", "prep"
TEXT_ONLY_REASONS = frozenset({"muted", "quiet_hours", "stale"})          # + "meeting" for a prep offer (R37)
HELD_REASONS = frozenset({"hidden", "locked", "minimized", "not_heard", "playing", "countdown", "busy", "spacing"})
TRANSIENT_PLAN_REASONS = frozenset({"early", "not_ready", "caps", "no_day"})   # R36
FINAL_PREP_REASONS = frozenset({"late", "all_day", "long", "guests", "declined", "skip_word", "ignored"})
HEARD_AFTER_MINUTES = 20                                                       # R26 (c)
SPACING_MINUTES = 5                 # between two spoken offers
GROUP_SPACING_MINUTES = 10          # between two prep groups
DAY_READ_FRESH_MINUTES = 10         # a plan offer needs a day read this recent
STALE_SCHEDULE_MINUTES = 30         # a minimized prep offer from an older schedule is re-checked (or stale)
PREP_MIN_BEFORE_START = 3           # no prep offer this close to the start
PREP_MAX_HOURS = 4                  # no prep offer for a longer event
PLAN_SPEECH_WINDOW = timedelta(hours=2)   # a held plan offer may still be said this long after posting
GROUP_MAX = 3
SPEAK, HELD, TEXT_ONLY = "speak", "held", "text_only"


@dataclass(frozen=True)
class QuietFacts:
    """What the app knows at a tick (memory only, no I/O)."""

    activity: str = "visible"     # "visible" | "minimized" | "off"
    locked: bool = False
    speak_ok: bool = True         # [assistant] speak and speak_replies on, the voice not muted
    playing: bool = False         # the briefing PLAYING or WAITING
    countdown: bool = False       # an undo countdown or a card job runs
    busy: bool = False            # an Ask or mode run, or Jarvis is speaking


def clock_time(value: Any) -> time:
    """"13:30" (or a time) -> time; a bad value is midnight."""
    if isinstance(value, time):
        return value
    try:
        hours, _, minutes = str(value).strip().partition(":")
        return time(int(hours), int(minutes or 0))
    except (TypeError, ValueError):
        return time(0, 0)


def _time_of(moment: datetime | time) -> time:
    return moment.time() if isinstance(moment, datetime) else moment


def in_quiet_hours(now: datetime | time, quiet_from: Any, quiet_until: Any) -> bool:
    """``quiet_from`` <= now < ``quiet_until``, wrapping midnight (22:00-08:00 covers 23:00 and 03:00).
    Equal bounds mean no quiet hours."""
    start, end = clock_time(quiet_from), clock_time(quiet_until)
    moment = _time_of(now)
    if start == end:
        return False
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


def _comparable(moment: datetime, like: datetime) -> datetime:
    """``moment`` made comparable with ``like``: an aware event time against a naive ``now`` is taken
    as the same wall time (the controller hands wall times of one zone)."""
    if (moment.tzinfo is None) == (like.tzinfo is None):
        return moment
    if like.tzinfo is None:
        return moment.replace(tzinfo=None)
    return moment.replace(tzinfo=like.tzinfo)


def event_span(event: EventBrief, like: datetime) -> tuple[datetime, datetime] | None:
    """A timed event's (start, end) comparable with ``like``; None for an all-day or untimed event."""
    if event.start is None or event.all_day:
        return None
    start = _comparable(event.start, like)
    end = _comparable(event.end, like) if event.end is not None else start
    return start, max(start, end)


def in_meeting(day: Mapping[str, Sequence[EventBrief]], now: datetime, own: Collection[str]) -> bool:
    """A timed event is under way (start <= now < end) with at least one other guest, not declined."""
    for briefs in day.values():
        for event in briefs or ():
            span = event_span(event, now)
            if span is None or event.self_response == "declined":
                continue
            if span[0] <= now < span[1] and other_guests(event, own) > 0:
                return True
    return False


def may_speak(kind: str, facts: QuietFacts, *, now: datetime, settings: Any, meeting: bool,
              last_spoken: datetime | None, heard: bool = True, stale: bool = False) -> str:
    """"" when the offer's sentence may be said now, else the first reason word in the spec's order
    (4.2): muted, quiet_hours, stale (prep), meeting, hidden (the reading screen is not up: never
    said over the prompt or a hidden wait), locked, minimized, not_heard (plan), playing, countdown,
    busy, spacing."""
    if not facts.speak_ok:
        return "muted"
    if in_quiet_hours(now, getattr(settings, "quiet_from", "22:00"), getattr(settings, "quiet_until", "08:00")):
        return "quiet_hours"
    if kind == KIND_PREP and stale:
        return "stale"
    if meeting:
        return "meeting"
    if facts.activity not in ("visible", "minimized"):
        return "hidden"
    if facts.locked:
        return "locked"
    if facts.activity == "minimized" and (kind == KIND_PLAN or not getattr(settings, "prep_speak_minimized", True)):
        return "minimized"
    if kind == KIND_PLAN and not heard:
        return "not_heard"
    if facts.playing:
        return "playing"
    if facts.countdown:
        return "countdown"
    if facts.busy:
        return "busy"
    if last_spoken is not None and now - _comparable(last_spoken, now) < timedelta(minutes=SPACING_MINUTES):
        return "spacing"
    return ""


def speech_disposition(kind: str, reason: str) -> str:
    """What becomes of the sentence: SPEAK (now), TEXT_ONLY (never retried: muted, quiet hours, a
    stale minimized schedule, a prep offer during a meeting), HELD (retried at later ticks; R37)."""
    if not reason:
        return SPEAK
    if reason in TEXT_ONLY_REASONS or (kind == KIND_PREP and reason == "meeting"):
        return TEXT_ONLY
    return HELD


def briefing_heard(*, finished: bool, viewed_then_jarvis: bool, announced_at: datetime | None,
                   playback_started: bool, now: datetime) -> bool:
    """R26: the AM briefing counts as heard once (a) its playback reached FINISHED, (b) the owner
    viewed it and afterwards made JARVIS current, or (c) 20 minutes passed since the announcement
    with no playback started."""
    if finished or viewed_then_jarvis:
        return True
    if announced_at is None or playback_started:
        return False
    return now - _comparable(announced_at, now) >= timedelta(minutes=HEARD_AFTER_MINUTES)


def caps_ok(caps: Any, reserve: int) -> bool:
    """The cached CapCheck allows a bundle run: allowed, a run that still leaves ``reserve`` Ask runs
    this hour, and a bundle run left today (R13). None (no check yet) is not ok."""
    if caps is None or not getattr(caps, "allowed", False):
        return False
    left_hour = int(getattr(caps, "left_hour", 0))
    return left_hour - 1 >= max(0, int(reserve)) and int(getattr(caps, "left_bundle_day", 0)) >= 1


def plan_offer_due(*, key: str, now: datetime, settings: Any, offered: bool, planned: bool, ready: bool,
                   caps_ok: bool, day_read_at: datetime | None, day_complete: bool, free_minutes: int,
                   needs_time: bool) -> str:
    """"" when the plan offer may be posted, else the reason word (4.3): off, not_am, late, early,
    offered, planned, not_ready, caps, no_day, busy_day, nothing. ``now`` and ``day_read_at`` are
    wall times in the personal zone; ``day_complete`` says whether every calendar was read (the
    controller keeps busy_day and nothing transient after a partial read, R36)."""
    if not getattr(settings, "plan_offer", True):
        return "off"
    if key != f"{now:%Y-%m-%d} AM":
        return "not_am"
    if now.time() >= clock_time(getattr(settings, "plan_offer_until", "13:30")):
        return "late"
    if now.time() < clock_time(getattr(settings, "day_start", "08:00")):
        return "early"
    if offered:
        return "offered"
    if planned:
        return "planned"
    if not ready:
        return "not_ready"
    if not caps_ok:
        return "caps"
    if day_read_at is None or now - _comparable(day_read_at, now) > timedelta(minutes=DAY_READ_FRESH_MINUTES):
        return "no_day"
    if free_minutes < int(getattr(settings, "plan_min_free_minutes", 60)):
        return "busy_day"
    if not needs_time:
        return "nothing"
    return ""


def plan_reason_transient(reason: str, day_complete: bool) -> bool:
    """R36: a transient reason keeps the candidate alive; busy_day and nothing are final only after a
    complete day read."""
    if reason in TRANSIENT_PLAN_REASONS:
        return True
    return reason in ("busy_day", "nothing") and not day_complete


def _has_skip_word(title: str, words: Collection[str]) -> bool:
    folded = " ".join(str(title or "").casefold().split())
    for word in words:
        word = " ".join(str(word).casefold().split())
        if word and re.search(rf"(?<![^\W_]){re.escape(word)}(?![^\W_])", folded):
            return True
    return False


def ignored_count(ignored: Callable[[str], int] | int, alias: str, event: EventBrief) -> int:
    """How often the event's recurring series was ignored (0 for a single event, or without a
    series store); ``ignored`` is the count itself or a function of the series key."""
    if not callable(ignored):
        return int(ignored or 0)
    if not getattr(event, "series_id", ""):
        return 0
    try:
        return int(ignored(series_key(alias, event.calendar_id, event.series_id)) or 0)
    except Exception:  # noqa: BLE001 - a broken store never stops an offer
        return 0


def prep_offer_due(alias: str, event: EventBrief, *, now: datetime, settings: Any, own: Collection[str],
                   offered: Collection[str], offered_today: int, last_group_at: datetime | None, ready: bool,
                   caps_ok: bool, ignored: Callable[[str], int] | int = 0, in_lead_window: bool = True) -> str:
    """"" when a brief may be offered for ``event`` now, else the reason word (4.4): off, offered,
    not_ready, caps, all_day, long, early, late, guests, declined, skip_word, ignored (3 times in a
    row, R38), per_day, spacing. ``in_lead_window=False`` (a back-to-back member, R51) skips the
    lead window. ``now`` wall or aware like the event's times."""
    if not getattr(settings, "prep_offers", True):
        return "off"
    if offer_key(alias, event) in offered:
        return "offered"
    if not ready:
        return "not_ready"
    if not caps_ok:
        return "caps"
    span = event_span(event, now)
    if span is None:
        return "all_day"
    start, end = span
    if end - start > timedelta(hours=PREP_MAX_HOURS):
        return "long"
    if in_lead_window:
        if now < start - timedelta(minutes=int(getattr(settings, "prep_lead_minutes", 15))):
            return "early"
        if now > start - timedelta(minutes=PREP_MIN_BEFORE_START):
            return "late"
    elif now > start - timedelta(minutes=PREP_MIN_BEFORE_START):
        return "late"
    guests = other_guests(event, own)
    if guests < 1 or guests > int(getattr(settings, "prep_max_offer_guests", 8)):
        return "guests"
    if event.self_response == "declined":
        return "declined"
    if _has_skip_word(event.title, getattr(settings, "prep_skip_words", ())):
        return "skip_word"
    if ignored_count(ignored, alias, event) >= IGNORED_LIMIT:
        return "ignored"
    if offered_today >= int(getattr(settings, "prep_offers_per_day", 8)):
        return "per_day"
    if last_group_at is not None and now - _comparable(last_group_at, now) < timedelta(minutes=GROUP_SPACING_MINUTES):
        return "spacing"
    return ""


def prep_offer_group(day: Mapping[str, Sequence[EventBrief]], first: tuple[str, EventBrief], *, now: datetime,
                     settings: Any, own: Collection[str], offered: Collection[str],
                     ignored: Callable[[str], int] | int = 0,
                     offered_today: int = 0) -> tuple[tuple[str, EventBrief], ...]:
    """``first`` plus the qualifying meetings that start during it or less than ``prep_lead_minutes``
    after it ends, chained the same way, at most GROUP_MAX in all (R51). Each member passes
    prep_offer_due without the lead window; spacing is the group's (checked for ``first`` by the
    caller); every member counts for ``prep_offers_per_day``, so the group never holds more than
    the offers left today after ``offered_today`` (``first`` was checked against it: at least 1)."""
    lead = timedelta(minutes=int(getattr(settings, "prep_lead_minutes", 15)))
    room = int(getattr(settings, "prep_offers_per_day", 8)) - max(0, int(offered_today))
    most = max(1, min(GROUP_MAX, room))
    members: list[tuple[str, EventBrief]] = [first]
    candidates: list[tuple[datetime, str, EventBrief]] = []
    for alias, briefs in day.items():
        for event in briefs or ():
            span = event_span(event, now)
            if span is None or (alias == first[0] and event.event_id == first[1].event_id
                                and event.calendar_id == first[1].calendar_id):
                continue
            candidates.append((span[0], alias, event))
    candidates.sort(key=lambda item: (item[0], item[1]))
    while len(members) < most:
        last_alias, last_event = members[-1]
        last_span = event_span(last_event, now)
        if last_span is None:
            break
        found = None
        for start, alias, event in candidates:
            if any(alias == m_alias and event.event_id == m_event.event_id for m_alias, m_event in members):
                continue
            if not (last_span[0] <= start < last_span[1] + lead):
                continue
            if prep_offer_due(alias, event, now=now, settings=settings, own=own, offered=offered, offered_today=0,
                              last_group_at=None, ready=True, caps_ok=True, ignored=ignored, in_lead_window=False):
                continue
            found = (alias, event)
            break
        if found is None:
            break
        members.append(found)
    return tuple(members)


def offer_key(alias: str, event: EventBrief) -> str:
    """One offer per event occurrence: alias|calendar|event id|start (memory only, never stored)."""
    start = event.start.isoformat() if event.start is not None else ""
    return f"{alias}|{event.calendar_id}|{event.event_id}|{start}"


def prep_series_key(alias: str, event: EventBrief) -> str:
    """The prep_series.json key of a recurring event ("" for a single one)."""
    series = getattr(event, "series_id", "")
    return series_key(alias, event.calendar_id, series) if series else ""
