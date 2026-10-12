"""Ask Jarvis from the command line (no window, no Qt):

    --ask-check                      is Ask ready? Claude Code found, its version and flags, a
                                     claude.ai plan sign-in, the work folder, which credential
                                     variables are kept from it, mail reading, usage and the
                                     extra-usage pause. Runs only
                                     claude --version, --help and auth status: no model request.
    --ask-text "..." --ask-dry-run   the exact text Jarvis would send to Claude Code first, with
                                     email text shown as counts, and the command line; no model
                                     request and nothing counted against the caps
    --ask-text "..."                 one real Ask (one or two planner runs on your Claude plan):
                                     prints what the planner said, the cards and an init summary
    --ask-text "web: ..."            web research only (one run; nothing is read from your accounts):
                                     the answer, its sources, what was left out and the cards; with
                                     --ask-dry-run the exact text the research would get and its
                                     command line
    --plan-dry-run ["plan ..."]      the productivity bundle's day plan: what its run would get (the
                                     day read fresh, read only; titles, briefing lines and notes as
                                     their sizes), its free stretches and its command line; no run,
                                     nothing counted. Default "plan my day"; "plan tomorrow", "plan my
                                     afternoon, gym" ... as typed in the app

With the productivity bundle's memory on ([productivity] enabled and memory), --ask-text gives the
planner the owner's notes like the app does, from memory.json opened read-only (the dry run shows
them as sizes); "remember that ..." (and "forget that ...", "show my memory") is the app's: the
command line prints so and exits 2, nothing saved or changed.

These print to the console only. The log gets what the app logs anyway (kinds, counts, durations):
never the request, the context, the planner's text or anything from the web.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from ..actions import card_view
from ..config import Config
from . import cli
from .context import BriefingContext
from .planner import AskPlanner, ClaudeEngine, GoogleSources, Readiness, ResearchEngine, briefing_from_page
from .research import forced_question
from .research_validate import steps_words
from .routes import ROUTE_MEMORY_FORGET, ROUTE_MEMORY_SAVE, ROUTE_MEMORY_SHOW, ROUTE_PLAN, RouteFlags, route
from .usage import KIND_BUNDLE, KIND_NUDGE, KIND_PLAN_DAY, KIND_PREP, KIND_RESEARCH, USAGE_FILE, UsageLog

logger = logging.getLogger(__name__)

ACTIONS_FILE = "actions.json"   # as ui.ACTIONS_FILE (ui imports Qt; this module must not)
STAGE_WORDS = {"context": "Reading your calendar...", "planning": "Planning (uses your Claude plan)...",
               "mail": "Reading your mail...", "planning_again": "Planning with your mail (uses your Claude plan)...",
               "research": "Searching the web (uses your Claude plan)...",
               "day": "Reading the day being planned...", "prep_mail": "Reading the meeting's guests' mail..."}
PLAN_DRY_RUN_DEFAULT = "plan my day"
NOT_A_PLAN = 'Not a day plan request: try "plan my day", "plan tomorrow" or "plan my afternoon, gym and reading".'
MEMORY_ROUTE_WORDS = {ROUTE_MEMORY_SAVE: "Notes are kept in the app (MEMORY tab); nothing was saved.",
                      ROUTE_MEMORY_FORGET: "Notes are kept in the app (MEMORY tab); nothing was changed.",
                      ROUTE_MEMORY_SHOW: "Notes are kept in the app (MEMORY tab)."}
MEMORY_ROUTE_EXIT = 2


def memory_on(config: Config) -> bool:
    """The productivity bundle's memory is on ([productivity] enabled and memory, with [ask] on)."""
    productivity = getattr(config, "productivity", None)
    return bool(config.ask.enabled and getattr(productivity, "enabled", False)
                and getattr(productivity, "memory", False))


def cli_memory(config: Config) -> Callable[[], tuple[str, ...]] | None:
    """The owner's notes for a command-line Ask: memory.json opened read-only (never written), or
    None while memory is off."""
    if not memory_on(config):
        return None
    from ..memory import MEMORY_FILE, MemoryStore

    store = MemoryStore(config.data_dir / MEMORY_FILE, read_only=True,
                        max_entries=config.productivity.max_memory)
    return lambda: store.prompt_notes()[0]


def _with_memory(planner: AskPlanner, config: Config) -> AskPlanner:
    planner.memory = cli_memory(config)   # never memory_suggestions: nothing could be saved here
    return planner


