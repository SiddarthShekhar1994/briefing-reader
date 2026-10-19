"""Configuration, application paths and logging for briefing-reader.

Settings come from three places:

* ``.env`` in the project folder, loaded into the process environment without
  overriding variables that are already set: ``NOTION_TOKEN`` and
  ``BRIEFING_PAGE_ID`` (both required; there is no default page);
* the environment itself;
* ``config.toml`` in the project root for voice, prompt, polling, section,
  Google Calendar, proposed-action (heading names, the extra hosts a card's
  Open may open, the hidden Undo hold after Send / Cancel event, the recipient domains that need
  no extra confirmation), account (``[accounts.<alias>]``: which service acts for
  "work" / "personal" and what it may do), schedule, hotkey,
  agenda, display, Ask Jarvis (``[ask]``, off by default), assistant (``[assistant]``: how
  Jarvis speaks on his own), LIVE view (``[live]``: the tab that shows every step Jarvis
  takes, on screen only) and screen (``[ui]``: how much the hologram moves) options. Which
  Google account an alias is never goes here: the sign-ins live in %LOCALAPPDATA%\\briefing-reader.

A bad setting never stops the app: invalid values are logged as warnings and
replaced by their defaults. The Notion token is never logged. As soon as it is
read it is registered with :class:`RedactingFilter`, which is attached to every
handler that :func:`setup_logging` installs.

This module is Qt-free and needs only the standard library and python-dotenv.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import logging.handlers
import math
import os
import re
import sys
import tempfile
import threading
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Any
from urllib.parse import urlsplit

from . import APP_NAME
from .hotkey import DEFAULT_COMBO, normalize_combo
from .runstate import DEFAULT_SLOTS, parse_time_of_day

logger = logging.getLogger(__name__)

# No built-in page: BRIEFING_PAGE_ID must be set. "" means missing (or not a page id),
# and the app then says so instead of fetching anything.
DEFAULT_PAGE_ID = ""
DEFAULT_NOTION_VERSION = "2022-06-28"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_FILE_NAME = "config.toml"
ENV_FILE_NAME = ".env"
LOG_FILE_NAME = "briefing-reader.log"
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(threadName)s] %(message)s"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 5
REDACTED = "[REDACTED]"

# Edge neural voices accept roughly half to double speed; SAPI clamps on its own.
RATE_RANGE = (-50, 100)
VOLUME_RANGE = (-100, 100)

DEFAULT_CLIENT_SECRET = "google_client_secret.json"
DEFAULT_ACTIONS_HEADING = "Proposed actions"
DEFAULT_DEADLINE_KEYWORDS = ("due", "deadline", "exam", "midterm", "final", "quiz", "submit",
                             "submission", "assignment", "lab report", "application")
CLOCK_12H = "12h"
CLOCK_24H = "24h"
MOTION_LIVELY = "lively"       # [ui] motion: screen changes wipe and assemble, the start-up plays
MOTION_CALM = "calm"           # slower, fewer effects (fades only)
MOTION_OFF = "off"             # no entrance or transition effects
MOTION_CHOICES = (MOTION_LIVELY, MOTION_CALM, MOTION_OFF)
# What an account may do (google_auth.FEATURE_SCOPES has the scopes of each): "calendar" answers
# invitations and moves or cancels events; "gmail_send" sends the replies and emails you approve;
# "gmail_read" lets Ask Jarvis read the Gmail threads your request is about (read only; never used
# to send, and nothing it reads is saved or logged); with the productivity bundle it also covers the
# follow-up list's header scan (headers only, last 14 days), meeting prep reads and nudge drafts.
ACCOUNT_FEATURES = ("calendar", "gmail_send", "gmail_read")
BACKEND_GOOGLE = "google"
BACKEND_COMPOSIO = "composio"     # accepted, but not built into this version
ACCOUNT_BACKENDS = (BACKEND_GOOGLE, BACKEND_COMPOSIO)
DEFAULT_ACCOUNT = "personal"
UNDO_SECONDS_RANGE = (3, 30)

_MISSING = object()
_KNOWN_TABLES = ("voice", "prompt", "polling", "sections", "notion", "calendar", "actions",
                 "accounts", "schedule", "hotkey", "agenda", "display", "ask", "research", "assistant", "live",
                 "productivity", "ui", "meetings")
# Library loggers kept at WARNING, also under --debug. The Google sign-in libraries log the
# authorization code, the access token and the refresh token at DEBUG (requests_oauthlib),
# before Jarvis could register them for redaction, so they never get DEBUG here.
_QUIET_LOGGERS = ("urllib3", "asyncio", "aiohttp", "comtypes", "charset_normalizer",
                  "requests_oauthlib", "oauthlib", "google_auth_oauthlib", "google.auth",
                  "google_auth_httplib2", "google.oauth2", "httplib2")
# Silenced (only CRITICAL): googleapiclient logs request URLs (calendar ids, which can be an
# address, and event ids) in its retry warnings and response bodies in its errors; gcal.py
# logs its own safe line for every failure.
_SILENT_LOGGERS = ("googleapiclient",)
_HANDLER_TAG = "_briefing_reader_handler"
_MIN_SECRET_LEN = 8


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class VoiceConfig:
    voice: str = "en-GB-RyanNeural"
    rate: str = "+0%"             # validated ^[+-]\d+%$, else default + warning
    volume: str = "+0%"           # same validation
    offline_voice: str = ""       # substring of a SAPI voice name (any case); "" = system default
    section_gap_ms: int = 600     # silence between sections
    divider_pause_ms: int = 900   # silence for a divider


@dataclass(frozen=True)
class PromptConfig:
    later_short_minutes: int = 10
    later_long_minutes: int = 30
    ignore_after_seconds: int = 120
    refocus_on_reprompt: bool = False   # re-prompts after Later stay on top, no focus steal


@dataclass(frozen=True)
class PollingConfig:
    interval_seconds: int = 60
    timeout_minutes: int = 15


def _empty_mapping() -> Mapping[str, str]:
    return MappingProxyType({})


@dataclass(frozen=True)
class SectionsConfig:
    ignore: tuple[str, ...] = ("Ignore",)   # heading names skipped unless "Read everything"
    # Heading -> spoken name. Keys keep the user's spelling; look them up
    # case-insensitively. hash=False keeps the frozen dataclass hashable.
    announce: Mapping[str, str] = field(default_factory=_empty_mapping, hash=False)

    def __post_init__(self) -> None:
        if not isinstance(self.ignore, tuple):
            object.__setattr__(self, "ignore", tuple(self.ignore))
        if not isinstance(self.announce, MappingProxyType):
            object.__setattr__(self, "announce", MappingProxyType(dict(self.announce)))


@dataclass(frozen=True)
class CalendarConfig:
    enabled: bool = True
    # load_config resolves a relative path against the project root.
    client_secret_path: Path = Path(DEFAULT_CLIENT_SECRET)
    calendar_id: str = "primary"


@dataclass(frozen=True)
class ActionsConfig:
    headings: tuple[str, ...] = (DEFAULT_ACTIONS_HEADING,)   # "Proposed actions" heading names
    # Extra https hosts a card's Open may open, besides actions.BUILTIN_LINK_HOSTS:
    # "name.tld" exactly or "*.name.tld" for every subdomain (casefolded).
    link_hosts: tuple[str, ...] = ()
    # Seconds a Send (Reply / Email) or Cancel event is held after the click before the call to Google:
    # the card shows "Sent" / "Cancelled" with Undo at once (no number anywhere); Undo during the hold
    # sends nothing. Calendar changes (Approve, Add block, Accept / Decline / Maybe, Move) act at once
    # and their Undo reverses them for 10 minutes instead.
    undo_seconds: int = 5
    # Recipient domains whose addresses never need the NEW RECIPIENT confirmation ("example.edu"
    # covers its subdomains too); casefolded. Empty by default.
    trusted_domains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("headings", "link_hosts", "trusted_domains"):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                object.__setattr__(self, name, tuple(value))


@dataclass(frozen=True)
class AccountConfig:
    """One ``[accounts.<alias>]`` table: which service acts for the alias and what it may do."""

    alias: str
    backend: str = BACKEND_GOOGLE                     # "google"; "composio" is not built yet
    features: tuple[str, ...] = ("calendar",)         # ACCOUNT_FEATURES

    def __post_init__(self) -> None:
        if not isinstance(self.features, tuple):
            object.__setattr__(self, "features", tuple(self.features))


def _default_accounts() -> Mapping[str, AccountConfig]:
    return MappingProxyType({DEFAULT_ACCOUNT: AccountConfig(DEFAULT_ACCOUNT)})


@dataclass(frozen=True)
class ScheduleConfig:
    # When the AM / PM tasks run, 24-hour "HH:MM". The tasks pass their own times
    # with --slots; these are the fallback (manual starts, older tasks).
    am: str = DEFAULT_SLOTS["AM"]
    pm: str = DEFAULT_SLOTS["PM"]

    @property
    def slots(self) -> dict[str, str]:
        """{"AM": "10:12", "PM": "23:42"}, as runstate.slot_for takes them."""
        return {"AM": self.am, "PM": self.pm}


@dataclass(frozen=True)
class HotkeyConfig:
    enabled: bool = True
    combo: str = DEFAULT_COMBO     # canonical spelling, e.g. "ctrl+alt+j"


@dataclass(frozen=True)
class AgendaConfig:
    evening_from_hour: int = 18                                # from then on TODAY shows tomorrow
    deadline_days: int = 14                                    # how far ahead DEADLINES looks
    calendars: tuple[str, ...] = ("primary",)                  # calendar ids listed in TODAY
    deadline_keywords: tuple[str, ...] = DEFAULT_DEADLINE_KEYWORDS   # calendar titles that are deadlines

    def __post_init__(self) -> None:
        for name in ("calendars", "deadline_keywords"):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                object.__setattr__(self, name, tuple(value))


@dataclass(frozen=True)
class DisplayConfig:
    clock: str = CLOCK_12H     # times on screen: "12h" (1:05 PM) or "24h" (13:05); speech stays 12-hour

    @property
    def hour24(self) -> bool:
        return self.clock == CLOCK_24H


@dataclass(frozen=True)
class UiConfig:
    """``[ui]``: how the hologram moves. ``motion`` is "lively" (screen changes wipe and assemble, the
    start-up plays), "calm" (slower, fewer effects) or "off" (no entrance or transition effects; Windows'
    "Show animations in Windows" off counts as off too). true / false are read as "lively" / "off"."""

    motion: str = MOTION_LIVELY


# Ask Jarvis ([ask]): the planner is your own signed-in Claude Code CLI on your claude.ai plan.
# "sonnet", "haiku", "opus" or a full model name; never anything that starts with "-".
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,79}")
ASK_RANGES = {"timeout_seconds": (30, 300), "max_turns": (1, 8), "max_per_hour": (1, 120),
              "max_per_day": (1, 500), "days_back": (0, 7), "days_ahead": (1, 60), "max_cards": (1, 8)}


@dataclass(frozen=True)
class AskConfig:
    """``[ask]``: Ask Jarvis, off by default. Bad values become the default with a warning."""

    enabled: bool = False          # opt-in; needs your own signed-in Claude Code (claude.ai plan)
    model: str = "sonnet"          # "haiku" uses less of your plan
    timeout_seconds: int = 90      # one planner run (30..300)
    max_turns: int = 4             # 1..8
    max_per_hour: int = 20         # planner runs (an Ask that reads mail is two runs), 1..120
    max_per_day: int = 60          # 1..500
    days_back: int = 1             # calendar context: from this many days back (0..7) ...
    days_ahead: int = 14           # ... to this many days ahead (1..60)
    calendars: tuple[str, ...] = ("primary",)   # the calendar ids read for each account
    max_cards: int = 8             # proposals shown per Ask (1..8)
    hardened_flags: bool = True    # add --safe-mode --restricted when this Claude Code has them
    read_mail: bool = True         # read the Gmail threads a request is about (accounts with gmail_read)

    def __post_init__(self) -> None:
        if not isinstance(self.calendars, tuple):
            object.__setattr__(self, "calendars", tuple(self.calendars))


# Web research ([research]): a separate run of the same Claude Code whose only tools are web search
# and web fetch, given only the owner's words and today's date (briefing_reader.ask.research).
RESEARCH_RANGES = {"timeout_seconds": (30, 300), "max_searches": (1, 8), "max_fetches": (0, 8),
                   "max_per_hour": (1, 30), "max_per_day": (1, 100), "max_sources": (1, 6)}


@dataclass(frozen=True)
class ResearchConfig:
    """``[research]``: web research (part of Ask; needs [ask] enabled). Bad values become the default
    with a warning."""

    enabled: bool = True           # needs [ask] enabled
    planner_may_ask: bool = True   # the Ask planner may hand a request to web research
    model: str = "sonnet"
    timeout_seconds: int = 120     # one research run (30..300)
    max_searches: int = 4          # web searches per run (1..8)
    max_fetches: int = 4           # page reads per run (0..8); 0 = search only (WebFetch not offered)
    max_per_hour: int = 6          # research runs (each also counts as an Ask planner run), 1..30
    max_per_day: int = 20          # 1..100
    max_sources: int = 5           # sources shown as Open cards (1..6)

    @property
    def max_turns(self) -> int:
        """The run's --max-turns: every allowed search and page read, the answer and one spare."""
        return self.max_searches + self.max_fetches + 2


