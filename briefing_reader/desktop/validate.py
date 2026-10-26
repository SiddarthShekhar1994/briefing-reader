"""The desktop planner's plan, checked (SPEC sections 2.1, 4.4 and 4.5, hard rules 2, 3, 6, 8 and 9; no Qt).

Claude only plans: its answer (ask.desktop_mode.plan_from reads it strictly into a ``DesktopAnswer`` of
``PlannedStep``s) names things by the refs this run issued (refs.RefIndex), never by a path, a program or an address.
``check_plan`` decides, step by step, what Jarvis would do with it - Jarvis's own code, never Claude's text, decides
the tier - and the planner's CLI schema is not trusted: this is the authority (no jsonschema library).

The checks, in this order; the first problem wins and its code goes to Live (``REASON_WORDS`` says it in words):

1. Shape: a known skill (any phase), only that skill's fields, its required fields, the enums and ranges
   (``SHAPE``); a skill's own item caps (``CAP``: move 200, rename 50, zip 1000 files).
2. Every ref was issued in THIS run's index, with the right kind (``UNKNOWN_REF``): a path, an id this index never
   issued, another run's id or a new folder (n1) no earlier passing make_folder made.
3. No duplicate files, no move into the same place, no repeated new name, no item opened twice (``SHAPE``).
4. New names (rename, a new folder, a zip): paths.name_problem (NFC, no control or hidden character, none of
   <>:"/\\|?*, no reserved device name, no trailing dot or space, 1-120 characters) and the extension kept unless
   the owner's words ask for another (``NAME_INVALID``); never a blocked type (``BLOCKED_TYPE``).
5. Typed values (fill_fields) word for word in the owner's words (``INTENT_NOT_LICENSED``) and plain text
   (``SHAPE``); a web address https on a public host (guard.url_problem: ``SHAPE`` for another scheme,
   ``SITE_NOT_APPROVED`` for a host that is not public).
6. The tier (INSTANT -> ONE_CLICK -> CARD -> NEVER), from the skill and its RESOLVED targets; checks only promote it.
   The NEVER list (guard; again in the executor right before each OS call) refuses: a secret-shaped file or a place
   outside the scope (``EXCLUDED`` / ``PATH_OUTSIDE``: paths.check_path when the caller gives ``path_check``), a
   blocked or not-openable type (``BLOCKED_TYPE``), a never-open app (``APP_NOT_APPROVED``), a sensitive field
   (``SENSITIVE_FIELD``), a payment page (``PAYMENT_PAGE``). Phase 1's tiers (SPEC 2.1, owner decision D5):
   find_files, show_in_folder and open_folder INSTANT; open_file INSTANT for a document (2.3), CARD for one that can
   run macros or scripts and for a shortcut (its real target shown first), ONE_CLICK when its name has hidden
   characters; open_app INSTANT when the owner named the app exactly, else ONE_CLICK; open_url INSTANT for a saved
   site or an address the owner typed word for word, else ONE_CLICK (its host shown). An online-only file is
   flagged, not promoted (D5: files open at once).
7. Intent licensing (``licensed_skills``: SPEC 4.5's table, deterministic, on the owner's typed words only - never a
   name, a title or Claude's text; "tidy my Downloads" can never license recycle_files) (``INTENT_NOT_LICENSED``)
   and scope licensing: what a step works on - its files, the place it looks in or opens - lies under the places
   the owner's words named to look in ("in my LING folder", "from Downloads", "the Uni folder"), when he named any
   (``SCOPE``). Where things go is no scope: "into LING" names a destination, and a move's ``to`` (or the place a
   new folder is made in) is not checked against the places named.
8. The suspect rule: a step touching an item whose name reads like instructions (refs.Ref.suspect, guard's
   scanner) is refused unless the owner's own words name that item (``SUSPECT_ITEM``).
9. The phase gate: a skill not shipped yet is refused (``NOT_YET``).

Then the rate caps over the steps that passed, in order: at most ``MAX_OPENS`` opens and ``MAX_URLS`` web pages a
request (``CAP``); a refused step uses none.

Public API
==========
Tiers ``INSTANT``, ``ONE_CLICK``, ``CARD``, ``NEVER`` (``TIERS``), ``promote(tier, to)``. Codes ``UNKNOWN_REF`` ...
``CAP`` (``REFUSAL_CODES``, ``NEVER_CODES``, ``REASON_WORDS``). Skills ``FIND_FILES`` ... ``FILL_FIELDS``
(``SKILL_PHASES``, ``PHASE_1_SKILLS``, ``SHIPPED_PHASE``, ``SKILL_FIELDS``, ``STEP_KEYS``, ``OPEN_SKILLS``,
``MAX_OPENS``, ``MAX_URLS``). Flags ``FLAG_*`` (why a tier went up). ``PlannedStep``, ``DesktopAnswer`` (the
planner's answer as read), ``CheckedStep``, ``DesktopPlan`` (``accepted()``, ``refused()``, ``counts()``,
``skills_text()``, ``codes_text()``), ``check_plan(answer, refs, owner_words, run_id=, path_check=)``,
``licensed_skills(words)``, ``names_item(words, label)``, ``named_exactly(words, label)``, ``typed_url(words, url)``.

Pure: nothing is logged, opened or read here, and every repr names skills, codes and counts only.
"""

from __future__ import annotations