def google_sources(config: Config) -> GoogleSources:
    """The app's Google objects for Ask (read-only use; nothing here signs in)."""
    from ..executor import build_accounts, build_calendars, build_readers, build_senders

    accounts = build_accounts(config)
    calendars = build_calendars(config, accounts) if config.calendar.enabled else {}
    return GoogleSources(config, calendars, build_senders(config, accounts), build_readers(config, accounts))


def cli_briefing(config: Config, from_file: str | None, *, now: datetime, out: TextIO) -> BriefingContext | None:
    """Today's briefing for a command-line Ask: the saved fixture (--from-file) or the Notion page
    (a read), its decided cards left out; None (said so) when it can't be read."""
    from ..actions import ActionStore
    from ..notion_client import FixtureSession, NotionClient, NotionError, fetch_briefing

    try:
        if from_file:
            session = FixtureSession(Path(from_file))
            client = NotionClient("", session=session, notion_version=config.notion_version)
            page_id = session.page_id or config.page_id
        else:
            if not config.notion_token or not config.page_id:
                out.write("Briefing: not loaded (NOTION_TOKEN / BRIEFING_PAGE_ID are not set)\n")
                return None
            client = NotionClient(config.notion_token, notion_version=config.notion_version)
            page_id = config.page_id
        page = fetch_briefing(client, page_id, now=now)
    except (NotionError, OSError, ValueError) as exc:
        out.write(f"Briefing: not loaded ({type(exc).__name__})\n")
        return None
    store = ActionStore(config.data_dir / ACTIONS_FILE)
    return briefing_from_page(page, config=config, now=now, decided=store.is_decided)


def _readiness_lines(ready: Readiness, config: Config, environ: Mapping[str, str]) -> list[str]:
    enabled = "yes" if config.ask.enabled else "no (set enabled = true under [ask] in config.toml)"
    lines = [f"  [ask] enabled: {enabled}"]
    if ready.exe is None:
        lines.append(f"  Claude Code: not found - {ready.message}")
        return lines
    lines.append(f"  Claude Code: {ready.version or 'unknown version'} ({ready.exe.source})")
    lines.append(f"    {ready.exe.path}")
    if ready.problem == "workdir":
        lines.append(f"  Work folder: {ready.message}")
        return lines
    if ready.problem == "cli_unsupported":
        lines.append(f"  Flags: {ready.message}")
        return lines
    lines.append("  Flags: every flag Ask needs is there; --safe-mode/--restricted: "
                 + ("used" if ready.hardened else "available, not used ([ask] hardened_flags)" if ready.hardening
                    else "not available"))
    auth = ready.auth
    if auth is not None:
        state = "OK" if auth.ok else auth.message
        lines.append(f"  Sign-in: {auth.method or 'none'} ({auth.provider or 'unknown provider'}) - {state}")
    elif ready.message:
        lines.append(f"  Sign-in: {ready.message}")
    removed, money = cli.removed_env(environ)
    kept = len([name for name in environ if name.upper() in cli.CHILD_ENV_ALLOWED])
    lines.append(f"  Child environment: {kept} variable(s) kept (Windows basics, proxies), {len(removed)} removed"
                 + (f", including {', '.join(money)}" if money else "") + "; values are never read or shown")
    return lines