# How Jarvis addresses the owner: a letter, then letters, spaces, . ' - (20 characters at most).
ADDRESS_RE = re.compile(r"[A-Za-z][A-Za-z .'-]{0,19}")


@dataclass(frozen=True)
class AssistantConfig:
    """``[assistant]``: how Jarvis talks on his own. Bad values become the default with a warning."""

    speak: bool = True             # false: never speaks on his own (the briefing still plays on Play)
    address: str = "sir"           # "Good morning, sir."; "" for no form of address
    greet: bool = True             # a short random greeting each time Jarvis is opened
    greeting_context: bool = True  # the greeting may add one safe fact (proposals waiting, next event)
    speak_replies: bool = True     # speak Ask's answers (and "One moment, sir" while it plans)
    speak_results: bool = True     # speak what happened after an approved card was carried out
    scheduled_prompt: bool = False   # true: scheduled runs show the classic "Hear it now?" prompt


LIVE_OPEN_TAB = "tab"
LIVE_OPEN_WINDOW = "window"
LIVE_OPEN_OFF = "off"
LIVE_OPEN_CHOICES = (LIVE_OPEN_TAB, LIVE_OPEN_WINDOW, LIVE_OPEN_OFF)


@dataclass(frozen=True)
class LiveConfig:
    """``[live]``: the LIVE tab (every step Jarvis takes, on screen only; never saved or logged).
    Bad values become the default with a warning."""

    enabled: bool = True           # the LIVE tab and its stream (false: no tab, nothing recorded)
    auto_open: str = LIVE_OPEN_TAB   # "tab" | "window" | "off" (true -> "tab", false -> "off" accepted)
    background: bool = True        # the rolling "Background reads" row (agenda, event checks, sign-ins...)
    text: bool = True              # keep the text blocks (false: only their sizes)
    pictures: bool = True          # email, calendar and web search pictures in LIVE, drawn on this PC
    # pictures of the pages a web research read or cites: the only part that uses the network
    page_snapshots: bool = True


