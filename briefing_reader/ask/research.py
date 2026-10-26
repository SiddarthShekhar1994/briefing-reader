"""Web research: one question answered by a separate run of the owner's Claude Code whose only tools
are WebSearch and WebFetch, isolated from every personal datum.

    forced_question(command)        "web: x" / "research: x" -> "x" ("" for just "web:"), else None
    check_question(question, command, now)   QuestionCheck: may the planner's shorter question go
                                    along as <search_hint>? Only when every word in it (as sent) is
                                    one the owner typed (or it with a plain ending), a date word, a
                                    number he typed (or up to two days / years) or a plain word
                                    (NEUTRAL_WORDS)
    build_research_prompt(question, hint=, now=, time_zone=, limits=)   the research run's stdin:
                                    <today>, <limits>, <search_hint> (optional), <question>; these
                                    parameters are all it can be given (no context, no config)
    normalize_url(url), fetch_url_problem(url)   the URL rules (actions.public_host_problem)
    check_research_init(event, limits)   the init guard: StructuredOutput, WebSearch and (allowed)
                                    WebFetch only, no MCP server, apiKeySource none, dontAsk
    WebTracker(limits, question, on_call, on_result)   every later event: each web search and page
                                    read as a WebCall (with its WebResult), the caps (the 5th search
                                    or page read stops the run), no other tool, never a local or
                                    private address; log() -> WebLog
    read_research_result(event, init)   the result -> ResearchAnswer or AskFailure
    run_research(...)               one research run end to end (stream.run_cli with the above);
                                    RunResult.answer is the ResearchAnswer, RunResult.web the WebLog

The isolation rule: the research run gets only build_research_prompt's text on stdin (the owner's
words, today's date and time zone and Jarvis's own limits), a command line with no tool but the two
web tools (cli.build_research_argv), the allowlisted environment (cli.clean_env) and an empty work
folder of its own. Its answer only becomes cards (research_validate) that wait for the owner's
click like every other card.

Pure except run_research (which starts the CLI through the given runner). Nothing here logs a
question, a query, a URL, a domain, page text or the answer: the reprs of WebCall, WebResult,
WebLog and ResearchAnswer name kinds, ids and counts only.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..actions import public_host_problem
from . import mail
from .cli import WEB_FETCH_TOOL, WEB_SEARCH_TOOL, Runner
from .context import COMMAND_CAP, data, now_line
from .context import _keep_bars as _command_text
from .stream import (
    ERROR,
    GARBLED,
    GUARD,
    INIT_TIMEOUT_S,
    MAX_TURNS,
    MESSAGES,
    POLL_S,
    RESEARCH_CAP,
    SCHEMA_RETRIES,
    STRUCTURED_TOOL,
    TIMEOUT,
    AskFailure,
    GuardTrip,
    InitSummary,
    Progress,
    RunResult,
    RunStop,
    _one_line,
    _report,
    _word,
    failure,
    result_failure,
    run_cli,
)

# --------------------------------------------------------------------------
# Routing: which question
# --------------------------------------------------------------------------

_FORCED_RE = re.compile(r"^(?:web|research)\s*:(?!//)\s*", re.IGNORECASE)
HINT_CAP = 200
MAX_UNKNOWN_SHOWN = 8

NEUTRAL_WORDS = mail._STOPWORDS | frozenset("""
a an the of in on at to for and or is are be do does did what which who whom whose when where why how much
many near nearby best top latest recent current new news update updates official website site page price
prices cost costs fee fees hours opening open opens close closes closing time times schedule date dates
weather forecast review reviews rating ratings vs versus compare comparison guide tutorial definition meaning
explained summary info information details list release released version status today tonight tomorrow
yesterday week weekend month year this next last morning afternoon evening day days long far old tall big
small""".split())
_DATE_WORDS = frozenset(word for name in (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february", "march",
    "april", "may", "june", "july", "august", "september", "october", "november", "december")
    for word in (name, name[:3])) | {"am", "pm"}
_LINKISH = ("@", "://", "www.")


def forced_question(command: str) -> str | None:
    """The question of a request that starts with "web:" or "research:" (any case; "" when nothing
    follows), else None. "webinar: x", "website: x", "the web: x" and "web://x" are not research."""
    text = " ".join((command or "").split())
    match = _FORCED_RE.match(text)
    return text[match.end():].strip() if match else None


@dataclass(frozen=True)
class QuestionCheck:
    """Whether the planner's question may go to the research as <search_hint> (shown in LIVE; the
    question, its words and the reason are never logged)."""

    ok: bool
    text: str                         # the hint as it would be sent (one line, <= 200)
    unknown: tuple[str, ...] = ()     # the words that failed (casefolded, sorted, at most 8)
    reason: str = ""                  # Jarvis's own words for LIVE (never logged)

    def __repr__(self) -> str:
        return f"QuestionCheck(ok={self.ok}, unknown={len(self.unknown)})"


_ENDINGS = ("s", "es", "d", "ed", "ing")
MAX_LOOSE_NUMBERS = 2   # numbers the owner didn't type: a year next to now's, a day next to a date word
_WORD_RE = re.compile(r"[^\W_]+")


def inflected(word: str, typed: str) -> bool:
    """``word`` is ``typed`` or the same word with a plain ending, either way round ("museums",
    "opened", "opening"; "closing" for "close"): never any other letters, so nothing can ride on a
    typed word ("timedrsmith" is not "time")."""
    if word == typed:
        return True
    for short, long_ in ((typed, word), (word, typed)):
        for ending in _ENDINGS:
            if long_ == short + ending:
                return True
            if ending in ("ed", "ing") and short.endswith("e") and long_ == short[:-1] + ending:
                return True
    return False


def check_question(question: str, command: str, now: datetime) -> QuestionCheck:
    """May the planner's ``question`` go to the web research (as <search_hint>)? Each word of the
    hint as it would be sent (hint_text: invisible characters removed first, so they cannot split
    or join words) must be a word of ``command`` as the research gets it (or that word with a
    plain ending: s, es, d, ed, ing), a plain word (NEUTRAL_WORDS), a weekday or month name or
    "am" / "pm", or a number the owner typed; besides those, at most two numbers: a year next to
    ``now``'s, or a day (1-31) right next to a weekday, month, "am" or "pm". Never an address, a
    link or "www." the command does not hold, never over 200 characters. Nothing from the
    calendar, mail or briefing can pass: the owner never typed it."""
    flat = " ".join(str(question or "").split())
    text = data(flat, HINT_CAP)
    if not flat or not text:
        return QuestionCheck(False, "", reason="it is empty")
    if len(flat) > HINT_CAP:
        return QuestionCheck(False, text, reason=f"it is longer than {HINT_CAP} characters")
    typed_text = question_text(command).casefold()
    for mark in _LINKISH:
        if (mark in flat.casefold() or mark in text.casefold()) and mark not in typed_text:
            return QuestionCheck(False, text, reason="it names an address or a link you didn't type")
    typed = mail.search_words(typed_text)
    years = {str(year) for year in (now.year - 1, now.year, now.year + 1)}
    words = _WORD_RE.findall(text.casefold())
    unknown: set[str] = set()
    loose = 0
    for index, word in enumerate(words):
        if word in NEUTRAL_WORDS or word in _DATE_WORDS or word in typed:
            continue
        if word.isdigit():
            next_to = {words[spot] for spot in (index - 1, index + 1) if 0 <= spot < len(words)}
            if loose < MAX_LOOSE_NUMBERS and (word in years or (1 <= int(word) <= 31 and next_to & _DATE_WORDS)):
                loose += 1
                continue
        elif any(inflected(word, own) for own in typed):
            continue
        unknown.add(word)
    if unknown:
        shown = tuple(sorted(unknown)[:MAX_UNKNOWN_SHOWN])
        return QuestionCheck(False, text, shown, f"it named words you didn't type ({', '.join(shown)})")
    return QuestionCheck(True, text)


def same_question(first: str, second: str) -> bool:
    """The two say the same (casefolded, whitespace collapsed): no hint is needed."""
    return " ".join(str(first or "").split()).casefold() == " ".join(str(second or "").split()).casefold()


# --------------------------------------------------------------------------
# The research run's stdin
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ResearchLimits:
    """What one research run may use (from [research]): Jarvis's own numbers, the only thing besides
    the question and the date the run is told."""

    max_searches: int = 4
    max_fetches: int = 4             # 0: search only (WebFetch is not offered)
    max_sources: int = 5
    max_suggestions: int = 3

    @classmethod
    def of(cls, research: Any) -> ResearchLimits:
        """The limits of a config.ResearchConfig."""
        return cls(max_searches=int(research.max_searches), max_fetches=int(research.max_fetches),
                   max_sources=int(research.max_sources))

    @property
    def max_turns(self) -> int:
        return self.max_searches + self.max_fetches + 2


def _count(number: int, noun: str, plural: str = "") -> str:
    return f"{number} {noun if number == 1 else (plural or noun + 's')}"


def limits_text(limits: ResearchLimits) -> str:
    """"at most 4 web searches and 4 page reads; at most 5 sources; at most 3 suggestions"."""
    reads = _count(limits.max_fetches, "page read") if limits.max_fetches > 0 else "no page reads"
    return (f"at most {_count(limits.max_searches, 'web search', 'web searches')} and {reads}; "
            f"at most {_count(limits.max_sources, 'source')}; "
            f"at most {_count(limits.max_suggestions, 'suggestion')}")


def build_research_prompt(question: str, *, hint: str, now: datetime, time_zone: str,
                          limits: ResearchLimits) -> str:
    """The research run's whole stdin. These parameters are everything it can hold: no context,
    no config, no briefing, no address (isolation; a test pins the signature). ``question`` is
    cleaned like the planner's <command> (one line, no control or invisible character, "<" / ">"
    as "\u2039" / "\u203a", at most context.COMMAND_CAP characters: the owner's words, as long as
    an Ask's), ``hint`` like a data field (at most 200). The run's own caps (searches, page reads,
    turns, the answer's size) do not depend on the question's length."""
    parts = [f"<today>\n{now_line(now, time_zone)}\n</today>", f"<limits>\n{limits_text(limits)}\n</limits>"]
    shown_hint = hint_text(hint)
    if shown_hint:
        parts.append(f"<search_hint>\n{shown_hint}\n</search_hint>")
    parts.append(f"<question>\n{question_text(question)}\n</question>")
    return "\n".join(parts) + "\n"


def question_text(question: str) -> str:
    """The question exactly as the research run gets it (cleaned like the planner's <command>)."""
    return _command_text(question, COMMAND_CAP)


def hint_text(hint: str) -> str:
    """The search hint exactly as the research run gets it ("" for none)."""
    return data(hint, HINT_CAP)


# --------------------------------------------------------------------------
# URLs
# --------------------------------------------------------------------------

URL_CAP = 2048
_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_url(url: Any) -> str:
    """``url`` compared as a page: scheme and host casefolded, a trailing dot and the default port
    dropped, the fragment dropped, an empty path as "/", a trailing "/" dropped except the root;
    the query kept ("" for anything that is not text)."""
    if not isinstance(url, str):
        return ""
    text = url.strip()
    try:
        parts = urlsplit(text)
        port = parts.port
        host = (parts.hostname or "").rstrip(".")
    except ValueError:
        return text
    if not parts.scheme or not host:
        return text
    scheme = parts.scheme.casefold()
    shown_host = f"[{host}]" if ":" in host else host
    netloc = shown_host
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{shown_host}:{port}"
    if parts.username is not None or parts.password is not None:
        user = parts.netloc.rpartition("@")[0]
        netloc = f"{user}@{netloc}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{scheme}://{netloc}{path}{query}"


def fetch_url_problem(url: Any) -> str:
    """Why WebFetch may not read ``url`` ("" when it may): http or https only, at most 2,048
    characters, no space or control character, no user name or password, port none, 80 or 443,
    and a public host (actions.public_host_problem: never localhost, an IP address, a .local,
    .lan, .internal ... name)."""
    if not isinstance(url, str) or not url.strip():
        return "not a web address"
    if len(url) > URL_CAP:
        return f"longer than {URL_CAP:,} characters"
    if any(char.isspace() or ord(char) < 32 or char == "\x7f" or char == "\\" for char in url):
        return "not a web address"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "not a web address"
    if parts.scheme.casefold() not in ("http", "https"):
        return "not an http or https address"
    host_problem = public_host_problem(parts.hostname or "")
    if host_problem:
        return host_problem
    if parts.username is not None or parts.password is not None:
        return "an address with a user name or password"
    if port not in (None, 80, 443):
        return "an address with an unusual port"
    return ""


def _url_in_text(url: str, text: str) -> bool:
    """The owner's words name this page ("example.org/visit", with or without https:// or www.)."""
    page = normalize_url(url).casefold()
    bare = re.sub(r"^[a-z]+://", "", page)
    lowered = (text or "").casefold()
    candidates = {bare, bare.removeprefix("www.")}
    return any(candidate and len(candidate) >= 4 and candidate in lowered for candidate in candidates)


# --------------------------------------------------------------------------
# The init guard
# --------------------------------------------------------------------------

NO_WEB_SEARCH_MESSAGE = ("Claude Code started without its web search tool, so the research was stopped; "
                         "nothing was proposed")


def check_research_init(event: Mapping[str, Any], limits: ResearchLimits) -> InitSummary:
    """The research run's init event as Jarvis requires it, else GuardTrip / RunStop (the caller
    kills the process): system/init, apiKeySource "none", no MCP server, tools only from
    StructuredOutput, WebSearch and (``limits.max_fetches`` > 0) WebFetch, with StructuredOutput
    and WebSearch there, and permissionMode "dontAsk" when it says one. WebFetch missing while
    allowed is not a stop (the research searches only)."""
    if event.get("type") != "system" or event.get("subtype") != "init":
        raise GuardTrip("not_init")
    if event.get("apiKeySource") != "none":
        raise GuardTrip("api_key_source")
    servers = event.get("mcp_servers")
    if not isinstance(servers, list) or servers:
        raise GuardTrip("mcp_servers")
    tools = event.get("tools")
    if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
        raise GuardTrip("tools")
    allowed = {STRUCTURED_TOOL, WEB_SEARCH_TOOL} | ({WEB_FETCH_TOOL} if limits.max_fetches > 0 else set())
    if set(tools) - allowed:
        raise GuardTrip("tools")
    if STRUCTURED_TOOL not in tools:
        raise GuardTrip("no_structured_output")
    mode = event.get("permissionMode")
    if mode is not None and mode != "dontAsk":
        raise GuardTrip("permission_mode")
    if WEB_SEARCH_TOOL not in tools:
        raise RunStop(failure(GUARD, "no_web_search", NO_WEB_SEARCH_MESSAGE))
    return InitSummary(api_key_source="none", tools=tuple(tools), mcp_servers=0, model=_word(event.get("model")),
                       version=_word(event.get("claude_code_version")), structured_tool=True,
                       permission_mode=_word(mode) if mode is not None else "")


# --------------------------------------------------------------------------
# Web steps
# --------------------------------------------------------------------------

SEARCH = "search"
FETCH = "fetch"
FOUND_SEARCH = "search"      # a fetch of a page a search listed
FOUND_OWNER = "owner"        # ... a page the owner's words name
FOUND_OTHER = "other"        # ... anything else (a link Claude followed, an address it typed)

RESULT_OK = "ok"
RESULT_EMPTY = "empty"       # nothing found
RESULT_UNREAD = "unread"     # results came back as text Jarvis could not list
RESULT_ERROR = "error"
RESULT_DENIED = "denied"     # Claude Code's permission rules refused it
RESULT_REDIRECT = "redirect"

TEXT_CAP = 65_536
MESSAGE_CAP = 300
MAX_HITS = 50
QUERY_CAP = 1000
PROMPT_CAP = 2000
DOMAINS_CAP = 20
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_DENIED_RE = re.compile(r"permission|haven't granted|not allowed|denied", re.IGNORECASE)
_REDIRECT_RE = re.compile(r"Redirect URL:\s*(\S+)")
_STATUS_CODE_RE = re.compile(r"status code (\d{3})\b")
_SERVER_NAMES = {"web_search": SEARCH, "web_fetch": FETCH}
_TOOL_NAMES = {WEB_SEARCH_TOOL: SEARCH, WEB_FETCH_TOOL: FETCH}
_ORDINALS = ("zeroth", "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth",
             "tenth", "eleventh", "twelfth")


@dataclass(frozen=True)
class WebCall:
    """One web search or page read Claude Code was about to make (its own words: shown in LIVE,
    never logged)."""

    tool_id: str
    kind: str                                 # SEARCH | FETCH
    number: int                               # 1-based within its kind for this run
    query: str = ""
    allowed_domains: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()
    url: str = ""                             # WebFetch input
    prompt: str = ""
    server: bool = False                      # a server_tool_use block
    found_by: str = ""                        # fetch: FOUND_SEARCH | FOUND_OWNER | FOUND_OTHER
    over: bool = False                        # over the cap: the run is being stopped at this call
    refused: str = ""                         # fetch_url_problem's reason (the run is being stopped)

    def __repr__(self) -> str:
        return (f"WebCall(id={self.tool_id[:40]!r}, kind={self.kind!r}, number={self.number}, over={self.over}, "
                f"refused={bool(self.refused)})")


@dataclass(frozen=True)
class WebResult:
    """What one call returned to Claude (web text: shown in LIVE as untrusted, never logged)."""

    tool_id: str
    kind: str
    status: str                               # RESULT_*
    hits: tuple[tuple[str, str], ...] = ()    # search: (title, url), in order, at most 50
    groups: int = 0                           # search: result groups the service reported
    text: str = ""                            # what the tool returned to Claude (<= 65,536 chars)
    code: int | None = None
    code_text: str = ""
    size: int | None = None
    took_ms: int | None = None
    final_url: str = ""
    redirect_to: str = ""
    message: str = ""                         # error / denial text, one line, <= 300

    def __repr__(self) -> str:
        return (f"WebResult(id={self.tool_id[:40]!r}, kind={self.kind!r}, status={self.status!r}, "
                f"hits={len(self.hits)}, code={self.code})")


@dataclass(frozen=True)
class WebLog:
    """Every web step of one run (shown; never logged: the repr is counts only)."""

    calls: tuple[WebCall, ...] = ()
    results: tuple[WebResult, ...] = ()
    searches: int = 0
    fetches: int = 0
    denied: int = 0
    hits: int = 0
    seen: frozenset[str] = frozenset()        # normalize_url of every hit and every page read (URL and final URL)
    checkable: bool = True                    # every finished search's hits could be read
    reads: int = 0                            # page reads that returned the page (status ok)

    def title_of(self, url: str) -> str:
        """The first search hit's title for that page ("" when none)."""
        page = normalize_url(url)
        for result in self.results:
            for title, hit in result.hits:
                if normalize_url(hit) == page and title:
                    return title
        return ""

    def result_of(self, tool_id: str) -> WebResult | None:
        return next((result for result in self.results if result.tool_id == tool_id), None)

    def __repr__(self) -> str:
        return (f"WebLog(calls={len(self.calls)}, results={len(self.results)}, searches={self.searches}, "
                f"fetches={self.fetches}, reads={self.reads}, denied={self.denied}, hits={self.hits}, "
                f"checkable={self.checkable})")


def _ordinal(number: int) -> str:
    return _ORDINALS[number] if 0 <= number < len(_ORDINALS) else f"{number}th"


def _a(word: str) -> str:
    return f"an {word}" if word[:1] in "aeiou" else f"a {word}"


def cap_message(kind: str, number: int, limit: int) -> str:
    """The research_cap failure's words: "... it wanted a fifth web search (at most 4: [research]
    max_searches); nothing was proposed"."""
    what, key = ("web search", "max_searches") if kind == SEARCH else ("page read", "max_fetches")
    return (f"Jarvis stopped the web research: it wanted {_a(_ordinal(number))} {what} (at most {limit}: "
            f"[research] {key}); nothing was proposed")


FETCH_URL_MESSAGE = ("Jarvis stopped the web research: Claude Code was about to read a local or private address, "
                     "which research never may; nothing was proposed")


def _text(value: Any, cap: int) -> str:
    return value[:cap] if isinstance(value, str) else ""


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item[:255] for item in value if isinstance(item, str))[:DOMAINS_CAP]


def _block_text(content: Any) -> str:
    """A tool_result's content: a string, or the text of its {"type": "text"} blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(item["text"] for item in content
                         if isinstance(item, dict) and isinstance(item.get("text"), str))
    return ""


def _number(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = int(value)
    except (OverflowError, ValueError):
        return None
    return number if number >= 0 else None


def _hit(item: Any) -> tuple[str, str] | None:
    if not isinstance(item, dict) or not isinstance(item.get("url"), str) or not item["url"].strip():
        return None
    title = item.get("title") if isinstance(item.get("title"), str) else ""
    return " ".join(title.split())[:300], item["url"].strip()[:URL_CAP + 1]


def _links_in(text: str) -> tuple[list[tuple[str, str]], bool]:
    """(the hits of every "Links: [...]" array in a search result's text, whether one was read)."""
    hits: list[tuple[str, str]] = []
    found = False
    decoder = json.JSONDecoder()
    start = 0
    while True:
        index = text.find("Links:", start)
        if index < 0:
            break
        start = index + 6
        bracket = text.find("[", start, start + 40)
        if bracket < 0:
            continue
        try:
            value, _end = decoder.raw_decode(text, bracket)
        except ValueError:
            continue
        if isinstance(value, list):
            found = True
            hits += [hit for hit in (_hit(item) for item in value) if hit is not None]
    return hits, found


class WebTracker:
    """run_cli's ``event_check`` for a research run: every web search and page read Claude Code is
    about to make (``on_call``) and what came back (``on_result``), the caps, the tool guard and the
    local-address guard. A broken callback never changes the run."""

    def __init__(self, limits: ResearchLimits, question: str = "",
                 on_call: Callable[[WebCall], None] | None = None,
                 on_result: Callable[[WebResult], None] | None = None) -> None:
        self.limits = limits
        self.question = question or ""
        self._on_call = on_call
        self._on_result = on_result
        self._lock = threading.Lock()
        self._calls: dict[str, WebCall] = {}
        self._order: list[WebCall] = []
        self._results: list[WebResult] = []
        self._searches = 0
        self._fetches = 0
        self._hits: set[str] = set()       # normalized hit URLs so far
        self._seen: set[str] = set()
        self._checkable = True
        self._denials = 0
        self._hit_count = 0

    # ---- the hook ---------------------------------------------------------------------------

    def check(self, event: Mapping[str, Any]) -> None:
        """One stream event (after init): GuardTrip / RunStop stop the run."""
        kind = event.get("type")
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = content if isinstance(content, list) else []
        if kind == "assistant":
            for block in blocks:
                if isinstance(block, dict):
                    self._assistant_block(block)
        elif kind == "user":
            results = [block for block in blocks if isinstance(block, dict) and block.get("type") == "tool_result"]
            structured = event.get("tool_use_result") if len(results) == 1 else None
            for block in results:
                self._tool_result(block, structured)
        elif kind == "result":
            denials = event.get("permission_denials")
            if isinstance(denials, list):
                with self._lock:
                    self._denials = max(self._denials, len(denials))

    def _assistant_block(self, block: Mapping[str, Any]) -> None:
        block_type = block.get("type")
        if block_type in ("tool_use", "server_tool_use", "mcp_tool_use"):
            name = block.get("name")
            if block_type == "tool_use" and name == STRUCTURED_TOOL:
                return
            kind = None
            if block_type == "tool_use":
                kind = _TOOL_NAMES.get(name) if isinstance(name, str) else None
            elif block_type == "server_tool_use":
                kind = _SERVER_NAMES.get(name) if isinstance(name, str) else None
            if kind is None:
                raise GuardTrip("tool_use")
            if kind == FETCH and self.limits.max_fetches <= 0:
                raise GuardTrip("tool_use")
            self._call(block, kind, server=block_type == "server_tool_use")
        elif block_type == "web_search_tool_result":
            self._server_result(block, SEARCH)
        elif block_type == "web_fetch_tool_result":
            self._server_result(block, FETCH)

    def _call(self, block: Mapping[str, Any], kind: str, *, server: bool) -> None:
        tool_id = block.get("id") if isinstance(block.get("id"), str) else ""
        with self._lock:
            if tool_id and tool_id in self._calls:   # the same call again: counted once
                return
        raw = block.get("input")
        given = raw if isinstance(raw, dict) else {}
        if kind == SEARCH:
            with self._lock:
                self._searches += 1
                number = self._searches
            call = WebCall(tool_id, SEARCH, number, query=_text(given.get("query"), QUERY_CAP),
                           allowed_domains=_strings(given.get("allowed_domains")),
                           blocked_domains=_strings(given.get("blocked_domains")), server=server)
            if number > self.limits.max_searches:
                self._report_call(replace(call, over=True))
                raise RunStop(failure(RESEARCH_CAP, "searches", cap_message(SEARCH, number, self.limits.max_searches)))
            self._report_call(call)
            return
        url = _text(given.get("url"), URL_CAP + 1)
        with self._lock:
            self._fetches += 1
            number = self._fetches
            hit = normalize_url(url) in self._hits
        found_by = FOUND_SEARCH if hit else (FOUND_OWNER if _url_in_text(url, self.question) else FOUND_OTHER)
        call = WebCall(tool_id, FETCH, number, url=url, prompt=_text(given.get("prompt"), PROMPT_CAP), server=server,
                       found_by=found_by)
        problem = fetch_url_problem(url)
        if problem:
            self._report_call(replace(call, refused=problem))
            raise RunStop(failure(GUARD, "fetch_url", FETCH_URL_MESSAGE))
        if number > self.limits.max_fetches:
            self._report_call(replace(call, over=True))
            raise RunStop(failure(RESEARCH_CAP, "fetches", cap_message(FETCH, number, self.limits.max_fetches)))
        self._report_call(call)

    def _report_call(self, call: WebCall) -> None:
        with self._lock:
            if call.tool_id:
                self._calls[call.tool_id] = call
            self._order.append(call)
        _tell(self._on_call, call)

    def _known(self, tool_id: Any, kind: str | None = None) -> WebCall | None:
        if not isinstance(tool_id, str) or not tool_id:
            return None
        with self._lock:
            call = self._calls.get(tool_id)
            done = any(result.tool_id == tool_id for result in self._results)
        if call is None or done or call.over or call.refused or (kind is not None and call.kind != kind):
            return None
        return call

    def _tool_result(self, block: Mapping[str, Any], structured: Any) -> None:
        call = self._known(block.get("tool_use_id"))
        if call is None:
            return
        text = _block_text(block.get("content"))
        details = structured if isinstance(structured, dict) else {}
        if not text and isinstance(structured, str):
            text = structured
        error = block.get("is_error") is True
        if call.kind == SEARCH:
            result = self._search_result(call, text, details, error)
        else:
            result = self._fetch_result(call, text, details, error)
        self._add_result(result)

    def _search_result(self, call: WebCall, text: str, details: Mapping[str, Any], error: bool) -> WebResult:
        took = _number(details.get("durationSeconds"))
        took_ms = int(float(details["durationSeconds"]) * 1000) if took is not None else None
        if error:
            return WebResult(call.tool_id, SEARCH, _error_status(text), text=text[:TEXT_CAP],
                             message=_message(text), took_ms=took_ms)
        hits: list[tuple[str, str]] = []
        groups = 0
        readable = False
        results = details.get("results")
        extra_text: list[str] = []
        if isinstance(results, list):
            readable = True
            for item in results:
                if isinstance(item, dict):
                    groups += 1
                    for entry in item.get("content") if isinstance(item.get("content"), list) else ():
                        found = _hit(entry)
                        if found is not None:
                            hits.append(found)
                elif isinstance(item, str):
                    extra_text.append(item)
        if not hits:
            linked, found_links = _links_in(text)
            hits, readable = linked, readable or found_links
        if not text and extra_text:
            text = "\n".join(extra_text)
        hits = hits[:MAX_HITS]
        if hits:
            status = RESULT_OK
        elif readable or not text.strip():
            status = RESULT_EMPTY
        else:
            status = RESULT_UNREAD
        return WebResult(call.tool_id, SEARCH, status, hits=tuple(hits), groups=groups, text=text[:TEXT_CAP],
                         took_ms=took_ms)

    def _fetch_result(self, call: WebCall, text: str, details: Mapping[str, Any], error: bool) -> WebResult:
        code = _number(details.get("code"))
        code_text = " ".join(_text(details.get("codeText"), 80).split())
        size = _number(details.get("bytes"))
        took_ms = _number(details.get("durationMs"))
        final_url = _text(details.get("url"), URL_CAP + 1)
        if not text:
            text = _text(details.get("result"), TEXT_CAP)
        if error:
            status = _error_status(text)
            if code is None:
                found = _STATUS_CODE_RE.search(text)
                code = int(found.group(1)) if found else None
            if code is not None and not code_text:
                code_text = _phrase(code)
            return WebResult(call.tool_id, FETCH, status, text=text[:TEXT_CAP], code=code, code_text=code_text,
                             size=size, took_ms=took_ms, final_url=final_url, message=_message(text))
        redirect = _REDIRECT_RE.search(text)
        if (code is not None and code in _REDIRECT_CODES) or text.lstrip().startswith("REDIRECT DETECTED"):
            return WebResult(call.tool_id, FETCH, RESULT_REDIRECT, text=text[:TEXT_CAP], code=code,
                             code_text=code_text, size=size, took_ms=took_ms, final_url=final_url,
                             redirect_to=redirect.group(1)[:URL_CAP + 1] if redirect else "")
        status = RESULT_ERROR if code is not None and code >= 400 else RESULT_OK
        return WebResult(call.tool_id, FETCH, status, text=text[:TEXT_CAP], code=code, code_text=code_text,
                         size=size, took_ms=took_ms, final_url=final_url,
                         message=f"HTTP {code} {code_text}".strip() if status == RESULT_ERROR else "")

    def _server_result(self, block: Mapping[str, Any], kind: str) -> None:
        call = self._known(block.get("tool_use_id"), kind)
        if call is None:
            return
        content = block.get("content")
        if isinstance(content, dict) and content.get("error_code") is not None:
            code = _text(content.get("error_code"), 80)
            result = WebResult(call.tool_id, kind, RESULT_ERROR, message=_message(code))
        elif kind == SEARCH:
            hits = [hit for hit in (_hit(item) for item in content) if hit is not None] if isinstance(content, list) \
                else []
            result = WebResult(call.tool_id, SEARCH, RESULT_OK if hits else RESULT_EMPTY, hits=tuple(hits[:MAX_HITS]),
                               groups=1 if isinstance(content, list) else 0)
        else:
            page = content if isinstance(content, dict) else {}
            document = page.get("content") if isinstance(page.get("content"), dict) else {}
            source = document.get("source") if isinstance(document.get("source"), dict) else {}
            text = _text(source.get("data"), TEXT_CAP)
            result = WebResult(call.tool_id, FETCH, RESULT_OK if page else RESULT_UNREAD, text=text,
                               final_url=_text(page.get("url"), URL_CAP + 1))
        self._add_result(result)

    def _add_result(self, result: WebResult) -> None:
        with self._lock:
            self._results.append(result)
            call = self._calls.get(result.tool_id)
            for _title, url in result.hits:
                page = normalize_url(url)
                self._hits.add(page)
                self._seen.add(page)
            self._hit_count += len(result.hits)
            if result.kind == SEARCH and result.status == RESULT_UNREAD:
                self._checkable = False
            if result.kind == FETCH and result.status == RESULT_OK:
                for url in (call.url if call is not None else "", result.final_url):
                    if url:
                        self._seen.add(normalize_url(url))
        _tell(self._on_result, result)

    # ---- the log ----------------------------------------------------------------------------

    def log(self) -> WebLog:
        with self._lock:
            denied = sum(1 for result in self._results if result.status == RESULT_DENIED)
            reads = sum(1 for result in self._results if result.kind == FETCH and result.status == RESULT_OK)
            return WebLog(calls=tuple(self._order), results=tuple(self._results), searches=self._searches,
                          fetches=self._fetches, denied=max(denied, self._denials), hits=self._hit_count,
                          seen=frozenset(self._seen), checkable=self._checkable, reads=reads)


def _tell(callback: Callable[[Any], None] | None, value: Any) -> None:
    """A LIVE callback; whatever it does, the run goes on unchanged."""
    if callback is None:
        return
    try:
        callback(value)
    except Exception:  # noqa: BLE001 - a broken callback never changes the run
        pass


def _phrase(code: int) -> str:
    """"Not Found" for 404 ("" for a code Python does not know)."""
    from http import HTTPStatus

    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return ""


def _error_status(text: str) -> str:
    return RESULT_DENIED if _DENIED_RE.search(text or "") else RESULT_ERROR


def _message(text: str) -> str:
    flat = _one_line(text or "")
    return flat if len(flat) <= MESSAGE_CAP else flat[:MESSAGE_CAP - 3].rstrip() + "..."


# --------------------------------------------------------------------------
# The answer
# --------------------------------------------------------------------------

ANSWER_CAP = 500
SOURCES_CAP = 6
TITLE_CAP = 200
SOURCE_URL_CAP = 2000
SUGGESTIONS_CAP = 3
SUGGESTION_CAP = 1000


@dataclass(frozen=True)
class ResearchAnswer:
    """The research run's structured_output, shape-checked (built from web text: shown, never
    logged; the repr is counts only)."""

    answer: str
    sources: tuple[tuple[str, str], ...] = ()     # (title, url), in the model's order
    suggestions: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return (f"ResearchAnswer(answer={len(self.answer)} chars, sources={len(self.sources)}, "
                f"suggestions={len(self.suggestions)})")


def answer_from(data_: Any) -> ResearchAnswer | None:
    """structured_output as a ResearchAnswer, or None when it does not fit the schema."""
    if not isinstance(data_, dict):
        return None
    answer, sources, suggestions = data_.get("answer"), data_.get("sources"), data_.get("suggestions")
    if not isinstance(answer, str) or not isinstance(sources, list) or not isinstance(suggestions, list):
        return None
    answer = _one_line(answer)
    if len(answer) > ANSWER_CAP or len(sources) > SOURCES_CAP or len(suggestions) > SUGGESTIONS_CAP:
        return None
    pairs: list[tuple[str, str]] = []
    for item in sources:
        if not isinstance(item, dict):
            return None
        title, url = item.get("title"), item.get("url")
        if not isinstance(title, str) or not isinstance(url, str):
            return None
        if len(title) > TITLE_CAP or len(url) > SOURCE_URL_CAP:
            return None
        pairs.append((title, url))
    if not all(isinstance(line, str) and len(line) <= SUGGESTION_CAP for line in suggestions):
        return None
    return ResearchAnswer(answer, tuple(pairs), tuple(suggestions))


def read_research_result(event: Mapping[str, Any], init: InitSummary | None, *,
                         now: Callable[[], datetime] = lambda: datetime.now().astimezone()
                         ) -> ResearchAnswer | AskFailure:
    """The research run's result event -> ResearchAnswer or AskFailure. The answer needs a
    checked init and structured_output (never a text fallback)."""
    found = result_failure(event, init, now=now)
    if found is not None:
        return found
    structured = event.get("structured_output")
    if structured is None:
        return failure(GARBLED, "no_structured_output")
    answer = answer_from(structured)
    return answer if answer is not None else failure(GARBLED, "shape")


# --------------------------------------------------------------------------
# One research run
# --------------------------------------------------------------------------

RESEARCH_MESSAGES = {
    MAX_TURNS: "The web research ran out of turns; nothing was proposed - try a narrower question",
    SCHEMA_RETRIES: "The web research's answer could not be read; nothing was proposed",
    GARBLED: "The web research's answer could not be read; nothing was proposed",
    GUARD: ("Claude Code started the web research with something Jarvis does not allow, so it was stopped; "
            "nothing was proposed"),
    ERROR: "Claude Code failed during the web research; nothing was proposed",
}


def research_failure(found: AskFailure, timeout_s: float) -> AskFailure:
    """``found`` in the web research's words when it has the generic planner wording (a specific
    message, such as a stop at the cap or a local address, stays; limit, sign-in and cancel too)."""
    if found.message != MESSAGES.get(found.kind):
        return found
    if found.kind == TIMEOUT:
        return replace(found, message=f"The web research took too long ({int(timeout_s)} s: [research] "
                                      "timeout_seconds); nothing was proposed")
    words = RESEARCH_MESSAGES.get(found.kind)
    return replace(found, message=words) if words else found


def run_research(runner: Runner, argv: Sequence[str], prompt: str, *, env: Mapping[str, str], cwd: Path,
                 timeout_s: float, limits: ResearchLimits, question: str = "",
                 cancel: threading.Event | None = None, clock: Callable[[], float] = time.monotonic,
                 init_timeout_s: float = INIT_TIMEOUT_S, poll_s: float = POLL_S,
                 now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
                 progress: Progress | None = None, capture: bool = False) -> RunResult:
    """One research run: stream.run_cli with check_research_init, a WebTracker and
    read_research_result. ``progress`` also hears ("web_call", WebCall) and ("web_result",
    WebResult) as they happen; ``question`` (the owner's words) only tells whether a page read
    is one the owner named. RunResult.answer is the ResearchAnswer, RunResult.web the WebLog
    (also after a stop); failures are in the research's own words."""
    tracker = WebTracker(limits, question,
                         on_call=lambda call: _report(progress, "web_call", call),
                         on_result=lambda result: _report(progress, "web_result", result))

    def heard(what: str, value: Any) -> None:   # "stopped" in the research's own words too
        if progress is not None:
            progress(what, research_failure(value, timeout_s) if what == "stopped" and value is not None else value)

    result = run_cli(runner, argv, prompt, env=env, cwd=cwd, timeout_s=timeout_s,
                     read=lambda event, init: read_research_result(event, init, now=now),
                     init_check=partial(check_research_init, limits=limits), event_check=tracker.check,
                     cancel=cancel, clock=clock, init_timeout_s=init_timeout_s, poll_s=poll_s, now=now,
                     progress=heard if progress is not None else None, capture=capture)
    found = research_failure(result.failure, timeout_s) if result.failure is not None else None
    return replace(result, failure=found, web=tracker.log())


__all__ = [
    "FETCH", "FOUND_OTHER", "FOUND_OWNER", "FOUND_SEARCH", "NEUTRAL_WORDS", "QuestionCheck", "ResearchAnswer",
    "ResearchLimits", "SEARCH", "WebCall", "WebLog", "WebResult", "WebTracker", "build_research_prompt",
    "check_question", "check_research_init", "fetch_url_problem", "forced_question", "normalize_url",
    "read_research_result", "run_research",
]
