"""What Jarvis says on his own, in his persona: concise, polite, British, "sir".

This module only builds sentences; it never speaks, logs or stores them.

* ``with_address(template, address)`` fills the address markers: ``{sir}``
  inside a sentence and ``{Sir}`` at its start ("Good morning, {sir}." ->
  "Good morning, sir."). With no address ("") the marker and its comma go
  ("Good morning.", "{Sir}, your AM ..." -> "Your AM ...").
* ``GREETINGS``: 24 short greetings with stable ids in five groups (morning,
  afternoon, evening, late night and neutral); ``bucket_for`` says which time
  group an hour belongs to and ``pick_greeting`` picks one at random that was
  not heard recently (never the last one).
* ``announcement``: "Sir, your AM briefing is ready to view." (after a greeting:
  "Your AM briefing is ready to view.").
* ``approvals_sentence`` and ``next_event_sentence``: the one fact a greeting
  may add, both safe to say aloud (a count; an event title and time, never
  email content or an address).
* ``compose_opening``: greeting + announcement + fact as one utterance of at
  most three sentences (the greeting then opens with a plain statement, never a
  question it does not wait for); ``salutation`` is the plain "Good morning,
  sir." of an hour (the prefix of a scheduled arrival's announcement).
* ``spoken_when``: "today at 3 PM", "tomorrow at 2:30 PM", "on Friday at 9 AM",
  "on October 12" for the sentences below.
* ``outcome_text`` / ``not_sent_text``: what Jarvis says after the executor
  carried out (or could not carry out) an approved card - the only place "Done"
  or "Sent" is ever said ("Done, sir - I've moved 'Project sync' to Friday at
  2 PM."); ``outcome_tone`` says how the conversation shows it. Never the
  error message, a body or a recipient.
* ``ask_reply_speech`` / ``ask_failure_speech`` / ``ACKS``: Ask's spoken answer
  (the planner's say and question only, guarded by ``claims_done``; the question
  is always kept, and after mail was read the say is kept short and never quotes
  it; ``ask_reply_note_speech``: one that repeats a memory note gives way to a
  counts-only sentence), its failures and the short "One moment, sir." while it
  plans.
* ``research_reply_speech``: web research's spoken answer (the answer without its
  [n] marks, guarded by ``claims_done(first_person_only=True)``, then that the
  sources are on screen and how many suggestions wait for the OK).
* ``scrub_for_speech``: addresses, links and long ids out, at most 400
  characters cut at a sentence; every spoken text goes through it.
* Memory (the productivity bundle): ``memory_saved_speech`` ("Noted, sir."),
  ``memory_forgotten_speech`` and ``memory_refused_speech`` are all that is
  said aloud (never a note); ``memory_saved_text``, ``memory_refusal_text``,
  ``memory_suggestion_text`` and ``quoted_note`` build the JARVIS entries.
* The day plan: ``plan_result_speech``, ``plan_question_speech`` and
  ``plan_no_run_speech`` (counts and times only), ``plan_answer_text`` and
  ``plan_no_run_text`` (the entries).
* Meeting prep: ``prep_result_speech`` ("Your brief for the 2 PM meeting is
  ready, sir."; the meeting's time only, never the brief) and
  ``prep_no_run_speech``; ``prep_choice_text``, ``prep_choices_text`` and
  ``prep_no_run_text`` build the entries.

Qt-free; standard library only; ASCII.
"""

from __future__ import annotations

import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, NamedTuple

MORNING = "morning"
AFTERNOON = "afternoon"
EVENING = "evening"
LATE = "late"
NEUTRAL = "neutral"
TIME_BUCKETS = (MORNING, AFTERNOON, EVENING, LATE)
BUCKETS = TIME_BUCKETS + (NEUTRAL,)
TIME_BUCKET_SHARE = 0.6        # how often the hour's own group is used (else the neutral lines)
RECENT_AVOIDED = 5             # greetings remembered (assistant.json) and avoided while possible
EVENT_TITLE_MAX = 60           # an event title said aloud is cut to this many characters (at a word)

_SENTENCE_END_RE = re.compile(r"(?<=[.?!])\s+")
_UNSAFE_RE = re.compile(r"@|://|www\.", re.IGNORECASE)


@dataclass(frozen=True)
class Greeting:
    """One greeting line: a stable id ("m3"), its group and its template ({sir} / {Sir} markers)."""

    id: str
    bucket: str
    template: str

    def text(self, address: str = "sir") -> str:
        return with_address(self.template, address)


GREETINGS: tuple[Greeting, ...] = (
    Greeting("m1", MORNING, "Good morning, {sir}."),
    Greeting("m2", MORNING, "Morning, {sir}. What's first on the list?"),
    Greeting("m3", MORNING, "Good morning, {sir}. Ready when you are."),
    Greeting("m4", MORNING, "Good morning, {sir}. I trust you slept well."),
    Greeting("m5", MORNING, "Morning, {sir}. Shall we get started?"),
    Greeting("a1", AFTERNOON, "Good afternoon, {sir}."),
    Greeting("a2", AFTERNOON, "Afternoon, {sir}. How can I help?"),
    Greeting("a3", AFTERNOON, "Good afternoon, {sir}. What do you need?"),
    Greeting("a4", AFTERNOON, "Afternoon, {sir}. How's the day treating you?"),
    Greeting("e1", EVENING, "Good evening, {sir}."),
    Greeting("e2", EVENING, "Evening, {sir}. What can I do for you?"),
    Greeting("e3", EVENING, "Good evening, {sir}. How was the day?"),
    Greeting("e4", EVENING, "Evening, {sir}. At your service."),
    Greeting("l1", LATE, "Working late, {sir}?"),
    Greeting("l2", LATE, "Burning the midnight oil, {sir}?"),
    Greeting("l3", LATE, "Still up, {sir}? I'm here."),
    Greeting("l4", LATE, "A late one tonight, {sir}."),
    Greeting("n1", NEUTRAL, "At your service, {sir}."),
    Greeting("n2", NEUTRAL, "Welcome back, {sir}."),
    Greeting("n3", NEUTRAL, "Hello, {sir}."),
    Greeting("n4", NEUTRAL, "Ready when you are, {sir}."),
    Greeting("n5", NEUTRAL, "How can I help, {sir}?"),
    Greeting("n6", NEUTRAL, "What can I do for you, {sir}?"),
    Greeting("n7", NEUTRAL, "Jarvis here, {sir}. What do you need?"),
)
GREETINGS_BY_ID = {greeting.id: greeting for greeting in GREETINGS}

_NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


# --------------------------------------------------------------------------
# The form of address
# --------------------------------------------------------------------------

def with_address(template: str, address: str = "sir") -> str:
    """``template`` with ``{sir}`` / ``{Sir}`` filled in.

    "boss": "Good morning, boss." and "Boss, your AM briefing ...". An empty
    address drops the marker with the comma or space before it ("How can I
    help?") and a sentence-start "{Sir}, " ("{Sir}, your" -> "Your").
    """
    address = " ".join(str(address or "").split())
    if address:
        capital = address[0].upper() + address[1:]
        return template.replace("{Sir}", capital).replace("{sir}", address)
    text = re.sub(r",?\s*\{sir\}", "", template)
    text = re.sub(r"\{Sir\}[,]?\s*(\w?)", lambda match: match.group(1).upper(), text)
    return " ".join(text.split())


# --------------------------------------------------------------------------
# Greetings
# --------------------------------------------------------------------------

def bucket_for(when: datetime | int) -> str:
    """The time group of an hour (or a datetime's local hour): morning 05:00-11:59, afternoon
    12:00-16:59, evening 17:00-21:59, late 22:00-04:59."""
    hour = when.hour if isinstance(when, datetime) else int(when) % 24
    if 5 <= hour < 12:
        return MORNING
    if 12 <= hour < 17:
        return AFTERNOON
    if 17 <= hour < 22:
        return EVENING
    return LATE