# The productivity bundle ([productivity]): plan my day, meeting prep, follow-ups and memory. Off in
# the public config.toml; JARVIS_PRODUCTIVITY_ENABLED (e.g. in the gitignored .env) turns it on per PC.
PRODUCTIVITY_RANGES = {"max_memory": (10, 200), "plan_max_blocks": (1, 8), "plan_min_free_minutes": (30, 240),
                       "prep_lead_minutes": (5, 60), "prep_offers_per_day": (0, 20), "prep_max_offer_guests": (1, 50),
                       "prep_max_guests": (1, 10), "prep_lookback_days": (3, 14), "scan_days": (3, 14),
                       "scan_every_minutes": (15, 240), "scan_max_threads": (20, 300), "waiting_after_hours": (1, 72),
                       "owed_after_days": (1, 13), "followups_shown": (1, 20), "max_runs_per_day": (1, 60),
                       "reserve_ask_runs": (0, 10)}
PRODUCTIVITY_TIMES = ("plan_offer_until", "day_start", "day_end", "quiet_from", "quiet_until")
DEFAULT_PREP_SKIP_WORDS = ("focus", "lunch", "commute", "gym", "break", "hold", "ooo", "out of office")


@dataclass(frozen=True)
class ProductivityConfig:
    """``[productivity]``: off by default; inert unless [ask] is on too. Bad values become the
    default with a warning. Times are 24-hour "HH:MM"."""

    enabled: bool = False
    memory: bool = True                # the MEMORY tab, "remember that ...", notes given to the planner
    memory_suggestions: bool = True    # Jarvis may suggest a note; kept only when you click
    max_memory: int = 100              # notes kept (10..200)
    plan_offer: bool = True            # "Shall I plan your day, sir?" after the AM briefing
    plan_offer_until: str = "13:30"    # no plan offer after this time
    day_start: str = "08:00"           # plan blocks only between these times
    day_end: str = "22:00"
    plan_max_blocks: int = 5           # 1..8
    plan_min_free_minutes: int = 60    # 30..240
    prep_offers: bool = True           # offer a brief before a meeting with guests
    prep_lead_minutes: int = 15        # 5..60
    prep_offers_per_day: int = 8       # 0..20
    prep_max_offer_guests: int = 8     # 1..50
    prep_max_guests: int = 5           # 1..10
    prep_lookback_days: int = 14       # 3..14, never past scan_days
    prep_other_accounts: bool = False
    prep_skip_words: tuple[str, ...] = DEFAULT_PREP_SKIP_WORDS   # casefolded
    prep_speak_minimized: bool = True
    minimized_check: bool = True
    followups: bool = True
    scan_days: int = 14                # 3..14
    scan_every_minutes: int = 30       # 15..240
    scan_max_threads: int = 150        # 20..300
    waiting_after_hours: int = 20      # 1..72
    owed_after_days: int = 3           # 1..13
    followups_shown: int = 6           # 1..20
    quiet_from: str = "22:00"
    quiet_until: str = "08:00"
    max_runs_per_day: int = 20         # 1..60
    reserve_ask_runs: int = 3          # 0..10

    def __post_init__(self) -> None:
        if not isinstance(self.prep_skip_words, tuple):
            object.__setattr__(self, "prep_skip_words", tuple(self.prep_skip_words))


@dataclass(frozen=True)
class MeetingsConfig:
    """``[meetings]``: meeting recording (off by default; JARVIS_MEETINGS_ENABLED overrides ``enabled``).
    Recording starts only from the owner's click after the consent tick; the devices are picked in
    Settings > Meetings (the Windows defaults are followed otherwise)."""

    enabled: bool = False