import ntpath
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import guard, paths
from .context import AppItem, FileItem, Place, SiteItem
from .refs import APP, CELL, FIELD, FILE, NEW, PLACE, SITE, WINDOW, Ref, RefIndex, parse_ref

# --------------------------------------------------------------------------
# Tiers and refusal codes
# --------------------------------------------------------------------------

INSTANT, ONE_CLICK, CARD, NEVER = "instant", "one_click", "card", "never"
TIERS = (INSTANT, ONE_CLICK, CARD, NEVER)

UNKNOWN_REF = "unknown_ref"
SHAPE = "shape"
PATH_OUTSIDE = paths.REFUSE_OUTSIDE              # "path_outside"
EXCLUDED = paths.REFUSE_EXCLUDED                 # "excluded"
BLOCKED_TYPE = paths.REFUSE_TYPE                 # "blocked_type"
APP_NOT_APPROVED = "app_not_approved"
SITE_NOT_APPROVED = "site_not_approved"
INTENT_NOT_LICENSED = "intent_not_licensed"
SCOPE = "scope"
SUSPECT_ITEM = "suspect_item"
NAME_INVALID = paths.REFUSE_NAME                 # "name_invalid"
SENSITIVE_FIELD = "sensitive_field"
PAYMENT_PAGE = "payment_page"
NOT_YET = "not_yet"
CAP = "cap"
REFUSAL_CODES = (UNKNOWN_REF, SHAPE, PATH_OUTSIDE, EXCLUDED, BLOCKED_TYPE, APP_NOT_APPROVED, SITE_NOT_APPROVED,
                 INTENT_NOT_LICENSED, SCOPE, SUSPECT_ITEM, NAME_INVALID, SENSITIVE_FIELD, PAYMENT_PAGE, NOT_YET, CAP)
# The NEVER list's codes: such a step's tier is NEVER (refused by the validator and again by the executor).
NEVER_CODES = frozenset({PATH_OUTSIDE, EXCLUDED, BLOCKED_TYPE, APP_NOT_APPROVED, SITE_NOT_APPROVED, SENSITIVE_FIELD,
                         PAYMENT_PAGE})
# Each code in Jarvis's own words (Live; never a name, a title or a path).
REASON_WORDS = {
    UNKNOWN_REF: "it named something Jarvis didn't give the planner",
    SHAPE: "it isn't a step Jarvis can carry out as written",
    PATH_OUTSIDE: "it's outside the folders you allow",
    EXCLUDED: "it's in a place Jarvis never touches",
    BLOCKED_TYPE: "Jarvis never opens that type of file",
    APP_NOT_APPROVED: "it's an app Jarvis never opens",
    SITE_NOT_APPROVED: "it isn't an ordinary https web address",
    INTENT_NOT_LICENSED: "you didn't ask for that",
    SCOPE: "it's outside the places you named",
    SUSPECT_ITEM: "its name reads like instructions and you didn't name it",
    NAME_INVALID: "that new name isn't allowed",
    SENSITIVE_FIELD: "Jarvis never fills that kind of box",
    PAYMENT_PAGE: "Jarvis never works on payment pages",
    NOT_YET: "Jarvis can't do that yet",
    CAP: "it's more than one request may open",
}

# Why a step's tier went up (a card row's words come from these later).
FLAG_SCRIPT = "script"                 # a document that can run macros or scripts: a card click first
FLAG_SHORTCUT = "shortcut"             # a .lnk: its real target is shown on a card first
FLAG_HIDDEN_CHARS = "hidden_chars"     # the name has hidden characters: shown as <U+202E> before it opens
FLAG_NOT_NAMED = "not_named"           # an app the owner did not name exactly: one click
FLAG_SUGGESTED = "suggested"           # a web address the owner did not type: one click, its host shown
FLAG_ONLINE_ONLY = "online_only"       # opening it downloads it from OneDrive (flagged only: D5)


def promote(tier: str, to: str) -> str:
    """``tier`` raised to ``to`` when ``to`` is higher; never lowered (hard rule 3)."""
    return to if TIERS.index(to) > TIERS.index(tier) else tier


# --------------------------------------------------------------------------
# Skills
# --------------------------------------------------------------------------

FIND_FILES, OPEN_FILE, SHOW_IN_FOLDER, OPEN_FOLDER, OPEN_APP, OPEN_URL = (
    "find_files", "open_file", "show_in_folder", "open_folder", "open_app", "open_url")
MOVE_FILES, COPY_FILES, RENAME_FILE, MAKE_FOLDER, ZIP_FILES, RECYCLE_FILES = (
    "move_files", "copy_files", "rename_file", "make_folder", "zip_files", "recycle_files")
VOLUME, MEDIA, TIMER, CANCEL_TIMER, SCREENSHOT, FOCUS_MODE = (
    "volume", "media", "timer", "cancel_timer", "screenshot", "focus_mode")
FILL_FIELDS = "fill_fields"
SKILL_PHASES = {FIND_FILES: 1, OPEN_FILE: 1, SHOW_IN_FOLDER: 1, OPEN_FOLDER: 1, OPEN_APP: 1, OPEN_URL: 1,
                MOVE_FILES: 2, COPY_FILES: 2, RENAME_FILE: 2, MAKE_FOLDER: 2, ZIP_FILES: 2, RECYCLE_FILES: 2,
                VOLUME: 2, MEDIA: 2, TIMER: 2, CANCEL_TIMER: 2, SCREENSHOT: 2, FOCUS_MODE: 2, FILL_FIELDS: 3}