def pick_greeting(now: datetime, recent: Sequence[str] = (), rng: random.Random | None = None) -> Greeting:
    """A random greeting for ``now``: the hour's own group with probability 0.6, else a neutral line.

    Lines among ``recent`` (the last few ids, oldest first) are avoided while the group has others;
    when it has none left the other group is used. The most recent one (``recent[-1]``) is never
    picked again, whatever happens.
    """
    rng = rng or random.Random()
    recent = [item for item in recent if isinstance(item, str)][-RECENT_AVOIDED:]
    last = recent[-1] if recent else None
    timed = [line for line in GREETINGS if line.bucket == bucket_for(now)]
    neutral = [line for line in GREETINGS if line.bucket == NEUTRAL]
    groups = (timed, neutral) if rng.random() < TIME_BUCKET_SHARE else (neutral, timed)
    for group in groups:
        fresh = [line for line in group if line.id not in recent]
        if fresh:
            return rng.choice(fresh)
    # Every line of both groups was heard recently (only with a longer history): anything but the last.
    return rng.choice([line for line in groups[0] + groups[1] if line.id != last])


# --------------------------------------------------------------------------
# The announcement and the one extra fact
# --------------------------------------------------------------------------

def announcement(run: str | None, address: str = "sir", *, after_greeting: bool = False) -> str:
    """"Sir, your AM briefing is ready to view." (after a greeting: "Your AM briefing is ready
    to view."); without a run label "your briefing"."""
    label = f"{run} briefing" if run in ("AM", "PM") else "briefing"
    if after_greeting:
        return f"Your {label} is ready to view."
    return with_address(f"{{Sir}}, your {label} is ready to view.", address)


def count_words(count: int) -> str:
    """one .. ten as words, larger numbers as digits."""
    return _NUMBER_WORDS[count] if 0 <= count < len(_NUMBER_WORDS) else str(count)


def approvals_sentence(count: int, address: str = "") -> str:
    """"One proposal is waiting for your OK." / "Three proposals are waiting for your OK."; "" for 0.

    With ``address``: "..., sir." (Ask's spoken reply); the greeting uses it without."""
    if count <= 0:
        return ""
    number = count_words(count)
    number = number[0].upper() + number[1:]
    verb = "proposal is" if count == 1 else "proposals are"
    tail = f", {address}" if address else ""
    return f"{number} {verb} waiting for your OK{tail}."


def spoken_clock(moment: datetime) -> str:
    """12-hour time for speech with ":00" dropped: "3 PM", "2:30 PM", "12 AM"."""
    hour = moment.hour % 12 or 12
    meridiem = "AM" if moment.hour < 12 else "PM"
    return f"{hour} {meridiem}" if moment.minute == 0 else f"{hour}:{moment.minute:02d} {meridiem}"


def cut_title(title: str, limit: int = EVENT_TITLE_MAX) -> str:
    """One line of at most ``limit`` characters, cut at a word (no ellipsis: it is spoken)."""
    flat = " ".join(str(title or "").split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit + 1].rsplit(" ", 1)[0] if " " in flat[:limit + 1] else flat[:limit]
    return cut.rstrip(" ,;:-")


def next_event_sentence(title: str, start: datetime) -> str:
    """"Your next event is Project sync at 3 PM."; "" when the title is empty or not safe to say
    aloud (it holds an address or a link)."""
    name = cut_title(title)
    if not name or _UNSAFE_RE.search(name):
        return ""
    return f"Your next event is {name} at {spoken_clock(start)}."


def sentences(text: str) -> list[str]:
    """``text`` split after ".", "?" and "!" (each piece keeps its end mark)."""
    return [piece for piece in _SENTENCE_END_RE.split(" ".join(str(text or "").split())) if piece]


# The plain salutation of each time group ("Good morning, {sir}."): the opening of a greeting that is
# followed by an announcement or a fact when its own first sentence would not suit, and the prefix of
# a scheduled arrival's announcement. Late at night it is "Good evening" until midnight, then "Hello".
_SALUTATIONS = {MORNING: "m1", AFTERNOON: "a1", EVENING: "e1"}
_LATE_EVENING_FROM = 22
_READY_RE = re.compile(r"\bready\b", re.IGNORECASE)


def salutation(now: datetime) -> Greeting:
    """The plain greeting of ``now``'s hour: "Good morning, {sir}." (m1), "Good afternoon" (a1),
    "Good evening" (e1, also 22:00-23:59), "Hello" (n3) after midnight."""
    bucket = bucket_for(now)
    if bucket == LATE:
        return GREETINGS_BY_ID["e1" if now.hour >= _LATE_EVENING_FROM else "n3"]
    return GREETINGS_BY_ID[_SALUTATIONS[bucket]]


def compose_opening(greeting: str, announcement_text: str = "", context: str = "", *, plain: str = "") -> str:
    """The greeting, then the announcement, then the fact, as one utterance of at most three
    sentences. When anything follows the greeting, only its first sentence is kept ("Good evening,
    sir. How was the day?" + "Your PM briefing is ready to view." -> "Good evening, sir. Your PM
    briefing is ready to view."), and when that sentence is a question ("How can I help, sir?") or
    says "ready" before "ready to view", ``plain`` (the hour's ``salutation``) opens instead: it never
    asks a question it does not wait for."""
    first = sentences(greeting)
    rest = [piece for part in (announcement_text, context) for piece in sentences(part)]
    if rest:
        first = first[:1]
        clash = bool(first) and (first[0].endswith("?")
                                 or (bool(announcement_text) and _READY_RE.search(first[0]) is not None))
        if clash and plain:
            first = sentences(plain)[:1]
    return " ".join((first + rest)[:3])


# --------------------------------------------------------------------------
# When, said aloud
# --------------------------------------------------------------------------

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December")


def spoken_when(start: datetime | date | None, end: datetime | date | None = None, now: datetime | None = None,
                *, all_day: bool = False) -> str:
    """"today at 3 PM", "tomorrow at 2:30 PM", "on Friday at 9 AM" (2-6 days ahead), "on October 12
    at 3 PM" (later or past); all-day (or a plain date): "today", "tomorrow", "on Friday", "on October
    12". "" without a start. ``end`` is not said (kept for the callers' symmetry)."""
    del end
    if start is None:
        return ""
    now = now or datetime.now().astimezone()
    timed = isinstance(start, datetime) and not all_day
    if isinstance(start, datetime):
        if start.tzinfo is not None and now.tzinfo is not None:
            start = start.astimezone(now.tzinfo)
        day = start.date()
    else:
        day = start
    delta = (day - now.date()).days
    if delta == 0:
        phrase = "today"
    elif delta == 1:
        phrase = "tomorrow"
    elif 2 <= delta <= 6:
        phrase = f"on {_WEEKDAYS[day.weekday()]}"
    else:
        phrase = f"on {_MONTHS[day.month - 1]} {day.day}"
    if not timed:
        return phrase
    assert isinstance(start, datetime)
    return f"{phrase} at {spoken_clock(start)}"


# --------------------------------------------------------------------------
# Safe to say aloud
# --------------------------------------------------------------------------

SPEECH_LIMIT = 400
_URL_RE = re.compile(r"(?:\b[a-z][a-z0-9+.-]*://|\bwww\.)[^\s<>\"]+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)*")
# An address written with spaces around the @ ("ana @ example.edu") or spelled out ("ana at
# example.edu"): a dotted domain follows, so "Ping @sam" and "meet at 3.30" stay as they are.
_SPACED_EMAIL_RE = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+\s*@\s*[\w-]+(?:\.[\w-]+)+")
_SPELLED_EMAIL_RE = re.compile(r"\b[\w.+-]+\s+at\s+[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b", re.IGNORECASE)
# Ids, tokens and other long machine strings: 16 or more word characters with a digit or an
# underscore in them, or hex only. Long English words ("responsibilities") stay.
_LONG_ID_RE = re.compile(r"\b(?:(?=[\w-]*[\d_])[\w-]{16,}|[0-9a-fA-F]{16,})\b")
_TRAILING_PUNCTUATION = ".,;:!?)]}'\""


