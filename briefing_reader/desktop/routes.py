"""The desktop's local routes: simple requests handled on this PC with no planner run (SPEC section 2.2; no Qt).

A request typed on the HUD's bar that ask.routes calls a desktop request (ROUTE_DESKTOP) comes here first; only what
no route below takes goes to the planner (MODE_DESKTOP). Every pattern is a case-insensitive FULL match of the
whitespace-collapsed text, in the style of ask.routes (an optional leading "jarvis," / "please", an optional trailing
"." or "!"):

* Phase 1 (``route`` returns the step it stands for; the tier is INSTANT - the owner named the thing exactly):
  - "open|start|launch <app name>": an app of the context's Start-menu list whose name the words give exactly
    (case and spacing aside), never one on the never-open list (guard.never_app);
  - "open|show (me) (my|the) <known folder> (folder)": Desktop, Documents, Downloads, Pictures, Music or Videos (the
    place is the context's when it lists that root);
  - "open <https address>" or a bare web address with a common web ending ("open youtube.com"; https added), on a
    public host (guard.url_problem), or "open <saved site name>" (a context site's title exactly);
  - "find|where is|where's <words>": when the local search (``search``; P1.8's index) finds at least one file
    with every word in its name.
* Phase 2 (recognised, answered NOT_YET, nothing done): "volume <n>|up|down|mute|unmute", "louder", "quieter",
  "mute", "unmute", "pause|play|resume|next|skip|previous" (also "... the music" / "next song"), "timer <n>
  minutes", "remind me in <n> minutes ...", "screenshot", "focus (mode) for <n> minutes".

``route_words(found, address)`` is Jarvis's own sentence for a route (the HUD's caption): what was understood and
that nothing was opened yet - P1.8 carries the steps out - or that it can't be done yet. Never a name: an app's,
a site's or a file's name is other people's text; only the known folders' own labels are said.

``desktop_request(text)`` is ask.routes' ROUTE_DESKTOP test (clear desktop verbs only): the shapes above, an opening
verb about something that is not Jarvis's own (mail, calendar, briefing, a screen), or a finding verb with a word that
says file, folder or place ("find my budget spreadsheet"; "find a time for lunch" stays an Ask).

Public API
==========
``LocalRoute(kind, tier, code, target, hits, words, folder)``; ``route(text, context=None, search=None)``;
``route_words(found, address="sir")``; ``desktop_request(text)``; ``KNOWN_FOLDER_WORDS``; ``WEB_ENDINGS``;
``NOT_YET_KINDS``.

Pure: nothing is logged here; LocalRoute's repr names its kind only.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..persona import with_address
from . import guard
from . import validate as dv
from .context import DesktopContext, Place
from .paths import ROOT_LABELS

# The known folders by the words the owner uses for them (paths.ROOT_LABELS' keys are the [desktop] folders names).
KNOWN_FOLDER_WORDS = {label.casefold(): key for key, label in ROOT_LABELS.items()}
# The web endings a bare address may have ("open youtube.com"); file types that are also endings (.md, .py, .sh,
# .ps ...) are not among them, so "open notes.md" stays a file.
WEB_ENDINGS = frozenset({"com", "org", "net", "edu", "gov", "uk", "io", "co", "us", "ca", "au", "nz", "ie", "eu",
                         "de", "fr", "nl", "es", "it", "jp", "in", "info", "app", "dev", "ai", "me", "tv", "ly",
                         "gg", "fm", "news", "online", "site", "tech", "blog"})
NOT_YET_KINDS = frozenset({dv.VOLUME, dv.MEDIA, dv.TIMER, dv.SCREENSHOT, dv.FOCUS_MODE})

_PREFIX = r"(?:jarvis[,:]?\s+)?(?:please\s+)?"
_SUFFIX = r"[.!]?"


def _pattern(core: str) -> re.Pattern[str]:
    return re.compile(_PREFIX + "(?:" + core + ")" + _SUFFIX, re.IGNORECASE | re.DOTALL)


_OPEN_APP_RE = _pattern(r"(?:open|start|launch)\s+(?:the\s+)?(?P<name>\S.*?)(?:\s+app)?")
_OPEN_FOLDER_RE = _pattern(r"(?:open|show\s+(?:me\s+)?)\s*(?:my\s+|the\s+)?"
                           r"(?P<folder>desktop|documents|downloads|pictures|music|videos)(?:\s+folder)?")
_OPEN_URL_RE = _pattern(r"open\s+(?P<url>https://\S+)")
_OPEN_HOST_RE = _pattern(r"open\s+(?P<host>(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?P<end>[a-z]{2,6}))"
                         r"(?P<path>/\S*)?")
_OPEN_SITE_RE = _pattern(r"open\s+(?:the\s+|my\s+)?(?P<site>\S.*)")
_FIND_RE = _pattern(r"(?:find|where\s+is|where's|where\s+are)\s+(?:my\s+|the\s+)?(?P<words>\S.*?)\??")
_MINUTES = r"\d{1,3}\s*(?:minutes?|mins?)"
_NOT_YET = (
    (dv.VOLUME, _pattern(r"volume\s+(?:\d{1,3}\s*%?|up|down|mute|unmute)|louder|quieter|mute|unmute"
                         r"|turn\s+(?:it|the\s+volume)\s+(?:up|down)")),
    (dv.MEDIA, _pattern(r"(?:pause|play|resume|next|skip|previous)(?:\s+(?:the\s+)?(?:music|song|track|video))?"
                        r"|(?:next|previous)\s+(?:song|track)")),
    (dv.TIMER, _pattern(r"(?:(?:set|start)\s+a\s+)?timer\s+(?:for\s+)?" + _MINUTES + r"(?:\s+\S.*)?"
                        r"|remind\s+me\s+in\s+" + _MINUTES + r"(?:\s+\S.*)?")),
    (dv.SCREENSHOT, _pattern(r"(?:take\s+a\s+)?screen\s*shot|(?:take\s+a\s+)?screen\s+grab")),
    (dv.FOCUS_MODE, _pattern(r"focus(?:\s+mode)?\s+for\s+" + _MINUTES)),
)

# ROUTE_DESKTOP's "clear desktop verbs" (ask.routes): opening ("open|launch|start <something>") unless it is about
# Jarvis's own things, and finding ("find|locate|where is|look for|show me ...") only with a word that says file,
# folder or place - "find a time for lunch" and "where is my next meeting?" stay Asks.
_OPENING_RE = _pattern(r"(?:open|launch|start)\s+(?P<what>\S.*?)\??")
_FINDING_RE = _pattern(r"(?:find|locate|look\s+for|where\s+(?:is|are|did\s+i\s+(?:put|save|leave))|where's"
                       r"|show\s+(?:me\s+)?)\s*(?P<what>\S.*?)\??")
# Jarvis's own things and screens: never a desktop request.
_JARVIS_THINGS_RE = re.compile(
    r"\b(?:e-?mails?|g?mail|inbox|messages?|threads?|repl(?:y|ies)|calendar|events?|meetings?|invites?|"
    r"invitations?|appointments?|briefings?|approvals?|follow[- ]?ups?|to-?dos?|proposals?|cards?|memory|"
    r"transcripts?|agenda|schedule)\b", re.IGNORECASE)
_SCREENS_RE = re.compile(r"(?:the\s+|my\s+)?(?:home|live|settings|meetings|memory|briefing|approvals|follow[- ]?ups)"
                         r"(?:\s+(?:screen|tab|page))?", re.IGNORECASE)
_NOT_STARTED_RE = re.compile(r"(?:over|again|(?:a\s+|the\s+)?recording|my\s+day|reading)\b.*", re.IGNORECASE)
# Words that say file, folder or place (a finding request needs one), or a file's type ("essay.docx").
_DESKTOP_NOUNS_RE = re.compile(
    r"\b(?:files?|folders?|documents?|docs?|pdfs?|spreadsheets?|sheets?|workbooks?|presentations?|slides?|decks?|"
    r"essays?|reports?|notes|photos?|pictures?|images?|screenshots?|videos?|songs?|music|tracks?|downloads?|"
    r"desktop|scans?|assignments?|coursework|homework|drafts?|cv|resume|letters?|invoices?|receipts?|statements?|"
    r"worksheets?|readings?|lectures?|templates?|zips?|apps?|programs?|shortcuts?)\b|\.[a-z0-9]{2,5}\b",
    re.IGNORECASE)


def desktop_request(text: Any) -> bool:
    """Whether ``text`` is clearly a desktop request (ask.routes' ROUTE_DESKTOP): one of the local routes' Phase 2
    shapes, an opening verb about something that is not Jarvis's own (mail, calendar, briefing, a screen ...), or a
    finding verb with a word that says file, folder or place. Anything else is a normal Ask."""
    text = " ".join(str(text or "").split())
    if not text:
        return False
    if any(pattern.fullmatch(text) for _kind, pattern in _NOT_YET):
        return True
    match = _OPENING_RE.fullmatch(text)
    if match:
        what = match.group("what").strip(" .!?")
        verb = text.casefold().split()
        started = "start" in verb[:3] and "open" not in verb[:3] and "launch" not in verb[:3]
        return not (_JARVIS_THINGS_RE.search(what) or _SCREENS_RE.fullmatch(what)
                    or (started and _NOT_STARTED_RE.fullmatch(what)))
    match = _FINDING_RE.fullmatch(text)
    if match:
        what = match.group("what").strip(" .!?")
        return bool(_DESKTOP_NOUNS_RE.search(what)) and not _JARVIS_THINGS_RE.search(what) \
            and not _SCREENS_RE.fullmatch(what)
    return False


@dataclass(frozen=True)
class LocalRoute:
    """A request handled on this PC: the skill it stands for (``kind``), its tier (INSTANT; "" for a NOT_YET one),
    its refusal code ("" or NOT_YET), what it is about (an AppItem, a Place - None when the context does not list
    that root -, an https address, a SiteItem) and, for a find, the files found (``hits``) and the words looked for.
    Memory only."""

    kind: str
    tier: str = ""
    code: str = ""
    target: Any = field(default=None, repr=False)
    hits: tuple[Any, ...] = field(default=(), repr=False)
    words: str = field(default="", repr=False)
    folder: str = ""                                # a known folder's key ("downloads") for open_folder

    def __repr__(self) -> str:
        return f"LocalRoute({self.kind}{', ' + self.code if self.code else ''})"


def _same(a: str, b: str) -> bool:
    return " ".join(a.casefold().split()) == " ".join(b.casefold().split())


def _root_place(context: DesktopContext | None, label: str) -> Place | None:
    if context is None:
        return None
    return next((place for place in context.places if len(place.parts) == 1 and _same(place.parts[0], label)), None)


def route(text: Any, context: DesktopContext | None = None, *,
          search: Callable[[Sequence[str]], Sequence[Any]] | None = None) -> LocalRoute | None:
    """The local route ``text`` asks for, or None (the planner then). ``context``: the apps, places and saved sites
    the request may name; ``search(words)``: the local search (files whose names hold every word)."""
    text = " ".join(str(text or "").split())
    if not text:
        return None
    for kind, pattern in _NOT_YET:
        if pattern.fullmatch(text):
            return LocalRoute(kind, code=dv.NOT_YET)
    match = _OPEN_FOLDER_RE.fullmatch(text)
    if match:
        key = KNOWN_FOLDER_WORDS[match.group("folder").casefold()]
        return LocalRoute(dv.OPEN_FOLDER, dv.INSTANT, target=_root_place(context, ROOT_LABELS[key]), folder=key)
    match = _OPEN_URL_RE.fullmatch(text)
    if match and not guard.url_problem(match.group("url")):
        return LocalRoute(dv.OPEN_URL, dv.INSTANT, target=match.group("url"))
    match = _OPEN_HOST_RE.fullmatch(text)
    if match and match.group("end").casefold() in WEB_ENDINGS:
        url = "https://" + match.group("host") + (match.group("path") or "")
        if not guard.url_problem(url):
            return LocalRoute(dv.OPEN_URL, dv.INSTANT, target=url)
    if context is not None:
        match = _OPEN_APP_RE.fullmatch(text)
        if match:
            name = match.group("name")
            app = next((app for app in context.apps if _same(app.name, name)), None)
            if app is not None and not guard.never_app(app.name, app.exe):
                return LocalRoute(dv.OPEN_APP, dv.INSTANT, target=app)
        match = _OPEN_SITE_RE.fullmatch(text)
        if match:
            site = next((site for site in context.sites if _same(site.title, match.group("site"))), None)
            if site is not None and not guard.url_problem(site.url):
                return LocalRoute(dv.OPEN_URL, dv.INSTANT, target=site)
    match = _FIND_RE.fullmatch(text)
    if match and search is not None:
        words = [word for word in guard.words(match.group("words"), camel=False) if word]
        if words:
            try:
                found = list(search(words))
            except Exception:  # noqa: BLE001 - no local answer: the planner then
                found = []
            hits = tuple(item for item in found
                         if all(word in " ".join(guard.words(getattr(item, "name", ""), camel=False)) for word in words))
            if hits:
                return LocalRoute(dv.FIND_FILES, dv.INSTANT, hits=hits, words=" ".join(words))
    return None


# --------------------------------------------------------------------------
# Jarvis's own words for a route (the HUD's caption)
# --------------------------------------------------------------------------

UNDERSTOOD = "Understood: {what}. Nothing was opened yet, {sir}."
NOT_YET_WORDS = {dv.VOLUME: "change the volume", dv.MEDIA: "control music", dv.TIMER: "set timers",
                 dv.SCREENSHOT: "take screenshots", dv.FOCUS_MODE: "start focus mode"}
NOT_YET = "I can't {what} yet, {sir} - nothing was changed."


def route_words(found: LocalRoute, address: str = "sir") -> str:
    """Jarvis's sentence for a local route: what was understood and that nothing was opened yet, or that it can't be
    done yet. Only a known folder's own label is named; never an app's, a site's or a file's name."""
    if found.code == dv.NOT_YET:
        return with_address(NOT_YET.format(what=NOT_YET_WORDS.get(found.kind, "do that"), sir="{sir}"), address)
    if found.kind == dv.OPEN_FOLDER:
        what = f"open your {ROOT_LABELS.get(found.folder, 'folder')} folder"
    elif found.kind == dv.OPEN_APP:
        what = "open an app"
    elif found.kind == dv.OPEN_URL:
        what = "open a web page"
    elif found.kind == dv.FIND_FILES:
        count = len(found.hits)
        what = f"look for files - {count} match{'es' if count != 1 else ''}"
    else:
        what = "that"
    return with_address(UNDERSTOOD.format(what=what, sir="{sir}"), address)
