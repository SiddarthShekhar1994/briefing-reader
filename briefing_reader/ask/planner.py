"""One Ask from request to cards (at most two planner runs), and the checks that need no model request.

    ClaudeEngine(ask_config, data_dir, runner, environ)
        readiness(refresh=False)   Readiness: claude.exe found, its flags probed, a claude.ai
                                   subscription sign-in, an empty work folder. No model request;
                                   cached until a run fails
        run(prompt, allow_search, cancel, progress, capture)   RunResult of one planner run
                                   (stream.run_planner; progress / capture feed the LIVE view)
    ResearchEngine(research_config, base)   web research on the same Claude Code: readiness (also
                                   --allowedTools and an empty research folder) and research runs
                                   (ask.research.run_research)
    GoogleSources(config, calendars, senders, readers)   what Jarvis may tell the planner about each
                                   account (no sign-in ever; reads only)
    AskPlanner(config, engine, sources, usage, research=)
        plan(command, briefing, page_ids, cancel, on_stage, live) -> AskOutcome (``live``: the Ask's
                                   LIVE view task: every check, read, run and line, as it happens)
        dry_run(command, briefing) -> DryRun (the exact stdin text, email text as counts)
        research_dry_run(question) -> DryRun (a "web:" request's research stdin and command line)
    briefing_context(...)          the briefing as Ask may see it (pending cards, sections, deadlines)

Web research: a request that starts with "web:" (or "research:") reads nothing from the accounts
and runs one research run with the owner's words; planner run 1 may instead answer web_research
{question, why} (when [research] allows it and the caps leave a research run and a planner run),
and the research then gets the owner's command as typed (the planner's question only as a search
hint, and only when every word in it is the owner's). The research run's stdin is
ask.research.build_research_prompt's: the question, the date and time zone and Jarvis's limits,
never the calendar, mail, contacts, briefing, accounts or addresses. Its answer becomes cards
through ask.research_validate (AskOutcome.research holds the answer and the sources).

An Ask: caps and readiness first (refused without a run), then the context: the calendar of every
signed-in account, the briefing, and the text of briefing threads the request clearly names (on
accounts with "gmail_read"). The first run may ask to search mail instead of planning
({account, query, why}); Jarvis checks that search (ask.mail.check_query), runs it read-only on
that account, takes at most 3 threads, trims them and runs the planner once more with them as
quoted data (both runs count against the caps). Every line becomes a card through
validate.to_cards. Thread text lives only in this call's memory: it is never logged, saved or
returned; with [live] text on, the LIVE view shows it on screen (memory only, this session).

Memory (the productivity bundle): ``AskPlanner.memory`` gives the owner's notes (memory.json) to
every planner context as a <memory> block (never to web research: the research paths never build
a context). With ``memory_suggestions`` a planner run 1 whose stdin has no <mail> may suggest one
note ("remember"); memory.check_suggestion keeps it only when grounded in that run's own context
and not dismissed before (AskOutcome.suggestion; saved only on the owner's click in the app).

The UI calls plan() on its Ask worker thread (it blocks: Google reads and up to two CLI runs);
``on_stage`` reports STAGE_* as it goes, and setting ``cancel`` ends a running CLI within a second.

Planner modes (the productivity bundle): plan_mode(request, inputs=, briefing=, ...) runs one mode
on the same thread and the same one-at-a-time lock as plan(). This build has the day plan
(ask.modes.MODE_PLAN, "plan my day"): the Ask caps plus the bundle's daily cap and the hourly
reserve for typed Asks, Claude Code ready, the personal calendar signed in; a fresh read of the day
being planned on every calendar account; the free stretches and what needs time (ask.dayplan),
where an empty day or a full one answers with no run at all; then one run with ClaudeEngine.run_mode
(its own system prompt and schema, no tools, no mail, no web, no second run) and its lines checked
by validate.to_cards(kinds={Calendar}, extra=dayplan.DayCheck) and keep_valid. Each valid block's
notes become "For: <the to-do it serves>" (Jarvis's own words) and ``serves`` names the Todo card.
plan_dry_run() shows the stdin with titles and notes as sizes (no run, nothing counted).

The meeting brief (ask.modes.MODE_PREP, "prep me for my 2 PM", spec 3.3): Ask on, nothing more up
front; the meeting read fresh (typed words resolved by ask.prep.resolve_meeting against today and
tomorrow: none, several, or one found by only some title words answer with no read at all; a
clicked meeting is found again by its calendar and id, wherever it moved; gone or ended stops).
Mail only when the meeting's account can be read, and only after Claude Code's readiness and the
caps (the bundle's too) were checked right then: else a brief of Jarvis's own, with no run (R12).
Each guest the meeting has (not the owner, a group or a guest who declined; at most
prep_max_guests) is looked up with Jarvis's own search (ask.prep.prep_query: that address, the
last prep_lookback_days days) after ask.prep.guest_allowed (R2), until 3 threads are found, one per
guest; those are read and given to one run (its own prompt and schema), whose answer is checked
(ask.prep.check_prep; its Reply lines by validate.to_cards with ask.prep.PrepCheck). No thread
found: the brief of Jarvis's own (who is coming, what each lookup found), no run. From the
follow-ups build a guest's newest thread on the meeting's account may come from the header scan's
participants index (ModeInputs.participants) instead of a search, always after guest_allowed (R2,
R3); a thread the planner marks as waiting on them, where Jarvis's own headers agree that the owner
wrote last, becomes a "Draft a nudge" target of the brief (PrepBrief.nudges, R32).

The meeting summary (ask.modes.MODE_MEETING, meetings part 2): the transcript of a meeting the owner
recorded is summarised only when he asked - his own "summarise it" tick on that meeting's start card (once
the transcript is ready), or his click on Summarise. The Ask caps and Claude Code ready (no bundle cap: it
counts as an Ask run); the transcript read on this thread from the meeting's folder (ModeRequest.payload,
an ask.meeting_summary.MeetingJob); one run with its own prompt and schema, the transcript marked as text
written by other people (data, never instructions) and the invite's guests by name only. The answer is checked
strictly
(meeting_summary.summary_from: the whole answer refused when it does not fit), then Jarvis's own checks
(meeting_summary.check_summary): on a linked call action items only for "you" or an invited guest, follow-ups
only to an invited guest (the invite's address, never one from the run) as Email cards through
validate.to_cards with meeting_summary.FollowupCheck; in person nothing is the owner's (every turn is "Room");
a meeting not linked to a calendar meeting gets no guests and keeps its follow-ups as plain text (no cards);
notes through memory.check_suggestion. Nothing is sent, added or kept here.

The drafted reply or nudge (ask.modes.MODE_NUDGE, a click on a FOLLOW-UPS row or under a brief,
spec 3.4): the Ask caps with the bundle's, Claude Code ready, the thread's account readable; the one
thread read in full (the only body read of a follow-up); ask.modes.nudge_guard stops with no run when
the owner replied since, they answered since or anything else changed; else one run with the
thread, <followup> and only the owner's own words from "Tell Jarvis what to say", and its one Reply
line checked by validate.to_cards with ask.modes.NudgeCheck.

A desktop request (ask.modes.MODE_DESKTOP, Jarvis on your desktop, P1.7): a request the owner typed that
no local route took (desktop.routes); ModeRequest.payload is an ask.desktop_mode.DesktopJob (its run id and
the context's candidates, names only). The Ask's checks (no bundle cap: it counts as an Ask run), one run
with its own prompt and schema and Ask's command line otherwise (no tools, no MCP, nothing saved, stdin
only), its answer read strictly (desktop_mode.plan_from: refused whole when it does not fit), then every
step checked by desktop.validate against this run's refs and the owner's typed words only (tiers, the NEVER
list, intent and scope licensing, the suspect rule, the phase gate, the rate caps). Nothing is opened here:
the outcome carries the checked plan (``brief``) and Jarvis's own words for it (``message``).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from ..actions import TODO, ProposedAction, link_host
from ..config import DEFAULT_ACCOUNT, Config
from ..gcal import CalendarError, CalendarNotSignedIn, EventBrief, local_timezone, zone_offset
from ..gmail import GmailError
from ..google_auth import PROBLEM_EXPIRED, PROBLEM_SCOPE, PROBLEM_SETUP, PROBLEM_SIGNED_OUT, logged_alias
from . import cli
from ..live import (
    ASK_CHECKS,
    ASK_VALIDATE,
    CALENDAR_READ,
    DESKTOP_FIND,
    DESKTOP_PLAN,
    DESKTOP_VALIDATE,
    MAIL_SEARCH,
    MAIL_THREAD,
    MEETING_INPUT,
    MEETING_VALIDATE,
    MODE_REQUEST,
    NO_STEP,
    NO_TASK,
    PLAN_FREE,
    PLAN_VALIDATE,
    PLANNER_RUN,
    PREP_MEETING,
    PREP_PEOPLE,
    PREP_VALIDATE,
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_WARN,
    Field,
    LiveStep,
    LiveTask,
    command_line_text,
    elapsed_text,
)
from ..live import quiet as _quiet
from ..pictures import CalendarRead, email_picture, plan_picture, week_picture
from .context import (
    COMMAND_CAP,
    SOURCE_OWNER,
    AccountInfo,
    AskContext,
    BriefingContext,
    ContextIndex,
    briefing_from_script,
    build_prompt,
    day_window,
    fit_mail,
    thread_text,
)
from .mail import (
    MAX_THREADS,
    MailPerson,
    MailReport,
    MailThread,
    QueryRefused,
    SearchGrounding,
    briefing_threads,
    check_query,
    report,
    thread_from_api,
    trim_threads,
)
from . import research_live
from .research import (
    ResearchAnswer,
    ResearchLimits,
    build_research_prompt,
    check_question,
    forced_question,
    hint_text,
    question_text,
    run_research,
    same_question,
)
from .research_validate import ResearchReport
from .research_validate import notes as research_notes
from .research_validate import validate as validate_research
from .stream import CANCELLED, OVERAGE, AskFailure, InitSummary, Plan, RunResult, RunStats, run_cli, run_planner
from .usage import KIND_RESEARCH, USAGE_FILE, CapCheck, UsageLog
from .validate import LINE_CARD, LINE_OVER_CAP, LINE_REFUSED, LINE_REPEAT, Cards, keep_valid, to_cards
from . import dayplan, desktop_mode, meeting_summary, modes, prep
from ..desktop import validate as desktop_validate
from .modes import (MODE_DESKTOP, MODE_KINDS, MODE_MAX_LINES, MODE_MEETING, MODE_NUDGE, MODE_PLAN, MODE_PREP,
                    MODE_USAGE, ModeInputs, ModeRequest)

logger = logging.getLogger(__name__)

STAGE_CONTEXT = "context"          # reading the calendar (and named briefing threads)
STAGE_PLANNING = "planning"        # the first planner run
STAGE_MAIL = "mail"                # running the search the planner asked for
STAGE_PLANNING_AGAIN = "planning_again"   # the second run, with the mail
STAGE_RESEARCH = "research"        # the web research run
STAGE_DAY = "day"                  # a day plan: reading the day being planned
STAGE_PREP_MAIL = "prep_mail"      # a meeting brief: Jarvis's own searches and reads of the guests' threads
STAGE_THREAD = "thread"            # a follow-up draft: reading the one thread the owner clicked

# Refusals before any run (AskOutcome.kind), besides stream's failure kinds.
EMPTY = "empty"
DISABLED = "disabled"
CAPS = "caps"
CLI_MISSING = "cli_missing"
NOT_SUBSCRIPTION = "not_subscription"
WORKDIR = "workdir"
NOT_READY = "not_ready"
RESEARCH_OFF = "research_off"                  # web: while [research] enabled = false
RESEARCH_LIMIT = "research_limit"              # the web research caps refuse it (the Ask caps allow it)
RESEARCH_UNSUPPORTED = "research_unsupported"  # Readiness.problem: this Claude Code lacks --allowedTools

AUTH_FRESH_S = 5.0        # run() trusts an auth status this recent; older, it asks again
MEETING_TIMEOUT_S = 240.0  # a meeting summary reads a long transcript: at least this long ([ask] timeout_seconds)
NO_TRANSCRIPT = "no_transcript"   # a meeting summary with no transcript to read (AskOutcome.kind)
NO_TRANSCRIPT_MESSAGE = "There is no transcript to summarise; nothing was proposed"
OVERAGE_PAUSE = timedelta(hours=5)   # the pause after an extra-usage signal that names no reset time

DISABLED_MESSAGE = "Ask Jarvis is off ([ask] enabled = false in config.toml)"
CLI_MISSING_MESSAGE = "Claude Code not found: set JARVIS_CLAUDE_EXE in .env or install it (README: Ask Jarvis)"
RESEARCH_OFF_MESSAGE = "Web research is off ([research] enabled = false in config.toml)"
RESEARCH_UNSUPPORTED_MESSAGE = "This Claude Code lacks --allowedTools, which web research needs"
EMPTY_WEB_MESSAGE = "Type what to look up after web:"
BOTH_MESSAGE = "The planner also asked for web research; Jarvis read mail first - ask again for the web"
RESEARCH_LIMIT_NOTE = "the web research limit is reached"


def _caps_kind(caps: CapCheck) -> str:
    """A refusal by the caps: RESEARCH_LIMIT for the web research caps, else CAPS."""
    return RESEARCH_LIMIT if caps.research_limit else CAPS


# --------------------------------------------------------------------------
# The engine: one claude.exe, checked
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Readiness:
    ok: bool
    # "" | cli_missing | cli_unsupported | not_signed_in | not_subscription | workdir | error
    problem: str = ""
    message: str = ""                 # safe to show and log (no path, no address)
    exe: cli.ClaudeExe | None = None
    version: str = ""
    hardening: bool = False           # --safe-mode and --restricted are both available
    hardened: bool = False            # ... and [ask] hardened_flags uses them
    auth: cli.AuthStatus | None = None
    research_missing: tuple[str, ...] = ()   # flags web research needs that --help lacks (Ask stays ok)


class ClaudeEngine:
    """The owner's Claude Code CLI for Ask: readiness (no model request) and planner runs."""

    def __init__(self, ask: Any, data_dir: Path, *, runner: cli.Runner | None = None,
                 environ: Mapping[str, str] | None = None,
                 locate: Callable[[Mapping[str, str]], cli.ClaudeExe | None] = cli.locate_claude,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.ask = ask
        self.work_folder = Path(data_dir) / cli.WORK_FOLDER
        self.runner = runner or cli.SubprocessRunner()
        self._environ = environ
        self._locate = locate
        self._clock = clock
        self._lock = threading.Lock()
        self._ready: Readiness | None = None
        self._auth_at: float | None = None      # self._clock() of the last passing auth status

    def _env_source(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    def readiness(self, *, refresh: bool = False) -> Readiness:
        """Is Ask's CLI usable now? Cached for the app session once it is; a failed check (or a
        failed run) is checked again next time. Runs only --version, --help and auth status. The
        cache is for the chip and the refusals: run() asks auth status again before every run."""
        with self._lock:
            if self._ready is not None and not refresh:
                return self._ready
        ready = self._check()
        with self._lock:
            self._ready = ready if ready.ok else None
            self._auth_at = self._clock() if ready.ok else None
        logger.info("Ask: readiness %s (Claude Code %s from %s, sign-in %s, hardening %s)",
                    "ok" if ready.ok else ready.problem, ready.version or "?",
                    ready.exe.source if ready.exe else "-",
                    ready.auth.method if ready.auth and ready.auth.method else "-",
                    "on" if ready.hardened else "off")
        return ready

    def _check(self) -> Readiness:
        environ = self._env_source()
        exe = self._locate(environ)
        if exe is None:
            return Readiness(False, CLI_MISSING, CLI_MISSING_MESSAGE)
        env = cli.clean_env(environ)
        problem = cli.prepare_work_folder(self.work_folder)
        if problem:
            return Readiness(False, WORKDIR, problem, exe)
        try:
            probe = cli.probe(exe.path, self.runner, env, self.work_folder)
        except OSError as exc:
            return Readiness(False, "error", f"Claude Code could not be started ({type(exc).__name__})", exe)
        missing = probe.research_missing
        if not probe.ok:
            return Readiness(False, "cli_unsupported", probe.message, exe, probe.version, probe.hardening,
                             research_missing=missing)
        try:
            auth = cli.auth_status(exe.path, self.runner, env, self.work_folder)
        except OSError as exc:
            return Readiness(False, "error", f"Claude Code could not be started ({type(exc).__name__})", exe,
                             probe.version, probe.hardening, research_missing=missing)
        hardened = probe.hardening and bool(getattr(self.ask, "hardened_flags", True))
        if not auth.ok:
            return Readiness(False, auth.problem or "error", auth.message, exe, probe.version, probe.hardening,
                             hardened, auth, missing)
        return Readiness(True, "", "", exe, probe.version, probe.hardening, hardened, auth, missing)

    def argv(self, ready: Readiness, *, allow_search: bool, allow_web: bool = False,
             allow_remember: bool = False) -> list[str]:
        assert ready.exe is not None
        return cli.build_argv(ready.exe.path, model=self.ask.model, max_turns=self.ask.max_turns,
                              schema=cli.schema_text(allow_search=allow_search, allow_web=allow_web,
                                                     allow_remember=allow_remember),
                              hardened=ready.hardened)

    def preflight(self) -> tuple[Readiness, AskFailure | None]:
        """Right before a planner run: readiness (the CLI and its flags cached), an empty work
        folder, and the sign-in again: it must still be a claude.ai plan sign-in (`claude auth
        status`, no model request), whatever it was when Jarvis started. (readiness, why not)."""
        from .stream import NOT_SIGNED_IN, failure

        ready = self.readiness()
        if not ready.ok:
            return ready, failure(ready.problem if ready.problem in ("not_signed_in", "cli_unsupported") else "error",
                                  ready.problem, ready.message)
        problem = cli.prepare_work_folder(self.work_folder)
        if problem:
            self.forget()
            return ready, failure("error", WORKDIR, problem)
        auth = self._fresh_auth(ready, cli.clean_env(self._env_source()))
        if not auth.ok:
            self.forget()
            logger.info("Ask: the Claude Code sign-in is no longer a claude.ai plan sign-in (%s); no planner run",
                        auth.problem or "error")
            kind = NOT_SIGNED_IN if auth.problem in ("not_signed_in", NOT_SUBSCRIPTION) else "error"
            return ready, failure(kind, auth.problem or "auth", auth.message)
        return ready, None

    def run(self, prompt: str, *, allow_search: bool, cancel: threading.Event | None = None,
            progress: Callable[[str, Any], None] | None = None, capture: bool = False,
            allow_web: bool = False, allow_remember: bool = False) -> RunResult:
        """One planner run with ``prompt`` on stdin, after preflight(). ``progress`` / ``capture``:
        stream.run_planner's (the LIVE view; neither changes the run). ``allow_web``: the run may
        hand the request to web research (planner run 1 only); ``allow_remember``: it may suggest
        one note to remember (planner run 1 without mail, with [productivity] memory_suggestions)."""
        from .stream import failure

        ready, refused = self.preflight()
        if refused is not None:
            return RunResult(failure=refused)
        if cancel is not None and cancel.is_set():
            return RunResult(failure=failure(CANCELLED, "cancel"))
        env = cli.clean_env(self._env_source())
        argv = self.argv(ready, allow_search=allow_search, allow_web=allow_web, allow_remember=allow_remember)
        result = run_planner(self.runner, argv, prompt, env=env, cwd=self.work_folder,
                             timeout_s=float(self.ask.timeout_seconds), allow_search=allow_search, cancel=cancel,
                             clock=self._clock, progress=progress, capture=capture, allow_web=allow_web,
                             allow_remember=allow_remember)
        init = result.init
        logger.info("Ask: planner run %s in %.1f s (%d turn(s), %d in / %d out / %d cached tokens; tools %s, "
                    "structured output %s, exit %s)", result.outcome, result.stats.duration_ms / 1000,
                    result.stats.turns, result.stats.input_tokens, result.stats.output_tokens,
                    result.stats.cache_read_tokens, ",".join(init.tools) if init else "-",
                    "yes" if init and init.structured_tool else "no", result.stats.exit_code)
        if result.failure is not None and result.failure.kind != CANCELLED:
            self.forget()
        return result

    def mode_argv(self, ready: Readiness, mode: str, *, allow_remember: bool = False) -> list[str]:
        """A planner mode's command line: Ask's exactly, but its own system prompt and schema."""
        assert ready.exe is not None
        return cli.build_argv(ready.exe.path, model=self.ask.model, max_turns=self.ask.max_turns,
                              schema=cli.mode_schema_text(mode, allow_remember=allow_remember),
                              hardened=ready.hardened, prompt_file=cli.MODE_PROMPTS[mode])

    def run_mode(self, prompt: str, mode: str, *, cancel: threading.Event | None = None,
                 progress: Callable[[str, Any], None] | None = None, capture: bool = False,
                 allow_remember: bool = False) -> RunResult:
        """One run of a planner mode (ask.modes: "plan", "prep") after preflight(): the same Claude
        Code, clean environment, empty work folder, deny-all settings, flags and checks as run()
        (stream.check_init / check_event), with the mode's own prompt and schema (never gmail_search
        or web_research) and the mode's reader: a plan's answer is a Plan (RunResult.plan), a brief's
        a prep.PrepAnswer (RunResult.answer). ``allow_remember``: it may suggest one note (a plan
        never carries mail; a brief always does, so never)."""
        from .stream import failure

        ready, refused = self.preflight()
        if refused is not None:
            return RunResult(failure=refused)
        if cancel is not None and cancel.is_set():
            return RunResult(failure=failure(CANCELLED, "cancel"))
        env = cli.clean_env(self._env_source())
        # Mail, the summary's own notes, or names other people wrote on the desktop: never "remember".
        if mode in (MODE_PREP, MODE_NUDGE, MODE_MEETING, MODE_DESKTOP):
            allow_remember = False
        argv = self.mode_argv(ready, mode, allow_remember=allow_remember)
        if mode == MODE_PREP:
            result = run_cli(self.runner, argv, prompt, env=env, cwd=self.work_folder,
                             timeout_s=float(self.ask.timeout_seconds),
                             read=lambda event, init: prep.read_prep_result(event, init), cancel=cancel,
                             clock=self._clock, progress=progress, capture=capture)
        elif mode == MODE_MEETING:   # a long transcript: a longer timeout, its own strict reader
            result = run_cli(self.runner, argv, prompt, env=env, cwd=self.work_folder,
                             timeout_s=min(300.0, max(float(self.ask.timeout_seconds), MEETING_TIMEOUT_S)),
                             read=meeting_summary.read_summary_result, cancel=cancel,
                             clock=self._clock, progress=progress, capture=capture)
        elif mode == MODE_DESKTOP:   # a plan of refs, read strictly (desktop_mode.plan_from); validate decides
            result = run_cli(self.runner, argv, prompt, env=env, cwd=self.work_folder,
                             timeout_s=float(self.ask.timeout_seconds),
                             read=desktop_mode.read_desktop_result, cancel=cancel,
                             clock=self._clock, progress=progress, capture=capture)
        else:
            result = run_planner(self.runner, argv, prompt, env=env, cwd=self.work_folder,
                                 timeout_s=float(self.ask.timeout_seconds), allow_search=False, cancel=cancel,
                                 clock=self._clock, progress=progress, capture=capture, allow_web=False,
                                 allow_remember=allow_remember)
        init = result.init
        logger.info("Ask: planner run %s in %.1f s (%d turn(s), %d in / %d out / %d cached tokens; tools %s, "
                    "structured output %s, exit %s) (mode %s)", result.outcome, result.stats.duration_ms / 1000,
                    result.stats.turns, result.stats.input_tokens, result.stats.output_tokens,
                    result.stats.cache_read_tokens, ",".join(init.tools) if init else "-",
                    "yes" if init and init.structured_tool else "no", result.stats.exit_code, mode)
        if result.failure is not None and result.failure.kind != CANCELLED:
            self.forget()
        return result

    def _fresh_auth(self, ready: Readiness, env: Mapping[str, str]) -> cli.AuthStatus:
        """`claude auth status` now, unless readiness ran it moments ago (AUTH_FRESH_S)."""
        with self._lock:
            checked_at = self._auth_at
        if ready.auth is not None and checked_at is not None and self._clock() - checked_at < AUTH_FRESH_S:
            return ready.auth
        assert ready.exe is not None
        try:
            auth = cli.auth_status(ready.exe.path, self.runner, env, self.work_folder)
        except OSError as exc:
            return cli.AuthStatus(False, problem="error",
                                  message=f"Claude Code could not be started ({type(exc).__name__})")
        if auth.ok:
            with self._lock:
                self._auth_at = self._clock()
        return auth

    def forget(self) -> None:
        """Check readiness again before the next run."""
        with self._lock:
            self._ready = None
            self._auth_at = None


class ResearchEngine:
    """Web research on the owner's Claude Code: the same claude.exe, probe, sign-in checks, clean
    environment and runner as ClaudeEngine (``base``); its own work folder, argv and stream checks
    (ask.research). Its only tools are WebSearch and WebFetch; it is given only its stdin."""

    def __init__(self, research: Any, base: ClaudeEngine) -> None:
        self.research = research
        self.base = base
        self.work_folder = base.work_folder.parent / cli.RESEARCH_WORK_FOLDER

    @property
    def limits(self) -> ResearchLimits:
        return ResearchLimits.of(self.research)

    def _research_checks(self, ready: Readiness) -> Readiness:
        if ready.research_missing:
            return replace(ready, ok=False, problem=RESEARCH_UNSUPPORTED, message=RESEARCH_UNSUPPORTED_MESSAGE)
        problem = cli.prepare_work_folder(self.work_folder, what="Web research", folder=cli.RESEARCH_WORK_FOLDER)
        if problem:
            return replace(ready, ok=False, problem=WORKDIR, message=problem)
        return ready

    def readiness(self) -> Readiness:
        """The base engine's readiness (cached; no model request), then this Claude Code's
        --allowedTools and an empty research folder. Never raises."""
        try:
            ready = self.base.readiness()
            return self._research_checks(ready) if ready.ok else ready
        except Exception as exc:  # noqa: BLE001 - a check must never take the Ask down
            return Readiness(False, "error", f"Web research could not be checked ({type(exc).__name__})")

    def preflight(self) -> tuple[Readiness, AskFailure | None]:
        """Right before a research run: the base engine's preflight (a fresh `claude auth status`,
        the Ask folder), then the two research checks. (readiness, why not)."""
        from .stream import failure

        ready, refused = self.base.preflight()
        if refused is not None:
            return ready, refused
        checked = self._research_checks(ready)
        if not checked.ok:
            return checked, failure("error", checked.problem, checked.message)
        return ready, None

    def argv(self, ready: Readiness) -> list[str]:
        assert ready.exe is not None
        return cli.build_research_argv(ready.exe.path, model=self.research.model, max_turns=self.limits.max_turns,
                                       schema=cli.research_schema_text(), hardened=ready.hardened,
                                       web_fetch=self.limits.max_fetches > 0)

    def run(self, prompt: str, *, question: str = "", cancel: threading.Event | None = None,
            progress: Callable[[str, Any], None] | None = None, capture: bool = False) -> RunResult:
        """One research run with ``prompt`` on stdin, after preflight() (research.run_research;
        ``question``: the owner's words, only to tell which page reads he named)."""
        from .stream import failure

        ready, refused = self.preflight()
        if refused is not None:
            return RunResult(failure=refused)
        if cancel is not None and cancel.is_set():
            return RunResult(failure=failure(CANCELLED, "cancel"))
        base = self.base
        result = run_research(base.runner, self.argv(ready), prompt, env=cli.clean_env(base._env_source()),
                              cwd=self.work_folder, timeout_s=float(self.research.timeout_seconds), limits=self.limits,
                              question=question, cancel=cancel, clock=base._clock, progress=progress, capture=capture)
        init, stats, web = result.init, result.stats, result.web
        logger.info("Research: run %s in %.1f s (%d turn(s), %d search(es), %d page read(s), %d refused; %d in / "
                    "%d out / %d cached tokens; tools %s; exit %s)", result.outcome, stats.duration_ms / 1000,
                    stats.turns, web.searches if web else 0, web.fetches if web else 0, web.denied if web else 0,
                    stats.input_tokens, stats.output_tokens, stats.cache_read_tokens,
                    ",".join(init.tools) if init else "-", stats.exit_code)
        if result.failure is not None and result.failure.kind != CANCELLED:
            base.forget()
        return result


CHIP_OK = "OK"
CHIP_SIGN_IN = "SIGN IN"
CHIP_LIMIT = "LIMIT"
CHIP_OFF = "OFF"
CHIP_ERR = "ERR"


def chip_state(enabled: bool, ready: Readiness | None, caps: CapCheck | None = None, *,
               research: bool = False) -> tuple[str, str]:
    """(the header's `claude` chip state, its tooltip): OFF while [ask] is off, SIGN IN without a
    claude.ai plan sign-in, ERR when Claude Code is missing, unsupported or its folder is not empty,
    LIMIT when the caps refuse the next run, else OK (with the runs left this hour; with
    ``research`` on, the web research runs left too, or that its limit is reached)."""
    if not enabled:
        return CHIP_OFF, DISABLED_MESSAGE
    if ready is None:
        return CHIP_ERR, "Ask has not checked Claude Code yet"
    if not ready.ok:
        if ready.problem in ("not_signed_in", NOT_SUBSCRIPTION):
            return CHIP_SIGN_IN, ready.message
        return CHIP_ERR, ready.message
    if caps is not None and not caps.allowed:
        return CHIP_LIMIT, caps.message
    left = ""
    if caps is not None:
        runs = f"{caps.left_hour} planner run(s)"
        research_left = min(caps.left_hour, caps.left_research_hour, caps.left_research_day)
        if research and min(caps.left_research_hour, caps.left_research_day) <= 0:
            left = f"; {runs} left this hour; {RESEARCH_LIMIT_NOTE}"
        elif research:
            left = f"; {runs} left this hour; {research_left} web research run(s) left"
        else:
            left = f"; {runs} left this hour"
    return CHIP_OK, f"Ask uses your Claude plan (Claude Code {ready.version or '?'}){left}"


# --------------------------------------------------------------------------
# What Jarvis may tell the planner about each account
# --------------------------------------------------------------------------

class Sources(Protocol):
    def accounts(self) -> list[AccountInfo]: ...

    def time_zone(self) -> str: ...

    def event_briefs(self, alias: str, start: datetime, end: datetime,
                     calendars: Sequence[str]) -> tuple[list[EventBrief], str]: ...

    def reader(self, alias: str) -> Any | None: ...


READ_NOT_ALLOWED = "no (not allowed in the Google sign-in)"
READ_NEEDS_SIGN_IN = "no (needs a Google sign-in)"
READ_OFF = "no (turned off: [ask] read_mail)"
READ_NOT_SET_UP = "no (not set up)"
READ_LIMIT = "no (the Ask limit leaves one run)"
_READ_WORDS = {PROBLEM_SCOPE: READ_NOT_ALLOWED, PROBLEM_SIGNED_OUT: READ_NEEDS_SIGN_IN,
               PROBLEM_EXPIRED: READ_NEEDS_SIGN_IN, PROBLEM_SETUP: READ_NOT_SET_UP}


class GoogleSources:
    """The app's own Google objects (executor.build_calendars / build_senders / build_readers),
    used read-only and never to sign in."""

    def __init__(self, config: Config, calendars: Mapping[str, Any], senders: Mapping[str, Any] | None = None,
                 readers: Mapping[str, Any] | None = None) -> None:
        self.config = config
        self.calendars = dict(calendars)
        self.senders = dict(senders or {})
        self.readers = dict(readers or {})

    def aliases(self) -> list[str]:
        names = list(self.config.accounts)
        for group in (self.calendars, self.senders, self.readers):
            names += [alias for alias in group if alias not in names]
        return names

    def accounts(self) -> list[AccountInfo]:
        found = []
        for alias in self.aliases():
            calendar = self.calendars.get(alias)
            sender = self.senders.get(alias)
            reader = self.readers.get(alias)
            address = ""
            for handle in (sender, calendar):
                account = getattr(handle, "account", None)
                getter = getattr(account, "bound_email", None)
                if callable(getter):
                    address = getter() or address
                if address:
                    break
            if calendar is None:
                status = "no"
            else:
                status = "yes" if _call_bool(calendar, "is_signed_in") else "not signed in"
            read = READ_NOT_SET_UP
            if not self.config.ask.read_mail:
                read = READ_OFF
            elif reader is not None:
                problem, _ = reader.available()
                read = "yes" if not problem else _READ_WORDS.get(problem, "no (not available now)")
            zone = ""
            if calendar is not None and status == "yes":
                try:
                    zone = calendar.timezone(interactive=False)
                except Exception:  # noqa: BLE001 - only what the planner is told
                    zone = ""
            found.append(AccountInfo(alias, address, status, sender is not None, read, zone))
        return found

    def time_zone(self) -> str:
        calendar = self.calendars.get("personal")
        if calendar is not None and _call_bool(calendar, "is_signed_in"):
            try:
                return calendar.timezone(interactive=False)
            except Exception:  # noqa: BLE001
                pass
        return local_timezone()

    def cached_time_zone(self) -> str:
        """The personal calendar's time zone only when it was read already, else this PC's: no
        Google request (web research)."""
        calendar = self.calendars.get("personal")
        cached = getattr(calendar, "cached_timezone", None) if calendar is not None else None
        try:
            zone = cached() if callable(cached) else None
        except Exception:  # noqa: BLE001 - only what the research is told
            zone = None
        return zone if isinstance(zone, str) and zone else local_timezone()

    def event_briefs(self, alias: str, start: datetime, end: datetime,
                     calendars: Sequence[str]) -> tuple[list[EventBrief], str]:
        calendar = self.calendars.get(alias)
        if calendar is None:
            return [], "no"
        try:
            return list(calendar.list_event_briefs(start, end, calendars)), "yes"
        except CalendarNotSignedIn:
            return [], "not signed in"
        except CalendarError:
            return [], "error"
        except Exception as exc:  # noqa: BLE001 - one account's calendar must not stop the Ask
            logger.warning("Ask: reading the %s calendar failed unexpectedly (%s)", logged_alias(alias),
                           type(exc).__name__)
            return [], "error"

    def reader(self, alias: str) -> Any | None:
        return self.readers.get(alias)


def unreadable_note(account: str, known: AccountInfo | None) -> str:
    """Why Jarvis did not read ``account``'s mail, in plain words with what to do first."""
    if known is None:
        return f"Jarvis didn't search mail: there is no {account or 'such'} account"
    why = known.read_mail
    if why == READ_NOT_ALLOWED or (why == READ_NEEDS_SIGN_IN and known.calendar == "yes"):
        return f'Jarvis couldn\'t read your {account} mail: click "Allow {account} mail" to let it'
    if why == READ_NEEDS_SIGN_IN:
        return f"Jarvis couldn't read your {account} mail: sign in to the {account} account first"
    if why == READ_OFF:
        return f"Jarvis didn't read your {account} mail: reading mail is off ([ask] read_mail)"
    if why == READ_NOT_SET_UP:
        return f'Jarvis can\'t read {account} mail: add "gmail_read" to that account in config.toml'
    if why == READ_LIMIT:
        return "Jarvis didn't search your mail: this hour's Ask limit leaves only one planner run"
    return f"Jarvis couldn't read your {account} mail right now"


def needs_mail_sign_in(account: str, ctx: AskContext) -> str:
    """``account`` when one more Google sign-in would let Jarvis read its mail ("" otherwise)."""
    known = next((item for item in ctx.accounts if item.alias == account), None)
    if known is not None and (known.read_mail == READ_NOT_ALLOWED
                              or (known.read_mail == READ_NEEDS_SIGN_IN and known.calendar == "yes")):
        return account
    return ""


def _call_bool(handle: Any, name: str) -> bool:
    getter = getattr(handle, name, None)
    try:
        return bool(getter()) if callable(getter) else False
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# One Ask
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AskOutcome:
    ok: bool
    kind: str                                    # "ok", a refusal (EMPTY, DISABLED, CAPS...) or a failure kind
    message: str = ""                            # Jarvis's own words for the status line
    say: str = ""                                # the planner's (shown, spoken from P3; never logged)
    question: str = ""
    cards: tuple[ProposedAction, ...] = ()
    dropped: int = 0                             # lines beyond [ask] max_cards
    refused: int = 0                             # cards that are information only
    runs: int = 0                                # planner runs this Ask used (they count against the caps)
    duration_ms: int = 0
    mail: MailReport = field(default_factory=MailReport)
    search: str = ""                             # the Gmail search Jarvis ran (shown; never logged)
    search_account: str = ""
    mail_needed: str = ""                        # the account whose mail the planner wanted but one more
                                                 # Google sign-in must allow ("Allow <alias> mail")
    init: InitSummary | None = None
    stats: tuple[RunStats, ...] = ()
    caps: CapCheck | None = None                 # what is left after this Ask
    research: ResearchReport | None = None       # a web research's answer and sources (shown; never logged)
    # What this Ask read from the calendars (pictures.CalendarRead: the LIVE day pictures draw the
    # other events of a day from it; memory only, never logged, never in summary()).
    calendar_read: Any = field(default=None, repr=False, compare=False)
    # A note the planner suggested keeping (memory.check_suggestion passed; shown with "Remember this",
    # saved only on the owner's click; never logged, never spoken).
    suggestion: str = field(default="", repr=False)
    # A planner mode's answer (the productivity bundle): its mode ("plan", "prep"), a meeting brief
    # (prep.PrepBrief: shown, never logged or spoken), meetings to pick from (prep.PrepChoice: "Prep
    # this one"), the day a plan is for, why it did not run (a word: a plan's "day_full", "nothing",
    # "part_over", "no_hours", "no_calendar"; a brief's "no_match", "gone", "ended", "no_calendar",
    # "choices", or with a brief of Jarvis's own "no_thread", "mail_off", "unreadable", "caps",
    # "not_ready"; its words are ``message``), the plan's window (dayplan.PlanWindow: the UI
    # replaces only the earlier plan cards it overlaps), its free stretches and the most blocks it
    # may give.
    mode: str = ""
    brief: Any = field(default=None, repr=False, compare=False)
    choices: tuple[Any, ...] = field(default=(), repr=False)
    plan_for: date | None = None
    note_kind: str = ""
    window: Any = field(default=None, repr=False, compare=False)
    slots: int = 0
    blocks_max: int = 0
    # A follow-up draft's thread (ask.modes.ThreadTarget: memory only; the UI re-fetches its row after a
    # guard stop and finds the card's thread).
    target: Any = field(default=None, repr=False, compare=False)

    def summary(self) -> str:
        """For the log: kinds and counts only."""
        kinds: dict[str, int] = {}
        for card in self.cards:
            name = "refused" if card.error else card.kind
            kinds[name] = kinds.get(name, 0) + 1
        listed = ", ".join(f"{kind} {count}" for kind, count in kinds.items()) or "none"
        text = (f"{self.kind}: {len(self.cards)} card(s) ({listed}), "
                f"{self.dropped} dropped, {self.runs} run(s), {self.duration_ms / 1000:.1f} s, "
                f"mail {self.mail.threads} thread(s) / {self.mail.messages} message(s)")
        report = self.research
        if report is not None:
            text += (f", research: {len(report.sources)} source(s), {len(report.left_out)} left out, "
                     f"{report.searches} search(es), {report.fetches} page read(s)")
        if self.mode:
            text += f", mode {self.mode}"
            brief = self.brief
            if self.mode == MODE_PREP and brief is not None:
                text += (f", brief threads {len(brief.threads)} / open {len(brief.open)} / points "
                         f"{len(brief.points)}, {brief.dropped} dropped")
            if self.mode == MODE_MEETING and brief is not None:
                text += ", summary " + ", ".join(f"{name} {count}" for name, count in brief.counts().items())
            if self.mode == MODE_DESKTOP and brief is not None:
                text += ", desktop plan " + ", ".join(f"{name} {count}" for name, count in brief.counts().items())
            if self.note_kind:
                text += f", no run ({self.note_kind})"
            elif self.mode == MODE_PLAN and self.ok:
                valid = sum(1 for card in self.cards if not card.error)
                text += f", slots {self.slots}, blocks {valid}/{self.blocks_max}"
        return text


@dataclass(frozen=True)
class DryRun:
    prompt: str                       # exactly what would go to stdin, email text as counts
    argv: tuple[str, ...]             # the command line ("" when Claude Code was not found)
    readiness: Readiness
    counts: Mapping[str, int]
    mail: MailReport
    research: bool = False            # a "web:" request's research run (its stdin and command line)
    note: str = ""                    # a plan dry run: why the plan would not run ("" when it would)


class AskPlanner:
    # The app's snapshot_engine.PageSnapshotter ([live] page_snapshots; None: no page pictures). It is
    # only handed the pages a research run read and the sources its checked answer shows, for the
    # LIVE view; nothing goes back to a run.
    page_snapshots: Any = None
    # The owner's notes (the productivity bundle's memory.json, newest first, at most 4 KB) as the
    # app holds them right now; called on the ask thread for every planner context (never for web
    # research). None: no <memory> block. The command line sets a read-only store's notes.
    memory: Callable[[], Sequence[str]] | None = None
    # [productivity] memory_suggestions: planner run 1 without mail may suggest one note (the app
    # sets it, as a callable asked for each run: off while memory.json can't be changed; the
    # command line never does).
    memory_suggestions: bool | Callable[[], bool] = False
    # The token hashes of the suggestions the owner dismissed (memory.MemoryStore.dismissed): a
    # suggestion that matches one is dropped.
    memory_dismissed: Callable[[], Sequence[frozenset[str]]] | None = None

    def __init__(self, config: Config, engine: ClaudeEngine, sources: Sources, *, usage: UsageLog | None = None,
                 clock: Callable[[], datetime] | None = None, monotonic: Callable[[], float] = time.monotonic,
                 research: ResearchEngine | None = None) -> None:
        from ..config import ResearchConfig

        self.config = config
        self.engine = engine
        self.sources = sources
        self.clock = clock or (lambda: datetime.now().astimezone())
        research_config = getattr(config, "research", None) or ResearchConfig()
        self.usage = usage or UsageLog(config.data_dir / USAGE_FILE, max_per_hour=config.ask.max_per_hour,
                                       max_per_day=config.ask.max_per_day, clock=self.clock,
                                       max_research_per_hour=research_config.max_per_hour,
                                       max_research_per_day=research_config.max_per_day,
                                       max_bundle_per_day=self._productivity().max_runs_per_day)
        self.research = research or ResearchEngine(research_config, engine)
        self._monotonic = monotonic
        self._busy = threading.Lock()
        self._reads = threading.local()   # .calendar: this thread's last calendar read (plan() hands it on)

    # ---- context ------------------------------------------------------------------------------

    def _context(self, command: str, briefing: BriefingContext | None, now: datetime,
                 on_stage: Callable[[str], None],
                 cancel: threading.Event | None = None, live: LiveTask = NO_TASK,
                 threads_shown: dict[tuple[str, str], LiveStep] | None = None) -> tuple[AskContext, list[str]]:
        """The context (Google reads only). Cancel is checked between reads: the rest is skipped and
        the caller sees the event set. ``live``: the LIVE view's calendar.read and mail.thread steps
        (``threads_shown`` collects the thread steps by (account, thread id))."""
        on_stage(STAGE_CONTEXT)
        ask = self.config.ask
        accounts = self.sources.accounts()
        start, end = day_window(now, ask.days_back, ask.days_ahead)
        calendar = _calendar_step(live, start, end, ask.calendars)
        events: dict[str, tuple[EventBrief, ...]] = {}
        updated = []
        for account in accounts:
            if account.calendar != "yes" or (cancel is not None and cancel.is_set()):
                _calendar_account(calendar, account.alias, account.calendar, None, cancelled=account.calendar == "yes")
                updated.append(account)
                continue
            briefs, status = self.sources.event_briefs(account.alias, start, end, ask.calendars)
            if status == "yes":
                events[account.alias] = tuple(briefs)
            _calendar_account(calendar, account.alias, status, briefs if status == "yes" else None)
            updated.append(replace(account, calendar=status))
        _calendar_done(calendar, events, updated, self._hour24(), cancelled=cancel is not None and cancel.is_set(),
                       start=start, end=end, now=now)
        self._reads.calendar = CalendarRead(start, end, tuple(events.items()), now)
        notes: list[str] = []
        threads: list[MailThread] = []
        readable = {account.alias for account in updated if account.read_mail == "yes"}
        if briefing is not None and readable:
            for ref in briefing_threads(command, briefing.pending, readable):
                if cancel is not None and cancel.is_set():
                    break
                thread = self._read_thread(ref.account, ref.thread_id, notes, live, WHY_BRIEFING, threads_shown)
                if thread is not None:
                    threads.append(thread)
        ctx = AskContext(now=now, time_zone=self.sources.time_zone(), command=command, accounts=tuple(updated),
                         events=events, briefing=briefing, mail=tuple(trim_threads(threads)), owner=self._owner(),
                         memory=self._memory_notes())
        return ctx, notes

    def _memory_notes(self) -> tuple[str, ...]:
        """The owner's notes for this context (() without memory, or when they can't be had)."""
        source = self.memory
        if source is None:
            return ()
        try:
            return tuple(note for note in source() if isinstance(note, str) and note)
        except Exception as exc:  # noqa: BLE001 - the Ask goes on without notes
            logger.warning("Ask: the memory notes could not be read (%s)", type(exc).__name__)
            return ()

    def _owner(self) -> str:
        """[assistant] address: the word Jarvis's answers address the owner with ("" for none)."""
        assistant = getattr(self.config, "assistant", None)
        return str(getattr(assistant, "address", "") or "")

    def _productivity(self) -> Any:
        """[productivity] (its defaults when the config has none, as in older tests)."""
        from ..config import ProductivityConfig

        return getattr(self.config, "productivity", None) or ProductivityConfig()

    def _hour24(self) -> bool:
        display = getattr(self.config, "display", None)
        return bool(getattr(display, "hour24", False))

    def _read_thread(self, alias: str, thread_id: str, notes: list[str], live: LiveTask = NO_TASK, why: str = "",
                     threads_shown: dict[tuple[str, str], LiveStep] | None = None) -> MailThread | None:
        step = _thread_step(live, alias, thread_id, why)
        reader = self.sources.reader(alias)
        if reader is None:
            step.done(STATUS_BLOCKED, summary=f"Not read: Jarvis can't read the {alias} account's mail")
            return None
        try:
            thread = thread_from_api(reader.thread(thread_id), alias)
        except GmailError as exc:
            note = f"Jarvis couldn't read a {alias} thread ({exc})"
            if note not in notes:
                notes.append(note)
            step.done(STATUS_FAILED, summary=note)
            return None
        except Exception as exc:  # noqa: BLE001 - reading is optional; the Ask goes on without it
            logger.warning("Ask: reading a %s thread failed unexpectedly (%s)", logged_alias(alias), type(exc).__name__)
            step.done(STATUS_FAILED, summary=f"unexpected error ({type(exc).__name__})")
            return None
        _thread_read(step, thread, self._hour24())
        if thread is not None and threads_shown is not None and step is not NO_STEP:
            threads_shown[(alias, thread.thread_id)] = step
        return thread

    # ---- runs ---------------------------------------------------------------------------------

    def _run(self, prompt: str, *, allow_search: bool, cancel: threading.Event | None, live: LiveTask = NO_TASK,
             number: int = 1, search_words: str = "", allow_web: bool = False, web_words: str = "",
             allow_remember: bool = False, mode: str = "") -> RunResult:
        """One planner run, counted when it starts (``mode``: a planner mode's run, ClaudeEngine.run_mode,
        counted as that mode's bundle kind)."""
        step = _run_step(live, self.config.ask.model, prompt, number, search_words, web_words, allow_remember,
                         mode=mode)
        ready, refused = self.engine.preflight()   # a run refused before it starts is not counted
        if refused is not None:
            step.rename_block(STDIN_PENDING, STDIN_NOT_SENT)
            step.done(STATUS_BLOCKED, status_text="SKIPPED", summary=f"Not started: {refused.message}")
            return RunResult(failure=refused, started=False)
        _run_ready(step, self.engine, ready, allow_search, allow_web, allow_remember, mode=mode)
        run = self.usage.start(kind=MODE_USAGE[mode]) if mode else self.usage.start()
        result: RunResult | None = None
        try:
            extra: dict[str, Any] = {}
            if step is not NO_STEP:   # only with the LIVE view: an engine without these keywords keeps working
                extra = {"progress": _run_progress(step), "capture": True}
            if allow_web:             # only when allowed: an engine without the keyword keeps working
                extra["allow_web"] = True
            if allow_remember:        # the same
                extra["allow_remember"] = True
            if mode:
                result = self.engine.run_mode(prompt, mode, cancel=cancel, **extra)
            else:
                result = self.engine.run(prompt, allow_search=allow_search, cancel=cancel, **extra)
            if result.limit is not None and result.limit.overage:
                self._pause_for_overage(result.limit.resets_at)
            _run_result(step, result, keep_text=live.keep_text)
            return result
        finally:
            stats = result.stats if result is not None else RunStats()
            self.usage.finish(run, outcome=result.outcome if result is not None else "error",
                              duration_ms=stats.duration_ms, turns=stats.turns, input_tokens=stats.input_tokens,
                              output_tokens=stats.output_tokens, cache_read_tokens=stats.cache_read_tokens,
                              cache_write_tokens=stats.cache_write_tokens)

    def _pause_for_overage(self, resets_at: datetime | None) -> None:
        """Claude Code said a run would use extra usage: no run (in any Jarvis process) until the
        plan's limit resets (OVERAGE_PAUSE when it did not say when)."""
        now = self.clock()
        until = resets_at if resets_at is not None and resets_at > now else now + OVERAGE_PAUSE
        until = self.usage.hold(until)
        logger.warning("Ask: Claude Code reported extra usage (usage credits); Ask is paused for %d minute(s)",
                       max(1, round((until - now).total_seconds() / 60)))

    # ---- plan ---------------------------------------------------------------------------------

    def plan(self, command: str, *, briefing: BriefingContext | None = None, page_ids: Sequence[str] = (),
             cancel: threading.Event | None = None,
             on_stage: Callable[[str], None] = lambda stage: None, live: LiveTask | None = None) -> AskOutcome:
        """One Ask (see the module docs). Never raises for Google or the CLI; one at a time.
        ``live``: the Ask's LIVE view task (its steps are added here; the caller finishes it)."""
        task = live or NO_TASK
        if not self._busy.acquire(blocking=False):
            task.step(ASK_CHECKS, CHECKS_TITLE, status=STATUS_BLOCKED, summary=BUSY_MESSAGE)
            return AskOutcome(False, "busy", BUSY_MESSAGE)
        started = self._monotonic()
        self._reads.calendar = None
        try:
            outcome = self._plan(" ".join((command or "").split())[:COMMAND_CAP], briefing, page_ids, cancel,
                                 on_stage, task)
        except Exception as exc:  # noqa: BLE001 - an Ask must never take the app down
            logger.warning("Ask: stopped by an unexpected error (%s)", type(exc).__name__)
            task.current().done(STATUS_FAILED, summary=f"unexpected error ({type(exc).__name__})")
            outcome = AskOutcome(False, "error", "Ask stopped because of an unexpected error; nothing was proposed")
        finally:
            self._busy.release()
        read, self._reads.calendar = getattr(self._reads, "calendar", None), None
        outcome = replace(outcome, duration_ms=int((self._monotonic() - started) * 1000), caps=self.usage.check(),
                          calendar_read=read)
        logger.info("Ask: %s", outcome.summary())
        return outcome

    def _refusal(self, live: LiveTask = NO_TASK, *, bundle: bool = False) -> AskOutcome | None:
        """Ask on, the caps, Claude Code ready (no model request); ``bundle``: a planner mode's run,
        which also needs a bundle run left today and must leave [productivity] reserve_ask_runs Ask
        runs this hour for the owner's own requests."""
        step = live.step(ASK_CHECKS, CHECKS_TITLE)
        if not self.config.ask.enabled:
            step.field("Ask", "off")
            step.done(STATUS_BLOCKED, summary=DISABLED_MESSAGE)
            return AskOutcome(False, DISABLED, DISABLED_MESSAGE)
        step.field("Ask", "on")
        reserve = self.usage.reserve_kept(int(self._productivity().reserve_ask_runs)) if bundle else 0
        caps = self.usage.check(runs=1, bundle=1, reserve=reserve) if bundle else self.usage.check()
        _checks_caps(step, caps, self.usage)
        if bundle:
            _checks_bundle(step, caps, self.usage, reserve)
        elif self._research_config().enabled:
            research_live.checks_research(step, caps, self.usage)
        if not caps.allowed:
            step.done(STATUS_BLOCKED, summary=caps.message)
            return AskOutcome(False, CAPS, caps.message, caps=caps)
        ready = self.engine.readiness()
        _checks_ready(step, ready)
        if not ready.ok:
            step.done(STATUS_BLOCKED, summary=ready.message)
            return AskOutcome(False, ready.problem or NOT_READY, ready.message)
        step.done(STATUS_OK)
        return None

    def _research_config(self) -> Any:
        return self.research.research

    def _research_zone(self) -> str:
        """The time zone the research is told: the personal calendar's setting only when Jarvis
        already has it (cached), else this PC's zone; never a Google request (a "web:" request
        reads nothing from the accounts). Sources without cached_time_zone (tests) give their
        time_zone()."""
        cached = getattr(self.sources, "cached_time_zone", None)
        return cached() if callable(cached) else self.sources.time_zone()

    def _web_allowed(self) -> tuple[bool, str]:
        """May planner run 1 hand the request to web research? (yes or no, the LIVE field's words).
        No model request; never raises."""
        research = self._research_config()
        if not getattr(research, "enabled", False):
            return False, "no - web research is off"
        if not getattr(research, "planner_may_ask", False):
            return False, "no - the planner may not ask ([research] planner_may_ask)"
        ready = self.research.readiness()
        if not ready.ok:
            return False, f"no - {ready.message}"
        caps = self.usage.check(runs=2, research=1)
        if caps.research_limit:
            return False, f"no - {RESEARCH_LIMIT_NOTE}"
        if not caps.allowed:
            return False, "no - the Ask limit leaves one run"
        return True, "yes"

    def _plan(self, command: str, briefing: BriefingContext | None, page_ids: Sequence[str],
              cancel: threading.Event | None, on_stage: Callable[[str], None],
              live: LiveTask = NO_TASK) -> AskOutcome:
        if not command:
            live.step(ASK_CHECKS, CHECKS_TITLE, status=STATUS_BLOCKED, summary=EMPTY_MESSAGE)
            return AskOutcome(False, EMPTY, EMPTY_MESSAGE)
        question = forced_question(command)
        if question is not None:   # "web:" / "research:": web research only, nothing read from the accounts
            return self._research_only(question, page_ids, cancel, on_stage, live)
        refusal = self._refusal(live)
        if refusal is not None:
            return refusal
        now = self.clock()
        shown: dict[tuple[str, str], LiveStep] = {}   # the mail.thread steps (this call's memory only)
        ctx, notes = self._context(command, briefing, now, on_stage, cancel, live, shown)
        if cancel is not None and cancel.is_set():
            return AskOutcome(False, CANCELLED, "Cancelled; nothing was proposed")
        searchable = {account.alias for account in ctx.accounts if account.read_mail == "yes"}
        allow_search = bool(searchable) and self.usage.check(runs=2).allowed
        if searchable and not allow_search:
            ctx = replace(ctx, accounts=tuple(replace(account, read_mail=READ_LIMIT)
                                              if account.alias in searchable else account for account in ctx.accounts))
        prompt, index = build_prompt(ctx)
        logger.info("Ask: context of %d characters (%s; mail %d thread(s))", len(prompt),
                    ", ".join(f"{name} {count}" for name, count in index.counts().items()), len(ctx.mail))
        _given(live, ctx, index)
        _threads_given(shown, ctx.mail, prompt)
        on_stage(STAGE_PLANNING)
        why_not = "" if allow_search else ("no - the Ask limit leaves one run" if searchable
                                           else "no - no account's mail can be read")
        allow_web, web_words = self._web_allowed()
        # Only a run whose stdin has no <mail> may suggest a note (never run 2).
        allow_remember = self._suggestions_on() and not ctx.mail
        first = self._run(prompt, allow_search=allow_search, cancel=cancel, live=live, number=1, search_words=why_not,
                          allow_web=allow_web, web_words=web_words, allow_remember=allow_remember)
        del prompt
        if first.failure is not None or first.plan is None:
            failure = first.failure
            return AskOutcome(False, failure.kind if failure else "error", failure.message if failure else "",
                              runs=1 if first.started else 0, init=first.init,
                              stats=(first.stats,) if first.started else (), mail=report(ctx.mail))
        plan, final_index, runs, stats = first.plan, index, 1, [first.stats]
        suggested = self._suggestion(first.plan.remember, index) if allow_remember else ("", "")
        mail_threads = list(ctx.mail)
        search_text = search_account = mail_needed = ""
        if plan.web is not None and allow_web:
            if plan.search is not None and allow_search:   # mail first, as asked; the web another time
                live.step(ASK_CHECKS, "Web research", status=STATUS_WARN, status_text="SKIPPED", summary=BOTH_MESSAGE)
            else:
                if suggested[0]:   # the answer will be the research's: a note run 1 suggested is not offered
                    logger.info("Memory: a suggestion was dropped (research)")
                if suggested[0] or suggested[1]:
                    _suggestion_item(live.find(PLANNER_RUN), ("", suggested[1] or SUGGESTION_HANDED_OVER),
                                     live.keep_text)
                return self._research_routed(command, plan, first, ctx, now, notes, page_ids, cancel, on_stage, live)
        if plan.search is not None and allow_search:
            search = plan.search
            mail_note, found, ran = self._search(search.account, search.query, ctx, searchable, on_stage, cancel,
                                                 index.search_grounding(), live, search.why, shown)
            if cancel is not None and cancel.is_set():
                return AskOutcome(False, CANCELLED, "Cancelled; nothing was proposed", runs=1, init=first.init,
                                  stats=tuple(stats), mail=report(ctx.mail))
            if ran:
                search_text, search_account = ran, search.account
            if mail_note:
                notes.append(mail_note)
                mail_needed = needs_mail_sign_in(search.account, ctx)
            elif found:
                caps = self.usage.check()
                if not caps.allowed:
                    notes.append(caps.message)
                    live.step(ASK_CHECKS, "Second planner run", status=STATUS_WARN, status_text="SKIPPED",
                              summary=caps.message)
                else:
                    mail_threads = trim_threads(list(ctx.mail) + found)
                    second_ctx = replace(ctx, mail=tuple(mail_threads), accounts=tuple(
                        replace(account, read_mail="no (mail already read for this request)")
                        if account.read_mail == "yes" else account for account in ctx.accounts))
                    second_prompt, final_index = build_prompt(second_ctx)
                    _threads_given(shown, second_ctx.mail, second_prompt)
                    on_stage(STAGE_PLANNING_AGAIN)
                    second = self._run(second_prompt, allow_search=False, cancel=cancel, live=live, number=2,
                                       search_words="no - this is the second run",
                                       web_words="no - this is the second run")
                    del second_prompt
                    if second.started:
                        runs, stats = 2, stats + [second.stats]
                    if second.failure is not None or second.plan is None:
                        failure = second.failure
                        return AskOutcome(False, failure.kind if failure else "error",
                                          failure.message if failure else "", runs=runs,
                                          init=second.init or first.init,
                                          stats=tuple(stats), mail=report(mail_threads), search=search_text,
                                          search_account=search_account)
                    plan = second.plan
        outcome = self._finish(plan, final_index, now=now, page_ids=page_ids, notes=notes, runs=runs,
                               stats=tuple(stats), init=first.init, mail=report(mail_threads), search=search_text,
                               search_account=search_account, live=live, suggested=suggested)
        return replace(outcome, mail_needed=mail_needed)

    def _suggestions_on(self) -> bool:
        """The app keeps notes and lets the planner suggest one ([productivity] memory_suggestions;
        a callable: asked now, e.g. never while memory.json can't be changed)."""
        if self.memory is None:
            return False
        on = self.memory_suggestions
        if callable(on):
            try:
                return bool(on())
            except Exception:  # noqa: BLE001 - unknown: no suggestion
                return False
        return bool(on)

    def _suggestion(self, remember: Sequence[str], index: ContextIndex) -> tuple[str, str]:
        """(the planner's suggested note when memory.check_suggestion keeps it, else ""; the reason
        word it was dropped, else ""): grounded in run 1's own context, never dismissed before."""
        from ..memory import check_suggestion, clean_note

        if not remember:
            return "", ""
        text = clean_note(remember[0])
        dismissed: Sequence[frozenset[str]] = ()
        source = getattr(self, "memory_dismissed", None)
        if callable(source):
            try:
                dismissed = tuple(source())
            except Exception:  # noqa: BLE001 - checked without them
                dismissed = ()
        titles = [facts.title for facts in index.events.values()]
        reason = check_suggestion(text, index=index, command=index.command, titles=titles, dismissed=dismissed)
        if reason:
            logger.info("Memory: a suggestion was dropped (%s)", reason)
            return "", reason
        return text, ""

    # ---- planner modes (the productivity bundle) ---------------------------------------------

    def plan_mode(self, request: ModeRequest, *, inputs: ModeInputs | None = None,
                  briefing: BriefingContext | None = None, page_ids: Sequence[str] = (),
                  cancel: threading.Event | None = None, on_stage: Callable[[str], None] = lambda stage: None,
                  live: LiveTask | None = None) -> AskOutcome:
        """One planner mode run (the day plan, the meeting brief, a follow-up draft, a meeting's summary, a desktop
        request) on the Ask thread: the same one-at-a-time lock as plan(); never raises; ``live`` gets its steps (the
        caller finishes the task)."""
        task = live or NO_TASK
        if not self._busy.acquire(blocking=False):
            task.step(ASK_CHECKS, CHECKS_TITLE, status=STATUS_BLOCKED, summary=BUSY_MESSAGE)
            return AskOutcome(False, "busy", BUSY_MESSAGE, mode=request.mode, target=_mode_target(request))
        started = self._monotonic()
        self._reads.calendar = None
        try:
            if request.mode == MODE_PLAN:
                outcome = self._plan_day(request, inputs or ModeInputs(), briefing, page_ids, cancel, on_stage, task)
            elif request.mode == MODE_PREP:
                outcome = self._prep(request, inputs or ModeInputs(), briefing, page_ids, cancel, on_stage, task)
            elif request.mode == MODE_NUDGE:
                outcome = self._nudge(request, inputs or ModeInputs(), page_ids, cancel, on_stage, task)
            elif request.mode == MODE_MEETING:
                outcome = self._meeting(request, inputs or ModeInputs(), page_ids, cancel, on_stage, task)
            elif request.mode == MODE_DESKTOP:
                outcome = self._desktop(request, cancel, on_stage, task)
            else:
                outcome = AskOutcome(False, "error", "Jarvis can't do that kind of request yet; nothing was proposed")
        except Exception as exc:  # noqa: BLE001 - a mode run must never take the app down
            logger.warning("Ask: a %s run stopped by an unexpected error (%s)", request.mode, type(exc).__name__)
            task.current().done(STATUS_FAILED, summary=f"unexpected error ({type(exc).__name__})")
            outcome = AskOutcome(False, "error", "Jarvis stopped because of an unexpected error; nothing was proposed")
        finally:
            self._busy.release()
        read, self._reads.calendar = getattr(self._reads, "calendar", None), None
        outcome = replace(outcome, mode=request.mode, duration_ms=int((self._monotonic() - started) * 1000),
                          caps=self.usage.check(), calendar_read=read, target=outcome.target or _mode_target(request))
        logger.info("Ask: %s", outcome.summary())
        return outcome

    def _plan_no_run(self, kind: str, live: LiveTask, *, window: Any = None, part: str = "",
                     step: LiveStep = NO_STEP) -> AskOutcome:
        """A day plan that does not run: Jarvis's own words, no planner run, nothing counted."""
        from ..persona import plan_no_run_text

        label = window.label if window is not None else dayplan.LABELS.get(part, "today")
        hint = window is not None and not window.tomorrow and kind == "day_full"
        text = plan_no_run_text(kind, self._owner(), label=label, part=part or "morning",
                                late=bool(window is not None and window.switched), hint_tomorrow=hint)
        logger.info("Plan: no run (%s)", kind)
        if step is NO_STEP:
            step = live.step(PLAN_FREE, "Your free time")
        _plan_free_no_run(step, kind, text)
        return AskOutcome(True, "ok", text, mode=MODE_PLAN, note_kind=kind,
                          plan_for=window.day if window is not None else None, window=window)

    def _plan_day(self, request: ModeRequest, inputs: ModeInputs, briefing: BriefingContext | None,
                  page_ids: Sequence[str], cancel: threading.Event | None, on_stage: Callable[[str], None],
                  live: LiveTask = NO_TASK) -> AskOutcome:
        """The day plan (spec 3.2): checks, the day read fresh, free time and what needs time (no
        run when the day is full or nothing needs time), one run, the blocks checked."""
        settings = self._productivity()
        refusal = self._refusal(live, bundle=True)
        if refusal is not None:
            if refusal.kind != DISABLED:   # Ask off: the bundle's shortcut never routes here
                logger.info("Plan: no run (%s)", CAPS if refusal.kind == CAPS else NOT_READY)
            return refusal
        accounts = self.sources.accounts()
        personal = next((account for account in accounts if account.alias == DEFAULT_ACCOUNT), None)
        if personal is None or personal.calendar != "yes":
            return self._plan_no_run("no_calendar", live, part=request.part)
        on_stage(STAGE_DAY)
        zone = self.sources.time_zone()
        now = self.clock()
        window = dayplan.plan_window(now, request.part or dayplan.PART_DAY, settings, zone,
                                     evening_from_hour=inputs.evening_from_hour)
        if isinstance(window, str):
            return self._plan_no_run(window, live, part=request.part)
        events, read_accounts = self._read_day(window, accounts, cancel, live)
        if cancel is not None and cancel.is_set():
            return AskOutcome(False, CANCELLED, "Cancelled; nothing was proposed")
        inputs = replace(inputs, todos=dayplan.plan_todos(inputs, window.day))
        own = frozenset(inputs.own) | frozenset(account.address.casefold() for account in accounts if account.address)
        slots = dayplan.free_slots(events, window, own=own)
        notes = self._memory_notes()
        _mode_given(live, window, events, inputs, notes, briefing, self._hour24())
        step = _plan_free_step(live, window, slots, events, inputs, request.typed, own, self._hour24())
        if not slots:
            return self._plan_no_run("day_full", live, window=window, part=request.part, step=step)
        wall_now = dayplan.to_wall(now, zone)
        if not dayplan.needs_time(inputs, wall_now, request.typed):
            return self._plan_no_run("nothing", live, window=window, part=request.part, step=step)
        step.done(STATUS_OK, summary=f"{_count_words(len(slots), 'free stretch', 'free stretches')}, "
                                     f"{dayplan.free_minutes(slots)} min")
        prompt, index, shown_rows = modes.build_plan_prompt(now, zone, self._owner(), window, events, slots, inputs,
                                                            briefing, notes, request,
                                                            max_blocks=settings.plan_max_blocks,
                                                            accounts=read_accounts)
        logger.info("Ask: context of %d characters (%s; mode plan)", len(prompt),
                    ", ".join(f"{name} {count}" for name, count in index.counts().items()))
        on_stage(STAGE_PLANNING)
        allow_remember = self._suggestions_on()
        result = self._run(prompt, allow_search=False, cancel=cancel, live=live, number=1,
                           search_words=NO_MAIL_WORDS, web_words=NO_WEB_WORDS, allow_remember=allow_remember,
                           mode=MODE_PLAN)
        del prompt
        if result.failure is not None or result.plan is None:
            failure = result.failure
            return AskOutcome(False, failure.kind if failure else "error", failure.message if failure else "",
                              runs=1 if result.started else 0, init=result.init,
                              stats=(result.stats,) if result.started else (), plan_for=window.day, window=window)
        plan = result.plan
        suggested = self._suggestion(plan.remember, index) if allow_remember else ("", "")
        # Only the rows the planner was shown may be served (a row cut to fit the stdin is no target).
        targets = {row: target for row, target in dayplan.row_targets(inputs).items() if row in shown_rows}
        shown = [(alias, brief) for alias, briefs in events.items() for brief in briefs]
        check = dayplan.DayCheck(window, slots, shown, wall_now, targets, owner_words=request.typed,
                                 briefing=bool(briefing is not None and briefing.sections), notes=notes, own=own,
                                 hour24=self._hour24())
        cards = to_cards(plan.lines, index, now=now, link_hosts=self.config.actions.link_hosts, page_ids=page_ids,
                         max_cards=MODE_MAX_LINES[MODE_PLAN], kinds=MODE_KINDS[MODE_PLAN], mode=MODE_PLAN, extra=check)
        cards = keep_valid(cards, settings.plan_max_blocks)
        cards = _served_cards(cards, check.served, targets)
        _plan_validated(live, cards, check.served, targets, events, window, now, self._hour24(), suggested)
        refused = sum(1 for card in cards.cards if card.error)
        message_parts = []
        if cards.dropped:
            message_parts.append(f"{cards.dropped} more block(s) were left out")
        if plan.via_fallback:
            message_parts.append("(read from the planner's plain text)")
        logger.info("Plan: %d block(s) proposed (%d refused)", len(cards.cards) - refused, refused)
        return AskOutcome(True, "ok", "; ".join(message_parts), say=plan.say, question=plan.question,
                          cards=cards.cards, dropped=cards.dropped, refused=refused, runs=1, init=result.init,
                          stats=(result.stats,), suggestion=suggested[0], mode=MODE_PLAN, plan_for=window.day,
                          window=window, slots=len(slots), blocks_max=settings.plan_max_blocks)

    def _read_day(self, window: Any, accounts: Sequence[AccountInfo], cancel: threading.Event | None,
                  live: LiveTask) -> tuple[dict[str, tuple[EventBrief, ...]], list[AccountInfo]]:
        """The day being planned, fresh, on every account whose calendar is signed in (its midnight to
        the next, in the personal calendar's zone), as CalendarRead for the LIVE day pictures."""
        start, end = _day_bounds(window.day, window.zone)
        ask = self.config.ask
        step = _calendar_step(live, start, end, ask.calendars)
        events: dict[str, tuple[EventBrief, ...]] = {}
        updated: list[AccountInfo] = []
        for account in accounts:
            if account.calendar != "yes" or (cancel is not None and cancel.is_set()):
                _calendar_account(step, account.alias, account.calendar, None,
                                  cancelled=account.calendar == "yes")
                updated.append(account)
                continue
            briefs, status = self.sources.event_briefs(account.alias, start, end, ask.calendars)
            if status == "yes":
                events[account.alias] = tuple(briefs)
            _calendar_account(step, account.alias, status, briefs if status == "yes" else None)
            updated.append(replace(account, calendar=status))
        _day_read_done(step, events, updated, window, self.clock(), self._hour24(),
                       cancelled=cancel is not None and cancel.is_set())
        self._reads.calendar = CalendarRead(start, end, tuple(events.items()), self.clock())
        return events, updated

    def plan_dry_run(self, request: ModeRequest, *, inputs: ModeInputs | None = None,
                     briefing: BriefingContext | None = None) -> DryRun:
        """What a day plan would send (titles, briefing lines and notes as their sizes) and its command
        line, without any run and without counting: the day is read fresh (read only). ``note``: why
        it would not run ("" when it would)."""
        from ..persona import plan_no_run_text

        settings = self._productivity()
        inputs = inputs or ModeInputs()
        ready = self.engine.readiness()
        accounts = self.sources.accounts()
        zone = self.sources.time_zone()
        now = self.clock()
        window = dayplan.plan_window(now, request.part or dayplan.PART_DAY, settings, zone,
                                     evening_from_hour=inputs.evening_from_hour)
        argv: tuple[str, ...] = ()
        if ready.exe is not None:
            argv = tuple(self.engine.mode_argv(ready, MODE_PLAN))
        if isinstance(window, str):
            note = plan_no_run_text(window, self._owner(), part=request.part or "morning")
            return DryRun("", argv, ready, {}, MailReport(), note=note)
        events, read_accounts = self._read_day(window, accounts, None, NO_TASK)
        self._reads.calendar = None
        inputs = replace(inputs, todos=dayplan.plan_todos(inputs, window.day))
        own = frozenset(inputs.own) | frozenset(account.address.casefold() for account in accounts if account.address)
        slots = dayplan.free_slots(events, window, own=own)
        note = ""
        if not slots:
            note = plan_no_run_text("day_full", self._owner(), label=window.label, late=window.switched,
                                    hint_tomorrow=not window.tomorrow)
        elif not dayplan.needs_time(inputs, dayplan.to_wall(now, zone), request.typed):
            note = plan_no_run_text("nothing", self._owner(), label=window.label, late=window.switched)
        prompt, index, _rows = modes.build_plan_prompt(now, zone, self._owner(), window, events, slots, inputs,
                                                       briefing, self._memory_notes(), request, redact=True,
                                                       max_blocks=settings.plan_max_blocks, accounts=read_accounts)
        counts = {"events": sum(len(briefs) for briefs in events.values()), "free stretches": len(slots),
                  "free minutes": dayplan.free_minutes(slots), "to-dos": len(inputs.todos),
                  "deadlines": len(inputs.deadlines), "memory": index.memory_notes}
        return DryRun(prompt, argv, ready, counts, MailReport(), note=note)

    # ---- a meeting's summary (meetings part 2) -------------------------------------------------

    def _meeting(self, request: ModeRequest, inputs: ModeInputs, page_ids: Sequence[str],
                 cancel: threading.Event | None, on_stage: Callable[[str], None],
                 live: LiveTask = NO_TASK) -> AskOutcome:
        """A recorded meeting's summary (see the module docs): the checks, the transcript read here, one run,
        its answer checked strictly, then Jarvis's own checks. Nothing is sent, added or kept here: the
        follow-ups are cards, the notes suggestions."""
        job = request.payload
        if not isinstance(job, meeting_summary.MeetingJob):
            return AskOutcome(False, "error", "Jarvis can't do that kind of request yet; nothing was proposed")
        refusal = self._refusal(live)
        if refusal is not None:
            logger.info("Meeting %s: no summary run (%s)", job.meeting_id, refusal.kind)
            return replace(refusal, target=job.meeting_id)
        on_stage(STAGE_CONTEXT)
        step = live.step(MEETING_INPUT, "The meeting's transcript")
        turns = meeting_summary.load_turns(job.transcript)
        if not turns:
            step.done(STATUS_BLOCKED, summary="No transcript to summarise")
            logger.info("Meeting %s: no summary run (%s)", job.meeting_id, NO_TRANSCRIPT)
            return AskOutcome(False, NO_TRANSCRIPT, NO_TRANSCRIPT_MESSAGE, target=job.meeting_id)
        accounts = self.sources.accounts()
        own = frozenset(inputs.own) | frozenset(account.address.casefold() for account in accounts if account.address)
        labels = meeting_summary.guest_labels(job.guests, own)
        zone = self.sources.time_zone()
        now = self.clock()
        wall = dayplan.to_wall(now, zone) if zone else now
        prompt = meeting_summary.build_prompt(wall, self._owner(), job, turns, labels, zone=zone)
        _meeting_input(step, turns, labels, prompt, job)
        logger.info("Meeting %s: summary context of %d characters (%s, linked: %s, %d turn(s), %d guest(s))",
                    job.meeting_id, len(prompt), job.kind, job.linked, len(turns), len(labels))
        on_stage(STAGE_PLANNING)
        result = self._run(prompt, allow_search=False, cancel=cancel, live=live, number=1, mode=MODE_MEETING)
        del prompt
        answer = result.answer
        if result.failure is not None or not isinstance(answer, meeting_summary.SummaryAnswer):
            failed = result.failure
            kind = failed.kind if failed else "error"
            logger.info("Meeting %s: summary run %s", job.meeting_id, kind)
            return AskOutcome(False, kind, failed.message if failed else "", runs=1 if result.started else 0,
                              init=result.init, stats=(result.stats,) if result.started else (),
                              target=job.meeting_id)
        summary = meeting_summary.check_summary(answer, job, labels, own=own, now=now)
        alias = job.alias or DEFAULT_ACCOUNT
        account = next((item for item in accounts if item.alias == alias), None)
        aliases = [item.alias for item in accounts]
        index = meeting_summary.followup_index(labels, aliases, own)
        cards = meeting_summary.followup_cards(summary, labels, alias=alias, aliases=aliases, own=own,
                                               can_send=bool(account is not None and account.send_mail), now=now,
                                               link_hosts=self.config.actions.link_hosts, page_ids=page_ids,
                                               meeting_id=job.meeting_id)
        notes, notes_dropped = self._meeting_notes(summary, index, job)
        summary = replace(summary, memory=notes)
        _meeting_validated(live, answer, summary, cards, notes_dropped, self._hour24(), job)
        logger.info("Meeting %s: summary checked (%s; %d follow-up card(s), %d refused; %d note(s) left out)",
                    job.meeting_id, ", ".join(f"{name} {count}" for name, count in summary.counts().items()),
                    len(cards.cards) - cards.refused, cards.refused, notes_dropped)
        message = "(read from the planner's plain text)" if answer.via_fallback else ""
        return AskOutcome(True, "ok", message, say=summary.title, cards=cards.cards, dropped=cards.dropped,
                          refused=cards.refused, runs=1, init=result.init, stats=(result.stats,), brief=summary,
                          mode=MODE_MEETING, target=job.meeting_id)

    def _meeting_notes(self, summary: Any, index: ContextIndex, job: Any) -> tuple[tuple[str, ...], int]:
        """The summary's suggested notes the owner may keep (memory.check_suggestion: grounded in the invite's
        names and the titles, no address, link, number or secret, not dismissed before); none while Memory
        can't keep a note. (kept, how many were left out)."""
        from ..memory import check_suggestion, clean_note

        if not summary.memory:
            return (), 0
        if not self._suggestions_on():
            return (), len(summary.memory)
        dismissed: Sequence[frozenset[str]] = ()
        source = getattr(self, "memory_dismissed", None)
        if callable(source):
            try:
                dismissed = tuple(source())
            except Exception:  # noqa: BLE001 - checked without them
                dismissed = ()
        kept: list[str] = []
        for text in summary.memory:
            note = clean_note(text)
            reason = check_suggestion(note, index=index, command="", titles=[job.title, summary.title],
                                      dismissed=dismissed)
            if reason:
                logger.info("Memory: a meeting suggestion was dropped (%s)", reason)
                continue
            kept.append(note)
        return tuple(kept), len(summary.memory) - len(kept)

    # ---- a desktop request (Jarvis on your desktop, P1.7) ----------------------------------------

    def _desktop(self, request: ModeRequest, cancel: threading.Event | None, on_stage: Callable[[str], None],
                 live: LiveTask = NO_TASK) -> AskOutcome:
        """A desktop request the owner typed (ModeRequest.payload: a desktop_mode.DesktopJob): the Ask's checks (Ask
        on, the caps, Claude Code ready - no bundle cap: it counts as an Ask run), the stdin from the job's context
        (names only; this run's refs), ONE planner run with its own prompt and schema, the answer read strictly and
        every step checked by desktop.validate against this run's refs and the owner's typed words only. Nothing is
        opened or changed here: the outcome carries the checked plan (``brief``: desktop.validate.DesktopPlan) and
        Jarvis's own words for it (``message``)."""
        job = request.payload
        if not isinstance(job, desktop_mode.DesktopJob):
            return AskOutcome(False, "error", "Jarvis can't do that kind of request yet; nothing was proposed")
        refusal = self._refusal(live)
        if refusal is not None:
            logger.info("Desktop %s: no planner run (%s)", job.run_id, refusal.kind)
            return replace(refusal, target=job.run_id)
        zone = self.sources.time_zone()
        now = self.clock()
        wall = dayplan.to_wall(now, zone) if zone else now
        prompt = desktop_mode.build_prompt(wall, request, job.context, zone=zone, run_id=job.run_id)
        _desktop_given(live, job, prompt)
        logger.info("Desktop %s: context of %d characters (%s)", job.run_id, len(prompt.text),
                    ", ".join(f"{name} {count}" for name, count in prompt.refs.counts().items()) or "nothing")
        on_stage(STAGE_PLANNING)
        result = self._run(prompt.text, allow_search=False, cancel=cancel, live=live, number=1, mode=MODE_DESKTOP)
        answer = result.answer
        if result.failure is not None or not isinstance(answer, desktop_validate.DesktopAnswer):
            failed = result.failure
            kind = failed.kind if failed else "error"
            logger.info("Desktop %s: planner run %s", job.run_id, kind)
            return AskOutcome(False, kind, failed.message if failed else "", runs=1 if result.started else 0,
                              init=result.init, stats=(result.stats,) if result.started else (), target=job.run_id)
        plan = desktop_validate.check_plan(answer, prompt.refs, request.typed, run_id=job.run_id)
        words = desktop_mode.plan_words(plan, self._owner())
        _desktop_validated(live, plan, words)
        logger.info("Desktop %s: plan checked, steps=%d (%s), refused=%d (%s)%s", job.run_id, len(plan.steps),
                    plan.skills_text() or "none", len(plan.refused()), plan.codes_text() or "none",
                    " (read from the planner's plain text)" if answer.via_fallback else "")
        return AskOutcome(True, "ok", words, say=answer.say, question=answer.question,
                          refused=len(plan.refused()), runs=1, init=result.init, stats=(result.stats,), brief=plan,
                          mode=MODE_DESKTOP, target=job.run_id)

    # ---- web research -------------------------------------------------------------------------

    # ---- meeting prep (spec 3.3) ---------------------------------------------------------------

    def _prep(self, request: ModeRequest, inputs: ModeInputs, briefing: BriefingContext | None,
              page_ids: Sequence[str], cancel: threading.Event | None, on_stage: Callable[[str], None],
              live: LiveTask = NO_TASK) -> AskOutcome:
        """A meeting brief: Ask on (readiness and the caps wait for the first mail read, R12), the
        meeting as read now, its guests looked up and their threads read, one run; or a brief of
        Jarvis's own with no run (mail can't be read, the checks refuse, no thread found)."""
        settings = self._productivity()
        days = prep.lookback(settings.prep_lookback_days)
        step = live.step(ASK_CHECKS, CHECKS_TITLE)
        if not self.config.ask.enabled:
            step.field("Ask", "off")
            step.done(STATUS_BLOCKED, summary=DISABLED_MESSAGE)
            return AskOutcome(False, DISABLED, DISABLED_MESSAGE)
        step.field("Ask", "on")
        step.field("Claude Code and the limits", PREP_CHECKS_LATER)
        step.done(STATUS_OK)
        on_stage(STAGE_CONTEXT)
        zone = self.sources.time_zone()
        now = self.clock()
        accounts = self.sources.accounts()
        own = frozenset(inputs.own) | frozenset(account.address.casefold() for account in accounts if account.address)
        found = self._prep_meeting(request, accounts, own, now, zone, cancel, live)
        if isinstance(found, AskOutcome):
            return found
        meeting = found
        alias = meeting.alias
        lookups = prep.prep_guests(meeting.brief, own, settings.prep_max_guests)
        known = next((account for account in accounts if account.alias == alias), None)
        people = _prep_people_step(live, days)
        notes = self._memory_notes()
        pending = tuple(briefing.pending) if briefing is not None else ()
        reader = self.sources.reader(alias)
        if not self.config.ask.read_mail or known is None or known.read_mail != "yes" or reader is None:
            reason = "mail_off" if not self.config.ask.read_mail else "unreadable"
            said = replace(known, read_mail=READ_OFF) if known is not None and reason == "mail_off" else known
            return self._prep_without_run(reason, meeting, lookups, pending, notes, unreadable_note(alias, said),
                                          days, live, people)
        reserve = self.usage.reserve_kept(int(settings.reserve_ask_runs))
        caps = self.usage.check(runs=1, bundle=1, reserve=reserve)
        ready = self.engine.readiness()
        _prep_checks(people, caps, self.usage, reserve, ready)
        if not caps.allowed:
            return self._prep_without_run("caps", meeting, lookups, pending, notes, f"{MAIL_NOT_READ}{caps.message}",
                                          days, live, people)
        if not ready.ok:
            return self._prep_without_run("not_ready", meeting, lookups, pending, notes,
                                          f"{MAIL_NOT_READ}{ready.message}", days, live, people)
        on_stage(STAGE_PREP_MAIL)
        lookups, searches, refused = self._prep_lookups(meeting, lookups, accounts, days,
                                                        bool(settings.prep_other_accounts), cancel, live, people,
                                                        participants=inputs.participants)
        shown: dict[tuple[str, str], LiveStep] = {}   # the mail.thread steps (this call's memory only)
        mail_notes: list[str] = []
        threads, lookups = self._prep_reads(lookups, mail_notes, cancel, live, shown)
        unsearched = sum(1 for lookup in lookups if lookup.status not in (prep.FOUND, prep.NONE, prep.SAME, prep.OWN))
        hits = sum(1 for lookup in lookups if lookup.via == prep.VIA_INDEX)
        logger.info("Prep: %d search(es) (%d refused), %d index hit(s), %d thread(s) read, %d guest(s) not searched",
                    searches, refused, hits, len(threads), unsearched)
        if cancel is not None and cancel.is_set():
            _prep_people_done(people, lookups, days, alias)
            return AskOutcome(False, CANCELLED, "Cancelled; nothing was proposed", mail=report(threads))
        if not threads:
            return self._prep_without_run("no_thread", meeting, lookups, pending, notes, "", days, live, people)
        _prep_people_done(people, lookups, days, alias)
        people_text = prep.people_block(lookups, days, alias)
        prompt, index = modes.build_prep_prompt(now, self._owner(), known, alias, meeting.brief, people_text, threads,
                                                notes, request, accounts=accounts, zone=zone)
        logger.info("Ask: context of %d characters (%s; mode prep)", len(prompt),
                    ", ".join(f"{name} {count}" for name, count in index.counts().items()))
        _threads_given(shown, trim_threads(threads, max_threads=prep.MAX_THREADS), prompt)
        on_stage(STAGE_PLANNING)
        result = self._run(prompt, allow_search=False, cancel=cancel, live=live, number=1,
                           search_words=PREP_NO_SEARCH_WORDS, web_words=PREP_NO_WEB_WORDS, mode=MODE_PREP)
        del prompt
        mail = report(threads)
        answer = result.answer
        if result.failure is not None or not isinstance(answer, prep.PrepAnswer):
            failure = result.failure
            return AskOutcome(False, failure.kind if failure else "error", failure.message if failure else "",
                              runs=1 if result.started else 0, init=result.init,
                              stats=(result.stats,) if result.started else (), mail=mail)
        brief = prep.check_prep(answer, meeting, lookups, threads, notes, days=days, zone=zone)
        brief = replace(brief, nudges=prep.brief_nudges(answer, threads, alias, own | _reader_own(reader), zone=zone))
        cards = to_cards(answer.lines, index, now=now, link_hosts=self.config.actions.link_hosts, page_ids=page_ids,
                         max_cards=MODE_MAX_LINES[MODE_PREP], kinds=MODE_KINDS[MODE_PREP], mode=MODE_PREP,
                         extra=prep.PrepCheck(alias, threads, notes, own=own))
        _prep_validated(live, answer, brief, cards, self._hour24())
        held = {card.error for card in cards.cards}
        if prep.NOTE_LEAK in held:
            logger.info("Prep: a draft held back (note)")
        if prep.MAIL_LEAK in held:
            logger.info("Prep: a draft held back (mail)")
        message_parts = list(mail_notes)
        if cards.dropped:
            message_parts.append(f"{cards.dropped} more reply draft(s) were left out")
        if answer.via_fallback:
            message_parts.append("(read from the planner's plain text)")
        return AskOutcome(True, "ok", "; ".join(message_parts), say=answer.say, cards=cards.cards,
                          dropped=cards.dropped, refused=cards.refused, runs=1, init=result.init,
                          stats=(result.stats,), mail=mail, brief=brief, mode=MODE_PREP)

    def _prep_meeting(self, request: ModeRequest, accounts: Sequence[AccountInfo], own: frozenset[str],
                      now: datetime, zone: str, cancel: threading.Event | None,
                      live: LiveTask) -> prep.PrepMeeting | AskOutcome:
        """The meeting as read now (spec 3.3 step 2), or why there is no brief (no reads of mail):
        typed words resolved against a fresh read of today and tomorrow on every signed-in calendar
        (none, several or one found by only some title words: no brief yet, R5); an offered or chosen
        meeting read again on its own calendar over the same two days and found by its calendar and
        id, wherever it moved (R20). Gone or ended stops."""
        from ..persona import prep_no_run_text

        ask = self.config.ask
        wall_now = dayplan.to_wall(now, zone)
        first = _day_bounds(wall_now.date(), zone)[0]
        last = _day_bounds(wall_now.date() + timedelta(days=1), zone)[1]
        step = _prep_meeting_step(live, request)
        owner = self._owner()
        events: dict[str, tuple[EventBrief, ...]] = {}
        failed = False
        moved = ""
        if request.event is None:
            for account in accounts:
                if account.calendar != "yes" or (cancel is not None and cancel.is_set()):
                    continue
                briefs, status = self.sources.event_briefs(account.alias, first, last, ask.calendars)
                _prep_read_field(step, account.alias, status, briefs)
                if status == "yes":
                    events[account.alias] = tuple(briefs)
                else:
                    failed = True
            self._reads.calendar = CalendarRead(first, last, tuple(events.items()), now)
            if cancel is not None and cancel.is_set():
                _prep_meeting_stop(step, STATUS_CANCELLED, "Cancel clicked")
                return AskOutcome(False, CANCELLED, "Cancelled; nothing was proposed")
            if not events:
                connect = not any(account.calendar == "yes" for account in accounts)
                return self._prep_no_run("no_calendar", prep_no_run_text("no_calendar", owner, connect=connect), step)
            match = prep.resolve_meeting(request.target, events, now, own, zone=zone)
            if not match.refs:
                kind = "no_calendar" if failed else "no_match"
                return self._prep_no_run(kind, prep_no_run_text(kind, owner), step)
            if len(match.refs) > 1 or match.how == prep.MATCH_TITLE_SOME:
                choices = tuple(prep.choice_of(alias, brief, now, zone) for alias, brief in match.found)
                return self._prep_choices(choices, step)
            alias, brief = match.found[0]
        else:
            ref = request.event
            alias = ref.alias
            briefs, status = self.sources.event_briefs(alias, first, last, ask.calendars)
            _prep_read_field(step, alias, status, briefs)
            if status == "yes":
                events[alias] = tuple(briefs)
            self._reads.calendar = CalendarRead(first, last, tuple(events.items()), now)
            if status != "yes":
                text = prep_no_run_text("no_calendar", owner, connect=status in ("no", "not signed in"))
                return self._prep_no_run("no_calendar", text, step)
            found = prep.find_event(briefs, ref.calendar_id, ref.event_id)
            if found is None or found.start is None or found.all_day:
                return self._prep_no_run("gone", prep_no_run_text("gone", owner), step)
            brief = found
            if ref.start is not None and brief.start != ref.start:
                moved_to = prep.meeting_of(alias, brief, now, zone)
                day = "" if moved_to.day == "today" else f"{moved_to.day} "
                moved = f"moved to {day}{dayplan.clock_text(moved_to.start, self._hour24())}"
        meeting = prep.meeting_of(alias, brief, now, zone)
        if wall_now >= meeting.end:
            return self._prep_no_run("ended", prep_no_run_text("ended", owner), step)
        _prep_meeting_done(live, step, meeting, moved, events, now, zone, self._hour24())
        return meeting

    def _prep_no_run(self, kind: str, text: str, step: LiveStep = NO_STEP) -> AskOutcome:
        """No brief and nothing read from mail: no such meeting, gone, ended, no calendar."""
        logger.info("Prep: no run (%s)", kind)
        _prep_meeting_stop(step, STATUS_WARN, text, status_text="NO RUN")
        return AskOutcome(True, "ok", text, mode=MODE_PREP, note_kind=kind)

    def _prep_choices(self, choices: Sequence[prep.PrepChoice], step: LiveStep = NO_STEP) -> AskOutcome:
        """Several meetings match, or one only by some title words (R5): the owner picks; nothing is
        read until that click."""
        from ..persona import prep_choices_text

        logger.info("Prep: no run (choices)")
        words = (f"{len(choices)} meetings match - Jarvis asks which one" if len(choices) > 1 else
                 "one meeting matches some of your words - Jarvis asks first")
        _prep_meeting_stop(step, STATUS_WARN, words, status_text="CHOOSE", choices=choices, hour24=self._hour24())
        return AskOutcome(True, "ok", prep_choices_text(len(choices), self._owner()), mode=MODE_PREP,
                          note_kind="choices", choices=tuple(choices))

    def _prep_without_run(self, reason: str, meeting: prep.PrepMeeting, lookups: Sequence[prep.GuestLookup],
                          pending: Sequence[ProposedAction], notes: Sequence[str], why: str, days: int,
                          live: LiveTask, people: LiveStep = NO_STEP) -> AskOutcome:
        """The brief of Jarvis's own (G4): no planner run, nothing counted."""
        from ..persona import PREP_NO_RUN_STATUS, prep_no_run_words

        kept = [replace(lookup, status=prep.NOT_SEARCHED, why=prep.NOT_READ_WHY)
                if lookup.status == prep.PENDING else lookup for lookup in lookups]
        brief = prep.deterministic_brief(meeting, kept, pending, notes, why, days=days)
        logger.info("Prep: brief without a planner run (%s)", reason)
        _prep_people_done(people, kept, days, meeting.alias)
        _prep_no_run_step(live, prep_no_run_words(reason))
        return AskOutcome(True, "ok", PREP_NO_RUN_STATUS, mode=MODE_PREP, note_kind=reason, brief=brief)

    def _prep_lookups(self, meeting: prep.PrepMeeting, lookups: Sequence[prep.GuestLookup],
                      accounts: Sequence[AccountInfo], days: int, other_accounts: bool,
                      cancel: threading.Event | None, live: LiveTask,
                      people: LiveStep, *, participants: Mapping[str, Sequence[Any]] | None = None
                      ) -> tuple[list[prep.GuestLookup], int, int]:
        """Each guest still to look up, in order, until 3 threads are found (spec 3.3 step 6):
        guest_allowed first (R2: refused -> neither the header list nor a search), then the header
        scan's participants index (the newest thread with that guest on the meeting's account; another
        readable account only with prep_other_accounts, R3), else Jarvis's own search on the meeting's
        account (and, with prep_other_accounts, on the other readable ones when that finds nothing).
        A thread another guest's lookup already found is not read twice (SAME). (lookups, searches,
        refused)."""
        lookups = list(lookups)
        alias = meeting.alias
        others = [account.alias for account in accounts
                  if other_accounts and account.alias != alias and account.read_mail == "yes"
                  and self.sources.reader(account.alias) is not None]
        found: dict[tuple[str, str], str] = {}
        searches = refused = 0
        for number, lookup in enumerate(lookups):
            if lookup.status != prep.PENDING:
                continue
            if cancel is not None and cancel.is_set():
                lookups[number] = replace(lookup, status=prep.NOT_SEARCHED, why=prep.CANCELLED_WHY)
                continue
            if len(found) >= prep.MAX_THREADS:
                lookups[number] = replace(lookup, status=prep.NOT_SEARCHED, why=prep.THREADS_WHY)
                continue
            address = lookup.guest.address
            if not prep.guest_allowed(address, days):
                refused += 1
                lookups[number] = replace(lookup, status=prep.REFUSED, why=prep.REFUSED_WHY)
                continue
            ref = prep.index_lookup(participants or {}, address, alias, other_accounts=other_accounts,
                                    since=self.clock() - timedelta(days=days))   # the prep lookback (R4)
            if ref is not None and (ref.alias == alias or ref.alias in others):
                key = (ref.alias, ref.thread_id)
                if key in found:
                    lookups[number] = replace(lookup, status=prep.SAME, alias=ref.alias, thread_id=ref.thread_id,
                                              why=found[key], via=prep.VIA_INDEX)
                else:
                    found[key] = lookup.guest.display
                    lookups[number] = replace(lookup, status=prep.FOUND, alias=ref.alias, thread_id=ref.thread_id,
                                              via=prep.VIA_INDEX)
                continue
            query = check_query(prep.prep_query(address, days), None)
            outcome = replace(lookup, status=prep.NONE)
            for target in [alias, *others]:
                searches += 1
                ids, error = self._prep_search(target, query, lookup, days, live, also=target != alias)
                if error:
                    if target == alias:
                        outcome = replace(lookup, status=prep.NOT_SEARCHED, why=prep.FAILED_WHY)
                        break
                    continue
                if ids:
                    key = (target, ids[0])
                    if key in found:
                        outcome = replace(lookup, status=prep.SAME, alias=target, thread_id=ids[0], why=found[key])
                    else:
                        found[key] = lookup.guest.display
                        outcome = replace(lookup, status=prep.FOUND, alias=target, thread_id=ids[0])
                    break
            lookups[number] = outcome
        return lookups, searches, refused

    def _prep_search(self, alias: str, query: str, lookup: prep.GuestLookup, days: int, live: LiveTask, *,
                     also: bool = False) -> tuple[list[str], bool]:
        """One search Jarvis built himself (one guest's address, the lookback, read only, at most one
        thread): (thread ids, failed)."""
        step = _prep_search_step(live, alias, query, lookup, days, also)
        reader = self.sources.reader(alias)
        if reader is None:
            _prep_search_done(step, None, f"Jarvis can't read the {alias} account's mail")
            return [], True
        try:
            ids = list(reader.search(query, max_threads=1))
        except GmailError as exc:
            _prep_search_done(step, None, f"Jarvis couldn't search your mail ({exc})")
            return [], True
        except Exception as exc:  # noqa: BLE001 - one guest's search must not stop the brief
            logger.warning("Prep: a %s search failed unexpectedly (%s)", logged_alias(alias), type(exc).__name__)
            _prep_search_done(step, None, f"unexpected error ({type(exc).__name__})")
            return [], True
        _prep_search_done(step, ids[:1], "", days=days)
        return ids[:1], False

    def _prep_reads(self, lookups: Sequence[prep.GuestLookup], notes: list[str], cancel: threading.Event | None,
                    live: LiveTask, shown: dict[tuple[str, str], LiveStep]
                    ) -> tuple[list[MailThread], list[prep.GuestLookup]]:
        """The threads found, read in guest order (at most 3; _read_thread: LIVE mail.thread with
        its email picture); a thread that can't be read is left out and its guests say so."""
        lookups = list(lookups)
        threads: list[MailThread] = []
        read: set[tuple[str, str]] = set()
        for number, lookup in enumerate(lookups):
            if lookup.status != prep.FOUND:
                continue
            if cancel is not None and cancel.is_set() or len(threads) >= prep.MAX_THREADS:
                why = prep.CANCELLED_WHY if cancel is not None and cancel.is_set() else prep.THREADS_WHY
                lookups[number] = replace(lookup, status=prep.NOT_SEARCHED, why=why)
                continue
            thread = self._read_thread(lookup.alias, lookup.thread_id, notes, live, prep.WHY_PREP, shown)
            if thread is None:
                lookups[number] = replace(lookup, status=prep.NOT_SEARCHED, why=prep.UNREAD_WHY)
                continue
            newest = thread.messages[-1].sent_at if thread.messages else None
            lookups[number] = replace(lookup, thread_id=thread.thread_id, last_at=newest)
            read.add((lookup.alias, lookup.thread_id))
            threads.append(thread)
        for number, lookup in enumerate(lookups):
            if lookup.status == prep.SAME and (lookup.alias, lookup.thread_id) not in read:
                lookups[number] = replace(lookup, status=prep.NOT_SEARCHED, why=prep.UNREAD_WHY)
        return threads, lookups

    # ---- a follow-up draft (spec 3.4) ---------------------------------------------------------

    def _nudge(self, request: ModeRequest, inputs: ModeInputs, page_ids: Sequence[str],
               cancel: threading.Event | None, on_stage: Callable[[str], None],
               live: LiveTask = NO_TASK) -> AskOutcome:
        """A drafted reply (WAITING) or nudge (OWED) in the one thread the owner clicked: the checks
        (Ask, the caps with the bundle's, Claude Code), the thread read in full, the guard (no run when
        it changed since it was listed), one run, its one Reply checked by NudgeCheck."""
        from ..persona import nudge_no_run_text

        target = request.thread
        if target is None:
            return AskOutcome(False, "error", "Jarvis can't tell which thread to draft in; nothing was proposed")
        refusal = self._refusal(live, bundle=True)
        if refusal is not None:
            if refusal.kind != DISABLED:
                logger.info("Nudge: no run (%s)", CAPS if refusal.kind == CAPS else NOT_READY)
            return replace(refusal, target=target)
        accounts = self.sources.accounts()
        alias = target.alias
        known = next((account for account in accounts if account.alias == alias), None)
        reader = self.sources.reader(alias)
        listed = request.origin == modes.ORIGIN_LIST
        if not self.config.ask.read_mail or known is None or known.read_mail != "yes" or reader is None:
            said = replace(known, read_mail=READ_OFF) if known is not None and not self.config.ask.read_mail else known
            return self._nudge_no_run("unreadable", unreadable_note(alias, said), target)
        own = frozenset(inputs.own) | frozenset(account.address.casefold() for account in accounts if account.address)
        own |= _reader_own(reader)
        on_stage(STAGE_THREAD)
        shown: dict[tuple[str, str], LiveStep] = {}
        mail_notes: list[str] = []
        thread = self._read_thread(alias, target.thread_id, mail_notes, live, WHY_NUDGE, shown)
        if cancel is not None and cancel.is_set():
            return AskOutcome(False, CANCELLED, "Cancelled; nothing was proposed", target=target)
        if thread is None:
            return self._nudge_no_run("gone", nudge_no_run_text("gone", self._owner()), target)
        guard = modes.nudge_guard(thread, target, own)
        _guard_field(shown.get((alias, thread.thread_id), NO_STEP), guard)
        if guard:
            return self._nudge_no_run(guard, nudge_no_run_text(guard, self._owner(), listed=listed), target,
                                      mail=report([thread]))
        zone = self.sources.time_zone()
        now = self.clock()
        notes = self._memory_notes()
        prompt, index = modes.build_nudge_prompt(now, self._owner(), known, thread, target, notes, request,
                                                 accounts=accounts, zone=zone)
        logger.info("Ask: context of %d characters (%s; mode nudge)", len(prompt),
                    ", ".join(f"{name} {count}" for name, count in index.counts().items()))
        _threads_given(shown, [thread], prompt)
        on_stage(STAGE_PLANNING)
        result = self._run(prompt, allow_search=False, cancel=cancel, live=live, number=1,
                           search_words=NUDGE_NO_SEARCH_WORDS, web_words=NUDGE_NO_WEB_WORDS, mode=MODE_NUDGE)
        del prompt
        mail = report([thread])
        plan = result.plan
        if result.failure is not None or plan is None:
            failure = result.failure
            return AskOutcome(False, failure.kind if failure else "error", failure.message if failure else "",
                              runs=1 if result.started else 0, init=result.init,
                              stats=(result.stats,) if result.started else (), mail=mail, target=target)
        check = modes.NudgeCheck(target, thread, notes, owner_words=request.typed, own=own)
        cards = to_cards(plan.lines, index, now=now, link_hosts=self.config.actions.link_hosts, page_ids=page_ids,
                         max_cards=MODE_MAX_LINES[MODE_NUDGE], kinds=MODE_KINDS[MODE_NUDGE], mode=MODE_NUDGE,
                         extra=check)
        _validated(live, cards, index, MODE_MAX_LINES[MODE_NUDGE], self._hour24())
        if any(card.error == modes.NUDGE_NOTE_LEAK for card in cards.cards):
            logger.info("Nudge: a draft held back (note)")
        message_parts = list(mail_notes)
        if cards.dropped:
            message_parts.append(f"{cards.dropped} more draft(s) were left out")
        if plan.via_fallback:
            message_parts.append("(read from the planner's plain text)")
        return AskOutcome(True, "ok", "; ".join(message_parts), say=plan.say, question=plan.question,
                          cards=cards.cards, dropped=cards.dropped, refused=cards.refused, runs=1, init=result.init,
                          stats=(result.stats,), mail=mail, mode=MODE_NUDGE, target=target)

    def _nudge_no_run(self, kind: str, text: str, target: Any, *, mail: MailReport | None = None) -> AskOutcome:
        """No draft and no run: the account can't be read, the thread is gone, or it changed since it
        was listed (the guard). Nothing counted."""
        logger.info("Nudge: no run (%s)", kind)
        return AskOutcome(True, "ok", text, mode=MODE_NUDGE, note_kind=kind, target=target,
                          mail=mail or MailReport())

    def _research_refusal(self, live: LiveTask = NO_TASK) -> AskOutcome | None:
        """The checks of a "web:" request ("Ready to research?"), first refusal wins; no run."""
        step = live.step(ASK_CHECKS, RESEARCH_CHECKS_TITLE)
        if not self.config.ask.enabled:
            step.field("Ask", "off")
            step.done(STATUS_BLOCKED, summary=DISABLED_MESSAGE)
            return AskOutcome(False, DISABLED, DISABLED_MESSAGE)
        step.field("Ask", "on")
        if not getattr(self._research_config(), "enabled", False):
            step.field("Web research", "off")
            step.done(STATUS_BLOCKED, summary=RESEARCH_OFF_MESSAGE)
            return AskOutcome(False, RESEARCH_OFF, RESEARCH_OFF_MESSAGE)
        step.field("Web research", "on")
        caps = self.usage.check(runs=1, research=1)
        _checks_caps(step, caps, self.usage)
        research_live.checks_research(step, caps, self.usage)
        if not caps.allowed:
            step.done(STATUS_BLOCKED, summary=caps.message)
            return AskOutcome(False, _caps_kind(caps), caps.message, caps=caps)
        ready = self.engine.readiness()
        _checks_ready(step, ready)
        if not ready.ok:
            step.done(STATUS_BLOCKED, summary=ready.message)
            return AskOutcome(False, ready.problem or NOT_READY, ready.message)
        research_ready = self.research.readiness()
        if not research_ready.ok:
            step.done(STATUS_BLOCKED, summary=research_ready.message)
            return AskOutcome(False, research_ready.problem or NOT_READY, research_ready.message)
        research_live.checks_tools(step, self.research.limits)
        step.done(STATUS_OK)
        return None

    def _research_only(self, question: str, page_ids: Sequence[str], cancel: threading.Event | None,
                       on_stage: Callable[[str], None], live: LiveTask = NO_TASK) -> AskOutcome:
        """A "web:" request: the checks, then web research with the owner's words. Nothing is read
        from the accounts (no calendar, briefing, thread or search: _context is never called)."""
        if not question:
            live.step(ASK_CHECKS, RESEARCH_CHECKS_TITLE, status=STATUS_BLOCKED, summary=EMPTY_WEB_MESSAGE)
            return AskOutcome(False, EMPTY, EMPTY_WEB_MESSAGE)
        refusal = self._research_refusal(live)
        if refusal is not None:
            return refusal
        logger.info("Research: started by web: (nothing read from the accounts)")
        return self._research(question, "", forced=True, page_ids=page_ids, cancel=cancel, on_stage=on_stage,
                              live=live)

    def _research_routed(self, command: str, plan: Plan, first: RunResult, ctx: AskContext, now: datetime,
                         notes: Sequence[str], page_ids: Sequence[str], cancel: threading.Event | None,
                         on_stage: Callable[[str], None], live: LiveTask) -> AskOutcome:
        """Planner run 1 asked for web research: it gets the owner's command as typed; the planner's
        shorter question goes along as a search hint only when every word in it is the owner's (or a
        date or a plain word): nothing from the calendar, mail or briefing reaches the research."""
        assert plan.web is not None
        check = check_question(plan.web.question, command, now)
        hint = check.text if check.ok and not same_question(check.text, command) else ""
        logger.info("Research: handed over by the planner (its question %s)", "used" if hint else "not used")
        return self._research(command, hint, forced=False, page_ids=page_ids, cancel=cancel, on_stage=on_stage,
                              live=live, runs=1, stats=(first.stats,), init=first.init, mail=report(ctx.mail),
                              notes=notes, check=check, planner_question=plan.web.question,
                              planner_why=plan.web.why, lines_left=len(plan.lines))

    def _research(self, question: str, hint: str, *, forced: bool, page_ids: Sequence[str],
                  cancel: threading.Event | None, on_stage: Callable[[str], None], live: LiveTask = NO_TASK,
                  runs: int = 0, stats: tuple[RunStats, ...] = (), init: InitSummary | None = None,
                  mail: MailReport | None = None, notes: Sequence[str] = (), check: Any = None,
                  planner_question: str = "", planner_why: str = "", lines_left: int = 0) -> AskOutcome:
        """One web research (forced or routed): the caps, the isolated stdin (the question, the
        hint, today's date and zone, Jarvis's limits: nothing else), the run, then the answer's
        check -> AskOutcome(research=ResearchReport, cards=...)."""
        mail = mail if mail is not None else MailReport()
        if cancel is not None and cancel.is_set():
            return AskOutcome(False, CANCELLED, "Cancelled; nothing was proposed", runs=runs, stats=stats, init=init,
                              mail=mail)
        caps = self.usage.check(runs=1, research=1)
        if not caps.allowed:
            live.step(ASK_CHECKS, "Web research", status=STATUS_BLOCKED, summary=caps.message)
            return AskOutcome(False, _caps_kind(caps), caps.message, runs=runs, stats=stats, init=init, mail=mail,
                              caps=caps)
        now = self.clock()
        zone = self._research_zone()
        limits = self.research.limits
        sent_question, sent_hint = question_text(question), hint_text(hint)
        prompt = build_research_prompt(question, hint=hint, now=now, time_zone=zone, limits=limits)
        research_live.input_step(live, forced=forced, question=sent_question, hint=sent_hint, now=now, time_zone=zone,
                                 hour24=self._hour24(), check=check, planner_question=planner_question,
                                 planner_why=planner_why, lines_left=lines_left)
        on_stage(STAGE_RESEARCH)
        # The run's web steps (with the LIVE view): its searches and page reads as they happen, then
        # the pictures of the sources the checked answer cites. Its page pictures end with this
        # research (finished()), after those of the sources: also when it failed or never started.
        web_steps = research_live.WebSteps(live, limits, snapshots=self.page_snapshots) if live.enabled else None
        try:
            result = self._research_run(prompt, question, cancel, live, web_steps)
            del prompt
            if result.started:
                runs, stats = runs + 1, stats + (result.stats,)
            init = init or result.init
            answer = result.answer
            if result.failure is not None or not isinstance(answer, ResearchAnswer) or result.web is None:
                failure = result.failure
                return AskOutcome(False, failure.kind if failure else "error",
                                  failure.message if failure else "The web research's answer could not be read; "
                                                                  "nothing was proposed",
                                  runs=runs, stats=stats, init=init, mail=mail)
            cards, found = validate_research(answer, result.web, now=now,
                                             max_sources=self.research.limits.max_sources,
                                             max_cards=self.config.ask.max_cards, page_ids=page_ids,
                                             question=sent_question, hint=sent_hint, forced=forced)
            research_live.validated(live, cards, found, answer, result.web, self._hour24(),
                                    web_steps=web_steps)
            logger.info("Research: %d source(s) shown, %d left out, %d suggestion card(s), %d dropped",
                        len(found.sources), len(found.left_out), len(found.suggested), len(found.dropped))
            message = "; ".join([*notes, *research_notes(found)])
            return AskOutcome(True, "ok", message, say=found.answer, cards=cards,
                              refused=sum(1 for card in cards if card.error), runs=runs, init=init, stats=stats,
                              mail=mail, research=found)
        finally:
            if web_steps is not None:
                web_steps.finished()   # no more page pictures for this research

    def _research_run(self, prompt: str, question: str, cancel: threading.Event | None,
                      live: LiveTask = NO_TASK, web_steps: research_live.WebSteps | None = None) -> RunResult:
        """The research run with its LIVE steps; counted (kind "research") only when it starts.
        ``web_steps`` (with the LIVE view) shows its searches (each drawn as a results page) and
        page reads as they happen; with ``page_snapshots`` (the app's
        snapshot_engine.PageSnapshotter) the pages the run read are pictured on their web.fetch
        steps. The run is told nothing of it."""
        research = self._research_config()
        limits = self.research.limits
        step = research_live.run_step(live, model=research.model, prompt=prompt, limits=limits,
                                      timeout_s=research.timeout_seconds)
        ready, refused = self.research.preflight()   # a run refused before it starts is not counted
        if refused is not None:
            research_live.run_skipped(step, refused.message)
            return RunResult(failure=refused, started=False)
        research_live.run_ready(step, self.research, ready)
        return self._research_started(prompt, question, cancel, live, step,
                                      web_steps if step is not NO_STEP else None)

    def _research_started(self, prompt: str, question: str, cancel: threading.Event | None, live: LiveTask,
                          step: LiveStep, web_steps: research_live.WebSteps | None) -> RunResult:
        run = self.usage.start(kind=KIND_RESEARCH)
        result: RunResult | None = None
        try:
            extra: dict[str, Any] = {}
            if web_steps is not None:   # only with the LIVE view
                extra = {"progress": research_live.run_progress(step, web_steps), "capture": True}
            result = self.research.run(prompt, question=question, cancel=cancel, **extra)
            if result.limit is not None and result.limit.overage:
                self._pause_for_overage(result.limit.resets_at)
            research_live.run_result(step, result, keep_text=live.keep_text)
            return result
        finally:
            stats = result.stats if result is not None else RunStats()
            self.usage.finish(run, outcome=result.outcome if result is not None else "error",
                              duration_ms=stats.duration_ms, turns=stats.turns, input_tokens=stats.input_tokens,
                              output_tokens=stats.output_tokens, cache_read_tokens=stats.cache_read_tokens,
                              cache_write_tokens=stats.cache_write_tokens)

    def _search(self, account: str, query: str, ctx: AskContext, searchable: set[str],
                on_stage: Callable[[str], None], cancel: threading.Event | None,
                grounding: SearchGrounding, live: LiveTask = NO_TASK, why: str = "",
                threads_shown: dict[tuple[str, str], LiveStep] | None = None) -> tuple[str, list[MailThread], str]:
        """(a note when nothing could be read, the new threads, the search as it ran ("" when it
        did not run)). The search must name only what the owner typed (``grounding``)."""
        step = live.step(MAIL_SEARCH, "Mail search the planner asked for",
                         fields=[("Account", account), Field("Planner's query", query, mono=True), ("Why", why)])
        if account not in searchable:
            known = next((item for item in ctx.accounts if item.alias == account), None)
            note = unreadable_note(account, known)
            step.done(STATUS_BLOCKED, summary=note)
            return note, [], ""
        try:
            query = check_query(query, grounding)
        except QueryRefused as exc:
            logger.info("Ask: refused a mail search the planner asked for")
            reason = str(exc)
            step.done(STATUS_BLOCKED, status_text="REFUSED", summary=f"Jarvis refused it: {reason}")
            return f"Jarvis didn't search your mail: {reason[:1].lower()}{reason[1:]}", [], ""
        reader = self.sources.reader(account)
        if reader is None:
            note = f"Jarvis can't read the {account} account's mail"
            step.done(STATUS_BLOCKED, summary=note)
            return note, [], ""
        if cancel is not None and cancel.is_set():
            step.done(STATUS_CANCELLED, summary="Cancel clicked - nothing was searched")
            return "", [], ""
        on_stage(STAGE_MAIL)
        seen = {(thread.account, thread.thread_id) for thread in ctx.mail}
        step.field("Gmail query (exact)", query, mono=True)
        step.field("Rules", f"read only - never spam or trash - at most {MAX_THREADS} threads")
        step.note("Sent to Gmail (read only)")
        try:
            ids = reader.search(query, max_threads=MAX_THREADS)
        except GmailError as exc:
            note = f"Jarvis couldn't search your mail ({exc})"
            step.done(STATUS_FAILED, summary=note)
            return note, [], query
        step.field("Found", _count_words(len(ids), "thread"))
        notes: list[str] = []
        found: list[MailThread] = []
        skipped = limited = 0
        for thread_id in ids:
            if cancel is not None and cancel.is_set():
                step.done(STATUS_CANCELLED, summary="Cancel clicked - the rest was not read")
                return "", [], query
            if len(found) + len(ctx.mail) >= MAX_THREADS or (account, thread_id) in seen:
                if (account, thread_id) in seen:
                    skipped += 1
                else:
                    limited += 1
                continue
            thread = self._read_thread(account, thread_id, notes, live, WHY_SEARCH, threads_shown)
            if thread is not None:
                found.append(thread)
        if skipped:
            step.note(f"{skipped} already read for this request - skipped")
        if limited:
            step.note(f"{MAX_THREADS}-thread limit reached")
        if not found:
            note = notes[0] if notes else "Jarvis found no email matching that search; nothing was read"
            if notes:
                step.done(STATUS_WARN, summary=note)
            else:
                step.done(STATUS_WARN, status_text="NOTHING FOUND", summary="nothing was read")
            return note, [], query
        step.done(STATUS_OK, summary=f"{_count_words(len(found), 'thread')} read")
        return "", found, query

    def _finish(self, plan: Plan, index: ContextIndex, *, now: datetime, page_ids: Sequence[str], notes: list[str],
                runs: int, stats: tuple[RunStats, ...], init: InitSummary | None, mail: MailReport, search: str,
                search_account: str, live: LiveTask = NO_TASK, suggested: tuple[str, str] = ("", "")) -> AskOutcome:
        cards: Cards = to_cards(plan.lines, index, now=now, link_hosts=self.config.actions.link_hosts,
                                page_ids=page_ids, max_cards=self.config.ask.max_cards)
        _validated(live, cards, index, self.config.ask.max_cards, self._hour24(), suggested)
        message_parts = list(notes)
        if cards.dropped:
            message_parts.append(f"{cards.dropped} more proposal(s) were left out")
        if plan.via_fallback:
            message_parts.append("(read from the planner's plain text)")
        return AskOutcome(True, "ok", "; ".join(message_parts), say=plan.say, question=plan.question,
                          cards=cards.cards, dropped=cards.dropped, refused=cards.refused, runs=runs, init=init,
                          stats=stats, mail=mail, search=search, search_account=search_account,
                          suggestion=suggested[0])

    # ---- dry run ------------------------------------------------------------------------------

    def dry_run(self, command: str, *, briefing: BriefingContext | None = None) -> DryRun:
        """What plan() would send first, without any planner run (and without counting against the
        caps): the stdin text with each email's text replaced by its length, and the argv."""
        command = " ".join((command or "").split())[:COMMAND_CAP]
        now = self.clock()
        ctx, _ = self._context(command, briefing, now, lambda stage: None)
        prompt, index = build_prompt(ctx, redact_mail=True)
        ready = self.engine.readiness()
        argv: tuple[str, ...] = ()
        if ready.exe is not None:
            searchable = any(account.read_mail == "yes" for account in ctx.accounts)
            allow_web = self._web_allowed()[0] if ready.ok else False   # as planner run 1 would
            argv = tuple(cli.build_argv(ready.exe.path, model=self.config.ask.model,
                                        max_turns=self.config.ask.max_turns,
                                        schema=cli.schema_text(allow_search=searchable, allow_web=allow_web),
                                        hardened=ready.hardened))
        return DryRun(prompt, argv, ready, index.counts(), report(ctx.mail))

    def research_dry_run(self, question: str) -> DryRun:
        """What a "web:" request's research run would get (its whole stdin) and its command line,
        without any run (and without counting): nothing is read from the accounts (the time zone
        as a real research: cached or this PC's)."""
        now = self.clock()
        prompt = build_research_prompt(question, hint="", now=now, time_zone=self._research_zone(),
                                       limits=self.research.limits)
        ready = self.research.readiness()
        argv: tuple[str, ...] = ()
        if ready.exe is not None:
            argv = tuple(self.research.argv(ready))
        return DryRun(prompt, argv, ready, {}, MailReport(), research=True)


# --------------------------------------------------------------------------
# The LIVE view's steps of an Ask (memory only, on screen only; every helper is quiet: whatever
# goes wrong in one, the Ask goes on exactly as without the LIVE view)
# --------------------------------------------------------------------------

CHECKS_TITLE = "Ready to plan?"
RESEARCH_CHECKS_TITLE = "Ready to research?"
BUSY_MESSAGE = "Jarvis is already planning; wait or Cancel it"
EMPTY_MESSAGE = "Type what you'd like Jarvis to do"
WHY_BRIEFING = "your request names this briefing reply"
WHY_SEARCH = "found by the planner's search"
MAX_PEOPLE_SHOWN = 20
STOPPED_KINDS = frozenset({"guard", "limit", "not_signed_in"})
HANDOVER_WORDS = "asked for web research"   # planner run 1's summary when it hands the request over
HANDOVER_WHY_CHARS = 80
OVERAGE_WORDS = "Claude Code said this would use extra usage, so Ask stopped it"
# The planner run's stdin block: named "sent" only once Claude Code has started with it.
STDIN_PENDING = "Text for Claude Code (stdin) - not sent yet"
STDIN_SENT = "Exact text sent to Claude Code (stdin)"
STDIN_NOT_SENT = "Text not sent to Claude Code (stdin) - it did not start"
REMEMBER_WORDS = "yes - one note, shown to you and kept only if you click Remember this"
SUGGESTION_KEPT = ("passed the checks; offered unless you already have it (MEMORY says) - kept only if you click "
                   "Remember this")
SUGGESTION_DROPPED = "not offered ({reason})"
SUGGESTION_HANDED_OVER = "handed to web research"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_KIND_WORDS = {"rsvp": "RSVP", "todo": "Todo"}


def _count_words(count: int, noun: str, plural: str = "") -> str:
    return f"{count} {noun if count == 1 else (plural or noun + 's')}"


def _day(moment: date | datetime) -> str:
    return f"{_DAYS[moment.weekday()]} {_MONTHS[moment.month - 1]} {moment.day}"


def _clock(moment: datetime, hour24: bool) -> tuple[str, str]:
    if hour24:
        return f"{moment.hour:02d}:{moment.minute:02d}", ""
    return f"{moment.hour % 12 or 12}:{moment.minute:02d}", "AM" if moment.hour < 12 else "PM"


def event_when(brief: EventBrief, hour24: bool = False) -> str:
    """"Wed Oct 7 2:00-3:00 PM", "Wed Oct 7 11:00 AM-1:00 PM", "Wed Oct 7 all day", "Mon Oct 12 to
    Fri Oct 16 all day" (the event's own wall times)."""
    if brief.all_day_start is not None:
        last = brief.all_day_end or brief.all_day_start
        if last == brief.all_day_start:
            return f"{_day(brief.all_day_start)} all day"
        return f"{_day(brief.all_day_start)} to {_day(last)} all day"
    if brief.start is None:
        return "no time"
    end = brief.end or brief.start
    (first, first_m), (last, last_m) = _clock(brief.start, hour24), _clock(end, hour24)
    if end.date() != brief.start.date():
        return f"{_day(brief.start)} {first} {first_m} to {_day(end)} {last} {last_m}".replace("  ", " ").strip()
    if first_m == last_m:
        return f"{_day(brief.start)} {first}-{last} {last_m}".rstrip()
    return f"{_day(brief.start)} {first} {first_m}-{last} {last_m}"


def event_item(alias: str, brief: EventBrief, hour24: bool = False) -> str:
    """One event as the LIVE view lists it (never a guest's name or address: they are in the stdin
    block)."""
    who = "you organize" if brief.organizer_self else "organized by someone else"
    guests = len(brief.guests)
    parts = [event_when(brief, hour24), brief.title or "(No title)", alias, who,
             _count_words(guests, "guest") if guests else "no guests"]
    if brief.recurring:
        parts.append("repeating")
    return " - ".join(parts)


def _event_key(brief: EventBrief) -> datetime:
    if brief.start is not None:
        return brief.start if brief.start.tzinfo is not None else brief.start.astimezone()
    first = brief.all_day_start or date.min
    return datetime.combine(first, datetime.min.time()).astimezone()


@_quiet(NO_STEP)
def _calendar_step(live: LiveTask, start: datetime, end: datetime, calendars: Sequence[str]) -> LiveStep:
    if not live.enabled:
        return NO_STEP
    last = end - timedelta(days=1)
    return live.step(CALENDAR_READ, "Calendar", fields=[("Window", f"{_day(start)} to {_day(last)}"),
                                                        ("Calendars", ", ".join(calendars))])


_CALENDAR_WORDS = {"no": "no calendar", "not signed in": "not signed in - not read", "error": "error - not read"}


@_quiet()
def _calendar_account(step: LiveStep, alias: str, status: str, briefs: Sequence[EventBrief] | None, *,
                      cancelled: bool = False) -> None:
    if step is NO_STEP:
        return
    if briefs is not None:
        step.field(alias, _count_words(len(briefs), "event"))
    elif cancelled:
        step.field(alias, "not read (cancelled)")
    else:
        step.field(alias, _CALENDAR_WORDS.get(status, f"{status} - not read"))


@_quiet()
def _calendar_done(step: LiveStep, events: Mapping[str, Sequence[EventBrief]], accounts: Sequence[AccountInfo],
                   hour24: bool, *, cancelled: bool, start: datetime | None = None, end: datetime | None = None,
                   now: datetime | None = None) -> None:
    if step is NO_STEP:
        return
    listed = sorted(((brief, alias) for alias, briefs in events.items() for brief in briefs),
                    key=lambda pair: (_event_key(pair[0]), pair[1]))
    for brief, alias in listed:
        step.item(event_item(alias, brief, hour24))
    if not cancelled and start is not None and end is not None and now is not None:
        _week_picture(step, events, start, end, now, hour24)
    summary = f"{_count_words(len(listed), 'event')} read"
    if cancelled:
        step.done(STATUS_CANCELLED, summary=f"{summary}; Cancel clicked")
    elif any(account.calendar == "error" for account in accounts):
        step.done(STATUS_WARN, summary=f"{summary}; a calendar answered with an error")
    else:
        step.done(STATUS_OK, summary=summary)


@_quiet()
def _week_picture(step: LiveStep, events: Mapping[str, Sequence[EventBrief]], start: datetime, end: datetime,
                  now: datetime, hour24: bool) -> None:
    """The LIVE picture of the calendar read: its 7 days from today (drawn from what was read)."""
    step.picture(week_picture(events, start, end, now=now, hour24=hour24))


@_quiet()
def _given(live: LiveTask, ctx: AskContext, index: ContextIndex) -> None:
    """After the first build_prompt: how many of the events read the planner was given."""
    if not live.enabled:
        return
    read = sum(len(briefs) for briefs in ctx.events.values())
    given = index.counts()["events"]
    words = f"{read} given to the planner" if given == read else f"{given} given - the rest did not fit"
    live.find(CALENDAR_READ).update(summary=f"{_count_words(read, 'event')} read; {words}")


@_quiet(NO_STEP)
def _thread_step(live: LiveTask, alias: str, thread_id: str, why: str) -> LiveStep:
    if not live.enabled:
        return NO_STEP
    return live.step(MAIL_THREAD, "Read email thread", key=f"{alias}:{thread_id}",
                     fields=[("Account", alias), ("Why", why), Field("Thread id", thread_id, mono=True)])


def _person_text(person: MailPerson) -> str:
    name = " ".join((person.name or "").split())
    return f"{name} <{person.email}>" if name else person.email


@_quiet()
def _thread_picture(step: LiveStep, thread: MailThread, hour24: bool) -> None:
    """The LIVE picture of a thread read: its newest message drawn as an email, the older ones as lines."""
    step.picture(email_picture(thread, hour24=hour24))


@_quiet()
def _thread_read(step: LiveStep, thread: MailThread | None, hour24: bool = False) -> None:
    if step is NO_STEP:
        return
    if thread is None:
        step.done(STATUS_WARN, summary="Gmail's answer was not a thread Jarvis can read; nothing was read")
        return
    people: list[str] = []
    seen: set[str] = set()
    for message in thread.messages:
        for person in message.people():
            key = (person.email or "").casefold()
            if key and key not in seen:
                seen.add(key)
                people.append(_person_text(person))
    shown = ", ".join(people[:MAX_PEOPLE_SHOWN])
    if len(people) > MAX_PEOPLE_SHOWN:
        shown += f", +{len(people) - MAX_PEOPLE_SHOWN} more"
    count = len(thread.messages)
    messages = f"{count} read (the newest)" if thread.left_out else f"{count} read"
    if thread.left_out:
        messages += f"; {thread.left_out} older not read"
    step.field("Subject", thread.subject or "(no subject)")
    step.field("People", shown or "none shown")
    step.field("Messages", messages)
    _thread_picture(step, thread, hour24)
    step.done(STATUS_OK, summary=f"{_count_words(count, 'message')} read")


@_quiet()
def _threads_given(shown: Mapping[tuple[str, str], LiveStep], mail: Sequence[MailThread], prompt: str) -> None:
    """After a build_prompt: each thread step shows the exact lines the planner got, checked
    against the prompt; a thread that is not in it says so."""
    if not shown:
        return
    fitted = {(thread.account, thread.thread_id): thread for thread in fit_mail(mail)}
    for key, step in shown.items():
        thread = fitted.get(key)
        text = thread_text(thread) if thread is not None else ""
        if text and text in prompt:
            step.block("Text given to the planner", text, untrusted=True)
        else:
            step.done(STATUS_WARN, summary="Not given to the planner: the mail did not fit")


@_quiet(NO_STEP)
def _run_step(live: LiveTask, model: str, prompt: str, number: int, search_words: str,
              web_words: str = "", allow_remember: bool = False, mode: str = "") -> LiveStep:
    if not live.enabled:
        return NO_STEP
    title = "Planner run 1" if number == 1 else f"Planner run {number} (with your mail)"
    if mode:
        title = f"Planner run ({MODE_RUN_WORDS.get(mode, mode)})"
    fields: list[Field | tuple[str, str]] = [Field("Model", model, mono=True)]
    if mode:
        fields += [("Mail search", "no"), ("Web research", "no")]
    else:
        fields.append(("May ask to search mail", search_words or "yes"))
    if web_words and not mode:
        fields.append(("May ask for web research", web_words))
    if allow_remember:
        fields.append(("May suggest a note", REMEMBER_WORDS))
    fields.append(("Input", f"{len(prompt):,} characters"))
    step = live.step(PLANNER_RUN, title, fields=fields)
    step.block(STDIN_PENDING, prompt, untrusted=True)
    return step


@_quiet()
def _run_ready(step: LiveStep, engine: Any, ready: Readiness, allow_search: bool, allow_web: bool = False,
               allow_remember: bool = False, mode: str = "") -> None:
    if step is NO_STEP:
        return
    hardened = "hardened (--safe-mode --restricted)" if ready.hardened else "not hardened"
    step.field("Claude Code", f"{ready.version or '?'}, {hardened}")
    method = ready.auth.method if ready.auth is not None else ""
    step.note(f"Sign-in checked again: {'claude.ai plan' if method == 'claude.ai' else method or 'ok'} "
              "(no model request)")
    extra = {"allow_web": True} if allow_web else {}
    if allow_remember:
        extra["allow_remember"] = True
    try:
        if mode:
            argv = engine.mode_argv(ready, mode, allow_remember=allow_remember)
        else:
            argv = engine.argv(ready, allow_search=allow_search, **extra)
    except Exception:  # noqa: BLE001 - only what the view shows
        return
    step.field("Command line", command_line_text(argv), mono=True)


def _run_progress(step: LiveStep) -> Callable[[str, Any], None]:
    def progress(what: str, value: Any) -> None:
        if what == "started":
            step.rename_block(STDIN_PENDING, STDIN_SENT)
            step.note("Claude Code started")
        elif what == "init" and value is not None:
            step.note(f"Checked its start: model {value.model or '?'}, tools: {', '.join(value.tools) or 'none'}, "
                      f"MCP servers: {value.mcp_servers}, API key source: {value.api_key_source}")
        elif what == "turn":
            step.note(f"Claude is answering (turn {value})")
        elif what == "result":
            step.note("Answer received")
        elif what == "stopped" and value is not None:
            step.note(f"Stopped: {value.message}")

    return progress


@_quiet()
def _run_result(step: LiveStep, result: RunResult, *, keep_text: bool = True) -> None:
    if step is NO_STEP:
        return
    if result.failure is not None and result.failure.detail == "start":
        step.rename_block(STDIN_PENDING, STDIN_NOT_SENT)   # Claude Code could not be started
    else:
        step.rename_block(STDIN_PENDING, STDIN_SENT)       # an engine that reports no "started"
    stats = result.stats
    step.field("Took", elapsed_text(stats.duration_ms / 1000))
    if stats.turns:
        step.field("Turns", str(stats.turns))
    if stats.input_tokens or stats.output_tokens or stats.cache_read_tokens or stats.cache_write_tokens:
        step.field("Tokens", f"{stats.input_tokens:,} in / {stats.output_tokens:,} out / "
                             f"{stats.cache_read_tokens:,} cache read / {stats.cache_write_tokens:,} cache write")
    if result.init is not None and result.init.model:
        step.field("Model (Claude Code says)", result.init.model, mono=True)
    if result.reply:
        step.block("Planner's reply (raw)", result.reply)
    plan = result.plan
    if plan is not None:
        if plan.say:
            step.item(f"Says: {plan.say}")
        if plan.question:
            step.item(f"Asks: {plan.question}")
        if plan.search is not None:
            search = plan.search
            step.item(f"Wants a mail search: {search.account} - {search.query} - {search.why}")
        if plan.web is not None:
            step.item(f"Wants web research: {plan.web.question} - {plan.web.why}")
        for note in plan.remember:
            step.item(f"Suggests a note: {note}" if keep_text else
                      f"Suggests a note [{_count_words(len(note), 'character')} - not kept ([live] text = false)]")
        for line in plan.lines:
            step.item(line_text(line, keep_text), mono=True)
    answer = result.answer
    if isinstance(answer, prep.PrepAnswer):   # a meeting brief's answer (its items: "Checking the brief")
        if answer.say:
            step.item(f"Says: {answer.say}")
        step.item(f"A brief: {_count_words(len(answer.threads), 'thread note')}, "
                  f"{_count_words(len(answer.open), 'open item')}, {_count_words(len(answer.points), 'point')}")
        for line in answer.lines:
            step.item(line_text(line, keep_text), mono=True)
    desktop = answer if isinstance(answer, desktop_validate.DesktopAnswer) else None
    if desktop is not None:   # a desktop plan (its steps are checked in "Checking the plan": codes, never names)
        if desktop.say:
            step.item(f"Says: {desktop.say}")
        if desktop.question:
            step.item(f"Asks: {desktop.question}")
    failure = result.failure
    if failure is None and plan is not None and plan.web is not None and plan.search is None:
        why = plan.web.why if 0 < len(plan.web.why) <= HANDOVER_WHY_CHARS else ""
        step.done(STATUS_OK, summary=HANDOVER_WORDS + (f" - {why}" if why else ""))
    elif failure is None and isinstance(answer, prep.PrepAnswer):
        step.done(STATUS_OK, summary="a brief" + (f" and {_count_words(len(answer.lines), 'reply draft')}"
                                                  if answer.lines else "") + (" (from its plain text)"
                                                                              if answer.via_fallback else ""))
    elif failure is None and desktop is not None:
        step.done(STATUS_OK, summary=f"a plan of {_count_words(len(desktop.steps), 'step')}"
                                     + (" and a question" if desktop.question else "")
                                     + (" (from its plain text)" if desktop.via_fallback else ""))
    elif failure is None:
        lines = len(plan.lines) if plan is not None else 0
        step.done(STATUS_OK, summary=_count_words(lines, "proposal") + (" (from its plain text)"
                                                                       if plan is not None and plan.via_fallback
                                                                       else ""))
    elif failure.kind == CANCELLED:
        step.done(STATUS_CANCELLED, summary="Cancel clicked")
    elif failure.kind in STOPPED_KINDS:
        summary = OVERAGE_WORDS if failure.detail == OVERAGE else failure.message
        step.done(STATUS_BLOCKED, status_text="STOPPED", summary=summary)
    else:
        step.done(STATUS_FAILED, summary=failure.message)


@_quiet()
def _checks_caps(step: LiveStep, caps: CapCheck, usage: Any) -> None:
    if step is NO_STEP:
        return
    hour = getattr(usage, "max_per_hour", "?")
    day = getattr(usage, "max_per_day", "?")
    step.field("Planner runs left", f"{caps.left_hour} of {hour} this hour, {caps.left_day} of {day} today")


@_quiet()
def _checks_bundle(step: LiveStep, caps: CapCheck, usage: Any, reserve: int) -> None:
    """A planner mode's own checks: the bundle runs left today and the Ask runs kept for typed requests."""
    if step is NO_STEP:
        return
    most = getattr(usage, "max_bundle_per_day", "?")
    step.field("Bundle runs left today", f"{caps.left_bundle_day} of {most}")
    step.field("Kept for your own Asks", f"{reserve} an hour")


@_quiet()
def _checks_ready(step: LiveStep, ready: Readiness) -> None:
    if step is NO_STEP:
        return
    if ready.exe is None:
        step.field("Claude Code", "not found")
    else:
        step.field("Claude Code", f"{ready.version or 'version unknown'}, {ready.exe.source}")
    auth = ready.auth
    if auth is not None:
        if auth.ok:
            step.field("Sign-in", "claude.ai plan (checked with `claude auth status`, no model request)")
        else:
            step.field("Sign-in", f"{auth.method or 'not signed in'} - not a claude.ai plan sign-in")
    if ready.exe is not None and ready.version:
        step.field("Hardening", "--safe-mode --restricted used" if ready.hardened else
                   ("available, off ([ask] hardened_flags)" if ready.hardening else "not available"))


_LINE_BODY_RE = re.compile(r"\|\s*body\s*=", re.IGNORECASE)


def line_text(line: str, keep_text: bool = True) -> str:
    """A proposal line as the LIVE view lists it: as written, or with [live] text = false its
    body= text (an outgoing message or note: the rest of the line) as its size only."""
    if keep_text:
        return line
    match = _LINE_BODY_RE.search(line)
    if match is None:
        return line
    body = line[match.end():].strip()
    return f"{line[:match.end()]} [{_count_words(len(body), 'character')} - not kept ([live] text = false)]"


def card_details(card: ProposedAction, hour24: bool = False) -> list[str]:
    """What a card does, for the LIVE view: its account, then the recipients (a Reply / Email), or
    the event's title and id and the change (an RSVP, Move or Cancel), or the title and time (a
    Calendar event, a Todo's block): two cards with the same title still read differently."""
    parts = [card.account] if card.account else []
    kind = card.kind
    title = card.title or card.raw
    if kind in ("email", "reply"):
        recipients = ", ".join(card.mail_recipients())
        if recipients:
            parts.append(f"to {recipients}")
        parts.append(title)
    elif kind in ("rsvp", "move", "cancel"):
        event = card.field("event")
        parts.append(f"{title} (event {event})" if event else title)
        if kind == "rsvp" and card.field("answer"):
            parts.append(f"answer {card.field('answer')}")
        elif kind == "move":
            parts.append(f"to {event_when(card, hour24)}" if card.start is not None else "no new time")
        elif kind == "cancel":
            parts.append("cancel the event")
        if card.field("notify"):
            parts.append(f"notify {card.field('notify')}")
    elif kind == "calendar":
        parts.append(title)
        if card.start is not None or card.all_day_start is not None:
            parts.append(event_when(card, hour24))
        if card.repeat:
            parts.append(card.repeat)
    elif kind == "todo":
        parts.append(title)
        if card.field("due"):
            parts.append(f"due {card.field('due')}")
        if card.start is not None:
            parts.append(f"block {event_when(card, hour24)}")
        if card.web and card.link:   # a web research Todo's page: any public site
            parts.append(f"opens {link_host(card.link)}")
    else:
        parts.append(title)
    return [part for part in parts if part]


def card_text(card: ProposedAction, hour24: bool = False) -> str:
    """A card as the LIVE view lists it: "Email - work - to sam@example.com - Re: Budget", "Move -
    work - Team sync (event abc123) - to Fri Oct 9 2:00-3:00 PM - notify all"."""
    return " - ".join([_KIND_WORDS.get(card.kind, card.kind.capitalize()), *card_details(card, hour24)])


@_quiet()
def _suggestion_item(step: LiveStep, suggested: tuple[str, str], keep_text: bool) -> None:
    """The planner's suggested note in "Checking the proposals": kept (offered to the owner, saved
    only on his click) or dropped with its reason word."""
    text, reason = suggested
    if text:
        shown = text if keep_text else f"[{_count_words(len(text), 'character')}]"
        step.item(f"Note to remember: {shown}", status=STATUS_OK, note=SUGGESTION_KEPT)
    elif reason:
        step.item("Note to remember", status=STATUS_WARN, status_text="DROPPED",
                  note=SUGGESTION_DROPPED.format(reason=reason))


@_quiet()
def _validated(live: LiveTask, cards: Cards, index: ContextIndex, max_cards: int, hour24: bool = False,
               suggested: tuple[str, str] = ("", "")) -> None:
    if not live.enabled:
        return
    keep_text = live.keep_text
    typed = sum(1 for sources in index.addresses.values() if SOURCE_OWNER in sources)
    supplied = sum(1 for sources in index.addresses.values() if sources - {SOURCE_OWNER})
    counts = index.counts()
    step = live.step(ASK_VALIDATE, "Checking the proposals", fields=[(
        "Checked against", f"{_count_words(counts['events'], 'event')}, {_count_words(counts['threads'], 'thread')}, "
                           f"{_count_words(supplied, 'address', 'addresses')} Jarvis supplied, {typed} you typed")])
    by_id = {card.id: card for card in cards.cards}
    new = 0
    for entry in cards.report:
        card = by_id.get(entry.card_id)
        if entry.outcome == LINE_CARD and card is not None:
            notes = [f"NEW recipient {address}: you didn't type it - tick it in Edit before Send"
                     for address in sorted(card.unverified)]
            new += len(card.unverified)
            notes += list(card.warnings)
            # Accepted, but look at it (amber): a recipient you didn't type, a soft problem.
            step.item(card_text(card, hour24), status=STATUS_WARN if notes else STATUS_OK, note="; ".join(notes))
        elif entry.outcome == LINE_REFUSED:
            step.item(card_text(card, hour24) if card is not None else line_text(entry.line, keep_text),
                      status=STATUS_BLOCKED, status_text="REFUSED", note=entry.reason)
        elif entry.outcome == LINE_REPEAT:
            step.item(line_text(entry.line, keep_text), status=STATUS_WARN, status_text="DROPPED",
                      note="a repeat of an earlier line")
        else:
            step.item(line_text(entry.line, keep_text), status=STATUS_WARN, status_text="DROPPED",
                      note=f"more than {max_cards} proposals")
    _suggestion_item(step, suggested, keep_text)
    made = sum(1 for card in cards.cards if not card.error)
    if not cards.report:
        step.done(STATUS_OK, summary="no proposals")
        return
    summary = f"{_count_words(made, 'card')}, {cards.refused} refused, {cards.dropped} dropped"
    if new:
        summary += f", {_count_words(new, 'recipient')} you didn't type"
    step.done(STATUS_WARN if cards.refused or cards.dropped or new else STATUS_OK, summary=summary)


# --------------------------------------------------------------------------
# The day plan's own LIVE steps (quiet, like every helper above) and its small helpers
# --------------------------------------------------------------------------

NO_MAIL_WORDS = "no - a day plan never reads mail"
NO_WEB_WORDS = "no - a day plan never uses the web"
MODE_RUN_WORDS = {MODE_PLAN: "day plan", MODE_PREP: "meeting brief", MODE_NUDGE: "follow-up draft",
                  MODE_MEETING: "meeting summary", MODE_DESKTOP: "desktop plan"}
FREE_TITLE = "Your free time"


def _day_bounds(day: date, zone: str) -> tuple[datetime, datetime]:
    """Midnight of ``day`` to the next midnight in the IANA ``zone`` as aware times (each with the
    offset the zone has that day; this PC's own rules when it can't be known)."""
    from datetime import time as clock_time, timezone as fixed

    def midnight(when: date) -> datetime:
        probe = datetime.combine(when, clock_time(12)).replace(tzinfo=fixed.utc)
        offset = zone_offset(zone, probe)
        wall = datetime.combine(when, clock_time())
        return wall.replace(tzinfo=fixed(offset)) if offset is not None else wall.astimezone()

    return midnight(day), midnight(day + timedelta(days=1))


def _served_cards(cards: Cards, served: Mapping[str, str], targets: Mapping[str, Any]) -> Cards:
    """Each valid block's notes become "For: <what its row names>" (Jarvis's own words from the
    row, R28) and a block for a to-do row carries that Todo card's id (``serves``). Notes and
    ``serves`` are not part of a Calendar card's id."""
    kept = []
    for card in cards.cards:
        row = served.get(card.id, "")
        if card.error or not row:
            kept.append(card)
            continue
        target = targets.get(row)
        serves = target.action_id if isinstance(target, modes.TodoFact) else ""
        kept.append(replace(card, notes=f"For: {dayplan.row_title(row, targets)}", serves=serves))
    return replace(cards, cards=tuple(kept))


def _when_words(window: Any, hour24: bool) -> str:
    return f"{window.label} {dayplan.hours_text(window.start, window.end, hour24)}"


@_quiet()
def _mode_given(live: LiveTask, window: Any, events: Mapping[str, Sequence[EventBrief]], inputs: ModeInputs,
                notes: Sequence[str], briefing: BriefingContext | None, hour24: bool) -> None:
    """The mode.request step (the UI's "You typed" / "You clicked") gets what the plan is for and
    what the run is given."""
    if not live.enabled:
        return
    step = live.find(MODE_REQUEST)
    created = step is NO_STEP
    if created:
        step = live.step(MODE_REQUEST, "Your request")
    count = sum(len(briefs) for briefs in events.values())
    proposed = sum(1 for todo in inputs.todos if todo.block)
    todos = _count_words(len(inputs.todos), "to-do") + (f" ({proposed} already has a block proposed)"
                                                         if proposed == 1 else
                                                         f" ({proposed} already have a block proposed)"
                                                         if proposed else "")
    given = [f"{window.label}'s calendar {_count_words(count, 'event')}, titles, times and busy / free only, "
             "no guests", todos, _count_words(len(inputs.deadlines), "deadline")]
    if inputs.followup_counts is not None:
        given.append("follow-up counts")
    if notes:
        given.append(_count_words(len(notes), "memory note"))
    given.append("briefing sections" if briefing is not None and briefing.sections else "no briefing")
    step.field("Plan for", _when_words(window, hour24) + f" ({window.zone})")
    step.field("Given", " \u00b7 ".join(given))
    if created:
        step.done(STATUS_OK)


@_quiet(NO_STEP)
def _plan_free_step(live: LiveTask, window: Any, slots: Sequence[Any], events: Mapping[str, Sequence[EventBrief]],
                    inputs: ModeInputs, typed: str, own: Collection[str], hour24: bool) -> LiveStep:
    """"Your free time": the window, the buffers, each free stretch, the events counted as free and
    what needs time."""
    if not live.enabled:
        return NO_STEP
    step = live.step(PLAN_FREE, FREE_TITLE, fields=[
        ("Window", _when_words(window, hour24) + f" ({window.zone})"),
        ("Buffer", f"{dayplan.BUFFER_MIN} min before and after meetings with guests"),
        ("Shortest stretch", f"{dayplan.MIN_SLOT_MIN} min")])
    for slot in slots:
        step.item(f"{dayplan.hours_text(slot.start, slot.end, hour24)} ({slot.minutes} min)", status=STATUS_OK)
    step.field("Free in all", f"{dayplan.free_minutes(slots)} min")
    free = [brief for briefs in events.values() for brief in briefs
            if brief.start is not None and not brief.all_day and not dayplan.busy_event(brief)]
    if free:
        step.field("Counted as free", f"{_count_words(len(free), 'event')} (declined, show as free or a working "
                                      "location)")
    open_todos = sum(1 for todo in inputs.todos if not todo.block)
    needs = [_count_words(open_todos, "to-do")] if open_todos else []
    if inputs.deadlines:
        needs.append(_count_words(len(inputs.deadlines), "deadline"))
    if typed:
        needs.append("your words")
    step.field("Needs time", ", ".join(needs) or "nothing")
    return step


@_quiet()
def _plan_free_no_run(step: LiveStep, kind: str, text: str) -> None:
    if step is NO_STEP:
        return
    step.field("No planner run", text)
    step.done(STATUS_WARN, status_text="NO RUN", summary=text)


@_quiet()
def _day_read_done(step: LiveStep, events: Mapping[str, Sequence[EventBrief]], accounts: Sequence[AccountInfo],
                   window: Any, now: datetime, hour24: bool, *, cancelled: bool) -> None:
    if step is NO_STEP:
        return
    listed = sorted(((brief, alias) for alias, briefs in events.items() for brief in briefs),
                    key=lambda pair: (_event_key(pair[0]), pair[1]))
    for brief, alias in listed:
        step.item(event_item(alias, brief, hour24))
    if not cancelled:
        step.picture(plan_picture(events, (), day=window.day, now=now, hour24=hour24, zone=window.zone))
    summary = f"{_count_words(len(listed), 'event')} read for {window.label}"
    if cancelled:
        step.done(STATUS_CANCELLED, summary=f"{summary}; Cancel clicked")
    elif any(account.calendar == "error" for account in accounts):
        step.done(STATUS_WARN, summary=f"{summary}; a calendar answered with an error")
    else:
        step.done(STATUS_OK, summary=summary)


@_quiet()
def _plan_validated(live: LiveTask, cards: Cards, served: Mapping[str, str], targets: Mapping[str, Any],
                    events: Mapping[str, Sequence[EventBrief]], window: Any, now: datetime, hour24: bool,
                    suggested: tuple[str, str] = ("", "")) -> None:
    """"Checking the blocks": each line as a card (and the row it serves) or why not, the suggestion
    kept or dropped, and the plan drawn on its day."""
    if not live.enabled:
        return
    keep_text = live.keep_text
    step = live.step(PLAN_VALIDATE, "Checking the blocks", fields=[
        ("Checked against", f"the free time of {window.label}, the events read and your list")])
    by_id = {card.id: card for card in cards.cards}
    for entry in cards.report:
        card = by_id.get(entry.card_id)
        if entry.outcome == LINE_CARD and card is not None:
            row = served.get(card.id, "")
            note = f"for {dayplan.row_title(row, targets)}" if row else ""
            step.item(card_text(card, hour24), status=STATUS_OK, note=note)
        elif entry.outcome == LINE_REFUSED:
            step.item(card_text(card, hour24) if card is not None else line_text(entry.line, keep_text),
                      status=STATUS_BLOCKED, status_text="REFUSED", note=entry.reason)
        elif entry.outcome == LINE_REPEAT:
            step.item(line_text(entry.line, keep_text), status=STATUS_WARN, status_text="DROPPED",
                      note="a repeat of an earlier line")
        elif entry.outcome == LINE_OVER_CAP:
            step.item(line_text(entry.line, keep_text), status=STATUS_WARN, status_text="DROPPED",
                      note="more blocks than a plan may give")
    _suggestion_item(step, suggested, keep_text)
    valid = [card for card in cards.cards if not card.error]
    step.picture(plan_picture(events, valid, day=window.day, now=now, hour24=hour24, zone=window.zone))
    summary = f"{_count_words(len(valid), 'block')}, {cards.refused} refused, {cards.dropped} dropped"
    step.done(STATUS_WARN if cards.refused or cards.dropped else STATUS_OK, summary=summary)


# --------------------------------------------------------------------------
# The meeting brief's own LIVE steps (quiet, like every helper above)
# --------------------------------------------------------------------------

WHY_NUDGE = "the follow-up thread you clicked (the only email read for this draft)"
NUDGE_NO_SEARCH_WORDS = "no - a follow-up draft reads only the thread you clicked"
NUDGE_NO_WEB_WORDS = "no - a follow-up draft never uses the web"
_GUARD_WORDS = {"": "unchanged since Jarvis listed it - drafting", "replied_since": "you replied since - no draft",
                "answered_since": "they answered since - no draft",
                "changed": "it changed since Jarvis listed it - no draft"}


def _reader_own(reader: Any) -> frozenset[str]:
    """The reader's own addresses (cached per account identity; none when it has none)."""
    getter = getattr(reader, "own_addresses", None)
    if not callable(getter):
        return frozenset()
    try:
        return frozenset(str(address).casefold() for address in getter() if address)
    except Exception:  # noqa: BLE001 - the bound addresses still count
        return frozenset()


@_quiet()
def _guard_field(step: LiveStep, guard: str) -> None:
    if step is NO_STEP:
        return
    step.field("Since Jarvis listed it", _GUARD_WORDS.get(guard, guard))


PREP_CHECKS_LATER = "checked right before any mail is read (a brief without mail needs neither)"
MAIL_NOT_READ = "I didn't read mail: "
PREP_NO_SEARCH_WORDS = "no - Jarvis searched himself, one guest's address at a time"
PREP_NO_WEB_WORDS = "no - a meeting brief never uses the web"
PREP_SEARCH_WHY = "Jarvis's own search: the latest thread with a guest of this meeting, last {days} days, read only"
PREP_PEOPLE_RULE = ("each guest's latest thread with you, last {days} days, at most 3 threads and one per guest; "
                    "never you, a group address or a guest who declined")
_LOOKUP_STATUS = {prep.FOUND: (STATUS_OK, ""), prep.SAME: (STATUS_OK, ""), prep.NONE: (STATUS_WARN, "NO MAIL"),
                  prep.REFUSED: (STATUS_BLOCKED, "NOT SEARCHED"), prep.GROUP: (STATUS_WARN, "SKIPPED"),
                  prep.DECLINED: (STATUS_WARN, "SKIPPED"), prep.NOT_SEARCHED: (STATUS_WARN, "SKIPPED"),
                  prep.PENDING: (STATUS_WARN, "SKIPPED")}


@_quiet(NO_STEP)
def _prep_meeting_step(live: LiveTask, request: ModeRequest) -> LiveStep:
    if not live.enabled:
        return NO_STEP
    if request.event is None:
        how = "the meeting your words name, read now (today and tomorrow, every calendar, read only)"
    else:
        how = "the meeting you clicked, read again now (its calendar, today and tomorrow, read only)"
    return live.step(PREP_MEETING, "The meeting, as read now", fields=[("Looking for", how)])


@_quiet()
def _prep_read_field(step: LiveStep, alias: str, status: str, briefs: Sequence[EventBrief]) -> None:
    if step is NO_STEP:
        return
    step.field(f"Read {alias}", _count_words(len(briefs), "event") if status == "yes"
               else _CALENDAR_WORDS.get(status, f"{status} - not read"))


@_quiet()
def _prep_meeting_stop(step: LiveStep, status: str, summary: str, *, status_text: str = "",
                       choices: Sequence[Any] = (), hour24: bool = False) -> None:
    if step is NO_STEP:
        return
    for choice in choices:
        step.item(f"{choice.title or '(No title)'} - {choice.day} "
                  f"{dayplan.hours_text(choice.start, choice.end, hour24)} ({choice.ref.alias})")
    step.done(status, status_text=status_text, summary=summary)


@_quiet()
def _prep_meeting_done(live: LiveTask, step: LiveStep, meeting: Any, moved: str,
                       events: Mapping[str, Sequence[EventBrief]], now: datetime, zone: str, hour24: bool) -> None:
    """The meeting as read now: its title, time, guests and their answers (on screen only), moved,
    and its day drawn as read."""
    if step is NO_STEP:
        return
    brief = meeting.brief
    when = f"{meeting.day} {dayplan.hours_text(meeting.start, meeting.end, hour24)} ({meeting.alias})"
    step.field("Meeting", brief.title or "(No title)")
    step.field("When", when)
    if moved:
        step.field("Moved", moved)
        step.note(f"The meeting {moved}; the brief is for its new time")
    organizer = "you organize it" if brief.organizer_self else (
        f"organized by {brief.organizer_name or brief.organizer_email or 'someone else'}")
    step.field("Organizer", organizer)
    step.field("Guests", _count_words(len(brief.guests), "guest") + (" besides you" if brief.guests else ""))
    for guest in brief.guests[:MAX_PEOPLE_SHOWN]:
        who = f"{guest.name} <{guest.email}>" if guest.name else guest.email
        step.item(f"{who} - {prep.RESPONSE_WORDS.get(guest.response or '', 'no answer yet')}")
    own_day = {meeting.alias: tuple(events.get(meeting.alias, ()))}   # the meeting's own calendar, as read
    step.picture(plan_picture(own_day, (), day=meeting.start.date(), now=now, hour24=hour24, zone=zone,
                              account=meeting.alias))
    step.done(STATUS_OK, summary=f"{brief.title or '(No title)'}, {when}")
    request = live.find(MODE_REQUEST)
    if request is not NO_STEP:
        request.field("Brief for", f"{brief.title or '(No title)'}, {when}")


@_quiet(NO_STEP)
def _prep_people_step(live: LiveTask, days: int) -> LiveStep:
    if not live.enabled:
        return NO_STEP
    return live.step(PREP_PEOPLE, "Who Jarvis looks up", fields=[("Rule", PREP_PEOPLE_RULE.format(days=days))])


@_quiet()
def _prep_checks(step: LiveStep, caps: CapCheck, usage: Any, reserve: int, ready: Readiness) -> None:
    """Readiness and the caps, as checked right before the first search (R12)."""
    if step is NO_STEP:
        return
    step.note("Checked right before any mail is read: Claude Code and the limits")
    _checks_caps(step, caps, usage)
    _checks_bundle(step, caps, usage, reserve)
    _checks_ready(step, ready)


@_quiet()
def _prep_people_done(step: LiveStep, lookups: Sequence[Any], days: int, alias: str) -> None:
    """Each guest: looked up (from Jarvis's own search) and what it found, or not and why."""
    if step is NO_STEP:
        return
    for lookup in lookups:
        if lookup.status == prep.OWN:
            continue
        guest = lookup.guest
        who = f"{guest.name} <{guest.address}>" if guest.name else guest.address
        status, status_text = _LOOKUP_STATUS.get(lookup.status, (STATUS_WARN, ""))
        if lookup.status == prep.FOUND and lookup.via == prep.VIA_INDEX:
            note = f"from Jarvis's header list: {lookup.alias}, newest thread (no search)"
        elif lookup.status == prep.FOUND:
            note = f"searched: found a thread on {lookup.alias}"
        elif lookup.status == prep.SAME:
            note = f"searched: {prep.lookup_words(lookup, days, alias)}"
        elif lookup.status == prep.NONE:
            note = f"searched: {prep.lookup_words(lookup, days, alias)}"
        else:
            note = prep.lookup_words(lookup, days, alias)
        step.item(f"{who} - {prep.response_words(guest)}", status=status, status_text=status_text, note=note)
    found = sum(1 for lookup in lookups if lookup.status == prep.FOUND)
    step.done(STATUS_OK if found else STATUS_WARN, summary=f"{_count_words(found, 'thread')} found")


@_quiet(NO_STEP)
def _prep_search_step(live: LiveTask, alias: str, query: str, lookup: Any, days: int, also: bool) -> LiveStep:
    if not live.enabled:
        return NO_STEP
    guest = lookup.guest
    title = "Jarvis's own search" + (f" (also {alias}: nothing on the meeting's account)" if also else "")
    step = live.step(MAIL_SEARCH, title, fields=[
        ("Account", alias), ("For", guest.display if guest.name else guest.address),
        Field("Gmail query (exact)", query, mono=True), ("Why", PREP_SEARCH_WHY.format(days=days)),
        ("Rules", "read only - never spam or trash - at most 1 thread")])
    step.note("Sent to Gmail (read only)")
    return step


@_quiet()
def _prep_search_done(step: LiveStep, ids: Sequence[str] | None, problem: str, *, days: int = 14) -> None:
    if step is NO_STEP:
        return
    if ids is None:
        step.done(STATUS_FAILED, summary=problem)
    elif ids:
        step.field("Found", "1 thread (the newest)")
        step.done(STATUS_OK, summary="found a thread")
    else:
        step.done(STATUS_WARN, status_text="NOTHING FOUND", summary=prep.NO_MAIL.format(days=days))


@_quiet()
def _prep_no_run_step(live: LiveTask, words: str) -> None:
    if not live.enabled:
        return
    live.step(PLANNER_RUN, f"Planner run ({MODE_RUN_WORDS[MODE_PREP]})", status=STATUS_WARN, status_text="NO RUN",
              summary=f"No planner run: {words}")


@_quiet()
def _prep_validated(live: LiveTask, answer: Any, brief: Any, cards: Cards, hour24: bool) -> None:
    """"Checking the brief": each thread note and open item kept or dropped (and why), the points,
    each reply line as a card or refused (a note leak included)."""
    if not live.enabled:
        return
    keep_text = live.keep_text
    read = {label for _who, label, _summary, _waiting in brief.threads}
    step = live.step(PREP_VALIDATE, "Checking the brief", fields=[
        ("Checked against", f"the {_count_words(len(read), 'thread')} Jarvis read for this brief")])
    kept_threads = sum(1 for _who, _label, summary, _waiting in brief.threads if summary)
    for thread_id, summary, waiting in answer.threads:
        shown = summary if keep_text else f"[{_count_words(len(summary), 'character')}]"
        step.item(f"Thread {thread_id}: {shown} (waiting on {waiting})")
    for text, thread_id in answer.open:
        shown = text if keep_text else f"[{_count_words(len(text), 'character')}]"
        step.item(f"Open: {shown}" + (f" (thread {thread_id})" if thread_id else ""))
    step.field("Kept", f"{_count_words(kept_threads, 'thread note')}, {_count_words(len(brief.open), 'open item')}, "
                       f"{_count_words(len(brief.points), 'point')}")
    if brief.dropped:
        step.field("Left out", f"{_count_words(brief.dropped, 'item')} (a thread Jarvis didn't read, or a repeat)")
    by_id = {card.id: card for card in cards.cards}
    for entry in cards.report:
        card = by_id.get(entry.card_id)
        if entry.outcome == LINE_CARD and card is not None:
            notes = [f"NEW recipient {address}: you didn't type it - tick it in Edit before Send"
                     for address in sorted(card.unverified)]
            step.item(card_text(card, hour24), status=STATUS_WARN if notes else STATUS_OK, note="; ".join(notes))
        elif entry.outcome == LINE_REFUSED:
            step.item(card_text(card, hour24) if card is not None else line_text(entry.line, keep_text),
                      status=STATUS_BLOCKED, status_text="REFUSED", note=entry.reason)
        else:
            step.item(line_text(entry.line, keep_text), status=STATUS_WARN, status_text="DROPPED",
                      note="a repeat or more replies than a brief may give")
    replies = sum(1 for card in cards.cards if not card.error)
    summary = (f"brief: {_count_words(kept_threads, 'thread note')}, {_count_words(len(brief.open), 'open item')}, "
               f"{_count_words(len(brief.points), 'point')}; {_count_words(replies, 'reply', 'replies')}")
    problems = brief.dropped or cards.refused or cards.dropped
    step.done(STATUS_WARN if problems else STATUS_OK, summary=summary)


@_quiet()
def _meeting_input(step: LiveStep, turns: Sequence[Any], labels: Mapping[str, Any], prompt: str,
                   job: Any = None) -> None:
    """The meeting summary's input: the transcript's size (its text only in the run's own stdin block, marked as
    written by other people), the guests by name (never an address; none for a meeting not linked to a calendar
    meeting) and that nothing else goes."""
    if step is NO_STEP:
        return
    if getattr(job, "in_person", False):
        whose = "in person: the speakers are not identified"
    else:
        whose = f"{sum(1 for turn in turns if turn.speaker == meeting_summary.SPEAKER_YOU)} yours"
    step.field("Transcript", f"{_count_words(len(turns), 'turn')} ({whose}), marked as written by other "
                             "people - data, never instructions")
    if getattr(job, "linked", True):
        step.field("Guests given", f"{_count_words(len(labels), 'name')} from the invite (no addresses)")
    else:
        step.field("Guests given", "none (not linked to a calendar meeting)")
    step.field("Not given", "mail, your notes, other meetings, the audio")
    step.field("Input", f"{len(prompt):,} characters")
    step.done(STATUS_OK, summary=f"{_count_words(len(turns), 'turn')}, {_count_words(len(labels), 'guest')}")


@_quiet()
def _meeting_validated(live: LiveTask, answer: Any, summary: Any, cards: Cards, notes_dropped: int,
                       hour24: bool, job: Any = None) -> None:
    """"Checking the summary": the counts kept and left out, each follow-up as a card or refused (a meeting not
    linked to a calendar meeting: its follow-ups as plain text, no cards)."""
    if not live.enabled:
        return
    linked = getattr(job, "linked", True)
    against = ("the calendar invite's guests (a follow-up only to one of them)" if linked else
               "no calendar meeting: no guests, the follow-ups as plain text (no drafts)")
    step = live.step(MEETING_VALIDATE, "Checking the summary", fields=[("Checked against", against)])
    counts = summary.counts()
    step.field("Kept", f"{_count_words(counts['decisions'], 'decision')}, "
                       f"{_count_words(counts['actions'], 'action')} ({counts['yours']} yours), "
                       f"{_count_words(counts['questions'], 'open question')}")
    if not linked:
        step.field("Plain follow-ups", _count_words(counts.get("plain_follow_ups", 0), "follow-up"))
    if summary.dropped:
        words = meeting_summary.left_out_words(job) if job is not None else meeting_summary.LEFT_OUT_LINKED
        step.field("Left out", f"{_count_words(summary.dropped, 'item')} ({words})")
    if notes_dropped:
        step.field("Notes left out", _count_words(notes_dropped, "suggestion"))
    by_id = {card.id: card for card in cards.cards}
    for entry in cards.report:
        card = by_id.get(entry.card_id)
        if entry.outcome == LINE_CARD and card is not None:
            notes = [f"NEW recipient {address}: tick it on the card before Send" for address in sorted(card.unverified)]
            step.item(card_text(card, hour24), status=STATUS_WARN if notes else STATUS_OK, note="; ".join(notes))
        elif entry.outcome == LINE_REFUSED:
            step.item(card_text(card, hour24) if card is not None else "a follow-up draft",
                      status=STATUS_BLOCKED, status_text="REFUSED", note=entry.reason)
    drafts = sum(1 for card in cards.cards if not card.error)
    problems = summary.dropped or cards.refused or notes_dropped
    step.done(STATUS_WARN if problems else STATUS_OK,
              summary=f"summary: {_count_words(counts['actions'], 'action')}; "
                      f"{_count_words(drafts, 'follow-up draft')}")


def _mode_target(request: ModeRequest) -> Any:
    """A meeting summary's outcome always names its meeting (its id), also when the run stopped before it read
    the transcript (busy, an unexpected error): the app then ends that summary there (Retry); a desktop request's
    names its run id. None otherwise."""
    job = request.payload
    if request.mode == MODE_DESKTOP:
        return job.run_id if isinstance(job, desktop_mode.DesktopJob) else None
    if request.mode != MODE_MEETING:
        return None
    return job.meeting_id if isinstance(job, meeting_summary.MeetingJob) else None


@_quiet()
def _desktop_given(live: LiveTask, job: Any, prompt: Any) -> None:
    """What the desktop planner is given (SPEC 10.1): where Jarvis looked, how many places, files, apps and sites
    it is shown (names only - never a path, a file's contents or a typed value), what was left out to fit, and how
    many names read like instructions (treated as data). Counts only: the names are in the run's stdin block."""
    if not live.enabled:
        return
    shown = prompt.shown
    context = job.context
    step = live.step(DESKTOP_FIND, "What the planner is given")
    if context.looked_in or context.found:
        step.field("Looked in", f"{_count_words(context.looked_in, 'place')}: "
                                f"{_count_words(context.found, 'file')}")
    else:
        step.field("Looked in", "nothing yet - Jarvis's own search of this PC comes in a later part")
    step.field("Given", f"{_count_words(shown['places'], 'place')}, {_count_words(shown['files'], 'file')}, "
                        f"{_count_words(shown['apps'], 'app')}, {_count_words(shown['sites'], 'site')} - names only, "
                        "never paths or contents")
    left = [part for part in (_count_words(shown["files_left_out"], "file") if shown["files_left_out"] else "",
                              _count_words(shown["apps_left_out"], "app") if shown["apps_left_out"] else "") if part]
    if left:
        step.field("Left out to fit", ", ".join(left))
    suspects = len(prompt.refs.suspects())
    if suspects:
        step.field("Names that read like instructions",
                   f"{suspects} - treated as data; used only when your own words name them")
    step.done(STATUS_WARN if suspects or left else STATUS_OK,
              summary=f"{_count_words(len(prompt.refs), 'item')} by id, names only")


@_quiet()
def _desktop_validated(live: LiveTask, plan: Any, words: str) -> None:
    """"Checking the plan": each step with its skill and its tier or refusal code (never a name, a path or Claude's
    text), then what Jarvis makes of it (desktop.plan): his own words, nothing opened yet."""
    if not live.enabled:
        return
    step = live.step(DESKTOP_VALIDATE, "Checking the plan",
                     fields=[("Checked against", "the ids given in this run, your typed words and Jarvis's safety "
                                                 "rules")])
    for checked in plan.steps:
        if checked.ok:
            step.item(desktop_validate.describe(checked), status=STATUS_OK)
        else:
            step.item(desktop_validate.describe(checked), status=STATUS_BLOCKED, status_text="REFUSED",
                      note=desktop_validate.reason_words(checked.code))
    if plan.question:
        step.field("Question back", "yes - see the planner's reply above")
    refused = plan.refused()
    summary = f"{_count_words(len(plan.accepted()), 'step')} OK, {len(refused)} refused"
    if refused:
        summary += f" - {desktop_validate.reason_words(refused[0].code)}"
    step.done(STATUS_WARN if refused or not plan.accepted() else STATUS_OK, summary=summary)
    result = live.step(DESKTOP_PLAN, "What Jarvis does with it", fields=[("Jarvis says", words)])
    result.done(STATUS_OK, summary="nothing was opened yet")


def mode_todos(pending: Sequence[ProposedAction], planned: Mapping[str, date] | None = None) -> tuple[Any, ...]:
    """The TodoFacts of the pending cards (modes.todo_facts) - the command line's and the app's."""
    return modes.todo_facts([card for card in pending if card.kind == TODO], planned=planned)


# --------------------------------------------------------------------------
# The briefing as Ask sees it
# --------------------------------------------------------------------------

def deadline_lines(deadlines: Sequence[Any]) -> list[str]:
    """agenda.Deadline items as "title | YYYY-MM-DD[ HH:MM]" lines."""
    lines = []
    for item in deadlines:
        due = getattr(item, "due", None)
        if isinstance(due, datetime):
            when = due.strftime("%Y-%m-%d %H:%M")
        elif isinstance(due, date):
            when = due.isoformat()
        else:
            continue
        lines.append(f"{getattr(item, 'title', '')} | {when}")
    return lines


def briefing_context(briefing: Any, pending: Sequence[ProposedAction], deadlines: Sequence[Any] = (), *,
                     config: Config, now: datetime) -> BriefingContext:
    """BriefingContext from what the app holds: ``briefing`` the page (models.Briefing) without its
    Proposed actions and Deadlines sections (AppController._briefing), ``pending`` the briefing's
    cards still waiting for a decision, ``deadlines`` agenda.Deadline items. The Ignore sections are
    left out."""
    from ..text_prep import build_script

    sections: Sequence[Any] = ()
    if briefing is not None:
        script = build_script(briefing, now=now, include_note=False, ignore_names=config.sections.ignore,
                              announce=config.sections.announce, actions=())
        sections = script.sections
    return briefing_from_script(sections, pending, deadline_lines(deadlines))


def briefing_from_page(page: Any, *, config: Config, now: datetime, decided: Callable[[str], bool] = lambda _id: False
                       ) -> BriefingContext:
    """BriefingContext from a freshly fetched page (the command line): its Deadlines and Proposed
    actions sections are taken out as the app does; ``decided`` tells which card ids are decided."""
    from ..actions import extract_actions
    from ..agenda import extract_deadlines

    headings = config.actions.headings
    deadlines, lines = extract_deadlines(page.lines, stop_names=headings)
    actions, lines = extract_actions(lines, headings, link_hosts=config.actions.link_hosts)
    pending = [action for action in actions if action.decidable and not decided(action.id)]
    return briefing_context(replace(page, lines=tuple(lines)), pending, deadlines, config=config, now=now)