PHASE_1_SKILLS = tuple(skill for skill, phase in SKILL_PHASES.items() if phase == 1)
SHIPPED_PHASE = 1                      # skills of a later phase are refused NOT_YET
# Every step key of SPEC 4.3's outline (any phase): plan_from refuses the whole answer for any other key.
STEP_KEYS = ("skill", "why", "files", "place", "to", "id", "name", "query", "kind", "within_days", "app", "site",
             "url", "action", "level", "step", "minutes", "label", "window", "fields")
FIELD_KEYS = ("field", "from", "typed")
# Each skill's own fields (besides "skill" and "why").
SKILL_FIELDS: dict[str, frozenset[str]] = {
    FIND_FILES: frozenset({"query", "place", "kind", "within_days"}),
    OPEN_FILE: frozenset({"files"}), SHOW_IN_FOLDER: frozenset({"files"}), OPEN_FOLDER: frozenset({"place"}),
    OPEN_APP: frozenset({"app"}), OPEN_URL: frozenset({"url", "site"}),
    MOVE_FILES: frozenset({"files", "to"}), COPY_FILES: frozenset({"files", "to"}),
    RENAME_FILE: frozenset({"files", "name"}), MAKE_FOLDER: frozenset({"id", "place", "name"}),
    ZIP_FILES: frozenset({"files", "to", "name"}), RECYCLE_FILES: frozenset({"files"}),
    VOLUME: frozenset({"action", "level", "step"}), MEDIA: frozenset({"action"}),
    TIMER: frozenset({"minutes", "label"}), CANCEL_TIMER: frozenset(), SCREENSHOT: frozenset(),
    FOCUS_MODE: frozenset({"minutes"}), FILL_FIELDS: frozenset({"window", "fields"}),
}
FIND_KINDS = ("document", "picture", "music", "video", "folder", "any")
VOLUME_ACTIONS = ("set", "up", "down", "mute", "unmute")
MEDIA_ACTIONS = ("play", "pause", "next", "previous")
SKILL_CAPS = {MOVE_FILES: 200, COPY_FILES: 200, RENAME_FILE: 50, ZIP_FILES: 1000, RECYCLE_FILES: 50}
MAX_FIELDS = 20
MAX_OPENS, MAX_URLS = 5, 3             # a request's rate caps (SPEC 2.1)
OPEN_SKILLS = frozenset({OPEN_FILE, SHOW_IN_FOLDER, OPEN_FOLDER, OPEN_APP, OPEN_URL})
_FILE_SKILLS = frozenset({OPEN_FILE, SHOW_IN_FOLDER, MOVE_FILES, COPY_FILES, RENAME_FILE, ZIP_FILES, RECYCLE_FILES})
_ONE_FILE = frozenset({OPEN_FILE, SHOW_IN_FOLDER})
_CARD_SKILLS = frozenset({MOVE_FILES, COPY_FILES, RENAME_FILE, MAKE_FOLDER, ZIP_FILES, RECYCLE_FILES, FILL_FIELDS})


# --------------------------------------------------------------------------
# The planner's answer, as read (ask.desktop_mode.plan_from makes these)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PlannedStep:
    """One step as the planner wrote it, its JSON types already checked (plan_from): ``keys`` are the keys it had."""

    skill: str
    why: str = field(default="", repr=False)
    keys: frozenset[str] = frozenset()
    files: tuple[str, ...] = ()
    place: str = ""
    to: str = ""
    id: str = ""
    name: str = field(default="", repr=False)
    query: str = field(default="", repr=False)
    kind: str = ""
    within_days: int | None = None
    app: str = ""
    site: str = ""
    url: str = field(default="", repr=False)
    action: str = ""
    level: int | None = None
    step: int | None = None
    minutes: int | None = None
    label: str = field(default="", repr=False)
    window: str = ""
    fields: tuple[tuple[str, str, str], ...] = field(default=(), repr=False)    # (field, from, typed)

    def __repr__(self) -> str:
        return f"PlannedStep({self.skill})"


@dataclass(frozen=True)
class DesktopAnswer:
    """The planner's answer (shown in Live as the planner's; never logged, never on the HUD as Jarvis's words)."""

    say: str = field(default="", repr=False)
    question: str = field(default="", repr=False)
    steps: tuple[PlannedStep, ...] = ()
    via_fallback: bool = False

    def __repr__(self) -> str:
        return f"DesktopAnswer({len(self.steps)} step(s), question: {bool(self.question)})"


