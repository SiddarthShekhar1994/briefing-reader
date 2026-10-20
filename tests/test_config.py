"""Tests for briefing_reader.config: settings, page ids, redaction and logging.

Every test uses a temporary project root and passes ``environ=`` (or patches
``os.environ`` with automatic restore), so the real .env, config and
environment are never read or changed. Logging state is restored after each
logging test.
"""

from __future__ import annotations

import dataclasses
import io
import json
import logging
import logging.handlers
import os
import sys
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from briefing_reader import config
from briefing_reader.config import (
    DEFAULT_PAGE_ID,
    LOG_FORMAT,
    REDACTED,
    DEFAULT_DEADLINE_KEYWORDS,
    AccountConfig,
    ActionsConfig,
    AgendaConfig,
    AskConfig,
    AssistantConfig,
    CalendarConfig,
    Config,
    DisplayConfig,
    HotkeyConfig,
    LiveConfig,
    PollingConfig,
    ProductivityConfig,
    PromptConfig,
    RedactingFilter,
    ResearchConfig,
    ScheduleConfig,
    SectionsConfig,
    VoiceConfig,
    default_data_dir,
    load_config,
    normalize_page_id,
    redact,
    register_secret,
    setup_logging,
)

CONFIG_LOGGER = "briefing_reader.config"
PAGE_ID = "0123456789abcdef0123456789abcdef"
DASHED_PAGE_ID = "01234567-89ab-cdef-0123-456789abcdef"
# Fake values only. Each is unique so it cannot collide with other tests.
FAKE_TOKEN = "ntn_FakeTokenForConfigTests0123456789abcdefXYZ"
PLAIN_SECRET = "plainSecretValue-for-redaction-tests-42"