def ask_check(config: Config, out: TextIO, *, environ: Mapping[str, str] | None = None,
              runner: cli.Runner | None = None, sources: GoogleSources | None = None,
              usage: UsageLog | None = None) -> int:
    """Print whether Ask is ready (no model request). 0 when it is ready to run, 1 otherwise."""
    environ = os.environ if environ is None else environ
    engine = ClaudeEngine(config.ask, config.data_dir, runner=runner, environ=environ)
    ready = engine.readiness(refresh=True)
    out.write("Ask Jarvis check (no Claude request was made)\n")
    for line in _readiness_lines(ready, config, environ):
        out.write(line + "\n")
    sources = sources if sources is not None else google_sources(config)
    try:
        accounts = sources.accounts()
    except Exception as exc:  # noqa: BLE001 - only a report
        out.write(f"  Accounts: could not be read ({type(exc).__name__})\n")
        accounts = []
    for account in accounts:
        out.write(f"  Account {account.alias}: calendar {account.calendar}, sending "
                  f"{'set up' if account.send_mail else 'not set up'}, mail reading {account.read_mail}\n")
    research = config.research
    productivity = config.productivity
    usage = usage or UsageLog(config.data_dir / USAGE_FILE, max_per_hour=config.ask.max_per_hour,
                              max_per_day=config.ask.max_per_day, max_research_per_hour=research.max_per_hour,
                              max_research_per_day=research.max_per_day,
                              max_bundle_per_day=productivity.max_runs_per_day)
    hour, day = usage.counts()
    out.write(f"  Usage: {hour} of {config.ask.max_per_hour} planner runs this hour, {day} of "
              f"{config.ask.max_per_day} today\n")
    kinds = [("Ask", "plan"), ("web research", KIND_RESEARCH), ("day plans", KIND_PLAN_DAY),
             ("meeting briefs", KIND_PREP), ("drafts", KIND_NUDGE)]
    per_kind = [(name, usage.counts(kind=kind)[1]) for name, kind in kinds]
    per_kind[0] = ("Ask", per_kind[0][1] - usage.counts(kind=KIND_BUNDLE)[1])   # typed Asks: without the bundle's
    out.write("  Runs today by kind: " + ", ".join(f"{name} {count}" for name, count in per_kind) + "\n")
    bundle_day = usage.counts(kind=KIND_BUNDLE)[1]
    kept = usage.reserve_kept(productivity.reserve_ask_runs)
    out.write(f"  Plans, briefs and drafts today: {bundle_day} of {productivity.max_runs_per_day} "
              f"({kept} Ask run{'' if kept == 1 else 's'} an hour kept for your requests)\n")
    caps = usage.check()
    if caps.held:
        out.write(f"  Paused: {caps.message}\n")
    engine_research = ResearchEngine(research, engine)
    research_ready = engine_research.readiness() if ready.ok else ready
    out.write(f"  Web research: {_research_line(config, ready, engine_research)}\n")
    research_hour, research_day = usage.counts(kind=KIND_RESEARCH)
    out.write(f"  Research usage: {research_hour} of {research.max_per_hour} research runs this hour, {research_day} "
              f"of {research.max_per_day} today\n")
    ok = ready.ok and config.ask.enabled and not caps.held
    out.write(f"Ready: {'yes' if ok else 'no'}\n")
    research_ok = ok and research.enabled and research_ready.ok and usage.check(research=1).allowed
    out.write(f"Web research ready: {'yes' if research_ok else 'no'}\n")   # never changes Ready: or the exit code
    return 0 if ok else 1


def _research_line(config: Config, ready: Readiness, research: ResearchEngine) -> str:
    """"on ([research] enabled) - --allowedTools: available - work folder research\\ empty"."""
    state = "on ([research] enabled)" if config.research.enabled else "off ([research] enabled = false)"
    if ready.exe is None or not ready.version:
        flag = "unknown (Claude Code not checked)"
    else:
        flag = "missing (update Claude Code)" if ready.research_missing else "available"
    problem = cli.prepare_work_folder(research.work_folder, what="Web research", folder=cli.RESEARCH_WORK_FOLDER)
    folder = problem or f"work folder {cli.RESEARCH_WORK_FOLDER}\\ empty"
    return f"{state} - --allowedTools: {flag} - {folder}"


def ask_dry_run(text: str, config: Config, out: TextIO, *, briefing: BriefingContext | None,
                environ: Mapping[str, str] | None = None, runner: cli.Runner | None = None,
                sources: GoogleSources | None = None) -> int:
    """Print what the first planner run would get (email text as counts) and its command line; for
    "web: ...", what the web research run would get and its command line (nothing read from the
    accounts)."""
    environ = os.environ if environ is None else environ
    engine = ClaudeEngine(config.ask, config.data_dir, runner=runner, environ=environ)
    planner = AskPlanner(config, engine, sources if sources is not None else google_sources(config))
    question = forced_question(text)
    if question is not None:
        return _research_dry_run(planner, question, out)   # web research never gets the notes
    _with_memory(planner, config)
    dry = planner.dry_run(text, briefing=briefing)
    out.write("Ask Jarvis dry run (no Claude request was made; nothing was counted)\n")
    out.write("---- stdin (email text shown as counts) ----\n")
    out.write(dry.prompt)
    out.write("---- end of stdin ----\n")
    if dry.argv:
        out.write("Command line (the prompt goes to stdin, never here):\n  " + subprocess.list2cmdline(dry.argv) + "\n")
    else:
        out.write(f"Command line: not built ({dry.readiness.message})\n")
    out.write("Context: " + ", ".join(f"{name} {count}" for name, count in dry.counts.items())
              + f", mail {dry.mail.threads} thread(s), {dry.mail.messages} message(s), {dry.mail.chars} characters\n")
    if not dry.readiness.ok:
        out.write(f"Not ready to run: {dry.readiness.message}\n")
    return 0


