"""The planner's lines -> NEEDS YOUR OK cards (source="ask"), each checked against what Jarvis supplied.

    to_cards(lines, index, ...)   Cards: one card per line (at most ``max_cards``), and
                                  Cards.report: what became of each line, in order (LineReport:
                                  a card, refused with its reason, a repeat, over the cap), for the
                                  LIVE view only

For each line, in order (the first problem wins; the card then shows it and offers nothing to
approve):

1. actions.parse_action_line (with ``[actions] link_hosts``); a line it cannot read keeps its reason.
2. actions.restrict(action, ASK_KINDS, "ask"): Slack, Share and lines without a kind are
   information only ("Ask can't propose Slack").
3. acct= must be one of your accounts.
4. RSVP, Move and Cancel need their (account, calendar, event) in the context; Move and Cancel only
   for an event you organize, RSVP only for one you do not.
5. A Reply needs its (account, thread) to be a briefing Reply line or a thread Jarvis read, and its
   msgid= / gmid= to be a message of that thread.
6. Every Email / Reply recipient must be in the context (a guest, a briefing line, mail Jarvis read)
   or typed by you; the ones you did not type go into the card's ``unverified`` (NEW until ticked,
   even in a trusted domain, unless Jarvis sent to them before: recipients.review).
7. A Move to a time in the past is refused; more than FAR_DAYS ahead gets a warning.
8. A card whose id is already on the page becomes "Already in your list under BRIEFING - use Edit
   there"; a repeat of an earlier line of the same answer is dropped.

A planner mode (the productivity bundle) narrows and extends this: ``kinds`` the kinds it may
propose (a day plan: Calendar only, "A day plan only adds time blocks"), ``extra(card) -> str``
its own checks after rule 8 (ask.dayplan.DayCheck: inside a free slot, never a clash ...; a reason
makes the card information only), ``mode`` set on every card; keep_valid() then keeps at most N
valid cards (the rest are dropped and counted). The defaults keep every Ask exactly as before.

Pure; nothing is logged here (planner.py logs the counts; Cards.report never is).
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from ..actions import (
    CALENDAR,
    CANCEL,
    EMAIL,
    MAIL_KINDS,
    MOVE,
    OPEN,
    REPLY,
    RSVP,
    SOURCE_ASK,
    TODO,
    ProposedAction,
    parse_action_line,
    restrict,
    with_error,
)
from .context import ContextIndex

ASK_KINDS = frozenset({CALENDAR, TODO, RSVP, MOVE, CANCEL, EMAIL, REPLY, OPEN})
ACCOUNT_LINE_KINDS = frozenset({RSVP, MOVE, CANCEL, EMAIL, REPLY})
FAR_DAYS = 60
ALREADY_LISTED = "Already in your list under BRIEFING - use Edit there"
UNKNOWN_EVENT = "Jarvis doesn't know this event - ask again"
NOT_ORGANIZER = "You don't organize this event, so Jarvis can't {verb} it - ask for an email to the organizer"
ORGANIZER_RSVP = "You organize this event, so there is no invitation to answer"
UNKNOWN_THREAD = "Jarvis can only reply to a thread in your briefing or in mail it read for this request"
UNKNOWN_MESSAGE = "This reply names a message that is not in that thread - ask again"
PAST_MOVE = "The new time is in the past"
FAR_MOVE = f"The new time is more than {FAR_DAYS} days away - check the date"
# A planner mode's refusal of a kind it may not propose (else restrict's "Ask can't propose ...").
MODE_KIND_REFUSALS = {"plan": "A day plan only adds time blocks",
                      "prep": "A meeting brief only drafts replies",
                      "nudge": "A follow-up draft is one reply"}


LINE_CARD = "card"            # the line became a card Jarvis can carry out
LINE_REFUSED = "refused"      # the line became an information-only card (reason: the card's error)
LINE_REPEAT = "repeat"        # a repeat of an earlier line of the same answer: dropped
LINE_OVER_CAP = "over_cap"    # past [ask] max_cards: dropped unchecked


@dataclass(frozen=True)
class LineReport:
    """What became of one planner line (the LIVE view's "Checking the proposals"; never logged)."""

    line: str                 # the planner's line as given
    outcome: str              # LINE_CARD | LINE_REFUSED | LINE_REPEAT | LINE_OVER_CAP
    card_id: str = ""
    reason: str = ""          # the refusal text (the card's error); "" for a card

    def __repr__(self) -> str:   # never the line or the reason (they name people)
        return f"LineReport(outcome={self.outcome!r})"


@dataclass(frozen=True)
class Cards:
    cards: tuple[ProposedAction, ...] = ()
    dropped: int = 0            # lines beyond max_cards (or repeats)
    refused: int = 0            # cards that became information only
    report: tuple[LineReport, ...] = field(default=(), repr=False)   # every line, in order

    def kinds(self) -> dict[str, int]:
        """Cards per kind, with "refused" for the information-only ones (safe to log)."""
        counts: dict[str, int] = {}
        for card in self.cards:
            name = "refused" if card.error else card.kind
            counts[name] = counts.get(name, 0) + 1
        return counts


def unknown_address(address: str) -> str:
    """Shown on the card only (it names the address); never logged or saved."""
    return f"Jarvis didn't find {address} in your calendar, briefing, mail or your words"


def _problem(action: ProposedAction, index: ContextIndex, now: datetime) -> str:
    kind = action.kind
    if kind in ACCOUNT_LINE_KINDS or (kind == TODO and action.account):
        if action.account not in index.aliases:
            return f'Jarvis has no account called "{action.account}"'
    if kind in (RSVP, MOVE, CANCEL):
        facts = index.event(action.account, action.field("cal", "primary"), action.field("event"))
        if facts is None:
            return UNKNOWN_EVENT
        if kind in (MOVE, CANCEL) and not facts.organizer_self:
            return NOT_ORGANIZER.format(verb="move" if kind == MOVE else "cancel")
        if kind == RSVP and facts.organizer_self:
            return ORGANIZER_RSVP
        if kind == MOVE and action.start is not None:
            zone = facts.start.tzinfo if facts.start is not None and facts.start.tzinfo else now.tzinfo
            if action.start.replace(tzinfo=zone) < now:
                return PAST_MOVE
    if kind == REPLY:
        ids = index.thread_ids(action.account, action.field("thread"))
        if ids is None:
            return UNKNOWN_THREAD
        msgid, gmid = action.field("msgid"), action.field("gmid")
        if (msgid and msgid not in ids) or (gmid and f"gmid:{gmid}" not in ids):
            return UNKNOWN_MESSAGE
    if kind in MAIL_KINDS:
        for address in action.mail_recipients():
            if not index.knows(address):
                return unknown_address(address)
    return ""


def check_line(line: str, index: ContextIndex, *, now: datetime, link_hosts: Sequence[str] = (),
               kinds: Collection[str] = ASK_KINDS, refusal: str = "") -> ProposedAction:
    """One planner line as an Ask card: valid, or information only with the reason. ``kinds``: what
    may be proposed (a planner mode's own); ``refusal``: its words for any other kind."""
    parsed = parse_action_line(line, link_hosts=link_hosts)
    if refusal and not parsed.error and parsed.kind not in kinds:
        return with_error(replace(parsed, source=SOURCE_ASK), refusal)
    action = restrict(parsed, kinds, SOURCE_ASK)
    if action.error:
        return action
    problem = _problem(action, index, now)
    if problem:
        return with_error(action, problem)
    if action.kind in MAIL_KINDS:
        unverified = frozenset(address.casefold() for address in action.mail_recipients() if not index.typed(address))
        action = replace(action, unverified=unverified)
    far = now.replace(tzinfo=None) + timedelta(days=FAR_DAYS)
    if action.kind == MOVE and action.start is not None and action.start > far:
        action = replace(action, warnings=action.warnings + (FAR_MOVE,))
    return action


def to_cards(lines: Sequence[str], index: ContextIndex, *, now: datetime, link_hosts: Sequence[str] = (),
             page_ids: Collection[str] = (), max_cards: int = 8, kinds: Collection[str] = ASK_KINDS,
             mode: str = "", extra: Callable[[ProposedAction], str] | None = None) -> Cards:
    """The planner's lines as cards, in order: at most ``max_cards``; a card whose id is already on
    the page is information only (ALREADY_LISTED); a repeat within this answer is dropped. A
    planner mode passes its ``kinds``, its ``mode`` (set on every card) and its ``extra`` check (run
    on each card still valid after the page and repeat checks; a reason makes it information only)."""
    cards: list[ProposedAction] = []
    seen: set[str] = set()
    dropped = 0
    report: list[LineReport] = []
    refusal = MODE_KIND_REFUSALS.get(mode, "")
    for line in lines:
        if len(cards) >= max_cards:
            dropped += 1
            report.append(LineReport(line, LINE_OVER_CAP))
            continue
        card = check_line(line, index, now=now, link_hosts=link_hosts, kinds=kinds, refusal=refusal)
        if not card.error and card.id in page_ids:
            card = with_error(card, ALREADY_LISTED)
        if card.id in seen:
            dropped += 1
            report.append(LineReport(line, LINE_REPEAT, card.id))
            continue
        seen.add(card.id)
        if extra is not None and not card.error:
            reason = extra(card)
            if reason:
                card = with_error(card, reason)
        if mode:
            card = replace(card, mode=mode)
        cards.append(card)
        report.append(LineReport(line, LINE_REFUSED if card.error else LINE_CARD, card.id, card.error))
    return Cards(tuple(cards), dropped, sum(1 for card in cards if card.error), tuple(report))


def keep_valid(cards: Cards, limit: int) -> Cards:
    """``cards`` with at most ``limit`` valid cards: the valid ones after that are dropped (counted in
    ``dropped``; their report lines say LINE_OVER_CAP). Information-only cards stay."""
    kept: list[ProposedAction] = []
    gone: set[str] = set()
    valid = 0
    for card in cards.cards:
        if not card.error:
            valid += 1
            if valid > max(0, limit):
                gone.add(card.id)
                continue
        kept.append(card)
    if not gone:
        return cards
    report = tuple(replace(entry, outcome=LINE_OVER_CAP) if entry.outcome == LINE_CARD and entry.card_id in gone
                   else entry for entry in cards.report)
    return Cards(tuple(kept), cards.dropped + len(gone), cards.refused, report)