def _replace_link(match: re.Match[str]) -> str:
    link = match.group(0)
    kept = link.rstrip(_TRAILING_PUNCTUATION)
    return "the link" + link[len(kept):]


def _scrub(text: str) -> str:
    """Links, addresses and long ids replaced by plain words; whitespace collapsed."""
    flat = " ".join(str(text or "").split())
    flat = _URL_RE.sub(_replace_link, flat)
    flat = _EMAIL_RE.sub("that address", flat)
    flat = _SPACED_EMAIL_RE.sub("that address", flat)
    flat = _SPELLED_EMAIL_RE.sub("that address", flat)
    flat = flat.replace("@", " at ")
    flat = _LONG_ID_RE.sub("that item", flat)
    return " ".join(flat.split())


_CLAUSE_RE = re.compile(r"(?:[,;:]|\s-)\s")


def scrub_for_speech(text: str, limit: int = SPEECH_LIMIT) -> str:
    """``text`` made safe and short enough to say aloud: email addresses become "that address", links
    "the link", long ids "that item"; at most ``limit`` characters, cut after the last sentence that
    fits. When that would keep less than half of ``limit`` (one long sentence), it is cut at the
    last clause that fits (", because ..." -> "."), else at a word."""
    flat = _scrub(text)
    if len(flat) <= limit:
        return flat
    head = flat[:limit]
    ends = [match.end() for match in re.finditer(r"[.?!](?=\s|$)", head)]
    sentence = ends[-1] if ends else 0
    if sentence < limit // 2:
        clauses = [match.start() for match in _CLAUSE_RE.finditer(head)]
        if clauses and clauses[-1] > sentence:
            return head[:clauses[-1]].rstrip(" ,;:-") + "."
    if sentence:
        return head[:sentence].strip()
    cut = head.rsplit(" ", 1)[0] if " " in head else head
    return cut.rstrip(" ,;:-") + "."


_DONE_VERBS = "sent|moved|cancel+ed|added|booked|scheduled|accepted|declined|deleted|emailed|replied|rescheduled"
# Completed verbs whose present tense differs ("I moved it" claims; "Shall I move it?" does not).
_DONE_PAST = (_DONE_VERBS + "|created|updated|blocked|told|notified|cleared|arranged|confirmed|made|informed"
              "|messaged|forwarded|removed|changed|shifted|pushed|sorted|handled")
# ... and the ones that read the same in the present ("I've set", "I've put"; never "Shall I set").
_DONE_PERFECT = _DONE_PAST + r"|set|put|let\s+\S+(?:\s+\S+)?\s+know|taken\s+care\s+of"
_ADVERBS = r"(?:(?:just|now|already|also|all|successfully|gone\s+ahead\s+and)\s+)*"
# "Done." / "Sent." / "All set." / "Booked ..." at the start of a sentence ("Right, sir. Moved Project
# sync to Friday.").
_CLAIM_START_RE = re.compile(r"(?:^|[.!?]\s+)\s*(?:done|all\s+done|all\s+set|sorted|" + _DONE_VERBS + r")\b",
                             re.IGNORECASE)
_CLAIM_RES = (
    # "I've sent", "I have now moved", "I've let Ana know", "I've taken care of it".
    re.compile(r"\b(?:i've|i\s+have)\s+" + _ADVERBS + r"(?:" + _DONE_PERFECT + r")\b", re.IGNORECASE),
    # "I moved it to Friday", "I've ... and I also told Ana".
    re.compile(r"\bi\s+" + _ADVERBS + r"(?:" + _DONE_PAST + r")\b", re.IGNORECASE),
    # "It has been moved", "is now cancelled", "The meeting was moved to Friday", "were sent".
    re.compile(r"\b(?:it's\s+been|it\s+has\s+been|has\s+been|have\s+been|is\s+now|are\s+now|'s\s+now|was|were)\s+"
               + _ADVERBS + r"(?:" + _DONE_PERFECT + r")\b", re.IGNORECASE),
    # "All done", "Consider it done", "on its way", "has gone out", "It's on your calendar",
    # "Project sync is now on Friday".
    re.compile(r"\b(?:all\s+(?:done|set|sorted)|consider\s+it\s+(?:done|sorted|handled)|taken\s+care\s+of"
               r"|on\s+(?:its|their)\s+way|(?:has|have)\s+gone\s+(?:out|through)"
               r"|(?:it's|it\s+is|that's|that\s+is|they're|they\s+are)\s+(?:now\s+)?(?:on|in)\s+your\s+(?:calendar|diary)"
               r"|(?:is|are|'s)\s+now\s+(?:on|at|in|for|set|booked|confirmed))\b", re.IGNORECASE),
)


def claims_done(text: str, *, first_person_only: bool = False) -> bool:
    """The text says something was already carried out ("Done.", "All set.", "I've sent the email",
    "I've put it on your calendar", "I've let Ana know", "It has been moved", "The meeting was moved
    to Friday", "Project sync is now on Friday", "Your email is on its way", "Consider it done").
    Only the executor's result may say that, so such a planner answer is never spoken as written.
    "I've lined up a move", "Shall I set up a block?" or "waiting for your OK" do not count. It errs
    on the side of a claim: the owner still reads the planner's words in the JARVIS tab.

    ``first_person_only`` (a web research answer, which states facts about the world): only a
    sentence that starts with a claim ("Done.", "All set.", "Booked ...") or the speaker's own
    ("I've added", "I moved") counts; "The match was cancelled [1]." is a fact, not a claim."""
    flat = " ".join(str(text or "").replace("\u2019", "'").split())
    patterns = _CLAIM_RES[:2] if first_person_only else _CLAIM_RES
    return bool(_CLAIM_START_RE.search(flat) or any(pattern.search(flat) for pattern in patterns))


# --------------------------------------------------------------------------
# After an approved card was carried out (the executor's result)
# --------------------------------------------------------------------------

OUTCOME_GOOD = "good"      # created / sent / already there
OUTCOME_ERROR = "error"    # failed
OUTCOME_WARN = "warn"      # unknown, or not sent at the end of the countdown