def plan_dry_run(text: str, config: Config, out: TextIO, *, briefing: BriefingContext | None,
                 environ: Mapping[str, str] | None = None, runner: cli.Runner | None = None,
                 sources: GoogleSources | None = None, now: Callable[[], datetime] | None = None) -> int:
    """Print what a day plan's run would get (the day read fresh, read only; titles, briefing lines and
    notes as their sizes), its free stretches and its command line: no run, nothing counted. 2 when
    ``text`` is no plan request."""
    from .modes import MODE_PLAN, ORIGIN_TYPED, ModeInputs, ModeRequest, todo_facts

    found = route(text or PLAN_DRY_RUN_DEFAULT, RouteFlags(plan=True))
    if found.kind != ROUTE_PLAN:
        out.write(NOT_A_PLAN + "\n")
        return 2
    environ = os.environ if environ is None else environ
    engine = ClaudeEngine(config.ask, config.data_dir, runner=runner, environ=environ)
    planner = AskPlanner(config, engine, sources if sources is not None else google_sources(config), clock=now)
    _with_memory(planner, config)
    pending = briefing.pending if briefing is not None else ()
    inputs = ModeInputs(todos=todo_facts(pending), deadlines=tuple(briefing.deadlines if briefing else ()),
                        evening_from_hour=int(config.agenda.evening_from_hour))
    request = ModeRequest(MODE_PLAN, ORIGIN_TYPED, typed=found.payload, part=found.part, typed_route=True)
    dry = planner.plan_dry_run(request, inputs=inputs, briefing=briefing)
    out.write("Day plan dry run (no Claude request was made; nothing was counted; titles and notes as sizes)\n")
    if not (config.ask.enabled and config.productivity.enabled):
        out.write("Note: the day plan is off in the app ([ask] enabled and [productivity] enabled, or "
                  "JARVIS_PRODUCTIVITY_ENABLED=true in .env)\n")
    if dry.note:
        out.write(f"No planner run: {dry.note}\n")
    if dry.prompt:
        out.write("---- stdin ----\n")
        out.write(dry.prompt)
        out.write("---- end of stdin ----\n")
    if dry.argv:
        out.write("Command line (the prompt goes to stdin, never here):\n  " + subprocess.list2cmdline(dry.argv) + "\n")
    else:
        out.write(f"Command line: not built ({dry.readiness.message})\n")
    if dry.counts:
        out.write("Context: " + ", ".join(f"{name} {count}" for name, count in dry.counts.items()) + "\n")
    if not dry.readiness.ok:
        out.write(f"Not ready to run: {dry.readiness.message}\n")
    return 0


def _research_dry_run(planner: AskPlanner, question: str, out: TextIO) -> int:
    from .planner import EMPTY_WEB_MESSAGE

    out.write("Ask Jarvis dry run (no Claude request was made; nothing was counted)\n")
    if not question:
        out.write(EMPTY_WEB_MESSAGE + "\n")
        return 1
    dry = planner.research_dry_run(question)
    out.write("Web research: nothing is read from your accounts for this request\n")
    out.write("---- stdin ----\n")
    out.write(dry.prompt)
    out.write("---- end of stdin ----\n")
    if dry.argv:
        out.write("Command line (the question goes to stdin, never here):\n  " + subprocess.list2cmdline(dry.argv)
                  + "\n")
    else:
        out.write(f"Command line: not built ({dry.readiness.message})\n")
    if not dry.readiness.ok:
        out.write(f"Not ready to run: {dry.readiness.message}\n")
    return 0


def _research_lines(outcome: Any, out: TextIO) -> None:
    """A research's answer, sources, what was left out and its web steps (counts)."""
    report = outcome.research
    out.write(f"Answer: {report.answer or '(none)'}\n")
    for source in report.sources:
        checked = "" if source.checked else " (not checked against the search results)"
        out.write(f"Source [{source.number}]: {source.title} ({source.domain}) {source.url}{checked}\n")
    for number, reason in report.left_out:
        out.write(f"Left out [{number}]: {reason}\n")
    for _line, reason in report.dropped:
        out.write(f"Suggestion left out: {reason}\n")
    out.write(f"Web: {steps_words(report)}, {report.denied} refused\n")