def _same_path(a: Path | str, b: Path | str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


class ProjectTestCase(unittest.TestCase):
    """A temporary project root and an isolated environment mapping."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.environ = {"NOTION_TOKEN": FAKE_TOKEN,
                        "LOCALAPPDATA": str(self.root / "localappdata")}

    def write_config(self, text: str, encoding: str = "utf-8") -> None:
        (self.root / "config.toml").write_text(text, encoding=encoding)

    def load(self, **overrides: str) -> Config:
        return load_config(self.root, environ={**self.environ, **overrides})

    def load_quietly(self, **overrides: str) -> Config:
        with self.assertNoLogs(CONFIG_LOGGER, level="WARNING"):
            return self.load(**overrides)

    def load_warning(self, *fragments: str, **overrides: str) -> Config:
        """Load and assert that warnings mentioning every fragment were logged."""
        with self.assertLogs(CONFIG_LOGGER, level="WARNING") as captured:
            cfg = self.load(**overrides)
        text = "\n".join(captured.output)
        for fragment in fragments:
            self.assertIn(fragment, text)
        return cfg


# --------------------------------------------------------------------------
# Page ids
# --------------------------------------------------------------------------

class NormalizePageIdTests(unittest.TestCase):
    def test_plain_hex(self) -> None:
        self.assertEqual(normalize_page_id(PAGE_ID), PAGE_ID)
        self.assertEqual(normalize_page_id(PAGE_ID.upper()), PAGE_ID)
        self.assertEqual(normalize_page_id(f"  {PAGE_ID}\n"), PAGE_ID)

    def test_dashed_uuid(self) -> None:
        self.assertEqual(normalize_page_id(DASHED_PAGE_ID), PAGE_ID)
        self.assertEqual(normalize_page_id(DASHED_PAGE_ID.upper()), PAGE_ID)

    def test_notion_urls(self) -> None:
        urls = [
            f"https://www.notion.so/{PAGE_ID}",
            f"https://www.notion.so/myspace/Daily-Briefing-{PAGE_ID}",
            f"https://www.notion.so/myspace/Daily-Briefing-{PAGE_ID}?pvs=4",
            f"https://www.notion.so/Daily-Briefing-{PAGE_ID}#some-heading",
            f"https://www.notion.so/myspace/Daily-Briefing-{PAGE_ID}/",
            f"notion.so/Daily-Briefing-{PAGE_ID}",
            f"https://myteam.notion.site/Daily-Briefing-{PAGE_ID}",
            f"https://www.notion.so/Daily-Briefing-{DASHED_PAGE_ID}",
            f"HTTPS://WWW.NOTION.SO/Daily-Briefing-{PAGE_ID.upper()}",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(normalize_page_id(url), PAGE_ID)

    def test_invalid_values_raise_value_error(self) -> None:
        bad: list[Any] = [
            "", "   ", "not-a-page-id", PAGE_ID[:-1], PAGE_ID + "0", "g" * 32,
            f"https://example.com/{PAGE_ID}",
            "https://www.notion.so/",
            "https://www.notion.so/Daily-Briefing",
            f"https://www.notion.so/abc{PAGE_ID}",
            f"https://www.notion.so/{PAGE_ID}/comments",
            None, 123,
        ]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_page_id(value)

    def test_error_message_does_not_echo_input(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            normalize_page_id(FAKE_TOKEN)
        self.assertNotIn(FAKE_TOKEN, str(ctx.exception))


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------

class DefaultsTests(ProjectTestCase):
    def test_dataclass_defaults_match_contract(self) -> None:
        self.assertEqual(VoiceConfig(), VoiceConfig(
            voice="en-GB-RyanNeural", rate="+0%", volume="+0%", offline_voice="",
            section_gap_ms=600, divider_pause_ms=900))
        self.assertEqual(PromptConfig(), PromptConfig(
            later_short_minutes=10, later_long_minutes=30, ignore_after_seconds=120,
            refocus_on_reprompt=False))
        self.assertEqual(PollingConfig(), PollingConfig(interval_seconds=60, timeout_minutes=15))
        self.assertEqual(SectionsConfig().ignore, ("Ignore",))
        self.assertEqual(dict(SectionsConfig().announce), {})
        self.assertEqual(CalendarConfig(), CalendarConfig(
            enabled=True, client_secret_path=Path("google_client_secret.json"),
            calendar_id="primary"))
        self.assertEqual(ActionsConfig().headings, ("Proposed actions",))
        headings: Any = ["A", "B"]
        self.assertEqual(ActionsConfig(headings=headings).headings, ("A", "B"))
        self.assertEqual(ScheduleConfig(), ScheduleConfig(am="10:12", pm="23:42"))
        self.assertEqual(ScheduleConfig().slots, {"AM": "10:12", "PM": "23:42"})
        self.assertEqual(HotkeyConfig(), HotkeyConfig(enabled=True, combo="ctrl+alt+j"))
        self.assertEqual(AgendaConfig(), AgendaConfig(
            evening_from_hour=18, deadline_days=14, calendars=("primary",),
            deadline_keywords=("due", "deadline", "exam", "midterm", "final", "quiz", "submit",
                               "submission", "assignment", "lab report", "application")))
        lists: Any = ["a", "b"]
        self.assertEqual(AgendaConfig(calendars=lists, deadline_keywords=lists).calendars, ("a", "b"))
        self.assertEqual(AgendaConfig(deadline_keywords=lists).deadline_keywords, ("a", "b"))
        self.assertEqual(DisplayConfig(), DisplayConfig(clock="12h"))
        self.assertEqual(AskConfig(), AskConfig(
            enabled=False, model="sonnet", timeout_seconds=90, max_turns=4, max_per_hour=20,
            max_per_day=60, days_back=1, days_ahead=14, calendars=("primary",), max_cards=8,
            hardened_flags=True, read_mail=True))
        self.assertEqual(AskConfig(calendars=lists).calendars, ("a", "b"))
        self.assertFalse(DisplayConfig().hour24)
        self.assertTrue(DisplayConfig(clock="24h").hour24)

    def test_defaults_without_config_file(self) -> None:
        cfg = self.load_quietly()
        self.assertEqual(cfg.notion_token, FAKE_TOKEN)
        self.assertEqual(cfg.page_id, "")   # no built-in page: BRIEFING_PAGE_ID is required
        self.assertEqual(cfg.notion_version, "2022-06-28")
        self.assertEqual(cfg.voice, VoiceConfig())
        self.assertEqual(cfg.prompt, PromptConfig())
        self.assertEqual(cfg.polling, PollingConfig())
        self.assertEqual(cfg.sections, SectionsConfig())
        self.assertEqual(cfg.project_root, self.root)
        self.assertEqual(cfg.config_path, self.root / "config.toml")
        self.assertEqual(cfg.data_dir, self.root / "localappdata" / "briefing-reader")
        self.assertEqual(cfg.log_dir, cfg.data_dir / "logs")
        self.assertEqual(cfg.audio_root, Path(tempfile.gettempdir()) / "briefing-reader")
        self.assertEqual(cfg.notion_url, "https://www.notion.so/")
        self.assertEqual(cfg.calendar, CalendarConfig(
            enabled=True, client_secret_path=self.root / "google_client_secret.json",
            calendar_id="primary"))
        self.assertEqual(cfg.actions, ActionsConfig())
        self.assertEqual(cfg.schedule, ScheduleConfig())
        self.assertEqual(cfg.hotkey, HotkeyConfig())
        self.assertEqual(cfg.agenda, AgendaConfig())
        self.assertEqual(cfg.display, DisplayConfig())
        self.assertEqual(cfg.ask, AskConfig())

    def test_load_config_does_not_create_directories(self) -> None:
        cfg = self.load_quietly()
        self.assertFalse(cfg.data_dir.exists())

    def test_missing_token_is_empty_string(self) -> None:
        cfg = load_config(self.root, environ={"LOCALAPPDATA": str(self.root)})
        self.assertEqual(cfg.notion_token, "")

    def test_data_dir_falls_back_to_home(self) -> None:
        cfg = load_config(self.root, environ={"NOTION_TOKEN": FAKE_TOKEN})
        self.assertEqual(cfg.data_dir, Path.home() / "AppData" / "Local" / "briefing-reader")

    def test_default_data_dir_uses_localappdata(self) -> None:
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}):
            self.assertEqual(default_data_dir(), self.root / "briefing-reader")
        with mock.patch.dict(os.environ):
            os.environ.pop("LOCALAPPDATA", None)
            self.assertEqual(default_data_dir(),
                             Path.home() / "AppData" / "Local" / "briefing-reader")

    def test_config_is_frozen_and_hashable(self) -> None:
        cfg = self.load_quietly()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cfg.page_id = PAGE_ID  # type: ignore[misc]
        with self.assertRaises(TypeError):
            cfg.sections.announce["x"] = "y"  # type: ignore[index]
        hash(cfg)
        self.assertEqual(cfg, self.load_quietly())


# --------------------------------------------------------------------------
# config.toml
# --------------------------------------------------------------------------

FULL_CONFIG = """
[voice]
voice = "en-US-GuyNeural"
rate = "+15%"
volume = "-10%"
offline_voice = "Zira"
section_gap_ms = 400
divider_pause_ms = 1200

[prompt]
later_short_minutes = 5
later_long_minutes = 45
ignore_after_seconds = 90
refocus_on_reprompt = true

[polling]
interval_seconds = 30
timeout_minutes = 20

[sections]
ignore = ["Ignore", "Promotions"]

[sections.announce]
"Work inbox" = "Your work inbox"
"Due" = "Things due"

[notion]
version = "2025-09-03"

[calendar]
enabled = false
client_secret = "secrets/client.json"
calendar_id = "team@group.calendar.google.com"

[actions]
heading = ["Proposed actions", "Actions"]

[schedule]
am = "7:30"
pm = "18:05"

[hotkey]
enabled = false
combo = "Shift + Ctrl + F9"

[agenda]
evening_from_hour = 17
deadline_days = 21
calendars = ["primary", "team@group.calendar.google.com"]
deadline_keywords = ["due", "Exam"]

[display]
clock = "24h"
"""


class ConfigFileTests(ProjectTestCase):
    def test_values_read_from_file(self) -> None:
        self.write_config(FULL_CONFIG)
        cfg = self.load_quietly()
        self.assertEqual(cfg.voice, VoiceConfig(
            voice="en-US-GuyNeural", rate="+15%", volume="-10%", offline_voice="Zira",
            section_gap_ms=400, divider_pause_ms=1200))
        self.assertEqual(cfg.prompt, PromptConfig(
            later_short_minutes=5, later_long_minutes=45, ignore_after_seconds=90,
            refocus_on_reprompt=True))
        self.assertEqual(cfg.polling, PollingConfig(interval_seconds=30, timeout_minutes=20))
        self.assertEqual(cfg.sections.ignore, ("Ignore", "Promotions"))
        self.assertEqual(dict(cfg.sections.announce),
                         {"Work inbox": "Your work inbox", "Due": "Things due"})
        self.assertEqual(cfg.notion_version, "2025-09-03")
        self.assertEqual(cfg.calendar, CalendarConfig(
            enabled=False, client_secret_path=self.root / "secrets" / "client.json",
            calendar_id="team@group.calendar.google.com"))
        self.assertEqual(cfg.actions.headings, ("Proposed actions", "Actions"))
        self.assertEqual(cfg.schedule, ScheduleConfig(am="07:30", pm="18:05"))
        self.assertEqual(cfg.hotkey, HotkeyConfig(enabled=False, combo="ctrl+shift+f9"))
        self.assertEqual(cfg.agenda, AgendaConfig(
            evening_from_hour=17, deadline_days=21,
            calendars=("primary", "team@group.calendar.google.com"),
            deadline_keywords=("due", "Exam")))
        self.assertEqual(cfg.display, DisplayConfig(clock="24h"))

    def test_partial_file_keeps_other_defaults(self) -> None:
        self.write_config('[voice]\nrate = "-10%"\n')
        cfg = self.load_quietly()
        self.assertEqual(cfg.voice, dataclasses.replace(VoiceConfig(), rate="-10%"))
        self.assertEqual(cfg.prompt, PromptConfig())
        self.assertEqual(cfg.sections, SectionsConfig())
        self.assertEqual(cfg.calendar.client_secret_path, self.root / "google_client_secret.json")
        self.assertEqual(cfg.actions, ActionsConfig())

    def test_utf8_bom_is_accepted(self) -> None:
        self.write_config('[voice]\nrate = "+10%"\n', encoding="utf-8-sig")
        self.assertEqual(self.load_quietly().voice.rate, "+10%")

    def test_percent_without_sign_is_normalized(self) -> None:
        self.write_config('[voice]\nrate = " 15% "\nvolume = "-0%"\n')
        cfg = self.load_quietly()
        self.assertEqual(cfg.voice.rate, "+15%")
        self.assertEqual(cfg.voice.volume, "+0%")

    def test_shipped_config_toml_is_valid_and_complete(self) -> None:
        shipped = config.PROJECT_ROOT / "config.toml"
        raw = shipped.read_bytes()
        raw.decode("ascii")  # must be plain ASCII
        doc = tomllib.loads(raw.decode("utf-8"))
        expected_keys = {
            "voice": {f.name for f in dataclasses.fields(VoiceConfig)},
            "prompt": {f.name for f in dataclasses.fields(PromptConfig)},
            "polling": {f.name for f in dataclasses.fields(PollingConfig)},
            "sections": {f.name for f in dataclasses.fields(SectionsConfig)},
            "notion": {"version"},
            "calendar": {"enabled", "client_secret", "calendar_id"},
            "actions": {"heading", "link_hosts", "undo_seconds", "trusted_domains"},
            "accounts": {"personal", "work"},
            "schedule": {f.name for f in dataclasses.fields(ScheduleConfig)},
            "hotkey": {f.name for f in dataclasses.fields(HotkeyConfig)},
            "agenda": {f.name for f in dataclasses.fields(AgendaConfig)},
            "display": {f.name for f in dataclasses.fields(DisplayConfig)},
            "ask": {f.name for f in dataclasses.fields(AskConfig)},
            "assistant": {f.name for f in dataclasses.fields(AssistantConfig)},
            "live": {f.name for f in dataclasses.fields(LiveConfig)},
            "research": {f.name for f in dataclasses.fields(ResearchConfig)},
            "productivity": {f.name for f in dataclasses.fields(ProductivityConfig)},
            "ui": {f.name for f in dataclasses.fields(config.UiConfig)},
            "meetings": {f.name for f in dataclasses.fields(config.MeetingsConfig)},
            "desktop": {f.name for f in dataclasses.fields(config.DesktopConfig)},
        }
        self.assertEqual(set(doc), set(expected_keys))
        for table, keys in expected_keys.items():
            with self.subTest(table=table):
                self.assertEqual(set(doc[table]), keys)

        with self.assertNoLogs(CONFIG_LOGGER, level="WARNING"):
            cfg = load_config(config.PROJECT_ROOT, environ=self.environ)
        self.assertEqual(cfg.voice, VoiceConfig())
        self.assertEqual(cfg.prompt, PromptConfig())
        self.assertEqual(cfg.polling, PollingConfig())
        self.assertEqual(cfg.sections.ignore, ("Ignore",))
        names = ["Work inbox", "Due", "Actionable", "Newsletters"]
        self.assertEqual(dict(cfg.sections.announce), {n: n for n in names})
        self.assertEqual(cfg.notion_version, "2022-06-28")
        self.assertEqual(cfg.calendar, CalendarConfig(
            enabled=True,
            client_secret_path=config.PROJECT_ROOT / "google_client_secret.json",
            calendar_id="primary"))
        self.assertEqual(cfg.actions, ActionsConfig(headings=("Proposed actions",)))
        self.assertEqual(cfg.actions.undo_seconds, 5)
        # Generic aliases only: which Google account each one is never goes into config.toml.
        self.assertEqual(dict(cfg.accounts), {
            "personal": AccountConfig("personal", features=("calendar", "gmail_send", "gmail_read")),
            "work": AccountConfig("work", features=("calendar", "gmail_send", "gmail_read"))})
        self.assertEqual(cfg.actions.trusted_domains, ())   # public default: nobody is trusted
        self.assertNotIn("@", raw.decode("ascii").split("[accounts", 1)[1].split("[schedule]", 1)[0])
        self.assertEqual(cfg.schedule, ScheduleConfig())
        self.assertEqual(cfg.hotkey, HotkeyConfig())
        self.assertEqual(cfg.agenda, AgendaConfig())
        self.assertEqual(cfg.display, DisplayConfig(clock="12h"))
        # Ask Jarvis ships off, with the defaults (no path or address in the public file).
        self.assertEqual(cfg.ask, AskConfig())
        self.assertFalse(cfg.ask.enabled)
        ask_part = raw.decode("ascii").split("[ask]", 1)[1]
        self.assertNotIn("@", ask_part)
        self.assertNotIn("claude.exe", ask_part.casefold())
        # Jarvis talks by default; the classic prompt is opt-in.
        self.assertEqual(cfg.assistant, AssistantConfig())
        self.assertTrue(cfg.assistant.speak)
        self.assertFalse(cfg.assistant.scheduled_prompt)
        self.assertIn("opens Jarvis", raw.decode("ascii").split("[hotkey]", 1)[1].split("[agenda]", 1)[0])
        # The LIVE tab ships on, opening as a tab, with the background row and the text blocks.
        self.assertEqual(cfg.live, LiveConfig(enabled=True, auto_open="tab", background=True, text=True))
        self.assertIn("never saved, never logged", raw.decode("ascii").split("[live]", 1)[1])
        # Web research ships with its defaults (it only runs with [ask] enabled); no address or path.
        self.assertEqual(cfg.research, ResearchConfig())
        research_part = raw.decode("ascii").split("[research]", 1)[1].split("[assistant]", 1)[0]
        self.assertNotIn("@", research_part)
        self.assertIn("never your calendar, mail, contacts", research_part)
        # The productivity bundle ships off, with every default (switched on per PC from .env).
        self.assertEqual(cfg.productivity, ProductivityConfig())
        self.assertFalse(cfg.productivity.enabled)
        self.assertEqual((cfg.productivity.prep_lookback_days, cfg.productivity.minimized_check), (14, True))
        productivity_part = raw.decode("ascii").split("[productivity]", 1)[1]
        self.assertNotIn("@", productivity_part)
        self.assertIn("JARVIS_PRODUCTIVITY_ENABLED=true", productivity_part)
        # Jarvis on your desktop ships off, with every default (switched on per PC from .env); no path in it.
        self.assertEqual(cfg.desktop, config.DesktopConfig())
        self.assertFalse(cfg.desktop.enabled)
        desktop_part = raw.decode("ascii").split("[desktop]", 1)[1]
        self.assertIn("JARVIS_DESKTOP_ENABLED=true", desktop_part)
        self.assertIn("\nenabled = false\n", desktop_part)
        self.assertNotIn(":\\", desktop_part)

    def test_client_secret_is_gitignored(self) -> None:
        lines = (config.PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("google_client_secret.json", [line.strip() for line in lines])

    def test_bad_rate_and_volume_fall_back(self) -> None:
        self.write_config('[voice]\nrate = "fast"\nvolume = 20\n')
        cfg = self.load_warning("voice.rate", "voice.volume")
        self.assertEqual(cfg.voice.rate, "+0%")
        self.assertEqual(cfg.voice.volume, "+0%")

    def test_negative_numbers_fall_back(self) -> None:
        self.write_config(
            "[prompt]\nlater_short_minutes = -5\nlater_long_minutes = -30\n"
            "[polling]\ntimeout_minutes = -1\ninterval_seconds = -60\n"
            "[voice]\nsection_gap_ms = -1\n")
        cfg = self.load_warning("prompt.later_short_minutes", "polling.timeout_minutes")
        self.assertEqual(cfg.prompt, PromptConfig())
        self.assertEqual(cfg.polling, PollingConfig())
        self.assertEqual(cfg.voice.section_gap_ms, 600)

    def test_wrong_types_fall_back(self) -> None:
        self.write_config(
            "[voice]\nvoice = 42\noffline_voice = false\nsection_gap_ms = [1]\n"
            "divider_pause_ms = nan\n"
            '[prompt]\nlater_long_minutes = "30"\nrefocus_on_reprompt = "yes"\n'
            "ignore_after_seconds = true\n"
            "[polling]\ninterval_seconds = 1979-05-27\n"
            "[notion]\nversion = 2022\n")
        cfg = self.load_warning("voice.voice", "prompt.refocus_on_reprompt",
                                "polling.interval_seconds", "notion.version")
        self.assertEqual(cfg.voice, VoiceConfig())
        self.assertEqual(cfg.prompt, PromptConfig())
        self.assertEqual(cfg.polling, PollingConfig())
        self.assertEqual(cfg.notion_version, "2022-06-28")

    def test_out_of_range_numbers_are_clamped(self) -> None:
        self.write_config(
            '[voice]\nrate = "+500%"\nvolume = "-250%"\nsection_gap_ms = 999999\n'
            "[prompt]\nlater_short_minutes = 0\nignore_after_seconds = 3\n"
            "later_long_minutes = 12.6\n"
            "[polling]\ninterval_seconds = 1\ntimeout_minutes = 100000\n")
        cfg = self.load_warning("voice.rate", "prompt.ignore_after_seconds")
        self.assertEqual(cfg.voice.rate, "+100%")
        self.assertEqual(cfg.voice.volume, "-100%")
        self.assertEqual(cfg.voice.section_gap_ms, 10_000)
        self.assertEqual(cfg.prompt.later_short_minutes, 1)
        self.assertEqual(cfg.prompt.ignore_after_seconds, 10)
        self.assertEqual(cfg.prompt.later_long_minutes, 13)
        self.assertEqual(cfg.polling.interval_seconds, 10)
        self.assertEqual(cfg.polling.timeout_minutes, 720)

    def test_empty_voice_falls_back_but_empty_offline_voice_is_allowed(self) -> None:
        self.write_config('[voice]\nvoice = "  "\noffline_voice = ""\n')
        cfg = self.load_warning("voice.voice")
        self.assertEqual(cfg.voice.voice, "en-GB-RyanNeural")
        self.assertEqual(cfg.voice.offline_voice, "")

    def test_bad_notion_version_falls_back(self) -> None:
        self.write_config('[notion]\nversion = "latest"\n')
        self.assertEqual(self.load_warning("notion.version").notion_version, "2022-06-28")

    def test_section_names_are_cleaned(self) -> None:
        self.write_config(
            '[sections]\nignore = [1, " Promo ", "", "promo", "Spam"]\n'
            '[sections.announce]\n"Due" = 3\n" Work inbox " = " Inbox "\n"Empty" = ""\n')
        cfg = self.load_warning("sections.ignore", "sections.announce")
        self.assertEqual(cfg.sections.ignore, ("Promo", "Spam"))
        self.assertEqual(dict(cfg.sections.announce), {"Work inbox": "Inbox"})

    def test_section_value_shapes(self) -> None:
        self.write_config('[sections]\nignore = "Spam"\n')
        self.assertEqual(self.load_quietly().sections.ignore, ("Spam",))
        self.write_config("[sections]\nignore = []\n")
        self.assertEqual(self.load_quietly().sections.ignore, ())
        self.write_config('[sections]\nignore = 5\nannounce = "Due"\n')
        cfg = self.load_warning("sections.ignore", "sections.announce")
        self.assertEqual(cfg.sections, SectionsConfig())

    def test_table_of_wrong_type_falls_back(self) -> None:
        self.write_config('voice = "loud"\nprompt = 3\n')
        cfg = self.load_warning("[voice]", "[prompt]")
        self.assertEqual(cfg.voice, VoiceConfig())
        self.assertEqual(cfg.prompt, PromptConfig())

    def test_unknown_settings_are_reported_and_ignored(self) -> None:
        self.write_config('[voice]\nspeed = 3\nrate = "+5%"\n[extras]\nfoo = 1\n')
        cfg = self.load_warning("voice.speed", "extras")
        self.assertEqual(cfg.voice.rate, "+5%")

    def test_invalid_toml_uses_defaults(self) -> None:
        self.write_config('[voice\nrate = = "+10%"\n')
        cfg = self.load_warning("not valid TOML")
        self.assertEqual(cfg.voice, VoiceConfig())
        self.assertEqual(cfg.sections, SectionsConfig())
        self.assertEqual(cfg.notion_token, FAKE_TOKEN)

    def test_non_utf8_file_uses_defaults(self) -> None:
        (self.root / "config.toml").write_bytes(b"\xff\xfe[\x00v\x00")
        cfg = self.load_warning("not UTF-8")
        self.assertEqual(cfg.voice, VoiceConfig())

    def test_unreadable_config_path_uses_defaults(self) -> None:
        (self.root / "config.toml").mkdir()
        cfg = self.load_warning("Could not read")
        self.assertEqual(cfg.polling, PollingConfig())


class CalendarAndActionsConfigTests(ProjectTestCase):
    def test_absolute_client_secret_path_is_kept(self) -> None:
        secret = self.root / "elsewhere" / "client.json"
        self.write_config(f"[calendar]\nclient_secret = {json.dumps(str(secret))}\n")
        self.assertEqual(self.load_quietly().calendar.client_secret_path, secret)

    def test_relative_client_secret_path_is_resolved_against_project_root(self) -> None:
        self.write_config('[calendar]\nclient_secret = " my_client.json "\n')
        cfg = self.load_quietly()
        self.assertEqual(cfg.calendar.client_secret_path, self.root / "my_client.json")
        self.assertTrue(cfg.calendar.client_secret_path.is_absolute())

    def test_bad_calendar_values_fall_back(self) -> None:
        self.write_config(
            '[calendar]\nenabled = "yes"\nclient_secret = 42\ncalendar_id = "has space"\n')
        cfg = self.load_warning("calendar.enabled", "calendar.client_secret",
                                "calendar.calendar_id")
        self.assertEqual(cfg.calendar, CalendarConfig(
            enabled=True, client_secret_path=self.root / "google_client_secret.json",
            calendar_id="primary"))

    def test_empty_calendar_strings_fall_back(self) -> None:
        self.write_config('[calendar]\nclient_secret = "  "\ncalendar_id = ""\n')
        cfg = self.load_warning("calendar.client_secret", "calendar.calendar_id")
        self.assertEqual(cfg.calendar.client_secret_path, self.root / "google_client_secret.json")
        self.assertEqual(cfg.calendar.calendar_id, "primary")

    def test_control_characters_in_client_secret_path_fall_back(self) -> None:
        self.write_config('[calendar]\nclient_secret = "bad\\u0000name.json"\n')
        cfg = self.load_warning("calendar.client_secret")
        self.assertEqual(cfg.calendar.client_secret_path, self.root / "google_client_secret.json")

    def test_calendar_table_of_wrong_type_falls_back(self) -> None:
        self.write_config('calendar = "on"\nactions = 3\n')
        cfg = self.load_warning("[calendar]", "[actions]")
        self.assertEqual(cfg.calendar.enabled, True)
        self.assertEqual(cfg.actions, ActionsConfig())

    def test_unknown_calendar_and_actions_keys_are_reported(self) -> None:
        self.write_config('[calendar]\nclient_id = "x"\n[actions]\nheadings = "A"\n')
        cfg = self.load_warning("calendar.client_id", "actions.headings")
        self.assertEqual(cfg.actions, ActionsConfig())

    def test_actions_heading_as_text_or_list(self) -> None:
        self.write_config('[actions]\nheading = " Needs your OK "\n')
        self.assertEqual(self.load_quietly().actions.headings, ("Needs your OK",))
        self.write_config('[actions]\nheading = ["Proposed actions", "proposed ACTIONS", "Todo"]\n')
        self.assertEqual(self.load_quietly().actions.headings, ("Proposed actions", "Todo"))

    def test_bad_actions_heading_falls_back(self) -> None:
        for value in ("[]", "3", '[1, ""]', '""'):
            with self.subTest(value=value):
                self.write_config(f"[actions]\nheading = {value}\n")
                cfg = self.load_warning("actions.heading")
                self.assertEqual(cfg.actions.headings, ("Proposed actions",))

    def test_mixed_actions_heading_keeps_valid_names(self) -> None:
        self.write_config('[actions]\nheading = ["Actions", 5]\n')
        cfg = self.load_warning("actions.heading")
        self.assertEqual(cfg.actions.headings, ("Actions",))

    # ---- [actions] link_hosts: the extra hosts a card's Open may open ----

    def test_link_hosts_default_is_empty(self) -> None:
        self.assertEqual(ActionsConfig().link_hosts, ())
        self.assertEqual(ActionsConfig(link_hosts=["a.example.edu"]).link_hosts, ("a.example.edu",))
        self.write_config('[actions]\nheading = "Proposed actions"\n')
        self.assertEqual(self.load_quietly().actions.link_hosts, ())

    def test_link_hosts_exact_and_wildcard(self) -> None:
        self.write_config('[actions]\nlink_hosts = [" Forms.Example.EDU ", "*.lms.example.edu", '
                          '"forms.example.edu", "xn--bcher-kva.example.com"]\n')
        cfg = self.load_quietly()
        self.assertEqual(cfg.actions.link_hosts,
                         ("forms.example.edu", "*.lms.example.edu", "xn--bcher-kva.example.com"))
        self.assertEqual(cfg.actions.headings, ("Proposed actions",))

    def test_bad_link_hosts_are_skipped_with_a_warning(self) -> None:
        for value in ('"example"', '"*.edu"', '"https://forms.example.edu"', '"forms.example.edu/x"',
                      '"x_y.example.edu"', '"*.*.example.edu"', '"-a.example.edu"', '"a..example.edu"', "5"):
            with self.subTest(value=value):
                self.write_config(f'[actions]\nlink_hosts = [{value}, "ok.example.edu"]\n')
                cfg = self.load_warning("actions.link_hosts")
                self.assertEqual(cfg.actions.link_hosts, ("ok.example.edu",))

    def test_link_hosts_as_one_string_or_a_wrong_type(self) -> None:
        self.write_config('[actions]\nlink_hosts = "forms.example.edu"\n')
        self.assertEqual(self.load_quietly().actions.link_hosts, ("forms.example.edu",))
        self.write_config("[actions]\nlink_hosts = 5\n")
        cfg = self.load_warning("actions.link_hosts")
        self.assertEqual(cfg.actions.link_hosts, ())

    def test_unknown_actions_keys_are_still_reported(self) -> None:
        self.write_config('[actions]\nlink_host = ["forms.example.edu"]\n')
        cfg = self.load_warning("actions.link_host")
        self.assertEqual(cfg.actions, ActionsConfig())

    # ---- [actions] undo_seconds: the hidden hold after Send / Cancel event (no countdown is shown) ----

    def test_undo_seconds_default_and_range(self) -> None:
        self.assertEqual(ActionsConfig().undo_seconds, 5)
        self.assertEqual(config.UNDO_SECONDS_RANGE, (3, 30))
        self.assertEqual(self.load_quietly().actions.undo_seconds, 5)
        for value, expected in (("3", 3), ("25", 25), ("30", 30), ("12.4", 12)):
            with self.subTest(value=value):
                self.write_config(f"[actions]\nundo_seconds = {value}\n")
                self.assertEqual(self.load_quietly().actions.undo_seconds, expected)

    def test_undo_seconds_outside_the_range_is_clamped_with_a_warning(self) -> None:
        for value, expected in (("1", 3), ("0", 3), ("31", 30), ("60", 30), ("3600", 30)):
            with self.subTest(value=value):
                self.write_config(f"[actions]\nundo_seconds = {value}\n")
                self.assertEqual(self.load_warning("actions.undo_seconds").actions.undo_seconds, expected)

    # ---- [actions] trusted_domains: recipients that need no NEW RECIPIENT confirmation ----

    def test_trusted_domains_default_is_empty(self) -> None:
        self.assertEqual(ActionsConfig().trusted_domains, ())
        self.assertEqual(ActionsConfig(trusted_domains=["example.edu"]).trusted_domains, ("example.edu",))
        self.assertEqual(self.load_quietly().actions.trusted_domains, ())

    def test_trusted_domains_are_casefolded_and_deduplicated(self) -> None:
        self.write_config('[actions]\ntrusted_domains = [" Example.EDU ", "@lab.example.com", "example.edu", '
                          '"mail.example-school.org"]\n')
        self.assertEqual(self.load_quietly().actions.trusted_domains,
                         ("example.edu", "lab.example.com", "mail.example-school.org"))
        self.write_config('[actions]\ntrusted_domains = "example.edu"\n')
        self.assertEqual(self.load_quietly().actions.trusted_domains, ("example.edu",))

    def test_bad_trusted_domains_are_skipped_with_a_warning(self) -> None:
        for value in ('"edu"', '"*.example.edu"', '"ana@example.edu"', '"https://example.edu"',
                      '"example.edu/x"', '"example..edu"', '"-x.example.edu"', '"example.123"',
                      '"exa mple.edu"', '"b\\u00fccher.example"', "5"):
            with self.subTest(value=value):
                self.write_config(f'[actions]\ntrusted_domains = [{value}, "ok.example.edu"]\n')
                cfg = self.load_warning("actions.trusted_domains")
                self.assertEqual(cfg.actions.trusted_domains, ("ok.example.edu",))

    def test_bad_undo_seconds_falls_back(self) -> None:
        for value in ('"ten"', "true", "-5", "nan"):
            with self.subTest(value=value):
                self.write_config(f"[actions]\nundo_seconds = {value}\n")
                self.assertEqual(self.load_warning("actions.undo_seconds").actions.undo_seconds, 5)


class AccountsConfigTests(ProjectTestCase):
    """[accounts.<alias>]: which service acts for an alias and what it may do."""

    def test_default_is_the_personal_account_only(self) -> None:
        cfg = self.load_quietly()
        self.assertEqual(dict(cfg.accounts), {"personal": AccountConfig("personal")})
        self.assertEqual(AccountConfig("personal"),
                         AccountConfig("personal", backend="google", features=("calendar",)))
        self.assertEqual(AccountConfig("x", features=["calendar"]).features, ("calendar",))
        hash(cfg)   # the frozen Config stays hashable

    def test_two_accounts(self) -> None:
        self.write_config('[accounts.personal]\nbackend = "google"\nfeatures = ["calendar"]\n'
                          '[accounts.work]\nbackend = "google"\nfeatures = ["calendar"]\n')
        cfg = self.load_quietly()
        self.assertEqual(list(cfg.accounts), ["personal", "work"])
        self.assertEqual(cfg.accounts["work"], AccountConfig("work"))
        with self.assertRaises(TypeError):
            cfg.accounts["school"] = AccountConfig("school")   # type: ignore[index]

    def test_missing_keys_take_the_defaults(self) -> None:
        self.write_config("[accounts.work]\n")
        self.assertEqual(dict(self.load_quietly().accounts), {"work": AccountConfig("work")})

    def test_composio_backend_is_accepted(self) -> None:
        self.write_config('[accounts.work]\nbackend = "Composio"\n')
        self.assertEqual(self.load_quietly().accounts["work"].backend, "composio")

    def test_unknown_backend_falls_back_to_google(self) -> None:
        self.write_config('[accounts.work]\nbackend = "zapier"\n')
        self.assertEqual(self.load_warning("accounts.work.backend").accounts["work"].backend, "google")

    def test_unknown_feature_is_skipped_and_duplicates_dropped(self) -> None:
        self.write_config('[accounts.work]\nfeatures = ["calendar", "gmail_send", "Calendar", "GMAIL_SEND", '
                          '"telepathy", "gmail_modify"]\n')
        cfg = self.load_warning("accounts.work.features", "telepathy", "gmail_modify")
        self.assertEqual(cfg.accounts["work"].features, ("calendar", "gmail_send"))

    def test_gmail_read_is_a_feature(self) -> None:
        self.write_config('[accounts.work]\nfeatures = ["calendar", "GMAIL_READ", "gmail_send", "gmail_read"]\n')
        self.assertEqual(self.load_quietly().accounts["work"].features, ("calendar", "gmail_read", "gmail_send"))

    def test_gmail_send_alone(self) -> None:
        self.write_config('[accounts.work]\nfeatures = ["gmail_send"]\n')
        self.assertEqual(self.load_quietly().accounts["work"].features, ("gmail_send",))
        self.assertEqual(config.ACCOUNT_FEATURES, ("calendar", "gmail_send", "gmail_read"))

    def test_no_features(self) -> None:
        self.write_config("[accounts.work]\nfeatures = []\n")
        self.assertEqual(self.load_quietly().accounts["work"].features, ())

    def test_bad_alias_is_skipped(self) -> None:
        self.write_config('[accounts."Work Mail"]\nbackend = "google"\n[accounts.work]\n')
        cfg = self.load_warning("Work Mail", "not an account name")
        self.assertEqual(list(cfg.accounts), ["work"])

    def test_alias_is_casefolded_and_a_repeat_skipped(self) -> None:
        self.write_config('[accounts.Work]\n[accounts.work]\nbackend = "composio"\n')
        cfg = self.load_warning("repeats the account name work")
        self.assertEqual(dict(cfg.accounts), {"work": AccountConfig("work")})

    def test_unknown_account_keys_are_reported(self) -> None:
        self.write_config('[accounts.work]\nemail = "someone@example.edu"\n')
        cfg = self.load_warning("accounts.work.email")
        self.assertEqual(cfg.accounts["work"], AccountConfig("work"))

    def test_an_account_that_is_not_a_table_is_skipped(self) -> None:
        self.write_config('[accounts]\nwork = "google"\npersonal = {}\n')
        cfg = self.load_warning("accounts.work should be a table")
        self.assertEqual(list(cfg.accounts), ["personal"])

    def test_no_usable_account_falls_back_to_personal(self) -> None:
        for text in ('accounts = "work"\n', "[accounts]\n", '[accounts."!"]\n'):
            with self.subTest(text=text):
                self.write_config(text)
                cfg = self.load_warning("accounts")
                self.assertEqual(dict(cfg.accounts), {"personal": AccountConfig("personal")})


class ScheduleHotkeyAgendaConfigTests(ProjectTestCase):
    def test_schedule_times_are_normalized(self) -> None:
        self.write_config('[schedule]\nam = " 9:05 "\npm = "00:00"\n')
        cfg = self.load_quietly()
        self.assertEqual(cfg.schedule, ScheduleConfig(am="09:05", pm="00:00"))
        self.assertEqual(cfg.schedule.slots, {"AM": "09:05", "PM": "00:00"})

    def test_bad_schedule_times_fall_back(self) -> None:
        for value in ('"24:00"', '"10:60"', '"10.12"', '"10:12 PM"', "1012", '""', "true"):
            with self.subTest(value=value):
                self.write_config(f"[schedule]\nam = {value}\npm = {value}\n")
                cfg = self.load_warning("schedule.am", "schedule.pm")
                self.assertEqual(cfg.schedule, ScheduleConfig())

    def test_hotkey_combo_is_canonical(self) -> None:
        for raw, expected in (("CTRL+ALT+J", "ctrl+alt+j"), ("alt + ctrl + j", "ctrl+alt+j"),
                              ("win+shift+5", "shift+win+5"), ("F24", "f24"),
                              ("control+windows+k", "ctrl+win+k")):
            with self.subTest(raw=raw):
                self.write_config(f"[hotkey]\ncombo = {json.dumps(raw)}\n")
                self.assertEqual(self.load_quietly().hotkey.combo, expected)

    def test_bad_hotkey_values_fall_back(self) -> None:
        for value in ('"j"', '"shift+j"', '"ctrl+alt"', '"ctrl+alt+jj"', '"ctrl++j"', '"ctrl+ctrl+j"',
                      '"ctrl+alt+F25"', '""', "5"):
            with self.subTest(value=value):
                self.write_config(f'[hotkey]\nenabled = "yes"\ncombo = {value}\n')
                cfg = self.load_warning("hotkey.enabled", "hotkey.combo")
                self.assertEqual(cfg.hotkey, HotkeyConfig())

    def test_agenda_numbers_are_clamped(self) -> None:
        self.write_config("[agenda]\nevening_from_hour = 30\ndeadline_days = 0\n")
        cfg = self.load_warning("agenda.evening_from_hour", "agenda.deadline_days")
        self.assertEqual(cfg.agenda.evening_from_hour, 24)
        self.assertEqual(cfg.agenda.deadline_days, 1)
        self.write_config("[agenda]\nevening_from_hour = 0\ndeadline_days = 60\n")
        cfg = self.load_quietly()
        self.assertEqual((cfg.agenda.evening_from_hour, cfg.agenda.deadline_days), (0, 60))

    def test_agenda_lists(self) -> None:
        self.write_config('[agenda]\ncalendars = " work@example.com "\ndeadline_keywords = []\n')
        cfg = self.load_quietly()
        self.assertEqual(cfg.agenda.calendars, ("work@example.com",))
        self.assertEqual(cfg.agenda.deadline_keywords, ())
        self.write_config('[agenda]\ncalendars = []\ndeadline_keywords = "due"\n')
        cfg = self.load_warning("agenda.calendars")
        self.assertEqual(cfg.agenda.calendars, ("primary",))
        self.assertEqual(cfg.agenda.deadline_keywords, ("due",))

    def test_bad_agenda_calendar_ids_are_skipped(self) -> None:
        self.write_config('[agenda]\ncalendars = ["has space", "primary", 3]\n')
        cfg = self.load_warning("agenda.calendars")
        self.assertEqual(cfg.agenda.calendars, ("primary",))
        self.write_config('[agenda]\ncalendars = ["has space"]\n')
        cfg = self.load_warning("agenda.calendars")
        self.assertEqual(cfg.agenda.calendars, ("primary",))

    def test_default_deadline_keywords(self) -> None:
        self.assertEqual(AgendaConfig().deadline_keywords, DEFAULT_DEADLINE_KEYWORDS)
        self.assertIn("lab report", DEFAULT_DEADLINE_KEYWORDS)

    def test_unknown_keys_and_wrong_tables_are_reported(self) -> None:
        self.write_config('[schedule]\nnoon = "12:00"\n[hotkey]\nkey = "j"\nagenda = 3\n')
        cfg = self.load_warning("schedule.noon", "hotkey.key", "hotkey.agenda")
        self.assertEqual(cfg.schedule, ScheduleConfig())
        self.write_config('schedule = "10:12"\nhotkey = true\nagenda = []\n')
        cfg = self.load_warning("[schedule]", "[hotkey]", "[agenda]")
        self.assertEqual((cfg.schedule, cfg.hotkey, cfg.agenda),
                         (ScheduleConfig(), HotkeyConfig(), AgendaConfig()))

    def test_display_clock(self) -> None:
        for raw, expected in (('"12h"', "12h"), ('"24h"', "24h"), ('" 24H "', "24h"), ('"12H"', "12h")):
            with self.subTest(raw=raw):
                self.write_config(f"[display]\nclock = {raw}\n")
                cfg = self.load_quietly()
                self.assertEqual(cfg.display, DisplayConfig(clock=expected))
                self.assertEqual(cfg.display.hour24, expected == "24h")

    def test_missing_display_table_is_12_hour(self) -> None:
        self.write_config("[agenda]\ndeadline_days = 7\n")
        self.assertEqual(self.load_quietly().display, DisplayConfig(clock="12h"))
        self.write_config("[display]\n")
        self.assertEqual(self.load_quietly().display, DisplayConfig(clock="12h"))

    def test_bad_display_clock_falls_back_to_12_hour(self) -> None:
        for value in ('"24"', '"12-hour"', '"24 h"', '"AM/PM"', '""', "24", "true", '["24h"]'):
            with self.subTest(value=value):
                self.write_config(f"[display]\nclock = {value}\n")
                cfg = self.load_warning("display.clock", '"12h" or "24h"', "using '12h'")
                self.assertEqual(cfg.display, DisplayConfig())

    def test_display_unknown_keys_and_wrong_table_are_reported(self) -> None:
        self.write_config('[display]\nclock = "24h"\nseconds = true\n')
        cfg = self.load_warning("display.seconds")
        self.assertEqual(cfg.display, DisplayConfig(clock="24h"))
        self.write_config('display = "24h"\n')
        cfg = self.load_warning("[display]")
        self.assertEqual(cfg.display, DisplayConfig())


class UiConfigTests(ProjectTestCase):
    """``[ui] motion`` (the motion prototype): lively (default), calm or off; anything else is the default."""

    def test_motion_values(self) -> None:
        for raw, expected in (('"lively"', "lively"), ('"calm"', "calm"), ('"off"', "off"), ('" Calm "', "calm"),
                              ('"OFF"', "off"), ("true", "lively"), ("false", "off")):
            with self.subTest(raw=raw):
                self.write_config(f"[ui]\nmotion = {raw}\n")
                self.assertEqual(self.load_quietly().ui, config.UiConfig(motion=expected))

    def test_missing_ui_table_is_lively(self) -> None:
        self.write_config('[display]\nclock = "24h"\n')
        self.assertEqual(self.load_quietly().ui.motion, "lively")
        self.write_config("[ui]\n")
        self.assertEqual(self.load_quietly().ui.motion, "lively")
        self.assertEqual(config.UiConfig(), config.UiConfig(motion="lively"))

    def test_bad_motion_falls_back_to_lively(self) -> None:
        for value in ('"fast"', '""', "3", '["calm"]', '"full"'):
            with self.subTest(value=value):
                self.write_config(f"[ui]\nmotion = {value}\n")
                cfg = self.load_warning("ui.motion")
                self.assertEqual(cfg.ui, config.UiConfig())

    def test_unknown_ui_keys_and_wrong_table_are_reported(self) -> None:
        self.write_config('[ui]\nmotion = "calm"\nspeed = 2\n')
        cfg = self.load_warning("ui.speed")
        self.assertEqual(cfg.ui.motion, "calm")
        self.write_config('ui = "calm"\n')
        self.assertEqual(self.load_warning("[ui]").ui, config.UiConfig())

    def test_the_shipped_file_explains_motion(self) -> None:
        raw = (config.PROJECT_ROOT / "config.toml").read_text(encoding="ascii")
        section = raw.split("[ui]", 1)[1].split("\n[", 1)[0]
        for word in ('"lively"', '"calm"', '"off"', "Show animations in Windows", 'motion = "lively"'):
            self.assertIn(word, section)


class AskConfigTests(ProjectTestCase):
    def test_full_ask_table(self) -> None:
        self.write_config('[ask]\nenabled = true\nmodel = "haiku"\ntimeout_seconds = 120\nmax_turns = 3\n'
                          'max_per_hour = 5\nmax_per_day = 12\ndays_back = 0\ndays_ahead = 30\n'
                          'calendars = ["primary", "team@group.calendar.google.com"]\nmax_cards = 4\n'
                          'hardened_flags = false\nread_mail = false\n')
        self.assertEqual(self.load_quietly().ask, AskConfig(
            enabled=True, model="haiku", timeout_seconds=120, max_turns=3, max_per_hour=5, max_per_day=12,
            days_back=0, days_ahead=30, calendars=("primary", "team@group.calendar.google.com"), max_cards=4,
            hardened_flags=False, read_mail=False))

    def test_out_of_range_numbers_are_clamped(self) -> None:
        self.write_config('[ask]\ntimeout_seconds = 5\nmax_turns = 50\nmax_per_hour = 0\nmax_per_day = 9999\n'
                          'days_back = 30\ndays_ahead = 0\nmax_cards = 20\n')
        cfg = self.load_warning("ask.timeout_seconds", "ask.max_turns", "ask.max_per_hour", "ask.max_cards")
        self.assertEqual((cfg.ask.timeout_seconds, cfg.ask.max_turns, cfg.ask.max_per_hour, cfg.ask.max_per_day,
                          cfg.ask.days_back, cfg.ask.days_ahead, cfg.ask.max_cards), (30, 8, 1, 500, 7, 1, 8))

    def test_bad_values_fall_back_to_the_defaults(self) -> None:
        # A model that could be read as a command-line option, or with spaces, is refused.
        for model in ('"--bare"', '"-p"', '"son net"', '""', '"sonnet; rm"', "3", '"a\\u0000b"'):
            with self.subTest(model=model):
                self.write_config(f"[ask]\nmodel = {model}\n")
                self.assertEqual(self.load_warning("ask.model").ask.model, "sonnet")
        self.write_config('[ask]\nenabled = "yes"\nread_mail = 1\nhardened_flags = "no"\n')
        cfg = self.load_warning("ask.enabled", "ask.read_mail", "ask.hardened_flags")
        self.assertEqual(cfg.ask, AskConfig())
        self.write_config('[ask]\ncalendars = ["--settings", "bad id"]\n')
        self.assertEqual(self.load_warning("ask.calendars").ask.calendars, ("primary",))

    def test_full_model_names_are_accepted(self) -> None:
        for model in ("opus", "claude-sonnet-4-5", "claude-sonnet-4-5[1m]", "haiku"):
            with self.subTest(model=model):
                self.write_config(f'[ask]\nmodel = "{model}"\n')
                self.assertEqual(self.load_quietly().ask.model, model)

    def test_unknown_ask_key_is_reported(self) -> None:
        self.write_config('[ask]\nenabled = true\napi_key = "sk-not-used"\n')
        cfg = self.load_warning("ask.api_key")
        self.assertTrue(cfg.ask.enabled)


class ResearchConfigTests(ProjectTestCase):
    def test_defaults(self) -> None:
        self.assertEqual(ResearchConfig(), ResearchConfig(
            enabled=True, planner_may_ask=True, model="sonnet", timeout_seconds=120, max_searches=4, max_fetches=4,
            max_per_hour=6, max_per_day=20, max_sources=5))
        self.assertEqual(ResearchConfig().max_turns, 10)
        self.assertEqual(ResearchConfig(max_searches=2, max_fetches=0).max_turns, 4)
        self.assertEqual(self.load_quietly().research, ResearchConfig())
        self.assertEqual(config.RESEARCH_RANGES, {
            "timeout_seconds": (30, 300), "max_searches": (1, 8), "max_fetches": (0, 8), "max_per_hour": (1, 30),
            "max_per_day": (1, 100), "max_sources": (1, 6)})

    def test_full_research_table(self) -> None:
        self.write_config('[research]\nenabled = false\nplanner_may_ask = false\nmodel = "haiku"\n'
                          'timeout_seconds = 60\nmax_searches = 2\nmax_fetches = 0\nmax_per_hour = 3\n'
                          'max_per_day = 9\nmax_sources = 2\n')
        self.assertEqual(self.load_quietly().research, ResearchConfig(
            enabled=False, planner_may_ask=False, model="haiku", timeout_seconds=60, max_searches=2, max_fetches=0,
            max_per_hour=3, max_per_day=9, max_sources=2))

    def test_out_of_range_numbers_are_clamped(self) -> None:
        self.write_config('[research]\ntimeout_seconds = 5\nmax_searches = 0\nmax_fetches = 50\n'
                          'max_per_hour = 999\nmax_per_day = 0\nmax_sources = 9\n')
        cfg = self.load_warning("research.timeout_seconds", "research.max_searches", "research.max_fetches",
                                "research.max_per_hour", "research.max_per_day", "research.max_sources")
        research = cfg.research
        self.assertEqual((research.timeout_seconds, research.max_searches, research.max_fetches,
                          research.max_per_hour, research.max_per_day, research.max_sources), (30, 1, 8, 30, 1, 6))
        self.write_config("[research]\nmax_fetches = -1\n")
        self.assertEqual(self.load_warning("research.max_fetches").research.max_fetches, 4)

    def test_bad_values_fall_back_to_the_defaults(self) -> None:
        for model in ('"--bare"', '"-p"', '"son net"', '""', "3"):
            with self.subTest(model=model):
                self.write_config(f"[research]\nmodel = {model}\n")
                self.assertEqual(self.load_warning("research.model").research.model, "sonnet")
        self.write_config('[research]\nenabled = "yes"\nplanner_may_ask = 1\nmax_searches = "4"\n')
        cfg = self.load_warning("research.enabled", "research.planner_may_ask", "research.max_searches")
        self.assertEqual(cfg.research, ResearchConfig())
        self.write_config('research = "on"\n')
        self.assertEqual(self.load_warning("[research]").research, ResearchConfig())

    def test_unknown_research_key_is_reported(self) -> None:
        self.write_config('[research]\nenabled = true\nallowed_domains = ["example.org"]\n')
        cfg = self.load_warning("research.allowed_domains")
        self.assertTrue(cfg.research.enabled)

    def test_env_overrides_enabled_both_ways(self) -> None:
        self.write_config("[research]\nenabled = false\n")
        for value in ("true", "1", "yes", "on", " TRUE "):
            with self.subTest(value=value):
                self.assertTrue(self.load_quietly(JARVIS_RESEARCH_ENABLED=value).research.enabled)
        self.write_config("[research]\nenabled = true\n")
        for value in ("false", "0", "no", "off"):
            with self.subTest(value=value):
                self.assertFalse(self.load_quietly(JARVIS_RESEARCH_ENABLED=value).research.enabled)
        self.assertTrue(self.load_quietly(JARVIS_RESEARCH_ENABLED="").research.enabled)
        cfg = self.load_warning("JARVIS_RESEARCH_ENABLED", JARVIS_RESEARCH_ENABLED="maybe")
        self.assertTrue(cfg.research.enabled)
        # The Ask switch does not move the research switch, nor the other way round.
        cfg = self.load_quietly(JARVIS_ASK_ENABLED="true", JARVIS_RESEARCH_ENABLED="false")
        self.assertEqual((cfg.ask.enabled, cfg.research.enabled), (True, False))


class AssistantConfigTests(ProjectTestCase):
    def test_defaults_without_the_table(self) -> None:
        self.write_config("")
        cfg = self.load_quietly()
        self.assertEqual(cfg.assistant, AssistantConfig(
            speak=True, address="sir", greet=True, greeting_context=True, speak_replies=True,
            speak_results=True, scheduled_prompt=False))

    def test_every_key(self) -> None:
        self.write_config('[assistant]\nspeak = false\naddress = "boss"\ngreet = false\n'
                          'greeting_context = false\nspeak_replies = false\nspeak_results = false\n'
                          'scheduled_prompt = true\n')
        self.assertEqual(self.load_quietly().assistant, AssistantConfig(
            speak=False, address="boss", greet=False, greeting_context=False, speak_replies=False,
            speak_results=False, scheduled_prompt=True))

    def test_addresses(self) -> None:
        for value, expected in (('"sir"', "sir"), ('" Ma\'am "', "Ma'am"), ('"Mr. Smith"', "Mr. Smith"),
                                ('"Dr Jones-Smith"', "Dr Jones-Smith"), ('""', ""), ('"  "', "")):
            with self.subTest(value=value):
                self.write_config(f"[assistant]\naddress = {value}\n")
                self.assertEqual(self.load_quietly().assistant.address, expected)

    def test_bad_addresses_fall_back_to_sir(self) -> None:
        for value in ('"sir!"', '"-sir"', '"a.b@example.com"', '"twenty-one characters"', '"1st"',
                      '"<b>sir</b>"', '"sir\\u0000"', "3", "true", '["sir"]'):
            with self.subTest(value=value):
                self.write_config(f"[assistant]\naddress = {value}\n")
                self.assertEqual(self.load_warning("assistant.address").assistant.address, "sir")

    def test_bad_flags_fall_back(self) -> None:
        self.write_config('[assistant]\nspeak = "no"\ngreet = 0\nscheduled_prompt = "yes"\n')
        cfg = self.load_warning("assistant.speak", "assistant.greet", "assistant.scheduled_prompt")
        self.assertEqual(cfg.assistant, AssistantConfig())

    def test_unknown_key_and_wrong_table_are_reported(self) -> None:
        self.write_config('[assistant]\nspeak = false\nvoice = "Ryan"\n')
        cfg = self.load_warning("assistant.voice")
        self.assertFalse(cfg.assistant.speak)
        self.write_config('assistant = "on"\n')
        self.assertEqual(self.load_warning("[assistant]").assistant, AssistantConfig())

    def test_address_pattern(self) -> None:
        self.assertTrue(config.ADDRESS_RE.fullmatch("sir"))
        self.assertTrue(config.ADDRESS_RE.fullmatch("A" * 20))
        self.assertIsNone(config.ADDRESS_RE.fullmatch("A" * 21))


class LiveConfigTests(ProjectTestCase):
    def test_defaults_without_the_table(self) -> None:
        self.write_config("")
        cfg = self.load_quietly()
        self.assertEqual(cfg.live, LiveConfig(enabled=True, auto_open="tab", background=True, text=True))
        self.assertEqual(config.LIVE_OPEN_CHOICES, ("tab", "window", "off"))

    def test_every_key(self) -> None:
        self.write_config('[live]\nenabled = false\nauto_open = "window"\nbackground = false\ntext = false\n')
        self.assertEqual(self.load_quietly().live, LiveConfig(enabled=False, auto_open="window", background=False,
                                                              text=False))

    def test_auto_open_values(self) -> None:
        for value, expected in (("true", "tab"), ("false", "off"), ('"tab"', "tab"), ('" Window "', "window"),
                                ('"OFF"', "off")):
            with self.subTest(value=value):
                self.write_config(f"[live]\nauto_open = {value}\n")
                self.assertEqual(self.load_quietly().live.auto_open, expected)

    def test_bad_values_fall_back(self) -> None:
        for value in ('"popup"', "1", '["tab"]', '""'):
            with self.subTest(value=value):
                self.write_config(f"[live]\nauto_open = {value}\n")
                self.assertEqual(self.load_warning("live.auto_open").live.auto_open, "tab")
        self.write_config('[live]\nenabled = "no"\nbackground = 0\ntext = "false"\n')
        cfg = self.load_warning("live.enabled", "live.background", "live.text")
        self.assertEqual(cfg.live, LiveConfig())

    def test_unknown_key_and_wrong_table_are_reported(self) -> None:
        self.write_config('[live]\nenabled = false\nexport = true\n')
        cfg = self.load_warning("live.export")
        self.assertFalse(cfg.live.enabled)
        self.write_config('live = "on"\n')
        self.assertEqual(self.load_warning("[live]").live, LiveConfig())

    def test_pictures_and_page_snapshots(self) -> None:
        self.write_config("")
        cfg = self.load_quietly()
        self.assertTrue(cfg.live.pictures)
        self.assertTrue(cfg.live.page_snapshots)
        self.write_config("[live]\npictures = false\npage_snapshots = false\n")
        cfg = self.load_quietly()
        self.assertEqual((cfg.live.pictures, cfg.live.page_snapshots), (False, False))
        self.write_config("[live]\npictures = false\n")
        self.assertEqual((self.load_quietly().live.pictures, self.load_quietly().live.page_snapshots), (False, True))
        self.write_config('[live]\npictures = "no"\npage_snapshots = 0\n')
        cfg = self.load_warning("live.pictures", "live.page_snapshots")
        self.assertEqual((cfg.live.pictures, cfg.live.page_snapshots), (True, True))

    def test_the_shipped_file_explains_both_picture_keys(self) -> None:
        raw = (config.PROJECT_ROOT / "config.toml").read_bytes()
        live_part = raw.decode("ascii").split("[live]", 1)[1].split("\n[", 1)[0]
        self.assertIn("\npictures = true\n", live_part)
        self.assertIn("\npage_snapshots = true\n", live_part)
        words = " ".join(live_part.replace("\n# ", " ").split())   # the comments as one text
        self.assertIn("no email HTML, no images from the web, no page opened for them", words)
        self.assertIn("each web search of a research drawn as its results page", words)
        self.assertIn("each page a research read, and each page its answer cites that a search listed, is opened "
                      "(again, for a page it read)", words)
        self.assertIn("the site sees your IP address, also a cited site the research never read", words)
        self.assertIn("false: no page is opened", words)


# --------------------------------------------------------------------------
# Environment and .env
# --------------------------------------------------------------------------

class EnvironmentTests(ProjectTestCase):
    def test_page_id_override_dashed_uuid(self) -> None:
        self.assertEqual(self.load_quietly(BRIEFING_PAGE_ID=DASHED_PAGE_ID).page_id, PAGE_ID)

    def test_page_id_override_notion_url(self) -> None:
        url = f"https://www.notion.so/myspace/Daily-Briefing-{PAGE_ID}?pvs=4"
        cfg = self.load_quietly(BRIEFING_PAGE_ID=url)
        self.assertEqual(cfg.page_id, PAGE_ID)
        self.assertEqual(cfg.notion_url, f"https://www.notion.so/{PAGE_ID}")

    def test_page_id_override_quoted(self) -> None:
        self.assertEqual(self.load_quietly(BRIEFING_PAGE_ID=f' "{PAGE_ID}" ').page_id, PAGE_ID)

    def test_page_id_plain(self) -> None:
        cfg = self.load_quietly(BRIEFING_PAGE_ID=PAGE_ID)
        self.assertEqual(cfg.page_id, PAGE_ID)
        self.assertEqual(cfg.notion_url, f"https://www.notion.so/{PAGE_ID}")

    def test_there_is_no_default_page(self) -> None:
        self.assertEqual(DEFAULT_PAGE_ID, "")
        self.assertEqual(config.DEFAULT_PAGE_ID, "")

    def test_invalid_page_id_counts_as_missing(self) -> None:
        with self.assertLogs(CONFIG_LOGGER, level="WARNING") as captured:
            cfg = self.load(BRIEFING_PAGE_ID="definitely-not-a-page")
        self.assertEqual(cfg.page_id, "")
        output = "\n".join(captured.output)
        self.assertIn("BRIEFING_PAGE_ID", output)
        self.assertNotIn("definitely-not-a-page", output)

    def test_missing_or_blank_page_id_is_empty_and_only_noted(self) -> None:
        for raw in (None, "", "   ", '""'):
            with self.subTest(raw=raw):
                overrides = {} if raw is None else {"BRIEFING_PAGE_ID": raw}
                with self.assertLogs(CONFIG_LOGGER, level="INFO") as captured:
                    cfg = self.load(**overrides)
                self.assertEqual(cfg.page_id, "")
                self.assertEqual([r.getMessage() for r in captured.records
                                  if r.levelno >= logging.WARNING], [])
                self.assertIn("BRIEFING_PAGE_ID is not set", "\n".join(captured.output))

    def test_token_is_stripped_of_whitespace_and_quotes(self) -> None:
        for raw in (f"  {FAKE_TOKEN}\n", f'"{FAKE_TOKEN}"', f"'{FAKE_TOKEN}'",
                    f' " {FAKE_TOKEN} " ', f"\t'{FAKE_TOKEN}'\r\n"):
            with self.subTest(raw=raw):
                self.assertEqual(self.load(NOTION_TOKEN=raw).notion_token, FAKE_TOKEN)

    def test_loaded_token_is_registered_for_redaction(self) -> None:
        token = "plain-token-registered-by-load-config-77"
        self.load(NOTION_TOKEN=token)
        self.assertEqual(redact(f"Bearer {token}!"), f"Bearer {REDACTED}!")

    def test_given_environ_does_not_touch_dotenv_or_os_environ(self) -> None:
        (self.root / ".env").write_text(
            "NOTION_TOKEN=token-from-dotenv-should-not-load\n"
            "BRIEFING_PAGE_ID_TEST_MARKER=1\n", encoding="utf-8")
        before = dict(os.environ)
        cfg = load_config(self.root, environ={"LOCALAPPDATA": str(self.root)})
        self.assertEqual(cfg.notion_token, "")
        self.assertEqual(dict(os.environ), before)

    def _load_with_dotenv(self, content: str, encoding: str,
                          preset: dict[str, str] | None = None) -> Config:
        (self.root / ".env").write_text(content, encoding=encoding)
        with mock.patch.dict(os.environ):   # restored on exit
            for name in ("NOTION_TOKEN", "BRIEFING_PAGE_ID"):
                os.environ.pop(name, None)
            os.environ.update(preset or {})
            return load_config(self.root)

    def test_dotenv_is_loaded_when_environ_is_none(self) -> None:
        content = f'NOTION_TOKEN="{FAKE_TOKEN}"\nBRIEFING_PAGE_ID={DASHED_PAGE_ID}\n'
        before = dict(os.environ)
        for encoding in ("utf-8", "utf-8-sig", "utf-16"):
            with self.subTest(encoding=encoding):
                cfg = self._load_with_dotenv(content, encoding)
                self.assertEqual(cfg.notion_token, FAKE_TOKEN)
                self.assertEqual(cfg.page_id, PAGE_ID)
        self.assertEqual(dict(os.environ), before)

    def test_environment_wins_over_dotenv(self) -> None:
        cfg = self._load_with_dotenv(
            "NOTION_TOKEN=token-from-dotenv-loses\n", "utf-8",
            preset={"NOTION_TOKEN": "token-from-environment-wins"})
        self.assertEqual(cfg.notion_token, "token-from-environment-wins")


class ReprTests(ProjectTestCase):
    def test_repr_and_str_never_contain_token(self) -> None:
        cfg = self.load_quietly(BRIEFING_PAGE_ID=PAGE_ID)
        for text in (repr(cfg), str(cfg), f"{cfg}", "%s" % (cfg,), "%r" % (cfg,)):
            self.assertNotIn(FAKE_TOKEN, text)
            self.assertNotIn("notion_token", text)
        self.assertIn(PAGE_ID, repr(cfg))


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

class RedactingFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        register_secret(PLAIN_SECRET)
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self.handler.addFilter(RedactingFilter())
        self.log = logging.getLogger("briefing_reader.tests.redaction")
        self.log.addHandler(self.handler)
        self.log.setLevel(logging.DEBUG)
        self.log.propagate = False
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        self.log.removeHandler(self.handler)
        self.handler.close()
        self.log.setLevel(logging.NOTSET)
        self.log.propagate = True

    def output(self) -> str:
        return self.stream.getvalue()

    def test_secret_in_message(self) -> None:
        self.log.info(f"token is {PLAIN_SECRET}")
        self.assertIn(f"token is {REDACTED}", self.output())
        self.assertNotIn(PLAIN_SECRET, self.output())

    def test_secret_in_percent_args(self) -> None:
        self.log.warning("token=%s headers=%r", PLAIN_SECRET,
                         {"Authorization": f"Bearer {PLAIN_SECRET}"})
        self.log.info("%(tok)s", {"tok": PLAIN_SECRET})
        self.assertNotIn(PLAIN_SECRET, self.output())
        self.assertIn(f"token={REDACTED}", self.output())
        self.assertIn(f"Bearer {REDACTED}", self.output())

    def test_secret_in_exception_traceback(self) -> None:
        try:
            try:
                raise ValueError(f"inner {PLAIN_SECRET}")
            except ValueError as inner:
                raise RuntimeError(f"request failed for {PLAIN_SECRET}") from inner
        except RuntimeError:
            self.log.exception("Fetch failed")
        out = self.output()
        self.assertIn("Traceback", out)
        self.assertIn("RuntimeError: request failed for " + REDACTED, out)
        self.assertIn("ValueError: inner " + REDACTED, out)
        self.assertNotIn(PLAIN_SECRET, out)

    def test_unregistered_token_shapes(self) -> None:
        ntn = "ntn_" + "A1b2C3d4E5" * 3
        secret = "secret_" + "x9" * 20
        self.log.info("tokens %s and %s", ntn, secret)
        self.log.info(f"inline {ntn}.")
        out = self.output()
        self.assertNotIn(ntn, out)
        self.assertNotIn(secret, out)
        self.assertIn(f"tokens {REDACTED} and {REDACTED}", out)
        self.assertIn(f"inline {REDACTED}.", out)

    def test_ordinary_text_is_untouched(self) -> None:
        self.log.info("ntn_short secret_ and a normal line 100%")
        self.assertIn("ntn_short secret_ and a normal line 100%", self.output())

    def test_short_secrets_are_ignored(self) -> None:
        register_secret("")
        register_secret("abc1234")
        register_secret(None)  # type: ignore[arg-type]
        self.log.info("value abc1234")
        self.assertIn("value abc1234", self.output())

    def test_secret_containing_another_is_fully_masked(self) -> None:
        longer = PLAIN_SECRET + "-and-more-suffix"
        register_secret(longer)
        self.log.info(longer)
        self.assertEqual(self.output().strip(), f"INFO {REDACTED}")

    def test_escaped_forms_of_a_secret_are_masked(self) -> None:
        odd = "odd-secret\x0bwith\"quote-for-redaction-tests"
        register_secret(odd)
        self.log.info("header value: %r", f"Bearer {odd}")
        self.log.info('{"token": %s}', json.dumps(odd))
        out = self.output()
        self.assertNotIn("quote-for-redaction-tests", out)
        self.assertEqual(out.count(REDACTED), 2)

    def test_filter_rewrites_record(self) -> None:
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "x %s",
                                   (PLAIN_SECRET,), None)
        flt = RedactingFilter()
        self.assertTrue(flt.filter(record))
        self.assertIsNone(record.args)
        self.assertEqual(record.msg, f"x {REDACTED}")
        self.assertTrue(flt.filter(record))   # second handler: unchanged
        self.assertEqual(record.getMessage(), f"x {REDACTED}")

    def test_bad_format_args_do_not_crash_or_leak(self) -> None:
        self.log.info("%d items", f"oops {PLAIN_SECRET}")
        self.assertIn(REDACTED, self.output())
        self.assertNotIn(PLAIN_SECRET, self.output())


# --------------------------------------------------------------------------
# setup_logging
# --------------------------------------------------------------------------

def _snapshot_logging() -> tuple[Any, ...]:
    root = logging.getLogger()
    levels = {name: logging.getLogger(name).level
              for name in (*config._QUIET_LOGGERS, *config._SILENT_LOGGERS)}
    return (list(root.handlers), root.level, sys.excepthook, threading.excepthook, levels)


def _restore_logging(snapshot: tuple[Any, ...]) -> None:
    handlers, level, excepthook, thread_hook, levels = snapshot
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)
    sys.excepthook = excepthook
    threading.excepthook = thread_hook
    for name, value in levels.items():
        logging.getLogger(name).setLevel(value)


class SetupLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.log_dir = self.tmp / "nested" / "logs"
        self.stderr = io.StringIO()
        stderr_patch = mock.patch.object(sys, "stderr", self.stderr)
        stderr_patch.start()
        self.addCleanup(stderr_patch.stop)
        # Registered last so it runs first: handlers are closed before the
        # temporary directory is deleted.
        self.addCleanup(_restore_logging, _snapshot_logging())

    def own_handlers(self) -> list[logging.Handler]:
        return [h for h in logging.getLogger().handlers
                if getattr(h, config._HANDLER_TAG, False)]

    def file_handler(self) -> logging.handlers.RotatingFileHandler:
        handlers = [h for h in self.own_handlers()
                    if isinstance(h, logging.handlers.RotatingFileHandler)]
        self.assertEqual(len(handlers), 1)
        return handlers[0]

    def read_log(self, path: Path) -> str:
        for handler in self.own_handlers():
            handler.flush()
        return path.read_text(encoding="utf-8")

    def test_writes_to_given_directory(self) -> None:
        path = setup_logging(self.log_dir)
        self.assertTrue(_same_path(path, self.log_dir / "briefing-reader.log"))
        logging.getLogger("briefing_reader.test").info("hello from the test")
        text = self.read_log(path)
        self.assertIn("INFO    briefing_reader.test [MainThread] hello from the test", text)

    def test_rotation_settings(self) -> None:
        setup_logging(self.log_dir)
        handler = self.file_handler()
        self.assertEqual(handler.maxBytes, 1_000_000)
        self.assertEqual(handler.backupCount, 5)
        self.assertEqual(handler.encoding, "utf-8")
        for own in self.own_handlers():
            self.assertEqual(own.formatter._fmt, LOG_FORMAT)  # type: ignore[union-attr]
            self.assertTrue(any(isinstance(f, RedactingFilter) for f in own.filters))

    def test_file_rotates(self) -> None:
        path = setup_logging(self.log_dir)
        self.file_handler().maxBytes = 300
        for i in range(20):
            logging.getLogger("briefing_reader.test").info("line %d with some padding text", i)
        self.assertTrue((self.log_dir / "briefing-reader.log.1").exists())
        self.assertTrue(path.exists())

    def test_idempotent(self) -> None:
        before = len(logging.getLogger().handlers)
        setup_logging(self.log_dir)
        setup_logging(self.log_dir)
        self.assertEqual(len(self.own_handlers()), 2)   # file + stderr
        self.assertEqual(len(logging.getLogger().handlers), before + 2)
        self.file_handler()

        other = self.tmp / "other"
        path = setup_logging(other)
        self.assertEqual(len(self.own_handlers()), 2)
        self.assertTrue(_same_path(self.file_handler().baseFilename, path))
        self.assertTrue(_same_path(path, other / "briefing-reader.log"))

        logging.getLogger("briefing_reader.test").warning("only once")
        self.assertEqual(self.read_log(path).count("only once"), 1)
        self.assertEqual(self.stderr.getvalue().count("only once"), 1)

    def test_keep_open_false_closes_the_file_after_each_record(self) -> None:
        with mock.patch.object(sys, "stderr", None):
            path = setup_logging(self.log_dir, keep_open=False)
        handler = self.file_handler()
        self.assertIsNone(handler.stream)
        logging.getLogger("briefing_reader.test").info("agent line one")
        self.assertIsNone(handler.stream)
        # Nothing holds the file: another process may rename it (rotation) right away.
        moved = self.log_dir / "moved.log"
        os.replace(path, moved)
        self.assertIn("agent line one", moved.read_text(encoding="utf-8"))
        logging.getLogger("briefing_reader.test").info("agent line two")
        self.assertIn("agent line two", path.read_text(encoding="utf-8"))
        self.assertNotIn("agent line one", path.read_text(encoding="utf-8"))

    def test_keep_open_false_still_rotates(self) -> None:
        with mock.patch.object(sys, "stderr", None):
            path = setup_logging(self.log_dir, keep_open=False)
        self.file_handler().maxBytes = 300
        for i in range(20):
            logging.getLogger("briefing_reader.test").info("line %d with some padding text", i)
        self.assertTrue((self.log_dir / "briefing-reader.log.1").exists())
        self.assertTrue(path.exists())

    def test_works_without_stderr(self) -> None:
        with mock.patch.object(sys, "stderr", None):
            path = setup_logging(self.log_dir)
            self.assertEqual(len(self.own_handlers()), 1)
            logging.getLogger("briefing_reader.test").warning("works without a console")
            try:
                raise ValueError("no console boom")
            except ValueError:
                sys.excepthook(*sys.exc_info())
        text = self.read_log(path)
        self.assertIn("works without a console", text)
        self.assertIn("ValueError: no console boom", text)

    def test_stream_handler_writes_to_stderr(self) -> None:
        setup_logging(self.log_dir)
        logging.getLogger("briefing_reader.test").warning("to the console")
        self.assertIn("to the console", self.stderr.getvalue())

    def test_levels(self) -> None:
        setup_logging(self.log_dir)
        self.assertEqual(logging.getLogger().level, logging.INFO)
        for name in ("urllib3", "asyncio", "aiohttp", "comtypes"):
            self.assertEqual(logging.getLogger(name).level, logging.WARNING)
        setup_logging(self.log_dir, debug=True)
        self.assertEqual(logging.getLogger().level, logging.DEBUG)

    def test_google_sign_in_secrets_never_logged_under_debug(self) -> None:
        # requests_oauthlib logs the token request body (the authorization code) and the
        # obtained token at DEBUG, before Jarvis could register them for redaction.
        code, access, refresh = "4/0FAKE-AUTH-CODE-1234", "ya29.FAKE-ACCESS-5678", "1//FAKE-REFRESH-9012"
        path = setup_logging(self.log_dir, debug=True)
        logging.getLogger("requests_oauthlib.oauth2_session").debug("Supplying data code=%s", code)
        logging.getLogger("requests_oauthlib.oauth2_session").debug(
            "Obtained token {'access_token': '%s', 'refresh_token': '%s'}", access, refresh)
        logging.getLogger("oauthlib.oauth2.rfc6749").debug("Prepared %s", code)
        logging.getLogger("google_auth_oauthlib.flow").debug("token %s", access)
        logging.getLogger("google.auth.transport.requests").debug("refresh %s", refresh)
        logging.getLogger("googleapiclient.http").warning(
            "Sleeping 1 seconds before retry 1 of 2 for request: GET "
            "https://www.googleapis.com/calendar/v3/calendars/ana%%40example.edu/events")
        logging.getLogger("briefing_reader.test").debug("still debugging")
        text = self.read_log(path) + self.stderr.getvalue()
        for secret in (code, access, refresh, "ana%40example.edu"):
            self.assertNotIn(secret, text)
        self.assertIn("still debugging", text)
        for name in ("requests_oauthlib", "oauthlib", "google_auth_oauthlib", "google.auth"):
            self.assertEqual(logging.getLogger(name).getEffectiveLevel(), logging.WARNING, name)
        self.assertEqual(logging.getLogger("googleapiclient.discovery").getEffectiveLevel(), logging.CRITICAL)

    def test_secrets_redacted_in_file_for_library_loggers(self) -> None:
        register_secret(PLAIN_SECRET)
        path = setup_logging(self.log_dir)
        logging.getLogger("urllib3.connectionpool").warning("header %s", PLAIN_SECRET)
        text = self.read_log(path)
        self.assertIn(f"header {REDACTED}", text)
        self.assertNotIn(PLAIN_SECRET, text)
        self.assertNotIn(PLAIN_SECRET, self.stderr.getvalue())

    def test_default_directory(self) -> None:
        with mock.patch.object(config, "default_data_dir", return_value=self.tmp / "data"):
            path = setup_logging()
        self.assertTrue(_same_path(path, self.tmp / "data" / "logs" / "briefing-reader.log"))

    def test_falls_back_to_temp_when_directory_unusable(self) -> None:
        blocker = self.tmp / "blocker"
        blocker.write_text("a file, not a folder", encoding="utf-8")
        with mock.patch.object(config.tempfile, "gettempdir",
                               return_value=str(self.tmp / "temp")):
            path = setup_logging(blocker / "logs")
        expected = self.tmp / "temp" / "briefing-reader" / "logs" / "briefing-reader.log"
        self.assertTrue(_same_path(path, expected))
        self.assertIn("Could not use the log folder", self.read_log(path))

    def test_excepthook_logs_uncaught_exception(self) -> None:
        path = setup_logging(self.log_dir)
        self.assertIs(sys.excepthook, config._log_uncaught_exception)
        try:
            raise ValueError(f"boom in main {PLAIN_SECRET}")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        text = self.read_log(path)
        self.assertIn("CRITICAL briefing_reader.config [MainThread] Uncaught exception", text)
        self.assertIn("Traceback", text)
        self.assertIn(f"ValueError: boom in main {REDACTED}", text)
        self.assertNotIn(PLAIN_SECRET, text)

    def test_excepthook_passes_keyboard_interrupt_on(self) -> None:
        path = setup_logging(self.log_dir)
        with mock.patch.object(config, "_previous_excepthook") as previous:
            exc = KeyboardInterrupt()
            sys.excepthook(KeyboardInterrupt, exc, None)
        previous.assert_called_once_with(KeyboardInterrupt, exc, None)
        self.assertNotIn("KeyboardInterrupt", self.read_log(path))

    def test_excepthooks_installed_once(self) -> None:
        original = sys.excepthook
        setup_logging(self.log_dir)
        setup_logging(self.log_dir)
        self.assertIs(sys.excepthook, config._log_uncaught_exception)
        self.assertIs(config._previous_excepthook, original)

    def test_thread_excepthook_logs(self) -> None:
        path = setup_logging(self.log_dir)
        self.assertIs(threading.excepthook, config._log_uncaught_thread_exception)

        def crash() -> None:
            raise RuntimeError("boom in worker")

        def leave() -> None:
            raise SystemExit(0)

        for target, name in ((crash, "worker-crash"), (leave, "worker-exit")):
            thread = threading.Thread(target=target, name=name)
            thread.start()
            thread.join()
        text = self.read_log(path)
        self.assertIn("Uncaught exception in thread worker-crash", text)
        self.assertIn("RuntimeError: boom in worker", text)
        self.assertNotIn("worker-exit", text)



class AskEnabledEnvTests(ProjectTestCase):
    """JARVIS_ASK_ENABLED (from the gitignored .env) overrides [ask] enabled."""

    def test_off_by_default(self) -> None:
        self.assertFalse(self.load_quietly().ask.enabled)

    def test_env_turns_it_on_without_touching_config(self) -> None:
        self.write_config("[ask]\nenabled = false\n")
        for value in ("true", "1", "yes", "on", " TRUE ", '"true"'):
            with self.subTest(value=value):
                self.assertTrue(self.load_quietly(JARVIS_ASK_ENABLED=value).ask.enabled)

    def test_env_turns_it_off(self) -> None:
        self.write_config("[ask]\nenabled = true\n")
        for value in ("false", "0", "no", "off"):
            with self.subTest(value=value):
                self.assertFalse(self.load_quietly(JARVIS_ASK_ENABLED=value).ask.enabled)

    def test_empty_env_keeps_the_config_value(self) -> None:
        self.write_config("[ask]\nenabled = true\n")
        self.assertTrue(self.load_quietly(JARVIS_ASK_ENABLED="").ask.enabled)

    def test_bad_env_value_warns_and_keeps_the_config_value(self) -> None:
        self.write_config("[ask]\nenabled = false\n")
        cfg = self.load_warning("JARVIS_ASK_ENABLED", JARVIS_ASK_ENABLED="maybe")
        self.assertFalse(cfg.ask.enabled)


class ProductivityConfigTests(ProjectTestCase):
    """[productivity]: the bundle, off by default; JARVIS_PRODUCTIVITY_ENABLED overrides enabled."""

    def test_defaults(self) -> None:
        cfg = self.load_quietly()
        self.assertEqual(cfg.productivity, ProductivityConfig())
        d = cfg.productivity
        self.assertFalse(d.enabled)
        self.assertEqual((d.memory, d.memory_suggestions, d.max_memory, d.plan_offer, d.plan_offer_until),
                         (True, True, 100, True, "13:30"))
        self.assertEqual((d.day_start, d.day_end, d.plan_max_blocks, d.plan_min_free_minutes),
                         ("08:00", "22:00", 5, 60))
        self.assertEqual((d.prep_offers, d.prep_lead_minutes, d.prep_offers_per_day, d.prep_max_offer_guests,
                          d.prep_max_guests, d.prep_lookback_days, d.prep_other_accounts),
                         (True, 15, 8, 8, 5, 14, False))
        self.assertEqual(d.prep_skip_words, ("focus", "lunch", "commute", "gym", "break", "hold", "ooo",
                                             "out of office"))
        self.assertEqual((d.prep_speak_minimized, d.minimized_check, d.followups, d.scan_days, d.scan_every_minutes,
                          d.scan_max_threads), (True, True, True, 14, 30, 150))
        self.assertEqual((d.waiting_after_hours, d.owed_after_days, d.followups_shown, d.quiet_from, d.quiet_until,
                          d.max_runs_per_day, d.reserve_ask_runs), (20, 3, 6, "22:00", "08:00", 20, 3))
        self.assertEqual(set(config.PRODUCTIVITY_RANGES) | set(config.PRODUCTIVITY_TIMES) | {
            "enabled", "memory", "memory_suggestions", "plan_offer", "prep_offers", "prep_other_accounts",
            "prep_skip_words", "prep_speak_minimized", "minimized_check", "followups"},
            {f.name for f in dataclasses.fields(ProductivityConfig)})

    def test_values_are_read(self) -> None:
        self.write_config("[productivity]\nenabled = true\nmemory = false\nmax_memory = 50\n"
                          'plan_offer_until = "12:15"\nday_start = "7:30"\nday_end = "20:00"\n'
                          'prep_skip_words = ["Focus", "standup", "STANDUP", " Out  of office "]\n'
                          "prep_lookback_days = 7\nscan_days = 10\nreserve_ask_runs = 0\n")
        d = self.load_quietly().productivity
        self.assertTrue(d.enabled)
        self.assertFalse(d.memory)
        self.assertEqual((d.max_memory, d.plan_offer_until, d.day_start, d.day_end), (50, "12:15", "07:30", "20:00"))
        self.assertEqual(d.prep_skip_words, ("focus", "standup", "out of office"))
        self.assertEqual((d.prep_lookback_days, d.scan_days, d.reserve_ask_runs), (7, 10, 0))

    def test_ranges_and_bad_values_fall_back(self) -> None:
        self.write_config("[productivity]\nmax_memory = 5\nplan_max_blocks = 99\nprep_lead_minutes = -3\n"
                          'memory = "yes"\nquiet_from = "late"\nfollowups_shown = 0\nscan_max_threads = 1000\n'
                          "surprise = 1\n")
        d = self.load_warning("productivity.max_memory", "productivity.plan_max_blocks", "productivity.memory",
                              "productivity.quiet_from", "productivity.surprise").productivity
        self.assertEqual((d.max_memory, d.plan_max_blocks, d.prep_lead_minutes, d.memory, d.quiet_from),
                         (10, 8, 15, True, "22:00"))
        self.assertEqual((d.followups_shown, d.scan_max_threads), (1, 300))

    def test_a_day_of_an_hour_or_less_falls_back(self) -> None:
        for start, end in (("09:00", "10:00"), ("18:00", "08:00"), ("12:00", "12:00")):
            with self.subTest(start=start, end=end):
                self.write_config(f'[productivity]\nday_start = "{start}"\nday_end = "{end}"\n')
                d = self.load_warning("productivity.day_end").productivity
                self.assertEqual((d.day_start, d.day_end), ("08:00", "22:00"))
        self.write_config('[productivity]\nday_start = "09:00"\nday_end = "10:01"\n')
        self.assertEqual(self.load_quietly().productivity.day_end, "10:01")

    def test_prep_lookback_never_past_the_scan_window(self) -> None:
        self.write_config("[productivity]\nprep_lookback_days = 14\nscan_days = 5\n")
        d = self.load_warning("productivity.prep_lookback_days").productivity
        self.assertEqual((d.prep_lookback_days, d.scan_days), (5, 5))
        self.write_config("[productivity]\nprep_lookback_days = 30\n")
        self.assertEqual(self.load_warning("productivity.prep_lookback_days").productivity.prep_lookback_days, 14)

    def test_env_override_both_ways(self) -> None:
        self.write_config("[productivity]\nenabled = false\n")
        for value in ("true", "1", "yes", "on", " TRUE "):
            with self.subTest(value=value):
                self.assertTrue(self.load_quietly(JARVIS_PRODUCTIVITY_ENABLED=value).productivity.enabled)
        self.write_config("[productivity]\nenabled = true\n")
        for value in ("false", "0", "no", "off"):
            with self.subTest(value=value):
                self.assertFalse(self.load_quietly(JARVIS_PRODUCTIVITY_ENABLED=value).productivity.enabled)
        self.assertTrue(self.load_quietly(JARVIS_PRODUCTIVITY_ENABLED="").productivity.enabled)
        self.assertTrue(self.load_warning("JARVIS_PRODUCTIVITY_ENABLED",
                                          JARVIS_PRODUCTIVITY_ENABLED="maybe").productivity.enabled)
        self.assertEqual(config.PRODUCTIVITY_ENABLED_ENV, "JARVIS_PRODUCTIVITY_ENABLED")

    def test_the_new_files_are_gitignored(self) -> None:
        lines = [line.strip() for line in (config.PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
        for name in ("/memory.json", "/followups.json", "/prep_series.json"):
            self.assertIn(name, lines)


class DesktopConfigTests(ProjectTestCase):
    """[desktop]: Jarvis on your desktop, off by default; JARVIS_DESKTOP_ENABLED overrides enabled; the HUD key is
    never the window key."""

    def test_defaults(self) -> None:
        d = self.load_quietly().desktop
        self.assertEqual(d, config.DesktopConfig())
        self.assertFalse(d.enabled)
        self.assertEqual(d.folders, ("desktop", "documents", "downloads", "pictures", "music", "videos"))
        self.assertEqual((d.extra_folders, d.hide_from_capture, d.screenshots_dir, d.undo_days, d.hud_key, d.apps),
                         ((), True, "", 7, "ctrl+alt+h", False))
        self.assertIn("desktop", config._KNOWN_TABLES)

    def test_a_whole_table(self) -> None:
        self.write_config(
            "[desktop]\nenabled = true\nfolders = [\"Documents\", \"downloads\"]\n"
            "extra_folders = ['C:\\Users\\Owner\\Uni', 'D:/Coursework']\nhide_from_capture = false\n"
            "screenshots_dir = 'C:\\Users\\Owner\\Shots'\nundo_days = 14\nhud_key = \"Alt + Ctrl + D\"\napps = true\n")
        d = self.load_quietly().desktop
        self.assertEqual(d, config.DesktopConfig(
            enabled=True, folders=("documents", "downloads"),
            extra_folders=("C:\\Users\\Owner\\Uni", "D:/Coursework"), hide_from_capture=False,
            screenshots_dir="C:\\Users\\Owner\\Shots", undo_days=14, hud_key="ctrl+alt+d", apps=True))

    def test_bad_values_fall_back_and_the_warnings_name_no_folder(self) -> None:
        self.write_config(
            "[desktop]\nenabled = \"yes\"\nfolders = [\"documents\", \"AppData\", \"documents\", 3]\n"
            "extra_folders = ['Uni', '\\\\server\\share\\Owner', 'C:Secret', 'C:\\Users\\Owner\\Uni']\n"
            "screenshots_dir = 'Shots'\nundo_days = 99\nhud_key = \"j\"\napps = 1\n")
        with self.assertLogs(CONFIG_LOGGER, level="WARNING") as captured:
            d = self.load().desktop
        text = "\n".join(captured.output)
        for fragment in ("desktop.enabled", "desktop.folders entry 'AppData'", "desktop.extra_folders entry 1",
                         "desktop.extra_folders entry 2", "desktop.extra_folders entry 3", "desktop.screenshots_dir",
                         "desktop.undo_days", "desktop.hud_key", "desktop.apps"):
            self.assertIn(fragment, text)
        for private in ("Owner", "server", "Secret", "Shots"):          # folders are named by their number only
            self.assertNotIn(private, text)
        self.assertEqual(d, config.DesktopConfig(folders=("documents",), extra_folders=("C:\\Users\\Owner\\Uni",),
                                                 undo_days=30))
        self.write_config("[desktop]\nundo_days = 0\n")
        self.assertEqual(self.load_warning("desktop.undo_days").desktop.undo_days, 1)
        self.write_config("[desktop]\nfolders = []\n")
        self.assertEqual(self.load_quietly().desktop.folders, ())

    def test_the_hud_key_is_never_the_window_key(self) -> None:
        self.write_config('[desktop]\nhud_key = "ctrl+alt+j"\n')               # the [hotkey] default
        self.assertEqual(self.load_warning("desktop.hud_key", "window key").desktop.hud_key, "ctrl+alt+h")
        self.write_config('[hotkey]\ncombo = "ctrl+alt+h"\n')                 # the window key took H: no HUD key
        cfg = self.load_warning("desktop.hud_key", "the Dock's Desktop still switches")
        self.assertEqual((cfg.hotkey.combo, cfg.desktop.hud_key), ("ctrl+alt+h", ""))
        self.write_config('[hotkey]\ncombo = "Ctrl+Shift+F9"\n[desktop]\nhud_key = "shift + ctrl + F9"\n')
        cfg = self.load_warning("desktop.hud_key")
        self.assertEqual(cfg.desktop.hud_key, "ctrl+alt+h")
        self.write_config('[hotkey]\ncombo = "ctrl+alt+k"\n[desktop]\nhud_key = "ctrl+alt+j"\n')
        self.assertEqual(self.load_quietly().desktop.hud_key, "ctrl+alt+j")    # J is free once the window key moved

    def test_env_override_both_ways(self) -> None:
        self.write_config("[desktop]\nenabled = false\n")
        for value in ("true", "1", "yes", "on", " TRUE "):
            with self.subTest(value=value):
                self.assertTrue(self.load_quietly(JARVIS_DESKTOP_ENABLED=value).desktop.enabled)
        self.write_config("[desktop]\nenabled = true\n")
        for value in ("false", "0", "no", "off"):
            with self.subTest(value=value):
                self.assertFalse(self.load_quietly(JARVIS_DESKTOP_ENABLED=value).desktop.enabled)
        self.assertTrue(self.load_quietly(JARVIS_DESKTOP_ENABLED="").desktop.enabled)
        self.assertTrue(self.load_warning("JARVIS_DESKTOP_ENABLED", JARVIS_DESKTOP_ENABLED="maybe").desktop.enabled)
        self.assertEqual(config.DESKTOP_ENABLED_ENV, "JARVIS_DESKTOP_ENABLED")

    def test_its_repr_names_no_folder(self) -> None:
        d = config.DesktopConfig(extra_folders=("C:\\Users\\Owner\\Uni",), screenshots_dir="C:\\Users\\Owner\\Shots")
        self.assertNotIn("Owner", repr(d))

    def test_the_folders_are_the_desktop_roots(self) -> None:
        from briefing_reader.desktop import backend

        self.assertEqual(config.DesktopConfig().folders, backend.ROOT_FOLDERS)
        self.assertTrue(set(backend.ROOT_FOLDERS) <= set(backend.KNOWN_FOLDERS))


if __name__ == "__main__":
    unittest.main()