_DONE, _EXISTS, _FAILED, _UNKNOWN = "done", "exists", "failed", "unknown"
_COLUMNS = {"created": _DONE, "sent": _DONE, "exists": _EXISTS, "failed": _FAILED, "unknown": _UNKNOWN}
_CHECK_CALENDAR = "Please check your calendar before trying again."
_CHECK_SENT = "Please check your Sent mail before trying again."
_CARD_SAYS = "The card says why."
# (kind, column) -> (with the title as {t}, without a title); {when} / {for_when} / {to_when} are
# filled in where they appear.
_OUTCOMES: dict[tuple[str, str], tuple[str, str]] = {
    ("calendar", _DONE): ("Done, {sir} - I've added '{t}' to your calendar{for_when}.",
                          "Done, {sir} - I've added the event to your calendar{for_when}."),
    ("calendar", _EXISTS): ("'{t}' was already on your calendar, {sir} - nothing new was added.",
                            "That event was already on your calendar, {sir} - nothing new was added."),
    ("calendar", _FAILED): ("Sorry, {sir} - I couldn't add '{t}'. " + _CARD_SAYS,
                            "Sorry, {sir} - I couldn't add the event. " + _CARD_SAYS),
    ("calendar", _UNKNOWN): ("{Sir}, I can't tell whether '{t}' was added. " + _CHECK_CALENDAR,
                             "{Sir}, I can't tell whether the event was added. " + _CHECK_CALENDAR),
    ("todo", _DONE): ("Done, {sir} - I've blocked time {when} for '{t}'.",
                      "Done, {sir} - I've blocked time {when} on your calendar."),
    ("todo", _EXISTS): ("That block for '{t}' was already on your calendar, {sir}.",
                        "That block was already on your calendar, {sir}."),
    ("todo", _FAILED): ("Sorry, {sir} - I couldn't block time for '{t}'. " + _CARD_SAYS,
                        "Sorry, {sir} - I couldn't block that time. " + _CARD_SAYS),
    ("todo", _UNKNOWN): ("{Sir}, I can't tell whether the block for '{t}' was added. " + _CHECK_CALENDAR,
                         "{Sir}, I can't tell whether the block was added. " + _CHECK_CALENDAR),
    ("rsvp", _FAILED): ("Sorry, {sir} - I couldn't answer '{t}'. " + _CARD_SAYS,
                        "Sorry, {sir} - I couldn't answer the invitation. " + _CARD_SAYS),
    ("rsvp", _UNKNOWN): ("{Sir}, I can't tell whether your answer to '{t}' went through. " + _CHECK_CALENDAR,
                         "{Sir}, I can't tell whether your answer to the invitation went through. "
                         + _CHECK_CALENDAR),
    ("move", _DONE): ("Done, {sir} - I've moved '{t}'{to_when}.",
                      "Done, {sir} - I've moved the meeting{to_when}."),
    ("move", _FAILED): ("Sorry, {sir} - I couldn't move '{t}'. " + _CARD_SAYS,
                        "Sorry, {sir} - I couldn't move the meeting. " + _CARD_SAYS),
    ("move", _UNKNOWN): ("{Sir}, I can't tell whether '{t}' was moved. " + _CHECK_CALENDAR,
                         "{Sir}, I can't tell whether the meeting was moved. " + _CHECK_CALENDAR),
    ("cancel", _DONE): ("Done, {sir} - I've cancelled '{t}'.", "Done, {sir} - I've cancelled the meeting."),
    ("cancel", _FAILED): ("Sorry, {sir} - I couldn't cancel '{t}'. " + _CARD_SAYS,
                          "Sorry, {sir} - I couldn't cancel the meeting. " + _CARD_SAYS),
    ("cancel", _UNKNOWN): ("{Sir}, I can't tell whether '{t}' was cancelled. " + _CHECK_CALENDAR,
                           "{Sir}, I can't tell whether the meeting was cancelled. " + _CHECK_CALENDAR),
    ("email", _DONE): ("Sent, {sir} - your email '{t}' is on its way.", "Sent, {sir} - your email is on its way."),
    ("email", _FAILED): ("Sorry, {sir} - I couldn't send '{t}'. " + _CARD_SAYS,
                         "Sorry, {sir} - I couldn't send the email. " + _CARD_SAYS),
    ("email", _UNKNOWN): ("{Sir}, I can't tell whether '{t}' was sent. " + _CHECK_SENT,
                          "{Sir}, I can't tell whether the email was sent. " + _CHECK_SENT),
    ("reply", _DONE): ("Sent, {sir} - your reply to '{t}' is on its way.", "Sent, {sir} - your reply is on its way."),
    ("reply", _FAILED): ("Sorry, {sir} - I couldn't send your reply to '{t}'. " + _CARD_SAYS,
                         "Sorry, {sir} - I couldn't send your reply. " + _CARD_SAYS),
    ("reply", _UNKNOWN): ("{Sir}, I can't tell whether your reply to '{t}' was sent. " + _CHECK_SENT,
                          "{Sir}, I can't tell whether your reply was sent. " + _CHECK_SENT),
}
_RSVP_DONE = {"yes": ("Done, {sir} - I've accepted '{t}'.", "Done, {sir} - I've accepted the invitation."),
              "no": ("Done, {sir} - I've declined '{t}'.", "Done, {sir} - I've declined the invitation."),
              "maybe": ("Done, {sir} - I've answered maybe to '{t}'.",
                        "Done, {sir} - I've answered maybe to the invitation.")}
_RSVP_DONE_OTHER = ("Done, {sir} - I've answered '{t}'.", "Done, {sir} - I've answered the invitation.")
_TODO_DONE_NO_WHEN = ("Done, {sir} - I've blocked time for '{t}'.",
                      "Done, {sir} - I've added the block to your calendar.")
_NOT_SENT_TAIL = ": the sending account changed. Please check From and try again."
_NOT_SENT = {"email": ("{Sir}, I didn't send '{t}'" + _NOT_SENT_TAIL, "{Sir}, I didn't send the email" + _NOT_SENT_TAIL),
             "reply": ("{Sir}, I didn't send your reply to '{t}'" + _NOT_SENT_TAIL,
                       "{Sir}, I didn't send your reply" + _NOT_SENT_TAIL)}
OUTCOME_KINDS = ("calendar", "todo", "rsvp", "move", "cancel", "email", "reply")
# "Re: ", "Fwd: ", "AW: " at the start of a subject are not said (the same rule as the spoken briefing,
# actions._REPLY_PREFIX_RE): "your reply to 'Budget review'".
_SUBJECT_PREFIX_RE = re.compile(r"^(?:(?:re|fwd?|aw)\s*:\s*)+", re.IGNORECASE)
_MAIL_KINDS = ("email", "reply")


def spoken_subject(subject: str) -> str:
    """An email's subject without its leading "Re:" / "Fwd:" (kept when nothing else is left)."""
    flat = " ".join(str(subject or "").split())
    return _SUBJECT_PREFIX_RE.sub("", flat).strip() or flat


def spoken_title(title: str, limit: int = EVENT_TITLE_MAX) -> str:
    """A title or subject as Jarvis says it: links, addresses and ids out, one line of at most
    ``limit`` characters (cut at a word); "" when nothing is left."""
    return cut_title(_scrub(title).strip(" '\""), limit)


def _fill(pair: tuple[str, str], title: str, when: str, address: str) -> str:
    name = spoken_title(title)
    when = _scrub(when)
    bare = when[3:] if when.startswith("on ") else when   # "to Friday at 2 PM", not "to on Friday"
    text = with_address(pair[0] if name else pair[1], address)
    return (text.replace("{for_when}", f" for {bare}" if bare else "")
                .replace("{to_when}", f" to {bare}" if bare else "")
                .replace("{when}", when)
                .replace("{t}", name))


def outcome_column(status: str) -> str:
    """created / sent -> "done"; exists, failed and unknown as they are; anything else "failed"."""
    return _COLUMNS.get(status, _FAILED)


def outcome_tone(status: str) -> str:
    """How the conversation shows a result: OUTCOME_GOOD (created, sent, already there),
    OUTCOME_ERROR (failed) or OUTCOME_WARN (unknown)."""
    column = outcome_column(status)
    if column in (_DONE, _EXISTS):
        return OUTCOME_GOOD
    return OUTCOME_WARN if column == _UNKNOWN else OUTCOME_ERROR


def outcome_text(kind: str, status: str, title: str = "", *, when: str = "", answer: str = "",
                 address: str = "sir") -> str:
    """The one sentence after the executor's result for an approved card of ``kind`` (calendar, todo,
    rsvp, move, cancel, email, reply) with ``status`` (created / sent, exists, failed, unknown):
    "Done, sir - I've added 'Study block' to your calendar for tomorrow at 7 PM." ``title`` is the
    event's title or the email's subject (said in quotes, cut to 60 characters; dropped when empty),
    ``when`` a spoken_when phrase (Calendar, Todo block, Move), ``answer`` an RSVP's yes / no / maybe.
    Never an error message, a body or a recipient: the card shows those."""
    column = outcome_column(status)
    if kind not in OUTCOME_KINDS:
        kind = "calendar"
    if column == _EXISTS and kind not in ("calendar", "todo"):
        column = _DONE
    if kind in _MAIL_KINDS:
        title = spoken_subject(title)
    if kind == "rsvp" and column == _DONE:
        pair = _RSVP_DONE.get(answer, _RSVP_DONE_OTHER)
    elif kind == "todo" and column == _DONE and not when:
        pair = _TODO_DONE_NO_WHEN
    else:
        pair = _OUTCOMES[(kind, column)]
    return _fill(pair, title, when, address)


def not_sent_text(kind: str, subject: str = "", address: str = "sir") -> str:
    """A Reply / Email that was not sent at the end of its countdown because the sending account
    changed meanwhile: "Sir, I didn't send 'Lunch Friday': the sending account changed. Please check
    From and try again." """
    return _fill(_NOT_SENT.get(kind, _NOT_SENT["email"]), spoken_subject(subject), "", address)