@dataclass(frozen=True)
class Config:
    notion_token: str = field(repr=False)   # "" when missing; NEVER logged
    page_id: str                            # normalized 32-hex, no dashes; "" when missing or invalid
    notion_version: str
    voice: VoiceConfig
    prompt: PromptConfig
    polling: PollingConfig
    sections: SectionsConfig
    project_root: Path
    config_path: Path
    data_dir: Path
    log_dir: Path
    audio_root: Path
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    actions: ActionsConfig = field(default_factory=ActionsConfig)
    # alias -> AccountConfig; without an [accounts] table just "personal". hash=False keeps the
    # frozen dataclass hashable.
    accounts: Mapping[str, AccountConfig] = field(default_factory=_default_accounts, hash=False)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    agenda: AgendaConfig = field(default_factory=AgendaConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    ask: AskConfig = field(default_factory=AskConfig)
    assistant: AssistantConfig = field(default_factory=AssistantConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    research: ResearchConfig = field(default_factory=ResearchConfig)
    productivity: ProductivityConfig = field(default_factory=ProductivityConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    meetings: MeetingsConfig = field(default_factory=MeetingsConfig)

    @property
    def notion_url(self) -> str:
        return f"https://www.notion.so/{self.page_id}"


# --------------------------------------------------------------------------
# Page ids
# --------------------------------------------------------------------------

_HEX32 = r"[0-9a-f]{32}"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ID_RE = re.compile(rf"{_HEX32}|{_UUID}", re.IGNORECASE)
# The last path segment of a page URL is "<title-slug>-<id>" or just "<id>".
_URL_ID_RE = re.compile(rf"(?:^|-)({_HEX32}|{_UUID})$", re.IGNORECASE)
_NOTION_HOSTS = ("notion.so", "notion.site", "notion.com")


def normalize_page_id(raw: str) -> str:
    """Return the page id in ``raw`` as 32 lowercase hex characters.

    Accepts a bare 32-hex id, a dashed UUID, or a Notion page URL whose last
    path segment ends in the id. Raises ValueError for anything else. The
    error message never echoes the input, in case a secret was pasted there.
    """
    if not isinstance(raw, str):
        raise ValueError("a Notion page id must be text")
    text = raw.strip()
    if _ID_RE.fullmatch(text):
        return _compact_id(text)
    page_id = _id_from_url(text)
    if page_id is None:
        raise ValueError("not a Notion page id or Notion page URL")
    return page_id


def _compact_id(text: str) -> str:
    return text.replace("-", "").lower()


def _id_from_url(text: str) -> str | None:
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if not any(host == name or host.endswith("." + name) for name in _NOTION_HOSTS):
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if not segments:
        return None
    match = _URL_ID_RE.search(segments[-1])
    return _compact_id(match.group(1)) if match else None


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def default_data_dir() -> Path:
    """%LOCALAPPDATA%\\briefing-reader (or ~/AppData/Local/briefing-reader)."""
    return _data_dir_from(os.environ)


def _data_dir_from(environ: Mapping[str, str]) -> Path:
    base = (environ.get("LOCALAPPDATA") or "").strip()
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / APP_NAME


def load_config(project_root: Path | None = None, *,
                environ: Mapping[str, str] | None = None) -> Config:
    """Load settings; never raises because of a bad value.

    With ``environ=None`` the project's ``.env`` is loaded into ``os.environ``
    (existing variables win) and ``os.environ`` is read. When ``environ`` is
    given, neither ``.env`` nor ``os.environ`` is touched.
    """
    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    if environ is None:
        _load_dotenv(root / ENV_FILE_NAME)
        environ = os.environ

    token = _clean_value(environ.get("NOTION_TOKEN"))
    register_secret(token)
    if not token:
        logger.info("NOTION_TOKEN is not set (expected in %s or the environment)",
                    root / ENV_FILE_NAME)

    config_path = root / CONFIG_FILE_NAME
    doc = _read_toml(config_path)
    for name in doc:
        if name not in _KNOWN_TABLES:
            logger.warning("config.toml: unknown setting %r ignored", name)

    data_dir = _data_dir_from(environ)
    return Config(
        notion_token=token,
        page_id=_page_id_from_env(environ.get("BRIEFING_PAGE_ID"), root / ENV_FILE_NAME),
        notion_version=_parse_notion_version(doc),
        voice=_parse_voice(doc),
        prompt=_parse_prompt(doc),
        polling=_parse_polling(doc),
        sections=_parse_sections(doc),
        project_root=root,
        config_path=config_path,
        data_dir=data_dir,
        log_dir=data_dir / "logs",
        audio_root=Path(tempfile.gettempdir()) / APP_NAME,
        calendar=_parse_calendar(doc, root),
        actions=_parse_actions(doc),
        accounts=_parse_accounts(doc),
        schedule=_parse_schedule(doc),
        hotkey=_parse_hotkey(doc),
        agenda=_parse_agenda(doc),
        display=_parse_display(doc),
        ask=_parse_ask(doc, environ),
        assistant=_parse_assistant(doc),
        live=_parse_live(doc),
        research=_parse_research(doc, environ),
        productivity=_parse_productivity(doc, environ),
        ui=_parse_ui(doc),
        meetings=_parse_meetings(doc, environ),
    )


def _clean_value(raw: object) -> str:
    """Strip whitespace and surrounding quotes from an environment value."""
    if not isinstance(raw, str):
        return ""
    return raw.strip().strip("\"'").strip()


def _page_id_from_env(raw: str | None, env_path: Path) -> str:
    """The normalized page id from BRIEFING_PAGE_ID, or "" (missing or unusable).

    There is no fallback page: the app reports the missing id instead of
    reading someone else's page. The value is never logged when it is bad, in
    case a secret was pasted there by mistake.
    """
    text = _clean_value(raw)
    if not text:
        logger.info("BRIEFING_PAGE_ID is not set (expected in %s or the environment)", env_path)
        return DEFAULT_PAGE_ID
    try:
        return normalize_page_id(text)
    except ValueError:
        logger.warning("BRIEFING_PAGE_ID is not a Notion page id or page URL; ignoring it "
                       "(the app reports the page id as missing)")
        return DEFAULT_PAGE_ID


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.warning("python-dotenv is not installed; %s was not read", path)
        return
    try:
        load_dotenv(path, override=False, encoding=_sniff_text_encoding(path))
    except (OSError, UnicodeError) as exc:
        # Only the exception type: decode errors can quote file bytes.
        logger.warning("Could not read %s (%s); using the environment only",
                       path, type(exc).__name__)


def _sniff_text_encoding(path: Path) -> str:
    # PowerShell 5.1 writes UTF-16 for ">" and UTF-8 *with BOM* for
    # "Set-Content -Encoding UTF8"; a BOM read as UTF-8 would corrupt the first key.
    with path.open("rb") as fh:
        head = fh.read(2)
    if head in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"
    return "utf-8-sig"


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        logger.info("No %s; using default settings", path)
        return {}
    except OSError as exc:
        logger.warning("Could not read %s (%s); using default settings",
                       path, exc.strerror or type(exc).__name__)
        return {}
    try:
        return tomllib.loads(data.decode("utf-8-sig"))
    except UnicodeDecodeError:
        logger.warning("%s is not UTF-8 text; using default settings", path)
    except tomllib.TOMLDecodeError as exc:
        logger.warning("%s is not valid TOML (%s); using default settings", path, exc)
    return {}


class _TableReader:
    """Reads typed values from one TOML table, replacing bad ones with defaults."""

    def __init__(self, doc: Mapping[str, Any], name: str, known: Sequence[str]) -> None:
        self.name = name
        table = doc.get(name, {})
        if not isinstance(table, dict):
            logger.warning("config.toml: %s = %s should be a [%s] table; using defaults",
                           name, _short_repr(table), name)
            table = {}
        self.table: dict[str, Any] = table
        for key in table:
            if key not in known:
                logger.warning("config.toml: unknown setting %s.%s ignored", name, key)

    def _get(self, key: str) -> Any:
        return self.table.get(key, _MISSING)

    def _invalid(self, key: str, value: Any, problem: str, default: Any) -> None:
        logger.warning("config.toml: %s.%s = %s %s; using %r",
                       self.name, key, _short_repr(value), problem, default)

    def integer(self, key: str, default: int, low: int, high: int) -> int:
        value = self._get(key)
        if value is _MISSING:
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self._invalid(key, value, "is not a number", default)
            return default
        if isinstance(value, float) and not math.isfinite(value):
            self._invalid(key, value, "is not a number", default)
            return default
        number = round(value)
        if number < 0:
            self._invalid(key, value, "must not be negative", default)
            return default
        clamped = min(max(number, low), high)
        if clamped != number:
            self._invalid(key, value, f"is outside {low}..{high}", clamped)
        return clamped

    def boolean(self, key: str, default: bool) -> bool:
        value = self._get(key)
        if value is _MISSING:
            return default
        if not isinstance(value, bool):
            self._invalid(key, value, "is not true or false", default)
            return default
        return value

    def string(self, key: str, default: str, *, allow_empty: bool = False,
               pattern: re.Pattern[str] | None = None) -> str:
        value = self._get(key)
        if value is _MISSING:
            return default
        if not isinstance(value, str):
            self._invalid(key, value, "is not a text value", default)
            return default
        text = value.strip()
        if not text and not allow_empty:
            self._invalid(key, value, "is empty", default)
            return default
        if text and pattern is not None and not pattern.fullmatch(text):
            self._invalid(key, value, "is not in the expected format", default)
            return default
        return text

    def choice(self, key: str, default: str, choices: Sequence[str]) -> str:
        """One of ``choices``, matched ignoring case and surrounding spaces ("24H " -> "24h")."""
        value = self._get(key)
        if value is _MISSING:
            return default
        text = value.strip().casefold() if isinstance(value, str) else None
        for option in choices:
            if text == option.casefold():
                return option
        self._invalid(key, value, "is not " + " or ".join(f'"{option}"' for option in choices), default)
        return default

    def percent(self, key: str, default: str, low: int, high: int) -> str:
        value = self._get(key)
        if value is _MISSING:
            return default
        match = _PERCENT_RE.fullmatch(value.strip()) if isinstance(value, str) else None
        if match is None:
            self._invalid(key, value, 'is not a percentage like "+10%" or "-10%"', default)
            return default
        number = int(match.group(2)) * (-1 if match.group(1) == "-" else 1)
        clamped = min(max(number, low), high)
        result = f"{clamped:+d}%"
        if clamped != number:
            self._invalid(key, value, f"is outside {low:+d}%..{high:+d}%", result)
        return result

    def names(self, key: str, default: tuple[str, ...], *,
              allow_empty: bool = True) -> tuple[str, ...]:
        value = self._get(key)
        if value is _MISSING:
            return default
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            self._invalid(key, value, "is not a list of names", default)
            return default
        result: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                logger.warning("config.toml: %s.%s entry %s is not a name; skipped",
                               self.name, key, _short_repr(item))
                continue
            name = item.strip()
            if name.casefold() not in (seen.casefold() for seen in result):
                result.append(name)
        if not result and not allow_empty:
            self._invalid(key, value, "names nothing", default)
            return default
        return tuple(result)

    def time_of_day(self, key: str, default: str) -> str:
        """A 24-hour "HH:MM" time, normalized ("9:05" -> "09:05")."""
        value = self._get(key)
        if value is _MISSING:
            return default
        try:
            return parse_time_of_day(value).strftime("%H:%M")
        except ValueError:
            self._invalid(key, value, "is not a 24-hour time like 10:12", default)
            return default

    def combo(self, key: str, default: str) -> str:
        """A hotkey such as "ctrl+alt+j", in its canonical spelling."""
        value = self._get(key)
        if value is _MISSING:
            return default
        if not isinstance(value, str):
            self._invalid(key, value, "is not a text value", default)
            return default
        try:
            return normalize_combo(value)
        except ValueError as exc:
            self._invalid(key, value, f"is not a hotkey ({exc})", default)
            return default

    def path(self, key: str, default: Path, root: Path) -> Path:
        """A file path; a relative one is resolved against ``root``."""
        text = self.string(key, str(default), pattern=_PATH_RE)
        path = Path(text)
        return path if path.is_absolute() else root / path

    def name_map(self, key: str) -> Mapping[str, str]:
        value = self._get(key)
        if value is _MISSING:
            return _empty_mapping()
        if not isinstance(value, dict):
            self._invalid(key, value, "is not a table", {})
            return _empty_mapping()
        result: dict[str, str] = {}
        for name, spoken in value.items():
            if not name.strip() or not isinstance(spoken, str) or not spoken.strip():
                logger.warning("config.toml: %s.%s entry %s = %s is not a name; skipped",
                               self.name, key, _short_repr(name), _short_repr(spoken))
                continue
            result[name.strip()] = spoken.strip()
        return MappingProxyType(result)


_PERCENT_RE = re.compile(r"([+-]?)(\d{1,4})%")
_NOTION_VERSION_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PATH_RE = re.compile(r"[^\x00-\x1f]+")
_CALENDAR_ID_RE = re.compile(r"\S{1,256}")


def _short_repr(value: Any, limit: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _field_names(cls: type) -> tuple[str, ...]:
    return tuple(f.name for f in dataclasses.fields(cls))


def _parse_voice(doc: Mapping[str, Any]) -> VoiceConfig:
    d = VoiceConfig()
    r = _TableReader(doc, "voice", _field_names(VoiceConfig))
    return VoiceConfig(
        voice=r.string("voice", d.voice),
        rate=r.percent("rate", d.rate, *RATE_RANGE),
        volume=r.percent("volume", d.volume, *VOLUME_RANGE),
        offline_voice=r.string("offline_voice", d.offline_voice, allow_empty=True),
        section_gap_ms=r.integer("section_gap_ms", d.section_gap_ms, 0, 10_000),
        divider_pause_ms=r.integer("divider_pause_ms", d.divider_pause_ms, 0, 10_000),
    )


def _parse_prompt(doc: Mapping[str, Any]) -> PromptConfig:
    d = PromptConfig()
    r = _TableReader(doc, "prompt", _field_names(PromptConfig))
    return PromptConfig(
        later_short_minutes=r.integer("later_short_minutes", d.later_short_minutes, 1, 1440),
        later_long_minutes=r.integer("later_long_minutes", d.later_long_minutes, 1, 1440),
        ignore_after_seconds=r.integer("ignore_after_seconds", d.ignore_after_seconds, 10, 3600),
        refocus_on_reprompt=r.boolean("refocus_on_reprompt", d.refocus_on_reprompt),
    )


def _parse_polling(doc: Mapping[str, Any]) -> PollingConfig:
    d = PollingConfig()
    r = _TableReader(doc, "polling", _field_names(PollingConfig))
    return PollingConfig(
        interval_seconds=r.integer("interval_seconds", d.interval_seconds, 10, 3600),
        timeout_minutes=r.integer("timeout_minutes", d.timeout_minutes, 1, 720),
    )


def _parse_sections(doc: Mapping[str, Any]) -> SectionsConfig:
    d = SectionsConfig()
    r = _TableReader(doc, "sections", _field_names(SectionsConfig))
    return SectionsConfig(ignore=r.names("ignore", d.ignore), announce=r.name_map("announce"))


def _parse_calendar(doc: Mapping[str, Any], root: Path) -> CalendarConfig:
    d = CalendarConfig()
    r = _TableReader(doc, "calendar", ("enabled", "client_secret", "calendar_id"))
    return CalendarConfig(
        enabled=r.boolean("enabled", d.enabled),
        client_secret_path=r.path("client_secret", d.client_secret_path, root),
        calendar_id=r.string("calendar_id", d.calendar_id, pattern=_CALENDAR_ID_RE),
    )


def _parse_actions(doc: Mapping[str, Any]) -> ActionsConfig:
    d = ActionsConfig()
    r = _TableReader(doc, "actions", ("heading", "link_hosts", "undo_seconds", "trusted_domains"))
    return ActionsConfig(headings=r.names("heading", d.headings, allow_empty=False),
                         link_hosts=_link_hosts(r.names("link_hosts", d.link_hosts)),
                         undo_seconds=r.integer("undo_seconds", d.undo_seconds, *UNDO_SECONDS_RANGE),
                         trusted_domains=_trusted_domains(r.names("trusted_domains", d.trusted_domains)))


_ALIAS_RE = re.compile(r"[a-z][a-z0-9_-]{0,23}")


def _parse_accounts(doc: Mapping[str, Any]) -> Mapping[str, AccountConfig]:
    """[accounts.<alias>] tables; without an [accounts] table just "personal" (Google, calendar).

    An alias is lowercase letters, digits, - or _ (24 at most); a bad alias, backend or feature
    is skipped with a warning. A table that names no usable account falls back to the default.
    """
    table = doc.get("accounts", _MISSING)
    if table is _MISSING:
        return _default_accounts()
    if not isinstance(table, dict):
        logger.warning("config.toml: accounts = %s should be a table of [accounts.<name>] tables; "
                       "using the personal account", _short_repr(table))
        return _default_accounts()
    accounts: dict[str, AccountConfig] = {}
    for name, value in table.items():
        alias = name.strip().casefold() if isinstance(name, str) else ""
        if not _ALIAS_RE.fullmatch(alias):
            logger.warning("config.toml: [accounts.%s] is not an account name (lowercase letters, digits, "
                           "- or _); skipped", _short_repr(name, 30))
            continue
        if alias in accounts:
            logger.warning("config.toml: [accounts.%s] repeats the account name %s; skipped",
                           _short_repr(name, 30), alias)
            continue
        if not isinstance(value, dict):
            logger.warning("config.toml: accounts.%s should be a table; skipped", alias)
            continue
        d = AccountConfig(alias)
        key = f"accounts.{alias}"
        r = _TableReader({key: value}, key, ("backend", "features"))
        backend = r.choice("backend", d.backend, ACCOUNT_BACKENDS)
        features = []
        for feature in r.names("features", d.features):
            if feature.casefold() not in ACCOUNT_FEATURES:
                logger.warning("config.toml: accounts.%s.features entry %s is not one of %s; skipped",
                               alias, _short_repr(feature), ", ".join(ACCOUNT_FEATURES))
            elif feature.casefold() not in features:
                features.append(feature.casefold())
        accounts[alias] = AccountConfig(alias, backend=backend, features=tuple(features))
    if not accounts:
        logger.warning("config.toml: [accounts] names no usable account; using the personal account")
        return _default_accounts()
    return MappingProxyType(accounts)


_LINK_HOST_RE = re.compile(
    r"(?:\*\.)?[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")


def _link_hosts(names: Sequence[str]) -> tuple[str, ...]:
    """[actions] link_hosts: "name.tld" or "*.name.tld", casefolded; bad entries skipped, repeats dropped."""
    hosts: list[str] = []
    for name in names:
        host = name.strip().casefold()
        if not _LINK_HOST_RE.fullmatch(host):
            logger.warning('config.toml: actions.link_hosts entry %s is not a host name like '
                           '"example.edu" or "*.example.edu"; skipped', _short_repr(name))
            continue
        if host not in hosts:
            hosts.append(host)
    return tuple(hosts)


# A mail domain: dot-separated labels ending in a 2-63 letter top-level domain (as in addresses).
_MAIL_DOMAIN_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


def _trusted_domains(names: Sequence[str]) -> tuple[str, ...]:
    """[actions] trusted_domains: "example.edu" (covers its subdomains too), casefolded; a leading
    "@" is dropped; bad entries (wildcards, addresses, URLs) skipped, repeats dropped."""
    domains: list[str] = []
    for name in names:
        domain = name.strip().casefold().removeprefix("@")
        if len(domain) > 253 or not _MAIL_DOMAIN_RE.fullmatch(domain):
            logger.warning('config.toml: actions.trusted_domains entry %s is not a domain like '
                           '"example.edu"; skipped', _short_repr(name))
            continue
        if domain not in domains:
            domains.append(domain)
    return tuple(domains)


def _parse_schedule(doc: Mapping[str, Any]) -> ScheduleConfig:
    d = ScheduleConfig()
    r = _TableReader(doc, "schedule", _field_names(ScheduleConfig))
    return ScheduleConfig(am=r.time_of_day("am", d.am), pm=r.time_of_day("pm", d.pm))


def _parse_hotkey(doc: Mapping[str, Any]) -> HotkeyConfig:
    d = HotkeyConfig()
    r = _TableReader(doc, "hotkey", _field_names(HotkeyConfig))
    return HotkeyConfig(enabled=r.boolean("enabled", d.enabled), combo=r.combo("combo", d.combo))


def _parse_agenda(doc: Mapping[str, Any]) -> AgendaConfig:
    d = AgendaConfig()
    r = _TableReader(doc, "agenda", _field_names(AgendaConfig))
    calendars = tuple(name for name in r.names("calendars", d.calendars, allow_empty=False)
                      if _calendar_id_ok(name))
    return AgendaConfig(
        evening_from_hour=r.integer("evening_from_hour", d.evening_from_hour, 0, 24),
        deadline_days=r.integer("deadline_days", d.deadline_days, 1, 60),
        calendars=calendars or d.calendars,
        deadline_keywords=r.names("deadline_keywords", d.deadline_keywords),
    )


def _calendar_id_ok(name: str) -> bool:
    if _CALENDAR_ID_RE.fullmatch(name):
        return True
    logger.warning("config.toml: agenda.calendars entry %s is not a calendar id; skipped",
                   _short_repr(name))
    return False


def _parse_display(doc: Mapping[str, Any]) -> DisplayConfig:
    d = DisplayConfig()
    r = _TableReader(doc, "display", _field_names(DisplayConfig))
    return DisplayConfig(clock=r.choice("clock", d.clock, (CLOCK_12H, CLOCK_24H)))


def _parse_ui(doc: Mapping[str, Any]) -> UiConfig:
    """``[ui]``: ``motion`` (any case; true -> "lively", false -> "off"); anything else is the default."""
    d = UiConfig()
    r = _TableReader(doc, "ui", _field_names(UiConfig))
    raw = r._get("motion")
    if isinstance(raw, bool):
        return UiConfig(motion=MOTION_LIVELY if raw else MOTION_OFF)
    return UiConfig(motion=r.choice("motion", d.motion, MOTION_CHOICES))


ASK_ENABLED_ENV = "JARVIS_ASK_ENABLED"
_ENV_TRUE = frozenset({"1", "true", "yes", "on"})
_ENV_FALSE = frozenset({"0", "false", "no", "off"})


def _enabled_override(enabled: bool, environ: Mapping[str, str] | None, name: str, setting: str) -> bool:
    """``enabled`` unless the environment variable ``name`` says true or false (a bad value is
    reported and the config.toml value kept)."""
    override = _clean_value((environ or {}).get(name)).lower()
    if override in _ENV_TRUE or override in _ENV_FALSE:
        return override in _ENV_TRUE
    if override:
        logger.warning("%s should be true or false; using config.toml %s", name, setting)
    return enabled


def _parse_ask(doc: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> AskConfig:
    """``[ask]`` from config.toml; ``JARVIS_ASK_ENABLED`` (e.g. in the gitignored ``.env``)
    overrides ``enabled`` so a personal setting never has to change the tracked file."""
    d = AskConfig()
    r = _TableReader(doc, "ask", _field_names(AskConfig))
    numbers = {key: r.integer(key, getattr(d, key), low, high) for key, (low, high) in ASK_RANGES.items()}
    calendars = []
    for name in r.names("calendars", d.calendars, allow_empty=False):
        if _CALENDAR_ID_RE.fullmatch(name) and not name.startswith("-"):
            calendars.append(name)
        else:
            logger.warning("config.toml: ask.calendars entry %s is not a calendar id; skipped", _short_repr(name))
    enabled = _enabled_override(r.boolean("enabled", d.enabled), environ, ASK_ENABLED_ENV, "ask.enabled")
    return AskConfig(
        enabled=enabled,
        model=r.string("model", d.model, pattern=_MODEL_RE),
        calendars=tuple(calendars) or d.calendars,
        hardened_flags=r.boolean("hardened_flags", d.hardened_flags),
        read_mail=r.boolean("read_mail", d.read_mail),
        **numbers,
    )


RESEARCH_ENABLED_ENV = "JARVIS_RESEARCH_ENABLED"


def _parse_research(doc: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> ResearchConfig:
    """``[research]`` from config.toml; ``JARVIS_RESEARCH_ENABLED`` (e.g. in the gitignored ``.env``)
    overrides ``enabled`` both ways, like JARVIS_ASK_ENABLED."""
    d = ResearchConfig()
    r = _TableReader(doc, "research", _field_names(ResearchConfig))
    numbers = {key: r.integer(key, getattr(d, key), low, high) for key, (low, high) in RESEARCH_RANGES.items()}
    enabled = _enabled_override(r.boolean("enabled", d.enabled), environ, RESEARCH_ENABLED_ENV, "research.enabled")
    return ResearchConfig(
        enabled=enabled,
        planner_may_ask=r.boolean("planner_may_ask", d.planner_may_ask),
        model=r.string("model", d.model, pattern=_MODEL_RE),
        **numbers,
    )


PRODUCTIVITY_ENABLED_ENV = "JARVIS_PRODUCTIVITY_ENABLED"
_MIN_DAY_MINUTES = 60   # day_end must be more than this after day_start


def _minutes(text: str) -> int:
    hours, _, minutes = text.partition(":")
    return int(hours) * 60 + int(minutes)


def _parse_productivity(doc: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> ProductivityConfig:
    """``[productivity]`` from config.toml; ``JARVIS_PRODUCTIVITY_ENABLED`` (e.g. in the gitignored
    ``.env``) overrides ``enabled`` both ways, like JARVIS_ASK_ENABLED."""
    d = ProductivityConfig()
    r = _TableReader(doc, "productivity", _field_names(ProductivityConfig))
    numbers = {key: r.integer(key, getattr(d, key), low, high) for key, (low, high) in PRODUCTIVITY_RANGES.items()}
    times = {key: r.time_of_day(key, getattr(d, key)) for key in PRODUCTIVITY_TIMES}
    flags = {key: r.boolean(key, getattr(d, key))
             for key in ("memory", "memory_suggestions", "plan_offer", "prep_offers", "prep_other_accounts",
                         "prep_speak_minimized", "minimized_check", "followups")}
    enabled = _enabled_override(r.boolean("enabled", d.enabled), environ, PRODUCTIVITY_ENABLED_ENV,
                                "productivity.enabled")
    skip: list[str] = []
    for word in r.names("prep_skip_words", d.prep_skip_words):
        folded = " ".join(word.casefold().split())
        if folded and folded not in skip:
            skip.append(folded)
    if _minutes(times["day_end"]) <= _minutes(times["day_start"]) + _MIN_DAY_MINUTES:
        logger.warning("config.toml: productivity.day_end (%s) must be more than an hour after day_start (%s); "
                       "using %s-%s", times["day_end"], times["day_start"], d.day_start, d.day_end)
        times["day_start"], times["day_end"] = d.day_start, d.day_end
    if numbers["prep_lookback_days"] > numbers["scan_days"]:
        logger.warning("config.toml: productivity.prep_lookback_days = %d is more than scan_days; using %d",
                       numbers["prep_lookback_days"], numbers["scan_days"])
        numbers["prep_lookback_days"] = numbers["scan_days"]
    return ProductivityConfig(enabled=enabled, prep_skip_words=tuple(skip), **flags, **times, **numbers)


MEETINGS_ENABLED_ENV = "JARVIS_MEETINGS_ENABLED"


def _parse_meetings(doc: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> MeetingsConfig:
    """``[meetings]`` from config.toml; ``JARVIS_MEETINGS_ENABLED`` (e.g. in the gitignored ``.env``)
    overrides ``enabled`` both ways, like JARVIS_PRODUCTIVITY_ENABLED."""
    d = MeetingsConfig()
    r = _TableReader(doc, "meetings", _field_names(MeetingsConfig))
    enabled = _enabled_override(r.boolean("enabled", d.enabled), environ, MEETINGS_ENABLED_ENV, "meetings.enabled")
    return MeetingsConfig(enabled=enabled)


def _parse_assistant(doc: Mapping[str, Any]) -> AssistantConfig:
    """``[assistant]``: Jarvis's own voice (greeting, "ready to view", answers, results)."""
    d = AssistantConfig()
    r = _TableReader(doc, "assistant", _field_names(AssistantConfig))
    flags = {key: r.boolean(key, getattr(d, key))
             for key in ("speak", "greet", "greeting_context", "speak_replies", "speak_results",
                         "scheduled_prompt")}
    return AssistantConfig(address=r.string("address", d.address, allow_empty=True, pattern=ADDRESS_RE),
                           **flags)


def _parse_live(doc: Mapping[str, Any]) -> LiveConfig:
    """``[live]``: the LIVE tab. ``auto_open`` also takes true ("tab") and false ("off")."""
    d = LiveConfig()
    r = _TableReader(doc, "live", _field_names(LiveConfig))
    raw = r._get("auto_open")
    if isinstance(raw, bool):
        auto_open = LIVE_OPEN_TAB if raw else LIVE_OPEN_OFF
    else:
        auto_open = r.choice("auto_open", d.auto_open, LIVE_OPEN_CHOICES)
    return LiveConfig(enabled=r.boolean("enabled", d.enabled), auto_open=auto_open,
                      background=r.boolean("background", d.background), text=r.boolean("text", d.text),
                      pictures=r.boolean("pictures", d.pictures),
                      page_snapshots=r.boolean("page_snapshots", d.page_snapshots))


def _parse_notion_version(doc: Mapping[str, Any]) -> str:
    r = _TableReader(doc, "notion", ("version",))
    return r.string("version", DEFAULT_NOTION_VERSION, pattern=_NOTION_VERSION_RE)


# --------------------------------------------------------------------------
# Secret redaction
# --------------------------------------------------------------------------

_TOKEN_SHAPE_RE = re.compile(r"\b(?:secret_|ntn_)[A-Za-z0-9]{20,}")
_secrets_lock = threading.Lock()
_secrets: tuple[str, ...] = ()   # replaced, never mutated; longest first


def register_secret(secret: str) -> None:
    """Make RedactingFilter mask ``secret``. Empty or short values are ignored."""
    global _secrets
    if not isinstance(secret, str):
        return
    secret = secret.strip()
    if len(secret) < _MIN_SECRET_LEN:
        return
    with _secrets_lock:
        merged = set(_secrets) | _escaped_forms(secret)
        # Longest first so a secret that contains another is masked whole.
        _secrets = tuple(sorted(merged, key=lambda s: (-len(s), s)))


def _escaped_forms(secret: str) -> set[str]:
    # A value with control characters or quotes shows up escaped when an
    # exception quotes it with repr() (requests' InvalidHeader does) or as JSON.
    return {secret, repr(secret)[1:-1], json.dumps(secret)[1:-1]}


def redact(text: str) -> str:
    """Replace registered secrets and token-shaped strings with [REDACTED]."""
    for secret in _secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    return _TOKEN_SHAPE_RE.sub(REDACTED, text)


_exception_formatter = logging.Formatter()


class RedactingFilter(logging.Filter):
    """Handler filter that masks secrets in the message and the traceback.

    The message is formatted here (``msg % args``) and stored back with
    ``args = None`` so no later formatter can reintroduce a secret from the
    arguments. The traceback text is rendered into ``exc_text``, which
    ``logging.Formatter`` uses instead of re-rendering ``exc_info``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(_render_message(record))
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = _exception_formatter.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def _render_message(record: logging.LogRecord) -> str:
    try:
        return record.getMessage()
    except Exception:
        # Mismatched %-args: keep the line (still redacted) instead of losing it.
        return f"{record.msg!s} (args: {record.args!r})"


# --------------------------------------------------------------------------
# Logging setup
# --------------------------------------------------------------------------

def setup_logging(log_dir: Path | None = None, *, debug: bool = False,
                  keep_open: bool = True) -> Path:
    """Log to a rotating file (and stderr when there is one). Idempotent.

    Returns the path of the log file. If ``log_dir`` cannot be created the
    log goes to %TEMP%\\briefing-reader\\logs instead. ``keep_open=False``
    opens the file only while writing a record: for a process that runs all
    day (the hotkey agent), so it never keeps the app from rotating the log.
    """
    root = logging.getLogger()
    _remove_own_handlers(root)

    wanted = Path(log_dir) if log_dir is not None else default_data_dir() / "logs"
    fallback = Path(tempfile.gettempdir()) / APP_NAME / "logs"
    handler_class = logging.handlers.RotatingFileHandler if keep_open else _ClosingRotatingFileHandler
    file_handler, used_dir = _open_log_file((wanted, fallback), handler_class)

    handlers: list[logging.Handler] = [] if file_handler is None else [file_handler]
    if sys.stderr is not None:   # None under pythonw
        handlers.append(logging.StreamHandler(sys.stderr))
    formatter = logging.Formatter(LOG_FORMAT)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(RedactingFilter())
        setattr(handler, _HANDLER_TAG, True)
        root.addHandler(handler)

    root.setLevel(logging.DEBUG if debug else logging.INFO)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in _SILENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.CRITICAL)
    _install_excepthooks()

    if file_handler is None:
        logger.error("Could not open a log file in %s or %s", wanted, fallback)
        return wanted / LOG_FILE_NAME
    if used_dir != wanted:
        logger.warning("Could not use the log folder %s; logging to %s instead", wanted, used_dir)
    return Path(file_handler.baseFilename)


def _remove_own_handlers(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            root.removeHandler(handler)
            handler.close()


class _ClosingRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """A rotating file handler that closes the file after every record.

    On Windows a file that another process holds open cannot be renamed, so a
    process that kept the log open all day would stop the app's rotation (and
    the app would lose the lines it tried to rotate for).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)   # opens the file once: proves the folder is usable
        self._close_stream()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
        finally:
            self._close_stream()

    def _close_stream(self) -> None:
        stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _open_log_file(directories: Sequence[Path],
                   handler_class: type[logging.handlers.RotatingFileHandler]
                   = logging.handlers.RotatingFileHandler,
                   ) -> tuple[logging.handlers.RotatingFileHandler | None, Path | None]:
    for directory in directories:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handler = handler_class(directory / LOG_FILE_NAME, maxBytes=LOG_MAX_BYTES,
                                    backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
        except OSError:
            continue
        return handler, directory
    return None, None


# --------------------------------------------------------------------------
# Uncaught exceptions
# --------------------------------------------------------------------------

_previous_excepthook: Any = None
_previous_threading_excepthook: Any = None


def _install_excepthooks() -> None:
    global _previous_excepthook, _previous_threading_excepthook
    if sys.excepthook is not _log_uncaught_exception:
        _previous_excepthook = sys.excepthook
        sys.excepthook = _log_uncaught_exception
    if threading.excepthook is not _log_uncaught_thread_exception:
        _previous_threading_excepthook = threading.excepthook
        threading.excepthook = _log_uncaught_thread_exception


def _log_uncaught_exception(exc_type: type[BaseException], exc_value: BaseException | None,
                            exc_tb: TracebackType | None) -> None:
    """sys.excepthook: log with traceback (redacted) instead of printing raw.

    Ctrl+C goes to the previous hook untouched; the interpreter still exits
    with the usual KeyboardInterrupt status. Other exceptions are not passed
    on, because the default hook would print an unredacted traceback.
    """
    if exc_type is not None and issubclass(exc_type, KeyboardInterrupt):
        (_previous_excepthook or sys.__excepthook__)(exc_type, exc_value, exc_tb)
        return
    try:
        logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
    except Exception:
        sys.__excepthook__(exc_type, exc_value, exc_tb)


def _log_uncaught_thread_exception(args: threading.ExceptHookArgs) -> None:
    """threading.excepthook: same policy as the main thread; SystemExit is silent."""
    exc_type = args.exc_type
    if exc_type is not None and issubclass(exc_type, SystemExit):
        return
    if exc_type is not None and issubclass(exc_type, KeyboardInterrupt):
        (_previous_threading_excepthook or threading.__excepthook__)(args)
        return
    thread_name = args.thread.name if args.thread is not None else "unknown"
    try:
        logger.critical("Uncaught exception in thread %s", thread_name,
                        exc_info=(exc_type, args.exc_value, args.exc_traceback))
    except Exception:
        threading.__excepthook__(args)