# --------------------------------------------------------------------------
# The checked plan
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CheckedStep:
    """One step checked: ``code`` "" when it passed (else the refusal code), its tier, why the tier went up
    (``flags``), the refs it resolved to in this run's index (``targets``: Jarvis's own entries - a card keeps
    these, never bare ids) and the planner's step as read (its query, kind, web address ...)."""

    number: int
    skill: str
    code: str = ""
    tier: str = ""
    flags: frozenset[str] = frozenset()
    targets: tuple[Ref, ...] = field(default=(), repr=False)
    planned: PlannedStep | None = field(default=None, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        return not self.code

    def __repr__(self) -> str:
        return f"CheckedStep({self.number}, {self.skill}, {self.code or self.tier})"


@dataclass(frozen=True)
class DesktopPlan:
    """A desktop run's plan, checked (``check_plan``): every step with its tier or refusal; the planner's say and
    question as written (shown as the planner's, never as Jarvis's words); the run's own RefIndex (``refs``: what
    each ref meant in THIS run - never re-resolved against a newer one); how many names shown were suspect."""

    run_id: str = ""
    steps: tuple[CheckedStep, ...] = ()
    say: str = field(default="", repr=False)
    question: str = field(default="", repr=False)
    via_fallback: bool = False
    suspects: int = 0
    refs: RefIndex | None = field(default=None, repr=False, compare=False)

    def accepted(self) -> tuple[CheckedStep, ...]:
        return tuple(step for step in self.steps if step.ok)

    def refused(self) -> tuple[CheckedStep, ...]:
        return tuple(step for step in self.steps if not step.ok)

    def counts(self) -> dict[str, int]:
        """Sizes only (safe to log)."""
        return {"steps": len(self.steps), "ok": len(self.accepted()), "refused": len(self.refused()),
                "suspect_names": self.suspects}

    def skills_text(self) -> str:
        """"open_file=1, open_app=1" (the accepted steps' skills; safe to log)."""
        return _tally(step.skill for step in self.accepted())

    def codes_text(self) -> str:
        """"unknown_ref=1" (the refused steps' codes; safe to log)."""
        return _tally(step.code for step in self.refused())

    def __repr__(self) -> str:
        return f"DesktopPlan({self.run_id}, {self.counts()})"


def _tally(items: Iterable[str]) -> str:
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return ", ".join(f"{name}={count}" for name, count in counts.items())


# --------------------------------------------------------------------------
# The owner's words (intent licensing and naming: his typed words only)
# --------------------------------------------------------------------------

def _p(*texts: str) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(text.split()) for text in texts)


@dataclass(frozen=True)
class Licence:
    """Words (token phrases; "*" is a gap: "put * into") that license ``skills``; ``lead``: only as the request's
    first word after "jarvis", "please", "can you" ... (a noun elsewhere: "the file", "the name")."""

    phrases: tuple[tuple[str, ...], ...]
    skills: frozenset[str]
    lead: bool = False


_OPENING = frozenset({FIND_FILES, OPEN_FILE, SHOW_IN_FOLDER, OPEN_FOLDER, OPEN_APP, OPEN_URL})
# SPEC 4.5, plus the plain synonyms of opening and finding (launch, start, go to, visit, view, locate).
LICENCES = (
    Licence(_p("open", "show", "find", "where is", "where s", "where are", "look for", "launch", "start", "locate",
               "go to", "visit", "view", "bring up", "pull up"), _OPENING),
    Licence(_p("tidy", "organise", "organize", "sort", "move", "put"), frozenset({MOVE_FILES, MAKE_FOLDER})),
    Licence(_p("file"), frozenset({MOVE_FILES, MAKE_FOLDER}), lead=True),
    Licence(_p("rename", "call it", "call them"), frozenset({RENAME_FILE})),
    Licence(_p("name"), frozenset({RENAME_FILE}), lead=True),
    Licence(_p("zip", "compress"), frozenset({ZIP_FILES})),
    Licence(_p("copy", "duplicate"), frozenset({COPY_FILES})),
    Licence(_p("delete", "remove", "bin", "trash", "get rid of"), frozenset({RECYCLE_FILES})),
    Licence(_p("fill", "enter", "form", "put * into", "copy * into"), frozenset({FILL_FIELDS})),
    Licence(_p("volume", "louder", "quieter", "mute", "unmute"), frozenset({VOLUME})),
    Licence(_p("play", "pause", "resume", "skip", "next", "previous"), frozenset({MEDIA})),
    Licence(_p("timer", "remind me in"), frozenset({TIMER, CANCEL_TIMER})),
    Licence(_p("focus", "do not disturb"), frozenset({FOCUS_MODE, VOLUME, MEDIA, TIMER})),
    Licence(_p("screenshot", "screen grab", "screen shot"), frozenset({SCREENSHOT})),
)
# Words before the request proper ("jarvis, can you please ..."): skipped to find the request's first word.
_LEAD_INS = frozenset({"jarvis", "hey", "hi", "ok", "okay", "please", "kindly", "can", "could", "would", "will",
                       "you", "i", "d", "like", "want", "need", "to", "now", "just"})
# A licence word right after one of these does not count ("don't delete anything", "without moving").
_NEGATIONS = frozenset({"not", "never", "no", "without", "don", "dont", "doesn", "didn", "won", "shouldn", "mustn"})


def _tokens(text: Any) -> list[str]:
    """``text`` as casefolded words (guard.words: NFKC, hidden characters removed, punctuation split; camelCase
    kept whole: the owner's words, not a label)."""
    return guard.words(text, camel=False)


def _find(tokens: Sequence[str], phrase: Sequence[str]) -> list[int]:
    """Where ``phrase`` starts in ``tokens`` ("*" matches any run of words, at least none)."""
    if "*" in phrase:
        cut = list(phrase).index("*")
        head, tail = list(phrase[:cut]), list(phrase[cut + 1:])
        found = []
        for start in _find(tokens, head):
            rest = tokens[start + len(head):]
            if not tail or _find(rest, tail):
                found.append(start)
        return found
    size = len(phrase)
    return [i for i in range(len(tokens) - size + 1) if size and tuple(tokens[i:i + size]) == tuple(phrase)]