# --------------------------------------------------------------------------
# Ask, said aloud
# --------------------------------------------------------------------------

ACKS: tuple[str, ...] = ("Right away, {sir}.", "One moment, {sir}.", "On it, {sir}.", "Let me see, {sir}.")


def pick_ack(last: str = "", rng: random.Random | None = None) -> str:
    """One of ACKS (a template) at random, never ``last`` (the previous one)."""
    rng = rng or random.Random()
    return rng.choice([ack for ack in ACKS if ack != last] or list(ACKS))


class ReplySpeech(NamedTuple):
    text: str          # what is said ("" for nothing)
    guarded: bool      # the planner's words claimed something was done: a neutral sentence instead


def _end_sentence(text: str) -> str:
    text = text.strip()
    ended = text.rstrip("\"'\u201d\u2019)").endswith((".", "?", "!"))   # also: ... Friday?"
    return text if not text or ended else text + "."


QUESTION_LIMIT = 200       # the planner's question is at most this long (it is always said whole)
MAIL_SAY_LIMIT = 200       # after mail was read, at most this much of the say is spoken (a short summary)
# A passage in double quotes: after mail was read, a sentence quoting one is not said (it may be the
# email's own words); single quotes stay (titles: 'Project sync').
_QUOTED_RE = re.compile(r"[\"\u201c]([^\"\u201d]{12,})[\"\u201d]")
_QUOTE_MARK = "\x00"


def _without_quotes(text: str) -> str:
    """``text`` without the sentences that quote a passage in double quotes (it may hold several
    sentences of its own)."""
    def mark(match: re.Match[str]) -> str:
        inner = match.group(1).rstrip()
        return _QUOTE_MARK + ("." if inner.endswith((".", "?", "!")) else "")

    marked = _QUOTED_RE.sub(mark, text)
    return " ".join(piece for piece in sentences(marked) if _QUOTE_MARK not in piece)


def _flat_field(outcome: Any, name: str) -> str:
    return _end_sentence(" ".join(str(getattr(outcome, name, "") or "").split()))


def _read_mail(outcome: Any) -> bool:
    """The planner had email text (a thread named in the briefing, or a Gmail search) for this answer."""
    mail = getattr(outcome, "mail", None)
    try:
        return int(getattr(mail, "threads", 0) or 0) > 0 or int(getattr(mail, "messages", 0) or 0) > 0
    except (TypeError, ValueError):
        return True   # unknown: treated as read (shorter is safer)


NOTE_QUESTION = "I have a question for you, {sir}; it's on screen."
NOTE_FOUND = "Here's what I found, {sir}."


def ask_reply_note_speech(outcome: Any, address: str = "sir", *, notes: Sequence[str] = ()) -> str:
    """"" unless the planner's ``say`` or ``question`` repeats one of ``notes`` (the memory notes it
    was given; ``memory.note_spoken``: a whole note, 5 of its words in a row or most of its words in
    any order). Then neither is said, only "Two proposals are waiting for your OK, sir.", "I have a
    question for you, sir; it's on screen." or "Here's what I found, sir.", so a note never goes to
    the online read-aloud service as Jarvis's answer (the JARVIS tab shows the whole answer)."""
    say, question = _flat_field(outcome, "say"), _flat_field(outcome, "question")
    if not notes or not (say or question):
        return ""
    from .memory import note_spoken

    if not note_spoken(f"{say} {question}", notes):
        return ""
    count = sum(1 for card in (getattr(outcome, "cards", ()) or ()) if not getattr(card, "error", ""))
    return scrub_for_speech(approvals_sentence(count, address) if count else
                            with_address(NOTE_QUESTION if question else NOTE_FOUND, address))


def ask_reply_speech(outcome: Any, address: str = "sir") -> ReplySpeech:
    """What Jarvis says when an Ask answered: the planner's ``say`` and ``question`` (never Jarvis's
    note, the mail search or a card's text). With neither: "Two proposals are waiting for your OK,
    sir." (or that nothing could be proposed). If the say claims something was already done
    (``claims_done``), "I've lined up two proposals for your OK, sir." is said instead (``guarded``);
    a question that does is not said. (While memory notes were given, ``ask_reply_note_speech``
    comes first.)

    The question is always said whole (it is what the owner has to answer): the say gets what is
    left of the 400 characters, cut after a sentence. After mail was read the say is cut to
    ``MAIL_SAY_LIMIT`` and a sentence quoting a passage in double quotes is left out, so an email is
    never read aloud. Always through ``scrub_for_speech``; the JARVIS tab shows the full words."""
    say, question = _flat_field(outcome, "say"), _flat_field(outcome, "question")
    count = sum(1 for card in (getattr(outcome, "cards", ()) or ()) if not getattr(card, "error", ""))
    read_mail = _read_mail(outcome)
    guarded = False
    if question and claims_done(question):
        guarded, question = True, ""
    if say and read_mail:
        say = _without_quotes(say)
    if say and claims_done(say):
        guarded = True
        if count:
            noun = "proposal" if count == 1 else "proposals"
            say = with_address(f"I've lined up {count_words(count)} {noun} for your OK, {{sir}}.", address)
        else:
            say = with_address("Here's what I found, {sir}.", address)
    elif not say and not question:
        say = (approvals_sentence(count, address) if count
               else with_address("I couldn't find anything to propose for that, {sir}.", address))
    asked = scrub_for_speech(question, limit=QUESTION_LIMIT) if question else ""
    room = SPEECH_LIMIT - (len(asked) + 1 if asked else 0)
    if read_mail:
        room = min(room, MAIL_SAY_LIMIT)
    said = scrub_for_speech(say, limit=room) if say else ""
    return ReplySpeech(" ".join(part for part in (said, asked) if part), guarded)


_SOURCE_MARK_RE = re.compile(r"\s*\[\d{1,2}\]")
_FOUND_GUARDED = "Here's what I found, {sir}. The details are on screen."
_NO_WEB_ANSWER = "I couldn't find an answer to that on the web, {sir}."
_SOURCES_SHOWN = "The sources are on screen, {sir}."
_NO_SOURCE = "I couldn't find a source I could show you, {sir}."


def research_reply_speech(answer: str, *, sources: int, cards: int, address: str = "sir") -> ReplySpeech:
    """What Jarvis says when web research answered: the answer without its [n] source marks, then
    "The sources are on screen, sir." (or that no source could be shown), then how many suggestion
    cards wait for the OK (``cards`` counts the decidable suggestion cards only, never the source
    cards). An answer that claims Jarvis did something in his own words ("I've booked ...", "Done.":
    ``claims_done(first_person_only=True)``; a fact such as "The match was cancelled" is not one)
    is replaced by "Here's what I found, sir. The details are on screen." (``guarded``). The address
    is said once, in the first sentence that has a place for it; the answer gets what is left of
    SPEECH_LIMIT; everything goes through scrub_for_speech (no address, link or long id is said).
    The address word is Jarvis's own: it never went to the research."""
    flat = " ".join(_SOURCE_MARK_RE.sub("", str(answer or "")).split())
    guarded = bool(flat) and claims_done(flat, first_person_only=True)
    templates: list[str] = []
    if guarded:
        templates.append(_FOUND_GUARDED)
    elif not flat:
        templates.append(_NO_WEB_ANSWER)
    if sources > 0:
        if not guarded:
            templates.append(_SOURCES_SHOWN)
    elif flat:   # with no answer either, "I couldn't find an answer" says it all
        templates.append(_NO_SOURCE)
    said: list[str] = []
    used = False
    for template in templates:
        said.append(with_address(template, "" if used else address))
        used = used or "{sir}" in template or "{Sir}" in template
    approvals = approvals_sentence(cards) if cards > 0 else ""
    tail = " ".join(part for part in (*said, approvals) if part)
    if guarded or not flat:
        return ReplySpeech(scrub_for_speech(tail), guarded)
    room = SPEECH_LIMIT - len(tail) - 1
    spoken = scrub_for_speech(_end_sentence(flat), limit=max(room, 40))
    return ReplySpeech(" ".join(part for part in (spoken, tail) if part), False)


