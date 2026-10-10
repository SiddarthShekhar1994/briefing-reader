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
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from ..actions import ProposedAction, link_host
from ..config import Config
from ..gcal import CalendarError, CalendarNotSignedIn, EventBrief, local_timezone
from ..gmail import GmailError
from ..google_auth import PROBLEM_EXPIRED, PROBLEM_SCOPE, PROBLEM_SETUP, PROBLEM_SIGNED_OUT, logged_alias
from . import cli
from ..live import (
    ASK_CHECKS,
    ASK_VALIDATE,
    CALENDAR_READ,
    MAIL_SEARCH,
    MAIL_THREAD,
    NO_STEP,
    NO_TASK,
    PLANNER_RUN,
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
from ..pictures import CalendarRead, email_picture, week_picture
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
from .stream import CANCELLED, OVERAGE, AskFailure, InitSummary, Plan, RunResult, RunStats, run_planner
from .usage import KIND_RESEARCH, USAGE_FILE, CapCheck, UsageLog
from .validate import LINE_CARD, LINE_REFUSED, LINE_REPEAT, Cards, to_cards

logger = logging.getLogger(__name__)

STAGE_CONTEXT = "context"          # reading the calendar (and named briefing threads)
STAGE_PLANNING = "planning"        # the first planner run
STAGE_MAIL = "mail"                # running the search the planner asked for
STAGE_PLANNING_AGAIN = "planning_again"   # the second run, with the mail
STAGE_RESEARCH = "research"        # the web research run

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
        return text


@dataclass(frozen=True)
class DryRun:
    prompt: str                       # exactly what would go to stdin, email text as counts
    argv: tuple[str, ...]             # the command line ("" when Claude Code was not found)
    readiness: Readiness
    counts: Mapping[str, int]
    mail: MailReport
    research: bool = False            # a "web:" request's research run (its stdin and command line)


class AskPlanner:
    # The app's snapshot_engine.PageSnapshotter ([live] page_snapshots; None: no page pictures). It is
    # only handed the pages a research run read, for the LIVE view; nothing goes back to a run.
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
                                       max_research_per_day=research_config.max_per_day)
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
             allow_remember: bool = False) -> RunResult:
        step = _run_step(live, self.config.ask.model, prompt, number, search_words, web_words, allow_remember)
        ready, refused = self.engine.preflight()   # a run refused before it starts is not counted
        if refused is not None:
            step.rename_block(STDIN_PENDING, STDIN_NOT_SENT)
            step.done(STATUS_BLOCKED, status_text="SKIPPED", summary=f"Not started: {refused.message}")
            return RunResult(failure=refused, started=False)
        _run_ready(step, self.engine, ready, allow_search, allow_web, allow_remember)
        run = self.usage.start()
        result: RunResult | None = None
        try:
            extra: dict[str, Any] = {}
            if step is not NO_STEP:   # only with the LIVE view: an engine without these keywords keeps working
                extra = {"progress": _run_progress(step), "capture": True}
            if allow_web:             # only when allowed: an engine without the keyword keeps working
                extra["allow_web"] = True
            if allow_remember:        # the same
                extra["allow_remember"] = True
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

    def _refusal(self, live: LiveTask = NO_TASK) -> AskOutcome | None:
        step = live.step(ASK_CHECKS, CHECKS_TITLE)
        if not self.config.ask.enabled:
            step.field("Ask", "off")
            step.done(STATUS_BLOCKED, summary=DISABLED_MESSAGE)
            return AskOutcome(False, DISABLED, DISABLED_MESSAGE)
        step.field("Ask", "on")
        caps = self.usage.check()
        _checks_caps(step, caps, self.usage)
        if self._research_config().enabled:
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

    # ---- web research -------------------------------------------------------------------------

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
        result = self._research_run(prompt, question, cancel, live)
        del prompt
        if result.started:
            runs, stats = runs + 1, stats + (result.stats,)
        init = init or result.init
        answer = result.answer
        if result.failure is not None or not isinstance(answer, ResearchAnswer) or result.web is None:
            failure = result.failure
            return AskOutcome(False, failure.kind if failure else "error",
                              failure.message if failure else "The web research's answer could not be read; nothing "
                                                              "was proposed",
                              runs=runs, stats=stats, init=init, mail=mail)
        cards, found = validate_research(answer, result.web, now=now, max_sources=self.research.limits.max_sources,
                                         max_cards=self.config.ask.max_cards, page_ids=page_ids,
                                         question=sent_question, hint=sent_hint, forced=forced)
        research_live.validated(live, cards, found, answer, result.web, self._hour24())
        logger.info("Research: %d source(s) shown, %d left out, %d suggestion card(s), %d dropped",
                    len(found.sources), len(found.left_out), len(found.suggested), len(found.dropped))
        message = "; ".join([*notes, *research_notes(found)])
        return AskOutcome(True, "ok", message, say=found.answer, cards=cards,
                          refused=sum(1 for card in cards if card.error), runs=runs, init=init, stats=stats,
                          mail=mail, research=found)

    def _research_run(self, prompt: str, question: str, cancel: threading.Event | None,
                      live: LiveTask = NO_TASK) -> RunResult:
        """The research run with its LIVE steps; counted (kind "research") only when it starts.
        With the LIVE view and ``page_snapshots`` (the app's snapshot_engine.PageSnapshotter), the
        pages the run read are pictured on their web.fetch steps; the run is told nothing of it."""
        research = self._research_config()
        limits = self.research.limits
        step = research_live.run_step(live, model=research.model, prompt=prompt, limits=limits,
                                      timeout_s=research.timeout_seconds)
        web_steps = research_live.WebSteps(live, limits, snapshots=self.page_snapshots) if step is not NO_STEP \
            else None
        try:
            ready, refused = self.research.preflight()   # a run refused before it starts is not counted
            if refused is not None:
                research_live.run_skipped(step, refused.message)
                return RunResult(failure=refused, started=False)
            research_live.run_ready(step, self.research, ready)
            return self._research_started(prompt, question, cancel, live, step, web_steps)
        finally:
            if web_steps is not None:
                web_steps.finished()   # no more page pictures for this run (also when it never started)

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
              web_words: str = "", allow_remember: bool = False) -> LiveStep:
    if not live.enabled:
        return NO_STEP
    title = "Planner run 1" if number == 1 else f"Planner run {number} (with your mail)"
    fields: list[Field | tuple[str, str]] = [Field("Model", model, mono=True),
                                             ("May ask to search mail", search_words or "yes")]
    if web_words:
        fields.append(("May ask for web research", web_words))
    if allow_remember:
        fields.append(("May suggest a note", REMEMBER_WORDS))
    fields.append(("Input", f"{len(prompt):,} characters"))
    step = live.step(PLANNER_RUN, title, fields=fields)
    step.block(STDIN_PENDING, prompt, untrusted=True)
    return step


@_quiet()
def _run_ready(step: LiveStep, engine: Any, ready: Readiness, allow_search: bool, allow_web: bool = False,
               allow_remember: bool = False) -> None:
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
    failure = result.failure
    if failure is None and plan is not None and plan.web is not None and plan.search is None:
        why = plan.web.why if 0 < len(plan.web.why) <= HANDOVER_WHY_CHARS else ""
        step.done(STATUS_OK, summary=HANDOVER_WORDS + (f" - {why}" if why else ""))
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