def _lead(tokens: Sequence[str]) -> int:
    """The index of the request's first word (after "jarvis", "please", "can you" ...)."""
    index = 0
    while index < len(tokens) and index < 8 and tokens[index] in _LEAD_INS:
        index += 1
    return index


def licensed_skills(owner_words: Any) -> frozenset[str]:
    """The skills the owner's typed words license (SPEC 4.5): deterministic, on his words only."""
    tokens = _tokens(owner_words)
    lead = _lead(tokens)
    allowed: set[str] = set()
    for licence in LICENCES:
        for phrase in licence.phrases:
            for start in _find(tokens, phrase):
                if licence.lead and start != lead:
                    continue
                if any(token in _NEGATIONS for token in tokens[max(0, start - 2):start]):
                    continue
                allowed |= licence.skills
                break
    return frozenset(allowed)


def _label_tokens(label: str, *, stem: bool = False) -> list[str]:
    text = label
    if stem and guard.suffix(label):
        text = label.rstrip(". ").rsplit(".", 1)[0]
    return _tokens(text)


def names_item(owner_words: Any, label: Any) -> bool:
    """The owner's words name this item: its whole name - or, for a file, its name without the extension when that is
    at least two words - appears in them word for word (case, punctuation and hidden characters aside)."""
    if not isinstance(label, str) or not label.strip():
        return False
    tokens = _tokens(owner_words)
    whole, stem = _label_tokens(label), _label_tokens(label, stem=True)
    candidates = [whole] + ([stem] if len(stem) >= 2 else [])
    return any(candidate and _find(tokens, candidate) for candidate in candidates)


def item_label(ref: Ref) -> str:
    """The name the owner would call ``ref``'s item by: a file's or an app's name, a folder's own name (the last part
    of a place), a site's title."""
    target = ref.target
    if isinstance(target, FileItem):
        return target.name
    if isinstance(target, Place):
        return target.parts[-1] if target.parts else ""
    if isinstance(target, AppItem):
        return target.name
    if isinstance(target, SiteItem):
        return target.title
    return ref.label


def named_exactly(owner_words: Any, label: Any) -> bool:
    """The owner named this app exactly: its whole name appears in his words (open_app INSTANT; else ONE_CLICK)."""
    if not isinstance(label, str):
        return False
    candidate = _label_tokens(label)
    return bool(candidate) and bool(_find(_tokens(owner_words), candidate))


def _bare_address(text: str) -> str:
    """An address as compared word for word: no scheme, no "www.", no trailing "/" or punctuation, host casefolded."""
    value = text.strip().strip(".,;:!?\"'()[]")
    lowered = value.casefold()
    for prefix in ("https://", "http://"):
        if lowered.startswith(prefix):
            value = value[len(prefix):]
            break
    if value.casefold().startswith("www."):
        value = value[4:]
    host, slash, rest = value.partition("/")
    value = host.casefold() + slash + rest
    return value.rstrip("/")


def typed_url(owner_words: Any, url: Any) -> bool:
    """The owner typed this web address word for word (with or without https:// and www.)."""
    if not isinstance(owner_words, str) or not isinstance(url, str):
        return False
    wanted = _bare_address(url)
    return bool(wanted) and any(_bare_address(word) == wanted for word in owner_words.split())


# Where the owner's words name a place to look in or work on: "in / from / under ... (my|the) <name>" or "<name>
# folder" - never where things go ("into / to / onto (my|the) <name> (folder)": a destination is no scope).
_PLACE_BEFORE = frozenset({"in", "from", "under", "inside", "within", "on", "at", "of"})
_PLACE_TO = frozenset({"into", "to", "onto"})
_PLACE_AFTER = frozenset({"folder", "folders", "directory"})
_FILLERS = frozenset({"my", "the", "our", "this", "that"})


def _named_place(tokens: Sequence[str], place: Place) -> bool:
    name = place.parts[-1] if place.parts else ""
    wanted = _tokens(name)
    if not wanted:
        return False
    for start in _find(tokens, wanted):
        before = [token for token in tokens[max(0, start - 3):start] if token not in _FILLERS]
        after = tokens[start + len(wanted):start + len(wanted) + 1]
        if before and before[-1] in _PLACE_TO:
            continue                                      # where things go: not a place to look in
        if (before and before[-1] in _PLACE_BEFORE) or (after and after[0] in _PLACE_AFTER):
            return True
    return False


def _key(path: str) -> str:
    return ntpath.normcase(path).rstrip("\\")


def _under(path: str, folder: str) -> bool:
    key, root = _key(path), _key(folder)
    return bool(key) and bool(root) and (key == root or key.startswith(root + "\\"))


# --------------------------------------------------------------------------
# check_plan
# --------------------------------------------------------------------------

@dataclass
class _Run:
    refs: RefIndex
    words: str
    licensed: frozenset[str]
    named_places: tuple[Place, ...]
    path_check: Callable[[str], Any] | None
    new_ids: set[str] = field(default_factory=set)
    passed: list[CheckedStep] = field(default_factory=list)
    names: set[str] = field(default_factory=set)