_SIGN_IN_AGAIN = "{Sir}, Claude Code needs you to sign in again. The details are under the bar."
_LIMIT_REACHED = "{Sir}, we've reached the Ask limit for now."
_FAILURE_SPEECH = {
    "timeout": "Sorry, {sir} - that took too long. Nothing was proposed.",
    "not_signed_in": _SIGN_IN_AGAIN,
    "not_subscription": _SIGN_IN_AGAIN,
    "caps": _LIMIT_REACHED,
    "limit": _LIMIT_REACHED,
    "research_cap": "Sorry, {sir} - that needed more searching than I allow. Nothing was proposed.",
    "research_off": "{Sir}, web research is turned off.",
    "research_limit": "{Sir}, we've reached the web research limit for now.",
}
_SILENT_FAILURES = ("cancelled", "empty", "busy")
_OTHER_FAILURE = "Sorry, {sir} - something went wrong. Nothing was proposed."


def ask_failure_speech(kind: str, address: str = "sir") -> str:
    """What Jarvis says when an Ask failed (the details are under the bar); "" for a cancelled, empty
    or busy one (the owner did that himself)."""
    if kind in _SILENT_FAILURES:
        return ""
    return with_address(_FAILURE_SPEECH.get(kind, _OTHER_FAILURE), address)


# --------------------------------------------------------------------------
# Memory (the productivity bundle): entries in the JARVIS tab and the few words said aloud
# --------------------------------------------------------------------------

MEMORY_SAVED_SPEECH = "Noted, {sir}."
MEMORY_FORGOTTEN_SPEECH = "Forgotten, {sir}."
MEMORY_REFUSED_SPEECH = "I'd rather not keep that, {sir}."
# Spoken refusals: the ones whose entry says "I'd rather not keep ..." (a long, duplicate, full or
# read-only note is only shown).
SPOKEN_REFUSALS = ("empty", "secret", "digits")
_REFUSED_SECRET = "I'd rather not keep passwords, codes or account numbers, {sir}; nothing was saved."
_MEMORY_REFUSALS = {
    "empty": _REFUSED_SECRET,
    "secret": _REFUSED_SECRET,
    "digits": _REFUSED_SECRET,
    "long": "That's longer than I keep ({cap} characters), {sir}; nothing was saved.",
    "full": "Your memory is full ({max_notes} notes), {sir} - delete some under MEMORY first.",
    "read_only": "memory.json can't be read, so I won't change it, {sir}.",
    "duplicate": "I already have that, {sir}: \"{kept}\"",
    "gone": "That note is no longer in MEMORY, {sir}.",
}


def memory_saved_speech(address: str = "sir") -> str:
    """"Noted, sir." after a note was saved (never the note)."""
    return with_address(MEMORY_SAVED_SPEECH, address)


def memory_forgotten_speech(address: str = "sir") -> str:
    """"Forgotten, sir." after "forget that ..." removed one note (never the note)."""
    return with_address(MEMORY_FORGOTTEN_SPEECH, address)


def memory_refused_speech(address: str = "sir") -> str:
    """"I'd rather not keep that, sir." for a note that looks like a secret (never the note)."""
    return with_address(MEMORY_REFUSED_SPEECH, address)


def quoted_note(note: str) -> str:
    """A note in quotes as an entry shows it, ending its sentence: "Ana is my project lead." (a note
    that already ends with . ! or ? keeps its own mark). Shown only; never spoken or logged."""
    text = " ".join(str(note or "").split())
    return f"\"{text}\"" if text.endswith((".", "!", "?")) else f"\"{text}.\""


def memory_saved_text(note: str, address: str = "sir") -> str:
    """The JARVIS entry after "remember that ...": 'Noted, sir: "Ana is my project lead."'."""
    return with_address("Noted, {sir}:", address) + " " + quoted_note(note)


def memory_forgotten_text(note: str, address: str = "sir") -> str:
    """The JARVIS entry after "forget that ..." removed one note."""
    return with_address("Forgotten, {sir}:", address) + " " + quoted_note(note)


def memory_refusal_text(reason: str, address: str = "sir", *, kept: str = "", cap: int = 200,
                        max_notes: int = 100) -> str:
    """Why a note was not kept (memory.REFUSE_*): shown under MEMORY's Add field or as a JARVIS entry."""
    template = _MEMORY_REFUSALS.get(reason, _REFUSED_SECRET)
    text = with_address(template, address)
    return text.replace("{cap}", str(cap)).replace("{max_notes}", str(max_notes)).replace(
        "{kept}", " ".join(str(kept or "").split()))


def memory_suggestion_text(text: str, address: str = "sir") -> str:
    """'Shall I remember this, sir? "Ana Lima organizes the weekly Project sync."' (never spoken)."""
    return with_address("Shall I remember this, {sir}?", address) + " " + quoted_note(text)


# --------------------------------------------------------------------------
# The day plan (the productivity bundle): entries in the JARVIS tab and the few words said aloud.
# Spoken sentences are built from counts and times only: never a block's title or the planner's say.
# --------------------------------------------------------------------------

PLAN_LATE_TEXT = "It's late for today, {sir} - here's a plan for tomorrow:"   # a late answer's first line
PLAN_LATE_LOOKED = "It's late for today, so I looked at tomorrow."    # before a late answer with no block
PLAN_ANSWER_TAIL = "Each block waits for your OK under NEEDS YOUR OK."
PLAN_NO_BLOCK_TEXT = "No block fitted (see the cards)."
PLAN_ANSWER_HINT = 'Answer with "plan my day, <your answer>".'      # today's; plan_answer_hint(label)
PLAN_QUESTION_SPEECH = "I have a question before I plan, {sir}; it's on screen."
PLAN_NO_CALENDAR_TEXT = "Plan my day needs your personal calendar - click Connect under TODAY."
PLAN_NO_RUN_KINDS = ("day_full", "nothing", "part_over", "no_hours", "no_calendar")
# A no-run reason in a few words (ACTIVITY's row, the LIVE task's end): never the raw reason word.
_PLAN_NO_RUN_WORDS = {"day_full": "day full", "nothing": "nothing needs time",
                      "part_over": "that part of the day is over", "no_hours": "outside the planned hours",
                      "no_calendar": "no personal calendar"}
# What a plan is for, said in a sentence: the window's label ("today", "tomorrow", "this morning" ...).
_PLAN_WHOSE = {"today": "Your day", "tomorrow": "Tomorrow", "this morning": "Your morning",
               "this afternoon": "Your afternoon", "this evening": "Your evening"}
_PLAN_TYPED = {"tomorrow": "plan tomorrow", "this morning": "plan my morning",
               "this afternoon": "plan my afternoon", "this evening": "plan my evening"}


def plan_no_run_text(kind: str, address: str = "sir", *, label: str = "today", part: str = "",
                     late: bool = False, hint_tomorrow: bool = False) -> str:
    """Why a day plan did not run (no planner run): ``day_full`` (no free stretch of half an hour;
    ``hint_tomorrow`` for a today plan that is not late adds 'Type "plan tomorrow" for tomorrow.',
    R30), ``nothing`` (nothing needs time; how to name what to fit in, R52), ``part_over``,
    ``no_hours``, ``no_calendar``. ``late``: "plan my day" switched to tomorrow (PLAN_LATE_LOOKED
    first: no plan is promised, and the address is said once)."""
    whose = _PLAN_WHOSE.get(label, "Your day")
    typed = _PLAN_TYPED.get(label, "plan my day")
    if kind == "day_full":
        text = f"{whose} is full, {{sir}} - there's no free stretch of half an hour left."
        if hint_tomorrow:
            text += ' Type "plan tomorrow" for tomorrow.'
    elif kind == "nothing":
        text = (f"Nothing on your list needs time {label}, {{sir}}. Tell me what to fit in: "
                f'"{typed}, gym and reading".')
    elif kind == "part_over":
        text = f'Your {part or "morning"} is over, {{sir}}; type "plan my day" for the rest of it.'
    elif kind == "no_hours":
        text = (f"{whose} is outside the hours I plan, {{sir}} ([productivity] day_start and day_end in "
                "config.toml).")
    else:
        text = PLAN_NO_CALENDAR_TEXT
    if late and kind in ("day_full", "nothing"):
        text = f"{PLAN_LATE_LOOKED} {text}"
    return with_address(text, address)