def ask_once(text: str, config: Config, out: TextIO, *, briefing: BriefingContext | None,
             environ: Mapping[str, str] | None = None, runner: cli.Runner | None = None,
             sources: GoogleSources | None = None, now: Callable[[], datetime] | None = None) -> int:
    """One real Ask (the owner's spike): one or two planner runs on the Claude plan."""
    environ = os.environ if environ is None else environ
    engine = ClaudeEngine(config.ask, config.data_dir, runner=runner, environ=environ)
    planner = AskPlanner(config, engine, sources if sources is not None else google_sources(config), clock=now)
    if forced_question(text) is None:
        _with_memory(planner, config)   # web research never gets the notes (nor any other context)
    page_ids = [action.id for action in (briefing.pending if briefing else ())]
    outcome = planner.plan(text, briefing=briefing, page_ids=page_ids,
                           on_stage=lambda stage: out.write(STAGE_WORDS.get(stage, stage) + "\n"))
    if outcome.init is not None:
        init = outcome.init
        structured = "yes" if init.structured_tool else "no"
        out.write(f"Init: apiKeySource={init.api_key_source}, tools={list(init.tools)}, "
                  f"mcp_servers={init.mcp_servers}, model={init.model}, version={init.version}, "
                  f"structured_output={structured}\n")
    for number, stats in enumerate(outcome.stats, 1):
        out.write(f"Run {number}: {stats.duration_ms / 1000:.1f} s, {stats.turns} turn(s), {stats.input_tokens} in / "
                  f"{stats.output_tokens} out / {stats.cache_read_tokens} cache-read / {stats.cache_write_tokens} "
                  f"cache-write tokens, exit {stats.exit_code}\n")
    if outcome.search:
        out.write(f"Mail search ({outcome.search_account}): {outcome.search}\n")
    if outcome.mail.threads:
        out.write(f"Mail read: {outcome.mail.threads} thread(s), {outcome.mail.messages} message(s)\n")
    if not outcome.ok:
        out.write(f"Not planned: {outcome.message}\n")
        return 1
    if outcome.research is not None:
        _research_lines(outcome, out)
    else:
        out.write(f"Say: {outcome.say}\n")
    if outcome.question:
        out.write(f"Question: {outcome.question}\n")
    if outcome.message:
        out.write(f"Note: {outcome.message}\n")
    today = (now or (lambda: datetime.now().astimezone()))().date()
    for card in outcome.cards:
        view = card_view(card, today)
        extra = f" (unverified recipients: {', '.join(sorted(card.unverified))})" if card.unverified else ""
        if card.web and card.link:
            extra += f" -> {card.link}"
        out.write(f"Card: ASK {view.kind_label.upper()} | {view.title} | {view.detail}{extra}\n")
    if not outcome.cards:
        out.write("Cards: none\n")
    out.write(f"Total: {outcome.runs} run(s), {outcome.duration_ms / 1000:.1f} s\n" if outcome.research is not None
              else f"Total: {outcome.runs} planner run(s), {outcome.duration_ms / 1000:.1f} s\n")
    return 0


def run(args: Any, config: Config, out: TextIO | None) -> int:
    """The __main__ entry for --ask-check / --ask-text [--ask-dry-run] / --plan-dry-run."""
    stream: Any = out if out is not None else _NullOut()
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:   # a redirected console may not take every character of an event title or a briefing
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
    if args.ask_check:
        return ask_check(config, stream)
    plan_text = getattr(args, "plan_dry_run", None)
    if plan_text is not None:
        now = datetime.now().astimezone()
        briefing = cli_briefing(config, getattr(args, "from_file", None), now=now, out=stream)
        return plan_dry_run(plan_text, config, stream, briefing=briefing)
    if forced_question(args.ask_text or "") is None:
        found = route(args.ask_text or "", RouteFlags(memory=memory_on(config)))
        if found.kind in MEMORY_ROUTE_WORDS:   # the app's MEMORY tab keeps notes: no Ask, nothing saved
            stream.write(MEMORY_ROUTE_WORDS[found.kind] + "\n")
            return MEMORY_ROUTE_EXIT
    now = datetime.now().astimezone()
    if forced_question(args.ask_text or "") is not None:   # web research: nothing is read, not even the briefing
        briefing = None
    else:
        briefing = cli_briefing(config, getattr(args, "from_file", None), now=now, out=stream)
    if args.ask_dry_run:
        return ask_dry_run(args.ask_text, config, stream, briefing=briefing)
    return ask_once(args.ask_text, config, stream, briefing=briefing)


class _NullOut:
    def write(self, _text: str) -> int:
        return 0