def check_plan(answer: DesktopAnswer, refs: RefIndex, owner_words: str, *, run_id: str = "",
               path_check: Callable[[str], Any] | None = None) -> DesktopPlan:
    """Every step of ``answer`` checked against ``refs`` (THIS run's index) and ``owner_words`` (the owner's typed
    words only) - see the module docstring. ``path_check(path)``: the 5.2 path check (paths.check_path with a
    backend; its verdict's ``ok`` / ``code``), given by the caller that has a backend; without it the context is
    trusted to list only items in scope (the executor checks again before each OS call)."""
    words = owner_words if isinstance(owner_words, str) else ""
    tokens = _tokens(words)
    places = [ref.target for ref in refs.refs(PLACE) if isinstance(ref.target, Place)]
    run = _Run(refs, words, licensed_skills(words), tuple(place for place in places if _named_place(tokens, place)),
               path_check)
    checked: list[CheckedStep] = []
    for number, step in enumerate(answer.steps, 1):
        result = _check_step(step, number, run)
        checked.append(result)
        if result.ok:
            run.passed.append(result)
    checked = _rate_caps(checked)
    return DesktopPlan(run_id, tuple(checked), answer.say, answer.question, answer.via_fallback,
                       len(refs.suspects()), refs)


def _rate_caps(steps: list[CheckedStep]) -> list[CheckedStep]:
    """At most MAX_OPENS opens and MAX_URLS web pages a request, counted over the steps that passed, in order."""
    opens = urls = 0
    kept: list[CheckedStep] = []
    for step in steps:
        if step.ok and step.skill in OPEN_SKILLS:
            opens += 1
            urls += step.skill == OPEN_URL
            if opens > MAX_OPENS or (step.skill == OPEN_URL and urls > MAX_URLS):
                opens -= 1
                urls -= step.skill == OPEN_URL
                step = CheckedStep(step.number, step.skill, CAP, step.tier, step.flags, step.targets, step.planned)
        kept.append(step)
    return kept


def _check_step(step: PlannedStep, number: int, run: _Run) -> CheckedStep:
    def refused(code: str, tier: str = "", flags: frozenset[str] = frozenset(),
                targets: tuple[Ref, ...] = ()) -> CheckedStep:
        return CheckedStep(number, step.skill, code, tier, flags, targets, step)

    code = _shape(step)                                                          # 1
    if code:
        return refused(code)
    code, targets = _resolve(step, run)                                          # 2
    if code:
        return refused(code)
    code = _duplicates(step, targets, run)                                       # 3
    if code:
        return refused(code, targets=targets)
    code = _names(step, targets, run)                                            # 4
    if code:
        return refused(code, targets=targets)
    code = _typed(step, targets, run)                                            # 5
    if code:
        return refused(code, targets=targets)
    tier, code, flags = _tier(step, targets, run)                                # 6
    if code:
        return refused(code, NEVER if code in NEVER_CODES else tier, flags, targets)
    code = _licensed(step, targets, run)                                         # 7
    if code:
        return refused(code, tier, flags, targets)
    if any(ref.suspect and not names_item(run.words, item_label(ref)) for ref in targets):   # 8
        return refused(SUSPECT_ITEM, tier, flags, targets)
    if SKILL_PHASES.get(step.skill, 99) > SHIPPED_PHASE:                         # 9
        return refused(NOT_YET, tier, flags, targets)
    if step.skill == MAKE_FOLDER and step.id:
        run.new_ids.add(step.id)
    if step.name:
        run.names.add(" ".join(step.name.casefold().split()))
    return CheckedStep(number, step.skill, "", tier, flags, targets, step)


# ---- 1. shape ------------------------------------------------------------------------------------------

def _in_range(value: int | None, low: int, high: int) -> bool:
    return value is None or low <= value <= high


def _shape(step: PlannedStep) -> str:
    allowed = SKILL_FIELDS.get(step.skill)
    if allowed is None or step.keys - {"skill", "why"} - allowed:
        return SHAPE
    skill = step.skill
    if skill in _ONE_FILE and len(step.files) != 1:
        return SHAPE
    if skill in _FILE_SKILLS and not step.files:
        return SHAPE
    if skill in SKILL_CAPS and len(step.files) > SKILL_CAPS[skill]:
        return CAP
    if skill == FIND_FILES:
        if not (step.query.strip() or step.place or step.kind or step.within_days is not None):
            return SHAPE
        if (step.kind and step.kind not in FIND_KINDS) or not _in_range(step.within_days, 1, 365):
            return SHAPE
    elif skill == OPEN_FOLDER and not step.place:
        return SHAPE
    elif skill == OPEN_APP and not step.app:
        return SHAPE
    elif skill == OPEN_URL:
        if bool(step.url) == bool(step.site):
            return SHAPE
        if step.url and not step.url.casefold().startswith("https://"):
            return SHAPE
    elif skill in (MOVE_FILES, COPY_FILES) and not step.to:
        return SHAPE
    elif skill == RENAME_FILE and not step.name:
        return SHAPE
    elif skill == MAKE_FOLDER and not (step.id and step.name and step.place):
        return SHAPE
    elif skill == ZIP_FILES and not step.name:
        return SHAPE
    elif skill == VOLUME:
        if step.action not in VOLUME_ACTIONS or (step.action == "set") != (step.level is not None):
            return SHAPE
        if not _in_range(step.level, 0, 100) or not _in_range(step.step, 5, 30):
            return SHAPE
    elif skill == MEDIA and step.action not in MEDIA_ACTIONS:
        return SHAPE
    elif skill in (TIMER, FOCUS_MODE) and (step.minutes is None or not _in_range(step.minutes, 1, 240)):
        return SHAPE
    elif skill == FILL_FIELDS:
        if not step.window or not 1 <= len(step.fields) <= MAX_FIELDS:
            return SHAPE
        if any(not field_id or bool(source) == bool(typed) for field_id, source, typed in step.fields):
            return SHAPE                                  # every field: from a cell or typed, exactly one
    return ""