def plan_no_run_words(kind: str) -> str:
    """"day full", "nothing needs time", "that part of the day is over", "outside the planned
    hours", "no personal calendar" (a plan that did not run, in ACTIVITY and LIVE)."""
    return _PLAN_NO_RUN_WORDS.get(kind, str(kind).replace("_", " "))


def plan_answer_hint(label: str = "today") -> str:
    """How to answer a plan's question (R46), for the window asked about: 'Answer with "plan
    tomorrow, <your answer>".' for tomorrow (following it plans the same window again)."""
    return f'Answer with "{_PLAN_TYPED.get(label, "plan my day")}, <your answer>".'


def plan_answer_text(blocks: Sequence[str], say: str, question: str = "", address: str = "sir", *,
                     label: str = "today", late: bool = False, refused: int = 0) -> str:
    """The plan's JARVIS entry (on screen only): "Here's a plan for today:" (late: "It's late for
    today, sir - here's a plan for tomorrow:"), one line per valid block ("10:30-12:00  Work on: Lab
    report"), the planner's say, "Each block waits for your OK under NEEDS YOUR OK."; a question and
    no block: the say, "Question: ..." and how to answer for that window (R46); every block refused:
    the say and "No block fitted (see the cards)."; no block when late: PLAN_LATE_LOOKED first."""
    parts: list[str] = []
    say = " ".join(str(say or "").split())
    question = " ".join(str(question or "").split())
    if blocks:
        parts.append(with_address(PLAN_LATE_TEXT, address) if late else f"Here's a plan for {label}:")
        parts += list(blocks)
        if say:
            parts.append(say)
        if question:
            parts.append(f"Question: {question}")
        parts.append(PLAN_ANSWER_TAIL)
    else:
        if late:
            parts.append(PLAN_LATE_LOOKED)
        if say:
            parts.append(say)
        if question:
            parts += [f"Question: {question}", plan_answer_hint(label)]
        elif refused:
            parts.append(PLAN_NO_BLOCK_TEXT)
        elif not say:
            parts.append(with_address(f"I couldn't fit a block into {label}, {{sir}}.", address))
    return "\n".join(parts)


def plan_result_speech(count: int, first_start: datetime | None, label: str = "today", address: str = "sir") -> str:
    """"I've lined up four blocks for today, the first at 10:30 AM. Each one waits for your OK,
    sir." (one block: "..., at 10:30 AM. It waits for your OK, sir."); none: "I couldn't fit a block
    into today, sir." Counts and times only."""
    if count <= 0 or first_start is None:
        return with_address(f"I couldn't fit a block into {label}, {{sir}}.", address)
    if count == 1:
        return with_address(f"I've lined up one block for {label}, at {spoken_clock(first_start)}. "
                            "It waits for your OK, {sir}.", address)
    return with_address(f"I've lined up {count_words(count)} blocks for {label}, the first at "
                        f"{spoken_clock(first_start)}. Each one waits for your OK, {{sir}}.", address)


def plan_question_speech(address: str = "sir") -> str:
    """"I have a question before I plan, sir; it's on screen." (a plan answer with no block, R46)."""
    return with_address(PLAN_QUESTION_SPEECH, address)


def plan_no_run_speech(reason: str, address: str = "sir", label: str = "today") -> str:
    """"Your day is full, sir." / "Nothing on your list needs time today, sir."; "" for the other
    reasons (the entry says them)."""
    if reason == "day_full":
        return with_address(f"{_PLAN_WHOSE.get(label, 'Your day')} is full, {{sir}}.", address)
    if reason == "nothing":
        return with_address(f"Nothing on your list needs time {label}, {{sir}}.", address)
    return ""


# --------------------------------------------------------------------------
# Meeting prep (the productivity bundle): entries in the JARVIS tab and the few words said aloud.
# Spoken sentences are built from the meeting's time only: never its title, a guest, a subject or
# the brief (the brief is on screen only).
# --------------------------------------------------------------------------

PREP_NO_MATCH_TEXT = "I don't see a meeting like that today or tomorrow, {sir}."
PREP_GONE_TEXT = "That meeting is no longer on your calendar, {sir}."
PREP_ENDED_TEXT = "That meeting has ended, {sir}."
PREP_NO_CALENDAR_TEXT = "I couldn't read your calendar just now, {sir}; nothing else was read."
PREP_CONNECT_TEXT = "Meeting prep needs your calendar - click Connect under TODAY."
PREP_CHOOSE_TEXT = "Which meeting shall I prepare, {sir}? Nothing is read until you pick one."
PREP_CHOOSE_ONE_TEXT = "Is this the meeting you mean, {sir}? Nothing is read until you click."
PREP_CHOOSE_SPEECH = "Which meeting, {sir}? The choices are on screen."
PREP_CHOOSE_ONE_SPEECH = "Is this the meeting you mean, {sir}? It's on screen."
PREP_READY_STATUS = "Your brief is in the JARVIS tab."
PREP_NO_RUN_STATUS = "Your brief is in the JARVIS tab (no planner run)."
PREP_THIS_ONE = "Prep this one"
# A no-run reason in a few words (ACTIVITY's row, the LIVE task's end): never the raw reason word.
_PREP_NO_RUN_WORDS = {"no_match": "no such meeting", "gone": "the meeting is gone", "ended": "the meeting has ended",
                      "no_calendar": "the calendar could not be read", "choices": "which meeting?",
                      "no_thread": "no recent thread", "mail_off": "mail reading is off",
                      "unreadable": "mail can't be read now", "caps": "the limits",
                      "not_ready": "Claude Code not ready"}
_PREP_NO_RUN_TEXTS = {"no_match": PREP_NO_MATCH_TEXT, "gone": PREP_GONE_TEXT, "ended": PREP_ENDED_TEXT,
                      "no_calendar": PREP_NO_CALENDAR_TEXT}


def prep_no_run_text(kind: str, address: str = "sir", *, connect: bool = False) -> str:
    """Why a meeting brief did not start: no such meeting today or tomorrow, the meeting is gone or
    ended, or the calendar could not be read (``connect``: none is signed in; nothing was read from
    mail; no planner run)."""
    if kind == "no_calendar" and connect:
        return PREP_CONNECT_TEXT
    return with_address(_PREP_NO_RUN_TEXTS.get(kind, PREP_NO_MATCH_TEXT), address)


def prep_no_run_words(kind: str) -> str:
    """"no such meeting", "no recent thread", ... (ACTIVITY and LIVE; never the reason word)."""
    return _PREP_NO_RUN_WORDS.get(kind, str(kind).replace("_", " "))


def prep_no_run_speech(kind: str, address: str = "sir", *, choices: int = 0) -> str:
    """What is said when a brief did not start: the same title-free sentence as the entry, or for
    the choices "Which meeting, sir? The choices are on screen." (one: "Is this the meeting you
    mean, sir? It's on screen.")."""
    if kind == "choices":
        return with_address(PREP_CHOOSE_ONE_SPEECH if choices == 1 else PREP_CHOOSE_SPEECH, address)
    return prep_no_run_text(kind, address)


def prep_choices_text(count: int, address: str = "sir") -> str:
    """The entry above the choices: "Which meeting shall I prepare, sir? ..." (one: "Is this the
    meeting you mean, sir? ...", R5)."""
    return with_address(PREP_CHOOSE_ONE_TEXT if count == 1 else PREP_CHOOSE_TEXT, address)


