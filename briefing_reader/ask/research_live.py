"""The LIVE view's steps of a web research: what the research gets (research.input), the run
(research.run) with one web.search / web.fetch step per tool call as it happens, the check of its
answer (research.validate) and a web.source step for each cited page no page read pictured.

Memory only, on screen only. Every helper is quiet (live.quiet): whatever goes wrong in one, the
research goes on exactly as without the LIVE view. Web text (search results, what a page read
returned, the raw answer) is shown only in untrusted blocks and in plain-text items and fields;
URLs are never links ([live] text = false keeps sizes only). Pictures: each web search is drawn as
a compact results page from what it returned (pictures.search_picture; [live] pictures; nothing is
opened for it). With page snapshots on ([live] page_snapshots), a page the run read gets a picture
on its web.fetch step, and each source the answer cites (shown after the check; never a left-out
one) that no page read pictured gets its own web.source step with the page's picture, within the
same cap (reads first, then the sources in citation order); WebSteps(snapshots=) hands them to
snapshot_engine.PageSnapshotter, which decides and takes them on this PC, one at a time. A source
whose address Jarvis could not check (no search listed it, no page read loaded it) is never
opened for a picture: its step says why. The research run itself never sees any of it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from ..actions import ProposedAction, link_host
from ..live import (
    NO_STEP,
    PICTURE_LINK,
    PICTURE_READY,
    RESEARCH_INPUT,
    RESEARCH_RUN,
    RESEARCH_VALIDATE,
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_RUNNING,
    STATUS_WARN,
    WEB_FETCH,
    WEB_SEARCH,
    WEB_SOURCE,
    Field,
    LiveStep,
    LiveTask,
    command_line_text,
    elapsed_text,
    quiet,
)
from ..pictures import KIND_SEARCH, page_unavailable, search_picture
from .research import (
    FETCH,
    FOUND_OWNER,
    FOUND_SEARCH,
    RESULT_DENIED,
    RESULT_EMPTY,
    RESULT_ERROR,
    RESULT_OK,
    RESULT_REDIRECT,
    RESULT_UNREAD,
    SEARCH,
    QuestionCheck,
    ResearchAnswer,
    ResearchLimits,
    WebCall,
    WebLog,
    WebResult,
    normalize_url,
)
from .research_validate import UNCHECKED_WARNING, ResearchReport, WebSource, cards_words, reads_words, steps_words

INPUT_TITLE = "What the web research gets"
RUN_TITLE = "Web research run"
VALIDATE_TITLE = "Checking the answer"
STARTED_FORCED = "you (web:)"
STARTED_ROUTED = "the planner (planner run 1)"
NOT_GIVEN = "your calendar, mail, contacts, briefing, accounts, addresses and how Jarvis addresses you"
READ_FORCED = "nothing"
READ_ROUTED = "your calendar and briefing (and any mail read) for the planner only - none of it goes to the research"
ISOLATION_PASSED = "passed: only words you typed, dates and plain words"
ISOLATION_SAME = "not needed: it says what you typed"
TOOLS_WORDS = "WebSearch, WebFetch - nothing else"
TOOLS_SEARCH_ONLY = "WebSearch - nothing else (page reads are off: [research] max_fetches = 0)"
NO_WEB_FETCH_NOTE = "WebFetch is not available in this Claude Code: search only"
RUN_BY_SEARCH = "Claude Code's WebSearch (Anthropic's search service)"
RUN_BY_SERVER = "Claude's own web search (on Anthropic's side)"
RAW_ANSWER = "Research answer (raw)"
SEARCH_BLOCK = "What WebSearch returned to Claude"
RESULTS_BLOCK = "The results as text"     # a search drawn as a results page: its list, closed, instead of items
FETCH_BLOCK = "What WebFetch returned to Claude (its summary of the page)"
EXCERPT_BLOCK = "Excerpt of what WebFetch returned"
STOPPED_WORDS = "the run was stopped"
UNREAD_WORDS = "results came back as text Jarvis could not list"
DENIED_WORDS = "Claude Code refused it (its permission rules)"
RULES_WORDS = ("https pages the research saw, on any site but local or private ones; Todo, one-off Calendar and Open "
               "suggestions only")
# True whether its picture is taken, fails or is refused (past the cap, cancelled ...): the picture
# says which (NOTE_OPENING / NOTE_OPENED: opened on this PC to show you; else why not).
SOURCE_SUMMARY = "cited in the answer - not read by the research; its picture below"
SOURCE_NOT_READ = "no - the answer cites it from what the search listed"
PICTURE_HERE = "picture: see the {where} step"   # true whether or not that step's picture is taken
# A cited source Jarvis could not check (no search listed it, no page read loaded it: UNCHECKED_WARNING)
# is never opened for a picture: it stays an Open card, opened on the owner's click. Its step says why.
SOURCE_UNCHECKED_SUMMARY = "cited in the answer - not opened: Jarvis could not check its address"
UNCHECKED_NO_PICTURE = ("Jarvis opens no address he could not check (no search listed it, no page read loaded "
                        "it); its Open card opens it in your browser, on your click")
PICTURE_NONE = "no picture (its address could not be checked): see the {where} step"
EXCERPT_CHARS = 300
EXCERPT_LINES = 8
MAX_HITS_SHOWN = 50
_FOUND_WORDS = {FOUND_SEARCH: "a search result", FOUND_OWNER: "your words"}
FOUND_OTHER_WORDS = "not a search result - Claude followed a link or typed the address"
_STOPPED_KINDS = frozenset({"guard", "limit", "research_cap", "not_signed_in"})
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _count(count: int, noun: str, plural: str = "") -> str:
    return f"{count} {noun if count == 1 else (plural or noun + 's')}"


def today_words(now: datetime, time_zone: str, hour24: bool = False) -> str:
    """"Wed Oct 7 2026 9:30 AM, America/Los_Angeles"."""
    if hour24:
        clock = f"{now.hour:02d}:{now.minute:02d}"
    else:
        clock = f"{now.hour % 12 or 12}:{now.minute:02d} {'AM' if now.hour < 12 else 'PM'}"
    zone = time_zone or "time zone unknown"
    return f"{_DAYS[now.weekday()]} {_MONTHS[now.month - 1]} {now.day} {now.year} {clock}, {zone}"


def limits_words(limits: ResearchLimits, timeout_s: int) -> str:
    """"4 searches, 4 page reads, 10 turns, 120 s"."""
    reads = _count(limits.max_fetches, "page read") if limits.max_fetches > 0 else "no page reads"
    return (f"{_count(limits.max_searches, 'search', 'searches')}, {reads}, {_count(limits.max_turns, 'turn')}, "
            f"{int(timeout_s)} s")


def web_words(web: WebLog | None) -> str:
    """"2 searches, 1 page read, 0 refused" (research_validate.steps_words, plus the refusals): a
    page read that did not return the page is "not read"."""
    if web is None:
        return "none"
    return f"{steps_words(web)}, {web.denied} refused"


# --------------------------------------------------------------------------
# research.input
# --------------------------------------------------------------------------

@quiet(NO_STEP)
def input_step(live: LiveTask, *, forced: bool, question: str, hint: str, now: datetime, time_zone: str,
               hour24: bool = False, check: QuestionCheck | None = None, planner_question: str = "",
               planner_why: str = "", lines_left: int = 0) -> LiveStep:
    """What the research gets (exactly) and what it does not; for a routed research, the planner's
    question and whether it passed the isolation check (WARN when it did not)."""
    if not live.enabled:
        return NO_STEP
    fields: list[Field | tuple[str, str]] = [("Started by", STARTED_FORCED if forced else STARTED_ROUTED)]
    if not forced:
        fields += [Field("Planner's question", planner_question, mono=True), ("Planner's why", planner_why)]
        if check is not None:
            if check.ok and not hint:
                fields.append(("Isolation check", ISOLATION_SAME))
            elif check.ok:
                fields.append(("Isolation check", ISOLATION_PASSED))
            else:
                fields.append(("Isolation check", f"not used: {check.reason} - your own words are sent instead"))
    fields.append(Field("Question (exact)", question, mono=True))
    if hint:
        fields.append(Field("Search hint (exact)", hint, mono=True))
    fields += [("Today", today_words(now, time_zone, hour24)), ("Not given", NOT_GIVEN),
               ("Read from your accounts", READ_FORCED if forced else READ_ROUTED)]
    refused = not forced and check is not None and not check.ok
    step = live.step(RESEARCH_INPUT, INPUT_TITLE, fields=fields)
    if lines_left:
        step.note(f"{_count(lines_left, 'proposal line')} the planner wrote alongside its web request "
                  f"{'was' if lines_left == 1 else 'were'} left out")
    step.done(STATUS_WARN if refused else STATUS_OK,
              summary="the planner's question was not used: your own words are sent" if refused
              else "only your words and today's date")
    return step


# --------------------------------------------------------------------------
# research.run and its web steps
# --------------------------------------------------------------------------

@quiet(NO_STEP)
def run_step(live: LiveTask, *, model: str, prompt: str, limits: ResearchLimits, timeout_s: int) -> LiveStep:
    if not live.enabled:
        return NO_STEP
    from . import planner as _planner

    step = live.step(RESEARCH_RUN, RUN_TITLE, fields=[
        Field("Model", model, mono=True), ("Tools", TOOLS_WORDS if limits.max_fetches > 0 else TOOLS_SEARCH_ONLY),
        ("Limits", limits_words(limits, timeout_s)), ("Input", f"{len(prompt):,} characters")])
    step.block(_planner.STDIN_PENDING, prompt)   # only the owner's words and Jarvis's own: not untrusted
    return step


@quiet()
def run_ready(step: LiveStep, engine: Any, ready: Any) -> None:
    if step is NO_STEP:
        return
    hardened = "hardened (--safe-mode --restricted)" if ready.hardened else "not hardened"
    step.field("Claude Code", f"{ready.version or '?'}, {hardened}")
    method = ready.auth.method if ready.auth is not None else ""
    step.note(f"Sign-in checked again: {'claude.ai plan' if method == 'claude.ai' else method or 'ok'} "
              "(no model request)")
    try:
        argv = engine.argv(ready)
    except Exception:  # noqa: BLE001 - only what the view shows
        return
    step.field("Command line", command_line_text(argv), mono=True)


@quiet()
def run_skipped(step: LiveStep, message: str) -> None:
    from . import planner as _planner

    step.rename_block(_planner.STDIN_PENDING, _planner.STDIN_NOT_SENT)
    step.done(STATUS_BLOCKED, status_text="SKIPPED", summary=f"Not started: {message}")


class WebSteps:
    """The web.search / web.fetch steps of one run, created and closed from its progress (each a
    sibling after research.run, in arrival order, keyed "web:<tool id>"), and after the answer's
    check a web.source step for each cited page no page read pictured (cited()).

    A search that came back is drawn on its web.search step as a compact results page (the exact
    query, each result's title and address: pictures.search_picture, from what it returned; no
    page is opened for it); its results are then listed once more only as text in a closed block,
    not one item each (without the picture, or with [live] text = false, they stay items).

    ``snapshots`` (snapshot_engine.PageSnapshotter, or None): a page the run READ (WebFetch came
    back OK) is pictured on its web.fetch step, and each source the answer cites that no page read
    pictured on its own web.source step - the snapshotter decides whether it may be (public https
    only, never a search results page, at most 4 per run: the reads come first, then the sources
    in citation order; one at a time) and shows "Opening..." at once. A source Jarvis could not
    check (no search listed it, no page read loaded it) is never handed to it.
    finished() says the run asks for no more pictures. LIVE side only: nothing of this reaches the
    research run (its input, tools, command line and folder are unchanged)."""

    def __init__(self, live: LiveTask, limits: ResearchLimits, snapshots: Any = None) -> None:
        self.live = live
        self.limits = limits
        self.snapshots = snapshots
        self.steps: dict[str, LiveStep] = {}
        self.calls: dict[str, WebCall] = {}
        self.titles: dict[str, str] = {}      # normalized page -> the first search hit's title
        # normalized page (asked and final URL) -> the web.fetch step whose picture was asked for, its title
        self.pictured: dict[str, tuple[LiveStep, str]] = {}

    @quiet()
    def call(self, call: WebCall) -> None:
        if not self.live.enabled:
            return
        key = f"web:{call.tool_id}" if call.tool_id else ""
        if call.kind == SEARCH:
            fields: list[Field | tuple[str, str]] = [Field("Query (exact)", call.query, mono=True)]
            if call.allowed_domains:
                fields.append(("Only these sites", ", ".join(call.allowed_domains)))
            if call.blocked_domains:
                fields.append(("Not these sites", ", ".join(call.blocked_domains)))
            fields += [("Search", f"{call.number} of at most {self.limits.max_searches}"),
                       ("Run by", RUN_BY_SERVER if call.server else RUN_BY_SEARCH)]
            step = self.live.step(WEB_SEARCH, f"Web search {call.number}", fields=fields, key=key)
        else:
            fields = [Field("URL (asked)", call.url, mono=True), ("Domain", link_host(call.url) or "none")]
            if call.prompt:
                fields.append(("Asked of the page", call.prompt))
            fields += [("Found by", _FOUND_WORDS.get(call.found_by, FOUND_OTHER_WORDS)),
                       ("Page read", f"{call.number} of at most {self.limits.max_fetches}")]
            step = self.live.step(WEB_FETCH, f"Page read {call.number}", fields=fields, key=key)
        if call.tool_id:
            self.steps[call.tool_id] = step
            self.calls[call.tool_id] = call
        if call.refused:
            step.done(STATUS_BLOCKED, status_text="STOPPED",
                      summary=f"{call.refused} - Jarvis stopped the research (it may have started)")
        elif call.over:
            if call.kind == SEARCH:
                summary = (f"Jarvis stopped the research here: at most {_count(self.limits.max_searches, 'search', 'searches')} "
                           "([research] max_searches); this search may have started already")
            else:
                summary = (f"Jarvis stopped the research here: at most {_count(self.limits.max_fetches, 'page read')} "
                           "([research] max_fetches); this page read may have started already")
            step.done(STATUS_BLOCKED, status_text="OVER LIMIT", summary=summary)

    @quiet()
    def result(self, result: WebResult) -> None:
        step = self.steps.get(result.tool_id, NO_STEP)
        if step is NO_STEP:
            return
        took = elapsed_text(result.took_ms / 1000) if result.took_ms is not None else ""
        if result.kind == SEARCH:
            self._search(step, result, took, self.calls.get(result.tool_id))
        else:
            self._fetch(step, result, took, self.calls.get(result.tool_id))

    def _search(self, step: LiveStep, result: WebResult, took: str, call: WebCall | None = None) -> None:
        if result.status in (RESULT_DENIED, RESULT_ERROR):
            self._failed(step, result, "WebSearch")
            return
        drawn = call is not None and bool(self._search_picture(step, call, result))
        listed = f"{_count(len(result.hits), 'page')} listed"
        if result.groups > 1:
            listed += f" from {result.groups} searches"
        step.field("Results", listed)
        if took:
            step.field("Took (Claude Code says)", took)
        for title, url in result.hits:
            if title:
                self.titles.setdefault(normalize_url(url), title)
        lines = [f"{title or '(no title)'} - {url}" for title, url in result.hits[:MAX_HITS_SHOWN]]
        if drawn and lines and self.live.keep_text:
            # The picture lists them: the same lines once more only as text, closed (to copy or for
            # a screen reader), so the page reads after it stay close.
            step.block(RESULTS_BLOCK, "\n".join(lines), untrusted=True)
        else:
            for line in lines:
                step.item(line, status=STATUS_OK)
        if result.text:
            step.block(SEARCH_BLOCK, result.text, untrusted=True)
        if result.status == RESULT_OK:
            step.done(STATUS_OK, summary=_count(len(result.hits), "result"))
        elif result.status == RESULT_UNREAD:
            step.done(STATUS_WARN, status_text="UNREAD", summary=UNREAD_WORDS)
        else:
            step.done(STATUS_WARN, status_text="NOTHING FOUND", summary="no pages listed")

    def _fetch(self, step: LiveStep, result: WebResult, took: str, call: WebCall | None) -> None:
        if result.code is not None:
            step.field("HTTP", f"{result.code} {result.code_text}".strip())
        if result.size is not None:
            step.field("Size", f"{result.size:,} bytes")
        if took:
            step.field("Took (Claude Code says)", took)
        asked = call.url if call is not None else ""
        if result.final_url and normalize_url(result.final_url) != normalize_url(asked):
            step.field("Final URL", result.final_url, mono=True)
        title = self.titles.get(normalize_url(asked))
        if title:
            step.field("Title", title)
        if result.status in (RESULT_DENIED, RESULT_ERROR) and result.code is None:
            self._failed(step, result, "WebFetch")
            return
        if result.text:
            # Page text is shown only as untrusted ("Written by other people"): a short text open
            # in one block; a long one as an open excerpt and the whole text below it.
            flat = " ".join(result.text.split())
            if len(flat) > EXCERPT_CHARS or result.text.count("\n") > EXCERPT_LINES:
                step.block(EXCERPT_BLOCK, flat[:EXCERPT_CHARS], start_open=True, untrusted=True)
                step.block(FETCH_BLOCK, result.text, untrusted=True)
            else:
                step.block(FETCH_BLOCK, result.text, start_open=True, untrusted=True)
        if result.status == RESULT_REDIRECT:
            if result.redirect_to:
                step.field("Redirects to", result.redirect_to, mono=True)
            step.done(STATUS_WARN, status_text="REDIRECTED", summary="the page sent Claude Code elsewhere")
        elif result.status in (RESULT_DENIED, RESULT_ERROR):
            self._failed(step, result, "WebFetch")
        elif result.status == RESULT_OK:
            if call is not None:
                self._picture(step, call.url, (result.final_url,), f"Page read {call.number}")
            size = f" - {result.size:,} bytes" if result.size is not None else ""
            step.done(STATUS_OK, summary=f"{result.code if result.code is not None else 'read'}{size}")
        else:
            step.done(STATUS_WARN, summary="Jarvis could not read what came back")

    @quiet(False)
    def _search_picture(self, step: LiveStep, call: WebCall, result: WebResult) -> bool:
        """A search that came back, drawn as a compact results page ([live] pictures decides);
        True when the step now shows it (READY: kept, not refused or dropped)."""
        if step is NO_STEP or result.status not in (RESULT_OK, RESULT_EMPTY, RESULT_UNREAD):
            return False
        step.picture(search_picture(call.query, result.hits, only=call.allowed_domains,
                                    never=call.blocked_domains, unread=result.status == RESULT_UNREAD))
        view = step.view()
        shown = view.picture if view is not None else None
        return shown is not None and shown.kind == KIND_SEARCH and shown.state == PICTURE_READY

    @quiet()
    def _picture(self, step: LiveStep, url: str, also: Sequence[str] = (), title: str = "") -> None:
        """A page the run read: its picture (the snapshotter refuses what may not be opened). The
        page (and where it ended, ``also``) is remembered, so a source citing it points here."""
        if self.snapshots is not None and step is not NO_STEP:
            for page in (url, *also):
                if page:
                    self.pictured.setdefault(normalize_url(page), (step, title))
            self.snapshots.request(step, url, task_id=self.live.id)

    def pictures_sources(self) -> bool:
        """Whether cited() gives the sources pictures: a snapshotter, the LIVE view, [live] text, and
        page reads allowed ([research] max_fetches = 0: no page is opened for this research at all,
        as the app's per-research picture cap says too)."""
        return (self.snapshots is not None and self.live.enabled and self.live.keep_text
                and self.limits.max_fetches > 0)

    def read_picture(self, url: str) -> tuple[LiveStep, str] | None:
        """The web.fetch step already pictured for that page, and its title ("Page read 1")."""
        return self.pictured.get(normalize_url(url))

    @quiet()
    def cited(self, report: ResearchReport, check: LiveStep = NO_STEP) -> None:
        """After the answer's check: each source it shows (never a left-out one), in citation order,
        gets a picture - the page read's when the run read that page, else its own web.source step
        ("Source [1]: example.org") with the page's picture, asked of the snapshotter after the
        reads (the same cap per research). ``check`` (the research.validate step) gets a link to
        each picture ("Picture [1]"). A source Jarvis could not check (checked=False: no search
        listed it, no page read loaded it) is never opened: its step says why it has no picture
        (UNCHECKED_NO_PICTURE), and the check gets no link for it."""
        if not self.pictures_sources():
            return
        for source in report.sources:
            read = self.read_picture(source.url)
            if read is not None:
                target, title = read
            elif source.checked:
                target, title = self._source_step(source), f"Source [{source.number}]"
            else:
                self._source_step(source)
                continue
            view = target.view() if target is not NO_STEP else None
            if view is not None and check is not NO_STEP:
                check.field(f"Picture [{source.number}]", f"{title}: {source.domain}",
                            link=f"{PICTURE_LINK}{view.id}")

    def _source_step(self, source: WebSource) -> LiveStep:
        """The web.source step of a cited page no page read pictured: its picture asked of the
        snapshotter when a search listed its address, else none (never opened) and why."""
        fields: list[Field | tuple[str, str]] = [
            ("Cited as", f"[{source.number}] {source.title}"), Field("URL", source.url, mono=True),
            ("Domain", source.domain),
            ("Read by the research", SOURCE_NOT_READ) if source.checked else ("Check", UNCHECKED_WARNING)]
        step = self.live.step(WEB_SOURCE, f"Source [{source.number}]: {source.domain}", fields=fields,
                              key=f"source:{source.number}")
        if step is NO_STEP:
            return step
        if source.checked:
            step.done(STATUS_OK, summary=SOURCE_SUMMARY)
            self._request(step, source.url)
        else:
            step.done(STATUS_OK, summary=SOURCE_UNCHECKED_SUMMARY)
            step.picture(page_unavailable(source.url, UNCHECKED_NO_PICTURE))   # nothing is opened
        return step

    @quiet()
    def _request(self, step: LiveStep, url: str) -> None:
        self.snapshots.request(step, url, task_id=self.live.id)

    @quiet()
    def finished(self) -> None:
        """The run is over (done, stopped, failed or never started): it asks for no more pictures."""
        if self.snapshots is not None:
            self.snapshots.end_run(self.live.id)

    def _failed(self, step: LiveStep, result: WebResult, tool: str) -> None:
        if result.message:
            step.field("Claude Code said", result.message)
        if result.status == RESULT_DENIED:
            step.done(STATUS_BLOCKED, status_text="DENIED", summary=DENIED_WORDS)
        elif result.code is not None and result.code >= 400:
            step.done(STATUS_FAILED, summary=f"HTTP {result.code} {result.code_text}".strip())
        else:
            step.done(STATUS_FAILED, summary=f"{tool} failed: {result.message or 'no reason given'}")

    @quiet()
    def stopped(self) -> None:
        """The run was stopped: every web step still RUNNING ends CANCELLED."""
        for step in self.steps.values():
            view = step.view()
            if view is not None and view.status == STATUS_RUNNING:
                step.done(STATUS_CANCELLED, summary=STOPPED_WORDS)


def run_progress(step: LiveStep, web: WebSteps) -> Callable[[str, Any], None]:
    """research.run_research's progress for the LIVE view (notes on research.run, the web steps)."""
    def progress(what: str, value: Any) -> None:
        from . import planner as _planner

        if what == "started":
            step.rename_block(_planner.STDIN_PENDING, _planner.STDIN_SENT)
            step.note("Claude Code started")
        elif what == "init" and value is not None:
            mode = f", permission mode: {value.permission_mode}" if value.permission_mode else ""
            step.note(f"Checked its start: model {value.model or '?'}, tools: {', '.join(value.tools) or 'none'}, "
                      f"MCP servers: {value.mcp_servers}, API key source: {value.api_key_source}{mode}")
            if web.limits.max_fetches > 0 and "WebFetch" not in value.tools:
                step.note(NO_WEB_FETCH_NOTE)
        elif what == "turn":
            step.note(f"Claude is working (turn {value})")
        elif what == "web_call" and value is not None:
            web.call(value)
        elif what == "web_result" and value is not None:
            web.result(value)
        elif what == "result":
            step.note("Answer received")
        elif what == "stopped" and value is not None:
            step.note(f"Stopped: {value.message}")
            web.stopped()

    return progress


@quiet()
def run_result(step: LiveStep, result: Any, *, keep_text: bool = True) -> None:
    if step is NO_STEP:
        return
    from . import planner as _planner

    if result.failure is not None and result.failure.detail == "start":
        step.rename_block(_planner.STDIN_PENDING, _planner.STDIN_NOT_SENT)
    else:
        step.rename_block(_planner.STDIN_PENDING, _planner.STDIN_SENT)
    stats = result.stats
    step.field("Took", elapsed_text(stats.duration_ms / 1000))
    if stats.turns:
        step.field("Turns", str(stats.turns))
    if stats.input_tokens or stats.output_tokens or stats.cache_read_tokens or stats.cache_write_tokens:
        step.field("Tokens", f"{stats.input_tokens:,} in / {stats.output_tokens:,} out / "
                             f"{stats.cache_read_tokens:,} cache read / {stats.cache_write_tokens:,} cache write")
    if result.init is not None and result.init.model:
        step.field("Model (Claude Code says)", result.init.model, mono=True)
    step.field("Web steps", web_words(result.web))
    if result.reply:
        step.block(RAW_ANSWER, result.reply, untrusted=True)
    answer = result.answer
    if isinstance(answer, ResearchAnswer):
        if answer.answer:
            step.item(f"Answer: {answer.answer}")
        for number, (title, url) in enumerate(answer.sources, 1):
            step.item(f"Source [{number}]: {title} - {url}")
        for line in answer.suggestions:
            step.item(f"Suggests: {_planner.line_text(line, keep_text)}", mono=True)
    failure = result.failure
    if failure is None:
        found = answer if isinstance(answer, ResearchAnswer) else ResearchAnswer("")
        step.done(STATUS_OK, summary=f"{_count(len(found.sources), 'source')}, "
                                     f"{_count(len(found.suggestions), 'suggestion')}")
    elif failure.kind == "cancelled":
        step.done(STATUS_CANCELLED, summary="Cancel clicked")
    elif failure.kind in _STOPPED_KINDS:
        summary = _planner.OVERAGE_WORDS if failure.detail == "overage" else failure.message
        step.done(STATUS_BLOCKED, status_text="STOPPED", summary=summary)
    else:
        step.done(STATUS_FAILED, summary=failure.message)


# --------------------------------------------------------------------------
# research.validate
# --------------------------------------------------------------------------

@quiet()
def validated(live: LiveTask, cards: Sequence[ProposedAction], report: ResearchReport, answer: ResearchAnswer,
              web: WebLog, hour24: bool = False, web_steps: WebSteps | None = None) -> None:
    """The check of the answer: each source (accepted, unchecked or left out with its reason), each
    suggestion (a card or dropped with its reason); the web.fetch steps get the title the answer
    gives their page when no search listed it. With ``web_steps`` (page pictures on), each source
    shown then gets its picture (WebSteps.cited: the page read's, or a web.source step of its own)
    and the check points to it: the source's line names the step, a "Picture [n]" link shows it.
    An unchecked source gets none: its line says so and names its step (which says why)."""
    if not live.enabled:
        return
    from . import planner as _planner

    listed = f"{_count(web.searches, 'search', 'searches')} ({_count(web.hits, 'result')} listed), " \
             f"{reads_words(web)}"
    step = live.step(RESEARCH_VALIDATE, VALIDATE_TITLE, fields=[("Checked against", listed), ("Rules", RULES_WORDS)])
    accepted = {source.number: source for source in report.sources}
    left = dict(report.left_out)
    for number, (title, url) in enumerate(answer.sources, 1):
        source = accepted.get(number)
        if source is not None:
            text = f"[{number}] {source.domain} - {source.title}"
            where = _picture_words(web_steps, source)
            if source.checked:
                step.item(text, status=STATUS_OK, note=source.url + where)
            else:
                step.item(text, status=STATUS_WARN, note=f"{UNCHECKED_WARNING} - {source.url}{where}")
        else:
            shown = " ".join(str(title or "").split()) or "(no title)"
            step.item(f"[{number}] {shown} - {url}", status=STATUS_BLOCKED, status_text="LEFT OUT",
                      note=left.get(number, "left out"))
    by_id = {card.id: card for card in cards}
    suggested = list(report.suggested)
    dropped = list(report.dropped)
    for line in answer.suggestions:   # each line's outcome, in order (a repeated line has its own)
        made = next((entry for entry in suggested if entry[0] == line), None)
        if made is not None:
            suggested.remove(made)
            card = by_id.get(made[1])
            if card is None:
                continue
            notes = "; ".join(card.warnings)
            if card.error:
                step.item(_planner.card_text(card, hour24), status=STATUS_WARN, status_text="LISTED", note=card.error)
            else:
                step.item(_planner.card_text(card, hour24), status=STATUS_WARN if notes else STATUS_OK, note=notes)
            continue
        gone = next((entry for entry in dropped if entry[0] == line), None)
        if gone is not None:
            dropped.remove(gone)
            step.item(_planner.line_text(line, live.keep_text), status=STATUS_BLOCKED, status_text="LEFT OUT",
                      note=gone[1])
    _titles(live, report, web)
    problems = bool(report.left_out or report.dropped)
    if not answer.sources and not answer.suggestions:
        step.done(STATUS_OK, summary="no sources")
        return
    sources = {source.card_id for source in report.sources}
    proposals = sum(1 for card in cards if not card.error and card.id not in sources)
    summary = cards_words(len(report.sources), proposals)
    if report.left_out:
        summary += f", {_count(len(report.left_out), 'source')} left out"
    if report.dropped:
        summary += f", {_count(len(report.dropped), 'suggestion')} left out"
    step.done(STATUS_WARN if problems else STATUS_OK, summary=summary)
    if web_steps is not None:
        web_steps.cited(report, step)


def _picture_words(web_steps: WebSteps | None, source: WebSource) -> str:
    """" - picture: see the Page read 1 step" / " - picture: see the Source [2] step"; for a source
    Jarvis could not check (never opened) " - no picture (...): see the Source [2] step" ("" when
    the sources get no pictures)."""
    if web_steps is None or not web_steps.pictures_sources():
        return ""
    read = web_steps.read_picture(source.url)
    if read is None and not source.checked:
        return " - " + PICTURE_NONE.format(where=f"Source [{source.number}]")
    where = read[1] if read is not None and read[1] else f"Source [{source.number}]"
    return " - " + PICTURE_HERE.format(where=where)


def _titles(live: LiveTask, report: ResearchReport, web: WebLog) -> None:
    """Each web.fetch step: the page's title from a search hit, else the title the answer gives it."""
    titles = {normalize_url(source.url): source.title for source in report.sources}
    for call in web.calls:
        if call.kind != FETCH or not call.tool_id:
            continue
        step = live.find(WEB_FETCH, key=f"web:{call.tool_id}")
        if step is NO_STEP:
            continue
        found = web.title_of(call.url)
        if found:
            step.field("Title", found)
        elif normalize_url(call.url) in titles:
            step.field("Title (as the answer names it)", titles[normalize_url(call.url)])


@quiet()
def checks_research(step: LiveStep, caps: Any, usage: Any) -> None:
    """"Research runs left" on a checks step."""
    if step is NO_STEP:
        return
    hour = getattr(usage, "max_research_per_hour", "?")
    day = getattr(usage, "max_research_per_day", "?")
    step.field("Research runs left", f"{caps.left_research_hour} of {hour} this hour, "
                                     f"{caps.left_research_day} of {day} today")


@quiet()
def checks_tools(step: LiveStep, limits: ResearchLimits) -> None:
    if step is NO_STEP:
        return
    step.field("Tools", TOOLS_WORDS if limits.max_fetches > 0 else TOOLS_SEARCH_ONLY)


__all__ = ["INPUT_TITLE", "RUN_TITLE", "VALIDATE_TITLE", "WebSteps", "checks_research", "checks_tools", "input_step",
           "run_progress", "run_ready", "run_result", "run_skipped", "run_step", "validated"]