# ---- 2. refs -------------------------------------------------------------------------------------------

def _resolve(step: PlannedStep, run: _Run) -> tuple[str, tuple[Ref, ...]]:
    """Every ref of the step resolved in this run's index (with its kind), in the step's order; UNKNOWN_REF for
    anything else. ``to`` may be a new folder an earlier passing make_folder made (n1); make_folder's own ``id`` is
    a new id (n1 ... n99) not made before."""
    wanted: list[tuple[str, str]] = [(ref_id, FILE) for ref_id in step.files]
    for value, kind in ((step.place, PLACE), (step.app, APP), (step.site, SITE), (step.window, WINDOW)):
        if value:
            wanted.append((value, kind))
    for field_id, source, _typed in step.fields:
        wanted.append((field_id, FIELD))
        if source:
            wanted.append((source, CELL))
    targets: list[Ref] = []
    for ref_id, kind in wanted:
        ref = run.refs.resolve(ref_id, kind)
        if ref is None:
            return UNKNOWN_REF, ()
        targets.append(ref)
    if step.to:
        parsed = parse_ref(step.to)
        if parsed is not None and parsed[0] == NEW:
            if step.to not in run.new_ids:
                return UNKNOWN_REF, ()
        else:
            ref = run.refs.resolve(step.to, PLACE)
            if ref is None:
                return UNKNOWN_REF, ()
            targets.append(ref)
    if step.id:
        parsed = parse_ref(step.id)
        if parsed is None or parsed[0] != NEW or step.id in run.new_ids:
            return UNKNOWN_REF, ()
    return "", tuple(targets)


# ---- 3. duplicates -------------------------------------------------------------------------------------

def _target_key(ref: Ref) -> str:
    target = ref.target
    for name in ("path", "app_id", "url"):
        value = getattr(target, name, None)
        if isinstance(value, str) and value:
            return f"{ref.kind}:{_key(value) if name == 'path' else value.casefold()}"
    return f"{ref.kind}:{ref.id}"


def _duplicates(step: PlannedStep, targets: tuple[Ref, ...], run: _Run) -> str:
    files = [ref for ref in targets if ref.kind == FILE]
    if len({_target_key(ref) for ref in files}) != len(files):
        return SHAPE                                      # the same file twice in one step
    if step.skill in (MOVE_FILES, COPY_FILES) and step.to and not step.to.startswith("n"):
        destination = next((ref for ref in targets if ref.kind == PLACE), None)
        if destination is not None and any(isinstance(ref.target, FileItem) and ref.target.place and
                                           _key(ref.target.place) == _key(destination.target.path) for ref in files):
            return SHAPE                                  # a move into the place it already is in
    if step.name and " ".join(step.name.casefold().split()) in run.names:
        return SHAPE                                      # a repeated new name
    if step.skill in OPEN_SKILLS or step.skill == FIND_FILES:
        mine = (step.skill, tuple(_target_key(ref) for ref in targets), _bare_address(step.url) if step.url else "",
                " ".join(step.query.casefold().split()))
        for earlier in run.passed:
            planned = earlier.planned
            theirs = (earlier.skill, tuple(_target_key(ref) for ref in earlier.targets),
                      _bare_address(planned.url) if planned is not None and planned.url else "",
                      " ".join(planned.query.casefold().split()) if planned is not None else "")
            if theirs == mine:
                return SHAPE                              # the same thing opened (or looked for) twice
    return ""


# ---- 4. new names --------------------------------------------------------------------------------------

def _names(step: PlannedStep, targets: tuple[Ref, ...], run: _Run) -> str:
    if step.skill not in (RENAME_FILE, MAKE_FOLDER, ZIP_FILES) or not step.name:
        return ""
    problem = paths.name_problem(step.name)
    if problem:
        return problem
    if step.skill == RENAME_FILE:
        new_type = guard.suffix(step.name)
        for ref in targets:
            if ref.kind != FILE:
                continue
            old_type = guard.suffix(getattr(ref.target, "name", ""))
            if old_type != new_type and not (new_type and new_type in _tokens(run.words)):
                return NAME_INVALID                       # the extension stays unless the owner's words ask
    if step.skill == ZIP_FILES and guard.suffix(step.name) != "zip":
        return NAME_INVALID
    return ""


# ---- 5. typed values and web addresses -----------------------------------------------------------------

def _typed(step: PlannedStep, targets: tuple[Ref, ...], run: _Run) -> str:
    if step.skill == FILL_FIELDS:
        owner = " ".join(run.words.casefold().split())
        for _field, _source, typed in step.fields:
            if not typed:
                continue
            if not guard.typed_text_allowed(typed):
                return SHAPE
            if " ".join(typed.casefold().split()) not in owner:
                return INTENT_NOT_LICENSED                # a typed value must be the owner's own words
    if step.skill == OPEN_URL:
        url = step.url or next((ref.target.url for ref in targets if isinstance(ref.target, SiteItem)), "")
        problem = guard.url_problem(url)
        if problem == guard.URL_SCHEME:
            return SHAPE
        if problem:
            return SITE_NOT_APPROVED
    return ""


# ---- 6. the tier and the NEVER list --------------------------------------------------------------------