def prep_choice_text(title: str, start: datetime, alias: str, *, day: str = "today", hour24: bool = False) -> str:
    """One choice: "Project sync at 2:00 PM (work)", ", tomorrow" when it is tomorrow (on screen only)."""
    if hour24:
        at = f"{start.hour:02d}:{start.minute:02d}"
    else:
        at = f"{start.hour % 12 or 12}:{start.minute:02d} {'AM' if start.hour < 12 else 'PM'}"
    shown = " ".join(str(title or "").split()) or "(No title)"
    text = f"{shown} at {at} ({alias})" if alias else f"{shown} at {at}"
    return text + (f", {day}" if day and day != "today" else "")


def prep_result_speech(start: datetime | None, address: str = "sir", *, in_jarvis_tab: bool = True,
                       day: str = "today") -> str:
    """"Your brief for the 2 PM meeting is ready, sir." (JARVIS current, or the window minimized) /
    "Your brief for the 2 PM meeting is in the JARVIS tab, sir." (another tab stays current, R33);
    tomorrow's meeting: "Your brief for tomorrow's 9 AM meeting ...". The time only, never the brief."""
    if start is None:
        meeting = "the meeting"
    elif day == "tomorrow":
        meeting = f"tomorrow's {spoken_clock(start)} meeting"
    else:
        meeting = f"the {spoken_clock(start)} meeting"
    where = "is ready" if in_jarvis_tab else "is in the JARVIS tab"
    return with_address(f"Your brief for {meeting} {where}, {{sir}}.", address)


# --------------------------------------------------------------------------
# Offers (the productivity bundle, spec 4 and 9.3): the plan offer after the AM briefing and the prep
# offer before a meeting with guests. Each is one entry with one link and at most one spoken
# sentence built from times and counts only: never a title, a guest or anything read.
# --------------------------------------------------------------------------

DOT = "\u00b7"                 # the middle dot of the entries' sub lines (shown; the source stays ASCII)
PLAN_OFFER_LINK = "Plan my day"
PREP_OFFER_LINK = "Prepare brief"
PLAN_OFFER_TEXT = ("Shall I plan your day, {sir}? I'd line up time blocks around your meetings for your to-dos "
                   "and deadlines. Nothing goes on your calendar until you approve each block.")
PLAN_OFFER_SPEECH = "Shall I plan your day, {sir}?"
PLAN_OFFER_WORDS = "Plan my day (offered after the AM briefing)"   # "You clicked" in LIVE and the YOU entry
PREP_OFFER_TAIL = "Shall I prepare a brief? I'd read your latest thread with each guest and send them to your Claude."
PREP_OFFER_SPEECH = "Your meeting at {at} starts in about {minutes}, {sir}. Shall I prepare a brief?"
PREP_OFFERS_SPEECH = "You have meetings at {ats}, {sir}. Shall I prepare briefs?"
PLAN_COST_SUB = "1 planner run"
PREP_COST_SUB = "1 planner run (none without recent mail)"
PROD_HINT_TEXT = ("Prep offers and follow-ups work while I'm open or minimized, {sir} - minimize instead of Done to "
                  "keep them.")
DONE_PREP_TOOLTIP = "Closes Jarvis - meeting prep offers stop until you open him again"


def clock_text(moment: datetime, hour24: bool = False) -> str:
    """"2:00 PM" (or "14:00" with the 24-hour clock) for the entries on screen."""
    if hour24:
        return f"{moment.hour:02d}:{moment.minute:02d}"
    return f"{moment.hour % 12 or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def _minutes_words(minutes: int) -> str:
    """"one minute", "two minutes", "15 minutes" (number words up to ten)."""
    count = max(1, int(minutes))
    number = _NUMBER_WORDS[count] if count <= 10 else str(count)
    return f"{number} minute{'' if count == 1 else 's'}"


def _joined(parts: Sequence[str]) -> str:
    items = list(parts)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def plan_offer_text(address: str = "sir") -> str:
    """The plan offer's entry (spec 9.3)."""
    return with_address(PLAN_OFFER_TEXT, address)


def plan_offer_speech(address: str = "sir") -> str:
    """"Shall I plan your day, sir?" (said once the AM briefing was heard or viewed, R26)."""
    return with_address(PLAN_OFFER_SPEECH, address)


def plan_offer_expired_text(posted_at: datetime, hour24: bool = False) -> str:
    """'Offered at 10:14 AM - expired. Type "plan my day" any time.'"""
    return f'Offered at {clock_text(posted_at, hour24)} - expired. Type "plan my day" any time.'


def plan_offer_planned_text(at: datetime, hour24: bool = False) -> str:
    """"You planned your day at 10:31 AM." (the offer retired by a plan)."""
    return f"You planned your day at {clock_text(at, hour24)}."


def prep_offer_text(title: str, start: datetime, alias: str, minutes: int, guests: int, address: str = "sir",
                    *, hour24: bool = False) -> str:
    """"Project sync at 2:00 PM (work) starts in 15 minutes, with 3 guests. Shall I prepare a brief? I'd
    read your latest thread with each guest and send them to your Claude." (on screen only)."""
    shown = " ".join(str(title or "").split()) or "(No title)"
    where = f" ({alias})" if alias else ""
    count = max(0, int(guests))
    left = int(minutes)   # digits on screen (number words are for speech only)
    starts = f"starts in {left} minute{'' if left == 1 else 's'}" if left > 0 else "starts now"
    return (f"{shown} at {clock_text(start, hour24)}{where} {starts}, with {count} guest{'' if count == 1 else 's'}. "
            + with_address(PREP_OFFER_TAIL, address))


def prep_offer_speech(start: datetime, minutes: int, address: str = "sir") -> str:
    """"Your meeting at 2 PM starts in about 15 minutes, sir. Shall I prepare a brief?" (time only)."""
    text = PREP_OFFER_SPEECH.replace("{at}", spoken_clock(start)).replace("{minutes}", _minutes_words(minutes))
    return with_address(text, address)


def prep_offers_speech(starts: Sequence[datetime], address: str = "sir") -> str:
    """"You have meetings at 1 PM and 2 PM, sir. Shall I prepare briefs?" (two or three back-to-back
    meetings offered together, R51)."""
    times = [spoken_clock(start) for start in starts]
    if len(times) <= 1:
        return prep_offer_speech(starts[0], 15, address) if times else ""
    return with_address(PREP_OFFERS_SPEECH.replace("{ats}", _joined(times)), address)


def prep_offer_ended_text(title: str, start: datetime, hour24: bool = False) -> str:
    """"Project sync at 2:00 PM has ended." (the offer's link is gone)."""
    shown = " ".join(str(title or "").split()) or "(No title)"
    return f"{shown} at {clock_text(start, hour24)} has ended."


def brief_ready_text(at: datetime, hour24: bool = False) -> str:
    """"Brief ready below (1:47 PM)." (a prep offer after its brief, R50)."""
    return f"Brief ready below ({clock_text(at, hour24)})."


def offer_cost_sub(left_bundle: int, max_runs: int, left_day: int, *, prep: bool = False, as_of: str = "") -> str:
    """The offer's sub line (R49): "1 planner run . 17 of 20 left today"; when the Ask day count is
    the tighter limit "1 planner run . 12 Ask runs left today"; a prep offer says "(none without
    recent mail)" and "as of 1:40 PM" (the schedule's read time)."""
    parts = [PREP_COST_SUB if prep else PLAN_COST_SUB]
    if left_day < left_bundle:
        parts.append(f"{max(0, int(left_day))} Ask run{'' if left_day == 1 else 's'} left today")
    else:
        parts.append(f"{max(0, int(left_bundle))} of {int(max_runs)} left today")
    if as_of:
        parts.append(f"as of {as_of}")
    return f" {DOT} ".join(parts)


def prod_hint_text(address: str = "sir") -> str:
    """The one-time "minimize instead of Done" entry (R34; never spoken)."""
    return with_address(PROD_HINT_TEXT, address)