def _path_code(path: str, run: _Run) -> str:
    """The 5.2 path check's refusal for ``path`` ("" when it passes or no check was given)."""
    if run.path_check is None:
        return ""
    try:
        verdict = run.path_check(path)
    except Exception:  # noqa: BLE001 - a check that cannot answer refuses
        return PATH_OUTSIDE
    if getattr(verdict, "ok", False):
        return ""
    code = getattr(verdict, "code", "")
    return code if code in (PATH_OUTSIDE, EXCLUDED) else PATH_OUTSIDE


def _place_code(place: Any, run: _Run) -> str:
    if not isinstance(place, Place):
        return PATH_OUTSIDE
    return _path_code(place.path, run)


def _file_code(item: Any, run: _Run) -> str:
    if not isinstance(item, FileItem):
        return PATH_OUTSIDE
    if guard.secret_file(item.name) or guard.secret_file(item.path):
        return EXCLUDED                                   # secret-shaped: never listed, never touched
    return _path_code(item.path, run)


def _tier(step: PlannedStep, targets: tuple[Ref, ...], run: _Run) -> tuple[str, str, frozenset[str]]:
    """(tier, NEVER-list code or "", flags) from the skill and its RESOLVED targets (never Claude's text)."""
    skill = step.skill
    flags: set[str] = set()
    tier = CARD if skill in _CARD_SKILLS else INSTANT
    for ref in targets:
        if ref.kind == FILE:
            code = _file_code(ref.target, run)
        elif ref.kind == PLACE:
            code = _place_code(ref.target, run)
        else:
            code = ""
        if code:
            return NEVER, code, frozenset(flags)
    if skill == OPEN_FILE:
        item = targets[0].target
        kind = guard.open_kind(item.name)
        if kind in (guard.OPEN_BLOCKED, guard.OPEN_OTHER):
            return NEVER, BLOCKED_TYPE, frozenset(flags)   # never opened (OTHER: Show in folder only)
        if kind == guard.OPEN_SCRIPT:
            tier = promote(tier, CARD)
            flags.add(FLAG_SCRIPT)
        elif kind == guard.OPEN_SHORTCUT:
            tier = promote(tier, CARD)
            flags.add(FLAG_SHORTCUT)
        if any(guard.hidden_char(char) for char in item.name):
            tier = promote(tier, ONE_CLICK)
            flags.add(FLAG_HIDDEN_CHARS)
        if item.online_only:
            flags.add(FLAG_ONLINE_ONLY)                   # flagged only: files open at once (owner decision D5)
    elif skill == OPEN_APP:
        app = targets[0].target
        if not isinstance(app, AppItem) or guard.never_app(app.name, app.exe):
            return NEVER, APP_NOT_APPROVED, frozenset(flags)
        if not named_exactly(run.words, app.name):
            tier = promote(tier, ONE_CLICK)
            flags.add(FLAG_NOT_NAMED)
    elif skill == OPEN_URL and step.url and not typed_url(run.words, step.url):
        tier = promote(tier, ONE_CLICK)                   # Claude suggested it: one click, its host shown
        flags.add(FLAG_SUGGESTED)
    elif skill == ZIP_FILES:
        if any(isinstance(ref.target, FileItem) and guard.blocked_type(ref.target.name) for ref in targets):
            return NEVER, BLOCKED_TYPE, frozenset(flags)   # blocked types are never zipped
    elif skill == FILL_FIELDS:
        for ref in targets:
            if ref.kind == FIELD and guard.sensitive_label(ref.label):
                return NEVER, SENSITIVE_FIELD, frozenset(flags)
            if ref.kind == WINDOW and guard.payment_page(ref.label):
                return NEVER, PAYMENT_PAGE, frozenset(flags)
    return tier, "", frozenset(flags)


# ---- 7. intent and scope licensing ---------------------------------------------------------------------

def _sources(step: PlannedStep, targets: tuple[Ref, ...]) -> list[str]:
    """The paths a step reads from or works on: its files and the place it looks in or opens - never where things go
    (a move's, a copy's or a zip's ``to``; the place a new folder is made in), which scope licensing does not ask
    about."""
    goes_to = {step.to, step.place if step.skill == MAKE_FOLDER else ""} - {""}
    return [ref.target.path for ref in targets
            if isinstance(ref.target, (FileItem, Place)) and ref.id not in goes_to]


def _licensed(step: PlannedStep, targets: tuple[Ref, ...], run: _Run) -> str:
    if step.skill not in run.licensed:
        return INTENT_NOT_LICENSED
    if run.named_places:
        for path in _sources(step, targets):
            if not any(_under(path, place.path) for place in run.named_places):
                return SCOPE
    return ""


def describe(step: CheckedStep) -> str:
    """"Step 2 - open_app - refused: app_not_approved" / "Step 1 - open_file - instant" (Live; never a name)."""
    if step.ok:
        flags = f" ({', '.join(sorted(step.flags))})" if step.flags else ""
        return f"Step {step.number} - {step.skill} - {step.tier.replace('_', '-')}{flags}"
    return f"Step {step.number} - {step.skill} - refused: {step.code}"


def reason_words(code: str) -> str:
    return REASON_WORDS.get(code, "")


def skill_names(skills: Collection[str]) -> list[str]:
    """The skills in SKILL_PHASES' order (Live and logs)."""
    return [skill for skill in SKILL_PHASES if skill in skills]
